//! SRS-EXE-001 — orders route to IB ONLY for the designated live strategy.
//!
//! L7 domain (safety) acceptance for the live-designation authority. SyRS
//! SYS-2a (exactly one live), SYS-2d/NFR-S2 (explicit, strategy-bound
//! confirmation), AC-15 (single-live enforcement). AGENTS.md core constraint:
//! *"Exactly one strategy may execute against the IB live account at any time."*
//!
//! [`ExecutionEngine::route_order`] derives live-ness from the
//! [`LiveDesignation`] authority rather than a caller-supplied `StrategyMode`.
//! A strategy that is not the single designated live strategy is rejected with
//! `NON_LIVE_STRATEGY_SUBMISSION` **before any broker / connectivity / freshness
//! port is consulted** — proven here with `Forbidden*` stubs that panic if
//! touched and a `ForbiddenBroker` that panics on any `submit_order`. The
//! one-live-among-thirty-paper sweep is the SRS-EXE-001 acceptance scenario.

use atp_execution::{
    BrokerageConnectivity, ConnectivityEventSink, ExecutionEngine, LiveBrokerageSubmit,
    LiveDesignation, LiveDesignationConfirmation, MarketDataFreshnessProbe, StaleDataEventSink,
};
use atp_types::{
    ConnectivityEvent, ConnectivityState, MarketDataFreshness, OrderErrorCategory, OrderReceipt,
    OrderSubmission, StaleDataEvent, StrategyId, StructuredOrderError,
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

/// Brokerage stub that panics if the engine ever routes an order to it. Used by
/// the NotDesignated tests to prove a non-designated submission never reaches
/// the broker.
struct ForbiddenBroker;

impl LiveBrokerageSubmit for ForbiddenBroker {
    fn submit_order(
        &self,
        _submission: OrderSubmission,
    ) -> Result<OrderReceipt, StructuredOrderError> {
        panic!("SRS-EXE-001: a non-designated strategy must never reach the brokerage port");
    }
}

struct AlwaysConnected;

impl BrokerageConnectivity for AlwaysConnected {
    fn state(&self) -> ConnectivityState {
        ConnectivityState::Connected
    }

    fn request_reconnect(&self) {}
}

/// Connectivity stub that panics if consulted — proves the NotDesignated
/// rejection short-circuits before the ERR-2 connectivity gate.
struct ForbiddenConnectivity;

impl BrokerageConnectivity for ForbiddenConnectivity {
    fn state(&self) -> ConnectivityState {
        panic!("SRS-EXE-001: NotDesignated rejection must not consult the connectivity port");
    }

    fn request_reconnect(&self) {
        panic!("SRS-EXE-001: NotDesignated rejection must not request a reconnect");
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

struct AlwaysFresh;

impl MarketDataFreshnessProbe for AlwaysFresh {
    fn freshness(&self, _symbol: &str) -> MarketDataFreshness {
        MarketDataFreshness::Fresh
    }

    fn staleness_seconds(&self, _symbol: &str) -> u64 {
        0
    }
}

/// Freshness stub that panics if consulted — proves the NotDesignated rejection
/// short-circuits before the ERR-3 freshness gate.
struct ForbiddenFreshness;

impl MarketDataFreshnessProbe for ForbiddenFreshness {
    fn freshness(&self, _symbol: &str) -> MarketDataFreshness {
        panic!("SRS-EXE-001: NotDesignated rejection must not consult the freshness port");
    }

    fn staleness_seconds(&self, _symbol: &str) -> u64 {
        panic!("SRS-EXE-001: NotDesignated rejection must not consult staleness_seconds");
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

fn confirm(strategy: &str) -> LiveDesignationConfirmation {
    LiveDesignationConfirmation::from_operator(
        StrategyId::new(strategy),
        "operator confirmed live designation",
    )
    .expect("non-empty acknowledgement yields a confirmation token")
}

#[test]
fn srs_exe_001_only_the_designated_strategy_routes_to_the_broker() {
    let engine = ExecutionEngine;
    let mut designation = LiveDesignation::new();
    designation
        .designate(StrategyId::new("live-alpha"), confirm("live-alpha"))
        .expect("designate the single live strategy");

    let spy = BrokerageSpy::default();
    let events = EventSinkSpy::default();
    let stale_events = StaleEventSinkSpy::default();

    let receipt = engine
        .route_order(
            &designation,
            submission("live-alpha", "AAPL", 10),
            &spy,
            &AlwaysConnected,
            &events,
            &AlwaysFresh,
            &stale_events,
        )
        .expect("the designated live strategy routes through the brokerage port");

    assert_eq!(receipt.broker_order_id, "ib-AAPL");
    assert_eq!(spy.calls.get(), 1);
}

#[test]
fn srs_exe_001_non_designated_strategy_is_rejected_before_any_port() {
    // The registry designates live-alpha; a DIFFERENT strategy submitting an
    // order must be rejected synchronously with NON_LIVE_STRATEGY_SUBMISSION and
    // must not consult the broker, connectivity, or freshness ports (all are
    // panic-on-touch stubs).
    let engine = ExecutionEngine;
    let mut designation = LiveDesignation::new();
    designation
        .designate(StrategyId::new("live-alpha"), confirm("live-alpha"))
        .expect("designate the single live strategy");

    let events = EventSinkSpy::default();
    let stale_events = StaleEventSinkSpy::default();
    let order = submission("paper-mean-rev-7", "TSLA", 5);

    let error = engine
        .route_order(
            &designation,
            order.clone(),
            &ForbiddenBroker,
            &ForbiddenConnectivity,
            &events,
            &ForbiddenFreshness,
            &stale_events,
        )
        .expect_err("a non-designated strategy must be rejected on the live path");

    assert_eq!(
        error.category,
        OrderErrorCategory::NonLiveStrategySubmission
    );
    assert_eq!(error.category.as_str(), "NON_LIVE_STRATEGY_SUBMISSION");
    assert_eq!(error.error_type, "NotDesignatedLiveStrategy");
    assert_eq!(
        error.original_order, order,
        "structured error must carry the original order parameters (SRS-ERR-001)"
    );
    assert!(error.message.contains("paper-mean-rev-7"));
    assert!(error.message.contains("SRS-EXE-001"));
    assert!(events.events.borrow().is_empty());
    assert!(stale_events.events.borrow().is_empty());
}

#[test]
fn srs_exe_001_no_designation_rejects_every_strategy() {
    // With no live strategy designated, NO strategy is authorized to route to
    // IB — the safe default.
    let engine = ExecutionEngine;
    let designation = LiveDesignation::new();
    let events = EventSinkSpy::default();
    let stale_events = StaleEventSinkSpy::default();

    let error = engine
        .route_order(
            &designation,
            submission("would-be-live", "AAPL", 1),
            &ForbiddenBroker,
            &ForbiddenConnectivity,
            &events,
            &ForbiddenFreshness,
            &stale_events,
        )
        .expect_err("no strategy may route to IB until one is explicitly designated");

    assert_eq!(
        error.category,
        OrderErrorCategory::NonLiveStrategySubmission
    );
    assert_eq!(error.category.as_str(), "NON_LIVE_STRATEGY_SUBMISSION");
}

#[test]
fn srs_exe_001_one_live_among_thirty_paper_routes_only_the_live() {
    // SRS-EXE-001 acceptance: with one live strategy and at least 30 paper
    // strategies running, only the live strategy can submit to IB; all other
    // IB-bound attempts are rejected with a structured error.
    let engine = ExecutionEngine;
    let mut designation = LiveDesignation::new();
    designation
        .designate(StrategyId::new("live-alpha"), confirm("live-alpha"))
        .expect("designate the single live strategy");

    let spy = BrokerageSpy::default();
    let events = EventSinkSpy::default();
    let stale_events = StaleEventSinkSpy::default();

    // The 30 paper strategies are each rejected (they short-circuit before any
    // port, so even the AlwaysConnected/AlwaysFresh stubs are never reached).
    for index in 0..30 {
        let order = submission(&format!("paper-{index}"), "SPY", 100);
        let error = engine
            .route_order(
                &designation,
                order.clone(),
                &spy,
                &AlwaysConnected,
                &events,
                &AlwaysFresh,
                &stale_events,
            )
            .expect_err("a paper strategy must never route to IB");
        assert_eq!(
            error.category,
            OrderErrorCategory::NonLiveStrategySubmission
        );
        assert_eq!(error.category.as_str(), "NON_LIVE_STRATEGY_SUBMISSION");
        assert_eq!(error.original_order, order);
    }

    // The single designated live strategy routes through the broker.
    let receipt = engine
        .route_order(
            &designation,
            submission("live-alpha", "AAPL", 10),
            &spy,
            &AlwaysConnected,
            &events,
            &AlwaysFresh,
            &stale_events,
        )
        .expect("the designated live strategy routes through the brokerage port");
    assert_eq!(receipt.broker_order_id, "ib-AAPL");

    assert_eq!(
        spy.calls.get(),
        1,
        "exactly one IB order side effect across 1 live + 30 paper strategies"
    );
}

#[test]
fn srs_exe_001_exactly_one_designation_is_demotable_and_re_designable() {
    // SYS-2a + the demotion-before-promotion lifecycle, observed end-to-end
    // through route_order.
    let engine = ExecutionEngine;
    let mut designation = LiveDesignation::new();
    designation
        .designate(StrategyId::new("live-alpha"), confirm("live-alpha"))
        .expect("first designation succeeds");

    // A second concurrent designation is refused (SYS-2a).
    designation
        .designate(StrategyId::new("live-beta"), confirm("live-beta"))
        .expect_err("a second concurrent designation must be refused");

    let spy = BrokerageSpy::default();
    let events = EventSinkSpy::default();
    let stale_events = StaleEventSinkSpy::default();

    // live-beta is not (yet) the live strategy — it is rejected.
    engine
        .route_order(
            &designation,
            submission("live-beta", "AAPL", 10),
            &spy,
            &AlwaysConnected,
            &events,
            &AlwaysFresh,
            &stale_events,
        )
        .expect_err("live-beta is not the designated live strategy");
    assert_eq!(spy.calls.get(), 0);

    // Demote live-alpha, then promote live-beta.
    designation
        .demote(&StrategyId::new("live-alpha"))
        .expect("demote the current live strategy");
    designation
        .designate(StrategyId::new("live-beta"), confirm("live-beta"))
        .expect("after demotion live-beta may be designated");

    // Now live-beta routes and the former live-alpha is rejected.
    let receipt = engine
        .route_order(
            &designation,
            submission("live-beta", "AAPL", 10),
            &spy,
            &AlwaysConnected,
            &events,
            &AlwaysFresh,
            &stale_events,
        )
        .expect("live-beta is now the designated live strategy");
    assert_eq!(receipt.broker_order_id, "ib-AAPL");
    assert_eq!(spy.calls.get(), 1);

    engine
        .route_order(
            &designation,
            submission("live-alpha", "AAPL", 10),
            &spy,
            &AlwaysConnected,
            &events,
            &AlwaysFresh,
            &stale_events,
        )
        .expect_err("the demoted live-alpha must no longer route to IB");
    assert_eq!(
        spy.calls.get(),
        1,
        "the demoted strategy produced no further IB order side effect"
    );
}
