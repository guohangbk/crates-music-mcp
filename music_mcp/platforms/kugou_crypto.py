"""酷狗 WebAPI 的签名与加密 —— 纯标准库实现，零第三方依赖。

协议来源：`kugou_music_api.dll` 里**明文内嵌的 JS**（webpack 压缩、未加密），
逐字对照 https://github.com/MakcRe/KuGouMusicApi （MIT）—— DLL 里那份就是它的压缩版
（已比对：同一个签名盐值、appid、baseURL、encryptType）。对应文件 `util/helper.js`
（签名）、`util/crypto.js`（AES/RSA）、`util/config.json`（常量）。

三套签名（都对"排序后的 k=v 串"取 md5）：

    android（默认，最常用）: md5(SALT + 排序参数串 + body + SALT)
    web（登录二维码那几条）: md5('NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt' + 排序参数串 + body + 盐)
    register（设备注册）    : md5('1014' + 排序后的**值**串 + '1014')

两种 RSA：

    rsa_raw_hex   ：裸 RSA（无填充）+ 左侧补零到 128 字节 —— 登录里 `pk` 用这个
    rsa_pkcs1_hex ：RSAES-PKCS1-v1_5 —— 歌单云服务里的会话串 `p` 用这个

AES 的三处用法（都在下面封好了）：

    random16 → key=md5(temp)[0:32]（32 字节 = AES-256）、iv=key[-16:] → hex 密文
    playlist → key=md5(random6)[0:16]、iv=md5(random6)[16:32] → base64 密文
    login    → 上面两种的组合（t1/t2 用固定 key，params 用随机 key）
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import secrets
from typing import Any

from . import aes as _aes

__all__ = [
    "APPID", "CLIENTVER", "SRCAPPID", "SALT_ANDROID", "SALT_WEB",
    "md5_hex", "signature_android", "signature_web", "signature_register",
    "rsa_raw_hex", "rsa_pkcs1_hex",
    "aes_with_random_key", "aes_decrypt_with_key",
    "playlist_encrypt", "playlist_decrypt",
    "device_identity", "random_string", "random_number",
]

# --------------------------------------------------------------------------
# 常量（逐字来自上游 util/config.json 与 util/helper.js）
# --------------------------------------------------------------------------

APPID = 1005
CLIENTVER = 20489
SRCAPPID = 2919
SALT_ANDROID = "OIlwieks28dk2k092lksi2UIkp"
SALT_WEB = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"
SALT_REGISTER = "1014"
DEFAULT_MAC = "02:00:00:00:00:00"
UA_ANDROID = "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi"

KUGOU_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDIAG7QOELSYoIJvTFJhMpe1s/gbjDJX51HBNnEl5HXqTW6lQ7LC8jr9fWZTwusknp+sVGzwd40MwP6U5yDE27M/X1+UR4tvOGOqp94TJtQ1EPnWGWXngpeIW5GxoQGao1rmYWAu6oi1z9XkChrsUdC6DJE5E221wf/4WLFxwAtRQIDAQAB
-----END PUBLIC KEY-----"""

# 固定 key 的 AES（登录 t1 / t2，只有概念版 lite 会用 t2，标准版传 0）
LITE_T2_KEY = "fd14b35e3f81af3817a20ae7adae7020"
LITE_T2_IV = "17a20ae7adae7020"
LITE_T1_KEY = "5e4ef500e9597fe004bd09a46d8add98"
LITE_T1_IV = "04bd09a46d8add98"


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def md5_hex(data: Any) -> str:
    if isinstance(data, (dict, list)):
        data = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.md5(data).hexdigest()


def random_string(length: int = 16) -> str:
    """上游 randomString：字符池 '1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ'（大写+数字）。"""
    pool = "1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return "".join(random.choice(pool) for _ in range(length))


def random_number(length: int = 16) -> str:
    return "".join(random.choice("1234567890") for _ in range(length))


def _params_string(params: dict[str, Any], with_equal: bool = True) -> str:
    """按 key 排序拼串；值若是对象先 JSON 化（上游行为）。"""
    parts: list[str] = []
    for key in sorted(params.keys()):
        value = params[key]
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        parts.append(f"{key}={value}" if with_equal else f"{key}{value}")
    return "".join(parts)


def signature_android(params: dict[str, Any], data: str | bytes = "") -> str:
    body = data.decode("utf-8") if isinstance(data, bytes) else (data or "")
    return md5_hex(f"{SALT_ANDROID}{_params_string(params)}{body}{SALT_ANDROID}")


def signature_web(params: dict[str, Any], data: str | bytes = "") -> str:
    body = data.decode("utf-8") if isinstance(data, bytes) else (data or "")
    return md5_hex(f"{SALT_WEB}{_params_string(params)}{body}{SALT_WEB}")


def signature_register(params: dict[str, Any]) -> str:
    values = sorted(str(value) for value in params.values())
    return md5_hex(f"{SALT_REGISTER}{''.join(values)}{SALT_REGISTER}")


def sign_params_key(data: str | int, appid: int = APPID, clientver: int = CLIENTVER) -> str:
    """上游 signParamsKey：md5(appid + 盐 + clientver + data)。

    登录（`key` 字段）与删歌单（`key` 参数）都用它；`data` 通常是 clienttime。
    """
    return md5_hex(f"{appid}{SALT_ANDROID}{clientver}{data}")


# --------------------------------------------------------------------------
# RSA（复用网易云那套 DER 解析：都是 SPKI PEM）
# --------------------------------------------------------------------------

def _public_key(pem: str = KUGOU_PUBLIC_KEY_PEM) -> tuple[int, int, int]:
    from .ncm_crypto import _read_tlv  # 同一个 SPKI 解析，不重复造

    body = "".join(line.strip() for line in pem.splitlines() if "-----" not in line)
    der = base64.b64decode(body)
    _, spki, _ = _read_tlv(der, 0)
    _, _, pos = _read_tlv(spki, 0)
    _, bit_string, _ = _read_tlv(spki, pos)
    _, rsa_key, _ = _read_tlv(bit_string[1:], 0)
    _, n_bytes, pos = _read_tlv(rsa_key, 0)
    _, e_bytes, _ = _read_tlv(rsa_key, pos)
    n = int.from_bytes(n_bytes, "big")
    e = int.from_bytes(e_bytes, "big")
    return n, e, (n.bit_length() + 7) // 8


def _as_bytes(data: Any) -> bytes:
    """上游 normalizeBuffer：对象先 JSON.stringify（紧凑、不转义非 ASCII）。"""
    if isinstance(data, (dict, list)):
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return data.encode("utf-8") if isinstance(data, str) else bytes(data)


def rsa_raw_hex(data: bytes | str | dict, pem: str = KUGOU_PUBLIC_KEY_PEM) -> str:
    """裸 RSA（无填充）+ **右**补零到 128 字节，输出小写 hex。

    注意方向和网易云那套相反：上游是 `padded = new Uint8Array(keyLength); padded.set(buffer)`
    —— 明文放在**前面**（高位），零补在后面。补错方向算出来的密文服务端解不开。
    （2026-11-07 拿上游 JS 的对拍值比对时发现的，见 tests/test_kugou_native.py。）
    """
    n, e, size = _public_key(pem)
    raw = _as_bytes(data)
    if len(raw) > size:
        raise ValueError("RSA 明文超出密钥长度")
    value = int.from_bytes(raw.ljust(size, b"\x00"), "big")
    return format(pow(value, e, n), "x").zfill(size * 2)


def rsa_pkcs1_hex(data: bytes | str | dict, pem: str = KUGOU_PUBLIC_KEY_PEM) -> str:
    """RSAES-PKCS1-v1_5（forge 的 encrypt(..., 'RSAES-PKCS1-V1_5')）。"""
    n, e, size = _public_key(pem)
    raw = _as_bytes(data)
    if len(raw) > size - 11:
        raise ValueError("RSA 明文超出密钥长度")
    padding = bytearray()
    while len(padding) < size - 3 - len(raw):
        byte = secrets.randbelow(256)
        if byte:                       # PKCS#1 要求填充字节非 0
            padding.append(byte)
    block = b"\x00\x02" + bytes(padding) + b"\x00" + raw
    return format(pow(int.from_bytes(block, "big"), e, n), "x").zfill(size * 2)


# --------------------------------------------------------------------------
# AES 三种用法
# --------------------------------------------------------------------------

def aes_cbc_hex(data: str | bytes, key: str, iv: str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return _aes.aes_cbc_encrypt(_aes.pkcs7_pad(raw), key.encode(), iv.encode()).hex()


def aes_cbc_dehex(cipher_hex: str, key: str, iv: str) -> str:
    raw = _aes.aes_cbc_decrypt(bytes.fromhex(cipher_hex), key.encode(), iv.encode())
    return _aes.pkcs7_unpad(raw).decode("utf-8", "replace")


def aes_with_random_key(data: Any) -> dict[str, str]:
    """上游 cryptoAesEncrypt（不传 key）：随机 16 位 → md5 前 32 位当 key（AES-256）。"""
    if isinstance(data, (dict, list)):
        data = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    temp_key = random_string(16).lower()
    key = md5_hex(temp_key)[:32]
    iv = key[-16:]
    return {"str": aes_cbc_hex(data, key, iv), "key": temp_key}


def aes_with_fixed_key(data: Any, key: str, iv: str) -> str:
    if isinstance(data, (dict, list)):
        data = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return aes_cbc_hex(data, key, iv)


def aes_decrypt_with_key(cipher_hex: str, key: str) -> Any:
    """上游 cryptoAesDecrypt（不传 iv）：key = md5(key)[0:32]，iv = key[-16:]。"""
    full = md5_hex(key)[:32]
    text = aes_cbc_dehex(cipher_hex, full, full[-16:])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def playlist_encrypt(data: Any) -> dict[str, str]:
    """上游 playlistAesEncrypt：随机 6 位作会话串，md5 前 16/后 16 位当 key/iv，密文 base64。"""
    if isinstance(data, (dict, list)):
        data = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    key = random_string(6).lower()
    digest = md5_hex(key)
    cipher = _aes.aes_cbc_encrypt(_aes.pkcs7_pad(data.encode()), digest[:16].encode(),
                                  digest[16:32].encode())
    return {"key": key, "str": base64.b64encode(cipher).decode()}


def playlist_decrypt(cipher_b64: str, key: str) -> Any:
    digest = md5_hex(key)
    raw = _aes.aes_cbc_decrypt(base64.b64decode(cipher_b64), digest[:16].encode(),
                               digest[16:32].encode())
    text = _aes.pkcs7_unpad(raw).decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


# --------------------------------------------------------------------------
# 设备指纹（上游 server.js：guid = md5(uuid)、mid = 十进制大整数、dev/mac 固定形态）
# --------------------------------------------------------------------------

def calculate_mid(guid: str) -> str:
    """上游 util.js 的 calculateMid：md5(guid) 当 16 进制大整数转十进制字符串。"""
    return str(int(md5_hex(guid), 16))


def device_identity(cookie: dict[str, str]) -> dict[str, str]:
    """补齐 dfid / mid / guid / dev / mac。

    我们的 Cookie 里通常只有 token + userid（扫码登录回的），没有设备字段；
    上游是每次启动现生成一套。这里**从 token/userid 派生**，保证同一份凭证每次算出来
    一样（服务端看到的是同一个设备，而不是每次换一个）。
    """
    seed = f"{cookie.get('token', '')}|{cookie.get('userid', '')}"
    guid = md5_hex(f"kugou-device|{seed}")
    out = {
        "dfid": cookie.get("dfid") or cookie.get("KUGOU_API_DFID") or "-",
        "mid": str(cookie.get("KUGOU_API_MID") or cookie.get("mid") or calculate_mid(guid)),
        "guid": str(cookie.get("KUGOU_API_GUID") or guid),
        "dev": str(cookie.get("KUGOU_API_DEV") or md5_hex(f"kugou-dev|{seed}")[:10]).upper(),
        "mac": str(cookie.get("KUGOU_API_MAC") or DEFAULT_MAC).upper(),
    }
    return out
