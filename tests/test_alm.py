"""ALM bridge: draft work items from AIOps findings and create them in SynthoGEN / Jira / Rally / Azure DevOps."""
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
os.environ.setdefault("AIOPS_ANOMALY_LOG", os.path.join(tempfile.mkdtemp(), "a.jsonl"))
HAS_LC = importlib.util.find_spec("langchain_core") is not None


class FakeResp:
    def __init__(self, body=None, headers=None, text=""):
        self._b, self.headers, self.text = body, headers or {"Content-Type": "application/json"}, text

    def json(self):
        return self._b


class Recorder:
    def __init__(self, *responses):
        self.calls, self.responses = [], list(responses)

    def __call__(self, method, url, headers=None, json_body=None, **_):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "body": json_body})
        return self.responses.pop(0) if self.responses else FakeResp({})


ITEM = {"type": "Defect", "title": "Fix plan-catalog timeout", "user_story": "As a member...", "description": "d",
        "acceptance_criteria": ["Given x, When y, Then z"], "priority": "High", "labels": ["aiops", "svc enrollment"], "story_points": 3}


def ctx():
    return {"kind": "incident", "ref": "INC0012345", "links": ["INC0012345", "CHG0098871"], "writeback": None}


class Drafts(unittest.TestCase):
    def test_every_kind_drafts_a_complete_item(self):
        from alm import bridge
        refs = {"incident": "INC0012345", "rca_action": "INC0012345", "anomaly": "payment-gw",
                "transaction": "TXN-ENR-20261024-0207", "slo": "enrollment-api", "change": "CHG0098905"}
        for kind, ref in refs.items():
            with self.subTest(kind=kind):
                d = bridge.draft(kind, ref, 0, use_llm=False)
                it = d["item"]
                self.assertIn(it["type"], bridge.TYPES)
                self.assertTrue(it["title"].strip())
                self.assertGreaterEqual(len(it["acceptance_criteria"]), 2)
                self.assertTrue(d["context"]["facts"])
                self.assertEqual(d["draft_source"], "template")
                self.assertEqual(d["targets"][0]["id"], "mock")

    def test_incident_writes_back_to_itself_and_change_to_change_request(self):
        from alm import bridge
        self.assertEqual(bridge.context("incident", "INC0012347")["writeback"], {"table": "incident", "number": "INC0012347"})
        self.assertEqual(bridge.context("change", "CHG0098905")["writeback"]["table"], "change_request")

    def test_transaction_draft_names_the_origin(self):
        from alm import bridge
        d = bridge.draft("transaction", "TXN-ENR-20261024-0207", use_llm=False)
        self.assertIn("plan-catalog-svc", d["item"]["title"] + d["item"]["description"])


class Create(unittest.TestCase):
    def test_mock_create_records_item_and_adds_work_note(self):
        from alm import bridge
        from incident_agent import tools
        from incident_agent.fake_servicenow import FakeServiceNowClient
        fake = FakeServiceNowClient()
        with mock.patch.object(tools, "sn", lambda: fake):
            c = bridge.context("incident", "INC0012347")
            rec = bridge.create(c, dict(ITEM), "mock", "ENRL", True)
            self.assertTrue(rec["key"].startswith("ENRL-"))
            self.assertEqual(rec["writeback"], "INC0012347")
            notes = [j["text"] for j in fake.get_journal("INC0012347") if j.get("type") == "work note"]
            self.assertTrue(any(rec["key"] in n for n in notes))
            self.assertEqual(bridge.items()[0]["key"], rec["key"])

    def test_rejects_unknown_target_and_missing_title(self):
        from alm import bridge
        with self.assertRaises(ValueError):
            bridge.create(ctx(), dict(ITEM), "nope")
        with self.assertRaises(ValueError):
            bridge.create(ctx(), {**ITEM, "title": " "}, "mock")


class Targets(unittest.TestCase):
    def test_synthogen_mcp_handshake_and_result(self):
        from alm import bridge
        rec = Recorder(FakeResp({"result": {}}, {"Content-Type": "application/json", "Mcp-Session-Id": "s1"}), FakeResp({}),
                       FakeResp(None, {"Content-Type": "text/event-stream"},
                                'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{\\"key\\":\\"ENRL-77\\",\\"url\\":\\"https://jira/x\\"}"}]}}\n'))
        env = {"AIOPS_SYNTHOGEN_MCP_URL": "https://synthogen/mcp", "AIOPS_SYNTHOGEN_TOKEN": "t", "AIOPS_SYNTHOGEN_SYSTEM": "rally"}
        with mock.patch.dict(os.environ, env), mock.patch.object(bridge, "http", rec):
            res = bridge._send("synthogen", "ENRL", dict(ITEM), ctx())
        self.assertEqual(res["key"], "ENRL-77")
        self.assertEqual(res["system"], "SynthoGEN → rally")
        self.assertEqual([c["body"].get("method") for c in rec.calls], ["initialize", "notifications/initialized", "tools/call"])
        call = rec.calls[2]
        self.assertEqual(call["headers"]["Mcp-Session-Id"], "s1")
        self.assertEqual(call["headers"]["Authorization"], "Bearer t")
        args = call["body"]["params"]["arguments"]
        self.assertEqual(call["body"]["params"]["name"], "create_work_item")
        self.assertEqual((args["system"], args["type"], args["project"]), ("rally", "Defect", "ENRL"))
        self.assertEqual(args["links"], ["INC0012345", "CHG0098871"])

    def test_synthogen_system_from_item_overrides_env(self):
        from alm import bridge
        rec = Recorder(FakeResp({}), FakeResp({}), FakeResp({"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": {"id": "42"}}}))
        with mock.patch.dict(os.environ, {"AIOPS_SYNTHOGEN_MCP_URL": "https://s/mcp"}), mock.patch.object(bridge, "http", rec):
            res = bridge._send("synthogen", "P", {**ITEM, "system": "jira"}, ctx())
        self.assertEqual((res["key"], res["system"]), ("42", "SynthoGEN → jira"))

    def test_synthogen_tool_error_surfaces(self):
        from alm import bridge
        from anomaly_agent.sources.base import SourceError
        rec = Recorder(FakeResp({}), FakeResp({}), FakeResp({"id": 2, "result": {"isError": True, "content": [{"type": "text", "text": "no project"}]}}))
        with mock.patch.dict(os.environ, {"AIOPS_SYNTHOGEN_MCP_URL": "https://s/mcp"}), mock.patch.object(bridge, "http", rec):
            with self.assertRaisesRegex(SourceError, "no project"):
                bridge._send("synthogen", "P", dict(ITEM), ctx())

    def test_jira_payload(self):
        from alm import bridge
        rec = Recorder(FakeResp({"key": "ENRL-5"}))
        with mock.patch.dict(os.environ, {"AIOPS_JIRA_URL": "https://jira.x/", "AIOPS_JIRA_TOKEN": "pat"}), mock.patch.object(bridge, "http", rec):
            res = bridge._send("jira", "ENRL", dict(ITEM), ctx())
        f = rec.calls[0]["body"]["fields"]
        self.assertEqual(rec.calls[0]["url"], "https://jira.x/rest/api/2/issue")
        self.assertEqual((f["issuetype"]["name"], f["project"]["key"], f["priority"]["name"]), ("Bug", "ENRL", "High"))
        self.assertIn("svc-enrollment", f["labels"])
        self.assertEqual(res["url"], "https://jira.x/browse/ENRL-5")

    def test_rally_payload_and_errors(self):
        from alm import bridge
        from anomaly_agent.sources.base import SourceError
        rec = Recorder(FakeResp({"CreateResult": {"Object": {"FormattedID": "US123", "_ref": "https://rally/us/1"}}}))
        with mock.patch.dict(os.environ, {"AIOPS_RALLY_APIKEY": "k"}), mock.patch.object(bridge, "http", rec):
            res = bridge._send("rally", "/project/9", {**ITEM, "type": "User Story"}, ctx())
        self.assertIn("hierarchicalrequirement/create", rec.calls[0]["url"])
        self.assertEqual(rec.calls[0]["body"]["HierarchicalRequirement"]["PlanEstimate"], 3)
        self.assertEqual(res["key"], "US123")
        bad = Recorder(FakeResp({"CreateResult": {"Errors": ["Project not found"]}}))
        with mock.patch.dict(os.environ, {"AIOPS_RALLY_APIKEY": "k"}), mock.patch.object(bridge, "http", bad):
            with self.assertRaisesRegex(SourceError, "Project not found"):
                bridge._send("rally", "x", dict(ITEM), ctx())

    def test_azure_devops_payload(self):
        from alm import bridge
        rec = Recorder(FakeResp({"id": 991, "_links": {"html": {"href": "https://dev.azure.com/o/p/_workitems/edit/991"}}}))
        with mock.patch.dict(os.environ, {"AIOPS_ADO_ORG_URL": "https://dev.azure.com/o", "AIOPS_ADO_PAT": "p"}), mock.patch.object(bridge, "http", rec):
            res = bridge._send("ado", "Member Portal", {**ITEM, "type": "Feature"}, ctx())
        self.assertIn("/Member%20Portal/_apis/wit/workitems/$Feature", rec.calls[0]["url"])
        self.assertEqual(rec.calls[0]["headers"]["Content-Type"], "application/json-patch+json")
        self.assertEqual(rec.calls[0]["body"][0]["value"], ITEM["title"])
        self.assertEqual(res["key"], "991")

    def test_targets_follow_env(self):
        from alm import bridge
        with mock.patch.dict(os.environ, {"AIOPS_JIRA_URL": "https://j", "AIOPS_JIRA_TOKEN": "t", "AIOPS_ALM_TARGET": "jira"}):
            ids = [t["id"] for t in bridge.targets()]
        self.assertEqual(ids[0], "jira")
        self.assertIn("mock", ids)


@unittest.skipUnless(HAS_LC, "langchain_core not installed")
class Tools(unittest.TestCase):
    def test_create_tool_is_gated(self):
        from alm.tools import create_backlog_item, draft_backlog_item
        from incident_agent import approval
        approval.set_approver(lambda a, d: None)
        d = json.loads(draft_backlog_item.invoke({"kind": "anomaly", "ref": "payment-gw"}))
        self.assertIn("mock", d["targets"])
        before = len(__import__("alm.bridge", fromlist=["items"]).items())
        create_backlog_item.invoke({"kind": "anomaly", "ref": "payment-gw"})
        pend = approval.take_new_pending()
        self.assertEqual(len(pend), 1)
        self.assertIn("Create", pend[0]["action"])
        self.assertEqual(len(__import__("alm.bridge", fromlist=["items"]).items()), before)  # nothing created yet
        approval.resolve_pending(pend[0]["id"], True)
        self.assertEqual(len(__import__("alm.bridge", fromlist=["items"]).items()), before + 1)


if __name__ == "__main__":
    unittest.main()
