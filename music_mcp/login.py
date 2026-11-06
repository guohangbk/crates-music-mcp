"""登录会话注册表。MCP 工具是无状态的请求/响应，所以「正在进行的登录」只能存在进程内存里。

这里只做三件**平台无关**的事：发号（session_id）、回收（TTL + 单平台只留一个待登录会话）、
按 session_id 找回平台。怎么扫码、怎么发短信、成功后写哪份凭证，全在 ``platforms/*.py`` 里。

这就解释了为什么还得有这么一个文件：``login_poll(session_id)`` 只拿到一个 id，
不知道是哪个平台，必须有个地方把它映射回去。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from . import qr
from .platforms import get_platform
from .platforms.base import LoginState

SESSION_TTL = 600
FINAL_STATUSES = {"succeeded", "expired", "failed", "parse_error"}

LOGIN_USAGE = ("让用户选一个 method，然后调用 login_start(service, method, phone=...)。"
               "手机验证码路径：login_start -> 用户在对话里给出验证码 -> login_submit。")

class LoginSessions:
    """所有平台的登录会话都住在这里；票据本身归平台所有。"""

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def start(self, service: Any, method: str, phone: str = "",
                    ctcode: int = 86) -> dict[str, Any]:
        platform = get_platform(service)
        if platform is None:
            return {"ok": False, "error": f"unknown service: {service}"}
        async with self._lock:
            await self._gc()
            # 每个平台同时只允许一个待登录会话：新码会顶掉旧码，扫到旧码会拿到 REFUSE(68)。
            retired = 0
            for sid, old in list(self._sessions.items()):
                if old["platform"] is platform and old["status"] == "running":
                    qr.delete(sid)
                    self._sessions.pop(sid, None)
                    retired += 1
            session_id = uuid.uuid4().hex[:12]
            self._sessions[session_id] = {"id": session_id, "platform": platform,
                                          "status": "running", "created_at": time.time(),
                                          "ticket": None, "result": None}
        try:
            started = await platform.login_start(method, phone, ctcode)
        except Exception as exc:
            await self._drop(session_id)
            return {"ok": False, "service": platform.key.value, "status": "failed",
                    "error": f"{exc.__class__.__name__}: {exc}"}

        session = self._sessions.get(session_id)
        if session is None:
            return {"ok": False, "error": "登录会话已失效，请重新 login_start"}
        session["ticket"] = started.ticket
        if not started.ok or started.ticket is None:
            session["status"] = started.status or "failed"
            await self._drop(session_id)

        payload: dict[str, Any] = {
            "ok": started.ok, "service": platform.key.value, "status": started.status,
            "message": started.message, "error": started.error,
            "scan_hint": platform.scan_hint(method), "session_id": session_id,
            "expires_in_seconds": SESSION_TTL, "default_method": platform.default_method,
            "methods": platform.login_methods(), "usage": LOGIN_USAGE,
        }
        if started.qr_image:
            payload["qr_data_url"] = started.qr_image
            payload["qr_path"] = qr.save_png(session_id, started.qr_image)
        if retired:
            payload["retired_old_sessions"] = retired
        return payload

    async def poll(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if not session:
            return {"ok": False, "status": "expired",
                    "error": "登录会话不存在或已过期，请重新 login_start"}
        if session["result"] is not None:      # 已经出结果了，重复 poll 直接回放
            return session["result"]
        platform = session["platform"]
        try:
            # 单次 poll 绝不允许拖住 agent 的一轮对话
            state = await asyncio.wait_for(platform.login_poll(session["ticket"]), timeout=12)
        except asyncio.TimeoutError:
            return {"ok": True, "status": "waiting",
                    "message": "本次状态查询超时，二维码仍有效，请稍后再 login_poll"}
        except Exception as exc:
            return await self._finish(session_id, LoginState(False, "failed",
                                                             error=f"{exc.__class__.__name__}: {exc}"))
        return await self._settle(session_id, state)

    async def submit(self, session_id: str, code: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if not session:
            return {"ok": False, "status": "expired",
                    "error": "登录会话不存在或已过期，请重新 login_start"}
        if session["result"] is not None:
            return session["result"]
        code = str(code or "").strip()
        if not code:
            return {"ok": False, "status": "awaiting_code", "error": "验证码为空"}
        try:
            state = await session["platform"].login_submit(session["ticket"], code)
        except Exception as exc:
            return await self._finish(session_id, LoginState(False, "failed",
                                                             error=f"{exc.__class__.__name__}: {exc}"))
        return await self._settle(session_id, state)

    # ------------------------------------------------------------------

    async def _settle(self, session_id: str, state: LoginState) -> dict[str, Any]:
        finished = state.status in FINAL_STATUSES or not state.ok
        if finished:
            return await self._finish(session_id, state)
        session = self._sessions.get(session_id)
        if session:
            session["ticket"] = session["ticket"]      # 未结束，票据留着继续轮询
        return {"ok": state.ok, "status": state.status, "service": state.__dict__.get("service", ""),
                "message": state.message, "error": state.error, "hint": state.hint,
                "session_id": session_id}

    async def _finish(self, session_id: str, state: LoginState) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        payload = {"ok": state.ok, "status": state.status,
                   "service": session["platform"].key.value if session else "",
                   "message": state.message, "error": state.error, "hint": state.hint,
                   "session_id": session_id}
        if session:
            session["status"] = state.status
            session["ticket"] = None        # 票据用完即弃（QQ 里的 live Client 已由平台关掉）
            session["result"] = payload
        qr.delete(session_id)               # 二维码一次性
        return payload

    async def _drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        qr.delete(session_id)

    async def _gc(self) -> None:
        now = time.time()
        for sid, session in list(self._sessions.items()):
            if now - session["created_at"] > SESSION_TTL:
                ticket = session.get("ticket")
                platform = session.get("platform")
                if ticket is not None and platform is not None:
                    try:
                        await platform.login_poll(ticket)   # 让平台自己收尾（QQ 要关 Client）
                    except Exception:
                        pass
                await self._drop(sid)

    def active(self) -> int:
        return len(self._sessions)

login_sessions = LoginSessions()
