"""ServiceNow evidence: incident, work notes, field history, changes on the CI and connected CIs,
CMDB relationships, incidents on connected CIs, problems/known errors, similar resolved incidents."""
from __future__ import annotations

from incident_agent.servicenow import ServiceNowError, get_client
from incident_agent.config import settings

from .base import Collected, Evidence, EvidenceSource


class ServiceNowSource(EvidenceSource):
    name = "servicenow"
    domains = ("itsm",)
    label = "ServiceNow"

    def __init__(self, client=None):
        self._client = client

    @property
    def sn(self):
        if self._client is not None:
            return self._client
        try:  # share the incident agent's cached client (one connection, same mode)
            from incident_agent import tools
            return tools.sn()
        except Exception:
            self._client = get_client()
            return self._client

    def status(self) -> dict:
        mode = settings.sn_mode
        return {"state": "connected", "detail": f"{mode} · {settings.sn_instance if mode == 'live' else mode}"}

    def collect(self, ctx: dict) -> Collected:
        c = self.sn
        t = ctx["incident"]
        number = t["number"]
        ci_id = t.get("cmdb_ci_raw") or t.get("cmdb_ci")
        related = c.related_cis(ci_id)
        cis = [ci_id] + [r["sys_id"] for r in related]
        journal = c.get_journal(number, limit=50)
        audit = c.audit_history(t)
        changes = c.changes_near(cis)
        concurrent = c.concurrent_on(cis, number)
        history = c.history_for(t)
        problems = c.problems_for(t)
        ev = []
        for i, j in enumerate(journal, 1):
            ev.append(Evidence(f"NOTE-{i}", "servicenow", "itsm", "note", j.get("text", "")[:160], j.get("at", ""),
                               {"type": j.get("type"), "by": j.get("by")}))
        for i, a in enumerate(audit, 1):
            ev.append(Evidence(f"AUD-{i}", "servicenow", "itsm", "field", f"{a['field']}: {a.get('old') or '—'} → {a.get('new')}",
                               a.get("at_raw", ""), {"by": a.get("by")}))
        for ch in changes:
            ev.append(Evidence(ch.get("number", ""), "servicenow", "itsm", "change", ch.get("short_description", ""),
                               ch.get("end_raw") or "", {k: ch.get(k) for k in ("cmdb_ci", "type", "risk", "state", "assignment_group")}))
        for r in concurrent:
            ev.append(Evidence(r["number"], "servicenow", "itsm", "incident", r.get("short_description", ""),
                               r.get("opened_at_raw", ""), {"ci": r.get("cmdb_ci"), "state": r.get("state")}))
        for p in problems:
            ev.append(Evidence(p.get("number", ""), "servicenow", "itsm", "problem", p.get("short_description", ""),
                               p.get("opened_at_raw", ""), {"known_error": p.get("known_error"), "workaround": p.get("workaround")}))
        for ch in changes:
            ch.setdefault("kind", "change")
        return Collected(evidence=ev, changes=changes,
                         extra={"journal": journal, "audit": audit, "related": related, "concurrent": concurrent,
                                "history": history, "problems": problems})

    def ci_name_for(self, incident: dict) -> str:
        return incident.get("cmdb_ci") or ""

    def incident_row(self, number: str) -> dict:
        try:
            return self.sn.incident_row(number)
        except ServiceNowError:
            raise
