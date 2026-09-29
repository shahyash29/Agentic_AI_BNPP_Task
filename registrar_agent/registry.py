"""Master Registry (source of truth) loader with a configurable column mapping.

Real registry exports rarely use our canonical column names, so every
canonical field accepts several header aliases. Override or extend them in
``config/registry_mapping.json`` without touching code.
"""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .normalize import parse_date, parse_relative_date, is_empty

# recognize different CSV column names for the same field.
DEFAULT_MAPPING: dict[str, list[str]] = {
    "student_id": ["student_id", "studentid", "student id", "emplid", "id"],
    "name": ["name_on_record", "legal_name", "student_name", "name", "full_name"],
    "aliases": ["known_aliases", "aliases", "previous_names", "former_names"],
    "dob": ["date_of_birth", "dob", "birth_date"],
    "degree": ["degree", "degree_name"],
    "major": ["major", "program", "plan"],
    "gpa": ["cumulative_gpa", "gpa", "cgpa"],
    "credits_earned": ["credits_earned", "earned_credits"],
    "credits_required": ["credits_required", "required_credits"],
    "final_term": ["final_term", "term", "completion_term"],
    "term_end_date": ["term_end_date", "term_end"],
    "conferral_rule": ["conferral_rule", "graduation_rule"],
    "conferral_date": ["conferral_date", "graduation_date"],
    "honors": ["honors", "latin_honors"],
    "audit_status": ["audit_status", "status"],
    "hold_reason": ["hold_reason", "hold_notes"],
    "last_audit_update": ["last_audit_update", "last_updated"],
}

# hold one student’s registry record and provide convenient ways to access its fields.
@dataclass
class RegistryRecord:
    raw: dict[str, str]

    def __getitem__(self, k):
        return self.raw.get(k)

    def get(self, k, default=None):
        v = self.raw.get(k)
        return default if is_empty(v) else v

    @property
    def aliases(self) -> list[str]:
        return [a.strip() for a in (self.raw.get("aliases") or "").split("|") if a.strip()]

    # determine the expected conferral date using this student’s registry data.
    def expected_conferral_date(self) -> tuple[date | None, str, list[str]]:
        """Return (date, derivation, issues). Handles FIXED dates and TERM_END+N[B]D rules."""
        issues: list[str] = []
        rule_txt = (self.get("conferral_rule") or "FIXED").strip()
        static = parse_date(self.get("conferral_date"))
        if rule_txt.upper() == "FIXED":
            if not static:
                issues.append("registry conferral_rule is FIXED but conferral_date is empty/unparseable")
            return static, "registry fixed date", issues
        rule = parse_relative_date(rule_txt)
        term_end = parse_date(self.get("term_end_date"))
        if not rule:
            issues.append(f"unrecognised registry conferral_rule '{rule_txt}'")
            return static, "registry fixed date (rule unparseable)", issues
        if not term_end:
            issues.append("registry conferral rule needs term_end_date, which is missing")
            return None, rule.describe(), issues
        computed = rule.apply(term_end)
        if static and static != computed:
            issues.append(f"registry internal inconsistency: conferral_date {static} != rule {rule.describe()} -> {computed}")
        return computed, f"{rule.describe()} from term end {term_end}", issues

# hold the entire loaded registry.
class Registry:
    def __init__(self, records: dict[str, RegistryRecord], source: str, sha256: str, mapping_used: dict):
        self.records, self.source, self.sha256, self.mapping_used = records, source, sha256, mapping_used

    @classmethod
    def load(cls, path: Path, mapping_file: Path | None = None) -> "Registry":
        mapping = {k: list(v) for k, v in DEFAULT_MAPPING.items()}
        if mapping_file and Path(mapping_file).exists():
            for k, v in json.loads(Path(mapping_file).read_text()).items():
                if k.startswith("_"):
                    continue
                mapping[k] = list(v) + mapping.get(k, [])
        raw = Path(path).read_bytes()
        reader = csv.DictReader(raw.decode("utf-8-sig").splitlines())
        headers = {h.strip().lower(): h for h in reader.fieldnames or []}
        resolved = {}
        for canonical, aliases in mapping.items():
            for a in aliases:
                if a.lower() in headers:
                    resolved[canonical] = headers[a.lower()]
                    break
        if "student_id" not in resolved:
            raise ValueError(f"Registry {path} has no recognisable student id column; headers={list(headers)}")
        records = {}
        for row in reader:
            rec = {c: (row.get(src) or "").strip() for c, src in resolved.items()}
            records[rec["student_id"]] = RegistryRecord(rec)
        return cls(records, str(path), hashlib.sha256(raw).hexdigest(), resolved)

    def get(self, student_id: str | None) -> RegistryRecord | None:
        return self.records.get((student_id or "").strip())

