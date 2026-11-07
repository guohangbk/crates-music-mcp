"""酷狗音乐的「基础能力」实现。

底层是**纯 Python** 的 `kugou_native.py`（自己算签名直连酷狗 WebAPI），
不依赖 MusicLibrary 引擎、不需要容器 —— 和网易云一样，三平台全部零原生依赖。
凭证是一串 Cookie（``token`` / ``userid`` / ``dfid`` ...），比 QQ 的 Credential 简单。

历史（别再往回加）：这里原先走 MusicLibrary 的 ``kuGouMusicApi`` 引擎，而
pymusiclibrary 的 manylinux wheel 里装的是 Windows DLL，Linux 上必然
``OSError: libengine.so``；引擎还有「同进程第二次调用随机 access violation」的毛病，
当时得靠一次性子进程兜着。现在引擎整条路都删了，子进程和重试壳子也不再需要。

踩过的坑（都是实测出来的，别改回去）：
  * 取歌单曲目要用 ``playlist_track_all`` + ``global_collection_id``，
    ``playlist_track_all_new`` 无论传什么都回 20010 未登录；
  * 新建歌单的 id 在 ``data.info.list_create_listid``，不在 data 顶层；
  * **新建歌单的曲目读不回来**（``playlist_track_all`` 回 0 首）——上游行为，
    引擎与纯 Python 实现实测一致（``slow_upload`` 是异步的，且那条路由面向"收藏的他人歌单"）；
    所以建单只信 ``playlist_tracks_add`` 的 ``status``，不做读回校验；
  * 字段名两套：歌单条目 snake_case（``name`` / ``singerinfo`` / ``hash``），
    搜索结果 CamelCase（``FileName`` / ``SingerName`` / ``FileHash`` / ``MixSongID``）；
  * 二维码内容要自己拼登录链接（上游那条路由不打网络），再用本地渲染成图片。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import httpx

from .. import vault
from . import kugou_native
from .base import (CreateResult, LoginStart, LoginState, LoginTicket, MusicPlatform,
                    Platform, Song, name_matches, song_key)

CALL_RETRIES = 2             # 网络抖动时的重试次数（引擎那套崩溃重试已经不需要了）
# 酷狗二维码有效期：2026-11-07 实测（15 秒一 poll）**480 秒仍回 code=1（待扫）**，
# 没等到过期点，所以别编一个更小的数 —— 上报"我们最多留多久"（会话 TTL 600）。
# 以前这里写 180，没有任何依据。
QR_TTL_SECONDS = 600

LIKE_TITLE_MARKERS = ("我喜欢", "喜欢")
TRACK_PAGE_SIZE = 300
TRACK_MAX_PAGES = 20


class KugouAuthError(RuntimeError):
    """酷狗未登录 / token 失效。"""


def cookie_to_dict(cookie: str) -> dict[str, str]:
    """``token=xx; userid=yy`` -> dict。"""
    items: dict[str, str] = {}
    for part in str(cookie or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        if key.strip():
            items[key.strip()] = value.strip()
    return items


def unwrap(body: Any) -> dict[str, Any]:
    """历史遗留：引擎偶尔回带前缀的字符串，现在纯 Python 直回 dict，留着兼容。"""
    if isinstance(body, dict):
        return body
    text = str(body or "").strip()
    start = text.find("{")
    if start < 0:
        return {}
    try:
        payload = json.loads(text[start:])
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


async def call(route: str, cookie: str = "", **params: Any) -> dict[str, Any]:
    """调一条酷狗路由；实现在 ``kugou_native.py``（纯 Python，直连上游）。"""
    last_error: Exception | None = None
    for attempt in range(CALL_RETRIES + 1):
        try:
            return await kugou_native.call(route, cookie, **params)
        except (httpx.HTTPError, OSError) as exc:      # 网络层抖动才重试
            last_error = exc
            if attempt < CALL_RETRIES:
                await asyncio.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"酷狗调用 {route} 连试 {CALL_RETRIES + 1} 次仍失败：{last_error}")


def _is_login_error(payload: dict[str, Any]) -> bool:
    """20010 = 未登录；status=0 且无数据也当未登录处理。"""
    return int(payload.get("error_code") or 0) in {20010, 40001} or payload.get("status") == 0


# ---------------------------------------------------------------------------
# 返回结构解析（酷狗字段都是 snake_case，这里统一收敛）
# ---------------------------------------------------------------------------


def _songs_from(node: Any) -> list[dict[str, Any]]:
    """从任意一层 data 里挖出歌曲数组：song_list / songs / info / lists。"""
    if isinstance(node, list):
        return [item for item in node if isinstance(item, dict)]
    if not isinstance(node, dict):
        return []
    for key in ("song_list", "songs", "info", "lists", "audios"):
        value = node.get(key)
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return [item for item in value if isinstance(item, dict)]
    return []


def _split_name(text: str) -> tuple[str, str]:
    """酷狗的 ``name`` / ``FileName`` 常写成「歌手 - 歌名」，拆成 (歌名, 歌手)。"""
    for sep in (" - ", "－"):
        if sep in text:
            head, _, tail = text.partition(sep)
            return tail.strip(), head.strip()
    return text.strip(), ""


def _song_name(item: dict[str, Any]) -> str:
    # 先找「本来就是干净歌名」的字段，再退回从「歌手 - 歌名」里拆
    for key in ("songname", "ori_audio_name", "OriSongName", "audio_name"):
        text = str(item.get(key) or "").strip()
        if text:
            return text
    for key in ("name", "FileName", "filename"):
        text = str(item.get(key) or "").strip()
        if text:
            # 新路由（/v4/get_list_all_file_v3）的 name 带扩展名：「纯音乐 - time back.mp3」
            name = _split_name(text)[0]
            return re.sub(r"\.(mp3|flac|m4a|ape|wav|ogg|320k|128k)$", "", name,
                          flags=re.IGNORECASE).strip()
    return ""


def _song_artist(item: dict[str, Any]) -> str:
    for key in ("author_name", "singername", "SingerName", "singer_name", "author"):
        text = str(item.get(key) or "").strip()
        if text:
            return text.split("、")[0].strip()  # 酷狗多歌手用「、」分隔，只留第一个
    singers = item.get("singerinfo") or item.get("singers") or item.get("Singers")
    if isinstance(singers, list) and singers and isinstance(singers[0], dict):
        names = [str(s.get("name") or "").strip() for s in singers if isinstance(s, dict)]
        names = [name for name in names if name]
        if names:
            return names[0]
    for key in ("name", "FileName", "filename"):
        text = str(item.get(key) or "").strip()
        if text:
            artist = _split_name(text)[1].split("、")[0].strip()
            if artist:
                return artist
    return ""


def _song_hash(item: dict[str, Any]) -> str:
    for key in ("hash", "audio_hash", "FileHash"):
        text = str(item.get(key) or "").strip()
        if text:
            return text
    return ""


def _to_songs(items: list[dict[str, Any]]) -> list[Song]:
    songs: list[Song] = []
    for item in items:
        name = _song_name(item)
        if name:
            songs.append(Song(name, _song_artist(item)))
    return songs


async def _playlist_tracks(cookie: str, listid: str = "", collection_id: str = "",
                           total: int = 0) -> list[Song]:
    """拉一个歌单的全部曲目。

    实测（2026-11-07，同一时刻对比）：
      * ``playlist_track_all_new``（POST /v4/get_list_all_file_v3，**listid + token**）
        → 「我喜欢」95 首 ✓，新建歌单的曲目也读得到；
      * ``playlist_track_all``（GET pubsongs/v2/get_other_list_file_nofilt，gcid）
        → 同一时刻回 **0 首**（连引擎也回 0，是上游那条路由变了/废了）。
    所以优先走 listid 那条，只有拿不到 listid 时才退回 gcid。单页会截断，按空页/总数停。
    """
    if not listid and not collection_id:
        return []
    songs: list[Song] = []
    page = 1
    while page <= TRACK_MAX_PAGES:
        if listid:
            payload = await call("playlist_track_all_new", cookie=cookie, listid=listid,
                                 page=page, pagesize=TRACK_PAGE_SIZE)
        else:
            payload = await call("playlist_track_all", cookie=cookie, id=collection_id,
                                 page=page, pagesize=TRACK_PAGE_SIZE)
        batch = _to_songs(_songs_from(payload.get("data") or {}))
        if not batch:
            break
        songs.extend(batch)
        if len(batch) < TRACK_PAGE_SIZE or (total and len(songs) >= total):
            break
        page += 1
    return songs


class KugouPlatform(MusicPlatform):
    key = Platform.KUGOU
    label = "酷狗音乐"
    default_method = "phone_code"

    def status(self) -> dict[str, Any]:
        return vault.status()["kugou"]

    def auth_hint(self) -> str:
        return ("酷狗未登录：login_start(service='kugou', method='phone_code', phone='...')，"
                "收到验证码后 login_submit；也可以 method='qr' 扫码。")

    def login_methods(self) -> list[dict[str, str]]:
        return [
            {"method": "phone_code", "label": "手机验证码（推荐）",
             "account": "手机号对应的酷狗账号", "needs": "phone + 短信验证码",
             "note": "无二维码、不用截图；若撞上风控（要求安全验证）就改用 qr"},
            {"method": "qr", "label": "酷狗 App 扫码",
             "account": "酷狗音乐 App 当前登录的账号", "needs": "酷狗音乐 App 扫一扫",
             "note": "至少 8 分钟有效（实测），过期就重新 login_start"},
        ]

    def scan_hint(self, method: str) -> str:
        if method == "phone_code":
            return "短信验证码登录，无需二维码。收到验证码后调用 login_submit。"
        return "用【酷狗音乐 App 的扫一扫】扫码并确认，再调用 login_poll。"

    def set_cookie(self, value: str, uid: str = "") -> dict[str, Any]:
        if "token=" not in value:
            return {"ok": False, "error": "酷狗 Cookie 里必须有 token="}
        vault.set_kugou(value, {"userid": uid, "source": "cookie_import"})
        return {"ok": True, "service": self.key.value, "source": "cookie_import",
                "hint": "调用 credential_status 验证"}

    def logout(self) -> None:
        vault.write_secret(vault.KG_FILE, {})

    # ---------------- 登录 ----------------
    #
    # 凭证形态是「一串 Cookie」（token / userid / dfid ...），比 QQ 的 Credential 简单。
    # 扫码状态码：0 过期 / 1 待扫 / 2 待确认 / 4 授权成功。

    async def login_start(self, method: str, phone: str = "", ctcode: int = 86) -> LoginStart:
        if method == "phone_code":
            if not phone.isdigit() or len(phone) < 6:
                return LoginStart(ok=False, status="failed", error="手机号格式不正确")
            payload = await self.login_captcha(phone)
            if int(payload.get("status") or 0) != 1:
                return LoginStart(ok=False, status="failed",
                                  error=f"发送验证码失败: {str(payload)[:200]}")
            return LoginStart(ok=True, status="awaiting_code",
                              ticket=LoginTicket({"phone": phone}, None, True),
                              message=f"验证码已发送到 {phone}")
        if method != "qr":
            return LoginStart(ok=False, status="failed", error=f"不支持的登录方式: {method}")
        key, fallback_image = await self.login_qr_key()
        if not key:
            return LoginStart(ok=False, status="failed", error="获取二维码 key 失败")
        # 首选「登录链接 + 本地渲染」（见 login_qr_create 的注释）；
        # 拿不到内容时退回 login_qr_key 里引擎已经渲染好的 PNG。
        raw = await self.login_qr_create(key) or fallback_image
        # 票据只有 key，poll 时现查（引擎每条路由都是一次性进程）→ 可落盘、可跨进程续
        return LoginStart(ok=True, status="waiting",
                          ticket=LoginTicket({"unikey": key}, None, True),
                          qr_image=_kugou_qr_data_url(raw), expires_in=QR_TTL_SECONDS,
                          message="二维码已作为图片返回。扫码后调用 login_poll。")

    async def login_poll(self, ticket: LoginTicket) -> LoginState:
        code, payload = await self.login_qr_check(str(ticket.payload.get("unikey") or ""))
        if code == 4:
            data = payload.get("data") or {}
            cookie = _kugou_cookie_from(data)
            if not vault.kugou_cookie_is_valid(cookie):
                return LoginState(ok=False, status="failed",
                                  error=f"授权成功但未取到 token: {str(payload)[:300]}")
            vault.set_kugou(cookie, {"userid": str(data.get("userid") or ""),
                                     "nickname": str(data.get("nickname") or "")})
            return LoginState(ok=True, status="succeeded",
                              message="酷狗登录成功，Cookie 已写入共享凭证库")
        status = {0: "expired", 1: "waiting", 2: "scanned"}.get(code, "waiting")
        return LoginState(ok=status != "expired", status=status,
                          message=str((payload.get("data") or {}).get("message") or f"二维码状态 {code}"))

    async def login_submit(self, ticket: LoginTicket, code: str) -> LoginState:
        payload = await self.login_cellphone(str(ticket.payload.get("phone") or ""), code)
        data = payload.get("data") or {}
        cookie = _kugou_cookie_from(data)
        if not vault.kugou_cookie_is_valid(cookie):
            return LoginState(ok=False, status="failed",
                              error=f"酷狗验证码登录失败: {str(payload)[:300]}")
        vault.set_kugou(cookie, {"userid": str(data.get("userid") or ""),
                                 "nickname": str(data.get("nickname") or "")})
        return LoginState(ok=True, status="succeeded",
                          message="酷狗验证码登录成功，Cookie 已写入共享凭证库")

    # ---------------- 基础能力 ----------------

    async def recommendations(self) -> tuple[list[Song], list[str], dict[str, Any]]:
        """今日推荐。匿名就能拉，不需要登录。"""
        diag: dict[str, Any] = {"errors": []}
        try:
            payload = await call("everyday_recommend")
        except Exception as exc:
            diag["errors"].append(f"everyday_recommend: {exc.__class__.__name__}: {exc}")
            return [], [], diag
        items = _songs_from(payload.get("data") or payload)
        songs = _to_songs(items)
        sources = [f"每日推荐({len(songs)})"] if songs else []
        return songs, sources, diag

    async def favorites(self) -> tuple[list[Song], list[str], dict[str, Any]]:
        """收藏：先取用户歌单，再找「我喜欢」那一份的曲目。"""
        diag: dict[str, Any] = {"errors": []}
        cookie = vault.kugou_cookie()
        if not vault.kugou_cookie_is_valid(cookie):
            diag["need_login"] = True
            return [], [], diag
        try:
            payload = await call("user_playlist", cookie=cookie, page=1, pagesize=200)
        except Exception as exc:
            diag["errors"].append(f"user_playlist: {exc.__class__.__name__}: {exc}")
            return [], [], diag
        if _is_login_error(payload):
            diag["need_login"] = True
            return [], [], diag
        lists = _songs_from(payload.get("data") or payload)
        diag["playlists"] = len(lists)
        diag["playlist_titles"] = [str(pl.get("listname") or pl.get("name") or "") for pl in lists[:20]]
        targets = [pl for pl in lists
                   if any(marker in str(pl.get("listname") or pl.get("name") or "")
                          for marker in LIKE_TITLE_MARKERS)]
        if not targets and lists:
            targets = [lists[0]]

        songs: list[Song] = []
        sources: list[str] = []
        for target in targets:
            title = str(target.get("listname") or target.get("name") or "?")
            listid = str(target.get("list_create_listid") or target.get("listid") or "")
            collection_id = str(target.get("global_collection_id") or "")
            try:
                batch = await _playlist_tracks(cookie, listid=listid,
                                               collection_id=collection_id,
                                               total=int(target.get("count") or 0))
            except Exception as exc:
                diag["errors"].append(f"歌单 {title}: {exc.__class__.__name__}: {exc}")
                continue
            songs.extend(batch)
            sources.append(f"{title}({len(batch)})")
        # 收藏里同名不同版本很多，按歌名+歌手去重
        seen: set[str] = set()
        deduped: list[Song] = []
        for song in songs:
            key = song_key(song.name, song.artist)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(song)
        if len(deduped) != len(songs):
            sources.append(f"去重后({len(deduped)})")
        return deduped, sources, diag

    async def search(self, name: str, artist: str = "") -> dict[str, Any] | None:
        """按歌名+歌手搜一首歌，返回酷狗的歌曲结构（建单时要 hash / 专辑 id）。"""
        payload = await call("search", keywords=f"{name} {artist}".strip(), page=1, pagesize=5)
        data = payload.get("data") or {}
        # 实测：``search`` 的 ``data.lists`` 直接就是歌曲记录（CamelCase 字段：
        # FileName / SingerName / FileHash / MixSongID），不是按类型分组的结构。
        candidates = _songs_from(data)
        target_key = song_key(name, artist)
        for item in candidates:
            if _song_hash(item) and song_key(_song_name(item), _song_artist(item)) == target_key:
                return item
        # 退一步：歌名对得上就用（歌手字段各家写法不一，可能对不齐）。
        # 歌名都对不上的直接当作没找到 —— 宁可少一首，也别塞进来一首不相干的。
        for item in candidates:
            if _song_hash(item) and name_matches(_song_name(item), name):
                return item
        return None

    async def create_playlist(self, name: str, songs: list[Song],
                              count: int) -> CreateResult:
        cookie = vault.kugou_cookie()
        if not vault.kugou_cookie_is_valid(cookie):
            return CreateResult(platform=self.key, ok=False, playlist_name=name,
                                error=self.auth_hint(), extra={"need_login": True,
                                                               "service": self.key})
        selected = songs[:max(1, count)]
        userid = cookie_to_dict(cookie).get("userid") or cookie_to_dict(cookie).get("kugouid") or ""
        try:
            created = await call("playlist_add", cookie=cookie, name=name,
                                 list_create_userid=userid,
                                 list_create_listid=int(time.time() * 1000) % 100000000,
                                 is_pri=0, type=0)
        except Exception as exc:
            return CreateResult(platform=self.key, ok=False, playlist_name=name,
                                error=f"playlist_add: {exc.__class__.__name__}: {exc}")
        # 实测：新建歌单的 id 在 ``data.info`` 里（list_create_listid + global_collection_id），
        # 不在 data 顶层。
        data = created.get("data") or {}
        info = data.get("info") or {}
        listid = str(info.get("list_create_listid") or info.get("listid")
                     or data.get("listid") or data.get("list_id") or "")
        if not listid:
            return CreateResult(platform=self.key, ok=False, playlist_name=name,
                                error=f"酷狗未返回 listid: {json.dumps(created, ensure_ascii=False)[:200]}")

        entries: list[str] = []
        matched_meta: list[dict[str, str]] = []
        failed: list[str] = []
        for song in selected:
            query = f"{song.name} {song.artist}".strip()
            try:
                hit = await self.search(song.name, song.artist)
            except Exception:
                hit = None
            if not hit:
                failed.append(query)          # 找不到就跳过（用户明确要求）
                continue
            hash_value = _song_hash(hit)
            album_id = str(hit.get("album_id") or hit.get("AlbumID") or "")
            audio_id = str(hit.get("album_audio_id") or hit.get("MixSongID")
                           or hit.get("mixsongid") or hit.get("Audioid") or "")
            entries.append("|".join(part for part in (_song_name(hit), hash_value, album_id, audio_id) if part))
            matched_meta.append(song.as_dict())

        if not entries:
            return CreateResult(platform=self.key.value, ok=False, playlist_name=name,
                                playlist_id=listid, failed=failed,
                                error="没有匹配到任何歌曲")
        try:
            added = await call("playlist_tracks_add", cookie=cookie, listid=listid,
                               data=",".join(entries))
        except Exception as exc:
            return CreateResult(platform=self.key.value, ok=False, playlist_name=name,
                                playlist_id=listid, failed=failed,
                                error=f"playlist_tracks_add: {exc.__class__.__name__}: {exc}")
        status = int(added.get("status") or 0)
        ok = status == 1 or int(added.get("error_code") or 0) == 0
        return CreateResult(platform=self.key.value, ok=ok, playlist_name=name,
                            added=len(entries) if ok else 0, failed=failed,
                            playlist_id=listid, matched=matched_meta if ok else [],
                            error="" if ok else json.dumps(added, ensure_ascii=False)[:200])

    async def delete_playlist(self, playlist_id: str) -> dict[str, Any]:
        """删除酷狗歌单（listid）。走子进程，崩了不污染主进程。"""
        cookie = vault.kugou_cookie()
        if not vault.kugou_cookie_is_valid(cookie):
            return {"ok": False, "error": "酷狗未登录", "service": self.key.value}
        try:
            listid = int(str(playlist_id).strip())
        except (TypeError, ValueError):
            return {"ok": False, "error": "listid 必须是数字", "service": self.key.value}
        try:
            payload = await call("playlist_del", cookie=cookie, listid=listid)
            ok = int(payload.get("status") or payload.get("error_code") or 0) in (0, 1)
            return {"ok": ok, "service": self.key.value,
                    "deleted_listid": listid,
                    "error": "" if ok else json.dumps(payload, ensure_ascii=False)[:200]}
        except Exception as exc:
            return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}",
                    "service": self.key.value}

    async def my_playlists(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """列出酷狗创建的歌单。"""
        diag: dict[str, Any] = {"errors": []}
        cookie = vault.kugou_cookie()
        if not vault.kugou_cookie_is_valid(cookie):
            diag["need_login"] = True
            return [], diag
        try:
            payload = await call("user_playlist", cookie=cookie, page=1, pagesize=200)
            lists = _songs_from(payload.get("data") or payload)
            items = []
            for pl in lists:
                items.append({
                    "listid": str(pl.get("listid") or pl.get("id") or ""),
                    "name": str(pl.get("listname") or pl.get("name") or ""),
                    "count": int(pl.get("count") or 0),
                })
            diag["count"] = len(items)
            return items, diag
        except Exception as exc:
            diag["errors"].append(f"user_playlist: {exc.__class__.__name__}: {exc}")
            return [], diag

    # ---------------- 登录（供 auth 层调用） ----------------

    async def login_qr_key(self) -> tuple[str, str]:
        """取二维码 key 与引擎已渲染好的图片（data URL，可能为空）。

        引擎实际回的字段名是 ``qrcode`` / ``qrcode_img``（不是 ``key``），两个都认。
        """
        payload = await call("login_qr_key")
        data = payload.get("data") or {}
        key = str(data.get("qrcode") or data.get("key") or "")
        return key, str(data.get("qrcode_img") or "")

    async def login_qr_create(self, key: str) -> str:
        """返回二维码**内容**（登录链接或 base64），由本地渲染成图片。

        踩过的坑：传 ``qrimg=True`` 会让引擎这条路由「响应解析失败」直接返回空 body（实测），
        所以**别传**。正常的一次调用回的是
        ``{"data": {"url": "https://h5.kugou.com/apps/loginQRCode/html/index.html?qrcode=<key>"}}``，
        这个链接本身就是二维码内容。
        """
        payload = await call("login_qr_create", key=key)
        data = payload.get("data") or {}
        return str(data.get("base64") or data.get("qrimg") or data.get("url") or "")

    async def login_qr_check(self, key: str) -> tuple[int, dict[str, Any]]:
        """返回 (status_code, payload)。0 过期 / 1 待扫 / 2 待确认 / 4 成功。"""
        payload = await call("login_qr_check", key=key)
        data = payload.get("data") or {}
        try:
            return int(data.get("status") or payload.get("status") or 0), payload
        except (TypeError, ValueError):
            return 0, payload

    async def login_captcha(self, mobile: str) -> dict[str, Any]:
        return await call("captcha_sent", mobile=mobile)

    async def login_cellphone(self, mobile: str, code: str) -> dict[str, Any]:
        return await call("login_cellphone", mobile=mobile, code=code)


KUGOU_AUTH_HINT = ("酷狗未登录：login_start(service='kugou', method='qr'|'phone_code', phone='...')，"
                   "扫码走 login_poll，短信走 login_submit；也可以 credential_set_cookie 直接导入。")

# 酷狗登录返回里的这些字段不是凭证，别混进 Cookie
_KUGOU_NON_CREDENTIAL = {"status", "message", "msg", "error_code", "nickname", "pic",
                         "is_first_login", "user_type"}


def _kugou_qr_data_url(raw: str) -> str:
    """引擎可能回 data URL / 纯 base64 / 登录链接，统一成 data URL。"""
    from ..qr import data_url as qr_data_url

    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith("data:"):
        return text
    if text.startswith("http"):
        return qr_data_url(text)
    return f"data:image/png;base64,{text}"


def _kugou_cookie_from(data: dict[str, Any]) -> str:
    """把登录返回里的标量字段拼成 Cookie（token / userid / dfid / mid ...）。"""
    parts: list[str] = []
    for key, value in (data or {}).items():
        if key in _KUGOU_NON_CREDENTIAL or isinstance(value, (dict, list)):
            continue
        entry = str(value).strip()
        if entry:
            parts.append(f"{key}={entry}")
    return "; ".join(parts)
