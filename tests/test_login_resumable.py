"""登录会话的两个行为：真实有效期上报 + 跨进程续。

背景（Hermes 反馈 2 / 3）：
  * 「票据可序列化、poll 时现场查一次」到底能不能做到跨进程续？
  * 二维码有效期报给用户的数字是不是真的？

跑法：python tests/test_login_resumable.py [netease|qq_music]
默认两个平台都跑（都只用到二维码，不需要任何登录凭证）。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"[{'ok' if condition else 'FAIL'}] {name}{'  ' + detail if detail else ''}")


# 各平台的扫码方式名不一样（QQ 是 qq_qr / wx_qr / mobile_qr）
SCAN_METHOD = {"netease": "qr", "qq_music": "qq_qr", "kugou": "qr"}


def _env(data_dir: Path) -> dict[str, str]:
    return {**os.environ, "MUSIC_DATA_DIR": str(data_dir), "PYTHONUTF8": "1"}


async def _start(service: str) -> dict:
    from music_mcp.login import login_sessions
    return await login_sessions.start(service, SCAN_METHOD[service])


async def _poll(service: str, session_id: str) -> dict:
    from music_mcp.login import login_sessions
    return await login_sessions.poll(session_id)


async def scenario(service: str) -> None:
    from music_mcp import login as login_mod

    started = await _start(service)
    check(f"{service}: login_start 返回二维码", bool(started.get("qr_data_url")),
          f"session={started.get('session_id')} error={started.get('error')}")
    check(f"{service}: 上报的有效期不是默认 600",
          int(started.get("expires_in_seconds") or 0) != login_mod.SESSION_TTL,
          f"expires_in_seconds={started.get('expires_in_seconds')}")

    # 同进程 poll 一次（真打上游）
    state = await _poll(service, started["session_id"])
    check(f"{service}: 同进程 poll 正常", state.get("status") not in {"expired", None},
          f"status={state.get('status')}")

    # 会话必须落盘，且票据是纯数据（可序列化）
    sessions_file = Path(os.environ["MUSIC_DATA_DIR"]) / "state" / "login_sessions.json"
    check(f"{service}: 会话已落盘", sessions_file.is_file(), str(sessions_file))
    stored = json.loads(sessions_file.read_text(encoding="utf-8")) if sessions_file.is_file() else {}
    entry = stored.get(started["session_id"]) or {}
    check(f"{service}: 落盘的是纯数据票据（无 live 句柄）",
          isinstance(entry.get("ticket"), dict) and bool(entry["ticket"]),
          json.dumps(entry.get("ticket", {}), ensure_ascii=False)[:80])

    # 换一个全新进程 poll 同一个 session_id —— 这就是"重启后还能续"
    code = (
        "import asyncio, json, sys;"
        f"sys.path.insert(0, {str(ROOT)!r});"
        "from music_mcp.login import login_sessions;"
        f"print(json.dumps(asyncio.run(login_sessions.poll({started['session_id']!r})), ensure_ascii=False))"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=_env(Path(os.environ["MUSIC_DATA_DIR"])))
    fresh = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
    check(f"{service}: 新进程能续上同一个 session（不再说「会话不存在」）",
          bool(fresh) and "不存在或已过期" not in str(fresh.get("error") or ""),
          f"status={fresh.get('status')} error={fresh.get('error')}")


async def cross_client_scenario(service: str) -> None:
    """真·跨客户端：B 先 poll 一次（那时盘上还是空的），A 之后才建会话，B 仍要能看见。"""
    from music_mcp import login as login_mod

    data_dir = Path(os.environ["MUSIC_DATA_DIR"])
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    (data_dir / "state" / "login_sessions.json").write_text("{}", encoding="utf-8")

    other = login_mod.LoginSessions()                     # 模拟"另一个客户端"
    early = await other.poll("nope")                      # 先读一次盘（空）
    check(f"{service}: 另一个客户端对未知 session 立刻报 expired",
          early.get("status") == "expired", str(early.get("error"))[:40])

    started = await _start(service)                       # A 这时才建会话
    later = await other.poll(started["session_id"])       # B 用同一张表再查
    check(f"{service}: 另一个客户端能看见后建的会话",
          later.get("status") not in {"expired", None},
          f"status={later.get('status')} error={later.get('error')}")


async def expired_scenario(service: str) -> None:
    """伪造一个已过期的会话：poll 必须报 expired，而不是拿 600 秒去糊弄用户。"""
    from music_mcp import login as login_mod

    data_dir = Path(os.environ["MUSIC_DATA_DIR"])
    sessions_file = data_dir / "state" / "login_sessions.json"
    session_id = "expiredtest1"
    sessions_file.parent.mkdir(parents=True, exist_ok=True)
    sessions_file.write_text(json.dumps({
        session_id: {"service": service, "status": "running",
                     "created_at": 1, "expires_at": 1,
                     "ticket": {"unikey": "deadbeef"}, "id": session_id},
    }), encoding="utf-8")
    fresh = login_mod.LoginSessions()          # 全新的注册表 = 新进程
    state = await fresh.poll(session_id)
    check(f"{service}: 过期会话直接报 expired", state.get("status") == "expired",
          str(state.get("error"))[:60])


def main() -> int:
    services = [sys.argv[1]] if len(sys.argv) > 1 else ["netease", "qq_music"]
    with tempfile.TemporaryDirectory(prefix="crates-login-") as tmp:
        os.environ["MUSIC_DATA_DIR"] = tmp
        for service in services:
            asyncio.run(scenario(service))
        for service in services:
            asyncio.run(cross_client_scenario(service))
        asyncio.run(expired_scenario(services[0]))
    print(f"\n通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("失败项:", ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
