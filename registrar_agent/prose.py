"""Extractor for narrative registrar documents (the institution's real format).

These documents have no header block or document ids. Facts are embedded in prose:

    Section 2 (Academic Record): Cumulative GPA at the close of the final term of enrollment: 3.61, ...
    Section 4 (Degree Conferral): The Degree Conferral Date for this student shall be the date
        sixty (60) days after the Final Term End Date set forth in Section 3.
    ... official academic record of Margaret A. Reyes (formerly recorded as Whitcombe) ...

Secondary documents (conferral / accreditation certificates) are linked by student id, so a
synthetic id "<TYPE>:<STUDENT_ID>" groups every version of the same certificate together.
"""
from __future__ import annotations

import re

from .documents import Document

TITLE_TYPES = [
    ("OFFICIAL ACADEMIC TRANSCRIPT", "TRANSCRIPT", "TR"),
    ("ACADEMIC TRANSCRIPT", "TRANSCRIPT", "TR"),
    ("DEGREE CONFERRAL CERTIFICATE", "CONFERRAL_CERTIFICATE", "DCC"),
    ("INSTITUTIONAL ACCREDITATION CERTIFICATE", "ACCREDITATION_CERTIFICATE", "ACC"),
    ("NAME CHANGE CERTIFICATE", "NAME_CHANGE", "NC"),
    ("REVISED DEGREE AUDIT", "REVISED_AUDIT", "RDA"),
]
# phrases in a primary document that defer a value to a secondary document
REFERENCE_PHRASES = [
    (re.compile(r"as recorded in the Degree Conferral Certificate", re.I), "DCC"),
    (re.compile(r"as (?:stated|recorded) (?:in|on) the (?:Revised )?Degree Audit", re.I), "RDA"),
]
ISO = r"(\d{4}-\d{2}-\d{2})"
SID = r"([A-Z]{2,4}-\d{3,})"

_P = {
    "student_id": [re.compile(rf"Student ID[:\s]+{SID}"), re.compile(rf"Issued for:\s*{SID}")],
    "name": [re.compile(r"Student Name:\s*(.+)"), re.compile(r"Issued for:\s*([^(\n]+?)\s*\(Student ID")],
    "institution": [re.compile(r"Institution:\s*([^,\n]+)")],
    "degree_program": [re.compile(r"Degree Program:\s*(.+)")],
    "credits_earned": [re.compile(r"based on (\d+) credit hours"), re.compile(r"Credit hours attempted to date:\s*(\d+)")],
    "term_end_date": [re.compile(rf"final term end date for this student is {ISO}", re.I)],
    "accreditation_ref": [re.compile(r"reference number ([A-Z]+-\d+(?:-[A-Z0-9]+)?)")],
    "accrediting_body": [re.compile(r"Accrediting Body:\s*(.+)")],
}
_GPA_NUM = re.compile(r"Cumulative GPA[^:\n]*:\s*(\d\.\d{1,3})")
_GPA_PENDING = re.compile(r"Cumulative GPA[^:\n]*:\s*((?:to be|not yet|pending)[^.\n]*)", re.I)
_CONF_STATIC = re.compile(rf"Degree Conferral Date (?:for this student )?is (?:set as )?{ISO}", re.I)
_CONF_RULE = re.compile(r"Degree Conferral Date for this student shall be (the date .+?)(?:\.\s|\.$)", re.I | re.S)
_CERTIFIED = re.compile(r"official academic record of (.+?)(?:\s*\(formerly (?:recorded as|known as) ([^)]+)\))? by the", re.I)
_ISSUED = re.compile(rf"(?:Certifying Officer|Issued by)[^\n]*?{ISO}\.?\s*$", re.I | re.M)
_PRIOR_ISSUE = re.compile(rf"certificate issued on {ISO}", re.I)
_STATED_CHANGE = re.compile(r"a change of ([a-z-]+|\d+)(?: \((\d+)\))? days", re.I)
_GRADE_CORRECTION = re.compile(rf"grade correction posted for ([A-Z]{{2,5}} ?\d{{2,4}}) on {ISO}", re.I)


def _first(patterns, text):
    for p in patterns:
        m = p.search(text)
        if m:
            return m.group(1).strip()
    return None


def _former_full_name(current: str, former: str) -> str:
    """'Margaret A. Reyes' + 'Whitcombe' -> 'Margaret A. Whitcombe' (surname-only former names)."""
    former = former.strip()
    if " " in former:
        return former
    parts = current.split()
    return " ".join(parts[:-1] + [former]) if len(parts) > 1 else former


def parse_prose(doc: Document) -> Document:
    text = doc.text
    title = next((l.strip() for l in text.splitlines() if l.strip()), "").upper()
    base = re.sub(r"\s*\((REVISED|CORRECTED|AMENDED|REISSUED)\)\s*$", "", title)
    m = re.search(r"\((REVISED|CORRECTED|AMENDED|REISSUED)\)", title)
    doc.status = "CORRECTED" if m else None
    abbr = "DOC"
    for t, dtype, a in TITLE_TYPES:
        if base.startswith(t):
            doc.doc_type, abbr = dtype, a
            break

    f: dict[str, str] = {}
    for fld, pats in _P.items():
        v = _first(pats, text)
        if v:
            f[fld] = v
    if f.get("degree_program"):
        dm = re.match(r"^(.+?)\s+in\s+(.+)$", f["degree_program"])
        if dm:
            f["degree"], f["major"] = dm.group(1).strip(), dm.group(2).strip()
        else:
            f["degree"] = f["degree_program"]

    g = _GPA_NUM.search(text)
    if g:
        f["gpa"] = g.group(1)
    else:
        gp = _GPA_PENDING.search(text)
        if gp:
            f["gpa"] = "PENDING: " + re.sub(r"\s+", " ", gp.group(1)).strip()

    sid = f.get("student_id")
    c = _CONF_STATIC.search(text)
    if c:
        f["conferral_date"] = c.group(1)
    else:
        r = _CONF_RULE.search(text)
        if r:
            f["conferral_date"] = re.sub(r"\s+", " ", r.group(1)).strip()
    for pat, ref_abbr in REFERENCE_PHRASES:
        if pat.search(text) and sid:
            f["conferral_date" if ref_abbr == "DCC" else "gpa"] = f"See {ref_abbr}:{sid}"

    cert = _CERTIFIED.search(text)
    if cert:
        f["certified_name"] = cert.group(1).strip()
        if cert.group(2):
            f["former_name"] = _former_full_name(cert.group(1).strip(), cert.group(2))
            f["new_name"] = cert.group(1).strip()

    iss = _ISSUED.findall(text)
    if iss:
        doc.issue_date = iss[-1]
    prior = _PRIOR_ISSUE.search(text)
    if prior and doc.status:
        doc.supersedes = f"{abbr}:{sid} issued {prior.group(1)}"
        f["supersedes_issue_date"] = prior.group(1)
    ch = _STATED_CHANGE.search(text)
    if ch:
        f["stated_change_days"] = ch.group(2) or ch.group(1)
    gc = _GRADE_CORRECTION.search(text)
    if gc:
        f["grade_correction_course"], f["grade_correction_date"] = gc.group(1), gc.group(2)

    doc.fields = f
    doc.doc_id = f"{abbr}:{sid}" if sid else None
    doc.extraction_method = "prose-pattern"
    return doc
