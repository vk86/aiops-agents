"""MELT evidence (Metrics, Events, Logs, Traces).

Placeholders for now: each reports whether it is configured and the gap it leaves in the RCA. Until a
connector is built, responders can add MELT findings as observations in the RCA Workbench
("error rate on eligibility-svc rose from 0.3% to 7% at 02:08") and they are cited as OBS-n.
Planned connectors (env var that will enable each):
  metrics  RCA_PROMETHEUS_URL | RCA_DYNATRACE_URL | RCA_DATADOG_SITE
  events   ServiceNow Event Management (em_alert) | RCA_PAGERDUTY_TOKEN
  logs     RCA_LOKI_URL | RCA_SPLUNK_URL
  traces   RCA_TEMPO_URL | RCA_JAEGER_URL
The AIOps PoC already has Tempo/Loki-shaped logic for transaction tracking (Demo 3).
"""
from __future__ import annotations

import os

from .base import Collected, EvidenceSource

PLANNED = {
    "metrics": ("Metrics", ["RCA_PROMETHEUS_URL", "RCA_DYNATRACE_URL", "RCA_DATADOG_SITE"],
                "cannot confirm when error rate/latency changed or rule out saturation"),
    "events": ("Events / alerts", ["RCA_EVENT_MGMT", "RCA_PAGERDUTY_TOKEN"],
               "cannot see which alerts fired first or how noisy the storm was"),
    "logs": ("Logs", ["RCA_LOKI_URL", "RCA_SPLUNK_URL"], "cannot quote the actual errors or stack traces"),
    "traces": ("Traces", ["RCA_TEMPO_URL", "RCA_JAEGER_URL"], "cannot pinpoint the failing hop in the transaction"),
}


class MeltSource(EvidenceSource):
    def __init__(self, domain: str):
        self.domain = domain
        self.name = domain
        self.domains = (domain,)
        self.label, self.env, self.gap = PLANNED[domain]

    def status(self) -> dict:
        configured = [e for e in self.env if os.getenv(e)]
        if configured:
            return {"state": "planned", "detail": f"{', '.join(configured)} set; connector not built yet. Add findings as observations."}
        return {"state": "not_connected", "detail": f"Not connected: {self.gap}. Add findings as observations."}

    def collect(self, ctx: dict) -> Collected:
        return Collected()


def melt_sources() -> list[MeltSource]:
    return [MeltSource(d) for d in PLANNED]
