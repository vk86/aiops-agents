"""Reliability agent API: /api/slo-agent/*."""
from __future__ import annotations

from fastapi import APIRouter

from incident_agent.chat_router import add_chat_routes, err

from . import slo
from .agent import build

router = APIRouter()


def _sn():
    from incident_agent import tools
    return tools.sn()


@router.get("/api/slo-agent/slos")
def slos():
    try:
        return slo.all_slos(_sn())
    except Exception as e:
        return err(e)


@router.get("/api/slo-agent/scorecard")
def scorecard():
    try:
        return slo.scorecard(_sn())
    except Exception as e:
        return err(e)


@router.get("/api/slo-agent/coverage")
def coverage():
    return slo.coverage()


add_chat_routes(router, "slo-agent", build)
