"""SLOs and error budgets.

Everything is computed from one question asked of an SLI source: "what fraction of events were bad between t1 and
t2?" (bad_ratio). With that:
  attainment        = 1 - bad_ratio(window)
  error budget      = 1 - objective
  budget consumed   = bad_ratio(window) / budget          (100% = budget spent)
  burn rate (w)     = bad_ratio(last w) / budget          (1 = spending exactly the budget over the window)
  alerts            multi-window, multi-burn-rate (Google SRE workbook):
                      page   1h >= 14.4 and 5m >= 14.4   |  6h >= 6 and 30m >= 6
                      ticket 24h >= 3 and 2h >= 3        |  72h >= 1 and 6h >= 1
  incident cost     = bad events during the incident / budget events in the window
  policy            remaining > 50% normal, 25-50% caution, 0-25% restrict, <= 0 freeze (reliability fixes only)

SLI sources: Dynatrace SLOs (evaluated for any timeframe), Splunk good/total searches, or the MOCK stream.
SLO definitions: "slos" in aiops_config.json (see docs/SRE.md).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from anomaly_agent.sources.base import SourceError, http, load_config, mock_mode
from incident_agent.insights import ts
from incident_agent.servicenow import ServiceNowError

UTC = timezone.utc
WINDOWS = [("5m", 5), ("30m", 30), ("1h", 60), ("2h", 120), ("6h", 360), ("24h", 1440), ("72h", 4320)]
RULES = [("page", "1h", "5m", 14.4), ("page", "6h", "30m", 6.0), ("ticket", "24h", "2h", 3.0), ("ticket", "72h", "6h", 1.0)]


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- SLI sources
class MockSLI:
    """MOCK traffic: a base bad ratio plus bad-ratio spikes matching the demo story."""
    label = "MOCK SLI stream"

    def __init__(self, now: datetime):
        h = lambda x: now - timedelta(hours=x)  # noqa: E731
        self.spikes = {
            "enrollment-availability": [(h(1.7), now, 0.38), (h(9 * 24 + 3), h(9 * 24 + 1), 0.10)],
            "eligibility-latency": [(h(1.7), now, 0.45)],
            "payment-availability": [(h(7), now, 0.03)],
            "claims-latency": [(h(26), now, 0.02)],
            "sso-availability": [(h(0.5), now, 0.03)],
            "vpn-availability": [(h(2.6), now, 0.06), (h(23), h(20), 0.04)],
        }
        self.base = {"enrollment-availability": 0.0004, "eligibility-latency": 0.004, "payment-availability": 0.0001,
                     "claims-latency": 0.003, "sso-availability": 0.00005, "vpn-availability": 0.001}

    def bad_ratio(self, slo: dict, start: datetime, end: datetime) -> float | None:
        key = slo.get("mock_key")
        if key not in self.base:
            return None
        dur = (end - start).total_seconds()
        if dur <= 0:
            return 0.0
        bad = self.base[key]
        for a, b, r in self.spikes[key]:
            ov = (min(b, end) - max(a, start)).total_seconds()
            if ov > 0:
                bad += r * ov / dur
        return min(bad, 1.0)


class DynatraceSLI:
    label = "Dynatrace SLOs"

    def bad_ratio(self, slo: dict, start: datetime, end: datetime) -> float | None:
        sid = slo.get("dynatrace_slo_id")
        if not sid:
            return None
        base = os.environ["AIOPS_DYNATRACE_URL"].rstrip("/")
        d = http("GET", f"{base}/api/v2/slo/{sid}", headers={"Authorization": f"Api-Token {os.environ['AIOPS_DYNATRACE_TOKEN']}"},
                 params={"timeFrame": "GTF", "from": int(start.timestamp() * 1000), "to": int(end.timestamp() * 1000)}).json()
        ep = d.get("evaluatedPercentage")
        if ep is None or ep < 0:  # Dynatrace returns -1 when there is no data
            return None
        return max(0.0, 1 - float(ep) / 100)


class SplunkSLI:
    """slo["splunk_search"] must return fields good and total for {earliest}..{latest} (epoch seconds)."""
    label = "Splunk SLI searches"

    def bad_ratio(self, slo: dict, start: datetime, end: datetime) -> float | None:
        q = slo.get("splunk_search")
        if not q:
            return None
        r = http("POST", os.environ["AIOPS_SPLUNK_URL"].rstrip("/") + "/services/search/jobs/export",
                 headers={"Authorization": f"Bearer {os.environ['AIOPS_SPLUNK_TOKEN']}"},
                 data={"search": q.format(earliest=int(start.timestamp()), latest=int(end.timestamp())), "output_mode": "json"},
                 timeout=90, verify=os.getenv("AIOPS_SPLUNK_VERIFY", "true").lower() != "false")
        good = total = 0.0
        for line in r.text.splitlines():
            try:
                row = json.loads(line).get("result") or {}
            except ValueError:
                continue
            good += float(row.get("good") or 0)
            total += float(row.get("total") or 0)
        return None if not total else max(0.0, 1 - good / total)


MOCK_SLOS = [
    {"service": "enrollment-api", "name": "Enrollment submit availability", "sli": "availability", "objective": 99.9, "window_days": 28, "mock_key": "enrollment-availability"},
    {"service": "eligibility-svc", "name": "Eligibility check latency < 300 ms", "sli": "latency", "objective": 99.0, "window_days": 28, "mock_key": "eligibility-latency"},
    {"service": "payment-gw", "name": "Autopay charge availability", "sli": "availability", "objective": 99.95, "window_days": 28, "mock_key": "payment-availability"},
    {"service": "claims-db-prod", "name": "Claims search latency < 2 s", "sli": "latency", "objective": 99.0, "window_days": 28, "mock_key": "claims-latency"},
    {"service": "okta-sso", "name": "Member SSO login availability", "sli": "availability", "objective": 99.95, "window_days": 28, "mock_key": "sso-availability"},
    {"service": "vpn-dallas-01", "name": "Remote access availability", "sli": "availability", "objective": 99.5, "window_days": 28, "mock_key": "vpn-availability"},
]


def definitions() -> list[dict]:
    if mock_mode():
        return [dict(s) for s in MOCK_SLOS]
    return [dict(s) for s in load_config().get("slos", [])]


def source_for(slo: dict, now: datetime):
    if mock_mode():
        return MockSLI(now)
    if slo.get("dynatrace_slo_id") and os.getenv("AIOPS_DYNATRACE_URL") and os.getenv("AIOPS_DYNATRACE_TOKEN"):
        return DynatraceSLI()
    if slo.get("splunk_search") and os.getenv("AIOPS_SPLUNK_URL") and os.getenv("AIOPS_SPLUNK_TOKEN"):
        return SplunkSLI()
    return None


def coverage() -> list[dict]:
    defs = definitions()
    dt = bool(os.getenv("AIOPS_DYNATRACE_URL") and os.getenv("AIOPS_DYNATRACE_TOKEN"))
    sp = bool(os.getenv("AIOPS_SPLUNK_URL") and os.getenv("AIOPS_SPLUNK_TOKEN"))
    return [
        {"label": "SLO definitions", "state": "mock" if mock_mode() else ("connected" if defs else "not_connected"),
         "detail": f"{len(defs)} SLO(s)" + ("" if defs or mock_mode() else ": add \"slos\" to aiops_config.json")},
        {"label": "Dynatrace SLOs", "state": "mock" if mock_mode() else ("connected" if dt else "not_connected"),
         "detail": "evaluated per window via /api/v2/slo/{id}" if dt else "AIOPS_DYNATRACE_URL + AIOPS_DYNATRACE_TOKEN (slo.read)"},
        {"label": "Splunk SLI searches", "state": "mock" if mock_mode() else ("connected" if sp else "not_connected"),
         "detail": "good/total per window" if sp else "AIOPS_SPLUNK_URL + AIOPS_SPLUNK_TOKEN"},
        {"label": "ServiceNow", "state": "connected", "detail": "incidents (budget cost, MTTR), changes (burn after change), CMDB owner"},
    ]


# ---------------------------------------------------------------- evaluation
def policy(remaining_pct: float | None) -> dict:
    if remaining_pct is None:
        return {"level": "unknown", "text": "No SLI data."}
    if remaining_pct <= 0:
        return {"level": "freeze", "text": "Error budget exhausted: freeze feature changes; only reliability fixes, with SRE approval."}
    if remaining_pct < 25:
        return {"level": "restrict", "text": "Under 25% budget left: risky changes need SRE review and a canary; prioritise reliability work."}
    if remaining_pct < 50:
        return {"level": "caution", "text": "Under 50% budget left: ship with canaries and watch burn rate."}
    return {"level": "normal", "text": "Healthy budget: normal release cadence."}


def evaluate(sn, slo: dict, now: datetime | None = None, with_series: bool | None = None) -> dict:
    now = now or datetime.now(UTC)
    src = source_for(slo, now)
    W = int(slo.get("window_days", 28))
    obj = float(slo.get("objective", 99.9))
    budget = 1 - obj / 100
    out = {**{k: slo.get(k) for k in ("service", "name", "sli", "objective")}, "window_days": W, "source": getattr(src, "label", None)}
    if not src:
        return {**out, "available": False, "policy": policy(None), "detail": "No SLI source configured for this SLO."}
    try:
        bw = src.bad_ratio(slo, now - timedelta(days=W), now)
    except (SourceError, KeyError, ValueError) as e:
        return {**out, "available": False, "policy": policy(None), "detail": str(e)}
    if bw is None:
        return {**out, "available": False, "policy": policy(None), "detail": "SLI source returned no data."}
    consumed = bw / budget if budget else 0.0
    remaining = round((1 - consumed) * 100, 1)
    burn = {}
    for name, minutes in WINDOWS:
        try:
            r = src.bad_ratio(slo, now - timedelta(minutes=minutes), now)
        except (SourceError, KeyError, ValueError):
            r = None
        burn[name] = None if r is None else round(r / budget, 2)
    alerts = [{"severity": sev, "rule": f"{lw} ≥ {thr:g}× and {sw} ≥ {thr:g}×", "long": burn.get(lw), "short": burn.get(sw)}
              for sev, lw, sw, thr in RULES if (burn.get(lw) or 0) >= thr and (burn.get(sw) or 0) >= thr]
    state = "page" if any(a["severity"] == "page" for a in alerts) else ("ticket" if alerts else "ok")
    b24 = burn.get("24h") or 0
    exhaust_days = None if remaining <= 0 or b24 <= 0 else round((remaining / 100) * W / b24, 1)
    budget_minutes = budget * W * 1440
    out.update(available=True, attainment=round((1 - bw) * 100, 4), budget_pct=round(budget * 100, 3), consumed_pct=round(consumed * 100, 1),
               remaining_pct=remaining, budget_minutes=round(budget_minutes, 1), remaining_minutes=round(budget_minutes * max(remaining, 0) / 100, 1),
               burn=burn, alerts=alerts, state=state, exhausted_in_days=exhaust_days, policy=policy(remaining))
    out.update(_servicenow(sn, slo, src, now, W, budget))
    if with_series if with_series is not None else (mock_mode() or os.getenv("AIOPS_SLO_SERIES", "false").lower() == "true"):
        out["series"] = _series(src, slo, now, W, budget)
    return out


def _series(src, slo: dict, now: datetime, W: int, budget: float) -> list[dict]:
    """Remaining budget at the end of each day of the window (budget consumed so far, cumulative)."""
    pts, start = [], now - timedelta(days=W)
    for d in range(1, W + 1):
        t = start + timedelta(days=d)
        try:
            r = src.bad_ratio(slo, start, t)
        except (SourceError, KeyError, ValueError):
            r = None
        if r is not None:
            used = r * (d / W) / budget  # bad events so far / budget events for the whole window
            pts.append({"day": _fmt(t)[:10], "remaining_pct": round((1 - used) * 100, 1)})
    return pts


def _servicenow(sn, slo: dict, src, now: datetime, W: int, budget: float) -> dict:
    ctx = {"owner": None, "ci": None, "incidents": [], "changes": [], "business_services": [], "change_window": None}
    try:
        ci = sn.ci_lookup(slo["service"])
    except ServiceNowError:
        ci = None
    if not ci:
        return ctx
    sid = ci.get("sys_id_raw") or ci.get("sys_id")
    ctx["ci"] = ci.get("name")
    try:
        ctx["owner"] = sn.ci_support_group(sid)
    except ServiceNowError:
        pass
    try:
        from anomaly_agent.signals import _services_above
        ctx["business_services"] = _services_above(sn, sid)
    except Exception:
        pass
    total_hours = W * 24
    try:
        incs = [r for r in sn.incidents_on_ci(sid, active_only=False, limit=100)
                if ts(r.get("opened_at_raw")) and ts(r["opened_at_raw"]) >= now - timedelta(days=W)]
    except ServiceNowError:
        incs = []
    covered = None  # overlapping incidents (duplicates of one outage) share the budget instead of double counting
    for r in sorted(incs, key=lambda x: x["opened_at_raw"]):
        a = ts(r["opened_at_raw"]) - timedelta(minutes=30)  # impact usually starts before the ticket
        b = ts(r.get("resolved_at_raw")) or now
        if covered and a < covered:
            a = covered
        if b <= a:
            continue
        covered = max(covered or b, b)
        try:
            rr = src.bad_ratio(slo, a, b)
        except (SourceError, KeyError, ValueError):
            rr = None
        if rr is None:
            continue
        cost = rr * ((b - a).total_seconds() / 3600) / (budget * total_hours) * 100
        if cost >= 1:
            ctx["incidents"].append({"number": r["number"], "priority": r.get("priority"), "state": r.get("state"),
                                     "short_description": r.get("short_description"), "opened": r["opened_at_raw"],
                                     "budget_cost_pct": round(cost, 1)})
    ctx["incidents"].sort(key=lambda x: -x["budget_cost_pct"])
    try:
        chg = sn.changes_near([sid])
    except ServiceNowError:
        chg = []
    for c in chg:
        en, st = ts(c.get("end_raw")), ts(c.get("start_raw"))
        if st and en and st <= now <= en:
            ctx["change_window"] = {"number": c.get("number"), "short_description": c.get("short_description"), "end": _fmt(en)}
        if not en or en < now - timedelta(days=W) or en > now:
            continue
        try:
            before = src.bad_ratio(slo, en - timedelta(hours=6), en)
            after = src.bad_ratio(slo, en, min(now, en + timedelta(hours=6)))
        except (SourceError, KeyError, ValueError):
            before = after = None
        jump = None if before in (None, 0) or after is None else round(after / max(before, 1e-9), 1)
        ctx["changes"].append({"number": c.get("number"), "short_description": c.get("short_description"), "ended": _fmt(en),
                               "burn_before": None if before is None else round(before / budget, 2),
                               "burn_after": None if after is None else round(after / budget, 2), "jump": jump,
                               "suspect": bool(jump and jump >= 3)})
    return ctx


def all_slos(sn, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    rows = [evaluate(sn, s, now) for s in definitions()]
    order = {"page": 0, "ticket": 1, "ok": 2}
    rows.sort(key=lambda r: (not r.get("available"), order.get(r.get("state"), 3), r.get("remaining_pct") if r.get("remaining_pct") is not None else 999))
    return {"checked_at": _fmt(now) + " UTC", "mock": mock_mode(), "slos": rows, "coverage": coverage(),
            "summary": {"page": sum(r.get("state") == "page" for r in rows), "ticket": sum(r.get("state") == "ticket" for r in rows),
                        "freeze": sum(r.get("policy", {}).get("level") == "freeze" for r in rows), "total": len(rows)}}


def for_service(sn, service: str, now: datetime | None = None) -> list[dict]:
    return [evaluate(sn, s, now, with_series=False) for s in definitions() if s["service"].lower() == (service or "").lower()]


def scorecard(sn, days: int = 28, now: datetime | None = None) -> list[dict]:
    """Per service: SLO budget, alert state, MTTR, P1/P2 count, change failure rate, deploy frequency, open P1/P2."""
    from change_agent.risk import history_stats
    now = now or datetime.now(UTC)
    slos = {}
    for s in all_slos(sn, now)["slos"]:
        slos.setdefault(s["service"], []).append(s)
    services = list(dict.fromkeys(list(slos) + [s["service"] for s in definitions()]))
    out = []
    for svc in services:
        try:
            ci = sn.ci_lookup(svc)
        except ServiceNowError:
            ci = None
        sid = (ci or {}).get("sys_id_raw") or (ci or {}).get("sys_id")
        incs = []
        if sid:
            try:
                incs = sn.incidents_on_ci(sid, active_only=False, limit=200)
            except ServiceNowError:
                incs = []
        recent = [r for r in incs if ts(r.get("opened_at_raw")) and ts(r["opened_at_raw"]) >= now - timedelta(days=days)
                  and str(r.get("priority") or "5")[:1] in "12"]
        res_h = [(ts(r["resolved_at_raw"]) - ts(r["opened_at_raw"])).total_seconds() / 3600 for r in recent if ts(r.get("resolved_at_raw"))]
        hs = history_stats(sn, sid, 90, now) if sid else {}
        worst = min(slos.get(svc, []), key=lambda s: s.get("remaining_pct", 999) if s.get("available") else 999, default=None)
        out.append({"service": svc, "slo": worst.get("name") if worst else None,
                    "remaining_pct": worst.get("remaining_pct") if worst and worst.get("available") else None,
                    "state": worst.get("state") if worst else None, "policy": (worst or {}).get("policy", {}).get("level"),
                    "p1p2_28d": len(recent), "open_p1p2": sum(1 for r in recent if not ts(r.get("resolved_at_raw"))),
                    "mttr_h": round(sum(res_h) / len(res_h), 1) if res_h else None,
                    "change_failure_rate": hs.get("change_failure_rate"), "deploys_per_week": hs.get("deploys_per_week"),
                    "changes_90d": hs.get("changes"), "failed_changes_90d": hs.get("failed")})
    order = {"freeze": 0, "restrict": 1, "caution": 2, "normal": 3}
    out.sort(key=lambda r: (order.get(r["policy"], 4), -(r["p1p2_28d"] or 0)))
    return out
