"""Code (Git) and release evidence from GitHub.

Needs RCA_GITHUB_TOKEN (read-only fine-grained token: Contents + Pull requests + Deployments: read) and a CI → repo
mapping in rca_config.json:
  {"ci_map": {"enrollment-api": {"repo": "acme/enrollment-api", "environment": "production"}}}
For the incident's CI (and connected CIs) it looks back RCA_LOOKBACK_HOURS (default 72) before the incident opened:
  * merged pull requests and commits on the default branch  -> domain "code"
  * deployments to the mapped environment                   -> domain "release"
Both are also returned as change-like rows so the analysis scores them like change requests.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import requests

from .base import Collected, Evidence, EvidenceSource

API = os.getenv("RCA_GITHUB_API", "https://api.github.com")
UTC = timezone.utc


def _iso(raw: str) -> datetime:
    return datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def _raw(iso: str | None) -> str:
    return iso.replace("T", " ").replace("Z", "")[:19] if iso else ""


class _GitHubBase(EvidenceSource):
    def __init__(self, ci_map: dict | None = None):
        self.token = os.getenv("RCA_GITHUB_TOKEN", "")
        self.ci_map = ci_map or {}
        self.hours = int(os.getenv("RCA_LOOKBACK_HOURS", "72"))

    def status(self) -> dict:
        if not self.token:
            return {"state": "not_connected", "detail": "Set RCA_GITHUB_TOKEN in .env to enable."}
        if not self.ci_map:
            return {"state": "not_connected", "detail": "Map CIs to repositories in rca_config.json (ci_map)."}
        return {"state": "connected", "detail": f"{len(self.ci_map)} CI(s) mapped"}

    def _get(self, path: str, params: dict | None = None):
        r = requests.get(f"{API}{path}", params=params, timeout=20,
                         headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json"})
        r.raise_for_status()
        return r.json()

    def _targets(self, ctx: dict):
        cis = [ctx.get("ci")] + [r.get("name") for r in ctx.get("related", [])]
        return [(ci, self.ci_map[ci]) for ci in cis if ci and ci in self.ci_map]

    def _window(self, ctx: dict):
        opened = _iso(ctx["incident"]["opened_at_raw"])
        return opened - timedelta(hours=self.hours), opened + timedelta(hours=1)


class GitSource(_GitHubBase):
    name = "github"
    domains = ("code",)
    label = "Code (GitHub)"

    def collect(self, ctx: dict) -> Collected:
        if self.status()["state"] != "connected":
            return Collected()
        start, end = self._window(ctx)
        out = Collected()
        for ci, m in self._targets(ctx):
            repo = m["repo"]
            try:
                pulls = self._get(f"/repos/{repo}/pulls", {"state": "closed", "sort": "updated", "direction": "desc", "per_page": 50})
            except requests.RequestException as e:
                out.extra.setdefault("errors", []).append(f"{repo}: {e}")
                continue
            for p in pulls:
                merged = p.get("merged_at")
                if not merged:
                    continue
                t = datetime.fromisoformat(merged.replace("Z", "+00:00"))
                if not (start <= t <= end):
                    continue
                pid = f"PR-{p['number']}"
                files = []
                try:
                    files = [f["filename"] for f in self._get(f"/repos/{repo}/pulls/{p['number']}/files", {"per_page": 30})]
                except requests.RequestException:
                    pass
                title = p.get("title", "")
                out.evidence.append(Evidence(pid, "github", "code", "commit", f"{repo}#{p['number']}: {title}", _raw(merged),
                                             {"repo": repo, "author": (p.get("user") or {}).get("login"), "files": files[:15]},
                                             p.get("html_url", "")))
                out.changes.append({"number": pid, "kind": "commit", "short_description": f"{title} ({repo})",
                                    "description": " ".join(files), "cmdb_ci": ci, "assignment_group": repo,
                                    "start_raw": _raw(merged), "end_raw": _raw(merged), "start_date": _raw(merged), "end_date": _raw(merged)})
        return out


class ReleaseSource(_GitHubBase):
    name = "release"
    domains = ("release",)
    label = "Releases (GitHub deployments)"

    def collect(self, ctx: dict) -> Collected:
        if self.status()["state"] != "connected":
            return Collected()
        start, end = self._window(ctx)
        out = Collected()
        for ci, m in self._targets(ctx):
            repo, env = m["repo"], m.get("environment", "production")
            try:
                deps = self._get(f"/repos/{repo}/deployments", {"environment": env, "per_page": 30})
            except requests.RequestException as e:
                out.extra.setdefault("errors", []).append(f"{repo}: {e}")
                continue
            for d in deps:
                t = datetime.fromisoformat(d["created_at"].replace("Z", "+00:00"))
                if not (start <= t <= end):
                    continue
                did = f"DEP-{d['id']}"
                desc = d.get("description") or d.get("ref") or ""
                out.evidence.append(Evidence(did, "release", "release", "deployment", f"{repo} → {env}: {desc} ({(d.get('sha') or '')[:7]})",
                                             _raw(d["created_at"]), {"repo": repo, "ref": d.get("ref"), "sha": d.get("sha"),
                                                                     "creator": (d.get("creator") or {}).get("login")}))
                out.changes.append({"number": did, "kind": "deployment", "short_description": f"Deploy {d.get('ref')} to {env} ({repo}) {desc}",
                                    "description": desc, "cmdb_ci": ci, "assignment_group": repo,
                                    "start_raw": _raw(d["created_at"]), "end_raw": _raw(d["created_at"]),
                                    "start_date": _raw(d["created_at"]), "end_date": _raw(d["created_at"])})
        return out
