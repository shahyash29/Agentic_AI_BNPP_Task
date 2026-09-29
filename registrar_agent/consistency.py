"""Cross-document integrity checks that do not need the registry.

These catch problems *inside* the evidence before it is compared with the source of truth:
a revised certificate whose stated change doesn't match the dates, a correction that points at a
certificate we never received, a conferral date before the term ended, two different transcripts
for one student, secondary documents naming a different person or institution, and so on.

Severity: HIGH blocks autonomous action (escalate); WARN is recorded but not blocking; INFO is
reported by `inspect` only.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from .documents import Document, DocumentStore
from .normalize import WORD_NUMBERS, extract_doc_refs, norm_name, norm_text, parse_date
from .resolvers import resolve_conferral_date, resolve_field


@dataclass
class Finding:
    severity: str        # HIGH | WARN | INFO
    code: str
    student_id: str | None
    message: str
    documents: list[str]

    def to_dict(self):
        return asdict(self)


def _n(v) -> int | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    return int(s) if s.isdigit() else WORD_NUMBERS.get(s)


def _compatible_names(a: str | None, b: str | None) -> bool:
    """Same person if identical, or same first + last name and middle names are compatible ('Elena M. Rossi')."""
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return False
    if a == b:
        return True
    ta, tb = a.split(), b.split()
    if ta[0] != tb[0] or ta[-1] != tb[-1]:
        return False
    ma, mb = ta[1:-1], tb[1:-1]
    return not ma or not mb or all(x[0] == y[0] for x, y in zip(ma, mb))


def _secondaries(store: DocumentStore, sid: str) -> list[list[Document]]:
    return [vs for doc_id, vs in store.by_id.items()
            if vs and not vs[-1].is_primary and vs[-1].fields.get("student_id") == sid]


def check_versions(versions: list[Document]) -> list[Finding]:
    out: list[Finding] = []
    latest = versions[-1]
    sid = latest.fields.get("student_id")
    ids = [f"{v.doc_id} v{v.version} ({v.path.rsplit('/', 1)[-1]})" for v in versions]
    target = latest.fields.get("supersedes_issue_date")
    if latest.status and target and len(versions) > 1:
        if not any(v.issue_date == target for v in versions[:-1]):
            out.append(Finding("HIGH", "SUPERSEDED_VERSION_MISSING", sid,
                               f"{latest.doc_id} correction refers to a version issued {target}, which is not in the inbox",
                               ids))
    if latest.status and target and len(versions) == 1:
        out.append(Finding("WARN", "ORIGINAL_NOT_RECEIVED", sid,
                           f"{latest.doc_id} is a correction of a version issued {target} that was not received", ids))
    stated = _n(latest.fields.get("stated_change_days"))
    if stated is not None and len(versions) > 1:
        new, old = parse_date(latest.fields.get("conferral_date")), parse_date(versions[-2].fields.get("conferral_date"))
        if new and old and abs((new - old).days) != stated:
            out.append(Finding("HIGH", "REVISION_DELTA_MISMATCH", sid,
                               f"revised {latest.doc_id} says the date changed by {stated} days, "
                               f"but {old} -> {new} is {(new - old).days} days", ids))
    return out


def check_student(store: DocumentStore, primary: Document) -> list[Finding]:
    f = primary.fields
    sid = f.get("student_id")
    out: list[Finding] = []
    me = f"{primary.doc_id} ({primary.path.rsplit('/', 1)[-1]})"

    # 1. competing transcripts for the same student
    others = [d for d in store.primaries() if d is not primary and d.fields.get("student_id") == sid]
    if any(d.sha256 != primary.sha256 for d in others):
        out.append(Finding("HIGH", "DUPLICATE_TRANSCRIPT", sid,
                           f"{len(others) + 1} different transcripts found for {sid}",
                           [me] + [d.path.rsplit('/', 1)[-1] for d in others]))

    # 2. conferral must not precede the end of the final term
    rv = resolve_field(store, primary, "conferral_date")
    dres = resolve_conferral_date(rv.value, None, f.get("term_end_date"))
    conf, term_end = parse_date(dres.document_date), parse_date(f.get("term_end_date"))
    if conf and term_end and conf < term_end:
        out.append(Finding("HIGH", "CONFERRAL_BEFORE_TERM_END", sid,
                           f"conferral date {conf} is before the final term end date {term_end}", rv.chain))

    # 3. every secondary document about this student must describe the same person / institution
    for versions in _secondaries(store, sid):
        latest = versions[-1]
        out += check_versions(versions)
        if latest.fields.get("name") and not any(_compatible_names(latest.fields["name"], n) for n in
                                                 (f.get("name"), f.get("new_name"), f.get("certified_name"))):
            out.append(Finding("HIGH", "SECONDARY_NAME_MISMATCH", sid,
                               f"{latest.doc_id} is issued for '{latest.fields['name']}', transcript names "
                               f"'{f.get('name')}'", [me, latest.doc_id]))
        if (latest.fields.get("institution") and f.get("institution")
                and norm_text(latest.fields["institution"]) != norm_text(f["institution"])):
            out.append(Finding("HIGH", "SECONDARY_INSTITUTION_MISMATCH", sid,
                               f"{latest.doc_id} is from '{latest.fields['institution']}', transcript from "
                               f"'{f['institution']}'", [me, latest.doc_id]))
        # 4. a grade correction after transcript issue may make transcript GPA stale
        gc_date = latest.fields.get("grade_correction_date")
        if gc_date:
            out.append(Finding("WARN", "GRADE_CORRECTION_AFTER_TRANSCRIPT", sid,
                               f"{latest.doc_id} cites a grade correction in {latest.fields.get('grade_correction_course')} "
                               f"on {gc_date}; transcript GPA {f.get('gpa')} may predate it (registry GPA is authoritative)",
                               [me, latest.doc_id]))
        # 5. secondary document that the transcript never relies on
        referenced = {r for v in f.values() for r in extract_doc_refs(v)}
        if latest.doc_id not in referenced:
            out.append(Finding("INFO", "UNREFERENCED_SECONDARY", sid,
                               f"{latest.doc_id} ({latest.doc_type}) is on file but the transcript does not rely on it; "
                               f"ignored for verification", [latest.doc_id]))
    return out


def inspect_store(store: DocumentStore) -> list[Finding]:
    """Whole-inbox review, including documents that have no transcript to anchor them."""
    out: list[Finding] = []
    seen_primary_sids = set()
    for p in store.primaries():
        if not p.fields.get("student_id"):
            out.append(Finding("HIGH", "NO_STUDENT_ID", None, "transcript without a student id", [p.path]))
            continue
        if p.fields["student_id"] in seen_primary_sids:
            continue
        seen_primary_sids.add(p.fields["student_id"])
        out += check_student(store, p)
    for doc_id, versions in store.by_id.items():
        latest = versions[-1]
        sid = latest.fields.get("student_id")
        if latest.is_primary or sid in seen_primary_sids:
            continue
        out += check_versions(versions)
        out.append(Finding("WARN", "ORPHAN_SECONDARY", sid,
                           f"{doc_id} ({latest.doc_type}) has no transcript for {sid} in the inbox",
                           [v.path.rsplit('/', 1)[-1] for v in versions]))
    for d in store.docs:
        if d.doc_type == "UNKNOWN":
            out.append(Finding("WARN", "UNCLASSIFIED_DOCUMENT", d.fields.get("student_id"),
                               "document type not recognised", [d.path]))
    order = {"HIGH": 0, "WARN": 1, "INFO": 2}
    return sorted(out, key=lambda x: (order[x.severity], x.student_id or "", x.code))
