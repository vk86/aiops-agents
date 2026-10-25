# AIOps for SRE: reliability and change guard

**AIOps for SRE: an AI engineering platform that keeps services inside their SLOs.** Agents detect from telemetry,
trace failures to their origin, guard changes before they ship, and turn every incident into an engineering fix.
Humans stay in control and every result is measured.

| Pillar | Agent | What makes it SRE, not ITOps |
|---|---|---|
| Detect | Anomalies | Telemetry + your predictive model; detection precision and lead time measured against incidents |
| Diagnose | Transactions, Root cause | Follow one request across systems (upstream/downstream origin); evidence-based RCA with MTTD/MTTR |
| Prevent | **Change guard**, **Reliability** | Changes scored on error budget, blast radius and change failure rate *before* deploy; SLO burn-rate alerting |
| Improve | Root cause, Reliability | Corrective actions, budget cost per incident, change failure rate, scorecard per service |
| Platform | all | Shared connectors, approval gate, audit via work notes, any LLM, evaluation built in |

## Reliability agent (SLOs and error budgets)

Everything comes from one question asked of an SLI source: *what fraction of events were bad between t1 and t2?*

- **Error budget** = 1 − objective. 99.9% over 28 days allows 0.1% bad events (≈ 40 min of full outage).
- **Budget consumed** = bad ratio over the window ÷ budget. Remaining = 100% − consumed.
- **Burn rate** over a window = bad ratio in that window ÷ budget. 1× spends exactly the budget over the SLO window.
- **Alerts** (multi-window, multi-burn-rate, from the Google SRE workbook):
  - page: 1h ≥ 14.4× and 5m ≥ 14.4×, or 6h ≥ 6× and 30m ≥ 6×
  - ticket: 24h ≥ 3× and 2h ≥ 3×, or 72h ≥ 1× and 6h ≥ 1×
- **Policy**: > 50% left normal · 25–50% caution · 0–25% restrict · ≤ 0 freeze (reliability fixes only). The change guard enforces it.
- **Incident cost**: bad events during each ServiceNow incident on the CI ÷ the window's budget (overlapping duplicates share one window).
- **Burn after change**: burn rate 6h after a change vs 6h before; ≥ 3× marks the change as a suspect.
- **Scorecard**: per service worst SLO, policy, P1/P2 (28d), MTTR, change failure rate and deploys/week (90d).

### SLO definitions (`aiops_config.json`)

```json
"slos": [
  {"service": "enrollment-api", "name": "Enrollment submit availability", "sli": "availability",
   "objective": 99.9, "window_days": 28, "dynatrace_slo_id": "a1b2c3d4-..."},
  {"service": "eligibility-svc", "name": "Eligibility check latency < 300 ms", "sli": "latency",
   "objective": 99.0, "window_days": 28,
   "splunk_search": "search index=app service=eligibility-svc earliest={earliest} latest={latest} | stats count(eval(duration_ms<300)) as good, count as total"}
]
```

- `dynatrace_slo_id`: evaluated for any timeframe via `GET /api/v2/slo/{id}?timeFrame=GTF&from=&to=` (token scope `slo.read`).
- `splunk_search`: must return `good` and `total`; `{earliest}` / `{latest}` are epoch seconds.
- `service` must match the CMDB CI name (owner, incidents, changes come from ServiceNow).
- `AIOPS_SLO_SERIES=true` adds the 28-day budget chart in live mode (one SLI query per day).

## Change guard

For each change (ServiceNow `change_request`), points add up to a 0–100 risk score:

| Factor | Points | Hard hold |
|---|---|---|
| Blast radius | 5 per business service above the CI (max 15), +10 critical service, +2 per dependent (max 5) | |
| Error budget | freeze 40 · restrict 25 · caution 10 | freeze |
| Change history | change failure rate × 60 (max 25), +10 similar failed change | |
| Current health | open P1/P2 on the CI 25, lower-priority open 5, anomaly agent raise/attach 15 | open P1/P2 |
| Collisions | 10 per overlapping change on the CI or neighbours (max 20) | |
| Timing | freeze window 30, peak hours 10 | freeze window |
| Hygiene | no backout plan 10, no test plan 5, emergency 5, ServiceNow risk High 10 / Moderate 5 | |

- **Change failure rate** = changes followed by a P1/P2 on the same CI within 24h (or closed unsuccessful) ÷ changes, 90 days.
- **Reliability fix** (rollback, revert, restore, fix, renew/rotate certificate, timeout increase): −15 points, and hard holds become **CANARY with SRE approval**, because shipping it lowers risk.
- **Recommendation**: HOLD (hard hold, or ≥ 60) · CANARY (30–59) · GO (< 30), with guardrails: canary steps, what to watch, rollback triggers (SLO burn ≥ 14.4×, error rate ≥ 2× baseline, new error signature, new P1/P2), pre-checks.
- **Verify after deploy**: SLO burn 6h before vs after, and incidents within 24h on the CI or what depends on it.
- **DORA**: deploy frequency, change failure rate, MTTR per service (lead time for changes needs Git/CI-CD, planned).
- **Post assessment**: work note on the change (approval required).

### Change policy (`aiops_config.json`)

```json
"change_policy": {
  "freeze_windows": [{"name": "Open enrollment", "from": "2026-11-01", "to": "2026-12-15",
                      "services": ["Member Enrollment"], "allow": "reliability fixes only"}],
  "peak_hours": "08-18", "peak_days": "Mon-Fri", "timezone": "America/Chicago",
  "critical_services": ["Member Enrollment", "Premium Payments", "Claims Processing"]
}
```

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/slo-agent/slos` · `/scorecard` · `/coverage` | SLOs with budget, burn, alerts, policy, incident cost; scorecard; sources |
| `GET /api/change-agent/upcoming?days=7` | Upcoming changes with score and recommendation |
| `GET /api/change-agent/assess?number=` · `/verify?number=` · `/dora` · `/policy` | Assessment, post-deploy check, DORA, policy |
| `POST /api/change-agent/post` | `{number}` → approval card for the work note |
| `POST /api/slo-agent/chat` · `/api/change-agent/chat` | Agent chat |
