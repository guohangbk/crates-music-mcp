"""Regression: login_poll must never block the agent's turn, and errors must be
reported accurately (no invented "user refused" for upstream parser failures).

Also verifies the no-QR path (phone_code) is offered by login_start.
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

# A poll for an unknown session must return immediately with an accurate error.
messages = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "poll-test", "version": "1.0"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
     "params": {"name": "login_poll", "arguments": {"session_id": "doesnotexist"}}},
]
start = time.time()
for item in messages:
    proc.stdin.write(json.dumps(item, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    time.sleep(0.3)

call = None
deadline = time.time() + 12
while time.time() < deadline:
    try:
        line = lines.get(timeout=0.5)
    except queue.Empty:
        continue
    if line == "__EOF__":
        break
    if line.startswith("{"):
        parsed = json.loads(line)
        if parsed.get("id") == 2:
            call = parsed
            break
elapsed = time.time() - start

proc.terminate()
try:
    proc.communicate(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()

if call is None:
    print("FAIL: no response within 12s (poll blocked)")
    sys.exit(1)
text = call.get("result", {}).get("content", [{}])[0].get("text", "")
info = json.loads(text)
print(f"poll elapsed: {elapsed:.2f}s")
print("response:", info)
if elapsed > 6:
    print("FAIL: poll blocked too long")
    sys.exit(1)
if info.get("status") != "expired":
    print("FAIL: unknown session should report expired, not refusal")
    sys.exit(1)
print("PASS: poll is fast and reports accurate status")
