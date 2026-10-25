"""Evidence sources: every system the RCA agent can learn from implements EvidenceSource.

collect(ctx) returns Evidence items. Sources that represent a change in the system (deployments, merged PRs,
commits) also return them as `changes` so the analysis scores them like ServiceNow change requests.
A source that is not configured reports status "not_connected" plus the gap it leaves, so the RCA
says honestly what it could not check.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Evidence:
    id: str                 # cited in the report, e.g. CHG0098871, PR-482, DEP-77, OBS-1
    source: str             # "servicenow", "github", "release", "engineer", ...
    domain: str             # template EVIDENCE_DOMAINS key: itsm, metrics, events, logs, traces, code, release, human
    kind: str               # change, deployment, commit, alert, log, trace, note, observation, incident, problem
    title: str
    time_raw: str = ""      # UTC "YYYY-MM-DD HH:MM:SS"
    detail: dict = field(default_factory=dict)
    link: str = ""


@dataclass
class Collected:
    evidence: list[Evidence] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)   # change-like rows for scoring (see analysis.analyze)
    extra: dict[str, Any] = field(default_factory=dict)  # source-specific payload (e.g. ServiceNow rows)


class EvidenceSource:
    name = "base"
    domains: tuple[str, ...] = ()
    label = "Evidence source"

    def status(self) -> dict:
        """{"state": connected|not_connected|planned|error, "detail": "..."}"""
        return {"state": "planned", "detail": "Not implemented yet."}

    def collect(self, ctx: dict) -> Collected:
        return Collected()
