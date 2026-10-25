"""Create a realistic incident + root-cause story in YOUR ServiceNow instance (real records).

What it creates (on CI EXCH-SD-05 by default, an Exchange server in PDI demo data):
  * a change request that finished ~90 min ago: TLS connector change on the Exchange server
  * a P2 incident opened ~60 min ago: users cannot send email, logged to Service Desk (misrouted)
    with two work notes that point at TLS errors after the maintenance window
  * two likely duplicates opened after it
  * three resolved incidents from previous weeks with the same symptom and fix
  * a known-error problem with a workaround
The agent's RCA, who-solved, duplicates and routing then all have something real to find.

  python scripts/seed_rca_story.py              asks before writing
  python scripts/seed_rca_story.py --ci "MY-CI"  use another CI by name
Run once. Every record is tagged "[demo-rca]" in its description so you can find and delete them.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from incident_agent.servicenow import ServiceNowClient, ServiceNowError  # noqa: E402

TAG = "[demo-rca]"
UTC = timezone.utc


def utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ci", default="EXCH-SD-05")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = ap.parse_args()

    c = ServiceNowClient()
    ci = c._list("cmdb_ci", f"name={args.ci}", ["sys_id", "name"], limit=1, display=False)
    if not ci:
        print(f"CI '{args.ci}' not found. Pick an existing CI name with --ci.")
        return 1
    ci_id = ci[0]["sys_id"]
    print(f"Instance: {c.base}\nCI: {args.ci} ({ci_id})")
    print("Will create: 1 change, 1 P2 incident + 2 work notes, 2 duplicates, 3 resolved past incidents, 1 known-error problem.")
    if not args.yes and input("Create these records? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Nothing created.")
        return 0

    now = datetime.now(UTC)
    made = []

    def post(table, body):
        r = c._req("POST", f"table/{table}", params={"sysparm_input_display_value": "true", "sysparm_fields": "sys_id,number"}, json=body)
        made.append(r.get("number"))
        return r

    def patch_raw(table, sys_id, body):
        try:
            c._req("PATCH", f"table/{table}/{sys_id}", params={"sysparm_input_display_value": "false"}, json=body)
        except ServiceNowError as e:
            print(f"   (could not set {', '.join(body)} on {table}: {e})")

    try:
        chg = post("change_request", {"short_description": "Disable legacy TLS 1.0 send connector on EXCH-SD-05",
                                      "description": f"{TAG} Security hardening: remove TLS 1.0/1.1 from the outbound connector and restart transport.",
                                      "type": "normal", "risk": "Moderate", "assignment_group": "Network", "cmdb_ci": ci_id})
        patch_raw("change_request", chg["sys_id"], {"start_date": utc(now - timedelta(minutes=120)), "end_date": utc(now - timedelta(minutes=90)),
                                                    "work_start": utc(now - timedelta(minutes=115)), "work_end": utc(now - timedelta(minutes=92))})
        print(f"OK change {chg['number']}")

        prb = post("problem", {"short_description": "Exchange outbound mail fails after TLS connector changes",
                               "description": f"{TAG} Partners still on TLS 1.0 reject mail; transport queue grows.",
                               "cmdb_ci": ci_id, "known_error": "true",
                               "cause_notes": "Outbound connector change removed TLS versions some partners still need.",
                               "workaround": "Re-enable TLS 1.2 fallback on the send connector and restart MSExchangeTransport."})
        print(f"OK problem {prb['number']} (known error)")

        past = [("Emails stuck in Outbox after Exchange maintenance", 21), ("Cannot send email - TLS negotiation failed", 14),
                ("Outbound email queue growing on Exchange", 6)]
        for sd, days in past:
            inc = post("incident", {"short_description": sd, "description": f"{TAG} past incident", "cmdb_ci": ci_id,
                                    "impact": "2", "urgency": "2", "assignment_group": "Network", "assigned_to": "Don Goodliffe",
                                    "caller_id": "Abel Tuter", "category": "network"})
            patch_raw("incident", inc["sys_id"], {"opened_at": utc(now - timedelta(days=days))})
            c._req("PATCH", f"table/incident/{inc['sys_id']}", params={"sysparm_input_display_value": "true"},
                   json={"state": "6", "close_code": "Solution provided",
                         "close_notes": "Re-enabled TLS 1.2 fallback on the send connector and restarted MSExchangeTransport; queue drained.",
                         "resolved_by": "Don Goodliffe"})
            patch_raw("incident", inc["sys_id"], {"resolved_at": utc(now - timedelta(days=days) + timedelta(hours=2, minutes=40))})
            print(f"OK past incident {inc['number']} (resolved)")

        main_inc = post("incident", {"short_description": "Outlook cannot send email - messages stuck in Outbox",
                                     "description": f"{TAG} Users across Plano cannot send external email since about an hour ago.",
                                     "cmdb_ci": ci_id, "impact": "1", "urgency": "2", "assignment_group": "Service Desk",
                                     "caller_id": "Abel Tuter", "category": "network"})
        patch_raw("incident", main_inc["sys_id"], {"opened_at": utc(now - timedelta(minutes=60))})
        c.add_note(main_inc["number"], "Users get 'TLS negotiation failed' on send. Internal mail works, external does not.")
        c.add_note(main_inc["number"], f"Transport queue on {args.ci} growing since the maintenance window; checking {chg['number']}.")
        print(f"OK main incident {main_inc['number']} (P2, Service Desk) + 2 work notes")

        for sd, mins in (("Emails stuck in outbox - Finance team", 40), ("Cannot send external email", 25)):
            d = post("incident", {"short_description": sd, "description": f"{TAG} duplicate", "cmdb_ci": ci_id,
                                  "impact": "2", "urgency": "2", "assignment_group": "Service Desk", "caller_id": "Abel Tuter"})
            patch_raw("incident", d["sys_id"], {"opened_at": utc(now - timedelta(minutes=mins))})
            print(f"OK duplicate {d['number']}")
    except ServiceNowError as e:
        print(f"X Stopped: {e}")
        print("Created so far:", ", ".join(n for n in made if n))
        return 1

    print("\nDone. Created:", ", ".join(n for n in made if n))
    print(f"In the UI open {main_inc['number']} and click Run RCA.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
