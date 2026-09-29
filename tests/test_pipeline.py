import csv
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from registrar_agent.agent import VerificationAgent
from registrar_agent.documents import DocumentStore
from registrar_agent.hitl import AuthorizationError, ReviewService
from registrar_agent.llm_agent import ClaudePlanner
from registrar_agent.normalize import parse_relative_date
from registrar_agent.registry import Registry
from registrar_agent.store import Store

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "data" / "demo_registry.csv"
INBOX = ROOT / "data" / "demo_inbox"
STAFF = ROOT / "config" / "staff.json"
TODAY = date(2026, 9, 28)

EXPECTED = {
    "S1001": "COMMIT", "S1002": "COMMIT", "S1003": "COMMIT", "S1004": "COMMIT",
    "S1005": "FOLLOW_UP", "S1006": "ESCALATE", "S1007": "ESCALATE", "S1008": "ESCALATE",
    "S1009": "FOLLOW_UP", "S1010": "ESCALATE", "S1011": "ESCALATE", "S9999": "ESCALATE",
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "t.db"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def agent(self, inbox=INBOX, **kw):
        return VerificationAgent.from_paths(self.db, REGISTRY, inbox, today=TODAY, **kw)

    def run_agent(self, **kw):
        a = self.agent(**kw)
        return a, {o.student_id: o for o in a.run()}


class TestScenarios(Base):
    def test_every_scenario_reaches_expected_action(self):
        _, out = self.run_agent()
        self.assertEqual({k: v.action for k, v in out.items()}, EXPECTED)

    def test_identity_evolution_resolved_via_name_change_certificate(self):
        a, _ = self.run_agent()
        rec = json.loads(a.store.current_record("S1002")["record_json"])
        self.assertEqual(rec["legal_name"], "Priya Raman")
        self.assertEqual(rec["identity"]["status"], "NAME_CHANGE_VERIFIED")
        self.assertEqual(rec["registry_update_proposals"][0]["from"], "Priya Iyer")

    def test_calculated_milestones(self):
        a, _ = self.run_agent()
        s1003 = json.loads(a.store.current_record("S1003")["record_json"])
        self.assertEqual(s1003["fields"]["conferral_date"], "2026-06-14")        # 2026-05-15 + 30 days
        rpt = json.loads(next(d for d in a.store.decisions() if d["student_id"] == "S1009")["report_json"])
        conf = next(c for c in rpt["checks"] if c["field"] == "conferral_date")
        self.assertEqual(conf["document_value"], "2026-08-21")                    # Fri 2026-08-07 + 10 business days

    def test_relative_rule_parsing(self):
        r = parse_relative_date("30 days following the end of the Spring 2026 term")
        self.assertEqual((r.offset, r.unit, r.anchor_term), (30, "calendar", "Spring 2026"))
        self.assertEqual(parse_relative_date("TERM_END+10BD").apply(date(2026, 8, 7)), date(2026, 8, 21))
        self.assertEqual(parse_relative_date("5 days before the end of the Fall 2026 term").offset, -5)

    def test_version_control_picks_corrected_certificate_regardless_of_file_order(self):
        a, _ = self.run_agent()
        rec = json.loads(a.store.current_record("S1004")["record_json"])
        self.assertEqual(rec["fields"]["honors"], "Magna Cum Laude")
        self.assertEqual(rec["provenance"]["honors"]["chain"], ["TR-2026-1004 v1", "DC-2026-0301 v2"])

    def test_indirect_truth_from_revised_audit(self):
        a, _ = self.run_agent()
        rpt = json.loads(next(d for d in a.store.decisions() if d["student_id"] == "S1005")["report_json"])
        gpa = next(c for c in rpt["checks"] if c["field"] == "gpa")
        self.assertEqual((gpa["status"], gpa["document_value"]), ("MATCH", "3.62"))
        self.assertEqual(gpa["provenance"]["chain"], ["TR-2026-1005 v1", "RDA-2026-0045 v1"])

    def test_wrong_term_anchor_is_a_conflict(self):
        inbox = self.tmp / "inbox"; inbox.mkdir()
        txt = (INBOX / "TR-2026-1003.txt").read_text().replace("the Spring 2026 term", "the Fall 2025 term")
        (inbox / "TR-2026-1003.txt").write_text(txt)
        out = {o.student_id: o for o in self.agent(inbox=inbox).run()}
        self.assertEqual(out["S1003"].action, "ESCALATE")
        self.assertIn("Fall 2025", " ".join(out["S1003"].reasons))


class TestAuditTrail(Base):
    def test_every_step_is_audited_and_chain_is_intact(self):
        a, out = self.run_agent()
        ev = a.store.audit.events()
        types = [e.event_type for e in ev]
        self.assertEqual(types[0], "RUN_STARTED"); self.assertEqual(types[-1], "RUN_FINISHED")
        self.assertEqual(types.count("DECISION"), len(out))
        self.assertEqual(types.count("RECORD_COMMITTED"), 4)
        tools = {e.payload["tool"] for e in ev if e.event_type == "TOOL_CALL"}
        self.assertTrue({"lookup_registry", "resolve_indirect_field", "list_document_versions", "validate_document",
                         "commit_record", "create_follow_up", "escalate_to_human"} <= tools)
        self.assertEqual(a.store.audit.verify(), (True, []))

    def test_update_and_delete_are_blocked(self):
        a, _ = self.run_agent()
        for sql in ("UPDATE audit_events SET actor='x'", "DELETE FROM audit_events",
                    "UPDATE verified_records SET status='REVOKED'", "DELETE FROM decisions"):
            with self.assertRaises(sqlite3.IntegrityError, msg=sql):
                a.store.conn.execute(sql)

    def test_out_of_band_tampering_is_detected(self):
        a, _ = self.run_agent()
        c = a.store.conn
        c.execute("DROP TRIGGER audit_no_update"); c.execute("DROP TRIGGER audit_no_delete")
        c.execute("UPDATE audit_events SET payload='{\"tampered\":true}' WHERE seq=5")
        c.execute("DELETE FROM audit_events WHERE seq=9"); c.commit()
        ok, problems = a.store.audit.verify()
        self.assertFalse(ok)
        self.assertTrue(any("seq 5" in p for p in problems))
        self.assertTrue(any("gap" in p for p in problems))

    def test_rerun_is_idempotent(self):
        self.run_agent()
        a2, out2 = self.run_agent()
        self.assertEqual(out2["S1001"].action, "SKIPPED")
        self.assertEqual(len(a2.store.cases("OPEN")), 6)       # no duplicate cases
        self.assertEqual(len(a2.store.tasks("OPEN")), 2)       # no duplicate tasks


class TestHITL(Base):
    def setUp(self):
        super().setUp()
        self.a, self.out = self.run_agent()
        self.svc = ReviewService(self.a.store, STAFF)

    def test_high_priority_needs_supervisor_and_justification(self):
        cid = self.out["S1006"].reference
        with self.assertRaises(AuthorizationError):
            self.svc.approve(cid, "a.nguyen", "ok")
        with self.assertRaises(AuthorizationError):
            self.svc.approve(cid, "stranger", "ok")
        with self.assertRaises(ValueError):
            self.svc.approve(cid, "r.patel", None)
        res = self.svc.approve(cid, "r.patel", "Bursar confirmed balance paid 2026-09-27")
        rec = self.a.store.current_record("S1006")
        self.assertEqual((rec["committed_by_type"], rec["override"]), ("human", 1))
        self.assertEqual(res["version"], 1)
        self.assertIn("AUTHZ_DENIED", [e.event_type for e in self.a.store.audit.events(subject="S1006")])

    def test_correction_creates_override_record_with_diff(self):
        cid = self.out["S1007"].reference
        res = self.svc.correct(cid, "r.patel", {"gpa": "3.45"}, "Transcript misprint; registrar ledger confirms 3.45")
        self.assertEqual(res["diff"]["gpa"], {"from": None, "to": "3.45"})
        rec = json.loads(self.a.store.current_record("S1007")["record_json"])
        self.assertEqual(rec["provenance"]["gpa"]["status"], "HUMAN_CORRECTED")
        self.assertEqual(self.a.store.case(cid)["status"], "CORRECTED")
        with self.assertRaises(ValueError):
            self.svc.approve(cid, "r.patel", "again")           # closed cases stay closed

    def test_reject_with_follow_up(self):
        res = self.svc.reject(self.out["S1010"].reference, "a.nguyen", "Awaiting RDA-2026-0099",
                              "Request RDA-2026-0099 from Degree Audit Office")
        self.assertIsNotNone(res["follow_up_task"])
        self.assertIsNone(self.a.store.current_record("S1010"))

    def test_revocation_blocks_autonomous_recommit(self):
        self.svc.revoke_record("S1001", "r.patel", "Diploma fraud investigation")
        hist = self.a.store.record_history("S1001")
        self.assertEqual([h["status"] for h in hist], ["ACTIVE", "REVOKED"])
        _, out = self.run_agent()
        self.assertEqual(out["S1001"].action, "ESCALATE")
        self.assertIn("revoked", out["S1001"].reasons[0])

    def test_complete_follow_up(self):
        tid = self.out["S1005"].reference
        self.assertEqual(self.svc.complete_task(tid, "a.nguyen", "grades posted")["status"], "DONE")


class TestGovernanceModes(Base):
    def test_require_approval_routes_commits_to_queue(self):
        a, out = self.run_agent(require_approval=True)
        self.assertEqual(out["S1001"].action, "AWAITING_APPROVAL")
        self.assertIsNone(a.store.current_record("S1001"))
        svc = ReviewService(a.store, STAFF)
        svc.approve(out["S1001"].reference, "a.nguyen")          # LOW priority: reviewer may approve, no override
        self.assertEqual(a.store.current_record("S1001")["override"], 0)

    def test_registry_with_different_column_names(self):
        alt = self.tmp / "alt.csv"
        rename = {"student_id": "EMPLID", "name_on_record": "Legal_Name", "cumulative_gpa": "GPA"}
        with open(REGISTRY) as f, open(alt, "w", newline="") as g:
            rows = list(csv.DictReader(f))
            w = csv.DictWriter(g, [rename.get(k, k) for k in rows[0]])
            w.writeheader()
            for r in rows:
                w.writerow({rename.get(k, k): v for k, v in r.items()})
        reg = Registry.load(alt)
        self.assertEqual(reg.get("S1001").get("gpa"), "3.78")


class FakeClaude:
    """Scripted stand-in for anthropic.Anthropic: investigates, then submits a fixed proposal."""

    def __init__(self, proposal):
        self.proposal, self.turn = proposal, 0
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        doc_id = kw["messages"][0]["content"].split()[-1].rstrip(".")
        self.turn += 1
        if self.turn % 2 == 1:
            block = {"type": "tool_use", "id": f"t{self.turn}", "name": "validate_document", "input": {"doc_id": doc_id}}
        else:
            block = {"type": "tool_use", "id": f"t{self.turn}", "name": "submit_decision", "input": self.proposal}
        return SimpleNamespace(content=[block], stop_reason="tool_use", usage=None)


class TestLLMGuard(Base):
    def test_llm_cannot_commit_past_policy(self):
        planner = ClaudePlanner(client=FakeClaude({"action": "COMMIT", "priority": "LOW", "reasons": ["looks fine"]}),
                                model="fake")
        a, out = self.run_agent(planner=planner)
        self.assertEqual(out["S1007"].action, "ESCALATE")        # data conflict
        self.assertEqual(out["S1006"].action, "ESCALATE")        # hold
        self.assertIn("overridden", out["S1007"].guard_note)
        self.assertEqual(out["S1001"].action, "COMMIT")
        llm_calls = [e for e in a.store.audit.events(event_type="TOOL_CALL") if e.actor_type == "llm"]
        self.assertTrue(llm_calls)

    def test_llm_may_be_more_cautious(self):
        planner = ClaudePlanner(client=FakeClaude({"action": "ESCALATE", "priority": "MEDIUM",
                                                   "reasons": ["seal looks irregular"]}), model="fake")
        _, out = self.run_agent(planner=planner)
        self.assertEqual(out["S1001"].action, "ESCALATE")
        self.assertIn("seal looks irregular", out["S1001"].reasons)


if __name__ == "__main__":
    unittest.main()
