"""酷狗调用的「一次性子进程」入口。

背景：``kuGouMusicApi`` 的原生引擎在同一进程里第 2 次之后的调用会随机
``OSError: access violation``，崩过之后还会卡死且没有超时保护。但**每个新进程的
第一次调用几乎必成功**。所以把每一次路由调用放进一个全新进程，用完立即退出，
崩溃就随进程一起消失，主进程永远干净。

协议（刻意不用 stdin/stdout 传结果）：
    argv[1] = 请求 JSON 文件  {"route": "...", "cookie": {...}, "params": {...}}
    argv[2] = 结果 JSON 文件  {"ok": true, "body": {...}} / {"ok": false, "error": "..."}

用文件而不是管道，是因为引擎会往 fd 1 打日志（甚至直接写原生 stdout），
任何管道都会污染 JSON-RPC 或结果解析。这里把 fd 1 直接重定向到 fd 2。

独立可跑：不 import music_mcp 的任何模块，避免相对导入与包路径问题。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def _protect_stdout() -> None:
    """把原生库的 printf 从 fd 1 赶到 fd 2。"""
    try:
        sys.stdout.flush()
        os.dup2(2, 1)
    except Exception:
        pass


def _unwrap(body: Any) -> dict[str, Any]:
    """引擎返回的 body 可能是 dict，也可能是带 <!--KG_TAG_RES_START--> 前缀的字符串。"""
    if isinstance(body, dict):
        return body
    text = str(body or "").strip()
    start = text.find("{")
    if start < 0:
        return {}
    try:
        payload = json.loads(text[start:])
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def main() -> None:
    _protect_stdout()
    if len(sys.argv) < 3:
        sys.stderr.write("kugou_worker: 需要 request/result 两个文件路径\n")
        return
    request_path, result_path = Path(sys.argv[1]), Path(sys.argv[2])

    def write(payload: dict[str, Any]) -> None:
        try:
            result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # 写盘失败也要留个痕迹，别让主进程空等
            sys.stderr.write(f"kugou_worker: 写结果失败 {exc}\n")

    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        route = str(request.get("route") or "")
        cookie = request.get("cookie") or {}
        params = request.get("params") or {}
        if not route:
            write({"ok": False, "error": "route 为空"})
            return

        from MusicLibrary.kuGouMusicApi import KuGouMusicApi

        api = KuGouMusicApi()
        raw = getattr(api, route)(cookie=cookie, **params)
        body = _unwrap(getattr(raw, "body", None))
        write({"ok": True, "body": body})
    except Exception as exc:  # 包括原生层的 OSError / access violation
        write({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"})


if __name__ == "__main__":
    main()
