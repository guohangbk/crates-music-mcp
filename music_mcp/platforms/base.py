"""平台抽象：枚举、数据契约、以及每个平台都要实现的那套接口。

分两层，别混：
  * ``platforms/*.py``（这一层）= **平台能力**：登录、凭证、收藏、今日推荐、搜索、建歌单。
    平台之间的差异（Cookie 还是 token、字段叫 ar 还是 singer、路由各不相同、引擎会不会崩）
    全部关在各个平台文件里，外面只认 Song 和 Platform。
  * ``compose.py`` = **组合能力**：把全平台的收藏和今日推荐合成一份歌单，再在每个已登录
    平台各建一份，外加命名/天气/历史。它不认任何平台细节。

平台标识用枚举，顶层往下传的就是它：login_start(service=Platform.QQ_MUSIC, ...)。
枚举是 str 子类，线上值仍是 qq_music，所以老配置和文档里的写法照样能用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

class Platform(str, Enum):
    """受支持的平台。成员名给代码看，值给 JSON / 工具参数看。"""

    QQ_MUSIC = "qq_music"
    NETEASE = "netease"
    KUGOU = "kugou"

    @property
    def value_str(self) -> str:
        return str(self.value)

    def __str__(self) -> str:  # 方便日志 / f-string
        return str(self.value)

def platform_of(value: Any) -> Platform | None:
    """把 Platform / "qq_music" 这类写法收敛成枚举（认不出返回 None）。"""
    if isinstance(value, Platform):
        return value
    text = str(value or "").strip()
    for member in Platform:
        if member.value == text:
            return member
    return None

def key_of(value: Any) -> str:
    """统一成线上字符串 key。"""
    member = platform_of(value)
    return member.value if member else str(value or "")

@dataclass(frozen=True)
class Song:
    """跨平台通用的一首歌。

    只认「歌名 + 歌手」：各平台曲库 id 互不相通，能跨平台对齐的只有这两个字段，
    真正落到某个平台时再用 search() 现查它自己的 id。

    rank 是画像匹配权重（默认 0 = 普通）：
      2 = 歌手命中画像 top_artists（最高优先级）
      1 = 歌名/风格命中 mood_tendencies
      0 = 普通歌曲
    """

    name: str
    artist: str = ""
    rank: int = 0

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "artist": self.artist}

@dataclass
class CreateResult:
    """建歌单的结果。同一份歌单会在多个平台各建一份，所以必须带 platform。"""

    platform: str
    ok: bool = False
    playlist_name: str = ""
    added: int = 0
    failed: list[str] = field(default_factory=list)
    playlist_id: str = ""
    error: str = ""
    matched: list[dict[str, str]] = field(default_factory=list)   # 真正加进去的歌（供历史去重）
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"platform": self.platform, "ok": self.ok,
                                "playlist_name": self.playlist_name,
                                "added": self.added, "failed": self.failed}
        if self.playlist_id:
            data["playlist_id"] = self.playlist_id
        if self.error:
            data["error"] = self.error
        if self.matched:
            data["matched"] = self.matched
        data.update(self.extra)
        return data

@dataclass
class LoginTicket:
    """一次待完成登录的全部中间状态。

    只有对应平台看得懂，外层只负责按 session_id 存着再交回去。QQ 的票据里还要攥着
    一个 live Client + 二维码对象（没法序列化，所以登录会话只能活在一个进程里）。
    """

    payload: dict[str, Any] = field(default_factory=dict)
    handle: Any = None

@dataclass
class LoginStart:
    ok: bool
    status: str = "waiting"            # waiting（等扫码）| awaiting_code（等验证码）| failed
    ticket: LoginTicket | None = None
    qr_image: str = ""                 # data URL：二维码图片（外层负责投递）
    message: str = ""
    error: str = ""

@dataclass
class LoginState:
    ok: bool
    status: str = "waiting"            # waiting|scanned|confirming|awaiting_code|succeeded|expired|failed
    message: str = ""
    error: str = ""
    hint: str = ""                     # 失败时的排错建议

def normalize(value: Any) -> str:
    """歌名/歌手归一化：去掉括号补充说明、空格与常见标点，用于跨平台比对与去重。"""
    text = re.sub(r"\(.*?\)|（.*?）", "", str(value or "").strip().lower())
    return re.sub(r"[\s\-_/·.,，。]+", "", text)

def song_key(name: Any, artist: Any) -> str:
    normalized = normalize(name)
    artist_key = normalize(artist)
    return f"{normalized}::{artist_key}" if artist_key else normalized

def name_matches(title: Any, expected: Any) -> bool:
    """歌名对不对得上是同一首：归一化后相等，或一方包含另一方。

    用来卡住「搜索只认关键词、结果风马牛不相及」的情况 —— 歌手再对也不能把
    《樱花树下》当成《下一秒相爱》加进去。expected 为空时不做限制。
    """
    title_key, expected_key = normalize(title), normalize(expected)
    if not expected_key:
        return True
    return bool(title_key) and (title_key == expected_key
                                or expected_key in title_key or title_key in expected_key)

def dedupe(songs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """按 (歌名, 歌手) 去重，保留首次出现的顺序。"""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for item in songs:
        if not item[0] or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out

class MusicPlatform:
    """平台接口。三个平台都实现全部方法；没有的能力要么返回空、要么返回 supported=False。"""

    key: Platform = Platform.QQ_MUSIC      # 子类覆盖
    label = ""                             # 展示名：QQ音乐 / 网易云音乐 / 酷狗音乐
    default_method = "phone_code"          # 该平台推荐的登录方式

    # ---------------- 凭证 ----------------

    def status(self) -> dict[str, Any]:
        """凭证状态，字段与 vault.status()[key] 保持一致。"""
        raise NotImplementedError

    def auth_hint(self) -> str:
        """未登录时给模型/用户的提示语。"""
        return (f"{self.label}未登录：先调 login_methods(service='{self.key.value}') 选一种方式，"
                f"再 login_start；也可以 credential_set_cookie 直接导入。")

    def login_methods(self) -> list[dict[str, str]]:
        """该平台支持的登录方式清单（给 agent 转述给用户）。"""
        raise NotImplementedError

    def scan_hint(self, method: str) -> str:
        """二维码/验证码这一步要用户怎么配合。"""
        return "按提示在手机上确认，再调用 login_poll。"

    def set_cookie(self, value: str, uid: str = "") -> dict[str, Any]:
        """直接导入凭证（网易云/酷狗是 Cookie，QQ 是 Credential JSON）。"""
        raise NotImplementedError

    def logout(self) -> None:
        raise NotImplementedError

    async def refresh(self) -> dict[str, Any]:
        """主动续期。平台没有续期接口时返回 supported=False，不算失败。"""
        return {"ok": False, "supported": False, "service": self.key.value,
                "error": f"{self.label}没有续期接口，失效后请重新登录"}

    # ---------------- 登录 ----------------

    async def login_start(self, method: str, phone: str = "", ctcode: int = 86) -> LoginStart:
        raise NotImplementedError

    async def login_poll(self, ticket: LoginTicket) -> LoginState:
        raise NotImplementedError

    async def login_submit(self, ticket: LoginTicket, code: str) -> LoginState:
        raise NotImplementedError

    # ---------------- 基础能力 ----------------

    async def favorites(self) -> tuple[list[Song], list[str], dict[str, Any]]:
        """收藏（我喜欢的音乐）。返回 (歌曲, 来源说明, 诊断信息)。"""
        raise NotImplementedError

    async def recommendations(self) -> tuple[list[Song], list[str], dict[str, Any]]:
        """今日推荐。返回 (歌曲, 来源说明, 诊断信息)。"""
        raise NotImplementedError

    async def search(self, name: str, artist: str = "") -> Any:
        """按歌名+歌手搜一首歌，返回平台自己的结构（解析规则归各平台）。"""
        raise NotImplementedError

    async def delete_playlist(self, playlist_id: str) -> dict[str, Any]:
        """删除一个自建歌单。返回 {"ok","error","service"}。"""
        raise NotImplementedError

    async def my_playlists(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """列出我创建的歌单。返回 (playlists, diagnostics)。"""
        raise NotImplementedError

    async def create_playlist(self, name: str, songs: list[Song],
                              count: int) -> CreateResult:
        """把 songs 建成本平台的同名歌单；匹配不到的直接跳过，写进 failed。"""
        raise NotImplementedError
