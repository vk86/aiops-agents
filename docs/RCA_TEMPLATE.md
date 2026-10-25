# AIOps RCA — capability and template (v1.0)

AIOps RCA is a **separate capability** from the incident console. It has its own agent (prompt, tools,
memory), its own API (`/api/rca-agent/*`) and its own UI, the **RCA Workbench** (`/rca`). It shares only
plumbing with the incident agent: the ServiceNow client, the LLM setting and the approval gate.

```
 Evidence sources                 RCA engine                         Output
 ───────────────────              ───────────────────────            ──────────────────────────
 ServiceNow (ITSM)   connected ─┐  collect → normalise (Evidence)     RCA report (this template)
 Git (commits / PRs) optional  ─┤  rank hypotheses + confidence  ──▶  Markdown / Workbench view
 Release / CI-CD     optional  ─┤  timeline + TTD/TTA/TTM/TTR         Publish to ServiceNow
 Metrics/Events/Logs/Traces    ─┤  coverage + evidence gaps            (work note + attachment +
   (MELT)            planned    │  LLM narrative from evidence only    problem cause notes,
 Engineer observations (OBS-n) ─┘                                      approval required)
```

Run it inside the console (`start.bat` → http://localhost:8001/rca) or on its own:
`uvicorn rca_agent.api:app --port 8002` → http://localhost:8002/rca.

## Principles

1. **Evidence first.** Every claim cites an evidence id (`CHG0098871`, `NOTE-2`, `AUD-1`, `PR-412`, `DEP-…`, `OBS-1`).
2. **No invention.** Unknowns go to *Open questions*. Confidence below 30% → root cause *Undetermined*.
3. **Show coverage.** The report states which evidence domains were checked and which were missing,
   so a reader knows how far to trust it.
4. **Human in the loop.** Engineers add observations; publishing to ServiceNow needs approval.
5. **Deterministic skeleton, LLM narrative.** Facts (timeline, metrics, hypotheses, evidence) are computed;
   the LLM only writes prose from them. Without an LLM the report still renders from a template.

## Sections

| # | Section | Filled by | Content |
|---|---|---|---|
| 1 | Incident details | evidence | Number, title, priority, CI/service, group, owner, status, dates |
| 2 | Executive summary | narrative | 2–3 sentences: what happened, impact, cause, current state |
| 3 | Customer & business impact | narrative | Who/what was affected, how much, how long (estimates marked) |
| 4 | Detection & response metrics | evidence | Impact start, detected, acknowledged, mitigated, resolved; TTD / TTA / TTM / TTR |
| 5 | Timeline (UTC) | evidence | Changes, deployments, commits, related incidents, notes, field changes, observations |
| 6 | Root cause | narrative | Statement, category (taxonomy below), confidence, evidence ids |
| 7 | Hypotheses considered | evidence | Ranked causes with reasons, confidence, evidence, next check |
| 8 | Contributing factors | narrative | What made it worse or slower to detect/fix |
| 9 | 5 whys | narrative | Symptom → fixable cause; assumptions marked |
| 10 | Evidence | evidence | Table: id, source, kind, finding, time |
| 11 | Evidence coverage | evidence | Per domain: connected / planned / not connected, item count |
| 12 | Corrective & preventive actions (CAPA) | narrative | Action, type (immediate / short-term / long-term), owner team, priority |
| 13 | Lessons learned | narrative | What went well, what to improve |
| 14 | Open questions | narrative | What is unknown and which evidence would answer it |
| 15 | Review & sign-off | evidence | Prepared by, status (draft → reviewed → approved), reviewers |

Machine-readable form: `GET /api/rca-agent/template` (from `src/rca_agent/template.py`).

## Root-cause taxonomy

Change / release · Code defect · Configuration · Capacity / performance · Dependency / third party ·
Infrastructure · Data · Security · Process / human · Known error / recurring · Undetermined

## Evidence domains

| Domain | What it answers | Source today | How to connect |
|---|---|---|---|
| ITSM (ServiceNow) | What changed on the CI and its neighbours, who did what, related incidents, known errors, past fixes | **Connected** — `incident`, `sys_journal_field`, `sys_audit`, `change_request`, `cmdb_rel_ci`, `problem` | Already uses `.env` ServiceNow settings |
| Code (Git) | Which commits/PRs merged on the service before impact | GitHub (`sources/github.py`) | `RCA_GITHUB_TOKEN` in `.env` + `ci_map` in `rca_config.json` |
| Release / CI-CD | Which deployments reached production in the window | GitHub Deployments | Same as Git; Harness / Jenkins / Azure DevOps next |
| Metrics | When error rate / latency / saturation moved — the real impact start | Planned (`sources/melt.py`) | `RCA_PROMETHEUS_URL` / `RCA_DYNATRACE_URL` / `RCA_DATADOG_SITE` |
| Events / alerts | Which alerts fired, and when (detection time) | Planned | ServiceNow Event Management (`em_alert`) or `RCA_PAGERDUTY_TOKEN` |
| Logs | The actual errors and stack traces | Planned | `RCA_LOKI_URL` / `RCA_SPLUNK_URL` |
| Traces | Which hop in the business transaction failed | Planned | `RCA_TEMPO_URL` / `RCA_JAEGER_URL` |
| Engineer observations | Anything a responder saw in a tool not yet connected | **Connected** — Workbench "Add an observation" or the agent's `add_observation` | — |

Until MELT is connected, responders paste the key finding as an observation (for example
*"eligibility-svc 5xx rose from 0.3% to 7% at 02:08 UTC"* with time `2026-10-21 02:08`). It becomes `OBS-n`,
appears on the timeline, sets the impact start for TTD, and re-ranks the hypotheses.

### Adding a source

Subclass `EvidenceSource` (`src/rca_agent/sources/base.py`): set `name`, `domains`, `label`, implement
`status()` and `collect(ctx) -> Collected`. Return `Evidence` items; return change-like rows in
`Collected.changes` (with `kind` = `deployment` or `commit`) to have them ranked as candidate causes.
Register it in `RCAEngine.__init__`.

## How hypotheses are ranked

| Hypothesis | Signals |
|---|---|
| Change / deployment / commit | Ended shortly before the incident opened (closer = stronger), on the same CI or a connected CI, words shared with the incident, risk, mentioned in notes |
| Dependency | Incidents opened on connected CIs around the same time (upstream first = stronger) |
| Known problem | Open problem / known error on the CI or with matching symptoms |
| Recurring | Similar resolved incidents and how they were fixed (capped at 50%) |

Engineer observations add weight to hypotheses whose terms they mention.

## Publishing

`publish_rca` (Workbench **Publish to ServiceNow…**) shows an approval card, then:
a work note with the summary, root cause and top actions; the full report attached as `RCA-<INC>.md`;
optionally the root cause written to a problem's *Cause notes*.
