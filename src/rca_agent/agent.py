"""AIOps RCA agent: a separate LangChain agent that investigates incidents and writes RCAs to the template.

Its tools wrap the RCA engine. It shares the ServiceNow client, LLM settings and approval gate with the
incident agent, but has its own prompt, memory and UI (the RCA Workbench at /rca).
"""
from __future__ import annotations

import json
from functools import lru_cache

from langchain_core.tools import tool

from incident_agent.approval import gated
from incident_agent.servicenow import ServiceNowError

from .engine import RCAEngine, public, to_markdown

SYSTEM_PROMPT = """You are the AIOps RCA agent, an incident investigator for an enterprise healthcare engineering org.
Your job: find the most likely root cause with evidence, and produce an RCA that follows the AIOps RCA template.

How to work:
- If the user does not give an incident number ("the top P1", "the VPN outage"), call find_incidents ONCE, pick the
  best match (top P1 = highest priority, then most recent) and say which one you picked. If nothing matches, ask.
- Start with start_rca for the incident. Call start_rca at most once per incident per reply (again only after
  add_observation). Never call the same tool with the same arguments twice in a turn. Report the verdict, the top 2-3 hypotheses with confidence and evidence ids,
  and which evidence domains are missing (evidence_coverage).
- Be an investigator: when evidence is thin, ask the engineer for specific findings (e.g. "What did the error-rate
  graph for eligibility-svc show between 02:00 and 02:15?") and record them with add_observation, then re-run start_rca.
- Never invent evidence, times, people, systems or numbers. Cite evidence ids like [CHG0098871], [NOTE-2], [OBS-1].
  If confidence is below 30%, say the cause is undetermined and list what would settle it.
- draft_rca_report writes the full report. publish_rca posts it to ServiceNow and needs the user's approval.
- Keep replies short: verdict first, then evidence, then what to check next.

Engineering follow-up: draft_backlog_item / create_backlog_item turn what you found into a user story, feature, defect or spike in the ALM (SynthoGEN MCP, Jira, Rally, Azure DevOps); creating needs approval and writes the link back to ServiceNow.

Today is {today}."""


@lru_cache(maxsize=1)
def engine() -> RCAEngine:
    return RCAEngine()


def _safe(fn):
    from functools import wraps

    @wraps(fn)
    def w(*a, **k):
        try:
            return fn(*a, **k)
        except ServiceNowError as e:
            return f"ServiceNow error: {e}"
        except Exception as e:
            return f"RCA tool error in {fn.__name__}: {type(e).__name__}: {e}"
    return w


def _brief(report: dict) -> dict:
    return {"incident": report["header"], "verdict": report["verdict"], "sufficient_evidence": report["sufficient_evidence"],
            "hypotheses": [{k: h[k] for k in ("title", "confidence", "category", "reasons", "evidence", "action")} for h in report["hypotheses"][:4]],
            "metrics": report["metrics"], "coverage": [{"domain": c["label"], "state": c["state"], "items": c["evidence_items"]} for c in report["coverage"]],
            "evidence_gaps": report["evidence_gaps"], "observations": report["observations"]}


@tool
@_safe
def start_rca(number: str) -> str:
    """Investigate an incident: collect evidence from every connected source (ServiceNow changes, CMDB dependencies,
    related incidents, work notes, field history, problems, past fixes, and Git/release/MELT when connected), rank
    likely root causes with confidence and evidence, and compute detection/response metrics."""
    return json.dumps(_brief(engine().investigate(number)), default=str)


@tool
@_safe
def find_incidents(text: str = "", priority: str = "<=2", active_only: bool = True, limit: int = 10) -> str:
    """Find incidents to investigate when the user gives no number. priority: "1" = P1 only, "<=2" = P1 and P2,
    "" = any. text matches the short description. Returns the highest priority first, then newest."""
    rows = engine().sn.sn.search_incidents(text=text, priority=priority, active_only=active_only, limit=min(max(limit, 1), 20))
    rows = sorted(rows, key=lambda r: str(r.get("priority") or "9")[:1])  # stable: priority asc, keeps newest-first order
    keep = ("number", "priority", "state", "short_description", "cmdb_ci", "assignment_group", "opened_at")
    return json.dumps({"count": len(rows), "incidents": [{k: r.get(k) for k in keep} for r in rows]}, default=str)


@tool
@_safe
def evidence_coverage(number: str = "") -> str:
    """Which evidence domains (ITSM, metrics, events, logs, traces, code, release, engineer observations) are
    connected, planned or missing, and how many evidence items each contributed to the last RCA."""
    r = engine().latest(number) if number else None
    return json.dumps(r["coverage"] if r else engine().coverage(number), default=str)


@tool
@_safe
def add_observation(number: str, finding: str, time_utc: str = "", source: str = "engineer") -> str:
    """Record a finding from a responder as evidence (e.g. what a dashboard, log search or trace showed), with the
    UTC time it refers to if known ("YYYY-MM-DD HH:MM"). It is cited as OBS-n in the RCA."""
    obs = engine().add_observation(number, finding, source, time_utc)
    return json.dumps({"recorded": obs, "hint": "Run start_rca again to re-rank with this evidence."})


@tool
@_safe
def draft_rca_report(number: str) -> str:
    """Write the full RCA report for an incident following the AIOps RCA template (summary, impact, metrics, timeline,
    root cause with category and confidence, hypotheses, contributing factors, 5 whys, evidence, coverage, CAPA,
    lessons learned, open questions, sign-off). Returns Markdown."""
    return to_markdown(engine().draft(number))


@tool
@_safe
def publish_rca(number: str, problem_number: str = "") -> str:
    """Publish the latest RCA report to ServiceNow: a work note with the summary, the full report attached to the
    incident as Markdown, and (if given) the root cause written to the problem's cause notes. Needs approval."""
    eng = engine()
    r = eng.latest(number)
    if not r or not r.get("narrative"):
        r = eng.draft(number)
    n = r["narrative"]
    rc = n.get("root_cause", {})
    note = (f"[RCA draft v{r['template_version']}] {n.get('summary')}\n"
            f"Root cause ({rc.get('category')}, {float(rc.get('confidence') or 0):.0%}): {rc.get('statement')}\n"
            + "\n".join(f"- {a.get('action')} ({a.get('type')}, {a.get('owner_team') or 'owner tbd'})" for a in (n.get("actions") or [])[:5])
            + f"\nFull report attached: RCA-{r['header']['number']}.md")
    md = to_markdown(r)

    def do():
        sn = eng.sn.sn
        sn.add_note(number, note)
        out = {"work_note": True}
        try:
            out["attachment"] = sn.add_attachment(number, f"RCA-{r['header']['number']}.md", md)
        except ServiceNowError as e:
            out["attachment_error"] = str(e)
        if problem_number:
            sn.update_problem(problem_number, {"cause_notes": rc.get("statement") or n.get("summary")})
            out["problem_updated"] = problem_number
        r["status"] = "published (draft for review)"
        return json.dumps(out)
    return gated(f"Publish RCA for {number}" + (f" and update {problem_number}" if problem_number else ""),
                 {"work_note": note, "attachment": f"RCA-{r['header']['number']}.md ({len(md.splitlines())} lines)",
                  "problem": problem_number}, do)


RCA_TOOLS = [find_incidents, start_rca, evidence_coverage, add_observation, draft_rca_report, publish_rca]


def build():
    from incident_agent.agent import build_agent
    from alm.tools import ALM_TOOLS
    return build_agent(tools=RCA_TOOLS + ALM_TOOLS, system_prompt=SYSTEM_PROMPT)
