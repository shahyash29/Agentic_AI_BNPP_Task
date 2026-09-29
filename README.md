# Registrar Verification Agent

An agentic pipeline that ingests student academic documents, verifies every data point against the
Master Registry (`registrar_degree_audit_registry.csv`), and autonomously **commits**, **follows up**, or
**escalates** each record. Every tool call, decision, commit and human override lands in a
hash-chained, append-only audit trail.

Core is pure Python 3.10+ standard library (SQLite). Claude integration is optional.

```b
python -m registrar_agent run                    # process data/inbox
python -m registrar_agent review list            # HITL queue
python -m registrar_agent audit verify           # prove the trail is intact
python -m unittest discover -s tests             # 20 scenario + governance tests
```

## Architecture

```
 inbox/*.txt ──► DocumentStore ──────────────┐   (parse, classify, extract, index ALL versions)
 registry.csv ─► Registry (column mapping) ──┤
                                             ▼
                ┌──────────── Planner ─────────────┐
                │ RulePlanner (deterministic)      │  proposes a decision
                │ ClaudePlanner (tool-use, opt.)   │
                └──────────────┬───────────────────┘
                               │ every call via ToolExecutor ──► TOOL_CALL audit event
          analysis tools: get_document · lookup_registry · resolve_student_identity ·
          resolve_indirect_field · list_document_versions · compute_conferral_date ·
          validate_document · evaluate_policy
                               ▼
                Policy guard (most conservative of proposal vs. policy wins)
                               ▼
      COMMIT ─► verified_records (append-only, versioned, per-field provenance)
      FOLLOW_UP ─► workflow_tasks (playbook by audit_status, due date, owner)
      ESCALATE ─► review_cases ─► HITL: approve / correct / reject (RBAC, notes required)
                               ▼
               audit_events: SHA-256 hash chain + UPDATE/DELETE triggers
```

| Module | Responsibility |
|---|---|
| `documents.py` | Parsing, doc-type classification, label-synonym field extraction, version ordering |
| `registry.py` | Source-of-truth loader; maps arbitrary CSV headers (`config/registry_mapping.json`); evaluates registry conferral rules |
| `resolvers.py` | Indirect truth (reference chains), identity evolution, relative-date milestones |
| `validation.py` | Field-by-field comparison with typed normalisation (GPA tolerance, degree synonyms, date formats) |
| `policy.py` | Lifecycle decision + guard |
| `tools.py` | Audited tool catalogue (also exported as Claude tool schemas) |
| `agent.py` | Orchestrator, idempotency, record building |
| `hitl.py` | Staff review, corrections, revocation, task completion |
| `audit.py` | Immutable hash-chained trail, verification, JSONL export |
| `llm_agent.py` | Optional Claude planner and LLM extraction fallback |

## How the hard cases are handled

**Indirect truth.** If a field's value contains a document id (`See Revised Degree Audit RDA-2026-0045`),
the resolver follows it (recursively, cycle-safe, max depth 4) to the *latest* version of the referenced
document and reads the same field there. The full chain is stored as provenance
(`["TR-2026-1005 v1", "RDA-2026-0045 v1"]`). A reference to a document not in the inbox is `UNRESOLVED` and escalated.

**Identity evolution.** Name resolution goes: exact → registry `known_aliases` → a *chain* of name-change
certificates from the registry name to the document name (bound by student id and DOB) → fuzzy near-match
(escalated at MEDIUM) → conflict (escalated at HIGH). A DOB mismatch is always a conflict. A verified name change
produces a `registry_update_proposals` entry. The agent never edits the Master Registry.

**Calculated milestones.** Both sides understand relative rules. Documents can say
`30 days following the end of the Spring 2026 term` or `10 business days after…`. The registry can say
`TERM_END+30D` / `TERM_END+10BD`. Both are evaluated against the registry `term_end_date`. If the document
names a different term than the registry's final term, that's a conflict. If the registry's static date disagrees
with its own rule, that's escalated as a registry integrity issue.

**Version control.** All versions of a document id are kept. The most authoritative one is chosen by
(not VOID/SUPERSEDED) → explicit version number → CORRECTED/REISSUED status → issue date. File order and
names are never used. The note records how many versions were considered and which one was chosen.

## Lifecycle policy (`policy.py`)

| Condition | Action | Priority |
|---|---|---|
| Student not in registry, identity conflict, any field mismatch, unresolved reference, registry inconsistency, `HOLD_*` status, record previously revoked by staff | ESCALATE | HIGH |
| Fuzzy identity, missing required field, unknown status | ESCALATE | MEDIUM |
| Verified, status `PENDING_*` | FOLLOW_UP (playbook: task, owner, SLA) | MEDIUM |
| Verified, status `CLEARED` | COMMIT (or approval queue with `--require-approval`) | LOW |

**Guardrail:** the final action is the more conservative of the planner's proposal and the policy
(ESCALATE > FOLLOW_UP > COMMIT). An LLM can make the agent more cautious but can never override a hold or a
conflict. This also neutralises prompt injection hidden in documents.

## Governance and HITL

- **RBAC** (`config/staff.json`): `reviewer` handles LOW/MEDIUM cases and tasks. HIGH-priority cases and record
  revocation need a `supervisor`. Denied attempts are themselves audited (`AUTHZ_DENIED`).
- **Approve** an escalation = override. A justification note is mandatory and the record is flagged `override=1`.
- **Correct** = commit with field-level diff, the agent's original provenance kept alongside `HUMAN_CORRECTED`.
- **Reject**, optionally opening a follow-up task.
- **Revoke** an autonomous commit (post-hoc oversight): creates a new `REVOKED` version. The agent will then
  escalate that same document rather than re-commit it.
- **Governance mode** `--require-approval`: no autonomous commits. Every COMMIT becomes an approval case.

## Audit trail

`audit_events` rows are chained: `hash = sha256(ts, run_id, actor, actor_type, event, subject, payload, prev_hash)`.
SQLite triggers block UPDATE/DELETE, and `audit verify` recomputes the chain to detect out-of-band edits or
deleted rows (both covered by tests). `verified_records` and `decisions` are append-only too. Each run logs the
registry SHA-256 and the document hashes, so any decision can be reproduced. Events include
`RUN_STARTED, PLAN, TOOL_CALL, LLM_TURN, LLM_EXTRACTION, DECISION, RECORD_COMMITTED, FOLLOW_UP_CREATED,
ESCALATED, HUMAN_APPROVED, HUMAN_CORRECTED, HUMAN_REJECTED, HUMAN_REVOKED, AUTHZ_DENIED, SKIPPED, DOC_FAILED`.

For production, anchor `audit.head()` externally on a schedule (WORM bucket, compliance mailbox) and move to
Postgres with the same triggers plus a role that only has INSERT/SELECT.

## Your Master Registry (`data/registrar_degree_audit_registry.csv`)

Your registry has **two columns: `student_id` and `audit_status`**. It covers 65 students and uses 6 statuses.
So the registry is the source of truth for *who exists* and *what state their audit is in*. It holds no names,
GPAs or dates. The agent therefore:

1. **Checks the student exists.** The student id must be in the registry, otherwise the case is escalated
   (STU-5009 is not in it).
2. **Takes the other values from the documents.** Name, GPA, credits, term end and conferral date are resolved
   from the documents: references are followed, the latest version is chosen, relative dates are computed and
   name changes are applied. The values are cross-checked for internal consistency. Each one is stored with
   status `DOC_ONLY` and its provenance, because the registry has no column to compare it with.
   If you later add columns (e.g. `cumulative_gpa`), they are compared automatically.
3. **Decides the action from `audit_status`** using `config/status_policy.json`:

| audit_status | Action |
|---|---|
| degree_conferred | COMMIT |
| requirement_waived | COMMIT (waiver noted) |
| conferral_pending | FOLLOW_UP (HIGH if the conferral date has already passed) |
| grade_appeal_pending | FOLLOW_UP |
| academic_hold | ESCALATE HIGH |
| record_sealed | ESCALATE HIGH (no automated processing) |
| anything else | ESCALATE MEDIUM |

4. **Cross-checks status against evidence:**
   - a finalised status with a future conferral date → ESCALATE;
   - a document saying the GPA is not final while the status is finalised → ESCALATE;
   - credits below `credit_minimum` (120) → warning.
5. **Handles students with only a certificate** (no transcript): FOLLOW_UP to request the transcript
   (STU-5002, STU-5016).

## Your document format (narrative transcripts)

`prose.py` reads the institution's real documents in `data/client_inbox/`. These documents have no header
block and no document ids, and several transcripts can share one file (split on `=====`).

- **Synthetic ids:** secondary documents are linked by student id: `TR:STU-5004`, `DCC:STU-5004`
  (conferral certificate) and `ACC:STU-5016` (accreditation). Every version of `DCC:STU-5004` is grouped, and
  `(REVISED)` or `(CORRECTED)` plus the issue date decide which version wins.
- **Deferred values:** "shall be as recorded in the Degree Conferral Certificate" becomes `See DCC:STU-xxxx`,
  which is then resolved like any other reference.
- **Relative dates:** "sixty (60) days after the Final Term End Date set forth in Section 3" is evaluated against
  the term end stated on the document itself. That term end is also checked against the registry.
- **Embedded name changes:** "record of Margaret A. Reyes (formerly recorded as Whitcombe)" counts as
  name-change evidence.
- **Pending values:** "GPA: to be calculated upon posting…" becomes a `PENDING` check. That leads to FOLLOW_UP if the
  registry status is PENDING_*, and to ESCALATE if the registry claims CLEARED.
- **Integrity checks** (`consistency.py`, no registry needed):
  - a revision's stated day-change must match the dates;
  - a correction must point to a version we actually have;
  - the conferral date can't be before the term end;
  - no duplicate transcripts;
  - secondary documents must name the same person and institution;
  - grade corrections are flagged;
  - certificates with no transcript are reported.

```bash
python -m registrar_agent inspect --inbox data/client_inbox          # registry-free data check
python -m registrar_agent run --inbox data/client_inbox --registry /path/to/registrar_degree_audit_registry.csv
```

## Using Gemini (default LLM provider)

```bash
pip install google-genai && export GEMINI_API_KEY=...
export GEMINI_MODEL=gemini-2.5-flash            # optional
python -m registrar_agent run --planner llm     # --provider gemini is the default
```

Gemini function calling is used with automatic execution **disabled**, so every tool call still passes through
`ToolExecutor` and the audit trail. Its decision is reconciled by the same policy guard.
`enrich_with_gemini` uses JSON-schema structured output to fill fields the pattern extractors miss.
The Gemini path is tested with a scripted fake client. Run it against your key before relying on it.

## Using Claude (optional)

```bash
pip install anthropic && export ANTHROPIC_API_KEY=...
export REGISTRAR_AGENT_MODEL=claude-sonnet-4-5    # any tool-use capable model
python -m registrar_agent run --planner llm
```

Claude investigates each transcript with the same audited tools (actor `llm:<model>`), reasons about anything
unusual, and calls `submit_decision`. The LLM extraction fallback fills fields the pattern extractor missed on
messy or unknown documents (pattern values always win). The live API path is exercised in tests with a
scripted fake client. Run it against the real API before relying on it.

## Plugging in your real data

1. Point `--registry` at your CSV. Add header aliases to `config/registry_mapping.json` if needed.
   Pipe-separate multiple values in the aliases column.
2. Drop documents (OCR text) into `--inbox`. Add label synonyms to `FIELD_LABELS` in `documents.py` if needed,
   or use `--planner llm` for free-form layouts.
3. Update `FOLLOW_UP_PLAYBOOK` in `policy.py` with your office's pending statuses, owners and SLAs.

## Demo scenarios (synthetic data)

| Student | Scenario | Outcome |
|---|---|---|
| S1001 | Clean record | COMMIT |
| S1002 | Legal name changed (NC-2025-0112) | COMMIT + registry update proposal |
| S1003 | Conferral = term end + 30 days | COMMIT (2026-06-14) |
| S1004 | Honors deferred to certificate; v2 CORRECTED supersedes v1 | COMMIT (Magna Cum Laude) |
| S1005 | GPA via Revised Degree Audit; PENDING_FINAL_GRADES | FOLLOW_UP |
| S1006 / S1011 | Financial / disciplinary hold | ESCALATE HIGH |
| S1007 | GPA conflict | ESCALATE HIGH |
| S1008 | Name differs, no evidence | ESCALATE HIGH |
| S1009 | +10 business days; committee pending | FOLLOW_UP |
| S1010 | References a missing revised audit | ESCALATE HIGH |
| S9999 | Not in registry | ESCALATE HIGH |

Regenerate the inbox with `python data/generate_inbox.py`.
