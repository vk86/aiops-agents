"""ALM bridge API: /api/alm/*."""
from __future__ import annotations

import json

from fastapi import APIRouter
from pydantic import BaseModel

from incident_agent.chat_router import err

from . import bridge

router = APIRouter()


class DraftIn(BaseModel):
    kind: str
    ref: str
    index: int = 0
    use_llm: bool = True


class CreateIn(BaseModel):
    kind: str
    ref: str
    index: int = 0
    target: str
    project: str = ""
    item: dict
    writeback: bool = True


@router.get("/api/alm/targets")
def targets():
    return {"targets": bridge.targets(), "coverage": bridge.coverage()}


@router.post("/api/alm/draft")
def draft(body: DraftIn):
    try:
        return bridge.draft(body.kind, body.ref, body.index, body.use_llm)
    except Exception as e:
        return err(e)


@router.post("/api/alm/create")
def create(body: CreateIn):
    """Parks the creation behind the approval gate; the UI shows the approval card."""
    from incident_agent import approval
    from incident_agent.approval import gated
    try:
        ctx = bridge.context(body.kind, body.ref, body.index)
    except Exception as e:
        return err(e)
    it = body.item
    out = gated(f"Create {it.get('type')} in {body.target}: {str(it.get('title', ''))[:70]}",
                {"target": body.target, "project": body.project, "type": it.get("type"), "title": it.get("title"),
                 "acceptance criteria": "\n".join(it.get("acceptance_criteria", [])), "links": ", ".join(ctx["links"]),
                 "write back to": (ctx.get("writeback") or {}).get("number") if body.writeback else "no"},
                lambda: json.dumps(bridge.create(ctx, it, body.target, body.project, body.writeback), default=str))
    return {"output": out, "pending": approval.take_new_pending()}


@router.get("/api/alm/items")
def items():
    return bridge.items()
