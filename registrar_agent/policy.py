"""Deterministic lifecycle policy: COMMIT, FOLLOW_UP or ESCALATE.

This is the governance guardrail. Whatever proposes an action (the rule-based planner, Gemini
or Claude), the final action is the MORE CONSERVATIVE of the proposal and this policy's verdict
(ESCALATE > FOLLOW_UP > COMMIT). An LLM can make the agent more cautious, never less.

The mapping from registry ``audit_status`` to action lives in ``config/status_policy.json`` so the
registrar's office can change it without touching code.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path

from .normalize import parse_date, parse_float
from .registry import RegistryRecord
from .validation import ValidationReport

COMMIT, FOLLOW_UP, ESCALATE = "COMMIT", "FOLLOW_UP", "ESCALATE"
SEVERITY = {COMMIT: 0, FOLLOW_UP: 1, ESCALATE: 2}
POLICY_FILE = Path(__file__).resolve().parent.parent / "config" / "status_policy.json"

# Fallback playbook for statuses not listed in the config (kept for the demo data set).
FOLLOW_UP_PLAYBOOK = {
    "PENDING_FINAL_GRADES": ("Request final grade posting from Office of Records, then re-verify", 7, "records-office"),
    "PENDING_COMMITTEE_APPROVAL": ("Route degree audit to Graduate Degree Committee for sign-off", 14, "grad-committee"),
    "PENDING_DOCUMENTS": ("Request outstanding supporting documents from student", 10, "student"),
}
DEFAULT_FOLLOW_UP = ("Resolve pending administrative action, then re-verify", 7, "registrar")


def load_policy(path: Path = POLICY_FILE) -> dict:
    cfg = json.loads(path.read_text()) if path.exists() else {}
    cfg["statuses"] = {k.lower(): v for k, v in cfg.get("statuses", {}).items()}
    return cfg


POLICY = load_policy()


def status_rule(status: str) -> dict:
    """Resolve an audit_status to {action, priority?, note?, task?, owner?, due_in_days?}."""
    s = (status or "").strip()
    rule = POLICY["statuses"].get(s.lower())
    if rule:
        return rule
    u = s.upper()
    if u.startswith("HOLD") or u.endswith("_HOLD"):
        return {"action": ESCALATE, "priority": "HIGH", "note": "Administrative hold"}
    if u.startswith("PENDING") or u.endswith("_PENDING"):
        task, days, owner = FOLLOW_UP_PLAYBOOK.get(u, DEFAULT_FOLLOW_UP)
        return {"action": FOLLOW_UP, "task": task, "due_in_days": days, "owner": owner}
    if u == "CLEARED":
        return {"action": COMMIT, "note": "Audit status CLEARED"}
    return {"action": ESCALATE, "priority": "MEDIUM", "note": f"Unrecognised audit status '{s or '(blank)'}'"}


@dataclass
class Decision:
    action: str
    priority: str                          # LOW | MEDIUM | HIGH
    reasons: list[str] = field(default_factory=list)
    follow_up: dict | None = None
    policy_version: str = "2026.09-2"
    proposed_by: str = "policy"
    guard_note: str | None = None

    def to_dict(self):
        return asdict(self)


def evaluate(report: ValidationReport, rec: RegistryRecord | None, today: date | None = None) -> Decision:
    today = today or date.today()
    reasons_high: list[str] = []
    reasons_med: list[str] = []

    if not report.registry_found:
        return Decision(ESCALATE, "HIGH", [f"Student {report.student_id} not found in Master Registry"])

    status = (rec.get("audit_status") or "").strip()
    rule = status_rule(status)
    action = rule["action"]

    # --- evidence problems (independent of status) ---------------------------------------------
    idn = report.identity
    if idn.status == "CONFLICT":
        reasons_high.append("Identity conflict: " + "; ".join(idn.notes or ["name mismatch"]))
    elif idn.status == "FUZZY":
        reasons_med.append(f"Identity near-match only (similarity {idn.similarity}); confirm manually")
    for c in report.mismatches:
        if c.field == "name":
            continue
        reasons_high.append(f"Data conflict on {c.field}: document={c.document_value!r} registry={c.registry_value!r}"
                            + (f" ({c.note})" if c.note else ""))
    for c in report.unresolved:
        reasons_high.append(f"Unresolved indirect reference for {c.field}: {c.note}")
    for issue in report.document_issues:
        if issue["severity"] == "HIGH":
            reasons_high.append(f"Document integrity [{issue['code']}]: {issue['message']}")
    warnings = [f"[warning] {i['code']}: {i['message']}" for i in report.document_issues if i["severity"] == "WARN"]
    for issue in report.registry_issues:
        reasons_high.append(f"Registry integrity: {issue}")
    for c in report.missing_required:
        reasons_med.append(f"Required field '{c.field}' missing from document")

    # --- evidence vs status consistency ---------------------------------------------------------
    # a value the document marks as not final is only acceptable while the registry is still pending
    if action != FOLLOW_UP:
        for c in report.pending:
            reasons_high.append(f"Document states {c.field} is not final ({c.note}) but registry audit status is "
                                f"'{status or '(blank)'}'")
    conf = parse_date(report.date_resolution.document_date) if report.date_resolution else None
    if action == COMMIT and conf and conf > today:
        reasons_high.append(f"Registry status '{status}' says finalised, but the document's conferral date {conf} "
                            f"is in the future")
    cm = POLICY.get("credit_minimum")
    credits = next((parse_float(c.document_value) for c in report.checks
                    if c.field == "credits_earned" and c.document_value), None)
    if cm and credits is not None and credits < cm["value"]:
        msg = f"Credit hours on transcript ({credits:g}) are below the configured minimum ({cm['value']})"
        if cm.get("severity") == "HIGH":
            reasons_high.append(msg)
        else:
            warnings.append(f"[warning] CREDITS_BELOW_MINIMUM: {msg}")

    if action == ESCALATE:
        reasons_high.append(f"Registry status '{status}': {rule.get('note', 'requires human review')}")

    if reasons_high:
        return Decision(ESCALATE, "HIGH", reasons_high + reasons_med + warnings)
    if reasons_med:
        return Decision(ESCALATE, "MEDIUM", reasons_med + warnings)

    if action == FOLLOW_UP:
        due = rule.get("due_in_days", 7)
        reasons = [f"Documents verified; registry status '{status}' means finalisation is waiting on an "
                   f"administrative action"]
        reasons += [f"Document states {c.field} is not final: {c.note}" for c in report.pending]
        priority = "MEDIUM"
        if conf and conf < today:
            overdue = (today - conf).days
            reasons.append(f"Conferral date {conf} passed {overdue} days ago while status is still '{status}'")
            due, priority = POLICY.get("overdue_follow_up_days", 2), "HIGH"
        return Decision(FOLLOW_UP, priority, reasons + warnings,
                        follow_up={"task": rule.get("task", DEFAULT_FOLLOW_UP[0]), "due_in_days": due,
                                   "owner": rule.get("owner", "registrar"), "trigger_status": status})
    return Decision(COMMIT, "LOW", [f"All document evidence verified; registry status '{status}': "
                                    f"{rule.get('note', 'finalised')}"] + warnings)


def guard(proposed: Decision | None, policy: Decision) -> Decision:
    """Reconcile a proposed decision with the policy verdict (most conservative wins)."""
    if proposed is None:
        return policy
    if SEVERITY[proposed.action] >= SEVERITY[policy.action]:
        if SEVERITY[proposed.action] > SEVERITY[policy.action]:
            proposed.guard_note = f"proposal more conservative than policy ({policy.action}); proposal kept"
            proposed.reasons = proposed.reasons + [f"[policy would have chosen {policy.action}]"]
            if proposed.action == FOLLOW_UP and not proposed.follow_up:
                proposed.follow_up = {"task": "Agent-requested follow-up: " + "; ".join(proposed.reasons[:1]),
                                      "due_in_days": 7, "owner": "registrar", "trigger_status": "AGENT_REQUEST"}
            return proposed
        policy.proposed_by = proposed.proposed_by
        policy.reasons = policy.reasons + [r for r in proposed.reasons if r not in policy.reasons]
        return policy
    policy.guard_note = (f"proposal {proposed.action} by {proposed.proposed_by} overridden: "
                         f"policy requires {policy.action}")
    return policy
