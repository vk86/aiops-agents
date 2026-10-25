"""MOCK transaction traces (SERVICENOW_MODE=mock), consistent with the demo story.

TXN-ENR-20261024-0207  enrollment fails: eligibility-svc times out after 200ms calling plan-catalog-svc (which answers
                       in 640ms); the error propagates up to enrollment-api (502) and the member portal. Origin downstream.
TXN-ENR-20261024-0311  enrollment-api rejects the request with 400 (planId missing) sent by enrollment-ui.
                       Reported at enrollment-api, the origin is upstream (the caller).
TXN-PAY-20261024-0412  autopay charge fails in payment-gw: TLS handshake, certificate expired.
TXN-ENR-20261024-0199  a healthy enrollment, for comparison.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .sources import Span

UTC = timezone.utc


def _t(base: datetime, ms: float) -> str:
    return (base + timedelta(milliseconds=ms)).strftime("%Y-%m-%d %H:%M:%S.%f")[:23]


def mock_spans(txn_id: str, now: datetime | None = None) -> list[Span]:
    now = now or datetime.now(UTC)
    tid = txn_id.strip().upper()
    dt = "https://mock.apps.dynatrace.com/ui/distributed-traces?trace="
    if tid == "TXN-ENR-20261024-0207":
        b, tr = now - timedelta(minutes=85), "4bf92f3577b34da6a3ce929d0e0e4736"
        return [
            Span("dynatrace", "enrollment-ui", "POST /enroll/submit", _t(b, 0), 236, "error", "Member shown 'Something went wrong' (HTTP 502 from enrollment-api)", 502, "a1", "", tr, link=dt + tr),
            Span("dynatrace", "enrollment-api", "POST /enrollments", _t(b, 6), 222, "error", "502 Bad Gateway: eligibility-svc call failed", 502, "b2", "a1", tr),
            Span("dynatrace", "enrollment-api", "INSERT enrollment_draft (enroll-db)", _t(b, 9), 3, "ok", "", None, "b3", "b2", tr),
            Span("dynatrace", "eligibility-svc", "POST /eligibility/check", _t(b, 14), 205, "error", "PlanCatalogClientTimeoutException: plan-catalog call timed out after 200ms", 504, "c4", "b2", tr),
            Span("dynatrace", "plan-catalog-svc", "GET /plans/{planId}", _t(b, 18), 640, "ok", "", 200, "d5", "c4", tr, attrs={"note": "completed after the caller had given up"}),
            Span("splunk", "eligibility-svc", "ERROR PlanCatalogClient", _t(b, 219), None, "error", "PlanCatalogClientTimeoutException: timed out after 200ms (planId=PPO-GOLD-2027) txn=TXN-ENR-20261024-0207", None, "", "", tr, "log", attrs={"host": "elig-7f9c-2", "level": "ERROR"}),
            Span("splunk", "enrollment-api", "ERROR UpstreamCall", _t(b, 224), None, "error", "upstream eligibility-svc returned 504; responding 502 txn=TXN-ENR-20261024-0207", 502, "", "", tr, "log", attrs={"host": "enroll-api-3", "level": "ERROR"}),
            Span("splunk", "plan-catalog-svc", "INFO access", _t(b, 658), 640, "ok", "", 200, "", "", tr, "log", attrs={"host": "catalog-1", "level": "INFO"}),
            Span("appdynamics", "enrollment-api", "Submit Enrollment", _t(b, 6), 222, "error", "HTTP 502 - eligibility-svc", None, kind="snapshot", attrs={"experience": "ERROR"}),
        ]
    if tid == "TXN-ENR-20261024-0311":
        b, tr = now - timedelta(minutes=40), "9a1c0e5d7f2b4c6e8a0b1c2d3e4f5a6b"
        return [
            Span("dynatrace", "enrollment-ui", "POST /enroll/submit", _t(b, 0), 31, "error", "Member shown 'Please check your plan selection' (HTTP 400)", 400, "a1", "", tr, link=dt + tr),
            Span("dynatrace", "enrollment-api", "POST /enrollments", _t(b, 5), 12, "error", "400 Bad Request: planId is required", 400, "b2", "a1", tr),
            Span("splunk", "enrollment-api", "WARN Validation", _t(b, 16), None, "error", "Request validation failed: planId missing in payload from enrollment-ui 4.19.0 txn=TXN-ENR-20261024-0311", 400, "", "", tr, "log", attrs={"level": "WARN"}),
            Span("splunk", "enrollment-ui", "ERROR SubmitForm", _t(b, 30), None, "error", "Submit failed 400; plan dropdown sent empty planId after release 4.19.0", 400, "", "", tr, "log", attrs={"level": "ERROR"}),
        ]
    if tid == "TXN-PAY-20261024-0412":
        b, tr = now - timedelta(hours=6, minutes=5), "77aa00bb11cc22dd33ee44ff55667788"
        return [
            Span("dynatrace", "autopay-batch", "charge premium (batch)", _t(b, 0), 30_150, "error", "Charge failed for member 88412: gateway error", None, "a1", "", tr, link=dt + tr),
            Span("dynatrace", "payment-gw", "POST /autopay/charge", _t(b, 40), 30_080, "error", "SSLHandshakeException: PKIX path validation failed: certificate expired", 502, "b2", "a1", tr),
            Span("splunk", "payment-gw", "ERROR TLS", _t(b, 30_120), None, "error", "SSLHandshakeException: certificate expired (CN=payment-gw.internal, notAfter yesterday) txn=TXN-PAY-20261024-0412", None, "", "", tr, "log", attrs={"level": "ERROR"}),
        ]
    if tid == "TXN-ENR-20261024-0199":
        b, tr = now - timedelta(hours=3), "1f2e3d4c5b6a79881f2e3d4c5b6a7988"
        return [
            Span("dynatrace", "enrollment-ui", "POST /enroll/submit", _t(b, 0), 318, "ok", "", 200, "a1", "", tr, link=dt + tr),
            Span("dynatrace", "enrollment-api", "POST /enrollments", _t(b, 6), 301, "ok", "", 201, "b2", "a1", tr),
            Span("dynatrace", "eligibility-svc", "POST /eligibility/check", _t(b, 14), 262, "ok", "", 200, "c4", "b2", tr),
            Span("dynatrace", "plan-catalog-svc", "GET /plans/{planId}", _t(b, 18), 231, "ok", "", 200, "d5", "c4", tr),
            Span("dynatrace", "notification-svc", "POST /notify/confirmation", _t(b, 280), 18, "ok", "", 202, "e6", "b2", tr),
        ]
    return []


# ---------------------------------------------------------------- MOCK system logs around a moment
_CONTEXT = {
    "enrollment-ui": [("INFO", "GET /enroll/plans 200 41ms"), ("INFO", "POST /enroll/submit 200 312ms"),
                      ("ERROR", "POST /enroll/submit failed: HTTP 502 from enrollment-api; showing error banner"),
                      ("WARN", "Retry disabled for submit (idempotency key missing)")],
    "enrollment-api": [("INFO", "POST /enrollments 201 298ms"), ("ERROR", "upstream eligibility-svc returned 504; responding 502"),
                       ("WARN", "eligibility-svc circuit breaker half-open (error rate 38%)"), ("INFO", "INSERT enrollment_draft ok 3ms"),
                       ("ERROR", "upstream eligibility-svc returned 504; responding 502")],
    "eligibility-svc": [("ERROR", "PlanCatalogClientTimeoutException: plan-catalog call timed out after 200ms"),
                        ("WARN", "plan-catalog client timeout=200ms (config plancatalog.client.timeout.ms, changed by CHG0098871)"),
                        ("ERROR", "PlanCatalogClientTimeoutException: plan-catalog call timed out after 200ms"),
                        ("INFO", "POST /eligibility/check 200 188ms (cache hit)")],
    "plan-catalog-svc": [("INFO", "GET /plans/{planId} 200 612ms"), ("WARN", "slow query plans_by_region 410ms (p95 240ms last 7d)"),
                         ("INFO", "GET /plans/{planId} 200 655ms"), ("INFO", "GC pause 38ms")],
    "notification-svc": [("INFO", "POST /notify/confirmation 202 17ms"), ("WARN", "template enrollment_confirmation_v7 render 1.2s")],
    "payment-gw": [("ERROR", "SSLHandshakeException: PKIX path validation failed: certificate expired"),
                   ("WARN", "certificate CN=payment-gw.internal expired at 00:00 UTC"), ("INFO", "health check 200")],
    "autopay-batch": [("INFO", "autopay batch started: 18,240 members"), ("ERROR", "charge failed for member: gateway error (payment-gw)")],
}


def mock_logs(service: str, around: datetime, txn_id: str = "") -> list[dict]:
    """A plausible slice of the system's own log around the moment, with the transaction's own lines flagged."""
    out = []
    ctx = _CONTEXT.get(service, [("INFO", "request handled 200"), ("INFO", "health check 200")])
    if txn_id.upper().endswith("0311"):  # the bad-request story: release 4.19.0 of the portal drops planId
        ctx = {"enrollment-ui": [("INFO", "release 4.19.0 active (plan selector refactor)"), ("ERROR", "Submit failed 400: planId missing"),
                                 ("INFO", "POST /enroll/submit 201 305ms"), ("ERROR", "Submit failed 400: planId missing")],
               "enrollment-api": [("WARN", "Request validation failed: planId missing (client enrollment-ui 4.19.0)"),
                                  ("INFO", "POST /enrollments 201 298ms"), ("WARN", "Request validation failed: planId missing (client enrollment-ui 4.19.0)")]}.get(service, ctx)
    for i, (lvl, msg) in enumerate(ctx * 2):
        t = around + timedelta(seconds=(i - len(ctx)) * 7 + 3)
        out.append({"time": t.strftime("%Y-%m-%d %H:%M:%S.%f")[:23], "level": lvl, "host": f"{service[:10]}-{i % 3 + 1}",
                    "message": msg + (f" txn=TXN-ENR-{t:%Y%m%d}-{(i * 37) % 900 + 100:04d}" if "txn" not in msg else ""), "this_txn": False})
    if txn_id:
        for sp in mock_spans(txn_id):
            if sp.service == service and sp.kind in ("log", "span"):
                msg = (sp.error or f"{sp.operation} {sp.http_status or ''} {sp.duration_ms or ''}ms").strip()
                out.append({"time": sp.start, "level": "ERROR" if sp.status == "error" else "INFO", "host": sp.attrs.get("host", f"{service[:10]}-1"),
                            "message": f"{msg} txn={txn_id}", "this_txn": True})
    return sorted(out, key=lambda x: x["time"])
