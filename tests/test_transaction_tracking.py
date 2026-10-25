"""Transaction tracking by id: span collection, call-chain rebuild, origin, upstream/downstream, connectors."""
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ["SERVICENOW_MODE"] = "mock"
os.environ["REQUIRE_APPROVAL"] = "true"
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
    import contextlib
    import txn_agent.sources as T
    import anomaly_agent.sources.base as B
    stack = contextlib.ExitStack()
    for mod in (T, B):
        stack.enter_context(mock.patch.object(mod, "mock_mode", lambda: False))
    return stack


class Track(unittest.TestCase):
    def test_downstream_slow_dependency(self):
        from txn_agent.trace import track
        r = track(fake(), "TXN-ENR-20261024-0207")
        self.assertEqual(r["reported_at"], "enrollment-api")             # from INC0012347 mentioning the id
        self.assertEqual(r["reported_at_source"], "ServiceNow INC0012347")
        self.assertEqual(r["failure_point"], "eligibility-svc")
        self.assertEqual(r["origin"]["service"], "plan-catalog-svc")
        self.assertEqual(r["origin"]["type"], "slow_dependency")
        self.assertEqual(r["direction"], "downstream")
        self.assertEqual([c["service"] for c in r["propagation"]], ["plan-catalog-svc", "eligibility-svc", "enrollment-api", "enrollment-ui"])
        self.assertIn("CHG0098871", [c["number"] for c in r["suspect_changes"]])
        self.assertEqual(r["services"]["eligibility-svc"]["owner"], "Eligibility Engineering")
        insert = next(h for h in r["hops"] if h["operation"].startswith("INSERT"))
        self.assertEqual(insert["status"], "ok")  # logs attach to the covering span, not the closest one

    def test_upstream_bad_request(self):
        from txn_agent.trace import track
        r = track(fake(), "TXN-ENR-20261024-0311")
        self.assertEqual(r["reported_at_source"], "ServiceNow INC0012346")  # found via the work note
        self.assertEqual((r["failure_point"], r["origin"]["service"], r["direction"]), ("enrollment-api", "enrollment-ui", "upstream"))
        self.assertEqual(r["origin"]["type"], "bad_request_from_caller")
        self.assertEqual(r["suspect_changes"], [])  # changes on the callee are not suspects for a bad request

    def test_direction_depends_on_reported_system(self):
        from txn_agent.trace import track
        sn = fake()
        self.assertEqual(track(sn, "TXN-ENR-20261024-0207", "plan-catalog-svc")["direction"], "same")
        self.assertEqual(track(sn, "TXN-ENR-20261024-0207", "enrollment-ui")["direction"], "downstream")

    def test_error_not_bad_request_for_5xx(self):
        from txn_agent.trace import track
        r = track(fake(), "TXN-PAY-20261024-0412")
        self.assertEqual((r["origin"]["service"], r["origin"]["type"], r["direction"]), ("payment-gw", "error", "downstream"))

    def test_healthy_and_unknown(self):
        from txn_agent.trace import track
        ok = track(fake(), "TXN-ENR-20261024-0199")
        self.assertFalse(ok["failed"])
        self.assertIn("Completed successfully", ok["verdict"])
        none = track(fake(), "TXN-NOPE-0001")
        self.assertFalse(none["found"])
        with self.assertRaises(ValueError):
            track(fake(), "ab")

    def test_logs_only_links_are_inferred_from_cmdb(self):
        from txn_agent.sources import Span
        from txn_agent.trace import _build, analyse
        spans = [Span("splunk", "enrollment-api", "e", "2026-10-24 10:00:00.010", None, "error", "upstream eligibility-svc returned 504", 502, kind="log").dict(),
                 Span("splunk", "eligibility-svc", "e", "2026-10-24 10:00:00.020", None, "error", "timed out after 200ms", None, kind="log").dict(),
                 Span("splunk", "enrollment-ui", "e", "2026-10-24 10:00:00.000", None, "error", "502 shown", 502, kind="log").dict()]
        nodes, inferred = _build(spans, fake())
        self.assertTrue(inferred)
        a = analyse(nodes, "enrollment-ui")
        self.assertEqual(a["failure_point"], "eligibility-svc")
        self.assertEqual(a["direction"], "downstream")


class SystemLogs(unittest.TestCase):
    def test_mock_logs_flag_this_transaction(self):
        from txn_agent.trace import service_logs, track
        r = track(fake(), "TXN-ENR-20261024-0207")
        hop = next(h for h in r["hops"] if h["service"] == "eligibility-svc")
        self.assertTrue(hop["id"] and hop["parent"])  # span links exposed for the diagram
        L = service_logs("eligibility-svc", hop["start"], 2, "TXN-ENR-20261024-0207")
        self.assertEqual(L["state"], "mock")
        mine = [l for l in L["lines"] if l["this_txn"]]
        self.assertTrue(mine and all("TXN-ENR-20261024-0207" in l["message"] for l in mine))
        self.assertTrue(any(not l["this_txn"] for l in L["lines"]))  # surrounding lines of that system too

    def test_live_not_connected_and_bad_name(self):
        from txn_agent.trace import service_logs
        with live():
            self.assertEqual(service_logs("eligibility-svc")["state"], "not_connected")
        with self.assertRaises(ValueError):
            service_logs('x" | delete')


class Connectors(unittest.TestCase):
    def test_jaeger(self):
        from txn_agent import sources as T
        body = {"data": [{"traceID": "abc", "processes": {"p1": {"serviceName": "enrollment-api"}, "p2": {"serviceName": "eligibility-svc"}},
                          "spans": [{"spanID": "1", "operationName": "POST /enrollments", "startTime": 1761300000000000, "duration": 222000,
                                     "processID": "p1", "tags": [{"key": "http.status_code", "value": 502}], "references": []},
                                    {"spanID": "2", "operationName": "POST /check", "startTime": 1761300000008000, "duration": 205000, "processID": "p2",
                                     "tags": [{"key": "error", "value": True}], "references": [{"refType": "CHILD_OF", "spanID": "1"}],
                                     "logs": [{"fields": [{"key": "message", "value": "timed out after 200ms"}]}]}]}]}
        with live(), mock.patch.dict(os.environ, {"AIOPS_JAEGER_URL": "http://jaeger:16686"}), mock.patch.object(T, "http", lambda *a, **k: Resp(body)):
            spans = T.JaegerTrace().find("4bf92f3577b34da6a3ce929d0e0e4736", {"abc"})
        self.assertEqual([(s.service, s.status, s.parent_id) for s in spans][:2], [("enrollment-api", "error", ""), ("eligibility-svc", "error", "1")])
        self.assertIn("timed out", spans[1].error)

    def test_splunk(self):
        from txn_agent import sources as T
        text = json.dumps({"result": {"_time": "1761300000.123", "service": "eligibility", "level": "ERROR", "message": "timed out after 200ms",
                                      "trace_id": "abc"}})
        with live(), mock.patch.dict(os.environ, {"AIOPS_SPLUNK_URL": "https://s:8089", "AIOPS_SPLUNK_TOKEN": "t"}), \
                mock.patch.object(T, "http", lambda *a, **k: Resp(text=text)):
            spans = T.SplunkTrace({"service_map": {"eligibility": "eligibility-svc"}}).find("TXN-1234", set())
        self.assertEqual((spans[0].service, spans[0].status, spans[0].trace_id, spans[0].kind), ("eligibility-svc", "error", "abc", "log"))

    def test_not_connected_in_live(self):
        from txn_agent.trace import track
        with live():
            r = track(fake(), "TXN-ENR-20261024-0207")
        self.assertFalse(r["found"])
        self.assertIn("No trace or log source is connected", r["verdict"])
        self.assertIn("INC0012347", r["verdict"])  # still reports where ServiceNow mentions it


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

    def test_track_tool_and_attach(self):
        from txn_agent.agent import TXN_TOOLS, attach_trace_to_incident, track_transaction
        self.assertEqual(TXN_TOOLS[0].name, "track_transaction")
        t = json.loads(track_transaction.invoke({"txn_id": "TXN-ENR-20261024-0207"}))
        self.assertEqual((t["origin"]["service"], t["direction"]), ("plan-catalog-svc", "downstream"))
        before = len(self.sn.get_journal("INC0012347"))
        attach_trace_to_incident.invoke({"txn_id": "TXN-ENR-20261024-0207", "number": "INC0012347"})
        p = self.approval.take_new_pending()
        self.assertEqual(len(p), 1)
        self.approval.resolve_pending(p[0]["id"], True)
        notes = self.sn.get_journal("INC0012347")
        self.assertEqual(len(notes), before + 1)
        self.assertIn("plan-catalog-svc", notes[-1]["text"])


if __name__ == "__main__":
    unittest.main()
