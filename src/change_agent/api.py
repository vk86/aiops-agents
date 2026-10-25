"""Change guard API: /api/change-agent/*."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from incident_agent.chat_router import add_chat_routes, err

from . import risk
from .agent import build

router = APIRouter()


def _sn():
    from incident_agent import tools
    return tools.sn()


@router.get("/api/change-agent/upcoming")
def upcoming(days: int = 7):
    try:
        return risk.upcoming(_sn(), days)
    except Exception as e:
        return err(e)


@router.get("/api/change-agent/assess")
def assess(number: str):
    try:
        return risk.assess(_sn(), number)
    except Exception as e:
        return err(e)


@router.get("/api/change-agent/verify")
def verify(number: str):
    try:
        return risk.verify(_sn(), number)
    except Exception as e:
        return err(e)


@router.get("/api/change-agent/dora")
def dora(days: int = 90):
    try:
        return risk.dora(_sn(), days)
    except Exception as e:
        return err(e)


@router.get("/api/change-agent/policy")
def policy():
    return risk.policy_config()


class PostIn(BaseModel):
    number: str


@router.post("/api/change-agent/post")
def post(body: PostIn):
    from incident_agent import approval
    from .agent import post_change_assessment
    out = post_change_assessment.invoke({"number": body.number})
    return {"output": out, "pending": approval.take_new_pending()}


add_chat_routes(router, "change-agent", build)
