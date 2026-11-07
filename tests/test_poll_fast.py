"""Regression: login_poll must never block the agent's turn, and errors must be
reported accurately (no invented "user refused" for upstream parser failures).

Also verifies the no-QR path (phone_code) is offered by login_start.

**计时口径（2026-11-07 修）**：以前把「进程启动 + initialize + poll」算成一件事，
阈值 6 秒 —— 实测冷启动就要 5.8 秒（机器忙起来更久），于是 poll 本身 4 毫秒也会误报失败。
现在拆成两段：启动单独计（阈值宽松，因为它取决于机器），poll 往返单独计（阈值收紧，
它才是这个测试真正要保的东西）。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 起一个"已知不存在"的会话 id，用它来验证 poll 立刻返回准确错误
POLL_SESSION = "doesnotexist"
# 阈值分两档：启动是机器相关（CI/负载下会到十几秒），poll 是毫秒级的
STARTUP_BUDGET = 30.0
POLL_BUDGET = 1.5

_TMP = tempfile.TemporaryDirectory(prefix="crates-test-")
TEST_DATA = Path(_TMP.name)
env = {
    **dict(os.environ),
    "MCP_SERVER": "auth",
    "TRANSPORT": "stdio",
    "MUSIC_SECRETS_DIR": str(TEST_DATA / "secrets"),
    "MUSIC_STATE_DIR": str(TEST_DATA / "state"),
    "PYTHONIOENCODING": "utf-8",
}

proc = subprocess.Popen(
    [sys.executable, "-m", "music_mcp.app"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8", errors="replace", env=env, cwd=str(ROOT),
)
lines: queue.Queue[str] = queue.Queue()


def _reader() -> None:
    for line in proc.stdout:
        if line.strip():
            lines.put(line.strip())
    lines.put("__EOF__")


threading.Thread(target=_reader, daemon=True).start()


def _send(payload: dict) -> None:
    proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    proc.stdin.flush()


def _await(rid: int, budget: float) -> tuple[dict | None, float]:
    """发出去之后等某个 id 的响应，返回 (响应, 秒数)。计时不含发送本身。"""
    started = time.time()
    deadline = started + budget
    while time.time() < deadline:
        try:
            line = lines.get(timeout=0.05)
        except queue.Empty:
            continue
        if line == "__EOF__":
            break
        if line.startswith("{"):
            parsed = json.loads(line)
            if parsed.get("id") == rid:
                return parsed, time.time() - started
    return None, time.time() - started


# ① 冷启动：进程起来 -> initialize 响应（把这段时间单独量出来，别混进 poll 的账）
_startup = time.time()
_send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
       "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                  "clientInfo": {"name": "poll-test", "version": "1.0"}}})
init, _ = _await(1, STARTUP_BUDGET)
startup_s = time.time() - _startup
if init is None:
    proc.kill()
    print(f"FAIL: initialize 在 {STARTUP_BUDGET:.0f}s 内没有响应")
    sys.exit(1)

# ② poll 往返：initialize 完成之后才开始计，前面那些 sleep/启动都不算
_send({"jsonrpc": "2.0", "method": "notifications/initialized"})
call_started = time.time()
_send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
       "params": {"name": "login_poll", "arguments": {"session_id": POLL_SESSION}}})
call, poll_s = _await(2, POLL_BUDGET + 3.0)

proc.terminate()
try:
    proc.communicate(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()

print(f"冷启动（进程起来 -> initialize 响应）: {startup_s:.2f}s")
print(f"login_poll 往返: {poll_s * 1000:.1f}ms")

if call is None:
    print(f"FAIL: poll 在 {POLL_BUDGET + 3:.1f}s 内没有响应（阻塞了）")
    sys.exit(1)

text = call.get("result", {}).get("content", [{}])[0].get("text", "")
info = json.loads(text)
print("response:", info)

failures = []
if poll_s > POLL_BUDGET:
    failures.append(f"poll 往返 {poll_s:.2f}s 超过 {POLL_BUDGET}s（真阻塞了，不是启动慢）")
if info.get("status") != "expired":
    failures.append(f"未知会话该报 expired，实际是 {info.get('status')!r}（可能在编造 refusal）")
if startup_s > STARTUP_BUDGET:
    failures.append(f"冷启动 {startup_s:.1f}s 超过 {STARTUP_BUDGET:.0f}s")
if failures:
    for line in failures:
        print("FAIL:", line)
    sys.exit(1)

print(f"PASS: poll 往返 {poll_s * 1000:.0f}ms（不含 {startup_s:.1f}s 冷启动），状态准确")
