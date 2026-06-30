//! SRS-EXE-002 — route all non-live strategy orders to the internal simulation
//! engine; paper strategy orders never create IB orders (SyRS SYS-2b / SYS-2e,
//! AC-10; StRS SN-1.06 / SN-1.29 / C-11).
//!
//! L7 domain (safety) acceptance for the order-routing dispatch authority. The
//! routing decision is derived from the engine-owned SRS-EXE-001
//! live-designation authority: the single designated live strategy is
//! dispatched to the live IB gate ([`ExecutionEngine::route_order`]); **every**
//! other (non-live) strategy is dispatched to the internal simulation engine
//! through the [`InternalSimulationSubmit`] port, and never reaches IB.
//!
//! The per-direction isolation is proven with `Forbidden*` stubs that panic if
//! touched. AC-10 is "a paper order never CREATES an IB order", so a non-live
//! dispatch routes to a counting simulation spy while the **broker** port is
//! panic-on-touch (`ForbiddenBroker` — the order-creating port); the read-only
//! connectivity/freshness gates are benign stubs, because the SRS-MD-004 /
//! SYS-87 simulated-order stale-data gate (deferred) will legitimately consult
//! freshness on the paper path. A designated dispatch routes to the live broker
//! while the simulation port is panic-on-touch. The one-live-among-thirty-paper
//! sweep is the AC-10 acceptance scenario: exactly one IB order side effect,
//! with all 30 paper orders handled by the simulation engine.

use atp_execution::{
    BrokerageConnectivity, ConnectivityEventSink, ExecutionEngine, InternalSimulationSubmit,
    LiveBrokerageSubmit, LiveDesignationConfirmation, MarketDataFreshnessProbe,
    OrderRoutingReceipt, SimulatedOrderReceipt, StaleDataEventSink,
};
use atp_types::{
    ConnectivityEvent, ConnectivityState, MarketDataFreshness, OrderReceipt, OrderSubmission,
    StaleDataEvent, StrategyId, StructuredOrderError,
};
use std::cell::{Cell, RefCell};

#[derive(Default)]
struct SimulationSpy {
    calls: Cell<u32>,
}

impl InternalSimulationSubmit for SimulationSpy {
    fn submit_simulated(
        &self,
        submission: OrderSubmission,
    ) -> Result<SimulatedOrderReceipt, StructuredOrderError> {
        self.calls.set(self.calls.get() + 1);
        Ok(SimulatedOrderReceipt {
            sim_order_id: format!("sim-{}", submission.symbol),
        })
    }
}

/// Simulation stub that panics if the engine ever routes a designated (live)
/// order to it — proves the LiveBrokerage dispatch never touches the simulation
/// port.
struct ForbiddenSimulation;

impl InternalSimulationSubmit for ForbiddenSimulation {
    fn submit_simulated(
        &self,
        _submission: OrderSubmission,
    ) -> Result<SimulatedOrderReceipt, StructuredOrderError> {
        panic!("SRS-EXE-002: a designated live strategy must never route to the simulation port");
    }
}

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

/// Brokerage stub that panics if a non-live order is ever routed to it — proves
/// a paper strategy's order never creates an IB order (AC-10).
struct ForbiddenBroker;

impl LiveBrokerageSubmit for ForbiddenBroker {
    fn submit_order(
        &self,
        _submission: OrderSubmission,
    ) -> Result<OrderReceipt, StructuredOrderError> {
        panic!("SRS-EXE-002: a non-live strategy must never reach the brokerage port");
    }
}

struct AlwaysConnected;

impl BrokerageConnectivity for AlwaysConnected {
    fn state(&self) -> ConnectivityState {
        ConnectivityState::Connected
    }

    fn request_reconnect(&self) {}
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
        asset_class: atp_types::AssetClass::Equity,
        side: atp_types::OrderSide::Buy,
        order_type: atp_types::OrderType::Market,
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
fn srs_exe_002_non_live_strategy_routes_to_internal_simulation() {
    // live-alpha is the designated live strategy; a DIFFERENT (non-live)
    // strategy dispatching an order must be routed to the internal simulation
    // engine and must not consult the broker, connectivity, or freshness ports
    // (all panic-on-touch stubs).
    let mut engine = ExecutionEngine::default();
    engine
        .designate(StrategyId::new("live-alpha"), confirm("live-alpha"))
        .expect("designate the single live strategy");

    let sim = SimulationSpy::default();
    let events = EventSinkSpy::default();
    let stale_events = StaleEventSinkSpy::default();

    let receipt = engine
        .dispatch_order(
            submission("paper-mean-rev-7", "TSLA", 5),
            &ForbiddenBroker,
            &AlwaysConnected,
            &events,
            &AlwaysFresh,
            &stale_events,
            &sim,
        )
        .expect("a non-live strategy routes to the internal simulation engine");

    assert_eq!(
        receipt,
        OrderRoutingReceipt::Simulated(SimulatedOrderReceipt {
            sim_order_id: "sim-TSLA".to_string(),
        })
    );
    assert_eq!(sim.calls.get(), 1);
    assert!(events.events.borrow().is_empty());
    assert!(stale_events.events.borrow().is_empty());
}

#[test]
fn srs_exe_002_no_designation_routes_every_strategy_to_simulation() {
    // With no live strategy designated, NO strategy is live — every order routes
    // to the internal simulation engine (the safe default; nothing reaches IB).
    let engine = ExecutionEngine::default();
    let sim = SimulationSpy::default();
    let events = EventSinkSpy::default();
    let stale_events = StaleEventSinkSpy::default();

    let receipt = engine
        .dispatch_order(
            submission("would-be-live", "AAPL", 1),
            &ForbiddenBroker,
            &AlwaysConnected,
            &events,
            &AlwaysFresh,
            &stale_events,
            &sim,
        )
        .expect("with no designation, every order routes to the simulation engine");

    assert!(matches!(receipt, OrderRoutingReceipt::Simulated(_)));
    assert_eq!(sim.calls.get(), 1);
    assert!(engine.designated().is_none());
}

#[test]
fn srs_exe_002_designated_strategy_routes_to_ib_only() {
    // The single designated live strategy dispatches an order: it reaches the
    // live broker and must NOT touch the simulation port (panic-on-touch stub).
    let mut engine = ExecutionEngine::default();
    engine
        .designate(StrategyId::new("live-alpha"), confirm("live-alpha"))
        .expect("designate the single live strategy");

    let broker = BrokerageSpy::default();
    let events = EventSinkSpy::default();
    let stale_events = StaleEventSinkSpy::default();

    let receipt = engine
        .dispatch_order(
            submission("live-alpha", "AAPL", 10),
            &broker,
            &AlwaysConnected,
            &events,
            &AlwaysFresh,
            &stale_events,
            &ForbiddenSimulation,
        )
        .expect("the designated live strategy routes to the live IB gate");

    assert_eq!(
        receipt,
        OrderRoutingReceipt::Live(OrderReceipt {
            broker_order_id: "ib-AAPL".to_string(),
        })
    );
    assert_eq!(broker.calls.get(), 1);
}

#[test]
fn srs_exe_002_one_live_among_thirty_paper_routes_only_the_live_to_ib() {
    // AC-10 acceptance: with one live strategy and 30 paper strategies, only the
    // live strategy's order creates an IB order; all 30 paper orders are handled
    // by the internal simulation engine. Counting spies record both
    // destinations; the assertion is exactly one IB order side effect and 30
    // simulated orders.
    let mut engine = ExecutionEngine::default();
    engine
        .designate(StrategyId::new("live-alpha"), confirm("live-alpha"))
        .expect("designate the single live strategy");

    let broker = BrokerageSpy::default();
    let sim = SimulationSpy::default();
    let events = EventSinkSpy::default();
    let stale_events = StaleEventSinkSpy::default();

    for index in 0..30 {
        let receipt = engine
            .dispatch_order(
                submission(&format!("paper-{index}"), "SPY", 100),
                &broker,
                &AlwaysConnected,
                &events,
                &AlwaysFresh,
                &stale_events,
                &sim,
            )
            .expect("a paper strategy routes to the internal simulation engine");
        assert!(
            matches!(receipt, OrderRoutingReceipt::Simulated(_)),
            "paper order must be simulated, never an IB order"
        );
    }

    let receipt = engine
        .dispatch_order(
            submission("live-alpha", "AAPL", 10),
            &broker,
            &AlwaysConnected,
            &events,
            &AlwaysFresh,
            &stale_events,
            &sim,
        )
        .expect("the designated live strategy routes to the live IB gate");
    assert_eq!(
        receipt,
        OrderRoutingReceipt::Live(OrderReceipt {
            broker_order_id: "ib-AAPL".to_string(),
        })
    );

    assert_eq!(
        broker.calls.get(),
        1,
        "exactly one IB order side effect across 1 live + 30 paper strategies (AC-10)"
    );
    assert_eq!(
        sim.calls.get(),
        30,
        "all 30 paper orders were handled by the internal simulation engine"
    );
}
