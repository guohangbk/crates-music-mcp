"""网易云音乐的「基础能力」：登录、凭证、收藏、今日推荐、搜索、建歌单。

后端是**自建**的本地库（MusicLibrary，QuickJS + 预编译动态库），不起容器、不起 sidecar：
set NCM_BACKEND=http 时才回落到 HTTP 后端（NCM_API_BASE 指向一个 ncm-api 服务）。

文件里有两段代码，都是原样搬过来的，别随手"简化"：
  * 上半段：两套后端的封装（同一套接口）；
  * 下半段：ncm_* 业务函数（收藏/推荐/建单），以及 NeteasePlatform。

踩过的坑（详见各函数注释）：引擎把多条 Set-Cookie 拼成一整段返回所以要正则扫；
引擎的大歌单路由会踩内存把进程打崩所以曲目直连 WebAPI；引擎非线程安全所以要固定单线程。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import httpx

from .. import vault
from ..qr import data_url as qr_data_url
from .base import (CreateResult, LoginStart, LoginState, LoginTicket, MusicPlatform,
                   Platform, Song, dedupe, name_matches, normalize, song_key)

_norm = normalize  # 旧代码里的名字

NCM_LIKED = "喜欢的音乐"
NCM_AUTH_HINT = ("网易云未登录或 Cookie 已失效：优先 credential_set_cookie(service='netease', "
                 "value='MUSIC_U=...; __csrf=...')，或 login_start(service='netease', "
                 "method='phone_code'|'qr') 走短信/扫码。")
REC_PLAYLIST_LIMIT = 3      # 最多解析几个推荐歌单
REC_PLAYLIST_TRACKS = 50    # 每个推荐歌单最多取多少首






BASE = os.environ.get("NCM_API_BASE", "http://ncm-api:3000").rstrip("/")

# 上游「需要登录」的返回码：api-enhanced 与 SDK 都沿用 301/302。
AUTH_CODES = {301, 302}

# Set-Cookie 里的属性不能当成 cookie 名回灌给上游。
_COOKIE_ATTRS = {"path", "domain", "expires", "max-age", "samesite", "secure",
                 "httponly", "version", "comment"}

# 扫描 `k=v` 令牌（值止于 ; 或 ,），用于从「一整段被拼起来的 Set-Cookie」里抠出所有 cookie。
_COOKIE_PAIR_RE = re.compile(r"(?<![\w.\-])([A-Za-z_][\w.\-]*)\s*=\s*([^;,]*)")


class NcmAuthError(RuntimeError):
    """网易云 Cookie 失效 / 未登录。可被调用方捕获后强制续期重试一次。"""


class NcmRateLimitError(RuntimeError):
    """上游限流（405「操作频繁」）且退避重试后仍未恢复。"""


def auth_failed(payload: dict[str, Any]) -> bool:
    return payload.get("code") in AUTH_CODES


def rate_limited(payload: dict[str, Any]) -> bool:
    """网易云对搜索这类接口按次数限流，撞上就回 405「操作频繁，请稍候再试」。"""
    if payload.get("code") != 405:
        return False
    text = f"{payload.get('msg') or ''}{payload.get('message') or ''}"
    return "频繁" in text or not text


# 撞上 405 限流后的退避序列（秒）；最后一个用完还不行就报 NcmRateLimitError。
RATE_BACKOFF = (2.0, 5.0, 10.0, 20.0)


# 网易云的曲库 id：key 是 song_key(歌名, 歌手)。
# 收藏和今日推荐本来就带回了 id，顺手存下来，建歌单时能直接命中，省掉一次 search ——
# 搜索接口限流很凶，少发一次是一次。
_KNOWN_IDS: dict[str, int] = {}


def _remember(song: dict[str, Any]) -> None:
    name = str(song.get("name") or "").strip()
    try:
        song_id = int(song.get("id") or 0)
    except (TypeError, ValueError):
        song_id = 0
    if not name or not song_id:
        return
    artists = song.get("ar") or song.get("artists") or [{}]
    artist = str(artists[0].get("name") or "").strip() if artists else ""
    _KNOWN_IDS[song_key(name, artist)] = song_id


def _cookie_to_dict(cookie: str) -> dict[str, str]:
    """`MUSIC_U=x; __csrf=y` -> dict —— SDK 只接受 dict 形式的 cookie。"""
    items: dict[str, str] = {}
    for part in str(cookie or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        if key and key.lower() not in _COOKIE_ATTRS:
            items[key] = value.strip()
    return items


def _cookies_to_string(values: Any) -> str:
    """把 Set-Cookie 压成请求用的 `k=v; k2=v2`。

    引擎会把一次响应里的多条 Set-Cookie **拼成一整段字符串**返回（属性一起带着，
    分隔符是 `;` 或 `,`，例如 `MUSIC_R_T=...; Path=/;; PATH=/, MUSIC_U=...`）。
    所以不能按 `;` 取第一段 —— 那会把 MUSIC_U 丢掉，只剩一个残片 Cookie。
    这里扫描出所有 `k=v` 令牌，再剔除属性名（Path/Expires/Max-Age 等）。
    """
    if isinstance(values, str):
        values = [values]
    pairs: list[str] = []
    seen: set[str] = set()
    for entry in values or []:
        if not isinstance(entry, str):
            continue
        for key, value in _COOKIE_PAIR_RE.findall(entry):
            if key.lower() in _COOKIE_ATTRS or key in seen:
                continue
            seen.add(key)
            pairs.append(f"{key}={value}")
    return "; ".join(pairs)


def _set_cookie_values(headers: Any) -> list[str]:
    if not isinstance(headers, dict):
        return []
    for key in ("Set-Cookie", "set-cookie", "SET-COOKIE"):
        value = headers.get(key)
        if isinstance(value, list):
            return [str(item) for item in value]
        if value:
            return [str(value)]
    return []


class HeaderShim:
    """让 `_extract_cookie` 同时吃得下 httpx 响应与 SDK 响应。"""

    def __init__(self, values: list[str] | None = None):
        self._values = list(values or [])

    @property
    def headers(self) -> "HeaderShim":
        return self

    def get_list(self, name: str) -> list[str]:
        return list(self._values)


def _now_ms() -> int:
    return int(time.time() * 1000)


# 官方 WebAPI（明文 /api，无需 weapi 加密）。
# 本地引擎的 /playlist/detail 与 /playlist/track/all 在网易返回**大歌单**时会踩内存：
# 实测小歌单（20 首）正常，684 首的「我喜欢的音乐」直接 access violation，
# 把整个 MCP 进程打崩（0xC0000409 / 0xC0000374）。所以歌单曲目一律直连这个老接口读，
# 不再经过引擎 —— 这条路由天然返回完整 tracks，比 /api/v6 只给 10 首 + trackIds 更省事。
NCM_WEB_BASE = os.environ.get("NCM_WEB_BASE", "https://music.163.com").rstrip("/")
_NCM_WEB_HEADERS = {
    "Referer": "https://music.163.com/",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
}


async def web_playlist_tracks(playlist_id: int, cookie: str = "",
                              limit: int = 1000) -> list[dict[str, Any]]:
    """直连 /api/playlist/detail 读歌单曲目（绕开引擎的崩溃路由）。"""
    headers = dict(_NCM_WEB_HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(f"{NCM_WEB_BASE}/api/playlist/detail",
                                    params={"id": int(playlist_id), "n": int(limit), "s": 0},
                                    headers=headers)
    try:
        payload = response.json()
    except ValueError:
        raise NcmAuthError(f"网易云歌单详情返回非 JSON（HTTP {response.status_code}）")
    code = payload.get("code")
    if code in AUTH_CODES:
        raise NcmAuthError(f"网易云未登录或 Cookie 已失效（code={code}）")
    playlist = payload.get("playlist") or payload.get("result") or {}
    return [track for track in (playlist.get("tracks") or []) if isinstance(track, dict)]


def _extract_cookie(payload: dict[str, Any], response: httpx.Response) -> str:
    cookie = str(payload.get("cookie") or "")
    if not cookie:
        data = payload.get("data")
        if isinstance(data, dict):
            cookie = str(data.get("cookie") or "")
    if not cookie:
        cookie = _cookies_to_string(response.headers.get_list("set-cookie"))
    return cookie.strip()


class NcmApiClient:
    backend = "http"

    def __init__(self, base: str | None = None, cookie: str | None = None):
        self.base = (base or BASE).rstrip("/")
        self.cookie = cookie or ""

    async def _get(self, path: str, **params: Any) -> tuple[dict[str, Any], httpx.Response]:
        params.setdefault("timestamp", _now_ms())
        headers = {"Cookie": self.cookie} if self.cookie else {}
        payload: dict[str, Any] = {}
        response: httpx.Response | None = None
        for backoff in (0.0, *RATE_BACKOFF):
            if backoff:
                await asyncio.sleep(backoff)
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                response = await client.get(f"{self.base}{path}", params=params, headers=headers)
            try:
                payload = response.json()
            except ValueError:
                payload = {"code": response.status_code, "raw": response.text[:300]}
            if not rate_limited(payload):
                return payload, response
        raise NcmRateLimitError(f"网易云限流：{path} 连试 {len(RATE_BACKOFF) + 1} 次都返回 405")

    # ---------------- auth ----------------

    async def login_status(self) -> dict[str, Any]:
        payload, _ = await self._get("/login/status", cookie=self.cookie)
        return payload

    @staticmethod
    def _raise_if_auth_failed(payload: dict[str, Any]) -> None:
        if payload.get("code") in AUTH_CODES:
            raise NcmAuthError(f"网易云未登录或 Cookie 已失效（code={payload.get('code')}）")

    async def refresh_cookie(self) -> tuple[str, dict[str, Any]]:
        payload, response = await self._get("/login/refresh", cookie=self.cookie)
        return _extract_cookie(payload, response), payload

    async def qr_key(self) -> str:
        payload, _ = await self._get("/login/qr/key")
        data = payload.get("data") or {}
        return str(data.get("unikey") or data.get("key") or "")

    async def qr_create(self, key: str) -> str:
        """Return a base64 PNG data URL (api-enhanced supports qrimg=true)."""
        payload, _ = await self._get("/login/qr/create", key=key, qrimg=True)
        data = payload.get("data") or {}
        return str(data.get("qrimg") or data.get("qrurl") or "")

    async def qr_check(self, key: str) -> tuple[int, str, dict[str, Any], httpx.Response]:
        payload, response = await self._get("/login/qr/check", key=key)
        return int(payload.get("code") or 0), str(payload.get("message") or ""), payload, response

    async def captcha_sent(self, phone: str, ctcode: int = 86) -> dict[str, Any]:
        payload, _ = await self._get("/captcha/sent", phone=phone, ctcode=ctcode)
        return payload

    async def captcha_verify(self, phone: str, code: str, ctcode: int = 86) -> dict[str, Any]:
        payload, _ = await self._get("/captcha/verify", phone=phone, code=code, ctcode=ctcode)
        return payload

    async def login_cellphone(self, phone: str, code: str, ctcode: int = 86) -> tuple[str, dict[str, Any], httpx.Response]:
        payload, response = await self._get("/login/cellphone", phone=phone, captcha=code,
                                           ctcode=ctcode)
        return _extract_cookie(payload, response), payload, response

    # ---------------- data ----------------

    async def user_playlists(self, uid: str) -> list[dict[str, Any]]:
        payload, _ = await self._get("/user/playlist", uid=uid)
        return list(payload.get("playlist") or [])

    async def playlist_tracks(self, playlist_id: int, limit: int = 1000,
                              max_pages: int = 50) -> list[dict[str, Any]]:
        """All tracks of a playlist, paged explicitly.

        Upstream defaults to ``limit=1000, offset=0``, so a 1000+ song playlist
        would be silently truncated — page it instead of trusting the default.
        """
        songs: list[dict[str, Any]] = []
        for page in range(max_pages):
            payload, _ = await self._get("/playlist/track/all", id=playlist_id,
                                         limit=limit, offset=page * limit)
            self._raise_if_auth_failed(payload)
            batch = [song for song in payload.get("songs") or [] if isinstance(song, dict)]
            songs.extend(batch)
            if len(batch) < limit:
                break
        return songs

    # ---------------- playlist write ----------------

    async def cloud_search(self, keyword: str, limit: int = 5, offset: int = 0) -> list[dict[str, Any]]:
        """Search songs by keyword. Anonymous-safe; used to resolve name -> id."""
        payload, _ = await self._get("/cloudsearch", keywords=keyword, type=1,
                                     limit=limit, offset=offset)
        self._raise_if_auth_failed(payload)
        result = payload.get("result")
        songs = result.get("songs") if isinstance(result, dict) else None
        return [song for song in songs or [] if isinstance(song, dict)]

    async def playlist_create(self, name: str, privacy: int = 0) -> tuple[int, dict[str, Any]]:
        payload, _ = await self._get("/playlist/create", name=name, privacy=privacy)
        self._raise_if_auth_failed(payload)
        created = payload.get("playlist") if isinstance(payload.get("playlist"), dict) else {}
        try:
            playlist_id = int(payload.get("id") or created.get("id") or 0)
        except (TypeError, ValueError):
            playlist_id = 0
        return playlist_id, payload

    async def playlist_track_add(self, playlist_id: int, song_ids: list[int]) -> dict[str, Any]:
        ids = ",".join(str(int(song_id)) for song_id in song_ids if song_id)
        # 加歌走 /playlist/tracks?op=add；/playlist/track/add 是「视频歌单」的接口，会回 401。
        payload, _ = await self._get("/playlist/tracks", op="add", pid=playlist_id, tracks=ids)
        self._raise_if_auth_failed(payload)
        return payload

    async def playlist_delete(self, playlist_id: int) -> dict[str, Any]:
        payload, _ = await self._get("/playlist/delete", id=int(playlist_id))
        self._raise_if_auth_failed(payload)
        return payload

    # ---------------- 平台推荐（今日推荐歌单 / 日推歌曲） ----------------

    async def daily_songs(self) -> list[dict[str, Any]]:
        """每日推荐歌曲（/recommend/songs）。"""
        payload, _ = await self._get("/recommend/songs")
        data = payload.get("data")
        songs = data.get("dailySongs") if isinstance(data, dict) else None
        return [song for song in songs or [] if isinstance(song, dict)]

    async def recommend_playlists(self, limit: int = 10) -> list[dict[str, Any]]:
        """推荐歌单。登录用 /recommend/resource，未登录回落到 /personalized。"""
        payload, _ = await self._get("/recommend/resource")
        playlists = payload.get("recommend")
        if auth_failed(payload) or not playlists:
            payload, _ = await self._get("/personalized", limit=limit)
            playlists = payload.get("result")
        return [item for item in playlists or [] if isinstance(item, dict)][:limit]


# ---------------------------------------------------------------------------
# SDK backend: MusicLibrary (QuickJS + 预编译库) —— 本地跑，不起容器
# ---------------------------------------------------------------------------

# 库自己声明「接口对象不能跨线程使用」，所以固定一条工作线程 + 缓存一个实例。
_SDK_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ncm-sdk")
_SDK_LOCK = threading.Lock()
_SDK_API: Any = None


def _sdk_api():
    global _SDK_API
    with _SDK_LOCK:
        if _SDK_API is None:
            from MusicLibrary.neteaseCloudMusicApi import NeteaseCloudMusicApi

            _SDK_API = NeteaseCloudMusicApi()
        return _SDK_API


class NcmSdkClient:
    """Same interface as :class:`NcmApiClient`, backed by the local bindings."""

    backend = "sdk"
    # 两次调用之间留最小间隔，撞上 405 再按 RATE_BACKOFF 退避重试。
    MIN_INTERVAL = 1.0
    _last_call = 0.0  # 类级：所有实例共用同一个 SDK 工作线程

    def __init__(self, cookie: str = "", base: str | None = None):
        self.cookie = (cookie or "").strip()

    async def _drive(self, run: Callable[[], Any], label: str, timeout: float
                     ) -> tuple[dict[str, Any], HeaderShim]:
        """带节流和限流退避的执行器：所有 SDK 调用都从这里出去。"""
        loop = asyncio.get_running_loop()
        payload: dict[str, Any] = {}
        shim = HeaderShim([])
        for backoff in (0.0, *RATE_BACKOFF):
            if backoff:
                await asyncio.sleep(backoff)
            gap = self.MIN_INTERVAL - (time.monotonic() - NcmSdkClient._last_call)
            if gap > 0:
                await asyncio.sleep(gap)
            try:
                raw = await asyncio.wait_for(loop.run_in_executor(_SDK_EXECUTOR, run),
                                             timeout=timeout)
            finally:
                NcmSdkClient._last_call = time.monotonic()
            payload, shim = self._adapt(raw)
            if not rate_limited(payload):
                return payload, shim
        raise NcmRateLimitError(
            f"网易云限流：{label} 连试 {len(RATE_BACKOFF) + 1} 次都返回 405")

    async def _call(self, route: str, timeout: float = 40.0,
                    **kwargs: Any) -> tuple[dict[str, Any], HeaderShim]:
        # 参数名必须叫 route：叫 name 会和 /playlist/create?name=... 这种路由参数撞名，
        # 报 "got multiple values for argument 'name'"，建歌单直接废掉。
        cookie = _cookie_to_dict(self.cookie)
        return await self._drive(
            lambda: getattr(_sdk_api(), route)(cookie=cookie, **kwargs), route, timeout)

    async def _request(self, path: str, timeout: float = 40.0,
                       **query: Any) -> tuple[dict[str, Any], HeaderShim]:
        """按路由字符串直接打：库里没包成方法的路由（如 /cloudsearch）走这条。"""
        cookie = _cookie_to_dict(self.cookie)
        return await self._drive(
            lambda: _sdk_api().request(path, cookie, **query), path, timeout)

    @staticmethod
    def _adapt(raw: Any) -> tuple[dict[str, Any], HeaderShim]:
        payload = dict(getattr(raw, "body", None) or {})
        values = _set_cookie_values(getattr(raw, "headers", None))
        shim = HeaderShim(values)
        body_cookie = payload.get("cookie")
        joined = _cookies_to_string(body_cookie) if body_cookie else ""
        if not joined and values:
            joined = _cookies_to_string(values)
        if joined:
            payload["cookie"] = joined
        return payload, shim

    @staticmethod
    def _raise_if_auth_failed(payload: dict[str, Any]) -> None:
        if auth_failed(payload):
            raise NcmAuthError(f"网易云未登录或 Cookie 已失效（code={payload.get('code')}）")

    # ---------------- auth ----------------

    async def login_status(self) -> dict[str, Any]:
        payload, _ = await self._call("login_status")
        return payload

    async def refresh_cookie(self) -> tuple[str, dict[str, Any]]:
        payload, shim = await self._call("login_refresh")
        return _extract_cookie(payload, shim), payload

    async def qr_key(self) -> str:
        payload, _ = await self._call("login_qr_key")
        data = payload.get("data") or {}
        return str(data.get("unikey") or data.get("key") or "")

    async def qr_create(self, key: str) -> str:
        """SDK 引擎不返回 qrimg，只有登录链接 —— 本地渲染成二维码。"""
        payload, _ = await self._call("login_qr_create", key=key)
        data = payload.get("data") or {}
        return str(data.get("qrimg") or "") or qr_data_url(str(data.get("qrurl") or ""))

    async def qr_check(self, key: str) -> tuple[int, str, dict[str, Any], HeaderShim]:
        payload, shim = await self._call("login_qr_check", key=key)
        return int(payload.get("code") or 0), str(payload.get("message") or ""), payload, shim

    async def captcha_sent(self, phone: str, ctcode: int = 86) -> dict[str, Any]:
        payload, _ = await self._call("captcha_sent", phone=phone, ctcode=ctcode)
        return payload

    async def captcha_verify(self, phone: str, code: str, ctcode: int = 86) -> dict[str, Any]:
        payload, _ = await self._call("captcha_verify", phone=phone, captcha=code, ctcode=ctcode)
        return payload

    async def login_cellphone(self, phone: str, code: str,
                              ctcode: int = 86) -> tuple[str, dict[str, Any], HeaderShim]:
        payload, shim = await self._call("login_cellphone", phone=phone, captcha=code,
                                         countrycode=ctcode)
        return _extract_cookie(payload, shim), payload, shim

    # ---------------- data ----------------

    async def user_playlists(self, uid: str) -> list[dict[str, Any]]:
        payload, _ = await self._call("user_playlist", uid=str(uid), limit=1000)
        return [item for item in payload.get("playlist") or [] if isinstance(item, dict)]

    async def playlist_tracks(self, playlist_id: int, limit: int = 1000,
                              max_pages: int = 50) -> list[dict[str, Any]]:
        # 引擎的 /playlist/track/all 在大歌单上必崩，直连 WebAPI（见 web_playlist_tracks）。
        return await web_playlist_tracks(playlist_id, self.cookie, limit=limit)

    async def cloud_search(self, keyword: str, limit: int = 5,
                           offset: int = 0) -> list[dict[str, Any]]:
        # 走 /cloudsearch（网页端那条搜索）：库包出来的 /search 是老的 search/get 接口，
        # 限流极狠 —— 连发十来次就 405「操作频繁」，而且一挂就是十几分钟不放，实测
        # /cloudsearch 在同一时刻照样能连发 12 次全部 200。
        payload, _ = await self._request("/cloudsearch", keywords=keyword, type=1,
                                         limit=limit, offset=offset)
        self._raise_if_auth_failed(payload)
        result = payload.get("result")
        songs = result.get("songs") if isinstance(result, dict) else None
        return [song for song in songs or [] if isinstance(song, dict)]

    async def playlist_create(self, name: str, privacy: int = 0) -> tuple[int, dict[str, Any]]:
        payload, _ = await self._call("playlist_create", name=name, privacy=privacy)
        self._raise_if_auth_failed(payload)
        created = payload.get("playlist") if isinstance(payload.get("playlist"), dict) else {}
        try:
            playlist_id = int(payload.get("id") or created.get("id") or 0)
        except (TypeError, ValueError):
            playlist_id = 0
        return playlist_id, payload

    async def playlist_track_add(self, playlist_id: int, song_ids: list[int]) -> dict[str, Any]:
        ids = ",".join(str(int(song_id)) for song_id in song_ids if song_id)
        # 见上：音乐歌单用 /playlist/tracks?op=add。
        payload, _ = await self._call("playlist_tracks", op="add", pid=playlist_id, tracks=ids)
        self._raise_if_auth_failed(payload)
        return payload

    async def playlist_delete(self, playlist_id: int) -> dict[str, Any]:
        """删除一个自建歌单（清理失败/半成品歌单用，不可逆）。"""
        payload, _ = await self._call("playlist_delete", id=int(playlist_id))
        self._raise_if_auth_failed(payload)
        return payload

    # ---------------- 平台推荐 ----------------

    async def daily_songs(self) -> list[dict[str, Any]]:
        payload, _ = await self._call("recommend_songs")
        data = payload.get("data")
        songs = data.get("dailySongs") if isinstance(data, dict) else None
        return [song for song in songs or [] if isinstance(song, dict)]

    async def recommend_playlists(self, limit: int = 10) -> list[dict[str, Any]]:
        payload, _ = await self._call("recommend_resource")
        playlists = payload.get("recommend")
        if auth_failed(payload) or not playlists:
            payload, _ = await self._call("personalized", limit=limit)
            playlists = payload.get("result")
        return [item for item in playlists or [] if isinstance(item, dict)][:limit]


def make_ncm_client(cookie: str = "", base: str | None = None):
    """Pick the backend: local SDK by default, container only if asked/needed."""
    backend = str(os.environ.get("NCM_BACKEND") or "sdk").strip().lower()
    if backend != "http":
        try:
            import MusicLibrary  # noqa: F401  (只做可用性探测)

            return NcmSdkClient(cookie=cookie, base=base)
        except ImportError as exc:
            print(f"[ncm] 本地后端不可用（{exc}），回落到 HTTP 容器：{BASE}", file=sys.stderr,
                  flush=True)
    return NcmApiClient(base=base, cookie=cookie)


async def ncm_credential(*, force: bool = False) -> str:
    with vault.vault_lock():
        cookie = vault.ncm_cookie()
        if not vault.ncm_cookie_is_valid(cookie):
            raise NcmAuthError(NCM_AUTH_HINT)
        # /login/refresh 每次成功都会重置 Cookie 寿命，所以按 TTL 主动续一次比等它失效再救更稳。
        if force or vault.ncm_needs_refresh():
            new_cookie, _payload = await make_ncm_client(cookie=cookie).refresh_cookie()
            if vault.ncm_cookie_is_valid(new_cookie):
                cookie = vault.merge_cookie(cookie, new_cookie)
                vault.set_ncm(cookie)
        return cookie

async def _ncm_run(operation):
    cookie = await ncm_credential()
    try:
        return await operation(cookie)
    except NcmAuthError:
        cookie = await ncm_credential(force=True)
        return await operation(cookie)

async def ncm_liked_songs() -> tuple[list[tuple[str, str]], list[str], dict[str, Any]]:
    """Return (songs, sources, diagnostics). Cookie-based, no login session needed."""
    diag: dict[str, Any] = {"configured": vault.ncm_cookie_is_valid(vault.ncm_cookie()), "errors": []}
    if not diag["configured"]:
        diag["need_login"] = True
        diag["errors"].append(NCM_AUTH_HINT)
        return [], [], diag

    async def _fetch(cookie: str):
        client = make_ncm_client(cookie=cookie)
        status = await client.login_status()
        account = (status.get("data") or {}).get("account") or {}
        uid = str(account.get("id") or "")
        if not uid:
            # 有效 MUSIC_U 必定能换回账号；拿不到就是 Cookie 死了，交给 _ncm_run 续期重试。
            raise NcmAuthError("网易云 /login/status 未返回账号")
        diag["uid"] = uid
        playlists = await client.user_playlists(uid)
        diag["playlists"] = len(playlists)
        titles = [str(pl.get("name") or "") for pl in playlists]
        diag["playlist_titles"] = titles[:20]
        targets = [pl for pl in playlists if NCM_LIKED in str(pl.get("name") or "")]
        if not targets and playlists:
            targets = [playlists[0]]
        songs: list[tuple[str, str]] = []
        sources: list[str] = []
        for pl in targets:
            tracks = await client.playlist_tracks(int(pl.get("id") or 0))
            for track in tracks:
                if isinstance(track, dict):
                    _remember(track)
                artists = track.get("ar") or track.get("artists") or [{}]
                artist = str(artists[0].get("name") or "") if artists else ""
                name = str(track.get("name") or "")
                if name:
                    songs.append((name, artist))
            sources.append(f"{pl.get('name', '?')}({len(tracks)})")
        deduped: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in songs:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        if len(deduped) != len(songs):
            sources.append(f"去重后({len(deduped)})")
        return deduped, sources

    try:
        songs, sources = await _ncm_run(_fetch)
    except NcmAuthError as exc:
        diag["need_login"] = True
        diag["errors"].append(f"{exc}")
        return [], [], diag
    except Exception as exc:
        diag["errors"].append(f"{exc.__class__.__name__}: {exc}")
        return [], [], diag
    return songs, sources, diag

def _ncm_song_pairs(songs: Any) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for song in songs or []:
        if not isinstance(song, dict):
            continue
        _remember(song)
        name = str(song.get("name") or "").strip()
        artists = song.get("ar") or song.get("artists") or [{}]
        artist = str(artists[0].get("name") or "").strip() if artists else ""
        if name:
            pairs.append((name, artist))
    return pairs

async def ncm_recommendations() -> tuple[list[tuple[str, str]], list[str], dict[str, Any]]:
    """网易云的今日推荐：日推歌曲 + 推荐歌单（连歌单里的歌一起取）。"""
    diag: dict[str, Any] = {"configured": vault.ncm_cookie_is_valid(vault.ncm_cookie()),
                            "errors": []}
    if not diag["configured"]:
        diag["need_login"] = True
        return [], [], diag

    async def _fetch(cookie: str):
        client = make_ncm_client(cookie=cookie)
        songs: list[tuple[str, str]] = []
        sources: list[str] = []
        daily = _ncm_song_pairs(await client.daily_songs())
        songs.extend(daily)
        sources.append(f"每日推荐({len(daily)})")
        try:
            playlists = await client.recommend_playlists(REC_PLAYLIST_LIMIT)
        except Exception as exc:
            diag["errors"].append(f"推荐歌单: {exc.__class__.__name__}: {exc}")
            playlists = []
        for playlist in playlists[:REC_PLAYLIST_LIMIT]:
            title = str(playlist.get("name") or "?")
            try:
                tracks = await client.playlist_tracks(int(playlist.get("id") or 0),
                                                      limit=REC_PLAYLIST_TRACKS, max_pages=1)
            except Exception as exc:
                diag["errors"].append(f"歌单 {title}: {exc.__class__.__name__}: {exc}")
                continue
            batch = _ncm_song_pairs(tracks)
            songs.extend(batch)
            sources.append(f"{title}({len(batch)})")
        return songs, sources

    try:
        songs, sources = await _ncm_run(_fetch)
    except NcmAuthError as exc:
        diag["need_login"] = True
        diag["errors"].append(f"{exc}")
        return [], [], diag
    except Exception as exc:
        diag["errors"].append(f"{exc.__class__.__name__}: {exc}")
        return [], [], diag
    deduped = dedupe(songs)
    if len(deduped) != len(songs):
        sources.append(f"去重后({len(deduped)})")
    return deduped, sources, diag

def _ncm_artists(song: dict[str, Any]) -> list[str]:
    artists = song.get("ar") or song.get("artists") or []
    return [str(item.get("name") or "").strip() for item in artists if isinstance(item, dict)]

def _ncm_pick(candidates: list[Any], expected_artist: str, seen: set[int],
              expected_name: str = ""):
    """挑一首最像的（网易云的歌手在 ar[] 里）。

    歌名对不上的一律不认：搜索接口只认关键词，返回的可能是完全无关的歌，
    拿它当「匹配成功」会把乱七八糟的歌塞进歌单。歌手对得上的优先，否则用第一首同名的。
    """
    artist_key, fallback = _norm(expected_artist), None
    for item in candidates or []:
        if not isinstance(item, dict):
            continue
        try:
            song_id = int(item.get("id") or 0)
        except (TypeError, ValueError):
            song_id = 0
        if not song_id or song_id in seen:
            continue
        if not name_matches(item.get("name"), expected_name):
            continue
        if fallback is None:
            fallback = item
        if artist_key and any(_norm(name) == artist_key for name in _ncm_artists(item)):
            return item
    return fallback

async def force_refresh_ncm() -> dict[str, Any]:
    cookie = vault.ncm_cookie()
    if not vault.ncm_cookie_is_valid(cookie):
        return need_login("netease", NCM_AUTH_HINT)
    with vault.vault_lock():
        new_cookie, payload = await make_ncm_client(cookie=cookie).refresh_cookie()
        if vault.ncm_cookie_is_valid(new_cookie):
            new_cookie = vault.merge_cookie(cookie, new_cookie)
            vault.set_ncm(new_cookie)
        return {"ok": bool(new_cookie), "service": "netease", "response_code": payload.get("code")}


class NeteasePlatform(MusicPlatform):
    """网易云音乐平台。"""

    key = Platform.NETEASE
    label = "网易云音乐"
    default_method = "cookie"

    # ---------------- 凭证 ----------------

    def status(self) -> dict[str, Any]:
        return vault.status()["netease"]

    def auth_hint(self) -> str:
        return NCM_AUTH_HINT

    def login_methods(self) -> list[dict[str, str]]:
        return [
            {"method": "cookie", "label": "直接导入 Cookie（推荐）",
             "account": "Cookie 对应的网易云账号", "needs": "MUSIC_U=...; __csrf=...",
             "note": "用 credential_set_cookie，无需扫码/验证码"},
            {"method": "phone_code", "label": "手机验证码",
             "account": "手机号对应的网易云账号", "needs": "phone + 短信验证码", "note": "无需二维码"},
            {"method": "qr", "label": "网易云 App 扫码",
             "account": "App 当前登录账号", "needs": "网易云 App 扫一扫", "note": "180 秒有效"},
        ]

    def scan_hint(self, method: str) -> str:
        if method == "phone_code":
            return "短信验证码登录，无需二维码。收到验证码后调用 login_submit。"
        return "用【网易云音乐 App 的扫一扫】扫码并确认，再调用 login_poll。"

    def set_cookie(self, value: str, uid: str = "") -> dict[str, Any]:
        vault.set_ncm(value, {"uid": uid, "source": "cookie_import"})
        return {"ok": True, "service": self.key.value, "source": "cookie_import",
                "hint": "调用 credential_status 或 analyze_music_profile 验证"}

    def logout(self) -> None:
        vault.write_secret(vault.NCM_FILE, {})

    async def refresh(self) -> dict[str, Any]:
        return await force_refresh_ncm()

    # ---------------- 登录 ----------------

    async def login_start(self, method: str, phone: str = "", ctcode: int = 86) -> LoginStart:
        client = make_ncm_client()
        if method == "phone_code":
            if not phone.isdigit() or len(phone) < 6:
                return LoginStart(ok=False, status="failed", error="手机号格式不正确")
            payload = await client.captcha_sent(phone, ctcode)
            if int(payload.get("code") or 0) != 200:
                return LoginStart(ok=False, status="failed", error=f"发送验证码失败: {payload}")
            return LoginStart(ok=True, status="awaiting_code",
                              ticket=LoginTicket({"phone": phone, "ctcode": ctcode}),
                              message=f"验证码已发送到 {phone}")
        if method != "qr":
            return LoginStart(ok=False, status="failed", error=f"不支持的登录方式: {method}")
        key = await client.qr_key()
        if not key:
            return LoginStart(ok=False, status="failed", error="获取二维码 key 失败")
        qrimg = await client.qr_create(key)
        return LoginStart(ok=True, status="waiting", ticket=LoginTicket({"unikey": key}),
                          qr_image=qrimg, message="二维码已作为图片一并返回。扫码后调用 login_poll。")

    async def login_poll(self, ticket: LoginTicket) -> LoginState:
        code, message, payload, response = await make_ncm_client().qr_check(
            str(ticket.payload.get("unikey") or ""))
        # 800 过期 / 801 待扫 / 802 待确认 / 803 授权成功
        if code == 803:
            cookie = _extract_cookie(payload, response)
            if not cookie:
                return LoginState(ok=False, status="failed", error="扫码成功但未取到 Cookie")
            if not vault.ncm_cookie_is_valid(cookie):
                # 残片 Cookie（缺 MUSIC_U）落库等于假装登录成功，后面每个接口都会 301。
                return LoginState(ok=False, status="failed",
                                  error=f"扫码成功但取到的 Cookie 不完整（缺 MUSIC_U）: {str(payload)[:300]}")
            profile = payload.get("profile") or {}
            vault.set_ncm(cookie, {"uid": str(profile.get("userId") or "")})
            return LoginState(ok=True, status="succeeded",
                              message="网易云登录成功，Cookie 已写入共享凭证库")
        status = {800: "expired", 801: "waiting", 802: "scanned"}.get(code, "waiting")
        return LoginState(ok=status != "expired", status=status,
                          message=message or f"二维码状态 {code}")

    async def login_submit(self, ticket: LoginTicket, code: str) -> LoginState:
        client = make_ncm_client()
        phone = str(ticket.payload.get("phone") or "")
        ctcode = int(ticket.payload.get("ctcode") or 86)
        verified = await client.captcha_verify(phone, code, ctcode)
        if int(verified.get("code") or 0) != 200:
            return LoginState(ok=False, status="failed", error=f"验证码校验失败: {verified}")
        cookie, payload, response = await client.login_cellphone(phone, code, ctcode)
        if not cookie:
            return LoginState(ok=False, status="failed", error=f"手机号登录失败: {str(payload)[:300]}")
        profile = payload.get("profile") or {}
        vault.set_ncm(cookie, {"uid": str(profile.get("userId") or "")})
        return LoginState(ok=True, status="succeeded",
                          message="网易云验证码登录成功，Cookie 已写入共享凭证库")

    # ---------------- 基础能力 ----------------

    async def favorites(self) -> tuple[list[Song], list[str], dict[str, Any]]:
        songs, sources, diag = await ncm_liked_songs()
        return [Song(name, artist) for name, artist in songs], sources, diag

    async def recommendations(self) -> tuple[list[Song], list[str], dict[str, Any]]:
        songs, sources, diag = await ncm_recommendations()
        return [Song(name, artist) for name, artist in songs], sources, diag

    async def search(self, name: str, artist: str = "") -> Any:
        async def _find(cookie: str):
            candidates = await make_ncm_client(cookie=cookie).cloud_search(
                f"{name} {artist}".strip(), limit=5)
            return _ncm_pick(candidates, artist, set(), name)

        return await _ncm_run(_find)

    async def create_playlist(self, name: str, songs: list[Song], count: int) -> CreateResult:
        """建歌单。歌名和曲目都由上层（compose.py）定好，这里只负责落地。"""
        if not vault.ncm_cookie_is_valid(vault.ncm_cookie()):
            return CreateResult(platform=self.key.value, ok=False, playlist_name=name,
                                error=NCM_AUTH_HINT,
                                extra={"need_login": True, "service": self.key.value})
        selected = songs[:max(1, count)]

        async def _create(cookie: str):
            client = make_ncm_client(cookie=cookie)
            matched: list[int] = []
            matched_meta: list[dict[str, str]] = []
            failed: list[str] = []
            ids: set[int] = set()
            limited = 0
            for index, song in enumerate(selected):
                query = f"{song.name} {song.artist}".strip()
                known = _KNOWN_IDS.get(song_key(song.name, song.artist))
                if known and known not in ids:
                    ids.add(known)
                    matched.append(known)
                    matched_meta.append(song.as_dict())
                    continue
                try:
                    candidates = await client.cloud_search(query, limit=5)
                except NcmRateLimitError as exc:
                    limited += 1
                    if limited >= 3:
                        # 连续几首都退避不回来，说明这轮被限流卡死了：剩下的直接记下来，
                        # 不然后面每首都要再退避 37 秒，整条链路会挂在这里十几分钟。
                        failed.extend(f"{s.name} {s.artist}".strip() + "（限流跳过）"
                                      for s in selected[index:])
                        break
                    failed.append(f"{query}（限流未恢复：{exc}）")
                    continue
                except Exception as exc:
                    failed.append(f"{query}（搜索出错：{exc.__class__.__name__}）")
                    continue
                limited = 0
                chosen = _ncm_pick(candidates, song.artist, ids, song.name)
                try:
                    song_id = int((chosen or {}).get("id") or 0)
                except (TypeError, ValueError):
                    song_id = 0
                if not song_id:
                    failed.append(query)
                    continue
                ids.add(song_id)
                matched.append(song_id)
                matched_meta.append(song.as_dict())
            if not matched:
                return CreateResult(platform=self.key.value, ok=False, playlist_name=name,
                                    failed=failed, error="没有匹配到任何歌曲")
            playlist_id, payload = await client.playlist_create(name)
            if not playlist_id:
                return CreateResult(platform=self.key.value, ok=False, playlist_name=name,
                                    failed=failed, error="网易云未返回歌单 id",
                                    extra={"response_code": payload.get("code"),
                                           "response": payload.get("msg")})
            added = await client.playlist_track_add(playlist_id, matched)
            return CreateResult(platform=self.key.value, ok=True, playlist_name=name,
                                playlist_id=str(playlist_id), added=len(matched),
                                failed=failed, matched=matched_meta,
                                extra={"response_code": added.get("code")})

        try:
            return await _ncm_run(_create)
        except NcmAuthError as exc:
            return CreateResult(platform=self.key.value, ok=False, playlist_name=name,
                                error=f"{exc}；{NCM_AUTH_HINT}",
                                extra={"need_login": True, "service": self.key.value})
        except Exception as exc:
            return CreateResult(platform=self.key.value, ok=False, playlist_name=name,
                                error=f"{exc.__class__.__name__}: {exc}")

    async def delete_playlist(self, playlist_id: str) -> dict[str, Any]:
        """删除网易云歌单。"""
        cookie = vault.ncm_cookie()
        if not cookie:
            return {"ok": False, "error": "网易云未登录", "service": self.key.value}
        try:
            pid = int(str(playlist_id).strip())
        except (TypeError, ValueError):
            return {"ok": False, "error": "playlist_id 必须是数字", "service": self.key.value}
        try:
            client = make_ncm_client(cookie=cookie)
            payload = await client.playlist_delete(pid)
            ok = int(payload.get("code") or 0) in (200, 0)
            return {"ok": ok, "service": self.key.value,
                    "deleted_id": pid,
                    "error": "" if ok else json.dumps(payload, ensure_ascii=False)[:200]}
        except NcmAuthError as exc:
            return {"ok": False, "error": f"{exc}；{NCM_AUTH_HINT}", "service": self.key.value}
        except Exception as exc:
            return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}",
                    "service": self.key.value}

    async def my_playlists(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """列出网易云创建的歌单。"""
        diag: dict[str, Any] = {"errors": []}
        cookie = vault.ncm_cookie()
        if not cookie:
            diag["need_login"] = True
            return [], diag
        try:
            client = make_ncm_client(cookie=cookie)
            uid = str(vault.get_ncm().get("uid") or "")
            if not uid:
                status = await client.login_status()
                uid = str(((status.get("data") or {}).get("account") or {}).get("id") or "")
            if not uid:
                diag["errors"].append("无法获取网易云 uid")
                return [], diag
            playlists = await client.user_playlists(uid)
            items = []
            for pl in playlists:
                items.append({
                    "id": str(pl.get("id") or ""),
                    "name": str(pl.get("name") or ""),
                    "track_count": pl.get("trackCount"),
                })
            diag["count"] = len(items)
            return items, diag
        except NcmAuthError as exc:
            diag["errors"].append(str(exc))
            diag["need_login"] = True
            return [], diag
        except Exception as exc:
            diag["errors"].append(f"{exc.__class__.__name__}: {exc}")
            return [], diag
