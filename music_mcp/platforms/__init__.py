"""平台注册表：一个平台一个文件，都实现 ``base.MusicPlatform`` 那套接口。

上层（compose.py / servers.py）只认 :class:`Platform` 枚举，不认识任何平台实现细节。
新增一个平台 = 加一个文件 + 在这里登记一行。
"""
from __future__ import annotations

from typing import Any

from .base import (CreateResult, LoginStart, LoginState, LoginTicket, MusicPlatform,
                   Platform, Song, dedupe, key_of, normalize, platform_of, song_key)
from .kugou import KugouPlatform
from .netease import NeteasePlatform
from .qq import QQMusicPlatform

# 顺序 = 工具里列平台、以及「全平台」生成歌单时的默认顺序
DEFAULT_ORDER: tuple[Platform, ...] = (Platform.QQ_MUSIC, Platform.NETEASE, Platform.KUGOU)

_PLATFORMS: dict[Platform, MusicPlatform] = {
    QQMusicPlatform.key: QQMusicPlatform(),
    NeteasePlatform.key: NeteasePlatform(),
    KugouPlatform.key: KugouPlatform(),
}


def get_platform(key: Any) -> MusicPlatform | None:
    """按枚举 / 字符串取平台；认不出的返回 None（不做静默兜底）。"""
    member = platform_of(key)
    return _PLATFORMS.get(member) if member else None


def all_platforms() -> dict[Platform, MusicPlatform]:
    return dict(_PLATFORMS)


def resolve_keys(services: Any = None) -> list[Platform]:
    """把 ``None`` / ``"all"`` / ``"qq_music,netease"`` / ``[Platform.KUGOU]`` 统一成有序枚举列表。

    空值 = 全部平台；未知的名字直接忽略。
    """
    if isinstance(services, (list, tuple, set)):
        requested = list(services)
    else:
        requested = [part.strip() for part in str(services or "").replace("，", ",").split(",")]
    requested = [item for item in requested if item is not None and str(item) != ""]
    if not requested or any(key_of(item).lower() == "all" for item in requested):
        return list(DEFAULT_ORDER)
    wanted = {member for member in (platform_of(item) for item in requested) if member}
    return [member for member in DEFAULT_ORDER if member in wanted]


__all__ = ["CreateResult", "LoginStart", "LoginState", "LoginTicket", "MusicPlatform",
           "Platform", "Song", "dedupe", "key_of", "normalize", "platform_of", "song_key",
           "DEFAULT_ORDER", "get_platform", "all_platforms", "resolve_keys"]
