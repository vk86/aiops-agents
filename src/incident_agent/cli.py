"""Interactive chat with the incident agent.

  python -m incident_agent.cli
  python -m incident_agent.cli "summarize INCxxxxxxx"     (one-shot)

Commands: /new (fresh conversation)  /tools  /exit
"""
from __future__ import annotations

import sys
import uuid

from .agent import ask, build_agent
from .config import settings
from .tools import ALL_TOOLS, WRITE_TOOLS

EXAMPLES = [
    "Show my open P1 and P2 incidents",
    "Summarize INCxxxxxxx",
    "What changed recently on the CI for INCxxxxxxx? Any similar past incidents?",
    "Create an incident: VPN down for the Dallas office, high impact, high urgency, assign to Network",
    "Add a work note to INCxxxxxxx: rollback started at 14:05",
    "Resolve INCxxxxxxx, solution provided, restarted the VPN concentrator",
]


def _banner() -> None:
    target = "MOCK data (offline)" if settings.sn_mode == "mock" else settings.sn_instance or "(SERVICENOW_INSTANCE_URL not set)"
    print("\nServiceNow incident agent")
    print(f"  ServiceNow: {target}")
    print(f"  Model:      {settings.llm_model}")
    print(f"  Approvals:  {'on, you confirm every write' if settings.require_approval else 'OFF'}")
    print("\nTry:")
    for e in EXAMPLES:
        print(f"  • {e}")
    print("\n/new  /tools  /exit\n")


def _show(result: dict) -> None:
    for c in result["tool_calls"]:
        args = ", ".join(f"{k}={v!r}" for k, v in c["args"].items())
        print(f"  ↳ {c['tool']}({args})")
    print(f"\n{result['reply']}\n")


def main() -> None:
    agent = build_agent()
    thread = uuid.uuid4().hex[:8]

    if len(sys.argv) > 1:
        _show(ask(agent, " ".join(sys.argv[1:]), thread))
        return

    _banner()
    while True:
        try:
            msg = input("you › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not msg:
            continue
        if msg in ("/exit", "/quit"):
            break
        if msg == "/new":
            thread = uuid.uuid4().hex[:8]
            print("  New conversation.\n")
            continue
        if msg == "/tools":
            for t in ALL_TOOLS:
                tag = " (asks approval)" if t in WRITE_TOOLS else ""
                print(f"  {t.name}{tag}: {t.description.splitlines()[0]}")
            print()
            continue
        try:
            _show(ask(agent, msg, thread))
        except Exception as e:  # keep the session alive on model/network errors
            print(f"  Error: {e}\n")


if __name__ == "__main__":
    main()
