"""Verify login_start returns a native MCP image content block.

Runs the auth MCP over stdio, calls login_start, and checks the response has
both a text block (session_id) and an image block (QR PNG).
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
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.strip():
            lines.put(line.strip())
    lines.put("__EOF__")


threading.Thread(target=_reader, daemon=True).start()

messages = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "qr-test", "version": "1.0"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
     "params": {"name": "login_start",
                "arguments": {"service": "qq_music", "method": "qq_qr"}}},
]
for item in messages:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(item, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    time.sleep(0.4)

responses: list[dict] = []
deadline = time.time() + 25
while time.time() < deadline:
    try:
        line = lines.get(timeout=0.5)
    except queue.Empty:
        continue
    if line == "__EOF__":
        break
    if line.startswith("{"):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        responses.append(parsed)
        if parsed.get("id") == 2:
            break

proc.terminate()
try:
    _, stderr = proc.communicate(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()
    _, stderr = proc.communicate()

call = next((r for r in responses if r.get("id") == 2), None)
if call is None:
    print("NO tools/call RESPONSE")
    print("[stderr]", stderr[:500])
    sys.exit(1)

content = call.get("result", {}).get("content", [])
print("content blocks:", [(c.get("type"), len(c.get("data", "")), len(c.get("text", ""))) for c in content])
types = [c.get("type") for c in content]
if "image" not in types:
    print("FAIL: no image content block")
    sys.exit(1)

texts = [c.get("text", "") for c in content if c.get("type") == "text"]
info = json.loads(texts[0])
print("session_id:", info.get("session_id"))
print("qr_data_url in text:", "qr_data_url" in info)
print("qr_path in text:", "qr_path" in info)
print("scan_hint:", (info.get("scan_hint") or "")[:60])
print("image mimeType:", next(c.get("mimeType") for c in content if c.get("type") == "image"))
print("qr_markdown:", (info.get("qr_markdown") or "")[:80])
print("qr_path in text:", "qr_path" in info)
print("ascii blocks (opt-in, default off):", sum(1 for t in texts if "█" in t or "▀" in t or "▄" in t))
if "qr_path" in info:
    print("FAIL: local path leaked into text")
    sys.exit(1)
if not info.get("qr_markdown"):
    print("FAIL: no qr_markdown for inline display")
    sys.exit(1)
if not info.get("qr_data_url"):
    print("FAIL: no data: URL fallback")
    sys.exit(1)
print("PASS: QR = markdown URL + native image + data URL, no path leak")
