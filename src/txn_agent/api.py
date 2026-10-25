"""Transaction agent API: /api/txn-agent/*."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from incident_agent.chat_router import add_chat_routes, err

from . import path
from .agent import build

router = APIRouter()


def _sn():
    from incident_agent import tools
    return tools.sn()


@router.get("/api/txn-agent/transactions")
def transactions():
    try:
        return {"transactions": path.transactions(_sn()), "impacted": path.impacted(_sn())}
    except Exception as e:
        return err(e)


@router.get("/api/txn-agent/trace")
def trace(name: str):
    try:
        return path.trace_path(_sn(), name)
    except Exception as e:
        return err(e)


@router.get("/api/txn-agent/find")
def find(txn_id: str):
    try:
        return path.find_transaction(_sn(), txn_id)
    except Exception as e:
        return err(e)


@router.get("/api/txn-agent/coverage")
def coverage():
    from . import trace
    try:
        base = [c for c in path.coverage(_sn()) if c["domain"] in ("cmdb", "itsm")]
        return base + [{"domain": t["name"], "label": t["label"], "state": t["state"], "detail": t["detail"]} for t in trace.coverage()]
    except Exception as e:
        return err(e)


@router.get("/api/txn-agent/track")
def track(txn_id: str, reported_at: str = ""):
    from . import trace
    try:
        return trace.track(_sn(), txn_id, reported_at)
    except Exception as e:
        return err(e)


@router.get("/api/txn-agent/logs")
def logs(service: str, around: str = "", minutes: int = 2, txn_id: str = ""):
    from . import trace
    try:
        return trace.service_logs(service, around, minutes, txn_id)
    except Exception as e:
        return err(e)


class AttachIn(BaseModel):
    txn_id: str
    number: str
    reported_at: str = ""


@router.post("/api/txn-agent/attach")
def attach(body: AttachIn):
    from incident_agent import approval
    from .agent import attach_trace_to_incident
    out = attach_trace_to_incident.invoke({"txn_id": body.txn_id, "number": body.number, "reported_at": body.reported_at})
    return {"output": out, "pending": approval.take_new_pending()}


add_chat_routes(router, "txn-agent", build)
