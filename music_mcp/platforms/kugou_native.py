"""酷狗 native 后端：纯 Python 直连酷狗 WebAPI，不依赖 MusicLibrary 引擎。

为什么要有它：`pymusiclibrary` 的 manylinux wheel 里装的是 Windows DLL，Linux 上
`from MusicLibrary.kuGouMusicApi import KuGouMusicApi` 必然 OSError（`libengine.so` 不存在）。
网易云之前已经用同样的办法绕开了，这里把酷狗也做成纯 Python。

上游参照：`kugou_music_api.dll` 里内嵌的明文 JS == https://github.com/MakcRe/KuGouMusicApi
（MIT，已逐字比对：同一签名盐值 / appid / baseURL / encryptType）。本文件就是那份 JS
的 `module/*.js` + `util/request.js` 的 Python 复刻，路由名与引擎的路由名**一一对应**，
所以 `kugou.py` 上层的解析代码一行都不用改。

    gateway.kugou.com            绝大多数接口（靠 x-router 头路由到具体服务）
    login-user.kugou.com         扫码登录（web 签名）
    login.user.kugou.com         发短信验证码
    loginserviceretry.kugou.com  短信验证码登录

签名/加密细节见 kugou_crypto.py。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from typing import Any, Callable

import httpx

from . import kugou_crypto as crypto

GATEWAY = "https://gateway.kugou.com"
LOGIN_USER = "https://login-user.kugou.com"
LOGIN_SMS = "http://login.user.kugou.com"
LOGIN_RETRY = "https://loginserviceretry.kugou.com"

MIN_INTERVAL = 0.35          # 两次调用之间的最小间隔，避免连发触发风控
TIMEOUT = 30.0

_last_call = 0.0
_lock = asyncio.Lock()


def cookie_to_dict(cookie: str | dict[str, str]) -> dict[str, str]:
    if isinstance(cookie, dict):
        return {str(k): str(v) for k, v in cookie.items() if k}
    items: dict[str, str] = {}
    for part in str(cookie or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key:
            items[key.strip()] = value.strip()
    return items


def _now() -> int:
    return int(time.time())


# ---------------------------------------------------------------------------
# 请求层：复刻 util/request.js 的 createRequest
# ---------------------------------------------------------------------------

async def _send(*, base_url: str, method: str, url: str,
                params: dict[str, Any] | None = None,
                data: Any = None,
                headers: dict[str, Any] | None = None,
                encrypt_type: str = "android",
                cookie: dict[str, str] | None = None,
                raw_response: bool = False) -> Any:
    cookie = dict(cookie or {})
    device = crypto.device_identity(cookie)
    clienttime = _now()

    default_params: dict[str, Any] = {
        "dfid": device["dfid"],
        "mid": device["mid"],
        "uuid": "-",
        "appid": crypto.APPID,
        "clientver": crypto.CLIENTVER,
        "clienttime": clienttime,
    }
    if cookie.get("token"):
        default_params["token"] = cookie["token"]
    if cookie.get("userid") and str(cookie["userid"]) != "0":
        default_params["userid"] = cookie["userid"]

    merged = {**default_params, **(params or {})}

    body = data
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    body = body or ""

    if "signature" not in merged:
        if encrypt_type == "web":
            merged["signature"] = crypto.signature_web(merged, body)
        elif encrypt_type == "register":
            merged["signature"] = crypto.signature_register(merged)
        else:
            merged["signature"] = crypto.signature_android(merged, body)

    request_headers: dict[str, str] = {
        "User-Agent": crypto.UA_ANDROID,
        "dfid": device["dfid"],
        "clienttime": str(merged.get("clienttime", clienttime)),
        "mid": device["mid"],
        "kg-rc": "1",
        "kg-thash": "5d816a0",
        "kg-rec": "1",
        "kg-rf": "B9EDA08A64250DEFFBCADDEE00F8F25F",
    }
    for key, value in (headers or {}).items():
        request_headers[str(key)] = str(value)

    request_kwargs: dict[str, Any] = {
        "params": {k: v for k, v in merged.items() if v is not None},
        "headers": request_headers,
        "timeout": TIMEOUT,
        "follow_redirects": True,
    }
    if method.lower() == "post" and body:
        request_kwargs["content"] = body.encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json;charset=utf-8")

    url = f"{base_url}{url}"
    async with httpx.AsyncClient() as client:
        response = await client.request(method.upper(), url, **request_kwargs)

    if raw_response:
        return response
    try:
        return response.json()
    except ValueError:
        return {"status": 0, "error_code": -1,
                "msg": f"上游返回非 JSON（HTTP {response.status_code}）",
                "raw": response.text[:200]}


async def _throttle() -> None:
    global _last_call
    gap = MIN_INTERVAL - (time.monotonic() - _last_call)
    if gap > 0:
        await asyncio.sleep(gap)
    _last_call = time.monotonic()


# ---------------------------------------------------------------------------
# 路由实现（一条路由一个函数）
# ---------------------------------------------------------------------------

async def _login_qr_key(cookie: dict[str, str], **params: Any) -> dict[str, Any]:
    return await _send(
        base_url=LOGIN_USER, method="GET", url="/v2/qrcode", encrypt_type="web",
        params={"appid": 1001, "type": 1, "plat": 4,
                "qrcode_txt": f"https://h5.kugou.com/apps/loginQRCode/html/index.html"
                              f"?appid={crypto.APPID}&",
                "srcappid": crypto.SRCAPPID},
        cookie=cookie)


async def _login_qr_create(cookie: dict[str, str], **params: Any) -> dict[str, Any]:
    """上游这条不打网络：只拼登录链接（二维码内容就是它）。"""
    key = str(params.get("key") or "")
    return {"code": 200, "status": 200,
            "data": {"url": f"https://h5.kugou.com/apps/loginQRCode/html/index.html?qrcode={key}",
                     "base64": ""}}


async def _login_qr_check(cookie: dict[str, str], **params: Any) -> dict[str, Any]:
    body = await _send(
        base_url=LOGIN_USER, method="GET", url="/v2/get_userinfo_qrcode", encrypt_type="web",
        params={"plat": 4, "appid": crypto.APPID, "srcappid": crypto.SRCAPPID,
                "qrcode": params.get("key"),
                "dev": crypto.device_identity(cookie)["dev"]},
        cookie=cookie)
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict) and str(data.get("status")) == "4":
        # 授权成功：上游会把 token/userid 写进 cookie 列表，这里直接并进 body，上层照读 data
        body.setdefault("cookie", [])
        body["cookie"] += [f"token={data.get('token')}", f"userid={data.get('userid')}"]
    return body


async def _captcha_sent(cookie: dict[str, str], **params: Any) -> dict[str, Any]:
    device = crypto.device_identity(cookie)
    return await _send(
        base_url=LOGIN_SMS, method="POST", url="/v7/send_mobile_code", encrypt_type="android",
        data={"businessid": 5, "mobile": str(params.get("mobile") or ""), "plat": 3},
        cookie={"mid": device["mid"]})


async def _login_cellphone(cookie: dict[str, str], **params: Any) -> dict[str, Any]:
    date_time = int(time.time() * 1000)
    mobile = str(params.get("mobile") or "")
    code = str(params.get("code") or "")
    encrypt = crypto.aes_with_random_key({"mobile": mobile, "code": code})
    device = crypto.device_identity(cookie)
    t2 = crypto.aes_with_fixed_key(
        f"{device['guid']}|0f607264fc6318a92b9e13c65db7cd3c|{device['mac']}|{device['dev']}|{date_time}",
        crypto.LITE_T2_KEY, crypto.LITE_T2_IV)
    t1 = crypto.aes_with_fixed_key(f"|{date_time}", crypto.LITE_T1_KEY, crypto.LITE_T1_IV)
    data_map: dict[str, Any] = {
        "plat": 1,
        "support_multi": 1,
        "t1": 0,
        "t2": 0,
        "clienttime_ms": date_time,
        "mobile": f"{mobile[:2]}*****{mobile[10:11]}" if mobile else "",
        "key": crypto.sign_params_key(date_time),
        "pk": crypto.rsa_raw_hex({"clienttime_ms": date_time, "key": encrypt["key"]}).upper(),
        "params": encrypt["str"],
        "t3": "MCwwLDAsMCwwLDAsMCwwLDA=",
    }
    if params.get("userid"):
        data_map["userid"] = params["userid"]
    body = await _send(
        base_url=LOGIN_RETRY, method="POST", url="/v7/login_by_verifycode", encrypt_type="android",
        data=data_map, cookie=cookie,
        headers={"support-calm": "1", "User-Agent": "Android16-1070-11440-130-0-LOGIN-wifi"})
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict) and str(body.get("status")) == "1":
        if data.get("secu_params"):
            token = crypto.aes_decrypt_with_key(str(data["secu_params"]), encrypt["key"])
            if isinstance(token, dict):
                data.update(token)
        # t1 / token / userid 从 data 里直接可用，上层 _kugou_cookie_from 会拼成 Cookie
        _ = t1
    return body


async def _user_playlist(cookie: dict[str, str], **params: Any) -> dict[str, Any]:
    userid = cookie.get("userid") or params.get("userid") or "0"
    token = cookie.get("token") or ""
    return await _send(
        base_url=GATEWAY, method="POST", url="/v7/get_all_list", encrypt_type="android",
        data={"userid": userid, "token": token, "total_ver": 979, "type": 2,
              "page": params.get("page") or 1, "pagesize": params.get("pagesize") or 30},
        params={"plat": 1, "userid": int(userid) if str(userid).isdigit() else 0, "token": token},
        headers={"x-router": "cloudlist.service.kugou.com"}, cookie=cookie)


async def _playlist_track_all(cookie: dict[str, str], **params: Any) -> dict[str, Any]:
    pagesize = int(params.get("pagesize") or 30)
    page = int(params.get("page") or 1)
    return await _send(
        base_url=GATEWAY, method="GET", url="/pubsongs/v2/get_other_list_file_nofilt",
        encrypt_type="android",
        params={"area_code": 1, "begin_idx": (page - 1) * pagesize, "plat": 1, "type": 1,
                "mode": 1, "personal_switch": 1,
                "extend_fields": "abtags,hot_cmt,popularization", "pagesize": pagesize,
                "global_collection_id": params.get("id")},
        cookie=cookie)


async def _playlist_track_all_new(cookie: dict[str, str], **params: Any) -> dict[str, Any]:
    """App 当前在用的读曲目路由（按 listid + token，走 cloudlist）。

    上游 `module/playlist_track_all_new.js`：POST /v4/get_list_all_file_v3。
    比老的 gcid 路由（pubsongs/v2/get_other_list_file_nofilt）可靠 —— 后者面向
    "收藏的他人歌单"，新建歌单经常回空。
    """
    userid = str(params.get("userid") or cookie.get("userid") or "0")
    token = str(params.get("token") or cookie.get("token") or "0")
    return await _send(
        base_url=GATEWAY, method="POST", url="/v4/get_list_all_file_v3", encrypt_type="android",
        data={"listid": str(params.get("listid") or ""), "userid": userid,
              "type": int(params.get("type") or 0), "page": int(params.get("page") or 1),
              "pagesize": int(params.get("pagesize") or 300), "area_code": 1,
              "allplatform": 1, "show_cover": 1, "token": token},
        headers={"x-router": "cloudlist.service.kugou.com"}, cookie=cookie)


async def _everyday_recommend(cookie: dict[str, str], **params: Any) -> dict[str, Any]:
    return await _send(
        base_url=GATEWAY, method="POST", url="/everyday_song_recommend", encrypt_type="android",
        params={"platform": params.get("platform") or "ios"},
        headers={"x-router": "everydayrec.service.kugou.com"}, cookie=cookie)


async def _search(cookie: dict[str, str], **params: Any) -> dict[str, Any]:
    keyword = params.get("keywords") or params.get("keyword") or ""
    return await _send(
        base_url=GATEWAY, method="GET", url="/v2/search/song", encrypt_type="android",
        params={"keyword": keyword, "page": params.get("page") or 1,
                "pagesize": params.get("pagesize") or 30, "platform": "AndroidFilter",
                "iscorrection": 1, "privilegefilter": 0, "area_code": 1, "dopicfull": 1},
        headers={"x-router": "complexsearch.kugou.com"}, cookie=cookie)


def _entries(raw: str) -> list[dict[str, Any]]:
    """上游 playlist_tracks_add 的 data 格式：'歌名|hash|album_id|audio_id' 逗号分隔。"""
    out: list[dict[str, Any]] = []
    for chunk in str(raw or "").split(","):
        if not chunk.strip():
            continue
        parts = chunk.split("|")
        out.append({
            "number": 1,
            "name": parts[0] if len(parts) > 0 else "",
            "hash": parts[1] if len(parts) > 1 else "",
            "size": 0, "sort": 0, "timelen": 0, "bitrate": 0,
            "album_id": int(parts[2]) if len(parts) > 2 and str(parts[2]).isdigit() else 0,
            "mixsongid": int(parts[3]) if len(parts) > 3 and str(parts[3]).isdigit() else 0,
        })
    return out


async def _playlist_add(cookie: dict[str, str], **params: Any) -> dict[str, Any]:
    userid = params.get("userid") or cookie.get("userid") or "0"
    token = cookie.get("token") or ""
    clienttime = _now()
    list_type = int(params.get("type") or 0)
    data_map: dict[str, Any] = {
        "userid": userid, "token": token, "total_ver": 0,
        "name": params.get("name"), "type": list_type,
        "source": params.get("source") or 1, "is_pri": int(params.get("is_pri") or 0),
        "list_create_userid": params.get("list_create_userid"),
        "list_create_listid": params.get("list_create_listid"),
        "list_create_gid": params.get("list_create_gid") or "",
        "from_shupinmv": 0,
    }
    query = ({"last_time": clienttime, "last_area": "gztx", "userid": userid, "token": token}
             if list_type == 0 else {})
    return await _send(
        base_url=GATEWAY, method="POST", url="/cloudlist.service/v5/add_list",
        encrypt_type="android", data=data_map, params=query, cookie=cookie)


async def _playlist_tracks_add(cookie: dict[str, str], **params: Any) -> dict[str, Any]:
    userid = params.get("userid") or cookie.get("userid") or "0"
    token = cookie.get("token") or ""
    clienttime = _now()
    return await _send(
        base_url=GATEWAY, method="POST", url="/cloudlist.service/v6/add_song",
        encrypt_type="android",
        data={"userid": userid, "token": token, "listid": params.get("listid"),
              "list_ver": 0, "type": 0, "slow_upload": 1, "scene": "false;null",
              "data": _entries(str(params.get("data") or ""))},
        params={"last_time": clienttime, "last_area": "gztx", "userid": userid, "token": token},
        cookie=cookie)


async def _playlist_del(cookie: dict[str, str], **params: Any) -> dict[str, Any]:
    userid = params.get("userid") or cookie.get("userid") or "0"
    token = cookie.get("token") or ""
    clienttime = _now()
    aes_encrypt = crypto.playlist_encrypt(
        {"listid": int(params.get("listid") or 0), "total_ver": 0, "type": 1})
    portrait = crypto.rsa_pkcs1_hex(
        {"aes": aes_encrypt["key"], "uid": userid, "token": token}).upper()
    query = {"clienttime": clienttime,
             "key": crypto.sign_params_key(clienttime),
             "last_area": "gztx", "clientver": crypto.CLIENTVER, "appid": crypto.APPID,
             "last_time": clienttime, "p": portrait}
    response = await _send(
        base_url=GATEWAY, method="POST", url="/v2/delete_list", encrypt_type="android",
        params=query, data=aes_encrypt["str"], cookie=cookie,
        headers={"x-router": "cloudlist.service.kugou.com"}, raw_response=True)
    # 上游响应是 arraybuffer 密文（JS: body.toString('base64') → playlistAesDecrypt），
    # 所以要拿**原始字节**做 base64，不能先按文本解码。
    cipher_b64 = base64.b64encode(response.content).decode()
    try:
        return crypto.playlist_decrypt(cipher_b64, aes_encrypt["key"])
    except Exception as exc:  # noqa: BLE001 - 服务端没加密时直接把 JSON 给上层
        text = response.content.decode("utf-8", "replace")[:200]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"status": 0, "error_code": -1, "msg": f"响应解密失败({exc})：{text}"}


ROUTES: dict[str, Callable[..., Any]] = {
    "login_qr_key": _login_qr_key,
    "login_qr_create": _login_qr_create,
    "login_qr_check": _login_qr_check,
    "captcha_sent": _captcha_sent,
    "login_cellphone": _login_cellphone,
    "user_playlist": _user_playlist,
    "playlist_track_all": _playlist_track_all,
    "playlist_track_all_new": _playlist_track_all_new,
    "everyday_recommend": _everyday_recommend,
    "search": _search,
    "playlist_add": _playlist_add,
    "playlist_tracks_add": _playlist_tracks_add,
    "playlist_del": _playlist_del,
}


async def call(route: str, cookie: str | dict[str, str] = "", **params: Any) -> dict[str, Any]:
    """与引擎 `call(route, cookie, **params)` 同形：返回上游 body（dict）。"""
    handler = ROUTES.get(route)
    if handler is None:
        raise KeyError(f"酷狗 native 没实现这条路由：{route}（已实现：{', '.join(sorted(ROUTES))}）")
    async with _lock:
        await _throttle()
        body = await handler(cookie_to_dict(cookie), **params)
    return body if isinstance(body, dict) else {"status": 0, "data": body}


def supported_routes() -> list[str]:
    return sorted(ROUTES)


def backend_name() -> str:
    return "native"


# 让老代码里的 `os.environ.get("KUGOU_BACKEND")` 有个明确落点（引擎那条只在 Windows 上存在）
DEFAULT_BACKEND = os.environ.get("KUGOU_BACKEND") or "native"
