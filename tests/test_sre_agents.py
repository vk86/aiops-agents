"""Reliability (SLO / error budget) and change guard agents against the mock ServiceNow."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ["SERVICENOW_MODE"] = "mock"
os.environ["REQUIRE_APPROVAL"] = "true"
os.environ.setdefault("AIOPS_ANOMALY_LOG", os.path.join(tempfile.mkdtemp(), "a.jsonl"))
HAS_LC = importlib.util.find_spec("langchain_core") is not None


def fake():
    from incident_agent.fake_servicenow import FakeServiceNowClient
    return FakeServiceNowClient()


class SLOMath(unittest.TestCase):
    def test_budget_and_burn_from_bad_ratio(self):
        from slo_agent import slo

        class Flat:
            label = "flat"

            def bad_ratio(self, s, a, b):
                return 0.002  # 0.2% bad all the time
        s = {"service": "x", "name": "x", "objective": 99.9, "window_days": 28}
        orig = slo.source_for
        slo.source_for = lambda *_: Flat()
        try:
            r = slo.evaluate(fake(), s, with_series=False)
        finally:
            slo.source_for = orig
        self.assertAlmostEqual(r["consumed_pct"], 200.0)       # 0.2% bad vs 0.1% budget
        self.assertEqual(r["remaining_pct"], -100.0)
        self.assertEqual(r["burn"]["1h"], 2.0)
        self.assertEqual(r["state"], "ticket")                 # 72h >= 1 and 6h >= 1, not page-level
        self.assertEqual(r["policy"]["level"], "freeze")

    def test_policy_levels(self):
        from slo_agent.slo import policy
        self.assertEqual([policy(x)["level"] for x in (80, 40, 10, -5)], ["normal", "caution", "restrict", "freeze"])


class Reliability(unittest.TestCase):
    def test_mock_slos(self):
        from slo_agent.slo import all_slos
        d = all_slos(fake())
        by = {s["service"]: s for s in d["slos"]}
        e = by["enrollment-api"]
        self.assertEqual((e["state"], e["policy"]["level"]), ("page", "freeze"))
        self.assertLess(e["remaining_pct"], 0)
        self.assertEqual(e["incidents"][0]["number"], "INC0012345")
        self.assertEqual(len([i for i in e["incidents"] if i["number"] in ("INC0012345", "INC0012346")]), 1)  # overlap not double counted
        self.assertTrue(any(c["number"] == "CHG0098871" and c["suspect"] for c in e["changes"]))
        self.assertEqual(by["okta-sso"]["change_window"]["number"], "CHG0098890")
        self.assertEqual(by["vpn-dallas-01"]["state"], "ok")
        self.assertEqual(len(e["series"]), 28)
        self.assertEqual(d["slos"][0]["service"], "enrollment-api")  # paging + lowest budget first

    def test_scorecard(self):
        from slo_agent.slo import scorecard
        rows = {r["service"]: r for r in scorecard(fake())}
        self.assertEqual(rows["enrollment-api"]["policy"], "freeze")
        self.assertGreater(rows["enrollment-api"]["change_failure_rate"], 0)
        self.assertEqual(rows["payment-gw"]["open_p1p2"], 1)


class ChangeGuard(unittest.TestCase):
    def test_upcoming_recommendations(self):
        from change_agent.risk import upcoming
        rec = {c["number"]: c["recommendation"] for c in upcoming(fake())["changes"]}
        self.assertEqual(rec["CHG0098905"], "HOLD")     # feature change: budget frozen, open P1, enrollment freeze
        self.assertEqual(rec["CHG0098901"], "CANARY")   # reliability fix during the same holds
        self.assertEqual(rec["CHG0098912"], "GO")

    def test_assessment_factors(self):
        from change_agent.risk import assess
        a = assess(fake(), "CHG0098905")
        hard = {f["key"] for f in a["factors"] if f["hard_hold"]}
        self.assertEqual(hard, {"budget", "health", "timing"})
        self.assertIn("Member Enrollment", a["business_services"])
        self.assertTrue(any("no backout plan" in f["detail"] for f in a["factors"]))
        self.assertIn("rollback_triggers", a["guardrails"])
        self.assertFalse(a["reliability_fix"])
        self.assertTrue(assess(fake(), "CHG0098901")["reliability_fix"])

    def test_change_failure_rate_counts_incident_after_change(self):
        from change_agent.risk import history_stats
        hs = history_stats(fake(), "enrollment-api")
        self.assertIn("CHG0098871", [f["number"] for f in hs["failed_changes"]])  # INC0012345 opened 18 min after it

    def test_verify(self):
        from change_agent.risk import verify
        v = verify(fake(), "CHG0098871")
        self.assertEqual(v["verdict"], "regression suspected")
        self.assertEqual(v["incidents_after"][0]["number"], "INC0012345")
        self.assertEqual(verify(fake(), "CHG0098905")["verdict"], "not finished")

    def test_peak_and_freeze(self):
        from change_agent.risk import _in_peak, policy_config
        pol = policy_config(datetime(2026, 11, 3, tzinfo=timezone.utc))
        self.assertTrue(any(f["name"] == "Open enrollment" for f in pol["freeze_windows"]))
        tue_10_cst = datetime(2026, 11, 3, 16, 0, tzinfo=timezone.utc)
        self.assertTrue(_in_peak(tue_10_cst, pol))
        self.assertFalse(_in_peak(tue_10_cst + timedelta(hours=10), pol))


@unittest.skipUnless(HAS_LC, "langchain-core not installed")
class Tools(unittest.TestCase):
    def setUp(self):
        from incident_agent import approval, tools
        self.sn = fake()
        self._orig = tools.sn
        tools.sn = lambda: self.sn
        self.tools, self.approval = tools, approval
        approval.set_approver(lambda action, d: None)

    def tearDown(self):
        self.tools.sn = self._orig

    def test_tools(self):
        from change_agent.agent import CHANGE_TOOLS, assess_change, post_change_assessment
        from slo_agent.agent import SLO_TOOLS, slo_status
        self.assertEqual([t.name for t in SLO_TOOLS], ["slo_status", "explain_slo", "reliability_scorecard"])
        self.assertEqual(CHANGE_TOOLS[0].name, "upcoming_change_risk")
        self.assertEqual(json.loads(slo_status.invoke({}))["summary"]["freeze"], 1)
        self.assertEqual(json.loads(assess_change.invoke({"number": "CHG0098905"}))["recommendation"], "HOLD")
        post_change_assessment.invoke({"number": "CHG0098905"})
        p = self.approval.take_new_pending()
        self.assertEqual(len(p), 1)
        self.assertFalse(getattr(self.sn, "change_notes", {}))
        self.approval.resolve_pending(p[0]["id"], True)
        self.assertIn("HOLD", self.sn.change_notes["CHG0098905"][0])


if __name__ == "__main__":
    unittest.main()
