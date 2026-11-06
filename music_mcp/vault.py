"""所有音乐 MCP server 共享的凭证库。

所有 MCP 进程（all / auth / profile / playlist）都读写同一份凭证文件，目录由
paths.py 决定（默认用户级目录，绝不写进包安装目录）。跨进程 fcntl 锁守着
「读取 -> 刷新 -> 回写」这一圈，保证会轮换的 refresh_token 不会被两个进程同时刷新。

只有 auth 侧持有登录会话；其余进程都是纯消费者 —— 这就是「多个 MCP 共用一份登录态」的实现方式。
"""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import paths

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# 目录统一由 paths.py 决定：默认用户级目录（绝不在包的安装目录里写东西），
# MUSIC_DATA_DIR / MUSIC_SECRETS_DIR / MUSIC_STATE_DIR 可覆盖。
SECRETS = paths.secrets_dir()
STATE = paths.state_dir()
QQ_FILE = SECRETS / "qq_music.json"
NCM_FILE = SECRETS / "netease.json"
KG_FILE = SECRETS / "kugou.json"
LOCK_FILE = STATE / "vault.lock"

# 必需字段：缺任何一个都调不动接口。
QQ_REQUIRED = ("musicid", "musickey", "openid")
# 续期字段：各登录方式拿到的凭证形态不一样 —— 手机客户端扫码（mobile_qr）**不回 access_token**，
# 短信验证码（phone_code）不回 refresh_token，但都会给 refresh_key。所以只要求「至少有一个」，
# 否则会把一份能用的登录态判成未登录。
QQ_REFRESH_ANY = ("refresh_key", "refresh_token", "access_token")

# 网易云 Cookie 的到期时间不可预测（上游不返回 expires），只能按使用周期主动续期：
# 距上次「登录/续期」超过这个时长就先调一次 /login/refresh，这样常驻的 Cookie 不会放坏。
NCM_COOKIE_TTL = 12 * 3600


def _ensure_dirs() -> None:
    SECRETS.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)


@contextmanager
def vault_lock() -> Iterator[None]:
    """Inter-process exclusive lock. Use around ANY credential refresh."""
    _ensure_dirs()
    with LOCK_FILE.open("a+") as handle:
        if sys.platform == "win32":
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if sys.platform == "win32":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_secret(path: Path, payload: dict[str, Any]) -> None:
    _ensure_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)


def get_qq() -> dict[str, Any]:
    payload = read_json(QQ_FILE)
    if isinstance(payload.get("credential_json"), str):
        try:
            payload = json.loads(payload["credential_json"])
        except json.JSONDecodeError:
            return {}
    return payload


def get_ncm() -> dict[str, Any]:
    return read_json(NCM_FILE)


def ncm_cookie() -> str:
    return str(get_ncm().get("cookie") or "").strip()


def ncm_cookie_is_valid(cookie: str) -> bool:
    """网易云登录态的唯一硬指标是 MUSIC_U —— 没有它，这串 Cookie 一定是残的。"""
    return "MUSIC_U=" in (cookie or "")


def merge_cookie(old: str, new: str) -> str:
    """按 key 合并两份 Cookie。

    /login/refresh 大多数时候回整份 Cookie，但字段少一个就会把好登录态刷成残片，
    所以永远以旧 Cookie 打底、用新值覆盖同名 key。
    """
    merged: dict[str, str] = {}
    for blob in (old, new):
        for part in str(blob or "").split(";"):
            part = part.strip()
            if not part:
                continue
            key, _, value = part.partition("=")
            key = key.strip()
            if key:
                merged[key] = value.strip()
    return "; ".join(f"{key}={value}" if value else key for key, value in merged.items())


def set_qq(payload: dict[str, Any]) -> None:
    write_secret(QQ_FILE, payload)


def set_ncm(cookie: str, extra: dict[str, Any] | None = None) -> None:
    # 以旧 payload 打底：续期时只回传 cookie，不能把 uid / source 这些元信息丢掉。
    payload = dict(get_ncm())
    payload.update({"cookie": cookie.strip(), "updated_at": time.time()})
    if extra:
        payload.update({key: value for key, value in extra.items() if value not in (None, "")})
    write_secret(NCM_FILE, payload)


def get_kugou() -> dict[str, Any]:
    return read_json(KG_FILE)


def kugou_cookie() -> str:
    return str(get_kugou().get("cookie") or "").strip()


def kugou_cookie_is_valid(cookie: str) -> bool:
    """酷狗登录态的硬指标是 token —— 没有它，这串 Cookie 一定是残的。"""
    return "token=" in (cookie or "")


def set_kugou(cookie: str, extra: dict[str, Any] | None = None) -> None:
    payload = dict(get_kugou())
    payload.update({"cookie": cookie.strip(), "updated_at": time.time()})
    if extra:
        payload.update({key: value for key, value in extra.items() if value not in (None, "")})
    write_secret(KG_FILE, payload)


def ncm_needs_refresh(ttl: int = NCM_COOKIE_TTL) -> bool:
    payload = get_ncm()
    if not ncm_cookie_is_valid(str(payload.get("cookie") or "")):
        return False
    updated = float(payload.get("updated_at") or 0)
    return updated <= 0 or time.time() - updated >= ttl


def qq_deadline(payload: dict[str, Any]) -> int | None:
    deadlines: list[int] = []
    expired_at = int(payload.get("expired_at") or 0)
    if expired_at > 0:
        deadlines.append(expired_at)
    created = int(payload.get("musickeyCreateTime") or payload.get("musickey_create_time") or 0)
    expires_in = int(payload.get("keyExpiresIn") or payload.get("key_expires_in") or 0)
    if created > 0 and expires_in > 0:
        deadlines.append(created + expires_in)
    return min(deadlines) if deadlines else None


def qq_is_complete(payload: dict[str, Any]) -> bool:
    if not payload or not all(str(payload.get(key) or "").strip() for key in QQ_REQUIRED):
        return False
    return any(str(payload.get(key) or "").strip() for key in QQ_REFRESH_ANY)


def qq_needs_refresh(payload: dict[str, Any], skew: int = 6 * 3600) -> bool:
    deadline = qq_deadline(payload)
    return qq_is_complete(payload) and deadline is not None and time.time() >= max(0, deadline - skew)


def status() -> dict[str, Any]:
    qq = get_qq()
    ncm = get_ncm()
    kg = get_kugou()
    deadline = qq_deadline(qq)
    return {
        "qq_music": {
            "configured": qq_is_complete(qq),
            "musicid": str(qq.get("musicid") or ""),
            "expires_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(deadline)) if deadline else None,
            "refresh_needed": qq_needs_refresh(qq),
        },
        "netease": {
            "configured": ncm_cookie_is_valid(ncm_cookie()),
            "uid": str(ncm.get("uid") or ""),
            "refresh_due": ncm_needs_refresh(),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ncm["updated_at"]))
            if ncm.get("updated_at") else None,
        },
        "kugou": {
            "configured": kugou_cookie_is_valid(kugou_cookie()),
            "userid": str(kg.get("userid") or ""),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(kg["updated_at"]))
            if kg.get("updated_at") else None,
        },
        "secrets_dir": str(SECRETS),
        "lock_file": str(LOCK_FILE),
        **paths.describe(),
    }
