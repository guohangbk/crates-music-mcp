"""Verify login method catalogue, cookie import, and account diagnostics."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 用临时目录，绝不能碰真实凭证：credential_set_cookie 会覆盖 netease.json。
_TMP = tempfile.TemporaryDirectory(prefix="crates-test-")
_TMP_ROOT = Path(_TMP.name)
env = {
    **dict(os.environ),
    "MCP_SERVER": "auth",
    "TRANSPORT": "stdio",
    "MUSIC_SECRETS_DIR": str(_TMP_ROOT / "secrets"),
    "MUSIC_STATE_DIR": str(_TMP_ROOT / "state"),
    "PYTHONIOENCODING": "utf-8",
}

proc = subprocess.Popen(
    [sys.executable, "-m", "music_mcp.app"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8", errors="replace", env=env, cwd=str(ROOT),
)
q: queue.Queue[str] = queue.Queue()


def _reader() -> None:
    for line in proc.stdout:
        if line.strip():
            q.put(line.strip())
    q.put("__EOF__")


threading.Thread(target=_reader, daemon=True).start()

calls = [
    ("login_methods", {"service": "qq_music"}),
    ("login_methods", {"service": "netease"}),
    ("credential_set_cookie", {"service": "netease", "value": "MUSIC_U=test_fixture; __csrf=abc"}),
    ("credential_status", {}),
]
messages = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "methods-test", "version": "1.0"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
]
for i, (name, args) in enumerate(calls, start=2):
    messages.append({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                     "params": {"name": name, "arguments": args}})

for item in messages:
    proc.stdin.write(json.dumps(item, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    time.sleep(0.3)

responses: dict[int, dict] = {}
deadline = time.time() + 15
while time.time() < deadline and len(responses) < len(calls) + 1:
    try:
        line = q.get(timeout=0.5)
    except queue.Empty:
        continue
    if line == "__EOF__":
        break
    if line.startswith("{"):
        parsed = json.loads(line)
        if "id" in parsed:
            responses[parsed["id"]] = parsed

proc.terminate()
try:
    proc.communicate(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()


def text_of(rid: int) -> dict:
    blocks = responses.get(rid, {}).get("result", {}).get("content", [])
    return json.loads(blocks[0]["text"]) if blocks else {}


qq = text_of(2)
ncm = text_of(3)
imp = text_of(4)
st = text_of(5)

print("qq methods:", [m["method"] for m in qq.get("methods", [])], "default:", qq.get("default"))
print("ncm methods:", [m["method"] for m in ncm.get("methods", [])], "default:", ncm.get("default"))
print("cookie import:", imp.get("ok"), imp.get("source"))
print("netease configured:", st.get("netease", {}).get("configured"))

assert {m["method"] for m in qq["methods"]} == {"phone_code", "qq_qr", "wx_qr", "mobile_qr"}
assert qq["default"] == "phone_code"
assert ncm["default"] == "cookie"
assert imp["ok"] and st["netease"]["configured"]
print("PASS: methods + cookie import")
