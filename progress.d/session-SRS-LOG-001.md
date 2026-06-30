=== SESSION SRS-LOG-001 ===
Date: 2026-06-30
Feature: SRS-LOG-001 — separate persistent system logs from user strategy logs.
Outcome: serialized (passes:false) — the operator-store SUBSTRATE is built + integrated (durable
  JSONL system/strategy sinks, separation, query, rotation); NOT a complete runtime half. Deferred:
  the core-runtime event FORWARDING path (Rust producers → store), the dashboard-viewing facet
  (SRS-UI-001), and the live REST/WS/CLI handlers (SRS-API-001).

Session arc (this was a multi-feature session):
- Started on SRS-API-001 (claimed at launch): its operator-interface runtime SUBSTRATE was already
  built+integrated by a prior session (serialized). It had NO recorded deps, so the scheduler kept
  re-offering a code-done/flip-blocked feature. Formalized the real edges (blocked-on SRS-EXE-001,
  SRS-SAFE-001, SRS-ORCH-005, SRS-RESV-002, SRS-RESV-003, SRS-LOG-001) + released.
- Claimed SRS-FAC-001 next: factor-job engine fully built + Codex-APPROVED (S49 + S75, 27 rounds);
  flip attempted+reverted TWICE because its AC is an NFR-P7 wall-clock perf proof over 8,000+ REAL
  securities (deterministic-clock fixture ≠ real proof). Formalized blocked-on SRS-DATA-005 + released.
- Operator (AskUserQuestion) chose SRS-LOG-001 as the next build target (foundational, uncontested).
- Acquired SRS-LOG-001 collision-safely via agent_pool's locked primitives (claim routes by id, can't
  target; LOG-001 was not first in pick_order).

What I did (SRS-LOG-001):
- Built python/atp_logging/persistence.py — the runtime half the SDK-surface log_record_contract
  deferred to "SRS-LOG-001's runtime half":
  - JsonlLogStore: durable, append-only JSON-Lines LogSink bound to ONE LogClass. SYSTEM and STRATEGY
    logs persist to SEPARATE physical files (the literal AC). write() refuses a wrong-class record AND
    runs full SDK validation (dispatcher.validate_log_record) so a direct caller (bypassing the
    dispatcher) cannot land a malformed/mis-attributed audit entry.
  - Durability: flush + os.fsync per append (default) + dir-fsync on rotation; a torn trailing fragment
    (crash mid-write, even one cutting a multi-byte UTF-8 char) is dropped, never fabricated; a
    complete-but-unparseable line fails closed (LogStoreCorruptionError).
  - Rotation: opt-in (max_bytes=None default → unbounded, no eviction); bounded retention when set.
  - read_records / JsonlLogStore.read / query: the GET /api/v1/logs read seam (filter by log_class /
    min_severity / source / event_type / correlation_id / time window; limit + newest_first).
  - build_separated_log_dispatcher: SYSTEM store + a SEPARATE STRATEGY store; physical separation
    cannot be bypassed (bare-basename validation + os.path.samefile cross-check).
- python/atp_logging/dispatcher.py: extracted validate_log_record() as the single validation source of
  truth, reused by dispatch() + the persistent sinks (all mutation-tested strings preserved verbatim).
- architecture/runtime_services.json: new log_persistence_contract block; reconciled the SDK
  log_record_contract deferred "SRS-LOG-001-runtime" entry (sink now BUILT). PASS-line in
  log_record_check.py reconciled; tests/test_log_record_contract.py assertion updated (sink built).
- tools/log_persistence_check.py (17 collectors) wired into init.sh + ci.yml + run_ci_locally.sh
  (same slot after log_record).
- README + module docstring + contract: framed the Python sink as the dashboard/API-backend operator
  log store (AGENTS.md line 111), core-runtime Rust emission deferred.
- NO prep commit needed: SAFETY_PATH_RE already matches atp_logging / persist / log_sink.

What I tested (per AC step):
- Step 1 (./init.sh → Environment ready): PASS — init.sh runs the new log persistence check between
  log_record and adapter checks and reaches "✓ Environment ready".
- Step 2/3 (exercise the surface; both classes available with the 6 fields + system-event taxonomy):
  PARTIAL/serialized — the persistent sinks + query are built + demonstrable via Python/CLI seam
  (write→read round-trip, separation, durability, query filters). The AC's "viewable from the
  dashboard" (Step 2 browser automation) needs SRS-UI-001; the live GET /api/v1/logs REST/WS/CLI needs
  SRS-API-001. Both deferred → passes:false.
- Step 4 (objective evidence, leave passes:false): DONE.
- Commands (all green): tools/log_persistence_check.py PASS (17 collectors); tools/log_record_check.py
  PASS; pytest -m "not integration and not e2e" → 2558 passed, 4 pre-existing skips (incl. the new
  L1+L7+L3 log-persistence cases + the SDK mutation tests after the dispatcher refactor); ruff
  check/format clean (new files); mypy clean on atp_logging.

Critic verdicts:
  deterministic (tools/critic_check.py --staged): APPROVE — 0 findings (safety-paired by
    tests/domain/test_log_persistence.py; atp_logging/persist/log_sink are SAFETY_PATH_RE paths).
  judgment (tools/codex_review.sh origin/main):
    r1 → needs-attention [high] path-alias separation bypass (./system.jsonl ≡ system.jsonl):
      FIXED — bare-basename validation + os.path.samefile cross-check + regression tests.
    r2 → needs-attention: [high] direct-write validation bypass: FIXED — validate_log_record reused in
      JsonlLogStore.write() + direct-store invalid-record tests (L1 + L7). [critical] arch:
      Python runtime sink vs the AGENTS.md Rust-core mandate.
    r2 [critical] arch (Python runtime sink vs Rust-core mandate): OPERATOR-AUTHORIZED override
      (AskUserQuestion: "Ship Python (serialized), authorize"). Rationale: the sink is the dashboard/API
      backend for the operator log surfaces (GET /api/v1/logs, dashboard pane, admin logs CLI), which
      AGENTS.md line 111 permits in Python "if it does not become a core runtime service"; consistent
      with the existing Python atp_logging package + its SDK contract + the Python consumers. Recorded
      honestly, NOT faked APPROVE.
    r3 [high] read path served invariant-violating records -> FIXED (_record_from_mapping re-validates
      via validate_log_record, fails closed). r3 [medium] stale log_record_contract description -> FIXED.
    r4 [critical] overclaimed runtime completion (the core-runtime event FORWARDING path — Rust
      producers → store — is ALSO deferred, not just UI/API) -> FIXED by honest scoping: contract/README/
      docstring/note now call this an operator-store SUBSTRATE (not a complete runtime half), and
      log_persistence_contract.deferred[] gained an SRS-LOG-001-core-forwarding entry. No Rust path built.

Known issues / notes for next agent:
- SRS-LOG-001 stays passes:false. To FLIP: (1) SRS-UI-001 renders the system + strategy log panes
  (browser-automation e2e), and (2) SRS-API-001 registers the GET /api/v1/logs query handler + LOGS
  WebSocket publisher + admin logs CLI on the operator-interface-runtime registry — all reading
  atp_logging.persistence.read_records / query. The persisted trail they consume is durable + queryable.
- A core-runtime Rust durable sink + the Rust→operator-store forwarding path (how Rust-emitted system
  events reach this Python store) is a separate, deferred concern (not built here).
- Two OTHER features were de-churned this session: SRS-API-001 (blocked-on its 6 domain owners) and
  SRS-FAC-001 (blocked-on SRS-DATA-005) — both code-done/flip-blocked; they had no recorded deps and
  the scheduler kept re-offering them. Now formally blocked.
- ENV: init.sh installs requirements.txt only; pytest/ruff/mypy/hypothesis were pip-installed into the
  worktree venv. run_ci_locally.sh with SYSTEM python3 fails on pre-existing numpy-missing arch checks
  + pre-existing mypy(66)/ruff repo-red — none of which are this change (my files are clean).
Resume / next: build SRS-UI-001 (dashboard log pane) or wire the SRS-API-001 LOGS handler over
  atp_logging.persistence to advance the SRS-LOG-001 flip.
