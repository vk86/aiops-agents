"""Anomaly and transaction agent tests against the mock ServiceNow (no LLM key needed)."""
import importlib.util
import json
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ["SERVICENOW_MODE"] = "mock"
os.environ["REQUIRE_APPROVAL"] = "true"

HAS_LC = importlib.util.find_spec("langchain_core") is not None


def fake():
    from incident_agent.fake_servicenow import FakeServiceNowClient
    return FakeServiceNowClient()


class Anomaly(unittest.TestCase):
    def test_spike_on_enrollment_api_with_change(self):
        from anomaly_agent.detect import scan
        r = scan(fake())
        self.assertIsNone(r["anchored"])
        top = r["anomalies"][0]
        self.assertEqual(top["key"], "enrollment-api")
        self.assertEqual(top["current"], 3)
        self.assertEqual(top["severity"], "high")
        self.assertIn("CHG0098871", [c["number"] for c in top["changes"]])
        self.assertEqual(len(top["series"]), 15)  # 14 baseline days + current window
        self.assertEqual(top["incidents"][0]["number"], "INC0012345")  # P1 first

    def test_other_dimensions(self):
        from anomaly_agent.detect import scan
        sn = fake()
        self.assertEqual(scan(sn, dimension="assignment_group")["anomalies"][0]["key"], "Service Desk")
        self.assertTrue(scan(sn, dimension="category")["anomalies"])
        with self.assertRaises(ValueError):
            scan(sn, dimension="caller_id")

    def test_window_is_anchored_when_data_is_old(self):
        from anomaly_agent.detect import scan
        sn = fake()
        r = scan(sn, now=sn.now + timedelta(days=5))
        self.assertIn("anchored", r["anchored"])
        self.assertEqual(r["anomalies"][0]["key"], "enrollment-api")

    def test_alert_noise_groups_bursts_and_noise(self):
        from anomaly_agent.detect import alert_noise
        n = alert_noise(fake())
        self.assertTrue(n["available"])
        self.assertGreater(n["dedup_reduction_pct"], 90)
        self.assertIn("enrollment-api", [b["node"] for b in n["bursts"]])
        self.assertIn("vpn-dallas-01", [b["node"] for b in n["bursts"]])
        self.assertIn("PRN-PLANO-3F", [b["node"] for b in n["noise"]])

    def test_alert_noise_unavailable_is_reported(self):
        from anomaly_agent.detect import alert_noise
        from incident_agent.fake_servicenow import FakeServiceNowClient
        sn = fake()
        snap = FakeServiceNowClient(data=(sn.rows, sn.journal, sn.changes, sn.ci_support))
        n = alert_noise(snap)
        self.assertFalse(n["available"])
        self.assertIn("em_alert", n["reason"])


class Transactions(unittest.TestCase):
    def test_trace_finds_origin_and_path(self):
        from txn_agent.path import trace_path
        r = trace_path(fake(), "Member Enrollment")
        self.assertEqual(r["status"], "down")
        self.assertEqual(r["origin"], "enrollment-api")
        names = [h["name"] for h in r["hops"]]
        for n in ("enrollment-ui", "enrollment-api", "eligibility-svc", "plan-catalog-svc", "notification-svc"):
            self.assertIn(n, names)
        self.assertIn("Member Enrollment → enrollment-ui → enrollment-api", r["verdict"])
        self.assertIn("notification-svc", r["verdict"])  # also unhealthy, but not the origin

    def test_unknown_transaction(self):
        from incident_agent.servicenow import ServiceNowError
        from txn_agent.path import trace_path
        with self.assertRaises(ServiceNowError):
            trace_path(fake(), "Nope")

    def test_impacted_walks_up_from_p1_p2(self):
        from txn_agent.path import impacted
        out = {t["transaction"]: t for t in impacted(fake())}
        self.assertIn("Member Enrollment", out)
        self.assertIn("Remote Access", out)
        self.assertEqual(list(out)[0], "Member Enrollment")  # P1 first
        self.assertIn("INC0012345", [i["number"] for i in out["Member Enrollment"]["incidents"]])

    def test_find_transaction_id(self):
        from txn_agent.path import find_transaction
        f = find_transaction(fake(), "TXN-ENR-20261024-0207")
        self.assertEqual([i["number"] for i in f["incidents"]], ["INC0012347"])
        self.assertEqual(f["cis"], ["enrollment-api"])
        self.assertEqual(f["trace_backend"]["state"], "not_connected")
        self.assertIn("not mentioned", find_transaction(fake(), "TXN-NOPE-1")["summary"])

    def test_rca_relations_unchanged(self):
        """Business-service relations must not leak into the RCA agent's dependency evidence."""
        names = {r["name"] for r in fake().related_cis("enrollment-api")}
        self.assertEqual(names, {"eligibility-svc", "notification-svc"})


@unittest.skipUnless(HAS_LC, "langchain-core not installed")
class AgentTools(unittest.TestCase):
    def setUp(self):
        from incident_agent import approval, tools
        self.sn = fake()
        self._orig = tools.sn
        tools.sn = lambda: self.sn
        self.tools, self.approval = tools, approval
        approval.set_approver(lambda action, d: None)

    def tearDown(self):
        self.tools.sn = self._orig

    def test_tool_sets(self):
        from anomaly_agent.agent import ANOMALY_TOOLS
        from txn_agent.agent import TXN_TOOLS
        self.assertEqual([t.name for t in ANOMALY_TOOLS][-3:], ["scan_anomalies", "alert_noise", "create_problem_for_anomaly"])
        self.assertEqual(ANOMALY_TOOLS[0].name, "detect_service_anomalies")
        self.assertEqual([t.name for t in TXN_TOOLS], ["track_transaction", "attach_trace_to_incident", "list_transactions",
                                                        "impacted_transactions", "trace_transaction", "find_transaction_id"])

    def test_scan_and_trace_tools(self):
        from anomaly_agent.agent import scan_anomalies
        from txn_agent.agent import trace_transaction
        a = json.loads(scan_anomalies.invoke({}))
        self.assertEqual(a["anomalies"][0]["key"], "enrollment-api")
        self.assertTrue(a["anomalies"][0]["incidents"][0].startswith("INC0012345 P1"))
        t = json.loads(trace_transaction.invoke({"name": "Remote Access"}))
        self.assertEqual(t["origin"], "vpn-dallas-01")

    def test_problem_needs_approval(self):
        from anomaly_agent.agent import create_problem_for_anomaly
        create_problem_for_anomaly.invoke({"short_description": "enrollment-api 5xx spike",
                                           "incident_numbers": ["INC0012345", "INC0012346"]})
        pending = self.approval.take_new_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.sn.problems, {})  # nothing written yet
        res = self.approval.resolve_pending(pending[0]["id"], True)
        self.assertTrue(self.sn.problems)
        self.assertIn("PRB", res["result"])

    def test_errors_come_back_as_text(self):
        from txn_agent.agent import trace_transaction
        self.assertIn("error", trace_transaction.invoke({"name": "Nope"}).lower())


if __name__ == "__main__":
    unittest.main()
