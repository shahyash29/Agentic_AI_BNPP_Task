"""Document ingestion: parsing, classification, field extraction and version control."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import parse_date, extract_doc_refs

# canonical field -> label synonyms seen on real-world documents
FIELD_LABELS: dict[str, list[str]] = {
    "student_id": ["student id", "student number", "id number", "student no"],
    "name": ["student name", "legal name", "current legal name", "name"],
    "dob": ["date of birth", "dob", "birth date"],
    "degree": ["degree awarded", "degree"],
    "major": ["major", "field of study", "major/concentration", "program of study"],
    "gpa": ["final cumulative gpa", "cumulative gpa", "cgpa", "gpa"],
    "credits_earned": ["credits earned", "total credits", "earned credits"],
    "final_term": ["final term", "completion term", "term"],
    "conferral_date": ["degree conferral date", "conferral date", "date conferred", "graduation date"],
    "honors": ["latin honors", "honors", "distinction"],
    "former_name": ["former name", "previous legal name", "name before change"],
    "new_name": ["new legal name", "name after change", "new name"],
    "effective_date": ["effective date"],
    "name_change_ref": ["name change reference", "previous name(s)", "prior names"],
}
_LABEL_TO_FIELD = {lbl: f for f, lbls in FIELD_LABELS.items() for lbl in lbls}

DOC_TYPES = {
    "OFFICIAL TRANSCRIPT": "TRANSCRIPT",
    "TRANSCRIPT": "TRANSCRIPT",
    "DEGREE CERTIFICATE": "DEGREE_CERTIFICATE",
    "DIPLOMA": "DEGREE_CERTIFICATE",
    "REVISED DEGREE AUDIT": "REVISED_AUDIT",
    "DEGREE AUDIT": "DEGREE_AUDIT",
    "NAME CHANGE CERTIFICATE": "NAME_CHANGE",
    "DEED POLL": "NAME_CHANGE",
}
PRIMARY_TYPES = {"TRANSCRIPT"}

_HEADER_RE = re.compile(r"^\s*(DOCUMENT TYPE|DOCUMENT ID|VERSION|STATUS|SUPERSEDES|ISSUE DATE)\s*:\s*(.+?)\s*$",
                        re.IGNORECASE | re.MULTILINE)
_FIELD_RE = re.compile(r"^\s*([A-Za-z][A-Za-z /()'.-]{1,40}?)\s*:\s*(.*?)\s*$", re.MULTILINE)


@dataclass
class Document:
    path: str
    sha256: str
    text: str
    doc_type: str = "UNKNOWN"
    doc_id: str | None = None
    version: int = 1
    status: str | None = None
    supersedes: str | None = None
    issue_date: str | None = None
    fields: dict[str, str] = field(default_factory=dict)
    extraction_method: str = "pattern"

    @property
    def is_primary(self) -> bool:
        return self.doc_type in PRIMARY_TYPES

    def summary(self) -> dict:
        return {"doc_id": self.doc_id, "doc_type": self.doc_type, "version": self.version, "status": self.status,
                "issue_date": self.issue_date, "path": self.path, "sha256": self.sha256[:16],
                "extraction_method": self.extraction_method}


SEGMENT_SPLIT_RE = re.compile(r"^\s*={5,}\s*$", re.MULTILINE)


def parse_file(path: Path) -> list[Document]:
    """A file may hold several documents separated by '=====' lines (e.g. STU-5014-5015.txt)."""
    text = path.read_bytes().decode("utf-8", errors="replace")
    segments = [s for s in SEGMENT_SPLIT_RE.split(text) if s.strip()]
    docs = []
    for i, seg in enumerate(segments, 1):
        loc = f"{path}#{i}" if len(segments) > 1 else str(path)
        docs.append(parse_document(path, seg.strip() + "\n", loc))
    return docs


def parse_document(path: Path, text: str | None = None, location: str | None = None) -> Document:
    if text is None:
        text = path.read_bytes().decode("utf-8", errors="replace")
    doc = Document(path=location or str(path), sha256=hashlib.sha256(text.encode()).hexdigest(), text=text)
    if not _HEADER_RE.search(text):
        from .prose import parse_prose          # narrative registrar documents (no header block)
        return parse_prose(doc)
    header = {k.upper(): v for k, v in _HEADER_RE.findall(text)}
    doc.doc_type = DOC_TYPES.get(header.get("DOCUMENT TYPE", "").upper(), "UNKNOWN")
    doc.doc_id = header.get("DOCUMENT ID") or (extract_doc_refs(path.stem) or [None])[0]
    if header.get("VERSION", "").strip().isdigit():
        doc.version = int(header["VERSION"])
    doc.status = header.get("STATUS", "").upper() or None
    doc.supersedes = header.get("SUPERSEDES")
    d = parse_date(header.get("ISSUE DATE"))
    doc.issue_date = d.isoformat() if d else None
    doc.fields = extract_fields(text)
    return doc


def extract_fields(text: str) -> dict[str, str]:
    """Pattern-based extraction of labelled fields. First occurrence wins."""
    out: dict[str, str] = {}
    for label, value in _FIELD_RE.findall(text):
        f = _LABEL_TO_FIELD.get(label.strip().lower())
        if f and f not in out and value:
            out[f] = value
    return out


def version_sort_key(doc: Document):
    """Most authoritative version sorts LAST. Order of precedence:
    void documents lose; explicit version number; CORRECTED/REISSUED status; issue date."""
    void = 1 if doc.status in ("VOID", "REVOKED", "SUPERSEDED") else 0
    corrected = 1 if doc.status in ("CORRECTED", "REISSUED", "AMENDED") else 0
    return (-void, doc.version, corrected, doc.issue_date or "")


class DocumentStore:
    """In-memory index of an inbox, grouped by document id with all versions retained."""

    def __init__(self, docs: list[Document]):
        self.docs = docs
        self.by_id: dict[str, list[Document]] = {}
        for d in docs:
            if d.doc_id:
                self.by_id.setdefault(d.doc_id, []).append(d)
        for versions in self.by_id.values():
            versions.sort(key=version_sort_key)
            # narrative documents carry no version numbers: number them by authority order (original = v1)
            if len(versions) > 1 and all(v.version == 1 for v in versions):
                for i, v in enumerate(versions, 1):
                    v.version = i

    @classmethod
    def from_directory(cls, inbox: Path) -> "DocumentStore":
        files = sorted(p for p in Path(inbox).iterdir() if p.is_file() and p.suffix.lower() in (".txt", ".md"))
        return cls([d for p in files for d in parse_file(p)])

    def versions(self, doc_id: str) -> list[Document]:
        return list(self.by_id.get(doc_id, []))

    def latest(self, doc_id: str) -> Document | None:
        v = self.by_id.get(doc_id)
        return v[-1] if v else None

    def primaries(self) -> list[Document]:
        return [d for d in self.docs if d.is_primary]

    def find(self, *, doc_type: str | None = None, student_id: str | None = None) -> list[Document]:
        out = []
        for doc_id in self.by_id:
            d = self.latest(doc_id)
            if doc_type and d.doc_type != doc_type:
                continue
            if student_id and d.fields.get("student_id") != student_id:
                continue
            out.append(d)
        return out
