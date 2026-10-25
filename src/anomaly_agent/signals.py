"""Service anomalies from observability signals + your predictive model, with ServiceNow as context and action.

detect():
  1. collect signals from every connected source (Dynatrace, AppDynamics, Splunk) for the window
  2. build one feature vector per service (error/latency ratios, KPI drop, problems, violations, new error
     signatures, sources agreeing) and add ServiceNow context features (change window, open incidents)
  3. score with the predictive model (or rank by source agreement if the model is not connected)
  4. decide per service, using ServiceNow:
       suppressed - anomalous, but an approved change window is open on the CI (expected)
       attach     - anomalous and an incident is already open on the CI: add evidence to it
       raise      - anomalous, nothing open: send an event to ServiceNow Event Management
       watch      - signals present but not anomalous
ServiceNow also supplies the CMDB owner (support group) and the business services above the CI (blast radius).
Detections are logged to data/anomaly_log.jsonl so evaluate() can score them against later incidents, and
labels() exports P1/P2 incidents as training labels for the model.
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from incident_agent.insights import ts
from incident_agent.servicenow import ServiceNowError

from .sources import PredictiveModel, SourceError, mock_mode, observability_sources
from .sources.model import FEATURES

UTC = timezone.utc
DECISION_ORDER = {"raise": 0, "attach": 1, "suppressed": 2, "watch": 3}
SEV_RANK = {"critical": 3, "major": 2, "minor": 1, "info": 0}
LOG = Path(os.getenv("AIOPS_ANOMALY_LOG", "data/anomaly_log.jsonl"))
_cache: dict = {}
_lock = threading.Lock()


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _p(r: dict) -> int:
    try:
        return int(str(r.get("priority") or "5").strip()[:1])
    except ValueError:
        return 5


def coverage() -> list[dict]:
    rows = [{"name": "model", "label": PredictiveModel.label, "domains": "score", **PredictiveModel().status()}]
    for s in observability_sources():
        rows.append({"name": s.name, "label": s.label, "domains": ", ".join(s.domains), **s.status()})
    rows.append({"name": "servicenow", "label": "ServiceNow", "domains": "context, labels, action", "state": "connected",
                 "detail": "CMDB owner & business services, changes, open incidents, Event Management events, training labels"})
    return rows


def features(sigs: list[dict], ctx: dict) -> dict:
    f = {k: 0.0 for k, _ in FEATURES}
    f["minutes_since_change"] = -1.0
    for s in sigs:
        if not s["anomalous"] and s["feature"] not in ("error_rate_pct", "p95_ms"):
            continue
        if s["feature"] == "error_rate_pct" and s["value"] is not None:
            f["error_rate_pct"] = max(f["error_rate_pct"], s["value"])
            f["error_rate_ratio"] = max(f["error_rate_ratio"], s["value"] / max(s["baseline"] or 0, 0.05))
        elif s["feature"] == "p95_ms" and s["value"] is not None:
            f["p95_ms"] = max(f["p95_ms"], s["value"])
            f["p95_ratio"] = max(f["p95_ratio"], s["value"] / max(s["baseline"] or 0, 1))
        elif s["feature"] == "kpi_drop_pct":
            f["kpi_drop_pct"] = max(f["kpi_drop_pct"], float(s["extra"].get("drop_pct") or 0))
        elif s["feature"] == "error_log_ratio" and s["value"] is not None:
            f["error_log_ratio"] = max(f["error_log_ratio"], s["value"] / max(s["baseline"] or 0, 1))
        elif s["feature"] in ("vendor_problems", "health_violations", "new_error_signatures"):
            f[s["feature"]] += 1
        if s["anomalous"] and s["severity"] == "critical":
            f["critical_signals"] += 1
    f["sources_agreeing"] = float(len({s["source"] for s in sigs if s["anomalous"]}))
    f["change_active"] = 1.0 if ctx.get("change_window") else 0.0
    if ctx.get("minutes_since_change") is not None:
        f["minutes_since_change"] = float(ctx["minutes_since_change"])
    f["open_incidents"] = float(len(ctx.get("open_incidents", [])))
    return {k: round(v, 2) for k, v in f.items()}


def _context(sn, service: str, now: datetime) -> dict:
    ctx = {"ci": None, "owner": None, "business_services": [], "open_incidents": [], "changes": [],
           "change_window": None, "minutes_since_change": None, "in_cmdb": False}
    try:
        ci = sn.ci_lookup(service)
    except ServiceNowError:
        ci = None
    if not ci:
        return ctx
    sid = ci.get("sys_id_raw") or ci.get("sys_id")
    ctx.update(ci=ci.get("name"), in_cmdb=True)
    try:
        ctx["owner"] = sn.ci_support_group(sid)
    except ServiceNowError:
        pass
    try:
        ctx["open_incidents"] = [{"number": r["number"], "priority": r.get("priority"), "state": r.get("state"),
                                  "short_description": r.get("short_description"), "assignment_group": r.get("assignment_group")}
                                 for r in sn.incidents_on_ci(sid, active_only=True)]
    except ServiceNowError:
        pass
    try:
        for ch in sn.changes_near([sid], limit=30):
            st, en = ts(ch.get("start_raw")), ts(ch.get("end_raw"))
            if st and en and st <= now <= en:
                ctx["change_window"] = {"number": ch.get("number"), "short_description": ch.get("short_description"),
                                        "state": ch.get("state"), "start": _fmt(st), "end": _fmt(en)}
            if en and en <= now and now - en <= timedelta(hours=24):
                m = int((now - en).total_seconds() // 60)
                if ctx["minutes_since_change"] is None or m < ctx["minutes_since_change"]:
                    ctx["minutes_since_change"] = m
                ctx["changes"].append({"number": ch.get("number"), "short_description": ch.get("short_description"),
                                       "ended": _fmt(en), "minutes_ago": m})
    except ServiceNowError:
        pass
    ctx["business_services"] = _services_above(sn, sid)
    return ctx


def _services_above(sn, sid: str, depth: int = 5) -> list[str]:
    try:
        services = {s.get("name") for s in sn.business_services()}
    except ServiceNowError:
        services = set()
    frontier, seen, tops = [sid], {sid}, []
    for _ in range(depth):
        nxt = []
        for x in frontier:
            try:
                ups = [u for u in sn.related_cis(x, with_services=True) if u.get("dir") == "up" and u.get("sys_id") not in seen]
            except ServiceNowError:
                ups = []
            for u in ups:
                seen.add(u["sys_id"])
                nxt.append(u["sys_id"])
                if u["name"] in services:
                    tops.append(u["name"])
        frontier = nxt
        if not frontier:
            break
    return sorted(set(tops))


def detect(sn, window_minutes: int = 60, now: datetime | None = None, use_cache: bool = True) -> dict:
    window_minutes = max(5, min(int(window_minutes), 24 * 60))
    key = (window_minutes, id(sn))
    with _lock:
        hit = _cache.get(key)
        if use_cache and now is None and hit and time.time() - hit[0] < 60:
            return hit[1]
    now = now or datetime.now(UTC)
    start = now - timedelta(minutes=window_minutes)
    signals, errors = [], []
    for src in observability_sources():
        try:
            signals += [s.dict() for s in src.collect(window_minutes)]
        except (SourceError, KeyError, ValueError) as e:
            errors.append(f"{src.label}: {e}")
    by = defaultdict(list)
    for s in signals:
        by[s["service"]].append(s)
    rows = []
    for svc, sigs in by.items():
        ctx = _context(sn, svc, now)
        rows.append({"service": svc, "signals": sorted(sigs, key=lambda s: (-s["anomalous"], -SEV_RANK.get(s["severity"], 0))),
                     "context": ctx, "features": features(sigs, ctx)})
    model = PredictiveModel()
    try:
        scored = model.score(_fmt(start), _fmt(now), [{"service": r["service"], "ci": r["context"]["ci"] or r["service"],
                                                         "features": r["features"]} for r in rows])
    except SourceError as e:
        errors.append(str(e))
        scored = {"available": False, "model_version": None, "results": {}}
    for r in rows:
        f, ctx = r["features"], r["context"]
        m = scored["results"].get(r["service"])
        r["model"] = m
        agree = int(f["sources_agreeing"])
        if m:
            anomalous = m["anomaly"]
            confidence = round(0.75 * m["score"] + 0.25 * min(agree, 3) / 3, 2)
        else:  # no model: two independent sources, or one critical signal, make it an anomaly
            anomalous = agree >= 2 or f["critical_signals"] >= 1
            confidence = round(min(1.0, 0.3 * agree + 0.2 * min(f["critical_signals"], 2)), 2)
        if not anomalous:
            decision, why = "watch", "Signals present but below the anomaly threshold."
        elif ctx["change_window"]:
            c = ctx["change_window"]
            decision, why = "suppressed", f"Expected: change {c['number']} window is open until {c['end']} UTC. Re-check after it closes."
        elif ctx["open_incidents"]:
            i = min(ctx["open_incidents"], key=_p)
            decision, why = "attach", f"{i['number']} is already open on {ctx['ci']}: add the evidence there instead of a new incident."
        else:
            decision, why = "raise", "Nothing open on this CI: send an event to ServiceNow Event Management."
        r.update(anomalous=anomalous, confidence=confidence, decision=decision, decision_reason=why,
                 sources=sorted({s["source"] for s in r["signals"] if s["anomalous"]}),
                 headline=_headline(r))
    rows.sort(key=lambda r: (DECISION_ORDER[r["decision"]], -r["confidence"]))
    out = {"window_minutes": window_minutes, "window_start": _fmt(start), "window_end": _fmt(now),
           "model": {"available": scored["available"], "version": scored["model_version"], **model.status()},
           "signals": len(signals), "services": rows, "errors": errors, "coverage": coverage(),
           "mock": mock_mode(),
           "summary": {d: sum(1 for r in rows if r["decision"] == d) for d in DECISION_ORDER}}
    _log(out)
    with _lock:
        _cache[key] = (time.time(), out)
    return out


def _headline(r: dict) -> str:
    top = [s for s in r["signals"] if s["anomalous"]][:3]
    parts = []
    for s in top:
        if s["value"] is not None and s["baseline"] not in (None, 0):
            parts.append(f"{s['name']} {s['value']:g}{s['unit']} vs {s['baseline']:g}{s['unit']}")
        else:
            parts.append(s["name"])
    return "; ".join(parts) or "no anomalous signals"


def service(sn, name: str, window_minutes: int = 60) -> dict:
    d = detect(sn, window_minutes)
    for r in d["services"]:
        if r["service"].lower() == name.strip().lower():
            return {**r, "window_start": d["window_start"], "window_end": d["window_end"], "model_status": d["model"]}
    raise ValueError(f"No signals for '{name}' in the last {window_minutes} minutes.")


# ------------------------------------------------------------------ actions on ServiceNow
SN_SEVERITY = {"critical": "1", "major": "2", "minor": "3", "info": "5"}


def event_fields(r: dict) -> dict:
    worst = max((s["severity"] for s in r["signals"] if s["anomalous"]), key=lambda x: SEV_RANK.get(x, 0), default="minor")
    m = r.get("model") or {}
    hour = datetime.now(UTC).strftime("%Y%m%d%H")
    info = {"confidence": r["confidence"], "model_score": m.get("score"), "top_features": m.get("top_features"),
            "sources": r["sources"], "business_services": r["context"]["business_services"],
            "signals": [{k: s[k] for k in ("source", "name", "value", "baseline", "unit", "severity", "link")} for s in r["signals"][:8]]}
    return {"source": "AIOps anomaly agent", "node": r["context"]["ci"] or r["service"], "type": "Anomaly",
            "resource": r["service"], "metric_name": (m.get("top_features") or [{}])[0].get("name") or "multi-signal",
            "severity": SN_SEVERITY[worst], "message_key": f"aiops-anomaly-{r['service']}-{hour}",
            "description": f"Anomaly on {r['service']}: {r['headline']}. " + (m.get("explanation") or ""),
            "additional_info": json.dumps(info, default=str)}


def evidence_note(r: dict) -> str:
    m = r.get("model") or {}
    lines = [f"[AIOps anomaly agent] {r['service']}: {r['headline']}.",
             f"Confidence {r['confidence']:.0%}" + (f", model score {m['score']} ({m.get('explanation', '')})" if m else "") + ".",
             "Signals:"] + [f"- {s['source']}: {s['name']}" + (f" {s['value']:g}{s['unit']} vs {s['baseline']:g}{s['unit']}"
                                                                 if s["value"] is not None and s["baseline"] is not None else "")
                            + (f" ({s['detail'][:90]})" if s["detail"] else "") for s in r["signals"] if s["anomalous"]][:10]
    if r["context"]["changes"]:
        lines.append("Recent changes: " + ", ".join(f"{c['number']} ({c['minutes_ago']} min ago)" for c in r["context"]["changes"]))
    return "\n".join(lines)


# ------------------------------------------------------------------ detection log, evaluation, labels
def _log(out: dict) -> None:
    rows = [{"at": out["window_end"], "service": r["service"], "ci": r["context"]["ci"], "decision": r["decision"],
             "confidence": r["confidence"], "score": (r.get("model") or {}).get("score")}
            for r in out["services"] if r["anomalous"]]
    if not rows:
        return
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            for x in rows:
                fh.write(json.dumps(x) + "\n")
    except OSError:
        pass


def evaluate(sn, days: int = 30, horizon_hours: int = 6) -> dict:
    """Score logged detections against P1/P2 incidents on the same CI: confirmed (incident within the horizon, with
    lead time = minutes from detection to the incident being opened), unconfirmed, and incidents with no detection."""
    try:
        logged = [json.loads(x) for x in LOG.read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        logged = []
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    first: dict[str, dict] = {}
    for d in logged:  # first detection per CI per hour is enough
        t = ts(d["at"])
        if not t or t < since or not d.get("ci"):
            continue
        k = f"{d['ci']}|{t:%Y%m%d%H}"
        first.setdefault(k, {**d, "t": t})
    detections = list(first.values())
    if detections:  # only incidents after monitoring started can count as missed
        since = max(since, min(d["t"] for d in detections) - timedelta(hours=horizon_hours))
    inc = [r for r in sn.opened_between(_fmt(since), _fmt(now + timedelta(hours=1))) if _p(r) <= 2 and r.get("cmdb_ci")]
    confirmed, leads, matched = 0, [], set()
    for d in detections:
        hits = [r for r in inc if r.get("cmdb_ci") == d["ci"] and ts(r.get("opened_at_raw"))
                and d["t"] - timedelta(hours=horizon_hours) <= ts(r["opened_at_raw"]) <= d["t"] + timedelta(hours=horizon_hours)]
        if hits:
            confirmed += 1
            h = min(hits, key=lambda r: r["opened_at_raw"])
            matched.update(r["number"] for r in hits)
            leads.append(round((ts(h["opened_at_raw"]) - d["t"]).total_seconds() / 60))
    watched = {d["ci"] for d in detections}
    missed = [r["number"] for r in inc if r["number"] not in matched and r.get("cmdb_ci") in watched]
    n = len(detections)
    return {"days": days, "horizon_hours": horizon_hours, "detections": n, "confirmed": confirmed,
            "precision": round(confirmed / n, 2) if n else None,
            "recall_on_watched_cis": round(len(matched) / (len(matched) + len(missed)), 2) if matched or missed else None,
            "median_lead_minutes": sorted(leads)[len(leads) // 2] if leads else None,
            "lead_note": "Positive = detected before the incident was opened; negative = after.",
            "missed_incidents": missed[:20], "log": str(LOG)}


LABEL_FIELDS = ["number", "service", "priority", "start_utc", "end_utc", "close_code", "label", "assignment_group", "short_description"]
NOT_REAL = ("not reproducible", "false", "duplicate", "canceled", "cancelled", "closed/resolved by caller")


def labels(sn, days: int = 90) -> list[dict]:
    """Training labels from ServiceNow: P1/P2 incidents with a CI give anomaly windows per service.
    label=0 where the close code says it was not a real issue (false positive, not reproducible, duplicate)."""
    now = datetime.now(UTC)
    out = []
    for r in sn.opened_between(_fmt(now - timedelta(days=days)), _fmt(now)):
        if _p(r) > 2 or not r.get("cmdb_ci"):
            continue
        code = (r.get("close_code") or "").lower()
        out.append({"number": r["number"], "service": r.get("cmdb_ci"), "priority": r.get("priority"),
                    "start_utc": r.get("opened_at_raw"), "end_utc": r.get("resolved_at_raw") or "",
                    "close_code": r.get("close_code") or "", "label": 0 if any(x in code for x in NOT_REAL) or r.get("state") == "Canceled" else 1,
                    "assignment_group": r.get("assignment_group"), "short_description": r.get("short_description")})
    return out


def labels_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=LABEL_FIELDS)
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in LABEL_FIELDS})
    return buf.getvalue()
