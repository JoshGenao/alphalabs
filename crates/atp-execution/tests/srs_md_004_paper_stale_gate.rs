//! SRS-MD-004 contract obligation C3 — the simulated-order stale-data gate
//! (SyRS SYS-87; ERR-3 paper half).
//!
//! "Given the probe reports an instrument stale, a paper submission for it
//! through dispatch_order returns MARKET_DATA_STALE, reaches no simulation
//! port and creates no IB order; after the probe reports fresh, the next paper
//! submission is accepted by the simulation port."
//!
//! `atp-execution` cannot see `atp-simulation` (SRS-ARCH-002), so the
//! simulation destination is a recording double here. The real
//! `PaperSimulationEngine` behind the port is exercised by obligation C4 in
//! `atp-orchestrator` (`srs_md_004_stale_gate_wired`).

use std::cell::{Cell, RefCell};

use atp_execution::{
    BrokerageConnectivity, ConnectivityEventSink, ExecutionEngine, InternalSimulationSubmit,
    LiveBrokerageSubmit, LiveDesignationConfirmation, MarketDataFreshnessProbe,
    OrderRoutingReceipt, SimulatedOrderReceipt, StaleDataEventSink,
};
use atp_types::{
    AssetClass, ConnectivityEvent, ConnectivityState, MarketDataFreshness, OrderErrorCategory,
    OrderReceipt, OrderSide, OrderSubmission, OrderType, StaleDataEvent, StrategyId,
    StructuredOrderError,
};

/// Records every order the simulation engine accepts.
#[derive(Default)]
struct SimulationSpy {
    accepted: RefCell<Vec<OrderSubmission>>,
}

impl InternalSimulationSubmit for SimulationSpy {
    fn submit_simulated(
        &self,
        submission: OrderSubmission,
    ) -> Result<SimulatedOrderReceipt, StructuredOrderError> {
        let mut accepted = self.accepted.borrow_mut();
        accepted.push(submission);
        Ok(SimulatedOrderReceipt {
            sim_order_id: format!("SIM-{}", accepted.len()),
        })
    }
}

/// Counts live broker calls. Paper orders must never reach it.
#[derive(Default)]
struct BrokerSpy {
    calls: Cell<u32>,
}

impl LiveBrokerageSubmit for BrokerSpy {
    fn submit_order(
        &self,
        submission: OrderSubmission,
    ) -> Result<OrderReceipt, StructuredOrderError> {
        self.calls.set(self.calls.get() + 1);
        Ok(OrderReceipt {
            broker_order_id: format!("IB-{}", submission.symbol),
        })
    }
}

/// Connectivity that counts reads. Paper never touches IB, so an IB outage is
/// no reason to refuse a simulated order and the paper arm must not read it.
struct ConnectivitySpy {
    state: ConnectivityState,
    reads: Cell<u32>,
}

impl ConnectivitySpy {
    fn new(state: ConnectivityState) -> Self {
        Self {
            state,
            reads: Cell::new(0),
        }
    }
}

impl BrokerageConnectivity for ConnectivitySpy {
    fn state(&self) -> ConnectivityState {
        self.reads.set(self.reads.get() + 1);
        self.state
    }

    fn request_reconnect(&self) {}
}

#[derive(Default)]
struct ConnectivityEvents;

impl ConnectivityEventSink for ConnectivityEvents {
    fn record(&self, _event: ConnectivityEvent) {}
}

/// A probe the test flips between stale and fresh, recording what it was
/// asked about.
struct ProbeSpy {
    state: Cell<MarketDataFreshness>,
    age: Cell<Option<u64>>,
    asked: RefCell<Vec<(String, AssetClass)>>,
}

impl ProbeSpy {
    fn stale(age: Option<u64>) -> Self {
        Self {
            state: Cell::new(MarketDataFreshness::Stale),
            age: Cell::new(age),
            asked: RefCell::new(Vec::new()),
        }
    }
}

impl MarketDataFreshnessProbe for ProbeSpy {
    fn freshness(&self, symbol: &str, asset_class: AssetClass) -> MarketDataFreshness {
        self.asked
            .borrow_mut()
            .push((symbol.to_string(), asset_class));
        self.state.get()
    }

    fn staleness_seconds(&self, _symbol: &str, _asset_class: AssetClass) -> Option<u64> {
        self.age.get()
    }
}

#[derive(Default)]
struct StaleEvents {
    events: RefCell<Vec<StaleDataEvent>>,
}

impl StaleDataEventSink for StaleEvents {
    fn record(&self, event: StaleDataEvent) {
        self.events.borrow_mut().push(event);
    }
}

fn order(strategy: &str, symbol: &str, quantity: i64) -> OrderSubmission {
    OrderSubmission {
        strategy_id: StrategyId::new(strategy),
        symbol: symbol.to_string(),
        quantity,
        asset_class: AssetClass::Equity,
        side: OrderSide::Buy,
        order_type: OrderType::Market,
    }
}

fn engine_with_live(strategy: &str) -> ExecutionEngine {
    let mut engine = ExecutionEngine::default();
    let confirmation = LiveDesignationConfirmation::from_operator(
        StrategyId::new(strategy),
        "operator confirmed live designation",
    )
    .expect("non-empty acknowledgement yields a confirmation token");
    engine
        .designate(StrategyId::new(strategy), confirmation)
        .expect("designation succeeds");
    engine
}

#[test]
fn c3_a_stale_paper_order_is_refused_and_a_fresh_one_reaches_the_simulator() {
    let engine = engine_with_live("live-alpha");
    let broker = BrokerSpy::default();
    let connectivity = ConnectivitySpy::new(ConnectivityState::Connected);
    let probe = ProbeSpy::stale(Some(19));
    let stale_events = StaleEvents::default();
    let simulation = SimulationSpy::default();

    let err = engine
        .dispatch_order(
            order("paper-beta", "msft", 5),
            &broker,
            &connectivity,
            &ConnectivityEvents,
            &probe,
            &stale_events,
            &simulation,
        )
        .expect_err("a paper order on a stale line must be refused");
    assert_eq!(err.category, OrderErrorCategory::MarketDataStale);
    assert_eq!(err.category.as_str(), "MARKET_DATA_STALE");
    assert_eq!(
        err.error_type, "MarketDataStale",
        "same error_type as live (SYS-64)"
    );
    assert!(
        err.message.contains("paper order submission"),
        "{}",
        err.message
    );
    assert!(err.message.contains("19s"), "{}", err.message);
    assert_eq!(err.original_order.symbol, "msft");
    assert!(
        simulation.accepted.borrow().is_empty(),
        "no simulated order while stale"
    );
    assert_eq!(
        broker.calls.get(),
        0,
        "a paper order never creates an IB order"
    );
    assert_eq!(
        connectivity.reads.get(),
        0,
        "paper never reads IB connectivity"
    );
    assert_eq!(
        probe.asked.borrow().as_slice(),
        &[("msft".to_string(), AssetClass::Equity)],
        "the gate asks about the order's own security, once"
    );
    {
        let events = stale_events.events.borrow();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].state, MarketDataFreshness::Stale);
        assert_eq!(events[0].strategy_id.as_str(), "paper-beta");
        assert_eq!(events[0].symbol, "msft");
        assert_eq!(events[0].staleness_seconds, Some(19));
    }

    // Fresh data arrives: the next paper order is accepted by the simulator.
    probe.state.set(MarketDataFreshness::Fresh);
    let receipt = engine
        .dispatch_order(
            order("paper-beta", "msft", 5),
            &broker,
            &connectivity,
            &ConnectivityEvents,
            &probe,
            &stale_events,
            &simulation,
        )
        .expect("paper submissions resume when fresh data is received");
    assert_eq!(
        receipt,
        OrderRoutingReceipt::Simulated(SimulatedOrderReceipt {
            sim_order_id: "SIM-1".to_string()
        })
    );
    assert_eq!(simulation.accepted.borrow().len(), 1);
    assert_eq!(broker.calls.get(), 0);
    assert_eq!(
        stale_events.events.borrow().len(),
        1,
        "no event for a fresh order"
    );
}

#[test]
fn c3_an_ib_outage_does_not_block_a_fresh_paper_order() {
    let engine = engine_with_live("live-alpha");
    let probe = ProbeSpy::stale(None);
    probe.state.set(MarketDataFreshness::Fresh);
    let simulation = SimulationSpy::default();
    let connectivity = ConnectivitySpy::new(ConnectivityState::Unreachable);

    engine
        .dispatch_order(
            order("paper-beta", "AAPL", 1),
            &BrokerSpy::default(),
            &connectivity,
            &ConnectivityEvents,
            &probe,
            &StaleEvents::default(),
            &simulation,
        )
        .expect("a simulated order does not depend on the IB connection");
    assert_eq!(simulation.accepted.borrow().len(), 1);
    assert_eq!(connectivity.reads.get(), 0);
}

#[test]
fn c3_an_unknown_age_is_reported_as_unknown() {
    let engine = engine_with_live("live-alpha");
    let stale_events = StaleEvents::default();
    let err = engine
        .dispatch_order(
            order("paper-beta", "AAPL", 1),
            &BrokerSpy::default(),
            &ConnectivitySpy::new(ConnectivityState::Connected),
            &ConnectivityEvents,
            &ProbeSpy::stale(None),
            &stale_events,
            &SimulationSpy::default(),
        )
        .expect_err("a never-observed line is stale");
    assert!(err.message.contains("age unknown"), "{}", err.message);
    assert_eq!(stale_events.events.borrow()[0].staleness_seconds, None);
}

#[test]
fn c3_live_and_paper_on_one_engine_are_both_blocked_with_one_event_each() {
    // Non-vacuity for the paper gate's placement: the live arm is gated inside
    // route_order, the paper arm in dispatch_order, and neither is gated twice.
    let engine = engine_with_live("live-alpha");
    let broker = BrokerSpy::default();
    let probe = ProbeSpy::stale(Some(16));
    let stale_events = StaleEvents::default();
    let simulation = SimulationSpy::default();
    let connectivity = ConnectivitySpy::new(ConnectivityState::Connected);

    for strategy in ["live-alpha", "paper-beta"] {
        let err = engine
            .dispatch_order(
                order(strategy, "AAPL", 1),
                &broker,
                &connectivity,
                &ConnectivityEvents,
                &probe,
                &stale_events,
                &simulation,
            )
            .expect_err("both destinations refuse a stale line");
        assert_eq!(
            err.category,
            OrderErrorCategory::MarketDataStale,
            "{strategy}"
        );
        assert_eq!(err.error_type, "MarketDataStale", "{strategy}");
    }
    assert_eq!(broker.calls.get(), 0);
    assert!(simulation.accepted.borrow().is_empty());
    assert_eq!(
        probe.asked.borrow().len(),
        2,
        "one freshness read per order"
    );
    assert_eq!(stale_events.events.borrow().len(), 2, "one event per order");
}

#[test]
fn c3_a_malformed_paper_order_is_refused_as_malformed_even_when_stale() {
    // Validation runs at the shared entry before routing, so a malformed order
    // is told what is actually wrong with it rather than to wait for data.
    let engine = engine_with_live("live-alpha");
    let probe = ProbeSpy::stale(Some(30));
    let err = engine
        .dispatch_order(
            order("paper-beta", "AAPL", 0),
            &BrokerSpy::default(),
            &ConnectivitySpy::new(ConnectivityState::Connected),
            &ConnectivityEvents,
            &probe,
            &StaleEvents::default(),
            &SimulationSpy::default(),
        )
        .expect_err("a zero-quantity order is malformed");
    assert_eq!(err.category, OrderErrorCategory::OrderParametersInvalid);
    assert!(probe.asked.borrow().is_empty());
}
