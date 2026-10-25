"""Shared /chat and /approve routes for the AIOps agents (each agent has its own prompt, tools and memory)."""
from __future__ import annotations

import threading
from typing import Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import approval
from .servicenow import ServiceNowError


class ChatIn(BaseModel):
    message: str
    thread_id: str = "default"


class ApproveIn(BaseModel):
    id: str
    approved: bool
    thread_id: str = "default"
    tell_agent: bool = False


def err(e: Exception) -> JSONResponse:
    code = 502 if isinstance(e, ServiceNowError) else 400 if isinstance(e, ValueError) else 500
    return JSONResponse(status_code=code, content={"error": str(e) if code != 500 else f"{type(e).__name__}: {e}"})


def add_chat_routes(router: APIRouter, prefix: str, build: Callable) -> None:
    """POST /api/{prefix}/chat and /api/{prefix}/approve for an agent built lazily by build()."""
    state = {"agent": None}
    lock = threading.Lock()

    def agent():
        if state["agent"] is None:
            state["agent"] = build()
        return state["agent"]

    @router.post(f"/api/{prefix}/chat")
    def chat(body: ChatIn):
        from .agent import ask
        try:
            with lock:
                return {**ask(agent(), body.message, body.thread_id), "pending": approval.take_new_pending()}
        except Exception as e:
            return err(e)

    @router.post(f"/api/{prefix}/approve")
    def approve(body: ApproveIn):
        with lock:
            res = approval.resolve_pending(body.id, body.approved)
        if body.tell_agent and res.get("action"):
            out = chat(ChatIn(message=f"[Approval result] The user {'approved' if body.approved else 'rejected'} "
                                      f"'{res['action']}'. Result: {res['result']} Confirm in one sentence.",
                              thread_id=body.thread_id))
            if isinstance(out, dict):
                return {**res, **out}
        return res
