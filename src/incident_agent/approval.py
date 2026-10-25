"""Human approval for write actions (create, update, note, resolve).

Every write tool goes through `gated(action, details, do)`. The current approver decides:
  True  -> run the write now
  False -> declined, nothing written
  None  -> park it as a pending action; a UI shows Approve / Reject and calls resolve_pending()

The CLI asks y/n in the terminal. The web UI parks writes as pending. Tests swap in their own.
REQUIRE_APPROVAL=false skips all of this (only for a throwaway sandbox instance).
"""
from __future__ import annotations

import json
import threading
import uuid
from typing import Callable, Optional

from .config import settings

Approver = Callable[[str, dict], Optional[bool]]

DECLINED = "The user declined this change. Nothing was written to ServiceNow."
PENDING = ("Waiting for the user to approve this change in the UI (approval id {id}). Nothing has been written yet. "
           "Tell the user in one sentence what will happen when they approve. Do not call this tool again for it.")


def _terminal(action: str, details: dict) -> bool:
    print(f"\n  ┌ Approval needed: {action}")
    for k, v in details.items():
        if v not in (None, ""):
            print(f"  │ {k}: {v if isinstance(v, str) else json.dumps(v)}")
    ans = input("  └ Proceed? [y/N] ").strip().lower()
    return ans in ("y", "yes")


_approver: Approver = _terminal
_pending: dict[str, dict] = {}
_lock = threading.Lock()


def set_approver(fn: Approver) -> None:
    global _approver
    _approver = fn


def approve(action: str, details: dict) -> Optional[bool]:
    if not settings.require_approval:
        return True
    return _approver(action, details)


def gated(action: str, details: dict, do: Callable[[], str]) -> str:
    decision = approve(action, details)
    if decision is True:
        return do()
    if decision is None:
        pid = uuid.uuid4().hex[:8]
        with _lock:
            _pending[pid] = {"id": pid, "action": action, "details": {k: v for k, v in details.items() if v not in (None, "")},
                             "do": do, "consumed": False}
        return PENDING.format(id=pid)
    return DECLINED


def take_new_pending() -> list[dict]:
    """Pending actions created since the last call (for showing approval cards)."""
    with _lock:
        out = []
        for p in _pending.values():
            if not p["consumed"]:
                p["consumed"] = True
                out.append({"id": p["id"], "action": p["action"], "details": p["details"]})
        return out


def resolve_pending(pid: str, approved: bool) -> dict:
    with _lock:
        p = _pending.pop(pid, None)
    if p is None:
        return {"ok": False, "result": "This approval has expired or was already handled."}
    if not approved:
        return {"ok": True, "action": p["action"], "approved": False, "result": DECLINED}
    try:
        result = p["do"]()
    except Exception as e:  # ServiceNow errors surface as text, like in the tools
        return {"ok": False, "action": p["action"], "approved": True, "result": f"ServiceNow error: {e}"}
    return {"ok": True, "action": p["action"], "approved": True, "result": result}
