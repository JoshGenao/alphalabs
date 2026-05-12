//! ERR-2 / SRS-SAFE-003 / SRS-MD-005 — when IB Gateway is unreachable (or
//! during the configured daily-restart window), a Live-mode submission is
//! rejected synchronously with `CONNECTIVITY_BLOCKED`, the brokerage port
//! is NEVER invoked, the engine requests a reconnect, and a structured
//! `ConnectivityEvent` is published.
//!
//! L7 domain (safety) test. The post-conditions are:
//!   * `BrokerageSpy.calls == 0` across every blocked submission.
//!   * `ConnectivitySpy.reconnect_calls == 1` per blocked submission.
//!   * `EventSinkSpy.events.len() == 1` per blocked submission, with the
//!     event payload matching the SRS-MD-005 suppression rule
//!     (`scheduled_restart` flag) when the state is
//!     `ScheduledRestartWindow`.

use atp_execution::{
    BrokerageConnectivity, ConnectivityEventSink, ExecutionEngine, LiveBrokerageSubmit,
    MarketDataFreshnessProbe, StaleDataEventSink,
};
use atp_types::{
    ConnectivityEvent, ConnectivityState, MarketDataFreshness, OrderErrorCategory, OrderReceipt,
    OrderSubmission, StaleDataEvent, StrategyId, StrategyMode, StructuredOrderError,
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

struct ConnectivitySpy {
    state: Cell<ConnectivityState>,
    reconnect_calls: Cell<u32>,
}

impl ConnectivitySpy {
    fn in_state(state: ConnectivityState) -> Self {
        Self {
            state: Cell::new(state),
            reconnect_calls: Cell::new(0),
        }
    }
}

impl BrokerageConnectivity for ConnectivitySpy {
    fn state(&self) -> ConnectivityState {
        self.state.get()
    }

    fn request_reconnect(&self) {
        self.reconnect_calls.set(self.reconnect_calls.get() + 1);
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

/// Always-fresh freshness stub used by the Connected positive-control test.
struct AlwaysFresh;

impl MarketDataFreshnessProbe for AlwaysFresh {
    fn freshness(&self, _symbol: &str) -> MarketDataFreshness {
        MarketDataFreshness::Fresh
    }

    fn staleness_seconds(&self, _symbol: &str) -> u64 {
        0
    }
}

/// Freshness stub that panics if consulted. Used by the Unreachable /
/// ScheduledRestartWindow tests to prove the ERR-2 connectivity gate
/// short-circuits before the ERR-3 freshness gate (the outer match arm
/// on `ConnectivityState` must reject before the inner freshness match
/// is reached).
struct ForbiddenFreshness;

impl MarketDataFreshnessProbe for ForbiddenFreshness {
    fn freshness(&self, _symbol: &str) -> MarketDataFreshness {
        panic!("ERR-2: blocked-connectivity branch must not consult the freshness port");
    }

    fn staleness_seconds(&self, _symbol: &str) -> u64 {
        panic!("ERR-2: blocked-connectivity branch must not consult staleness_seconds");
    }
}

#[derive(Default)]
struct StaleEventSinkSpy {
    events: RefCell<Vec<StaleDataEvent>>,
}

impl StaleDataEventSink for StaleEventSinkSpy {
    fn record(&self, event: StaleDataEvent) {
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
fn err_2_unreachable_state_blocks_live_submission_with_no_broker_call() {
    // SRS-SAFE-003: live submissions must fail with CONNECTIVITY_BLOCKED
    // while IB is unreachable; the engine must request a reconnect and
    // publish a structured event for logs/dashboard.
    let engine = ExecutionEngine;
    let broker = BrokerageSpy::default();
    let connectivity = ConnectivitySpy::in_state(ConnectivityState::Unreachable);
    let events = EventSinkSpy::default();
    let freshness = ForbiddenFreshness;
    let stale_events = StaleEventSinkSpy::default();
    let order = submission("live-alpha-1", "AAPL", 100);

    let error = engine
        .submit_live_order(
            StrategyMode::Live,
            order.clone(),
            &broker,
            &connectivity,
            &events,
            &freshness,
            &stale_events,
        )
        .expect_err("ERR-2: Unreachable state must reject the live submission");

    assert_eq!(
        error.category,
        OrderErrorCategory::ConnectivityBlocked,
        "SRS-SAFE-003: category must be CONNECTIVITY_BLOCKED"
    );
    assert_eq!(
        error.category.as_str(),
        "CONNECTIVITY_BLOCKED",
        "wire string must match SyRS SYS-64 vocabulary"
    );
    assert_eq!(error.error_type, "IbGatewayUnreachable");
    assert!(
        error.message.contains("live-alpha-1"),
        "message must name the submitting strategy"
    );
    assert!(
        error.message.contains("SRS-SAFE-003"),
        "message must trace SRS-SAFE-003"
    );
    assert_eq!(
        error.original_order, order,
        "structured error must carry the original order parameters (SRS-ERR-001)"
    );

    assert_eq!(
        broker.calls.get(),
        0,
        "no IB order side effect — broker spy must observe zero submit_order calls"
    );
    assert_eq!(
        connectivity.reconnect_calls.get(),
        1,
        "SRS-SAFE-003: engine must attempt a reconnect when blocked"
    );
    let recorded = events.events.borrow();
    assert_eq!(
        recorded.len(),
        1,
        "exactly one ConnectivityEvent must be recorded for dashboard alerting"
    );
    assert_eq!(recorded[0].state, ConnectivityState::Unreachable);
    assert_eq!(recorded[0].strategy_id.as_str(), "live-alpha-1");
    assert_eq!(recorded[0].symbol, "AAPL");
    assert!(
        !recorded[0].scheduled_restart,
        "Unreachable is the unscheduled connectivity-loss path"
    );
    assert!(
        stale_events.events.borrow().is_empty(),
        "Unreachable branch must not emit a stale-data event"
    );
}

#[test]
fn err_2_scheduled_restart_window_blocks_with_suppressed_marker() {
    // SRS-MD-005: during the configured daily-restart window, submissions
    // are suspended; the published event carries scheduled_restart=true so
    // the notification dispatcher can apply the suppression rule.
    let engine = ExecutionEngine;
    let broker = BrokerageSpy::default();
    let connectivity = ConnectivitySpy::in_state(ConnectivityState::ScheduledRestartWindow);
    let events = EventSinkSpy::default();
    let freshness = ForbiddenFreshness;
    let stale_events = StaleEventSinkSpy::default();
    let order = submission("live-alpha-1", "MSFT", 50);

    let error = engine
        .submit_live_order(
            StrategyMode::Live,
            order,
            &broker,
            &connectivity,
            &events,
            &freshness,
            &stale_events,
        )
        .expect_err("ERR-2: ScheduledRestartWindow must reject the live submission");

    assert_eq!(error.category, OrderErrorCategory::ConnectivityBlocked);
    assert_eq!(error.category.as_str(), "CONNECTIVITY_BLOCKED");
    assert_eq!(broker.calls.get(), 0);
    assert_eq!(
        connectivity.reconnect_calls.get(),
        1,
        "SRS-MD-005: reconnect attempts continue during the restart window"
    );

    let recorded = events.events.borrow();
    assert_eq!(recorded.len(), 1);
    assert_eq!(
        recorded[0].state,
        ConnectivityState::ScheduledRestartWindow
    );
    assert!(
        recorded[0].scheduled_restart,
        "SRS-MD-005 suppression flag must be set on scheduled-restart events"
    );
    assert!(
        stale_events.events.borrow().is_empty(),
        "ScheduledRestartWindow branch must not emit a stale-data event"
    );
}

#[test]
fn err_2_connected_state_still_routes_through_broker() {
    // Negative control: ERR-2's rejection must be selective. A Live + Connected
    // submission still reaches the broker — otherwise the gate would silently
    // disable the live path even when IB is healthy.
    let engine = ExecutionEngine;
    let broker = BrokerageSpy::default();
    let connectivity = ConnectivitySpy::in_state(ConnectivityState::Connected);
    let events = EventSinkSpy::default();
    let freshness = AlwaysFresh;
    let stale_events = StaleEventSinkSpy::default();
    let order = submission("live-alpha-1", "AAPL", 10);

    let receipt = engine
        .submit_live_order(
            StrategyMode::Live,
            order,
            &broker,
            &connectivity,
            &events,
            &freshness,
            &stale_events,
        )
        .expect("Connected + Fresh must still allow the live submission through");

    assert_eq!(receipt.broker_order_id, "ib-AAPL");
    assert_eq!(broker.calls.get(), 1);
    assert_eq!(
        connectivity.reconnect_calls.get(),
        0,
        "no reconnect should be requested when IB is reachable"
    );
    assert!(
        events.events.borrow().is_empty(),
        "no connectivity event should be emitted on the happy path"
    );
    assert!(stale_events.events.borrow().is_empty());
}

#[test]
fn err_2_unreachable_holds_across_many_live_submissions() {
    // Pseudo-property: regardless of strategy / symbol / quantity, an
    // Unreachable state must never permit the broker to be called, and
    // every blocked submission must produce its own ConnectivityEvent +
    // reconnect attempt.
    let engine = ExecutionEngine;
    let broker = BrokerageSpy::default();
    let connectivity = ConnectivitySpy::in_state(ConnectivityState::Unreachable);
    let events = EventSinkSpy::default();
    let freshness = ForbiddenFreshness;
    let stale_events = StaleEventSinkSpy::default();
    let cases = [
        ("live-alpha-1", "AAPL", 1),
        ("live-alpha-1", "AAPL", -1),
        ("live-alpha-1", "BRK.B", 10_000),
        ("live-alpha-1", "MSFT", 0),
        ("live-alpha-1", "SPY", 250),
    ];
    for (strategy, symbol, qty) in cases {
        let order = submission(strategy, symbol, qty);
        let err = engine
            .submit_live_order(
                StrategyMode::Live,
                order.clone(),
                &broker,
                &connectivity,
                &events,
                &freshness,
                &stale_events,
            )
            .expect_err("Unreachable always blocks");
        assert_eq!(err.category, OrderErrorCategory::ConnectivityBlocked);
        assert_eq!(err.original_order, order);
    }
    assert_eq!(
        broker.calls.get(),
        0,
        "no IB order side effect across {} blocked submissions",
        cases.len()
    );
    assert_eq!(
        connectivity.reconnect_calls.get(),
        cases.len() as u32,
        "one reconnect attempt per blocked submission"
    );
    assert_eq!(
        events.events.borrow().len(),
        cases.len(),
        "one ConnectivityEvent per blocked submission"
    );
    assert!(
        stale_events.events.borrow().is_empty(),
        "Unreachable branch must not emit any stale-data events"
    );
}
