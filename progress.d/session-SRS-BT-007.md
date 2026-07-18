=== SESSION SRS-BT-007 ===
Date: 2026-07-18
Feature: SRS-BT-007 — support grid search and multidimensional parameter sweeps for
backtests (SRS-5.6, P2, method=Test; SyRS SYS-19; StRS SN-1.16/SC-12)
Outcome: serialized (code complete + verified over fixtures; passes stays false;
blocked-on SRS-BT-001 recorded — see "Classification" below)

What I did:
- crates/atp-simulation/src/sweep.rs — the whole BT-007 core, each AC noun a named
  artifact: ParameterSpace (validated ParameterAxis dims; fail-closed on zero axes,
  empty/dup axis names, empty value lists/tokens, dup values; axes held name-sorted so
  declaration order never changes enumeration; points() = deterministic Cartesian
  product of canonical StrategyParameters — the BT-009 parameter-set identity;
  cardinality via checked_mul in u128, TooManyPoints against MAX_SWEEP_POINTS=10_000
  BEFORE materialization), ObjectiveFunction (ObjectiveMetric allowlist over all eight
  SYS-16 metrics × Direction max|min; maximize_sharpe / minimize_max_drawdown SYS-19
  conveniences), SweepStrategyFactory (the fail-closed StrategyParameters→strategy
  bridge seam; MissingParameter/UnknownParameter/InvalidParameterValue — never a silent
  default run; BT-008 walk-forward reuses this per in-sample window), SweepRunner::run
  (sequential, reuses shipped BacktestEngine::run + benchmark::compare per point; no
  parallelism/RNG/clock), SweepReport (ranked best-first via f64::total_cmp,
  direction-driven, ties broken by canonical entries; 1-based ranks; undefined (None)
  objective → unranked bucket w/ ObjectiveUndefined, never fabricated/ranked-last/
  dropped — total_points == ranked+unranked; any per-point failure aborts the WHOLE
  sweep w/ PointFailed naming the point; bounded per-point payloads, no curves/logs).
  Persistence deliberately NOT in the runner (caller composes BacktestRecord::from_result
  with the BT-009 store; keeps sweep pure, keeps BT-008 loops from flooding history).
- bt007_sweep_cli (Cargo [[bin]]) — operator surface: repeatable --axis name=v1,v2,...;
  explicit --objective REQUIRES explicit --direction (never guessed); --format human|kv
  (kv count-prefixed, contiguously indexed, control-char fail-closed via kv_field);
  genuinely parameterized ParamRoundTrip fixture strategy (lot × sell_ts move real
  Sharpe/drawdown) + fixture bars/benchmark per the verification step's wording.
- architecture/runtime_services.json — new sim_parameter_sweep_contract block (sibling
  of sim_backtest_store_contract) pinning the full surface + 5 deferred owners.
- tools/backtest_sweep_check.py — structural gate (15 checks: surface, per-variant
  space validation each RAISED not just declared, cap-before-materialize ordering,
  8-metric allowlist, factory vocabulary, engine/compare reuse, total_cmp+direction+
  tie-break ranking, None→unranked routing w/ no unwrap_or fallback, PointFailed
  naming, determinism tokens, lib re-export, no-broker-dep, vendor isolation, CLI
  registration + direction-refusal + kv guard, cargo smoke). Wired via its two pytest
  consumers (contract + domain), same as every sibling per-feature check — NOT in the
  ci.yml/run_ci_locally.sh loop (that loop is architecture-level scripts only; verified).

What I tested (per step):
  Step 1 (init): PASS — ./init.sh → "✓ Environment ready" (dev deps installed into
    .venv manually; init.sh skips requirements-dev.txt).
  Step 2 (exercise CLI workflows w/ fixtures): PASS — bt007_sweep_cli run (6-point demo
    space, stated default objective, real ranked table); run --objective max_drawdown
    --direction min (rank 1 flips to lot=5,sell_ts=5 vs Sharpe's lot=20,sell_ts=5);
    --format kv grammar inspected; fail-closed: --objective w/o --direction, dup axis
    value, unknown flag/metric → non-zero, no ranking output.
  Step 3 (AC): PASS over fixtures — parameter space definition (--axis flags →
    ParameterSpace) produces ranked results (1-based best-first) by the selected
    objective, proven against INDEPENDENT hand-derived rankings (hand-run
    engine+compare per point, hand sort) under both SYS-19 named objectives:
    cargo test -p atp-simulation --test srs_bt_007_parameter_sweep (11 tests) +
    --test srs_bt_007_cli (8 tests, incl. byte-identical fresh-process repeat runs).
  Step 4 (evidence, passes stays false): PASS — passes remains false (serialized).
  Gate: pytest -m "not integration and not e2e" 3876 passed (18 domain + 46 contract
    are new); cargo test --workspace green (136 suites); cargo fmt --check + clippy
    --workspace -D warnings clean; ruff check clean; ruff format clean on my files.
    KNOWN PRE-EXISTING main baselines (NOT this feature, untouched): ruff format
    --check would reformat 13 files (toolchain-pin drift, owner = pins PR); mypy
    python/ 68 errors in 16 files (this diff adds no python/ source).

Critic verdicts:
  deterministic: APPROVE — no findings.
  judgment (adversarial_review.py, reviewer=codex): r1 BLOCK — sole finding
    (classification, no code defect): sweeps can only instantiate fixture Rust
    strategies while the Python strategy host is deferred, so flipping passes:true
    would claim a capability operators can't use on real user strategies; codex's own
    remedy = "integrate as partial/serialized and keep SRS-BT-007 open with the
    dependency recorded".
  OPERATOR DECISION (2026-07-18, interactive): integrate serialized + block on
    SRS-BT-001 — explicitly authorized over the alternative "complete per precedent"
    (BT-002/003/006/010 closed green on the same fixture-CLI pattern). No fake
    APPROVE; the codex verdict is honored as-is.

Classification:
- Code: complete and verified solo end to end over fixtures (nothing needs
  IB/integration/e2e). Flip semantics: BLOCKED-ON SRS-BT-001 (recorded via
  agent_pool block; edge lives in ROOT tools/feature_deps.json — if ROOT lags
  origin/main run `git -C <ROOT> pull`). The flip needs the production
  SweepStrategyFactory implementor: the SRS-BT-001 Python strategy host
  instantiating real user strategy code per point (then a sweep over a real
  strategy + real stored bars closes the AC for product use). Operator may also
  judge the sibling precedent sufficient and flip via close_feature/--force-complete.

Resume / next:
- DON'T rebuild the sweep core. To flip: implement the Python-host-backed
  SweepStrategyFactory once SRS-BT-001 lands (the seam + tests are ready), wire a
  real-strategy sweep demo, re-run codex, integrate complete.
- Adjacent consumers ready to build on this: SRS-BT-008 walk-forward (call
  SweepRunner::run per in-sample window — designed for it), REST/dashboard sweep
  surface (SRS-API-001/UI owners), sweep-point persistence into the BT-009 store.
