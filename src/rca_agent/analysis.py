"""Root cause analysis: gather evidence, rank hypotheses, build a timeline (no LLM).

Evidence ids (cited by the LLM narrative and shown in the UI):
  CHG...   change_request           PRB...    problem / known error
  INC...   other incidents          NOTE-n    work note / comment n on this incident
  AUD-n    field change n (sys_audit)

Scoring is deliberately simple and explainable: every point a hypothesis gets comes with a
human-readable reason. Confidence is the score capped at 0.95; below 0.3 the result says the
evidence is insufficient and lists what to collect next.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

from incident_agent.insights import CLOSED, brief, prio, rank_similar, ts, words

UTC = timezone.utc
ROLLBACK = re.compile(r"\b(roll(ed)?\s?back|revert(ed)?|backed out|undo)\b", re.I)
CHG_REF = re.compile(r"\bCHG\d{7}\b", re.I)


GENERIC = {"service", "server", "system", "update", "page", "user", "member", "members", "error", "fail", "issue",
           "change", "request", "new", "team", "app", "application", "enrollment"}


def terms(text: str) -> set[str]:
    """Words for change/problem matching: split hyphenated names, drop plurals, ignore generic words."""
    out = set()
    for w in words(text):
        for part in [w] + w.split("-"):
            if len(part) < 3 or part.isdigit():
                continue
            if (part.endswith("s") and len(part) > 4 and part.isalpha()
                    and not part.endswith(("ss", "as", "us", "is"))):
                part = part[:-1]
            if part not in GENERIC:
                out.add(part)
    return out


def _hours(a: datetime | None, b: datetime | None) -> float | None:
    return round((b - a).total_seconds() / 3600, 1) if a and b else None


def _text(target: dict, journal: list[dict]) -> str:
    return " ".join([target.get("short_description", ""), target.get("description", "")] +
                    [j.get("text", "") for j in journal])


KIND_LABEL = {"change": "Change", "deployment": "Deployment", "commit": "Code change"}


def analyze(target: dict, journal: list[dict], audit: list[dict], changes: list[dict], related: list[dict],
            concurrent: list[dict], history: list[dict], problems: list[dict], now: datetime | None = None,
            observations: list[dict] | None = None) -> dict:
    now = now or datetime.now(UTC)
    opened = ts(target.get("opened_at_raw"))
    ci = target.get("cmdb_ci") or ""
    related_names = {r["name"]: r for r in related if r.get("name")}
    observations = observations or []
    obs_text = " ".join(o.get("text", "") for o in observations)
    inc_words = terms(_text(target, journal) + " " + obs_text)
    notes_text = " ".join(j.get("text", "") for j in journal) + " " + obs_text
    hyps = []

    # ---- H1: a recent change on this CI or a connected CI
    for c in changes:
        end = ts(c.get("end_raw") or c.get("end_date_raw")) or ts(c.get("start_raw"))
        gap = _hours(end, opened) if end and opened else None
        if gap is None or gap < -1 or gap > 72:
            continue
        why, score = [], 0.0
        rec = math.exp(-max(gap, 0) / 24)
        score += 0.35 * rec
        why.append(f"finished {gap:.1f}h before the incident opened" if gap >= 0 else "was still running when the incident opened")
        if c.get("cmdb_ci") == ci:
            score += 0.25
            why.append(f"on the same CI ({ci})")
        elif c.get("cmdb_ci") in related_names:
            score += 0.15
            rel = related_names[c["cmdb_ci"]]
            why.append(f"on {c['cmdb_ci']}, which {ci} {rel.get('relation', 'is connected to')}")
        overlap = inc_words & terms(c.get("short_description", "") + " " + c.get("description", ""))
        if overlap:
            score += min(0.3, 0.1 * len(overlap))
            why.append("change text matches the incident/work notes: " + ", ".join(sorted(overlap)[:5]))
        if c.get("number") and c["number"].upper() in notes_text.upper():
            score += 0.2
            why.append("named in the work notes")
        if ROLLBACK.search(notes_text):
            score += 0.05
            why.append("work notes mention a rollback/revert")
        kind = c.get("kind", "change")
        hyps.append({"type": kind, "title": f"{KIND_LABEL.get(kind, 'Change')} {c.get('number')}: {c.get('short_description')}",
                     "score": score, "reasons": why, "evidence": [c.get("number")],
                     "detail": {k: c.get(k) for k in ("number", "short_description", "cmdb_ci", "type", "risk", "state",
                                                     "assignment_group", "start_date", "end_date")},
                     "action": f"Review {c.get('number')} with {c.get('assignment_group') or 'the implementing team'}; "
                               f"roll back or fix forward if confirmed."})

    # ---- H2: upstream / connected CI failing at the same time
    up = []
    for r in concurrent:
        t = ts(r.get("opened_at_raw"))
        if not (t and opened) or abs((t - opened).total_seconds()) > 24 * 3600:
            continue
        on_related = r.get("cmdb_ci") in related_names and r.get("cmdb_ci") != ci
        if on_related:
            up.append((r, related_names[r["cmdb_ci"]], _hours(t, opened)))
    if up:
        first = sorted(up, key=lambda x: -(x[2] or 0))[0]
        r, rel, lead = first
        score = 0.35 + 0.1 * min(len(up), 3) + (0.1 if (lead or 0) > 0 else 0)
        hyps.append({"type": "dependency", "title": f"Connected CI {r['cmdb_ci']} failing at the same time",
                     "score": score,
                     "reasons": [f"{len(up)} incident(s) on CIs connected to {ci} within 24h",
                                 f"{r['number']} on {r['cmdb_ci']} opened {abs(lead or 0):.1f}h "
                                 + ("before" if (lead or 0) > 0 else "after") + " this one"],
                     "evidence": [x[0]["number"] for x in up],
                     "detail": {"related": [{"incident": x[0]["number"], "ci": x[0]["cmdb_ci"], "relation": x[1].get("relation")} for x in up]},
                     "action": f"Check health of {r['cmdb_ci']} first; treat this incident as downstream impact."})

    # ---- H3: known problem / known error
    for p in problems:
        pw = terms(p.get("short_description", "") + " " + p.get("cause_notes", ""))
        overlap = inc_words & pw
        same_ci = p.get("cmdb_ci") and p.get("cmdb_ci") == ci
        if not overlap and not same_ci:
            continue
        known = str(p.get("known_error")).lower() == "true"
        score = 0.15 + min(0.3, 0.07 * len(overlap)) + (0.2 if same_ci else 0) + (0.15 if known else 0)
        why = []
        if same_ci:
            why.append(f"problem is on the same CI ({ci})")
        if overlap:
            why.append("matching words: " + ", ".join(sorted(overlap)[:5]))
        if known:
            why.append("marked as a known error")
        hyps.append({"type": "known_problem", "title": f"Known problem {p.get('number')}: {p.get('short_description')}",
                     "score": score, "reasons": why, "evidence": [p.get("number")],
                     "detail": {k: p.get(k) for k in ("number", "short_description", "state", "known_error", "cause_notes",
                                                     "workaround", "fix_notes")},
                     "action": ("Apply the documented workaround: " + p["workaround"]) if p.get("workaround")
                               else f"Link to {p.get('number')} and follow its investigation."})

    # ---- H4: recurring issue with a known fix
    similar = [r for r in rank_similar(target, history) if r.get("state") in CLOSED or r.get("resolved_at_raw")][:8]
    if len(similar) >= 2:
        fixes = {}
        for r in similar:
            k = (r.get("close_notes") or "").strip()
            if k:
                fixes.setdefault(k, []).append(r)
        top_fix, rows = max(fixes.items(), key=lambda kv: len(kv[1])) if fixes else ("", similar[:1])
        score = min(0.5, 0.15 + 0.05 * len(similar) + 0.05 * len(rows))
        hyps.append({"type": "recurring", "title": f"Recurring issue ({len(similar)} similar incidents resolved before)",
                     "score": score,
                     "reasons": [f"{len(similar)} similar resolved incidents",
                                 f"most common fix used {len(rows)} time(s)" if top_fix else "fixes vary"],
                     "evidence": [r["number"] for r in similar[:5]],
                     "detail": {"common_fix": top_fix, "resolved_by": sorted({r.get('resolved_by') for r in similar if r.get('resolved_by')})},
                     "action": (f"Try the fix that worked before: {top_fix}" if top_fix else "Review the similar incidents' notes.")
                               + " Raise a problem record if it keeps coming back."})

    from .template import HYPOTHESIS_TO_CATEGORY
    for h in hyps:
        h["confidence"] = round(min(h["score"], 0.95), 2)
        h["category"] = HYPOTHESIS_TO_CATEGORY.get(h["type"], "Undetermined")
    hyps.sort(key=lambda h: -h["confidence"])

    # ---- timeline
    tl = []
    for c in changes:
        for key, label in (("start_raw", "started"), ("end_raw", "finished")):
            t = ts(c.get(key))
            if t and opened and timedelta(hours=-1) <= opened - t <= timedelta(hours=72):
                tl.append({"time": t, "kind": c.get("kind", "change"), "text": f"{c.get('number')} {label}: {c.get('short_description')} ({c.get('cmdb_ci')})", "ref": c.get("number")})
    if opened:
        tl.append({"time": opened, "kind": "incident", "text": f"{target.get('number')} opened ({target.get('priority')}): {target.get('short_description')}", "ref": target.get("number")})
    for r in concurrent:
        t = ts(r.get("opened_at_raw"))
        if t and opened and abs((t - opened).total_seconds()) <= 24 * 3600:
            tl.append({"time": t, "kind": "related", "text": f"{r['number']} opened on {r.get('cmdb_ci') or 'no CI'}: {r.get('short_description')}", "ref": r["number"]})
    for i, a in enumerate(audit, 1):
        t = ts(a.get("at_raw") or a.get("at"))
        if t:
            tl.append({"time": t, "kind": "field", "text": f"{a.get('field')}: {a.get('old') or '—'} → {a.get('new')}" + (f" by {a['by']}" if a.get("by") else ""), "ref": f"AUD-{i}"})
    for i, j in enumerate(journal, 1):
        t = ts(j.get("at"))
        if t:
            tl.append({"time": t, "kind": "note", "text": f"{j.get('type')} by {j.get('by')}: {j.get('text')}", "ref": f"NOTE-{i}"})
    for o in observations:
        t = ts(o.get("at_raw"))
        if t:
            tl.append({"time": t, "kind": "observation", "text": f"{o.get('by') or 'engineer'}: {o.get('text')}", "ref": o.get("id")})
    if target.get("resolved_at_raw"):
        tl.append({"time": ts(target["resolved_at_raw"]), "kind": "resolved", "text": f"Resolved: {target.get('close_notes') or ''}", "ref": target.get("number")})
    tl = [e for e in tl if e["time"]]
    tl.sort(key=lambda e: e["time"])
    for e in tl:
        e["time"] = e["time"].strftime("%Y-%m-%d %H:%M UTC")

    top = hyps[0] if hyps else None
    sufficient = bool(top and top["confidence"] >= 0.3)
    end = ts(target.get("resolved_at_raw")) or now
    impact = {"priority": target.get("priority"), "state": target.get("state"), "ci": ci or None,
              "duration_hours": _hours(opened, end), "related_incidents": len([e for e in tl if e["kind"] == "related"]),
              "connected_cis": [r["name"] for r in related][:8]}
    gaps = []
    if not ci:
        gaps.append("Incident has no configuration item; set cmdb_ci so changes and dependencies can be checked.")
    if not changes:
        gaps.append("No change requests found on this CI or its connected CIs in the 72h before.")
    if not journal:
        gaps.append("No work notes yet; add findings (errors seen, what was tried).")
    if not related and ci:
        gaps.append("No CI relationships in the CMDB for this CI; dependencies could not be checked.")
    gaps.append("Logs/metrics/traces are not connected in this demo (AIOps PoC covers Tempo/Loki).")
    return {"incident": brief(target), "verdict": ("Likely cause: " + top["title"]) if sufficient
            else "Insufficient evidence for a confident root cause", "sufficient_evidence": sufficient,
            "hypotheses": hyps[:5], "timeline": tl[-40:], "impact": impact, "evidence_gaps": gaps,
            "next_actions": [h["action"] for h in hyps[:3]] or ["Collect the missing evidence listed above."]}


RCA_PROMPT = """Write a root cause analysis for incident {number} for an engineering audience.
Use ONLY the evidence below. Cite evidence ids in square brackets, e.g. [CHG0098871], [NOTE-2], [INC0012346].
If the evidence is insufficient, say so plainly and list what to check next. Never invent systems, people or times.

Structure (Markdown, concise):
**Summary** (2-3 sentences: what happened, impact, likely cause with confidence)
**Root cause** (the top hypothesis and why; mention the runner-up and why it is less likely)
**Contributing factors**
**Timeline** (UTC, key events only)
**5 whys** (short chain ending at a fixable cause; mark any step that is an assumption)
**Corrective actions** (immediate fix, then prevention; owners as teams)

EVIDENCE (JSON):
{evidence}
"""


def template_narrative(a: dict) -> str:
    """Used when the LLM is unavailable; still evidence-based."""
    inc = a["incident"]
    lines = [f"**Summary** {inc.get('number')} ({inc.get('priority')}) on {a['impact'].get('ci') or 'no CI'}. {a['verdict']}."]
    for i, h in enumerate(a["hypotheses"][:3], 1):
        lines.append(f"{i}. {h['title']} — confidence {h['confidence']:.2f}: " + "; ".join(h["reasons"])
                     + " " + " ".join(f"[{e}]" for e in h["evidence"] if e))
    lines.append("**Next actions** " + " | ".join(a["next_actions"]))
    if a["evidence_gaps"]:
        lines.append("**Evidence gaps** " + " ".join(a["evidence_gaps"]))
    return "\n".join(lines)
