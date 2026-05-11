use atp_strategy_engine::StrategyRuntimeBoundary;
use atp_types::{
    OrderErrorCategory, OrderReceipt, OrderSubmission, RuntimeService, StrategyMode,
    StructuredOrderError,
};

#[derive(Debug, Default)]
pub struct ExecutionEngine;

/// Port trait the execution engine uses to push an order to a live brokerage
/// after it has decided the submission is allowed (mode == Live, connected,
/// data not stale, etc.). Lives at the execution layer — adapter crates do
/// not implement it directly; the orchestrator wires an adapter to it.
///
/// Defining the port here keeps `atp-execution` independent of
/// `atp-adapters` (SRS-ARCH-002 dependency direction: adapters are a
/// sibling crate, not an upstream dep of execution).
pub trait LiveBrokerageSubmit {
    fn submit_order(
        &self,
        submission: OrderSubmission,
    ) -> Result<OrderReceipt, StructuredOrderError>;
}

impl ExecutionEngine {
    pub fn service(&self) -> RuntimeService {
        RuntimeService::ExecutionEngine
    }

    pub fn accepts_live_boundary(&self, boundary: &StrategyRuntimeBoundary) -> String {
        format!("live-order-boundary:{}", boundary.strategy_id().as_str())
    }

    /// ERR-1 / SRS-EXE-001 / SRS-ERR-001: route an order to the live broker
    /// only if the submitting strategy is in `Live` mode. Non-live attempts
    /// are rejected **synchronously** with a structured error, before the
    /// brokerage port is invoked — so a `Paper` submission cannot produce
    /// any IB order side effect.
    pub fn submit_live_order<B: LiveBrokerageSubmit>(
        &self,
        mode: StrategyMode,
        submission: OrderSubmission,
        broker: &B,
    ) -> Result<OrderReceipt, StructuredOrderError> {
        match mode {
            StrategyMode::Live => broker.submit_order(submission),
            StrategyMode::Paper => Err(StructuredOrderError {
                category: OrderErrorCategory::NonLiveStrategySubmission,
                error_type: "NonLiveLiveRouteBlocked".to_string(),
                message: format!(
                    "strategy `{}` is not the designated live strategy; \
                     live IB execution path is reserved for the single \
                     live strategy (SRS-EXE-001)",
                    submission.strategy_id.as_str()
                ),
                original_order: submission,
            }),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use atp_data::DataLayer;
    use atp_types::StrategyId;
    use std::cell::Cell;

    struct CountingBroker {
        calls: Cell<u32>,
    }

    impl CountingBroker {
        fn new() -> Self {
            Self {
                calls: Cell::new(0),
            }
        }
    }

    impl LiveBrokerageSubmit for CountingBroker {
        fn submit_order(
            &self,
            submission: OrderSubmission,
        ) -> Result<OrderReceipt, StructuredOrderError> {
            self.calls.set(self.calls.get() + 1);
            Ok(OrderReceipt {
                broker_order_id: format!("ib-{}", submission.symbol),
            })
        }
    }

    #[test]
    fn is_a_rust_execution_service_boundary() {
        let boundary = StrategyRuntimeBoundary::new(StrategyId::new("live-1"), DataLayer);
        let engine = ExecutionEngine;
        assert_eq!(engine.service(), RuntimeService::ExecutionEngine);
        assert_eq!(
            engine.accepts_live_boundary(&boundary),
            "live-order-boundary:live-1"
        );
    }

    #[test]
    fn live_strategy_submission_is_routed_to_the_broker() {
        let engine = ExecutionEngine;
        let broker = CountingBroker::new();
        let submission = OrderSubmission {
            strategy_id: StrategyId::new("live-1"),
            symbol: "AAPL".to_string(),
            quantity: 10,
        };

        let receipt = engine
            .submit_live_order(StrategyMode::Live, submission, &broker)
            .expect("live mode must route through the brokerage port");

        assert_eq!(receipt.broker_order_id, "ib-AAPL");
        assert_eq!(broker.calls.get(), 1);
    }

    #[test]
    fn paper_strategy_submission_is_rejected_synchronously_with_no_broker_call() {
        // ERR-1: A non-live strategy submitting an order down the live
        // execution path must be rejected synchronously with a structured
        // error AND must produce no IB order side effect.
        let engine = ExecutionEngine;
        let broker = CountingBroker::new();
        let submission = OrderSubmission {
            strategy_id: StrategyId::new("paper-research-3"),
            symbol: "TSLA".to_string(),
            quantity: 5,
        };

        let error = engine
            .submit_live_order(StrategyMode::Paper, submission.clone(), &broker)
            .expect_err("paper mode must be rejected on the live path");

        assert_eq!(
            error.category,
            OrderErrorCategory::NonLiveStrategySubmission
        );
        assert_eq!(error.error_type, "NonLiveLiveRouteBlocked");
        assert!(error.message.contains("paper-research-3"));
        assert!(error.message.contains("SRS-EXE-001"));
        assert_eq!(error.original_order, submission);
        assert_eq!(
            broker.calls.get(),
            0,
            "the broker port must not be invoked when mode is Paper"
        );
    }

    #[test]
    fn structured_error_display_includes_category_wire_string() {
        let engine = ExecutionEngine;
        let broker = CountingBroker::new();
        let submission = OrderSubmission {
            strategy_id: StrategyId::new("paper-x"),
            symbol: "MSFT".to_string(),
            quantity: 1,
        };
        let error = engine
            .submit_live_order(StrategyMode::Paper, submission, &broker)
            .unwrap_err();
        assert!(format!("{error}").contains("NON_LIVE_STRATEGY_SUBMISSION"));
    }
}
