"""Anomaly agent API: /api/anomaly-agent/*."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from incident_agent.chat_router import add_chat_routes, err

from . import detect
from .agent import build

router = APIRouter()


def _sn():
    from incident_agent import tools
    return tools.sn()


@router.get("/api/anomaly-agent/scan")
def scan(window_hours: int = 24, baseline_days: int = 14, dimension: str = "cmdb_ci", min_count: int = 3, z_min: float = 3.0):
    try:
        return detect.scan(_sn(), window_hours, baseline_days, dimension, min_count=min_count, z_min=z_min)
    except Exception as e:
        return err(e)


@router.get("/api/anomaly-agent/alerts")
def alerts(hours: int = 24):
    try:
        return detect.alert_noise(_sn(), hours)
    except Exception as e:
        return err(e)


@router.get("/api/anomaly-agent/coverage")
def coverage():
    from . import signals
    noise = detect.alert_noise(_sn(), 1)
    return signals.coverage() + [{"name": "em_alert", "label": "ServiceNow Event Management alerts", "domains": "alert noise",
                                  "state": "connected" if noise.get("available") else "not_connected",
                                  "detail": "em_alert readable" if noise.get("available") else noise.get("reason", "")}]


@router.get("/api/anomaly-agent/detect")
def detect_services(window_minutes: int = 60, refresh: bool = False):
    from . import signals
    try:
        return signals.detect(_sn(), window_minutes, use_cache=not refresh)
    except Exception as e:
        return err(e)


class Act(BaseModel):
    service: str
    window_minutes: int = 60
    number: str = ""


@router.post("/api/anomaly-agent/action/{kind}")
def action(kind: str, body: Act):
    from incident_agent import approval
    from . import agent as A
    tool = {"event": A.send_anomaly_event, "attach": A.attach_anomaly_to_incident, "incident": A.raise_incident_for_anomaly}.get(kind)
    if not tool:
        return err(ValueError("kind must be event, attach or incident"))
    args = {"service": body.service, "window_minutes": body.window_minutes} | ({"number": body.number} if kind == "attach" else {})
    out = tool.invoke(args)
    return {"output": out, "pending": approval.take_new_pending()}


@router.get("/api/anomaly-agent/evaluate")
def evaluate(days: int = 30):
    from . import signals
    try:
        return signals.evaluate(_sn(), days)
    except Exception as e:
        return err(e)


@router.get("/api/anomaly-agent/labels.csv")
def labels_csv(days: int = 90):
    from fastapi.responses import PlainTextResponse
    from . import signals
    try:
        rows = signals.labels(_sn(), days)
    except Exception as e:
        return err(e)
    return PlainTextResponse(signals.labels_csv(rows), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="anomaly_labels_{days}d.csv"'})


@router.get("/api/anomaly-agent/model-contract")
def model_contract():
    from .sources.model import FEATURES
    return {"features": [{"name": n, "description": d} for n, d in FEATURES], "doc": "docs/ANOMALY_DETECTION.md"}


add_chat_routes(router, "anomaly-agent", build)
