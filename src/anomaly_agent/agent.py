"""AIOps anomaly agent: finds unusual incident volume and alert noise in ServiceNow and explains it."""
from __future__ import annotations

import json
from functools import wraps

from langchain_core.tools import tool

from incident_agent.approval import gated
from incident_agent.servicenow import ServiceNowError

from . import detect

SYSTEM_PROMPT = """You are the AIOps anomaly agent for an enterprise healthcare engineering org.

Detection runs on observability signals, scored by the team's predictive model:
- detect_service_anomalies (default last 60 min) collects signals from Dynatrace, AppDynamics and Splunk, builds
  features per service, scores them with the predictive model, and adds ServiceNow context: CMDB owner, business
  services affected, open incidents, open change windows. Each service gets a decision:
  raise (send an event to ServiceNow), attach (an incident is already open: add evidence to it), suppressed (an
  approved change window is open, so it is expected), watch (below threshold).
- explain_service gives one service's signals, model explanation and context.
- Actions need the user's approval: send_anomaly_event (ServiceNow Event Management, preferred so its de-duplication
  and alert rules apply), attach_anomaly_to_incident, raise_incident_for_anomaly (only if Event Management is not
  available), create_problem_for_anomaly (recurring pattern).
- scan_anomalies is the ServiceNow incident-volume backstop (catches user-reported issues monitors miss);
  alert_noise analyses Event Management alerts; evaluate_detections scores past detections against incidents.

How to report: lead with what to act on (raise / attach), then suppressed and watch. For each: the service, the
business services it affects, the strongest signals with numbers, the model score and its top features, the
decision and why. Say which sources are mock or not connected. If the model is not connected, say anomalies are
ranked by how many sources agree.
Never call the same tool with the same arguments twice in a turn. Never invent signals, scores, incidents or changes.
Keep replies short.

Engineering follow-up: draft_backlog_item / create_backlog_item turn what you found into a user story, feature, defect or spike in the ALM (SynthoGEN MCP, Jira, Rally, Azure DevOps); creating needs approval and writes the link back to ServiceNow.

Today is {today}."""


def _sn():
    from incident_agent import tools
    return tools.sn()


def _safe(fn):
    @wraps(fn)
    def w(*a, **k):
        try:
            return fn(*a, **k)
        except ServiceNowError as e:
            return f"ServiceNow error: {e}"
        except Exception as e:
            return f"Anomaly tool error in {fn.__name__}: {type(e).__name__}: {e}"
    return w


def _brief(r: dict) -> dict:
    return {**{k: r[k] for k in ("dimension", "window_hours", "baseline_days", "window_start", "window_end", "anchored",
                                  "incidents_scanned", "values_checked", "overall")},
            "anomalies": [{k: a.get(k) for k in ("key", "current", "expected", "z", "new", "severity", "reason", "common_terms",
                                                  "changes", "related_spiking")}
                          | {"incidents": [f"{i['number']} P{str(i['priority'] or '')[:1]} {i['short_description']}"
                                           for i in a["incidents"][:6]]} for a in r["anomalies"]]}


def _sig():
    from . import signals
    return signals


def _compact(r: dict) -> dict:
    m = r.get("model") or {}
    return {"service": r["service"], "decision": r["decision"], "why": r["decision_reason"], "confidence": r["confidence"],
            "model_score": m.get("score"), "model_explanation": m.get("explanation"),
            "top_features": [f"{f.get('name')}={f.get('value')}" for f in (m.get("top_features") or [])],
            "headline": r["headline"], "sources": r["sources"],
            "signals": [f"{s['source']}: {s['name']}" + (f" {s['value']:g}{s['unit']} vs {s['baseline']:g}{s['unit']}"
                                                          if s["value"] is not None and s["baseline"] is not None else "")
                        for s in r["signals"] if s["anomalous"]][:6],
            "ci": r["context"]["ci"], "owner": r["context"]["owner"], "business_services": r["context"]["business_services"],
            "open_incidents": [f"{i['number']} P{str(i['priority'] or '')[:1]} {i['short_description']}" for i in r["context"]["open_incidents"]][:4],
            "change_window": r["context"]["change_window"], "recent_changes": r["context"]["changes"][:3]}


@tool
@_safe
def detect_service_anomalies(window_minutes: int = 60) -> str:
    """Detect service anomalies from observability signals (Dynatrace, AppDynamics, Splunk) scored by the predictive
    model, with ServiceNow context (owner, business services, open incidents, change windows) and a decision per
    service: raise | attach | suppressed | watch."""
    d = _sig().detect(_sn(), window_minutes)
    return json.dumps({"window": f"{d['window_start']} → {d['window_end']} UTC", "model": {k: d["model"].get(k) for k in ("state", "version", "detail")},
                       "sources": [f"{c['label']}: {c['state']}" for c in d["coverage"]], "summary": d["summary"], "errors": d["errors"],
                       "services": [_compact(r) for r in d["services"]]}, default=str)


@tool
@_safe
def explain_service(service: str, window_minutes: int = 60) -> str:
    """All signals, model features and ServiceNow context for one service from the latest detection."""
    r = _sig().service(_sn(), service, window_minutes)
    return json.dumps({**_compact(r), "features": r["features"], "signals": r["signals"]}, default=str)


@tool
@_safe
def send_anomaly_event(service: str, window_minutes: int = 60) -> str:
    """Send the service's anomaly to ServiceNow Event Management (em_event) with its signals, model score and
    business services. Needs the user's approval."""
    S = _sig()
    r = S.service(_sn(), service, window_minutes)
    f = S.event_fields(r)

    def do():
        try:
            return json.dumps({"event_sent": True, **_sn().create_event(f)})
        except ServiceNowError as e:
            return json.dumps({"event_sent": False, "error": str(e),
                               "hint": "Event Management may not be installed; use raise_incident_for_anomaly instead."})
    return gated(f"Send anomaly event for {r['service']}", {"node": f["node"], "severity": f["severity"], "message_key": f["message_key"],
                                                            "description": f["description"]}, do)


@tool
@_safe
def attach_anomaly_to_incident(service: str, number: str = "", window_minutes: int = 60) -> str:
    """Add the anomaly evidence (signals, model score, recent changes) as a work note on the incident already open
    on the service's CI (or the given number). Needs the user's approval."""
    S = _sig()
    r = S.service(_sn(), service, window_minutes)
    num = (number or (min(r["context"]["open_incidents"], key=lambda i: str(i["priority"]))["number"] if r["context"]["open_incidents"] else "")).upper()
    if not num:
        return "No open incident on this CI; use send_anomaly_event instead."
    note = S.evidence_note(r)
    return gated(f"Work note on {num} (anomaly evidence)", {"incident": num, "note": note},
                 lambda: (_sn().add_note(num, note), json.dumps({"added": "work note", "incident": num}))[1])


@tool
@_safe
def raise_incident_for_anomaly(service: str, window_minutes: int = 60) -> str:
    """Create an incident for the anomaly (use when Event Management is not available). Needs the user's approval."""
    S = _sig()
    r = S.service(_sn(), service, window_minutes)
    worst = S.event_fields(r)["severity"]
    impact = {"1": "1", "2": "2"}.get(worst, "3")
    fields = {"short_description": f"Anomaly on {r['service']}: {r['headline']}"[:160], "description": S.evidence_note(r),
              "cmdb_ci": r["context"]["ci"] or "", "impact": impact, "urgency": impact,
              "assignment_group": r["context"]["owner"] or ""}
    fields = {k: v for k, v in fields.items() if v}
    return gated(f"Create incident for {r['service']} anomaly", fields, lambda: json.dumps(_sn().create_incident(fields), default=str))


@tool
@_safe
def evaluate_detections(days: int = 30) -> str:
    """How well past detections matched P1/P2 incidents on the same CI: precision, recall on watched CIs, median lead
    time (minutes before the incident was opened)."""
    return json.dumps(_sig().evaluate(_sn(), days), default=str)


@tool
@_safe
def scan_anomalies(window_hours: int = 24, baseline_days: int = 14, dimension: str = "cmdb_ci") -> str:
    """ServiceNow backstop: unusual incident volume (user-reported impact monitors may miss). Compares incidents
    opened in the last window_hours with the same-size windows over baseline_days. dimension: cmdb_ci |
    assignment_group | category."""
    return json.dumps(_brief(detect.scan(_sn(), window_hours, baseline_days, dimension)), default=str)


@tool
@_safe
def alert_noise(hours: int = 24) -> str:
    """Analyse Event Management alerts (em_alert) for the last N hours: de-duplication rate, bursting groups and
    steady noise (flapping or always-on alerts worth tuning)."""
    return json.dumps(detect.alert_noise(_sn(), hours), default=str)


@tool
@_safe
def create_problem_for_anomaly(short_description: str, incident_numbers: list[str], description: str = "") -> str:
    """Raise a problem record for an anomaly and link its incidents. Needs the user's approval."""
    nums = [n.strip().upper() for n in incident_numbers if n.strip()][:20]
    if not nums:
        return "Give at least one incident number."
    fields = {"short_description": short_description, "description": description or f"Raised by the AIOps anomaly agent for {', '.join(nums)}."}
    return gated(f"Create problem: {short_description}", {"short_description": short_description, "incidents": ", ".join(nums),
                                                          "description": fields["description"]},
                 lambda: json.dumps(_sn().create_problem(fields, nums), default=str))


ANOMALY_TOOLS = [detect_service_anomalies, explain_service, send_anomaly_event, attach_anomaly_to_incident,
                 raise_incident_for_anomaly, evaluate_detections, scan_anomalies, alert_noise, create_problem_for_anomaly]


def build():
    from incident_agent.agent import build_agent
    from alm.tools import ALM_TOOLS
    return build_agent(tools=ANOMALY_TOOLS + ALM_TOOLS, system_prompt=SYSTEM_PROMPT)
