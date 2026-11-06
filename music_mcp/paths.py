"""统一的数据目录。

默认落在**用户级**目录，绝不写进包安装目录 —— site-packages 是只读的、升级会被清空、
多人共用一份，把凭证写进去是隐雷（不少 MCP 包就是这么干的）。

    Windows  %LOCALAPPDATA%\\crates-music
    macOS    ~/Library/Application Support/crates-music
    Linux    ~/.local/share/crates-music   （尊重 XDG_DATA_HOME）

覆盖顺序：MUSIC_DATA_DIR > MUSIC_SECRETS_DIR / MUSIC_STATE_DIR（旧变量） > 默认。
所以别人 `pipx install` 之后**不需要配任何东西**；开发时想指到项目里再加环境变量。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 应用名，同时也是用户目录名
APP_DIR_NAME = "crates-music"


def _default_root() -> Path:
    if sys.platform == "win32":
        base = (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
                or str(Path.home() / "AppData" / "Local"))
        return Path(base) / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / APP_DIR_NAME


def data_root() -> Path:
    """数据根目录（可被 MUSIC_DATA_DIR 覆盖）。"""
    override = os.environ.get("MUSIC_DATA_DIR")
    return Path(override).expanduser() if override else _default_root()


def secrets_dir() -> Path:
    """凭证目录（登录态、Cookie、token）。"""
    legacy = os.environ.get("MUSIC_SECRETS_DIR")
    return Path(legacy).expanduser() if legacy else data_root() / "secrets"


def state_dir() -> Path:
    """状态目录（推荐历史、画像、二维码等可重新生成的东西）。"""
    legacy = os.environ.get("MUSIC_STATE_DIR")
    return Path(legacy).expanduser() if legacy else data_root() / "state"


def describe() -> dict[str, str]:
    """给 credential_status 用：让人一眼看出数据到底落在哪。"""
    return {"data_root": str(data_root()), "secrets_dir": str(secrets_dir()),
            "state_dir": str(state_dir()),
            "override": "MUSIC_DATA_DIR" if os.environ.get("MUSIC_DATA_DIR")
            else ("legacy_env" if (os.environ.get("MUSIC_SECRETS_DIR")
                                   or os.environ.get("MUSIC_STATE_DIR")) else "default")}


# ---------------------------------------------------------------------------
# state 目录下的文件名：一处定义，别在各处拼字符串。
# 命名口径（用户定的）：各平台收藏各自一个文件，画像和推荐历史各一个。
# ---------------------------------------------------------------------------

PROFILE_FILENAME = "music_prefer_profile.json"
RECENT_FILENAME = "recent_music_history.json"

# 旧名字，只为兼容读取（读到就顺手迁到新名字）；新代码一律不要再写这些
LEGACY_PROFILE_FILENAMES = ("profile.json", "profile_summary.json")
LEGACY_RECENT_FILENAMES = ("recent_history.json",)
LEGACY_SONGS_RAW_FILENAME = "songs_raw.json"


def state_file(name: str):
    return state_dir() / name


def liked_filename(platform: str) -> str:
    """各平台的收藏：``qq_music_liked.json`` / ``netease_liked.json`` / ``kugou_liked.json``。"""
    return f"{platform}_liked.json"


def liked_file(platform: str):
    return state_file(liked_filename(platform))
