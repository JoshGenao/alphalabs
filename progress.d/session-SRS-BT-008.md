=== SESSION SRS-BT-008 ===
Date: 2026-07-26
Feature: SRS-BT-008 — support walk-forward analysis (SRS-5.6, SyRS SYS-20, StRS SN-1.17, P2;
method=Test). AC: "In-sample windows are optimized, out-of-sample windows are evaluated, and
outputs preserve the parameter set and metrics per window."
Outcome: serialized (code complete + verified solo over fixtures; passes stays false;
blocked-on SRS-BT-007 recorded — see "Classification" below)

What I did:
- crates/atp-simulation/src/walk_forward.rs — the whole BT-008 core, a thin DETERMINISTIC
  orchestration over the shipped SRS-BT-007 SweepRunner (no new engine, no parallel
  re-implementation of the BacktestEngine + benchmark::compare chain). Each AC noun is a named
  artifact:
    * WalkForwardWindow { in_sample, out_of_sample } + validate() — the SAFETY CORE: enforces
      the NO-LOOKAHEAD invariant (in_sample.end < out_of_sample.start → else LookaheadWindow),
      fail-closed on an inverted sub-range (InvalidWindow). An abutting window (oos = is.end+1)
      is valid; only overlap/precedence is a lookahead.
    * WalkForwardSchedule::new (fail-closed: EmptySchedule / per-window validate /
      NonMonotonicFolds — in-sample start must strictly advance / OverlappingFolds — oos tiles
      forward without double-counting / TooManyFolds — > MAX_WALK_FORWARD_FOLDS) +
      WalkForwardSchedule::rolling (canonical generator; checked_add/checked_mul →
      ScheduleOverflow; ZeroLength/ZeroStep/ZeroFolds; TooManyFolds capped BEFORE
      Vec::with_capacity so an unbounded operator count can't panic/OOM — codex r1 fix; funnels
      through new() so step<oos_len overlap is rejected by one rule set).
    * WalkForwardRunner::run<F: SweepStrategyFactory> — per fold: (in-sample OPTIMIZED) run a
      full sweep over the in-sample window, take report.ranked.first() (rank-1); empty ranking →
      NoOptimum{window} (never selects an unranked point). (out-of-sample EVALUATED) rebuild a
      SINGLE-point ParameterSpace from the winner's entries (singleton_space) and run it through
      the SAME SweepRunner over the oos window — the oos number is exactly what a standalone
      backtest of that point reports. Per-fold failure aborts the WHOLE analysis naming the
      window (InSampleSweepFailed / OutOfSampleEvalFailed, each window: *window).
    * WalkForwardFold PRESERVES per window: selected_parameters + in_sample_metrics/comparison +
      out_of_sample_metrics/comparison + out_of_sample_objective:Option<f64> (Some when defined,
      honestly None when undefined — NEVER a fabricated stand-in). WalkForwardReport.total_folds
      == folds.len() (every scheduled fold accounted for). WalkForwardError: 13 fail-closed
      variants. NO sweep.rs edits — the singleton space is built through BT-007's public API, so
      the closed serialized BT-007 surface is untouched.
- crates/atp-simulation/src/bin/bt008_walk_forward_cli.rs (Cargo [[bin]]) — operator surface:
  explicit --fold IS:IE:OS:OE (repeatable) or --rolling START:IS_LEN:OOS_LEN:STEP:COUNT (mutually
  exclusive); --axis; --objective+--direction (explicit-objective-requires-explicit-direction);
  --format human|kv (kv count-prefixed, contiguously indexed, control-char fail-closed via
  kv_field). Reuses BT-007's ParamRoundTrip strategy + factory + fixture benchmark over a 12-bar
  fixture catalog so 3 rolling folds tile forward.
- crates/atp-simulation/src/lib.rs — pub mod walk_forward; + doc.
- architecture/runtime_services.json — new sim_walk_forward_contract block (sibling of
  sim_parameter_sweep_contract) pinning the full surface + 4 deferred owners.
- tools/walk_forward_check.py — structural gate (16 evidence items incl cargo smoke): window/
  schedule types + DateRange import, no-lookahead guard RAISED, schedule validation raised
  variant-by-variant, rolling checked arithmetic + zero-guards + funnels-through-new, runner
  REUSES SweepRunner (self.sweep.run present + no BacktestEngine::new / benchmark::compare /
  compare-import re-implementation), in-sample rank-1 + NoOptimum, oos singleton + Option objective
  + unranked route, preservation (selected_parameters/in_sample_metrics/out_of_sample_metrics +
  total_folds), no-fabrication (no unwrap_or(0.0)/unwrap_or_default), point-failure naming the
  window, full error-enum, determinism, lib re-export, no-broker-dep, vendor isolation, CLI
  registration + direction-refusal + no-lookahead-surfaced + kv guard, cargo smoke. Wired via its
  two pytest consumers (L3 contract + L7 domain), NOT in ci.yml/run_ci_locally.sh (architecture-
  level scripts only) — same as the sibling backtest_sweep_check.py.

What I tested (per step):
  Step 1 (init): PASS — ./init.sh → "✓ Environment ready" (dev deps installed into .venv manually;
    init.sh skips requirements-dev.txt).
  Step 2 (exercise CLI over fixtures): PASS — bt008_walk_forward_cli run (default rolling 1:4:2:2:3,
    maximize sharpe → 3 forward folds; minimize max_drawdown flips fold-0 winner lot=20,sell_ts=5 →
    lot=5,sell_ts=5); --format kv grammar inspected; explicit --fold honored; fail-closed exits:
    lookahead --fold, --objective w/o --direction, zero rolling args, overlapping rolling, both
    --fold+--rolling, malformed fold, unknown flag/metric → non-zero, no fold output.
  Step 3 (AC): PASS over fixtures — cargo test -p atp-simulation --test srs_bt_008_walk_forward
    (12 tests) + --test srs_bt_008_cli (8 tests). In-sample optimization proven against an
    INDEPENDENT sweep's rank-1 under both SYS-19 objectives; out-of-sample metrics proven against a
    RAW BacktestEngine+compare of the winner on the unseen window (an oracle independent of the
    runner's singleton-sweep path); no-lookahead holds across folds + lookahead schedule fails
    closed; NoOptimum / InSampleSweepFailed{window} / OutOfSampleEvalFailed{window} fail-closed;
    total_folds accounting; byte-identical repeat runs.
  Step 4 (evidence, passes stays false): PASS — passes remains false (serialized).
  Gate: pytest -m "not integration and not e2e" green (66 new: L3 contract + L7 domain);
    cargo test --workspace green (exit 0); cargo fmt --check clean; cargo clippy --workspace
    -D warnings clean (test-target result_large_err handled by a file-scoped #![allow] w/ comment,
    matching atp-types/atp-execution cold-error convention); ruff check + ruff format --check clean
    (whole repo — the prior 13-file toolchain drift was resolved on main by 6ca5bb9).
    KNOWN PRE-EXISTING baseline (NOT this feature, untouched): mypy python/ 68 errors in 16 files
    (this diff adds no python/ source; mypy does not check tools/ or tests/).

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings.
  judgment (adversarial_review.py, reviewer=codex; re-run against merge-base after main advanced):
    r1 BLOCK (high) — "Unbounded rolling fold count allocates before validation":
      WalkForwardSchedule::rolling called Vec::with_capacity(fold_count) before any bound, so a
      huge --rolling count could panic on capacity overflow / attempt a huge allocation instead
      of a typed error. FIXED: MAX_WALK_FORWARD_FOLDS cap + TooManyFolds variant checked BEFORE
      allocation in rolling() (and in new()); Rust + CLI regression tests for oversized count;
      structural gate asserts the cap-before-alloc ordering scoped to rolling().
    r2 BLOCK (high) — "CLI human report drops required metrics": the default human output
      rendered only 5 of the 8 SYS-16 metrics per window (omitting alpha/beta/annualized_volatility
      + benchmark identity) while the AC requires outputs to preserve metrics per window. FIXED:
      format_metrics_human renders the FULL eight-metric family + benchmark for both the in-sample
      and out-of-sample halves of every fold; CLI test asserts each of the 9 fields appears 6× (3
      folds × 2 windows).
    r3 WARN (medium, OVERRIDDEN) — "Serialized blocker is only in prose, not the scheduler DAG":
      tools/feature_deps.json had no SRS-BT-008 entry. OVERRIDE: this is a process/DAG concern,
      not a code defect. Resolved by running `agent_pool.py block SRS-BT-008 --on SRS-BT-007` —
      the edge is now in the canonical ROOT feature_deps.json (verified SRS-BT-008 →
      ['SRS-BT-007']), and `integrate` commits it to main via `_sync_deps_into`. The edge lands
      out-of-band by the scheduler's design, not in the feature branch commits, so the branch
      diff codex reviews cannot show it — re-running would warn identically.

Classification:
- Code: complete and verified solo end to end over fixtures (nothing needs IB/integration/e2e).
  Flip semantics: BLOCKED-ON SRS-BT-007 (recorded via agent_pool block; edge in ROOT
  tools/feature_deps.json — if ROOT lags origin/main run `git -C <ROOT> pull`). Walk-forward reuses
  BT-007's SweepRunner + SweepStrategyFactory, which can only instantiate FIXTURE Rust strategies
  while the Python strategy host is deferred (SRS-BT-001, BT-007's own blocker). Per the recorded
  lesson, codex blocks passes:true on fixture-only BT closes; operator confirmed serialize+block
  (2026-07-26) over "complete per precedent". No fake APPROVE / green.

Resume / next:
- DON'T rebuild the walk-forward core. To flip: once SRS-BT-007 is complete (its Python-host-backed
  SweepStrategyFactory), wire a real-strategy walk-forward demo over a real strategy + real stored
  bars, re-run codex, integrate complete. The seam + tests are ready.
- Adjacent consumers ready to build on this: REST/dashboard walk-forward surface (SRS-API-001 / UI
  owners consume the walk_forward API); fold persistence into the BT-009 store (compose
  BacktestRecord::from_result per window — runner deliberately stays pure).
