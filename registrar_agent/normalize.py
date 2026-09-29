"""Value normalisation and comparison helpers (dates, names, GPA, degrees, honors)."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

DATE_FORMATS = ["%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%m/%d/%Y", "%d-%b-%Y", "%Y/%m/%d"]

# classic ids (RDA-2026-0045) and synthetic student-linked ids for narrative documents (DCC:STU-5004)
DOC_REF_RE = re.compile(r"\b([A-Z]{2,4}-\d{4}-\d{4}|[A-Z]{2,4}:[A-Z]{2,4}-\d{3,})\b")
EMPTY_VALUES = {"", "none", "n/a", "na", "-", "null"}

DEGREE_SYNONYMS = {
    "ba": "bachelor of arts", "b.a.": "bachelor of arts", "ab": "bachelor of arts",
    "bs": "bachelor of science", "b.s.": "bachelor of science", "bsc": "bachelor of science",
    "be": "bachelor of engineering", "b.e.": "bachelor of engineering", "beng": "bachelor of engineering",
    "ms": "master of science", "m.s.": "master of science", "msc": "master of science",
    "ma": "master of arts", "m.a.": "master of arts", "mba": "master of business administration",
    "phd": "doctor of philosophy", "ph.d.": "doctor of philosophy",
}


def is_empty(v) -> bool:
    return v is None or str(v).strip().lower() in EMPTY_VALUES


def parse_date(v) -> date | None:
    if isinstance(v, date):
        return v
    if is_empty(v):
        return None
    s = re.sub(r"\s+", " ", str(v).strip())
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def norm_name(v: str | None) -> str:
    if not v:
        return ""
    s = unicodedata.normalize("NFKD", v).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z\s-]", "", s).replace("-", " ")
    return re.sub(r"\s+", " ", s).strip().casefold()


def name_similarity(a: str, b: str) -> float:
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return 0.0
    # order-insensitive token comparison handles "Rossi, Elena" vs "Elena Rossi"
    ta, tb = " ".join(sorted(a.split())), " ".join(sorted(b.split()))
    return max(SequenceMatcher(None, a, b).ratio(), SequenceMatcher(None, ta, tb).ratio())


def norm_degree(v: str | None) -> str:
    if is_empty(v):
        return ""
    s = re.sub(r"\s+", " ", str(v).strip().casefold())
    key = s.replace(" ", "")
    return DEGREE_SYNONYMS.get(key, DEGREE_SYNONYMS.get(s, s))


def norm_honors(v: str | None) -> str:
    if is_empty(v):
        return ""
    s = str(v).casefold().replace("with", " ")
    return re.sub(r"\s+", " ", s).strip()


def norm_text(v) -> str:
    return "" if is_empty(v) else re.sub(r"\s+", " ", str(v).strip().casefold())


def parse_float(v) -> float | None:
    if is_empty(v):
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m else None


def extract_doc_refs(v) -> list[str]:
    return DOC_REF_RE.findall(str(v or ""))


# --------------------------------------------------------------------------- relative dates
@dataclass(frozen=True)
class RelativeDateRule:
    offset: int
    unit: str            # "calendar" | "business"
    anchor: str          # "TERM_END" (registry term end) | "DOC_TERM_END" (term end stated on the document)
    anchor_term: str | None = None   # e.g. "Spring 2026" when the text names the term

    def apply(self, anchor_date: date) -> date:
        if self.unit == "calendar":
            return anchor_date + timedelta(days=self.offset)
        step = 1 if self.offset >= 0 else -1
        d, remaining = anchor_date, abs(self.offset)
        while remaining:
            d += timedelta(days=step)
            if d.weekday() < 5:
                remaining -= 1
        return d

    def describe(self) -> str:
        return f"{self.anchor}{'+' if self.offset >= 0 else ''}{self.offset}{'BD' if self.unit == 'business' else 'D'}"


_REL_TEXT_RE = re.compile(
    r"(?P<n>\d+)\s+(?P<unit>calendar\s+|business\s+|working\s+)?days?\s+"
    r"(?P<dir>after|following|from|past|before|prior to)\s+"
    r"(?:the\s+)?(?:end|close|conclusion)\s+of\s+(?:the\s+)?(?P<term>.*?)\s*(?:term|semester|session)?\s*\.?$",
    re.IGNORECASE,
)
# "the date sixty (60) days after the Final Term End Date set forth in Section 3"
_REL_FTED_RE = re.compile(
    r"(?:(?P<word>[a-z-]+)\s+)?\(?(?P<n>\d+)?\)?\s*(?P<unit>calendar\s+|business\s+|working\s+)?days?\s+"
    r"(?P<dir>after|following|from|before|prior to)\s+the\s+final\s+term\s+end\s+date",
    re.IGNORECASE,
)
WORD_NUMBERS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())}
WORD_NUMBERS.update({"thirty": 30, "forty": 40, "forty-five": 45, "fifty": 50, "sixty": 60, "seventy": 70,
                     "seventy-five": 75, "eighty": 80, "ninety": 90, "hundred": 100, "one hundred": 100})
_REL_CODE_RE = re.compile(r"^TERM_END\s*(?P<sign>[+-])\s*(?P<n>\d+)\s*(?P<unit>BD|D)$", re.IGNORECASE)


def parse_relative_date(v) -> RelativeDateRule | None:
    """Parse natural-language ("30 days following the end of the Spring 2026 term")
    or coded ("TERM_END+30D", "TERM_END+10BD") relative date rules."""
    if is_empty(v):
        return None
    s = str(v).strip()
    m = _REL_CODE_RE.match(s)
    if m:
        n = int(m["n"]) * (-1 if m["sign"] == "-" else 1)
        return RelativeDateRule(n, "business" if m["unit"].upper() == "BD" else "calendar", "TERM_END")
    m = _REL_FTED_RE.search(s)
    if m:
        word_n = WORD_NUMBERS.get((m["word"] or "").lower())
        n = int(m["n"]) if m["n"] else word_n
        if n is None:
            return None
        if m["n"] and word_n is not None and word_n != int(m["n"]):
            return None                         # "sixty (50) days" - internally inconsistent, refuse to guess
        n *= -1 if m["dir"].lower() in ("before", "prior to") else 1
        unit = "business" if (m["unit"] or "").strip().lower() in ("business", "working") else "calendar"
        return RelativeDateRule(n, unit, "DOC_TERM_END")
    m = _REL_TEXT_RE.search(s)
    if m:
        n = int(m["n"]) * (-1 if m["dir"].lower() in ("before", "prior to") else 1)
        unit = "business" if (m["unit"] or "").strip().lower() in ("business", "working") else "calendar"
        term = (m["term"] or "").strip() or None
        return RelativeDateRule(n, unit, "TERM_END", term.title() if term else None)
    return None
