"""Step 1: check the ServiceNow connection. No LLM or API key needed.

  python scripts/check_servicenow.py
  python scripts/check_servicenow.py INCxxxxxxx
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from incident_agent.config import settings  # noqa: E402
from incident_agent.servicenow import ServiceNowError, get_client  # noqa: E402


def main() -> int:
    print(f"Mode: {settings.sn_mode}   Instance: {settings.sn_instance or '-'}   User: {settings.sn_user or '-'}")
    try:
        sn = get_client()
        me = sn.whoami()
        print(f"OK Connected as {me.get('name') or me.get('user_name') or settings.sn_user}")
        rows = sn.search_incidents(active_only=True, limit=5)
        print(f"OK {len(rows)} recent active incidents:")
        for r in rows:
            print(f"   {r['number']}  P{str(r.get('priority', '?'))[:1]}  {r.get('state', ''):<12} {r.get('short_description', '')[:70]}")
        number = sys.argv[1] if len(sys.argv) > 1 else (rows[0]["number"] if rows else None)
        if number:
            inc = sn.get_incident(number)
            print(f"OK Read {inc['number']}: {inc.get('short_description')} "
                  f"(group: {inc.get('assignment_group') or '-'}, CI: {inc.get('cmdb_ci') or '-'})")
            notes = sn.get_journal(number)
            print(f"OK {len(notes)} work notes / comments")
        print("\nServiceNow is reachable. Next: python -m incident_agent.cli")
        return 0
    except ServiceNowError as e:
        print(f"X {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
