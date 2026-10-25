"""AppDynamics: open health-rule violations (these already use AppDynamics dynamic baselines) on business
transactions, tiers and nodes.

Env: AIOPS_APPD_URL (https://<account>.saas.appdynamics.com), AIOPS_APPD_TOKEN (API client OAuth token),
AIOPS_APPD_APPS (comma-separated application names).
API: /controller/rest/applications/{app}/problems/healthrule-violations
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from urllib.parse import quote

from .base import Signal, SignalSource, http


class AppDynamicsSource(SignalSource):
    name, label = "appdynamics", "AppDynamics"
    domains = ("trace", "metric")
    env = ("AIOPS_APPD_URL", "AIOPS_APPD_TOKEN", "AIOPS_APPD_APPS")

    def describe(self) -> str:
        return f"apps: {os.getenv('AIOPS_APPD_APPS', '')}"

    def fetch(self, window_minutes: int) -> list[Signal]:
        base = os.environ["AIOPS_APPD_URL"].rstrip("/")
        hdr = {"Authorization": f"Bearer {os.environ['AIOPS_APPD_TOKEN']}"}
        out = []
        for app in [a.strip() for a in os.environ["AIOPS_APPD_APPS"].split(",") if a.strip()]:
            rows = http("GET", f"{base}/controller/rest/applications/{quote(app)}/problems/healthrule-violations", headers=hdr,
                        params={"time-range-type": "BEFORE_NOW", "duration-in-mins": max(window_minutes, 15), "output": "JSON"}).json()
            for v in rows:
                if v.get("incidentStatus") not in (None, "OPEN"):
                    continue
                ent = v.get("affectedEntityDefinition") or {}
                kind = ent.get("entityType", "")
                t = v.get("startTimeInMillis")
                out.append(Signal("appdynamics", "trace" if kind == "BUSINESS_TRANSACTION" else "metric", self.svc(ent.get("name")),
                                  f"Health rule: {v.get('name', '')}", severity="critical" if v.get("severity") == "CRITICAL" else "major",
                                  time=datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if t else "",
                                  detail=f"{app} · {kind.replace('_', ' ').lower()} {ent.get('name', '')} · {(v.get('description') or '')[:140]}",
                                  link=v.get("deepLinkUrl", ""), feature="health_violations"))
        return out
