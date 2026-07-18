# SRS-BT-007 — Grid search & multidimensional parameter sweeps

## Context

Feature `SRS-BT-007` (P2, verification=Test, SyRS SYS-19, StRS SN-1.16/SC-12):
"support grid search and multidimensional parameter sweeps for backtests. AC: A
parameter space definition produces ranked backtest results by the selected
objective function." SyRS names the example objectives: maximize Sharpe,
minimize max drawdown.

All building blocks exist and are green: deterministic `BacktestEngine`
(BT-002/003/010), the 8-metric `PerformanceMetrics` family via
`benchmark::compare` (BT-004/005), and `BacktestResultStore` whose
`StrategyParameters` (canonical order-independent string map) is already the
"one point of a parameter sweep" identity (BT-009). **No grid/sweep/objective
code exists anywhere.** Gap: nothing bridges a `StrategyParameters` point to a
configured `BacktestStrategy` — BT-007 introduces that seam. Sibling BT-008
(walk-forward, unbuilt) will reuse the sweep runner per in-sample window, so the
core is a library call, CLI on top.

**Expected completeness: `complete`** — every step runs solo (fixture bars,
fixture benchmark, local CLI, cargo/pytest). No IB/integration/e2e. Precedent:
BT-006 closed green on the same CLI-over-fixture-engine pattern.

## Files (all one feat commit — see Sequencing)

### 1. `crates/atp-simulation/src/sweep.rs` (new — the core)

- `ParameterAxis::new(name, values) -> Result` / `ParameterSpace::new(axes)`.
  Fail-closed `SweepError` variants for: zero axes, empty/whitespace axis name,
  duplicate axis name, empty value list, empty value token, duplicate value in
  an axis (would create identical points → ambiguous ranking).
  `point_count() -> u128` via `checked_mul` BEFORE materialization;
  `MAX_SWEEP_POINTS = 10_000` cap → `TooManyPoints` before any backtest runs.
  `points() -> Vec<StrategyParameters>` — deterministic Cartesian product
  (axes sorted by name, values in declared order), reuses
  `StrategyParameters::from_pairs`.
- `ObjectiveMetric` enum — all 8 metrics (sharpe_ratio, sortino_ratio, alpha,
  beta, max_drawdown, annualized_return, annualized_volatility, win_rate);
  `parse()` allowlist fails closed on unknown tokens; `value(&PerformanceMetrics)
  -> Option<f64>`. `Direction { Maximize, Minimize }`;
  `ObjectiveFunction { metric, direction }` + conveniences `maximize_sharpe()`,
  `minimize_max_drawdown()` (the SyRS-named pair).
- `trait SweepStrategyFactory { type Strategy: BacktestStrategy;
  fn build(&self, &StrategyParameters) -> Result<Self::Strategy, SweepError> }`
  — the missing bridge; fails closed on missing/unknown/unparseable parameter
  (never silently defaults). Static dispatch (associated type, not Box<dyn>).
- `SweepRunner::run(&SweepRequest, &factory, &impl BarSource,
  &SweepEvaluation)` — sequential, deterministic: per point
  `factory.build` → `BacktestEngine::run` → `benchmark::compare` → objective
  extraction. `SweepRequest { base: BacktestRequest, space, objective }`;
  `SweepEvaluation` groups benchmark selection/source/metrics-config (keeps
  clippy arg count down). `with_max_points()` test seam.
- `SweepReport { objective, total_points, ranked: Vec<RankedPoint>,
  unranked: Vec<UnrankedPoint> }` (PartialEq for determinism tests).
  `RankedPoint { rank (1-based), parameters, objective_value (finite),
  metrics, comparison, final_equity_minor, trade_count }` — metrics-only per
  point, no curves/logs (bounded memory at 10k points).

Decided semantics (fail-closed, each pinned by a test):
- **`None` objective → `unranked` bucket** with reason `objective_undefined`
  — never ranked-last, never fabricated 0, never dropped
  (`total_points == ranked + unranked` proves accounting). Preserves the
  metrics module's "None = undefined, never fabricated" contract.
- **Ordering**: `f64::total_cmp` (desc for Maximize, asc for Minimize);
  **tie-break = ascending canonical `StrategyParameters::entries()` lex order**
  (already sorted; strict total order since points are pairwise distinct).
- **Any per-point failure** (factory reject / BacktestError / BenchmarkError)
  → whole sweep fails closed, `PointFailed { parameters, reason }` naming the
  offending point. Partial rankings could mis-rank; BT-008 needs all-or-error.
- **Non-finite objective** → `NonFiniteObjective` (defense-in-depth; compare
  already guarantees finiteness).
- **No persistence in the runner/CLI** — caller composes with the BT-009 store
  (`BacktestRecord::from_result`) if wanted; keeps the sweep pure and keeps
  future BT-008 in-sample sweeps from spamming history.

Also: `lib.rs` gains `pub mod sweep;` + doc paragraph; every source file
references SRS-BT-007 (critic rule).

### 2. `crates/atp-simulation/src/bin/bt007_sweep_cli.rs` (new) + Cargo.toml `[[bin]]`

Mirrors `bt009_store_cli` (hand-rolled allowlist flag loop, USAGE const,
ExitCode, `--format human|kv`, kv control-char guard):

```
bt007_sweep_cli run [--axis name=v1,v2,...]... [--objective <metric> --direction <max|min>] [--format human|kv]
```

- Fixture producer: shared 5-bar FixtureCatalog (bt009's bars) + parameterized
  `ParamRoundTrip { lot, sell_ts }` strategy with an in-binary factory parsing
  `lot`/`sell_ts` fail-closed — different points genuinely change
  Sharpe/drawdown, so ranking is real.
- Defaults: no `--axis` → demo space `lot=5,10,20 × sell_ts=3,5`; no
  `--objective` → maximize sharpe_ratio (stated in output). **Explicit
  `--objective` requires explicit `--direction`** (refuse to guess).
- Fail-closed parsing: unknown flag/metric/direction/format, missing `=`,
  empty name/value, duplicate `--axis` name, control chars → error + non-zero,
  no partial output.
- Human: space definition, point count, objective, best-first ranked table
  (undefined metrics `n/a`), `unranked:` section. kv: `objective.*`,
  `point_count`, `ranked_count`, `unranked_count`, `ranked.<i>.rank/.objective_value/.param.<j>.key/.value/.metric.*/.comparison.*`,
  `unranked.<i>.reason` — contiguous indices.

### 3. `architecture/runtime_services.json` — new `sim_parameter_sweep_contract` block

Sibling of `sim_backtest_store_contract`: ac_traces SRS-BT-007, SYS-19,
SN-1.16; sweep module + bin tokens (`ParameterSpace`, `ObjectiveFunction`,
`SweepStrategyFactory`, `SweepReport`, `total_cmp`, `checked_mul`,
`MAX_SWEEP_POINTS`, `ObjectiveUndefined`, `PointFailed`); `deferred[]` naming
the honest deferrals: real Python-strategy factory (deferred host), REST/UI
sweep surface (SRS-API-001/UI owners), stored-data benchmark resolver
(SRS-BT-005 pattern), walk-forward consumer (SRS-BT-008).

### 4. `tools/backtest_sweep_check.py` (new gate script)

Mirrors `tools/backtest_store_check.py`: surface check, lib.rs re-export,
no-broker-dep, vendor isolation, determinism (no par_iter/rand/Instant/
SystemTime tokens), objective-ranking pinned to `total_cmp` + Direction match,
None-fail-closed routing (ObjectiveUndefined token), point cap
(`checked_mul` + TooManyPoints before materialization), space validation.
Prints `SRS-BT-007 SDK-SURFACE PASS` naming deferred owners.
**No ci.yml / run_ci_locally.sh edits** — verified: per-feature check scripts
are wired via their pytest consumers (contract + domain tests), not the CI
check loop (that loop is architecture-level scripts only).

### 5. Tests

- `crates/atp-simulation/tests/srs_bt_007_parameter_sweep.rs`: (1) 3×2
  Cartesian coverage + deterministic order; (2) rank-by-max-sharpe vs
  hand-computed compare() per point; (3) minimize max_drawdown flips rank 1;
  (4) undefined objective → unranked, not fabricated; (5) repeat-run
  SweepReport equality; (6) tie-break by canonical param order (two sell_ts
  past last bar → identical results); (7) each degenerate-space variant → its
  exact SweepError; (8) cap fails before any backtest (counting factory proves
  0 builds); (9) factory rejection names offending point; (10) engine error
  fails sweep closed; (11) all 8 metric tokens + max/min round-trip, unknown
  fails closed.
- `crates/atp-simulation/tests/srs_bt_007_cli.rs`: CARGO_BIN_EXE pattern —
  default demo run; min-drawdown flips rank 1 vs max run; kv grammar; two
  identical invocations → byte-identical stdout; fail-closed non-zero exits.
- `tests/domain/test_backtest_parameter_sweep.py` (**same commit** — critic
  paired-test rule for `crates/atp-simulation/`, verified
  `critic_check.py:286`): mirrors `test_backtest_record_query.py` — safety
  framing (mis-ranked sweep mis-allocates capital); shells the key cargo tests
  `--exact`; imports check functions and proves each non-vacuous via in-memory
  source mutations (inject par_iter, swap total_cmp, drop ObjectiveUndefined
  routing, neuter cap, drop re-export → each caught).
- `tests/test_backtest_sweep_contract.py`: mirrors
  `test_backtest_store_contract.py` — shells the check script end-to-end
  (PASS line + deferred owners), negative text-mutation coverage per check.

## Sequencing

Single feat commit (sweep.rs + CLI + Cargo.toml + lib.rs + metadata + gate
script + all four test files + feature_list notes untouched — integrate flips).
No prep commit: the gate script pins sweep.rs tokens so it can't land alone,
and the domain pairing must share the commit range anyway. Then chore commit
with `progress.d/session-SRS-BT-007.md`.

## Verification (Step 6 walk)

1. `./init.sh` → "✓ Environment ready".
2. `cargo test -p atp-simulation --test srs_bt_007_parameter_sweep --test
   srs_bt_007_cli`; run `bt007_sweep_cli run --axis lot=5,10,20 --axis
   sell_ts=3,5 --objective max_drawdown --direction min` + default run,
   inspect ranked output both formats.
3. AC: parameter space definition (`--axis` flags → ParameterSpace) produces
   ranked results (best-first table with 1-based ranks) by selected objective
   (metric+direction) — asserted clause-by-clause in tests 2/3 and the CLI test.
4. Full gate: `tools/run_ci_locally.sh`, `cargo test --workspace`,
   `pytest -m "not integration and not e2e"`, critic_check --staged,
   `adversarial_review.py origin/main` (record reviewer). Then
   `agent_pool.py integrate SRS-BT-007 --mode complete`.

## Risks

- Lexicographic tie-break ("10" < "5" as strings) — determinism is the
  requirement, not numeric intuition; documented + tested.
- Token-pinned structural checks are brittle — keep script/source tokens in
  lockstep; every structural assertion made non-vacuous per house style.
- `architecture_check.py` must tolerate the new JSON block (~80 precedent
  blocks say yes; confirm before authoring).
