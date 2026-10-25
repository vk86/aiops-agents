"""Signals-based anomaly detection: connectors (canned API responses), model contract, decisions, actions."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ["SERVICENOW_MODE"] = "mock"
os.environ["REQUIRE_APPROVAL"] = "true"
os.environ["AIOPS_ANOMALY_LOG"] = os.path.join(tempfile.mkdtemp(), "anomaly_log.jsonl")

HAS_LC = importlib.util.find_spec("langchain_core") is not None


def fake():
    from incident_agent.fake_servicenow import FakeServiceNowClient
    return FakeServiceNowClient()


class Resp:
    def __init__(self, body=None, text=""):
        self._b, self.text = body, text

    def json(self):
        return self._b


def live():
    """Pretend we are not in mock mode so connectors use their live code paths."""
    import contextlib
    import anomaly_agent.signals as S
    import anomaly_agent.sources.base as B
    import anomaly_agent.sources.model as M
    stack = contextlib.ExitStack()
    for mod in (B, M, S):
        stack.enter_context(mock.patch.object(mod, "mock_mode", lambda: False))
    return stack


class Decisions(unittest.TestCase):
    def setUp(self):
        from anomaly_agent import signals
        signals._cache.clear()
        self.S, self.sn = signals, fake()

    def test_mock_story_decisions(self):
        d = self.S.detect(self.sn, use_cache=False)
        dec = {r["service"]: r["decision"] for r in d["services"]}
        self.assertEqual(dec["eligibility-svc"], "raise")         # origin, nothing open
        self.assertEqual(dec["enrollment-api"], "attach")         # INC0012345 already open
        self.assertEqual(dec["vpn-dallas-01"], "attach")
        self.assertEqual(dec["okta-sso"], "suppressed")           # CHG0098890 window open
        self.assertEqual(dec["claims-db-prod"], "watch")
        e = next(r for r in d["services"] if r["service"] == "enrollment-api")
        self.assertEqual(e["context"]["business_services"], ["Member Enrollment"])
        self.assertEqual(e["context"]["owner"], "Enrollment Platform")
        self.assertEqual(set(e["sources"]), {"dynatrace", "appdynamics", "splunk"})
        self.assertGreaterEqual(e["features"]["kpi_drop_pct"], 38)
        self.assertEqual(d["services"][0]["decision"], "raise")   # actionable first

    def test_no_sources_connected_in_live(self):
        with live():
            d = self.S.detect(self.sn, use_cache=False)
        self.assertEqual(d["services"], [])
        states = {c["name"]: c["state"] for c in d["coverage"]}
        self.assertEqual(states["model"], "not_connected")
        self.assertEqual(states["dynatrace"], "not_connected")

    def test_event_and_note_text(self):
        d = self.S.detect(self.sn, use_cache=False)
        r = next(x for x in d["services"] if x["service"] == "eligibility-svc")
        f = self.S.event_fields(r)
        self.assertEqual(f["type"], "Anomaly")
        self.assertEqual(f["node"], "eligibility-svc")
        self.assertIn("Member Enrollment", f["additional_info"])
        self.assertIn("PlanCatalogClientTimeoutException", self.S.evidence_note(r))

    def test_labels_and_evaluation(self):
        rows = self.S.labels(self.sn)
        self.assertTrue(rows and all(r["priority"][0] in "12" for r in rows))
        self.assertIn("start_utc", self.S.labels_csv(rows).splitlines()[0])
        self.S.detect(self.sn, use_cache=False)
        ev = self.S.evaluate(self.sn)
        self.assertGreater(ev["detections"], 0)
        self.assertGreaterEqual(ev["confirmed"], 1)  # enrollment-api detection matches INC0012345


class Connectors(unittest.TestCase):
    def test_dynatrace(self):
        from anomaly_agent.sources import dynatrace as D
        probs = {"problems": [{"displayId": "P-1", "title": "Failure rate increase", "severityLevel": "ERROR", "impactLevel": "SERVICES",
                               "problemId": "abc", "startTime": 1761350000000, "affectedEntities": [{"name": "EnrollmentService"}],
                               "rootCauseEntity": {"name": "eligibility"}}]}
        series = {"result": [{"data": [{"dimensionMap": {"dt.entity.service.name": "EnrollmentService"}, "timestamps": [1, 2, 3, 4],
                                        "values": [0.3, 0.2, 0.4, 7.5]}]}]}
        calls = []

        def fake_http(method, url, **kw):
            calls.append(url)
            return Resp(probs if url.endswith("/problems") else series)
        env = {"AIOPS_DYNATRACE_URL": "https://x.live.dynatrace.com", "AIOPS_DYNATRACE_TOKEN": "t"}
        with live(), mock.patch.dict(os.environ, env), mock.patch.object(D, "http", fake_http):
            sigs = D.DynatraceSource({"service_map": {"EnrollmentService": "enrollment-api"}}).collect(60)
        self.assertEqual(sigs[0].service, "enrollment-api")
        self.assertEqual(sigs[0].severity, "critical")
        err = next(s for s in sigs if s.feature == "error_rate_pct")
        self.assertTrue(err.anomalous)
        self.assertEqual(err.baseline, 0.3)

    def test_appdynamics(self):
        from anomaly_agent.sources import appdynamics as AP
        rows = [{"name": "Submit Enrollment error %", "severity": "CRITICAL", "incidentStatus": "OPEN", "startTimeInMillis": 1761350000000,
                 "affectedEntityDefinition": {"entityType": "BUSINESS_TRANSACTION", "name": "Submit Enrollment"}, "deepLinkUrl": "u"},
                {"name": "old", "severity": "WARNING", "incidentStatus": "RESOLVED", "affectedEntityDefinition": {"name": "x"}}]
        env = {"AIOPS_APPD_URL": "https://a.saas.appdynamics.com", "AIOPS_APPD_TOKEN": "t", "AIOPS_APPD_APPS": "Enrollment"}
        with live(), mock.patch.dict(os.environ, env), mock.patch.object(AP, "http", lambda *a, **k: Resp(rows)):
            sigs = AP.AppDynamicsSource({"service_map": {"Submit Enrollment": "enrollment-api"}}).collect(60)
        self.assertEqual(len(sigs), 1)
        self.assertEqual((sigs[0].service, sigs[0].domain, sigs[0].severity), ("enrollment-api", "trace", "critical"))

    def test_splunk(self):
        from anomaly_agent.sources import splunk as SP
        text = "\n".join(json.dumps({"result": r}) for r in [
            {"service": "eligibility", "sig": "PlanCatalogClientTimeoutException", "n": "412", "base": "0", "last": "1761350000"},
            {"service": "claims", "sig": "Lock wait timeout", "n": "4", "base": "80", "last": "1761350000"}])
        env = {"AIOPS_SPLUNK_URL": "https://s:8089", "AIOPS_SPLUNK_TOKEN": "t"}
        with live(), mock.patch.dict(os.environ, env), mock.patch.object(SP, "http", lambda *a, **k: Resp(text=text)):
            sigs = SP.SplunkSource({"service_map": {"eligibility": "eligibility-svc"}}).collect(60)
        new = sigs[0]
        self.assertEqual((new.service, new.name, new.anomalous), ("eligibility-svc", "New error signature", True))
        self.assertFalse(sigs[1].anomalous)

    def test_model_contract_batch_and_single(self):
        from anomaly_agent.sources import model as M
        sent = []

        def fake_http(method, url, **kw):
            sent.append(kw["json_body"])
            b = kw["json_body"]
            if "services" in b:
                return Resp({"model_version": "v9", "results": [{"service": "enrollment-api", "score": 0.91,
                                                                  "top_features": [{"name": "error_rate_ratio", "value": 24, "contribution": 0.5}]}]})
            return Resp({"score": 0.2})
        items = [{"service": "enrollment-api", "ci": "enrollment-api", "features": {"error_rate_ratio": 24}}]
        with live(), mock.patch.dict(os.environ, {"AIOPS_MODEL_URL": "https://m/score", "AIOPS_MODEL_THRESHOLD": "0.6"}), \
                mock.patch.object(M, "http", fake_http):
            out = M.PredictiveModel().score("a", "b", items)
            self.assertEqual(out["model_version"], "v9")
            r = out["results"]["enrollment-api"]
            self.assertTrue(r["anomaly"])  # 0.91 >= default threshold
            self.assertEqual(sent[0]["services"][0]["features"]["error_rate_ratio"], 24)
            with mock.patch.dict(os.environ, {"AIOPS_MODEL_BATCH": "false"}):
                single = M.PredictiveModel().score("a", "b", items)
            self.assertFalse(single["results"]["enrollment-api"]["anomaly"])
            self.assertEqual(sent[-1]["service"], "enrollment-api")


@unittest.skipUnless(HAS_LC, "langchain-core not installed")
class Actions(unittest.TestCase):
    def setUp(self):
        from anomaly_agent import signals
        from incident_agent import approval, tools
        signals._cache.clear()
        self.sn = fake()
        self._orig = tools.sn
        tools.sn = lambda: self.sn
        self.tools, self.approval = tools, approval
        approval.set_approver(lambda action, d: None)

    def tearDown(self):
        self.tools.sn = self._orig

    def test_send_event_needs_approval(self):
        from anomaly_agent.agent import send_anomaly_event
        send_anomaly_event.invoke({"service": "eligibility-svc"})
        p = self.approval.take_new_pending()
        self.assertEqual(len(p), 1)
        self.assertFalse(getattr(self.sn, "events", []))
        res = self.approval.resolve_pending(p[0]["id"], True)
        self.assertTrue(json.loads(res["result"])["event_sent"])
        self.assertEqual(self.sn.events[0]["node"], "eligibility-svc")

    def test_attach_adds_note_to_open_incident(self):
        from anomaly_agent.agent import attach_anomaly_to_incident
        before = len(self.sn.get_journal("INC0012351"))
        attach_anomaly_to_incident.invoke({"service": "vpn-dallas-01"})
        p = self.approval.take_new_pending()
        self.assertIn("INC0012351", p[0]["action"])
        self.approval.resolve_pending(p[0]["id"], True)
        self.assertEqual(len(self.sn.get_journal("INC0012351")), before + 1)

    def test_detect_tool_and_tool_set(self):
        from anomaly_agent.agent import ANOMALY_TOOLS, detect_service_anomalies
        self.assertEqual(ANOMALY_TOOLS[0].name, "detect_service_anomalies")
        d = json.loads(detect_service_anomalies.invoke({}))
        self.assertEqual(d["services"][0]["service"], "eligibility-svc")
        self.assertIn("Predictive model: mock", d["sources"])


if __name__ == "__main__":
    unittest.main()
