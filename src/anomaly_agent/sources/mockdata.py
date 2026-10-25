"""MOCK observability streams that match the demo story (enrollment outage after CHG0098871, VPN DPD drops,
expired payment-gateway certificate, SSO certificate rotation in an open change window, slow claims DB)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .base import Signal


def mock_signals(window_minutes: int, now: datetime | None = None) -> list[Signal]:
    now = now or datetime.now(timezone.utc)

    def t(minutes_ago: float) -> str:
        return (now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
    dt = "https://mock.live.dynatrace.com/#problems/problemdetails;pid="
    s = [
        # Dynatrace
        Signal("dynatrace", "event", "enrollment-api", "Problem P-2610241: Failure rate increase", severity="critical", time=t(95),
               detail="ERROR · impact SERVICES · root cause eligibility-svc", link=dt + "mock-1", feature="vendor_problems"),
        Signal("dynatrace", "event", "vpn-dallas-01", "Problem P-2610238: Connectivity loss on host", severity="major", time=t(150),
               detail="AVAILABILITY · impact INFRASTRUCTURE", link=dt + "mock-2", feature="vendor_problems"),
        Signal("dynatrace", "metric", "enrollment-api", "Error rate", 7.4, 0.3, "%", "major", True, t(2), feature="error_rate_pct"),
        Signal("dynatrace", "metric", "enrollment-api", "Response time p95", 2100, 240, "ms", "major", True, t(2), feature="p95_ms"),
        Signal("dynatrace", "metric", "eligibility-svc", "Response time p95", 1840, 310, "ms", "major", True, t(2), feature="p95_ms"),
        Signal("dynatrace", "metric", "payment-gw", "Error rate", 4.1, 0.2, "%", "major", True, t(3), feature="error_rate_pct"),
        Signal("dynatrace", "metric", "okta-sso", "Error rate", 3.0, 0.4, "%", "major", True, t(4), feature="error_rate_pct"),
        Signal("dynatrace", "metric", "claims-db-prod", "Response time p95", 980, 760, "ms", "info", False, t(3), feature="p95_ms"),
        Signal("dynatrace", "kpi", "enrollment-api", "Enrollments per minute", 31, 50, "/min", "critical", True, t(1),
               feature="kpi_drop_pct", extra={"drop_pct": 38.0}),
        # AppDynamics
        Signal("appdynamics", "trace", "enrollment-api", "Health rule: Submit Enrollment - Error % above baseline", severity="critical",
               time=t(88), detail="Enrollment · business transaction Submit Enrollment · error % 38 vs baseline 0.5",
               link="https://mock.saas.appdynamics.com/controller/#/violation=mock-1", feature="health_violations"),
        Signal("appdynamics", "trace", "claims-db-prod", "Health rule: Claims Search - Response time above baseline", severity="major",
               time=t(40), detail="Claims · business transaction Claims Search · 1.3x baseline", feature="health_violations"),
        # Splunk
        Signal("splunk", "log", "eligibility-svc", "New error signature", 412, 0, "events", "major", True, t(1),
               detail="PlanCatalogClientTimeoutException: plan-catalog call timed out after Nms", feature="new_error_signatures"),
        Signal("splunk", "log", "enrollment-api", "Error signature", 380, 2.1, "events", "major", True, t(1),
               detail="HTTP 502 from eligibility-svc on POST /enrollments", feature="error_log_ratio"),
        Signal("splunk", "log", "vpn-dallas-01", "Error signature", 36, 1.8, "events", "major", True, t(5),
               detail="IKE DPD timeout for peer N.N.N.N, tearing down SA", feature="error_log_ratio"),
        Signal("splunk", "log", "payment-gw", "New error signature", 120, 0, "events", "major", True, t(6),
               detail="SSLHandshakeException: PKIX path validation failed: certificate expired", feature="new_error_signatures"),
        Signal("splunk", "log", "okta-sso", "New error signature", 64, 0, "events", "major", True, t(8),
               detail="SAMLException: signature validation failed for assertion (cert rollover)", feature="new_error_signatures"),
        Signal("splunk", "log", "claims-db-prod", "Error signature", 4, 3.5, "events", "info", False, t(20),
               detail="Lock wait timeout exceeded on claim_line", feature="error_log_ratio"),
    ]
    return s
