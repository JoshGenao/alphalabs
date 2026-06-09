use atp_strategy_engine::StrategyRuntimeBoundary;
use atp_types::RuntimeService;

/// Deterministic backtest engine (SRS-BT-001). Co-located with the internal
/// simulation engine because SRS-BT-003 mandates a shared transaction-cost model
/// family for paper simulation and backtesting.
pub mod backtest;

/// The configurable transaction-cost model family (SRS-BT-002): commission,
/// slippage, and spread-impact models with SyRS-matching defaults. The backtest
/// engine applies it to fills; the internal simulation engine shares the same
/// family for paper fills (SRS-BT-003).
pub mod cost;

/// The internal simulation engine's paper-fill cost path (SRS-BT-003). It
/// consumes the SAME [`cost::CostConfig`] family the [`backtest`] engine applies
/// — defaulting to the identical SyRS baseline (SYS-15e) — so a paper strategy
/// and a backtest with identical cost configuration compute fills and
/// commissions from the same model family.
pub mod sim;

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
/// every other strategy and of the IB account's actual positions. The deferred
/// halves (the SYS-70 live feed, SYS-88 corporate actions / SRS-DATA-021,
/// SYS-89 persistence / SRS-SIM-004, SYS-85 paper metrics, SRS-EXE-002
/// orchestrator routing, and the Python runtime) keep SRS-SIM-003 at
/// `passes:false`.
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
