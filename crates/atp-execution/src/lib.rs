use atp_strategy_engine::StrategyRuntimeBoundary;
use atp_types::{
    ConnectivityEvent, ConnectivityState, OrderErrorCategory, OrderReceipt, OrderSubmission,
    RuntimeService, StrategyMode, StructuredOrderError,
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

/// ERR-2 / SRS-SAFE-003 / SRS-MD-005: port the execution engine consults at
/// every live submission to decide whether the brokerage path is reachable,
/// and to request a reconnect when the gate is closed. The implementation
/// (later: the IB adapter wired by the orchestrator) owns the actual TCP
/// probe / readiness check / restart-window detection. Defining the port
/// here keeps the safety gate observable from execution without pulling
/// `atp-execution` into a dependency on `atp-adapters`.
pub trait BrokerageConnectivity {
    fn state(&self) -> ConnectivityState;
    fn request_reconnect(&self);
}

/// ERR-2 / SRS-SAFE-003 / SRS-MD-005: structured-event sink the execution
/// engine pushes a `ConnectivityEvent` into whenever it blocks a live
/// submission. Concrete implementations (later) route the event to logs,
/// the dashboard WebSocket (`ALERTS` / `ACCOUNT_STATUS` channels), and the
/// notification dispatcher (SRS-NOTIF-001).
pub trait ConnectivityEventSink {
    fn record(&self, event: ConnectivityEvent);
}

impl ExecutionEngine {
    pub fn service(&self) -> RuntimeService {
        RuntimeService::ExecutionEngine
    }

    pub fn accepts_live_boundary(&self, boundary: &StrategyRuntimeBoundary) -> String {
        format!("live-order-boundary:{}", boundary.strategy_id().as_str())
    }

    /// ERR-1 + ERR-2 / SRS-EXE-001 / SRS-ERR-001 / SRS-SAFE-003 / SRS-MD-005:
    /// route an order to the live broker only if (a) the submitting strategy
    /// is in `Live` mode AND (b) the IB Gateway is reachable. Both rejections
    /// are synchronous, return a `StructuredOrderError` matching the SyRS
    /// SYS-64 wire vocabulary, and produce zero IB order side effect.
    ///
    /// On `Unreachable` or `ScheduledRestartWindow`, the engine also
    /// publishes a `ConnectivityEvent` for downstream consumers and
    /// requests a reconnect — neither side effect runs the broker port.
    pub fn submit_live_order<B, C, E>(
        &self,
        mode: StrategyMode,
        submission: OrderSubmission,
        broker: &B,
        connectivity: &C,
        events: &E,
    ) -> Result<OrderReceipt, StructuredOrderError>
    where
        B: LiveBrokerageSubmit,
        C: BrokerageConnectivity,
        E: ConnectivityEventSink,
    {
        match mode {
            StrategyMode::Live => match connectivity.state() {
                ConnectivityState::Connected => broker.submit_order(submission),
                state @ (ConnectivityState::Unreachable
                | ConnectivityState::ScheduledRestartWindow) => {
                    events.record(ConnectivityEvent {
                        state,
                        strategy_id: submission.strategy_id.clone(),
                        symbol: submission.symbol.clone(),
                        scheduled_restart: matches!(
                            state,
                            ConnectivityState::ScheduledRestartWindow
                        ),
                    });
                    connectivity.request_reconnect();
                    Err(StructuredOrderError {
                        category: OrderErrorCategory::ConnectivityBlocked,
                        error_type: "IbGatewayUnreachable".to_string(),
                        message: format!(
                            "live order submission for strategy `{}` blocked: \
                             IB Gateway is unreachable (SRS-SAFE-003)",
                            submission.strategy_id.as_str()
                        ),
                        original_order: submission,
                    })
                }
            },
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
    use std::cell::{Cell, RefCell};

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

    struct StubConnectivity {
        state: Cell<ConnectivityState>,
        reconnect_calls: Cell<u32>,
    }

    impl StubConnectivity {
        fn connected() -> Self {
            Self {
                state: Cell::new(ConnectivityState::Connected),
                reconnect_calls: Cell::new(0),
            }
        }

        fn in_state(state: ConnectivityState) -> Self {
            Self {
                state: Cell::new(state),
                reconnect_calls: Cell::new(0),
            }
        }
    }

    impl BrokerageConnectivity for StubConnectivity {
        fn state(&self) -> ConnectivityState {
            self.state.get()
        }

        fn request_reconnect(&self) {
            self.reconnect_calls.set(self.reconnect_calls.get() + 1);
        }
    }

    /// A connectivity stub that panics if the engine consults it. Used to
    /// prove the Paper-mode arm of ERR-1 does not reach the connectivity
    /// gate at all.
    struct ForbiddenConnectivity;

    impl BrokerageConnectivity for ForbiddenConnectivity {
        fn state(&self) -> ConnectivityState {
            panic!("Paper submissions must not consult the connectivity port");
        }

        fn request_reconnect(&self) {
            panic!("Paper submissions must not request a reconnect");
        }
    }

    struct RecordingEvents {
        events: RefCell<Vec<ConnectivityEvent>>,
    }

    impl RecordingEvents {
        fn new() -> Self {
            Self {
                events: RefCell::new(Vec::new()),
            }
        }
    }

    impl ConnectivityEventSink for RecordingEvents {
        fn record(&self, event: ConnectivityEvent) {
            self.events.borrow_mut().push(event);
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
        let connectivity = StubConnectivity::connected();
        let events = RecordingEvents::new();
        let submission = OrderSubmission {
            strategy_id: StrategyId::new("live-1"),
            symbol: "AAPL".to_string(),
            quantity: 10,
        };

        let receipt = engine
            .submit_live_order(
                StrategyMode::Live,
                submission,
                &broker,
                &connectivity,
                &events,
            )
            .expect("live mode + connected must route through the brokerage port");

        assert_eq!(receipt.broker_order_id, "ib-AAPL");
        assert_eq!(broker.calls.get(), 1);
        assert_eq!(connectivity.reconnect_calls.get(), 0);
        assert!(events.events.borrow().is_empty());
    }

    #[test]
    fn paper_strategy_submission_is_rejected_synchronously_with_no_broker_call() {
        // ERR-1: A non-live strategy submitting an order down the live
        // execution path must be rejected synchronously with a structured
        // error AND must produce no IB order side effect. The connectivity
        // port must NOT be consulted — Paper rejection is independent of
        // connectivity state.
        let engine = ExecutionEngine;
        let broker = CountingBroker::new();
        let connectivity = ForbiddenConnectivity;
        let events = RecordingEvents::new();
        let submission = OrderSubmission {
            strategy_id: StrategyId::new("paper-research-3"),
            symbol: "TSLA".to_string(),
            quantity: 5,
        };

        let error = engine
            .submit_live_order(
                StrategyMode::Paper,
                submission.clone(),
                &broker,
                &connectivity,
                &events,
            )
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
        assert!(events.events.borrow().is_empty());
    }

    #[test]
    fn structured_error_display_includes_category_wire_string() {
        let engine = ExecutionEngine;
        let broker = CountingBroker::new();
        let connectivity = ForbiddenConnectivity;
        let events = RecordingEvents::new();
        let submission = OrderSubmission {
            strategy_id: StrategyId::new("paper-x"),
            symbol: "MSFT".to_string(),
            quantity: 1,
        };
        let error = engine
            .submit_live_order(
                StrategyMode::Paper,
                submission,
                &broker,
                &connectivity,
                &events,
            )
            .unwrap_err();
        assert!(format!("{error}").contains("NON_LIVE_STRATEGY_SUBMISSION"));
    }

    #[test]
    fn live_submission_is_blocked_when_gateway_unreachable() {
        // ERR-2 / SRS-SAFE-003: When IB Gateway is unreachable, a live
        // submission must be rejected with CONNECTIVITY_BLOCKED, no broker
        // call must happen, the connectivity port must be asked to
        // reconnect, and exactly one ConnectivityEvent must be recorded.
        let engine = ExecutionEngine;
        let broker = CountingBroker::new();
        let connectivity = StubConnectivity::in_state(ConnectivityState::Unreachable);
        let events = RecordingEvents::new();
        let submission = OrderSubmission {
            strategy_id: StrategyId::new("live-alpha"),
            symbol: "AAPL".to_string(),
            quantity: 10,
        };

        let error = engine
            .submit_live_order(
                StrategyMode::Live,
                submission.clone(),
                &broker,
                &connectivity,
                &events,
            )
            .expect_err("Unreachable connectivity must block the live submission");

        assert_eq!(error.category, OrderErrorCategory::ConnectivityBlocked);
        assert_eq!(error.error_type, "IbGatewayUnreachable");
        assert!(error.message.contains("live-alpha"));
        assert!(error.message.contains("SRS-SAFE-003"));
        assert_eq!(error.original_order, submission);
        assert!(format!("{error}").contains("CONNECTIVITY_BLOCKED"));

        assert_eq!(broker.calls.get(), 0, "broker must not be invoked");
        assert_eq!(
            connectivity.reconnect_calls.get(),
            1,
            "the engine must request a reconnect on Unreachable"
        );
        let recorded = events.events.borrow();
        assert_eq!(recorded.len(), 1, "exactly one ConnectivityEvent expected");
        assert_eq!(recorded[0].state, ConnectivityState::Unreachable);
        assert_eq!(recorded[0].strategy_id.as_str(), "live-alpha");
        assert_eq!(recorded[0].symbol, "AAPL");
        assert!(!recorded[0].scheduled_restart);
    }

    #[test]
    fn live_submission_is_blocked_during_scheduled_restart_window() {
        // ERR-2 / SRS-MD-005: During the configured daily restart window,
        // submissions are suspended; the published event carries
        // scheduled_restart=true so the notification dispatcher can apply
        // the suppression rule.
        let engine = ExecutionEngine;
        let broker = CountingBroker::new();
        let connectivity = StubConnectivity::in_state(ConnectivityState::ScheduledRestartWindow);
        let events = RecordingEvents::new();
        let submission = OrderSubmission {
            strategy_id: StrategyId::new("live-alpha"),
            symbol: "AAPL".to_string(),
            quantity: 10,
        };

        let error = engine
            .submit_live_order(
                StrategyMode::Live,
                submission,
                &broker,
                &connectivity,
                &events,
            )
            .expect_err("ScheduledRestartWindow must block the live submission");

        assert_eq!(error.category, OrderErrorCategory::ConnectivityBlocked);
        assert_eq!(broker.calls.get(), 0);
        assert_eq!(connectivity.reconnect_calls.get(), 1);
        let recorded = events.events.borrow();
        assert_eq!(recorded.len(), 1);
        assert_eq!(
            recorded[0].state,
            ConnectivityState::ScheduledRestartWindow
        );
        assert!(
            recorded[0].scheduled_restart,
            "SRS-MD-005 suppression flag must be set"
        );
    }
}
