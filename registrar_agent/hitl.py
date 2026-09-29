"""Human-in-the-loop review: approve, correct, reject, revoke, complete follow-ups.

Every staff action is authorised against config/staff.json and written to the
immutable audit trail with actor_type='human'. Corrections never edit an
existing record - they commit a new version flagged ``override=1`` that carries
the reviewer's diff and justification.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .store import Store

ROLE_RANK = {"viewer": 0, "reviewer": 1, "supervisor": 2}
DEFAULT_STAFF = {"system-admin": {"role": "supervisor", "name": "System Administrator"}}


class AuthorizationError(PermissionError):
    pass


class ReviewService:
    def __init__(self, store: Store, staff_file: Path | None = None):
        self.store = store
        self.staff = DEFAULT_STAFF | (json.loads(Path(staff_file).read_text()) if staff_file and
                                      Path(staff_file).exists() else {})

    # ------------------------------------------------------------------ helpers
    def _authorize(self, reviewer: str, min_role: str, action: str, subject=None):
        role = self.staff.get(reviewer, {}).get("role")
        if role is None or ROLE_RANK[role] < ROLE_RANK[min_role]:
            self.store.audit.record("AUTHZ_DENIED", {"reviewer": reviewer, "role": role, "action": action,
                                                     "required_role": min_role},
                                    actor=reviewer, actor_type="human", subject=subject)
            raise AuthorizationError(f"{reviewer!r} (role={role}) may not {action}; requires {min_role}")

    def _open_case(self, case_id):
        case = self.store.case(case_id)
        if not case:
            raise KeyError(f"case {case_id} not found")
        if case["status"] != "OPEN":
            raise ValueError(f"case {case_id} is already {case['status']}")
        return case

    @staticmethod
    def _required_role(case) -> str:
        return "supervisor" if case["priority"] == "HIGH" else "reviewer"

    def _log(self, event, reviewer, subject, payload):
        self.store.audit.record(event, payload, actor=reviewer, actor_type="human", subject=subject)

    def _commit(self, case, record, reviewer, override, note):
        res = self.store.append_record(case["student_id"], record, source_doc_id=case["doc_id"],
                                       source_sha256=case["doc_sha256"], decision_id=case["decision_id"],
                                       by=reviewer, by_type="human", override=override, note=note)
        self._log("RECORD_COMMITTED", reviewer, case["student_id"],
                  res | {"case_id": case["case_id"], "override": override, "note": note, "record": record})
        return res

    # ------------------------------------------------------------------ case actions
    def approve(self, case_id, reviewer, note=None):
        case = self._open_case(case_id)
        self._authorize(reviewer, self._required_role(case), "approve case", case["student_id"])
        override = case["kind"] == "ESCALATION"
        if override and not note:
            raise ValueError("approving an agent escalation is an override and requires a justification note")
        record = json.loads(case["proposed_record_json"])
        record["human_review"] = {"case_id": case_id, "reviewer": reviewer, "resolution": "APPROVED", "note": note}
        res = self._commit(case, record, reviewer, override, note)
        self.store.resolve_case(case_id, "APPROVED", reviewer, "COMMITTED", note)
        self._log("HUMAN_APPROVED", reviewer, case["student_id"],
                  {"case_id": case_id, "kind": case["kind"], "override": override, "note": note, **res})
        return res

    def correct(self, case_id, reviewer, corrections: dict, note):
        case = self._open_case(case_id)
        self._authorize(reviewer, self._required_role(case), "correct case", case["student_id"])
        if not note:
            raise ValueError("corrections require a justification note")
        record = json.loads(case["proposed_record_json"])
        before = copy.deepcopy(record)
        diff = {}
        for k, v in corrections.items():
            if k == "legal_name":
                diff[k] = {"from": record.get("legal_name"), "to": v}
                record["legal_name"] = v
            else:
                diff[k] = {"from": record["fields"].get(k), "to": v}
                record["fields"][k] = v
                record["provenance"][k] = {"status": "HUMAN_CORRECTED", "by": reviewer, "note": note,
                                           "agent_provenance": before["provenance"].get(k)}
        record["human_review"] = {"case_id": case_id, "reviewer": reviewer, "resolution": "CORRECTED",
                                  "note": note, "diff": diff}
        res = self._commit(case, record, reviewer, True, note)
        self.store.resolve_case(case_id, "CORRECTED", reviewer, "COMMITTED_WITH_CORRECTIONS", note)
        self._log("HUMAN_CORRECTED", reviewer, case["student_id"], {"case_id": case_id, "diff": diff, "note": note, **res})
        return res | {"diff": diff}

    def reject(self, case_id, reviewer, note, follow_up_task: str | None = None, due_in_days: int = 7):
        case = self._open_case(case_id)
        self._authorize(reviewer, "reviewer", "reject case", case["student_id"])
        if not note:
            raise ValueError("rejection requires a note")
        tid = None
        if follow_up_task:
            tid = self.store.open_task(case["decision_id"], case["student_id"], case["doc_id"],
                                       {"task": follow_up_task, "due_in_days": due_in_days, "owner": reviewer,
                                        "trigger_status": f"HUMAN:{case_id}"})
        self.store.resolve_case(case_id, "REJECTED", reviewer, "NOT_COMMITTED", note)
        self._log("HUMAN_REJECTED", reviewer, case["student_id"], {"case_id": case_id, "note": note, "follow_up_task": tid})
        return {"case_id": case_id, "status": "REJECTED", "follow_up_task": tid}

    # ------------------------------------------------------------------ post-hoc oversight of autonomous commits
    def revoke_record(self, student_id, reviewer, reason):
        self._authorize(reviewer, "supervisor", "revoke record", student_id)
        cur = self.store.current_record(student_id)
        if not cur or cur["status"] != "ACTIVE":
            raise ValueError(f"no active record for {student_id}")
        record = json.loads(cur["record_json"]) | {"revocation": {"by": reviewer, "reason": reason,
                                                                  "revoked_record_id": cur["record_id"]}}
        res = self.store.append_record(student_id, record, status="REVOKED", source_doc_id=cur["source_doc_id"],
                                       source_sha256=cur["source_sha256"], decision_id=cur["decision_id"], by=reviewer,
                                       by_type="human", override=True, note=reason)
        self._log("HUMAN_REVOKED", reviewer, student_id, {"revoked_record_id": cur["record_id"], "reason": reason, **res})
        return res

    def complete_task(self, task_id, reviewer, note=None, cancel=False):
        task = self.store.task(task_id)
        if not task or task["status"] != "OPEN":
            raise ValueError(f"task {task_id} not open")
        self._authorize(reviewer, "reviewer", "complete task", task["student_id"])
        status = "CANCELLED" if cancel else "DONE"
        self.store.complete_task(task_id, reviewer, note, status)
        self._log("FOLLOW_UP_" + status, reviewer, task["student_id"], {"task_id": task_id, "note": note})
        return {"task_id": task_id, "status": status}
