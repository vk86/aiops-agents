"""In-memory stand-in for ServiceNowClient (SERVICENOW_MODE=mock).

60 incidents (15 open, 45 resolved over the last 90 days), generated relative to *now* so the
shift briefing always looks current. Built-in demo stories:
  * INC0012345 P1 enrollment outage, with two likely duplicates opened after it
  * INC0012351 VPN Dallas: Don Goodliffe has fixed this 4 times -> "who solved this before"
  * INC0012352 Outlook sync logged to Service Desk -> assignment suggests Messaging / Fred Luddy
  * INC0012353 claims DB slow: P2 open 26h -> SLA breached
  * vpn-dallas-01: 7 incidents in 30 days, no problem record -> problem candidate
Same method signatures as the live client, so tools and UI run offline.
"""
from __future__ import annotations

import copy
import random
from datetime import datetime, timedelta, timezone
from itertools import count

from .servicenow import STATES, ServiceNowError

UTC = timezone.utc
_LABELS = {v: k.title() for k, v in STATES.items()}
_PRIORITY = {1: "1 - Critical", 2: "2 - High", 3: "3 - Moderate", 4: "4 - Low", 5: "5 - Planning"}
_LEVEL = {1: "1 - High", 2: "2 - Medium", 3: "3 - Low"}
_IU = {1: (1, 1), 2: (1, 2), 3: (2, 2), 4: (2, 3), 5: (3, 3)}


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _now() -> str:
    return _fmt(datetime.now(UTC))


def _prio(impact: int, urgency: int) -> str:
    return _PRIORITY[{2: 1, 3: 2, 4: 3, 5: 4, 6: 5}[impact + urgency]]


# theme: (category, ci, group, [(resolver, weight)], [short descriptions], [(close_code, fix)], typical hours)
THEMES = {
    "enroll": ("Software", "enrollment-api", "Enrollment Platform", [("Beth Anglin", 3), ("David Loo", 1)],
               ["Eligibility check timeouts after deploy", "Plan enrollment page returns 504",
                "Enrollment submissions failing intermittently", "Eligibility service timing out calling plan-catalog"],
               [("Resolved by change", "Rolled back the release; restored plan-catalog client timeout to 800ms."),
                ("Solution provided", "Restarted eligibility pods after the nightly cache rebuild overran."),
                ("Workaround provided", "Raised plan-catalog timeout and added one retry; root cause tracked in a problem.")], 2.5),
    "vpn": ("Network", "vpn-dallas-01", "Network", [("Don Goodliffe", 4), ("Bud Richman", 2)],
            ["VPN disconnects every 30 minutes - Dallas office", "Dallas VPN tunnel dropping during calls",
             "Cannot connect to VPN from Dallas office", "VPN keeps disconnecting for Plano users"],
            [("Solution provided", "Cleared stale IKE SAs and reloaded tunnel config on vpn-dallas-01."),
             ("Workaround provided", "Raised DPD timeout on vpn-dallas-01; firmware upgrade scheduled."),
             ("Solution provided", "Replaced failing SFP on the Dallas edge; tunnel stable.")], 3.0),
    "mail": ("Software", "EXCH-SD-05", "Messaging", [("Fred Luddy", 3), ("Luke Wilson", 1)],
             ["Outlook not syncing new email", "Emails stuck in outbox", "Shared mailbox not updating in Outlook"],
             [("Solution provided", "Rebuilt the Outlook OST profile; mailbox synced."),
              ("Solution provided", "Restarted transport service on EXCH-SD-05; queue drained.")], 6.0),
    "lock": ("Inquiry / Help", "", "Service Desk", [("Luke Wilson", 4), ("ITIL User", 2)],
             ["Account locked out after password change", "Password reset not working for SSO users",
              "User locked out of workstation"],
             [("Solution provided", "Unlocked AD account and cleared cached credentials on the laptop."),
              ("User error", "User was typing the old password in Outlook; updated saved credentials.")], 1.0),
    "db": ("Database", "claims-db-prod", "Database", [("Bow Ruggeri", 3)],
           ["Claims database queries very slow", "Claims search timing out", "Batch claim load running long on claims-db-prod"],
           [("Solution provided", "Rebuilt index on claim_line and refreshed statistics."),
            ("Resolved by change", "Rolled back query plan change from last night's release.")], 8.0),
    "pay": ("Software", "payment-gw", "Premium Billing", [("Krystle Stika", 2)],
            ["Premium payment gateway timeouts", "Autopay failing with gateway error"],
            [("Solution provided", "Rotated the expired TLS certificate on payment-gw.")], 4.0),
    "print": ("Hardware", "PRN-PLANO-3F", "Hardware", [("Charlie Whitherspoon", 2)],
              ["3rd floor printer jamming", "Printer offline - Plano 3F"],
              [("Solution provided", "Replaced fuser unit on PRN-PLANO-3F.")], 20.0),
    "sso": ("Software", "okta-sso", "Identity", [("Rick Berzle", 2)],
            ["SSO login failing for member portal", "Users redirected in a loop at SSO login"],
            [("Solution provided", "Re-imported the SAML signing certificate for the member portal app.")], 3.0),
}
CI_SUPPORT = {"enrollment-api": "Enrollment Platform", "eligibility-svc": "Eligibility Engineering",
              "vpn-dallas-01": "Network", "EXCH-SD-05": "Messaging", "claims-db-prod": "Database",
              "payment-gw": "Premium Billing", "PRN-PLANO-3F": "Hardware", "okta-sso": "Identity",
              "notification-svc": "Member Comms", "enrollment-ui": "Enrollment Platform"}
CALLERS = ["Abel Tuter", "Fred Luddy", "Joe Employee", "Bertie Luby", "Margaret Grey", "Jerrod Bennett"]
GROUPS = sorted({t[2] for t in THEMES.values()} | {"Enrollment Platform", "Member Comms", "Hardware"})

# 45 resolved: theme -> count (vpn mostly within the last 30 days -> problem candidate)
RESOLVED_PLAN = [("enroll", 5), ("vpn", 7), ("mail", 7), ("lock", 10), ("db", 5), ("pay", 4), ("print", 4), ("sso", 3)]

# 15 open: number, theme-ish fields, hours ago
OPEN = [
    dict(number="INC0012345", short_description="Enrollment submissions failing with 5xx errors",
         description="Members cannot submit plan enrollment. enrollment-api returns 502/504; ~38% of submissions failing.",
         state="In Progress", p=1, category="Software", caller_id="Abel Tuter", assignment_group="Enrollment Platform",
         assigned_to="Beth Anglin", cmdb_ci="enrollment-api", h=1.5),
    dict(number="INC0012346", short_description="Plan enrollment page returns 504", description="Several members report a 504 on submit.",
         state="New", p=2, category="Software", caller_id="Bertie Luby", assignment_group="Service Desk", assigned_to="",
         cmdb_ci="enrollment-api", h=1.2),
    dict(number="INC0012347", short_description="Members cannot submit enrollment - error 502",
         description="Call center seeing 502 errors when submitting enrollment for members, e.g. transaction TXN-ENR-20261024-0207.",
         state="New", p=3, category="Software", caller_id="Margaret Grey", assignment_group="Service Desk", assigned_to="",
         cmdb_ci="enrollment-api", h=0.8),
    dict(number="INC0012350", short_description="Email confirmation delayed for enrollment",
         description="Confirmation emails arriving 20+ minutes late.", state="New", p=4, category="Software",
         caller_id="Abel Tuter", assignment_group="Member Comms", assigned_to="", cmdb_ci="notification-svc", h=3.0),
    dict(number="INC0012351", short_description="VPN disconnects every 30 minutes - Dallas office",
         description="Dallas staff losing VPN every ~30 minutes since this morning.", state="New", p=2, category="Network",
         caller_id="Jerrod Bennett", assignment_group="Network", assigned_to="", cmdb_ci="vpn-dallas-01", h=2.6),
    dict(number="INC0012352", short_description="Outlook not syncing new email",
         description="New mail not arriving in Outlook desktop; webmail works.", state="New", p=3, category="Software",
         caller_id="Joe Employee", assignment_group="Service Desk", assigned_to="", cmdb_ci="EXCH-SD-05", h=5.0),
    dict(number="INC0012353", short_description="Claims database queries very slow",
         description="Claims search taking 40s+; adjusters blocked.", state="In Progress", p=2, category="Database",
         caller_id="Fred Luddy", assignment_group="Database", assigned_to="Bow Ruggeri", cmdb_ci="claims-db-prod", h=26.0, reassign=1),
    dict(number="INC0012354", short_description="Account locked out after password change",
         description="User locked out after the 90-day password change.", state="In Progress", p=3, category="Inquiry / Help",
         caller_id="Bertie Luby", assignment_group="Service Desk", assigned_to="Luke Wilson", cmdb_ci="", h=46.0),
    dict(number="INC0012355", short_description="3rd floor printer jamming", description="Jams every few pages.",
         state="On Hold", p=3, category="Hardware", caller_id="Margaret Grey", assignment_group="Hardware",
         assigned_to="Charlie Whitherspoon", cmdb_ci="PRN-PLANO-3F", h=150.0),
    dict(number="INC0012356", short_description="Password reset not working for SSO users",
         description="Self-service reset link expires immediately.", state="New", p=4, category="Inquiry / Help",
         caller_id="Joe Employee", assignment_group="Service Desk", assigned_to="", cmdb_ci="okta-sso", h=4.2),
    dict(number="INC0012357", short_description="Premium payment gateway timeouts",
         description="Autopay batch seeing gateway timeouts since 07:00.", state="In Progress", p=2, category="Software",
         caller_id="Jerrod Bennett", assignment_group="Premium Billing", assigned_to="Krystle Stika", cmdb_ci="payment-gw", h=7.0),
    dict(number="INC0012358", short_description="Shared mailbox not updating in Outlook",
         description="Claims shared mailbox stale for some users.", state="In Progress", p=3, category="Software",
         caller_id="Fred Luddy", assignment_group="Messaging", assigned_to="Fred Luddy", cmdb_ci="EXCH-SD-05", h=240.0, reassign=2),
    dict(number="INC0012359", short_description="Printer offline - Plano 3F", description="Shows offline for all users.",
         state="New", p=4, category="Hardware", caller_id="Abel Tuter", assignment_group="Hardware", assigned_to="",
         cmdb_ci="PRN-PLANO-3F", h=0.5),
    dict(number="INC0012360", short_description="Dallas VPN tunnel dropping during calls",
         description="Teams calls drop when VPN reconnects.", state="In Progress", p=3, category="Network",
         caller_id="Bertie Luby", assignment_group="Network", assigned_to="Bud Richman", cmdb_ci="vpn-dallas-01", h=20.0),
    dict(number="INC0012361", short_description="Request access to Grafana dashboard",
         description="Needs viewer access to the claims dashboard.", state="New", p=5, category="Inquiry / Help",
         caller_id="Joe Employee", assignment_group="Service Desk", assigned_to="", cmdb_ci="", h=50.0),
]
JOURNAL_SEED = {
    "INC0012345": [(1.4, "beth.anglin", "work note", "Alert storm grouped. Investigating enrollment-api 5xx."),
                   (1.2, "beth.anglin", "work note", "eligibility-svc logs show plan-catalog-client timeouts after 200ms."),
                   (1.1, "beth.anglin", "comment", "We are aware of enrollment errors and are working on a fix.")],
    "INC0012346": [(0.6, "service.desk", "work note", "Member called again: enrollment rejected, transaction TXN-ENR-20261024-0311.")],
    "INC0012353": [(20, "bow.ruggeri", "work note", "Long-running query on claim_line; checking index fragmentation."),
                   (3, "bow.ruggeri", "work note", "Index rebuild scheduled for 13:00 maintenance window.")],
    "INC0012357": [(5, "krystle.stika", "work note", "Gateway returning TLS handshake errors; cert expiry suspected.")],
    "INC0012360": [(6, "bud.richman", "work note", "Captured tunnel logs; DPD timeouts on vpn-dallas-01.")],
}
CHANGES = {
    "okta-sso": [{"number": "CHG0098890", "short_description": "Rotate SAML signing certificate for SSO",
                  "state": "Implement", "type": "Normal", "assignment_group": "Identity", "risk": "Moderate",
                  "ended_hours_ago": -1.5, "duration_h": 2.0}],
    "enrollment-api": [{"number": "CHG0098871", "short_description": "Reduce plan-catalog client timeout to 200ms",
                        "state": "Closed", "type": "Normal", "assignment_group": "Eligibility Engineering", "risk": "Low",
                        "ended_hours_ago": 1.8}],
    "notification-svc": [{"number": "CHG0098859", "short_description": "Update enrollment confirmation email template",
                          "state": "Closed", "type": "Standard", "assignment_group": "Member Comms", "risk": "Low",
                          "ended_hours_ago": 2.2}],
    "vpn-dallas-01": [{"number": "CHG0098844", "short_description": "Firmware patch on Dallas edge firewall",
                       "state": "Closed", "type": "Standard", "assignment_group": "Network", "risk": "Moderate",
                       "ended_hours_ago": 9}],
}


# business services (transactions) and the CIs they depend on
SERVICES = ["Member Enrollment", "Premium Payments", "Remote Access", "Claims Processing", "Email"]
SERVICE_RELATIONS = [("Member Enrollment", "enrollment-ui", "Depends on::Used by"),
                     ("enrollment-ui", "enrollment-api", "Depends on::Used by"),
                     ("Premium Payments", "payment-gw", "Depends on::Used by"),
                     ("payment-gw", "okta-sso", "Depends on::Used by"),
                     ("Remote Access", "vpn-dallas-01", "Depends on::Used by"),
                     ("Claims Processing", "claims-db-prod", "Depends on::Used by"),
                     ("Email", "EXCH-SD-05", "Depends on::Used by")]
RELATIONS = [("enrollment-api", "eligibility-svc", "Depends on::Used by"),
             ("eligibility-svc", "plan-catalog-svc", "Depends on::Used by"),
             ("enrollment-api", "notification-svc", "Depends on::Used by"),
             ("vpn-dallas-01", "dallas-edge-fw", "Depends on::Used by")]
PROBLEMS = [{"number": "PRB0039001", "short_description": "Dallas VPN tunnel drops when DPD times out",
             "state": "Root Cause Analysis", "known_error": "true", "cmdb_ci": "vpn-dallas-01",
             "cause_notes": "Dead-peer-detection timeout too aggressive after edge firmware updates.",
             "workaround": "Clear IKE SAs and raise DPD timeout to 60s on vpn-dallas-01.", "fix_notes": ""}]
AUDIT_SEED = {"INC0012345": [(1.45, "priority", "2 - High", "1 - Critical", "beth.anglin"),
                             (1.4, "state", "New", "In Progress", "beth.anglin"),
                             (1.38, "assigned_to", "", "changed", "beth.anglin")]}


# upcoming changes for the change guard (hours from now to start, duration h)
UPCOMING = [
    dict(number="CHG0098901", short_description="Restore plan-catalog client timeout to 800ms on enrollment-api",
         description="Reliability fix for INC0012345: revert the 200ms timeout from CHG0098871.", cmdb_ci="enrollment-api",
         type="Emergency", risk="Moderate", state="Authorize", assignment_group="Eligibility Engineering", start_in=1.0, dur=0.5,
         backout_plan="Revert config to previous value.", test_plan="Synthetic enrollment submit in stage and prod."),
    dict(number="CHG0098905", short_description="Upgrade enrollment-api to Spring Boot 3.3 (release 4.20)",
         description="Framework upgrade and dependency bumps.", cmdb_ci="enrollment-api", type="Normal", risk="High",
         state="Scheduled", assignment_group="Enrollment Platform", start_in=20.0, dur=2.0, backout_plan="", test_plan="Regression suite"),
    dict(number="CHG0098910", short_description="Renew TLS certificate on payment-gw", description="Replace the expired certificate.",
         cmdb_ci="payment-gw", type="Standard", risk="Low", state="Scheduled", assignment_group="Premium Billing", start_in=6.0, dur=0.5,
         backout_plan="Re-install previous keystore.", test_plan="TLS handshake check and test charge."),
    dict(number="CHG0098912", short_description="Firmware update on PRN-PLANO-3F", description="Vendor firmware 4.2.1.",
         cmdb_ci="PRN-PLANO-3F", type="Standard", risk="Low", state="Scheduled", assignment_group="Hardware", start_in=30.0, dur=1.0,
         backout_plan="Re-flash 4.1.9.", test_plan="Print test page."),
    dict(number="CHG0098915", short_description="Rebuild claim_line index and refresh statistics on claims-db-prod",
         description="Index maintenance to fix slow claims search.", cmdb_ci="claims-db-prod", type="Normal", risk="Moderate",
         state="Scheduled", assignment_group="Database", start_in=3.0, dur=1.5, backout_plan="Drop new index, restore stats snapshot.", test_plan=""),
]


def _mock_upcoming(now: datetime) -> list[dict]:
    out = []
    for u in UPCOMING:
        st = now + timedelta(hours=u["start_in"])
        en = st + timedelta(hours=u["dur"])
        out.append({k: v for k, v in u.items() if k not in ("start_in", "dur")} |
                   {"start_date": _fmt(st), "end_date": _fmt(en), "start_raw": _fmt(st), "end_raw": _fmt(en), "sys_id": u["number"],
                    "implementation_plan": "See runbook.", "close_code": ""})
    return out


def _mock_change_history(now: datetime, rows: dict, current: list[dict]) -> list[dict]:
    """90 days of past changes per CI: one failed change before every incident resolved by change, plus routine ones."""
    rng = random.Random(11)
    out, seq = [], count(97001)
    for r in rows.values():
        if r.get("close_code") == "Resolved by change" and r.get("cmdb_ci"):
            opened = datetime.strptime(r["opened_at_raw"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            en = opened - timedelta(hours=rng.uniform(0.5, 3))
            out.append({"number": f"CHG{next(seq):07d}", "short_description": rng.choice(["Release deploy", "Config change: client timeouts",
                        "Query plan change", "Dependency upgrade"]) + f" on {r['cmdb_ci']}", "cmdb_ci": r["cmdb_ci"], "type": "Normal",
                        "risk": "Moderate", "state": "Closed", "close_code": "Unsuccessful", "assignment_group": r["assignment_group"],
                        "start_raw": _fmt(en - timedelta(minutes=40)), "end_raw": _fmt(en)})
    for ci, n in [("enrollment-api", 9), ("eligibility-svc", 7), ("payment-gw", 4), ("claims-db-prod", 5), ("vpn-dallas-01", 3),
                  ("okta-sso", 4), ("notification-svc", 6), ("PRN-PLANO-3F", 2), ("EXCH-SD-05", 3)]:
        for _ in range(n):
            en = now - timedelta(days=rng.uniform(2, 89))
            out.append({"number": f"CHG{next(seq):07d}", "short_description": rng.choice(["Release deploy", "Patch", "Config change",
                        "Certificate renewal", "Scale-out"]) + f" on {ci}", "cmdb_ci": ci, "type": rng.choice(["Normal", "Standard"]),
                        "risk": "Low", "state": "Closed", "close_code": "Successful", "assignment_group": CI_SUPPORT.get(ci, ""),
                        "start_raw": _fmt(en - timedelta(minutes=30)), "end_raw": _fmt(en)})
    out += [dict(c, close_code=c.get("close_code") or ("" if c.get("state") != "Closed" else "Successful")) for c in current]
    return sorted(out, key=lambda c: c["end_raw"], reverse=True)


def _mock_alerts(now: datetime) -> list[dict]:
    """MOCK Event Management stream for the last 24h: steady noise plus a 5xx burst around the enrollment outage."""
    rng = random.Random(7)
    out, seq = [], count(70001)

    def add(hours_ago, node, metric, sev, desc, key, ci=""):
        out.append({"number": f"Alert{next(seq)}", "source": "Prometheus" if "svc" in node or "api" in node else "SolarWinds",
                    "node": node, "type": metric, "metric_name": metric, "severity": sev, "state": "Open",
                    "description": desc, "cmdb_ci": ci or node, "message_key": key,
                    "sys_created_on_raw": _fmt(now - timedelta(hours=hours_ago)), "sys_created_on": _fmt(now - timedelta(hours=hours_ago))})
    for i in range(96):  # flapping disk + CPU noise every 15 minutes
        add(24 - i * 0.25, "PRN-PLANO-3F", "Printer status", "4 - Warning", "Printer offline/online flap", "prn-flap")
        if i % 2 == 0:
            add(24 - i * 0.25 - 0.05, "claims-db-prod", "Disk usage", "3 - Minor", "Disk /data above 85%", "claims-disk")
    for i in range(70):  # enrollment 5xx storm: starts ~1.7h ago, peaks after the 200ms timeout change
        h = 1.7 - i * 0.02
        add(h, "enrollment-api", "HTTP 5xx rate", "1 - Critical" if i > 10 else "2 - Major",
            f"5xx rate {rng.uniform(4, 9):.1f}% (threshold 2%)", "enroll-5xx")
        if i % 3 == 0:
            add(h - 0.01, "eligibility-svc", "Upstream timeout", "2 - Major", "plan-catalog-client timeout after 200ms", "elig-timeout")
    for i in range(18):
        add(2.6 - i * 0.12, "vpn-dallas-01", "Tunnel down", "2 - Major", "IPsec tunnel to Dallas DC down (DPD timeout)", "vpn-dpd")
    for i in range(6):
        add(rng.uniform(0, 24), rng.choice(["okta-sso", "payment-gw", "EXCH-SD-05"]), "Heartbeat", "4 - Warning", "Missed heartbeat", f"hb-{i}")
    return sorted(out, key=lambda a: a["sys_created_on_raw"])


def _mock_changes(now: datetime) -> list[dict]:
    out = []
    for ci, items in CHANGES.items():
        for c in items:
            end = now - timedelta(hours=c["ended_hours_ago"])
            start = end - timedelta(hours=c.get("duration_h", 1 / 6))
            out.append({k: v for k, v in c.items() if k not in ("ended_hours_ago", "duration_h")} |
                       {"cmdb_ci": ci, "start_date": _fmt(start), "end_date": _fmt(end),
                        "start_raw": _fmt(start), "end_raw": _fmt(end)})
    return out


def _build(now: datetime) -> tuple[dict, dict]:
    rng = random.Random(42)
    rows: dict[str, dict] = {}
    seq = count(11901)
    for theme, n in RESOLVED_PLAN:
        cat, ci, grp, resolvers, descs, fixes, typ_h = THEMES[theme]
        pool = [r for r, w in resolvers for _ in range(w)]
        for i in range(n):
            days_ago = rng.uniform(2, 28) if theme == "vpn" else rng.uniform(3, 90)
            opened = now - timedelta(days=days_ago, hours=rng.uniform(0, 8))
            dur = max(0.3, rng.gauss(typ_h, typ_h / 3))
            p = rng.choice([2, 3, 3, 4]) if theme not in ("enroll", "pay") else rng.choice([1, 2, 2])
            imp, urg = _IU[p]
            code, fix = fixes[i % len(fixes)]
            who = pool[i % len(pool)]
            num = f"INC{next(seq):07d}"
            rows[num] = {"number": num, "short_description": descs[i % len(descs)], "description": "",
                         "state": "Closed" if days_ago > 10 else "Resolved", "priority": _PRIORITY[p],
                         "impact": _LEVEL[imp], "urgency": _LEVEL[urg], "category": cat, "cmdb_ci": ci,
                         "assignment_group": grp, "assigned_to": who, "resolved_by": who,
                         "caller_id": rng.choice(CALLERS), "close_code": code, "close_notes": fix,
                         "opened_at_raw": _fmt(opened), "resolved_at_raw": _fmt(opened + timedelta(hours=dur)),
                         "reassignment_count": str(rng.choice([0, 0, 0, 1, 2])), "problem_id": "", "parent_incident": ""}
    # keep the original resolved enrollment incident number used in docs/tests (replaces one generated row)
    first = next(k for k, v in rows.items() if v["cmdb_ci"] == "enrollment-api")
    r = rows.pop(first)
    r.update(number="INC0012311", short_description="Eligibility check timeouts after deploy",
             close_code="Resolved by change", close_notes="Rolled back release 4.18; timeout restored to 800ms.")
    rows["INC0012311"] = r
    for o in OPEN:
        imp, urg = _IU[o["p"]]
        opened = now - timedelta(hours=o["h"])
        rows[o["number"]] = {"number": o["number"], "short_description": o["short_description"],
                             "description": o["description"], "state": o["state"], "priority": _PRIORITY[o["p"]],
                             "impact": _LEVEL[imp], "urgency": _LEVEL[urg], "category": o["category"],
                             "cmdb_ci": o["cmdb_ci"], "assignment_group": o["assignment_group"],
                             "assigned_to": o["assigned_to"], "resolved_by": "", "caller_id": o["caller_id"],
                             "close_code": "", "close_notes": "", "opened_at_raw": _fmt(opened), "resolved_at_raw": "",
                             "reassignment_count": str(o.get("reassign", 0)), "problem_id": "", "parent_incident": ""}
    journal = {n: [{"type": t, "at": _fmt(now - timedelta(hours=h)), "by": by, "text": txt} for h, by, t, txt in items]
               for n, items in JOURNAL_SEED.items()}
    for i, (n, row) in enumerate(rows.items()):
        row["sys_id"] = f"sys{n}"
        row["opened_at"] = row["opened_at_raw"]
        row["resolved_at"] = row["resolved_at_raw"]
        upd = journal[n][-1]["at"] if n in journal else (row["resolved_at_raw"] or row["opened_at_raw"])
        row["sys_updated_on"] = row["sys_updated_on_raw"] = upd
        row["cmdb_ci_raw"] = row["cmdb_ci"]
    return rows, journal


class FakeServiceNowClient:
    """data=(rows, journal, changes, ci_support) replaces the generated history (used by snapshot mode)."""

    def __init__(self, now: datetime | None = None, data: tuple | None = None):
        self.now = now or datetime.now(UTC)
        if data:
            self.rows, self.journal, self.changes, self.ci_support = data
            self.relations, self.service_relations, self.problem_seed, self.audit_seed = [], [], [], {}
        else:
            self.rows, self.journal = _build(self.now)
            self.changes, self.ci_support = _mock_changes(self.now), dict(CI_SUPPORT)
        self._seq = count(12400)
        self._prb = count(40001)
        self.problems: dict[str, dict] = {}

    # ---------------------------------------------------------------- basics
    def _get(self, number: str) -> dict:
        n = number.strip().upper()
        if n not in self.rows:
            raise ServiceNowError(f"Incident {number} not found.")
        return self.rows[n]

    @staticmethod
    def _public(r: dict) -> dict:
        return {k: v for k, v in r.items() if not k.endswith("_raw")}

    def _open(self):
        return [r for r in self.rows.values() if r["state"] not in ("Resolved", "Closed", "Canceled")]

    def get_incident(self, number):
        return self._public(self._get(number))

    def search_incidents(self, text="", state="", priority="", assignment_group="", active_only=True,
                         encoded_query="", limit=10):
        out = []
        for r in self.rows.values():
            if active_only and r["state"] in ("Resolved", "Closed", "Canceled"):
                continue
            if text and text.lower() not in (r["short_description"] + r.get("description", "")).lower():
                continue
            if state and r["state"].lower() != state.lower():
                continue
            if priority:
                p = int(r["priority"][0])
                lim = int(priority.strip("<>= "))
                if (priority.startswith("<=") and p > lim) or (priority.isdigit() and p != lim):
                    continue
            if assignment_group and assignment_group.lower() not in r["assignment_group"].lower():
                continue
            out.append({k: r.get(k, "") for k in ("number", "short_description", "state", "priority",
                                                  "assignment_group", "assigned_to", "cmdb_ci", "opened_at")})
        out.sort(key=lambda r: r["opened_at"], reverse=True)
        return out[:limit]

    def get_journal(self, number, limit=20):
        self._get(number)
        return self.journal.get(number.upper(), [])[:limit]

    def create_incident(self, fields):
        if not fields.get("short_description"):
            raise ServiceNowError("short_description is required.")
        impact, urgency = int(str(fields.get("impact", 3))[0]), int(str(fields.get("urgency", 3))[0])
        n = f"INC{next(self._seq):07d}"
        now = _now()
        row = {"sys_id": f"sys{n}", "number": n, "state": "New", "opened_at": now, "opened_at_raw": now,
               "sys_updated_on": now, "sys_updated_on_raw": now, "resolved_at": "", "resolved_at_raw": "",
               "impact": _LEVEL[impact], "urgency": _LEVEL[urgency], "priority": _prio(impact, urgency),
               "assigned_to": "", "resolved_by": "", "caller_id": "System Administrator", "category": "Inquiry / Help",
               "close_code": "", "close_notes": "", "reassignment_count": "0", "problem_id": "", "parent_incident": "",
               "cmdb_ci": "", "assignment_group": "", "description": ""}
        row.update({k: v for k, v in fields.items() if k not in ("impact", "urgency") and v not in (None, "")})
        row["cmdb_ci_raw"] = row["cmdb_ci"]
        self.rows[n] = row
        return self._public(row)

    def update_incident(self, number, fields):
        r = self._get(number)
        body = {k: v for k, v in fields.items() if v not in (None, "")}
        if not body:
            raise ServiceNowError("Nothing to update.")
        for k in ("work_notes", "comments"):
            if k in body:
                self.journal.setdefault(r["number"], []).append(
                    {"type": "work note" if k == "work_notes" else "comment", "at": _now(), "by": "admin", "text": body.pop(k)})
        if "state" in body:
            s = str(body["state"])
            body["state"] = _LABELS.get(int(s), s) if s.isdigit() else s.title()
            if body["state"] == "Resolved":
                if not (body.get("close_code") or r.get("close_code")) or not (body.get("close_notes") or r.get("close_notes")):
                    raise ServiceNowError("Resolution code and resolution notes are mandatory to resolve.")
                body["resolved_at"] = body["resolved_at_raw"] = _now()
                body["resolved_by"] = r.get("assigned_to") or "System Administrator"
        if "assigned_to" in body and r.get("assigned_to") and body["assigned_to"] != r["assigned_to"]:
            body["reassignment_count"] = str(int(r.get("reassignment_count") or 0) + 1)
        r.update(body)
        r["sys_updated_on"] = r["sys_updated_on_raw"] = _now()
        return self._public(r)

    def add_note(self, number, text, customer_visible=False):
        return self.update_incident(number, {"comments" if customer_visible else "work_notes": text})

    def resolve_incident(self, number, close_code, close_notes):
        return self.update_incident(number, {"state": "6", "close_code": close_code, "close_notes": close_notes})

    def find_similar(self, text, exclude_number="", limit=5):
        from .insights import words
        ws = words(text)
        scored = []
        for r in self.rows.values():
            if r["number"] == exclude_number.upper():
                continue
            overlap = len(ws & words(r["short_description"]))
            if overlap:
                scored.append((overlap, r))
        scored.sort(key=lambda x: (-x[0], x[1]["number"]))
        return [{k: r.get(k, "") for k in ("number", "short_description", "state", "priority", "close_code",
                                           "close_notes", "resolved_at")} for _, r in scored[:limit]]

    def recent_changes(self, number, hours=48):
        inc = self._get(number)
        ci, opened = inc.get("cmdb_ci", ""), inc.get("opened_at_raw")
        if not ci or not opened:
            return []
        t = datetime.strptime(opened, "%Y-%m-%d %H:%M:%S")
        out = []
        for c in self.changes:
            if c.get("cmdb_ci") != ci or not c.get("end_raw"):
                continue
            e = datetime.strptime(c["end_raw"], "%Y-%m-%d %H:%M:%S")
            if timedelta(0) <= t - e <= timedelta(hours=hours):
                out.append({k: v for k, v in c.items() if k != "end_raw"})
        return sorted(out, key=lambda c: c.get("end_date", ""), reverse=True)

    def find_groups(self, name="", limit=10):
        groups = sorted({r["assignment_group"] for r in self.rows.values() if r.get("assignment_group")} | set(GROUPS if not hasattr(self, "_snapshot") else []))
        return [{"name": g, "description": "", "manager": ""} for g in groups if name.lower() in g.lower()][:limit]

    def whoami(self):
        return {"user_name": "admin", "name": "System Administrator (mock)", "email": ""}

    # ---------------------------------------------------------------- insight queries (same as live)
    def incident_row(self, number):
        return dict(self._get(number))

    def open_incidents(self, group="", limit=200):
        return [dict(r) for r in self._open() if not group or group.lower() in r["assignment_group"].lower()][:limit]

    def _within(self, raw, hours):
        if not raw:
            return False
        t = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        return (datetime.now(UTC) - t).total_seconds() <= hours * 3600

    def incidents_window(self, kind, hours, group="", limit=100):
        rows = self.rows.values()
        if kind == "opened":
            sel = [r for r in rows if self._within(r["opened_at_raw"], hours)]
        elif kind == "resolved":
            sel = [r for r in rows if self._within(r["resolved_at_raw"], hours)]
        else:
            sel = [r for r in self._open() if self._within(r["sys_updated_on_raw"], hours)]
        return [dict(r) for r in sel if not group or group.lower() in r["assignment_group"].lower()][:limit]

    def resolved_days(self, days, group="", limit=500):
        return self.incidents_window("resolved", days * 24, group, limit)

    def opened_days(self, days, limit=500):
        return self.incidents_window("opened", days * 24, "", limit)

    def sla_records(self, group="", limit=50):
        return []  # mock has no task_sla table: insights estimate SLA from priority targets

    def history_for(self, target, limit=150):
        return [dict(r) for r in self.rows.values()
                if r["state"] in ("Resolved", "Closed") and r["number"] != target.get("number")][:limit]

    def ci_support_group(self, ci):
        return self.ci_support.get(ci or "")

    # ---------------------------------------------------------------- RCA evidence
    def _relations(self, with_services=False):
        rel = list(getattr(self, "relations", RELATIONS))
        return rel + (list(getattr(self, "service_relations", SERVICE_RELATIONS)) if with_services else [])

    def related_cis(self, ci, with_services=False):
        out = []
        for p, c, t in self._relations(with_services):
            a, b = t.split("::")
            if p == ci:
                out.append({"name": c, "sys_id": c, "relation": a.lower(), "dir": "down"})
            elif c == ci:
                out.append({"name": p, "sys_id": p, "relation": b.lower(), "dir": "up"})
        return out

    # ---------------------------------------------------------------- anomaly + transaction data
    def latest_opened(self):
        return max((r["opened_at_raw"] for r in self.rows.values()), default=None)

    def opened_between(self, start_raw, end_raw, limit=2000):
        sel = [dict(r) for r in self.rows.values() if start_raw <= r["opened_at_raw"] <= end_raw]
        return sorted(sel, key=lambda r: r["opened_at_raw"])[:limit]

    def alerts(self, hours=24, limit=2000):
        if getattr(self, "relations", None) == []:  # snapshot mode: no Event Management export
            raise ServiceNowError("em_alert is not in the snapshot.")
        if not hasattr(self, "_alerts"):
            self._alerts = _mock_alerts(self.now)
        cut = _fmt(self.now - timedelta(hours=hours))
        return [dict(a) for a in self._alerts if a["sys_created_on_raw"] >= cut][:limit]

    def business_services(self, limit=200):
        if getattr(self, "relations", None) == []:
            return []
        return [{"name": n, "sys_id": n} for n in SERVICES][:limit]

    def ci_lookup(self, name):
        n = name.strip().lower()
        known = {x for p, c, _ in self._relations(True) for x in (p, c)} | set(self.ci_support) | {r["cmdb_ci"] for r in self.rows.values()}
        hit = next((k for k in known if k and k.lower() == n), None)
        return {"name": hit, "sys_id": hit} if hit else None

    def incidents_on_ci(self, ci, active_only=True, limit=20):
        sel = [dict(r) for r in self.rows.values() if ci and r.get("cmdb_ci") == ci
               and (not active_only or r["state"] not in ("Resolved", "Closed", "Canceled"))]
        return sorted(sel, key=lambda r: (r["priority"], r["opened_at_raw"]))[:limit]

    def mentions(self, text, limit=20):
        t = text.strip().lower()
        inc = [dict(r) for r in self.rows.values()
               if t in (r["short_description"] + " " + r.get("description", "") + " " + r.get("close_notes", "")).lower()]
        notes = [{"number": n, **j} for n, js in self.journal.items() for j in js if t in j["text"].lower()]
        return inc[:limit], notes[:limit]

    def changes_near(self, cis, limit=100):
        return [dict(c) for c in self.changes if c.get("cmdb_ci") in set(cis)][:limit]

    # ---------------------------------------------------------------- changes (change guard, SLO scorecard)
    def _chg_all(self):
        if not hasattr(self, "_upcoming"):
            snap = getattr(self, "relations", None) == []
            self._upcoming = [] if snap else _mock_upcoming(self.now)
            self._history = list(self.changes) if snap else _mock_change_history(self.now, self.rows, self.changes)
        return self._upcoming, self._history

    def change_row(self, number):
        up, hist = self._chg_all()
        for c in up + hist + list(self.changes):
            if c["number"] == number.strip().upper():
                return dict(c)
        raise ServiceNowError(f"Change {number} not found.")

    def upcoming_changes(self, days=7, limit=50):
        up, _ = self._chg_all()
        cut = _fmt(self.now + timedelta(days=days))
        return [dict(c) for c in up if c["start_raw"] <= cut][:limit]

    def active_changes(self, limit=50):
        up, _ = self._chg_all()
        return [dict(c) for c in up + [x for x in self.changes if x.get("state") not in ("Closed", "Canceled")]][:limit]

    def change_history(self, ci, days=90, limit=200):
        _, hist = self._chg_all()
        cut = _fmt(self.now - timedelta(days=days))
        return [dict(c) for c in hist if c.get("cmdb_ci") == ci and (c.get("end_raw") or "") >= cut][:limit]

    def add_change_note(self, number, text):
        self.change_row(number)
        self.__dict__.setdefault("change_notes", {}).setdefault(number.upper(), []).append(text)
        return {"number": number.upper()}

    def create_event(self, fields):
        self.__dict__.setdefault("events", []).append(dict(fields))
        return {"sys_id": f"evt{len(self.events)}", "message_key": fields.get("message_key"), "table": "em_event"}

    def audit_history(self, row):
        out = []
        for h, f, old, new, by in getattr(self, "audit_seed", AUDIT_SEED).get(row["number"], []):
            out.append({"field": f, "old": old, "new": new, "by": by, "at_raw": _fmt(self.now - timedelta(hours=h))})
        return out

    def concurrent_on(self, cis, exclude, limit=50):
        cis = set(cis)
        return [dict(r) for r in self.rows.values() if r.get("cmdb_ci") in cis and r["number"] != exclude.upper()][:limit]

    def problems_for(self, target, limit=20):
        from .insights import words
        tw = words(target.get("short_description", "") + " " + target.get("description", ""))
        out = []
        for p in list(getattr(self, "problem_seed", PROBLEMS)) + list(self.problems.values()):
            if p.get("cmdb_ci") and p.get("cmdb_ci") == target.get("cmdb_ci") or tw & words(p.get("short_description", "")):
                out.append(dict(p))
        return out[:limit]

    def add_attachment(self, number, file_name, content, content_type="text/markdown"):
        self._get(number)
        self.__dict__.setdefault("attachments", {}).setdefault(number.upper(), []).append(
            {"file_name": file_name, "size": len(content), "content": content})
        return {"file_name": file_name, "sys_id": f"att{len(self.attachments[number.upper()])}"}

    def update_problem(self, number, fields):
        for p in list(getattr(self, "problem_seed", PROBLEMS)) + list(self.problems.values()):
            if p["number"] == number.upper():
                p.update({k: v for k, v in fields.items() if v})
                return dict(p)
        raise ServiceNowError(f"Problem {number} not found.")

    def set_parent(self, child, parent):
        self._get(parent)
        return self.update_incident(child, {"parent_incident": parent.strip().upper()})

    def create_problem(self, fields, incident_numbers):
        n = f"PRB{next(self._prb):07d}"
        prb = {"number": n, "state": "New", **{k: v for k, v in fields.items() if v}}
        self.problems[n] = prb
        linked = []
        for num in incident_numbers:
            if num.upper() in self.rows:
                self.rows[num.upper()]["problem_id"] = n
                linked.append(num.upper())
        return {**prb, "linked_incidents": linked}
