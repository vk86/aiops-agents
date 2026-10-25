"""Tool tests against the mock ServiceNow (needs langchain-core installed; no LLM key)."""
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ["SERVICENOW_MODE"] = "mock"
os.environ["REQUIRE_APPROVAL"] = "true"

HAS_LC = importlib.util.find_spec("langchain_core") is not None


@unittest.skipUnless(HAS_LC, "langchain-core not installed")
class Tools(unittest.TestCase):
    def setUp(self):
        from incident_agent import approval, tools
        from incident_agent.fake_servicenow import FakeServiceNowClient
        fake = FakeServiceNowClient()
        self._orig_sn = tools.sn
        tools.sn = lambda: fake  # isolate from whatever .env / other tests configured
        self.t, self.approval = tools, approval
        self.asked = []
        approval.set_approver(lambda action, d: self.asked.append(action) or self.answer)
        self.answer = True

    def tearDown(self):
        self.t.sn = self._orig_sn

    def test_get_and_search(self):
        r = json.loads(self.t.get_incident.invoke({"number": "INC0012345"}))
        self.assertEqual(r["assignment_group"], "Enrollment Platform")
        s = json.loads(self.t.search_incidents.invoke({"priority": "1"}))
        self.assertEqual(s["count"], 1)

    def test_errors_come_back_as_text(self):
        self.assertIn("not found", self.t.get_incident.invoke({"number": "INC404"}))

    def test_create_asks_approval_and_writes(self):
        out = json.loads(self.t.create_incident.invoke(
            {"short_description": "VPN down Dallas", "impact": "1", "urgency": "1", "assignment_group": "Network"}))
        self.assertEqual(self.asked, ["Create incident"])
        self.assertEqual(out["priority"], "1 - Critical")

    def test_declined_write_changes_nothing(self):
        self.answer = False
        out = self.t.resolve_incident.invoke({"number": "INC0012345", "close_code": "Solution provided", "close_notes": "x"})
        self.assertIn("declined", out)
        self.assertEqual(self.t.sn().get_incident("INC0012345")["state"], "In Progress")

    def test_ui_mode_parks_write_until_approved(self):
        self.approval.set_approver(lambda a, d: None)  # what the web UI does
        out = self.t.add_work_note.invoke({"number": "INC0012345", "note": "rollback started"})
        self.assertIn("Waiting for the user to approve", out)
        pending = self.approval.take_new_pending()
        self.assertEqual(pending[0]["action"], "Work note on INC0012345")
        before = len(self.t.sn().get_journal("INC0012345"))
        res = self.approval.resolve_pending(pending[0]["id"], True)
        self.assertTrue(res["approved"])
        self.assertEqual(len(self.t.sn().get_journal("INC0012345")), before + 1)
        self.assertFalse(self.approval.resolve_pending(pending[0]["id"], True)["ok"])  # one-shot

    def test_ui_reject_writes_nothing(self):
        self.approval.set_approver(lambda a, d: None)
        self.t.resolve_incident.invoke({"number": "INC0012345", "close_code": "Solution provided", "close_notes": "x"})
        pid = self.approval.take_new_pending()[0]["id"]
        self.assertFalse(self.approval.resolve_pending(pid, False)["approved"])
        self.assertEqual(self.t.sn().get_incident("INC0012345")["state"], "In Progress")

    def test_update_refuses_resolve_shortcut(self):
        out = self.t.update_incident.invoke({"number": "INC0012345", "state": "Resolved"})
        self.assertIn("resolve_incident", out)
        self.assertEqual(self.asked, [])

    def test_shift_and_insight_tools(self):
        b = json.loads(self.t.shift_briefing.invoke({}))
        self.assertEqual(len(b["top"]), 10)
        e = json.loads(self.t.who_solved_this_before.invoke({"number": "INC0012351"}))
        self.assertEqual(e["experts"][0]["person"], "Don Goodliffe")
        out = self.t.link_duplicate.invoke({"child_number": "INC0012346", "parent_number": "INC0012345"})
        self.assertEqual(json.loads(out)["parent"], "INC0012345")  # approver says yes in this test

    def test_schemas_expose_choices(self):
        props = self.t.resolve_incident.args
        self.assertIn("close_code", props)
        self.assertEqual(len(self.t.ALL_TOOLS), 23)


if __name__ == "__main__":
    unittest.main()
