"""Orchestrator: ingest -> plan -> investigate (tools) -> decide -> guard -> act.

Two planners share the same tools, policy guard and action layer:
* ``RulePlanner``  - deterministic, offline, always available;
* ``ClaudePlanner`` (llm_agent.py) - Claude drives the investigation with tool use
  and proposes a decision, which is then reconciled by the policy guard.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .documents import Document, DocumentStore
from .policy import COMMIT, ESCALATE, FOLLOW_UP, Decision, evaluate, guard, status_rule
from .registry import Registry
from .store import Store
from .tools import ToolExecutor

AGENT_ID = "registrar-agent/1.0"


@dataclass
class DocOutcome:
    doc_id: str
    student_id: str | None
    action: str
    priority: str | None = None
    reasons: list[str] = field(default_factory=list)
    reference: str | None = None       # record / task / case id
    decision_id: str | None = None
    planner: str | None = None
    guard_note: str | None = None


class RulePlanner:
    """Deterministic investigation plan. Chooses tools based on what the document contains."""
    name = "rule-planner"

    def investigate(self, ex: ToolExecutor, doc_id: str) -> Decision | None:
        doc = ex.call("get_document", {"doc_id": doc_id})
        sid = doc["fields"].get("student_id")
        plan = ["lookup_registry", "resolve_student_identity"]
        plan += [f"resolve_indirect_field:{f}" for f in doc["references"] if f != "name_change_ref"]
        plan += [f"list_document_versions:{r}" for refs in doc["references"].values() for r in refs]
        plan += ["compute_conferral_date", "validate_document", "evaluate_policy"]
        ex.store.audit.record("PLAN", {"doc_id": doc_id, "planner": self.name, "steps": plan},
                              actor=AGENT_ID, actor_type="agent", run_id=ex.run_id, subject=sid)
        for step in plan:
            name, _, arg = step.partition(":")
            if name == "lookup_registry":
                ex.call(name, {"student_id": sid})
            elif name == "resolve_indirect_field":
                ex.call(name, {"doc_id": doc_id, "field": arg})
            elif name == "list_document_versions":
                ex.call(name, {"doc_id": arg})
            elif name == "evaluate_policy":
                return Decision(**ex.call(name, {"doc_id": doc_id}) | {"proposed_by": self.name})
            else:
                ex.call(name, {"doc_id": doc_id})
        return None


def build_record(report, doc: Document, registry_row: dict | None) -> dict:
    """Canonical record + per-field provenance. Registry changes are *proposed*, never applied."""
    rec = {"student_id": report.student_id, "legal_name": report.identity.current_legal_name,
           "source_document": {"doc_id": doc.doc_id, "sha256": doc.sha256, "issue_date": doc.issue_date},
           "identity": {"status": report.identity.status, "evidence": report.identity.evidence},
           "fields": {}, "provenance": {}, "registry_update_proposals": []}
    for c in report.checks:
        if c.field in ("student_id", "name"):
            continue
        rec["fields"][c.field] = c.document_value if c.status in ("MATCH", "DOC_ONLY") else None
        rec["provenance"][c.field] = {"status": c.status, "chain": (c.provenance or {}).get("chain"),
                                      "derivation": (c.provenance or {}).get("derivation"), "note": c.note}
    if doc.fields.get("former_name"):
        rec["former_names"] = [doc.fields["former_name"]]
    if report.identity.status == "NAME_CHANGE_VERIFIED" and registry_row:
        rec["registry_update_proposals"].append(
            {"field": "name_on_record", "from": registry_row.get("name"), "to": report.identity.current_legal_name,
             "append_alias": registry_row.get("name"), "evidence": report.identity.evidence})
    return rec


class VerificationAgent:
    def __init__(self, store: Store, registry: Registry, docs: DocumentStore, *, planner=None,
                 require_approval: bool = False, today: date | None = None):
        self.store, self.registry, self.docs = store, registry, docs
        self.planner = planner or RulePlanner()
        self.require_approval = require_approval
        self.today = today
        self.run_id = f"RUN-{uuid.uuid4().hex[:8].upper()}"
        self.ex = ToolExecutor(store, registry, docs, self.run_id, AGENT_ID, "agent", today)

    @classmethod
    def from_paths(cls, db: Path, registry_csv: Path, inbox: Path, mapping: Path | None = None, **kw):
        return cls(Store(db), Registry.load(registry_csv, mapping), DocumentStore.from_directory(inbox), **kw)

    def run(self) -> list[DocOutcome]:
        audit = self.store.audit
        audit.record("RUN_STARTED", {"planner": self.planner.name, "require_approval": self.require_approval,
                                     "registry": self.registry.source, "registry_sha256": self.registry.sha256,
                                     "registry_columns": self.registry.mapping_used,
                                     "documents": [d.summary() for d in self.docs.docs]},
                     actor=AGENT_ID, actor_type="agent", run_id=self.run_id)
        outcomes = []
        for doc in self.docs.primaries():
            try:
                outcomes.append(self._process(doc))
            except Exception as exc:          # never silently drop a document
                audit.record("DOC_FAILED", {"doc_id": doc.doc_id, "error": f"{type(exc).__name__}: {exc}"},
                             actor=AGENT_ID, actor_type="agent", run_id=self.run_id,
                             subject=doc.fields.get("student_id"))
                outcomes.append(DocOutcome(doc.doc_id, doc.fields.get("student_id"), "ERROR", reasons=[str(exc)]))
        # students for whom only secondary documents arrived (e.g. a certificate but no transcript)
        seen = {d.fields.get("student_id") for d in self.docs.primaries()}
        for doc_id, versions in self.docs.by_id.items():
            latest = versions[-1]
            sid = latest.fields.get("student_id")
            if latest.is_primary or not sid or sid in seen:
                continue
            seen.add(sid)
            try:
                outcomes.append(self._process_orphan(latest))
            except Exception as exc:
                audit.record("DOC_FAILED", {"doc_id": doc_id, "error": f"{type(exc).__name__}: {exc}"},
                             actor=AGENT_ID, actor_type="agent", run_id=self.run_id, subject=sid)
                outcomes.append(DocOutcome(doc_id, sid, "ERROR", reasons=[str(exc)]))
        audit.record("RUN_FINISHED", {"summary": {a: sum(o.action == a for o in outcomes)
                                                  for a in {o.action for o in outcomes}}},
                     actor=AGENT_ID, actor_type="agent", run_id=self.run_id)
        return outcomes

    def _process_orphan(self, doc: Document) -> DocOutcome:
        """A secondary document with no transcript cannot be verified on its own: request the transcript
        (FOLLOW_UP), unless the registry status or a missing registry row demands a human (ESCALATE)."""
        sid = doc.fields.get("student_id")
        rec = self.registry.get(sid)
        self.ex.call("lookup_registry", {"student_id": sid})
        self.ex.call("list_document_versions", {"doc_id": doc.doc_id})
        report = self.ex.report(doc.doc_id)
        reason = (f"Only {doc.doc_type} {doc.doc_id} was received for {sid}; no official transcript to verify "
                  f"against")
        if rec is None:
            policy = Decision(ESCALATE, "HIGH", [f"Student {sid} not found in Master Registry", reason])
        else:
            status = rec.get("audit_status") or ""
            rule = status_rule(status)
            if rule["action"] == ESCALATE:
                policy = Decision(ESCALATE, "HIGH", [f"Registry status '{status}': {rule.get('note')}", reason])
            else:
                policy = Decision(FOLLOW_UP, "MEDIUM", [reason, f"Registry status is '{status}'"],
                                  follow_up={"task": f"Request the official transcript for {sid} from the issuing "
                                                     f"institution, then re-run verification",
                                             "due_in_days": 7, "owner": "registrar",
                                             "trigger_status": "MISSING_TRANSCRIPT"})
        did = self.store.save_decision(self.run_id, doc, sid, policy, report)
        self.store.audit.record("DECISION", {"decision_id": did, "doc_id": doc.doc_id, **policy.to_dict(),
                                             "policy_verdict": policy.action, "proposal": None, "orphan": True},
                                actor=AGENT_ID, actor_type="agent", run_id=self.run_id, subject=sid)
        out = DocOutcome(doc.doc_id, sid, policy.action, policy.priority, policy.reasons, decision_id=did,
                         planner="orphan-handler")
        if policy.action == FOLLOW_UP:
            out.reference = self.ex.call("create_follow_up", {"doc_id": doc.doc_id, "decision_id": did,
                                                              "student_id": sid, "follow_up": policy.follow_up})["task_id"]
        else:
            out.reference = self.ex.call("escalate_to_human", {
                "doc_id": doc.doc_id, "decision_id": did, "student_id": sid, "priority": policy.priority,
                "reasons": policy.reasons, "proposed_record": build_record(report, doc, rec.raw if rec else None)})["case_id"]
        return out

    def _process(self, doc: Document) -> DocOutcome:
        sid = doc.fields.get("student_id")
        if sid and self.store.already_committed(sid, doc.sha256):
            self.store.audit.record("SKIPPED", {"doc_id": doc.doc_id, "reason": "identical document already committed"},
                                    actor=AGENT_ID, actor_type="agent", run_id=self.run_id, subject=sid)
            return DocOutcome(doc.doc_id, sid, "SKIPPED", reasons=["already committed"])

        proposed = self.planner.investigate(self.ex, doc.doc_id)
        report = self.ex.report(doc.doc_id)
        policy = evaluate(report, self.registry.get(sid), self.today)
        cur = self.store.current_record(sid) if sid else None
        if cur and cur["status"] == "REVOKED" and cur["source_sha256"] == doc.sha256:
            # a human revoked the record built from this exact document: never re-commit it autonomously
            policy = Decision(ESCALATE, "HIGH", [f"Record from this document was revoked by {cur['committed_by']}: "
                                                 f"{cur['note']}"] + policy.reasons)
        final = guard(proposed, policy)
        if self.require_approval and final.action == COMMIT:
            final.reasons.append("governance mode: autonomous commits require staff approval")
        did = self.store.save_decision(self.run_id, doc, sid, final, report)
        self.store.audit.record("DECISION", {"decision_id": did, "doc_id": doc.doc_id, **final.to_dict(),
                                             "policy_verdict": policy.action,
                                             "proposal": proposed.action if proposed else None},
                                actor=AGENT_ID, actor_type="agent", run_id=self.run_id, subject=sid)

        record = build_record(report, doc, self.registry.get(sid).raw if self.registry.get(sid) else None)
        out = DocOutcome(doc.doc_id, sid, final.action, final.priority, final.reasons, decision_id=did,
                         planner=final.proposed_by, guard_note=final.guard_note)
        if final.action == COMMIT and not self.require_approval:
            out.reference = self.ex.call("commit_record", {"doc_id": doc.doc_id, "decision_id": did,
                                                           "record": record})["record_id"]
        elif final.action == COMMIT:
            out.action = "AWAITING_APPROVAL"
            out.reference = self.ex.call("escalate_to_human", {
                "doc_id": doc.doc_id, "decision_id": did, "student_id": sid, "priority": "LOW",
                "reasons": final.reasons, "proposed_record": record, "kind": "APPROVAL"})["case_id"]
        elif final.action == FOLLOW_UP:
            out.reference = self.ex.call("create_follow_up", {"doc_id": doc.doc_id, "decision_id": did,
                                                              "student_id": sid, "follow_up": final.follow_up})["task_id"]
        elif final.action == ESCALATE:
            out.reference = self.ex.call("escalate_to_human", {
                "doc_id": doc.doc_id, "decision_id": did, "student_id": sid, "priority": final.priority,
                "reasons": final.reasons, "proposed_record": record})["case_id"]
        return out
