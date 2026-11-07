"""酷狗 native 后端的对拍测试。

参照值不是我自己算的，是**上游原版 JS**（MakcRe/KuGouMusicApi 的 util/helper.js + util/crypto.js，
在 node 里跑出来的）—— 见下面 REF 字典里的注释。这样"签名到底写得对不对"就不靠自证了。

离线部分（默认跑）：签名 / md5 / RSA / AES / 歌单加密，逐条对上游输出。
联网部分（KUGOU_LIVE=1）：真打一遍搜索 / 歌单 / 日推 / 二维码，并检查与引擎时代一致的结构。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from music_mcp.platforms.core import kugou_crypto as C  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"[{'ok' if condition else 'FAIL'}] {name}{'  ' + detail if detail else ''}")


# 由 `node ref.js` 生成（ref.js 直接 require 上游的 util/helper.js 与 util/crypto.js）
REF = {
    "md5_abc": "900150983cd24fb0d6963f7d28e17f72",
    "sig_android": "36f7b566da399acc6086d8e566e7cff2",
    "sig_android_body": "bb0d57fb8409fb82b2a74a66aac932fd",
    "sig_web": "c4861d1dacd3a3341a5b73406e096779",
    "sig_web_body": "51e8d8004c347624f5a58e5fc2ceaeb9",
    "sig_register": "9b2db33411911dee6e7fe65cf0e04ca2",
    "sign_params_key": "a1f65b6a8fe7e191521406ce8661ae02",
    "rsa_raw_hello": "86f08e61937dddaebfacfb696995e68cba1ebaa695d9b436ddad5a75b3c4c6d4"
                     "55b9269c12c9b9633d55a97bb2459f468dade23bc70ba5048b9a227601603c7b"
                     "49ea92bb9dc78641a6d75d8023f634a1880b16e31efd69644e5abb54cf7c36bc"
                     "950b91b039d9cd0b90d97092556fd4ff65f98adcfdf03bba87a662c3cae37d64",
    "rsa_raw_json": "203362179d38d888f6d7b946cdae716ae8ae72a4d26e70269f1f275b856d1e72"
                    "530a3158b8737264ef825e9fe83341ef03e61c5f6355288c9b505fa04b8c5431"
                    "aedfc9375bf2a57b43ab278036f0c30577597269c1cedd85f9055667d91b12d5"
                    "26a9f7f9dc66868bd0b6836331ba9967d322d6277183f9d7c5180a836efa62df",
    "aes_fixed": "ffcb80bec2437fc2e30b0ef0a8ec5b64",
    # 上游 cryptoAesEncrypt(data)（随机 key）与 playlistAesEncrypt 的输出：key 是随机的，
    # 所以只能拿它的 key 在 Python 侧解密，验证密钥推导与 AES-256/128 是否一致。
    "aes_random": {"str": "a03f6e93e5548d142054829ceebd20f12f795c964fac436495837c4617fc6143"
                          "99bf13b0b4251212d0f39555b2f481bd",
                   "key": "xl7flkxlnrr3n82m"},
    "playlist": {"key": "g0tbjv",
                 "str": "LOPBbN/gPcbp0HT3WopxMYbBNP3x7WiKhXQTK5OBdQYUAGT0BujU31VDCWwZbM6F"},
}

PARAMS = {"s": "周杰伦", "type": 1, "limit": 5, "mid": "1234567890", "dfid": "-"}
BODY = json.dumps({"a": 1, "mobile": "13800000000"}, separators=(",", ":"))


def test_signatures() -> None:
    check("md5 与上游一致", C.md5_hex("abc") == REF["md5_abc"], C.md5_hex("abc"))
    check("android 签名（无 body）一致",
          C.signature_android(PARAMS, "") == REF["sig_android"])
    check("android 签名（带 body）一致",
          C.signature_android(PARAMS, BODY) == REF["sig_android_body"])
    check("web 签名一致", C.signature_web(PARAMS, "") == REF["sig_web"])
    check("web 签名（带 body）一致", C.signature_web(PARAMS, BODY) == REF["sig_web_body"])
    check("register 签名一致", C.signature_register(PARAMS) == REF["sig_register"])
    check("signParamsKey 一致", C.sign_params_key("1700000000") == REF["sign_params_key"])


def test_rsa() -> None:
    check("裸 RSA（字符串）与上游一致",
          C.rsa_raw_hex("hello-kugou") == REF["rsa_raw_hello"])
    check("裸 RSA（对象，JSON 化）与上游一致",
          C.rsa_raw_hex({"clienttime_ms": 1700000000000, "key": "abcdef0123456789"})
          == REF["rsa_raw_json"])
    n, e, size = C._public_key()
    check("酷狗公钥 1024 位 / e=65537", size == 128 and e == 65537, f"{size} {e}")

    pkcs1 = C.rsa_pkcs1_hex({"aes": "abcdef", "uid": "100000001", "token": "t"})
    check("PKCS1 密文是 128 字节 hex", len(pkcs1) == 256, pkcs1[:16])
    n_mod, _, _ = C._public_key()
    check("PKCS1 密文小于模数", int(pkcs1, 16) < n_mod)
    # PKCS1 每次填充随机，只能验结构；随机性也顺手验一下
    check("PKCS1 填充是随机的",
          pkcs1 != C.rsa_pkcs1_hex({"aes": "abcdef", "uid": "100000001", "token": "t"}))


def test_aes() -> None:
    check("固定 key/iv 的 AES 与上游一致",
          C.aes_with_fixed_key("hello", "0123456789abcdef", "abcdef0123456789")
          == REF["aes_fixed"])
    plain = C.aes_decrypt_with_key(REF["aes_random"]["str"], REF["aes_random"]["key"])
    check("能解开上游随机 key 的密文（AES-256 + 密钥推导一致）",
          plain == {"mobile": "13800000000", "code": "1234"}, str(plain))
    pl = C.playlist_decrypt(REF["playlist"]["str"], REF["playlist"]["key"])
    check("能解开上游的歌单密文（AES-128 + base64）",
          pl == {"listid": 13, "total_ver": 0, "type": 1}, str(pl))
    mine = C.playlist_encrypt({"listid": 7, "type": 1})
    check("歌单加密自洽（加密→解密回原文）",
          C.playlist_decrypt(mine["str"], mine["key"]) == {"listid": 7, "type": 1})


def test_device_identity() -> None:
    cookie = {"token": "tk", "userid": "100000001"}
    first, second = C.device_identity(cookie), C.device_identity(cookie)
    check("设备指纹稳定（同凭证算出来一样）", first == second, json.dumps(first, ensure_ascii=False))
    other = C.device_identity({"token": "tk2", "userid": "100000001"})
    check("不同凭证算出不同设备", first["mid"] != other["mid"])
    check("mid 是十进制大整数", first["mid"].isdigit() and len(first["mid"]) > 20,
          first["mid"][:12])


async def live() -> None:
    from music_mcp import vault
    from music_mcp.platforms.core import kugou_native

    body = await kugou_native.call("search", keywords="晴天 周杰伦", page=1, pagesize=3)
    items = (body.get("data") or {}).get("lists") or []
    check("联网搜索有结果（匿名可用）", bool(items), f"{len(items)} 首")
    if items:
        check("搜索字段是 CamelCase（FileName/FileHash）",
              bool(items[0].get("FileName") and items[0].get("FileHash")),
              str(items[0].get("FileName")))

    body = await kugou_native.call("login_qr_key")
    key = str((body.get("data") or {}).get("qrcode") or "")
    check("联网取二维码 key", bool(key), key[:16])
    if key:
        created = await kugou_native.call("login_qr_create", key=key)
        check("二维码内容是 h5 登录链接",
              "h5.kugou.com" in str((created.get("data") or {}).get("url")),
              str((created.get("data") or {}).get("url"))[:60])
        checked = await kugou_native.call("login_qr_check", key=key)
        status = str((checked.get("data") or {}).get("status"))
        check("二维码状态可查（0 过期 / 1 待扫 / 2 待确认 / 4 成功）",
              status in {"0", "1", "2", "4"}, f"status={status}")

    cookie = vault.kugou_cookie()
    if not vault.kugou_cookie_is_valid(cookie):
        print("[skip] 本地没有酷狗 Cookie，跳过登录态相关检查")
        return
    body = await kugou_native.call("user_playlist", cookie=cookie, page=1, pagesize=200)
    lists = (body.get("data") or {}).get("info") or []
    check("联网拿到用户歌单", bool(lists), f"{len(lists)} 个")
    liked = [pl for pl in lists if "我喜欢" in str(pl.get("name") or "")]
    if liked:
        listid = str(liked[0].get("list_create_listid") or liked[0].get("listid") or "")
        tracks = await kugou_native.call("playlist_track_all_new", cookie=cookie, listid=listid,
                                         page=1, pagesize=300)
        songs = (tracks.get("data") or {}).get("info") or []
        check("联网拿到「我喜欢」曲目（新 listid 路由）", len(songs) > 50,
              f"{len(songs)} 首 / 共 {liked[0].get('count')}")
        if songs:
            names = [str(s.get("name") or s.get("FileName") or "") for s in songs[:3]]
            check("曲目里有歌名（name/FileName）", all(names), str(names))


def main() -> int:
    test_signatures()
    test_rsa()
    test_aes()
    test_device_identity()
    if os.environ.get("KUGOU_LIVE") == "1":
        asyncio.run(live())
    else:
        print("[skip] 联网冒烟（KUGOU_LIVE=1 开启）")
    print(f"\n通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("失败项:", ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
