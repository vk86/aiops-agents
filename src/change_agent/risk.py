"""Change guard: score a change before it ships, and verify it after.

Risk factors (points add up to a 0-100 score; some are hard holds):
  blast radius     business services above the CI in the CMDB, critical services, dependents
  error budget     the CI's SLO policy: freeze (hard hold) / restrict / caution
  change history   the CI's change failure rate (changes followed by a P1/P2 on the CI within 24h) and similar past
                   failed changes
  current health   open P1/P2 on the CI (hard hold), anomalies the anomaly agent wants raised/attached
  collisions       other changes on the CI or its neighbours overlapping the window
  timing           freeze windows (hard hold) and peak business hours
  hygiene          missing backout or test plan, emergency type, ServiceNow risk
A change that restores reliability (rollback, revert, restore, fix, renew/rotate certificate, timeout increase) is a
reliability fix: hard holds become "canary with SRE approval" because shipping it reduces risk.
Recommendation: HOLD (hard hold, or score >= 60), CANARY (30-59), GO (< 30), each with guardrails.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

from anomaly_agent.sources.base import load_config, mock_mode
from incident_agent.insights import similarity, ts
from incident_agent.servicenow import ServiceNowError

UTC = timezone.utc
FIX = re.compile(r"\b(roll ?back|revert|restore|hot ?fix|fix(es|ed)?|renew|rotate|replace (the )?expired|increase .*timeout)\b", re.I)
SEV = {"critical": 3, "high": 2, "medium": 1, "low": 0}


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _p(r: dict) -> int:
    try:
        return int(str(r.get("priority") or "5").strip()[:1])
    except ValueError:
        return 5


def policy_config(now: datetime | None = None) -> dict:
    cfg = load_config().get("change_policy", {})
    now = now or datetime.now(UTC)
    freezes = cfg.get("freeze_windows")
    if freezes is None and mock_mode():  # MOCK: open enrollment freeze for member-facing enrollment systems
        y = now.year
        freezes = [{"name": "Open enrollment", "from": f"{y}-10-20", "to": f"{y}-12-15",
                    "services": ["Member Enrollment"], "allow": "reliability fixes only"}]
    return {"freeze_windows": freezes or [], "peak_hours": cfg.get("peak_hours", os.getenv("AIOPS_PEAK_HOURS", "08-18")),
            "peak_days": cfg.get("peak_days", "Mon-Fri"), "timezone": cfg.get("timezone", os.getenv("SHIFT_TZ", "America/Chicago")),
            "critical_services": cfg.get("critical_services", ["Member Enrollment", "Premium Payments", "Claims Processing"] if mock_mode() else []),
            "thresholds": {"hold": 60, "canary": 30}}


def history_stats(sn, ci_sys_id: str, days: int = 90, now: datetime | None = None) -> dict:
    """Change failure rate: share of the CI's changes followed by a P1/P2 incident on the same CI within 24h."""
    now = now or datetime.now(UTC)
    if not ci_sys_id:
        return {}
    try:
        chg = [c for c in sn.change_history(ci_sys_id, days) if ts(c.get("end_raw")) and ts(c["end_raw"]) <= now]
        inc = [r for r in sn.incidents_on_ci(ci_sys_id, active_only=False, limit=300) if _p(r) <= 2]
    except ServiceNowError:
        return {}
    failed = []
    for c in chg:
        en = ts(c["end_raw"])
        hit = next((r for r in inc if ts(r.get("opened_at_raw")) and en <= ts(r["opened_at_raw"]) <= en + timedelta(hours=24)), None)
        if hit or str(c.get("close_code") or "").lower().startswith("unsuccess"):
            failed.append({"number": c.get("number"), "short_description": c.get("short_description"), "ended": c["end_raw"],
                           "incident": hit["number"] if hit else None})
    n = len(chg)
    return {"changes": n, "failed": len(failed), "change_failure_rate": round(len(failed) / n, 2) if n else None,
            "deploys_per_week": round(n / (days / 7), 1) if n else 0.0, "failed_changes": failed[:10]}


def _in_peak(t: datetime, pol: dict) -> bool:
    try:
        from zoneinfo import ZoneInfo
        lt = t.astimezone(ZoneInfo(pol["timezone"]))
    except Exception:
        lt = t
    a, b = [int(x) for x in str(pol["peak_hours"]).split("-")]
    weekday_only = str(pol.get("peak_days", "Mon-Fri")).lower().startswith("mon-fri")
    return (not weekday_only or lt.weekday() < 5) and a <= lt.hour < b


def assess(sn, number: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    c = sn.change_row(number)
    pol = policy_config(now)
    ci_name = c.get("cmdb_ci") or ""
    try:
        ci = sn.ci_lookup(ci_name) if ci_name else None
    except ServiceNowError:
        ci = None
    sid = (ci or {}).get("sys_id_raw") or (ci or {}).get("sys_id") or c.get("cmdb_ci_raw") or ""
    st, en = ts(c.get("start_raw")), ts(c.get("end_raw"))
    text = f"{c.get('short_description', '')} {c.get('description', '')}"
    fix = bool(FIX.search(text))
    factors, holds = [], []

    def add(key, label, points, detail, evidence=None, hard=False):
        factors.append({"key": key, "label": label, "points": points, "detail": detail, "evidence": evidence or [], "hard_hold": hard})
        if hard:
            holds.append(label)

    # blast radius
    services, dependents = [], 0
    if sid:
        from anomaly_agent.signals import _services_above
        try:
            services = _services_above(sn, sid)
            dependents = sum(1 for r in sn.related_cis(sid, with_services=True) if r.get("dir") == "up")
        except ServiceNowError:
            pass
    crit = [s for s in services if s in pol["critical_services"]]
    pts = min(15, 5 * len(services)) + (10 if crit else 0) + min(5, dependents * 2)
    add("blast", "Blast radius", pts, (f"Affects {', '.join(services)}" if services else "No business service above this CI in the CMDB")
        + (f"; critical: {', '.join(crit)}" if crit else "") + f"; {dependents} direct dependent(s).", services)
    # error budget
    from slo_agent.slo import for_service
    slos = [s for s in for_service(sn, ci_name, now) if s.get("available")]
    worst = min(slos, key=lambda s: s["remaining_pct"], default=None)
    if worst:
        lvl = worst["policy"]["level"]
        pts = {"freeze": 40, "restrict": 25, "caution": 10}.get(lvl, 0)
        add("budget", "Error budget", pts, f"{worst['name']}: {worst['remaining_pct']}% budget left ({lvl}); burn 1h {worst['burn'].get('1h')}×.",
            [worst["name"]], hard=(lvl == "freeze"))
    else:
        add("budget", "Error budget", 0, "No SLO defined for this CI.")
    # history
    hs = history_stats(sn, sid, 90, now) if sid else {}
    cfr = hs.get("change_failure_rate")
    similar = [f for f in hs.get("failed_changes", []) if similarity({"short_description": f["short_description"] or ""},
                                                                      {"short_description": c.get("short_description", "")})[0] >= 0.25]
    pts = min(25, int((cfr or 0) * 60)) + (10 if similar else 0)
    add("history", "Change history", pts, (f"Change failure rate {round((cfr or 0) * 100)}% ({hs.get('failed', 0)} of {hs.get('changes', 0)} in 90 days)"
                                           if hs.get("changes") else "No change history on this CI")
        + (f"; similar failed change: {similar[0]['number']} {similar[0]['short_description']}" if similar else "") + ".",
        [f["number"] for f in hs.get("failed_changes", [])][:5])
    # current health
    open_inc = []
    if sid:
        try:
            open_inc = sn.incidents_on_ci(sid, active_only=True)
        except ServiceNowError:
            pass
    p12 = [r for r in open_inc if _p(r) <= 2]
    anomalies = []
    try:
        from anomaly_agent import signals
        anomalies = [r for r in signals.detect(sn, 60)["services"] if r["service"] == ci_name and r["decision"] in ("raise", "attach")]
    except Exception:
        pass
    pts = (25 if p12 else 0) + (5 if open_inc and not p12 else 0) + (15 if anomalies else 0)
    add("health", "Current health", pts, (f"Open P1/P2 on the CI: {', '.join(r['number'] for r in p12)}" if p12 else
                                          f"{len(open_inc)} open lower-priority incident(s)" if open_inc else "No open incidents on the CI")
        + (f"; anomaly agent: {anomalies[0]['decision']} ({anomalies[0]['headline']})" if anomalies else "") + ".",
        [r["number"] for r in open_inc][:5], hard=bool(p12))
    # collisions
    coll = []
    if st and en:
        try:
            neigh = {ci_name} | {r["name"] for r in (sn.related_cis(sid, with_services=True) if sid else [])}
            pool = sn.upcoming_changes(14) + sn.active_changes()
        except ServiceNowError:
            neigh, pool = {ci_name}, []
        seen = set()
        for o in pool:
            if o.get("number") in (c.get("number"), *seen) or o.get("cmdb_ci") not in neigh:
                continue
            os_, oe = ts(o.get("start_raw")), ts(o.get("end_raw"))
            if os_ and oe and os_ <= en + timedelta(hours=2) and oe >= st - timedelta(hours=2):
                seen.add(o["number"])
                coll.append(f"{o['number']} {o.get('short_description', '')} ({o.get('cmdb_ci')})")
    add("collision", "Collisions", min(20, 10 * len(coll)), ("Overlaps: " + "; ".join(coll)) if coll else "No overlapping changes on this CI or its neighbours.", coll)
    # timing
    tpts, tdet, frz = 0, [], None
    if st:
        for f in pol["freeze_windows"]:
            if f["from"] <= _fmt(st)[:10] <= f["to"] and (not f.get("services") or set(f["services"]) & set(services) or ci_name in f.get("services", [])):
                frz = f
        if frz:
            tpts += 30
            tdet.append(f"inside the {frz['name']} freeze ({frz['from']} to {frz['to']}, {frz.get('allow', 'no changes')})")
        if _in_peak(st, pol):
            tpts += 10
            tdet.append(f"starts in peak hours ({pol['peak_hours']} {pol['timezone']})")
    add("timing", "Timing", tpts, ("Starts " + _fmt(st) + " UTC, " + "; ".join(tdet) if tdet else f"Starts {_fmt(st)} UTC, off-peak, no freeze") if st else "No planned start date.",
        [frz["name"]] if frz else [], hard=bool(frz))
    # hygiene
    hy, hd = 0, []
    if not (c.get("backout_plan") or "").strip():
        hy += 10
        hd.append("no backout plan")
    if not (c.get("test_plan") or "").strip():
        hy += 5
        hd.append("no test plan")
    if str(c.get("type", "")).lower() == "emergency":
        hy += 5
        hd.append("emergency change")
    risk = str(c.get("risk") or "").lower()
    hy += 10 if risk.startswith("high") else (5 if risk.startswith("moderate") else 0)
    if risk:
        hd.append(f"ServiceNow risk {c.get('risk')}")
    add("hygiene", "Change hygiene", hy, ("; ".join(hd) or "Backout and test plans present") + ".")
    score = min(100, sum(f["points"] for f in factors) - (15 if fix else 0))
    score = max(score, 0)
    th = pol["thresholds"]
    if holds and not fix:
        rec, why = "HOLD", "Hard hold: " + ", ".join(holds) + "."
    elif holds and fix:
        rec, why = "CANARY", "Reliability fix during " + ", ".join(holds).lower() + ": ship it, but as a canary with SRE approval."
    elif score >= th["hold"]:
        rec, why = "HOLD", f"Risk score {score} ≥ {th['hold']}."
    elif score >= th["canary"]:
        rec, why = "CANARY", f"Risk score {score}: ship progressively with rollback triggers."
    else:
        rec, why = "GO", f"Risk score {score}: low risk."
    return {"number": c.get("number"), "short_description": c.get("short_description"), "ci": ci_name, "type": c.get("type"),
            "risk_field": c.get("risk"), "state": c.get("state"), "start": c.get("start_raw"), "end": c.get("end_raw"),
            "assignment_group": c.get("assignment_group"), "reliability_fix": fix, "score": score, "recommendation": rec, "why": why,
            "factors": sorted(factors, key=lambda f: -f["points"]), "hard_holds": holds, "business_services": services,
            "slo": worst, "history": hs, "open_incidents": [{"number": r["number"], "priority": r.get("priority"),
                                                            "short_description": r.get("short_description")} for r in open_inc][:6],
            "guardrails": guardrails(rec, ci_name, worst, services, anomalies), "assessed_at": _fmt(now) + " UTC"}


def guardrails(rec: str, ci: str, slo: dict | None, services: list[str], anomalies: list) -> dict:
    watch = [f"{slo['name']} burn rate (1h, 5m)"] if slo else []
    watch += ["error rate and p95 latency on " + ci] + [f"{s} transaction health" for s in services[:2]]
    triggers = (["SLO burn rate ≥ 14.4× over 1h and 5m (page-level burn)"] if slo else []) + \
               ["error rate ≥ 2× its pre-change baseline for 10 min", "a new error signature in logs", "a new P1/P2 incident on the CI or its dependents"]
    steps = {"GO": ["Deploy", "Watch for 30 min", "Run verify_change"],
             "CANARY": ["Deploy to 5% (or one node) and hold 15 min", "25% and hold 30 min", "100% only if no trigger fired", "Run verify_change"],
             "HOLD": ["Do not deploy in this window", "Resolve the hard holds or reschedule", "Re-assess before the new window"]}[rec]
    pre = ["Backout plan tested in stage", "No open P1/P2 on dependencies", "On-call for " + ci + " aware of the window"]
    return {"plan": steps, "watch": watch, "rollback_triggers": triggers, "pre_checks": pre}


def upcoming(sn, days: int = 7, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    try:
        rows = sn.upcoming_changes(days)
        note = None
        if not rows:
            rows, note = sn.active_changes(), f"No changes start in the next {days} days; showing the latest active changes."
    except ServiceNowError as e:
        return {"changes": [], "note": str(e)}
    out = []
    for c in rows[:25]:
        try:
            a = assess(sn, c["number"], now)
            out.append({k: a[k] for k in ("number", "short_description", "ci", "start", "score", "recommendation", "why", "reliability_fix", "type")})
        except ServiceNowError as e:
            out.append({"number": c.get("number"), "short_description": c.get("short_description"), "error": str(e)})
    order = {"HOLD": 0, "CANARY": 1, "GO": 2}
    out.sort(key=lambda r: (order.get(r.get("recommendation"), 3), r.get("start") or ""))
    return {"changes": out, "note": note, "policy": policy_config(now), "checked_at": _fmt(now) + " UTC"}


def verify(sn, number: str, now: datetime | None = None) -> dict:
    """After a change: did the SLI get worse, did incidents follow on the CI or what depends on it?"""
    now = now or datetime.now(UTC)
    c = sn.change_row(number)
    st, en = ts(c.get("start_raw")), ts(c.get("end_raw"))
    if not en or en > now:
        return {"number": c.get("number"), "verdict": "not finished", "detail": "The change has not ended yet; verify after it closes."}
    ci_name = c.get("cmdb_ci") or ""
    try:
        ci = sn.ci_lookup(ci_name)
    except ServiceNowError:
        ci = None
    sid = (ci or {}).get("sys_id_raw") or (ci or {}).get("sys_id") or ""
    from slo_agent.slo import definitions, source_for
    sli = []
    for s in [d for d in definitions() if d["service"] == ci_name]:
        src = source_for(s, now)
        if not src:
            continue
        budget = 1 - float(s["objective"]) / 100
        b = src.bad_ratio(s, st - timedelta(hours=6), st)
        a = src.bad_ratio(s, en, min(now, en + timedelta(hours=6)))
        if a is None or b is None:
            continue
        sli.append({"slo": s["name"], "burn_before": round(b / budget, 2), "burn_after": round(a / budget, 2),
                    "regressed": a > max(3 * b, budget)})
    incs = []
    if sid:
        cis = [sid] + [r["sys_id"] for r in sn.related_cis(sid, with_services=True) if r.get("dir") == "up"]
        for x in cis:
            try:
                for r in sn.incidents_on_ci(x, active_only=False, limit=100):
                    t = ts(r.get("opened_at_raw"))
                    if t and en <= t <= en + timedelta(hours=24):
                        incs.append({"number": r["number"], "priority": r.get("priority"), "cmdb_ci": r.get("cmdb_ci"),
                                     "short_description": r.get("short_description"), "minutes_after": int((t - en).total_seconds() // 60)})
            except ServiceNowError:
                pass
    bad = any(x["regressed"] for x in sli) or any(_p(i) <= 2 for i in incs)
    verdict = "regression suspected" if bad else "healthy"
    return {"number": c.get("number"), "short_description": c.get("short_description"), "ci": ci_name, "ended": c.get("end_raw"),
            "verdict": verdict, "sli": sli, "incidents_after": sorted(incs, key=lambda i: i["minutes_after"]),
            "detail": ("SLI burn rose after the change" if any(x["regressed"] for x in sli) else "")
                      + ("; " if any(x["regressed"] for x in sli) and incs else "")
                      + (f"{len(incs)} incident(s) within 24h on the CI or its dependents" if incs else ("" if bad else "No SLI regression and no incidents within 24h."))}


def dora(sn, days: int = 90, now: datetime | None = None) -> list[dict]:
    from slo_agent.slo import definitions
    now = now or datetime.now(UTC)
    services = list(dict.fromkeys([d["service"] for d in definitions()] + [c.get("cmdb_ci") for c in sn.upcoming_changes(14) if c.get("cmdb_ci")]))
    out = []
    for svc in services:
        try:
            ci = sn.ci_lookup(svc)
        except ServiceNowError:
            ci = None
        sid = (ci or {}).get("sys_id_raw") or (ci or {}).get("sys_id")
        hs = history_stats(sn, sid, days, now) if sid else {}
        try:
            inc = [r for r in sn.incidents_on_ci(sid, active_only=False, limit=300) if _p(r) <= 2 and ts(r.get("resolved_at_raw"))
                   and ts(r["opened_at_raw"]) >= now - timedelta(days=days)] if sid else []
        except ServiceNowError:
            inc = []
        mttr = [(ts(r["resolved_at_raw"]) - ts(r["opened_at_raw"])).total_seconds() / 3600 for r in inc]
        out.append({"service": svc, "deploys_per_week": hs.get("deploys_per_week"), "change_failure_rate": hs.get("change_failure_rate"),
                    "changes": hs.get("changes"), "failed": hs.get("failed"), "mttr_h": round(sum(mttr) / len(mttr), 1) if mttr else None,
                    "failed_changes": hs.get("failed_changes", [])[:5]})
    return sorted(out, key=lambda r: -(r["change_failure_rate"] or 0))
