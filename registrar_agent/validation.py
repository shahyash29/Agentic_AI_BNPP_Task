"""Field-by-field validation of resolved document values against the Master Registry."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

from .documents import Document, DocumentStore
from .normalize import (is_empty, norm_degree, norm_honors, norm_text, parse_date, parse_float)
from .registry import RegistryRecord
from .resolvers import (DateResolution, IdentityResolution, ResolvedValue, resolve_conferral_date,
                        resolve_field, resolve_identity)

GPA_TOLERANCE = 0.005
REQUIRED_FIELDS = ("student_id", "name", "degree", "gpa", "conferral_date")
COMPARED_FIELDS = ("dob", "institution", "degree", "major", "gpa", "credits_earned", "final_term",
                   "term_end_date", "conferral_date", "honors")


@dataclass
class FieldCheck:
    field: str
    status: str                 # MATCH | MISMATCH | MISSING | UNRESOLVED | PENDING | DOC_ONLY | SKIPPED
    document_value: str | None
    registry_value: str | None
    provenance: dict | None = None
    note: str | None = None

    def to_dict(self):
        return asdict(self)


@dataclass
class ValidationReport:
    doc_id: str
    student_id: str | None
    registry_found: bool
    identity: IdentityResolution
    checks: list[FieldCheck] = field(default_factory=list)
    registry_issues: list[str] = field(default_factory=list)
    date_resolution: DateResolution | None = None
    document_issues: list[dict] = field(default_factory=list)

    @property
    def mismatches(self):
        return [c for c in self.checks if c.status == "MISMATCH"]

    @property
    def unresolved(self):
        return [c for c in self.checks if c.status == "UNRESOLVED"]

    @property
    def pending(self):
        return [c for c in self.checks if c.status == "PENDING"]

    @property
    def missing_required(self):
        return [c for c in self.checks if c.status == "MISSING" and c.field in REQUIRED_FIELDS]

    def verified_values(self) -> dict:
        vals = {c.field: c.document_value for c in self.checks if c.status == "MATCH"}
        vals["student_id"] = self.student_id
        vals["legal_name"] = self.identity.current_legal_name
        return vals

    def to_dict(self):
        return {"doc_id": self.doc_id, "student_id": self.student_id, "registry_found": self.registry_found,
                "identity": self.identity.to_dict(), "checks": [c.to_dict() for c in self.checks],
                "registry_issues": self.registry_issues,
                "date_resolution": self.date_resolution.to_dict() if self.date_resolution else None,
                "document_issues": self.document_issues}


def _compare(fld: str, doc_val, reg_val) -> bool:
    if fld == "gpa":
        a, b = parse_float(doc_val), parse_float(reg_val)
        return a is not None and b is not None and abs(a - b) <= GPA_TOLERANCE
    if fld == "credits_earned":
        return parse_float(doc_val) == parse_float(reg_val)
    if fld in ("dob", "conferral_date", "term_end_date"):
        return parse_date(doc_val) is not None and parse_date(doc_val) == parse_date(reg_val)
    if fld == "degree":
        return norm_degree(doc_val) == norm_degree(reg_val)
    if fld == "honors":
        return norm_honors(doc_val) == norm_honors(reg_val)
    return norm_text(doc_val) == norm_text(reg_val)


def validate(store: DocumentStore, doc: Document, rec: RegistryRecord | None) -> ValidationReport:
    sid = doc.fields.get("student_id")
    identity = resolve_identity(store, doc, rec)
    rpt = ValidationReport(doc.doc_id, sid, rec is not None, identity)
    from .consistency import check_student
    rpt.document_issues = [x.to_dict() for x in check_student(store, doc) if x.severity != "INFO"]
    if rec is None:
        return rpt

    for fld in COMPARED_FIELDS:
        # does the registry carry this field at all? (the institution's registry may hold only id + status)
        has_col = fld in rec.raw or (fld == "conferral_date" and
                                     ("conferral_date" in rec.raw or "conferral_rule" in rec.raw))
        rv: ResolvedValue = resolve_field(store, doc, fld)
        reg_val = rec.get(fld) if has_col else None
        prov = rv.to_dict()
        if rv.value and str(rv.value).upper().startswith("PENDING:"):
            # the document itself says the value is not final yet (e.g. GPA awaiting transfer evaluation)
            rpt.checks.append(FieldCheck(fld, "PENDING", None, reg_val, prov, str(rv.value)[8:].strip()))
            continue
        if rv.status == "UNRESOLVED":
            rpt.checks.append(FieldCheck(fld, "UNRESOLVED", None, reg_val, prov, rv.note))
            continue
        if rv.status == "MISSING":
            if fld in REQUIRED_FIELDS:
                rpt.checks.append(FieldCheck(fld, "MISSING", None, reg_val, prov))
            elif fld == "honors" and has_col:   # "Honors: None" is an assertion, not an omission
                rpt.checks.append(FieldCheck(fld, "MATCH" if is_empty(reg_val) else "MISMATCH", None, reg_val, prov))
            continue

        doc_val = rv.value
        note = rv.note
        if fld == "conferral_date":
            # always turn the statement into a concrete date (relative rule / certificate), registry or not
            dres = resolve_conferral_date(doc_val, rec, doc.fields.get("term_end_date"))
            rpt.date_resolution = dres
            prov["derivation"] = dres.derivation
            if dres.issues:
                rpt.checks.append(FieldCheck(fld, "MISMATCH", dres.document_date, reg_val, prov, "; ".join(dres.issues)))
                continue
            if dres.document_date is None:
                rpt.checks.append(FieldCheck(fld, "UNRESOLVED", None, reg_val, prov, dres.derivation))
                continue
            doc_val = dres.document_date
            if has_col:
                expected, derivation, reg_issues = rec.expected_conferral_date()
                rpt.registry_issues += reg_issues
                reg_val = expected.isoformat() if expected else None
                note = f"document: {dres.derivation}; registry: {derivation}"
            else:
                note = f"document: {dres.derivation}" + (f"; {rv.note}" if rv.note else "")
        if not has_col:
            # resolved from the documents, internally consistent, but the registry holds no value to compare
            rpt.checks.append(FieldCheck(fld, "DOC_ONLY", doc_val, None, prov, note))
            continue
        status = "MATCH" if _compare(fld, doc_val, reg_val) else "MISMATCH"
        if status == "MISMATCH" and fld == "honors" and is_empty(doc_val) and is_empty(reg_val):
            status = "MATCH"
        rpt.checks.append(FieldCheck(fld, status, doc_val, reg_val, prov, note))

    # registry self-consistency only matters once the audit claims to be cleared
    if (rec.get("audit_status") or "").upper() == "CLEARED":
        earned, req = parse_float(rec.get("credits_earned")), parse_float(rec.get("credits_required"))
        if earned is not None and req is not None and earned < req:
            rpt.registry_issues.append(f"registry marked CLEARED but credits_earned {earned} < required {req}")

    # student_id / name are identity fields, represented as checks for completeness
    rpt.checks.insert(0, FieldCheck("student_id", "MATCH", sid, rec.get("student_id")))
    rpt.checks.insert(1, FieldCheck("name", "MATCH" if identity.is_verified else
                                    ("MISSING" if not identity.document_name else "MISMATCH"),
                                    identity.document_name, identity.registry_name,
                                    {"identity_status": identity.status, "evidence": identity.evidence},
                                    "; ".join(identity.notes) or None))
    return rpt
