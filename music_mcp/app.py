"""Entry point: one process per MCP server.

Environment:
  MCP_SERVER  all | auth | profile | playlist   (all = 一个 server 带全部工具)
  PORT        HTTP port                   (default 8765)
  TRANSPORT    stdio | http               (default: stdio for entry points, http for -m)
  NCM_BACKEND    已废弃，只剩纯 Python 一套后端（设了别的值也照样走 native，仅提示一句）
  MUSIC_DATA_DIR             数据根目录（凭证 + 状态）；默认用户级目录，不写进安装目录
  MUSIC_SECRETS_DIR / MUSIC_STATE_DIR   旧的分别覆盖写法，仍兼容（容器里由 compose 指定）

Run:
  python -m music_mcp.app                          # HTTP（服务器上由 systemd 常驻）
  MCP_SERVER=all PORT=8765 python -m music_mcp.app
  crates-music                                     # stdio, 单 server 全功能（推荐）
  crates-auth                                      # stdio, 只带登录/凭证工具
  crates-profile
  crates-playlist
"""
from __future__ import annotations

import os
import sys

import uvicorn

from . import compose
from .servers import build_server


def _protect_stdout() -> None:
    """让原生库的 printf 走 stderr，别把 MCP 的 JSON-RPC 通道搅乱。

    Netease 的本地后端是 C/QuickJS 库，日志直接写 fd 1；stdio 模式下 fd 1 就是
    协议通道，多打一行 JSON 解析就崩。做法：把 fd 1 指向 stderr，Python 自己
    留一份原 fd 1 的副本继续做 stdout（因此 print / MCP 都还写到客户端）。
    """
    try:
        sys.stdout.flush()
        saved = os.dup(1)
        os.dup2(2, 1)
        sys.stdout = os.fdopen(saved, "w", buffering=1, encoding="utf-8", errors="replace")
    except Exception:
        pass


def _start(name: str, transport: str, port: int) -> None:
    _protect_stdout()
    # 旧 state 文件名（profile.json / profile_summary.json / recent_history.json / songs_raw.json）
    # 一次性迁到新口径；幂等，失败也不该挡启动。
    try:
        compose.migrate_legacy()
    except Exception:
        pass
    mcp = build_server(name)
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    # uvicorn 只在 http 传输时才导入：它自己约 175ms（进程内 importtime 实测），
    # 而 stdio 是绝大多数用法 —— MCP 客户端每次 spawn 进程都要付一次这个导入成本。
    import uvicorn

    app = mcp.streamable_http_app()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


def main() -> None:
    """Entry point for crates-auth (stdio by default)."""
    name = os.environ.get("MCP_SERVER", "auth")
    transport = os.environ.get("TRANSPORT", "stdio")
    port = int(os.environ.get("PORT", "8765"))
    _start(name, transport, port)


def main_all() -> None:
    """Entry point for crates-music: ONE stdio server exposing every music tool."""
    name = os.environ.get("MCP_SERVER", "all")
    transport = os.environ.get("TRANSPORT", "stdio")
    port = int(os.environ.get("PORT", "8765"))
    _start(name, transport, port)


def main_profile() -> None:
    """Entry point for crates-profile (stdio by default)."""
    name = os.environ.get("MCP_SERVER", "profile")
    transport = os.environ.get("TRANSPORT", "stdio")
    port = int(os.environ.get("PORT", "8766"))
    _start(name, transport, port)


def main_playlist() -> None:
    """Entry point for crates-playlist (stdio by default)."""
    name = os.environ.get("MCP_SERVER", "playlist")
    transport = os.environ.get("TRANSPORT", "stdio")
    port = int(os.environ.get("PORT", "8767"))
    _start(name, transport, port)


if __name__ == "__main__":
    # `python -m music_mcp.app` → HTTP（systemd / 反向代理都用这个）
    transport = os.environ.get("TRANSPORT", "http")
    name = os.environ.get("MCP_SERVER", "auth")
    port = int(os.environ.get("PORT", "8765"))
    _start(name, transport, port)
