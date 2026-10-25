"""AIOps transaction agent: tracks business transactions end to end through the CMDB and finds the broken hop."""
from __future__ import annotations

import json
from functools import wraps

from langchain_core.tools import tool

from incident_agent.servicenow import ServiceNowError

from . import path

SYSTEM_PROMPT = """You are the AIOps transaction agent for an enterprise healthcare engineering org.

Two ways to follow a transaction:
- By transaction or correlation id -> track_transaction. It pulls the id's spans and logs from every connected source
  (Splunk, AppDynamics, Dynatrace, Jaeger, Tempo), rebuilds the call chain, finds the failure point and the origin,
  and says whether the origin is upstream or downstream of the system where it was reported (given by the user, or
  the CI of the ServiceNow incident that mentions the id, or the entry point). It adds ServiceNow owner, open
  incidents and changes per hop, and suspect changes on the failing chain.
- By business service -> trace_transaction (CMDB path with hop health), impacted_transactions (which services are hit
  by open P1/P2 incidents now), list_transactions.

How to report a tracked id: verdict first (failed or not), origin and why (timeout vs slow dependency, bad request
from the caller, error), upstream/downstream relative to the reported system, the propagation path, suspect changes,
the owner group of the origin and its open incidents. Say when links were inferred (no trace source) and which sources
were mock or not connected. attach_trace_to_incident adds the trace summary to an incident (needs approval).
Never call the same tool with the same arguments twice in a turn. Never invent hops, timings or incidents. Keep replies short.

Engineering follow-up: draft_backlog_item / create_backlog_item turn what you found into a user story, feature, defect or spike in the ALM (SynthoGEN MCP, Jira, Rally, Azure DevOps); creating needs approval and writes the link back to ServiceNow.

Today is {today}."""


def _sn():
    from incident_agent import tools
    return tools.sn()


def _safe(fn):
    @wraps(fn)
    def w(*a, **k):
        try:
            return fn(*a, **k)
        except ServiceNowError as e:
            return f"ServiceNow error: {e}"
        except Exception as e:
            return f"Transaction tool error in {fn.__name__}: {type(e).__name__}: {e}"
    return w


@tool
@_safe
def list_transactions() -> str:
    """List the business transactions (business services in the CMDB) that can be traced."""
    return json.dumps(path.transactions(_sn()), default=str)


@tool
@_safe
def impacted_transactions() -> str:
    """Which business transactions are affected right now, by walking up the CMDB from open P1/P2 incidents."""
    return json.dumps(path.impacted(_sn()), default=str)


@tool
@_safe
def trace_transaction(name: str) -> str:
    """Trace a business transaction (business service or CI name) through everything it depends on: health of each
    hop (open incidents, recent changes), the likely origin of a failure and the impact path."""
    r = path.trace_path(_sn(), name)
    r["hops"] = [{k: h[k] for k in ("name", "depth", "parent", "status", "why", "origin")}
                 | {"incidents": [i["number"] for i in h["incidents"]], "changes": [c["number"] for c in h["changes"]]}
                 for h in r["hops"]]
    return json.dumps(r, default=str)


@tool
@_safe
def find_transaction_id(txn_id: str) -> str:
    """Only search ServiceNow incidents and work notes for a transaction id (track_transaction does this too)."""
    return json.dumps(path.find_transaction(_sn(), txn_id), default=str)


def _trace_summary(r: dict) -> dict:
    return {k: r.get(k) for k in ("txn_id", "found", "verdict", "failure_point", "origin", "direction", "reported_at", "reported_at_source",
                                  "entry", "inferred_links", "trace_ids", "suspect_changes", "errors")} | {
        "propagation": [f"{c['service']} ({c['status']}{' ' + str(c['http_status']) if c.get('http_status') else ''})" for c in r.get("propagation", [])],
        "hops": [f"{h['service']} {h['operation']} {h['status']} {h.get('duration_ms') or ''}ms{' [' + h['role'] + ']' if h.get('role') else ''}" for h in r.get("hops", [])],
        "servicenow": {"incidents": [i["number"] for i in r["servicenow"]["incidents"]], "notes": len(r["servicenow"]["notes"])},
        "services": {k: {x: v.get(x) for x in ("owner", "status", "in_cmdb")} | {"incidents": [i["number"] for i in v.get("incidents", [])]}
                     for k, v in r.get("services", {}).items()},
        "sources": [f"{s['label']}: {s['state']} ({s['spans']} spans)" for s in r["sources"]]}


@tool
@_safe
def track_transaction(txn_id: str, reported_at: str = "") -> str:
    """Track a transaction/correlation id across Splunk, AppDynamics, Dynatrace, Jaeger and Tempo: call chain, failure
    point, origin (slow dependency, bad request from caller, error), upstream/downstream of the reported system
    (reported_at: service/CI name, optional), ServiceNow context and suspect changes."""
    from . import trace
    return json.dumps(_trace_summary(trace.track(_sn(), txn_id, reported_at)), default=str)


@tool
@_safe
def attach_trace_to_incident(txn_id: str, number: str, reported_at: str = "") -> str:
    """Add the transaction trace summary (verdict, origin, direction, propagation, suspect changes) as a work note on
    an incident. Needs the user's approval."""
    from incident_agent.approval import gated
    from . import trace
    r = trace.track(_sn(), txn_id, reported_at)
    note = trace.note(r)
    num = number.strip().upper()
    return gated(f"Work note on {num} (trace {txn_id})", {"incident": num, "note": note},
                 lambda: (_sn().add_note(num, note), json.dumps({"added": "work note", "incident": num}))[1])


TXN_TOOLS = [track_transaction, attach_trace_to_incident, list_transactions, impacted_transactions, trace_transaction, find_transaction_id]


def build():
    from incident_agent.agent import build_agent
    from alm.tools import ALM_TOOLS
    return build_agent(tools=TXN_TOOLS + ALM_TOOLS, system_prompt=SYSTEM_PROMPT)
