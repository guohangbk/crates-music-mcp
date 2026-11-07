"""网易云 native 后端的离线自检 + 联网冒烟。

离线部分（默认跑）不需要网络，也不碰凭证：
  * AES-128 对 FIPS-197 / NIST SP 800-38A 官方向量；
  * S 盒是自生成的，检查它是双射（生成算法抄错立刻暴露）；
  * RSA 公钥解析（SPKI PEM → n/e），模数十六进制与官方引擎里的那份逐字节一致；
  * weapi/eapi 输出的结构性不变量。

联网部分（`NCM_LIVE=1` 时才跑）：
  * 未登录就能打的 `/api/cloudsearch/pc`（eapi）—— 参数加密对不对，一次就能验出来；
  * 有本地 Cookie 时再验 `/api/w/nuser/account/get`（weapi）、歌单、日推。

跑法：
    python tests/test_ncm_native.py            # 只跑离线
    NCM_LIVE=1 python tests/test_ncm_native.py # 带上联网冒烟
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from music_mcp.platforms.core import ncm_crypto as C  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"[{'ok' if condition else 'FAIL'}] {name}{'  ' + detail if detail else ''}")


def test_aes_fips197() -> None:
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    cipher = C.aes_ecb_encrypt(plain, key)
    check("AES-128 ECB FIPS-197 向量", cipher.hex() == "69c4e0d86a7b0430d8cdb78070b4c55a",
          cipher.hex())


def test_aes_cbc_nist() -> None:
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a"
                          "ae2d8a571e03ac9c9eb76fac45af8e51"
                          "30c81c46a35ce411e5fbc1191a0a52ef"
                          "f69f2445df4f9b17ad2b417be66c3710")
    want = ("7649abac8119b246cee98e9b12e9197d"
            "5086cb9b507219ee95db113a917678b2"
            "73bed6b8e3c1743b7116e69e22229516"
            "3ff1caa1681fac09120eca307586e1a7")
    got = C.aes_cbc_encrypt(plain, key, iv).hex()
    check("AES-128 CBC NIST SP800-38A 向量", got == want, got)
    check("AES CBC 解密回原文",
          C.aes_cbc_decrypt(bytes.fromhex(want), key, iv) == plain)


def test_sbox_and_helpers() -> None:
    check("S 盒是双射", sorted(C.SBOX) == list(range(256)))
    check("逆 S 盒一致", all(C.INV_SBOX[C.SBOX[i]] == i for i in range(256)))
    check("GF 乘法 gmul(0x57,0x83)=0xc1", C._gmul(0x57, 0x83) == 0xc1)
    check("PKCS7 往返", C._pkcs7_unpad(C._pkcs7_pad(b"abc")) == b"abc")


def test_rsa_key() -> None:
    import base64

    n, e = C.rsa_public_key()
    check("RSA 指数 = 65537", e == 65537, str(e))
    check("RSA 模数 1024 位", n.bit_length() == 1024, str(n.bit_length()))
    modulus_hex = format(n, "x")
    # 与 DLL 内嵌 JS 的公钥逐字节一致（DER 里那段 00e0b509f6... 就是它）
    check("模数首段与官方引擎一致",
          modulus_hex.startswith("e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b72515"),
          modulus_hex[:48])
    check("模数末段与官方引擎一致",
          modulus_hex.endswith("52741d546b8e289dc6935b3ece0462db0a22b8e7"), modulus_hex[-20:])
    # 交叉验证：不走 DER 解析器，直接在原始字节里找 INTEGER(129B) 那一段
    body = "".join(line.strip() for line in C.PUBLIC_KEY_PEM.splitlines() if "-----" not in line)
    der = base64.b64decode(body)
    marker = bytes.fromhex("028181" + "00") + bytes.fromhex(modulus_hex[:16])
    offset = der.find(marker)
    check("DER 原始字节里能找到同一段模数", offset > 0, f"offset={offset}")
    if offset > 0:
        found = der[offset + 4: offset + 4 + 128].hex()
        check("DER 手抠模数 == 解析器结果", found == modulus_hex)


def test_weapi_shape() -> None:
    payload = C.weapi({"s": "周杰伦", "type": 1})
    check("weapi 出 params/encSecKey",
          set(payload) == {"params", "encSecKey"}, str(list(payload)))
    check("encSecKey 是 256 位十六进制",
          len(payload["encSecKey"]) == 256 and all(c in "0123456789abcdef" for c in payload["encSecKey"]))
    n, _ = C.rsa_public_key()
    check("encSecKey < 模数", int(payload["encSecKey"], 16) < n)
    import base64
    raw = base64.b64decode(payload["params"])
    check("params 是 16 字节整数倍", len(raw) % 16 == 0 and len(raw) >= 32)
    check("两次 weapi 的 params 不同（secretKey 随机）",
          payload["params"] != C.weapi({"s": "周杰伦", "type": 1})["params"])


def test_aes_roundtrip_utf8() -> None:
    text = '{"s":"周杰伦 Jay","limit":5}'
    secret = "aB3xY9zQ0pLmN2vR"
    inner = C.aes_cbc_encrypt_b64(text, C.PRESET_KEY)
    outer = C.aes_cbc_encrypt_b64(inner, secret.encode())
    back = C.aes_cbc_decrypt_b64(outer, secret.encode())
    check("weapi 两层 AES 可原路解回", C.aes_cbc_decrypt_b64(back, C.PRESET_KEY) == text)


def test_eapi_shape() -> None:
    payload = C.eapi("/api/cloudsearch/pc", {"s": "test", "type": 1})
    check("eapi 只有 params", set(payload) == {"params"})
    raw = bytes.fromhex(payload["params"])
    check("eapi params 十六进制且 16 字节对齐", len(payload["params"]) % 32 == 0 and len(raw) % 16 == 0)
    plain = C.aes_ecb_decrypt_hex(payload["params"])
    check("eapi 明文带 -36cd479b6b5- 分隔与 md5 摘要",
          plain.count(C.EAPI_SEP) == 2 and plain.startswith("/api/cloudsearch/pc" + C.EAPI_SEP))
    # 摘要字段必须等于 md5("nobody"+uri+"use"+json+"md5forencrypt")
    import hashlib
    _, body, digest = plain.split(C.EAPI_SEP)
    want = hashlib.md5(f"nobody/api/cloudsearch/pcuse{body}md5forencrypt".encode()).hexdigest()
    check("eapi 摘要算法与引擎一致", digest == want)


async def live_smoke() -> None:
    from music_mcp.platforms.core.ncm_native import NcmNativeClient

    client = NcmNativeClient()
    songs = await client.cloud_search("晴天 周杰伦", limit=3)
    check("联网 eapi 搜索返回歌曲", bool(songs), f"{len(songs)} 首")
    if songs:
        first = songs[0]
        check("搜索结果字段完整（id/name/ar）",
              bool(first.get("id")) and bool(first.get("name")) and bool(first.get("ar")),
              f"{first.get('name')} - {(first.get('ar') or [{}])[0].get('name')}")

    # 未登录也能拿到的推荐（personalized 走 weapi）
    try:
        playlists = await client.recommend_playlists(3)
        check("联网 weapi 推荐歌单返回", bool(playlists), f"{len(playlists)} 个")
    except Exception as exc:  # noqa: BLE001
        check("联网 weapi 推荐歌单返回", False, f"{type(exc).__name__}: {exc}")

    from music_mcp import vault
    cookie = vault.ncm_cookie()
    if not vault.ncm_cookie_is_valid(cookie):
        print("[skip] 本地没有网易云 Cookie，跳过登录态相关检查")
        return
    client = NcmNativeClient(cookie=cookie)
    status = await client.login_status()
    account = ((status.get("data") or {}).get("account") or {})
    check("联网 login_status 拿到账号", bool(account.get("id")),
          f"uid={account.get('id')} code={status.get('code')}")
    if account.get("id"):
        playlists = await client.user_playlists(str(account["id"]))
        check("联网 user_playlists 返回歌单", bool(playlists), f"{len(playlists)} 个")
        names = [str(p.get("name")) for p in playlists[:3]]
        print("     前几个歌单:", " / ".join(names))


def main() -> int:
    test_aes_fips197()
    test_aes_cbc_nist()
    test_sbox_and_helpers()
    test_rsa_key()
    test_weapi_shape()
    test_aes_roundtrip_utf8()
    test_eapi_shape()
    if os.environ.get("NCM_LIVE") == "1":
        asyncio.run(live_smoke())
    else:
        print("[skip] 联网冒烟（NCM_LIVE=1 开启）")
    print(f"\n通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("失败项:", ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
