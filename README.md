# ServiceNow Incident Agent (LangChain)

A tool-calling LangChain agent for incident management on ServiceNow. Ask in plain English; it reads, summarizes, triages and (with your approval) creates and updates incidents through the ServiceNow Table API.

```
you › show open P1 and P2 incidents for Network
  ↳ search_incidents(priority='<=2', assignment_group='Network')
you › summarize INC0000039 and check what changed on its CI
  ↳ summarize_incident(number='INC0000039')
  ↳ find_recent_changes(number='INC0000039')
you › create an incident: VPN down for Dallas office, high impact, high urgency, assign to Network
  ┌ Approval needed: Create incident
  │ short_description: VPN down for Dallas office
  │ impact: 1   urgency: 1   assignment_group: Network
  └ Proceed? [y/N] y
  ↳ create_incident(...)
Created INC0010012 (P1 - Critical), assigned to Network.
```

## Tools

| Tool | What it does | Writes? |
|---|---|---|
| `get_incident` | Full record by number | |
| `search_incidents` | Filter by text, state, priority (`2` or `<=2`), group, active | |
| `get_incident_activity` | Work notes and customer comments, oldest first | |
| `summarize_incident` | LLM summary: status, impact, actions so far, leads, next steps | |
| `find_similar_incidents` | Past incidents with matching words, with their resolutions | |
| `find_recent_changes` | Change requests on the same CI in the 48 h before the incident | |
| `list_assignment_groups` | Exact group names before assigning | |
| `create_incident` | New incident; priority derived from impact × urgency | asks first |
| `update_incident` | State, group, assignee, impact, urgency, short description | asks first |
| `add_work_note` | Internal work note, or customer-visible comment | asks first |
| `resolve_incident` | Resolution code + notes | asks first |

## Setup (Windows, VS Code)

1. Open this folder in VS Code (**File → Open Folder**). Install the recommended Python extension when prompted.
2. Terminal (**Ctrl+`**):
   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   pip install -e .
   copy .env.example .env
   ```
   macOS/Linux: `source .venv/bin/activate` and `cp .env.example .env`.
3. Edit `.env`: set `SERVICENOW_PASSWORD` and an LLM key (`OPENAI_API_KEY`, or the Azure / Anthropic settings with `LLM_MODEL` to match). The instance URL and user are already filled in for dev443878.
4. **Ctrl+Shift+P → Python: Select Interpreter →** `.venv`.

## Run

Use the **Run and Debug** panel (Ctrl+Shift+D) or the terminal:

| Step | VS Code launch config | Terminal |
|---|---|---|
| Check ServiceNow (no LLM needed) | `1. Check ServiceNow connection` | `python scripts/check_servicenow.py` |
| **Web UI** (opens the browser) | `2. Web UI (http://localhost:8001/aiops)` | `uvicorn incident_agent.api:app --app-dir src --port 8001 --reload` |
| Web UI on mock data | `3. Web UI on mock data (offline ServiceNow)` | `set SERVICENOW_MODE=mock` then the line above |
| Terminal chat | `4. Terminal chat` | `python -m incident_agent.cli` |
| Tests | Testing panel | `pytest -q` |

## Web UI

Open **http://localhost:8001/aiops** (`/` redirects there). Switch agents with **Incident agent | RCA agent** in the top bar: chat on the left, that agent's workspace tabs on the right.

- **Chat** (left): talk to the agent. Each reply shows the tools it called; click a tool chip to see its arguments. Incident numbers are links that open the incident on the right. Quick prompts sit above the input.
- **Approval cards**: create, update, note and resolve never run straight from the chat. They appear as amber cards with the exact fields. **Approve and write** sends them to ServiceNow and tells the agent the result. **Reject** writes nothing.
- **Incidents** (right): search and open incidents straight from ServiceNow, no LLM involved. You get fields, description, activity log, recent changes on the CI and similar incidents. **Summarize with agent**, **Triage** and **Add note…** send them to the chat.
- **Tool tester** (right): run any tool by hand with a form built from its schema. Use it to check the ServiceNow side before blaming the model. Write tools return an approval card here too.
- The top bar shows the instance, the logged-in user, the model and whether approvals are on. A red light means ServiceNow is unreachable; hover it for the reason.

The Incidents and Tool tester tabs work without an LLM key, so you can test ServiceNow first and add the key later.

API used by the page (also usable from the AIOps React app): `POST /api/chat`, `POST /api/approve`, `GET /api/incidents`, `GET /api/incidents/{number}`, `GET /api/tools`, `POST /api/tools/{name}`, `GET /api/health`.

## Incident management use cases (demo)

| Use case | Ask in chat, or click | Tool | ServiceNow data |
|---|---|---|---|
| Start-of-shift briefing | Opens automatically on the **Shift** tab · "Start my shift" | `shift_briefing` | open incidents, `task_sla` |
| Shift handover | **Handover** button · "Handover for the last 12 hours" | `shift_handover` | opened/resolved/updated + work notes |
| Who solved this before | Incident view → *Who solved this before* · "Who fixed incidents like INC…?" | `who_solved_this_before` | resolved/closed incidents, `resolved_by`, close notes |
| Duplicate detection | Incident view → *Possible duplicates* → **Link under…** (approval) | `find_duplicate_incidents`, `link_duplicate` | open incidents, `parent_incident` |
| Assignment suggestion | Incident view → *Routing* → **Apply…** (approval) | `suggest_assignment` | history by group, CI support group, workload |
| SLA at risk | Shift tab · "Which SLAs are about to breach?" | `sla_at_risk` | `task_sla` (estimated from priority if none) |
| Team health | "How is each team doing this month?" | `team_health` | open + resolved incidents |
| Problem candidates | "Recurring incidents that need a problem?" → create (approval) | `find_problem_candidates`, `create_problem` | incidents by CI/keywords, `problem` |
| **Root cause analysis** | Incident view → **Run RCA** · "Why did INC… happen?" → **Post as work note…** (approval) | `root_cause_analysis`, `record_rca` | `change_request` on the CI and connected CIs, `cmdb_rel_ci`, incidents on connected CIs, work notes, `sys_audit`, `problem`/known errors, past fixes |
| Summarize, notes, resolve | Incident view buttons · chat | existing tools | incident + journal |

### Data source: always your ServiceNow data
The top bar shows which source is in use: **LIVE** (API), **SNAPSHOT** (your exported records) or **MOCK DATA** (invented, only if you set it).

- **Live** (`SERVICENOW_MODE=live`, default): needs API access. Newer instances block Basic Auth, so use OAuth client credentials (`SERVICENOW_AUTH=oauth`, see below).
- **Snapshot** (`start-snapshot.bat`): the same real records, exported from ServiceNow in your browser while logged in. Save the downloads into the `snapshot\` folder:
  - Incidents (required): `https://<instance>/incident_list.do?CSV&sysparm_default_export_fields=all`
  - Work notes: `https://<instance>/sys_journal_field_list.do?CSV&sysparm_query=name=incident&sysparm_default_export_fields=all`
  - Changes: `https://<instance>/change_request_list.do?CSV&sysparm_default_export_fields=all`
  - SLAs: `https://<instance>/task_sla_list.do?CSV&sysparm_query=task.sys_class_name=incident&sysparm_default_export_fields=all`

  PDI demo incidents are years old, so snapshot mode shifts all timestamps (keeping their spacing) so the newest incident was opened 30 minutes ago; `SNAPSHOT_REBASE=false` turns this off. Writes in snapshot mode change the in-memory copy only.

### AIOps RCA agent: a separate capability in the same UI
In **/aiops**, choose **RCA agent** in the top bar (or **Investigate with RCA agent →** on any incident; `/aiops#rca/INC0012345` links straight to one). Chat with the RCA agent on the left; its workspace on the right has **Investigation**, **Evidence** and **Report** tabs. It has its own prompt, tools and memory and only shares the ServiceNow client, LLM and approval gate with the incident agent. Old `/rca` links redirect here. To run it alone: `uvicorn rca_agent.api:app --app-dir src --port 8002` (standalone workbench page).

- **Investigate**: collects evidence from every source, ranks causes with confidence, builds the UTC timeline and TTD / TTA / TTM / TTR.
- **Evidence coverage**: shows which domains were checked. ServiceNow is connected today; Git and releases connect with `RCA_GITHUB_TOKEN` + `rca_config.json`; metrics, events, logs and traces (MELT) are planned.
- **Add an observation**: paste what a dashboard, log search or trace showed. It becomes `OBS-n` evidence and re-ranks the causes.
- **Draft report**: the full report in the AIOps RCA template (15 sections, CAPA, 5 whys, open questions). **Copy Markdown** or **Publish to ServiceNow…** (approval: work note + `RCA-<INC>.md` attachment + optional problem cause notes).
- **Ask the RCA agent**: an investigator that asks for the evidence it is missing.

Template, evidence domains and how to add a source: [docs/RCA_TEMPLATE.md](docs/RCA_TEMPLATE.md).

### AIOps anomaly agent (top bar → Anomalies)
Detection runs on observability signals scored by your predictive model; ServiceNow supplies context, labels and the place to act. Tabs:
- **Signals** (default): Dynatrace problems, error rate, p95 and KPIs; AppDynamics health-rule violations; Splunk new error signatures. Features per service go to your model (`AIOPS_MODEL_URL`), then ServiceNow decides: **raise** (event to Event Management), **attach** (evidence on the incident already open), **suppressed** (change window open), **watch**. Actions need approval.
- **Volume**: ServiceNow incident-volume backstop (user-reported impact monitors miss).
- **Alert noise**: Event Management alert de-duplication, bursts, steady noise.
- **Sources**: connector status, detection quality (precision, lead time vs incidents), training labels CSV, model feature list.

Model contract, signals, decisions and configuration: [docs/ANOMALY_DETECTION.md](docs/ANOMALY_DETECTION.md). In mock mode every source and the model serve labelled MOCK data.

### AIOps transaction agent (top bar → Transactions)
- **Track ID** (default): give a transaction / correlation / trace id. It pulls the id's spans and logs from Splunk, AppDynamics, Dynatrace, Jaeger and Tempo, rebuilds the call chain, finds the **failure point** and the **origin** (a dependency slower than the caller's timeout, a bad request from the caller, or the failing system itself), and says whether the origin is **upstream or downstream** of where it was reported (you, the ServiceNow incident that mentions the id, or the entry point). ServiceNow adds owner, open incidents and changes per hop, and suspect changes on the failing path. **Add trace to INC…** (approval), **Investigate with RCA →**.
- **Call-flow diagram** in Track ID: callers above, called systems below, failing calls in red, each system tagged UPSTREAM / REPORTED / DOWNSTREAM; click a system to see its spans and its own Splunk log around that moment.
- **Service map**: the static CMDB dependency map of a business service (not one transaction) as the same diagram, coloured by incidents and changes; click a system for its recent log. **Impacted right now** from open P1/P2 incidents.
- Details: [docs/TRANSACTION_TRACKING.md](docs/TRANSACTION_TRACKING.md).

Deep links: `/aiops#slo`, `/aiops#change/CHG0098905`, `/aiops#anomaly`, `/aiops#track/TXN-ENR-20261024-0207`, `/aiops#txn/Member%20Enrollment`.

### AIOps for SRE: Reliability and Change guard
- **Reliability** (top row → Reliability): every SLO's error budget, burn rate (5m–72h), multi-window burn-rate alerts (page / ticket), release policy (normal / caution / restrict / freeze), which incidents spent the budget, changes after which burn jumped, 28-day budget chart; **Scorecard** per service (budget, MTTR, P1/P2, change failure rate, deploys/week).
- **Change guard** (top row → Change guard): upcoming ServiceNow changes scored before they ship on blast radius, error budget, change failure rate, open P1/P2 and anomalies, collisions, freeze windows and peak hours, hygiene → **GO / CANARY / HOLD** with guardrails (canary steps, watch list, rollback triggers). Reliability fixes ship during freezes as canaries. **Verify after deploy**, **DORA**, **Post assessment to CHG…** (approval).
- Positioning, formulas, SLO and change-policy configuration: [docs/SRE.md](docs/SRE.md).

### Root cause analysis: how it decides
Every likely cause gets points for evidence it can show, and the reasons are listed in the UI:
- **Change**: finished shortly before the incident opened, on the same CI or a connected CI, wording matches the incident or work notes, named in the work notes.
- **Connected CI failing**: incidents on CIs this one depends on (CMDB relationships), opened around the same time.
- **Known problem**: open problem or known error on the same CI or with matching wording; shows its workaround.
- **Recurring issue**: similar resolved incidents and the fix that worked most often.

Confidence is capped at 95%. Below 30% the agent says the evidence is insufficient and lists what is missing (no CI, no changes, no notes, no CMDB relationships). **Write RCA with agent** turns the evidence into a written RCA (summary, root cause, contributing factors, timeline, 5 whys, corrective actions) that cites evidence ids such as `[CHG0012345]` and `[NOTE-2]`. Field history (`sys_audit`) needs the admin role; without it, RCA still runs.

**Demo story in your own instance:** `seed-rca-story.bat` creates real records tagged `[demo-rca]`: a TLS change on EXCH-SD-05, the P2 it caused (misrouted to Service Desk), two work notes, two duplicates, three past incidents fixed by Don Goodliffe, and a known-error problem. Open the new P2 and click **Run RCA**.

### 10-minute demo script
1. Open the UI. The **Shift** tab shows the briefing: shift, headline, top 10 by priority, SLA at risk, new this shift.
2. Click **Brief me in chat**: the agent explains what to pick up first.
3. Click the top P1/P2. Show *Who solved this before* (names, how many, median time, last fix).
4. Show *Possible duplicates* → **Link under…** → approve.
5. Show *Routing* on a misrouted incident → **Apply…** → approve; the incident moves to the suggested group and person.
6. Chat: "Add a work note: rollback started" → approve. Then "Resolve it, solution provided, …" → approve.
7. **Investigate with RCA agent →** on the P1/P2 (switches to the RCA agent): **Investigate** (ranked causes, timeline, metrics, coverage), add an observation ("error rate jumped at 02:08"), **Draft report**, then **Publish to ServiceNow…** → approve.
8. Chat: "Are there recurring incidents that need a problem record?" → create the problem → approve.
9. **Handover** button: what the next shift inherits.

## ALM: incident or anomaly → story / defect

Every screen has **+ Create story / defect…**: incident, each RCA corrective action, anomaly card, failed transaction,
SLO card, change assessment. AIOps drafts the item from the evidence (title, user story, acceptance criteria, links), you
edit it, approve, and it is created in **SynthoGEN MCP** (or Jira / Rally / Azure DevOps directly). The link is written back
to the ServiceNow incident or change. **Backlog** in the header lists what was created. Setup: [docs/ALM.md](docs/ALM.md).

## PDI notes

- A Personal Developer Instance hibernates after inactivity. If calls return an HTML page or time out, wake it at developer.servicenow.com.
- Resetting or upgrading the PDI changes the admin password; update `.env`.
- PDIs ship with demo incidents (run step 1 to list real numbers). `find_recent_changes` returns results only when incidents have a CI (`cmdb_ci`) and there are change requests on that CI.
- Use an `itil` user instead of `admin` for anything beyond your own PDI.

## How it works

```
src/incident_agent/
  config.py           settings from .env
  servicenow.py       Table API client (incident, sys_journal_field, change_request, sys_user_group)
  fake_servicenow.py  in-memory client for SERVICENOW_MODE=mock
  tools.py            the 11 LangChain tools
  approval.py         gate every write passes through: y/n in the terminal, Approve/Reject cards in the UI
  llm.py              init_chat_model("provider:model")
  agent.py            create_agent(...) + in-memory checkpointer for multi-turn memory
  cli.py              terminal chat
  api.py              FastAPI: serves the web UI and /api/* (writes come back as approval cards)
  web/index.html      the test console (single file, no build step)
scripts/check_servicenow.py
tests/                client tests (mocked HTTP) and tool tests (mock ServiceNow)
```

- Reads send `sysparm_display_value=true`, so the model sees "In Progress" and "Network" rather than sys_ids.
- Writes send `sysparm_input_display_value=true`, so the agent can pass names for reference fields (group, caller, CI).
- Tool errors (401, not found, hibernating instance) come back as text the agent can explain, so a bad call doesn't end the conversation.
- Turn on LangSmith in `.env` to see every tool call and prompt.

## Next steps

- Wire it into the AIOps React demo: call `POST /api/chat` and `POST /api/approve` from the incident panel.
- Add tools for `problem` and `change_request` creation (same client pattern).
- For a pilot, swap basic auth for OAuth (`/oauth_token.do`) and persist conversations with `langgraph-checkpoint-postgres`.
