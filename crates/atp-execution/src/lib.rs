use atp_strategy_engine::StrategyRuntimeBoundary;
use atp_types::RuntimeService;

#[derive(Debug, Default)]
pub struct ExecutionEngine;

impl ExecutionEngine {
    pub fn service(&self) -> RuntimeService {
        RuntimeService::ExecutionEngine
    }

    pub fn accepts_live_boundary(&self, boundary: &StrategyRuntimeBoundary) -> String {
        format!("live-order-boundary:{}", boundary.strategy_id().as_str())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use atp_data::DataLayer;
    use atp_types::StrategyId;

    #[test]
    fn is_a_rust_execution_service_boundary() {
        let boundary = StrategyRuntimeBoundary::new(StrategyId::new("live-1"), DataLayer);
        let engine = ExecutionEngine;
        assert_eq!(engine.service(), RuntimeService::ExecutionEngine);
        assert_eq!(engine.accepts_live_boundary(&boundary), "live-order-boundary:live-1");
    }
}
