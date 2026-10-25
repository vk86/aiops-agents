"""ServiceNow Table API client for incident management.

Docs: https://developer.servicenow.com/dev.do#!/reference/api/latest/rest/c_TableAPI
- Reads use sysparm_display_value=true so you get labels ("In Progress", "Network") not sys_ids.
- Writes use sysparm_input_display_value=true so you can pass names ("Network", "Abel Tuter")
  for reference fields instead of sys_ids.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

import requests

from .config import settings

INCIDENT_FIELDS = [
    "sys_id", "number", "short_description", "description", "state", "priority", "impact", "urgency",
    "category", "subcategory", "caller_id", "assignment_group", "assigned_to", "cmdb_ci", "business_service",
    "opened_at", "opened_by", "sys_updated_on", "resolved_at", "close_code", "close_notes", "contact_type",
]

# Out-of-box incident states
STATES = {"new": 1, "in progress": 2, "on hold": 3, "resolved": 6, "closed": 7, "canceled": 8}


class ServiceNowError(RuntimeError):
    pass


# ====================================================================== insight queries
ROW_FIELDS = ["sys_id", "number", "short_description", "description", "state", "priority", "impact", "urgency",
              "assignment_group", "assigned_to", "resolved_by", "cmdb_ci", "category", "caller_id", "opened_at",
              "resolved_at", "sys_updated_on", "close_code", "close_notes", "reassignment_count", "problem_id",
              "parent_incident"]
RAW_KEEP = {"opened_at", "resolved_at", "sys_updated_on", "cmdb_ci", "assignment_group", "sys_id",
            "percentage", "has_breached", "reassignment_count", "parent", "child", "start_date", "end_date",
            "work_start", "work_end", "closed_at", "known_error", "sys_created_on", "element_id"}
STATE_LABEL = {"1": "New", "2": "In Progress", "3": "On Hold", "6": "Resolved", "7": "Closed", "8": "Canceled"}
PRIORITY_LABEL = {"1": "1 - Critical", "2": "2 - High", "3": "3 - Moderate", "4": "4 - Low", "5": "5 - Planning"}


def _flatten(rec: dict) -> dict:
    """sysparm_display_value=all gives {display_value, value} per field; keep the label and, for
    timestamps and references, the raw value as <field>_raw (timestamps are UTC)."""
    row = {}
    for k, v in rec.items():
        if isinstance(v, dict) and "display_value" in v:
            row[k] = v.get("display_value") or ""
            if k in RAW_KEEP:
                row[k + "_raw"] = v.get("value") or ""
        else:
            row[k] = v
    return row


class InsightQueries:
    """Mixed into ServiceNowClient. Every method returns plain rows for insights.py."""

    def _rows(self, table: str, query: str, fields=ROW_FIELDS, limit: int = 200) -> list[dict]:
        params = {"sysparm_query": query, "sysparm_limit": limit, "sysparm_display_value": "all",
                  "sysparm_exclude_reference_link": "true", "sysparm_fields": ",".join(fields)}
        return [_flatten(r) for r in self._req("GET", f"table/{table}", params=params)]

    @staticmethod
    def _grp(group: str, prefix: str = "") -> str:
        return f"^{prefix}assignment_group.nameLIKE{group}" if group else ""

    def incident_row(self, number: str) -> dict:
        rows = self._rows("incident", f"number={number.strip().upper()}", limit=1)
        if not rows:
            raise ServiceNowError(f"Incident {number} not found.")
        return rows[0]

    def open_incidents(self, group: str = "", limit: int = 200) -> list[dict]:
        return self._rows("incident", "active=true" + self._grp(group) + "^ORDERBYpriority^ORDERBYDESCopened_at", limit=limit)

    def incidents_window(self, kind: str, hours: int, group: str = "", limit: int = 100) -> list[dict]:
        q = {"opened": f"opened_at>=javascript:gs.hoursAgo({hours})",
             "resolved": f"resolved_at>=javascript:gs.hoursAgo({hours})",
             "updated_open": f"active=true^sys_updated_on>=javascript:gs.hoursAgo({hours})"}[kind]
        return self._rows("incident", q + self._grp(group) + "^ORDERBYDESCsys_updated_on", limit=limit)

    def resolved_days(self, days: int, group: str = "", limit: int = 500) -> list[dict]:
        return self._rows("incident", f"resolved_at>=javascript:gs.daysAgo({days})" + self._grp(group), limit=limit)

    def opened_days(self, days: int, limit: int = 500) -> list[dict]:
        return self._rows("incident", f"opened_at>=javascript:gs.daysAgo({days})^ORDERBYopened_at", limit=limit)

    def sla_records(self, group: str = "", limit: int = 50) -> list[dict]:
        try:
            rows = self._rows("task_sla", "active=true^task.sys_class_name=incident" + self._grp(group, "task.")
                              + "^ORDERBYDESCpercentage",
                              fields=["task", "sla", "percentage", "has_breached", "planned_end_time", "stage"], limit=limit)
        except ServiceNowError:
            return []  # SLA plugin/table not readable: insights fall back to priority targets
        return rows

    def history_for(self, target: dict, limit: int = 150) -> list[dict]:
        from .insights import keywords
        terms = [f"short_descriptionLIKE{w}" for w in keywords(target, 4)]
        if target.get("cmdb_ci_raw"):
            terms.append(f"cmdb_ci={target['cmdb_ci_raw']}")
        if not terms:
            return []
        q = "stateIN6,7^" + "^OR".join(terms) + f"^number!={target.get('number', '')}^ORDERBYDESCresolved_at"
        return self._rows("incident", q, limit=limit)

    def ci_support_group(self, ci_sys_id: str | None) -> str | None:
        if not ci_sys_id:
            return None
        try:
            rows = self._rows("cmdb_ci", f"sys_id={ci_sys_id}", fields=["name", "support_group"], limit=1)
        except ServiceNowError:
            return None
        return (rows[0].get("support_group") or None) if rows else None

    # ---------------------------------------------------------------- RCA evidence
    def related_cis(self, ci_sys_id: str | None, with_services: bool = True) -> list[dict]:  # live CMDB always includes services
        if not ci_sys_id:
            return []
        try:
            rows = self._rows("cmdb_rel_ci", f"parent={ci_sys_id}^ORchild={ci_sys_id}", fields=["parent", "child", "type"], limit=50)
        except ServiceNowError:
            return []
        out = []
        for r in rows:
            labels = (r.get("type") or "connected to::connected to").split("::")
            if r.get("parent_raw") == ci_sys_id:  # this CI is the parent: the other CI is downstream of it
                out.append({"name": r.get("child"), "sys_id": r.get("child_raw"), "relation": labels[0].lower(), "dir": "down"})
            else:
                out.append({"name": r.get("parent"), "sys_id": r.get("parent_raw"), "relation": labels[-1].lower(), "dir": "up"})
        return [o for o in out if o["name"]]

    def changes_near(self, ci_sys_ids: list[str], limit: int = 100) -> list[dict]:
        ids = [i for i in ci_sys_ids if i]
        if not ids:
            return []
        try:
            rows = self._rows("change_request", f"cmdb_ciIN{','.join(ids)}^ORDERBYDESCsys_updated_on",
                              fields=["number", "short_description", "description", "cmdb_ci", "type", "risk", "state",
                                      "assignment_group", "start_date", "end_date", "work_start", "work_end", "closed_at"],
                              limit=limit)
        except ServiceNowError:
            return []
        for r in rows:
            r["start_raw"] = r.get("work_start_raw") or r.get("start_date_raw")
            r["end_raw"] = r.get("work_end_raw") or r.get("end_date_raw") or r.get("closed_at_raw")
        return rows

    # ---------------------------------------------------------------- anomaly + transaction data
    def latest_opened(self) -> str | None:
        rows = self._rows("incident", "ORDERBYDESCopened_at", fields=["opened_at"], limit=1)
        return rows[0].get("opened_at_raw") if rows else None

    def opened_between(self, start_raw: str, end_raw: str, limit: int = 2000) -> list[dict]:
        """Incidents opened between two UTC times ("YYYY-MM-DD HH:MM:SS")."""
        q = (f"opened_at>=javascript:gs.dateGenerate('{start_raw[:10]}','{start_raw[11:19]}')"
             f"^opened_at<=javascript:gs.dateGenerate('{end_raw[:10]}','{end_raw[11:19]}')^ORDERBYopened_at")
        return self._rows("incident", q, limit=limit)

    def alerts(self, hours: int = 24, limit: int = 2000) -> list[dict]:
        """Event Management alerts (em_alert). Raises ServiceNowError if the plugin/table is not available."""
        return self._rows("em_alert", f"sys_created_on>=javascript:gs.hoursAgo({hours})^ORDERBYsys_created_on",
                          fields=["number", "source", "node", "type", "metric_name", "severity", "state", "description",
                                  "cmdb_ci", "message_key", "sys_created_on"], limit=limit)

    def business_services(self, limit: int = 200) -> list[dict]:
        try:
            return self._rows("cmdb_ci_service", "ORDERBYname", fields=["sys_id", "name", "busines_criticality",
                                                                        "operational_status"], limit=limit)
        except ServiceNowError:
            return []

    def ci_lookup(self, name: str) -> dict | None:
        rows = self._rows("cmdb_ci", f"name={name.strip()}", fields=["sys_id", "name", "sys_class_name"], limit=1)
        return rows[0] if rows else None

    def incidents_on_ci(self, ci_sys_id: str, active_only: bool = True, limit: int = 20) -> list[dict]:
        if not ci_sys_id:
            return []
        q = f"cmdb_ci={ci_sys_id}" + ("^active=true" if active_only else "") + "^ORDERBYpriority^ORDERBYDESCopened_at"
        return self._rows("incident", q, limit=limit)

    def mentions(self, text: str, limit: int = 20) -> tuple[list[dict], list[dict]]:
        """Incidents and work notes/comments that mention a string (e.g. a transaction or correlation id)."""
        t = text.strip()
        inc = self._rows("incident", f"short_descriptionLIKE{t}^ORdescriptionLIKE{t}^ORclose_notesLIKE{t}"
                                     "^ORDERBYDESCopened_at", limit=limit)
        try:
            notes = self._rows("sys_journal_field", f"name=incident^valueLIKE{t}^ORDERBYDESCsys_created_on",
                               fields=["element_id", "element", "value", "sys_created_on", "sys_created_by"], limit=limit)
        except ServiceNowError:
            notes = []
        ids = sorted({n.get("element_id_raw") or n.get("element_id") for n in notes} - {None, ""})
        num = {}
        if ids:
            num = {r["sys_id_raw" if "sys_id_raw" in r else "sys_id"]: r["number"]
                   for r in self._rows("incident", f"sys_idIN{','.join(ids)}", fields=["sys_id", "number"], limit=len(ids))}
        out = [{"number": num.get(n.get("element_id_raw") or n.get("element_id"), ""), "type": n.get("element"),
                "text": n.get("value", ""), "at": n.get("sys_created_on_raw") or n.get("sys_created_on"),
                "by": n.get("sys_created_by")} for n in notes]
        return inc, out

    # ---------------------------------------------------------------- changes for the change guard / SLO scorecard
    CHG_FIELDS = ["sys_id", "number", "short_description", "description", "cmdb_ci", "type", "risk", "impact", "state",
                  "assignment_group", "assigned_to", "start_date", "end_date", "work_start", "work_end", "closed_at",
                  "close_code", "backout_plan", "test_plan", "implementation_plan", "justification"]

    @staticmethod
    def _chg(rows: list[dict]) -> list[dict]:
        for r in rows:
            r["start_raw"] = r.get("work_start_raw") or r.get("start_date_raw")
            r["end_raw"] = r.get("work_end_raw") or r.get("end_date_raw") or r.get("closed_at_raw")
        return rows

    def change_row(self, number: str) -> dict:
        rows = self._chg(self._rows("change_request", f"number={number.strip().upper()}", fields=self.CHG_FIELDS, limit=1))
        if not rows:
            raise ServiceNowError(f"Change {number} not found.")
        return rows[0]

    def upcoming_changes(self, days: int = 7, limit: int = 50) -> list[dict]:
        """Active changes starting from 2 hours ago to N days ahead (scheduled, authorize, implement...)."""
        q = (f"active=true^start_date>=javascript:gs.hoursAgo(2)^start_date<=javascript:gs.daysAgo(-{int(days)})"
             "^ORDERBYstart_date")
        return self._chg(self._rows("change_request", q, fields=self.CHG_FIELDS, limit=limit))

    def active_changes(self, limit: int = 50) -> list[dict]:
        return self._chg(self._rows("change_request", "active=true^ORDERBYDESCsys_updated_on", fields=self.CHG_FIELDS, limit=limit))

    def change_history(self, ci_sys_id: str, days: int = 90, limit: int = 200) -> list[dict]:
        if not ci_sys_id:
            return []
        q = f"cmdb_ci={ci_sys_id}^end_date>=javascript:gs.daysAgo({int(days)})^ORDERBYDESCend_date"
        return self._chg(self._rows("change_request", q, fields=self.CHG_FIELDS, limit=limit))

    def add_change_note(self, number: str, text: str) -> dict:
        sid = self.change_row(number).get("sys_id_raw") or self.change_row(number).get("sys_id")
        return self._req("PATCH", f"table/change_request/{sid}", params={"sysparm_fields": "number,sys_id"}, json={"work_notes": text})

    def create_event(self, fields: dict) -> dict:
        """Send an event to ServiceNow Event Management (em_event); its rules de-duplicate and create alerts/incidents."""
        r = self._req("POST", "table/em_event", json=fields)
        return {"sys_id": (r or {}).get("sys_id"), "message_key": fields.get("message_key"), "table": "em_event"}

    def audit_history(self, row: dict) -> list[dict]:
        try:
            rows = self._rows("sys_audit", f"tablename=incident^documentkey={row.get('sys_id_raw') or row.get('sys_id')}"
                              "^fieldnameINstate,priority,assignment_group,assigned_to,impact,urgency^ORDERBYsys_created_on",
                              fields=["fieldname", "oldvalue", "newvalue", "user", "sys_created_on"], limit=100)
        except ServiceNowError:
            return []  # sys_audit usually needs admin; RCA works without it
        out = []
        for a in rows:
            f = a.get("fieldname")
            lab = STATE_LABEL if f == "state" else PRIORITY_LABEL if f == "priority" else {}
            old, new = a.get("oldvalue") or "", a.get("newvalue") or ""
            if f in ("assignment_group", "assigned_to"):
                old, new = ("set" if old else ""), "changed"
            out.append({"field": f, "old": lab.get(old, old), "new": lab.get(new, new), "by": a.get("user"),
                        "at_raw": a.get("sys_created_on_raw") or a.get("sys_created_on")})
        return out

    def concurrent_on(self, ci_sys_ids: list[str], exclude: str, limit: int = 50) -> list[dict]:
        ids = [i for i in ci_sys_ids if i]
        if not ids:
            return []
        return self._rows("incident", f"cmdb_ciIN{','.join(ids)}^number!={exclude}^ORDERBYDESCopened_at", limit=limit)

    def problems_for(self, target: dict, limit: int = 20) -> list[dict]:
        from .insights import keywords
        terms = [f"short_descriptionLIKE{w}" for w in keywords(target, 3)]
        if target.get("cmdb_ci_raw"):
            terms.insert(0, f"cmdb_ci={target['cmdb_ci_raw']}")
        if not terms:
            return []
        try:
            return self._rows("problem", "^OR".join(terms) + "^ORDERBYDESCopened_at",
                              fields=["number", "short_description", "state", "known_error", "cause_notes", "fix_notes",
                                      "workaround", "cmdb_ci", "opened_at"], limit=limit)
        except ServiceNowError:
            return []

    def add_attachment(self, number: str, file_name: str, content: str, content_type: str = "text/markdown") -> dict:
        """Attach a file to an incident (Attachment API)."""
        sid = self._sys_id(number)
        headers = {"Content-Type": content_type, "Accept": "application/json"}
        if getattr(self, "auth_mode", "basic") == "oauth":
            headers["Authorization"] = f"Bearer {self._bearer()}"
        try:
            r = self.s.post(f"{self.base}/api/now/attachment/file", data=content.encode("utf-8"), headers=headers,
                            params={"table_name": "incident", "table_sys_id": sid, "file_name": file_name}, timeout=self.timeout)
        except requests.RequestException as e:
            raise ServiceNowError(f"Could not upload attachment: {e}") from e
        if r.status_code >= 400:
            raise ServiceNowError(f"Attachment upload failed ({r.status_code}): {r.text[:200]}")
        res = r.json().get("result", {})
        return {"file_name": res.get("file_name", file_name), "sys_id": res.get("sys_id")}

    def update_problem(self, number: str, fields: dict) -> dict:
        rows = self._list("problem", f"number={number.strip().upper()}", ["sys_id"], limit=1, display=False)
        if not rows:
            raise ServiceNowError(f"Problem {number} not found.")
        return self._req("PATCH", f"table/problem/{rows[0]['sys_id']}",
                         params={"sysparm_input_display_value": "true", "sysparm_display_value": "true",
                                 "sysparm_fields": "number,short_description,cause_notes,fix_notes"}, json=fields)

    def set_parent(self, child: str, parent: str) -> dict:
        return self.update_incident(child, {"parent_incident": parent.strip().upper()})

    def create_problem(self, fields: dict, incident_numbers: list[str]) -> dict:
        body = {k: v for k, v in fields.items() if v not in (None, "")}
        prb = self._req("POST", "table/problem",
                        params={"sysparm_input_display_value": "true", "sysparm_display_value": "true",
                                "sysparm_exclude_reference_link": "true",
                                "sysparm_fields": "number,short_description,state,assignment_group,cmdb_ci"},
                        json=body)
        linked = []
        for n in incident_numbers:
            try:
                self.update_incident(n, {"problem_id": prb.get("number")})
                linked.append(n)
            except ServiceNowError:
                pass
        return {**prb, "linked_incidents": linked}




class ServiceNowClient(InsightQueries):
    """auth="basic": user + password.  auth="oauth": OAuth 2.0 client credentials
    (client id + secret from a ServiceNow inbound OAuth integration); the token is fetched
    from /oauth_token.do, cached, and refreshed when it expires or is rejected."""

    def __init__(self, instance_url: str | None = None, username: str | None = None,
                 password: str | None = None, timeout: float | None = None,
                 session: requests.Session | None = None, auth: str | None = None,
                 client_id: str | None = None, client_secret: str | None = None):
        self.base = (instance_url or settings.sn_instance).rstrip("/")
        if not self.base:
            raise ServiceNowError("SERVICENOW_INSTANCE_URL is not set. Copy .env.example to .env and fill it in.")
        self.timeout = timeout or settings.sn_timeout
        self.s = session or requests.Session()
        self.s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        self.auth_mode = (auth or settings.sn_auth).lower()
        self.user = username or settings.sn_user
        if self.auth_mode == "oauth":
            self.client_id = client_id or settings.sn_client_id
            self.client_secret = client_secret or settings.sn_client_secret
            if not (self.client_id and self.client_secret):
                raise ServiceNowError("SERVICENOW_AUTH=oauth needs SERVICENOW_CLIENT_ID and SERVICENOW_CLIENT_SECRET in .env.")
            self._token, self._token_exp = None, 0.0
        else:
            self.s.auth = (self.user, password or settings.sn_password)

    # ------------------------------------------------------------------ auth
    def _bearer(self, force: bool = False) -> str:
        if not force and self._token and time.time() < self._token_exp - 60:
            return self._token
        try:
            r = requests.post(f"{self.base}/oauth_token.do", timeout=self.timeout,
                              data={"grant_type": "client_credentials", "client_id": self.client_id,
                                    "client_secret": self.client_secret},
                              headers={"Accept": "application/json"})
        except requests.RequestException as e:
            raise ServiceNowError(f"Could not reach {self.base}/oauth_token.do: {e}") from e
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code != 200 or "access_token" not in body:
            reason = body.get("error_description") or body.get("error") or r.text[:200]
            raise ServiceNowError(f"OAuth token request failed ({r.status_code}): {reason}. Check the client id/secret, "
                                  "that the integration uses the Client Credentials grant, and that it has an "
                                  "OAuth Application User.")
        self._token = body["access_token"]
        self._token_exp = time.time() + float(body.get("expires_in", 1800))
        return self._token

    # ------------------------------------------------------------------ low level
    def _send(self, method, url, params, json, force_token=False):
        headers = {"Authorization": f"Bearer {self._bearer(force_token)}"} if self.auth_mode == "oauth" else None
        return self.s.request(method, url, params=params, json=json, timeout=self.timeout, headers=headers)

    def _req(self, method: str, path: str, params: dict | None = None, json: dict | None = None) -> Any:
        url = f"{self.base}/api/now/{path.lstrip('/')}"
        try:
            r = self._send(method, url, params, json)
            if r.status_code == 401 and self.auth_mode == "oauth":  # token revoked/expired early
                r = self._send(method, url, params, json, force_token=True)
        except requests.RequestException as e:
            raise ServiceNowError(f"Could not reach {self.base}: {e}") from e
        if r.status_code == 401:
            if self.auth_mode == "oauth":
                raise ServiceNowError("ServiceNow rejected the OAuth token (401). Check the integration's "
                                      "OAuth Application User is active and has the itil role.")
            raise ServiceNowError("ServiceNow rejected the login (401). Either the user/password is wrong, or the "
                                  "instance blocks Basic Auth for the REST API (common on newer releases). "
                                  "Use SERVICENOW_AUTH=oauth with client credentials; see README.")
        if r.status_code == 403:
            raise ServiceNowError("The ServiceNow user lacks permission for this action (403). It needs the itil or admin role.")
        if r.status_code >= 400:
            try:
                detail = r.json().get("error", {})
                msg = f"{detail.get('message', '')} {detail.get('detail', '')}".strip()
            except ValueError:
                msg = r.text[:300]
            raise ServiceNowError(f"ServiceNow {method} {path} failed ({r.status_code}): {msg}")
        if not r.content:
            return {}
        if "json" not in r.headers.get("Content-Type", ""):
            raise ServiceNowError("ServiceNow returned a non-JSON page. A hibernating PDI does this; "
                                  "wake it at developer.servicenow.com and retry.")
        return r.json().get("result", {})

    def _list(self, table: str, query: str, fields: list[str] | None = None, limit: int = 10,
              display: bool = True) -> list[dict]:
        params = {"sysparm_query": query, "sysparm_limit": limit,
                  "sysparm_display_value": "true" if display else "false",
                  "sysparm_exclude_reference_link": "true"}
        if fields:
            params["sysparm_fields"] = ",".join(fields)
        return self._req("GET", f"table/{table}", params=params)

    # ------------------------------------------------------------------ incidents
    def get_incident(self, number: str) -> dict:
        rows = self._list("incident", f"number={number.strip().upper()}", INCIDENT_FIELDS, limit=1)
        if not rows:
            raise ServiceNowError(f"Incident {number} not found.")
        return rows[0]

    def _sys_id(self, number: str) -> str:
        rows = self._list("incident", f"number={number.strip().upper()}", ["sys_id"], limit=1, display=False)
        if not rows:
            raise ServiceNowError(f"Incident {number} not found.")
        return rows[0]["sys_id"]

    def search_incidents(self, text: str = "", state: str = "", priority: str = "", assignment_group: str = "",
                         active_only: bool = True, encoded_query: str = "", limit: int = 10) -> list[dict]:
        q = []
        if encoded_query:
            q.append(encoded_query)
        if active_only:
            q.append("active=true")
        if text:
            q.append(f"short_descriptionLIKE{text}^ORdescriptionLIKE{text}")
        if state:
            q.append(f"state={STATES.get(state.lower(), state)}")
        if priority:  # "2" = exactly P2, "<=2" = P1 and P2
            p = priority.replace(" ", "")
            q.append(f"priority{p}" if p[:1] in "<>=" else f"priority={p}")
        if assignment_group:
            q.append(f"assignment_group.nameLIKE{assignment_group}")
        q.append("ORDERBYDESCsys_created_on")
        fields = ["number", "short_description", "state", "priority", "assignment_group", "assigned_to",
                  "cmdb_ci", "opened_at"]
        return self._list("incident", "^".join(q), fields, limit=min(max(limit, 1), 50))

    def get_journal(self, number: str, limit: int = 20) -> list[dict]:
        """Work notes and customer comments, oldest first."""
        sid = self._sys_id(number)
        rows = self._list("sys_journal_field",
                          f"element_id={sid}^elementINwork_notes,comments^ORDERBYsys_created_on",
                          ["element", "value", "sys_created_on", "sys_created_by"], limit=limit)
        return [{"type": "work note" if r.get("element") == "work_notes" else "comment",
                 "at": r.get("sys_created_on"), "by": r.get("sys_created_by"), "text": r.get("value")} for r in rows]

    def create_incident(self, fields: dict) -> dict:
        body = {k: v for k, v in fields.items() if v not in (None, "")}
        return self._req("POST", "table/incident",
                         params={"sysparm_input_display_value": "true", "sysparm_display_value": "true",
                                 "sysparm_exclude_reference_link": "true",
                                 "sysparm_fields": ",".join(INCIDENT_FIELDS)},
                         json=body)

    def update_incident(self, number: str, fields: dict) -> dict:
        body = {k: v for k, v in fields.items() if v not in (None, "")}
        if not body:
            raise ServiceNowError("Nothing to update.")
        if "state" in body:
            body["state"] = str(STATES.get(str(body["state"]).lower(), body["state"]))
        return self._req("PATCH", f"table/incident/{self._sys_id(number)}",
                         params={"sysparm_input_display_value": "true", "sysparm_display_value": "true",
                                 "sysparm_exclude_reference_link": "true",
                                 "sysparm_fields": ",".join(INCIDENT_FIELDS)},
                         json=body)

    def add_note(self, number: str, text: str, customer_visible: bool = False) -> dict:
        return self.update_incident(number, {"comments" if customer_visible else "work_notes": text})

    def resolve_incident(self, number: str, close_code: str, close_notes: str) -> dict:
        return self.update_incident(number, {"state": "6", "close_code": close_code, "close_notes": close_notes})

    # ------------------------------------------------------------------ context for triage
    def find_similar(self, text: str, exclude_number: str = "", limit: int = 5) -> list[dict]:
        words = [w for w in "".join(c if c.isalnum() else " " for c in text).split() if len(w) > 3][:4]
        if not words:
            return []
        q = "^OR".join(f"short_descriptionLIKE{w}" for w in words)
        if exclude_number:
            q += f"^number!={exclude_number.upper()}"
        q += "^ORDERBYDESCsys_created_on"
        return self._list("incident", q, ["number", "short_description", "state", "priority", "close_code",
                                          "close_notes", "resolved_at"], limit=limit)

    def recent_changes(self, number: str, hours: int = 48) -> list[dict]:
        """Change requests on the same CI that ended within `hours` before the incident opened."""
        inc = self._list("incident", f"number={number.upper()}", ["cmdb_ci", "opened_at"], limit=1, display=False)
        if not inc:
            raise ServiceNowError(f"Incident {number} not found.")
        ci, opened = inc[0].get("cmdb_ci"), inc[0].get("opened_at")
        if not ci:
            return []
        if isinstance(ci, dict):
            ci = ci.get("value", "")
        try:
            t = datetime.strptime(opened, "%Y-%m-%d %H:%M:%S")
            since = (t - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
            window = f"^end_date>={since}^start_date<={opened}"
        except (TypeError, ValueError):
            window = ""
        return self._list("change_request", f"cmdb_ci={ci}{window}^ORDERBYDESCend_date",
                          ["number", "short_description", "state", "type", "start_date", "end_date",
                           "assignment_group", "risk"], limit=10)

    def find_groups(self, name: str = "", limit: int = 10) -> list[dict]:
        q = f"nameLIKE{name}^active=true" if name else "active=true"
        return self._list("sys_user_group", q + "^ORDERBYname", ["name", "description", "manager"], limit=limit)

    def whoami(self) -> dict:
        if self.user:
            rows = self._list("sys_user", f"user_name={self.user}", ["user_name", "name", "email"], limit=1)
            if rows:
                return rows[0]
        self._list("incident", "", ["number"], limit=1)  # proves the token works
        return {"user_name": self.user or "OAuth application user"}


def get_client():
    if settings.sn_mode == "snapshot":
        from .snapshot import load
        return load()
    if settings.sn_mode == "mock":
        from .fake_servicenow import FakeServiceNowClient
        return FakeServiceNowClient()
    return ServiceNowClient()
