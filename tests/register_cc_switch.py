"""把 crates 的 MCP server 注册进 cc-switch 自己的数据库。

cc-switch 把 MCP 条目存在 `~/.cc-switch/cc-switch.db`（表 `mcp_servers`），
**不是** `~/.codex/config.toml` —— 它的界面读那张表，只改 config.toml 不会出现在 cc-switch 里。

幂等：先按 id 删除再插入。跑之前先备份数据库。

路径全部从环境变量 / 仓库位置推导，没有任何写死的本机路径：
  CC_SWITCH_DB                     cc-switch 数据库（默认 ~/.cc-switch/cc-switch.db）
  MUSIC_SECRETS_DIR / MUSIC_STATE_DIR   凭证与状态目录（默认仓库 .data/ 下）
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = Path(os.environ.get("CC_SWITCH_DB") or (Path.home() / ".cc-switch" / "cc-switch.db"))
BIN = REPO / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
SECRETS = os.environ.get("MUSIC_SECRETS_DIR") or str(REPO / ".data" / "secrets")
STATE = os.environ.get("MUSIC_STATE_DIR") or str(REPO / ".data" / "state")

SERVERS = [
    # (db id / config.toml 段名, exe 名, MCP_SERVER 值)
    ("music_auth", "crates-auth", "auth"),
    ("music_profile", "crates-profile", "profile"),
    ("music_playlist", "crates-playlist", "playlist"),
]

con = sqlite3.connect(DB)
for name, exe, mcp_server in SERVERS:
    config = {
        "type": "stdio",
        "command": str(BIN / f"{exe}{EXE_SUFFIX}"),
        "args": [],
        "startup_timeout_sec": 30,
        "env": {
            "MUSIC_SECRETS_DIR": SECRETS,
            "MUSIC_STATE_DIR": STATE,
            "MCP_SERVER": mcp_server,
        },
    }
    con.execute("delete from mcp_servers where id = ?", (name,))
    con.execute(
        """insert into mcp_servers
           (id, name, server_config, description, tags,
            enabled_claude, enabled_codex, enabled_gemini, enabled_grokbuild,
            enabled_opencode, enabled_hermes, enabled_mcode, enabled_pi)
           values (?, ?, ?, ?, ?, 0, 1, 0, 0, 0, 0, 0, 0)""",
        (name, name, json.dumps(config, ensure_ascii=False),
         "crates: shared QQ Music / NetEase credential vault", '["music","mcp"]'),
    )
con.commit()
print("registered:")
for row in con.execute("select id, enabled_codex, server_config from mcp_servers"):
    print(" ", row[0], "codex=", row[1], "|", row[2][:120])
