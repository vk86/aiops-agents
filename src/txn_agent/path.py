"""Transaction tracking on ServiceNow data.

A business transaction (e.g. "Member Enrollment") is modelled by its business service in the CMDB and everything it
depends on (cmdb_rel_ci, parent -> child). For each hop we read open incidents and recent changes and score it:
  down     - an open P1/P2, or 2+ open incidents
  degraded - an open incident, or a change finished in the last 24h
  healthy  - nothing open, nothing changed
The likely origin is the deepest unhealthy hop whose own dependencies are healthy: failures propagate upstream, so
the lowest broken hop is where to look first.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from incident_agent.insights import ts
from incident_agent.servicenow import ServiceNowError

UTC = timezone.utc
RANK = {"down": 2, "degraded": 1, "healthy": 0}
CHANGE_HOURS = 24
LOOKBACK_CHANGES_H = 72


def _p(r: dict) -> int:
    try:
        return int(str(r.get("priority") or "5").strip()[:1])
    except ValueError:
        return 5


def transactions(sn) -> list[dict]:
    return [{"name": s.get("name"), "sys_id": s.get("sys_id_raw") or s.get("sys_id"),
             "criticality": s.get("busines_criticality") or ""} for s in sn.business_services() if s.get("name")]


def _hop_health(sn, sys_id: str, now: datetime) -> dict:
    try:
        inc = sn.incidents_on_ci(sys_id, active_only=True)
    except ServiceNowError:
        inc = []
    changes = []
    try:
        for ch in sn.changes_near([sys_id], limit=30):
            t = ts(ch.get("end_raw") or ch.get("start_raw"))
            if t and now - timedelta(hours=LOOKBACK_CHANGES_H) <= t <= now + timedelta(hours=1):
                changes.append({"number": ch.get("number"), "short_description": ch.get("short_description"),
                                "state": ch.get("state"), "ended": t.strftime("%Y-%m-%d %H:%M"),
                                "hours_ago": round((now - t).total_seconds() / 3600, 1)})
    except ServiceNowError:
        pass
    top = min((_p(r) for r in inc), default=9)
    recent_change = any(c["hours_ago"] <= CHANGE_HOURS for c in changes)
    status = "down" if top <= 2 or len(inc) >= 2 else ("degraded" if inc or recent_change else "healthy")
    why = []
    if inc:
        why.append(f"{len(inc)} open incident(s), highest P{top}")
    if recent_change:
        why.append("change in the last 24h: " + ", ".join(c["number"] for c in changes if c["hours_ago"] <= CHANGE_HOURS))
    return {"status": status, "why": "; ".join(why) or "no open incidents, no recent changes",
            "incidents": [{"number": r["number"], "priority": r.get("priority"), "state": r.get("state"),
                           "short_description": r.get("short_description"), "opened": r.get("opened_at_raw")} for r in inc[:8]],
            "changes": changes[:5]}


def trace_path(sn, name: str, max_depth: int = 5, max_nodes: int = 25, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    root = sn.ci_lookup(name)
    if not root:
        raise ServiceNowError(f"No CI or business service named '{name}' in the CMDB.")
    rid = root.get("sys_id_raw") or root.get("sys_id")
    hops = [{"name": root.get("name"), "sys_id": rid, "depth": 0, "parent": None, "relation": "entry point",
             "ci_class": root.get("sys_class_name") or ""}]
    seen, i = {rid}, 0
    while i < len(hops) and len(hops) < max_nodes:
        h = hops[i]
        i += 1
        if h["depth"] >= max_depth:
            continue
        try:
            rel = sn.related_cis(h["sys_id"], with_services=True)
        except ServiceNowError:
            rel = []
        for r in rel:
            if r.get("dir") != "down" or not r.get("sys_id") or r["sys_id"] in seen:
                continue
            seen.add(r["sys_id"])
            hops.append({"name": r["name"], "sys_id": r["sys_id"], "depth": h["depth"] + 1, "parent": h["name"],
                         "relation": r.get("relation") or "depends on", "ci_class": ""})
            if len(hops) >= max_nodes:
                break
    for h in hops:
        h.update(_hop_health(sn, h["sys_id"], now))
    children = {}
    for h in hops:
        children.setdefault(h["parent"], []).append(h["name"])

    by = {h["name"]: h for h in hops}

    def descendants(n: str) -> list[dict]:
        out = []
        for c in children.get(n, []):
            out += [by[c]] + descendants(c)
        return out
    # a broken dependency explains a hop only if it is at least as broken; otherwise the hop itself is the origin
    bad = [h for h in hops if h["status"] != "healthy"]
    origins = [h for h in bad if not any(RANK[d["status"]] >= RANK[h["status"]] for d in descendants(h["name"]))]
    origins.sort(key=lambda h: (-RANK[h["status"]], min((_p(x) for x in h["incidents"]), default=9), -h["depth"]))
    origin = origins[0] if origins else None
    for h in hops:
        h["origin"] = bool(origin and h["name"] == origin["name"])
        h.pop("sys_id", None) if h.get("sys_id") == h.get("name") else None
    worst = max((RANK[h["status"]] for h in hops), default=0)
    status = {2: "down", 1: "degraded", 0: "healthy"}[worst]
    if origin:
        chain = _chain(hops, origin["name"])
        also = [h["name"] for h in bad if h is not origin and h["name"] not in chain]
        verdict = (f"{root.get('name')} is {status}. Likely origin: {origin['name']} ({origin['why']}). "
                   f"Impact path: {' → '.join(chain)}." + (f" Also unhealthy: {', '.join(also)}." if also else ""))
    else:
        verdict = f"{root.get('name')}: all {len(hops)} hops healthy (no open incidents, no changes in the last 24h)."
    return {"transaction": root.get("name"), "status": status, "verdict": verdict, "hops": hops,
            "origin": origin["name"] if origin else None, "hop_count": len(hops),
            "truncated": len(hops) >= max_nodes, "checked_at": now.strftime("%Y-%m-%d %H:%M UTC"),
            "note": "Hop health comes from ServiceNow incidents and changes. Per-request timings need a trace source (see Sources)."}


def _chain(hops: list[dict], name: str) -> list[str]:
    by = {h["name"]: h for h in hops}
    out = [name]
    while by[out[-1]]["parent"]:
        out.append(by[out[-1]]["parent"])
    return list(reversed(out))


def impacted(sn, limit: int = 10) -> list[dict]:
    """Which transactions are affected right now: walk up from the CIs of open P1/P2 incidents to the entry points."""
    try:
        inc = [r for r in sn.open_incidents(limit=200) if _p(r) <= 2 and (r.get("cmdb_ci_raw") or r.get("cmdb_ci"))]
    except ServiceNowError:
        return []
    services = {s["name"] for s in transactions(sn)}
    found: dict[str, dict] = {}
    for r in inc:
        start = r.get("cmdb_ci_raw") or r.get("cmdb_ci")
        frontier, seen, tops = [(start, r.get("cmdb_ci"), [r.get("cmdb_ci")])], {start}, []
        for _ in range(5):
            nxt = []
            for sid, nm, path in frontier:
                try:
                    ups = [u for u in sn.related_cis(sid, with_services=True) if u.get("dir") == "up" and u.get("sys_id") not in seen]
                except ServiceNowError:
                    ups = []
                if not ups or nm in services:
                    tops.append((nm, path))
                for u in ups:
                    seen.add(u["sys_id"])
                    nxt.append((u["sys_id"], u["name"], [u["name"]] + path))
            frontier = nxt
            if not frontier:
                break
        tops += [(nm, path) for _, nm, path in frontier]
        for nm, path in tops:
            if nm not in services and len(path) == 1:
                continue  # an isolated CI with no service above it is not a transaction
            t = found.setdefault(nm, {"transaction": nm, "is_business_service": nm in services, "incidents": [], "paths": []})
            t["incidents"].append({"number": r["number"], "priority": r.get("priority"), "cmdb_ci": r.get("cmdb_ci"),
                                   "short_description": r.get("short_description")})
            if " → ".join(path) not in t["paths"]:
                t["paths"].append(" → ".join(path))
    out = sorted(found.values(), key=lambda t: (min(_p(i) for i in t["incidents"]), -len(t["incidents"])))
    return out[:limit]


TRACE_ENV = ["AIOPS_TEMPO_URL", "AIOPS_JAEGER_URL", "AIOPS_DYNATRACE_URL", "RCA_TEMPO_URL", "RCA_JAEGER_URL"]


def trace_backend() -> dict:
    configured = [v for v in TRACE_ENV if os.getenv(v)]
    if configured:
        return {"state": "planned", "detail": f"{', '.join(configured)} set; trace connector not built yet."}
    return {"state": "not_connected", "detail": "Set AIOPS_TEMPO_URL, AIOPS_JAEGER_URL or AIOPS_DYNATRACE_URL to add per-request timings."}


def find_transaction(sn, txn_id: str) -> dict:
    txn_id = txn_id.strip()
    if len(txn_id) < 4:
        raise ValueError("Give at least 4 characters of the transaction or correlation id.")
    inc, notes = sn.mentions(txn_id)
    cis = sorted({r.get("cmdb_ci") for r in inc if r.get("cmdb_ci")})
    return {"id": txn_id, "incidents": [{"number": r["number"], "priority": r.get("priority"), "state": r.get("state"),
                                         "cmdb_ci": r.get("cmdb_ci"), "short_description": r.get("short_description"),
                                         "assignment_group": r.get("assignment_group"), "assigned_to": r.get("assigned_to"),
                                         "opened": r.get("opened_at_raw")} for r in inc],
            "notes": notes, "cis": cis, "trace_backend": trace_backend(),
            "summary": (f"'{txn_id}' is mentioned in {len(inc)} incident(s)"
                        + (f" ({', '.join(r['number'] for r in inc[:5])})" if inc else "")
                        + f" and {len(notes)} work note(s)/comment(s)"
                        + (f" ({', '.join(sorted({n['number'] for n in notes if n.get('number')})[:5])})" if notes else "")
                        + (f", on {', '.join(cis)}" if cis else "") + "." if inc or notes
                        else f"'{txn_id}' is not mentioned in any incident or work note.")}


def coverage(sn) -> list[dict]:
    svc = len(transactions(sn))
    return [
        {"domain": "cmdb", "label": "CMDB (ServiceNow)", "state": "connected" if svc else "not_connected",
         "detail": f"{svc} business service(s); paths from cmdb_rel_ci" if svc else "No business services readable (cmdb_ci_service)."},
        {"domain": "itsm", "label": "Incidents & changes (ServiceNow)", "state": "connected", "detail": "Hop health"},
        {"domain": "traces", "label": "Traces", **trace_backend()},
        {"domain": "logs", "label": "Logs", "state": "not_connected", "detail": "Search a transaction id in Loki/Splunk (planned)."},
    ]
