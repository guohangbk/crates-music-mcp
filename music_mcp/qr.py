"""二维码工具：本地渲染 + 落盘 + 回环 HTTP 投递。与平台无关，三家登录流程共用。

为什么自己渲染：网易云 / 酷狗的本地引擎有的只回登录链接文本、有的回 base64，
统一在这里兜住，平台文件就不用各自写一遍。

为什么还要一个回环 HTTP：MCP 的 ImageContent 常被客户端折叠在工具结果抽屉里，
纯文本二维码又受字体/主题/窄屏影响；一个普通图片 URL 放进 Markdown 反而最稳。
只绑 127.0.0.1，懒启动。

二维码是**一次性**的：登录会话一结束（成功 / 过期 / 失败）就 delete()，
启动时再兜底清掉超过 1 天的遗留文件。
"""
from __future__ import annotations

import base64
import io
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import paths

PORT = int(os.environ.get("QR_HTTP_PORT", "8788"))
_server: ThreadingHTTPServer | None = None
_lock = threading.Lock()

def qr_dir():
    return paths.state_dir() / "qr"

def data_url(text: str) -> str:
    """把二维码内容（链接/字符串）渲染成 data URL。"""
    if not text:
        return ""
    try:
        import qrcode

        image = qrcode.make(text)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
    except Exception as exc:  # 渲染失败不该把登录流程整个搞挂
        print(f"[qr] 本地二维码渲染失败: {exc}", file=sys.stderr, flush=True)
        return ""

def save_png(session_id: str, image: str) -> str:
    """落盘一份二维码，返回绝对路径（部分客户端会把本地路径当图片发出去）。"""
    raw = image.split(",", 1)[1] if image.startswith("data:") and "," in image else image
    if not raw:
        return ""
    directory = qr_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.png"
    try:
        path.write_bytes(base64.b64decode(raw))
        cleanup()
        return str(path)
    except Exception:
        return ""

def delete(session_id: str) -> None:
    """删掉某个会话的二维码（登录成功 / 过期 / 失败后都调它）。"""
    if not session_id:
        return
    try:
        (qr_dir() / f"{session_id}.png").unlink(missing_ok=True)
    except OSError:
        return
    _drop_empty_dir()

def cleanup(max_age: float = 86400.0) -> int:
    """兜底扫一遍：删掉超过 max_age 秒的遗留二维码（默认 1 天）。返回删了几个。"""
    directory = qr_dir()
    if not directory.is_dir():
        return 0
    removed, now = 0, time.time()
    for path in directory.glob("*.png"):
        try:
            if now - path.stat().st_mtime > max_age:
                path.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        _drop_empty_dir()
    return removed

def _drop_empty_dir() -> None:
    """目录空了就顺手删掉，别留一个空壳。"""
    try:
        qr_dir().rmdir()
    except OSError:
        pass

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if not self.path.startswith("/qr/") or not self.path.endswith(".png"):
            self.send_error(404)
            return
        name = self.path.rsplit("/", 1)[-1]
        if not name.replace(".png", "").isalnum():
            self.send_error(400)
            return
        path = qr_dir() / name
        if not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        pass  # keep stdout clean for MCP stdio clients

def start() -> int:
    """Start the server once, return the port (0 if it could not bind)."""
    global _server
    with _lock:
        if _server is not None:
            return PORT
        try:
            qr_dir().mkdir(parents=True, exist_ok=True)
            _server = ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
        except OSError:
            return 0
        threading.Thread(target=_server.serve_forever, daemon=True).start()
        return PORT

def url(session_id: str) -> str:
    """回环图片 URL；起不来就返回空串（调用方回落到 data URL）。"""
    port = start()
    if not port:
        return ""
    return f"http://127.0.0.1:{port}/qr/{session_id}.png"
