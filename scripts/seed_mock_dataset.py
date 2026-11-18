"""Load the full MOCK demo dataset (src/incident_agent/fake_servicenow.py) into a real ServiceNow instance.

What it creates, with the same numbers/names the mock uses (INC0012345, CHG0098871, PRB0039001 ...):
  * assignment groups + users (resolvers, assignees, callers) and group memberships
  * CIs (enrollment-api, vpn-dallas-01, claims-db-prod ...) with support groups, 5 business services,
    and the "Depends on::Used by" CMDB relationships
  * 60 incidents: 15 open + 45 resolved/closed, back-dated over the last 90 days, with work notes
  * changes: recent ones, 90 days of history (incl. failed changes before "resolved by change"
    incidents) and upcoming scheduled ones for the change guard
  * the known-error problem PRB0039001
  * the .env service user gets roles itil + cmdb_read (+ REST role if present) so the agent can read it all

Every record is tagged correlation_id=demo-mock (hidden from the agent's text) so --cleanup can remove them.
Times are relative to NOW, so seed shortly before the demo (or --cleanup and re-seed).

  python scripts/seed_mock_dataset.py                 prompts for the admin password (never saved)
  python scripts/seed_mock_dataset.py --cleanup       delete everything tagged demo-mock
  python scripts/seed_mock_dataset.py --alerts        also send the 24h alert stream to em_event (needs Event Mgmt)
  set SN_ADMIN_PASSWORD=...  to skip the prompt
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from incident_agent import fake_servicenow as fk  # noqa: E402
from incident_agent.config import settings  # noqa: E402

TAG = "demo-mock"
UTC = timezone.utc
AGENT_ROLES = ["itil", "cmdb_read", "snc_platform_rest_api_access"]

CI_CLASS = {"enrollment-api": "cmdb_ci_appl", "eligibility-svc": "cmdb_ci_appl", "plan-catalog-svc": "cmdb_ci_appl",
            "notification-svc": "cmdb_ci_appl", "enrollment-ui": "cmdb_ci_appl", "payment-gw": "cmdb_ci_appl",
            "okta-sso": "cmdb_ci_appl", "vpn-dallas-01": "cmdb_ci_netgear", "dallas-edge-fw": "cmdb_ci_netgear",
            "EXCH-SD-05": "cmdb_ci_win_server", "claims-db-prod": "cmdb_ci_database", "PRN-PLANO-3F": "cmdb_ci_printer"}
EXTRA_SUPPORT = {"plan-catalog-svc": "Eligibility Engineering", "dallas-edge-fw": "Network"}
CATEGORY = {"Inquiry / Help": "inquiry", "Software": "software", "Hardware": "hardware", "Network": "network",
            "Database": "database"}
STATE = {"New": "1", "In Progress": "2", "On Hold": "3", "Resolved": "6", "Closed": "7"}
CHG_STEPS = ["Assess", "Authorize", "Scheduled", "Implement", "Review", "Closed"]
CHG_VALUE = {"New": "-5", "Assess": "-4", "Authorize": "-3", "Scheduled": "-2", "Implement": "-1", "Review": "0",
             "Closed": "3", "Canceled": "4"}
# mock close codes -> choice VALUES on newer releases (Brazil relabelled them); old names kept as fallback
CLOSE_CODE = {"Solution provided": ["Resolved – Permanent Fix Applied", "Solution provided"],
              "Resolved by change": ["Resolved – Permanent Fix Applied", "Resolved by change"],
              "Workaround provided": ["Workaround provided"],
              "User error": ["Resolved by caller", "User error"]}


def ok(msg):
    print("OK " + msg)


def warn(msg):
    print("!  " + msg)


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


class SN:
    def __init__(self, base, user, pw):
        self.base = base
        self.s = requests.Session()
        self.s.auth = (user, pw)
        self.s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    def req(self, method, path, display=None, **kw):
        params = kw.pop("params", {})
        if display is not None:
            params["sysparm_input_display_value"] = "true" if display else "false"
        r = self.s.request(method, f"{self.base}/api/now/{path}", params=params, timeout=90, **kw)
        if method == "DELETE" and r.status_code in (200, 204):
            return None
        if "json" not in r.headers.get("Content-Type", ""):
            raise RuntimeError(f"HTTP {r.status_code}, non-JSON page (instance asleep?). Wake it in a browser and retry.")
        body = r.json()
        if r.status_code >= 400:
            e = body.get("error") or {}
            raise RuntimeError(f"HTTP {r.status_code}: {e.get('message')} {e.get('detail') or ''}".strip())
        return body.get("result")

    def get(self, table, query, fields="sys_id", limit=1):
        return self.req("GET", f"table/{table}", params={"sysparm_query": query, "sysparm_limit": limit,
                                                         "sysparm_fields": fields}) or []

    def first(self, table, query, fields="sys_id"):
        rows = self.get(table, query, fields)
        return rows[0] if rows else None

    def post(self, table, body, display=False):
        return self.req("POST", f"table/{table}", display=display, json=body)

    def patch(self, table, sys_id, body, display=False):
        return self.req("PATCH", f"table/{table}/{sys_id}", display=display, json=body)


# ---------------------------------------------------------------------------------------- reference data
def ensure_groups(sn, names):
    out = {}
    for g in sorted(names):
        row = sn.first("sys_user_group", f"name={g}")
        out[g] = row["sys_id"] if row else sn.post("sys_user_group", {"name": g, "description": f"{TAG} demo group"})["sys_id"]
    ok(f"{len(out)} assignment groups ready")
    return out


def ensure_users(sn, names):
    out = {}
    made = 0
    for n in sorted(names):
        row = sn.first("sys_user", f"name={n}")
        if not row:
            first, _, last = n.partition(" ")
            row = sn.post("sys_user", {"first_name": first, "last_name": last or first, "active": "true",
                                       "user_name": n.lower().replace(" ", "."), "email": f"{n.lower().replace(' ', '.')}@example.com"})
            made += 1
        out[n] = row["sys_id"]
    ok(f"{len(out)} users ready ({made} created)")
    return out


def ensure_members(sn, users, groups, pairs):
    for u, g in sorted(pairs):
        if u in users and g in groups and not sn.first("sys_user_grmember", f"user={users[u]}^group={groups[g]}"):
            sn.post("sys_user_grmember", {"user": users[u], "group": groups[g]})
    ok(f"{len(pairs)} group memberships ready")


def grant_agent_roles(sn, user_name):
    u = sn.first("sys_user", f"user_name={user_name}")
    if not u:
        warn(f".env user '{user_name}' not found on the instance; roles not granted")
        return
    for role in AGENT_ROLES:
        r = sn.first("sys_user_role", f"name={role}")
        if not r:
            continue
        if not sn.first("sys_user_has_role", f"user={u['sys_id']}^role={r['sys_id']}"):
            sn.post("sys_user_has_role", {"user": u["sys_id"], "role": r["sys_id"]})
            ok(f"granted {role} to {user_name}")
    ok(f"agent user {user_name} has the read roles")


def ensure_cis(sn, groups):
    out = {}
    support = {**fk.CI_SUPPORT, **EXTRA_SUPPORT}
    names = set(CI_CLASS) | {c for rel in fk.RELATIONS for c in rel[:2]}
    for name in sorted(names):
        grp = groups.get(support.get(name, ""), "")
        row = sn.first("cmdb_ci", f"name={name}")
        if row:
            if grp:
                sn.patch("cmdb_ci", row["sys_id"], {"support_group": grp})
            out[name] = row["sys_id"]
            continue
        body = {"name": name, "support_group": grp, "operational_status": "1", "correlation_id": TAG,
                "short_description": f"Demo CI ({TAG})"}
        try:
            out[name] = sn.post(CI_CLASS.get(name, "cmdb_ci_appl"), body)["sys_id"]
        except RuntimeError:
            out[name] = sn.post("cmdb_ci_appl", body)["sys_id"]  # class table not on this instance
    for svc in fk.SERVICES:
        row = sn.first("cmdb_ci_service", f"name={svc}")
        out[svc] = row["sys_id"] if row else sn.post("cmdb_ci_service", {
            "name": svc, "operational_status": "1", "busines_criticality": "1 - most critical",
            "correlation_id": TAG, "short_description": f"Demo business service ({TAG})"})["sys_id"]
    ok(f"{len(out)} CIs / business services ready")
    return out


def ensure_relations(sn, cis):
    types = {}
    n = 0
    for parent, child, typ in fk.RELATIONS + fk.SERVICE_RELATIONS:
        if typ not in types:
            t = sn.first("cmdb_rel_type", f"name={typ}")
            if not t:
                warn(f"relationship type '{typ}' missing; skipped")
                continue
            types[typ] = t["sys_id"]
        p, c = cis.get(parent), cis.get(child)
        if p and c and not sn.first("cmdb_rel_ci", f"parent={p}^child={c}^type={types[typ]}"):
            sn.post("cmdb_rel_ci", {"parent": p, "child": c, "type": types[typ]})
            n += 1
    ok(f"CMDB relationships ready ({n} created)")


# ---------------------------------------------------------------------------------------- tasks
def seed_incidents(sn, rows, journal, users, groups, cis):
    made = skipped = 0
    for num, r in sorted(rows.items(), key=lambda kv: kv[1]["opened_at_raw"]):
        old = sn.first("incident", f"number={num}", "sys_id,state,correlation_id")
        if old:
            if old.get("correlation_id") == TAG and old["state"] != STATE[r["state"]]:  # half-done by an earlier run
                sn.req("DELETE", f"table/incident/{old['sys_id']}")
            else:
                skipped += 1
                continue
        imp, urg = r["impact"][0], r["urgency"][0]
        body = {"number": num, "short_description": r["short_description"], "description": r["description"],
                "impact": imp, "urgency": urg, "category": CATEGORY.get(r["category"], "inquiry"),
                "caller_id": users.get(r["caller_id"], ""), "assignment_group": groups.get(r["assignment_group"], ""),
                "assigned_to": users.get(r["assigned_to"], ""), "cmdb_ci": cis.get(r["cmdb_ci"], ""),
                "contact_type": "phone", "correlation_id": TAG}
        inc = sn.post("incident", body)
        sid = inc["sys_id"]
        st = r["state"]
        if st in ("In Progress", "On Hold"):
            sn.patch("incident", sid, {"state": STATE[st], **({"hold_reason": "4"} if st == "On Hold" else {})})
        if st in ("Resolved", "Closed"):
            resolved = False
            for code in CLOSE_CODE.get(r["close_code"], [r["close_code"]]):
                try:
                    sn.patch("incident", sid, {"state": "6", "close_code": code, "close_notes": r["close_notes"],
                                               "resolved_by": users.get(r["resolved_by"], "")})
                    resolved = True
                    break
                except RuntimeError:
                    continue
            if not resolved:
                raise RuntimeError(f"{num}: no accepted resolution code for '{r['close_code']}'")
            if st == "Closed":
                sn.patch("incident", sid, {"state": "7"})
        for note in journal.get(num, []):
            field = "comments" if note["type"] == "comment" else "work_notes"
            sn.patch("incident", sid, {field: note["text"]})
        # back-date last: business rules stamp "now" on the state changes above
        dates = {"opened_at": r["opened_at_raw"], "reassignment_count": r["reassignment_count"]}
        if r["resolved_at_raw"]:
            dates["resolved_at"] = r["resolved_at_raw"]
            dates["resolved_by"] = users.get(r["resolved_by"], "")
            if st == "Closed":
                dates["closed_at"] = fmt(parse(r["resolved_at_raw"]) + timedelta(days=3))
        sn.patch("incident", sid, dates)
        made += 1
        if made % 10 == 0:
            print(f"   ... {made} incidents")
    ok(f"incidents: {made} created, {skipped} already there")


def _chg_state(sn, sid):
    return sn.req("GET", f"table/change_request/{sid}", params={"sysparm_fields": "state"})["state"]


def set_change_state(sn, sid, target, close_code):
    """Walk the change model: Assess -> Authorize -> (approve) -> Scheduled -> Implement -> Review -> Closed.
    Steps a model does not have (standard/emergency) fail with 403 and are skipped."""
    want = CHG_VALUE[target]
    order = [CHG_VALUE[s] for s in CHG_STEPS]
    extra = {"close_code": "unsuccessful" if close_code == "Unsuccessful" else "successful",
             "close_notes": "Change backed out; caused incidents." if close_code == "Unsuccessful" else "Implemented as planned."}
    for step in order[:order.index(want) + 1]:
        cur = _chg_state(sn, sid)
        if cur == want:
            return True
        if cur in order and order.index(cur) >= order.index(step):
            continue
        if step == CHG_VALUE["Scheduled"]:  # approve whatever the model requested, which moves it to Scheduled
            for _ in range(20):  # approval records are created asynchronously after Authorize
                for ap in sn.get("sysapproval_approver", f"sysapproval={sid}^state=requested", "sys_id", limit=50):
                    sn.patch("sysapproval_approver", ap["sys_id"], {"state": "approved"})
                if _chg_state(sn, sid) != CHG_VALUE["Authorize"]:
                    break
                time.sleep(1.5)
            if _chg_state(sn, sid) == want:
                return True
        try:
            sn.patch("change_request", sid, {"state": step, **(extra if step == "3" else {})})
        except RuntimeError:
            continue
    return _chg_state(sn, sid) == want


def seed_changes(sn, changes, groups, cis):
    made = skipped = bad = 0
    for c in changes:
        st, en = c["start_raw"], c["end_raw"]
        past = parse(en) <= datetime.now(UTC)
        dates = {"start_date": st, "end_date": en}
        if c["state"] in ("Closed", "Review", "Implement") or past:
            dates.update(work_start=st, work_end=en if past else "")
        old = sn.first("change_request", f"number={c['number']}", "sys_id,state,correlation_id")
        if old:
            skipped += 1
            if old.get("correlation_id") == TAG and old["state"] != CHG_VALUE[c["state"]]:  # finish an earlier partial run
                if not set_change_state(sn, old["sys_id"], c["state"], c.get("close_code", "")):
                    bad += 1
                sn.patch("change_request", old["sys_id"], dates)
            continue
        body = {"number": c["number"], "short_description": c["short_description"], "description": c.get("description", ""),
                "type": c["type"].lower(), "cmdb_ci": cis.get(c["cmdb_ci"], ""),
                "assignment_group": groups.get(c.get("assignment_group", ""), ""),
                "backout_plan": c.get("backout_plan", "Revert to previous version."),
                "test_plan": c.get("test_plan", "Smoke test."), "implementation_plan": c.get("implementation_plan", "See runbook."),
                "correlation_id": TAG}
        chg = sn.post("change_request", body)
        sid = chg["sys_id"]
        sn.patch("change_request", sid, {"risk": c.get("risk", "Low")}, display=True)
        sn.patch("change_request", sid, dates)
        if not set_change_state(sn, sid, c["state"], c.get("close_code", "")):
            bad += 1
        sn.patch("change_request", sid, dates)  # state model may reset dates
        made += 1
        if made % 10 == 0:
            print(f"   ... {made} changes")
    ok(f"changes: {made} created, {skipped} already there (states fixed where needed)")
    if bad:
        warn(f"{bad} changes could not reach their target state (change state model). Dates and CIs are set; "
             "the change guard and RCA still use them.")


def seed_problems(sn, cis):
    for p in fk.PROBLEMS:
        if sn.first("problem", f"number={p['number']}"):
            continue
        sn.post("problem", {"number": p["number"], "short_description": p["short_description"], "cmdb_ci": cis.get(p["cmdb_ci"], ""),
                            "known_error": p["known_error"], "cause_notes": p["cause_notes"], "workaround": p["workaround"],
                            "fix_notes": p["fix_notes"], "correlation_id": TAG})
        ok(f"problem {p['number']} (known error)")


def seed_alerts(sn, now):
    if not sn.first("sys_db_object", "name=em_event"):
        warn("Event Management (em_event) is not on this instance; alerts skipped")
        return
    sev = {"1 - Critical": "1", "2 - Major": "2", "3 - Minor": "3", "4 - Warning": "4"}
    alerts = fk._mock_alerts(now)
    for a in alerts:
        sn.post("em_event", {"source": a["source"], "node": a["node"], "type": a["type"], "resource": a["metric_name"],
                             "metric_name": a["metric_name"], "severity": sev.get(a["severity"], "4"), "description": a["description"],
                             "message_key": a["message_key"], "time_of_event": a["sys_created_on_raw"], "event_class": TAG})
    ok(f"{len(alerts)} events sent to em_event (alerts are created by your event rules at insert time)")


def cleanup(sn):
    for table in ("incident", "change_request", "problem"):
        rows = sn.get(table, f"correlation_id={TAG}", "sys_id", limit=1000)
        for r in rows:
            sn.req("DELETE", f"table/{table}/{r['sys_id']}")
        ok(f"deleted {len(rows)} {table} records")
    cis = sn.get("cmdb_ci", f"correlation_id={TAG}", "sys_id", limit=1000)
    for ci in cis:
        for rel in sn.get("cmdb_rel_ci", f"parent={ci['sys_id']}^ORchild={ci['sys_id']}", "sys_id", limit=200):
            sn.req("DELETE", f"table/cmdb_rel_ci/{rel['sys_id']}")
        sn.req("DELETE", f"table/cmdb_ci/{ci['sys_id']}")
    ok(f"deleted {len(cis)} demo CIs / services and their relationships (users and groups are kept)")


# ---------------------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", default=settings.sn_instance, help="default: SERVICENOW_INSTANCE_URL from .env")
    ap.add_argument("--admin-user", default="admin")
    ap.add_argument("--alerts", action="store_true", help="also send the mock 24h alert stream to em_event")
    ap.add_argument("--cleanup", action="store_true", help="delete records tagged demo-mock and exit")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    base = a.instance.rstrip("/")
    if not base.startswith("http"):
        base = f"https://{base}.service-now.com"
    pw = os.getenv("SN_ADMIN_PASSWORD") or getpass.getpass(f"Password for {a.admin_user} on {base} (not shown, not saved): ")
    sn = SN(base, a.admin_user, pw)
    try:
        sn.first("sys_user", f"user_name={a.admin_user}")
        ok(f"logged in to {base} as {a.admin_user}")
    except RuntimeError as e:
        print(f"X  admin login failed: {e}")
        return 1

    if a.cleanup:
        if not a.yes and input("Delete all demo-mock records? [y/N] ").strip().lower() not in ("y", "yes"):
            return 0
        cleanup(sn)
        return 0

    now = datetime.now(UTC)
    rows, journal = fk._build(now)
    recent = fk._mock_changes(now)
    changes = fk._mock_change_history(now, rows, recent) + fk._mock_upcoming(now)
    print(f"Will load: {len(rows)} incidents, {len(changes)} changes, {len(fk.PROBLEMS)} problem, "
          f"{len(CI_CLASS) + len(fk.SERVICES)} CIs/services, users, groups" + (", alerts" if a.alerts else ""))
    if not a.yes and input("Go? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Nothing created.")
        return 0

    try:
        if settings.sn_user and settings.sn_user != a.admin_user:
            grant_agent_roles(sn, settings.sn_user)
        group_names = {r["assignment_group"] for r in rows.values()} | set(fk.CI_SUPPORT.values()) | set(EXTRA_SUPPORT.values()) \
            | {c.get("assignment_group", "") for c in changes}
        groups = ensure_groups(sn, group_names - {""})
        user_names = {r[k] for r in rows.values() for k in ("caller_id", "assigned_to", "resolved_by")}
        users = ensure_users(sn, user_names - {""})
        pairs = {(r[k], r["assignment_group"]) for r in rows.values() for k in ("assigned_to", "resolved_by") if r[k]}
        ensure_members(sn, users, groups, pairs)
        cis = ensure_cis(sn, groups)
        ensure_relations(sn, cis)
        seed_problems(sn, cis)
        seed_changes(sn, changes, groups, cis)
        seed_incidents(sn, rows, journal, users, groups, cis)
        if a.alerts:
            seed_alerts(sn, now)
    except RuntimeError as e:
        print(f"X  stopped: {e}\n   Re-run the script; records already created are skipped.")
        return 1

    print("\nDone. Start the UI with start-live.bat. Demo stories: INC0012345 (P1 enrollment), INC0012351 (Dallas VPN),\n"
          "INC0012352 (misrouted Outlook), INC0012353 (claims DB SLA). Remove later with --cleanup.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
