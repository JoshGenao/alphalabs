use atp_types::RuntimeService;

#[derive(Debug, Default)]
pub struct StrategyOrchestrator;

impl StrategyOrchestrator {
    pub fn service(&self) -> RuntimeService {
        RuntimeService::StrategyOrchestrator
    }

    pub fn owns_strategy_container_lifecycle(&self) -> bool {
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifies_strategy_orchestrator() {
        let orchestrator = StrategyOrchestrator;
        assert_eq!(orchestrator.service(), RuntimeService::StrategyOrchestrator);
        assert!(orchestrator.owns_strategy_container_lifecycle());
    }
}
