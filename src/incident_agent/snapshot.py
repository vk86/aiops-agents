"""Snapshot mode: run the agent on real records exported from your ServiceNow instance.

SERVICENOW_MODE=snapshot reads CSV exports from the `snapshot/` folder (SNAPSHOT_DIR to change):
  incidents.csv   required   incident list export          (or ServiceNow's default name incident.csv)
  journal.csv     optional   work notes and comments       (sys_journal_field.csv)
  changes.csv     optional   change requests               (change_request.csv)
  slas.csv        optional   SLA records                   (task_sla.csv)
Export links are in README ("Snapshot mode"). Column headers may be field names
(short_description) or labels (Short description).

SNAPSHOT_REBASE=true (default) shifts every timestamp so the newest incident looks like it was
opened ~30 minutes ago. PDI demo data is years old; rebasing keeps the relative timing (what was
opened when, how long fixes took) but makes the shift briefing and SLA views meaningful today.
Writes (create, note, resolve) change the in-memory copy only; nothing goes back to ServiceNow.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .fake_servicenow import FakeServiceNowClient
from .servicenow import ServiceNowError

UTC = timezone.utc
LABELS = {  # label -> field name
    "number": "number", "sys id": "sys_id", "short description": "short_description", "description": "description",
    "state": "state", "incident state": "state", "priority": "priority", "impact": "impact", "urgency": "urgency",
    "assignment group": "assignment_group", "assigned to": "assigned_to", "resolved by": "resolved_by",
    "configuration item": "cmdb_ci", "category": "category", "caller": "caller_id", "opened": "opened_at",
    "resolved": "resolved_at", "updated": "sys_updated_on", "resolution code": "close_code",
    "close code": "close_code", "resolution notes": "close_notes", "close notes": "close_notes",
    "reassignment count": "reassignment_count", "problem": "problem_id", "parent incident": "parent_incident",
    "closed": "closed_at", "closed by": "closed_by",
    # journal
    "element id": "element_id", "element": "element", "value": "value", "created": "sys_created_on",
    "created by": "sys_created_by",
    # changes
    "planned start date": "start_date", "planned end date": "end_date", "type": "type", "risk": "risk",
    # sla
    "task": "task", "sla definition": "sla", "business elapsed percentage": "percentage", "has breached": "has_breached",
    "breach time": "planned_end_time", "stage": "stage",
}
TIME_FIELDS = ("opened_at", "resolved_at", "sys_updated_on", "closed_at")
FORMATS = ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%m-%d-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
           "%Y-%m-%d %H:%M", "%m/%d/%Y %I:%M:%S %p")


def _key(h: str) -> str:
    h = h.strip().lstrip("﻿")
    return LABELS.get(h.lower(), h if "_" in h or h.islower() else h.lower().replace(" ", "_"))


ALIASES = {"incidents.csv": ["incident.csv"], "journal.csv": ["sys_journal_field.csv"],
           "changes.csv": ["change_request.csv"], "slas.csv": ["task_sla.csv"]}


def _read(path: Path) -> list[dict]:
    if not path.exists():
        for alt in ALIASES.get(path.name, []):  # ServiceNow's default download names
            if (path.parent / alt).exists():
                path = path.parent / alt
                break
        else:
            return []
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with path.open(newline="", encoding=enc) as f:
                return [{_key(k): (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(f)]
        except UnicodeDecodeError:
            continue
    return []


def _parse(v: str) -> datetime | None:
    for fmt in FORMATS:
        try:
            return datetime.strptime(v.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def load(folder: str | None = None, rebase: bool | None = None) -> FakeServiceNowClient:
    d = Path(folder or os.getenv("SNAPSHOT_DIR") or Path(__file__).resolve().parents[2] / "snapshot")
    incidents = _read(d / "incidents.csv")
    if not incidents:
        raise ServiceNowError(f"Snapshot mode: no incidents found in {d} (incidents.csv or incident.csv). "
                              "Export the incident list from ServiceNow as CSV (see README) and save it there.")
    rebase = (os.getenv("SNAPSHOT_REBASE", "true").lower() != "false") if rebase is None else rebase
    opened = [_parse(r.get("opened_at", "")) for r in incidents]
    newest = max((t for t in opened if t), default=None)
    shift = (datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30) - newest) if (rebase and newest) else timedelta(0)

    def t(v: str) -> str:
        p = _parse(v) if v else None
        return _fmt(p + shift) if p else ""

    rows, by_sysid = {}, {}
    for r in incidents:
        n = (r.get("number") or "").upper()
        if not n:
            continue
        row = {k: r.get(k, "") for k in ("number", "short_description", "description", "state", "priority", "impact",
                                         "urgency", "assignment_group", "assigned_to", "resolved_by", "cmdb_ci",
                                         "category", "caller_id", "close_code", "close_notes", "reassignment_count",
                                         "problem_id", "parent_incident")}
        row["number"] = n
        for f in TIME_FIELDS:
            row[f] = row[f + "_raw"] = t(r.get(f, ""))
        if not row["resolved_at_raw"] and row["state"] in ("Closed",) and row.get("closed_at_raw"):
            row["resolved_at"] = row["resolved_at_raw"] = row["closed_at_raw"]
        row["resolved_by"] = row["resolved_by"] or (r.get("closed_by", "") if row["state"] in ("Resolved", "Closed") else "")
        row["sys_updated_on_raw"] = row["sys_updated_on_raw"] or row["opened_at_raw"]
        row["cmdb_ci_raw"] = row["cmdb_ci"]
        row["sys_id"] = r.get("sys_id") or f"snap{n}"
        rows[n] = row
        by_sysid[row["sys_id"]] = n

    journal: dict[str, list] = {}
    for j in _read(d / "journal.csv"):
        n = by_sysid.get(j.get("element_id", "")) or (j.get("element_id", "").upper() if j.get("element_id", "").upper() in rows else None)
        if n:
            journal.setdefault(n, []).append({"type": "work note" if j.get("element") == "work_notes" else "comment",
                                              "at": t(j.get("sys_created_on", "")), "by": j.get("sys_created_by", ""),
                                              "text": j.get("value", "")})
    for items in journal.values():
        items.sort(key=lambda x: x["at"])

    changes = []
    for c in _read(d / "changes.csv"):
        end = t(c.get("end_date", "")) or t(c.get("closed_at", ""))
        changes.append({"number": c.get("number"), "short_description": c.get("short_description"),
                        "state": c.get("state"), "type": c.get("type"), "assignment_group": c.get("assignment_group"),
                        "risk": c.get("risk"), "cmdb_ci": c.get("cmdb_ci"), "start_date": t(c.get("start_date", "")),
                        "end_date": end, "end_raw": end})

    client = FakeServiceNowClient(data=(rows, journal, changes, {}))
    client._snapshot = str(d)
    slas = _read(d / "slas.csv")
    if slas:
        client.sla_records = lambda group="", limit=50, _s=slas: [
            s for s in _s if (s.get("stage", "").lower() in ("in progress", "in_progress", "paused", "breached", ""))
            and s.get("task", "").upper() in {k for k, v in rows.items() if v["state"] not in ("Resolved", "Closed", "Canceled")}
        ][:limit]
    client.whoami = lambda: {"user_name": "snapshot", "name": f"Snapshot of {len(rows)} incidents"
                             + (" (rebased to now)" if rebase else ""), "email": ""}
    return client
