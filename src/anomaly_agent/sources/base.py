"""Common types for anomaly signal sources."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests


class SourceError(RuntimeError):
    pass


@dataclass
class Signal:
    source: str             # dynatrace | appdynamics | splunk | servicenow
    domain: str             # kpi | metric | trace | log | event
    service: str            # normalised service / CI name (see service_map)
    name: str               # what was measured or raised, e.g. "Error rate", "Health rule violation"
    value: float | None = None
    baseline: float | None = None
    unit: str = ""
    severity: str = "minor"  # critical | major | minor | info
    anomalous: bool = True
    time: str = ""          # UTC "YYYY-MM-DD HH:MM:SS"
    detail: str = ""
    link: str = ""
    feature: str = ""       # feature name this signal feeds (error_rate_pct, p95_ms, ...)
    extra: dict = field(default_factory=dict)

    def dict(self) -> dict:
        return asdict(self)


def mock_mode() -> bool:
    from incident_agent.config import settings
    return settings.sn_mode == "mock"


def load_config() -> dict:
    """aiops_config.json (or AIOPS_CONFIG): service_map (platform name -> CMDB CI name) and kpis."""
    p = Path(os.getenv("AIOPS_CONFIG", "aiops_config.json"))
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError as e:
            raise SourceError(f"{p} is not valid JSON: {e}") from e
    return {}


class SignalSource:
    name = "base"
    label = "Base"
    domains: tuple[str, ...] = ()
    env: tuple[str, ...] = ()       # required environment variables

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}
        self.service_map = {k.lower(): v for k, v in (self.cfg.get("service_map") or {}).items()}

    def configured(self) -> bool:
        return all(os.getenv(v) for v in self.env)

    def status(self) -> dict:
        if mock_mode():
            return {"state": "mock", "detail": "MOCK stream (SERVICENOW_MODE=mock)"}
        if self.configured():
            return {"state": "connected", "detail": self.describe()}
        return {"state": "not_connected", "detail": "Set " + ", ".join(self.env) + " in .env"}

    def describe(self) -> str:
        return ", ".join(f"{v}=set" for v in self.env)

    def svc(self, name: str) -> str:
        return self.service_map.get((name or "").lower(), name or "unknown")

    def collect(self, window_minutes: int) -> list[Signal]:
        if mock_mode():
            from .mockdata import mock_signals
            return [s for s in mock_signals(window_minutes) if s.source == self.name]
        if not self.configured():
            return []
        return self.fetch(window_minutes)

    def fetch(self, window_minutes: int) -> list[Signal]:  # live implementation
        raise NotImplementedError


def http(method: str, url: str, *, headers: dict | None = None, params=None, data=None, json_body=None,
         timeout: int = 30, verify: bool = True, stream: bool = False) -> requests.Response:
    try:
        r = requests.request(method, url, headers=headers, params=params, data=data, json=json_body,
                             timeout=timeout, verify=verify, stream=stream)
    except requests.RequestException as e:
        raise SourceError(f"{url.split('?')[0]}: {e}") from e
    if r.status_code >= 400:
        raise SourceError(f"{url.split('?')[0]} returned {r.status_code}: {r.text[:200]}")
    return r
