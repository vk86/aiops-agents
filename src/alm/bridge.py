"""Incident / anomaly / RCA / trace / SLO / change → ALM work item (user story, feature, defect, spike).

1. context(kind, ref): pull what the screen is about (ServiceNow incident + RCA verdict, anomaly signals, transaction
   trace, SLO budget, change assessment, an RCA corrective action) into one evidence block with links.
2. draft(): an LLM writes the work item from that evidence (title, As a / I want / so that, description, Given/When/
   Then acceptance criteria, priority, labels); without an LLM a per-kind template writes it.
3. create(): sends it to the ALM target, after approval:
     synthogen  SynthoGEN MCP server tool (Streamable HTTP, JSON-RPC tools/call) - preferred, one door to Jira/Rally/RTC
     jira       Jira REST  /rest/api/2/issue
     rally      Rally WSAPI /slm/webservice/v2.0/{hierarchicalrequirement|portfoliofeature|defect}/create
     ado        Azure DevOps /_apis/wit/workitems/${type}
     mock       in-memory (SERVICENOW_MODE=mock)
   then (optionally) writes a work note with the ALM link back to the ServiceNow incident or change.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from itertools import count

from anomaly_agent.sources.base import SourceError, http, load_config, mock_mode
from incident_agent.servicenow import ServiceNowError

UTC = timezone.utc
TYPES = ["User Story", "Feature", "Defect", "Spike"]
KINDS = ["incident", "rca_action", "anomaly", "transaction", "slo", "change"]
_items: list[dict] = []
_lock = threading.Lock()
_seq = count(1042)


def _sn():
    from incident_agent import tools
    return tools.sn()


def _p(v) -> int:
    try:
        return int(str(v or "5").strip()[:1])
    except ValueError:
        return 5


# ---------------------------------------------------------------- context per screen
def context(kind: str, ref: str, index: int = 0) -> dict:
    """Evidence block for one thing on screen. Returns {kind, ref, title, facts[], links[], writeback, defaults}."""
    sn = _sn()
    ref = (ref or "").strip()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if kind == "incident":
        i = sn.incident_row(ref)
        facts = [f"Incident {i['number']} ({i.get('priority')}, {i.get('state')}): {i.get('short_description')}",
                 f"CI {i.get('cmdb_ci') or 'none'} · group {i.get('assignment_group') or 'none'}"]
        if i.get("description"):
            facts.append("Description: " + i["description"][:400])
        try:
            from rca_agent.agent import engine
            r = engine().latest(i["number"]) or engine().investigate(i["number"])
            facts.append("RCA verdict: " + r["verdict"])
            if r["hypotheses"]:
                h = r["hypotheses"][0]
                facts.append(f"Top cause ({round(h['confidence'] * 100)}%): " + "; ".join(h["reasons"][:3]))
        except Exception:
            pass
        if i.get("close_notes"):
            facts.append("Resolution: " + i["close_notes"][:300])
        p = _p(i.get("priority"))
        return {"kind": kind, "ref": i["number"], "title": i.get("short_description"), "facts": facts, "service": i.get("cmdb_ci"),
                "links": [i["number"]], "writeback": {"table": "incident", "number": i["number"]},
                "defaults": {"type": "Defect", "priority": "Highest" if p == 1 else "High" if p == 2 else "Medium"}}
    if kind == "rca_action":
        from rca_agent.agent import engine
        eng = engine()
        r = eng.latest(ref) or eng.draft(ref, use_llm=False)
        if not r.get("narrative"):
            r = eng.draft(ref, use_llm=False)
        acts = (r["narrative"] or {}).get("actions") or []
        if not acts:
            raise ValueError(f"The RCA for {ref} has no corrective actions yet; draft the report first.")
        a = acts[min(max(index, 0), len(acts) - 1)]
        rc = (r["narrative"] or {}).get("root_cause", {})
        facts = [f"Corrective action from the RCA of {r['header']['number']}: {a.get('action')}",
                 f"Action type {a.get('type')} · owner {a.get('owner_team') or 'tbd'} · priority {a.get('priority')}",
                 f"Root cause ({rc.get('category')}): {rc.get('statement') or r['verdict']}",
                 f"Incident: {r['header']['short_description']} on {r['header'].get('cmdb_ci')}"]
        return {"kind": kind, "ref": r["header"]["number"], "index": index, "title": a.get("action"), "facts": facts,
                "service": r["header"].get("cmdb_ci"), "links": [r["header"]["number"]] + [e for e in r["hypotheses"][0]["evidence"][:2] if e] if r["hypotheses"] else [r["header"]["number"]],
                "writeback": {"table": "incident", "number": r["header"]["number"]}, "actions": [x.get("action") for x in acts],
                "defaults": {"type": "User Story", "priority": "High" if a.get("priority") == "high" else "Medium", "team": a.get("owner_team")}}
    if kind == "anomaly":
        from anomaly_agent import signals
        r = signals.service(sn, ref)
        m = r.get("model") or {}
        facts = [f"Anomaly on {r['service']} ({r['decision']}, confidence {round(r['confidence'] * 100)}%): {r['headline']}",
                 *[f"{s['source']}: {s['name']}" + (f" {s['value']:g}{s['unit']} vs {s['baseline']:g}{s['unit']}" if s["value"] is not None and s["baseline"] is not None else "")
                   + (f" ({s['detail'][:100]})" if s["detail"] else "") for s in r["signals"] if s["anomalous"]][:6]]
        if m:
            facts.append(f"Model score {m.get('score')}: {m.get('explanation')}")
        if r["context"]["changes"]:
            facts.append("Recent changes: " + ", ".join(f"{c['number']} {c['short_description']}" for c in r["context"]["changes"][:3]))
        inc = r["context"]["open_incidents"][0]["number"] if r["context"]["open_incidents"] else None
        return {"kind": kind, "ref": r["service"], "title": f"{r['service']}: {r['headline']}", "facts": facts, "service": r["service"],
                "links": [x for x in [inc] + [c["number"] for c in r["context"]["changes"][:2]] if x],
                "writeback": {"table": "incident", "number": inc} if inc else None,
                "defaults": {"type": "Spike" if r["decision"] in ("watch", "suppressed") else "Defect", "priority": "High" if r["decision"] in ("raise", "attach") else "Medium",
                             "team": r["context"]["owner"]}}
    if kind == "transaction":
        from txn_agent import trace
        r = trace.track(sn, ref)
        if not r.get("found"):
            raise ValueError(r["verdict"])
        o = r.get("origin") or {}
        facts = [f"Transaction {r['txn_id']}: {r['verdict']}"]
        if r.get("propagation"):
            facts.append("Propagation: " + " → ".join(f"{c['service']} ({c['status']}{' ' + str(c['http_status']) if c.get('http_status') else ''})" for c in r["propagation"]))
        if r.get("suspect_changes"):
            facts.append("Suspect changes: " + ", ".join(f"{c['number']} {c['short_description']}" for c in r["suspect_changes"]))
        inc = r["servicenow"]["incidents"][0]["number"] if r["servicenow"]["incidents"] else None
        owner = (r["services"].get(o.get("service")) or {}).get("owner")
        return {"kind": kind, "ref": r["txn_id"], "title": f"{o.get('service')}: {o.get('type', '').replace('_', ' ')}" if o else r["verdict"][:80],
                "facts": facts, "service": o.get("service"), "links": [x for x in [inc] + [c["number"] for c in r.get("suspect_changes", [])] if x],
                "writeback": {"table": "incident", "number": inc} if inc else None,
                "defaults": {"type": "Defect" if r.get("failed") else "Spike", "priority": "High" if r.get("failed") else "Low", "team": owner}}
    if kind == "slo":
        from slo_agent import slo
        rows = [s for s in slo.for_service(sn, ref) if s.get("available")]
        if not rows:
            raise ValueError(f"No SLO data for {ref}.")
        s = min(rows, key=lambda x: x["remaining_pct"])
        facts = [f"SLO {s['name']} on {s['service']}: objective {s['objective']}%, attainment {s['attainment']}%, budget left {s['remaining_pct']}% ({s['policy']['level']})",
                 f"Burn rate 1h {s['burn'].get('1h')}×, 24h {s['burn'].get('24h')}×; alert state {s['state']}"]
        if s.get("incidents"):
            facts.append("Budget spent by: " + ", ".join(f"{i['number']} {i['budget_cost_pct']}%" for i in s["incidents"][:3]))
        if any(c.get("suspect") for c in s.get("changes", [])):
            facts.append("Burn jumped after: " + ", ".join(c["number"] for c in s["changes"] if c.get("suspect")))
        return {"kind": kind, "ref": s["service"], "title": f"Restore {s['name']} to {s['objective']}%", "facts": facts, "service": s["service"],
                "links": [i["number"] for i in s.get("incidents", [])[:2]], "writeback": {"table": "incident", "number": s["incidents"][0]["number"]} if s.get("incidents") else None,
                "defaults": {"type": "Feature", "priority": "Highest" if s["policy"]["level"] == "freeze" else "High", "team": s.get("owner")}}
    from change_agent import risk  # change
    a = risk.assess(sn, ref)
    missing = [f["detail"] for f in a["factors"] if f["points"] and f["key"] in ("hygiene", "history", "budget", "health")]
    facts = [f"Change {a['number']} ({a['ci']}): {a['short_description']}", f"Change guard: {a['recommendation']} (risk {a['score']}/100). {a['why']}", *missing[:4]]
    return {"kind": kind, "ref": a["number"], "title": a["short_description"], "facts": facts, "service": a["ci"], "links": [a["number"]],
            "writeback": {"table": "change_request", "number": a["number"]},
            "defaults": {"type": "User Story", "priority": "High" if a["recommendation"] == "HOLD" else "Medium", "team": a.get("assignment_group")}}


# ---------------------------------------------------------------- drafting
DRAFT_PROMPT = """You turn operational evidence into one ALM work item for an enterprise healthcare engineering team.
Write it so an engineering team can pick it up: specific, testable, no invented facts. Use only the evidence below.

Kind: {kind}   Suggested type: {type}   Suggested priority: {priority}   Owning team: {team}
Evidence:
{facts}

Return JSON only:
{{"type": "User Story|Feature|Defect|Spike", "title": "<= 90 chars, imperative",
  "user_story": "As a <role>, I want <capability>, so that <outcome>",
  "description": "2-5 sentences: problem, impact, evidence ids, what done looks like",
  "acceptance_criteria": ["Given ... When ... Then ...", "..."],
  "priority": "Highest|High|Medium|Low", "labels": ["aiops", "..."], "story_points": <1|2|3|5|8|13 or null>}}"""


def _template(ctx: dict) -> dict:
    k, svc, d = ctx["kind"], ctx.get("service") or "the service", ctx["defaults"]
    ev = ", ".join(ctx["links"]) or ctx["ref"]
    base = {"type": d["type"], "priority": d["priority"], "labels": ["aiops", f"svc-{svc}", f"from-{k}"], "story_points": None}
    if k == "incident":
        return {**base, "title": f"Fix: {ctx['title']}"[:90], "user_story": f"As a member using services that depend on {svc}, I want this failure fixed at its cause, so that {ctx['title'].lower()} does not recur.",
                "description": " ".join(ctx["facts"][:4]) + f" Evidence: {ev}.",
                "acceptance_criteria": [f"Given {svc} under normal production load, When the failing operation runs, Then it completes without the error in {ctx['ref']}",
                                        f"Given the fix is deployed, When the synthetic check for {svc} runs for 24 hours, Then it passes every run",
                                        f"Given the failure recurs, When it is detected, Then an alert pages the owning team within 5 minutes"]}
    if k == "rca_action":
        team = d.get("team") or "owning"
        act = ctx["title"].strip().rstrip(".")
        return {**base, "title": act[:90], "user_story": f"As the {team} team, I want to {act[0].lower() + act[1:]}, so that the root cause of {ctx['ref']} cannot cause another outage.",
                "description": " ".join(ctx["facts"]) + f" Evidence: {ev}.",
                "acceptance_criteria": [f"Given the change is in production, When the conditions of {ctx['ref']} are replayed in a test environment, Then the service stays within its SLO",
                                        f"Given 14 days after release, When the error budget of {svc} is reviewed, Then the burn rate stays below 1x",
                                        f"Given the action is complete, When the RCA for {ctx['ref']} is reviewed, Then this item is linked as done"]}
    if k == "anomaly":
        return {**base, "title": f"Eliminate the cause of the {svc} anomaly"[:90], "user_story": f"As the owner of {svc}, I want the anomaly cause found and removed, so that it stops degrading the service before users notice.",
                "description": " ".join(ctx["facts"][:5]) + f" Evidence: {ev}.",
                "acceptance_criteria": [f"Given normal traffic, When {svc} is monitored for 7 days, Then the anomaly model scores stay below the threshold",
                                        "Given the root cause is identified, When it is documented, Then the runbook and alert thresholds are updated",
                                        f"Given a recurrence, When signals cross the threshold, Then an event is raised to ServiceNow for {svc}"]}
    if k == "transaction":
        return {**base, "title": f"{ctx['title']}: make the call chain resilient"[:90], "user_story": f"As a member completing this transaction, I want it to succeed even when {svc} is slow or failing, so that a single dependency does not break the journey.",
                "description": " ".join(ctx["facts"]) + f" Evidence: {ev}.",
                "acceptance_criteria": [f"Given {svc} responds slower than its caller's timeout, When the transaction runs, Then the caller degrades gracefully instead of returning 5xx",
                                        "Given caller timeouts and retries, When reviewed, Then they are aligned with the dependency's p99 latency",
                                        f"Given transaction {ctx['ref']} is replayed after the fix, When it runs, Then it completes successfully"]}
    if k == "slo":
        return {**base, "title": ctx["title"][:90], "user_story": f"As an SRE for {svc}, I want the top error-budget burners removed, so that the SLO is met and feature releases can resume.",
                "description": " ".join(ctx["facts"]) + f" Evidence: {ev}.",
                "acceptance_criteria": [f"Given 14 days after delivery, When the SLO for {svc} is evaluated, Then the 24h burn rate stays below 1x",
                                        "Given the incidents that consumed the budget, When each is reviewed, Then every one has a linked fix",
                                        "Given the error budget recovers above 50%, When the policy is evaluated, Then the release freeze is lifted"]}
    return {**base, "title": f"Make {ctx['ref']} safe to ship"[:90], "user_story": f"As the change owner for {svc}, I want the change guard's risks addressed, so that {ctx['ref']} can ship without a HOLD.",
            "description": " ".join(ctx["facts"]) + f" Evidence: {ev}.",
            "acceptance_criteria": ["Given the change record, When it is reviewed, Then it has a tested backout plan and a test plan",
                                    f"Given the change guard re-assesses {ctx['ref']}, When it runs, Then the recommendation is GO or CANARY",
                                    "Given the change ships, When verify_change runs, Then no SLO regression or P1/P2 follows within 24 hours"]}


def draft(kind: str, ref: str, index: int = 0, use_llm: bool = True) -> dict:
    ctx = context(kind, ref, index)
    item, source = _template(ctx), "template"
    if use_llm:
        try:
            from incident_agent.llm import get_model, text_of
            d = ctx["defaults"]
            txt = text_of(get_model().invoke(DRAFT_PROMPT.format(kind=kind, type=d["type"], priority=d["priority"], team=d.get("team") or "unknown",
                                                                 facts="\n".join(f"- {f}" for f in ctx["facts"]))))
            m = re.search(r"\{.*\}", txt or "", re.S)
            got = json.loads(m.group(0)) if m else None
            if isinstance(got, dict) and got.get("title"):
                item = {**item, **{k: v for k, v in got.items() if v not in (None, "", [])}}
                source = "llm"
        except Exception:
            pass
    if item.get("type") not in TYPES:
        item["type"] = ctx["defaults"]["type"]
    item["labels"] = list(dict.fromkeys(["aiops"] + [str(x) for x in item.get("labels", [])]))[:8]
    return {"context": ctx, "item": item, "draft_source": source, "targets": targets()}


# ---------------------------------------------------------------- ALM targets
def targets() -> list[dict]:
    cfg = load_config().get("alm", {})
    out = []
    if mock_mode():
        out.append({"id": "mock", "label": "Mock ALM (demo)", "projects": ["ENRL", "PLAT", "SRE"], "state": "mock"})
    if os.getenv("AIOPS_SYNTHOGEN_MCP_URL"):
        out.append({"id": "synthogen", "label": "SynthoGEN MCP", "projects": cfg.get("synthogen_projects", []), "state": "connected",
                    "systems": cfg.get("synthogen_systems", ["jira", "rally", "rtc"])})
    if os.getenv("AIOPS_JIRA_URL") and (os.getenv("AIOPS_JIRA_TOKEN")):
        out.append({"id": "jira", "label": "Jira", "projects": cfg.get("jira_projects", [os.getenv("AIOPS_JIRA_PROJECT", "")]), "state": "connected"})
    if os.getenv("AIOPS_RALLY_APIKEY"):
        out.append({"id": "rally", "label": "Rally", "projects": cfg.get("rally_projects", [os.getenv("AIOPS_RALLY_PROJECT", "")]), "state": "connected"})
    if os.getenv("AIOPS_ADO_ORG_URL") and os.getenv("AIOPS_ADO_PAT"):
        out.append({"id": "ado", "label": "Azure DevOps", "projects": cfg.get("ado_projects", [os.getenv("AIOPS_ADO_PROJECT", "")]), "state": "connected"})
    default = os.getenv("AIOPS_ALM_TARGET")
    out.sort(key=lambda t: t["id"] != default)
    return out


def coverage() -> list[dict]:
    have = {t["id"] for t in targets()}
    rows = [("synthogen", "SynthoGEN MCP", "AIOPS_SYNTHOGEN_MCP_URL (+ AIOPS_SYNTHOGEN_TOKEN, AIOPS_SYNTHOGEN_CREATE_TOOL)"),
            ("jira", "Jira", "AIOPS_JIRA_URL, AIOPS_JIRA_TOKEN (+ AIOPS_JIRA_EMAIL for Cloud), AIOPS_JIRA_PROJECT"),
            ("rally", "Rally", "AIOPS_RALLY_APIKEY, AIOPS_RALLY_PROJECT (project ref)"),
            ("ado", "Azure DevOps", "AIOPS_ADO_ORG_URL, AIOPS_ADO_PROJECT, AIOPS_ADO_PAT")]
    out = [{"label": l, "state": "connected" if i in have else "not_connected", "detail": "ready" if i in have else "Set " + env} for i, l, env in rows]
    if mock_mode():
        out.insert(0, {"label": "Mock ALM", "state": "mock", "detail": "items stored in memory (SERVICENOW_MODE=mock)"})
    return out


def _mcp_call(tool: str, args: dict) -> dict:
    url = os.environ["AIOPS_SYNTHOGEN_MCP_URL"]
    hdr = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if os.getenv("AIOPS_SYNTHOGEN_TOKEN"):
        hdr["Authorization"] = f"Bearer {os.environ['AIOPS_SYNTHOGEN_TOKEN']}"
    r = http("POST", url, headers=hdr, json_body={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "aiops-agents", "version": "1.0"}}})
    sid = r.headers.get("Mcp-Session-Id") or r.headers.get("mcp-session-id")
    if sid:
        hdr["Mcp-Session-Id"] = sid
    http("POST", url, headers=hdr, json_body={"jsonrpc": "2.0", "method": "notifications/initialized"})
    r = http("POST", url, headers=hdr, json_body={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}})
    msg = None
    if "text/event-stream" in (r.headers.get("Content-Type") or ""):
        for line in r.text.splitlines():
            if line.startswith("data:"):
                try:
                    d = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if d.get("id") == 2:
                    msg = d
    else:
        msg = r.json()
    if not msg:
        raise SourceError("SynthoGEN MCP returned no result.")
    if msg.get("error"):
        raise SourceError(f"SynthoGEN MCP error: {msg['error'].get('message')}")
    res = msg.get("result") or {}
    if res.get("isError"):
        raise SourceError("SynthoGEN MCP tool error: " + " ".join(c.get("text", "") for c in res.get("content", [])))
    if isinstance(res.get("structuredContent"), dict):
        return res["structuredContent"]
    text = " ".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
    try:
        return json.loads(text)
    except ValueError:
        return {"message": text}


def _description(item: dict, ctx: dict) -> str:
    ac = "\n".join(f"- {a}" for a in item.get("acceptance_criteria", []))
    return (f"{item.get('user_story', '')}\n\n{item.get('description', '')}\n\nAcceptance criteria:\n{ac}\n\n"
            f"Source: AIOps {ctx['kind'].replace('_', ' ')} {ctx['ref']} · links: {', '.join(ctx['links']) or '-'}")


def _send(target: str, project: str, item: dict, ctx: dict) -> dict:
    t = item["type"]
    if target == "mock":
        key = f"{project or 'ENRL'}-{next(_seq)}"
        return {"key": key, "url": f"https://alm.example.com/browse/{key}", "system": "Mock ALM"}
    if target == "synthogen":
        system = (item.get("system") or os.getenv("AIOPS_SYNTHOGEN_SYSTEM", "jira")).lower()
        out = _mcp_call(os.getenv("AIOPS_SYNTHOGEN_CREATE_TOOL", "create_work_item"), {
            "system": system, "project": project, "type": t, "title": item["title"], "user_story": item.get("user_story", ""),
            "description": item.get("description", ""), "acceptance_criteria": item.get("acceptance_criteria", []),
            "priority": item.get("priority"), "labels": item.get("labels", []), "story_points": item.get("story_points"),
            "links": ctx["links"], "source": {"tool": "aiops", "kind": ctx["kind"], "ref": ctx["ref"]}})
        return {"key": out.get("key") or out.get("id") or out.get("formattedId") or "?", "url": out.get("url") or out.get("link") or "",
                "system": f"SynthoGEN → {system}"}
    if target == "jira":
        base = os.environ["AIOPS_JIRA_URL"].rstrip("/")
        hdr = {"Content-Type": "application/json"}
        auth = None
        if os.getenv("AIOPS_JIRA_EMAIL"):  # Jira Cloud: email + API token (basic)
            import base64
            hdr["Authorization"] = "Basic " + base64.b64encode(f"{os.environ['AIOPS_JIRA_EMAIL']}:{os.environ['AIOPS_JIRA_TOKEN']}".encode()).decode()
        else:  # Data Center: personal access token
            hdr["Authorization"] = f"Bearer {os.environ['AIOPS_JIRA_TOKEN']}"
        jt = {"User Story": "Story", "Feature": os.getenv("AIOPS_JIRA_FEATURE_TYPE", "Epic"), "Defect": "Bug", "Spike": os.getenv("AIOPS_JIRA_SPIKE_TYPE", "Task")}[t]
        fields = {"project": {"key": project}, "summary": item["title"], "description": _description(item, ctx), "issuetype": {"name": jt},
                  "labels": [re.sub(r"\s+", "-", x) for x in item.get("labels", [])]}
        if item.get("priority"):
            fields["priority"] = {"name": item["priority"]}
        d = http("POST", f"{base}/rest/api/2/issue", headers=hdr, json_body={"fields": fields}).json()
        return {"key": d.get("key"), "url": f"{base}/browse/{d.get('key')}", "system": "Jira"}
    if target == "rally":
        base = os.getenv("AIOPS_RALLY_URL", "https://rally1.rallydev.com").rstrip("/")
        typ = {"User Story": "hierarchicalrequirement", "Feature": "portfolioitem/feature", "Defect": "defect", "Spike": "hierarchicalrequirement"}[t]
        body_key = {"hierarchicalrequirement": "HierarchicalRequirement", "portfolioitem/feature": "PortfolioItem/Feature", "defect": "Defect"}[typ]
        obj = {"Name": item["title"], "Description": _description(item, ctx).replace("\n", "<br>"), "Project": project or os.getenv("AIOPS_RALLY_PROJECT")}
        if typ == "hierarchicalrequirement" and item.get("story_points"):
            obj["PlanEstimate"] = item["story_points"]
        d = http("POST", f"{base}/slm/webservice/v2.0/{typ}/create", headers={"ZSESSIONID": os.environ["AIOPS_RALLY_APIKEY"], "Content-Type": "application/json"},
                 json_body={body_key: obj}).json()
        res = d.get("CreateResult", {})
        if res.get("Errors"):
            raise SourceError("Rally: " + "; ".join(res["Errors"]))
        o = res.get("Object", {})
        return {"key": o.get("FormattedID"), "url": o.get("_ref", ""), "system": "Rally"}
    if target == "ado":
        base = os.environ["AIOPS_ADO_ORG_URL"].rstrip("/")
        proj = project or os.environ.get("AIOPS_ADO_PROJECT", "")
        wt = {"User Story": "User Story", "Feature": "Feature", "Defect": "Bug", "Spike": "Task"}[t]
        import base64
        auth = "Basic " + base64.b64encode(f":{os.environ['AIOPS_ADO_PAT']}".encode()).decode()
        ops = [{"op": "add", "path": "/fields/System.Title", "value": item["title"]},
               {"op": "add", "path": "/fields/System.Description", "value": (item.get("user_story", "") + "<br><br>" + item.get("description", "")).replace("\n", "<br>")},
               {"op": "add", "path": "/fields/Microsoft.VSTS.Common.AcceptanceCriteria", "value": "<br>".join(item.get("acceptance_criteria", []))},
               {"op": "add", "path": "/fields/System.Tags", "value": "; ".join(item.get("labels", []))}]
        from urllib.parse import quote
        d = http("POST", f"{base}/{quote(proj)}/_apis/wit/workitems/${quote(wt)}?api-version=7.1",
                 headers={"Authorization": auth, "Content-Type": "application/json-patch+json"}, json_body=ops).json()
        return {"key": str(d.get("id")), "url": (d.get("_links", {}).get("html", {}) or {}).get("href", ""), "system": "Azure DevOps"}
    raise ValueError(f"Unknown ALM target {target}.")


def create(ctx: dict, item: dict, target: str, project: str = "", writeback: bool = True) -> dict:
    """Create the work item in the ALM target, then write the link back to ServiceNow. Call through the approval gate."""
    if target not in {t["id"] for t in targets()}:
        raise ValueError(f"ALM target '{target}' is not configured.")
    if not (item.get("title") or "").strip():
        raise ValueError("The work item needs a title.")
    if item.get("type") not in TYPES:
        raise ValueError(f"type must be one of {', '.join(TYPES)}")
    res = _send(target, project, item, ctx)
    rec = {**res, "type": item["type"], "title": item["title"], "project": project, "kind": ctx["kind"], "ref": ctx["ref"],
           "links": ctx["links"], "created_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"), "writeback": None}
    wb = ctx.get("writeback")
    if writeback and wb and wb.get("number"):
        note = f"[AIOps] {item['type']} {res['key']} created in {res['system']}: {item['title']}" + (f" ({res['url']})" if res.get("url") else "")
        try:
            sn = _sn()
            (sn.add_change_note if wb["table"] == "change_request" else sn.add_note)(wb["number"], note)
            rec["writeback"] = wb["number"]
        except ServiceNowError as e:
            rec["writeback_error"] = str(e)
    with _lock:
        _items.insert(0, rec)
    return rec


def items() -> list[dict]:
    with _lock:
        return list(_items)
