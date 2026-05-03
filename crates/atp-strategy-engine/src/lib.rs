use atp_data::DataLayer;
use atp_types::{RuntimeService, StrategyId};

#[derive(Debug)]
pub struct StrategyRuntimeBoundary {
    strategy_id: StrategyId,
    data_layer: DataLayer,
}

impl StrategyRuntimeBoundary {
    pub fn new(strategy_id: StrategyId, data_layer: DataLayer) -> Self {
        Self {
            strategy_id,
            data_layer,
        }
    }

    pub fn service(&self) -> RuntimeService {
        RuntimeService::StrategyEngine
    }

    pub fn strategy_id(&self) -> &StrategyId {
        &self.strategy_id
    }

    pub fn data_query_owner(&self) -> &'static str {
        self.data_layer.query_owner(&self.strategy_id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn owns_runtime_boundary_without_python_strategy_logic() {
        let boundary = StrategyRuntimeBoundary::new(StrategyId::new("s1"), DataLayer);
        assert_eq!(boundary.service(), RuntimeService::StrategyEngine);
        assert_eq!(boundary.data_query_owner(), "data-layer");
    }
}
