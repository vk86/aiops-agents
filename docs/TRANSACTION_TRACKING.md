# AIOps transaction tracking

Give the transaction agent a **transaction / correlation / trace id**. It follows that one transaction across your
systems, finds where it failed, and says whether the cause is **upstream or downstream** of the system where the
problem was reported.

```
transaction id ──▶ Splunk logs · AppDynamics snapshots   (find every service that logged / handled the id,
                                                           and the trace ids it carried)
               ──▶ Dynatrace spans · Jaeger · Tempo      (the real span tree for those trace ids)
                        │
                        ▼
          call chain (parent links; or inferred from time order + CMDB "depends on")
                        │
     failure point = deepest failing hop      origin = where the cause most likely is
                        │
     direction vs the reported system (you / ServiceNow incident that mentions the id / entry point)
                        │
     ServiceNow per hop: owner, open incidents, changes → suspect changes on the failing path
```

## How the origin is decided

| Failure point looks like | Origin | Example |
|---|---|---|
| Timed out calling a dependency | **The dependency** (slower than the caller's timeout) | eligibility-svc timed out after 200 ms; plan-catalog-svc answered in 640 ms |
| Rejected the request: HTTP 4xx, validation error | **Its caller** (sent a bad request) | enrollment-api 400 "planId is required"; enrollment-ui sent it |
| Any other error (5xx, exception) | **The failure point itself** | payment-gw TLS handshake: certificate expired |

**Direction** is relative to the system where the problem was reported:
- **downstream**: the origin is something the reported system calls (directly or further down)
- **upstream**: the origin calls the reported system
- **same**: the origin is the reported system

The reported system is what you type in "Reported at"; if empty, the CI of the ServiceNow incident (or work note)
that mentions the id; otherwise the entry point of the trace.

When only logs are available (no trace backend), parent links are inferred from the order services logged the id and
CMDB "depends on" relations, and the hops are marked *inferred*.

## Sources

| Source | What it gives | Configuration (`.env`) |
|---|---|---|
| Splunk | Every log line with the id: service, level, status, duration, trace/span ids | `AIOPS_SPLUNK_URL`, `AIOPS_SPLUNK_TOKEN`; optional `AIOPS_SPLUNK_TXN_SEARCH` (placeholders `{id}`, `{lookback}`; fields `_time service level status http_status duration_ms trace_id span_id parent_span_id message`) |
| AppDynamics | Request snapshots per tier, filtered by a business data collector holding the id | `AIOPS_APPD_URL`, `AIOPS_APPD_TOKEN`, `AIOPS_APPD_APPS`, `AIOPS_APPD_TXN_COLLECTOR` |
| Dynatrace | Spans from Grail (`fetch spans`), by trace id or a span attribute holding the business id | `AIOPS_DYNATRACE_GRAIL_URL` (`https://<env>.apps.dynatrace.com`), `AIOPS_DYNATRACE_PLATFORM_TOKEN`, `AIOPS_DT_TXN_ATTR` (default `txn.id`) |
| Jaeger | Span tree by trace id | `AIOPS_JAEGER_URL` |
| Tempo | Span tree by trace id (OTLP JSON) | `AIOPS_TEMPO_URL` |
| ServiceNow | Incidents/work notes mentioning the id; per hop: CMDB owner, open incidents, changes | existing ServiceNow settings |

`AIOPS_TXN_LOOKBACK` (default `24h`) bounds the search. `service_map` in `aiops_config.json` maps platform service
names to CMDB CI names. Trace ids found in Splunk/AppDynamics are used to query Dynatrace, Jaeger and Tempo, so a
business id works even when the trace backends only know trace ids.

## UI and API

AIOps → **Transactions** → **Track ID**: id + optional "Reported at" →
- verdict with UPSTREAM/DOWNSTREAM and the origin type;
- **call-flow diagram**: callers above, called systems below; red arrows are failing calls; each system is tagged
  UPSTREAM / REPORTED / DOWNSTREAM relative to the reported system and marked ORIGIN / FAILURE POINT;
- **click a system** → its spans for this transaction, ServiceNow owner / incidents / changes, and **its own log around
  that moment** (Splunk, ±2 min, this transaction's lines highlighted);
- propagation path, suspect changes, timing waterfall, ServiceNow per hop, where ServiceNow mentions the id;
- actions: **Add trace to INC…** (work note, approval), **Investigate with RCA →**. Deep link: `/aiops#track/<id>`.

**Service map** tab: the static dependency map of a business service from the CMDB (what it depends on), coloured by
open incidents and recent changes, as the same diagram; click a system for its recent log (last 30 min) and incidents.
It is not a single transaction; use Track ID for that.

System log search (Splunk) can be replaced with `AIOPS_SPLUNK_SERVICE_SEARCH` (placeholders `{svc}`, `{earliest}`,
`{latest}`, `{limit}`; fields `_time host level message`).

| Endpoint | Purpose |
|---|---|
| `GET /api/txn-agent/track?txn_id=&reported_at=` | Full tracking result |
| `GET /api/txn-agent/logs?service=&around=&minutes=&txn_id=` | A system's own log lines around a moment |
| `POST /api/txn-agent/attach` | `{txn_id, number, reported_at}` → approval card for the work note |
| `GET /api/txn-agent/coverage` | Source status |
| `GET /api/txn-agent/trace?name=` · `/transactions` | Business-service path view |

Mock mode (`SERVICENOW_MODE=mock`) ships four ids: `TXN-ENR-20261024-0207` (downstream slow dependency),
`TXN-ENR-20261024-0311` (upstream bad request), `TXN-PAY-20261024-0412` (error in payment-gw),
`TXN-ENR-20261024-0199` (healthy).
