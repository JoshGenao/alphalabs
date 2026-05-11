//! ERR-1 / SRS-EXE-001 / SRS-ERR-001 — a Paper-mode strategy attempting to
//! submit through the live execution path is rejected synchronously with a
//! structured error and the brokerage port is NEVER invoked.
//!
//! L7 domain (safety) test. The spy `BrokerageSpy` counts every
//! `submit_order` invocation; the post-condition is exactly zero calls.
//!
//! ERR-2 (SRS-SAFE-003 / SRS-MD-005) added a connectivity gate to
//! `submit_live_order`. The Paper-mode rejection must remain independent of
//! connectivity state, so this file uses a `ForbiddenConnectivity` stub that
//! panics if the engine consults it during a Paper submission — proving the
//! Paper rejection short-circuits before the connectivity port is reached.

use atp_execution::{
    BrokerageConnectivity, ConnectivityEventSink, ExecutionEngine, LiveBrokerageSubmit,
};
use atp_types::{
    ConnectivityEvent, ConnectivityState, OrderErrorCategory, OrderReceipt, OrderSubmission,
    StrategyId, StrategyMode, StructuredOrderError,
};
use std::cell::{Cell, RefCell};

#[derive(Default)]
struct BrokerageSpy {
    calls: Cell<u32>,
}

impl LiveBrokerageSubmit for BrokerageSpy {
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

/// Always-connected connectivity stub used by the Live control test.
struct AlwaysConnected;

impl BrokerageConnectivity for AlwaysConnected {
    fn state(&self) -> ConnectivityState {
        ConnectivityState::Connected
    }

    fn request_reconnect(&self) {}
}

/// Connectivity stub that panics if the engine consults it. Used by the
/// Paper-rejection tests to prove ERR-1's short-circuit is independent of
/// the ERR-2 connectivity gate.
struct ForbiddenConnectivity;

impl BrokerageConnectivity for ForbiddenConnectivity {
    fn state(&self) -> ConnectivityState {
        panic!("ERR-1: Paper submissions must not consult the connectivity port");
    }

    fn request_reconnect(&self) {
        panic!("ERR-1: Paper submissions must not trigger a reconnect");
    }
}

#[derive(Default)]
struct EventSinkSpy {
    events: RefCell<Vec<ConnectivityEvent>>,
}

impl ConnectivityEventSink for EventSinkSpy {
    fn record(&self, event: ConnectivityEvent) {
        self.events.borrow_mut().push(event);
    }
}

fn submission(strategy: &str, symbol: &str, qty: i64) -> OrderSubmission {
    OrderSubmission {
        strategy_id: StrategyId::new(strategy),
        symbol: symbol.to_string(),
        quantity: qty,
    }
}

#[test]
fn err_1_paper_strategy_is_rejected_with_no_broker_call() {
    let engine = ExecutionEngine;
    let spy = BrokerageSpy::default();
    let connectivity = ForbiddenConnectivity;
    let events = EventSinkSpy::default();
    let order = submission("paper-mean-rev-7", "AAPL", 100);

    let outcome = engine.submit_live_order(
        StrategyMode::Paper,
        order.clone(),
        &spy,
        &connectivity,
        &events,
    );

    let error = outcome.expect_err("ERR-1: paper submissions must NOT succeed on the live path");
    assert_eq!(
        error.category,
        OrderErrorCategory::NonLiveStrategySubmission,
        "category must be NON_LIVE_STRATEGY_SUBMISSION"
    );
    assert_eq!(
        error.category.as_str(),
        "NON_LIVE_STRATEGY_SUBMISSION",
        "wire string must match SyRS SYS-64 vocabulary"
    );
    assert_eq!(
        error.original_order, order,
        "structured error must carry the original order parameters (SRS-ERR-001)"
    );
    assert!(
        error.message.contains("paper-mean-rev-7"),
        "message must name the submitting strategy"
    );
    assert_eq!(
        spy.calls.get(),
        0,
        "no IB order side effect — spy must have observed zero submit_order calls"
    );
    assert!(
        events.events.borrow().is_empty(),
        "Paper rejection must not emit a connectivity event"
    );
}

#[test]
fn err_1_holds_for_many_paper_submissions() {
    // Pseudo-property: regardless of symbol / quantity / strategy id, a
    // Paper submission must never reach the brokerage port.
    let engine = ExecutionEngine;
    let spy = BrokerageSpy::default();
    let connectivity = ForbiddenConnectivity;
    let events = EventSinkSpy::default();
    let cases = [
        ("paper-1", "AAPL", 1),
        ("paper-1", "AAPL", -1),
        ("paper-research-99", "BRK.B", 10_000),
        ("paper-zero-qty", "MSFT", 0),
        ("paper-vol-arb", "SPY", 250),
    ];
    for (strategy, symbol, qty) in cases {
        let order = submission(strategy, symbol, qty);
        let err = engine
            .submit_live_order(
                StrategyMode::Paper,
                order.clone(),
                &spy,
                &connectivity,
                &events,
            )
            .expect_err("paper submissions are always blocked on the live path");
        assert_eq!(err.category, OrderErrorCategory::NonLiveStrategySubmission);
        assert_eq!(err.original_order, order);
    }
    assert_eq!(
        spy.calls.get(),
        0,
        "no IB order side effect across {} paper submissions",
        cases.len()
    );
    assert!(events.events.borrow().is_empty());
}

#[test]
fn err_1_live_strategy_still_routes_through_the_broker() {
    // Negative control: the rejection MUST be selective. A Live submission
    // still reaches the broker — otherwise ERR-1 would degenerate into
    // "no orders ever go through" and silently break the live path.
    let engine = ExecutionEngine;
    let spy = BrokerageSpy::default();
    let connectivity = AlwaysConnected;
    let events = EventSinkSpy::default();
    let order = submission("live-alpha-1", "AAPL", 10);

    let receipt = engine
        .submit_live_order(StrategyMode::Live, order, &spy, &connectivity, &events)
        .expect("live submissions must reach the brokerage port");

    assert_eq!(receipt.broker_order_id, "ib-AAPL");
    assert_eq!(spy.calls.get(), 1);
    assert!(events.events.borrow().is_empty());
}
