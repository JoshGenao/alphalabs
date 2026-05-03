use atp_types::RuntimeService;

#[derive(Debug, Default)]
pub struct MarketDataSubscriptionManager;

impl MarketDataSubscriptionManager {
    pub fn service(&self) -> RuntimeService {
        RuntimeService::MarketDataSubscriptionManager
    }

    pub fn owns_subscription_fanout(&self) -> bool {
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifies_market_data_subscription_manager() {
        let manager = MarketDataSubscriptionManager;
        assert_eq!(manager.service(), RuntimeService::MarketDataSubscriptionManager);
        assert!(manager.owns_subscription_fanout());
    }
}
