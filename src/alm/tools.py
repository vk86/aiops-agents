"""Chat tools shared by every agent: draft and create ALM work items."""
from __future__ import annotations

import json

from langchain_core.tools import tool

from incident_agent.approval import gated

from . import bridge


@tool
def draft_backlog_item(kind: str, ref: str, index: int = 0) -> str:
    """Draft an ALM work item (user story, feature, defect or spike) from something on screen.
    kind: incident (ref INC number) | rca_action (ref INC number, index of the corrective action) | anomaly (ref service)
    | transaction (ref transaction id) | slo (ref service) | change (ref CHG number). Returns the draft and ALM targets."""
    try:
        d = bridge.draft(kind, ref, index)
        return json.dumps({"item": d["item"], "links": d["context"]["links"], "writeback": d["context"]["writeback"],
                           "targets": [t["id"] for t in d["targets"]], "draft_source": d["draft_source"]}, default=str)
    except Exception as e:
        return f"Could not draft: {type(e).__name__}: {e}"


@tool
def create_backlog_item(kind: str, ref: str, target: str = "", project: str = "", index: int = 0, item_type: str = "",
                        title: str = "", writeback: bool = True) -> str:
    """Create the drafted work item in the ALM (target: synthogen | jira | rally | ado | mock; empty = default) and
    write the link back to the ServiceNow incident/change. Optional overrides: item_type, title. Needs approval."""
    try:
        d = bridge.draft(kind, ref, index)
    except Exception as e:
        return f"Could not draft: {type(e).__name__}: {e}"
    item = dict(d["item"])
    if item_type:
        item["type"] = item_type
    if title:
        item["title"] = title
    tg = target or (d["targets"][0]["id"] if d["targets"] else "")
    if not tg:
        return "No ALM target is configured (SynthoGEN MCP, Jira, Rally or Azure DevOps). The draft is ready to copy: " + json.dumps(item)
    proj = project or next((p for t in d["targets"] if t["id"] == tg for p in t.get("projects", []) if p), "")
    ctx = d["context"]
    return gated(f"Create {item['type']} in {tg}: {item['title'][:70]}",
                 {"target": tg, "project": proj, "type": item["type"], "title": item["title"], "links": ", ".join(ctx["links"]),
                  "write back to": (ctx.get("writeback") or {}).get("number") if writeback else "no"},
                 lambda: json.dumps(bridge.create(ctx, item, tg, proj, writeback), default=str))


ALM_TOOLS = [draft_backlog_item, create_backlog_item]
