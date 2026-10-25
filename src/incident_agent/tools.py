"""LangChain tools for incident management on ServiceNow.

Read tools: get_incident, search_incidents, get_incident_activity, summarize_incident,
            find_similar_incidents, find_recent_changes, list_assignment_groups
Write tools (ask for approval first): create_incident, update_incident, add_work_note, resolve_incident

Tools return compact JSON strings; errors come back as text the agent can explain,
instead of exceptions that end the run.
"""
from __future__ import annotations

import json
from functools import lru_cache, wraps
from typing import Literal, Optional

from langchain_core.tools import tool

from . import insights as I
from .approval import gated
from .llm import get_model, text_of
from .servicenow import ServiceNowError, get_client

Level = Literal["1", "2", "3"]  # 1 = High, 2 = Medium, 3 = Low
State = Literal["New", "In Progress", "On Hold", "Resolved", "Closed", "Canceled"]
CloseCode = Literal["Solution provided", "Workaround provided", "Resolved by change", "Resolved by problem",
                    "Resolved by caller", "Known error", "Duplicate", "User error", "No resolution provided"]


@lru_cache(maxsize=1)
def sn():
    return get_client()


def _out(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _safe(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ServiceNowError as e:
            return f"ServiceNow error: {e}"
        except Exception as e:  # any bug or odd data comes back as text; never break the conversation
            return f"Tool error in {fn.__name__}: {type(e).__name__}: {e}"
    return wrapper


def _brief(r: dict) -> dict:
    keep = ("number", "short_description", "state", "priority", "assignment_group", "assigned_to", "cmdb_ci",
            "opened_at", "close_code", "close_notes")
    return {k: r.get(k) for k in keep if r.get(k) not in (None, "")}


# ============================================================== read tools
@tool
@_safe
def get_incident(number: str) -> str:
    """Get full details of one incident by its number, e.g. INCxxxxxxx."""
    r = sn().get_incident(number)
    return _out({k: v for k, v in r.items() if v not in (None, "") and k != "sys_id"})


@tool
@_safe
def search_incidents(text: str = "", state: Optional[State] = None, priority: str = "",
                     assignment_group: str = "", active_only: bool = True, limit: int = 10) -> str:
    """Search incidents. All filters are optional.
    text: words in the short description or description.
    priority: "1".."5" for an exact priority, or "<=2" for P1 and P2.
    assignment_group: part of the group name.
    active_only: false to include resolved and closed incidents."""
    rows = sn().search_incidents(text=text, state=state or "", priority=priority,
                                 assignment_group=assignment_group, active_only=active_only, limit=limit)
    return _out({"count": len(rows), "incidents": rows})


@tool
@_safe
def get_incident_activity(number: str) -> str:
    """Get the work notes and customer comments on an incident, oldest first."""
    return _out(sn().get_journal(number))


SUMMARY_PROMPT = """Summarize this ServiceNow incident for an on-call engineer and their manager.
Use exactly these headings, short bullet points, plain language, and only facts given below:

**Status** (number, priority, state, owner, how long open)
**What is happening** (symptom and business impact)
**What has been done** (from the activity log, in order)
**Likely cause / leads** (only if the facts support it; otherwise say "Not yet identified")
**Next steps** (2-3 concrete actions)

INCIDENT:
{incident}

ACTIVITY (oldest first):
{activity}

RECENT CHANGES ON THE SAME CI:
{changes}
"""


@tool
@_safe
def summarize_incident(number: str) -> str:
    """Write a structured summary of an incident (status, impact, actions so far, leads, next steps)
    from its record, activity log and recent changes on the same configuration item."""
    client = sn()
    inc = client.get_incident(number)
    activity = client.get_journal(number)
    try:
        changes = client.recent_changes(number)
    except ServiceNowError:
        changes = []
    prompt = SUMMARY_PROMPT.format(incident=_out(_brief(inc) | {"description": inc.get("description")}),
                                   activity=_out(activity) if activity else "None",
                                   changes=_out(changes) if changes else "None")
    return text_of(get_model().invoke(prompt))


@tool
@_safe
def find_similar_incidents(number: str = "", text: str = "", limit: int = 5) -> str:
    """Find past incidents similar to a given incident (by number) or to a description (text).
    Useful for spotting repeats and reusing known resolutions."""
    if number and not text:
        text = sn().get_incident(number).get("short_description", "")
    if not text:
        return "Give an incident number or some text to match."
    return _out(sn().find_similar(text, exclude_number=number, limit=limit))


@tool
@_safe
def find_recent_changes(number: str, hours: int = 48) -> str:
    """List change requests on the incident's configuration item that ended in the `hours`
    before the incident opened. A recent change is often the cause."""
    rows = sn().recent_changes(number, hours=hours)
    return _out(rows) if rows else "No change requests on this CI in that window (or the incident has no CI)."


@tool
@_safe
def list_assignment_groups(name_contains: str = "") -> str:
    """List active assignment groups, optionally filtered by part of the name.
    Use this to find the exact group name before assigning."""
    return _out([g["name"] for g in sn().find_groups(name_contains, limit=25)])


# ============================================================== write tools (approval required)
@tool
@_safe
def create_incident(short_description: str, description: str = "", impact: Level = "3", urgency: Level = "3",
                    assignment_group: str = "", caller: str = "", category: str = "",
                    configuration_item: str = "") -> str:
    """Create a new incident. impact and urgency: "1" High, "2" Medium, "3" Low (priority is derived).
    assignment_group, caller and configuration_item are names as shown in ServiceNow.
    Always confirm the details with the user before calling this."""
    fields = {"short_description": short_description, "description": description, "impact": impact,
              "urgency": urgency, "assignment_group": assignment_group, "caller_id": caller,
              "category": category, "cmdb_ci": configuration_item}
    def do():
        r = sn().create_incident(fields)
        return _out({"created": r.get("number"), **_brief(r)})
    return gated("Create incident", fields, do)


@tool
@_safe
def update_incident(number: str, state: Optional[State] = None, assignment_group: str = "", assigned_to: str = "",
                    impact: Optional[Level] = None, urgency: Optional[Level] = None,
                    short_description: str = "") -> str:
    """Update fields on an incident: state, assignment group, assignee, impact, urgency or short description.
    To resolve, use resolve_incident instead (it needs a resolution code and notes)."""
    if state in ("Resolved", "Closed"):
        return "Use resolve_incident to resolve; it needs a resolution code and notes."
    fields = {"state": state, "assignment_group": assignment_group, "assigned_to": assigned_to,
              "impact": impact, "urgency": urgency, "short_description": short_description}
    fields = {k: v for k, v in fields.items() if v}
    if not fields:
        return "Nothing to update. Say which field to change."
    return gated(f"Update {number}", fields,
                 lambda: _out({"updated": number, **_brief(sn().update_incident(number, fields))}))


@tool
@_safe
def add_work_note(number: str, note: str, customer_visible: bool = False) -> str:
    """Add a note to an incident. Internal work note by default;
    customer_visible=true posts it as an additional comment the caller can see."""
    kind = "Customer comment" if customer_visible else "Work note"
    def do():
        sn().add_note(number, note, customer_visible)
        return _out({"added": kind.lower(), "incident": number})
    return gated(f"{kind} on {number}", {"text": note}, do)


@tool
@_safe
def resolve_incident(number: str, close_code: CloseCode, close_notes: str) -> str:
    """Resolve an incident with a resolution code and resolution notes describing the fix."""
    return gated(f"Resolve {number}", {"close_code": close_code, "close_notes": close_notes},
                 lambda: _out({"resolved": number, **_brief(sn().resolve_incident(number, close_code, close_notes))}))


# ============================================================== shift & insight tools (read)
def _clamp(n, lo, hi):
    return max(lo, min(hi, int(n or 0)))


def briefing_data(group: str = "") -> dict:
    c = sn()
    return I.briefing(c.open_incidents(group), c.sla_records(group), I.current_shift(), group=group)


def handover_data(group: str = "", hours: int = 12) -> dict:
    c = sn()
    hours = _clamp(hours, 1, 72)
    upd = c.incidents_window("updated_open", hours, group)
    journal = {}
    for r in sorted(upd, key=I.prio)[:10]:
        try:
            journal[r["number"]] = c.get_journal(r["number"], limit=50)
        except ServiceNowError:
            pass
    return I.handover(c.incidents_window("opened", hours, group), c.incidents_window("resolved", hours, group),
                      upd, journal, hours)


def insight_bundle(number: str) -> dict:
    """Everything the incident view needs, computed once."""
    c = sn()
    t = c.incident_row(number)
    hist = c.history_for(t)
    open_rows = c.open_incidents()
    return {"experts": I.experts(t, hist),
            "assignment": I.suggest_assignment(t, hist, c.ci_support_group(t.get("cmdb_ci_raw") or t.get("cmdb_ci")), open_rows),
            "duplicates": I.duplicates(t, open_rows) if t.get("state") not in I.CLOSED else None}


@tool
@_safe
def shift_briefing(assignment_group: str = "") -> str:
    """Start-of-shift briefing: which shift it is, top 10 open incidents by priority, what is new since the shift
    started, SLAs at risk or breached, unassigned and aged incidents. Use when someone starts a shift, logs in,
    says good morning, or asks 'what's going on' / 'what should I work on'. Optional assignment_group filter."""
    return _out(briefing_data(assignment_group))


@tool
@_safe
def shift_handover(assignment_group: str = "", hours: int = 12) -> str:
    """Handover for the last N hours (default 12): incidents opened, resolved (with who fixed them and how),
    and still-open incidents carried over with their latest work note."""
    return _out(handover_data(assignment_group, hours))


@tool
@_safe
def who_solved_this_before(number: str = "", text: str = "") -> str:
    """Find who historically resolved incidents like this one: ranks people by similar incidents they resolved,
    their group, median time to resolve and the fix notes they used. Give an incident number or a description."""
    c = sn()
    target = c.incident_row(number) if number else {"number": "", "short_description": text}
    return _out(I.experts(target, c.history_for(target)))


@tool
@_safe
def find_duplicate_incidents(number: str, hours: int = 48) -> str:
    """Find open incidents that look like duplicates of this one (similar wording or same CI, opened within
    `hours`), and suggest which one should be the parent."""
    c = sn()
    return _out(I.duplicates(c.incident_row(number), c.open_incidents(), _clamp(hours, 1, 168)))


@tool
@_safe
def suggest_assignment(number: str) -> str:
    """Suggest the right assignment group and assignee for an incident, from who handled similar incidents,
    the CI's support group, and current workload. Says whether the current assignment already looks right."""
    c = sn()
    t = c.incident_row(number)
    return _out(I.suggest_assignment(t, c.history_for(t), c.ci_support_group(t.get("cmdb_ci_raw") or t.get("cmdb_ci")),
                                     c.open_incidents()))


@tool
@_safe
def sla_at_risk(assignment_group: str = "") -> str:
    """Open incidents whose SLA is breached or more than 75% elapsed, most urgent first."""
    c = sn()
    rows = I.sla_view(c.open_incidents(assignment_group), c.sla_records(assignment_group), I.datetime.now(I.UTC))
    return _out([r for r in rows if r["percent_elapsed"] >= 75][:15])


@tool
@_safe
def team_health(assignment_group: str = "", days: int = 30) -> str:
    """Manager view: open incidents by group and priority, aged incidents, median time to resolve and
    reassignments over the last N days, and the CIs with the most incidents."""
    c = sn()
    days = _clamp(days, 1, 180)
    return _out(I.team_health(c.open_incidents(assignment_group), c.resolved_days(days, assignment_group), days))


@tool
@_safe
def find_problem_candidates(days: int = 30, min_incidents: int = 3) -> str:
    """Find recurring incidents worth a problem record: clusters of incidents on the same CI or with the same
    keywords in the last N days that are not yet linked to a problem."""
    days = _clamp(days, 1, 365)
    return _out(I.problem_candidates(sn().opened_days(days), days, _clamp(min_incidents, 2, 20))[:8])


# ============================================================== root cause analysis
def rca_evidence(number: str) -> dict:
    """Evidence + ranked hypotheses from the AIOps RCA agent (separate capability, shared engine)."""
    from rca_agent.agent import engine
    from rca_agent.engine import public
    return public(engine().investigate(number))


@tool
@_safe
def root_cause_analysis(number: str) -> str:
    """Hand off to the AIOps RCA agent: investigates the incident (changes on its CI and connected CIs, CMDB
    dependencies, related incidents, work notes, field history, known problems, past fixes, plus Git/release/MELT
    when connected), ranks likely causes with evidence, and returns the RCA summary. The full template report and
    investigation live in the RCA Workbench (/rca). Use for 'why did this happen', 'RCA', 'root cause', 'postmortem'."""
    from rca_agent.agent import engine
    r = engine().draft(number)
    n = r["narrative"]
    rc = n.get("root_cause", {})
    lines = [f"**RCA (AIOps RCA agent)** for {r['header']['number']}: {n.get('summary')}",
             f"**Root cause** ({rc.get('category')}, {float(rc.get('confidence') or 0):.0%}): {rc.get('statement')}",
             "**Hypotheses:** " + " | ".join(f"{h['title']} ({h['confidence']:.0%}) [{', '.join(e for e in h['evidence'] if e)}]" for h in r["hypotheses"][:3]),
             "**Missing evidence:** " + ", ".join(c["label"] for c in r["coverage"] if c["state"] != "connected"),
             "**Next:** " + " | ".join(a.get("action", "") for a in (n.get("actions") or [])[:3]),
             f"Full report and investigation: RCA Workbench at /rca#{r['header']['number']}"]
    return "\n".join(lines)


@tool
@_safe
def record_rca(number: str, rca_summary: str, problem_number: str = "") -> str:
    """Post an RCA summary on the incident as a work note, and if a problem number is given, write it to the
    problem's cause notes. Use after the user agrees with the RCA."""
    note = "[RCA] " + rca_summary.strip()

    def do():
        sn().add_note(number, note)
        out = {"work_note_added": number}
        if problem_number:
            sn().update_problem(problem_number, {"cause_notes": rca_summary.strip()})
            out["problem_updated"] = problem_number
        return _out(out)
    return gated(f"Record RCA on {number}" + (f" and {problem_number}" if problem_number else ""),
                 {"work_note": note, "problem": problem_number}, do)


# ============================================================== insight write tools (approval required)
@tool
@_safe
def link_duplicate(child_number: str, parent_number: str) -> str:
    """Mark an incident as a child (duplicate) of a parent incident so the issue is worked once."""
    return gated(f"Link {child_number} under {parent_number}", {"child": child_number, "parent": parent_number},
                 lambda: _out({"linked": child_number, "parent": parent_number,
                               **_brief(sn().set_parent(child_number, parent_number))}))


@tool
@_safe
def create_problem(short_description: str, incident_numbers: list[str], description: str = "",
                   configuration_item: str = "", assignment_group: str = "") -> str:
    """Create a problem record for recurring incidents and link those incidents to it."""
    fields = {"short_description": short_description, "description": description, "cmdb_ci": configuration_item,
              "assignment_group": assignment_group}
    return gated("Create problem", {**fields, "link_incidents": ", ".join(incident_numbers)},
                 lambda: _out(sn().create_problem(fields, incident_numbers)))


READ_TOOLS = [shift_briefing, shift_handover, get_incident, search_incidents, get_incident_activity, summarize_incident,
              who_solved_this_before, find_duplicate_incidents, suggest_assignment, sla_at_risk,
              root_cause_analysis, find_similar_incidents, find_recent_changes, team_health, find_problem_candidates,
              list_assignment_groups]
WRITE_TOOLS = [create_incident, update_incident, add_work_note, resolve_incident, link_duplicate, create_problem, record_rca]
ALL_TOOLS = READ_TOOLS + WRITE_TOOLS
