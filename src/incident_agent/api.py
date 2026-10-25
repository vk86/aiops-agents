"""Web UI + HTTP API for testing the incident agent.

  uvicorn incident_agent.api:app --reload --port 8001      then open http://localhost:8001

Writes never run straight from a chat turn: they come back as `pending` approval cards and
run only when the UI calls /api/approve with approved=true.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import approval
from .config import settings
from .servicenow import ServiceNowError
from . import tools as T
from .tools import ALL_TOOLS, WRITE_TOOLS

app = FastAPI(title="ServiceNow Incident Agent")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://localhost:3000"],
                   allow_methods=["*"], allow_headers=["*"])

WEB = Path(__file__).parent / "web" / "aiops.html"


# AIOps: one UI at /aiops with an agent switch (Incident agent | RCA agent).
@app.get("/aiops")
def aiops():
    return FileResponse(WEB)


@app.get("/")
def root():
    return RedirectResponse("/aiops")


@app.get("/rca")
def rca_page():  # old link; the browser keeps any #INC... fragment across the redirect
    return RedirectResponse("/aiops#rca")


# AIOps RCA agent: separate capability (own agent, tools, memory), API at /api/rca-agent/*
from rca_agent.api import router as rca_router  # noqa: E402
from anomaly_agent.api import router as anomaly_router  # noqa: E402
from txn_agent.api import router as txn_router  # noqa: E402
from slo_agent.api import router as slo_router  # noqa: E402
from change_agent.api import router as change_router  # noqa: E402
from alm.api import router as alm_router  # noqa: E402
app.include_router(rca_router)
app.include_router(anomaly_router)
app.include_router(txn_router)
app.include_router(slo_router)
app.include_router(change_router)
app.include_router(alm_router)
approval.set_approver(lambda action, details: None)  # every write becomes an approval card
_lock = threading.Lock()
_agent = None


def agent():
    """Built on first chat so the viewer and tool tester work even without an LLM key."""
    global _agent
    if _agent is None:
        from .agent import build_agent
        from .tools import ALL_TOOLS
        from alm.tools import ALM_TOOLS
        _agent = build_agent(tools=ALL_TOOLS + ALM_TOOLS)
    return _agent


def _ask(message: str, thread_id: str) -> dict:
    from .agent import ask
    out = ask(agent(), message, thread_id)
    return {**out, "pending": approval.take_new_pending()}


@app.exception_handler(ServiceNowError)
def sn_error(_, e: ServiceNowError):
    return JSONResponse(status_code=502, content={"error": str(e)})


@app.exception_handler(Exception)
def any_error(_, e: Exception):
    return JSONResponse(status_code=500, content={"error": f"{type(e).__name__}: {e}"})


class ChatIn(BaseModel):
    message: str
    thread_id: str = "default"


class ApproveIn(BaseModel):
    id: str
    approved: bool
    thread_id: str = "default"
    tell_agent: bool = True


class ToolIn(BaseModel):
    args: dict[str, Any] = {}


@app.get("/api/health")
def health():
    label = {"live": settings.sn_instance, "snapshot": "snapshot export (real records)", "mock": "MOCK DATA (not ServiceNow)"}
    info = {"ok": True, "mode": settings.sn_mode, "instance": label.get(settings.sn_mode, settings.sn_instance),
            "model": settings.llm_model, "approvals": settings.require_approval}
    try:
        me = T.sn().whoami()
        info["user"] = me.get("name") or me.get("user_name") or settings.sn_user
        info["servicenow_ok"] = True
    except Exception as e:
        info["servicenow_ok"] = False
        info["servicenow_error"] = str(e)
    return info


@app.post("/api/chat")
def chat(body: ChatIn):
    with _lock:
        return _ask(body.message, body.thread_id)


@app.post("/api/approve")
def approve(body: ApproveIn):
    with _lock:
        res = approval.resolve_pending(body.id, body.approved)
        if body.tell_agent and res.get("action"):
            verdict = "approved" if body.approved else "rejected"
            note = (f"[Approval result] The user {verdict} '{res['action']}'. Result: {res['result']} "
                    f"Confirm to the user in one short sentence, including any incident number.")
            try:
                return {**res, **_ask(note, body.thread_id)}
            except Exception as e:  # the write already happened; still report it
                return {**res, "reply": None, "agent_error": str(e), "tool_calls": [], "pending": []}
        return res


@app.get("/api/incidents")
def search(text: str = "", priority: str = "", state: str = "", assignment_group: str = "",
           active_only: bool = True, limit: int = 20):
    return T.sn().search_incidents(text=text, priority=priority, state=state, assignment_group=assignment_group,
                                 active_only=active_only, limit=limit)


@app.get("/api/incidents/{number}")
def incident(number: str):
    c = T.sn()
    inc = c.get_incident(number)
    out = {"incident": inc, "activity": c.get_journal(number)}
    try:
        out["changes"] = c.recent_changes(number)
    except ServiceNowError:
        out["changes"] = []
    out["similar"] = c.find_similar(inc.get("short_description", ""), exclude_number=number, limit=5)
    return out


@app.get("/api/shift/briefing")
def shift_briefing(group: str = ""):
    return T.briefing_data(group)


@app.get("/api/shift/handover")
def shift_handover(group: str = "", hours: int = 12):
    return T.handover_data(group, hours)


@app.get("/api/insights/{number}")
def insights(number: str):
    return T.insight_bundle(number)


@app.get("/api/rca/{number}")
def rca(number: str):
    return T.rca_evidence(number)


@app.get("/api/team-health")
def team_health(group: str = "", days: int = 30):
    import json
    return json.loads(T.team_health.invoke({"assignment_group": group, "days": days}))


@app.get("/api/problem-candidates")
def problem_candidates(days: int = 30):
    import json
    out = T.find_problem_candidates.invoke({"days": days})
    return json.loads(out) if out.startswith("[") else {"error": out}


@app.get("/api/tools")
def tools():
    return [{"name": t.name, "description": t.description, "write": t in WRITE_TOOLS, "args": t.args} for t in ALL_TOOLS]


@app.post("/api/tools/{name}")
def run_tool(name: str, body: ToolIn):
    t = next((t for t in ALL_TOOLS if t.name == name), None)
    if t is None:
        raise HTTPException(404, f"Unknown tool {name}")
    args = {k: v for k, v in body.args.items() if v not in (None, "")}
    with _lock:
        output = t.invoke(args)
        return {"output": output, "pending": approval.take_new_pending()}
