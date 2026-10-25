"""Your predictive anomaly model, called over REST. Contract (docs/ANOMALY_DETECTION.md):

POST {AIOPS_MODEL_URL}            Authorization: Bearer {AIOPS_MODEL_TOKEN}   (token optional)
{
  "window_start": "2026-10-24 22:00:00", "window_end": "2026-10-24 23:00:00",
  "services": [{"service": "enrollment-api", "ci": "enrollment-api", "features": {"error_rate_pct": 7.4, ...}}]
}
-> 200
{
  "model_version": "anomaly-xgb-2026.10",
  "results": [{"service": "enrollment-api", "score": 0.97, "anomaly": true, "threshold": 0.6,
               "top_features": [{"name": "error_rate_ratio", "value": 24.7, "contribution": 0.41}],
               "explanation": "Error rate 25x baseline with a new error signature"}]
}
"anomaly" and "threshold" are optional (score >= AIOPS_MODEL_THRESHOLD, default 0.6). If your model only scores one
service per call, set AIOPS_MODEL_BATCH=false: the body is then {"window_start", "window_end", "service", "ci",
"features"} and the response is one result object.
"""
from __future__ import annotations

import math
import os

from .base import SourceError, http, mock_mode

FEATURES = [  # name, description: the vector the agent builds per service from all sources
    ("error_rate_pct", "Current error rate % (APM)"), ("error_rate_ratio", "Error rate / its baseline"),
    ("p95_ms", "Current p95 response time, ms"), ("p95_ratio", "p95 / its baseline"),
    ("kpi_drop_pct", "Business KPI drop vs baseline, %"), ("vendor_problems", "Open APM problems (Dynatrace)"),
    ("health_violations", "Open health-rule violations (AppDynamics)"), ("critical_signals", "Critical signals from any source"),
    ("new_error_signatures", "Error log signatures not seen in the baseline (Splunk)"), ("error_log_ratio", "Error log volume / baseline"),
    ("sources_agreeing", "Independent sources with an anomalous signal"), ("change_active", "1 if a change window is open on the CI"),
    ("minutes_since_change", "Minutes since the last change on the CI ended (-1 if none in 24h)"),
    ("open_incidents", "Open ServiceNow incidents on the CI"),
]


class PredictiveModel:
    label = "Predictive model"

    def configured(self) -> bool:
        return bool(os.getenv("AIOPS_MODEL_URL"))

    def status(self) -> dict:
        if mock_mode():
            return {"state": "mock", "detail": "MOCK scorer standing in for your model (SERVICENOW_MODE=mock)"}
        if self.configured():
            return {"state": "connected", "detail": os.environ["AIOPS_MODEL_URL"].split("?")[0]}
        return {"state": "not_connected", "detail": "Set AIOPS_MODEL_URL (and AIOPS_MODEL_TOKEN) in .env; until then signals are ranked by how many sources agree"}

    def score(self, window_start: str, window_end: str, items: list[dict]) -> dict:
        """items: [{"service", "ci", "features"}] -> {"available", "model_version", "results": {service: result}}"""
        if not items:
            return {"available": self.configured() or mock_mode(), "model_version": None, "results": {}}
        if mock_mode():
            return {"available": True, "model_version": "mock-logistic-1", "results": {i["service"]: _mock(i) for i in items}}
        if not self.configured():
            return {"available": False, "model_version": None, "results": {}}
        thr = float(os.getenv("AIOPS_MODEL_THRESHOLD", "0.6"))
        hdr = {"Authorization": f"Bearer {os.environ['AIOPS_MODEL_TOKEN']}"} if os.getenv("AIOPS_MODEL_TOKEN") else {}
        url, out, version = os.environ["AIOPS_MODEL_URL"], {}, None
        batch = os.getenv("AIOPS_MODEL_BATCH", "true").lower() != "false"
        for b in ([items] if batch else [[i] for i in items]):
            body = ({"window_start": window_start, "window_end": window_end, "services": b}
                    if batch else {"window_start": window_start, "window_end": window_end, **b[0]})
            try:
                resp = http("POST", url, headers=hdr, json_body=body, timeout=int(os.getenv("AIOPS_MODEL_TIMEOUT", "20"))).json()
            except (SourceError, ValueError) as e:
                raise SourceError(f"Predictive model call failed: {e}") from e
            version = resp.get("model_version", version)
            for r in resp.get("results", [resp] if "score" in resp else []):
                svc = r.get("service") or b[0]["service"]
                t = float(r.get("threshold", thr))
                out[svc] = {"score": round(float(r.get("score", 0)), 3), "threshold": t,
                            "anomaly": bool(r.get("anomaly", float(r.get("score", 0)) >= t)),
                            "top_features": r.get("top_features", [])[:6], "explanation": r.get("explanation", "")}
        return {"available": True, "model_version": version, "results": out}


WEIGHTS = {"error_log_ratio": 0.12, "error_rate_ratio": 0.18, "p95_ratio": 0.25, "kpi_drop_pct": 0.05, "vendor_problems": 0.9, "health_violations": 0.8,
           "new_error_signatures": 1.1, "critical_signals": 0.5, "sources_agreeing": 0.6, "open_incidents": 0.15}


def _mock(item: dict) -> dict:
    """MOCK only: a small logistic scorer so the demo shows model scores and feature contributions."""
    f = item["features"]
    contrib = {k: w * min(float(f.get(k) or 0), 25) for k, w in WEIGHTS.items()}
    z = sum(contrib.values()) - 3.2
    score = 1 / (1 + math.exp(-z))
    top = sorted(((k, v) for k, v in contrib.items() if v > 0), key=lambda kv: -kv[1])[:4]
    total = sum(v for _, v in top) or 1
    names = {"error_rate_ratio": "error rate {:.0f}x baseline", "p95_ratio": "p95 {:.1f}x baseline", "kpi_drop_pct": "KPI down {:.0f}%",
             "vendor_problems": "{:.0f} APM problem(s)", "health_violations": "{:.0f} health-rule violation(s)",
             "new_error_signatures": "{:.0f} new error signature(s)", "critical_signals": "{:.0f} critical signal(s)",
             "sources_agreeing": "{:.0f} sources agree", "open_incidents": "{:.0f} open incident(s)",
             "error_log_ratio": "error logs {:.0f}x baseline"}
    return {"score": round(score, 3), "threshold": 0.6, "anomaly": score >= 0.6,
            "top_features": [{"name": k, "value": f.get(k), "contribution": round(v / total, 2)} for k, v in top],
            "explanation": "; ".join(names[k].format(float(f.get(k) or 0)) for k, _ in top) or "no notable features"}
