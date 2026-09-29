"""Operational persistence: decisions, verified records, review cases, follow-up tasks.

``decisions`` and ``verified_records`` are append-only (triggers). Record
corrections and revocations create new versions instead of editing old ones.
Review cases and tasks have mutable status columns, but every transition is
also written to the immutable audit trail.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, timedelta
from pathlib import Path

from .audit import AuditTrail, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY, run_id TEXT, doc_id TEXT, doc_sha256 TEXT, student_id TEXT,
    action TEXT, priority TEXT, decision_json TEXT, report_json TEXT, created_at TEXT);
CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions is append-only'); END;

CREATE TABLE IF NOT EXISTS verified_records (
    record_id TEXT PRIMARY KEY, student_id TEXT NOT NULL, version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','REVOKED')),
    record_json TEXT NOT NULL, source_doc_id TEXT, source_sha256 TEXT, decision_id TEXT,
    committed_by TEXT, committed_by_type TEXT, override INTEGER DEFAULT 0, note TEXT, created_at TEXT,
    UNIQUE(student_id, version));
CREATE TRIGGER IF NOT EXISTS records_no_update BEFORE UPDATE ON verified_records
BEGIN SELECT RAISE(ABORT, 'verified_records is append-only; create a new version'); END;
CREATE TRIGGER IF NOT EXISTS records_no_delete BEFORE DELETE ON verified_records
BEGIN SELECT RAISE(ABORT, 'verified_records is append-only; create a new version'); END;

CREATE TABLE IF NOT EXISTS review_cases (
    case_id TEXT PRIMARY KEY, decision_id TEXT, student_id TEXT, doc_id TEXT, doc_sha256 TEXT,
    kind TEXT, priority TEXT, reasons_json TEXT, proposed_record_json TEXT,
    status TEXT NOT NULL DEFAULT 'OPEN', resolved_by TEXT, resolution TEXT, resolution_note TEXT,
    created_at TEXT, resolved_at TEXT);

CREATE TABLE IF NOT EXISTS workflow_tasks (
    task_id TEXT PRIMARY KEY, decision_id TEXT, student_id TEXT, doc_id TEXT, task TEXT, owner TEXT,
    trigger_status TEXT, due_date TEXT, status TEXT NOT NULL DEFAULT 'OPEN',
    completed_by TEXT, completion_note TEXT, created_at TEXT, completed_at TEXT);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


class Store:
    def __init__(self, db_path: str | Path):
        self.conn = connect(db_path)
        self.audit = AuditTrail(self.conn)

    # ---------------------------------------------------------------- decisions
    def save_decision(self, run_id, doc, student_id, decision, report) -> str:
        did = new_id("DEC")
        self.conn.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (did, run_id, doc.doc_id, doc.sha256, student_id, decision.action, decision.priority,
                           json.dumps(decision.to_dict()), json.dumps(report.to_dict()), utcnow()))
        self.conn.commit()
        return did

    def decision(self, decision_id):
        r = self.conn.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
        return dict(r) if r else None

    def decisions(self, run_id=None):
        q, a = "SELECT * FROM decisions", []
        if run_id:
            q += " WHERE run_id=?"; a.append(run_id)
        return [dict(r) for r in self.conn.execute(q + " ORDER BY created_at", a)]

    # ---------------------------------------------------------------- records
    def current_record(self, student_id):
        r = self.conn.execute("SELECT * FROM verified_records WHERE student_id=? ORDER BY version DESC LIMIT 1",
                              (student_id,)).fetchone()
        return dict(r) if r else None

    def record_history(self, student_id):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM verified_records WHERE student_id=? ORDER BY version", (student_id,))]

    def records(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT v.* FROM verified_records v JOIN (SELECT student_id, MAX(version) mv FROM verified_records "
            "GROUP BY student_id) m ON v.student_id=m.student_id AND v.version=m.mv ORDER BY v.student_id")]

    def already_committed(self, student_id, doc_sha256) -> bool:
        cur = self.current_record(student_id)
        return bool(cur and cur["status"] == "ACTIVE" and cur["source_sha256"] == doc_sha256)

    def append_record(self, student_id, record: dict, *, status="ACTIVE", source_doc_id=None, source_sha256=None,
                      decision_id=None, by, by_type, override=False, note=None) -> dict:
        cur = self.current_record(student_id)
        version = (cur["version"] + 1) if cur else 1
        rid = new_id("REC")
        self.conn.execute("INSERT INTO verified_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (rid, student_id, version, status, json.dumps(record, sort_keys=True), source_doc_id,
                           source_sha256, decision_id, by, by_type, int(override), note, utcnow()))
        self.conn.commit()
        return {"record_id": rid, "student_id": student_id, "version": version, "status": status}

    # ---------------------------------------------------------------- review cases
    def open_case(self, decision_id, student_id, doc, kind, priority, reasons, proposed_record) -> str:
        cid = new_id("CASE")
        self.conn.execute(
            "INSERT INTO review_cases (case_id, decision_id, student_id, doc_id, doc_sha256, kind, priority,"
            " reasons_json, proposed_record_json, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,'OPEN',?)",
            (cid, decision_id, student_id, doc.doc_id, doc.sha256, kind, priority, json.dumps(reasons),
             json.dumps(proposed_record, sort_keys=True), utcnow()))
        self.conn.commit()
        return cid

    def case(self, case_id):
        r = self.conn.execute("SELECT * FROM review_cases WHERE case_id=?", (case_id,)).fetchone()
        return dict(r) if r else None

    def cases(self, status=None):
        q, a = "SELECT * FROM review_cases", []
        if status:
            q += " WHERE status=?"; a.append(status)
        q += " ORDER BY CASE priority WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END, created_at"
        return [dict(r) for r in self.conn.execute(q, a)]

    def open_case_for(self, student_id, doc_sha256):
        r = self.conn.execute("SELECT case_id FROM review_cases WHERE student_id IS ? AND doc_sha256=? AND status='OPEN'",
                              (student_id, doc_sha256)).fetchone()
        return r[0] if r else None

    def resolve_case(self, case_id, status, by, resolution, note):
        self.conn.execute("UPDATE review_cases SET status=?, resolved_by=?, resolution=?, resolution_note=?, "
                          "resolved_at=? WHERE case_id=? AND status='OPEN'",
                          (status, by, resolution, note, utcnow(), case_id))
        self.conn.commit()

    # ---------------------------------------------------------------- follow-up tasks
    def open_task(self, decision_id, student_id, doc_id, spec: dict, today: date | None = None) -> str:
        tid = new_id("TASK")
        due = ((today or date.today()) + timedelta(days=spec.get("due_in_days", 7))).isoformat()
        self.conn.execute(
            "INSERT INTO workflow_tasks (task_id, decision_id, student_id, doc_id, task, owner, trigger_status,"
            " due_date, status, created_at) VALUES (?,?,?,?,?,?,?,?,'OPEN',?)",
            (tid, decision_id, student_id, doc_id, spec["task"], spec.get("owner"), spec.get("trigger_status"),
             due, utcnow()))
        self.conn.commit()
        return tid

    def open_task_for(self, student_id, trigger_status):
        r = self.conn.execute("SELECT task_id FROM workflow_tasks WHERE student_id=? AND trigger_status=? "
                              "AND status='OPEN'", (student_id, trigger_status)).fetchone()
        return r[0] if r else None

    def tasks(self, status=None):
        q, a = "SELECT * FROM workflow_tasks", []
        if status:
            q += " WHERE status=?"; a.append(status)
        return [dict(r) for r in self.conn.execute(q + " ORDER BY due_date", a)]

    def task(self, task_id):
        r = self.conn.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
        return dict(r) if r else None

    def complete_task(self, task_id, by, note, status="DONE"):
        self.conn.execute("UPDATE workflow_tasks SET status=?, completed_by=?, completion_note=?, completed_at=? "
                          "WHERE task_id=? AND status='OPEN'", (status, by, note, utcnow(), task_id))
        self.conn.commit()
