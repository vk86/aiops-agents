# ALM bridge: from an incident or anomaly to a backlog item

Any finding in AIOps can become a **User Story, Feature, Defect or Spike** in your ALM, without leaving the screen.

| Where | Button | Default type | Written back to |
|---|---|---|---|
| Incident detail | + Create story / defect… | Defect (P1/P2), User Story otherwise | the incident (work note) |
| RCA report → Corrective actions | + Create story… (one per action) | User Story / Spike | the incident |
| Anomaly signal card | + Create story… | Defect / Spike | the linked incident, if any |
| Transaction (Track ID), failed | + Create defect… | Defect for the origin system | the incident that mentions the ID |
| Reliability (SLO card) | + Create feature… | Feature (reliability work) | – |
| Change guard → Assess | + Create story… | User Story (make the change safe) | the change request (work note) |

Flow: **draft** (LLM writes it from the evidence; template if no LLM) → **you edit** (type, title, story, description,
acceptance criteria, priority, points, labels, project, target) → **Create…** → approval card → created → link written back
to ServiceNow. Header **Backlog (n)** lists everything created from AIOps this session. Chat works too: "create a defect for
the payment-gw anomaly in Rally".

## Targets (first match is the default; force one with `AIOPS_ALM_TARGET`)

| Target | Settings in `.env` |
|---|---|
| **SynthoGEN MCP** (preferred) | `AIOPS_SYNTHOGEN_MCP_URL`, `AIOPS_SYNTHOGEN_TOKEN`, `AIOPS_SYNTHOGEN_CREATE_TOOL` (default `create_work_item`), `AIOPS_SYNTHOGEN_SYSTEM` (default `jira`) |
| Jira | `AIOPS_JIRA_URL`, `AIOPS_JIRA_TOKEN`, `AIOPS_JIRA_EMAIL` (Cloud only), `AIOPS_JIRA_PROJECT` |
| Rally | `AIOPS_RALLY_APIKEY`, `AIOPS_RALLY_PROJECT` (project ref), `AIOPS_RALLY_URL` |
| Azure DevOps | `AIOPS_ADO_ORG_URL`, `AIOPS_ADO_PROJECT`, `AIOPS_ADO_PAT` |
| Mock | automatic when `SERVICENOW_MODE=mock` |

Project pick-lists come from `aiops_config.json` → `alm`: `synthogen_projects`, `synthogen_systems` (e.g. `["jira","rally","rtc"]`,
shown as "SynthoGEN pushes to"), `jira_projects`, `rally_projects`, `ado_projects`.

## SynthoGEN MCP contract

AIOps speaks MCP Streamable HTTP: `initialize` → `notifications/initialized` → `tools/call` (keeps `Mcp-Session-Id`;
accepts JSON or SSE replies). Tool arguments:

```json
{ "system": "jira | rally | rtc | ...", "project": "ENRL", "type": "User Story | Feature | Defect | Spike",
  "title": "...", "user_story": "As a ..., I want ..., so that ...", "description": "...",
  "acceptance_criteria": ["Given ..., When ..., Then ..."], "priority": "High", "labels": ["aiops"],
  "story_points": 3, "links": ["INC0012345", "CHG0098871"],
  "source": { "tool": "aiops", "kind": "incident", "ref": "INC0012345" } }
```

Expected result (`structuredContent`, or JSON in a text content block): `{ "key": "ENRL-123", "url": "https://..." }`
(`id` / `formattedId` / `link` also accepted). `isError: true` is shown to the user as the error.
If SynthoGEN's tool has a different name or argument names, set `AIOPS_SYNTHOGEN_CREATE_TOOL` and adjust `_send()` in
`src/alm/bridge.py` (one dictionary).

## Safety
Nothing is created without **Approve and write**. Credentials stay in your local `.env`. Write-back is a work note only.
