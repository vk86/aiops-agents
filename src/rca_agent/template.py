"""The AIOps RCA template: the contract every RCA report follows.

Sections are filled from two places:
  * deterministic: computed from evidence (header, timeline, metrics, evidence table, hypotheses, coverage)
  * narrative:     written by the LLM from that evidence only (summary, impact, root cause statement, ...)
Every narrative claim must cite evidence ids; unknowns go to "Open questions", never guessed.
"""
from __future__ import annotations

TEMPLATE_VERSION = "1.0"

# Root-cause taxonomy (one primary category per RCA)
ROOT_CAUSE_CATEGORIES = [
    "Change / release",          # planned change, deployment or config push
    "Code defect",               # bug introduced in a commit/PR
    "Configuration",             # config drift or wrong setting not tied to a change record
    "Capacity / performance",    # saturation, limits, load
    "Dependency / third party",  # upstream service, vendor, network path
    "Infrastructure",            # host, network, storage, cloud
    "Data",                      # bad data, schema, batch
    "Security",                  # certs, auth, keys, attack
    "Process / human",           # missed step, runbook gap
    "Known error / recurring",   # documented problem that recurred
    "Undetermined",              # not enough evidence yet
]

HYPOTHESIS_TO_CATEGORY = {"change": "Change / release", "deployment": "Change / release", "commit": "Code defect",
                          "dependency": "Dependency / third party", "known_problem": "Known error / recurring",
                          "recurring": "Known error / recurring"}

# Evidence domains the template expects; coverage is reported per domain
EVIDENCE_DOMAINS = [
    ("itsm", "ITSM (ServiceNow)", "Incidents, work notes, field history, changes, problems, CMDB relationships"),
    ("metrics", "Metrics", "Error rate, latency, saturation around the incident window (Prometheus, Dynatrace, Datadog)"),
    ("events", "Events / alerts", "Alerts and events that fired (Event Management, PagerDuty, Moogsoft)"),
    ("logs", "Logs", "Errors and stack traces from the affected services (Splunk, Loki, ELK)"),
    ("traces", "Traces", "Failing hop in a business transaction (Tempo, Jaeger, Dynatrace)"),
    ("code", "Code (Git)", "Commits and merged PRs on the service's repository in the window"),
    ("release", "Release / CI-CD", "Deployments to production in the window (GitHub, Harness, Jenkins, Azure DevOps)"),
    ("human", "Engineer observations", "Findings added by responders in the RCA Workbench"),
]

SECTIONS = [
    # key, title, filled_by, guidance
    ("header", "Incident details", "evidence", "Number, title, priority, service/CI, owner group, status, dates"),
    ("summary", "Executive summary", "narrative", "2-3 sentences: what happened, impact, root cause, current state"),
    ("impact", "Customer & business impact", "narrative", "Who/what was affected, how much, how long; mark estimates"),
    ("metrics", "Detection & response metrics", "evidence", "Impact start, detected, acknowledged, mitigated, resolved; TTD/TTA/TTR"),
    ("timeline", "Timeline (UTC)", "evidence", "Changes, deployments, alerts, incident events, notes in time order"),
    ("root_cause", "Root cause", "narrative", "Primary cause, category from taxonomy, confidence, evidence ids"),
    ("hypotheses", "Hypotheses considered", "evidence", "Ranked causes with reasons, confidence and evidence; why runner-ups were ruled down"),
    ("contributing", "Contributing factors", "narrative", "Conditions that made it worse or slower to detect/fix"),
    ("five_whys", "5 whys", "narrative", "Chain from symptom to a fixable cause; mark assumptions"),
    ("evidence", "Evidence", "evidence", "Table of evidence items: id, source, finding, time"),
    ("coverage", "Evidence coverage", "evidence", "Which domains were checked, connected or missing"),
    ("actions", "Corrective & preventive actions (CAPA)", "narrative", "Action, type (immediate/short-term/long-term), owner team, priority"),
    ("lessons", "Lessons learned", "narrative", "What went well, what to improve"),
    ("open_questions", "Open questions", "narrative", "What is still unknown and which evidence would answer it"),
    ("signoff", "Review & sign-off", "evidence", "Prepared by, status (draft/reviewed/approved), reviewers"),
]

NARRATIVE_KEYS = [k for k, _, by, _ in SECTIONS if by == "narrative"]

NARRATIVE_PROMPT = """You are the AIOps RCA agent. Fill the narrative sections of an RCA report for incident {number}.
Use ONLY the evidence JSON below. Cite evidence ids in square brackets in every claim, e.g. [CHG0098871], [NOTE-2],
[INC0012346], [OBS-1]. If something is not supported by evidence, put it under open_questions instead of guessing.
Pick root_cause.category from this list exactly: {categories}.
If the top hypothesis confidence is below 0.3, set category to "Undetermined" and say what evidence is needed.

Return ONLY a JSON object with these keys:
{{
 "summary": "2-3 sentences",
 "impact": "who/what was affected, how much, how long (mark estimates)",
 "root_cause": {{"statement": "...", "category": "...", "confidence": 0.0}},
 "contributing": ["..."],
 "five_whys": ["Why 1 ...", "Why 2 ...", "..."],
 "actions": [{{"action": "...", "type": "immediate|short-term|long-term", "owner_team": "...", "priority": "high|medium|low"}}],
 "lessons": {{"went_well": ["..."], "improve": ["..."]}},
 "open_questions": ["..."]
}}

EVIDENCE:
{evidence}
"""


def empty_narrative(analysis: dict) -> dict:
    """Deterministic narrative used when no LLM is available: evidence-based, terse, honest."""
    hyps = analysis.get("hypotheses") or []
    top = hyps[0] if hyps else None
    sufficient = analysis.get("sufficient_evidence")
    inc = analysis.get("incident", {})
    cat = HYPOTHESIS_TO_CATEGORY.get(top["type"], "Undetermined") if (top and sufficient) else "Undetermined"
    cite = (" " + " ".join(f"[{e}]" for e in top["evidence"] if e)) if top else ""
    return {
        "summary": f"{inc.get('number')} ({inc.get('priority')}) {inc.get('short_description')}. "
                   + (f"Most likely cause: {top['title']} (confidence {top['confidence']:.0%}).{cite}" if sufficient and top
                      else "Root cause not yet determined; see open questions."),
        "impact": f"Priority {inc.get('priority')}; {analysis.get('impact', {}).get('related_incidents', 0)} related incident(s); "
                  f"duration {analysis.get('impact', {}).get('duration_hours')}h so far (from ServiceNow timestamps).",
        "root_cause": {"statement": ("; ".join(top["reasons"]) + cite) if (top and sufficient) else "Undetermined.",
                       "category": cat, "confidence": top["confidence"] if top else 0.0},
        "contributing": [f"{h['title']} ({h['confidence']:.0%})" for h in hyps[1:3]],
        "five_whys": [],
        "actions": [{"action": a, "type": "immediate" if i == 0 else "short-term", "owner_team": inc.get("assignment_group") or "",
                     "priority": "high" if i == 0 else "medium"} for i, a in enumerate(analysis.get("next_actions", [])[:3])],
        "lessons": {"went_well": [], "improve": []},
        "open_questions": analysis.get("evidence_gaps", []),
    }
