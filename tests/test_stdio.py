"""Smoke test: drive each MCP server over stdio (the transport Codex uses).

Usage: python tests/test_stdio.py [auth|profile|playlist]
Sends initialize + initialized + tools/list over newline-delimited JSON,
then closes stdin and reads all responses from stdout.
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
PY = sys.executable
NAME = sys.argv[1] if len(sys.argv) > 1 else "auth"

env = {
    **dict(os.environ),
    "MCP_SERVER": NAME,
    "TRANSPORT": "stdio",
    "MUSIC_SECRETS_DIR": str(TEST_DATA / "secrets"),
    "MUSIC_STATE_DIR": str(TEST_DATA / "state"),
    "PYTHONIOENCODING": "utf-8",
}

messages = [
    {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "crates-smoke", "version": "1.0"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
]

proc = subprocess.Popen(
    [PY, "-m", "music_mcp.app"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    encoding="utf-8",
    errors="replace",
    env=env,
    cwd=str(ROOT),
)

lines: queue.Queue[str] = queue.Queue()


def _reader() -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.strip():
            lines.put(line.strip())
    lines.put("__EOF__")


threading.Thread(target=_reader, daemon=True).start()
for item in messages:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(item, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    time.sleep(0.35)

responses: list[dict] = []
deadline = time.time() + 8
while time.time() < deadline and len(responses) < 2:
    try:
        line = lines.get(timeout=0.5)
    except queue.Empty:
        continue
    if line == "__EOF__":
        break
    if line.startswith("{"):
        try:
            responses.append(json.loads(line))
        except json.JSONDecodeError:
            pass

proc.terminate()
try:
    _, stderr = proc.communicate(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()
    _, stderr = proc.communicate()

print(f"=== {NAME} ===")
if stderr.strip():
    print("[stderr]", stderr.strip()[:300])
for item in responses:
    if item.get("id") == 1:
        print("initialize ->", item.get("result", {}).get("serverInfo", {}).get("name"))
    elif item.get("id") == 2:
        tools = item.get("result", {}).get("tools", [])
        print("tools ->", [t["name"] for t in tools])

if not responses:
    print("NO RESPONSES — stdio transport broken")
    sys.exit(1)
