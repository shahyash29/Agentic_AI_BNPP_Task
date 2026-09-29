"""Command-line interface for agents, staff reviewers and auditors.

    python -m registrar_agent run                         # process the inbox
    python -m registrar_agent review list                 # HITL queue
    python -m registrar_agent review approve CASE --reviewer r.patel --note "hold lifted"
    python -m registrar_agent audit verify                # prove the trail is intact
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

from .agent import RulePlanner, VerificationAgent
from .documents import DocumentStore
from .hitl import AuthorizationError, ReviewService
from .registry import Registry
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
DEF = {
    "db": os.environ.get("REGISTRAR_DB", str(ROOT / "registrar_agent.db")),
    "registry": str(ROOT / "data" / "registrar_degree_audit_registry.csv"),
    "inbox": str(ROOT / "data" / "client_inbox"),
    "mapping": str(ROOT / "config" / "registry_mapping.json"),
    "staff": str(ROOT / "config" / "staff.json"),
}


def _table(rows: list[dict], cols: list[str], widths: dict | None = None):
    widths = widths or {}
    w = {c: min(widths.get(c, 60), max([len(c)] + [len(str(r.get(c) or "")) for r in rows])) for c in cols}
    print("  ".join(c.upper().ljust(w[c]) for c in cols))
    print("  ".join("-" * w[c] for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c) if r.get(c) is not None else "")[: w[c]].ljust(w[c]) for c in cols))


def _llm(provider: str):
    """Return (planner_cls, enrich_fn) for the chosen provider, or exit with setup help."""
    if provider == "gemini":
        from . import gemini_agent as m
        if not m.available():
            sys.exit("Gemini needs GEMINI_API_KEY (or GOOGLE_API_KEY) and `pip install google-genai`.")
        return m.GeminiPlanner, m.enrich_with_gemini
    from . import llm_agent as m
    if not m.available():
        sys.exit("Claude needs ANTHROPIC_API_KEY and `pip install anthropic`.")
    return m.ClaudePlanner, m.enrich_with_llm


def cmd_inspect(a):
    """Registry-free integrity review of the inbox: what was extracted and what looks wrong."""
    from .consistency import inspect_store
    docs = DocumentStore.from_directory(Path(a.inbox))
    rows = [{"doc_id": d.doc_id, "type": d.doc_type, "ver": d.version, "status": d.status or "",
             "issued": d.issue_date or "", "file": Path(d.path).name,
             "key_fields": "; ".join(f"{k}={v}" for k, v in d.fields.items()
                                     if k in ("name", "gpa", "credits_earned", "term_end_date", "conferral_date",
                                              "new_name", "accreditation_ref"))} for d in docs.docs]
    _table(rows, ["doc_id", "type", "ver", "status", "issued", "file", "key_fields"], {"key_fields": 110})
    print()
    findings = inspect_store(docs)
    _table([x.to_dict() for x in findings], ["severity", "code", "student_id", "message"], {"message": 120})


def cmd_run(a):
    store = Store(a.db)
    registry = Registry.load(Path(a.registry), Path(a.mapping))
    docs = DocumentStore.from_directory(Path(a.inbox))
    planner = RulePlanner()
    if a.planner == "llm":
        planner_cls, enrich = _llm(a.provider)
        enrich(docs, store.audit)
        planner = planner_cls()
    agent = VerificationAgent(store, registry, docs, planner=planner, require_approval=a.require_approval,
                              today=date.fromisoformat(a.today) if a.today else None)
    outcomes = agent.run()
    print(f"Run {agent.run_id}  planner={planner.name}  registry sha256={registry.sha256[:12]}\n")
    _table([o.__dict__ | {"reason": (o.reasons or [""])[0]} for o in outcomes],
           ["doc_id", "student_id", "action", "priority", "reference", "reason"], {"reason": 90})
    ok, _ = store.audit.verify()
    print(f"\nAudit trail: {'INTACT' if ok else 'BROKEN'}  head={store.audit.head()[:16]}")


def cmd_review(a):
    store = Store(a.db)
    svc = ReviewService(store, Path(a.staff))
    try:
        if a.review_cmd == "list":
            rows = store.cases(None if a.all else "OPEN")
            for r in rows:
                r["reason"] = (json.loads(r["reasons_json"]) or [""])[0]
            _table(rows, ["case_id", "priority", "kind", "student_id", "doc_id", "status", "reason"], {"reason": 80})
        elif a.review_cmd == "show":
            c = store.case(a.case_id)
            if not c:
                sys.exit(f"case {a.case_id} not found")
            d = store.decision(c["decision_id"])
            print(json.dumps({"case": {k: v for k, v in c.items() if not k.endswith("_json")},
                              "reasons": json.loads(c["reasons_json"]),
                              "proposed_record": json.loads(c["proposed_record_json"]),
                              "validation": json.loads(d["report_json"]) if d else None}, indent=2))
        elif a.review_cmd == "approve":
            print(json.dumps(svc.approve(a.case_id, a.reviewer, a.note), indent=2))
        elif a.review_cmd == "correct":
            corr = dict(kv.split("=", 1) for kv in a.set)
            print(json.dumps(svc.correct(a.case_id, a.reviewer, corr, a.note), indent=2))
        elif a.review_cmd == "reject":
            print(json.dumps(svc.reject(a.case_id, a.reviewer, a.note, a.follow_up), indent=2))
    except (AuthorizationError, ValueError, KeyError) as e:
        sys.exit(f"Refused: {e}")


def cmd_records(a):
    store = Store(a.db)
    if a.records_cmd == "list":
        _table(store.records(), ["student_id", "version", "status", "committed_by", "committed_by_type",
                                 "override", "source_doc_id", "created_at"])
    elif a.records_cmd == "show":
        hist = store.record_history(a.student_id)
        for h in hist:
            h["record"] = json.loads(h.pop("record_json"))
        print(json.dumps(hist, indent=2))
    elif a.records_cmd == "revoke":
        try:
            print(json.dumps(ReviewService(store, Path(a.staff)).revoke_record(a.student_id, a.reviewer, a.reason), indent=2))
        except (AuthorizationError, ValueError) as e:
            sys.exit(f"Refused: {e}")


def cmd_tasks(a):
    store = Store(a.db)
    if a.tasks_cmd == "list":
        _table(store.tasks(None if a.all else "OPEN"),
               ["task_id", "student_id", "owner", "due_date", "status", "task"], {"task": 70})
    else:
        try:
            print(json.dumps(ReviewService(store, Path(a.staff)).complete_task(
                a.task_id, a.reviewer, a.note, cancel=a.tasks_cmd == "cancel"), indent=2))
        except (AuthorizationError, ValueError) as e:
            sys.exit(f"Refused: {e}")


def cmd_audit(a):
    store = Store(a.db)
    if a.audit_cmd == "verify":
        ok, problems = store.audit.verify()
        n = len(store.audit.events())
        print(f"{n} events, chain {'INTACT' if ok else 'BROKEN'}; head hash {store.audit.head()}")
        for p in problems:
            print("  !", p)
        sys.exit(0 if ok else 2)
    if a.audit_cmd == "log":
        ev = store.audit.events(subject=a.subject, run_id=a.run, event_type=a.type, limit=a.limit)
        rows = [{"seq": e.seq, "ts": e.ts[:19], "actor": e.actor, "type": e.actor_type, "event": e.event_type,
                 "subject": e.subject,
                 "detail": e.payload.get("tool") or e.payload.get("action") or e.payload.get("case_id")
                 or e.payload.get("doc_id") or ""} for e in ev]
        _table(rows, ["seq", "ts", "actor", "type", "event", "subject", "detail"])
    if a.audit_cmd == "export":
        with open(a.out, "w") as f:
            for line in store.audit.export_jsonl():
                f.write(line + "\n")
        print(f"exported to {a.out}; head hash {store.audit.head()}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="registrar_agent", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=DEF["db"])
    p.add_argument("--staff", default=DEF["staff"])
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="process the document inbox")
    r.add_argument("--registry", default=DEF["registry"])
    r.add_argument("--inbox", default=DEF["inbox"])
    r.add_argument("--mapping", default=DEF["mapping"])
    r.add_argument("--planner", choices=["rules", "llm"], default="rules")
    r.add_argument("--provider", choices=["gemini", "claude"], default=os.environ.get("REGISTRAR_LLM_PROVIDER", "gemini"),
                   help="LLM used with --planner llm (default gemini)")
    r.add_argument("--require-approval", action="store_true", help="route every COMMIT to staff approval")
    r.add_argument("--today", help="override today's date (YYYY-MM-DD) for follow-up due dates")
    r.set_defaults(fn=cmd_run)

    i = sub.add_parser("inspect", help="registry-free integrity review of the document inbox")
    i.add_argument("--inbox", default=DEF["inbox"])
    i.set_defaults(fn=cmd_inspect)

    rv = sub.add_parser("review", help="human-in-the-loop review queue")
    rs = rv.add_subparsers(dest="review_cmd", required=True)
    x = rs.add_parser("list"); x.add_argument("--all", action="store_true")
    x = rs.add_parser("show"); x.add_argument("case_id")
    x = rs.add_parser("approve"); x.add_argument("case_id"); x.add_argument("--reviewer", required=True); x.add_argument("--note")
    x = rs.add_parser("correct"); x.add_argument("case_id"); x.add_argument("--reviewer", required=True)
    x.add_argument("--set", action="append", required=True, metavar="FIELD=VALUE"); x.add_argument("--note", required=True)
    x = rs.add_parser("reject"); x.add_argument("case_id"); x.add_argument("--reviewer", required=True)
    x.add_argument("--note", required=True); x.add_argument("--follow-up", help="optional follow-up task text")
    rv.set_defaults(fn=cmd_review)

    rc = sub.add_parser("records", help="verified records")
    rcs = rc.add_subparsers(dest="records_cmd", required=True)
    rcs.add_parser("list")
    x = rcs.add_parser("show"); x.add_argument("student_id")
    x = rcs.add_parser("revoke"); x.add_argument("student_id"); x.add_argument("--reviewer", required=True)
    x.add_argument("--reason", required=True)
    rc.set_defaults(fn=cmd_records)

    t = sub.add_parser("tasks", help="follow-up workflow tasks")
    ts = t.add_subparsers(dest="tasks_cmd", required=True)
    x = ts.add_parser("list"); x.add_argument("--all", action="store_true")
    for c in ("complete", "cancel"):
        x = ts.add_parser(c); x.add_argument("task_id"); x.add_argument("--reviewer", required=True); x.add_argument("--note")
    t.set_defaults(fn=cmd_tasks)

    au = sub.add_parser("audit", help="immutable audit trail")
    aus = au.add_subparsers(dest="audit_cmd", required=True)
    aus.add_parser("verify")
    x = aus.add_parser("log"); x.add_argument("--subject"); x.add_argument("--run"); x.add_argument("--type")
    x.add_argument("--limit", type=int, default=50)
    x = aus.add_parser("export"); x.add_argument("--out", required=True)
    au.set_defaults(fn=cmd_audit)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
