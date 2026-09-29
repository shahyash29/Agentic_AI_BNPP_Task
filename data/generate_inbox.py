"""Generates the synthetic document inbox used for demos and tests.

Each document mimics OCR'd text output from a scanned registrar document:
a header block (type / id / version / issue date) followed by labelled fields
and, for transcripts, course lines that the extractor must ignore.
"""
from pathlib import Path

INBOX = Path(__file__).parent / "demo_inbox"

COURSES = """
--- COURSE HISTORY (abridged) ---
CS 101   Intro to Programming          4.0  A
MATH 221 Linear Algebra                3.0  A-
WRIT 110 Academic Writing              3.0  B+
--- END OF COURSE HISTORY ---
"""


def transcript(doc_id, issued, sid, name, dob, degree, major, gpa, credits, term,
               conferral, honors="", extra=""):
    return f"""UNIVERSITY OF WESTBROOK - OFFICE OF THE REGISTRAR
DOCUMENT TYPE: OFFICIAL TRANSCRIPT
DOCUMENT ID: {doc_id}
ISSUE DATE: {issued}

Student Name: {name}
Student ID: {sid}
Date of Birth: {dob}
Degree: {degree}
Major: {major}
Cumulative GPA: {gpa}
Credits Earned: {credits}
Final Term: {term}
Degree Conferral Date: {conferral}
Honors: {honors or 'None'}
{extra}{COURSES}
This transcript is official only when bearing the registrar's seal.
"""


DOCS = {
    # 1. Clean record -> COMMIT
    "TR-2026-1001.txt": transcript("TR-2026-1001", "2026-06-02", "S1001", "Aarav Mehta", "2002-03-14",
                                   "Bachelor of Science", "Computer Science", "3.78", "124", "Spring 2026",
                                   "May 30, 2026", "Magna Cum Laude"),
    # 2. Identity evolution: current legal name differs from registry; resolved via name-change certificate
    "TR-2026-1002.txt": transcript("TR-2026-1002", "2026-06-05", "S1002", "Priya Raman", "2001-11-02",
                                   "B.A.", "Economics", "3.41", "121", "Spring 2026", "2026-05-30", "",
                                   extra="Name Change Reference: See Name Change Certificate NC-2025-0112\n"),
    "NC-2025-0112.txt": """COUNTY OF WESTBROOK - CIVIL RECORDS
DOCUMENT TYPE: NAME CHANGE CERTIFICATE
DOCUMENT ID: NC-2025-0112
ISSUE DATE: 2025-09-14

Student ID: S1002
Date of Birth: 2001-11-02
Former Name: Priya Iyer
New Legal Name: Priya Raman
Effective Date: 2025-09-01
""",
    # 3. Calculated milestone: conferral defined relative to term end
    "TR-2026-1003.txt": transcript("TR-2026-1003", "2026-06-20", "S1003", "Marcus Chen", "2002-07-21",
                                   "Bachelor of Engineering", "Mechanical Engineering", "3.12", "132",
                                   "Spring 2026", "30 days following the end of the Spring 2026 term"),
    # 4. Version control: transcript defers honors to a certificate that was reissued (corrected)
    "TR-2026-1004.txt": transcript("TR-2026-1004", "2026-06-11", "S1004", "Elena M. Rossi", "2002-01-09",
                                   "Bachelor of Science", "Biology", "3.71", "126", "Spring 2026",
                                   "2026-05-30", "As stated on Degree Certificate DC-2026-0301"),
    # original (wrong honors) - file name deliberately sorts AFTER the correction
    "DC-2026-0301_z_original.txt": """UNIVERSITY OF WESTBROOK
DOCUMENT TYPE: DEGREE CERTIFICATE
DOCUMENT ID: DC-2026-0301
VERSION: 1
ISSUE DATE: 2026-06-01

Student Name: Elena Rossi
Student ID: S1004
Degree Awarded: Bachelor of Science
Honors: Cum Laude
Date Conferred: 2026-05-30
""",
    "DC-2026-0301_a_reissue.txt": """UNIVERSITY OF WESTBROOK
DOCUMENT TYPE: DEGREE CERTIFICATE
DOCUMENT ID: DC-2026-0301
VERSION: 2
STATUS: CORRECTED
SUPERSEDES: DC-2026-0301 v1
ISSUE DATE: 2026-06-10

Student Name: Elena Rossi
Student ID: S1004
Degree Awarded: Bachelor of Science
Honors: Magna Cum Laude
Date Conferred: 2026-05-30
Correction Note: Honors designation corrected following grade appeal.
""",
    # 5. Indirect truth (GPA in revised audit) + PENDING status -> FOLLOW_UP
    "TR-2026-1005.txt": transcript("TR-2026-1005", "2026-08-11", "S1005", "Jamal Carter", "2001-09-30",
                                   "Bachelor of Arts", "Psychology", "See Revised Degree Audit RDA-2026-0045",
                                   "118", "Summer 2026", "14 days after the end of the Summer 2026 term"),
    "RDA-2026-0045.txt": """UNIVERSITY OF WESTBROOK - DEGREE AUDIT OFFICE
DOCUMENT TYPE: REVISED DEGREE AUDIT
DOCUMENT ID: RDA-2026-0045
ISSUE DATE: 2026-08-10

Student ID: S1005
Student Name: Jamal Carter
Final Cumulative GPA: 3.62
Credits Earned: 118
Audit Note: GPA recalculated after incomplete in PSYC 410 converted to letter grade.
""",
    # 6. Administrative hold -> ESCALATE even though data matches
    "TR-2026-1006.txt": transcript("TR-2026-1006", "2026-06-02", "S1006", "Sofia Alvarez", "2002-05-18",
                                   "Bachelor of Science", "Chemistry", "3.55", "122", "Spring 2026",
                                   "2026-05-30", "Cum Laude"),
    # 7. Data conflict (GPA) -> ESCALATE
    "TR-2026-1007.txt": transcript("TR-2026-1007", "2026-06-02", "S1007", "Liam O'Brien", "2001-12-11",
                                   "Bachelor of Arts", "History", "3.94", "120", "Spring 2026", "2026-05-30"),
    # 8. Identity conflict, no supporting name-change document -> ESCALATE
    "TR-2026-1008.txt": transcript("TR-2026-1008", "2026-06-02", "S1008", "Daniel Okafor", "2002-02-27",
                                   "Bachelor of Science", "Mathematics", "3.88", "124", "Spring 2026",
                                   "2026-05-30", "Summa Cum Laude"),
    # 9. Business-day relative date + committee pending -> FOLLOW_UP
    "TR-2026-1009.txt": transcript("TR-2026-1009", "2026-08-12", "S1009", "Hana Sato", "2002-10-05",
                                   "M.S.", "Data Science", "3.90", "36", "Summer 2026",
                                   "10 business days after the end of the Summer 2026 term"),
    # 10. Broken indirect reference (document never received) -> ESCALATE
    "TR-2026-1010.txt": transcript("TR-2026-1010", "2026-06-02", "S1010", "Noah Williams", "2001-04-16",
                                   "Bachelor of Science", "Physics", "Refer to Revised Degree Audit RDA-2026-0099",
                                   "123", "Spring 2026", "2026-05-30"),
    # 11. Disciplinary hold -> ESCALATE
    "TR-2026-1011.txt": transcript("TR-2026-1011", "2026-06-02", "S1011", "Grace Kim", "2002-08-19",
                                   "Bachelor of Arts", "English", "3.66", "120", "Spring 2026",
                                   "2026-05-30", "Cum Laude"),
    # 12. Student not in registry -> ESCALATE
    "TR-2026-9999.txt": transcript("TR-2026-9999", "2026-06-02", "S9999", "Unknown Person", "2000-01-01",
                                   "Bachelor of Arts", "Art History", "3.10", "120", "Spring 2026", "2026-05-30"),
}


def main(target: Path = INBOX) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    for name, body in DOCS.items():
        (target / name).write_text(body)
    return target


if __name__ == "__main__":
    print(f"Wrote {len(DOCS)} documents to {main()}")
