"""MCP 工具定义。一个 FastMCP server（``all``）或三个，共享同一套凭证 vault。

每个工具都很薄：校验参数 -> 转给 platforms/* 或 compose.py -> 把结果 JSON 化。
平台差异不允许出现在这里 —— 这里只认 :class:`Platform` 枚举。

  MCP_SERVER=all       -> 每个工具都在一个进程里（推荐）
  MCP_SERVER=auth      -> 登录流程（二维码 / 验证码）+ 凭证查看/续期/登出
  MCP_SERVER=profile   -> 汇总听歌数据、生成/保存偏好画像
  MCP_SERVER=playlist  -> 搜索、去重、建歌单、今日推荐

MCP 里「一个 server 带多个 tool」才是常规形态，所以 ``all`` 是默认。分体保留给
只想暴露登录工具的客户端，或按区域部署的 systemd unit；两者共享同一份凭证文件。
"""
from __future__ import annotations

import base64
import io
import json
import re
import os
import time
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from . import compose, login, paths, qr, vault
from .login import LOGIN_USAGE, login_sessions
from .platforms import DEFAULT_ORDER, all_platforms, get_platform, resolve_keys
from .platforms.core.base import Platform, Song, platform_of


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _qr_image(data_url: str) -> Image:
    """把 data: URL 变成 MCP ImageContent（放大到 444px，扫码更稳）。"""
    raw = data_url.split(",", 1)[-1] if "," in data_url else data_url
    data = base64.b64decode(raw)
    try:
        from PIL import Image as PILImage

        source = PILImage.open(io.BytesIO(data)).convert("RGB")
        source = source.resize((444, 444), PILImage.NEAREST)  # 111 * 4：整数倍缩放，模块不糊
        buffer = io.BytesIO()
        source.save(buffer, format="PNG")
        data = buffer.getvalue()
    except Exception:
        pass
    return Image(data=data, format="png")


def _qr_ascii(data_url: str) -> str:
    """半块字符渲染的二维码；客户端会把图片折叠时用。

    纯文本在任何终端都可见、可扫（用 zxing 验过），但依赖等宽字体/深浅主题，只作兜底。
    """
    raw = data_url.split(",", 1)[-1] if "," in data_url else data_url
    try:
        from PIL import Image as PILImage

        image = PILImage.open(io.BytesIO(base64.b64decode(raw))).convert("L")
        image = image.resize((37, 37), PILImage.LANCZOS)
        px = image.load()
        rows = []
        for y in range(0, 37, 2):
            line = []
            for x in range(37):
                top = px[x, y] < 128
                bottom = px[x, y + 1] < 128 if y + 1 < 37 else False
                line.append("█" if top and bottom else "▀" if top else "▄" if bottom else " ")
            rows.append("".join(line))
        return "\n".join(rows)
    except Exception:
        return ""


# 二维码的四种投递通道（客户端差异很大），按推荐顺序：
#   1. qr_markdown  - 普通图片 URL，绝大多数客户端内联显示
#   2. ImageContent - 原生 MCP 图片（常被折叠进工具结果详情）
#   3. qr_data_url  - data: URL，给允许内联的客户端
#   4. qr_ascii     - 最后兜底，字体/主题/宽度敏感，默认关
QR_ASCII = os.environ.get("QR_ASCII", "0") == "1"


def _with_login_options(payload: dict[str, Any]) -> dict[str, Any]:
    """未登录时把「支持哪些平台/哪些方式/只登哪一个」补齐，供 agent 直接转述给用户。"""
    if not payload.get("need_login"):
        return payload
    platform = get_platform(payload.get("service") or "")
    if platform is None:
        return payload
    payload["methods"] = platform.login_methods()
    payload["default_method"] = platform.default_method
    payload["usage"] = LOGIN_USAGE
    payload["hint"] = ("只登录你要用的那个平台即可，不必全登：把 methods 列给用户选一个，"
                       "手机验证码走 login_start -> login_submit，二维码走 login_start -> login_poll，"
                       "网易云/酷狗也可以直接 credential_set_cookie 导入。登录完成后重新调用本工具。")
    return payload


def _resolve_method(service: Platform, method: str) -> str:
    """method 留空就用该平台推荐的方式。"""
    platform = get_platform(service)
    return method or (platform.default_method if platform else "phone_code")


def _quiet_http_logs() -> None:
    """把 HTTP 客户端的 INFO 日志压掉。

    httpx 在 INFO 级别会把**完整 URL** 打进日志，而酷狗的 token、网易云的 Cookie 都是
    请求参数/头的一部分 —— 默认配置下这些会原样落到 MCP 客户端的 stderr 里。
    这里只压这几个库，MCP 自己的日志（工具调用）保持原样。
    """
    import logging

    for name in ("httpx", "httpcore", "urllib3", "niquests", "anyio", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)


def build_server(name: str) -> FastMCP:
    _quiet_http_logs()
    # "all" = 一个 server 带全部工具（MCP 的常规形态）；其余三个是分体模式。
    parts = {"all": ("auth", "profile", "playlist"),
             "auth": ("auth",),
             "profile": ("profile",),
             "playlist": ("playlist",)}.get(name)
    if parts is None:
        raise ValueError(f"unknown MCP server: {name}")
    mcp = FastMCP(f"music-{name}", host="0.0.0.0", port=int(os.environ.get("PORT", "8765")),
                  stateless_http=True)

    if "auth" in parts:
        @mcp.tool()
        async def login_methods(service: Platform = Platform.QQ_MUSIC) -> str:
            """List every supported login method for a platform.

            平台：qq_music | netease | kugou。Call this FIRST and let the user
            choose. QQ Music / WeChat / mobile QQ are often DIFFERENT accounts —
            picking the wrong one logs into an empty account. Only log into the
            platforms the user actually needs.
            """
            platform = get_platform(service)
            if platform is None:
                return _dump({"ok": False, "error": f"unknown service: {service}"})
            return _dump({"ok": True, "service": platform.key.value,
                          "methods": platform.login_methods(),
                          "default": platform.default_method,
                          "configured": platform.status().get("configured"),
                          "usage": LOGIN_USAGE})

        @mcp.tool(structured_output=False)
        async def login_start(service: Platform = Platform.QQ_MUSIC, method: str = "",
                              phone: str = "", ctcode: int = 86) -> list:
            """Start a login session. Call login_methods FIRST so the user can pick.

            service: qq_music | netease | kugou
            method:  qq_music -> phone_code（默认）| qq_qr | wx_qr | mobile_qr
                     netease  -> cookie（不用 login_start）| phone_code | qr
                     kugou    -> qr（默认）| phone_code
                     留空 = 用该平台推荐的方式。
            phone:   phone_code 必填；手机号由用户在对话里给出。

            phone_code 不发二维码：login_start 发短信 -> 用户把验证码贴进对话 ->
            login_submit(session_id, code)。扫码方式返回 qr_markdown / ImageContent
            / qr_data_url，用户扫码并在手机上确认后调用 login_poll。
            """
            service_key = platform_of(service)
            resolved = _resolve_method(service_key or service, method)
            if resolved == "cookie":
                return [_dump({"ok": False,
                               "error": "Cookie 不需要 login_start，直接调用 credential_set_cookie(service='netease', value='MUSIC_U=...; __csrf=...')"})]
            result = await login_sessions.start(service_key or service, resolved, phone, ctcode)
            result.pop("qr_path", None)  # 绝不外发本地路径：客户端会当成坏图
            data_url = result.pop("qr_data_url", "") or ""
            if not result.get("ok") or not data_url:
                result["message"] = (result.get("message")
                                     or "把 scan_hint 转述给用户；用户把收到的验证码输入对话框后，"
                                        "调用 login_submit(session_id, code)。")
                return [_dump(result)]

            sid = result.get("session_id", "")
            url = qr.url(sid) if sid else ""
            if url:
                result["qr_markdown"] = f"![登录二维码]({url})"
                result["message"] = ("把 qr_markdown 原样放进回复即可显示二维码（普通图片 URL，跨设备稳定）。"
                                     "工具结果里另附原生 ImageContent。用户手机扫码并确认后调用 login_poll；"
                                     "不要贴本地文件路径。")
            else:
                result["qr_markdown"] = f"![登录二维码]({data_url})"
                result["message"] = ("回环 HTTP 服务未启动，qr_markdown 改用 data: URL；若客户端不显示，"
                                     "让用户展开本工具调用详情查看原生图片。扫码确认后调用 login_poll。")
            result["qr_data_url"] = data_url
            blocks: list = [_dump(result), _qr_image(data_url)]
            if QR_ASCII:
                ascii_qr = _qr_ascii(data_url)
                if ascii_qr:
                    blocks.append("二维码（纯文本兜底，字体/主题/宽度敏感，建议优先用 qr_markdown）：\n" + ascii_qr)
            return blocks

        @mcp.tool()
        async def login_poll(session_id: str) -> str:
            """Poll a login session started by login_start."""
            return _dump(await login_sessions.poll(session_id))

        @mcp.tool()
        async def login_submit(session_id: str, code: str) -> str:
            """Submit the SMS / captcha code for a phone_code login session."""
            return _dump(await login_sessions.submit(session_id, code))

        @mcp.tool()
        async def credential_status() -> str:
            """Check every platform's credential status (qq_music / netease / kugou).

            除 vault 里的存档信息外，还会让平台自己补全「要问一次上游才知道」的字段
            （例如网易云的 uid —— Cookie 导入时没有，不能一直显示成空）。
            """
            payload = vault.status()
            for key, platform in all_platforms().items():
                entry = payload.get(key.value)
                if not isinstance(entry, dict):
                    continue
                try:
                    payload[key.value] = await platform.enrich_status(entry)
                except Exception as exc:  # noqa: BLE001 - 补信息失败不影响主状态
                    entry["enrich_error"] = f"{exc.__class__.__name__}: {exc}"
            return _dump(payload)

        @mcp.tool()
        async def credential_refresh(service: Platform = Platform.QQ_MUSIC) -> str:
            """Force-refresh a shared credential (qq_music | netease).

            酷狗没有续期接口，会返回 supported=false，不算失败。
            """
            platform = get_platform(service)
            if platform is None:
                return _dump({"ok": False, "error": f"unknown service: {service}"})
            return _dump(await platform.refresh())

        @mcp.tool()
        async def credential_set_cookie(service: Platform, value: str, uid: str = "") -> str:
            """Import a credential blob directly, no login flow needed.

            netease: value 是 Cookie 串（MUSIC_U=...; __csrf=...），
                     从浏览器已登录网易云的请求头里整段复制即可。
            kugou:   value 是 Cookie 串，必须含 token=。
            qq_music: value 是完整 Credential JSON（含 musicid / musickey /
                     refresh_key / access_token 等；带 refresh_token 时才能自动续期）。
            """
            platform = get_platform(service)
            if platform is None:
                return _dump({"ok": False, "error": f"unknown service: {service}"})
            value = (value or "").strip()
            if not value:
                return _dump({"ok": False, "error": "value 为空"})
            return _dump(platform.set_cookie(value, uid))

        @mcp.tool()
        async def credential_logout(service: Platform) -> str:
            """Delete a shared credential file (requires re-login afterwards)."""
            platform = get_platform(service)
            if platform is None:
                return _dump({"ok": False, "error": f"unknown service: {service}"})
            platform.logout()
            return _dump({"ok": True, "service": platform.key.value})

    if "profile" in parts:
        @mcp.tool()
        async def analyze_music_profile(services: list[Platform] | None = None) -> str:
            """Pull every platform's liked songs and return a compact statistical
            summary, which the model turns into a taste profile and stores with
            save_profile.

            services: 平台列表，如 ["qq_music","netease","kugou"]；留空 = 全部平台。
            """
            return _dump(await compose.analyze(services))

        @mcp.tool()
        async def get_profile() -> str:
            """Return the stored taste profile JSON (empty if never saved)."""
            return _dump({"ok": True, "profile": compose.get_profile()})

        @mcp.tool()
        async def save_profile(profile_json: str) -> str:
            """Store the LLM-authored taste profile JSON (favorite_genres,
            top_artists, mood_tendencies, language_ratio, energy_level,
            recent_trend, summary)."""
            try:
                return _dump(compose.save_profile(json.loads(profile_json)))
            except json.JSONDecodeError as exc:
                return _dump({"ok": False, "error": f"invalid JSON: {exc}"})

        @mcp.tool()
        async def recent_tracks() -> str:
            """**我们最近 14 天推荐过的歌**（建单去重用），不是用户的听歌历史。

            ⚠️ 名字容易误会：想看"用户最近在听什么"要用 recent_played(service=...)；
            这个工具只回答"我们推过哪些歌"，换了名字叫 recent_recommended_tracks。
            """
            return _dump(compose.recent_tracks())

    if "playlist" in parts:
        async def _create_from_list(service: Platform, songs_json: str, count: int,
                                    playlist_name: str, date: str, prompt: str,
                                    avoid_days: int = 7) -> str:
            """显式给一份歌单时的公共流程：去重选歌 -> 定名 -> 交给平台建单。

            普通建单的歌单名**不带日期前缀**（只有"今日歌单"才带），名字上限 20 字。
            这条路径**不写歌单台账**：用户明确要的就是这份清单，不该污染日常去重池。
            """
            platform = get_platform(service)
            if platform is None:
                return _dump({"ok": False, "error": f"unknown service: {service}"})
            try:
                payload = json.loads(songs_json)
            except json.JSONDecodeError as exc:
                return _dump({"ok": False, "error": f"invalid songs_json: {exc}"})
            # 裸列表也认：{"songs":[...]} 和 [{...}] 两种写法都收，
            # 以前只认前者，传成列表会直接抛 AttributeError 给模型看。
            if isinstance(payload, list):
                payload = {"songs": payload}
            if not isinstance(payload, dict):
                return _dump({"ok": False,
                              "error": 'songs_json 要 JSON 对象或数组，例如 '
                                       '{"songs":[{"name":"天后","artist":"陈势安"}]}'})
            songs = [Song(str(item.get("name") or ""), str(item.get("artist") or ""))
                     for item in (payload.get("songs") or []) if isinstance(item, dict)]
            if isinstance(payload.get("playlist_name"), str) and not playlist_name:
                playlist_name = payload["playlist_name"]
            day = date or time.strftime("%Y%m%d")
            # 只调一次 resolve_theme：名字里的随机素材就在这一次里定下来，
            # 调两次（再来一次 resolve_name）会拿到两组不同的随机素材。
            theme, name_source = compose.resolve_theme(prompt, playlist_name, day, None, songs)
            name = compose.limit_name(theme, compose.NAME_LIMIT)      # 普通建单不带日期
            selected, reused = compose.select_songs(songs, count, None, avoid_days)
            if not selected:
                return _dump({"ok": False, "error": "没有有效歌曲"})
            created = await platform.create_playlist(name, selected, len(selected))
            result = created.as_dict()
            result["reused_recent"] = reused
            result["name_source"] = name_source
            result["avoid_days"] = avoid_days
            return _dump(_with_login_options(result))

        @mcp.tool()
        async def create_qq_playlist(songs_json: str, count: int = 20,
                                     playlist_name: str = "", date: str = "",
                                     prompt: str = "", avoid_days: int = 7) -> str:
            """Create a real QQ Music playlist.

            songs_json: {"playlist_name":"清晨慢摇","songs":[{"name":"歌名","artist":"歌手"}]}
            count:        how many songs to add (default 20)
            playlist_name: explicit name from the user; leave empty to auto-name
            date:        YYYYMMDD (default today)
            prompt:       original user request, used to decide the name
            avoid_days:  skip songs recommended in the last N days (default 7; 0 = no dedup)
            Returns {"ok","playlist_name","playlist_id","added","failed","reused_recent",
                     "name_source"}.
            The name has NO date prefix here (only create_daily_playlist adds one),
            and it is capped at 20 characters.

            If the result has need_login=true, this platform is not logged in yet:
            show the methods list to the user, let them pick ONE, then retry.
            """
            return await _create_from_list(Platform.QQ_MUSIC, songs_json, count,
                                           playlist_name, date, prompt, avoid_days)

        @mcp.tool()
        async def create_ncm_playlist(songs_json: str, count: int = 20,
                                      playlist_name: str = "", date: str = "",
                                      prompt: str = "", avoid_days: int = 7) -> str:
            """Create a real Netease Cloud Music playlist.

            Same arguments and same contract as create_qq_playlist; songs are
            resolved through Netease cloud search, then added to a new playlist.
            """
            return await _create_from_list(Platform.NETEASE, songs_json, count,
                                           playlist_name, date, prompt, avoid_days)

        @mcp.tool()
        async def create_kugou_playlist(songs_json: str, count: int = 20,
                                        playlist_name: str = "", date: str = "",
                                        prompt: str = "", avoid_days: int = 7) -> str:
            """Create a real Kugou Music playlist.

            Same arguments and same contract as create_qq_playlist; songs are
            resolved through Kugou search, then added to a new playlist.
            """
            return await _create_from_list(Platform.KUGOU, songs_json, count,
                                           playlist_name, date, prompt, avoid_days)

        @mcp.tool()
        async def qq_recent() -> str:
            """⚠️ 名字误导：与 QQ 平台无关，返回的也不是听歌历史。

            它是 recent_recommended_tracks 的旧名字（我们最近 14 天推荐过的歌，用于去重），
            保留只为兼容老提示词。想要"用户最近听过的歌"用 recent_played(service=...)。
            """
            return _dump(compose.recent_tracks())

        @mcp.tool()
        async def daily_recommendations(service: Platform = Platform.QQ_MUSIC) -> str:
            """Today's platform recommendations (the raw material for a daily playlist).

            service: qq_music | netease | kugou
            QQ: 雷达推荐 + 猜你喜欢 + 推荐歌单；Netease: 每日推荐 + 推荐歌单；
            酷狗: 每日推荐（未登录也能拉）。
            Returns {"ok","recommended","sources","songs":[{"name","artist"}],"diagnostics"}.
            """
            platform = get_platform(service)
            if platform is None:
                return _dump({"ok": False, "error": f"unknown service: {service}"})
            songs, sources, diag = await platform.recommendations()
            return _dump(_with_login_options({
                "ok": bool(songs), "service": platform.key.value, "recommended": len(songs),
                "sources": sources, "songs": [song.as_dict() for song in songs],
                "diagnostics": diag, "need_login": bool(diag.get("need_login"))}))

        @mcp.tool()
        async def create_daily_playlist(services: list[Platform] | None = None,
                                        service: Platform | None = None,
                                        count: int = 40, playlist_name: str = "",
                                        date: str = "", prompt: str = "",
                                        include: str = "",
                                        with_weather: bool = True,
                                        avoid_days: int = 7) -> str:
            """生成「今日歌单」：全平台候选池 -> 同一份歌单，多平台各建一份。

            候选池 = 所有平台的「今日推荐」+「全部收藏」去重（推荐优先），
            自动避开最近 avoid_days 天推过的歌；然后**在每一个已登录的平台各建一份同名歌单**，
            每首歌在目标平台现场搜索，找不到就跳过（记在该平台的 failed 里）。

            services: 平台列表，如 ["qq_music","netease","kugou"]；留空 = 全部平台。
            service:  单平台写法（等价于 services=[xxx]）。
            count:   要建多少首，随便填（默认 40，想要 100 首就传 100；池子不够时有多少给多少）。
            include: 点名想听的歌，逗号/分号/换行分隔，写成「歌名 - 歌手」，例如
                     "晴天 - 周杰伦, 海阔天空 - Beyond"。点名的歌一定排在最前面。
            avoid_days: 避开最近几天推过的歌（默认 7；给 0 = 完全不看去重池）。
            playlist_name: 指定歌单名（**推荐**：先调 suggest_playlist_names 挑一个，
                     或你自己起 —— 名字是给用户看的，你起得比内置规则好）。
                     留空则自动 "<YYYYMMDD>-<主题>"，总长 ≤20 字（主题 ≤11 字）。
            prompt:  用户的原始请求。话里带心情/场景（「今天心情不好」）、歌手名或年代
                     （「邓紫棋2020年以前的歌」）就贴着它起名；什么都没说就随机抽素材
                     （天气/时段/主题池），**不做**任何"雨天一定叫雨天窗边"的硬绑定。
            with_weather: 天气是否参与命名（默认 true；可选项，取不到不影响生成）。
                     天气默认按出口 IP 定位，在服务器上会是机房所在地；想让天气贴合本城，
                     让用户设环境变量 MUSIC_WEATHER_CITY=城市名。
            返回 {"ok","playlist_name","theme","name_source","name_budget","recent_names",
                  "avoid_days","count","weather","weather_hint","pool_size","pool_sources",
                  "results":[{platform,...}]}。
            name_source 说明名字怎么来的（explicit/mood/artist/year/artist+year/random）。
            某个平台没登录会在 results 里标 skipped=not_logged_in，不影响其他平台。
            若返回里 weather_hint 非空，把它转述给用户（提醒设置 MUSIC_WEATHER_CITY）。
            """
            target = services if services else ([service] if service else None)
            result = await compose.create_daily_playlist(target, count, playlist_name,
                                                         date, prompt, include, with_weather,
                                                         avoid_days)
            return _dump(_with_login_options(result))

        @mcp.tool()
        async def suggest_playlist_names(prompt: str = "", date: str = "",
                                         with_weather: bool = True, count: int = 3,
                                         songs_json: str = "", artists: str = "") -> str:
            """起名素材 + 候选名字：**给歌单起名优先用这个工具，而不是靠内置规则。**

            你是 AI，起名这件事你比内置规则强，也更懂用户想要的那种"共鸣"
            （今天天气怎样、他说了心情不好、他点了邓紫棋）。内置规则只负责"没人管时给个
            不重复的名字"。

            用法：调这个工具 → 从 candidates 里挑一个，或自己起一个 → 传给
            create_daily_playlist / create_qq_playlist 等的 playlist_name。
            prompt: 用户原话（用来提取心情 / 歌手 / 年代等线索），可以留空。
            songs_json: 你手上那份曲目（和 create_*_playlist 同一个格式），**建议传**——
                     名字要带上歌手（"邓紫棋2019以前"）就得知道有哪些歌手。
                     不传就只能用日期/天气/心情/时段这些素材。
            artists: 只要歌手名时可以用这个，逗号分隔，例如 "邓紫棋,周杰伦"。
            count:  要几个候选（默认 3）。
            with_weather: 是否把当天天气作为素材（默认 true，取不到不影响）。
            Returns {"ok","materials","candidates","name_budget","recent_names","note"}。
            name_budget: {"daily": 11, "manual": 20} —— 今日歌单会加 8 位日期前缀，
            所以主题只有 11 个字可写；普通建单不带日期，最多 20 个字。
            recent_names 是最近生成过的名字，用来避免"连着两天叫一样"。
            """
            day = date or time.strftime("%Y%m%d")
            weather = await compose.current() if with_weather else {"ok": False}
            # 曲目/歌手名单：没有它，"邓紫棋2020年以前的歌"里的歌手根本认不出来
            songs: list[Song] = []
            if songs_json.strip():
                try:
                    payload = json.loads(songs_json)
                except json.JSONDecodeError as exc:
                    return _dump({"ok": False, "error": f"invalid songs_json: {exc}"})
                if isinstance(payload, list):
                    payload = {"songs": payload}
                if isinstance(payload, dict):
                    songs = [Song(str(item.get("name") or ""), str(item.get("artist") or ""))
                             for item in (payload.get("songs") or []) if isinstance(item, dict)]
            if artists.strip():
                songs += [Song("", part.strip()) for part in re.split(r"[,，;；\s]+", artists)
                          if part.strip()]
            material = compose.naming_material(prompt, day, weather, songs)
            candidates = compose.naming_candidates(prompt, day, count, weather, songs)
            return _dump(_with_login_options({
                "ok": True, "date": day, "materials": material,
                "candidates": candidates,
                "name_budget": material["name_budget"],
                "recent_names": material["recent_names"],
                "note": "名字很随意，能让人有共鸣就行（今天的天气 / 他说的心情 / 时段都可以）。"
                        "挑一个传 playlist_name 即可；留空则用内置规则自动起名。",
            }))

        async def _liked_songs(service: Platform, limit: int = 0) -> dict[str, Any]:
            """收藏工具的公共实现：拉某平台的全部收藏（不是摘要）。"""
            platform = get_platform(service)
            if platform is None:
                return {"ok": False, "error": f"unknown service: {service}"}
            songs, sources, diag = await platform.favorites()
            snapshot_at = ""
            if songs:
                snapshot = compose.save_liked(platform.key.value, songs, sources)
                snapshot_at = str(snapshot.get("updated_at") or "")
            returned = songs[:limit] if limit and limit > 0 else songs
            return _with_login_options({
                "ok": bool(songs), "service": platform.key.value,
                "count": len(songs), "returned": len(returned), "sources": sources,
                "songs": [song.as_dict() for song in returned],
                "snapshot": str(paths.liked_file(platform.key.value)) if songs else "",
                "snapshot_updated_at": snapshot_at,
                "diagnostics": diag, "need_login": bool(diag.get("need_login"))})

        @mcp.tool()
        async def liked_songs(service: Platform = Platform.KUGOU, limit: int = 0) -> str:
            """某个平台**全部收藏**（「我喜欢」/收藏的歌单，全量，不是统计摘要）。

            service: qq_music | netease | kugou
            limit:   0 = 全部；>0 只返回前 N 首（大账号如 QQ 700+ 首时可截断）
            Returns {"ok","service","count","returned","sources",
                     "songs":[{"name","artist"}],"snapshot","diagnostics","need_login"}.
            同时把快照写进 state/<platform>_liked.json（与建歌单流程同一口径）。
            """
            return _dump(await _liked_songs(service, limit))

        @mcp.tool()
        async def ncm_liked_songs(limit: int = 0) -> str:
            """网易云收藏（等价于 liked_songs(service='netease')，保留是为了兼容老提示词）。"""
            return _dump(await _liked_songs(Platform.NETEASE, limit))

        @mcp.tool()
        async def search_songs(service: Platform, keyword: str, limit: int = 10) -> str:
            """在某个平台搜歌，返回可直接喂给建单/加歌的候选。

            service: qq_music | netease | kugou
            keyword: 歌名、或「歌名 歌手」
            limit:   返回条数（默认 10，最多 50）
            Returns {"ok","service","count","songs":[{"name","artist","id","album"}]}。
            id 是该平台的歌曲标识（QQ songid / 网易云 song id / 酷狗 hash）。
            想确认"某首歌在平台上到底有没有、是哪一首"时用它。
            """
            platform = get_platform(service)
            if platform is None:
                return _dump({"ok": False, "error": f"unknown service: {service}"})
            if not str(keyword or "").strip():
                return _dump({"ok": False, "error": "keyword 为空"})
            songs, diag = await platform.search_items(keyword, limit)
            return _dump(_with_login_options({
                "ok": bool(songs), "service": platform.key.value, "keyword": keyword,
                "count": len(songs), "songs": songs, "diagnostics": diag,
                "need_login": bool(diag.get("need_login"))}))

        @mcp.tool()
        async def playlist_songs(service: Platform, playlist_id: str,
                                 limit: int = 0) -> str:
            """读某个歌单的曲目（playlist_id 用 my_playlists 返回的 id）。

            service: qq_music | netease | kugou
            limit:   0 = 全部；>0 只返回前 N 首
            Returns {"ok","service","playlist_id","count","songs":[{"name","artist"}]}。
            用途：把某个歌单当"种子"做推荐（先读出来，再挑歌建新单），或核对歌单内容。
            """
            platform = get_platform(service)
            if platform is None:
                return _dump({"ok": False, "error": f"unknown service: {service}"})
            pid = str(playlist_id or "").strip()
            if not pid:
                return _dump({"ok": False, "error": "playlist_id 为空"})
            songs, diag = await platform.playlist_tracks(pid, limit)
            return _dump(_with_login_options({
                "ok": bool(songs), "service": platform.key.value, "playlist_id": pid,
                "count": len(songs), "songs": [song.as_dict() for song in songs],
                "diagnostics": diag, "need_login": bool(diag.get("need_login"))}))

        @mcp.tool()
        async def add_songs_to_playlist(service: Platform, playlist_id: str,
                                        songs: str = "", count: int = 0) -> str:
            """给**已有**歌单加歌（不会新建歌单）。

            service:     qq_music | netease | kugou
            playlist_id: my_playlists 返回的 id
            songs:       「歌名 - 歌手」，逗号/分号/换行分隔；也接受列表 JSON。
                         例如 "晴天 - 周杰伦, 海阔天空 - Beyond"
            count:       最多加几首（0 = 全部）
            Returns {"ok","service","playlist_id","added","matched","failed"}。
            每首歌在目标平台现场搜索，找不到就跳过（记进 failed），不会塞错歌。
            """
            platform = get_platform(service)
            if platform is None:
                return _dump({"ok": False, "error": f"unknown service: {service}"})
            pid = str(playlist_id or "").strip()
            if not pid:
                return _dump({"ok": False, "error": "playlist_id 为空"})
            wanted = compose.parse_include(songs)
            if not wanted:
                return _dump({"ok": False, "error": "songs 为空（写成「歌名 - 歌手」，逗号分隔）"})
            if count and count > 0:
                wanted = wanted[:count]
            return _dump(_with_login_options(await platform.add_tracks(pid, wanted)))

        @mcp.tool()
        async def recent_played(service: Platform, limit: int = 50) -> str:
            """某个平台**你自己最近听过的歌**（听歌排行），不是我们推荐过的歌。

            service: qq_music | netease | kugou（QQ 暂不支持，会返回 supported=false）
            limit:   取多少首（默认 50）
            Returns {"ok","service","count","songs":[{"name","artist"}],"details",
                     "diagnostics"}；details 里带 play_count（网易云）/ played_at（酷狗）。
            用途：拿它当"种子"——先看用户最近在听什么，再据此建一张新歌单。
            """
            platform = get_platform(service)
            if platform is None:
                return _dump({"ok": False, "error": f"unknown service: {service}"})
            songs, diag = await platform.recently_played(limit)
            return _dump(_with_login_options({
                "ok": bool(songs), "service": platform.key.value, "count": len(songs),
                "songs": [song.as_dict() for song in songs],
                "details": diag.get("details", []), "diagnostics": diag,
                "need_login": bool(diag.get("need_login"))}))

        @mcp.tool()
        async def recent_recommended_tracks() -> str:
            """**我们最近 14 天推荐过的歌**（去重用），不是用户的听歌历史。

            想看"用户最近在听什么"用 recent_played(service=...)。
            """
            return _dump(compose.recent_tracks())

        @mcp.tool()
        async def delete_playlist(service: Platform, playlist_id: str,
                                  confirm: bool = False) -> str:
            """删除一个自建歌单（不可逆）。

            playlist_id 用 my_playlists 返回的 ``id``（三平台已统一：QQ 的 dirid、
            网易云的 id、酷狗的 listid 都放在这个字段里）。
            必须传 confirm=True 才会真的执行；先不带 confirm 调用可查看歌单名做二次确认。
            """
            platform = get_platform(service)
            if platform is None:
                return _dump({"ok": False, "error": f"unknown service: {service}"})
            pid = str(playlist_id or "").strip()
            if not pid:
                return _dump({"ok": False, "error": "playlist_id 为空"})
            if not confirm:
                playlists, diag = await platform.my_playlists()
                target = next((p for p in playlists
                               if str(p.get("id") or p.get("dirid") or p.get("listid") or "") == pid),
                              None)
                name = str((target or {}).get("name") or (target or {}).get("title") or "(未在列表中找到)")
                return _dump({"ok": False, "needs_confirmation": True,
                              "service": platform.key.value, "playlist_id": pid,
                              "name": name,
                              "hint": "确认无误后再次调用并传 confirm=True"})
            return _dump(await platform.delete_playlist(pid))

        @mcp.tool()
        async def my_playlists(service: Platform = Platform.QQ_MUSIC) -> str:
            """List my created playlists on a platform.

            service: qq_music | netease | kugou
            Returns {"ok","service","count","playlists":[{id,name,track_count}],"diagnostics"}.
            """
            platform = get_platform(service)
            if platform is None:
                return _dump({"ok": False, "error": f"unknown service: {service}"})
            playlists, diag = await platform.my_playlists()
            return _dump(_with_login_options({
                "ok": bool(playlists), "service": platform.key.value,
                "count": len(playlists), "playlists": playlists,
                "diagnostics": diag, "need_login": bool(diag.get("need_login"))}))

    return mcp
