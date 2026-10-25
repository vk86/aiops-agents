"""Anomaly detection on ServiceNow data.

1. Incident-volume anomalies: for each value of a dimension (CI, assignment group, category) count incidents opened
   in the current window and in each of the previous windows over the baseline (e.g. 24h windows over 14 days).
   Flag values whose current count is well above their own baseline (z-score with a Poisson floor, so rare-but-real
   bursts are not drowned by zero variance), or that are new (no incidents in the baseline, now several).
2. Alert noise: Event Management alerts (em_alert) grouped by node + metric + message key, showing how much
   de-duplication removes, which groups are bursting, and which are steady noise.

If nothing was opened in the most recent window (common on demo instances with old data), the window is anchored at
the newest incident and the result says so.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from incident_agent.insights import keywords, ts
from incident_agent.servicenow import ServiceNowError

UTC = timezone.utc
DIMENSIONS = {"cmdb_ci": "Configuration item", "assignment_group": "Assignment group", "category": "Category"}


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _p(r: dict) -> int:
    try:
        return int(str(r.get("priority") or "5").strip()[:1])
    except ValueError:
        return 5


def scan(sn, window_hours: int = 24, baseline_days: int = 14, dimension: str = "cmdb_ci", now: datetime | None = None,
         min_count: int = 3, z_min: float = 3.0, top: int = 8) -> dict:
    if dimension not in DIMENSIONS:
        raise ValueError(f"dimension must be one of {', '.join(DIMENSIONS)}")
    window_hours = max(1, min(int(window_hours), 24 * 7))
    baseline_days = max(2, min(int(baseline_days), 90))
    end = now or datetime.now(UTC)
    win = timedelta(hours=window_hours)
    span = timedelta(days=baseline_days) + win
    rows = sn.opened_between(_fmt(end - span), _fmt(end))
    anchored = None
    if not any(ts(r.get("opened_at_raw")) and ts(r["opened_at_raw"]) > end - win for r in rows):
        latest = ts(sn.latest_opened())
        if latest and latest < end - win:
            anchored = f"Nothing opened in the last {window_hours}h; window anchored at the newest incident ({_fmt(latest)} UTC)."
            end = latest + timedelta(minutes=1)
            rows = sn.opened_between(_fmt(end - span), _fmt(end))
    n_base = max(1, int(baseline_days * 24 // window_hours))
    counts: dict[str, list[int]] = defaultdict(lambda: [0] * (n_base + 1))
    members: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        t = ts(r.get("opened_at_raw"))
        if not t or t > end:
            continue
        i = int((end - t) / win)
        if i > n_base:
            continue
        key = (r.get(dimension) or "").strip() or "(not set)"
        for k in (key, "__ALL__"):
            counts[k][i] += 1
        if i == 0:
            members[key].append(r)
    flagged, overall = [], None
    for key, c in counts.items():
        cur, base = c[0], c[1:]
        mean = sum(base) / len(base)
        std = math.sqrt(sum((x - mean) ** 2 for x in base) / len(base))
        sigma = max(std, math.sqrt(max(mean, 0.25)), 0.5)
        z = (cur - mean) / sigma
        item = {"key": key, "current": cur, "expected": round(mean, 2), "z": round(z, 1),
                "series": list(reversed(c)), "new": sum(base) == 0 and cur > 0}
        if key == "__ALL__":
            overall = item
            continue
        if key != "(not set)" and cur >= min_count and z >= z_min:
            flagged.append(item)
    flagged.sort(key=lambda x: (-x["z"], -x["current"]))
    out = []
    for f in flagged[:top]:
        inc = sorted(members[f["key"]], key=lambda r: (_p(r), r.get("opened_at_raw") or ""))
        f["incidents"] = [{"number": r["number"], "priority": r.get("priority"), "state": r.get("state"),
                           "short_description": r.get("short_description"), "opened": r.get("opened_at_raw"),
                           "cmdb_ci": r.get("cmdb_ci"), "assignment_group": r.get("assignment_group"),
                           "assigned_to": r.get("assigned_to")} for r in inc[:12]]
        top_p = min((_p(r) for r in inc), default=5)
        f["severity"] = "high" if f["z"] >= 6 or top_p <= 1 else "medium"
        terms = Counter(w for r in inc for w in set(keywords(r, 6)))
        f["common_terms"] = [w for w, n in terms.most_common(5) if n >= 2]
        f["reason"] = (f"{f['current']} incidents in {window_hours}h vs {f['expected']} expected per {window_hours}h "
                       f"(z={f['z']})" + ("; none in the previous " + f"{baseline_days} days" if f["new"] else ""))
        f["changes"], f["related"] = _context(sn, dimension, f["key"], inc, end, win)
        out.append(f)
    keys = {f["key"] for f in out}
    for f in out:  # connected CIs that are spiking too point at a shared cause
        f["related_spiking"] = [r["name"] for r in f.get("related", []) if r["name"] in keys]
    return {"dimension": dimension, "dimension_label": DIMENSIONS[dimension], "window_hours": window_hours,
            "baseline_days": baseline_days, "window_start": _fmt(end - win), "window_end": _fmt(end), "anchored": anchored,
            "incidents_scanned": len(rows), "values_checked": len(counts) - 1, "overall": overall,
            "thresholds": {"min_count": min_count, "z_min": z_min}, "anomalies": out}


def _context(sn, dimension: str, key: str, inc: list[dict], end: datetime, win: timedelta):
    """Changes that ended in or just before the window on the CIs involved, and the CIs connected to them."""
    if dimension == "cmdb_ci":
        cis = [next((r.get("cmdb_ci_raw") for r in inc if r.get("cmdb_ci_raw")), None) or key]
    else:
        cis = list(dict.fromkeys(r.get("cmdb_ci_raw") or r.get("cmdb_ci") for r in inc if r.get("cmdb_ci")))[:5]
    lo = end - win - timedelta(hours=24)
    changes = []
    try:
        for ch in sn.changes_near(cis):
            t = ts(ch.get("end_raw") or ch.get("start_raw")) or (ts(ch["ended_raw"]) if ch.get("ended_raw") else None)
            if t and lo <= t <= end:
                changes.append({"number": ch.get("number"), "short_description": ch.get("short_description"),
                                "cmdb_ci": ch.get("cmdb_ci"), "state": ch.get("state"), "ended": _fmt(t)})
    except ServiceNowError:
        pass
    related = []
    if dimension == "cmdb_ci" and cis[0]:
        try:
            related = [{"name": r["name"], "relation": r["relation"]} for r in sn.related_cis(cis[0])]
        except ServiceNowError:
            pass
    return changes, related


def alert_noise(sn, hours: int = 24, top: int = 10) -> dict:
    hours = max(1, min(int(hours), 24 * 7))
    try:
        alerts = sn.alerts(hours)
    except ServiceNowError as e:
        return {"available": False, "hours": hours,
                "reason": f"Event Management alerts (em_alert) are not readable on this instance: {e}"}
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for a in alerts:
        groups[(a.get("node") or a.get("cmdb_ci") or "?", a.get("metric_name") or a.get("type") or "",
                a.get("message_key") or (a.get("description") or "")[:40])].append(a)
    rows = []
    for (node, metric, key), items in groups.items():
        times = sorted(ts(a.get("sys_created_on_raw")) for a in items if ts(a.get("sys_created_on_raw")))
        per_hour = Counter(t.strftime("%Y-%m-%d %H") for t in times)
        span_h = ((times[-1] - times[0]).total_seconds() / 3600) if len(times) > 1 else 0
        peak = max(per_hour.values(), default=0)
        steady = len(items) >= 12 and span_h >= hours * 0.6 and peak <= max(6, len(items) / max(span_h, 1) * 2)
        sev = sorted({a.get("severity") or "" for a in items})
        rows.append({"node": node, "metric": metric, "message_key": key, "count": len(items), "first": _fmt(times[0]) if times else "",
                     "last": _fmt(times[-1]) if times else "", "peak_per_hour": peak, "severities": sev,
                     "pattern": "steady noise" if steady else ("burst" if peak >= 10 or (len(items) >= 10 and span_h <= 3) else "intermittent"),
                     "sample": (items[-1].get("description") or "")[:120]})
    rows.sort(key=lambda r: (r["pattern"] != "burst", -r["count"]))
    n = len(alerts)
    return {"available": True, "hours": hours, "alerts": n, "groups": len(rows),
            "dedup_reduction_pct": round(100 * (1 - len(rows) / n), 1) if n else 0.0,
            "bursts": [r for r in rows if r["pattern"] == "burst"][:top],
            "noise": [r for r in rows if r["pattern"] == "steady noise"][:top],
            "top_groups": rows[:top]}
