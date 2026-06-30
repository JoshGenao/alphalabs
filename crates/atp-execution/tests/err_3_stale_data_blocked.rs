//! ERR-3 / SRS-MD-004 / NFR-P5 — when subscribed market data for the
//! order's symbol is stale (heartbeat age > 15 s), a Live submission is
//! rejected synchronously with `MARKET_DATA_STALE`, the brokerage port is
//! NEVER invoked, no reconnect is requested (staleness is a data-side
//! condition, not a transport fault), and a structured `StaleDataEvent`
//! is published carrying the observed staleness.
//!
//! L7 domain (safety) test. The post-conditions are:
//!   * `BrokerageSpy.calls == 0` across every blocked submission.
//!   * `ConnectivitySpy.reconnect_calls == 0` per blocked submission.
//!   * `StaleEventSinkSpy.events.len() == 1` per blocked submission,
//!     with `state == Stale`, the correct strategy/symbol, and a
//!     `staleness_seconds` matching what the freshness probe reported.
//!   * The positive control (Connected + Fresh) still routes to the
//!     broker — proving the gate is selective.
//!   * The Unreachable case still rejects with `CONNECTIVITY_BLOCKED`
//!     and does NOT consult the freshness port — proving the ERR-2
//!     connectivity gate short-circuits ahead of the ERR-3 freshness
//!     gate (nested-match invariant).

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

/// Freshness probe parameterized over (state, staleness_seconds). Counts
/// every `freshness()` call so tests can assert the gate is consulted
/// exactly the expected number of times.
struct FreshnessSpy {
    state: Cell<MarketDataFreshness>,
    staleness_seconds: Cell<u64>,
    freshness_calls: Cell<u32>,
    staleness_calls: Cell<u32>,
}

impl FreshnessSpy {
    fn stale(seconds: u64) -> Self {
        Self {
            state: Cell::new(MarketDataFreshness::Stale),
            staleness_seconds: Cell::new(seconds),
            freshness_calls: Cell::new(0),
            staleness_calls: Cell::new(0),
        }
    }

    fn fresh() -> Self {
        Self {
            state: Cell::new(MarketDataFreshness::Fresh),
            staleness_seconds: Cell::new(0),
            freshness_calls: Cell::new(0),
            staleness_calls: Cell::new(0),
        }
    }
}

impl MarketDataFreshnessProbe for FreshnessSpy {
    fn freshness(&self, _symbol: &str) -> MarketDataFreshness {
        self.freshness_calls.set(self.freshness_calls.get() + 1);
        self.state.get()
    }

    fn staleness_seconds(&self, _symbol: &str) -> u64 {
        self.staleness_calls.set(self.staleness_calls.get() + 1);
        self.staleness_seconds.get()
    }
}

/// Freshness stub that panics if consulted. Used by the Unreachable case
/// to prove the connectivity gate short-circuits before the freshness
/// gate (nested-match invariant).
struct ForbiddenFreshness;

impl MarketDataFreshnessProbe for ForbiddenFreshness {
    fn freshness(&self, _symbol: &str) -> MarketDataFreshness {
        panic!("ERR-3: blocked-connectivity branch must not consult the freshness port");
    }

    fn staleness_seconds(&self, _symbol: &str) -> u64 {
        panic!("ERR-3: blocked-connectivity branch must not consult staleness_seconds");
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

#[test]
fn err_3_stale_state_blocks_live_submission_with_no_broker_call() {
    // SRS-MD-004 / NFR-P5: when subscribed market data is stale, the live
    // submission must fail with MARKET_DATA_STALE; the broker must not be
    // called; no reconnect must be requested (staleness is data-side, not
    // transport); and exactly one StaleDataEvent must be recorded with
    // the observed staleness_seconds matching what the probe reported.
    let engine = ExecutionEngine::default();
    let broker = BrokerageSpy::default();
    let connectivity = ConnectivitySpy::in_state(ConnectivityState::Connected);
    let events = EventSinkSpy::default();
    let freshness = FreshnessSpy::stale(22);
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
        .expect_err("ERR-3: Stale market data must reject the live submission");

    assert_eq!(
        error.category,
        OrderErrorCategory::MarketDataStale,
        "SRS-MD-004: category must be MARKET_DATA_STALE"
    );
    assert_eq!(
        error.category.as_str(),
        "MARKET_DATA_STALE",
        "wire string must match SyRS SYS-64 vocabulary"
    );
    assert_eq!(error.error_type, "MarketDataStale");
    assert!(
        error.message.contains("live-alpha-1"),
        "message must name the submitting strategy"
    );
    assert!(
        error.message.contains("AAPL"),
        "message must name the stale symbol"
    );
    assert!(
        error.message.contains("SRS-MD-004"),
        "message must trace SRS-MD-004"
    );
    assert!(
        error.message.contains("NFR-P5"),
        "message must cite the NFR-P5 15s threshold"
    );
    assert!(
        error.message.contains("22"),
        "message must surface the observed staleness in seconds"
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
        0,
        "staleness must not trigger a reconnect — that is reserved for transport faults"
    );
    assert!(
        events.events.borrow().is_empty(),
        "no ConnectivityEvent should be emitted for a data-side rejection"
    );

    let recorded = stale_events.events.borrow();
    assert_eq!(
        recorded.len(),
        1,
        "exactly one StaleDataEvent must be recorded for dashboard alerting"
    );
    assert_eq!(recorded[0].state, MarketDataFreshness::Stale);
    assert_eq!(recorded[0].strategy_id.as_str(), "live-alpha-1");
    assert_eq!(recorded[0].symbol, "AAPL");
    assert_eq!(
        recorded[0].staleness_seconds, 22,
        "the event must carry the staleness reported by the freshness probe"
    );
    assert_eq!(
        freshness.freshness_calls.get(),
        1,
        "the freshness port must be consulted exactly once per Live submission"
    );
    assert_eq!(
        freshness.staleness_calls.get(),
        1,
        "the staleness_seconds probe must be consulted exactly once on Stale"
    );
}

#[test]
fn err_3_fresh_state_still_routes_through_broker() {
    // Negative control: ERR-3's rejection must be selective. A Live +
    // Connected + Fresh submission still reaches the broker — otherwise
    // the gate would silently disable the live path even when the feed
    // is healthy.
    let engine = ExecutionEngine::default();
    let broker = BrokerageSpy::default();
    let connectivity = ConnectivitySpy::in_state(ConnectivityState::Connected);
    let events = EventSinkSpy::default();
    let freshness = FreshnessSpy::fresh();
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
        "no reconnect should be requested when the feed is fresh"
    );
    assert!(events.events.borrow().is_empty());
    assert!(stale_events.events.borrow().is_empty());
    assert_eq!(
        freshness.freshness_calls.get(),
        1,
        "the freshness port must be consulted exactly once on the Live + Connected path"
    );
    assert_eq!(
        freshness.staleness_calls.get(),
        0,
        "staleness_seconds must not be probed when the state is Fresh"
    );
}

#[test]
fn err_3_unreachable_state_does_not_consult_freshness_port() {
    // Nested-match invariant: the ERR-2 connectivity gate must
    // short-circuit before the ERR-3 freshness gate. If
    // `ConnectivityState::Unreachable` somehow fell through to the
    // freshness check, `ForbiddenFreshness` would panic the test. The
    // submission still fails — but with CONNECTIVITY_BLOCKED, not
    // MARKET_DATA_STALE.
    let engine = ExecutionEngine::default();
    let broker = BrokerageSpy::default();
    let connectivity = ConnectivitySpy::in_state(ConnectivityState::Unreachable);
    let events = EventSinkSpy::default();
    let freshness = ForbiddenFreshness;
    let stale_events = StaleEventSinkSpy::default();
    let order = submission("live-alpha-1", "AAPL", 100);

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
        .expect_err("Unreachable still blocks — but for the connectivity reason");

    assert_eq!(error.category, OrderErrorCategory::ConnectivityBlocked);
    assert_eq!(error.category.as_str(), "CONNECTIVITY_BLOCKED");
    assert_ne!(
        error.category,
        OrderErrorCategory::MarketDataStale,
        "Unreachable must be reported as CONNECTIVITY_BLOCKED, not MARKET_DATA_STALE"
    );
    assert_eq!(broker.calls.get(), 0);
    assert_eq!(connectivity.reconnect_calls.get(), 1);
    assert_eq!(events.events.borrow().len(), 1);
    assert!(
        stale_events.events.borrow().is_empty(),
        "Unreachable must not emit a stale-data event"
    );
}

#[test]
fn err_3_stale_state_holds_across_many_live_submissions() {
    // Pseudo-property: regardless of strategy / symbol / quantity /
    // staleness age, a Stale state must never permit the broker to be
    // called, and every blocked submission must produce its own
    // StaleDataEvent carrying the observed age.
    let engine = ExecutionEngine::default();
    let broker = BrokerageSpy::default();
    let connectivity = ConnectivitySpy::in_state(ConnectivityState::Connected);
    let events = EventSinkSpy::default();
    let freshness = FreshnessSpy::stale(0);
    let stale_events = StaleEventSinkSpy::default();
    let cases: [(&str, &str, i64, u64); 5] = [
        ("live-alpha-1", "AAPL", 1, 16),
        ("live-alpha-1", "AAPL", -1, 30),
        ("live-alpha-1", "BRK.B", 10_000, 60),
        ("live-alpha-1", "MSFT", 0, 999),
        ("live-alpha-1", "SPY", 250, 17),
    ];
    for (strategy, symbol, qty, staleness) in cases {
        freshness.staleness_seconds.set(staleness);
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
            .expect_err("Stale always blocks");
        assert_eq!(err.category, OrderErrorCategory::MarketDataStale);
        assert_eq!(err.original_order, order);
    }
    assert_eq!(
        broker.calls.get(),
        0,
        "no IB order side effect across {} stale submissions",
        cases.len()
    );
    assert_eq!(
        connectivity.reconnect_calls.get(),
        0,
        "no reconnect attempts across {} stale submissions (data-side, not transport)",
        cases.len()
    );
    assert!(events.events.borrow().is_empty());
    let recorded = stale_events.events.borrow();
    assert_eq!(
        recorded.len(),
        cases.len(),
        "one StaleDataEvent per blocked submission"
    );
    for (i, (_, symbol, _, staleness)) in cases.iter().enumerate() {
        assert_eq!(recorded[i].symbol, *symbol);
        assert_eq!(recorded[i].staleness_seconds, *staleness);
        assert_eq!(recorded[i].state, MarketDataFreshness::Stale);
    }
}
