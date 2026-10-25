# AIOps anomaly detection

Detection runs on **observability signals**, scored by **your predictive model**. ServiceNow is not the detector:
it supplies **context** (who owns it, what it affects, is a change running, is an incident already open),
**labels** for training, and the **place to act**.

```
Dynatrace · AppDynamics · Splunk  ──signals──▶  feature pipeline (per service)  ──▶  predictive model (score + why)
                                                         ▲                                   │
ServiceNow: CMDB owner + business services, change windows, open incidents ──context──┘      ▼
                                                                             decision per service
                       raise → em_event (Event Management) │ attach → work note on open incident
                       suppressed → change window open     │ watch → below threshold
ServiceNow P1/P2 incidents ──labels──▶ retraining      detections log ──▶ evaluation (precision, lead time)
```

## Signals collected

| Source | What | Feeds feature |
|---|---|---|
| Dynatrace | Open problems (Davis), service error rate and p95 response time vs their own recent baseline, business KPIs from metric selectors | `vendor_problems`, `error_rate_pct`, `error_rate_ratio`, `p95_ms`, `p95_ratio`, `kpi_drop_pct` |
| AppDynamics | Open health-rule violations on business transactions, tiers, nodes (AppDynamics dynamic baselines) | `health_violations` |
| Splunk | Error log signatures per service: new signatures (not in the baseline) and bursts | `new_error_signatures`, `error_log_ratio` |
| ServiceNow | Change window open on the CI, minutes since the last change, open incidents | `change_active`, `minutes_since_change`, `open_incidents` |
| All | Critical signals, independent sources agreeing | `critical_signals`, `sources_agreeing` |

Business KPIs (enrollments per minute, claim submissions, payment success) matter most; add them in `aiops_config.json`.

## Predictive model contract

```http
POST {AIOPS_MODEL_URL}
Authorization: Bearer {AIOPS_MODEL_TOKEN}        # optional
Content-Type: application/json

{
  "window_start": "2026-10-24 22:00:00",
  "window_end":   "2026-10-24 23:00:00",
  "services": [
    {"service": "enrollment-api", "ci": "enrollment-api",
     "features": {"error_rate_pct": 7.4, "error_rate_ratio": 24.7, "p95_ms": 2100, "p95_ratio": 8.75,
                  "kpi_drop_pct": 38, "vendor_problems": 1, "health_violations": 1, "critical_signals": 3,
                  "new_error_signatures": 0, "error_log_ratio": 181, "sources_agreeing": 3,
                  "change_active": 0, "minutes_since_change": 108, "open_incidents": 3}}
  ]
}
```

Response:

```json
{
  "model_version": "anomaly-xgb-2026.10",
  "results": [
    {"service": "enrollment-api", "score": 0.97, "anomaly": true, "threshold": 0.6,
     "top_features": [{"name": "error_rate_ratio", "value": 24.7, "contribution": 0.41}],
     "explanation": "Error rate 25x baseline with a KPI drop"}
  ]
}
```

- `anomaly` and `threshold` are optional; default threshold is `AIOPS_MODEL_THRESHOLD` (0.6).
- One service per call? Set `AIOPS_MODEL_BATCH=false`: the body becomes `{window_start, window_end, service, ci, features}` and the response is one result object.
- If your model expects different feature names, put a thin adapter in front of it, or tell us the mapping.
- Without a model the agent still works: a service is anomalous when 2+ independent sources agree or any source raises a critical signal.

## Decisions (why ServiceNow is connected)

| Decision | When | Action (needs approval) |
|---|---|---|
| **suppressed** | Anomalous, but an approved change window is open on the CI | None; re-check after the window. "Send event anyway" is available |
| **attach** | Anomalous and an incident is already open on the CI | Work note with the evidence on that incident (no duplicate incident) |
| **raise** | Anomalous and nothing open | Event to **Event Management** (`em_event`) so its de-duplication, correlation and alert rules apply. If Event Management is not installed: create an incident |
| **watch** | Signals below the threshold | None |

Each card also shows the CMDB owner (support group) and the business services above the CI (blast radius).

## Labels and evaluation

- `GET /api/anomaly-agent/labels.csv?days=90`: P1/P2 incidents with a CI as anomaly windows per service (`start_utc`, `end_utc`, `label`); label 0 when closed as not reproducible, duplicate or false positive. Join with your feature history to retrain.
- Every detection run logs anomalies to `data/anomaly_log.jsonl`. `GET /api/anomaly-agent/evaluate?days=30` scores them against P1/P2 incidents on the same CI within ±6h: precision, recall on watched CIs, median lead time (positive = detected before the incident was opened).

## Configuration

`.env`:

```
AIOPS_MODEL_URL=https://models.internal/anomaly/score
AIOPS_MODEL_TOKEN=
AIOPS_MODEL_THRESHOLD=0.6
AIOPS_MODEL_BATCH=true

AIOPS_DYNATRACE_URL=https://<env>.live.dynatrace.com
AIOPS_DYNATRACE_TOKEN=            # Api-Token: problems.read, metrics.read

AIOPS_APPD_URL=https://<account>.saas.appdynamics.com
AIOPS_APPD_TOKEN=                 # API client OAuth token
AIOPS_APPD_APPS=Enrollment,Claims

AIOPS_SPLUNK_URL=https://splunk.internal:8089
AIOPS_SPLUNK_TOKEN=
AIOPS_SPLUNK_BASELINE=-24h
# AIOPS_SPLUNK_SEARCH=...         # must return service, sig, n, base, last; placeholders {minutes} {baseline}
```

`aiops_config.json` (copy `aiops_config.example.json`): `service_map` maps platform names (Dynatrace service,
AppDynamics business transaction or tier, Splunk service field) to CMDB CI names; `kpis` adds business KPIs.

ServiceNow roles for the integration user: read `incident`, `change_request`, `cmdb_ci`, `cmdb_rel_ci`,
`cmdb_ci_service`; write `incident` (work notes) and `em_event` (role `evt_mgmt_integration`) if you send events.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/anomaly-agent/detect?window_minutes=60&refresh=true` | Signals, features, model scores, context, decisions |
| `POST /api/anomaly-agent/action/{event,attach,incident}` | Body `{service, window_minutes, number}`; returns an approval card |
| `GET /api/anomaly-agent/coverage` | Source status (model, Dynatrace, AppDynamics, Splunk, ServiceNow, em_alert) |
| `GET /api/anomaly-agent/evaluate` · `/labels.csv` · `/model-contract` | Evaluation, training labels, feature list |
| `GET /api/anomaly-agent/scan` · `/alerts` | ServiceNow incident-volume backstop, Event Management alert noise |
