"""ServiceNow client and mock client tests. No network, no LLM.  Run: pytest -q"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("SERVICENOW_INSTANCE_URL", "https://example.service-now.com")

from incident_agent.fake_servicenow import FakeServiceNowClient  # noqa: E402
from incident_agent.servicenow import ServiceNowClient, ServiceNowError  # noqa: E402


def _resp(status=200, result=None, ctype="application/json"):
    r = MagicMock()
    r.status_code = status
    r.headers = {"Content-Type": ctype}
    r.content = b"x"
    r.json.return_value = {"result": result} if status < 400 else {"error": {"message": "bad", "detail": "nope"}}
    r.text = "<html>"
    return r


class LiveClientRequests(unittest.TestCase):
    def setUp(self):
        self.session = MagicMock()
        self.session.headers = {}
        self.c = ServiceNowClient("https://dev1.service-now.com/", "admin", "pw", session=self.session)

    def call(self):
        return self.session.request.call_args

    def test_get_incident_uses_table_api_with_display_values(self):
        self.session.request.return_value = _resp(result=[{"number": "INC0000001"}])
        self.assertEqual(self.c.get_incident("inc0000001")["number"], "INC0000001")
        args, kw = self.call()
        self.assertEqual(args, ("GET", "https://dev1.service-now.com/api/now/table/incident"))
        self.assertEqual(kw["params"]["sysparm_query"], "number=INC0000001")
        self.assertEqual(kw["params"]["sysparm_display_value"], "true")
        self.assertEqual(self.session.auth, ("admin", "pw"))

    def test_not_found(self):
        self.session.request.return_value = _resp(result=[])
        with self.assertRaises(ServiceNowError):
            self.c.get_incident("INC9")

    def test_search_builds_encoded_query(self):
        self.session.request.return_value = _resp(result=[])
        self.c.search_incidents(text="vpn", priority="<=2", assignment_group="Network", state="In Progress")
        q = self.call()[1]["params"]["sysparm_query"]
        for part in ("active=true", "short_descriptionLIKEvpn", "priority<=2", "state=2",
                     "assignment_group.nameLIKENetwork", "ORDERBYDESCsys_created_on"):
            self.assertIn(part, q)

    def test_create_posts_with_input_display_value_and_drops_blanks(self):
        self.session.request.return_value = _resp(result={"number": "INC0010099"})
        self.c.create_incident({"short_description": "VPN down", "assignment_group": "Network", "caller_id": ""})
        args, kw = self.call()
        self.assertEqual(args[0], "POST")
        self.assertEqual(kw["params"]["sysparm_input_display_value"], "true")
        self.assertEqual(kw["json"], {"short_description": "VPN down", "assignment_group": "Network"})

    def test_update_maps_state_label_and_patches_by_sys_id(self):
        self.session.request.side_effect = [_resp(result=[{"sys_id": "abc"}]), _resp(result={"number": "INC1"})]
        self.c.update_incident("INC1", {"state": "On Hold"})
        args, kw = self.call()
        self.assertEqual(args, ("PATCH", "https://dev1.service-now.com/api/now/table/incident/abc"))
        self.assertEqual(kw["json"], {"state": "3"})

    def test_work_note_vs_comment(self):
        self.session.request.side_effect = [_resp(result=[{"sys_id": "a"}]), _resp(result={})] * 2
        self.c.add_note("INC1", "internal")
        self.assertEqual(self.call()[1]["json"], {"work_notes": "internal"})
        self.c.add_note("INC1", "public", customer_visible=True)
        self.assertEqual(self.call()[1]["json"], {"comments": "public"})

    def test_resolve_sets_state_code_and_notes(self):
        self.session.request.side_effect = [_resp(result=[{"sys_id": "a"}]), _resp(result={})]
        self.c.resolve_incident("INC1", "Solution provided", "Restarted")
        self.assertEqual(self.call()[1]["json"], {"state": "6", "close_code": "Solution provided", "close_notes": "Restarted"})

    def test_friendly_errors(self):
        for status, text in ((401, "credentials"), (403, "permission"), (500, "failed (500)")):
            self.session.request.side_effect = None
            self.session.request.return_value = _resp(status=status)
            with self.assertRaises(ServiceNowError) as e:
                self.c.get_incident("INC1")
            self.assertIn(text, str(e.exception))

    def test_hibernating_instance_html(self):
        self.session.request.return_value = _resp(ctype="text/html")
        with self.assertRaises(ServiceNowError) as e:
            self.c.get_incident("INC1")
        self.assertIn("hibernat", str(e.exception))


class OAuthClient(unittest.TestCase):
    def test_client_credentials_token_is_fetched_cached_and_sent(self):
        from unittest.mock import patch
        session = MagicMock(); session.headers = {}
        session.request.return_value = _resp(result=[{"number": "INC1"}])
        tok = MagicMock(status_code=200); tok.json.return_value = {"access_token": "T1", "expires_in": 1800}
        with patch("incident_agent.servicenow.requests.post", return_value=tok) as post:
            c = ServiceNowClient("https://dev1.service-now.com", session=session, auth="oauth",
                                 client_id="cid", client_secret="sec")
            c.get_incident("INC1"); c.get_incident("INC1")
        self.assertEqual(post.call_count, 1)  # cached
        self.assertEqual(post.call_args[1]["data"]["grant_type"], "client_credentials")
        self.assertEqual(session.request.call_args[1]["headers"], {"Authorization": "Bearer T1"})

    def test_token_failure_is_explained(self):
        from unittest.mock import patch
        bad = MagicMock(status_code=401, text="x"); bad.json.return_value = {"error": "access_denied"}
        with patch("incident_agent.servicenow.requests.post", return_value=bad):
            c = ServiceNowClient("https://dev1.service-now.com", session=MagicMock(headers={}), auth="oauth",
                                 client_id="cid", client_secret="sec")
            with self.assertRaises(ServiceNowError) as e:
                c.get_incident("INC1")
        self.assertIn("access_denied", str(e.exception))


class MockClient(unittest.TestCase):
    def setUp(self):
        self.c = FakeServiceNowClient()

    def test_sixty_incidents_fifteen_open(self):
        self.assertEqual(len(self.c.rows), 60)
        self.assertEqual(len(self.c.open_incidents()), 15)

    def test_create_derives_priority(self):
        r = self.c.create_incident({"short_description": "VPN down", "impact": "1", "urgency": "2"})
        self.assertEqual(r["priority"], "2 - High")
        self.assertEqual(self.c.get_incident(r["number"])["state"], "New")

    def test_note_lands_in_activity(self):
        self.c.add_note("INC0012345", "rollback started")
        self.assertEqual(self.c.get_journal("INC0012345")[-1]["text"], "rollback started")

    def test_resolve_requires_code_and_notes_and_sets_resolver(self):
        with self.assertRaises(ServiceNowError):
            self.c.update_incident("INC0012345", {"state": "6"})
        r = self.c.resolve_incident("INC0012345", "Resolved by change", "Rolled back CHG0098871")
        self.assertEqual(r["state"], "Resolved")
        self.assertEqual(r["resolved_by"], "Beth Anglin")

    def test_search_filters(self):
        self.assertEqual({r["number"] for r in self.c.search_incidents(priority="1")}, {"INC0012345"})
        self.assertEqual(len(self.c.search_incidents(active_only=False, limit=100)), 60)

    def test_similar_and_changes(self):
        self.assertIn("INC0012311", [r["number"] for r in self.c.find_similar("Eligibility timeouts", "INC0012345")])
        self.assertEqual(self.c.recent_changes("INC0012345")[0]["number"], "CHG0098871")


class RootCause(unittest.TestCase):
    def setUp(self):
        from incident_agent import rca
        self.R, self.c = rca, FakeServiceNowClient()

    def run_rca(self, n):
        c = self.c
        t = c.incident_row(n)
        rel = c.related_cis(t["cmdb_ci_raw"])
        cis = [t["cmdb_ci_raw"]] + [r["sys_id"] for r in rel]
        return self.R.analyze(t, c.get_journal(n), c.audit_history(t), c.changes_near(cis), rel,
                              c.concurrent_on(cis, n), c.history_for(t), c.problems_for(t))

    def test_p1_points_at_timeout_change_with_evidence(self):
        a = self.run_rca("INC0012345")
        top = a["hypotheses"][0]
        self.assertTrue(a["sufficient_evidence"])
        self.assertEqual(top["evidence"], ["CHG0098871"])
        self.assertGreater(top["confidence"], a["hypotheses"][1]["confidence"] + 0.2)
        self.assertTrue(any("timeout" in r for r in top["reasons"]))
        kinds = {e["kind"] for e in a["timeline"]}
        self.assertTrue({"change", "incident", "note", "field"} <= kinds)

    def test_vpn_finds_known_error_and_change(self):
        types = {h["type"] for h in self.run_rca("INC0012351")["hypotheses"]}
        self.assertTrue({"known_problem", "change"} <= types)

    def test_no_evidence_is_honest(self):
        a = self.run_rca("INC0012361")
        self.assertFalse(a["sufficient_evidence"])
        self.assertTrue(a["evidence_gaps"])

    def test_template_narrative_cites_evidence(self):
        txt = self.R.template_narrative(self.run_rca("INC0012345"))
        self.assertIn("[CHG0098871]", txt)


class Insights(unittest.TestCase):
    def setUp(self):
        from incident_agent import insights as I
        self.I, self.c = I, FakeServiceNowClient()

    def test_shift_detection_overnight(self):
        from datetime import datetime, timezone
        s = self.I.current_shift(datetime(2026, 10, 24, 6, 30, tzinfo=timezone.utc), "Day=07-15,Evening=15-23,Night=23-07")
        self.assertIn(s["name"], {"Night", "Evening", "Day"})
        self.assertGreaterEqual(s["hours_in"], 0)

    def test_briefing_top10_sorted_by_priority(self):
        b = self.I.briefing(self.c.open_incidents(), [], self.I.current_shift())
        prios = [int(t["priority"][0]) for t in b["top"]]
        self.assertEqual(prios, sorted(prios))
        self.assertEqual(len(b["top"]), 10)
        self.assertIn("INC0012353", [s["number"] for s in b["sla_at_risk"]])  # P2 open 26h

    def test_sla_records_with_formatted_numbers(self):
        from datetime import datetime, timezone
        rows = [{"task": "INC0010297", "sla": "Priority 1 resolution", "percentage": "219,750.44", "has_breached": "true"},
                {"task": "INC0010300", "sla": "Priority 3 resolution", "percentage": "80.5", "has_breached": "false"}]
        out = self.I.sla_view([], rows, datetime.now(timezone.utc))
        self.assertEqual(out[0]["percent_elapsed"], 219750)
        self.assertTrue(out[0]["breached"])
        self.assertFalse(out[1]["breached"])

    def test_flatten_keeps_raw_numbers(self):
        from incident_agent.servicenow import _flatten
        r = _flatten({"percentage": {"display_value": "219,750.44", "value": "219750.44"},
                      "task": {"display_value": "INC0010297", "value": "abc"}})
        self.assertEqual(r["percentage_raw"], "219750.44")
        self.assertEqual(r["task"], "INC0010297")

    def test_who_solved_vpn(self):
        t = self.c.incident_row("INC0012351")
        e = self.I.experts(t, self.c.history_for(t))
        self.assertEqual(e["experts"][0]["person"], "Don Goodliffe")
        self.assertTrue(e["experts"][0]["examples"][0]["fix"])

    def test_assignment_reroutes_outlook_to_messaging(self):
        t = self.c.incident_row("INC0012352")
        a = self.I.suggest_assignment(t, self.c.history_for(t), self.c.ci_support_group("EXCH-SD-05"), self.c.open_incidents())
        self.assertEqual(a["recommendation"]["assignment_group"], "Messaging")
        self.assertFalse(a["already_correct"])

    def test_duplicates_of_p1(self):
        d = self.I.duplicates(self.c.incident_row("INC0012345"), self.c.open_incidents())
        self.assertEqual({c["number"] for c in d["candidates"]}, {"INC0012346", "INC0012347"})
        self.assertEqual(d["suggested_parent"], "INC0012345")

    def test_problem_candidate_vpn(self):
        pc = self.I.problem_candidates(self.c.opened_days(30), 30)
        self.assertEqual(pc[0]["configuration_item"], "vpn-dallas-01")

    def test_snapshot_loader_rebases_and_maps_labels(self):
        import tempfile, os
        from incident_agent.snapshot import load
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "incidents.csv"), "w", encoding="utf-8") as f:
            f.write("Number,Short description,State,Priority,Assignment group,Resolved by,Configuration item,Opened,Resolved\n"
                    "INC0000050,Exchange server down,Closed,1 - Critical,Network,Don Goodliffe,EXCH-SD-05,2015-10-15 08:00:00,2015-10-15 10:30:00\n"
                    "INC0000060,Cannot reach Exchange server,New,2 - High,Network,,EXCH-SD-05,2015-11-02 09:00:00,\n")
        c = load(d)
        self.assertEqual(len(c.rows), 2)
        self.assertTrue(c.rows["INC0000060"]["opened_at_raw"].startswith("20"))
        self.assertNotIn("2015", c.rows["INC0000060"]["opened_at_raw"])
        e = self.I.experts(c.incident_row("INC0000060"), c.history_for(c.incident_row("INC0000060")))
        self.assertEqual(e["experts"][0]["person"], "Don Goodliffe")


if __name__ == "__main__":
    unittest.main()
