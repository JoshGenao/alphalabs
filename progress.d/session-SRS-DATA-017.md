=== SESSION SRS-DATA-017 ===
Date: 2026-07-10
Feature: SRS-DATA-017 — support concurrent reads during ingestion writes (SyRS SYS-63; StRS SN-1.26 /
SN-1.28). Verification mode: Load test.
Outcome: serialized — the substrate + full test pyramid were already on main (SESSION 69); this session
ADDED the AC-named Python-consumer-vs-held-writer Load test (the load-bearing close SESSION 69 deferred)
and de-churns the feature. passes STAYS false: that Load test is ATP_RUN_INTEGRATION-gated and cannot
run in a parallel autonomous session — the operator runs it to flip.

CONTEXT / WHY THIS WAS RE-OFFERED:
- The DATA-017 code + tests are already integrated on main (branch was 0-ahead). Don't rebuild:
  crates/atp-data/tests/srs_data_017_concurrent_reads.rs (always-run in-process Load test: deterministic
  read/write overlap, non-blocking, two-writer no-loss), crates/atp-data/examples/data017_lock_holder.rs
  (cross-process held-lock fixture), tests/integration/test_data017_concurrent_reads.py (L5, CLI
  readers), tools/concurrent_read_check.py (9 static + cargo smoke), L7 domain + L3 contract.
- It churned because SESSION 69 (18 days ago) folded its note into progress.txt (>>> lines) rather
  than a progress.d/session-SRS-DATA-017.md, so agent_pool.serialized_notes() never saw it and the
  scheduler kept re-offering it. THIS note (Outcome: serialized) fixes that: DATA-017 moves to the
  awaiting-verification bucket and is no longer re-offered until an operator flips it.
- SESSION 69 Codex R1 held passes:false because the AC NAMES in-process Python consumers (strategy
  containers / backtests / factor jobs / notebooks) and the existing L5 test reads via the CLI as an
  ANALOGY — a premature over-claim. That close was blocked on the consumer binding (SRS-DATA-007).
  DATA-007 is now passes:true and StoreBackedHistoricalData exists (python/atp_strategy/store_history.py
  itself names "the concurrent-read-DURING-write Load test for this named Python consumer" as the
  deferred SRS-DATA-017 close) → blocker LIFTED. Operator chose (AskUserQuestion) to build the full L5
  named-consumer test.

WHAT I DID (new tests + doc sweep; NO production code / substrate change):
- tests/integration/test_data017_named_consumer_concurrent_read.py (L5, ATP_RUN_INTEGRATION-gated,
  OPERATOR-RUN): the ACTUAL named consumer StoreBackedHistoricalData.get_bars_range(RAW) reads while a
  data017_lock_holder writer PROCESS genuinely holds the single-writer lock mid load-modify-save. During
  the held window the consumer must COMPLETE within its timeout (a block surfaces StoreQueryError →
  FAIL, never hang), still see the committed seed (1 AAPL bar, uncorrupted), and NOT see the holder's
  uncommitted write (snapshot isolation); after release it sees seed + committed holder (2 bars).
  Modeled byte-for-byte on the proven test_data017_concurrent_reads.py orchestration (bounded waits,
  release-in-finally).
- tests/domain/test_data017_named_consumer_read.py (L7, SOLO-RUN de-risk): the same consumer reader
  (StoreBackedHistoricalData.get_bars_range RAW) reads a seeded temp store, returns a real Bar (positive
  OHLC, high>=low, chrono order), and reflects a subsequent commit — proves the exact reader path the
  gated Load test drives is sound. Plus a non-vacuity guard asserting the gated Load test still drives
  StoreBackedHistoricalData through a HELD window (ready/release + StoreQueryError + snapshot-isolation
  tokens), since it does not run in the default suite and could otherwise be silently gutted.
- Doc-cluster sweep (the named-consumer Load test now EXISTS but its held-writer run stays
  operator-gated → passes stays false): architecture/runtime_services.json
  (concurrent_read_runtime_contract + store_history_binding_contract descriptions),
  tools/architecture_check.py (assert_concurrent_read + assert_store_history summaries),
  tools/store_history_check.py, tools/concurrent_read_check.py (_DEFERRED_OWNERS[0]),
  tests/integration/test_data007_python_binding.py docstring. Every "not yet in place" / "deferred 017
  close" claim reworded to "now written; operator-gated ATP_RUN_INTEGRATION run pending".

WHAT I TESTED (per AC step):
  Step 1 (init): ./init.sh → "✓ Environment ready" (its --require-cargo block runs
    concurrent_read_check → the DATA-017 static checks + cargo smoke PASS).
  Step 2 (exercise via CLI/API + fixtures): tests/domain/test_data017_named_consumer_read.py → PASS —
    builds data016_ingest_cli + data007_query_cli, seeds a temp store, StoreBackedHistoricalData reads
    the seed (1 AAPL bar) then reflects a committed ingest (2 bars).
  Step 3 (acceptance criteria — reads during writes, no corruption / no blocking, by the named
    consumers): the concurrent-read-DURING-write property is proven end-to-end and deterministically by
    the always-run Rust Load test (cargo test -p atp-data → the 3 srs_data_017_concurrent_reads tests
    PASS) and by the CLI-reader L5 test; the NAMED-consumer variant is WRITTEN and collects cleanly
    (pytest --collect-only → 1 test) but is ATP_RUN_INTEGRATION-gated, so its held-writer run is the
    DEFERRED operator step. The named-consumer READER path is solo-verified (Step 2). options/indicators
    clauses are unaffected (this AC is the concurrency property, not normalization).
  Step 4 (objective evidence): cargo test -p atp-data → ok (0 failed, incl. the 3 Load tests);
    pytest -m "not integration and not e2e" → 3230 passed / 4 skipped (documented deferrals) / 22
    deselected; concurrent_read_check.py / store_history_check.py / architecture_check.py → PASS;
    ruff clean on all changed files (ruff check + ruff format --check pass on every file I touched);
    run_ci_locally.sh → halts at the PRE-EXISTING repo ruff-format debt (tests/e2e/
    test_dashboard_refresh.py + tools/deployment_check.py — NOT my files; left untouched to avoid
    repo-wide-format scope creep), which integrate skips (integrate only rebases + pushes).

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings.
  judgment (adversarial_review.py, reviewer=codex): WARN (1 medium) — OVERRIDDEN. Finding: the domain
    companion tests/domain/test_data017_named_consumer_read.py does real cargo/subprocess/disk I/O but is
    pytest.mark.domain, so it runs in the default suite (claimed to bypass the ATP_RUN_INTEGRATION gate).
    OVERRIDE rationale: it follows the ESTABLISHED codebase convention — the sibling
    tests/domain/test_data017_concurrent_reads.py already shells cargo + data016_ingest_cli +
    data007_query_cli against a temp dir in the default domain suite (ran green in this session's
    3230-pass run). The de-facto ATP_RUN_INTEGRATION criterion is held-window / shared-resource /
    container, NONE of which this hermetic temp-dir reader test has; the held-writer concurrency PROOF IS
    correctly L5-gated (test_data017_named_consumer_concurrent_read.py, pytest.mark.integration). The
    gating boundary is explicit (module layer-note added): this file de-risks only the consumer READ
    path, never claims the held-writer proof. codex's recommended fake-runner alternative would duplicate
    the existing boundary test (test_store_history_binding.py) and DROP the real subprocess+store de-risk
    that is the whole value for the unrunnable L5 test. Not a ship-blocker (WARN, not block).

COMPLETENESS: serialized. passes STAYS false. The AC's verification mode is a Load test, and the
faithful named-consumer proof is a cross-process HELD-writer test (ATP_RUN_INTEGRATION=1) that a
parallel autonomous session must not run. Everything solo-runnable is green; the reader path is
solo-verified; the gated test is written, collects, and is guarded against gutting.

Resume / next (OPERATOR flip): run
  ATP_RUN_INTEGRATION=1 pytest tests/integration/test_data017_named_consumer_concurrent_read.py \
                               tests/integration/test_data017_concurrent_reads.py
then flip via `close_feature.py --verified` (agent_pool integrate --force-complete) or the verified-e2e
label. Closing DATA-017 unblocks SRS-DATA-010. Don't rebuild the substrate/tests — only the operator's
gated Load-test run remains.
