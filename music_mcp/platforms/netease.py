"""网易云音乐的「基础能力」：登录、凭证、收藏、今日推荐、搜索、建歌单。

后端只有一套：**纯 Python 直连官方 WebAPI**（weapi/eapi 参数自己算），实现在
`ncm_native.py`（路由层）与 `ncm_crypto.py`（加密层）。零原生依赖、不需要容器，
Windows / Linux 一个样。

历史提醒（别再往回加）：这里曾经有本地 `MusicLibrary` 引擎和 ncm-api sidecar 两条路，
都删了 —— pymusiclibrary 的 manylinux wheel 里装的是 Windows DLL，Linux 上永远起不来；
容器那条只是把同一套公开协议换个地方算，白多一个服务。

踩过的坑（详见各函数注释）：歌单曲目直连老的明文 `/api/playlist/detail`（天然给完整
tracks）；上游 405「操作频繁」要退避重试，搜索这类接口尤其凶。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from typing import Any

import httpx

from .. import vault
from .core.base import (CreateResult, LoginStart, LoginState, LoginTicket, MusicPlatform,
                   Platform, Song, dedupe, name_matches, normalize, song_key)
from .core.ncm_native import NcmNativeClient

_norm = normalize  # 旧代码里的名字

NCM_LIKED = "喜欢的音乐"
# 「我喜欢的音乐」的 specialType（实测：我喜欢=5、自建=0、年度歌单=20、十年精选=700）
NCM_LIKED_SPECIAL_TYPE = 5
# 扫码登录那个 unikey 的真实寿命（秒）。2026-11-07 实测：第 291 秒仍回 801（等待扫码），
# 第 306 秒回 800（二维码不存在或已过期）→ 取 300。以前这里写 180、会话却报 600，两个都不对。
NCM_QR_TTL = 300
NCM_AUTH_HINT = ("网易云未登录或 Cookie 已失效：优先 credential_set_cookie(service='netease', "
                 "value='MUSIC_U=...; __csrf=...')，或 login_start(service='netease', "
                 "method='phone_code'|'qr') 走短信/扫码。")
REC_PLAYLIST_LIMIT = 3      # 最多解析几个推荐歌单
REC_PLAYLIST_TRACKS = 50    # 每个推荐歌单最多取多少首


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

# 废弃的 NCM_BACKEND 只提示一次（make_ncm_client 每次操作都会被调一次）。
_WARNED_LEGACY = [False]


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


def make_ncm_client(cookie: str = "", base: str | None = None) -> NcmNativeClient:
    """网易云客户端工厂 —— 只有一套实现：纯 Python 直连官方 WebAPI。

    这里曾经有三套后端（本地 MusicLibrary 引擎 / ncm-api sidecar 容器 / 现在这个），
    靠 `NCM_BACKEND` 切来切去。两条老路现在都删了：

      * 引擎：pymusiclibrary 的 manylinux wheel 里装的是 Windows DLL，Linux 上永远起不来；
      * 容器：加密和路由本来就是公开协议（见 ncm_crypto.py），没必要再多跑一个服务。

    `NCM_BACKEND` 只做兼容 —— 设了别的值也照样走 native，只在 stderr 提示一次，
    免得老部署里残留的环境变量让人误以为还在切后端。
    """
    legacy = str(os.environ.get("NCM_BACKEND") or "").strip().lower()
    if legacy not in ("", "native") and not _WARNED_LEGACY[0]:
        _WARNED_LEGACY[0] = True      # 每次调用都来一遍太吵，一个进程提示一次就够
        print(f"[ncm] NCM_BACKEND={legacy} 已废弃（现在只有纯 Python 一套后端，"
              f"native / 引擎 / 容器都不再切换），忽略", file=sys.stderr, flush=True)
    return NcmNativeClient(cookie=cookie, base=base)


async def ncm_ensure_uid(timeout: float = 10.0) -> str:
    """把账号 uid 补进 vault。

    为什么需要：`credential_set_cookie` 导入时通常只给 Cookie（没有 uid），于是
    `credential_status` 里 netease.uid 永远是空 —— 而 QQ 那边有 musicid，看起来就像
    网易云"没登录成功"。有效 MUSIC_U 一问 `/login/status` 就有 uid，问一次落库即可。
    """
    cookie = vault.ncm_cookie()
    if not vault.ncm_cookie_is_valid(cookie):
        return ""
    uid = str(vault.get_ncm().get("uid") or "")
    if uid:
        return uid
    try:
        status = await asyncio.wait_for(make_ncm_client(cookie=cookie).login_status(), timeout)
    except Exception:  # noqa: BLE001 - 补信息失败不该让调用方出错（离线/限流都算）
        return ""
    account = (status.get("data") or {}).get("account") or {}
    uid = str(account.get("id") or "")
    if uid:
        vault.set_ncm(cookie, {"uid": uid})
    return uid


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
    # 顺手把缺失的 uid 补上（不占锁，失败无所谓）：任何取数路径走过一次，
    # credential_status 之后就能像 QQ 一样显示账号。
    if not vault.get_ncm().get("uid"):
        await ncm_ensure_uid()
    return cookie

async def _ncm_run(operation):
    cookie = await ncm_credential()
    try:
        return await operation(cookie)
    except NcmAuthError:
        cookie = await ncm_credential(force=True)
        return await operation(cookie)

def _pick_liked_ncm(playlists: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """挑出「我喜欢的音乐」，并说明按什么挑中的。

    优先级：specialType(5) → 歌单名 → 第一个歌单。
    specialType 才是结构化依据（实测：我喜欢=5，自建=0，年度歌单=20，十年精选=700）；
    只按名字挑，遇到用户把歌单改名、或自建歌单叫「喜欢的音乐」都会判错。
    """
    picked = [pl for pl in playlists
              if int(pl.get("specialType") or 0) == NCM_LIKED_SPECIAL_TYPE]
    if picked:
        return picked, f"specialType={NCM_LIKED_SPECIAL_TYPE}"
    picked = [pl for pl in playlists if NCM_LIKED in str(pl.get("name") or "")]
    if picked:
        return picked, "name"
    return ([playlists[0]], "first_playlist") if playlists else ([], "none")


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
        targets, picked_by = _pick_liked_ncm(playlists)
        diag["liked_picked_by"] = picked_by
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
    default_method = "phone_code"

    # ---------------- 凭证 ----------------

    def status(self) -> dict[str, Any]:
        # 只有一套后端（纯 Python 直连），顺手报出来 —— agent 排查"为什么连不上"
        # 时能一眼看到用的不是容器/引擎，省得去猜部署形态。
        return {**vault.status()["netease"], "backend": "native"}

    async def enrich_status(self, status: dict[str, Any]) -> dict[str, Any]:
        """把 uid 补上再报出去（Cookie 导入时没有 uid，得问一次上游；见 ncm_ensure_uid）。"""
        out = {**status, "backend": "native"}
        if not out.get("uid"):
            uid = await ncm_ensure_uid()
            if uid:
                out["uid"] = uid
        return out

    def auth_hint(self) -> str:
        return NCM_AUTH_HINT

    def login_methods(self) -> list[dict[str, str]]:
        return [
            {"method": "phone_code", "label": "手机验证码（推荐）",
             "account": "手机号对应的网易云账号", "needs": "phone + 短信验证码",
             "note": "无需二维码、不用截图，跨设备最稳"},
            {"method": "cookie", "label": "直接导入 Cookie",
             "account": "Cookie 对应的网易云账号", "needs": "MUSIC_U=...; __csrf=...",
             "note": "用 credential_set_cookie；浏览器已登录时复制一段请求头即可"},
            {"method": "qr", "label": "网易云 App 扫码",
             "account": "App 当前登录账号", "needs": "网易云 App 扫一扫",
             "note": f"约 {NCM_QR_TTL} 秒有效（2026-11-07 实测：291 秒仍可用、306 秒已过期）"},
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
            # 票据只有手机号，submit 时现开 client —— 进程重启后照样能提交验证码
            return LoginStart(ok=True, status="awaiting_code",
                              ticket=LoginTicket({"phone": phone, "ctcode": ctcode}, None, True),
                              message=f"验证码已发送到 {phone}")
        if method != "qr":
            return LoginStart(ok=False, status="failed", error=f"不支持的登录方式: {method}")
        key = await client.qr_key()
        if not key:
            return LoginStart(ok=False, status="failed", error="获取二维码 key 失败")
        qrimg = await client.qr_create(key)
        # 票据只有 unikey（poll 时现开 client 查状态）→ 可落盘、可跨进程续
        return LoginStart(ok=True, status="waiting",
                          ticket=LoginTicket({"unikey": key}, None, True),
                          qr_image=qrimg, expires_in=NCM_QR_TTL,
                          message="二维码已作为图片一并返回。扫码后调用 login_poll。")

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

    async def search_items(self, keyword: str, limit: int = 10,
                           ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """搜索歌曲，返回统一结构（网易云的歌手在 ar[] 里）。"""
        async def _find(cookie: str):
            client = make_ncm_client(cookie=cookie)
            rows = await client.cloud_search(keyword.strip(),
                                             limit=max(1, min(int(limit or 10), 50)))
            items: list[dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict) or not row.get("name"):
                    continue
                artists = row.get("ar") or row.get("artists") or []
                album = row.get("al") or row.get("album") or {}
                items.append({
                    "name": str(row.get("name") or ""),
                    "artist": str((artists[0] or {}).get("name") or "") if artists else "",
                    "id": str(row.get("id") or ""),
                    "album": str(album.get("name") or ""),
                    "duration_ms": int(row.get("dt") or row.get("duration") or 0),
                })
            return items

        return await _ncm_run(_find), {}

    async def playlist_tracks(self, playlist_id: str, limit: int = 0,
                              ) -> tuple[list[Song], dict[str, Any]]:
        """读某个歌单的曲目（playlist_id 就是 my_playlists 的 id）。"""
        pid = str(playlist_id or "").strip()
        if not pid.isdigit():
            return [], {"error": f"playlist_id 必须是数字，收到 {playlist_id!r}"}

        async def _fetch(cookie: str):
            client = make_ncm_client(cookie=cookie)
            return await client.playlist_tracks(int(pid))

        tracks = await _ncm_run(_fetch)
        songs: list[Song] = []
        for track in tracks:
            if not isinstance(track, dict):
                continue
            _remember(track)          # 顺手记住 id：以后建单/加歌不用再搜一遍
            artists = track.get("ar") or track.get("artists") or []
            name = str(track.get("name") or "")
            if name:
                songs.append(Song(name, str((artists[0] or {}).get("name") or "") if artists else ""))
        total = len(songs)
        return (songs[:limit] if limit and limit > 0 else songs), {"total": total}

    async def add_tracks(self, playlist_id: str, songs: list[Song]) -> dict[str, Any]:
        """给已有歌单加歌：歌名 → 平台内搜索 → 拿 id → playlist_track_add。"""
        pid = str(playlist_id or "").strip()
        if not pid.isdigit():
            return {"ok": False, "service": self.key.value,
                    "error": f"playlist_id 必须是数字，收到 {playlist_id!r}"}
        if not vault.ncm_cookie_is_valid(vault.ncm_cookie()):
            return {"ok": False, "service": self.key.value, "error": NCM_AUTH_HINT,
                    "need_login": True}

        async def _add(cookie: str):
            client = make_ncm_client(cookie=cookie)
            matched: list[int] = []
            matched_meta: list[dict[str, str]] = []
            failed: list[str] = []
            ids: set[int] = set()
            for song in songs:
                query = f"{song.name} {song.artist}".strip()
                known = _KNOWN_IDS.get(song_key(song.name, song.artist))
                if known and known not in ids:
                    ids.add(known)
                    matched.append(known)
                    matched_meta.append(song.as_dict())
                    continue
                try:
                    candidates = await client.cloud_search(query, limit=5)
                except Exception as exc:  # noqa: BLE001 - 单首失败不该毁掉整批
                    failed.append(f"{query}（搜索出错：{exc.__class__.__name__}）")
                    continue
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
                return {"ok": False, "service": self.key.value, "playlist_id": pid,
                        "failed": failed, "error": "没有匹配到任何歌曲"}
            result = await client.playlist_track_add(int(pid), matched)
            code = int((result or {}).get("code") or 0)
            return {"ok": code == 200, "service": self.key.value, "playlist_id": pid,
                    "added": len(matched) if code == 200 else 0, "matched": matched_meta,
                    "failed": failed, "track_ids": matched,
                    "error": "" if code == 200 else json.dumps(result, ensure_ascii=False)[:200]}

        return await _ncm_run(_add)

    async def recently_played(self, limit: int = 50,
                              ) -> tuple[list[Song], dict[str, Any]]:
        """网易云听歌排行（最近播放）。type=0 所有时间，拿不到就退一周。"""
        limit = max(1, min(int(limit or 50), 200))
        async def _fetch(cookie: str):
            client = make_ncm_client(cookie=cookie)
            uid = await ncm_ensure_uid()
            if not uid:
                return [], {"error": "拿不到网易云 uid（Cookie 可能已失效）"}
            rows = await client.play_records(uid, type_=0, limit=limit)
            picked = "allData"
            if not rows:
                rows = await client.play_records(uid, type_=1, limit=limit)
                picked = "weekData"
            return rows, {"uid": uid, "bucket": picked, "rows": len(rows)}

        rows, diag = await _ncm_run(_fetch)
        songs = [Song(str(row.get("name") or ""), str(row.get("artist") or "")) for row in rows]
        songs = [song for song in songs if song.name]
        details = [{"name": row.get("name"), "artist": row.get("artist"),
                    "play_count": row.get("play_count")} for row in rows]
        return songs, {**diag, "details": details[:limit]}

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
        """列出网易云创建的歌单（统一字段：id / name / track_count）。"""
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
                    # 统一成整数：上游没给就是 0，别再往外吐 null
                    "track_count": int(pl.get("trackCount") or 0),
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
