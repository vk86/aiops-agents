"""Dynatrace: open problems (Davis AI) + service error rate / p95 response time vs their own recent baseline,
plus optional business KPIs from metric selectors in aiops_config.json.

Env: AIOPS_DYNATRACE_URL (https://<env>.live.dynatrace.com), AIOPS_DYNATRACE_TOKEN (Api-Token with problems.read,
metrics.read). API: /api/v2/problems, /api/v2/metrics/query.
"""
from __future__ import annotations

import os
import statistics
from datetime import datetime, timezone

from .base import Signal, SignalSource, http

SEV = {"AVAILABILITY": "critical", "ERROR": "critical", "PERFORMANCE": "major", "RESOURCE_CONTENTION": "major",
       "CUSTOM_ALERT": "major", "MONITORING_UNAVAILABLE": "minor", "INFO": "info"}
METRICS = [  # selector, signal name, feature, unit, scale, anomalous when current >= factor x baseline and >= floor
    ('builtin:service.errors.total.rate:splitBy("dt.entity.service"):avg:names', "Error rate", "error_rate_pct", "%", 1, 3.0, 1.0),
    ('builtin:service.response.time:splitBy("dt.entity.service"):percentile(95):names', "Response time p95", "p95_ms", "ms", 0.001, 2.0, 500),
]


def _t(ms) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ms else ""


class DynatraceSource(SignalSource):
    name, label = "dynatrace", "Dynatrace"
    domains = ("event", "metric", "kpi")
    env = ("AIOPS_DYNATRACE_URL", "AIOPS_DYNATRACE_TOKEN")

    def _get(self, path: str, params: dict) -> dict:
        base = os.environ["AIOPS_DYNATRACE_URL"].rstrip("/")
        return http("GET", base + path, params=params,
                    headers={"Authorization": f"Api-Token {os.environ['AIOPS_DYNATRACE_TOKEN']}"}).json()

    def fetch(self, window_minutes: int) -> list[Signal]:
        base = os.environ["AIOPS_DYNATRACE_URL"].rstrip("/")
        out: list[Signal] = []
        probs = self._get("/api/v2/problems", {"from": f"now-{max(window_minutes, 60)}m", "pageSize": 100,
                                                "problemSelector": 'status("OPEN")'})
        for p in probs.get("problems", []):
            ents = p.get("affectedEntities") or p.get("impactedEntities") or []
            for e in ents[:5] or [{"name": (p.get("rootCauseEntity") or {}).get("name", "unknown")}]:
                out.append(Signal("dynatrace", "event", self.svc(e.get("name")), f"Problem {p.get('displayId', '')}: {p.get('title', '')}",
                                  severity=SEV.get(p.get("severityLevel"), "major"), time=_t(p.get("startTime")),
                                  detail=f"{p.get('severityLevel', '')} · impact {p.get('impactLevel', '')} · root cause "
                                         f"{(p.get('rootCauseEntity') or {}).get('name', 'n/a')}",
                                  link=f"{base}/#problems/problemdetails;pid={p.get('problemId', '')}", feature="vendor_problems"))
        res = f"{max(window_minutes // 2, 5)}m"
        for sel, label, feat, unit, scale, factor, floor in METRICS:
            out += self._metric(sel, label, feat, unit, scale, factor, floor, window_minutes, res)
        for k in self.cfg.get("kpis", []):
            if k.get("source") == "dynatrace" and k.get("metric"):
                out += self._kpi(k, window_minutes)
        return out

    def _series(self, selector: str, window_minutes: int, res: str):
        d = self._get("/api/v2/metrics/query", {"metricSelector": selector, "from": f"now-{window_minutes * 8}m",
                                                 "resolution": res})
        for r in d.get("result", []):
            for s in r.get("data", []):
                vals = [v for v in s.get("values", []) if v is not None]
                if len(vals) >= 3:
                    dm = s.get("dimensionMap", {})
                    name = next((v for k, v in dm.items() if k.endswith(".name")), None) or (s.get("dimensions") or ["?"])[0]
                    yield name, vals, s.get("timestamps", [None])[-1]

    def _metric(self, sel, label, feat, unit, scale, factor, floor, window_minutes, res) -> list[Signal]:
        out = []
        for name, vals, ts in self._series(sel, window_minutes, res):
            cur, base = vals[-1] * scale, statistics.median(vals[:-1]) * scale
            bad = cur >= floor and cur >= factor * max(base, 1e-9)
            out.append(Signal("dynatrace", "metric", self.svc(name), label, round(cur, 2), round(base, 2), unit,
                              "major" if bad else "info", bad, _t(ts), feature=feat))
        return out

    def _kpi(self, k: dict, window_minutes: int) -> list[Signal]:
        out = []
        for name, vals, ts in self._series(k["metric"], window_minutes, f"{max(window_minutes // 2, 5)}m"):
            cur, base = vals[-1], statistics.median(vals[:-1])
            drop = (base - cur) / base * 100 if base else 0.0
            bad = drop >= float(k.get("drop_pct", 20)) if k.get("direction", "down") == "down" else -drop >= float(k.get("rise_pct", 50))
            out.append(Signal("dynatrace", "kpi", self.svc(k.get("service") or name), k.get("name", "KPI"), round(cur, 2), round(base, 2),
                              k.get("unit", ""), "critical" if bad else "info", bad, _t(ts), feature="kpi_drop_pct",
                              extra={"drop_pct": round(drop, 1)}))
        return out
