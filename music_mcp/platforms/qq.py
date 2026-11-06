"""QQ 音乐的「基础能力」：登录、凭证、收藏、今日推荐、搜索、建歌单。

凭证是 refresh_token 会轮转的 Credential，读写一律走 vault 的跨进程锁 ——
这是「同一份凭证只被一个进程刷新」的唯一保证。

下面 qq_* / _* 这些函数是历史实现**原样搬过来**的（一行没改，包括注释），
QQMusicPlatform 只负责把它们的 (歌名, 歌手) 元组口径转成统一的 Song。
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import anyio

from qqmusic_api import Client, Credential
from qqmusic_api.models.login import PhoneLoginEvents, QRCodeLoginEvents, QRLoginType
from qqmusic_api.modules.login_utils import PhoneLoginSession
from qqmusic_api.modules.search import SearchType

from .. import vault
from .base import (CreateResult, LoginStart, LoginState, LoginTicket, MusicPlatform,
                   Platform, Song, dedupe, name_matches, normalize)

_norm = normalize  # 旧代码里的名字

QQ_AUTH_HINT = ("QQ 音乐未登录或凭证不完整：login_start(service='qq_music', method='phone_code', "
                "phone='...') 后 login_submit。")
LIKED_TITLES = {"我喜欢", "我的收藏", "喜欢"}
EXPIRED_CODES = {1000, 2001, 104400, 104401}
REC_PLAYLIST_LIMIT = 3      # 最多解析几个推荐歌单
REC_PLAYLIST_TRACKS = 50    # 每个推荐歌单最多取多少首
MOBILE_POLL_WAIT = 8        # mobile_qr 单次 poll 最多等多久（外层 login.py 限 12 秒，别超）

QR_HINTS = {
    "qq_qr": "必须用【手机 QQ 的扫一扫】扫这个码（微信、系统相机、QQ 音乐 App 都不行，会打开网页跳到 QQ 首页）。扫码后 QQ 会弹出「QQ音乐登录确认」，点确认，再调用 login_poll。",
    "wx_qr": "用【微信扫一扫】扫码并在手机上确认，再调用 login_poll。",
    "mobile_qr": "用【QQ 音乐 App 内的扫一扫】扫码并确认，再调用 login_poll。想用手机 QQ 扫请改用 qq_qr。",
    "phone_code": "短信验证码登录，无需二维码，跨设备最可靠。收到验证码后调用 login_submit。",
}


# 二维码登录的事件 -> (对外状态, 给用户看的话)。qq_qr / wx_qr / mobile_qr 共用。
QR_EVENT_STATUS = {
    QRCodeLoginEvents.SCAN: ("scanned", "已扫码，请在手机上点确认"),
    QRCodeLoginEvents.CONF: ("confirming", "登录确认中，请稍候再 poll"),
    QRCodeLoginEvents.TIMEOUT: ("expired", "二维码已过期，请重新 login_start"),
    QRCodeLoginEvents.REFUSE: ("failed", "QQ 返回 68：二维码失效或被取消。常见原因是扫了过期/被新码顶替的旧码，或在手机上点了取消。请重新 login_start，180 秒内用手机 QQ 扫一扫。"),
}

# mobile_qr 走 MQTT 推送：流一连上库就先 yield 一个 SCAN（表示「已就绪」），
# 真正的扫码是 CONF。所以这条路上 SCAN 要按「等待扫码」讲，不能照搬 qq_qr 的文案。
MOBILE_QR_SCAN_STATUS = ("waiting", "二维码已就绪，等待扫码")


async def _close_handle(handle: dict[str, Any] | None) -> None:
    """关掉 QQ 登录会话里的 live Client 和后台 MQTT 任务（作废/成功/失败时都要调）。"""
    if not handle:
        return
    task = handle.get("task")
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 收尾失败无所谓
            pass
    client = handle.get("client")
    if client is None:
        return
    try:
        await client.__aexit__(None, None, None)
    except Exception:
        pass


async def _mobile_qr_worker(client: Any, qr: Any, queue: "asyncio.Queue[Any]",
                            deadline: float) -> None:
    """把 mobile_qr 的 MQTT 事件流搬进队列，供 login_poll 逐条取。

    ``check_qrcode`` 只认 QQ / 微信那套 qrsig 流程，MOBILE 的 identifier 是 qrcodeID，
    拿它去查必然得到「无效 qrsig」；库给手机客户端单独开了 ``checking_mobile_qrcode``
    （MQTT 长连接生成器），所以这里用后台任务把它转成队列。
    """
    try:
        async for item in client.login.checking_mobile_qrcode(qr, deadline=deadline):
            await queue.put(item)
            if item.event in {QRCodeLoginEvents.DONE, QRCodeLoginEvents.REFUSE,
                              QRCodeLoginEvents.TIMEOUT}:
                break
    except Exception as exc:  # noqa: BLE001 - 原样带给用户
        await queue.put(exc)
    finally:
        await queue.put(None)          # 流结束的哨兵


async def _poll_mobile_qr(ticket: LoginTicket) -> LoginState:
    """mobile_qr 的 poll：从 MQTT 事件队列里取一条。"""
    handle = ticket.handle or {}
    queue = handle.get("queue")
    if queue is None:
        return LoginState(ok=False, status="expired", error="登录会话已失效，请重新 login_start")
    try:
        item = await asyncio.wait_for(queue.get(), timeout=MOBILE_POLL_WAIT)
    except asyncio.TimeoutError:      # 队列只是还没来消息，会话仍然有效
        return LoginState(ok=True, status="waiting", message="等待扫码")
    if item is None:                  # 哨兵：MQTT 流结束
        await _close_handle(handle)
        ticket.handle = None
        return LoginState(ok=False, status="expired", error="二维码已过期，请重新 login_start")
    if isinstance(item, Exception):
        await _close_handle(handle)
        ticket.handle = None
        return LoginState(ok=False, status="failed", error=f"{type(item).__name__}: {item}")
    if item.event == QRCodeLoginEvents.DONE and item.credential is not None:
        vault.set_qq(_credential_payload(item.credential))
        await _close_handle(handle)
        ticket.handle = None
        return LoginState(ok=True, status="succeeded",
                          message="QQ 音乐登录成功，完整 Credential 已写入共享凭证库")
    if item.event == QRCodeLoginEvents.SCAN:
        status, message = MOBILE_QR_SCAN_STATUS
    else:
        status, message = QR_EVENT_STATUS.get(
            item.event, ("failed", f"二维码登录失败: {getattr(item.event, 'name', item.event)}"))
    if status in {"expired", "failed"}:
        await _close_handle(handle)
        ticket.handle = None
    return LoginState(ok=status not in {"expired", "failed"}, status=status, message=message)



def _to_credential(payload: dict[str, Any]):
    if hasattr(Credential, "model_validate"):
        return Credential.model_validate(payload)
    if hasattr(Credential, "parse_obj"):
        return Credential.parse_obj(payload)
    return Credential(**payload)

def _credential_payload(credential: Any) -> dict[str, Any]:
    if hasattr(credential, "model_dump"):
        return dict(credential.model_dump(by_alias=True))
    if hasattr(credential, "dict"):
        return dict(credential.dict(by_alias=True))
    return dict(credential)

async def qq_credential(*, force: bool = False):
    with vault.vault_lock():
        payload = vault.get_qq()
        if not vault.qq_is_complete(payload):
            raise RuntimeError(QQ_AUTH_HINT)
        if force or vault.qq_needs_refresh(payload):
            current = _to_credential(payload)
            async with Client(credential=current) as client:
                refreshed = await client.login.refresh_credential(current)
            payload = _credential_payload(refreshed)
            vault.set_qq(payload)
        return _to_credential(payload)

async def _qq_run(operation):
    credential = await qq_credential()
    try:
        return await operation(credential)
    except Exception as exc:
        code = getattr(exc, "code", None)
        if exc.__class__.__name__ not in {"LoginExpiredError", "NotLoginError"} and code not in EXPIRED_CODES:
            raise
        credential = await qq_credential(force=True)
        return await operation(credential)

async def qq_liked_songs() -> tuple[list[tuple[str, str]], list[str], dict[str, Any]]:
    """Return (songs, sources, diagnostics). Never raises on an empty account."""
    diag: dict[str, Any] = {"musicid": None, "created_playlists": 0, "errors": []}
    if not vault.qq_is_complete(vault.get_qq()):
        diag["need_login"] = True

    async def _fetch(credential):
        songs: list[tuple[str, str]] = []
        sources: list[str] = []
        musicid = int(getattr(credential, "musicid", 0) or 0)
        diag["musicid"] = musicid
        async with Client(credential=credential) as client:
            try:
                result = await client.user.get_created_songlist(uin=musicid, credential=credential)
                playlists = list(getattr(result, "playlists", []) or [])
            except Exception as exc:
                diag["errors"].append(f"get_created_songlist: {exc.__class__.__name__}: {exc}")
                playlists = []
            diag["created_playlists"] = len(playlists)
            titles = [str(getattr(pl, "title", "") or "") for pl in playlists]
            diag["playlist_titles"] = titles[:20]
            targets = [pl for pl in playlists if str(getattr(pl, "title", "") or "") in LIKED_TITLES]
            if not targets and playlists:
                targets = [playlists[0]]
            for pl in targets:
                page, before = 1, len(songs)
                api_total = None
                while True:
                    try:
                        detail = await client.songlist.get_detail(
                            songlist_id=getattr(pl, "id", 0), num=100, page=page
                        )
                    except Exception as exc:
                        diag["errors"].append(f"get_detail({getattr(pl, 'title', '?')}): {exc}")
                        break
                    if api_total is None:
                        api_total = getattr(detail, "total", None)
                    added = 0
                    for song in getattr(detail, "songs", []) or []:
                        name = str(getattr(song, "name", "") or getattr(song, "title", "") or "")
                        singers = getattr(song, "singer", None) or []
                        artist = str(getattr(singers[0], "name", "") or "") if singers else ""
                        if name:
                            songs.append((name, artist))
                            added += 1
                    # 分页上限只是防跑飞的保险，别当业务上限：QQ 的单歌单页数没有固定值
                    # （实测「我喜欢」754 首 = 8 页）。真实的停止条件是 hasmore 或空页。
                    if added == 0 or not getattr(detail, "hasmore", False) or page >= 100:
                        break
                    page += 1
                got = len(songs) - before
                size = api_total if isinstance(api_total, int) and api_total > 0 else got
                sources.append(f"{getattr(pl, 'title', '?')}({size})")
            # Fallback: "我喜欢" often lives in the favourite-song endpoint, not
            # in created playlists (esp. WeChat-bound accounts).
            if not songs:
                try:
                    page = 1
                    while page <= 100:
                        fav = await client.user.get_fav_song(str(musicid), page=page, num=100,
                                                            credential=credential)
                        items = list(getattr(fav, "songs", []) or [])
                        for song in items:
                            name = str(getattr(song, "name", "") or getattr(song, "title", "") or "")
                            singers = getattr(song, "singer", None) or []
                            artist = str(getattr(singers[0], "name", "") or "") if singers else ""
                            if name:
                                songs.append((name, artist))
                        if not items or not getattr(fav, "hasmore", False):
                            break
                        page += 1
                    diag["fav_songs"] = len(songs)
                    if songs:
                        sources.append(f"我喜欢({len(songs)})")
                except Exception as exc:
                    diag["errors"].append(f"get_fav_song: {exc.__class__.__name__}: {exc}")
        # 「喜欢」与「我喜欢」高度重叠（实测 754 + 585 原始条目里只有 753 首不重复），
        # 不去重会让画像统计把同一首歌算两遍、个别歌手权重翻倍。
        deduped: list[tuple[str, str]] = []
        seen_songs: set[tuple[str, str]] = set()
        for item in songs:
            if item in seen_songs:
                continue
            seen_songs.add(item)
            deduped.append(item)
        if len(deduped) != len(songs):
            sources.append(f"去重后({len(deduped)})")
            songs = deduped
        return songs, sources

    try:
        songs, sources = await _qq_run(_fetch)
    except Exception as exc:
        diag["errors"].append(f"qq_credential: {exc.__class__.__name__}: {exc}")
        return [], [], diag
    return songs, sources, diag

def _qq_song_pairs(songs: Any) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for song in songs or []:
        name = str(getattr(song, "name", "") or getattr(song, "title", "") or "").strip()
        singers = getattr(song, "singer", None) or []
        artist = str(getattr(singers[0], "name", "") or "").strip() if singers else ""
        if name:
            pairs.append((name, artist))
    return pairs

async def qq_recommendations() -> tuple[list[tuple[str, str]], list[str], dict[str, Any]]:
    """QQ 音乐的今日推荐：雷达推荐 + 猜你喜欢 + 推荐歌单（连歌单里的歌一起取）。"""
    diag: dict[str, Any] = {"errors": []}
    if not vault.qq_is_complete(vault.get_qq()):
        diag["need_login"] = True
        return [], [], diag

    async def _fetch(credential):
        songs: list[tuple[str, str]] = []
        sources: list[str] = []
        async with Client(credential=credential) as client:
            for label, factory in (
                ("雷达推荐", lambda: client.recommend.get_radar_recommend(page=1)),
                ("猜你喜欢", lambda: client.recommend.get_guess_recommend()),
            ):
                try:
                    result = await factory()
                except Exception as exc:
                    diag["errors"].append(f"{label}: {exc.__class__.__name__}: {exc}")
                    continue
                batch = _qq_song_pairs(getattr(result, "songs", None))
                songs.extend(batch)
                sources.append(f"{label}({len(batch)})")
            try:
                page = await client.recommend.get_recommend_songlist(page=1,
                                                                    num=REC_PLAYLIST_LIMIT)
                songlists = list(getattr(page, "songlists", None) or [])[:REC_PLAYLIST_LIMIT]
            except Exception as exc:
                diag["errors"].append(f"推荐歌单: {exc.__class__.__name__}: {exc}")
                songlists = []
            for item in songlists:
                title = str(getattr(item, "title", "") or "?")
                try:
                    detail = await client.songlist.get_detail(
                        songlist_id=int(getattr(item, "id", 0) or 0),
                        num=REC_PLAYLIST_TRACKS, page=1)
                except Exception as exc:
                    diag["errors"].append(f"歌单 {title}: {exc.__class__.__name__}: {exc}")
                    continue
                batch = _qq_song_pairs(getattr(detail, "songs", None))
                songs.extend(batch)
                sources.append(f"{title}({len(batch)})")
        return songs, sources

    try:
        songs, sources = await _qq_run(_fetch)
    except Exception as exc:
        diag["errors"].append(f"qq_credential: {exc.__class__.__name__}: {exc}")
        return [], [], diag
    deduped = dedupe(songs)
    if len(deduped) != len(songs):
        sources.append(f"去重后({len(deduped)})")
    return deduped, sources, diag

def _song_id(song: Any) -> int:
    try:
        return int(getattr(song, "id", 0) or 0) if hasattr(song, "id") else int(song.get("id") or 0)
    except (TypeError, ValueError):
        return 0

def _artists(song: Any) -> list[str]:
    singers = getattr(song, "singer", None) or (song.get("singer") if isinstance(song, dict) else [])
    return [str(getattr(s, "name", "") or (s.get("name") if isinstance(s, dict) else "")).strip()
            for s in singers or []]

def _title(song: Any) -> str:
    return str(getattr(song, "name", None) or (song.get("name") if isinstance(song, dict) else "")
               or "")

def _pick(candidates: list[Any], expected_artist: str, seen: set[int],
          expected_name: str = ""):
    """歌手对得上的优先，否则用第一首同名的；歌名对不上的一律不认（当作没找到）。"""
    artist_key, fallback = _norm(expected_artist), None
    for item in candidates or []:
        song_id = _song_id(item)
        if not song_id or song_id in seen:
            continue
        if not name_matches(_title(item), expected_name):
            continue
        if fallback is None:
            fallback = item
        if artist_key and any(_norm(name) == artist_key for name in _artists(item)):
            return item
    return fallback

async def force_refresh_qq() -> dict[str, Any]:
    credential = await qq_credential(force=True)
    return {"ok": True, "service": "qq_music", "musicid": str(getattr(credential, "musicid", "") or "")}


class QQMusicPlatform(MusicPlatform):
    """QQ 音乐平台。"""

    key = Platform.QQ_MUSIC
    label = "QQ音乐"
    default_method = "phone_code"

    # ---------------- 凭证 ----------------

    def status(self) -> dict[str, Any]:
        return vault.status()["qq_music"]

    def auth_hint(self) -> str:
        return QQ_AUTH_HINT

    def login_methods(self) -> list[dict[str, str]]:
        return [
            {"method": "phone_code", "label": "手机验证码（推荐）",
             "account": "手机号对应的 QQ 音乐账号", "needs": "phone + 短信验证码",
             "note": "无二维码，跨设备最稳；若撞上风控会让先开安全验证链接，验证完再发一次"},
            {"method": "mobile_qr", "label": "QQ 音乐 App 扫码",
             "account": "QQ 音乐 App 当前登录的账号", "needs": "QQ 音乐 App 扫一扫",
             "note": "要确认 App 里登的是目标账号；与 QQ / 微信账号可能不同"},
            {"method": "qq_qr", "label": "手机 QQ 扫码",
             "account": "该 QQ 号绑定的 QQ 音乐账号", "needs": "手机 QQ 扫一扫",
             "note": "扫码后手机会弹出「QQ音乐登录确认」，点确认才会成功"},
            {"method": "wx_qr", "label": "微信扫码",
             "account": "微信绑定的 QQ 音乐账号", "needs": "微信扫一扫",
             "note": "与 QQ 账号常是两个不同账号，容易登错，非必要不选"},
        ]

    def scan_hint(self, method: str) -> str:
        return QR_HINTS.get(method, "按提示在手机上确认，再调用 login_poll。")

    def set_cookie(self, value: str, uid: str = "") -> dict[str, Any]:
        """导入完整 Credential JSON（含 refresh_token 才能自动续期）。"""
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"不是合法 JSON: {exc}"}
        if isinstance(payload, dict) and isinstance(payload.get("credential_json"), str):
            payload = json.loads(payload["credential_json"])
        if not isinstance(payload, dict) or not vault.qq_is_complete(payload):
            return {"ok": False,
                    "error": "凭证不是完整 Credential（需 musicid/musickey/openid，"
                             "且 refresh_key / refresh_token / access_token 至少有一个）"}
        vault.set_qq(payload)
        return {"ok": True, "service": self.key.value, "source": "credential_import",
                "hint": "调用 credential_status 查看过期时间"}

    def logout(self) -> None:
        vault.write_secret(vault.QQ_FILE, {})

    async def refresh(self) -> dict[str, Any]:
        return await force_refresh_qq()

    # ---------------- 登录 ----------------

    async def login_start(self, method: str, phone: str = "", ctcode: int = 86) -> LoginStart:
        if method == "phone_code":
            if not phone.isdigit() or len(phone) < 6:
                return LoginStart(ok=False, status="failed", error="手机号格式不正确")
            async with Client() as client:
                flow = PhoneLoginSession(client.login, int(phone), ctcode)
                result = await flow.send_authcode()
            if result.event == PhoneLoginEvents.CAPTCHA:
                # 风控要求先过安全验证：库只把验证页链接给我们，没法代过。
                # 把链接原样带出去，让用户打开验证完再重发，这条路就通了。
                url = str(result.info or "")
                return LoginStart(
                    ok=False, status="failed",
                    error="发送前需完成安全验证（QQ 风控）",
                    message=(f"先在浏览器打开这个链接完成安全验证：{url}\n"
                             f"验证通过后重新调用 login_start(method='phone_code') 发验证码。"
                             if url else "QQ 要求先完成安全验证，但没有返回验证链接，稍后重试。"))
            if result.event == PhoneLoginEvents.FREQUENCY:
                return LoginStart(ok=False, status="failed", error="发送过于频繁，请稍后再试")
            return LoginStart(ok=True, status="awaiting_code",
                              ticket=LoginTicket({"phone": phone, "ctcode": ctcode}),
                              message=f"验证码已发送到 {phone[-4:].rjust(len(phone), '*')}")

        login_type = {"qq_qr": QRLoginType.QQ, "wx_qr": QRLoginType.WX,
                      "mobile_qr": QRLoginType.MOBILE}.get(method)
        if login_type is None:
            return LoginStart(ok=False, status="failed", error=f"不支持的登录方式: {method}")
        client = Client()
        await client.__aenter__()
        try:
            qr = await client.login.get_qrcode(login_type)
        except Exception:
            await _close_handle({"client": client})
            raise
        # 把 live Client 与 QR 对象放进票据：qq_qr / wx_qr 的 check_qrcode 是每次现查的，
        # 可预测；不用 flow.iter_events()（那个生成器自己 sleep + 去重，手动 __anext__ 会卡满 180s）。
        image = (f"data:{qr.mimetype or 'image/png'};base64,"
                 + base64.b64encode(qr.data or b"").decode())
        handle: dict[str, Any] = {"client": client, "qr": qr}
        if login_type == QRLoginType.MOBILE:
            # 手机客户端二维码只能用 MQTT 推送查状态（见 _mobile_qr_worker），
            # 起个后台任务把事件搬进队列，login_poll 每次取一条。
            queue: asyncio.Queue[Any] = asyncio.Queue()
            handle["queue"] = queue
            handle["task"] = asyncio.create_task(
                _mobile_qr_worker(client, qr, queue, anyio.current_time() + 300))
        return LoginStart(ok=True, status="waiting",
                          ticket=LoginTicket({"method": method}, handle),
                          qr_image=image, message="二维码已作为图片一并返回。扫码后调用 login_poll。")

    async def login_poll(self, ticket: LoginTicket) -> LoginState:
        handle = ticket.handle or {}
        if handle.get("queue") is not None:
            return await _poll_mobile_qr(ticket)
        client, qr = handle.get("client"), handle.get("qr")
        if client is None or qr is None:
            return LoginState(ok=False, status="expired", error="登录会话已失效，请重新 login_start")
        try:
            result = await client.login.check_qrcode(qr)
        except Exception as exc:
            # qqmusic-api-python 在解析不了 QQ 的 ptqrlogin 响应时会抛 ApiDataError。
            # 这是上游解析问题，不是用户拒登 —— 要如实说清楚。
            text = str(exc)
            if "缺少必要参数" in text or "无法解析" in text or "获取二维码状态失败" in text:
                await _close_handle(ticket.handle)
                ticket.handle = None
                return LoginState(ok=False, status="parse_error",
                                  error=f"QQ 授权响应解析失败（上游库兼容问题）：{text}",
                                  hint=("改用 method='phone_code'（短信验证码，最稳）或 "
                                        "method='wx_qr' / 'mobile_qr'；也可重新 login_start 再试一次。"))
            raise
        if result.event == QRCodeLoginEvents.DONE and result.credential is not None:
            vault.set_qq(_credential_payload(result.credential))
            await _close_handle(ticket.handle)
            ticket.handle = None
            return LoginState(ok=True, status="succeeded",
                              message="QQ 音乐登录成功，完整 Credential 已写入共享凭证库")
        status, message = QR_EVENT_STATUS.get(
            result.event, ("failed", f"二维码登录失败: {getattr(result.event, 'name', result.event)}"))
        if status in {"expired", "failed"}:
            await _close_handle(ticket.handle)
            ticket.handle = None
        return LoginState(ok=status not in {"expired", "failed"}, status=status, message=message)

    async def login_submit(self, ticket: LoginTicket, code: str) -> LoginState:
        async with Client() as client:
            flow = PhoneLoginSession(client.login, int(ticket.payload.get("phone") or 0),
                                     int(ticket.payload.get("ctcode") or 86))
            credential = await flow.authorize(code)
        vault.set_qq(_credential_payload(credential))
        return LoginState(ok=True, status="succeeded",
                          message="QQ 音乐验证码登录成功，凭证已写入共享凭证库")

    # ---------------- 基础能力 ----------------

    async def favorites(self) -> tuple[list[Song], list[str], dict[str, Any]]:
        songs, sources, diag = await qq_liked_songs()
        return [Song(name, artist) for name, artist in songs], sources, diag

    async def recommendations(self) -> tuple[list[Song], list[str], dict[str, Any]]:
        songs, sources, diag = await qq_recommendations()
        return [Song(name, artist) for name, artist in songs], sources, diag

    async def search(self, name: str, artist: str = "") -> Any:
        async def _find(credential):
            async with Client(credential=credential) as client:
                result = await client.search.search_by_type(
                    keyword=f"{name} {artist}".strip(), search_type=SearchType.SONG,
                    num=5, page=1)
            return _pick(list(getattr(result, "song", None) or []), artist, set(), name)

        return await _qq_run(_find)

    async def create_playlist(self, name: str, songs: list[Song], count: int) -> CreateResult:
        """建歌单。歌名和曲目都由上层（compose.py）定好，这里只负责落地。"""
        if not vault.qq_is_complete(vault.get_qq()):
            return CreateResult(platform=self.key.value, ok=False, playlist_name=name,
                                error=QQ_AUTH_HINT,
                                extra={"need_login": True, "service": self.key.value})
        selected = songs[:max(1, count)]

        async def _create(credential):
            matched: list[tuple[int, int]] = []
            matched_meta: list[dict[str, str]] = []
            failed: list[str] = []
            ids: set[int] = set()
            async with Client(credential=credential) as client:
                for song in selected:
                    query = f"{song.name} {song.artist}".strip()
                    try:
                        result = await client.search.search_by_type(
                            keyword=query, search_type=SearchType.SONG, num=5, page=1
                        )
                        chosen = _pick(list(getattr(result, "song", None) or []), song.artist,
                                       ids, song.name)
                    except Exception:
                        chosen = None
                    song_id = _song_id(chosen) if chosen is not None else 0
                    if not song_id:
                        failed.append(query)
                        continue
                    ids.add(song_id)
                    matched.append((song_id, 0))
                    matched_meta.append(song.as_dict())
                if not matched:
                    return CreateResult(platform=self.key.value, ok=False, playlist_name=name,
                                        failed=failed, error="没有匹配到任何歌曲")
                created = await client.songlist.create(dirname=name, credential=credential)
                dirid = int(getattr(created, "dirid", 0) or 0)
                if not dirid:
                    return CreateResult(platform=self.key.value, ok=False, playlist_name=name,
                                        failed=failed, error="QQ 音乐未返回 dirid")
                added = await client.songlist.add_songs(dirid=dirid, song_info=matched,
                                                        tid=0, credential=credential)
                if not added:
                    return CreateResult(platform=self.key.value, ok=False, playlist_name=name,
                                        playlist_id=str(dirid), failed=failed,
                                        error="歌单已创建但加歌被拒绝")
            return CreateResult(platform=self.key.value, ok=True, playlist_name=name,
                                playlist_id=str(dirid), added=len(matched),
                                failed=failed, matched=matched_meta)


        return await _qq_run(_create)

    async def delete_playlist(self, playlist_id: str) -> dict[str, Any]:
        """删除 QQ 音乐歌单（dirid）。"""
        if not vault.qq_is_complete(vault.get_qq()):
            return {"ok": False, "error": "QQ 音乐未登录或凭证不完整", "service": self.key.value}
        try:
            dirid = int(str(playlist_id).strip())
        except (TypeError, ValueError):
            return {"ok": False, "error": "dirid 必须是数字", "service": self.key.value}

        async def _delete(credential):
            async with Client(credential=credential) as client:
                result = await client.songlist.delete(dirid=dirid, credential=credential)
            return {"ok": bool(getattr(result, "ok", False)), "service": self.key.value,
                    "deleted_dirid": dirid}

        try:
            return await _qq_run(_delete)
        except Exception as exc:
            return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}",
                    "service": self.key.value}

    async def my_playlists(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """列出 QQ 音乐创建的歌单。"""
        diag: dict[str, Any] = {"errors": []}
        if not vault.qq_is_complete(vault.get_qq()):
            diag["need_login"] = True
            return [], diag
        async def _fetch(credential):
            musicid = int(getattr(credential, "musicid", 0) or 0)
            async with Client(credential=credential) as client:
                result = await client.user.get_created_songlist(uin=musicid, credential=credential)
            playlists = list(getattr(result, "playlists", []) or [])
            return [{"dirid": str(getattr(pl, "id", "") or ""),
                     "title": str(getattr(pl, "title", "") or ""),
                     "songnum": int(getattr(pl, "songnum", 0) or 0)}
                    for pl in playlists]
        try:
            items = await _qq_run(_fetch)
            diag["count"] = len(items)
            return items, diag
        except Exception as exc:
            diag["errors"].append(f"qq_credential: {exc.__class__.__name__}: {exc}")
            return [], diag