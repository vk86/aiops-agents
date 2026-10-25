"""Trace sources: find every hop a transaction id went through.

Each source returns Span rows for one transaction id. Log sources (Splunk) and snapshot sources (AppDynamics)
give one row per service event with no parent links; trace sources (Dynatrace Grail spans, Jaeger, Tempo) give a
real span tree. trace.py merges them.

Env (see docs/TRANSACTION_TRACKING.md):
  Splunk      AIOPS_SPLUNK_URL, AIOPS_SPLUNK_TOKEN, optional AIOPS_SPLUNK_TXN_SEARCH ({id}, {lookback})
  Dynatrace   AIOPS_DYNATRACE_GRAIL_URL (https://<env>.apps.dynatrace.com), AIOPS_DYNATRACE_PLATFORM_TOKEN,
              optional AIOPS_DT_TXN_ATTR (span attribute holding the business transaction id, default txn.id)
  AppDynamics AIOPS_APPD_URL, AIOPS_APPD_TOKEN, AIOPS_APPD_APPS, AIOPS_APPD_TXN_COLLECTOR (data collector name)
  Jaeger      AIOPS_JAEGER_URL      Tempo  AIOPS_TEMPO_URL      (queried by the trace ids found above)
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from anomaly_agent.sources.base import SourceError, http, mock_mode

UTC = timezone.utc
LOOKBACK = os.getenv("AIOPS_TXN_LOOKBACK", "24h")


@dataclass
class Span:
    source: str
    service: str
    operation: str
    start: str                    # UTC "YYYY-MM-DD HH:MM:SS.mmm"
    duration_ms: float | None = None
    status: str = "ok"            # ok | error
    error: str = ""
    http_status: int | None = None
    span_id: str = ""
    parent_id: str = ""
    trace_id: str = ""
    kind: str = "span"            # span | log | snapshot
    link: str = ""
    attrs: dict = field(default_factory=dict)

    def dict(self) -> dict:
        return asdict(self)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:23]


def _from_epoch(sec: float) -> str:
    return _iso(datetime.fromtimestamp(sec, UTC))


class TraceSource:
    name, label, env = "base", "Base", ()

    def __init__(self, cfg: dict | None = None):
        self.service_map = {k.lower(): v for k, v in ((cfg or {}).get("service_map") or {}).items()}

    def svc(self, n: str) -> str:
        return self.service_map.get((n or "").lower(), n or "unknown")

    def configured(self) -> bool:
        return all(os.getenv(v) for v in self.env)

    def status(self) -> dict:
        if mock_mode():
            return {"state": "mock", "detail": "MOCK traces (SERVICENOW_MODE=mock)"}
        if self.configured():
            return {"state": "connected", "detail": ", ".join(f"{v}=set" for v in self.env)}
        return {"state": "not_connected", "detail": "Set " + ", ".join(self.env) + " in .env"}

    def find(self, txn_id: str, trace_ids: set[str]) -> list[Span]:
        if mock_mode():
            from .mocktraces import mock_spans
            return [s for s in mock_spans(txn_id) if s.source == self.name]
        if not self.configured():
            return []
        return self.fetch(txn_id, trace_ids)

    def fetch(self, txn_id: str, trace_ids: set[str]) -> list[Span]:
        raise NotImplementedError


SPLUNK_TXN_SEARCH = ('search index=* "{id}" earliest=-{lookback} '
                     '| eval service=coalesce(service, app, sourcetype), level=coalesce(level, log_level, severity) '
                     '| table _time service host level status http_status duration_ms trace_id span_id parent_span_id message '
                     '| sort 0 _time | head 500')


class SplunkTrace(TraceSource):
    name, label, env = "splunk", "Splunk logs", ("AIOPS_SPLUNK_URL", "AIOPS_SPLUNK_TOKEN")

    def fetch(self, txn_id, trace_ids):
        if not re.fullmatch(r"[\w.:\-]{4,128}", txn_id):
            raise SourceError("Transaction id has unexpected characters.")
        q = (os.getenv("AIOPS_SPLUNK_TXN_SEARCH") or SPLUNK_TXN_SEARCH).format(id=txn_id, lookback=LOOKBACK)
        r = http("POST", os.environ["AIOPS_SPLUNK_URL"].rstrip("/") + "/services/search/jobs/export",
                 headers={"Authorization": f"Bearer {os.environ['AIOPS_SPLUNK_TOKEN']}"}, data={"search": q, "output_mode": "json"},
                 timeout=90, verify=os.getenv("AIOPS_SPLUNK_VERIFY", "true").lower() != "false")
        out = []
        for line in r.text.splitlines():
            try:
                row = json.loads(line).get("result")
            except ValueError:
                continue
            if not row:
                continue
            level = str(row.get("level") or "").upper()
            code = int(row["http_status"]) if str(row.get("http_status") or row.get("status") or "").isdigit() else None
            code = code or (int(row["status"]) if str(row.get("status") or "").isdigit() else None)
            err = level in ("ERROR", "FATAL", "CRITICAL") or (code or 0) >= 500
            t = row.get("_time")
            start = _from_epoch(float(t)) if t and str(t).replace(".", "").isdigit() else str(t or "")[:23].replace("T", " ")
            out.append(Span("splunk", self.svc(row.get("service")), (row.get("message") or "")[:80], start,
                            float(row["duration_ms"]) if row.get("duration_ms") else None, "error" if err else "ok",
                            (row.get("message") or "")[:300] if err else "", code, row.get("span_id") or "", row.get("parent_span_id") or "",
                            row.get("trace_id") or "", "log", attrs={"host": row.get("host"), "level": level}))
        return out


class DynatraceTrace(TraceSource):
    """Dynatrace Grail: fetch spans for the transaction id (trace id or business attribute)."""
    name, label, env = "dynatrace", "Dynatrace spans", ("AIOPS_DYNATRACE_GRAIL_URL", "AIOPS_DYNATRACE_PLATFORM_TOKEN")

    def fetch(self, txn_id, trace_ids):
        if not re.fullmatch(r"[\w.:\-]{4,128}", txn_id):
            raise SourceError("Transaction id has unexpected characters.")
        attr = os.getenv("AIOPS_DT_TXN_ATTR", "txn.id")
        ids = sorted({txn_id} | trace_ids)
        cond = " or ".join([f'trace.id == "{i}"' for i in ids] + [f'`{attr}` == "{txn_id}"'])
        base = os.environ["AIOPS_DYNATRACE_GRAIL_URL"].rstrip("/")
        hdr = {"Authorization": f"Bearer {os.environ['AIOPS_DYNATRACE_PLATFORM_TOKEN']}"}
        q = (f"fetch spans, from:now()-{LOOKBACK} | filter {cond} | fields start_time, duration, span.id, span.parent_id, "
             "trace.id, service.name, span.name, span.status_code, http.response.status_code, exception.message | limit 500")
        r = http("POST", f"{base}/platform/storage/query/v1/query:execute", headers=hdr, json_body={"query": q}).json()
        for _ in range(20):  # long queries return a token to poll
            if r.get("state") == "SUCCEEDED" or "result" in r:
                break
            time.sleep(0.5)
            r = http("GET", f"{base}/platform/storage/query/v1/query:poll", headers=hdr,
                     params={"request-token": r.get("requestToken")}).json()
        recs = (r.get("result") or {}).get("records", [])
        out = []
        for x in recs:
            err = str(x.get("span.status_code", "")).lower() == "error" or int(x.get("http.response.status_code") or 0) >= 500
            st = str(x.get("start_time", ""))[:23].replace("T", " ")
            dur = x.get("duration")
            out.append(Span("dynatrace", self.svc(x.get("service.name")), x.get("span.name") or "", st,
                            round(float(dur) / 1e6, 1) if dur else None, "error" if err else "ok", x.get("exception.message") or "",
                            int(x["http.response.status_code"]) if x.get("http.response.status_code") else None,
                            x.get("span.id") or "", x.get("span.parent_id") or "", x.get("trace.id") or ""))
        return out


class AppDynamicsTrace(TraceSource):
    """AppDynamics request snapshots filtered by a business data collector that holds the transaction id."""
    name, label = "appdynamics", "AppDynamics snapshots"
    env = ("AIOPS_APPD_URL", "AIOPS_APPD_TOKEN", "AIOPS_APPD_APPS", "AIOPS_APPD_TXN_COLLECTOR")

    def fetch(self, txn_id, trace_ids):
        base = os.environ["AIOPS_APPD_URL"].rstrip("/")
        hdr = {"Authorization": f"Bearer {os.environ['AIOPS_APPD_TOKEN']}"}
        out = []
        for app in [a.strip() for a in os.environ["AIOPS_APPD_APPS"].split(",") if a.strip()]:
            tiers = {t.get("id"): t.get("name") for t in http("GET", f"{base}/controller/rest/applications/{quote(app)}/tiers",
                                                               headers=hdr, params={"output": "JSON"}).json()}
            snaps = http("GET", f"{base}/controller/rest/applications/{quote(app)}/request-snapshots", headers=hdr, params={
                "time-range-type": "BEFORE_NOW", "duration-in-mins": 1440, "output": "JSON",
                "data-collector-name": os.environ["AIOPS_APPD_TXN_COLLECTOR"], "data-collector-type": "Business Data",
                "data-collector-value": txn_id}).json()
            for s in snaps:
                err = bool(s.get("errorOccurred")) or s.get("userExperience") == "ERROR"
                t = s.get("serverStartTime") or s.get("localStartTime")
                out.append(Span("appdynamics", self.svc(tiers.get(s.get("applicationComponentId"), str(s.get("applicationComponentId")))),
                                s.get("url") or s.get("businessTransactionName") or "", _from_epoch(t / 1000) if t else "",
                                s.get("timeTakenInMilliSecs"), "error" if err else "ok", s.get("errorSummary") or "",
                                kind="snapshot", link=s.get("deepLinkUrl") or "", attrs={"guid": s.get("requestGUID"),
                                                                                       "experience": s.get("userExperience")}))
        return out


class _NoMock(TraceSource):
    """Trace backends with no mock stream: shown as not connected in mock mode."""

    def status(self) -> dict:
        if mock_mode():
            return {"state": "not_connected", "detail": "Not used in the mock (Dynatrace spans stand in)"}
        return super().status()

    def find(self, txn_id, trace_ids):
        return [] if mock_mode() else super().find(txn_id, trace_ids)


class JaegerTrace(_NoMock):
    name, label, env = "jaeger", "Jaeger traces", ("AIOPS_JAEGER_URL",)

    def fetch(self, txn_id, trace_ids):
        out = []
        for tid in _trace_ids(txn_id, trace_ids):
            try:
                d = http("GET", os.environ["AIOPS_JAEGER_URL"].rstrip("/") + f"/api/traces/{tid}").json()
            except SourceError:
                continue
            for tr in d.get("data", []):
                procs = tr.get("processes", {})
                for s in tr.get("spans", []):
                    tags = {t["key"]: t.get("value") for t in s.get("tags", [])}
                    code = tags.get("http.status_code") or tags.get("http.response.status_code")
                    err = bool(tags.get("error")) or int(code or 0) >= 500
                    parent = next((r["spanID"] for r in s.get("references", []) if r.get("refType") == "CHILD_OF"), "")
                    logs = " ".join(str(f.get("value")) for lg in s.get("logs", []) for f in lg.get("fields", []) if f.get("key") in ("message", "error.object"))
                    out.append(Span("jaeger", self.svc(procs.get(s.get("processID"), {}).get("serviceName")), s.get("operationName", ""),
                                    _from_epoch(s["startTime"] / 1e6), round(s.get("duration", 0) / 1000, 1), "error" if err else "ok",
                                    logs[:300], int(code) if code else None, s.get("spanID", ""), parent, s.get("traceID", tid)))
        return out


class TempoTrace(_NoMock):
    name, label, env = "tempo", "Tempo traces", ("AIOPS_TEMPO_URL",)

    def fetch(self, txn_id, trace_ids):
        out = []
        for tid in _trace_ids(txn_id, trace_ids):
            try:
                d = http("GET", os.environ["AIOPS_TEMPO_URL"].rstrip("/") + f"/api/traces/{tid}").json()
            except SourceError:
                continue
            for rs in d.get("batches", d.get("resourceSpans", [])):
                res = {a["key"]: list(a.get("value", {}).values())[0] for a in rs.get("resource", {}).get("attributes", []) if a.get("value")}
                for ss in rs.get("scopeSpans", rs.get("instrumentationLibrarySpans", [])):
                    for s in ss.get("spans", []):
                        at = {a["key"]: list(a.get("value", {}).values())[0] for a in s.get("attributes", []) if a.get("value")}
                        code = at.get("http.status_code") or at.get("http.response.status_code")
                        err = (s.get("status", {}).get("code") in (2, "STATUS_CODE_ERROR")) or int(code or 0) >= 500
                        st, en = int(s.get("startTimeUnixNano", 0)), int(s.get("endTimeUnixNano", 0))
                        out.append(Span("tempo", self.svc(res.get("service.name")), s.get("name", ""), _from_epoch(st / 1e9),
                                        round((en - st) / 1e6, 1) if en else None, "error" if err else "ok",
                                        s.get("status", {}).get("message", ""), int(code) if code else None,
                                        _hex(s.get("spanId", "")), _hex(s.get("parentSpanId", "")), _hex(s.get("traceId", tid))))
        return out


def _hex(v: str) -> str:
    """OTLP JSON ids are base64 in some Tempo versions; keep hex as is."""
    if not v or re.fullmatch(r"[0-9a-fA-F]+", v):
        return v or ""
    import base64
    try:
        return base64.b64decode(v).hex()
    except ValueError:
        return v


def _trace_ids(txn_id: str, found: set[str]) -> list[str]:
    ids = set(found)
    if re.fullmatch(r"[0-9a-fA-F]{16}|[0-9a-fA-F]{32}", txn_id):
        ids.add(txn_id)
    return sorted(ids)[:5]


def all_sources() -> list[TraceSource]:
    from anomaly_agent.sources.base import load_config
    cfg = load_config()
    # log/snapshot sources first: they reveal trace ids for the trace backends
    return [SplunkTrace(cfg), AppDynamicsTrace(cfg), DynatraceTrace(cfg), JaegerTrace(cfg), TempoTrace(cfg)]
