"""Splunk: error log signatures per service, flagging new signatures (not seen in the baseline) and bursts.

Env: AIOPS_SPLUNK_URL (https://splunk:8089), AIOPS_SPLUNK_TOKEN (bearer token), optional AIOPS_SPLUNK_VERIFY=false,
AIOPS_SPLUNK_BASELINE (default -24h), AIOPS_SPLUNK_SEARCH to replace the default search. The search must return
fields: service, sig, n (count in window), base (count in baseline), last (epoch). Placeholders: {minutes} {baseline}.
API: POST /services/search/jobs/export (output_mode=json)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .base import Signal, SignalSource, http

DEFAULT_SEARCH = (
    'search index=* (log_level=ERROR OR level=ERROR OR severity=ERROR) earliest={baseline} '
    '| eval service=coalesce(service, app, sourcetype) '
    '| eval sig=replace(substr(coalesce(message, _raw), 1, 120), "\\d+", "N") '
    '| eval recent=if(_time>=relative_time(now(), "-{minutes}m"), 1, 0) '
    '| stats sum(recent) as n, sum(eval(1-recent)) as base, max(_time) as last by service, sig '
    '| where n>0 | sort - n | head 100')


def _minutes(spec: str) -> float:
    spec = spec.strip().lstrip("-")
    unit = spec[-1]
    return float(spec[:-1]) * {"m": 1, "h": 60, "d": 1440}.get(unit, 1)


class SplunkSource(SignalSource):
    name, label = "splunk", "Splunk"
    domains = ("log",)
    env = ("AIOPS_SPLUNK_URL", "AIOPS_SPLUNK_TOKEN")

    def fetch(self, window_minutes: int) -> list[Signal]:
        baseline = os.getenv("AIOPS_SPLUNK_BASELINE", "-24h")
        search = (os.getenv("AIOPS_SPLUNK_SEARCH") or DEFAULT_SEARCH).format(minutes=window_minutes, baseline=baseline)
        r = http("POST", os.environ["AIOPS_SPLUNK_URL"].rstrip("/") + "/services/search/jobs/export",
                 headers={"Authorization": f"Bearer {os.environ['AIOPS_SPLUNK_TOKEN']}"},
                 data={"search": search, "output_mode": "json"}, timeout=90,
                 verify=os.getenv("AIOPS_SPLUNK_VERIFY", "true").lower() != "false")
        windows = max(1.0, (_minutes(baseline) - window_minutes) / window_minutes)
        out = []
        for line in r.text.splitlines():
            try:
                row = json.loads(line).get("result")
            except ValueError:
                continue
            if not row:
                continue
            n, base = float(row.get("n") or 0), float(row.get("base") or 0)
            rate = base / windows
            new = base == 0
            bad = (new and n >= 3) or n >= 4 * rate + 5
            last = row.get("last")
            out.append(Signal("splunk", "log", self.svc(row.get("service")), "New error signature" if new else "Error signature",
                              n, round(rate, 1), "events", "major" if bad else "info", bad,
                              datetime.fromtimestamp(float(last), timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if last else "",
                              detail=row.get("sig", "")[:160], feature="new_error_signatures" if new else "error_log_ratio"))
        return out
