"""AIOps RCA agent tests against the mock ServiceNow (needs langchain-core for the tool tests; no LLM key)."""
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ["SERVICENOW_MODE"] = "mock"
os.environ["REQUIRE_APPROVAL"] = "true"
os.environ.pop("RCA_GITHUB_TOKEN", None)

HAS_LC = importlib.util.find_spec("langchain_core") is not None


def make_engine():
    from incident_agent.fake_servicenow import FakeServiceNowClient
    from rca_agent.engine import RCAEngine
    from rca_agent.sources.servicenow import ServiceNowSource
    fake = FakeServiceNowClient()
    return RCAEngine(sn_source=ServiceNowSource(client=fake)), fake


class Template(unittest.TestCase):
    def test_sections_and_domains(self):
        from rca_agent.template import EVIDENCE_DOMAINS, HYPOTHESIS_TO_CATEGORY, ROOT_CAUSE_CATEGORIES, SECTIONS
        keys = [s[0] for s in SECTIONS]
        self.assertEqual(len(keys), len(set(keys)))
        for k in ("summary", "timeline", "root_cause", "five_whys", "actions", "open_questions", "coverage"):
            self.assertIn(k, keys)
        self.assertEqual({d[0] for d in EVIDENCE_DOMAINS},
                         {"itsm", "metrics", "events", "logs", "traces", "code", "release", "human"})
        self.assertIn("Undetermined", ROOT_CAUSE_CATEGORIES)
        self.assertTrue(set(HYPOTHESIS_TO_CATEGORY.values()) <= set(ROOT_CAUSE_CATEGORIES))


class Engine(unittest.TestCase):
    def setUp(self):
        self.eng, self.fake = make_engine()

    def test_investigate_finds_the_change(self):
        r = self.eng.investigate("INC0012345")
        self.assertTrue(r["sufficient_evidence"])
        self.assertIn("CHG0098871", r["verdict"])
        top = r["hypotheses"][0]
        self.assertEqual(top["category"], "Change / release")
        self.assertIn("CHG0098871", top["evidence"])
        self.assertTrue(r["timeline"])
        ids = {e["id"] for e in r["evidence"]}
        self.assertIn("CHG0098871", ids)
        self.assertTrue(any(i.startswith("NOTE-") for i in ids))

    def test_coverage_marks_only_itsm_connected(self):
        r = self.eng.investigate("INC0012345")
        cov = {c["domain"]: c for c in r["coverage"]}
        self.assertEqual(cov["itsm"]["state"], "connected")
        self.assertGreater(cov["itsm"]["evidence_items"], 0)
        for d in ("code", "release"):
            self.assertEqual(cov[d]["state"], "not_connected")
        for d in ("metrics", "events", "logs", "traces"):
            self.assertIn(cov[d]["state"], ("not_connected", "planned"))
        self.assertEqual(cov["human"]["state"], "connected")

    def test_observation_becomes_evidence_and_sets_impact_start(self):
        r0 = self.eng.investigate("INC0012345")
        opened = r0["header"]["opened"]
        # an error-rate jump 10 minutes before the incident was opened
        from datetime import datetime, timedelta
        at = (datetime.strptime(opened[:19], "%Y-%m-%d %H:%M:%S") - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M")
        obs = self.eng.add_observation("inc0012345", "enrollment-api 5xx rose from 0.3% to 7% after plan catalog timeout", "Beth", at)
        self.assertEqual(obs["id"], "OBS-1")
        r = self.eng.investigate("INC0012345")
        self.assertIn("OBS-1", {e["id"] for e in r["evidence"]})
        self.assertTrue(any(e["kind"] == "observation" for e in r["timeline"]))
        self.assertAlmostEqual(r["metrics"]["time_to_detect_h"], 10 / 60, places=1)
        cov = {c["domain"]: c for c in r["coverage"]}
        self.assertEqual(cov["human"]["evidence_items"], 1)

    def test_draft_without_llm_renders_every_section(self):
        from rca_agent.engine import public, to_markdown
        from rca_agent.template import SECTIONS
        r = self.eng.draft("INC0012345", use_llm=False)
        self.assertEqual(r["narrative_source"], "template")
        md = to_markdown(r)
        for _, title, _, _ in SECTIONS:
            self.assertIn(title, md, title)
        self.assertIn("CHG0098871", md)
        self.assertNotIn("_analysis", public(r))
        json.dumps(public(r), default=str)  # API-serialisable

    def test_broken_connector_does_not_break_rca(self):
        from rca_agent.sources.base import EvidenceSource

        class Boom(EvidenceSource):
            name, label, domains = "boom", "Boom", ("logs",)

            def collect(self, ctx):
                raise RuntimeError("down")
        self.eng.sources.append(Boom())
        r = self.eng.investigate("INC0012345")
        self.assertIn("CHG0098871", r["verdict"])
        self.assertTrue(any("Boom" in g for g in r["evidence_gaps"]))

    def test_unknown_incident_raises(self):
        with self.assertRaises(Exception):
            self.eng.investigate("INC9999999")


@unittest.skipUnless(HAS_LC, "langchain-core not installed")
class Tools(unittest.TestCase):
    def setUp(self):
        from incident_agent import approval
        from rca_agent import agent
        self.eng, self.fake = make_engine()
        self._orig = agent.engine
        agent.engine = lambda: self.eng
        self.agent, self.approval = agent, approval
        approval.set_approver(lambda action, d: None)  # UI mode: park writes as pending

    def tearDown(self):
        self.agent.engine = self._orig

    def test_tool_set(self):
        self.assertEqual([t.name for t in self.agent.RCA_TOOLS],
                         ["find_incidents", "start_rca", "evidence_coverage", "add_observation", "draft_rca_report", "publish_rca"])

    def test_find_incidents_puts_top_p1_first(self):
        out = json.loads(self.agent.find_incidents.invoke({}))
        self.assertGreater(out["count"], 1)
        self.assertEqual(out["incidents"][0]["number"], "INC0012345")
        self.assertTrue(all(str(r["priority"])[:1] in "12" for r in out["incidents"]))

    def test_start_rca_and_observation(self):
        out = json.loads(self.agent.start_rca.invoke({"number": "INC0012345"}))
        self.assertIn("CHG0098871", out["verdict"])
        o = json.loads(self.agent.add_observation.invoke({"number": "INC0012345", "finding": "p99 latency 4s"}))
        self.assertEqual(o["recorded"]["id"], "OBS-1")

    def test_publish_needs_approval_then_writes_note_and_attachment(self):
        self.agent.draft_rca_report.invoke({"number": "INC0012345"})
        before = len(self.fake.get_journal("INC0012345"))
        self.agent.publish_rca.invoke({"number": "INC0012345"})
        pending = self.approval.take_new_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(len(self.fake.get_journal("INC0012345")), before)  # nothing written yet
        res = self.approval.resolve_pending(pending[0]["id"], True)
        self.assertIn("attachment", res["result"])
        self.assertEqual(len(self.fake.get_journal("INC0012345")), before + 1)
        att = self.fake.attachments["INC0012345"][0]
        self.assertEqual(att["file_name"], "RCA-INC0012345.md")
        self.assertIn("Root cause", att["content"])

    def test_tool_errors_come_back_as_text(self):
        self.assertIn("error", self.agent.start_rca.invoke({"number": "INC404"}).lower())


class FakeGraph:
    """Minimal stand-in for a LangGraph agent with a checkpointer (get_state / update_state / invoke)."""
    def __init__(self, fail_with=None, accept_update=True):
        self.threads, self.fail_with, self.accept_update, self.seen = {}, fail_with, accept_update, []

    def get_state(self, config):
        from types import SimpleNamespace
        return SimpleNamespace(values={"messages": list(self.threads.get(config["configurable"]["thread_id"], []))})

    def update_state(self, config, values, as_node=None):
        if not self.accept_update:
            raise ValueError("ambiguous update")
        self.threads.setdefault(config["configurable"]["thread_id"], []).extend(values["messages"])

    def invoke(self, inp, config):
        from langchain_core.messages import AIMessage, HumanMessage
        tid = config["configurable"]["thread_id"]
        self.seen.append(tid)
        msgs = self.threads.setdefault(tid, [])
        msgs.append(HumanMessage(inp["messages"][0]["content"]))
        if self.fail_with:
            msgs.append(AIMessage("", tool_calls=[{"name": "start_rca", "args": {"number": "INC1"}, "id": "c1"}]))
            err = type(self.fail_with, (Exception,), {})("boom")
            self.fail_with = None
            raise err
        msgs.append(AIMessage("done"))
        return {"messages": msgs}


@unittest.skipUnless(HAS_LC, "langchain-core not installed")
class AskRecovery(unittest.TestCase):
    def test_step_limit_becomes_a_reply_and_thread_is_repaired(self):
        from incident_agent import agent as A
        g = FakeGraph(fail_with="GraphRecursionError")
        out = A.ask(g, "Investigate the top P1", "t-rec")
        self.assertIn("too many steps", out["reply"])
        self.assertEqual(out["tool_calls"][0]["tool"], "start_rca")
        self.assertEqual(A._dangling(g.get_state(A._config("t-rec")).values["messages"]), [])
        self.assertEqual(A.ask(g, "What evidence is missing?", "t-rec")["reply"], "done")

    def test_unrepairable_thread_is_rotated(self):
        from incident_agent import agent as A
        g = FakeGraph(fail_with="BadRequestError", accept_update=False)
        with self.assertRaises(Exception):
            A.ask(g, "hi", "t-rot")
        self.assertEqual(A.ask(g, "again", "t-rot")["reply"], "done")
        self.assertNotEqual(g.seen[0], g.seen[1])  # second turn ran on a fresh thread


if __name__ == "__main__":
    unittest.main()
