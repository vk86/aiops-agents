"""Track one transaction id end to end and say where it failed, and whether the cause is upstream or downstream.

1. Collect spans and log events for the id from every connected source (Splunk logs and AppDynamics snapshots
   first; the trace ids they reveal are then used to query Dynatrace, Jaeger and Tempo).
2. Rebuild the call chain: real parent links when a trace source has them; otherwise order services by first
   appearance and use CMDB "depends on" relations to infer who called whom (marked as inferred).
3. Failure point = the deepest failing hop (nothing below it failed).
   Origin = where the cause most likely is:
     - failure point returned 4xx / validation error  -> its caller sent a bad request (upstream of it)
     - failure point timed out calling a dependency    -> that dependency (slower than the caller's timeout)
     - otherwise                                       -> the failure point itself
4. Direction is relative to the system where the problem was reported (you can pass it; otherwise the CI of the
   ServiceNow incident that mentions the id; otherwise the entry point): downstream if the origin is something it
   calls, upstream if the origin calls it.
5. ServiceNow adds owner, open incidents and recent changes for every hop, and changes on the failing chain shortly
   before the transaction are listed as suspects.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from anomaly_agent.sources.base import SourceError
from incident_agent.servicenow import ServiceNowError

from . import path
from .sources import all_sources

UTC = timezone.utc
TIMEOUT = re.compile(r"time[d\s-]*out|timeout|deadline exceeded", re.I)
BAD_REQUEST = re.compile(r"bad request|validation failed|is required|invalid request|malformed|missing in payload|unprocessable", re.I)


def _ts(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime((s or "")[:26], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def collect(txn_id: str) -> tuple[list[dict], list[dict], list[str]]:
    spans, status, errors, trace_ids = [], [], [], set()
    for src in all_sources():
        got = []
        try:
            got = src.find(txn_id, trace_ids)
        except (SourceError, KeyError, ValueError, TypeError) as e:
            errors.append(f"{src.label}: {e}")
        trace_ids |= {s.trace_id for s in got if s.trace_id}
        spans += [s.dict() for s in got]
        status.append({"name": src.name, "label": src.label, **src.status(), "spans": len(got)})
    return spans, status, errors


def _build(spans: list[dict], sn) -> tuple[list[dict], bool]:
    """Return hop nodes (id, parent, service, ...) with log/snapshot evidence attached, and whether links were inferred."""
    tree = [s for s in spans if s["span_id"] and s["kind"] == "span"]
    loose = [s for s in spans if s not in tree]
    nodes, inferred = [], False
    for s in sorted(tree, key=lambda x: x["start"]):
        nodes.append({**s, "id": s["span_id"], "parent": s["parent_id"] or None, "sources": [s["source"]], "evidence": [], "inferred": False})
    by_service = defaultdict(list)
    for n in nodes:
        by_service[n["service"]].append(n)
    for ev in sorted(loose, key=lambda x: x["start"]):
        cands = by_service.get(ev["service"])
        if cands:  # attach to the span of that service whose time window covers the event, else its outermost span
            t = _ts(ev["start"])

            def covers(n):
                st = _ts(n["start"])
                return bool(t and st and st - timedelta(milliseconds=5) <= t <= st + timedelta(milliseconds=(n.get("duration_ms") or 0) + 50))
            outer = [n for n in cands if not any(m["id"] == n["parent"] for m in cands)]
            covering = sorted([n for n in cands if covers(n)], key=lambda n: -(n.get("duration_ms") or 0))
            best = (covering or outer or cands)[0]
            best["evidence"].append({k: ev[k] for k in ("source", "kind", "start", "status", "error", "http_status", "operation", "link")})
            if ev["source"] not in best["sources"]:
                best["sources"].append(ev["source"])
            if ev["status"] == "error" and best["status"] != "error":
                best.update(status="error", error=ev["error"] or best["error"])
        else:  # a service only seen in logs/snapshots: becomes its own hop, linked later
            n = {**ev, "id": f"{ev['source']}:{ev['service']}", "parent": None, "sources": [ev["source"]], "evidence": [], "inferred": True}
            nodes.append(n)
            by_service[ev["service"]].append(n)
            inferred = True
    # link hops that have no parent (other than the entry) using CMDB "depends on", else time order
    nodes.sort(key=lambda n: n["start"])
    roots = [n for n in nodes if not n["parent"]]
    if len(roots) > 1:
        rel = _depends(sn, {n["service"] for n in nodes})
        for n in roots[1:]:
            callers = [m for m in nodes if m is not n and n["service"] in rel.get(m["service"], set()) and m["start"] <= n["start"]]
            prev = [m for m in nodes if m["start"] <= n["start"] and m is not n and m["service"] != n["service"]]
            parent = (callers or prev or [None])[-1]
            if parent is not None:
                n["parent"], n["inferred"], inferred = parent["id"], True, True
    return nodes, inferred


def _depends(sn, services: set[str]) -> dict[str, set[str]]:
    out = {}
    for s in services:
        try:
            out[s] = {r["name"] for r in sn.related_cis(s if not hasattr(sn, "rows") else s, with_services=True)
                      if r.get("dir") == "down"} if sn.ci_lookup(s) else set()
        except ServiceNowError:
            out[s] = set()
    return out


def _ms(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*ms", text or "")
    return float(m.group(1)) if m else None


def analyse(nodes: list[dict], reported_at: str = "") -> dict:
    by_id = {n["id"]: n for n in nodes}
    kids = defaultdict(list)
    for n in nodes:
        if n["parent"] in by_id:
            kids[n["parent"]].append(n)
    root = next((n for n in nodes if not n["parent"] or n["parent"] not in by_id), nodes[0])

    def depth(n):
        d = 0
        while n["parent"] in by_id and d < 50:
            n, d = by_id[n["parent"]], d + 1
        return d

    def ancestors(n):
        out = []
        while n["parent"] in by_id and len(out) < 50:
            n = by_id[n["parent"]]
            out.append(n)
        return out

    def desc_error(n):
        return any(c["status"] == "error" or desc_error(c) for c in kids[n["id"]])
    t0 = _ts(root["start"])
    for n in nodes:
        n["depth"] = depth(n)
        t = _ts(n["start"])
        n["offset_ms"] = round((t - t0).total_seconds() * 1000, 1) if t and t0 else None
    errors = [n for n in nodes if n["status"] == "error"]
    result = {"entry": root["service"], "failed": bool(errors)}
    if not errors:
        slow = max(nodes, key=lambda n: n.get("duration_ms") or 0)
        result.update(failure_point=None, origin=None, direction=None, propagation=[],
                      verdict=f"Completed successfully through {len({n['service'] for n in nodes})} services; slowest hop "
                              f"{slow['service']} {slow.get('duration_ms') or '?'} ms.")
        return result
    fp = sorted([n for n in errors if not desc_error(n)], key=lambda n: (-n["depth"], n["start"]))[0]
    text = " ".join([fp["error"]] + [e["error"] for e in fp["evidence"]])
    origin, otype, why = fp, "error", f"{fp['service']} raised the error: {fp['error'][:160]}"
    parent = by_id.get(fp["parent"])
    code = fp.get("http_status") or 0
    bad_request = 400 <= code < 500 or (not code and BAD_REQUEST.search(text))
    if bad_request and not TIMEOUT.search(text) and parent:
        origin, otype = parent, "bad_request_from_caller"
        why = (f"{fp['service']} rejected the request ({fp.get('http_status') or 'validation error'}: {fp['error'][:120]}); "
               f"the caller {parent['service']} sent it.")
    elif TIMEOUT.search(text) and kids[fp["id"]]:
        dep = max(kids[fp["id"]], key=lambda c: c.get("duration_ms") or 0)
        limit = _ms(text)
        origin, otype = dep, "slow_dependency"
        why = (f"{fp['service']} timed out" + (f" after {limit:g} ms" if limit else "") + f" calling {dep['service']}, which took "
               f"{dep.get('duration_ms') or '?'} ms" + (" and still completed: the caller's timeout is shorter than the dependency's response time."
                                                        if dep["status"] == "ok" else "."))
    rep = reported_at.strip() or root["service"]
    o_anc = [a["service"] for a in ancestors(origin)]
    rep_nodes = [n for n in nodes if n["service"].lower() == rep.lower()]
    if not rep_nodes:
        direction = "unknown"
    elif origin["service"].lower() == rep.lower():
        direction = "same"
    elif rep.lower() in [a.lower() for a in o_anc]:
        direction = "downstream"
    elif any(origin["id"] in [a["id"] for a in ancestors(r)] for r in rep_nodes):
        direction = "upstream"
    else:
        direction = "other branch"
    chain, n = [], fp
    while n:
        if chain and chain[-1]["service"] == n["service"]:  # several spans in one service: keep the outermost
            chain.pop()
        chain.append({"service": n["service"], "operation": n["operation"], "status": n["status"], "http_status": n.get("http_status"),
                      "error": n["error"][:160], "duration_ms": n.get("duration_ms")})
        n = by_id.get(n["parent"])
    if otype == "slow_dependency":
        chain.insert(0, {"service": origin["service"], "operation": origin["operation"], "status": "slow", "http_status": origin.get("http_status"),
                         "error": f"responded in {origin.get('duration_ms')} ms", "duration_ms": origin.get("duration_ms")})
    for x in nodes:
        x["role"] = "origin" if x is origin else ("failure point" if x is fp else ("affected" if x["status"] == "error" else ""))
    dir_text = {"downstream": f"downstream of {rep}", "upstream": f"upstream of {rep}", "same": f"in {rep} itself",
                "other branch": f"in another branch from {rep}", "unknown": f"(reported system {rep} not in this trace)"}[direction]
    result.update(failure_point=fp["service"], origin={"service": origin["service"], "type": otype, "explanation": why,
                                                       "operation": origin["operation"]},
                  reported_at=rep, direction=direction, propagation=chain,
                  verdict=f"Failed. Origin: {origin['service']} ({otype.replace('_', ' ')}), {dir_text}. {why} "
                          f"Propagated: {' → '.join(c['service'] for c in chain)}.")
    return result


def track(sn, txn_id: str, reported_at: str = "") -> dict:
    txn_id = txn_id.strip()
    if len(txn_id) < 4:
        raise ValueError("Give at least 4 characters of the transaction or correlation id.")
    spans, status, errors = collect(txn_id)
    mentions = path.find_transaction(sn, txn_id)
    rep_src = "you" if reported_at.strip() else ""
    if not reported_at.strip():  # where was it reported? the CI of the incident that mentions the id
        cands = [(i["number"], i.get("cmdb_ci")) for i in mentions["incidents"]]
        for note in mentions["notes"]:
            if note.get("number"):
                try:
                    cands.append((note["number"], sn.get_incident(note["number"]).get("cmdb_ci")))
                except ServiceNowError:
                    pass
        hit = next(((n, ci) for n, ci in cands if ci), None)
        if hit:
            reported_at, rep_src = hit[1], f"ServiceNow {hit[0]}"
    base = {"txn_id": txn_id, "sources": status, "errors": errors, "servicenow": mentions, "checked_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")}
    if not spans:
        connected = [s for s in status if s["state"] in ("connected", "mock")]
        return {**base, "found": False, "hops": [], "services": {},
                "verdict": (f"No spans or logs found for {txn_id} in {', '.join(s['label'] for s in connected)}." if connected
                            else "No trace or log source is connected, so the id cannot be followed across systems yet. ")
                           + (" " + mentions["summary"] if mentions["incidents"] or mentions["notes"] else "")}
    nodes, inferred = _build(spans, sn)
    a = analyse(nodes, reported_at)
    if not rep_src:
        rep_src = "entry point"
    t0 = min((_ts(n["start"]) for n in nodes if _ts(n["start"])), default=datetime.now(UTC))
    services = {}
    for svc in dict.fromkeys(n["service"] for n in nodes):
        try:
            ci = sn.ci_lookup(svc)
        except ServiceNowError:
            ci = None
        if not ci:
            services[svc] = {"in_cmdb": False}
            continue
        sid = ci.get("sys_id_raw") or ci.get("sys_id")
        h = path._hop_health(sn, sid, t0 + timedelta(minutes=5))
        try:
            owner = sn.ci_support_group(sid)
        except ServiceNowError:
            owner = None
        services[svc] = {"in_cmdb": True, "ci": ci.get("name"), "owner": owner, "status": h["status"], "incidents": h["incidents"], "changes": h["changes"]}
    chain = {c["service"] for c in a.get("propagation", [])}
    if (a.get("origin") or {}).get("type") == "bad_request_from_caller":  # the caller's own changes are the suspects
        chain = {a["origin"]["service"]}
    suspects = [{"service": s, **c} for s, v in services.items() if s in chain for c in v.get("changes", [])
                if c.get("hours_ago") is not None and -0.1 <= c["hours_ago"] <= 24]
    if a.get("origin") and not services.get(a["origin"]["service"], {}).get("in_cmdb", True):
        a["origin"]["external"] = True
        a["verdict"] += f" {a['origin']['service']} is not in the CMDB (external or unregistered system)."
    if suspects and a.get("failed"):
        a["verdict"] += " Suspect change: " + "; ".join(f"{c['number']} {c['short_description']} on {c['service']} (ended {round(c['hours_ago'] * 60 - 5)} min before the transaction)" for c in suspects[:3]) + "."
    hops = [{k: n.get(k) for k in ("id", "parent", "service", "operation", "start", "offset_ms", "duration_ms", "status", "error", "http_status",
                                    "depth", "inferred", "sources", "evidence", "role", "link", "trace_id")} for n in sorted(nodes, key=lambda n: n["start"])]
    return {**base, "found": True, "inferred_links": inferred, "trace_ids": sorted({n["trace_id"] for n in nodes if n.get("trace_id")}),
            "reported_at_source": rep_src, **a, "hops": hops, "services": services, "suspect_changes": suspects,
            "started": min(n["start"] for n in nodes)}


def note(r: dict) -> str:
    lines = [f"[AIOps transaction agent] {r['txn_id']}: {r['verdict']}"]
    if r.get("propagation"):
        lines.append("Path: " + " → ".join(f"{c['service']} ({c['status']}{' ' + str(c['http_status']) if c.get('http_status') else ''})" for c in r["propagation"]))
    if r.get("trace_ids"):
        lines.append("Trace ids: " + ", ".join(r["trace_ids"]))
    lines.append("Sources: " + ", ".join(f"{s['label']} ({s['spans']})" for s in r["sources"] if s["spans"]))
    return "\n".join(lines)


SERVICE_LOG_SEARCH = ('search index=* earliest={earliest} latest={latest} (service="{svc}" OR app="{svc}" OR sourcetype="{svc}") '
                      '| eval level=coalesce(level, log_level, severity) '
                      '| table _time host level status message | sort 0 _time | head {limit}')


def service_logs(service: str, around: str = "", minutes: int = 2, txn_id: str = "", limit: int = 200) -> dict:
    """A system's own log lines around a moment (the transaction's time on that system, or now). Lines that mention the
    transaction id are flagged. Source: Splunk (mock stream in SERVICENOW_MODE=mock)."""
    import os
    from anomaly_agent.sources.base import http, mock_mode
    from .sources import _from_epoch
    if not re.fullmatch(r"[\w.\-:/ ]{1,80}", service or ""):
        raise ValueError("Unexpected characters in the service name.")
    minutes = max(1, min(int(minutes), 120))
    t = _ts(around) or datetime.now(UTC)
    window = {"from": (t - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S"),
              "to": (t + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")}
    if mock_mode():
        from .mocktraces import mock_logs
        lines = mock_logs(service, t, txn_id)
        return {"service": service, "source": "Splunk (MOCK)", "state": "mock", "window": window, "lines": lines}
    if not (os.getenv("AIOPS_SPLUNK_URL") and os.getenv("AIOPS_SPLUNK_TOKEN")):
        return {"service": service, "source": "Splunk", "state": "not_connected", "window": window, "lines": [],
                "detail": "Set AIOPS_SPLUNK_URL and AIOPS_SPLUNK_TOKEN to see this system's logs."}
    q = (os.getenv("AIOPS_SPLUNK_SERVICE_SEARCH") or SERVICE_LOG_SEARCH).format(
        earliest=int((t - timedelta(minutes=minutes)).timestamp()), latest=int((t + timedelta(minutes=minutes)).timestamp()),
        svc=service.replace('"', ""), limit=min(int(limit), 500))
    r = http("POST", os.environ["AIOPS_SPLUNK_URL"].rstrip("/") + "/services/search/jobs/export",
             headers={"Authorization": f"Bearer {os.environ['AIOPS_SPLUNK_TOKEN']}"}, data={"search": q, "output_mode": "json"},
             timeout=90, verify=os.getenv("AIOPS_SPLUNK_VERIFY", "true").lower() != "false")
    import json as _json
    lines = []
    for raw in r.text.splitlines():
        try:
            row = _json.loads(raw).get("result")
        except ValueError:
            continue
        if not row:
            continue
        tm = row.get("_time")
        msg = row.get("message") or row.get("_raw") or ""
        lines.append({"time": _from_epoch(float(tm)) if tm and str(tm).replace(".", "").isdigit() else str(tm or "")[:23],
                      "level": str(row.get("level") or "INFO").upper(), "host": row.get("host") or "", "message": msg[:500],
                      "this_txn": bool(txn_id and txn_id in msg)})
    return {"service": service, "source": "Splunk", "state": "connected", "window": window, "lines": lines}


def coverage() -> list[dict]:
    return [{"name": s.name, "label": s.label, **s.status()} for s in all_sources()]
