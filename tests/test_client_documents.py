"""Tests against the institution's real data: narrative documents in data/client_inbox and the real
Master Registry data/registrar_degree_audit_registry.csv (columns: student_id, audit_status).
Some tests copy the registry and change one status to exercise a specific branch."""
import csv
import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from registrar_agent.agent import VerificationAgent
from registrar_agent.consistency import inspect_store
from registrar_agent.documents import DocumentStore
from registrar_agent.resolvers import resolve_field

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "data" / "client_inbox"
REGISTRY = ROOT / "data" / "registrar_degree_audit_registry.csv"
TODAY = date(2026, 9, 29)

EXPECTED = {
    "STU-5001": "FOLLOW_UP", "STU-5002": "COMMIT", "STU-5003": "COMMIT", "STU-5004": "FOLLOW_UP",
    "STU-5005": "FOLLOW_UP", "STU-5008": "FOLLOW_UP", "STU-5009": "ESCALATE", "STU-5010": "COMMIT",
    "STU-5011": "COMMIT", "STU-5012": "COMMIT", "STU-5013": "FOLLOW_UP", "STU-5014": "COMMIT",
    "STU-5015": "COMMIT", "STU-5016": "COMMIT",
    "STU-5006": "COMMIT", "STU-5007": "COMMIT",
}


def registry_with(path: Path, **overrides):
    rows = list(csv.DictReader(open(REGISTRY)))
    for r in rows:
        if r["student_id"] in overrides:
            r["audit_status"] = overrides[r["student_id"]]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, ["student_id", "audit_status"])
        w.writeheader()
        w.writerows(rows)
    return path


class TestNarrativeParsing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = DocumentStore.from_directory(INBOX)

    def test_all_twenty_files_used(self):
        files = {Path(d.path.split("#")[0]).name for d in self.store.docs}
        self.assertEqual(len(files), 19)                                       # + the registry CSV = 20
        self.assertEqual(len(self.store.primaries()), 16)                      # STU-5001 .. STU-5016

    def test_multi_transcript_file_is_split(self):
        ids = {d.doc_id for d in self.store.primaries()}
        self.assertTrue({"TR:STU-5014", "TR:STU-5015"} <= ids)
        self.assertEqual(self.store.latest("TR:STU-5015").fields["gpa"], "3.36")

    def test_relative_conferral_uses_document_term_end(self):
        from registrar_agent.resolvers import resolve_conferral_date
        d = self.store.latest("TR:STU-5001")
        self.assertEqual(resolve_conferral_date(d.fields["conferral_date"], None, d.fields["term_end_date"]).document_date,
                         "2026-07-07")

    def test_revised_certificate_supersedes_original(self):
        rv = resolve_field(self.store, self.store.latest("TR:STU-5004"), "conferral_date")
        self.assertEqual((rv.status, rv.value), ("INDIRECT", "2026-06-12"))
        self.assertEqual(rv.chain, ["TR:STU-5004 v1", "DCC:STU-5004 v2"])

    def test_embedded_name_change(self):
        f = self.store.latest("TR:STU-5003").fields
        self.assertEqual((f["former_name"], f["new_name"]), ("Margaret A. Whitcombe", "Margaret A. Reyes"))

    def test_pending_gpa(self):
        self.assertTrue(self.store.latest("TR:STU-5005").fields["gpa"].startswith("PENDING:"))

    def test_integrity_findings(self):
        codes = {(x.code, x.student_id) for x in inspect_store(self.store)}
        self.assertFalse([c for c in codes if c[0] == "ORPHAN_SECONDARY"])   # every certificate has its transcript
        self.assertIn(("UNREFERENCED_SECONDARY", "STU-5016"), codes)         # accreditation cert not used for data
        self.assertIn(("GRADE_CORRECTION_AFTER_TRANSCRIPT", "STU-5004"), codes)
        self.assertFalse([x for x in inspect_store(self.store) if x.code == "REVISION_DELTA_MISMATCH"])


class TestAgentOnClientDocuments(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.reg = REGISTRY

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_agent(self, **kw):
        a = VerificationAgent.from_paths(self.tmp / "t.db", self.reg, INBOX, today=TODAY, **kw)
        return a, {o.student_id: o for o in a.run()}

    def test_outcomes(self):
        a, out = self.run_agent()
        self.assertEqual({k: v.action for k, v in out.items()}, EXPECTED)
        rec = json.loads(a.store.current_record("STU-5003")["record_json"])
        self.assertEqual((rec["legal_name"], rec["former_names"]), ("Margaret A. Reyes", ["Margaret A. Whitcombe"]))
        self.assertEqual(rec["fields"]["gpa"], "3.87")
        self.assertIn("Conferral date 2026-07-07 passed", " ".join(out["STU-5001"].reasons))
        self.assertTrue(any("CREDITS_BELOW_MINIMUM" in r for r in out["STU-5011"].reasons))
        self.assertTrue(a.store.audit.verify()[0])

    def test_revised_certificate_date_is_recorded(self):
        a, out = self.run_agent()
        d = next(x for x in a.store.decisions() if x["student_id"] == "STU-5004")
        conf = next(c for c in json.loads(d["report_json"])["checks"] if c["field"] == "conferral_date")
        self.assertEqual((conf["status"], conf["document_value"]), ("DOC_ONLY", "2026-06-12"))

    def test_sealed_record_escalates(self):
        self.reg = registry_with(self.tmp / "r.csv", **{"STU-5010": "record_sealed"})
        _, out = self.run_agent()
        self.assertEqual(out["STU-5010"].action, "ESCALATE")

    def test_pending_gpa_with_conferred_registry_escalates(self):
        self.reg = registry_with(self.tmp / "r.csv", **{"STU-5005": "degree_conferred"})
        _, out = self.run_agent()
        self.assertEqual(out["STU-5005"].action, "ESCALATE")

    def test_grade_appeal_is_follow_up(self):
        self.reg = registry_with(self.tmp / "r.csv", **{"STU-5014": "grade_appeal_pending"})
        _, out = self.run_agent()
        self.assertEqual(out["STU-5014"].action, "FOLLOW_UP")


class FakeGemini:
    """Scripted stand-in for google.genai.Client: one tool call, then submit_decision."""

    def __init__(self, proposal):
        self.proposal, self.turn = proposal, 0
        self.models = SimpleNamespace(generate_content=self.generate_content)

    def generate_content(self, model, contents, config):
        doc_id = contents[0].parts[0].text.split()[-1].rstrip(".")
        self.turn += 1
        name, args = (("validate_document", {"doc_id": doc_id}) if self.turn % 2
                      else ("submit_decision", self.proposal))
        part = SimpleNamespace(function_call=SimpleNamespace(name=name, args=args), text=None)
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(role="model", parts=[part]))],
                               usage_metadata=None)


@unittest.skipUnless(__import__("importlib").util.find_spec("google.genai"), "google-genai not installed")
class TestGeminiGuard(TestAgentOnClientDocuments):
    def test_outcomes(self):  # override parent: gemini-driven run
        from registrar_agent.gemini_agent import GeminiPlanner
        planner = GeminiPlanner(client=FakeGemini({"action": "COMMIT", "priority": "LOW", "reasons": ["ok"]}),
                                model="fake-gemini")
        a, out = self.run_agent(planner=planner)
        self.assertEqual(out["STU-5014"].action, "COMMIT")
        self.assertEqual(out["STU-5009"].action, "ESCALATE")      # not in registry: guard overrides COMMIT
        self.assertEqual(out["STU-5005"].action, "FOLLOW_UP")     # conferral_pending: guard overrides COMMIT
        self.assertIn("overridden", out["STU-5005"].guard_note)
        llm_calls = [e for e in a.store.audit.events(event_type="TOOL_CALL") if e.actor == "llm:fake-gemini"]
        self.assertTrue(llm_calls)

    test_revised_certificate_date_is_recorded = None
    test_sealed_record_escalates = None
    test_pending_gpa_with_conferred_registry_escalates = None
    test_grade_appeal_is_follow_up = None


if __name__ == "__main__":
    unittest.main()
