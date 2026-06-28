//! ERR-4 / SRS-MD-002 / SyRS SYS-70 / SYS-64 / StRS A-13 — when a new
//! subscription request would exceed the operator-configured IB
//! market-data line limit, the subscription manager rejects the request
//! synchronously with `SUBSCRIPTION_LIMIT_REACHED`, publishes a
//! structured `SubscriptionLimitEvent` carrying both the observed
//! `current_lines` count and the `configured_limit` snapshot, and does
//! NOT mutate the subscription registry (the rejected request leaves
//! the line accounting exactly as it found it).
//!
//! L7 domain (safety) test. The post-conditions are:
//!   * `LineCounterSpy.try_acquire_calls == 1` per request (the gate
//!     probes the counter exactly once).
//!   * `EventSinkSpy.events.len() == 1` per blocked request, with
//!     `state == ExceededLimit`, `current_lines` and `configured_limit`
//!     matching what the counter reported, and the correct strategy /
//!     symbol.
//!   * The positive control (WithinLimit) returns
//!     `Ok(SubscriptionAccepted)` and emits zero events — proving the
//!     gate is selective.
//!   * The pseudo-property sweep over varying `current_lines` values
//!     (16, 30, 60, 999, 17 mirroring the ERR-3 sweep with line-count
//!     semantics) keeps the broker at zero acceptances and emits
//!     exactly one event per case.
//!   * SyRS SYS-64 invariant: live and paper subscriber strategy_ids
//!     produce identical rejection envelopes — the gate takes no
//!     `StrategyMode` parameter.
//!   * Zero-registry-mutation invariant (behavioral anchor): the
//!     `SubscriptionLineCounter` port exposes no mutator method, so
//!     the manager cannot move the in-use count through it. The
//!     primary enforcement lives in
//!     `tools/subscription_limit_check.py` via the contract's
//!     `forbidden_mutations` allowlist (which rejects any
//!     `registry.insert(`, `registry.add(`, `registry.push(`,
//!     `subscriptions.insert(`, etc. call inside the ExceededLimit
//!     match arm); this Rust test anchors the port-shape
//!     post-condition at the behavioral layer.

use atp_market_data::{
    MarketDataSubscriptionManager, SubscriptionLimitEventSink, SubscriptionLineCounter,
};
use atp_types::{
    AssetClass, OrderErrorCategory, StrategyId, SubscriptionLimitEvent, SubscriptionLimitState,
    SubscriptionRequest,
};
use std::cell::{Cell, RefCell};

struct LineCounterSpy {
    state: Cell<SubscriptionLimitState>,
    current_lines: Cell<u32>,
    configured_limit: Cell<u32>,
    try_acquire_calls: Cell<u32>,
    lines_in_use_calls: Cell<u32>,
    line_limit_calls: Cell<u32>,
}

impl LineCounterSpy {
    fn exceeded(current: u32, limit: u32) -> Self {
        Self {
            state: Cell::new(SubscriptionLimitState::ExceededLimit),
            current_lines: Cell::new(current),
            configured_limit: Cell::new(limit),
            try_acquire_calls: Cell::new(0),
            lines_in_use_calls: Cell::new(0),
            line_limit_calls: Cell::new(0),
        }
    }

    fn within(current: u32, limit: u32) -> Self {
        Self {
            state: Cell::new(SubscriptionLimitState::WithinLimit),
            current_lines: Cell::new(current),
            configured_limit: Cell::new(limit),
            try_acquire_calls: Cell::new(0),
            lines_in_use_calls: Cell::new(0),
            line_limit_calls: Cell::new(0),
        }
    }
}

impl SubscriptionLineCounter for LineCounterSpy {
    fn lines_in_use(&self) -> u32 {
        self.lines_in_use_calls
            .set(self.lines_in_use_calls.get() + 1);
        self.current_lines.get()
    }

    fn line_limit(&self) -> u32 {
        self.line_limit_calls.set(self.line_limit_calls.get() + 1);
        self.configured_limit.get()
    }

    fn try_acquire(&self, _request: &SubscriptionRequest) -> SubscriptionLimitState {
        self.try_acquire_calls.set(self.try_acquire_calls.get() + 1);
        self.state.get()
    }
}

#[derive(Default)]
struct EventSinkSpy {
    events: RefCell<Vec<SubscriptionLimitEvent>>,
}

impl SubscriptionLimitEventSink for EventSinkSpy {
    fn record(&self, event: SubscriptionLimitEvent) {
        self.events.borrow_mut().push(event);
    }
}

/// Sink that panics if consulted. Used by the WithinLimit positive
/// control to prove the rejection event channel is never invoked when
/// the gate admits.
struct ForbiddenSink;

impl SubscriptionLimitEventSink for ForbiddenSink {
    fn record(&self, _event: SubscriptionLimitEvent) {
        panic!("ERR-4: WithinLimit branch must not record a SubscriptionLimitEvent");
    }
}

fn request(strategy: &str, symbol: &str) -> SubscriptionRequest {
    SubscriptionRequest {
        strategy_id: StrategyId::new(strategy),
        symbol: symbol.to_string(),
        asset_class: AssetClass::Equity,
    }
}

#[test]
fn err_4_exceeded_state_blocks_request_with_structured_error() {
    // SRS-MD-002 / SyRS SYS-70: when a new subscription request would
    // exceed the configured ceiling, the manager must reject with
    // SUBSCRIPTION_LIMIT_REACHED, publish exactly one
    // SubscriptionLimitEvent carrying both current_lines AND
    // configured_limit, and surface the originating request unchanged
    // in the structured error envelope.
    let manager = MarketDataSubscriptionManager;
    let counter = LineCounterSpy::exceeded(100, 100);
    let sink = EventSinkSpy::default();
    let req = request("live-alpha-1", "AAPL");

    let error = manager
        .request_subscription(req.clone(), &counter, &sink)
        .expect_err("ERR-4: ExceededLimit must reject the subscription request");

    assert_eq!(
        error.category,
        OrderErrorCategory::SubscriptionLimitReached,
        "SRS-MD-002: category must be SubscriptionLimitReached"
    );
    assert_eq!(
        error.category.as_str(),
        "SUBSCRIPTION_LIMIT_REACHED",
        "wire string must match SyRS SYS-64 vocabulary"
    );
    assert_eq!(error.error_type, "SubscriptionLimitReached");
    assert!(
        error.message.contains("live-alpha-1"),
        "message must name the requesting strategy"
    );
    assert!(
        error.message.contains("AAPL"),
        "message must name the subscription symbol"
    );
    assert!(
        error.message.contains("SRS-MD-002"),
        "message must trace SRS-MD-002"
    );
    assert!(
        error.message.contains("SYS-70"),
        "message must cite SyRS SYS-70 (subscription manager enforcement)"
    );
    assert!(
        error.message.contains("100"),
        "message must surface the configured limit"
    );
    assert_eq!(
        error.original_request, req,
        "structured error must carry the original request parameters (SRS-MD-002)"
    );

    let recorded = sink.events.borrow();
    assert_eq!(
        recorded.len(),
        1,
        "exactly one SubscriptionLimitEvent must be recorded for dashboard alerting"
    );
    assert_eq!(recorded[0].state, SubscriptionLimitState::ExceededLimit);
    assert_eq!(recorded[0].strategy_id.as_str(), "live-alpha-1");
    assert_eq!(recorded[0].symbol, "AAPL");
    assert_eq!(recorded[0].current_lines, 100);
    assert_eq!(recorded[0].configured_limit, 100);
    assert_eq!(
        counter.try_acquire_calls.get(),
        1,
        "the gate must probe try_acquire exactly once per request"
    );
    assert_eq!(
        counter.lines_in_use_calls.get(),
        1,
        "lines_in_use must be read exactly once on the rejection leaf"
    );
    assert_eq!(
        counter.line_limit_calls.get(),
        1,
        "line_limit must be read exactly once on the rejection leaf"
    );
}

#[test]
fn err_4_within_limit_state_returns_accepted_and_emits_no_event() {
    // Negative control: ERR-4's rejection must be selective. A
    // WithinLimit state must return SubscriptionAccepted and must NOT
    // touch the event sink. The ForbiddenSink would panic if invoked.
    let manager = MarketDataSubscriptionManager;
    let counter = LineCounterSpy::within(50, 100);
    let sink = ForbiddenSink;
    let req = request("paper-alpha-7", "AAPL");

    let accepted = manager
        .request_subscription(req, &counter, &sink)
        .expect("WithinLimit must accept the request");

    assert_eq!(accepted.strategy_id.as_str(), "paper-alpha-7");
    assert_eq!(accepted.symbol, "AAPL");
    assert_eq!(
        counter.try_acquire_calls.get(),
        1,
        "the gate must probe try_acquire exactly once on the accept path too"
    );
    assert_eq!(
        counter.lines_in_use_calls.get(),
        0,
        "the WithinLimit leaf must not read lines_in_use (no event to populate)"
    );
    assert_eq!(
        counter.line_limit_calls.get(),
        0,
        "the WithinLimit leaf must not read line_limit (no event to populate)"
    );
}

#[test]
fn err_4_exceeded_state_holds_across_many_requests() {
    // Pseudo-property: regardless of strategy / symbol / current_lines,
    // an ExceededLimit state must never produce an acceptance, and
    // every blocked request must produce its own SubscriptionLimitEvent
    // carrying the per-case current_lines and configured_limit.
    let manager = MarketDataSubscriptionManager;
    let counter = LineCounterSpy::exceeded(100, 100);
    let sink = EventSinkSpy::default();
    let cases: [(&str, &str, u32, u32); 5] = [
        ("live-alpha-1", "AAPL", 100, 100),
        ("paper-bravo-2", "MSFT", 101, 100),
        ("paper-charlie-3", "BRK.B", 250, 200),
        ("paper-delta-4", "SPY", 500, 100),
        ("paper-echo-5", "QQQ", 9_999, 100),
    ];
    for (strategy, symbol, current, limit) in cases {
        counter.current_lines.set(current);
        counter.configured_limit.set(limit);
        let req = request(strategy, symbol);
        let err = manager
            .request_subscription(req.clone(), &counter, &sink)
            .expect_err("ExceededLimit always blocks");
        assert_eq!(err.category, OrderErrorCategory::SubscriptionLimitReached);
        assert_eq!(err.original_request, req);
    }
    assert_eq!(
        counter.try_acquire_calls.get(),
        cases.len() as u32,
        "try_acquire must be probed once per request — no double-counting"
    );
    let recorded = sink.events.borrow();
    assert_eq!(
        recorded.len(),
        cases.len(),
        "one SubscriptionLimitEvent per blocked request"
    );
    for (i, (strategy, symbol, current, limit)) in cases.iter().enumerate() {
        assert_eq!(recorded[i].state, SubscriptionLimitState::ExceededLimit);
        assert_eq!(recorded[i].strategy_id.as_str(), *strategy);
        assert_eq!(recorded[i].symbol, *symbol);
        assert_eq!(recorded[i].current_lines, *current);
        assert_eq!(recorded[i].configured_limit, *limit);
    }
}

#[test]
fn err_4_identical_contract_for_live_and_paper_subscribers() {
    // SyRS SYS-64 invariant: the rejection envelope must be identical
    // for a strategy_id naming a live strategy and one naming a paper
    // strategy. The subscription manager API takes NO StrategyMode
    // parameter precisely so that the two modes flow through the same
    // gate — this test demonstrates that the absence is correct.
    let manager = MarketDataSubscriptionManager;
    let counter = LineCounterSpy::exceeded(100, 100);
    let sink = EventSinkSpy::default();

    let live_req = request("live-alpha-1", "AAPL");
    let paper_req = request("paper-bravo-7", "AAPL");

    let live_err = manager
        .request_subscription(live_req.clone(), &counter, &sink)
        .expect_err("ExceededLimit must reject the live subscriber");
    let paper_err = manager
        .request_subscription(paper_req.clone(), &counter, &sink)
        .expect_err("ExceededLimit must reject the paper subscriber identically");

    // The wire form must be byte-identical across modes — that's
    // SYS-64's whole point.
    assert_eq!(live_err.category, paper_err.category);
    assert_eq!(live_err.error_type, paper_err.error_type);
    assert_eq!(live_err.category.as_str(), "SUBSCRIPTION_LIMIT_REACHED");
    assert_eq!(paper_err.category.as_str(), "SUBSCRIPTION_LIMIT_REACHED");

    // The original_request differs (different strategy_id) — that's
    // expected and is the per-caller payload.
    assert_eq!(live_err.original_request, live_req);
    assert_eq!(paper_err.original_request, paper_req);

    let recorded = sink.events.borrow();
    assert_eq!(
        recorded.len(),
        2,
        "one event per blocked request, regardless of mode"
    );
    // Same state and same numeric payload across both events — only
    // the strategy_id differs. SYS-70 fans out events for both modes.
    assert_eq!(recorded[0].state, recorded[1].state);
    assert_eq!(recorded[0].current_lines, recorded[1].current_lines);
    assert_eq!(recorded[0].configured_limit, recorded[1].configured_limit);
    assert_eq!(recorded[0].strategy_id.as_str(), "live-alpha-1");
    assert_eq!(recorded[1].strategy_id.as_str(), "paper-bravo-7");
}

#[test]
fn err_4_exceeded_state_anchors_zero_mutation_via_port_shape() {
    // Zero-registry-mutation invariant — behavioral anchor.
    //
    // The PRIMARY enforcement of this invariant is the static check in
    // `tools/subscription_limit_check.py`, which parses the
    // ExceededLimit match arm and rejects any call to the patterns
    // listed in the contract block's `forbidden_mutations` array
    // (registry.insert, registry.add, registry.push,
    // subscriptions.insert, etc.). This test anchors the post-condition
    // at the behavioral level by demonstrating that the manager's
    // public port surface (`SubscriptionLineCounter`) exposes NO
    // mutator method — `lines_in_use`, `line_limit`, and `try_acquire`
    // are all read-only. The manager therefore cannot mutate the
    // registry through the port even if a future refactor wanted to;
    // the only way to introduce a mutation would be to either widen
    // the port (which the static check on the trait body would catch)
    // or call a method on a concrete type bypassing the trait (which
    // the forbidden_mutations static check would catch).
    //
    // The behavioral assertions below pin the port-shape post-condition:
    //   * The manager invokes the read-only port methods (proving the
    //     gate is consulted).
    //   * No method on the port is observed to mutate the spy's
    //     internal `current_lines` — because the trait offers no such
    //     method.
    let manager = MarketDataSubscriptionManager;
    let counter = LineCounterSpy::exceeded(100, 100);
    let sink = EventSinkSpy::default();
    let req = request("live-alpha-1", "AAPL");

    let before = counter.current_lines.get();
    let _ = manager.request_subscription(req, &counter, &sink);
    let after = counter.current_lines.get();

    assert_eq!(
        before, after,
        "the SubscriptionLineCounter port exposes no mutator — a \
         rejected request cannot move the in-use count through this \
         surface"
    );
    // The manager DID consult the read-only methods for the event:
    // exactly one try_acquire, one lines_in_use, one line_limit. If
    // any of those counts grew past one on a single request, the gate
    // would be double-counting against the registry.
    assert_eq!(counter.try_acquire_calls.get(), 1);
    assert_eq!(counter.lines_in_use_calls.get(), 1);
    assert_eq!(counter.line_limit_calls.get(), 1);
    assert_eq!(
        sink.events.borrow().len(),
        1,
        "exactly one event recorded, proving the rejection ran end-to-end"
    );
}
