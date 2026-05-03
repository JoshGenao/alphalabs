use atp_types::{RuntimeService, StrategyId};

#[derive(Debug, Default)]
pub struct DataLayer;

impl DataLayer {
    pub fn service(&self) -> RuntimeService {
        RuntimeService::DataLayer
    }

    pub fn query_owner(&self, _strategy_id: &StrategyId) -> &'static str {
        "data-layer"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifies_data_layer_service() {
        let layer = DataLayer;
        assert_eq!(layer.service(), RuntimeService::DataLayer);
    }
}
