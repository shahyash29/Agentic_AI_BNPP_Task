"""Agent tools. Every invocation goes through :class:`ToolExecutor`, which writes a
TOOL_CALL event (arguments, result digest, latency, errors) to the immutable audit trail.

Tools are split into two classes:
* analysis tools - read-only, available to the rule-based planner AND to the LLM;
* action tools   - side-effecting (commit / follow-up / escalate). They are invoked
  only by the orchestrator *after* the policy guard, never directly by an LLM.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from .documents import DocumentStore, FIELD_LABELS
from .normalize import extract_doc_refs, parse_relative_date
from .policy import Decision, evaluate
from .registry import Registry
from .resolvers import resolve_conferral_date, resolve_field, resolve_identity
from .store import Store
from .validation import ValidationReport, validate


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    fn: Callable[..., Any]
    kind: str = "analysis"     # analysis | action

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props)}


_DOC = {"doc_id": {"type": "string", "description": "Document id, e.g. TR-2026-1001"}}


class ToolExecutor:
    def __init__(self, store: Store, registry: Registry, docs: DocumentStore, run_id: str,
                 actor: str = "registrar-agent", actor_type: str = "agent", today: date | None = None):
        self.store, self.registry, self.docs, self.run_id = store, registry, docs, run_id
        self.actor, self.actor_type, self.today = actor, actor_type, today
        self._reports: dict[str, ValidationReport] = {}
        self.tools: dict[str, Tool] = {t.name: t for t in self._define()}

    # ------------------------------------------------------------------ dispatch
    def call(self, name: str, args: dict | None = None, *, caller: str | None = None,
             caller_type: str | None = None) -> Any:
        args = args or {}
        tool = self.tools.get(name)
        t0 = time.perf_counter()
        status, result, error = "ok", None, None
        try:
            if tool is None:
                raise KeyError(f"unknown tool '{name}'")
            result = tool.fn(**args)
            return result
        except Exception as exc:          # recorded, then re-raised
            status, error = "error", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            digest = json.dumps(result, sort_keys=True, default=str) if result is not None else ""
            self.store.audit.record(
                "TOOL_CALL",
                {"tool": name, "kind": tool.kind if tool else None, "args": args, "status": status, "error": error,
                 "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
                 "result_sha256": hashlib.sha256(digest.encode()).hexdigest(),
                 "result_preview": digest[:600]},
                actor=caller or self.actor, actor_type=caller_type or self.actor_type,
                run_id=self.run_id, subject=args.get("student_id") or self._sid(args.get("doc_id")))

    def specs(self, kind: str = "analysis") -> list[dict]:
        return [t.spec() for t in self.tools.values() if t.kind == kind]

    def _sid(self, doc_id):
        d = self.docs.latest(doc_id) if doc_id else None
        return d.fields.get("student_id") if d else None

    def _doc(self, doc_id):
        d = self.docs.latest(doc_id)
        if d is None:
            raise KeyError(f"document {doc_id} not found")
        return d

    def report(self, doc_id) -> ValidationReport:
        if doc_id not in self._reports:
            d = self._doc(doc_id)
            self._reports[doc_id] = validate(self.docs, d, self.registry.get(d.fields.get("student_id")))
        return self._reports[doc_id]

    # ------------------------------------------------------------------ analysis tools
    def list_primary_documents(self):
        return [d.summary() | {"student_id": d.fields.get("student_id")} for d in self.docs.primaries()]

    def get_document(self, doc_id, include_text: bool = False):
        d = self._doc(doc_id)
        refs = {f: extract_doc_refs(v) for f, v in d.fields.items() if extract_doc_refs(v)}
        rel = {f: v for f, v in d.fields.items() if f == "conferral_date" and parse_relative_date(v)}
        out = d.summary() | {"fields": d.fields, "references": refs, "relative_dates": rel}
        if include_text:
            out["text"] = d.text
        return out

    def list_document_versions(self, doc_id):
        versions = self.docs.versions(doc_id)
        if not versions:
            return {"doc_id": doc_id, "found": False, "versions": []}
        return {"doc_id": doc_id, "found": True, "selected_version": versions[-1].version,
                "versions": [v.summary() | {"fields": v.fields} for v in versions]}

    def lookup_registry(self, student_id):
        rec = self.registry.get(student_id)
        return {"found": False, "student_id": student_id} if rec is None else {"found": True, **rec.raw}

    def resolve_indirect_field(self, doc_id, field):
        return resolve_field(self.docs, self._doc(doc_id), field).to_dict()

    def resolve_student_identity(self, doc_id):
        d = self._doc(doc_id)
        return resolve_identity(self.docs, d, self.registry.get(d.fields.get("student_id"))).to_dict()

    def compute_conferral_date(self, doc_id):
        d = self._doc(doc_id)
        rec = self.registry.get(d.fields.get("student_id"))
        rv = resolve_field(self.docs, d, "conferral_date")
        doc_side = resolve_conferral_date(rv.value, rec).to_dict()
        if rec:
            exp, how, issues = rec.expected_conferral_date()
            doc_side["registry_expected"] = exp.isoformat() if exp else None
            doc_side["registry_derivation"] = how
            doc_side["registry_issues"] = issues
        return doc_side

    def validate_document(self, doc_id):
        return self.report(doc_id).to_dict()

    def evaluate_policy(self, doc_id):
        rpt = self.report(doc_id)
        return evaluate(rpt, self.registry.get(rpt.student_id), self.today).to_dict()

    # ------------------------------------------------------------------ action tools
    def commit_record(self, doc_id, decision_id, record, override=False, note=None, by=None, by_type=None):
        d = self._doc(doc_id)
        sid = record["student_id"]
        res = self.store.append_record(sid, record, source_doc_id=d.doc_id, source_sha256=d.sha256,
                                       decision_id=decision_id, by=by or self.actor,
                                       by_type=by_type or self.actor_type, override=override, note=note)
        self.store.audit.record("RECORD_COMMITTED", res | {"doc_id": doc_id, "decision_id": decision_id,
                                                          "override": override, "record": record},
                                actor=by or self.actor, actor_type=by_type or self.actor_type,
                                run_id=self.run_id, subject=sid)
        return res

    def create_follow_up(self, doc_id, decision_id, student_id, follow_up):
        existing = self.store.open_task_for(student_id, follow_up.get("trigger_status"))
        if existing:
            return {"task_id": existing, "created": False, "reason": "open task already exists"}
        tid = self.store.open_task(decision_id, student_id, doc_id, follow_up, self.today)
        self.store.audit.record("FOLLOW_UP_CREATED", {"task_id": tid, "decision_id": decision_id, **follow_up},
                                actor=self.actor, actor_type=self.actor_type, run_id=self.run_id, subject=student_id)
        return {"task_id": tid, "created": True}

    def escalate_to_human(self, doc_id, decision_id, student_id, priority, reasons, proposed_record, kind="ESCALATION"):
        d = self._doc(doc_id)
        existing = self.store.open_case_for(student_id, d.sha256)
        if existing:
            return {"case_id": existing, "created": False, "reason": "open case already exists for this document"}
        cid = self.store.open_case(decision_id, student_id, d, kind, priority, reasons, proposed_record)
        self.store.audit.record("ESCALATED", {"case_id": cid, "kind": kind, "priority": priority, "reasons": reasons,
                                              "decision_id": decision_id},
                                actor=self.actor, actor_type=self.actor_type, run_id=self.run_id, subject=student_id)
        return {"case_id": cid, "created": True}

    # ------------------------------------------------------------------ catalogue
    def _define(self) -> list[Tool]:
        fields = sorted(FIELD_LABELS)
        return [
            Tool("list_primary_documents", "List primary documents (transcripts) awaiting verification.",
                 _obj({}), self.list_primary_documents),
            Tool("get_document", "Read a document's header, extracted fields, detected cross-document "
                 "references and relative-date statements.",
                 _obj(_DOC | {"include_text": {"type": "boolean"}}, ["doc_id"]), self.get_document),
            Tool("list_document_versions", "List every version of a document id, ordered so the most "
                 "authoritative (latest/corrected) version is last.", _obj(_DOC), self.list_document_versions),
            Tool("lookup_registry", "Fetch the Master Registry (source of truth) row for a student.",
                 _obj({"student_id": {"type": "string"}}), self.lookup_registry),
            Tool("resolve_indirect_field", "Resolve a field whose value points to another document "
                 "(e.g. 'See Revised Degree Audit RDA-2026-0045'), following the chain to the latest version.",
                 _obj(_DOC | {"field": {"type": "string", "enum": fields}}), self.resolve_indirect_field),
            Tool("resolve_student_identity", "Reconcile the document name with the registry name using "
                 "aliases and name-change certificates.", _obj(_DOC), self.resolve_student_identity),
            Tool("compute_conferral_date", "Evaluate the document's conferral statement (static or relative "
                 "to term end) and the registry's expected date.", _obj(_DOC), self.compute_conferral_date),
            Tool("validate_document", "Full field-by-field validation of a document against the registry.",
                 _obj(_DOC), self.validate_document),
            Tool("evaluate_policy", "Deterministic lifecycle policy verdict (COMMIT/FOLLOW_UP/ESCALATE).",
                 _obj(_DOC), self.evaluate_policy),
            Tool("commit_record", "Commit a verified record (append-only).", _obj({}), self.commit_record, "action"),
            Tool("create_follow_up", "Open a follow-up workflow task.", _obj({}), self.create_follow_up, "action"),
            Tool("escalate_to_human", "Open a human review case.", _obj({}), self.escalate_to_human, "action"),
        ]
