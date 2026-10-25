"""RCA engine: collect evidence from every source, analyse it, and build a report that follows the template."""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from incident_agent.insights import ts

from .analysis import analyze
from .sources.base import Collected, Evidence
from .sources.github import GitSource, ReleaseSource
from .sources.melt import melt_sources
from .sources.servicenow import ServiceNowSource
from .template import (EVIDENCE_DOMAINS, NARRATIVE_PROMPT, ROOT_CAUSE_CATEGORIES, SECTIONS, TEMPLATE_VERSION,
                       empty_narrative)

UTC = timezone.utc
MITIGATION = re.compile(r"\b(mitigat\w*|workaround|rolled back|roll(ed)?back|restored|reverted|failed over|stable)\b", re.I)


def load_config() -> dict:
    for p in (os.getenv("RCA_CONFIG"), Path.cwd() / "rca_config.json", Path(__file__).resolve().parents[2] / "rca_config.json"):
        if p and Path(p).exists():
            try:
                return json.loads(Path(p).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
    return {}


def _fmt(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else None


def _h(a: datetime | None, b: datetime | None) -> float | None:
    return round((b - a).total_seconds() / 3600, 2) if a and b and b >= a else None


class RCAEngine:
    def __init__(self, sources: list | None = None, sn_source: ServiceNowSource | None = None):
        cfg = load_config()
        ci_map = cfg.get("ci_map", {})
        self.sn = sn_source or ServiceNowSource()
        self.sources = sources if sources is not None else [GitSource(ci_map), ReleaseSource(ci_map), *melt_sources()]
        self.observations: dict[str, list[dict]] = {}
        self.reports: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ coverage
    def coverage(self, number: str | None = None, used: dict | None = None) -> list[dict]:
        out = []
        for key, label, what in EVIDENCE_DOMAINS:
            if key == "itsm":
                srcs = [{"name": self.sn.name, "label": self.sn.label, **self.sn.status()}]
            elif key == "human":
                n = len(self.observations.get((number or "").upper(), []))
                srcs = [{"name": "engineer", "label": "RCA Workbench", "state": "connected",
                         "detail": f"{n} observation(s) added" if n else "Add findings (e.g. from dashboards/logs) as observations"}]
            else:
                srcs = [{"name": s.name, "label": s.label, **s.status()} for s in self.sources if key in s.domains]
            state = "connected" if any(x["state"] == "connected" for x in srcs) else ("planned" if any(x["state"] == "planned" for x in srcs) else "not_connected")
            items = (used or {}).get(key, 0)
            out.append({"domain": key, "label": label, "what": what, "state": state, "evidence_items": items, "sources": srcs})
        return out

    # ------------------------------------------------------------------ observations (human evidence)
    def add_observation(self, number: str, text: str, by: str = "engineer", at_utc: str = "") -> dict:
        number = number.strip().upper()
        with self._lock:
            items = self.observations.setdefault(number, [])
            at = at_utc.strip() or datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
            obs = {"id": f"OBS-{len(items) + 1}", "text": text.strip(), "by": by or "engineer", "at_raw": at[:19].replace("T", " ")}
            items.append(obs)
        return obs

    # ------------------------------------------------------------------ investigate
    def investigate(self, number: str) -> dict:
        number = number.strip().upper()
        incident = self.sn.incident_row(number)
        ctx = {"incident": incident, "ci": incident.get("cmdb_ci") or ""}
        sn = self.sn.collect(ctx)
        ctx["related"] = sn.extra.get("related", [])
        others, errors = Collected(), []
        for s in self.sources:
            try:
                got = s.collect(ctx)
            except Exception as e:  # a broken connector must not break the RCA
                errors.append(f"{s.label}: {e}")
                continue
            others.evidence += got.evidence
            others.changes += got.changes
            errors += got.extra.get("errors", [])
        obs = self.observations.get(number, [])
        x = sn.extra
        a = analyze(incident, x["journal"], x["audit"], sn.changes + others.changes, x["related"], x["concurrent"],
                    x["history"], x["problems"], observations=obs)
        evidence = sn.evidence + others.evidence + [Evidence(o["id"], "engineer", "human", "observation", o["text"], o["at_raw"], {"by": o["by"]}) for o in obs]
        used: dict[str, int] = {}
        for e in evidence:
            used[e.domain] = used.get(e.domain, 0) + 1
        report = {
            "template_version": TEMPLATE_VERSION, "status": "draft", "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            "header": {k: incident.get(k) for k in ("number", "short_description", "priority", "state", "cmdb_ci",
                                                     "assignment_group", "assigned_to", "caller_id", "category")}
                      | {"opened": incident.get("opened_at_raw"), "resolved": incident.get("resolved_at_raw") or None},
            "verdict": a["verdict"], "sufficient_evidence": a["sufficient_evidence"],
            "metrics": self._metrics(incident, x["journal"], x["audit"], a["timeline"]),
            "timeline": a["timeline"], "hypotheses": a["hypotheses"],
            "evidence": [e.__dict__ for e in sorted(evidence, key=lambda e: e.time_raw or "")],
            "coverage": self.coverage(number, used), "evidence_gaps": a["evidence_gaps"] + [f"Connector error: {e}" for e in errors],
            "next_actions": a["next_actions"], "impact": a["impact"], "observations": obs,
            "narrative": None, "_analysis": a,
        }
        with self._lock:
            self.reports[number] = report
        return report

    def _metrics(self, inc: dict, journal: list, audit: list, timeline: list) -> dict:
        opened = ts(inc.get("opened_at_raw"))
        starts = [ts(e["time"].replace(" UTC", ":00")) for e in timeline if e["kind"] == "observation"]
        starts = [t for t in starts if t and opened and t <= opened]
        impact_start = min(starts) if starts else opened
        ack = None
        for a in audit:
            if a.get("field") in ("state", "assigned_to") and a.get("new") not in ("New", ""):
                ack = ts(a.get("at_raw"))
                break
        if not ack and journal:
            ack = ts(journal[0].get("at"))
        mitig = next((ts(j.get("at")) for j in journal if MITIGATION.search(j.get("text", ""))), None)
        resolved = ts(inc.get("resolved_at_raw"))
        now = datetime.now(UTC)
        return {"impact_start": _fmt(impact_start), "detected": _fmt(opened), "acknowledged": _fmt(ack),
                "mitigated": _fmt(mitig), "resolved": _fmt(resolved),
                "time_to_detect_h": _h(impact_start, opened), "time_to_acknowledge_h": _h(opened, ack),
                "time_to_mitigate_h": _h(opened, mitig), "time_to_resolve_h": _h(opened, resolved),
                "open_for_h": None if resolved else _h(opened, now),
                "note": "Impact start = earliest engineer observation before the incident opened, else the open time; connect metrics for the exact onset."}

    # ------------------------------------------------------------------ narrative + render
    def draft(self, number: str, use_llm: bool = True) -> dict:
        number = number.strip().upper()
        report = self.investigate(number)
        a = report["_analysis"]
        narrative, source = empty_narrative(a), "template"
        if use_llm:
            try:
                from incident_agent.llm import get_model, text_of
                payload = {k: v for k, v in report.items() if not k.startswith("_")}
                txt = text_of(get_model().invoke(NARRATIVE_PROMPT.format(
                    number=number, categories=", ".join(ROOT_CAUSE_CATEGORIES), evidence=json.dumps(payload, default=str)[:60000])))
                parsed = _json(txt)
                if parsed:
                    narrative = {**narrative, **{k: v for k, v in parsed.items() if v not in (None, "", [], {})}}
                    source = "llm"
            except Exception as e:
                report["evidence_gaps"].append(f"Narrative written from template (LLM unavailable: {type(e).__name__}).")
        if narrative.get("root_cause", {}).get("category") not in ROOT_CAUSE_CATEGORIES:
            narrative.setdefault("root_cause", {})["category"] = "Undetermined"
        report["narrative"], report["narrative_source"] = narrative, source
        with self._lock:
            self.reports[number] = report
        return report

    def latest(self, number: str) -> dict | None:
        return self.reports.get(number.strip().upper())


def _json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


def public(report: dict) -> dict:
    return {k: v for k, v in report.items() if not k.startswith("_")}


def to_markdown(r: dict) -> str:
    h, n, m = r["header"], r.get("narrative") or {}, r["metrics"]
    rc = n.get("root_cause", {})
    L = [f"# RCA: {h['number']} {h.get('short_description') or ''}",
         f"_AIOps RCA template v{r['template_version']} · status: {r['status']} · generated {r['generated_at']}_", ""]
    for key, title, _by, _g in SECTIONS:
        if key == "header":
            L += [f"## {title}", "| Field | Value |", "|---|---|"] + [f"| {k.replace('_', ' ').title()} | {v or '—'} |" for k, v in h.items()] + [""]
        elif key == "summary":
            L += [f"## {title}", n.get("summary") or r["verdict"], ""]
        elif key == "impact":
            L += [f"## {title}", n.get("impact") or "—", ""]
        elif key == "metrics":
            L += [f"## {title}", "| Milestone | Time | Duration |", "|---|---|---|",
                  f"| Impact start | {m['impact_start'] or '—'} | TTD {m['time_to_detect_h'] if m['time_to_detect_h'] is not None else '—'} h |",
                  f"| Detected (incident opened) | {m['detected'] or '—'} | |",
                  f"| Acknowledged | {m['acknowledged'] or '—'} | TTA {m['time_to_acknowledge_h'] if m['time_to_acknowledge_h'] is not None else '—'} h |",
                  f"| Mitigated | {m['mitigated'] or '—'} | TTM {m['time_to_mitigate_h'] if m['time_to_mitigate_h'] is not None else '—'} h |",
                  f"| Resolved | {m['resolved'] or ('open ' + str(m['open_for_h']) + ' h' if m['open_for_h'] else '—')} | TTR {m['time_to_resolve_h'] if m['time_to_resolve_h'] is not None else '—'} h |",
                  f"_{m['note']}_", ""]
        elif key == "timeline":
            L += [f"## {title}", "| Time | Type | Event | Ref |", "|---|---|---|---|"] + \
                 [f"| {e['time']} | {e['kind']} | {e['text'].replace('|', '/')} | {e.get('ref') or ''} |" for e in r["timeline"]] + [""]
        elif key == "root_cause":
            L += [f"## {title}", f"**Category:** {rc.get('category', 'Undetermined')} · **Confidence:** {float(rc.get('confidence') or 0):.0%}", "",
                  rc.get("statement") or r["verdict"], ""]
        elif key == "hypotheses":
            L += [f"## {title}"] + [f"{i}. **{x['title']}** ({x['confidence']:.0%}, {x.get('category')}): " + "; ".join(x["reasons"])
                                    + " · evidence: " + ", ".join(e for e in x["evidence"] if e) for i, x in enumerate(r["hypotheses"], 1)] + [""]
        elif key == "contributing":
            L += [f"## {title}"] + [f"- {c}" for c in n.get("contributing") or ["—"]] + [""]
        elif key == "five_whys":
            L += [f"## {title}"] + [f"{i}. {w}" for i, w in enumerate(n.get("five_whys") or [], 1)] + (["—"] if not n.get("five_whys") else []) + [""]
        elif key == "evidence":
            L += [f"## {title}", "| Id | Source | Kind | Finding | Time |", "|---|---|---|---|---|"] + \
                 [f"| {e['id']} | {e['source']} | {e['kind']} | {str(e['title']).replace('|', '/')[:140]} | {e['time_raw'] or ''} |" for e in r["evidence"]] + [""]
        elif key == "coverage":
            L += [f"## {title}", "| Domain | Status | Items | Detail |", "|---|---|---|---|"] + \
                 [f"| {c['label']} | {c['state']} | {c['evidence_items']} | {'; '.join(s['detail'] for s in c['sources'])} |" for c in r["coverage"]] + [""]
        elif key == "actions":
            acts = n.get("actions") or []
            L += [f"## {title}", "| Action | Type | Owner team | Priority |", "|---|---|---|---|"] + \
                 [f"| {x.get('action')} | {x.get('type')} | {x.get('owner_team') or '—'} | {x.get('priority')} |" for x in acts] + ([] if acts else ["| — | | | |"]) + [""]
        elif key == "lessons":
            les = n.get("lessons") or {}
            L += [f"## {title}", "**Went well**"] + [f"- {x}" for x in les.get("went_well") or ["—"]] + ["", "**Improve**"] + \
                 [f"- {x}" for x in les.get("improve") or ["—"]] + [""]
        elif key == "open_questions":
            L += [f"## {title}"] + [f"- {q}" for q in (n.get("open_questions") or []) + [g for g in r["evidence_gaps"] if g not in (n.get("open_questions") or [])]] + [""]
        elif key == "signoff":
            L += [f"## {title}", "| Prepared by | Status | Reviewers |", "|---|---|---|",
                  f"| AIOps RCA agent ({r.get('narrative_source', 'evidence only')}) | {r['status']} | — |", ""]
    return "\n".join(L)
