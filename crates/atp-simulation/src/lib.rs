use atp_strategy_engine::StrategyRuntimeBoundary;
use atp_types::RuntimeService;

/// Deterministic backtest engine (SRS-BT-001). Co-located with the internal
/// simulation engine because SRS-BT-003 mandates a shared transaction-cost model
/// family for paper simulation and backtesting.
pub mod backtest;

/// The backtest **launch surface** binding (SRS-BT-001). The [`backtest`] engine
/// takes a configurable [`backtest::DateRange`] of opaque `u64` timestamps and
/// leaves "binding them to wall-clock calendar dates [as] the launch surface's
/// concern"; [`launch::parse_window`] is that binding — it parses operator-supplied
/// `YYYY-MM-DD` start/end dates into the engine's inclusive epoch-second range,
/// failing closed on a malformed / impossible / pre-epoch / inverted window. This
/// realizes the "start and end dates are selectable" half of the acceptance
/// criterion with real, reusable, integer-only (dependency-free) code that the
/// `bt001_backtest_cli` operator binary uses and the deferred REST/dashboard launch
/// surfaces (SRS-API-001 / SRS-UI) can reuse. The user-uploaded Apache Parquet
/// reader and the Rust<->Python strategy host remain deferred, so SRS-BT-001 stays
/// `passes:false`.
pub mod launch;

/// The backtest engine's SRS-DATA-007 system-catalog reader
/// ([`store_bar_source::StoreBarSource`]). It implements [`backtest::BarSource`] over the durable
/// [`atp_data::store::MarketDataStore`], reading the unified, source-neutral historical query path
/// ([`atp_data::store::MarketDataStore::query_unified`] raw / `query_split_adjusted` gated) — so the
/// backtest engine queries by symbol / date range / resolution with NO provider named. This is the
/// real backtest consumer the SRS-DATA-007 acceptance names; the `BacktestDataSource::SystemData`
/// seam is now served by shipped product code, not a test stand-in. (The user-uploaded Parquet
/// reader for `BacktestDataSource::UploadedData` and the Python strategy host remain deferred — see
/// `backtest_contract`.)
pub mod store_bar_source;

/// The configurable transaction-cost model family (SRS-BT-002): commission,
/// slippage, and spread-impact models with SyRS-matching defaults. The backtest
/// engine applies it to fills; the internal simulation engine shares the same
/// family for paper fills (SRS-BT-003).
pub mod cost;

/// The internal simulation engine's paper-fill cost path (SRS-BT-003). It
/// consumes the SAME [`cost::CostConfig`] family the [`backtest`] engine applies
/// — defaulting to the identical SyRS baseline (SYS-15e) — so a paper strategy
/// and a backtest with identical cost configuration compute fills and
/// commissions from the same model family. The acceptance criterion is proven
/// fill-for-fill by `srs_bt_003_shared_cost_family` and made operator-demonstrable
/// by the `bt003_shared_cost_cli` binary (`compare` → `cost-family-match:true`),
/// so SRS-BT-003 is `passes:true`.
pub mod sim;

/// The paper-engine HALTED lifecycle gate (SRS-SAFE-001 / SyRS SYS-44a; StRS SN-1.11). The kill
/// switch's acceptance criterion requires that "paper simulation engines transition to the HALTED
/// state with no further `on_fill` callbacks emitted". [`halt::HaltablePaperEngine`] owns a PRIVATE
/// [`sim::PaperSimulationEngine`] behind a sealed gate (private field, no accessor / Deref /
/// into_inner, not Clone): while Running it delegates fills
/// unchanged; once [`halt::HaltablePaperEngine::halt`] flips it to [`halt::PaperEngineState::Halted`]
/// (idempotently), [`halt::HaltablePaperEngine::simulate_fill`] refuses to PRODUCE a
/// [`sim::PaperFill`] and returns [`halt::HaltError::Halted`] — so no fill exists to drive an
/// `on_fill` callback (the domain-level realization, there is no callback runtime yet). The bare
/// [`sim::PaperSimulationEngine`] stays a public fill primitive, so this seals a HELD gate, not the
/// whole system; routing every non-live strategy onto a halt-aware engine is the deferred
/// SRS-EXE-002 orchestrator's job. This is ONE
/// named sub-component: the full kill-switch sequence (IB cancel/disconnect = SRS-EXE-006;
/// orchestrated activation + 5s NFR-P3 = SRS-EXE-002 / SAFE-001 runtime; SRS-LOG-001 1s HALTED
/// observability; email/SMS = SRS-NOTIF-001; dashboard/CLI/REST trigger = SRS-API-001 / SRS-UI) is
/// deferred, so SRS-SAFE-001 stays `passes:false`.
pub mod halt;
pub mod halt_fleet;

/// The internal simulation engine's paper order-intake path (SRS-SIM-001). It
/// accepts market/limit/stop/stop-limit, equity/option, and multi-leg composite
/// orders and routes every one to the internal simulation engine — there is no
/// brokerage routing variant, so paper orders create no IB API order calls
/// (SyRS SYS-82).
pub mod paper_order;

/// The internal simulation engine's fill-model / triggering path (SRS-SIM-002).
/// It turns a routed [`paper_order::OrderType`] plus a live [`fill_model::MarketSnapshot`]
/// (bid/ask/last/volume) into a [`fill_model::FillDecision`] — market fills at the
/// touch, limit on price cross, stop on a last crossing the stop, stop-limit on a
/// triggered stop then the limit rule (SyRS SYS-83) — capped at the bar's observed
/// volume (SYS-87b). A filled decision feeds [`sim::PaperSimulationEngine::simulate_fill`],
/// so a triggered fill flows through the SAME cost family the backtest engine uses.
pub mod fill_model;

/// The internal simulation engine's per-paper-strategy virtual position ledger
/// (SRS-SIM-003 / SyRS SYS-84). It consumes a priced [`sim::PaperFill`] and
/// maintains, per strategy and per symbol, the signed quantity, average cost,
/// realized P&L, and commission paid, plus an unrealized P&L marked to market
/// against a live [`fill_model::MarketSnapshot`]. Each strategy's ledger is an
/// independent map entry holding only virtual state, so it is independent of
/// every other strategy and of the IB account's actual positions. The
/// `sim003_ledger_cli` operator binary makes that isolation operator-demonstrable,
/// so SRS-SIM-003 is `passes:true`. The remaining items (the SYS-70 live feed,
/// SYS-88 corporate actions / SRS-DATA-021, SYS-89 persistence / SRS-SIM-004,
/// SYS-85 paper metrics, SRS-EXE-002 orchestrator routing, and the Python
/// runtime) are genuinely ADJACENT features -- separate requirements, NOT
/// contexts inside SRS-SIM-003's acceptance criterion.
pub mod virtual_ledger;

/// The paper engine's virtual RESTING-order store (SRS-DATA-021). The SRS-SIM-001
/// intake path routes an order without retaining it, so until this store no
/// accepted-but-unfilled paper order existed for a corporate action to reach (the
/// SRS-SIM-004 snapshot reserves an always-empty slot for exactly this).
/// [`virtual_orders::VirtualOrderBook`] holds intake-validated
/// ([`paper_order`]'s own `validate_leg` — one authority) [`paper_order::OrderLeg`]s
/// per [`atp_types::StrategyId`] under book-assigned ids; a cancelled order is
/// terminal and auditable, never deleted. Wiring the fill path to consume resting
/// orders and persisting the book into the SRS-SIM-004 reserved slot are the
/// adjacent owners' work.
pub mod virtual_orders;

/// Corporate-action application for the paper books (SRS-DATA-021 / SyRS SYS-88;
/// StRS SN-1.14 / SN-1.29). [`corporate_actions::apply_corporate_action`] adjusts
/// every paper strategy's [`virtual_ledger::VirtualPosition`] quantity and cost
/// basis for splits (exact `N/M`, basis invariant), cash dividends (additive
/// `basis − amount · quantity`), and stock-for-stock mergers / symbol changes
/// (remap with history intact), and cancels [`virtual_orders`] resting orders on
/// delisted / merged securities — per-position fail-closed to a manual-review
/// outcome (position untouched) and per-order fail-closed to a cancel, with
/// operator paging through the fallible
/// [`corporate_actions::PaperCorpActionAlertSink`] port.
/// [`corporate_actions::actions_from_facts`] binds the inputs to
/// `atp_data::MarketDataStore::query_corporate_action_facts` — the SAME
/// coverage-gated corporate-action data source the backtest engine's adjusted
/// reads and the live SRS-DATA-019/020 planners' composition roots consume — so
/// paper, live, and backtest derive from one record set. The money math is kept
/// semantically byte-stable with the SRS-DATA-020 live position planner.
pub mod corporate_actions;

/// The internal simulation engine's paper-state persistence path (SRS-SIM-004 /
/// SyRS SYS-89). It captures three of the four SYS-89 sub-states —
/// [`virtual_ledger::VirtualLedgerBook`], the per-strategy
/// [`paper_metrics::PaperMetricsAccumulator`] metrics, and the per-strategy
/// user-state dictionaries — plus the [`paper_state::PersistenceConfig`] cadence
/// (default 60s interval, 30s restore deadline) into a versioned
/// [`paper_state::PaperStateSnapshot`], serializes it to a deterministic,
/// dependency-free text form (sorted keys, length-prefixed symbols), persists it
/// atomically to disk ([`paper_state::PaperStateSnapshot::save_to_path`]), and
/// restores it fail-closed on a corrupt/tampered blob while enforcing the 30s restore
/// deadline ([`paper_state::recover_from_path`]). The fourth sub-state SYS-89 names,
/// pending simulated orders, has no runtime store yet, so it is a reserved,
/// forward-compatible slot. The live 60s timer + real container-restart wiring
/// (SRS-EXE-002 / SYS-89 lifecycle), the paper-order pending store, and the Python
/// strategy runtime that WRITES the user-state dictionary are deferred, so
/// SRS-SIM-004 stays `passes:false`.
pub mod paper_state;

/// The shared performance-metric family (SRS-BT-004 / SyRS SYS-16, SYS-86). It
/// computes the eight required metrics (Sharpe, Sortino, alpha, beta, maximum
/// drawdown, annualized return, annualized volatility, win rate) deterministically
/// from the [`backtest::EquityPoint`] curve and [`backtest::Fill`] trade log this
/// engine already produces, against a [`metrics::Benchmark`] that defaults to SPY.
/// Money enters in integer minor units; the metrics themselves are dimensionless
/// `f64` ratios, computed with fixed left-to-right folds (no parallelism, RNG, or
/// clock) so identical inputs yield identical metrics (SRS-BT-010). A metric that is
/// undefined on the input is reported `None` rather than a fabricated zero, and a
/// non-finite result fails closed. The same family serves backtest, paper, and live
/// reporting (SYS-86); the backtest path computes it from a [`backtest::BacktestResult`]
/// and the paper path from the [`paper_metrics`] accumulator, while the live dashboard
/// reporting path and the runtime that supplies the accumulator's marks (SYS-70 feed)
/// remain deferred, so SRS-BT-004 stays `passes:false`.
pub mod metrics;

/// The paper-strategy metric ACCUMULATOR (SRS-BT-004 / SyRS SYS-86). SYS-86 requires the
/// internal simulation engine to compute the SAME [`metrics`] family for paper strategies
/// as the backtest engine and live dashboard. This module is the paper-side producer of
/// the two primitives [`metrics::compute`] consumes: it accumulates the mark-to-market
/// net-liquidation equity curve (`cash + sum(position * mark)`, the identical quantity the
/// backtest engine marks) and the trade log from the SYS-84 [`virtual_ledger`] and the
/// simulated [`sim::PaperFill`] stream, then feeds them to the shared family — so a paper
/// run reports the metrics a backtest of the same activity would (a crate integration test
/// asserts the equality). It fails closed on a missing mark for an open position, a
/// non-positive/duplicate mark, or a non-monotonic mark/fill, and reads no clock/RNG so it
/// stays deterministic. The accumulator is demonstrable solo over fixtures and is persisted
/// and restored by the SRS-SIM-004 snapshot (its construction invariants re-validated
/// fail-closed on restore); the runtime that SUPPLIES the marks at production time (the SYS-70 subscription feed) and
/// the live dashboard reporting path are the deferred owners, so SRS-BT-004 stays
/// `passes:false`.
pub mod paper_metrics;

/// Benchmark selection, resolution, and comparison (SRS-BT-005 / SyRS SYS-17, SYS-36,
/// SYS-37). It wraps the [`metrics`] family: [`benchmark::BenchmarkSelection`] resolves
/// to SPY when the operator selects none; the [`benchmark::BenchmarkSource`] port turns
/// a selected [`metrics::Benchmark`] into the integer-minor level series
/// [`metrics::compute`] needs (the real stored-data resolver is the deferred (SRS-DATA-007 interface complete; real data = SRS-DATA-005 / SRS-FAC-001)
/// owner); and [`benchmark::compare`] computes alpha/beta against the resolved benchmark
/// and packages a [`benchmark::BenchmarkComparison`] that identifies it. The resolved
/// series is re-validated fail-closed at the source trust boundary before any metric is
/// reported. The CLI rendering surface is realized by the `benchmark_comparison_cli` binary
/// (a default run identifies SPY; `--benchmark` selects another; undefined statistics render
/// as `undefined` and every trust-boundary fault fails closed), but SRS-BT-005 stays
/// `passes:false`: the AC requires the web dashboard AND backtest reports to identify the
/// benchmark, and the dashboard / REST identification (SRS-UI / SRS-API, SYS-36 <=5s) is not
/// built. Resolving the benchmark's actual historical levels from stored data (read via the
/// now-complete SRS-DATA-007 interface; the resolver wiring is SRS-BT-005, behind the fixture
/// source the CLI uses) also remains deferred.
pub mod benchmark;

/// Completed-backtest result persistence + query (SRS-BT-009 / SyRS SYS-21, SYS-79). It
/// bundles the seven artifacts the acceptance names — the [`backtest::BacktestRequest`]
/// parameters, the [`metrics::PerformanceMetrics`] family (SRS-BT-004), the
/// [`backtest::Fill`] trade log, the [`backtest::EquityPoint`] equity curve, the
/// [`benchmark::BenchmarkComparison`] (SRS-BT-005), a strategy code version, and a
/// producer-supplied completion timestamp — into one queryable
/// [`backtest_store::BacktestRecord`], and holds them in a
/// [`backtest_store::BacktestResultStore`] that answers the three query axes (by strategy,
/// by date range, by parameter set) in a deterministic canonical order and serializes the
/// whole store to a checksummed, dependency-free text blob that restores fail-closed.
/// Trade-log/equity money stays integer minor units; the metric/comparison ratios round-trip
/// exactly via `f64::to_bits` and are verified finite on restore (SRS-BT-010). Writing the
/// blob to the SSD/NAS tier (SRS-DATA-008), rendering the history to an operator
/// (SRS-UI-004 / SRS-API), and a full orchestrated run that stamps real provenance
/// (SRS-BT-001 / orchestrator) are deferred, so SRS-BT-009 stays `passes:false`.
pub mod backtest_store;

/// Deterministic-backtest verification surface (SRS-BT-010 / SyRS SYS-62; StRS SN-1.02). The
/// [`backtest::BacktestEngine`] is already deterministic by construction; this module makes
/// that guarantee *falsifiable*. [`determinism::digest_result`] / [`determinism::digest_run`]
/// fold a [`backtest::BacktestResult`] (and, optionally, the [`metrics::PerformanceMetrics`]
/// family) into a stable [`determinism::RunDigest`] — the trade log and equity curve as
/// exact integer minor units, the dimensionless metric ratios via `f64::to_bits`, so there is
/// no float-formatting nondeterminism. [`determinism::runs_match`] /
/// [`determinism::metrics_match`] localize the first divergent artifact. Two harnesses run the
/// engine twice over identical inputs and fail closed with a localized
/// [`determinism::DeterminismError`] if the replays disagree: [`determinism::verify_reproducible`]
/// checks the **trade log + equity curve** (two of the three artifacts), and
/// [`determinism::verify_reproducible_with_metrics`] additionally computes the SRS-BT-004
/// [`metrics::PerformanceMetrics`] family for each run and compares it — the in-process check of
/// all three artifacts ("repeated runs produce identical trade logs, equity curves, *and*
/// metrics"). This is the in-process verification only: the cross-process operator repeated-run
/// workflow (which closes the platform-randomness clause across restarts) and the full
/// input-provenance manifest under the real Python strategy host are deferred, so SRS-BT-010 stays
/// `passes:false`.
pub mod determinism;

/// Grid search and multidimensional parameter sweeps (SRS-BT-007 / SyRS SYS-19; StRS
/// SN-1.16). A validated [`sweep::ParameterSpace`] (named axes, deterministic Cartesian
/// product, `checked_mul` cardinality cap enforced before any run) is evaluated point by
/// point through the SAME shipped chain a single backtest uses —
/// [`backtest::BacktestEngine`] then [`benchmark::compare`] — with each point's
/// [`backtest_store::StrategyParameters`] turned into a configured strategy by the
/// fail-closed [`sweep::SweepStrategyFactory`] seam. Results are ranked best-first by
/// the selected [`sweep::ObjectiveFunction`] (any of the eight SYS-16 metrics ×
/// max/min; SYS-19 names maximize-Sharpe and minimize-max-drawdown) via
/// `f64::total_cmp` with canonical-parameter tie-breaks; a point whose objective is
/// mathematically undefined is reported unranked (never a fabricated 0, never ranked
/// last, never dropped — [`sweep::SweepReport::total_points`] proves the accounting),
/// and any per-point failure aborts the sweep naming the point. The `bt007_sweep_cli`
/// binary is the operator surface over fixtures; the real Python-strategy factory
/// (deferred host), the REST/dashboard sweep surface (SRS-API-001 / SRS-UI), and the
/// SRS-BT-008 walk-forward consumer (which reuses [`sweep::SweepRunner::run`] per
/// in-sample window) are the deferred owners.
pub mod sweep;

#[derive(Debug, Default)]
pub struct InternalSimulationEngine;

impl InternalSimulationEngine {
    pub fn service(&self) -> RuntimeService {
        RuntimeService::InternalSimulationEngine
    }

    pub fn accepts_paper_boundary(&self, boundary: &StrategyRuntimeBoundary) -> String {
        format!(
            "paper-simulation-boundary:{}",
            boundary.strategy_id().as_str()
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use atp_data::DataLayer;
    use atp_types::StrategyId;

    #[test]
    fn is_a_rust_simulation_service_boundary() {
        let boundary = StrategyRuntimeBoundary::new(StrategyId::new("paper-1"), DataLayer);
        let engine = InternalSimulationEngine;
        assert_eq!(engine.service(), RuntimeService::InternalSimulationEngine);
        assert_eq!(
            engine.accepts_paper_boundary(&boundary),
            "paper-simulation-boundary:paper-1"
        );
    }
}
