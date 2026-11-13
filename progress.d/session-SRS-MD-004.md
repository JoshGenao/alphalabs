=== SESSION SRS-MD-004 ===
Date: 2026-11-12
Feature: SRS-MD-004 - block live and simulated order submission when required market data is stale
Outcome: implemented (B/verification: S1 waits on SRS-MD-003's verification; I1 needs an operator IB paper-gateway window; no production producer feeds the board until SRS-EXE-001 composes the feed loop into the live host)

What I did:
- atp-market-data `freshness_board.rs` (new): `LineFreshnessBoard`, a thread-safe, cloneable store over
  `HeartbeatFreshnessMonitor` plus an explicit `GapTracking` mode (`Unavailable` for IB, which has no
  upstream sequence; `Tracked` wraps `SequenceGapDetector` and uses `combined_line_freshness`). One
  `FreshnessClock` stamps and evaluates; verdicts are computed at read time. Fail closed: unwatched,
  never-observed, uncanonicalizable (options), poisoned, and clock-unavailable all read Stale with no age.
  On a tracked board only a tick carrying new data refreshes a line; a sequence-less refresh is refused.
  Every write returns `BoardWriteError` (Poisoned, SequenceRequired, Uncanonicalizable, ClockUnavailable).
- atp-execution port: `MarketDataFreshnessProbe` takes `(symbol, asset_class)`; `staleness_seconds` and
  `StaleDataEvent.staleness_seconds` are `Option<u64>`; an unknown age prints "age unknown", never "0s".
  All 19 impls updated. The composite gate passes each leg with `AssetClass::Option` (fails closed).
- atp-execution paper gate: `dispatch_order`'s `InternalSimulation` arm checks freshness after validate,
  before the simulation port; same category and error_type as live (SYS-64); no connectivity read.
- atp-orchestrator `market_data_freshness.rs` (new): `BoardFreshnessProbe` (the production probe) and
  `MonotonicEpochClock`, which reads the OS suspend-inclusive clock (CLOCK_BOOTTIME on Linux,
  CLOCK_MONOTONIC on macOS) through the new `rustix` dependency (operator-approved 2026-11-12; the
  workspace forbids unsafe, so raw libc was not possible).
- Live host IB tier: still refused; the refusal names the remaining blocker (feeding the board from the
  live tick stream, owner SRS-EXE-001). Updated the domain test that asserted the old owner.
- `tools/freshness_check.py`: new paper-gate check; both checks ignore `//` comments and tolerate a
  rustfmt break before `.method(`.
- `tools/_rust_parser.py`: a block-bodied match arm now ends at its closing brace. rustfmt writes no comma
  after a block arm, so the old scan swallowed the arms after it (it broke `connectivity_check` and
  twelve architecture tests once my change made rustfmt wrap the Connected arm).
- Contract tests: three negative tests mutated anchors my port change removed; every mutation in
  `test_freshness_contract.py` now goes through `mutate()`, which raises on a missing anchor.
- `architecture/runtime_services.json`: `freshness_contract.paper_gate` + `production_probe`; re-pointed
  deferred entries and the sequence_gap description. Contract `docs/contracts/SRS-MD-004.json`: paths,
  C3 "accepted by the simulation port" (fill loop is SIM-002/003/004), new C4 (shipped composition).

What I tested (per step):
  Step 1: PASS - ./init.sh -> Environment ready
  Step 2: PASS - cargo test -p atp-orchestrator --test srs_md_004_stale_gate_wired (real board + probe +
          dispatch_order + WiredPaperSimulation + IB bridge, fixture data, injected clock)
  Step 3: PASS - pytest tests/domain/test_stale_data_block.py (13 cases: 4 ERR-3 + 9 MD-004 Rust cases)
  Step 4: PASS - tools/freshness_check.py (live + paper gate pinned, cargo smoke)
  C1-C4: PASS via tools/evidence.py run; implementation-complete gate PASS; evidence verify complete.
  Gates: cargo clippy -D warnings; cargo test --workspace; tools/run_ci_locally.sh (5777 passed);
  cargo check --target x86_64-unknown-linux-gnu for the Boottime path. Every fix mutation-verified by
  hand (mutation_verify.py reports the Python tests red by import, which is dependence, not behaviour).
Critic verdicts:
  deterministic: APPROVE - no findings
  judgment (adversarial_review.py, reviewer=codex): APPROVE at round 6 - no findings
Adversarial rounds: 6
  r1 BLOCK - replayed ticks revive a time-stale tracked line; Instant stops during suspend (writers that
     refresh without proof of new data; suspend-blind clock)
  r2 BLOCK - high-water clamp froze the clock after a corrected forward jump (suspend-blind clock)
  r3 BLOCK - anchor-relative wall read went unavailable before start-up and counted as zero (unknown as empty)
  r4 BLOCK - a backward correction during suspend hid the suspend (suspend-blind clock)
  r5 BLOCK - a correction smaller than the suspend left a positive wall delta (suspend-blind clock);
     stopped and asked the operator: approved rustix for the OS suspend-inclusive clock
  r6 APPROVE
Playbook updates: safety-paths.md rules 58-59 (suspend-inclusive clock; writers must prove new data);
  test-integrity.md rules 47-48 (a port change disarms other features' mutation tests; rustfmt writes no
  comma after a block arm); pipeline-and-integrate.md rules 42-43 (do not commit the plan file; record
  evidence last).
Integrate attempt 1 (refused, nothing landed): evidence predated the chore commit's playbook edits, and
  the branch carried progress.d/plan-SRS-MD-004.md, which Step 4.1 of the coding prompt asks for but
  shared_state_violations refuses. Removed the plan file (its substance is in this note) and re-recorded
  evidence last. Operator follow-up: prompts/coding_prompt.md Step 4.1 contradicts the integrate guard.
Push transport: this session had no SSH key loaded, so integrate ran with per-process git settings routing
  git@github.com to HTTPS through `gh auth git-credential` (no git config or remote was changed).
Follow-ups seen, not fixed here: ~12 pre-existing rustdoc unresolved-link warnings in
  atp-orchestrator (hot_swap_promotion.rs, resv005_hot_swap_promote_cli.rs, cooldown_store.rs,
  trigger_config_store.rs, err001_broker_envelope_cli.rs); `cargo audit` is not installed locally, so the
  new rustix/bitflags/errno/libc entries are first audited by CI's security workflow.
Resume / next: implementation is complete. Remaining: I1 (operator IB paper-gateway window, after
  SRS-MD-003 verifies) and S1 (closes once SRS-MD-003 is passes:true). SRS-EXE-001 owns feeding a
  LineFreshnessBoard from the live IB tick stream inside the live host (see the host's refusal text and
  sequence_gap_contract.deferred[1]).
