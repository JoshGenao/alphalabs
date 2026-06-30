=== SESSION SRS-BT-004 ===
Date: 2026-06-30
Feature: SRS-BT-004 — "compute required backtest and paper/live performance metrics"
(docs/SRS.md SRS-5.6; traces SyRS SYS-16 / SYS-86; StRS SN-1.04 / SN-1.05 / SN-1.29).
Outcome: serialized (code integrates; passes STAYS false — the live dashboard reporting
context (SRS-UI/SRS-API, SYS-36 <=5s) is an unbuilt named AC context).

PRIOR PROGRESS (Session 44, commit 145908a): the 8-metric family `metrics::compute`
(Sharpe/Sortino/alpha/beta/max-drawdown/annualized-return/annualized-vol/win-rate) was
built + fully tested, but SRS-BT-004 stayed passes:false with 4 deferred owners:
(1) live dashboard reporting, (2) the paper/live runtime metric ACCUMULATOR that feeds
the family, (3) SRS-BT-005 benchmark resolution, (4) atomic run-snapshot identity.
The AC names THREE reporting contexts: completed backtests (demonstrated — integration
test runs a real BacktestEngine -> compute), paper strategies (deferred owner #2,
UNBUILT), live dashboard (deferred owner #1, UNBUILT UI surface).

WHAT I DID — built deferred owner #2, the in-scope half: the PAPER-STRATEGY metrics
ACCUMULATOR (SYS-86: the internal sim engine computes the SAME family for paper
strategies the backtest engine does). It is deterministic + dependency-free and
demonstrable solo over fixtures; the runtime that SUPPLIES the marks at production time
(SYS-70 feed) + the live dashboard / SRS-SIM-004 persisted-slot wiring stay deferred —
so SRS-BT-004 stays passes:false (serialized).

Implementation (1 feat commit; NO prep commit — SAFETY_PATH_RE already matches
paper_metrics via S44's `paper[_-]?metric`/`metric` tokens):
- crates/atp-simulation/src/paper_metrics.rs (NEW): `PaperMetricsAccumulator`. Holds a
  starting-cash baseline, running cash (i128), a SYS-84 StrategyLedger, a trade log, and
  a mark-to-market net-liq equity curve. apply_fill(PaperFill) -> ledger validates +
  cash += cash_delta_minor + append Fill; mark(ts, &[(symbol, mark_minor)]) -> net-liq =
  cash + sum(position.market_value_minor) over OPEN positions; compute_metrics() ->
  delegates to the SHARED metrics::compute (so paper == backtest by construction).
- crates/atp-simulation/src/virtual_ledger.rs: added VirtualPosition::market_value_minor
  (mark*quantity; == cost_basis + unrealized_pnl; fail-closed NonPositiveMark) — the
  net-liq building block + 4 unit tests.
- crates/atp-simulation/src/bin/sim_paper_metrics_cli.rs (NEW): operator CLI; `paper`
  renders the 8 paper metrics, `parity` runs the SAME activity as a backtest and prints
  `paper-backtest-parity:true` (CostConfig::zero so the fixtures mirror exactly).
- lib.rs `pub mod paper_metrics;` + doc; sweep of the metrics/ lib doc.
- architecture/runtime_services.json: sim_metrics_contract.paper_accumulator block +
  updated deferred[] (accumulator now EXISTS; live-feed + dashboard wiring deferred).
- tools/metrics_check.py: check_paper_accumulator collector (20th static check) +
  updated _DEFERRED_OWNERS.

FAIL-CLOSED guards (the accumulator's hazard is fabricating equity):
- MissingMark: an OPEN position with no supplied mark is rejected (never valued at 0).
- NonPositiveMark: a non-positive quote rejected even for an UNHELD symbol (corrupt data).
- DuplicateMark / NonMonotonicMarkTimestamps / NonMonotonicFill / NonPositiveStartingCash.
- CROSS-STREAM coherence (Codex round-1 [high] fix): FillBeforeMark (a fill at/before an
  already-recorded mark would retroactively change an already-valued position) +
  MarkBeforeFill (a mark before an applied fill would value a past instant with a future
  position). The within-bar apply-fills-then-mark order (equal ts) is permitted.
- compute coherence (TradeLogOutsideRun) surfaces a fill outside the marked window.

What I tested (per AC step):
- Step 1 (init): ./init.sh -> "Environment ready" (PASS).
- Step 2 (CLI/API workflow): ./target/debug/sim_paper_metrics_cli paper -> 8 metrics
  rendered; `parity` -> paper-backtest-parity:true, exit 0; error paths (bad subcommand,
  non-canonical --benchmark) exit 1 (PASS).
- Step 3 (AC): the 8 metrics are produced for completed backtests (existing integration
  test) AND paper strategies (new accumulator). Integration test
  srs_bt_004_paper_metrics_match_the_backtest_family[_with_costs] asserts paper metrics
  EQUAL the backtest family for the same activity (SYS-86). Live dashboard context UNBUILT.
- Step 4: passes stays false (honest serialized) — live dashboard reporting unbuilt.
- cargo test --workspace: 1151+ green (exit 0); paper_metrics 20 lib tests; srs_bt_004
  integration 18 tests; virtual_ledger market_value 4 tests.
- pytest -m "not integration and not e2e": 2539 passed / 4 pre-existing pending-impl skips.
- metrics_check.py --require-cargo PASS (21 evidence items incl. paper_accumulator).
- cargo fmt --check clean (new files only, NO whole-crate fmt); clippy --workspace
  -D warnings clean; ruff check+format clean on my Python.

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — 0 findings.
  judgment (tools/codex_review.sh origin/main): round 1 needs-attention [high] —
    "accumulator does not serialize fill/mark event order" (out-of-order interleaving ->
    time-incoherent curve compute can't detect). FIXED (FillBeforeMark/MarkBeforeFill
    cross-stream guards + 3 unit tests + L3/L7 non-vacuity checks + contract tokens).
    round 2: APPROVE — no material findings.

Resume / next:
- SRS-BT-004 STILL passes:false BY DESIGN. Remaining deferred owners to flip it:
  (1) live dashboard performance reporting (SRS-UI / SRS-API; SYS-36 <=5s) — the only
      remaining UNBUILT named AC context; (3) SRS-BT-005 benchmark resolution; the
      production runtime that SUPPLIES the accumulator's marks from the SYS-70 feed +
      wires its output into the SRS-SIM-004 persisted metrics slot.
- The paper accumulator is the load-bearing piece a live/paper runtime (SRS-EXE-002
  orchestrator / SYS-70 subscription) calls each cadence; that runtime + the dashboard
  are what finally flip SRS-BT-004.
- Reusable shape: net-liq = cash + sum(position.market_value_minor); fill-then-mark per
  bar; cross-stream ordering must be serialized (don't track fill/mark ts independently).
