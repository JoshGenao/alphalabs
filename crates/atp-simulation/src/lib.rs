use atp_strategy_engine::StrategyRuntimeBoundary;
use atp_types::RuntimeService;

/// Deterministic backtest engine (SRS-BT-001). Co-located with the internal
/// simulation engine because SRS-BT-003 mandates a shared transaction-cost model
/// family for paper simulation and backtesting.
pub mod backtest;

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

/// The internal simulation engine's paper-state persistence path (SRS-SIM-004 /
/// SyRS SYS-89). It captures a [`virtual_ledger::VirtualLedgerBook`] plus the
/// [`paper_state::PersistenceConfig`] cadence (default 60s interval, 30s restore
/// deadline) into a versioned [`paper_state::PaperStateSnapshot`], serializes it to
/// a deterministic, dependency-free text form (sorted keys, length-prefixed
/// symbols), and restores it fail-closed on a corrupt or tampered blob. Only the
/// virtual ledger has a runtime type today, so the pending-order, metric, and
/// user-state sub-states SYS-89 also names are reserved, forward-compatible slots;
/// the live 60s timer + 30s-restore container wiring (SRS-EXE-002 / SYS-89), the
/// paper-order pending store, the SYS-85 / SRS-BT-004 metric family, and the Python
/// runtime are deferred, so SRS-SIM-004 stays `passes:false`.
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
/// reporting (SYS-86); the live dashboard path, the paper/live runtime accumulators
/// that feed it (the SRS-SIM-004 snapshot reserves the metrics slot for them), and
/// the SRS-BT-005 benchmark-resolution surface are deferred, so SRS-BT-004 stays
/// `passes:false`.
pub mod metrics;

/// Benchmark selection, resolution, and comparison (SRS-BT-005 / SyRS SYS-17, SYS-36,
/// SYS-37). It wraps the [`metrics`] family: [`benchmark::BenchmarkSelection`] resolves
/// to SPY when the operator selects none; the [`benchmark::BenchmarkSource`] port turns
/// a selected [`metrics::Benchmark`] into the integer-minor level series
/// [`metrics::compute`] needs (the real stored-data resolver is the deferred SRS-DATA-007
/// owner); and [`benchmark::compare`] computes alpha/beta against the resolved benchmark
/// and packages a [`benchmark::BenchmarkComparison`] that identifies it. The resolved
/// series is re-validated fail-closed at the source trust boundary before any metric is
/// reported. The CLI rendering surface is realized by the `benchmark_comparison_cli` binary
/// (a default run identifies SPY; `--benchmark` selects another; undefined statistics render
/// as `undefined` and every trust-boundary fault fails closed), but SRS-BT-005 stays
/// `passes:false`: the AC requires the web dashboard AND backtest reports to identify the
/// benchmark, and the dashboard / REST identification (SRS-UI / SRS-API, SYS-36 <=5s) is not
/// built. Resolving the benchmark's actual historical levels from stored data (SRS-DATA-007,
/// behind the fixture source the CLI uses) also remains deferred.
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
