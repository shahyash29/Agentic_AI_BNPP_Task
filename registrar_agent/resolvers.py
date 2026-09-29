"""Resolvers for the hard cases: indirect truth, identity evolution, calculated milestones."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date

from .documents import Document, DocumentStore
from .normalize import (extract_doc_refs, name_similarity, norm_name, parse_date,
                        parse_relative_date, is_empty)
from .registry import RegistryRecord

MAX_REF_DEPTH = 4
FUZZY_NAME_THRESHOLD = 0.90


# --------------------------------------------------------------------------- indirect truth
@dataclass
class ResolvedValue:
    field: str
    value: str | None
    source_doc: str | None
    source_version: int | None = None
    chain: list[str] = field(default_factory=list)       # e.g. ["TR-2026-1005", "RDA-2026-0045 v1"]
    status: str = "DIRECT"                                 # DIRECT | INDIRECT | UNRESOLVED | MISSING
    note: str | None = None
    versions_considered: list[dict] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def resolve_field(store: DocumentStore, doc: Document, fld: str, *, _depth: int = 0,
                  _seen: tuple = ()) -> ResolvedValue:
    """Resolve a field on ``doc``. If the value is a pointer to another document
    ("See Revised Degree Audit RDA-2026-0045"), follow it to the most recent
    version of that document and read the same field there (recursively)."""
    here = f"{doc.doc_id} v{doc.version}"
    raw = doc.fields.get(fld)
    if is_empty(raw):
        return ResolvedValue(fld, None, doc.doc_id, doc.version, [here], "MISSING")
    refs = [r for r in extract_doc_refs(raw) if r != doc.doc_id]
    if not refs:
        return ResolvedValue(fld, raw, doc.doc_id, doc.version, [here], "DIRECT" if _depth == 0 else "INDIRECT")
    ref = refs[0]
    if ref in _seen or _depth >= MAX_REF_DEPTH:
        return ResolvedValue(fld, None, doc.doc_id, doc.version, [here, ref], "UNRESOLVED",
                             note=f"circular or too-deep reference chain at {ref}")
    versions = store.versions(ref)
    if not versions:
        return ResolvedValue(fld, None, doc.doc_id, doc.version, [here, ref], "UNRESOLVED",
                             note=f"referenced document {ref} not found in inbox")
    target = versions[-1]
    sub = resolve_field(store, target, fld, _depth=_depth + 1, _seen=_seen + (doc.doc_id,))
    sub.chain = [here] + sub.chain
    if not sub.versions_considered:
        sub.versions_considered = [v.summary() for v in versions]
    if sub.status == "MISSING":
        sub.status, sub.note = "UNRESOLVED", f"{ref} does not state '{fld}'"
    elif sub.status == "DIRECT":
        sub.status = "INDIRECT"
    if len(versions) > 1:
        sub.note = (sub.note + "; " if sub.note else "") + \
            f"{len(versions)} versions of {ref}; selected v{target.version} ({target.status or 'ORIGINAL'}, issued {target.issue_date})"
    return sub


# --------------------------------------------------------------------------- identity evolution
@dataclass
class IdentityResolution:
    status: str                  # EXACT | ALIAS | NAME_CHANGE_VERIFIED | ID_MATCH | FUZZY | CONFLICT | NOT_FOUND
    document_name: str | None
    registry_name: str | None
    current_legal_name: str | None
    evidence: list[str] = field(default_factory=list)
    similarity: float | None = None
    dob_match: bool | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def is_verified(self) -> bool:
        return self.status in ("EXACT", "ALIAS", "NAME_CHANGE_VERIFIED", "ID_MATCH")

    def to_dict(self):
        return asdict(self)


def _name_change_links(store: DocumentStore, student_id: str, dob: str | None) -> list[tuple[str, str, Document]]:
    links = []
    for nc in store.find(doc_type="NAME_CHANGE"):
        f = nc.fields
        if f.get("student_id") and f["student_id"] != student_id:
            continue
        if dob and f.get("dob") and parse_date(f["dob"]) != parse_date(dob):
            continue
        if f.get("former_name") and f.get("new_name"):
            links.append((f["former_name"], f["new_name"], nc))
    return links


def resolve_identity(store: DocumentStore, doc: Document, rec: RegistryRecord | None) -> IdentityResolution:
    """Reconcile names. The document's *current* name is the name in an in-document change clause
    ("record of Margaret A. Reyes (formerly recorded as Whitcombe)") if present, else Student Name."""
    f = doc.fields
    doc_name = f.get("name")
    current = f.get("new_name") or doc_name or f.get("certified_name")
    if rec is None:
        return IdentityResolution("NOT_FOUND", doc_name, None, None,
                                  notes=[f"student id {f.get('student_id')} not in registry"])
    reg_name = rec.get("name")
    dob_match = None
    if f.get("dob") and rec.get("dob"):
        dob_match = parse_date(f["dob"]) == parse_date(rec.get("dob"))
    res = IdentityResolution("CONFLICT", doc_name, reg_name, None, dob_match=dob_match)
    if dob_match is False:
        res.notes.append("date of birth does not match registry")
        return res

    # internal consistency: certification line must name the same person as the header (or explain why not)
    cert = f.get("certified_name")
    if cert and doc_name and norm_name(cert) != norm_name(doc_name) and not f.get("former_name"):
        res.notes.append(f"document is internally inconsistent: header name '{doc_name}' vs certified name '{cert}'")
        return res
    in_doc_change = bool(f.get("former_name") and f.get("new_name"))
    if in_doc_change:
        res.evidence.append(f"{doc.doc_id} certification clause: '{f['former_name']}' -> '{f['new_name']}'")

    if not reg_name:
        # The registry holds no name (e.g. only student_id + audit_status): identity is established by the
        # student id; the current legal name comes from the document, including any in-document change clause.
        res.status, res.current_legal_name = "ID_MATCH", current
        res.evidence.append("registry has no name on file; identity matched on student id")
        if in_doc_change:
            res.notes.append(f"current legal name '{current}' differs from historical name '{f['former_name']}'")
        return res
    if norm_name(current) == norm_name(reg_name):
        res.status, res.current_legal_name = "EXACT", reg_name
        if in_doc_change and norm_name(doc_name) != norm_name(current):
            res.notes.append(f"header still shows former name '{doc_name}'; registry already holds current name")
        return res
    if any(norm_name(current) == norm_name(a) or norm_name(doc_name) == norm_name(a) for a in rec.aliases):
        res.status, res.current_legal_name = "ALIAS", reg_name
        res.evidence.append("matched registry known_aliases")
        return res

    # Walk name-change evidence (may be a chain A -> B -> C) from the registry name to the current name.
    links = _name_change_links(store, rec["student_id"], rec.get("dob"))
    if in_doc_change:
        links.append((f["former_name"], f["new_name"], doc))
    referenced = set(extract_doc_refs(f.get("name_change_ref", "")))
    frontier, path, visited = norm_name(reg_name), [], set()
    while frontier and frontier not in visited:
        visited.add(frontier)
        nxt = next((l for l in links if norm_name(l[0]) == frontier), None)
        if not nxt:
            break
        path.append(nxt)
        frontier = norm_name(nxt[1])
        if frontier == norm_name(current):
            res.status, res.current_legal_name = "NAME_CHANGE_VERIFIED", current
            res.evidence = [f"{nc.doc_id}: '{a}' -> '{b}'" + (f" (effective {nc.fields['effective_date']})"
                                                             if nc.fields.get("effective_date") else "")
                            for a, b, nc in path]
            unref = [nc.doc_id for _, _, nc in path if referenced and nc.doc_id not in referenced]
            if unref:
                res.notes.append(f"name change evidence found but not referenced by transcript: {unref}")
            return res
    if referenced and not links:
        res.notes.append(f"transcript references {sorted(referenced)} but certificate not found in inbox")

    res.similarity = round(name_similarity(current or "", reg_name or ""), 3)
    if res.similarity >= FUZZY_NAME_THRESHOLD:
        res.status = "FUZZY"
        res.notes.append("names are near-identical (possible typo / transliteration); human confirmation required")
    else:
        res.notes.append("name differs from registry and no name-change evidence links them")
    return res


# --------------------------------------------------------------------------- calculated milestones
@dataclass
class DateResolution:
    document_value: str | None
    document_date: str | None
    derivation: str
    issues: list[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def resolve_conferral_date(raw: str | None, rec: RegistryRecord | None,
                           doc_term_end: str | None = None) -> DateResolution:
    """Turn the document's conferral statement into a concrete date. Static dates are
    parsed; relative statements are evaluated against the registry's term end date."""
    if is_empty(raw):
        return DateResolution(raw, None, "missing")
    d = parse_date(raw)
    if d:
        return DateResolution(raw, d.isoformat(), "static date on document")
    rule = parse_relative_date(raw)
    if not rule:
        return DateResolution(raw, None, "unparseable", [f"cannot interpret conferral statement '{raw}'"])
    issues = []
    if rule.anchor == "DOC_TERM_END":
        # anchored to the Final Term End Date stated on the document itself (validated separately vs registry)
        anchor = parse_date(doc_term_end) or (parse_date(rec.get("term_end_date")) if rec else None)
        if not anchor:
            return DateResolution(raw, None, rule.describe(), ["no Final Term End Date to evaluate relative rule"])
        src = "document Final Term End Date" if parse_date(doc_term_end) else "registry term_end_date (document silent)"
        return DateResolution(raw, rule.apply(anchor).isoformat(), f"{rule.describe()} applied to {src} {anchor}", issues)
    if rec is None or not parse_date(rec.get("term_end_date")):
        return DateResolution(raw, None, rule.describe(), ["term end date unavailable to evaluate relative rule"])
    if rule.anchor_term and rec.get("final_term") and rule.anchor_term.casefold() != rec.get("final_term").casefold():
        issues.append(f"document anchors to '{rule.anchor_term}' but registry final term is '{rec.get('final_term')}'")
    term_end: date = parse_date(rec.get("term_end_date"))
    return DateResolution(raw, rule.apply(term_end).isoformat(),
                          f"{rule.describe()} applied to registry term_end_date {term_end}", issues)
