//! SRS-MD-001 / SyRS SYS-70 / StRS SN-1.10 / SN-1.29 / SC-25 / A-13 — the
//! consolidated market-data subscription registry deduplicates real-time
//! subscriptions across active strategies (one upstream IB subscription per
//! security regardless of subscriber count) and fans received ticks out to
//! every subscriber of that security — and to no other subscriber.
//!
//! L7 domain (safety) test. The post-conditions are:
//!   * Dedup: N strategies subscribing to the same symbol keep
//!     `distinct_subscriptions() == 1` for that symbol; only the FIRST
//!     subscriber returns `Opened` (one new upstream line), subsequent
//!     ones return `SubscriberAdded` (no new line).
//!   * Fan-out isolation: a tick for symbol X reaches exactly the
//!     subscribers of X, in subscription order, and never a subscriber of
//!     another symbol.
//!   * Lifecycle: removing a non-last subscriber returns `SubscriberRemoved`
//!     and holds the line; removing the LAST subscriber returns `Closed`
//!     and releases the upstream line.
//!   * Change publication: every line-affecting / dedup transition publishes
//!     one `SubscriptionChangeEvent` carrying the post-transition
//!     subscriber_count and lines_in_use; the idempotent no-ops publish
//!     nothing (a `ForbiddenChangeSink` panics if invoked).
//!   * Cross-feature seam: the registry IS the concrete
//!     `SubscriptionLineCounter` the SRS-MD-002 gate consumes — a duplicate
//!     of an existing symbol is admitted (no new line), a new symbol at the
//!     ceiling is rejected with `SUBSCRIPTION_LIMIT_REACHED`.
//!   * Fail-closed: empty symbol / empty strategy_id are rejected and
//!     register nothing.
//!
//! The ≤100 ms fan-out latency NFR and the real IB upstream feed are
//! deferred runtime halves (see `subscription_fanout_contract.deferred[]`);
//! this test pins the structural dedup + fan-out + line-accounting contract.

use atp_market_data::{
    ConsolidatedSubscriptionRegistry, MarketDataSubscriptionManager, SubscriptionChangeSink,
    SubscriptionLimitEventSink, SubscriptionLineCounter, SubscriptionRegistryError,
};
use atp_types::{
    MarketDataTick, OrderErrorCategory, StrategyId, SubscriptionChange, SubscriptionChangeEvent,
    SubscriptionLimitEvent, SubscriptionLimitState, SubscriptionRequest,
};
use std::cell::RefCell;

#[derive(Default)]
struct ChangeSinkSpy {
    events: RefCell<Vec<SubscriptionChangeEvent>>,
}

impl SubscriptionChangeSink for ChangeSinkSpy {
    fn record(&self, event: SubscriptionChangeEvent) {
        self.events.borrow_mut().push(event);
    }
}

/// Sink that panics if consulted — proves an idempotent no-op publishes
/// nothing.
struct ForbiddenChangeSink;

impl SubscriptionChangeSink for ForbiddenChangeSink {
    fn record(&self, _event: SubscriptionChangeEvent) {
        panic!("SRS-MD-001: idempotent no-op must not publish a SubscriptionChangeEvent");
    }
}

/// Limit-event sink for the SRS-MD-002 gate seam test.
#[derive(Default)]
struct LimitSinkSpy {
    events: RefCell<Vec<SubscriptionLimitEvent>>,
}

impl SubscriptionLimitEventSink for LimitSinkSpy {
    fn record(&self, event: SubscriptionLimitEvent) {
        self.events.borrow_mut().push(event);
    }
}

fn sub(strategy: &str, symbol: &str) -> SubscriptionRequest {
    SubscriptionRequest {
        strategy_id: StrategyId::new(strategy),
        symbol: symbol.to_string(),
    }
}

#[test]
fn srs_md_001_duplicate_subscriptions_consume_one_upstream_line() {
    // Core AC: "Multiple strategies subscribing to the same security
    // consume one IB subscription."
    let mut registry = ConsolidatedSubscriptionRegistry::new(100);
    let sink = ChangeSinkSpy::default();

    assert_eq!(
        registry
            .subscribe(&sub("live-alpha", "AAPL"), &sink)
            .unwrap(),
        SubscriptionChange::Opened,
        "first subscriber opens one upstream line"
    );
    for strat in ["paper-b", "paper-c", "paper-d", "paper-e"] {
        assert_eq!(
            registry.subscribe(&sub(strat, "AAPL"), &sink).unwrap(),
            SubscriptionChange::SubscriberAdded,
            "additional subscribers must dedup onto the same line"
        );
    }

    assert_eq!(
        registry.distinct_subscriptions(),
        1,
        "five subscribers must consume exactly one upstream IB subscription"
    );
    assert_eq!(registry.subscriber_count("AAPL"), 5);
    assert!(registry.is_subscribed(&StrategyId::new("paper-c"), "AAPL"));
}

#[test]
fn srs_md_001_fan_out_isolates_by_symbol() {
    // Core AC: "each subscriber receives fan-out data" — and the
    // isolation invariant that a subscriber of one security never
    // receives another security's tick.
    let mut registry = ConsolidatedSubscriptionRegistry::new(100);
    let sink = ChangeSinkSpy::default();
    registry
        .subscribe(&sub("live-alpha", "AAPL"), &sink)
        .unwrap();
    registry.subscribe(&sub("paper-b", "AAPL"), &sink).unwrap();
    registry.subscribe(&sub("paper-c", "MSFT"), &sink).unwrap();

    let aapl = registry
        .fan_out(&MarketDataTick {
            symbol: "AAPL".to_string(),
            tick_seq: 42,
        })
        .unwrap();
    let aapl_ids: Vec<&str> = aapl.iter().map(StrategyId::as_str).collect();
    assert_eq!(
        aapl_ids,
        vec!["live-alpha", "paper-b"],
        "AAPL tick fans out to AAPL subscribers in subscription order"
    );
    assert!(
        !aapl_ids.contains(&"paper-c"),
        "the MSFT subscriber must never receive an AAPL tick"
    );

    let msft = registry
        .fan_out(&MarketDataTick {
            symbol: "MSFT".to_string(),
            tick_seq: 43,
        })
        .unwrap();
    assert_eq!(
        msft.iter().map(StrategyId::as_str).collect::<Vec<_>>(),
        vec!["paper-c"]
    );

    // A tick for a symbol nobody subscribes to reaches no one.
    let none = registry
        .fan_out(&MarketDataTick {
            symbol: "NFLX".to_string(),
            tick_seq: 44,
        })
        .unwrap();
    assert!(none.is_empty());
}

#[test]
fn srs_md_001_unsubscribe_lifecycle_releases_line() {
    let mut registry = ConsolidatedSubscriptionRegistry::new(100);
    let sink = ChangeSinkSpy::default();
    registry
        .subscribe(&sub("live-alpha", "AAPL"), &sink)
        .unwrap();
    registry.subscribe(&sub("paper-b", "AAPL"), &sink).unwrap();

    assert_eq!(
        registry
            .unsubscribe(&StrategyId::new("live-alpha"), "AAPL", &sink)
            .unwrap(),
        SubscriptionChange::SubscriberRemoved,
        "removing a non-last subscriber holds the line"
    );
    assert_eq!(registry.distinct_subscriptions(), 1);
    assert!(!registry.is_subscribed(&StrategyId::new("live-alpha"), "AAPL"));

    assert_eq!(
        registry
            .unsubscribe(&StrategyId::new("paper-b"), "AAPL", &sink)
            .unwrap(),
        SubscriptionChange::Closed,
        "removing the last subscriber releases the upstream line"
    );
    assert_eq!(registry.distinct_subscriptions(), 0);

    // Re-subscribing after Closed opens a fresh line.
    assert_eq!(
        registry.subscribe(&sub("paper-z", "AAPL"), &sink).unwrap(),
        SubscriptionChange::Opened
    );
    assert_eq!(registry.distinct_subscriptions(), 1);
}

#[test]
fn srs_md_001_change_events_track_consolidation() {
    // Every line-affecting / dedup transition publishes exactly one
    // SubscriptionChangeEvent with the post-transition counts; the
    // idempotent no-op publishes nothing.
    let mut registry = ConsolidatedSubscriptionRegistry::new(100);
    let sink = ChangeSinkSpy::default();
    registry
        .subscribe(&sub("live-alpha", "AAPL"), &sink)
        .unwrap();
    registry.subscribe(&sub("paper-b", "AAPL"), &sink).unwrap();

    // Idempotent re-subscribe must publish nothing (ForbiddenChangeSink
    // panics on record).
    assert_eq!(
        registry
            .subscribe(&sub("paper-b", "AAPL"), &ForbiddenChangeSink)
            .unwrap(),
        SubscriptionChange::AlreadySubscribed
    );

    let events = sink.events.borrow();
    assert_eq!(events.len(), 2, "only the two real transitions publish");

    assert_eq!(events[0].change, SubscriptionChange::Opened);
    assert_eq!(events[0].strategy_id.as_str(), "live-alpha");
    assert_eq!(events[0].symbol, "AAPL");
    assert_eq!(events[0].subscriber_count, 1);
    assert_eq!(events[0].lines_in_use, 1);
    assert!(events[0].change.changes_line_count());

    assert_eq!(events[1].change, SubscriptionChange::SubscriberAdded);
    assert_eq!(events[1].strategy_id.as_str(), "paper-b");
    assert_eq!(events[1].subscriber_count, 2);
    // Dedup: two subscribers, still exactly one upstream line.
    assert_eq!(events[1].lines_in_use, 1);
    assert!(!events[1].change.changes_line_count());
}

#[test]
fn srs_md_001_registry_is_concrete_line_counter_for_md_002_gate() {
    // Cross-feature seam: the consolidated registry IS the concrete
    // SubscriptionLineCounter the SRS-MD-002 gate consumes (closing the
    // subscription_limit_contract.deferred[] ownership). A duplicate of
    // an existing symbol is admitted (no new line); a NEW symbol at the
    // ceiling is rejected with SUBSCRIPTION_LIMIT_REACHED.
    let mut registry = ConsolidatedSubscriptionRegistry::new(1);
    registry
        .subscribe(&sub("live-alpha", "AAPL"), &ChangeSinkSpy::default())
        .unwrap();

    let manager = MarketDataSubscriptionManager;
    let limit_sink = LimitSinkSpy::default();

    // Duplicate of the already-subscribed security → admitted (dedup
    // consumes no new line even at a limit of 1).
    let accepted = manager
        .request_subscription(sub("paper-b", "AAPL"), &registry, &limit_sink)
        .expect("a duplicate subscription consumes no new line and is admitted");
    assert_eq!(accepted.symbol, "AAPL");
    assert!(
        limit_sink.events.borrow().is_empty(),
        "admission emits no limit event"
    );

    // A new security would need a 2nd line against a limit of 1 → rejected.
    let err = manager
        .request_subscription(sub("paper-b", "MSFT"), &registry, &limit_sink)
        .expect_err("a new symbol at the ceiling must be rejected");
    assert_eq!(err.category, OrderErrorCategory::SubscriptionLimitReached);
    assert_eq!(err.category.as_str(), "SUBSCRIPTION_LIMIT_REACHED");
    assert_eq!(limit_sink.events.borrow().len(), 1);
    assert_eq!(
        limit_sink.events.borrow()[0].state,
        SubscriptionLimitState::ExceededLimit
    );

    assert_eq!(registry.lines_in_use(), 1);
    assert_eq!(registry.line_limit(), 1);
}

#[test]
fn srs_md_001_rejects_empty_symbol_and_strategy() {
    // Fail-closed boundary: empty / whitespace symbol and strategy_id are
    // rejected, and a rejected subscribe registers nothing.
    let mut registry = ConsolidatedSubscriptionRegistry::new(100);
    let sink = ChangeSinkSpy::default();

    assert_eq!(
        registry.subscribe(&sub("live-alpha", "   "), &sink),
        Err(SubscriptionRegistryError::EmptySymbol)
    );
    assert_eq!(
        registry.subscribe(&sub("", "AAPL"), &sink),
        Err(SubscriptionRegistryError::EmptyStrategyId)
    );
    assert_eq!(
        registry.fan_out(&MarketDataTick {
            symbol: String::new(),
            tick_seq: 1,
        }),
        Err(SubscriptionRegistryError::EmptySymbol)
    );
    assert_eq!(
        registry.unsubscribe(&StrategyId::new(""), "AAPL", &sink),
        Err(SubscriptionRegistryError::EmptyStrategyId)
    );

    assert_eq!(registry.distinct_subscriptions(), 0);
    assert!(sink.events.borrow().is_empty());
}

#[test]
fn srs_md_001_fan_out_holds_across_many_symbols_and_subscribers() {
    // Pseudo-property sweep: build a multi-symbol book and assert the
    // dedup + isolation invariants hold across every symbol.
    let mut registry = ConsolidatedSubscriptionRegistry::new(100);
    let sink = ChangeSinkSpy::default();
    let book: [(&str, &[&str]); 4] = [
        ("AAPL", &["live-a", "paper-b", "paper-c"]),
        ("MSFT", &["paper-b", "paper-d"]),
        ("SPY", &["live-a", "paper-c", "paper-d", "paper-e"]),
        ("QQQ", &["paper-e"]),
    ];
    for (symbol, subs) in book {
        for strat in subs {
            registry.subscribe(&sub(strat, symbol), &sink).unwrap();
        }
    }

    // One upstream line per distinct symbol, regardless of total
    // subscriber count (3 + 2 + 4 + 1 = 10 subscriptions, 4 lines).
    assert_eq!(registry.distinct_subscriptions(), book.len() as u32);

    for (symbol, subs) in book {
        let recipients = registry
            .fan_out(&MarketDataTick {
                symbol: symbol.to_string(),
                tick_seq: 1,
            })
            .unwrap();
        let ids: Vec<&str> = recipients.iter().map(StrategyId::as_str).collect();
        assert_eq!(
            ids, subs,
            "{symbol} must fan out to exactly its subscribers, in order"
        );
        assert_eq!(registry.subscriber_count(symbol), subs.len() as u32);
    }
}
