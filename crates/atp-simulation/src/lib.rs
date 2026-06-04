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
