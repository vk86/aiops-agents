"""RCA capability API + RCA Workbench page.

Mounted into the incident console (http://localhost:8001/rca), or run on its own:
  uvicorn rca_agent.api:app --port 8002      ->  http://localhost:8002/rca
"""
from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from incident_agent import approval
from incident_agent.servicenow import ServiceNowError

from .agent import engine, publish_rca
from .engine import public, to_markdown
from .template import EVIDENCE_DOMAINS, ROOT_CAUSE_CATEGORIES, SECTIONS, TEMPLATE_VERSION

WEB = Path(__file__).parent / "web" / "rca.html"
router = APIRouter()
_lock = threading.Lock()
_agent = None


def rca_agent():
    global _agent
    if _agent is None:
        from .agent import build
        _agent = build()
    return _agent


class Num(BaseModel):
    number: str
    use_llm: bool = True


class Obs(BaseModel):
    number: str
    text: str
    by: str = "engineer"
    at_utc: str = ""


class Chat(BaseModel):
    message: str
    thread_id: str = "rca"


class Pub(BaseModel):
    number: str
    problem_number: str = ""


class Approve(BaseModel):
    id: str
    approved: bool
    thread_id: str = "rca"
    tell_agent: bool = False


def _err(e: Exception):
    code = 502 if isinstance(e, ServiceNowError) else 500
    return JSONResponse(status_code=code, content={"error": f"{e}" if code == 502 else f"{type(e).__name__}: {e}"})


@router.get("/rca")
def workbench():
    return FileResponse(WEB)


@router.get("/api/rca-agent/template")
def template():
    return {"version": TEMPLATE_VERSION, "sections": [{"key": k, "title": t, "filled_by": b, "guidance": g} for k, t, b, g in SECTIONS],
            "root_cause_categories": ROOT_CAUSE_CATEGORIES,
            "evidence_domains": [{"key": k, "label": l, "what": w} for k, l, w in EVIDENCE_DOMAINS]}


@router.get("/api/rca-agent/coverage")
def coverage(number: str = ""):
    return engine().coverage(number)


@router.post("/api/rca-agent/investigate")
def investigate(body: Num):
    try:
        return public(engine().investigate(body.number))
    except Exception as e:
        return _err(e)


@router.post("/api/rca-agent/observation")
def observation(body: Obs):
    if not body.text.strip():
        return JSONResponse(status_code=400, content={"error": "Write the finding first."})
    return engine().add_observation(body.number, body.text, body.by, body.at_utc)


@router.post("/api/rca-agent/draft")
def draft(body: Num):
    try:
        r = engine().draft(body.number, use_llm=body.use_llm)
        return {**public(r), "markdown": to_markdown(r)}
    except Exception as e:
        return _err(e)


@router.get("/api/rca-agent/report/{number}")
def report(number: str):
    r = engine().latest(number)
    if not r:
        return JSONResponse(status_code=404, content={"error": f"No RCA yet for {number}. Investigate first."})
    return {**public(r), "markdown": to_markdown(r)}


@router.post("/api/rca-agent/publish")
def publish(body: Pub):
    with _lock:
        out = publish_rca.invoke({"number": body.number, "problem_number": body.problem_number})
        return {"output": out, "pending": approval.take_new_pending()}


@router.post("/api/rca-agent/chat")
def chat(body: Chat):
    from incident_agent.agent import ask
    try:
        with _lock:
            out = ask(rca_agent(), body.message, body.thread_id)
            return {**out, "pending": approval.take_new_pending()}
    except Exception as e:
        return _err(e)


@router.post("/api/rca-agent/approve")
def approve(body: Approve):
    with _lock:
        res = approval.resolve_pending(body.id, body.approved)
    if body.tell_agent and res.get("action"):
        try:
            return {**res, **chat(Chat(message=f"[Approval result] The user {'approved' if body.approved else 'rejected'} "
                                               f"'{res['action']}'. Result: {res['result']} Confirm in one sentence.",
                                       thread_id=body.thread_id))}
        except Exception:
            pass
    return res


# ---- standalone app (optional: run the RCA capability on its own port)
app = FastAPI(title="AIOps RCA agent")
app.include_router(router)
approval.set_approver(lambda action, details: None)


@app.get("/")
def root():
    return RedirectResponse("/rca")


@app.get("/api/incidents")
def incidents(priority: str = "", active_only: bool = False, limit: int = 15, text: str = ""):
    from incident_agent.tools import sn
    return sn().search_incidents(text=text, priority=priority, active_only=active_only, limit=limit)


@app.get("/api/health")
def health():
    from incident_agent.config import settings
    return {"ok": True, "mode": settings.sn_mode, "instance": settings.sn_instance, "model": settings.llm_model}
