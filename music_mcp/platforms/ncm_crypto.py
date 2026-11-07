"""网易云 WebAPI 的参数加密：weapi / eapi —— 纯标准库实现，零第三方依赖。

为什么要自己写：`pymusiclibrary` 的 manylinux wheel 打错了包（里面是 Windows DLL，
没有 Linux .so），Linux 上根本加载不了；上游没有新版可换。与其等它修，不如把
「加密 + HTTP 调用」直接用 Python 实现一遍 —— 协议本身是公开且稳定的。

协议来源不是猜的，是从 `ncm_music_api.dll` 里内嵌的 JS 源码抠出来的（同一份
QuickJS bundle 里就有 `var weapi = function weapi(object) {...}`），所以这里的
每个常量、每处参数拼接都和官方引擎逐字对应：

    iv            = "0102030405060708"
    presetKey     = "0CoJUm6Qyw8W8jud"
    eapiKey       = "e82ckenh8dichen8"
    publicKey     = SPKI PEM（1024-bit RSA，e = 65537）

weapi（老接口，music.163.com /weapi/...）::

    text      = JSON.stringify(obj)
    secretKey = base62 随机取 16 个字符
    params    = base64(AES-CBC(base64(AES-CBC(text, presetKey, iv)), secretKey, iv))
    encSecKey = hex(RSA(secretKey 反转后的字节, publicKey))   # 左侧补零到 128 字节

eapi（新接口，interface.music.163.com /eapi/...）::

    digest = md5("nobody" + uri + "use" + text + "md5forencrypt")
    data   = uri + "-36cd479b6b5-" + text + "-36cd479b6b5-" + digest
    params = hex(AES-ECB(data, eapiKey))

注意：weapi 里 secretKey **只反转一次**（引擎源码 `secretKey.split('').reverse().join('')`），
反转后按 UTF-8 字节当大整数做裸 RSA（RSAES-PKCS1 都不要，就是 forge 的 `encrypt(str, 'NONE')`）。
网上有些 Python 实现在这里反转两次，那样 encSecKey 是错的，登录一定失败。

AES 本体（128/192/256、CBC/ECB、PKCS7）已抽到 `aes.py`，与酷狗共用；这里只保留
网易云自己的常量与两种参数封装。
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Any

from . import aes as _aes

# 兼容旧调用点：这几个名字历史上挂在 ncm_crypto 上（测试也在用）
from .aes import SBOX, INV_SBOX  # noqa: F401
from .aes import _gmul  # noqa: F401
from .aes import pkcs7_pad as _pkcs7_pad  # noqa: F401
from .aes import pkcs7_unpad as _pkcs7_unpad  # noqa: F401

__all__ = [
    "PRESET_KEY", "IV", "EAPI_KEY", "PUBLIC_KEY_PEM",
    "aes_cbc_encrypt_b64", "aes_ecb_encrypt_hex",
    "aes_ecb_decrypt_hex", "aes_cbc_decrypt_b64",
    "rsa_public_key", "rsa_encrypt_hex", "weapi", "eapi", "eapi_decrypt_response",
]

# --------------------------------------------------------------------------
# 常量（逐字来自引擎内嵌 JS）
# --------------------------------------------------------------------------

IV = b"0102030405060708"
PRESET_KEY = b"0CoJUm6Qyw8W8jud"
EAPI_KEY = b"e82ckenh8dichen8"
BASE62 = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
EAPI_SEP = "-36cd479b6b5-"

PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDgtQn2JZ34ZC28NWYpAUd98iZ37BUrX/aKzmFbt7clFSs6sXqHauqKWqdtLkF2KexO40H1YTX8z2lSgBBOAxLsvaklV8k4cBFK9snQXE9/DDaFt6Rr7iVZMldczhC0JNgTz+SHXT6CBHuX3e9SdB1Ua44oncaTWz7OBGLbCiK45wIDAQAB
-----END PUBLIC KEY-----"""


# --------------------------------------------------------------------------
# AES：薄封装（本体在 aes.py，酷狗共用）
# --------------------------------------------------------------------------

def aes_ecb_encrypt(data: bytes, key: bytes = EAPI_KEY) -> bytes:
    return _aes.aes_ecb_encrypt(data, key)


def aes_ecb_decrypt(data: bytes, key: bytes = EAPI_KEY) -> bytes:
    return _aes.aes_ecb_decrypt(data, key)


def aes_cbc_encrypt(data: bytes, key: bytes, iv: bytes = IV) -> bytes:
    return _aes.aes_cbc_encrypt(data, key, iv)


def aes_cbc_decrypt(data: bytes, key: bytes, iv: bytes = IV) -> bytes:
    return _aes.aes_cbc_decrypt(data, key, iv)


def aes_cbc_encrypt_b64(text: str | bytes, key: bytes, iv: bytes = IV) -> str:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return base64.b64encode(aes_cbc_encrypt(_pkcs7_pad(raw), key, iv)).decode()


def aes_cbc_decrypt_b64(cipher: str, key: bytes, iv: bytes = IV) -> str:
    return _pkcs7_unpad(aes_cbc_decrypt(base64.b64decode(cipher), key, iv)).decode("utf-8")


def aes_ecb_encrypt_hex(text: str | bytes, key: bytes = EAPI_KEY) -> str:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return aes_ecb_encrypt(_pkcs7_pad(raw), key).hex().upper()


def aes_ecb_decrypt_hex(cipher_hex: str, key: bytes = EAPI_KEY) -> str:
    return _pkcs7_unpad(aes_ecb_decrypt(bytes.fromhex(cipher_hex), key)).decode("utf-8")


# --------------------------------------------------------------------------
# RSA：从 SPKI PEM 现取模数与指数（不硬编码两百多位的十六进制，免得抄错）
# --------------------------------------------------------------------------

def _read_tlv(data: bytes, pos: int) -> tuple[int, bytes, int]:
    tag = data[pos]
    pos += 1
    length = data[pos]
    pos += 1
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    return tag, data[pos:pos + length], pos + length


def rsa_public_key(pem: str = PUBLIC_KEY_PEM) -> tuple[int, int]:
    """返回 (n, e)。结构是 SubjectPublicKeyInfo → BIT STRING → RSAPublicKey。"""
    body = "".join(line.strip() for line in pem.splitlines() if "-----" not in line)
    der = base64.b64decode(body)
    _, spki, _ = _read_tlv(der, 0)
    _, _, pos = _read_tlv(spki, 0)                 # AlgorithmIdentifier
    _, bit_string, _ = _read_tlv(spki, pos)
    _, rsa_key, _ = _read_tlv(bit_string[1:], 0)   # 跳过 unused-bits 字节
    _, n_bytes, pos = _read_tlv(rsa_key, 0)
    _, e_bytes, _ = _read_tlv(rsa_key, pos)
    return int.from_bytes(n_bytes, "big"), int.from_bytes(e_bytes, "big")


def rsa_encrypt_hex(message: bytes) -> str:
    """裸 RSA（无填充）+ 左侧补零到 128 字节，等价 forge 的 encrypt(str, 'NONE')。"""
    n, e = rsa_public_key()
    value = int.from_bytes(message, "big")
    return format(pow(value, e, n), "x").zfill(256)


# --------------------------------------------------------------------------
# weapi / eapi
# --------------------------------------------------------------------------

def _dumps(obj: Any) -> str:
    # 引擎用 JSON.stringify：没有空格、非 ASCII 不转义
    if isinstance(obj, str):
        return obj
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def weapi(obj: Any) -> dict[str, str]:
    """返回可直接当 form data 提交的 {"params", "encSecKey"}。"""
    text = _dumps(obj)
    secret_key = "".join(secrets.choice(BASE62) for _ in range(16))
    params = aes_cbc_encrypt_b64(aes_cbc_encrypt_b64(text, PRESET_KEY, IV),
                                 secret_key.encode(), IV)
    return {"params": params,
            "encSecKey": rsa_encrypt_hex(secret_key[::-1].encode("utf-8"))}


def eapi(uri: str, obj: Any) -> dict[str, str]:
    """uri 用 /api/... 形式（和引擎一致），返回 {"params": <HEX>}。"""
    text = _dumps(obj)
    digest = hashlib.md5(f"nobody{uri}use{text}md5forencrypt".encode()).hexdigest()
    return {"params": aes_ecb_encrypt_hex(f"{uri}{EAPI_SEP}{text}{EAPI_SEP}{digest}")}


def eapi_decrypt_response(params_hex: str) -> Any:
    """eapi 请求 e_r=true 时响应的解密（引擎里的 eapiResDecrypt）。"""
    return json.loads(aes_ecb_decrypt_hex(params_hex))
