"""AIOps change guard agent: risk before deploy, verification after, DORA metrics."""
from __future__ import annotations

import json
from functools import wraps

from langchain_core.tools import tool

from incident_agent.approval import gated
from incident_agent.servicenow import ServiceNowError

from . import risk

SYSTEM_PROMPT = """You are the AIOps change guard for an enterprise healthcare engineering org. You stop risky changes
before they ship and check changes after they ship, using data rather than CAB opinion.

Tools:
- upcoming_change_risk: ServiceNow changes in the next days with a risk score and GO / CANARY / HOLD.
- assess_change(number): full assessment: blast radius (CMDB business services), error budget policy, change failure
  rate and similar failed changes, open P1/P2 and anomalies on the CI, collisions, freeze windows and peak hours,
  hygiene (backout/test plan); hard holds; guardrails (canary steps, what to watch, rollback triggers).
- verify_change(number): after a change, did the SLO burn rise or incidents follow on the CI or its dependents.
- dora_metrics: deploy frequency, change failure rate and MTTR per service.
- post_change_assessment(number): writes the assessment as a work note on the change (needs approval).

How to answer: recommendation first (HOLD / CANARY / GO) and the deciding reasons with evidence (incident, change,
SLO numbers). Reliability fixes (rollback, revert, restore, renew certificate) are allowed during freezes as canaries
with SRE approval: say so. Give the guardrails. Never invent numbers or change records. Never call the same tool with
the same arguments twice in a turn. Keep replies short.

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
            return f"Change guard error in {fn.__name__}: {type(e).__name__}: {e}"
    return w


def _brief(a: dict) -> dict:
    return {k: a.get(k) for k in ("number", "short_description", "ci", "start", "type", "reliability_fix", "score", "recommendation",
                                  "why", "hard_holds", "business_services", "guardrails")} | {
        "factors": [f"{f['label']} +{f['points']}{' (HARD HOLD)' if f['hard_hold'] else ''}: {f['detail']}" for f in a["factors"]],
        "slo": a["slo"] and {k: a["slo"].get(k) for k in ("name", "remaining_pct", "state")}}


@tool
@_safe
def upcoming_change_risk(days: int = 7) -> str:
    """Upcoming ServiceNow changes with a risk score and GO / CANARY / HOLD recommendation."""
    d = risk.upcoming(_sn(), days)
    return json.dumps({"note": d.get("note"), "changes": d["changes"]}, default=str)


@tool
@_safe
def assess_change(number: str) -> str:
    """Full risk assessment for one change: factors with points, hard holds, recommendation and guardrails."""
    return json.dumps(_brief(risk.assess(_sn(), number)), default=str)


@tool
@_safe
def verify_change(number: str) -> str:
    """After a change ended: SLO burn before vs after and incidents within 24h on the CI or its dependents."""
    return json.dumps(risk.verify(_sn(), number), default=str)


@tool
@_safe
def dora_metrics(days: int = 90) -> str:
    """DORA-style metrics per service: deploys per week, change failure rate, MTTR."""
    return json.dumps(risk.dora(_sn(), days), default=str)


def note_text(a: dict) -> str:
    g = a["guardrails"]
    return "\n".join([f"[AIOps change guard] {a['number']}: {a['recommendation']} (risk {a['score']}/100). {a['why']}"]
                     + [f"- {f['label']} +{f['points']}: {f['detail']}" for f in a["factors"] if f["points"]]
                     + ["Plan: " + " → ".join(g["plan"]), "Rollback if: " + "; ".join(g["rollback_triggers"])])


@tool
@_safe
def post_change_assessment(number: str) -> str:
    """Write the change assessment as a work note on the ServiceNow change. Needs the user's approval."""
    a = risk.assess(_sn(), number)
    note = note_text(a)
    return gated(f"Work note on {a['number']} (change assessment: {a['recommendation']})", {"change": a["number"], "note": note},
                 lambda: (_sn().add_change_note(a["number"], note), json.dumps({"added": "work note", "change": a["number"]}))[1])


CHANGE_TOOLS = [upcoming_change_risk, assess_change, verify_change, dora_metrics, post_change_assessment]


def build():
    from incident_agent.agent import build_agent
    from alm.tools import ALM_TOOLS
    return build_agent(tools=CHANGE_TOOLS + ALM_TOOLS, system_prompt=SYSTEM_PROMPT)
