"""Show exactly why ServiceNow rejects the API login. Prints no secrets."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import requests  # noqa: E402

from incident_agent.config import settings  # noqa: E402

base = settings.sn_instance.rstrip("/")
pw = settings.sn_password
print(f"Instance: {base}")
print(f"User: {settings.sn_user!r}   password length: {len(pw)}   "
      f"leading/trailing space: {pw != pw.strip()}   starts/ends with quote: {pw[:1] in chr(34)+chr(39) or pw[-1:] in chr(34)+chr(39)}")

def show(label, r):
    print(f"\n--- {label}: HTTP {r.status_code}")
    for h in ("WWW-Authenticate", "X-Is-Logged-In", "Content-Type"):
        if h in r.headers:
            print(f"{h}: {r.headers[h]}")
    print("Body:", r.text[:600].replace(pw, "***") if pw else r.text[:600])

s = requests.Session()
s.headers["Accept"] = "application/json"
r = s.get(f"{base}/api/now/table/sys_user", params={"sysparm_limit": 1, "sysparm_fields": "user_name"},
          auth=(settings.sn_user, pw), timeout=30)
show("Basic auth, Table API", r)

r2 = s.get(f"{base}/api/now/table/sys_user", params={"sysparm_limit": 1}, timeout=30)
show("No auth (for comparison)", r2)

r3 = s.get(f"{base}/stats.do", timeout=30)
print(f"\n--- Instance awake check (stats.do): HTTP {r3.status_code}")
