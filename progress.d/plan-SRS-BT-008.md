# SRS-BT-008 — Walk-Forward Analysis

## Context

`SRS-BT-008` (SRS-5.6, SyRS SYS-20, StRS SN-1.17, P2) requires the platform to
**support walk-forward analysis**. Acceptance criterion:

> In-sample windows are optimized, out-of-sample windows are evaluated, and
> outputs preserve the parameter set and metrics per window.

Walk-forward analysis is the standard guard against over-fitting a backtest: you
optimize strategy parameters on an *in-sample* window, then measure how the winning
parameters perform on the *immediately-following, unseen out-of-sample* window, and
march that pair forward through time. Its safety core is the **no-lookahead
invariant**: the out-of-sample window must lie strictly after the in-sample window,
or the "out-of-sample" result is contaminated with data the optimizer already saw
and the reported edge is fabricated confidence an operator would size capital on.

BT-008 is the **named downstream consumer of BT-007's sweep core** (the
`sim_parameter_sweep_contract` deferred list literally says
*"walk-forward analysis consuming `SweepRunner::run` per in-sample window
(SRS-BT-008)"*). The build **reuses `SweepRunner::run`** — it does not add a new
optimizer.

### Classification (decided up front — honest path; operator-confirmed serialized)

BT-007 integrated **serialized** (`passes:false`), blocked-on `SRS-BT-001`, because a
sweep can only instantiate *fixture* Rust strategies while the Python strategy host
is deferred; codex blocked a `passes:true` flip as a capability-overclaim (no code
defect) and the operator authorized serialize+block. Walk-forward inherits that same
boundary verbatim (it optimizes/evaluates the same fixture strategies). So BT-008
lands **serialized**: code complete and verified solo end-to-end over fixtures,
`passes` stays **false**, and it is `block`ed on the deferred producer. This is
recorded as a decision, not a limitation discovered mid-build. (Operator confirmed
serialized+block, 2026-07-26.)

## What I'll build

All under `crates/atp-simulation` (the operator-directed home for the backtest/sim
core), mirroring the BT-007 sweep slice file-for-file.

### 1. Core module — `crates/atp-simulation/src/walk_forward.rs` (new)

A thin, deterministic orchestration over the shipped `SweepRunner` — **no new
engine, no parallel re-implementation of the backtest/compare chain**. Each AC noun
maps to a named artifact:

- **`WalkForwardWindow { in_sample: DateRange, out_of_sample: DateRange }`** with
  `validate()` — fails closed on an invalid sub-range (`DateRange::validate`) and,
  critically, on a **lookahead window**: it requires `in_sample.end <
  out_of_sample.start` (out-of-sample strictly follows in-sample). This is the
  domain-safety invariant.
- **`WalkForwardSchedule { windows: Vec<WalkForwardWindow> }`**:
  - `new(windows)` — fails closed on an empty schedule, any invalid window, and
    non-forward folds (each fold's in-sample start must strictly advance and its
    out-of-sample window must not overlap the prior fold's — walk-forward tiles
    forward, it does not revisit).
  - `rolling(start, in_sample_len, out_of_sample_len, step, fold_count)` — the
    canonical generator; all boundary arithmetic via `checked_add`/`checked_mul`
    (fail closed `ScheduleOverflow`), and fails closed on a zero length / step /
    fold_count. Produces validated windows through `new`, so one rule set.
- **`WalkForwardRequest { base: BacktestRequest, space: ParameterSpace,
  objective: ObjectiveFunction, schedule: WalkForwardSchedule }`** — the shared
  launch config (symbol, cash, cost model) held constant; the schedule + space +
  objective are the varying inputs.
- **`WalkForwardRunner`** wrapping an internal `SweepRunner` (`new()` /
  `with_max_points()` test seam). `run<F: SweepStrategyFactory>(request, factory,
  bars, eval) -> Result<WalkForwardReport, WalkForwardError>`. Per fold:
  1. **In-sample optimization** — build a `SweepRequest` over the in-sample window
     and call `self.sweep.run(...)`. Take `report.ranked.first()` (rank-1) as the
     optimized point. Fail closed `NoOptimum { window }` if `ranked` is empty
     (every in-sample point had an undefined objective → nothing to select; never
     silently pick an unranked point). Record its parameters + objective value +
     in-sample metrics + comparison.
  2. **Out-of-sample evaluation** — rebuild a **single-point `ParameterSpace`** from
     the winning point's `entries()` (one `ParameterAxis::new(key, vec![value])`
     per entry → `ParameterSpace::new`) and `self.sweep.run(...)` it over the
     out-of-sample window. The sole evaluated row (ranked, or unranked when the
     objective is undefined on OOS) yields the out-of-sample metrics + comparison;
     the OOS objective is `Some(..)` when defined, honestly `None` otherwise —
     **never fabricated**. This routes OOS through the *identical* shipped chain.
     Rebuild failure is fail-closed (`SingletonRebuild`), never an `unwrap`.
- **`WalkForwardFold`** preserving, per window: the in/out ranges, the selected
  `StrategyParameters`, in-sample objective value + `PerformanceMetrics` +
  `BenchmarkComparison`, and out-of-sample `PerformanceMetrics` +
  `BenchmarkComparison` + `Option<f64>` OOS objective. ("outputs preserve the
  parameter set and metrics per window.")
- **`WalkForwardReport { objective, total_folds, folds }`** with
  `total_folds == folds.len()` proving every scheduled fold is accounted for.
- **`WalkForwardError`** (fail-closed, no broker/vendor tokens): `EmptySchedule`,
  `InvalidWindow`, `LookaheadWindow`, `NonMonotonicFolds`/`OverlappingFolds`,
  `ScheduleOverflow`, `ZeroLength`/`ZeroStep`/`ZeroFolds`, `NoOptimum`,
  `InSampleSweepFailed { window, .. }`, `OutOfSampleEvalFailed { window, .. }`
  (each per-window sweep failure **names the window**, mirroring `PointFailed`),
  `SingletonRebuild`.
- `lib.rs`: add `pub mod walk_forward;` (after `pub mod sweep;`) with a doc comment.

**No `sweep.rs` edits** — the single-point space is built through BT-007's existing
public API, keeping the closed serialized BT-007 surface untouched and BT-008
changes atomic.

### 2. Operator CLI — `crates/atp-simulation/src/bin/bt008_walk_forward_cli.rs` (new)

Mirrors `bt007_sweep_cli`. Reuses the same fixture chain (`ParamRoundTrip` strategy
+ `ParamRoundTripFactory` + windowed fixture catalog + `FixtureBenchmark`, copied
per the established per-bin fixture pattern). Flags: repeatable
`--fold is_start:is_end:oos_start:oos_end` **or** `--rolling
start:is_len:oos_len:step:count`; repeatable `--axis name=v1,v2,...`; `--objective
<metric> --direction <max|min>` (explicit-objective-requires-explicit-direction);
`--format human|kv`. Per-fold output: window bounds, selected params, in-sample
objective/metrics, out-of-sample objective/metrics. `kv` grammar count-prefixed +
contiguously indexed with the `kv_field` control-char fail-closed emitter. Register
as a `[[bin]]` in `crates/atp-simulation/Cargo.toml` with a scope-note comment.

### 3. Structural gate — `tools/walk_forward_check.py` (new) + contract block

New `sim_walk_forward_contract` block in `architecture/runtime_services.json`
(sibling of `sim_parameter_sweep_contract`), and a check script mirroring
`tools/backtest_sweep_check.py`. Checks (each with a non-vacuity negative in the
pytest consumers): window/schedule types; **no-lookahead guard actually raised**
(`Err(WalkForwardError::LookaheadWindow`); rolling generator checked arithmetic +
zero-guards; runner **reuses `SweepRunner`** (token present, and no direct
`BacktestEngine`/`compare` re-implementation in this module); in-sample uses
`ranked.first`/`NoOptimum`; OOS `Option` objective not fabricated (no
`unwrap_or(0.0)`/`unwrap_or_default()`); `total_folds` accounting field;
determinism forbidden-token set; full `WalkForwardError` variant list; lib
re-export `pub mod walk_forward;`; no broker dep; vendor isolation; CLI registration
+ `--objective requires --direction` + `kv_field`. PASS line `SRS-BT-008
WALK-FORWARD PASS` naming the deferred owners (real Python-strategy factory / host,
REST/dashboard surface, stored-data benchmark resolver, SRS-BT-009 persistence).

### 4. Tests

- **L5 Rust integration** `crates/atp-simulation/tests/srs_bt_008_walk_forward.rs`:
  hand-derived ground truth over a fixture catalog (~12 bars) and a 2–3 fold rolling
  schedule with a small `lot × sell_ts` space. Asserts: (a) each fold's selected
  params equal an **independent hand-run in-sample sweep** rank-1 under both SYS-19
  objectives; (b) recorded OOS metrics equal an **independent hand-run
  backtest+compare** of the winner on the OOS window; (c) no-lookahead holds
  (`in_sample.end < out_of_sample.start`) and a lookahead window fails closed; (d)
  `total_folds` accounting; (e) repeat runs byte-identical (determinism); (f)
  fail-closed: empty schedule, in-sample all-undefined → `NoOptimum`, per-window
  sweep failure names the window, rolling zero-len/step/count + overflow.
- **L5 Rust CLI** `crates/atp-simulation/tests/srs_bt_008_cli.rs`: process-boundary
  round-trip (human + kv), `--rolling` and `--fold`, fail-closed flags, fresh-process
  repeat determinism.
- **L3 contract** `tests/test_walk_forward_contract.py`: shells
  `walk_forward_check.py`; in-process negative spot-checks mutating source /
  lib.rs / Cargo.toml (dropped no-lookahead guard, bypassed `SweepRunner` reuse,
  fabricated OOS objective, neutered rolling arithmetic, dropped re-export, injected
  broker dep, leaked vendor token, direction-guessing CLI).
- **L7 domain (safety)** `tests/domain/test_walk_forward.py` (`pytest.mark.domain`,
  `pytest.mark.safety`): behavioral (shells the Rust tests for no-lookahead,
  in-sample optimization correctness, OOS evaluation, determinism, no-fabrication)
  + structural non-vacuity. The trading-safety framing: a walk-forward that leaks
  future data or fabricates an OOS metric would promote an over-fit configuration.

### CI wiring

`walk_forward_check.py` is a per-feature contract script, wired **only** via its two
pytest consumers (L3 + L7) — the same as `backtest_sweep_check.py`, which is *not*
in `ci.yml` / `run_ci_locally.sh` (that loop is architecture-level scripts only).

## Files

| Path | Change |
|---|---|
| `crates/atp-simulation/src/walk_forward.rs` | **new** core |
| `crates/atp-simulation/src/lib.rs` | add `pub mod walk_forward;` + doc |
| `crates/atp-simulation/src/bin/bt008_walk_forward_cli.rs` | **new** CLI |
| `crates/atp-simulation/Cargo.toml` | register `bt008_walk_forward_cli` bin |
| `architecture/runtime_services.json` | **new** `sim_walk_forward_contract` block |
| `tools/walk_forward_check.py` | **new** structural gate |
| `crates/atp-simulation/tests/srs_bt_008_walk_forward.rs` | **new** L5 |
| `crates/atp-simulation/tests/srs_bt_008_cli.rs` | **new** L5 CLI |
| `tests/test_walk_forward_contract.py` | **new** L3 |
| `tests/domain/test_walk_forward.py` | **new** L7 |
| `progress.d/plan-SRS-BT-008.md` | persist this plan (first post-approval write) |
| `progress.d/session-SRS-BT-008.md` | resume/handoff note (chore commit) |

## Reused (do not rebuild)

- `sweep::{SweepRunner, SweepRequest, SweepEvaluation, SweepReport, ParameterSpace,
  ParameterAxis, ObjectiveFunction, SweepStrategyFactory, RankedPoint, UnrankedPoint}`
  — `crates/atp-simulation/src/sweep.rs`.
- `backtest::{BacktestRequest, DateRange, BarSource, BacktestBar}` — replay is
  window-restricted, so one `BarSource` serves every fold.
- `backtest_store::StrategyParameters` (`entries()`, `from_pairs`) — point identity.
- `benchmark::{compare, BenchmarkSelection, BenchmarkSource}`, `metrics::MetricsConfig`
  — reached transitively through `SweepRunner::run`; no direct use in the new module.

## Verification (per `steps[]`)

1. **Step 1 (env):** `./init.sh` → `✓ Environment ready`.
2. **Step 2 (exercise CLI over fixtures):** run `bt008_walk_forward_cli` with a
   `--rolling` schedule and with explicit `--fold`s; inspect human + kv output;
   confirm fail-closed exits on a lookahead `--fold`, `--objective` without
   `--direction`, zero rolling args, and an unknown flag.
3. **Step 3 (AC):** `cargo test -p atp-simulation --test srs_bt_008_walk_forward`
   and `--test srs_bt_008_cli` — in-sample optimization matches hand-derived rank-1,
   OOS evaluation matches an independent run, per-window params+metrics preserved,
   no-lookahead enforced.
4. **Step 4 (evidence, passes stays false):** record per-step PASS/FAIL; keep
   `passes:false` (serialized).

Gate before integrate: `tools/run_ci_locally.sh`, `cargo test --workspace`,
`pytest -m "not integration and not e2e"`, `cargo fmt --check`, `cargo clippy
--workspace -D warnings`, `ruff check`/`ruff format` on new files; then
`python3 tools/critic_check.py --staged` (deterministic) and
`python3 tools/adversarial_review.py origin/main` (judgment) — both must APPROVE.

## Endgame

`python3 tools/agent_pool.py block SRS-BT-008 --on SRS-BT-007 --reason "walk-forward
reuses BT-007 SweepRunner/SweepStrategyFactory; a non-fixture close needs the
deferred Python strategy host (SRS-BT-001, BT-007's own blocker)"`, then
`integrate SRS-BT-008 --mode serialized`. Write `progress.d/session-SRS-BT-008.md`.
Confirm with the operator before claiming the next feature.
