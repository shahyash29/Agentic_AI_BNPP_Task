"""Immutable, hash-chained audit trail.

Every agent tool call, decision, autonomous commit and human override is
written here as an append-only event. Immutability is enforced at three levels:

1. SQLite triggers reject any UPDATE or DELETE on ``audit_events``.
2. Each event stores ``prev_hash`` and ``hash = sha256(canonical(event) + prev_hash)``,
   so any out-of-band edit (e.g. someone opening the DB file and dropping the
   triggers) breaks the chain and is detected by :meth:`AuditTrail.verify`.
3. :meth:`AuditTrail.head` exposes the latest hash so it can be anchored
   externally (e-mailed to compliance, written to WORM storage, etc.).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

GENESIS_HASH = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    run_id      TEXT,
    actor       TEXT NOT NULL,
    actor_type  TEXT NOT NULL CHECK (actor_type IN ('agent','human','system','llm')),
    event_type  TEXT NOT NULL,
    subject     TEXT,
    payload     TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_audit_subject ON audit_events(subject);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_events(run_id);

CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
"""


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass
class AuditEvent:
    seq: int
    ts: str
    run_id: str | None
    actor: str
    actor_type: str
    event_type: str
    subject: str | None
    payload: dict
    prev_hash: str
    hash: str


class AuditTrail:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._lock = threading.Lock()
        conn.executescript(SCHEMA)

    @staticmethod
    def compute_hash(ts, run_id, actor, actor_type, event_type, subject, payload_json, prev_hash) -> str:
        material = _canonical([ts, run_id, actor, actor_type, event_type, subject, payload_json, prev_hash])
        return hashlib.sha256(material.encode()).hexdigest()

    def head(self) -> str:
        row = self.conn.execute("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else GENESIS_HASH

    def record(self, event_type: str, payload: dict, *, actor: str, actor_type: str,
               run_id: str | None = None, subject: str | None = None) -> AuditEvent:
        with self._lock:
            ts = utcnow()
            payload_json = _canonical(payload)
            prev = self.head()
            h = self.compute_hash(ts, run_id, actor, actor_type, event_type, subject, payload_json, prev)
            cur = self.conn.execute(
                "INSERT INTO audit_events (ts, run_id, actor, actor_type, event_type, subject, payload, prev_hash, hash)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (ts, run_id, actor, actor_type, event_type, subject, payload_json, prev, h),
            )
            self.conn.commit()
            return AuditEvent(cur.lastrowid, ts, run_id, actor, actor_type, event_type, subject,
                              json.loads(payload_json), prev, h)

    def events(self, *, subject: str | None = None, run_id: str | None = None,
               event_type: str | None = None, limit: int | None = None) -> list[AuditEvent]:
        q, args = "SELECT * FROM audit_events WHERE 1=1", []
        if subject:
            q += " AND subject = ?"; args.append(subject)
        if run_id:
            q += " AND run_id = ?"; args.append(run_id)
        if event_type:
            q += " AND event_type = ?"; args.append(event_type)
        q += " ORDER BY seq"
        if limit:
            q = f"SELECT * FROM ({q.replace('ORDER BY seq', 'ORDER BY seq DESC')} LIMIT {int(limit)}) ORDER BY seq"
        return [self._row(r) for r in self.conn.execute(q, args)]

    @staticmethod
    def _row(r) -> AuditEvent:
        return AuditEvent(r[0], r[1], r[2], r[3], r[4], r[5], r[6], json.loads(r[7]), r[8], r[9])

    def verify(self) -> tuple[bool, list[str]]:
        """Recompute the whole chain. Returns (ok, problems)."""
        problems: list[str] = []
        prev = GENESIS_HASH
        expected_seq = None
        for r in self.conn.execute("SELECT * FROM audit_events ORDER BY seq"):
            seq, ts, run_id, actor, actor_type, et, subject, payload_json, prev_hash, h = r
            if expected_seq is not None and seq != expected_seq:
                problems.append(f"seq gap: expected {expected_seq}, found {seq} (event deleted?)")
            expected_seq = seq + 1
            if prev_hash != prev:
                problems.append(f"seq {seq}: prev_hash does not link to previous event")
            if self.compute_hash(ts, run_id, actor, actor_type, et, subject, payload_json, prev_hash) != h:
                problems.append(f"seq {seq}: content hash mismatch (event altered)")
            prev = h
        return (not problems, problems)

    def export_jsonl(self) -> Iterable[str]:
        for e in self.events():
            yield _canonical(e.__dict__)
