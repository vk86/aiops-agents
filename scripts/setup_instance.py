"""One-shot setup for a NEW ServiceNow PDI: service user + .env + demo data.

  python scripts/setup_instance.py                      prompts for instance + admin password
  python scripts/setup_instance.py --instance dev282967 --no-seed

Steps (admin credentials are used only for this run and are never written to disk):
  1. checks the instance is awake and the admin login works over the REST API
  2. creates (or resets) user svc_incident_agent with a generated password, roles itil (+ REST role if present)
  3. updates .env: instance URL, username, password (backup in .env.bak)
  4. seeds the demo RCA story (scripts/seed_rca_story.py) as admin
  5. checks the agent can read incidents as svc_incident_agent
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import string
import subprocess
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
SVC_USER = "svc_incident_agent"
ROLES = ["itil", "snc_platform_rest_api_access"]  # 2nd only exists on newer releases; skipped if absent
CI_CANDIDATES = ["EXCH-SD-05", "EXCH-SD-02", "EXCH-SD-01"]


def say(ok: bool, msg: str) -> None:
    print(("OK " if ok else "X  ") + msg)


def gen_password(n: int = 20) -> str:
    # letters, digits and a few symbols that are safe in .env and URLs
    alphabet = string.ascii_letters + string.digits + "-_.@#"
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(n))
        if any(c.isupper() for c in p) and any(c.islower() for c in p) and any(c.isdigit() for c in p) \
                and any(c in "-_.@#" for c in p):
            return p


class SN:
    def __init__(self, base: str, user: str, pw: str):
        self.base = base
        self.s = requests.Session()
        self.s.auth = (user, pw)
        self.s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    def req(self, method: str, path: str, **kw):
        r = self.s.request(method, f"{self.base}/api/now/{path}", timeout=60, **kw)
        if "json" not in r.headers.get("Content-Type", ""):
            raise RuntimeError(f"HTTP {r.status_code}, non-JSON page (instance asleep or login page). "
                               "Open the instance in a browser to wake it, then retry.")
        body = r.json()
        if r.status_code >= 400:
            err = body.get("error") or {}
            raise RuntimeError(f"HTTP {r.status_code}: {err.get('message')} {err.get('detail') or ''}".strip())
        return body.get("result")

    def first(self, table: str, query: str, fields: str = "sys_id"):
        rows = self.req("GET", f"table/{table}", params={"sysparm_query": query, "sysparm_limit": 1, "sysparm_fields": fields})
        return rows[0] if rows else None


def update_env(base: str, pw: str) -> None:
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else \
        (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    if ENV.exists():
        (ROOT / ".env.bak").write_text("\n".join(lines) + "\n", encoding="utf-8")
    want = {"SERVICENOW_MODE": "live", "SERVICENOW_INSTANCE_URL": base, "SERVICENOW_USERNAME": SVC_USER,
            "SERVICENOW_PASSWORD": pw, "SERVICENOW_AUTH": "basic"}
    seen = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in want and not line.lstrip().startswith("#"):
            out.append(f"{key}={want[key]}")
            seen.add(key)
        else:
            out.append(line)
    for k, v in want.items():
        if k not in seen:
            out.append(f"{k}={v}")
    ENV.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", help="dev282967 or https://dev282967.service-now.com")
    ap.add_argument("--admin-user", default="admin")
    ap.add_argument("--no-seed", action="store_true", help="skip the demo RCA story")
    a = ap.parse_args()

    inst = a.instance or input("Instance (e.g. dev282967): ").strip()
    base = inst.rstrip("/") if inst.startswith("http") else f"https://{inst}.service-now.com"
    admin_pw = os.getenv("SN_ADMIN_PASSWORD") or getpass.getpass(f"Password for {a.admin_user} (not shown, not saved): ")
    print(f"\nInstance: {base}\n")

    # 1. admin login
    sn = SN(base, a.admin_user, admin_pw)
    try:
        sn.first("sys_user", f"user_name={a.admin_user}", "user_name")
        say(True, f"Logged in as {a.admin_user}")
    except Exception as e:
        say(False, f"Admin login failed: {e}")
        print("   Check the password on developer.servicenow.com (Manage my instance) and that the instance is Online.")
        return 1

    # 2. service user
    svc_pw = gen_password()
    try:
        u = sn.first("sys_user", f"user_name={SVC_USER}", "sys_id")
        body = {"user_name": SVC_USER, "first_name": "Incident", "last_name": "Agent", "email": "svc_incident_agent@example.com",
                "active": "true", "locked_out": "false", "password_needs_reset": "false", "user_password": svc_pw}
        p = {"sysparm_input_display_value": "true"}  # display-value input = plain text password gets hashed
        if u:
            sn.req("PATCH", f"table/sys_user/{u['sys_id']}", params=p, json=body)
            uid = u["sys_id"]
            say(True, f"User {SVC_USER} existed: password reset, unlocked")
        else:
            uid = sn.req("POST", "table/sys_user", params=p, json=body)["sys_id"]
            say(True, f"Created user {SVC_USER}")
        for role in ROLES:
            r = sn.first("sys_user_role", f"name={role}", "sys_id")
            if not r:
                print(f"   (role {role} not on this instance, skipped)")
                continue
            if sn.first("sys_user_has_role", f"user={uid}^role={r['sys_id']}", "sys_id"):
                say(True, f"Role {role} already granted")
            else:
                sn.req("POST", "table/sys_user_has_role", json={"user": uid, "role": r["sys_id"]})
                say(True, f"Granted role {role}")
    except Exception as e:
        say(False, f"Could not set up {SVC_USER}: {e}")
        return 1

    # 3. .env
    update_env(base, svc_pw)
    say(True, f".env updated (instance, user {SVC_USER}, new password). Old file saved as .env.bak")

    # 4. demo data, as admin (admin can set opened_at / resolved_at to back-date records)
    env = {**os.environ, "SERVICENOW_MODE": "live", "SERVICENOW_INSTANCE_URL": base, "SERVICENOW_USERNAME": a.admin_user,
           "SERVICENOW_PASSWORD": admin_pw, "SERVICENOW_AUTH": "basic", "PYTHONUTF8": "1"}
    if not a.no_seed:
        if sn.first("incident", "descriptionLIKE[demo-rca]", "number"):
            print("   Demo RCA records already exist on this instance; skipping the seed.")
        else:
            ci = next((c for c in CI_CANDIDATES if sn.first("cmdb_ci", f"name={c}", "sys_id")), None)
            if not ci:
                say(False, "No Exchange demo CI found (EXCH-SD-05). Run: python scripts\\seed_rca_story.py --ci \"<CI name>\"")
            else:
                print(f"\nSeeding demo RCA story on CI {ci} ...")
                rc = subprocess.call([sys.executable, str(ROOT / "scripts" / "seed_rca_story.py"), "--ci", ci, "--yes"],
                                     cwd=ROOT, env=env)
                say(rc == 0, "Demo data seeded" if rc == 0 else "Seed stopped; see the lines above")

    # 5. check as the service user
    print("\nChecking the agent login ...")
    svc = SN(base, SVC_USER, svc_pw)
    try:
        rows = svc.req("GET", "table/incident", params={"sysparm_query": "active=true^ORDERBYDESCopened_at",
                                                         "sysparm_limit": 5, "sysparm_fields": "number,short_description"})
        say(True, f"{SVC_USER} can read incidents ({len(rows)} shown):")
        for r in rows:
            print(f"   {r['number']}  {r['short_description'][:70]}")
    except Exception as e:
        say(False, f"{SVC_USER} login check failed: {e}")
        print("   New users can take a minute to work; run check_servicenow later. If it keeps failing, "
              "the instance may block Basic Auth (see README: SERVICENOW_AUTH=oauth).")
        return 1

    print("\nDone. Start the UI with start-live.bat and open the [demo-rca] P2 incident -> Run RCA.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
