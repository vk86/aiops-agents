"""Incident insights computed from plain incident rows (no LLM, no network).

Both the live ServiceNow client and the mock client return rows with these keys
(missing ones are fine): number, short_description, description, state, priority,
assignment_group, assigned_to, resolved_by, cmdb_ci, category, caller_id,
close_code, close_notes, reassignment_count, problem_id,
opened_at_raw / resolved_at_raw / sys_updated_on_raw  ("YYYY-MM-DD HH:MM:SS", UTC).
"""
from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

UTC = timezone.utc
STOP = set("""the and for with from that this have has had not are was were been after before into onto over under
when then than them they their there what which while who whom will would could should about again also unable
cannot can't does doesn't issue issues error errors problem problems please help user users getting get working
work works down slow some all any our your its out off very more most just still since being""".split())
SLA_TARGET_H = {"1": 4, "2": 8, "3": 72, "4": 120, "5": 240}   # resolution targets used when task_sla is empty
CLOSED = {"Resolved", "Closed", "Canceled", "Cancelled"}


# ------------------------------------------------------------------ helpers
def ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip().replace("T", " ").rstrip("Z").replace(" UTC", "")
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16)):
        try:
            return datetime.strptime(raw[:n], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def num(v, default: float = 0.0) -> float:
    """ServiceNow display values can be '219,750.44' or '1.234,5'; raw values are plain. Parse either."""
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("\u00a0", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", "") if len(s.split(",")[-1]) == 3 else s.replace(",", ".")
    try:
        return float(re.sub(r"[^0-9.\-]", "", s) or default)
    except ValueError:
        return default


def prio(row: dict) -> int:
    m = re.match(r"\s*(\d)", str(row.get("priority", "")))
    return int(m.group(1)) if m else 5


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9\-]{2,}", (text or "").lower()) if w not in STOP and not w.isdigit()}


def keywords(row: dict, n: int = 4) -> list[str]:
    """Most distinctive words of an incident (short description first)."""
    sd = [w for w in re.findall(r"[a-z0-9][a-z0-9\-]{2,}", (row.get("short_description") or "").lower())
          if w not in STOP and not w.isdigit()]
    seen, out = set(), []
    for w in sorted(sd, key=len, reverse=True):
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:n]


def similarity(a: dict, b: dict) -> tuple[float, list[str]]:
    wa, wb = words(a.get("short_description", "")), words(b.get("short_description", ""))
    wa |= {w for w in words(a.get("description", "")) if w in wb} if wb else set()
    union = wa | wb
    score = len(wa & wb) / len(union) if union else 0.0
    reasons = []
    if wa & wb:
        reasons.append("same words: " + ", ".join(sorted(wa & wb)[:4]))
    if a.get("cmdb_ci") and a.get("cmdb_ci") == b.get("cmdb_ci"):
        score += 0.35
        reasons.append(f"same CI {a['cmdb_ci']}")
    if a.get("category") and a.get("category") == b.get("category"):
        score += 0.05
    return round(min(score, 1.0), 2), reasons


def hours_between(a: str | None, b: str | None) -> float | None:
    ta, tb = ts(a), ts(b)
    if ta and tb and tb >= ta:
        return round((tb - ta).total_seconds() / 3600, 1)
    return None


def age_h(row: dict, now: datetime) -> float:
    t = ts(row.get("opened_at_raw"))
    return round((now - t).total_seconds() / 3600, 1) if t else 0.0


def brief(row: dict, now: datetime | None = None) -> dict:
    out = {k: row.get(k) for k in ("number", "short_description", "priority", "state", "assignment_group",
                                   "assigned_to", "cmdb_ci") if row.get(k) not in (None, "")}
    if now:
        out["age_hours"] = age_h(row, now)
    return out


# ------------------------------------------------------------------ shifts
def parse_shifts(spec: str | None = None) -> list[tuple[str, int, int]]:
    spec = spec or os.getenv("SHIFTS", "Day=07-15,Evening=15-23,Night=23-07")
    out = []
    for part in spec.split(","):
        name, _, rng = part.partition("=")
        a, _, b = rng.partition("-")
        out.append((name.strip(), int(a), int(b)))
    return out


def shift_tz():
    name = os.getenv("SHIFT_TZ", "America/Chicago")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name), name
    except Exception:  # Windows without tzdata: fall back to the machine's local time
        return datetime.now().astimezone().tzinfo, "local time"


def current_shift(now_utc: datetime | None = None, spec: str | None = None) -> dict:
    tz, tzname = shift_tz()
    now_utc = now_utc or datetime.now(UTC)
    local = now_utc.astimezone(tz)
    for name, a, b in parse_shifts(spec):
        h = local.hour
        inside = a <= h < b if a < b else (h >= a or h < b)
        if inside:
            start = local.replace(hour=a, minute=0, second=0, microsecond=0)
            if a > b and h < b:
                start -= timedelta(days=1)
            end = start + timedelta(hours=(b - a) % 24 or 24)
            return {"name": name, "start_local": start.strftime("%a %d %b %H:%M"), "end_local": end.strftime("%H:%M"),
                    "timezone": tzname, "start_utc": start.astimezone(UTC), "hours_in": round((local - start).total_seconds() / 3600, 1)}
    start = local - timedelta(hours=8)
    return {"name": "Current", "start_local": start.strftime("%a %d %b %H:%M"), "end_local": local.strftime("%H:%M"),
            "timezone": tzname, "start_utc": start.astimezone(UTC), "hours_in": 8.0}


# ------------------------------------------------------------------ SLA
def sla_view(open_rows: list[dict], sla_rows: list[dict], now: datetime) -> list[dict]:
    """Real task_sla records when the instance has them; otherwise estimate from priority targets."""
    out = []
    if sla_rows:
        for s in sla_rows:
            pct = num(s.get("percentage_raw") or s.get("percentage"))
            out.append({"number": s.get("task"), "sla": s.get("sla"), "percent_elapsed": round(pct),
                        "breached": str(s.get("has_breached_raw") or s.get("has_breached")).lower() == "true" or pct >= 100,
                        "due": s.get("planned_end_time"), "source": "task_sla"})
    else:
        for r in open_rows:
            if r.get("state") in ("On Hold",):
                continue
            target = SLA_TARGET_H.get(str(prio(r)), 120)
            pct = 100 * age_h(r, now) / target
            due = ts(r.get("opened_at_raw"))
            out.append({"number": r["number"], "sla": f"P{prio(r)} resolution ({target}h)", "percent_elapsed": round(pct),
                        "breached": pct >= 100, "due": (due + timedelta(hours=target)).strftime("%Y-%m-%d %H:%M UTC") if due else None,
                        "source": "estimated from priority"})
    out.sort(key=lambda x: -x["percent_elapsed"])
    return out


# ------------------------------------------------------------------ briefing / handover
def briefing(open_rows: list[dict], sla_rows: list[dict], shift: dict, now: datetime | None = None,
             group: str = "", top: int = 10) -> dict:
    now = now or datetime.now(UTC)
    rows = sorted(open_rows, key=lambda r: (prio(r), -(ts(r.get("opened_at_raw")) or now).timestamp()))
    start = shift["start_utc"]
    new = [r for r in rows if (ts(r.get("opened_at_raw")) or now) >= start]
    sla = sla_view(open_rows, sla_rows, now)
    at_risk = [s for s in sla if s["percent_elapsed"] >= 75]
    by_p = Counter(f"P{prio(r)}" for r in rows)
    unassigned = [r for r in rows if not r.get("assigned_to")]
    aged = [r for r in rows if age_h(r, now) > 72]
    headlines = []
    if by_p.get("P1"):
        headlines.append(f"{by_p['P1']} P1 open")
    if at_risk:
        b = sum(1 for s in at_risk if s["breached"])
        headlines.append(f"{len(at_risk)} SLA at risk" + (f" ({b} breached)" if b else ""))
    if new:
        headlines.append(f"{len(new)} new since {shift['name'].lower()} shift started")
    if unassigned:
        headlines.append(f"{len(unassigned)} unassigned")
    return {
        "shift": {k: v for k, v in shift.items() if k != "start_utc"},
        "scope": group or "all groups",
        "headline": "; ".join(headlines) or "Queue is quiet",
        "open_total": len(rows), "open_by_priority": dict(sorted(by_p.items())),
        "top": [brief(r, now) for r in rows[:top]],
        "new_since_shift_start": [brief(r, now) for r in new[:top]],
        "sla_at_risk": at_risk[:top],
        "unassigned": [brief(r, now) for r in unassigned[:top]],
        "aged_over_3_days": [brief(r, now) for r in aged[:top]],
    }


def handover(opened: list[dict], resolved: list[dict], updated_open: list[dict], journal_by_number: dict,
             hours: int, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    still_open = sorted(updated_open, key=prio)
    return {
        "window_hours": hours,
        "summary": f"{len(opened)} opened, {len(resolved)} resolved, {len(still_open)} still open with activity",
        "opened": [brief(r, now) for r in sorted(opened, key=prio)],
        "resolved": [{**brief(r), "resolved_by": r.get("resolved_by"), "fix": r.get("close_notes")} for r in resolved],
        "carry_over": [{**brief(r, now), "last_note": (journal_by_number.get(r["number"]) or [{}])[-1].get("text")}
                       for r in still_open],
        "reassigned": [brief(r) for r in still_open if int(num(r.get("reassignment_count"))) > 0],
    }


# ------------------------------------------------------------------ history: who solved it, assignment
def rank_similar(target: dict, candidates: list[dict], min_score: float = 0.15) -> list[dict]:
    out = []
    for c in candidates:
        if c.get("number") == target.get("number"):
            continue
        s, why = similarity(target, c)
        if s >= min_score:
            out.append({**c, "_score": s, "_why": why})
    out.sort(key=lambda r: r.get("resolved_at_raw") or "", reverse=True)   # newest first among equals
    out.sort(key=lambda r: -r["_score"])
    return out


def experts(target: dict, history: list[dict], limit: int = 5) -> dict:
    similar = [r for r in rank_similar(target, history) if r.get("state") in CLOSED or r.get("resolved_at_raw")]
    people: dict[str, dict] = {}
    for r in similar:
        who = r.get("resolved_by") or r.get("assigned_to")
        if not who:
            continue
        p = people.setdefault(who, {"person": who, "solved": 0, "groups": Counter(), "hours": [], "fixes": [],
                                    "weight": 0.0})
        p["solved"] += 1
        p["weight"] += r["_score"]
        if r.get("assignment_group"):
            p["groups"][r["assignment_group"]] += 1
        h = hours_between(r.get("opened_at_raw"), r.get("resolved_at_raw"))
        if h is not None:
            p["hours"].append(h)
        p["fixes"].append({"number": r["number"], "resolved": (r.get("resolved_at_raw") or "")[:10],
                           "close_code": r.get("close_code"), "fix": r.get("close_notes"), "match": r["_score"]})
    ranked = sorted(people.values(), key=lambda p: (-p["weight"], -p["solved"]))
    out = []
    for p in ranked[:limit]:
        out.append({"person": p["person"], "solved_similar": p["solved"],
                    "group": p["groups"].most_common(1)[0][0] if p["groups"] else None,
                    "median_hours_to_resolve": median(p["hours"]) if p["hours"] else None,
                    "examples": p["fixes"][:3]})
    fixes = Counter((r.get("close_code") or "—") for r in similar)
    return {"incident": target.get("number"), "searched_for": keywords(target) + ([target["cmdb_ci"]] if target.get("cmdb_ci") else []),
            "similar_resolved": len(similar), "experts": out,
            "common_resolutions": [{"close_code": k, "count": v} for k, v in fixes.most_common(3)],
            "most_similar": [{"number": r["number"], "short_description": r.get("short_description"), "match": r["_score"],
                              "why": r["_why"], "resolved_by": r.get("resolved_by"), "fix": r.get("close_notes")}
                             for r in similar[:5]]}


def suggest_assignment(target: dict, history: list[dict], ci_support_group: str | None = None,
                       open_rows: list[dict] | None = None) -> dict:
    similar = [r for r in rank_similar(target, history) if r.get("assignment_group")]
    weights: dict[str, float] = defaultdict(float)
    for r in similar:
        weights[r["assignment_group"]] += r["_score"]
    if ci_support_group:
        weights[ci_support_group] += 0.6
    total = sum(weights.values()) or 1.0
    groups = sorted(weights.items(), key=lambda kv: -kv[1])
    if not groups:
        return {"incident": target.get("number"), "recommendation": None,
                "reason": "No similar history and no CI support group found."}
    group, w = groups[0]
    solvers = Counter((r.get("resolved_by") or r.get("assigned_to")) for r in similar
                      if r.get("assignment_group") == group and (r.get("resolved_by") or r.get("assigned_to")))
    load = Counter(r.get("assigned_to") for r in (open_rows or []) if r.get("assigned_to"))
    assignee = None
    if solvers:
        # best solver, but skip someone already carrying a heavy queue
        for person, _ in solvers.most_common():
            if load.get(person, 0) < 5:
                assignee = person
                break
        assignee = assignee or solvers.most_common(1)[0][0]
    reasons = []
    n = sum(1 for r in similar if r["assignment_group"] == group)
    if n:
        reasons.append(f"{n} similar incidents were handled by {group}")
    if ci_support_group == group:
        reasons.append(f"{group} is the support group for CI {target.get('cmdb_ci')}")
    if assignee:
        reasons.append(f"{assignee} resolved {solvers[assignee]} of them; {load.get(assignee, 0)} open now")
    return {"incident": target.get("number"), "current_group": target.get("assignment_group") or None,
            "current_assignee": target.get("assigned_to") or None,
            "recommendation": {"assignment_group": group, "assigned_to": assignee, "confidence": round(w / total, 2)},
            "reasons": reasons, "alternatives": [{"assignment_group": g, "share": round(v / total, 2)} for g, v in groups[1:3]],
            "already_correct": (target.get("assignment_group") == group)}


# ------------------------------------------------------------------ duplicates
def duplicates(target: dict, open_rows: list[dict], hours: int = 48, min_score: float = 0.3) -> dict:
    t0 = ts(target.get("opened_at_raw"))
    cands = []
    for r in open_rows:
        if r.get("number") == target.get("number"):
            continue
        t = ts(r.get("opened_at_raw"))
        if t0 and t and abs((t - t0).total_seconds()) > hours * 3600:
            continue
        s, why = similarity(target, r)
        if s >= min_score:
            cands.append({**brief(r), "opened": (r.get("opened_at_raw") or "")[:16], "match": s, "why": why,
                          "already_child_of": r.get("parent_incident") or None})
    cands.sort(key=lambda c: -c["match"])
    group = [target] + [r for r in open_rows if r.get("number") in {c["number"] for c in cands}]
    parent = min(group, key=lambda r: (prio(r), r.get("opened_at_raw") or "9")) if cands else None
    return {"incident": target.get("number"), "window_hours": hours, "candidates": cands,
            "suggested_parent": parent.get("number") if parent else None,
            "advice": (f"Link {len(cands)} incident(s) as children of {parent['number']} so they are worked once."
                       if cands else "No likely duplicates.")}


# ------------------------------------------------------------------ team health, problems
def team_health(open_rows: list[dict], resolved_rows: list[dict], days: int, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    groups: dict[str, dict] = defaultdict(lambda: {"open": Counter(), "aged": 0, "resolved": 0, "hours": [], "reassign": []})
    for r in open_rows:
        g = groups[r.get("assignment_group") or "(unassigned)"]
        g["open"][f"P{prio(r)}"] += 1
        g["aged"] += age_h(r, now) > 72
    for r in resolved_rows:
        g = groups[r.get("assignment_group") or "(unassigned)"]
        g["resolved"] += 1
        h = hours_between(r.get("opened_at_raw"), r.get("resolved_at_raw"))
        if h is not None:
            g["hours"].append(h)
        g["reassign"].append(int(num(r.get("reassignment_count"))))
    table = []
    for name, g in groups.items():
        table.append({"group": name, "open": sum(g["open"].values()), "open_by_priority": dict(sorted(g["open"].items())),
                      "aged_over_3_days": g["aged"], f"resolved_last_{days}d": g["resolved"],
                      "median_hours_to_resolve": median(g["hours"]) if g["hours"] else None,
                      "avg_reassignments": round(sum(g["reassign"]) / len(g["reassign"]), 1) if g["reassign"] else None})
    table.sort(key=lambda x: (-x["open"], x["group"]))
    ci = Counter(r.get("cmdb_ci") for r in open_rows + resolved_rows if r.get("cmdb_ci"))
    return {"days": days, "groups": table, "top_cis": [{"ci": k, "incidents": v} for k, v in ci.most_common(5)],
            "open_total": len(open_rows), f"resolved_last_{days}d": len(resolved_rows)}


def problem_candidates(rows: list[dict], days: int, min_count: int = 3) -> list[dict]:
    free = [r for r in rows if not r.get("problem_id")]
    out = []
    by_ci = defaultdict(list)
    for r in free:
        if r.get("cmdb_ci"):
            by_ci[r["cmdb_ci"]].append(r)
    seen = set()
    for ci, items in by_ci.items():
        if len(items) >= min_count:
            kw = Counter(w for r in items for w in words(r.get("short_description", "")))
            out.append({"cluster": f"CI {ci}", "configuration_item": ci, "incidents": len(items),
                        "numbers": [r["number"] for r in sorted(items, key=lambda r: r.get("opened_at_raw") or "")],
                        "common_words": [w for w, _ in kw.most_common(4)],
                        "groups": sorted({r.get("assignment_group") for r in items if r.get("assignment_group")}),
                        "suggested_title": f"Recurring incidents on {ci}: " + ", ".join(w for w, _ in kw.most_common(3))})
            seen.update(r["number"] for r in items)
    # clusters by shared keywords for incidents without a CI
    by_kw = defaultdict(list)
    for r in free:
        if r["number"] in seen:
            continue
        k = tuple(sorted(keywords(r, 2)))
        if len(k) == 2:
            by_kw[k].append(r)
    for k, items in by_kw.items():
        if len(items) >= min_count:
            out.append({"cluster": "keywords " + " + ".join(k), "configuration_item": None, "incidents": len(items),
                        "numbers": [r["number"] for r in items], "common_words": list(k),
                        "groups": sorted({r.get("assignment_group") for r in items if r.get("assignment_group")}),
                        "suggested_title": "Recurring incidents: " + " ".join(k)})
    out.sort(key=lambda c: -c["incidents"])
    return out
