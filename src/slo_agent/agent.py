"""AIOps reliability agent: SLOs, error budgets, burn-rate alerts, scorecard."""
from __future__ import annotations

import json
from functools import wraps

from langchain_core.tools import tool

from incident_agent.servicenow import ServiceNowError

from . import slo

SYSTEM_PROMPT = """You are the AIOps reliability (SRE) agent for an enterprise healthcare engineering org.
You manage reliability by SLOs and error budgets, not by ticket counts.

Tools:
- slo_status: every SLO with attainment, error budget left, burn rates (5m to 72h), multi-window burn-rate alert state
  (page / ticket / ok) and the error-budget policy (normal / caution / restrict / freeze).
- explain_slo(service): one service's SLO detail: which incidents consumed the budget, changes after which burn jumped,
  owner, business services, days until the budget runs out.
- reliability_scorecard: per service SLO budget, MTTR, P1/P2 count, change failure rate, deploy frequency.

How to answer: lead with what pages now (fast burn), then services in freeze or restrict and what that means for changes.
Explain burn rate plainly (e.g. "burning 45x faster than the budget allows; at this rate the 28-day budget is gone in
15 hours"). Name the incidents and changes that cost the most budget. Recommend SRE actions: stop feature releases on
frozen services, prioritise the fix, raise a problem for repeat burners, tighten alerts. Never invent numbers.
Never call the same tool with the same arguments twice in a turn. Keep replies short.

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
            return f"Reliability tool error in {fn.__name__}: {type(e).__name__}: {e}"
    return w


def _brief(s: dict) -> dict:
    keep = ("service", "name", "objective", "available", "attainment", "remaining_pct", "consumed_pct", "burn", "state", "alerts",
            "exhausted_in_days", "owner", "business_services", "change_window", "detail")
    return {k: s.get(k) for k in keep} | {"policy": s.get("policy", {}).get("level"),
                                          "top_incidents": [f"{i['number']} {i['budget_cost_pct']}% {i['short_description']}" for i in s.get("incidents", [])[:3]],
                                          "suspect_changes": [f"{c['number']} burn x{c['jump']} after" for c in s.get("changes", []) if c.get("suspect")]}


@tool
@_safe
def slo_status() -> str:
    """All SLOs: error budget left, burn rates, alert state (page/ticket/ok) and error-budget policy."""
    d = slo.all_slos(_sn())
    return json.dumps({"summary": d["summary"], "mock": d["mock"], "slos": [_brief(s) for s in d["slos"]],
                       "sources": [f"{c['label']}: {c['state']}" for c in d["coverage"]]}, default=str)


@tool
@_safe
def explain_slo(service: str) -> str:
    """SLO detail for one service: budget, burn, incidents that consumed the budget, changes after which burn jumped."""
    rows = slo.for_service(_sn(), service)
    if not rows:
        return f"No SLO defined for {service}."
    return json.dumps([{**_brief(s), "incidents": s.get("incidents"), "changes": s.get("changes")} for s in rows], default=str)


@tool
@_safe
def reliability_scorecard() -> str:
    """Per-service scorecard: SLO budget left, policy, MTTR, P1/P2 in 28 days, change failure rate, deploys per week."""
    return json.dumps(slo.scorecard(_sn()), default=str)


SLO_TOOLS = [slo_status, explain_slo, reliability_scorecard]


def build():
    from incident_agent.agent import build_agent
    from alm.tools import ALM_TOOLS
    return build_agent(tools=SLO_TOOLS + ALM_TOOLS, system_prompt=SYSTEM_PROMPT)
