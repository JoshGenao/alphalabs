//! SRS-MD-001 runtime / SyRS SYS-70 - the thread-safe fan-out runtime drives
//! exactly ONE upstream line per security and delivers every tick to each
//! subscriber's own bounded queue without letting a slow reader delay others.
//!
//! Boundary + domain test over a fake upstream feed (no IB). Post-conditions:
//!   * Upstream binding: `open` runs once per security on the first
//!     subscriber; `close` runs once when the last subscriber leaves.
//!   * Fail closed: a failed `open`, an over-limit request, a suspended or
//!     disconnected gateway (SRS-MD-005 / SYS-45), or an unregistered
//!     subscriber registers nothing, publishes nothing, and sends nothing
//!     upstream.
//!   * Delivery: each subscriber receives every tick of its securities, in
//!     order, and nothing else.
//!   * Slow reader: a full queue drops the tick for that subscriber only,
//!     counts it, and publishes one lag event per episode.
//!   * Disconnect: a dropped receiver deregisters the subscriber and releases
//!     the lines it held alone.
//!   * Latency probe (fixture feed, `#[ignore]`-gated wall-clock test): p99.9
//!     ingest-to-receive latency within the 100 ms budget, written to
//!     target/verification/ as a PROBE report. It is not an SRS-MD-001
//!     verification artifact: that needs the real IB feed and a
//!     PTP-disciplined clock (`LatencyVerificationArtifact` refuses anything
//!     else) and is SERIALIZED.

use atp_market_data::{
    FanOutDelivery, FanOutRuntime, FanOutRuntimeError, IngestReport, MarketDataSubscriptionManager,
    RestartWindowGate, SubscriberLagEvent, SubscriberLagSink, SubscriberReceiver,
    SubscriptionChangeSink, SubscriptionLimitEventSink, SubscriptionLineCounter,
    SubscriptionRegistryError, UpstreamFeedError, UpstreamMarketDataFeed,
};
use atp_types::{
    AssetClass, LatencyPercentiles, MarketDataAdmission, MarketDataTick, OrderErrorCategory,
    Percentile, SecurityKey, StrategyId, SubscriptionChange, SubscriptionChangeEvent,
    SubscriptionLimitEvent, SubscriptionLimitState, SubscriptionRequest,
    SUBSCRIPTION_FANOUT_LATENCY_MS,
};
use std::collections::{BTreeSet, VecDeque};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

/// Local handle so the test crate can implement the runtime's ports for
/// shared fixtures (the orphan rule forbids `impl ForeignTrait for Arc<_>`).
struct Shared<T>(Arc<T>);

impl<T> std::ops::Deref for Shared<T> {
    type Target = T;
    fn deref(&self) -> &T {
        &self.0
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum FeedCall {
    Open(String),
    Close(String),
}

/// Fake upstream feed: records every call; can be told to refuse a symbol,
/// or to PANIC on one (to poison the runtime lock it is called under).
#[derive(Default)]
struct FakeFeed {
    calls: Mutex<Vec<FeedCall>>,
    refuse: Mutex<BTreeSet<String>>,
    panic_on: Mutex<BTreeSet<String>>,
}

impl FakeFeed {
    fn calls(&self) -> Vec<FeedCall> {
        self.calls.lock().unwrap().clone()
    }
    fn refuse(&self, symbol: &str) {
        self.refuse.lock().unwrap().insert(symbol.to_string());
    }
    fn panic_on(&self, symbol: &str) {
        self.panic_on.lock().unwrap().insert(symbol.to_string());
    }
}

impl UpstreamMarketDataFeed for Shared<FakeFeed> {
    fn open(&self, key: &SecurityKey) -> Result<(), UpstreamFeedError> {
        if self.panic_on.lock().unwrap().contains(key.symbol()) {
            panic!("fixture panics opening {}", key.symbol());
        }
        if self.refuse.lock().unwrap().contains(key.symbol()) {
            return Err(UpstreamFeedError {
                reason: format!("fixture refuses {}", key.symbol()),
            });
        }
        self.calls
            .lock()
            .unwrap()
            .push(FeedCall::Open(key.symbol().to_string()));
        Ok(())
    }
    fn close(&self, key: &SecurityKey) {
        self.calls
            .lock()
            .unwrap()
            .push(FeedCall::Close(key.symbol().to_string()));
    }
}

/// Controllable SRS-MD-005 gate. Answers from a script first (one answer per
/// `admission()` call, to model the window closing between two checks), then
/// from the steady state.
struct FakeWindow {
    steady: Mutex<MarketDataAdmission>,
    script: Mutex<VecDeque<MarketDataAdmission>>,
}

impl Default for FakeWindow {
    fn default() -> Self {
        Self {
            steady: Mutex::new(MarketDataAdmission::Admitted),
            script: Mutex::new(VecDeque::new()),
        }
    }
}

impl FakeWindow {
    fn set(&self, admission: MarketDataAdmission) {
        *self.steady.lock().unwrap() = admission;
    }
    fn script(&self, answers: &[MarketDataAdmission]) {
        self.script.lock().unwrap().extend(answers.iter().copied());
    }
}

impl RestartWindowGate for Shared<FakeWindow> {
    fn admission(&self) -> MarketDataAdmission {
        self.script
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or_else(|| *self.steady.lock().unwrap())
    }
}

/// An always-open window for the SRS-MD-002 gate call.
struct OpenWindow;

impl RestartWindowGate for OpenWindow {
    fn admission(&self) -> MarketDataAdmission {
        MarketDataAdmission::Admitted
    }
}

#[derive(Default)]
struct ChangeSpy {
    events: Mutex<Vec<SubscriptionChangeEvent>>,
}

impl SubscriptionChangeSink for Shared<ChangeSpy> {
    fn record(&self, event: SubscriptionChangeEvent) {
        self.events.lock().unwrap().push(event);
    }
}

#[derive(Default)]
struct LagSpy {
    events: Mutex<Vec<SubscriberLagEvent>>,
}

impl SubscriberLagSink for Shared<LagSpy> {
    fn record(&self, event: SubscriberLagEvent) {
        self.events.lock().unwrap().push(event);
    }
}

#[derive(Default)]
struct LimitSpy {
    events: Mutex<Vec<SubscriptionLimitEvent>>,
}

impl SubscriptionLimitEventSink for LimitSpy {
    fn record(&self, event: SubscriptionLimitEvent) {
        self.events.lock().unwrap().push(event);
    }
}

type Runtime =
    FanOutRuntime<Shared<FakeFeed>, Shared<FakeWindow>, Shared<ChangeSpy>, Shared<LagSpy>>;

struct Fixture {
    runtime: Arc<Runtime>,
    feed: Arc<FakeFeed>,
    window: Arc<FakeWindow>,
    changes: Arc<ChangeSpy>,
    lag: Arc<LagSpy>,
}

fn fixture(line_limit: u32) -> Fixture {
    let feed = Arc::new(FakeFeed::default());
    let window = Arc::new(FakeWindow::default());
    let changes = Arc::new(ChangeSpy::default());
    let lag = Arc::new(LagSpy::default());
    let runtime = Arc::new(FanOutRuntime::new(
        line_limit,
        Shared(Arc::clone(&feed)),
        Shared(Arc::clone(&window)),
        Shared(Arc::clone(&changes)),
        Shared(Arc::clone(&lag)),
    ));
    Fixture {
        runtime,
        feed,
        window,
        changes,
        lag,
    }
}

fn id(name: &str) -> StrategyId {
    StrategyId::new(name)
}

fn sub(strategy: &str, symbol: &str) -> SubscriptionRequest {
    SubscriptionRequest {
        strategy_id: id(strategy),
        symbol: symbol.to_string(),
        asset_class: AssetClass::Equity,
    }
}

fn key(symbol: &str) -> SecurityKey {
    SecurityKey::new(symbol, AssetClass::Equity).unwrap()
}

fn tick(symbol: &str, tick_seq: u64) -> MarketDataTick {
    MarketDataTick {
        symbol: symbol.to_string(),
        asset_class: AssetClass::Equity,
        tick_seq,
    }
}

fn open(symbol: &str) -> FeedCall {
    FeedCall::Open(symbol.to_string())
}

fn close(symbol: &str) -> FeedCall {
    FeedCall::Close(symbol.to_string())
}

fn drain(receiver: &SubscriberReceiver) -> Vec<(String, u64)> {
    std::iter::from_fn(|| receiver.try_recv().ok())
        .map(|d| (d.tick.symbol.clone(), d.tick.tick_seq))
        .collect()
}

#[test]
fn srs_md_001_many_subscribers_drive_one_upstream_open() {
    // Core AC: five strategies on AAPL consume ONE upstream subscription.
    let f = fixture(100);
    let mut receivers = Vec::new();
    for name in ["live-a", "paper-b", "paper-c", "paper-d", "paper-e"] {
        receivers.push(f.runtime.register_subscriber(id(name), 16).unwrap());
        f.runtime.subscribe(&sub(name, "AAPL")).unwrap();
    }
    assert_eq!(
        f.feed.calls(),
        vec![open("AAPL")],
        "exactly one upstream open"
    );
    assert_eq!(f.runtime.lines_in_use(), 1);
    assert_eq!(f.runtime.subscriber_count(&key("AAPL")), 5);

    let report = f.runtime.ingest(tick("AAPL", 1), Instant::now()).unwrap();
    assert_eq!(
        report,
        IngestReport {
            delivered: 5,
            dropped: 0,
            disconnected: 0
        }
    );
    for receiver in &receivers {
        let delivery = receiver.try_recv().expect("every subscriber gets the tick");
        assert_eq!(delivery.tick.tick_seq, 1);
    }
}

#[test]
fn srs_md_001_close_runs_only_when_last_subscriber_leaves() {
    let f = fixture(100);
    let _a = f.runtime.register_subscriber(id("live-a"), 4).unwrap();
    let _b = f.runtime.register_subscriber(id("paper-b"), 4).unwrap();
    f.runtime.subscribe(&sub("live-a", "AAPL")).unwrap();
    f.runtime.subscribe(&sub("paper-b", "AAPL")).unwrap();

    assert_eq!(
        f.runtime.unsubscribe(&id("live-a"), &key("AAPL")).unwrap(),
        SubscriptionChange::SubscriberRemoved
    );
    assert_eq!(
        f.feed.calls(),
        vec![open("AAPL")],
        "line still held by paper-b"
    );

    assert_eq!(
        f.runtime.unsubscribe(&id("paper-b"), &key("AAPL")).unwrap(),
        SubscriptionChange::Closed
    );
    assert_eq!(f.feed.calls(), vec![open("AAPL"), close("AAPL")]);
    assert_eq!(f.runtime.lines_in_use(), 0);

    // Re-subscribing opens a fresh line.
    f.runtime.subscribe(&sub("paper-b", "AAPL")).unwrap();
    assert_eq!(
        f.feed.calls(),
        vec![open("AAPL"), close("AAPL"), open("AAPL")]
    );
}

#[test]
fn srs_md_001_failed_upstream_open_registers_nothing() {
    let f = fixture(100);
    let _a = f.runtime.register_subscriber(id("live-a"), 4).unwrap();
    f.feed.refuse("AAPL");

    let error = f.runtime.subscribe(&sub("live-a", "AAPL")).unwrap_err();
    assert!(matches!(error, FanOutRuntimeError::Upstream(_)));
    assert_eq!(f.runtime.lines_in_use(), 0);
    assert_eq!(f.runtime.subscriber_count(&key("AAPL")), 0);
    assert!(f.changes.events.lock().unwrap().is_empty(), "no event leak");
    assert!(f.feed.calls().is_empty());

    // A tick for the refused security reaches no one.
    let report = f.runtime.ingest(tick("AAPL", 1), Instant::now()).unwrap();
    assert_eq!(report, IngestReport::default());
}

#[test]
fn srs_md_001_over_limit_request_never_touches_upstream() {
    let f = fixture(1);
    let _a = f.runtime.register_subscriber(id("live-a"), 4).unwrap();
    f.runtime.subscribe(&sub("live-a", "AAPL")).unwrap();

    assert_eq!(
        f.runtime.subscribe(&sub("live-a", "MSFT")),
        Err(FanOutRuntimeError::Registry(
            SubscriptionRegistryError::LineLimitReached {
                configured_limit: 1
            }
        ))
    );
    assert_eq!(
        f.feed.calls(),
        vec![open("AAPL")],
        "no open-then-close of MSFT"
    );

    // A duplicate still fits: it needs no new line.
    let _b = f.runtime.register_subscriber(id("paper-b"), 4).unwrap();
    assert_eq!(
        f.runtime.subscribe(&sub("paper-b", "AAPL")).unwrap(),
        SubscriptionChange::SubscriberAdded
    );
}

#[test]
fn srs_md_001_suspended_window_sends_nothing_upstream() {
    // SRS-MD-005 / SYS-75(a): opening a line is a market-data request. During
    // the restart window the first subscriber is refused BEFORE the upstream
    // is asked, and a request to join an existing line is refused too.
    let f = fixture(100);
    let _a = f.runtime.register_subscriber(id("live-a"), 4).unwrap();
    let _b = f.runtime.register_subscriber(id("paper-b"), 4).unwrap();
    f.runtime.subscribe(&sub("live-a", "AAPL")).unwrap();
    f.window
        .set(MarketDataAdmission::SuspendedForScheduledRestart);

    assert_eq!(
        f.runtime.subscribe(&sub("live-a", "MSFT")),
        Err(FanOutRuntimeError::Registry(
            SubscriptionRegistryError::SuspendedForScheduledRestart
        ))
    );
    assert_eq!(
        f.runtime.subscribe(&sub("paper-b", "AAPL")),
        Err(FanOutRuntimeError::Registry(
            SubscriptionRegistryError::SuspendedForScheduledRestart
        ))
    );
    assert_eq!(
        f.feed.calls(),
        vec![open("AAPL")],
        "no request during the window"
    );
    assert_eq!(f.runtime.subscriber_count(&key("AAPL")), 1);

    // Releasing a line is always allowed, even inside the window.
    assert_eq!(
        f.runtime.unsubscribe(&id("live-a"), &key("AAPL")).unwrap(),
        SubscriptionChange::Closed
    );
    assert_eq!(f.feed.calls(), vec![open("AAPL"), close("AAPL")]);

    // After the window the identical request succeeds.
    f.window.set(MarketDataAdmission::Admitted);
    f.runtime.subscribe(&sub("live-a", "MSFT")).unwrap();
    assert_eq!(
        f.feed.calls(),
        vec![open("AAPL"), close("AAPL"), open("MSFT")]
    );
}

#[test]
fn srs_md_001_lost_gateway_sends_nothing_upstream() {
    // SYS-45: an unreachable gateway is an incident, not maintenance; the
    // refusal says so and no upstream request is attempted.
    let f = fixture(100);
    let _a = f.runtime.register_subscriber(id("live-a"), 4).unwrap();
    f.window.set(MarketDataAdmission::ConnectivityLost);
    assert_eq!(
        f.runtime.subscribe(&sub("live-a", "AAPL")),
        Err(FanOutRuntimeError::Registry(
            SubscriptionRegistryError::ConnectivityLost
        ))
    );
    assert!(f.feed.calls().is_empty());
    assert!(f.changes.events.lock().unwrap().is_empty());
}

#[test]
fn srs_md_001_window_closing_mid_admission_releases_the_line() {
    // The runtime's check admits, then the window closes before the
    // registry's own check. The just-opened line must be closed again so no
    // upstream line outlives a refused registration.
    let f = fixture(100);
    let _a = f.runtime.register_subscriber(id("live-a"), 4).unwrap();
    f.window.script(&[
        MarketDataAdmission::Admitted,
        MarketDataAdmission::SuspendedForScheduledRestart,
    ]);
    assert_eq!(
        f.runtime.subscribe(&sub("live-a", "AAPL")),
        Err(FanOutRuntimeError::Registry(
            SubscriptionRegistryError::SuspendedForScheduledRestart
        ))
    );
    assert_eq!(f.feed.calls(), vec![open("AAPL"), close("AAPL")]);
    assert_eq!(f.runtime.lines_in_use(), 0);
    assert!(f.changes.events.lock().unwrap().is_empty());

    // The strategy holds nothing, so deregistering closes nothing.
    assert!(f
        .runtime
        .deregister_subscriber(&id("live-a"))
        .unwrap()
        .is_empty());
    assert_eq!(f.feed.calls(), vec![open("AAPL"), close("AAPL")]);
}

#[test]
fn srs_md_001_subscribe_requires_registered_queue() {
    let f = fixture(100);
    assert_eq!(
        f.runtime.subscribe(&sub("ghost", "AAPL")),
        Err(FanOutRuntimeError::SubscriberNotRegistered)
    );
    assert!(f.feed.calls().is_empty());

    assert_eq!(
        f.runtime.register_subscriber(id("live-a"), 0).unwrap_err(),
        FanOutRuntimeError::ZeroCapacity
    );
    assert_eq!(
        f.runtime.register_subscriber(id("  "), 4).unwrap_err(),
        FanOutRuntimeError::Registry(SubscriptionRegistryError::EmptyStrategyId)
    );
    let _a = f.runtime.register_subscriber(id("live-a"), 4).unwrap();
    assert_eq!(
        f.runtime.register_subscriber(id("live-a"), 4).unwrap_err(),
        FanOutRuntimeError::SubscriberAlreadyRegistered
    );

    // Options still fail closed before any upstream call, even inside the
    // window (a permanently-invalid request gets its own error in every phase).
    f.window
        .set(MarketDataAdmission::SuspendedForScheduledRestart);
    let option = SubscriptionRequest {
        strategy_id: id("live-a"),
        symbol: "AAPL".to_string(),
        asset_class: AssetClass::Option,
    };
    assert_eq!(
        f.runtime.subscribe(&option),
        Err(FanOutRuntimeError::Registry(
            SubscriptionRegistryError::OptionContractUnsupported
        ))
    );
    assert!(f.feed.calls().is_empty());
}

#[test]
fn srs_md_001_delivery_is_isolated_and_ordered() {
    let f = fixture(100);
    let a = f.runtime.register_subscriber(id("live-a"), 64).unwrap();
    let b = f.runtime.register_subscriber(id("paper-b"), 64).unwrap();
    f.runtime.subscribe(&sub("live-a", "AAPL")).unwrap();
    f.runtime.subscribe(&sub("live-a", "MSFT")).unwrap();
    f.runtime.subscribe(&sub("paper-b", "MSFT")).unwrap();

    for seq in 1..=10 {
        let symbol = if seq % 2 == 0 { "AAPL" } else { "MSFT" };
        f.runtime.ingest(tick(symbol, seq), Instant::now()).unwrap();
    }

    let got_a = drain(&a);
    assert_eq!(
        got_a.iter().map(|(_, seq)| *seq).collect::<Vec<_>>(),
        (1..=10).collect::<Vec<_>>()
    );
    assert_eq!(
        drain(&b),
        [1, 3, 5, 7, 9]
            .into_iter()
            .map(|seq| ("MSFT".to_string(), seq))
            .collect::<Vec<_>>(),
        "paper-b never receives AAPL"
    );
}

#[test]
fn srs_md_001_slow_reader_drops_only_its_own_ticks() {
    // Operator decision: drop + mark lagging. A full queue never delays the
    // other subscriber and never blocks ingest.
    let f = fixture(100);
    let slow = f.runtime.register_subscriber(id("slow"), 2).unwrap();
    let fast = f.runtime.register_subscriber(id("fast"), 64).unwrap();
    f.runtime.subscribe(&sub("slow", "AAPL")).unwrap();
    f.runtime.subscribe(&sub("fast", "AAPL")).unwrap();

    for seq in 1..=5 {
        f.runtime.ingest(tick("AAPL", seq), Instant::now()).unwrap();
    }
    assert_eq!(f.runtime.dropped_count(&id("slow")), Some(3));
    assert_eq!(f.runtime.dropped_count(&id("fast")), Some(0));
    assert!(f.runtime.is_lagging(&id("slow")));
    assert_eq!(drain(&fast).len(), 5);

    // One lag event for the whole episode, naming the first dropped tick.
    {
        let events = f.lag.events.lock().unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].strategy_id, id("slow"));
        assert_eq!(events[0].symbol, "AAPL");
        assert_eq!(events[0].tick_seq, 3);
        assert_eq!(events[0].dropped_total, 1);
    }

    // The slow reader catches up -> the episode ends at the next delivery.
    let seqs: Vec<u64> = drain(&slow).into_iter().map(|(_, seq)| seq).collect();
    assert_eq!(seqs, vec![1, 2], "drops show up as a tick_seq gap");
    f.runtime.ingest(tick("AAPL", 6), Instant::now()).unwrap();
    assert!(!f.runtime.is_lagging(&id("slow")));

    // A new overflow starts a new episode and publishes again: the queue
    // holds 6 and 7, so 8 is the first drop and 9 the second.
    for seq in 7..=9 {
        f.runtime.ingest(tick("AAPL", seq), Instant::now()).unwrap();
    }
    let events = f.lag.events.lock().unwrap();
    assert_eq!(events.len(), 2);
    assert_eq!(events[1].tick_seq, 8);
    assert_eq!(events[1].dropped_total, 4);
    assert_eq!(f.runtime.dropped_count(&id("slow")), Some(5));
}

#[test]
fn srs_md_001_dropped_receiver_releases_its_lines() {
    let f = fixture(100);
    let gone = f.runtime.register_subscriber(id("gone"), 4).unwrap();
    let stay = f.runtime.register_subscriber(id("stay"), 4).unwrap();
    f.runtime.subscribe(&sub("gone", "AAPL")).unwrap();
    f.runtime.subscribe(&sub("gone", "MSFT")).unwrap();
    f.runtime.subscribe(&sub("stay", "MSFT")).unwrap();
    drop(gone);

    let report = f.runtime.ingest(tick("MSFT", 1), Instant::now()).unwrap();
    assert_eq!(
        report,
        IngestReport {
            delivered: 1,
            dropped: 0,
            disconnected: 1
        }
    );
    // AAPL was gone's alone -> closed; MSFT is still held by stay.
    assert_eq!(
        f.feed.calls(),
        vec![open("AAPL"), open("MSFT"), close("AAPL")]
    );
    assert_eq!(f.runtime.lines_in_use(), 1);
    assert_eq!(f.runtime.dropped_count(&id("gone")), None);
    assert_eq!(stay.try_recv().unwrap().tick.tick_seq, 1);
}

#[test]
fn srs_md_001_dropped_receiver_on_an_idle_security_frees_its_line() {
    // Review r1: a receiver dropped while its security never ticks again must
    // not keep the line open (and counting toward the IB line limit) until a
    // tick that may never come. The next runtime call of ANY kind reaps it.
    let f = fixture(1);
    let idle = f.runtime.register_subscriber(id("idle"), 4).unwrap();
    f.runtime.subscribe(&sub("idle", "ILLQ")).unwrap();
    drop(idle);

    // No tick for ILLQ ever arrives. A read already reflects the release ...
    assert_eq!(f.runtime.lines_in_use(), 0);
    assert_eq!(f.feed.calls(), vec![open("ILLQ"), close("ILLQ")]);
    assert_eq!(f.runtime.dropped_count(&id("idle")), None);
    // ... so the single-line budget is free for the next strategy.
    let _next = f.runtime.register_subscriber(id("next"), 4).unwrap();
    f.runtime.subscribe(&sub("next", "MSFT")).unwrap();
    assert_eq!(f.runtime.lines_in_use(), 1);
    // The explicit sweep has nothing left to do.
    assert_eq!(f.runtime.reap_departed().unwrap(), 0);
}

#[test]
fn srs_md_001_stale_departure_note_never_tears_down_a_re_registration() {
    // Identity, not name: a strategy that deregisters and registers again
    // gets a NEW queue. Dropping its OLD receiver afterwards must not reap it.
    let f = fixture(100);
    let old = f.runtime.register_subscriber(id("live-a"), 4).unwrap();
    f.runtime.deregister_subscriber(&id("live-a")).unwrap();
    let new = f.runtime.register_subscriber(id("live-a"), 4).unwrap();
    f.runtime.subscribe(&sub("live-a", "AAPL")).unwrap();
    drop(old);

    assert_eq!(f.runtime.reap_departed().unwrap(), 0, "the note is stale");
    let report = f.runtime.ingest(tick("AAPL", 1), Instant::now()).unwrap();
    assert_eq!(report.delivered, 1);
    assert_eq!(report.disconnected, 0);
    assert_eq!(new.try_recv().unwrap().tick.tick_seq, 1);
    assert_eq!(f.feed.calls(), vec![open("AAPL")]);
}

#[test]
fn srs_md_001_reap_departed_counts_each_dropped_subscriber_once() {
    let f = fixture(100);
    let a = f.runtime.register_subscriber(id("a"), 4).unwrap();
    let b = f.runtime.register_subscriber(id("b"), 4).unwrap();
    f.runtime.subscribe(&sub("a", "AAPL")).unwrap();
    f.runtime.subscribe(&sub("b", "AAPL")).unwrap();
    drop(a);
    drop(b);
    assert_eq!(f.runtime.reap_departed().unwrap(), 2);
    assert_eq!(f.runtime.reap_departed().unwrap(), 0);
    assert_eq!(f.feed.calls(), vec![open("AAPL"), close("AAPL")]);
}

#[test]
fn srs_md_001_poisoned_lock_fails_every_mutation_closed() {
    // A port that panics under the runtime lock poisons it. From then on the
    // state may be inconsistent: every mutation refuses with Poisoned, the
    // SRS-MD-002 probe refuses capacity, and reads still report but never
    // reap (reaping mutates).
    let f = fixture(100);
    let gone = f.runtime.register_subscriber(id("gone"), 4).unwrap();
    let _live = f.runtime.register_subscriber(id("live-a"), 4).unwrap();
    f.runtime.subscribe(&sub("gone", "AAPL")).unwrap();
    f.feed.panic_on("BOOM");
    let runtime = Arc::clone(&f.runtime);
    let crashed = thread::spawn(move || runtime.subscribe(&sub("live-a", "BOOM"))).join();
    assert!(crashed.is_err(), "the port panic must propagate");

    drop(gone);
    assert_eq!(
        f.runtime.subscribe(&sub("live-a", "MSFT")),
        Err(FanOutRuntimeError::Poisoned)
    );
    assert_eq!(
        f.runtime.unsubscribe(&id("live-a"), &key("AAPL")),
        Err(FanOutRuntimeError::Poisoned)
    );
    assert_eq!(
        f.runtime.ingest(tick("AAPL", 1), Instant::now()),
        Err(FanOutRuntimeError::Poisoned)
    );
    assert_eq!(
        f.runtime.register_subscriber(id("new"), 4).unwrap_err(),
        FanOutRuntimeError::Poisoned
    );
    assert_eq!(
        f.runtime.deregister_subscriber(&id("live-a")),
        Err(FanOutRuntimeError::Poisoned)
    );
    assert_eq!(f.runtime.reap_departed(), Err(FanOutRuntimeError::Poisoned));
    assert_eq!(
        f.runtime.try_acquire(&sub("live-a", "MSFT")),
        SubscriptionLimitState::ExceededLimit,
        "a poisoned runtime must not report capacity"
    );
    // Reads still answer, without reaping: AAPL's line is still counted and
    // no close was sent for the dropped receiver.
    assert_eq!(f.runtime.lines_in_use(), 1);
    assert_eq!(f.runtime.subscriber_count(&key("AAPL")), 1);
    assert_eq!(f.feed.calls(), vec![open("AAPL")]);
}

#[test]
fn srs_md_001_deregister_releases_every_line() {
    let f = fixture(100);
    let _a = f.runtime.register_subscriber(id("live-a"), 4).unwrap();
    f.runtime.subscribe(&sub("live-a", "AAPL")).unwrap();
    f.runtime.subscribe(&sub("live-a", "SPY")).unwrap();
    // An explicit unsubscribe is forgotten, so deregistration does not try to
    // release AAPL twice.
    f.runtime.unsubscribe(&id("live-a"), &key("AAPL")).unwrap();
    assert_eq!(
        f.runtime.deregister_subscriber(&id("live-a")).unwrap(),
        vec![SubscriptionChange::Closed]
    );
    assert_eq!(
        f.feed.calls(),
        vec![open("AAPL"), open("SPY"), close("AAPL"), close("SPY")]
    );
    assert_eq!(
        f.runtime.deregister_subscriber(&id("live-a")),
        Err(FanOutRuntimeError::SubscriberNotRegistered)
    );
}

#[test]
fn srs_md_001_runtime_is_the_md_002_line_counter() {
    let f = fixture(1);
    let _a = f.runtime.register_subscriber(id("live-a"), 4).unwrap();
    f.runtime.subscribe(&sub("live-a", "AAPL")).unwrap();
    let gate = MarketDataSubscriptionManager;
    let limit = LimitSpy::default();

    assert!(gate
        .request_subscription(
            sub("paper-b", "AAPL"),
            &OpenWindow,
            f.runtime.as_ref(),
            &limit
        )
        .is_ok());
    let error = gate
        .request_subscription(
            sub("paper-b", "MSFT"),
            &OpenWindow,
            f.runtime.as_ref(),
            &limit,
        )
        .unwrap_err();
    assert_eq!(error.category, OrderErrorCategory::SubscriptionLimitReached);
    assert_eq!(limit.events.lock().unwrap().len(), 1);
    assert_eq!(f.runtime.line_limit(), 1);
}

#[test]
fn srs_md_001_concurrent_subscribers_share_one_line() {
    // Thread safety: 8 threads subscribe to the same security at once; the
    // upstream still sees exactly one open.
    let f = fixture(100);
    let handles: Vec<_> = (0..8)
        .map(|n| {
            let runtime = Arc::clone(&f.runtime);
            thread::spawn(move || {
                let name = format!("paper-{n}");
                let receiver = runtime.register_subscriber(id(&name), 4).unwrap();
                runtime.subscribe(&sub(&name, "AAPL")).unwrap();
                receiver
            })
        })
        .collect();
    let receivers: Vec<_> = handles.into_iter().map(|h| h.join().unwrap()).collect();
    assert_eq!(f.feed.calls(), vec![open("AAPL")]);
    assert_eq!(f.runtime.subscriber_count(&key("AAPL")), 8);
    let opened = f
        .changes
        .events
        .lock()
        .unwrap()
        .iter()
        .filter(|e| e.change == SubscriptionChange::Opened)
        .count();
    assert_eq!(opened, 1);
    drop(receivers);
}

#[test]
fn srs_md_001_receivers_dropped_during_ingest_release_every_line_once() {
    // Review r3: drop receivers on other threads WHILE ingest and unsubscribe
    // run, so the departure-note path and the Disconnected path race for real.
    // Overlap is MEASURED, not inferred: each dropper records whether the
    // ingest loop was mid-flight at the moment it dropped.
    use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};

    const DROPPERS: usize = 8;
    let f = fixture(100);
    let symbols = ["AAPL", "MSFT", "SPY", "QQQ"];
    let mut receivers = Vec::new();
    for n in 0..DROPPERS {
        let name = format!("s-{n}");
        receivers.push(f.runtime.register_subscriber(id(&name), 2).unwrap());
        // Each subscriber holds two symbols; every symbol has several holders.
        f.runtime.subscribe(&sub(&name, symbols[n % 4])).unwrap();
        f.runtime
            .subscribe(&sub(&name, symbols[(n + 1) % 4]))
            .unwrap();
    }
    assert_eq!(f.runtime.lines_in_use(), 4);

    let ingesting = Arc::new(AtomicBool::new(true));
    let overlapping_drops = Arc::new(AtomicU32::new(0));
    let ingest = {
        let runtime = Arc::clone(&f.runtime);
        let ingesting = Arc::clone(&ingesting);
        thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(10);
            let mut seq = 0;
            let mut disconnected = 0;
            // Run until every subscriber is gone (bounded: FAIL, never HANG).
            while runtime.lines_in_use() > 0 {
                assert!(Instant::now() < deadline, "ingest loop never drained");
                seq += 1;
                let symbol = ["AAPL", "MSFT", "SPY", "QQQ"][(seq % 4) as usize];
                disconnected += runtime
                    .ingest(tick(symbol, seq), Instant::now())
                    .unwrap()
                    .disconnected;
            }
            ingesting.store(false, Ordering::SeqCst);
            disconnected
        })
    };
    let droppers: Vec<_> = receivers
        .into_iter()
        .enumerate()
        .map(|(n, receiver)| {
            let runtime = Arc::clone(&f.runtime);
            let ingesting = Arc::clone(&ingesting);
            let overlapping = Arc::clone(&overlapping_drops);
            thread::spawn(move || {
                // Read a little, unsubscribe one symbol (racing ingest), then drop.
                let _ = receiver.recv_timeout(Duration::from_millis(5));
                if n % 2 == 0 {
                    runtime
                        .unsubscribe(&id(&format!("s-{n}")), &key(symbols[n % 4]))
                        .unwrap();
                }
                if ingesting.load(Ordering::SeqCst) {
                    overlapping.fetch_add(1, Ordering::SeqCst);
                }
                drop(receiver);
            })
        })
        .collect();
    for dropper in droppers {
        dropper.join().unwrap();
    }
    let disconnected = ingest.join().unwrap();

    assert!(
        overlapping_drops.load(Ordering::SeqCst) >= 1,
        "no receiver was dropped while ingest was running - the race was not exercised"
    );
    // Reaping happens inside whichever call comes first (an ingest, the
    // loop's own lines_in_use read, a dropper's unsubscribe), so ingest sees
    // only some of the departures. The property is "every subscriber is gone
    // and every line closed exactly once", asserted directly below.
    assert!(
        disconnected as usize <= DROPPERS,
        "no subscriber reaped twice"
    );
    for n in 0..DROPPERS {
        assert_eq!(f.runtime.dropped_count(&id(&format!("s-{n}"))), None);
    }
    assert_eq!(f.runtime.lines_in_use(), 0);
    assert_eq!(
        f.runtime.reap_departed().unwrap(),
        0,
        "nothing reaped twice"
    );
    // Every symbol was opened once and closed exactly once.
    let calls = f.feed.calls();
    for symbol in symbols {
        assert_eq!(
            calls.iter().filter(|c| **c == open(symbol)).count(),
            1,
            "{symbol}"
        );
        assert_eq!(
            calls.iter().filter(|c| **c == close(symbol)).count(),
            1,
            "{symbol}"
        );
    }
}

// ------------------------------------------------------------------------- //
// Latency probe (fixture feed). Wall-clock, so `#[ignore]`-gated: run it with
// `cargo test -p atp-market-data --test srs_md_001_fanout_runtime -- --ignored`.
// It is a PROBE of the in-process transport, not the AC's verification
// artifact: that needs the real IB feed and a PTP-disciplined clock, which
// `LatencyVerificationArtifact::from_samples` enforces. SERIALIZED.
// ------------------------------------------------------------------------- //

const SUBSCRIBERS: usize = 5;
const TICKS: u64 = 10_000;

#[test]
#[ignore = "wall-clock latency probe; run explicitly with --ignored"]
fn srs_md_001_fanout_latency_probe_on_fixture_feed() {
    let f = fixture(100);
    let symbols = ["AAPL", "MSFT", "SPY"];
    // Every subscriber takes all three symbols, so each tick fans out to all
    // five. Queues hold every tick so nothing is dropped: the probe measures
    // transport latency, not the drop policy.
    let mut readers = Vec::new();
    for n in 0..SUBSCRIBERS {
        let name = format!("strategy-{n}");
        let receiver = f
            .runtime
            .register_subscriber(id(&name), TICKS as usize)
            .unwrap();
        for symbol in symbols {
            f.runtime.subscribe(&sub(&name, symbol)).unwrap();
        }
        readers.push(thread::spawn(move || {
            let mut samples_ns = Vec::with_capacity(TICKS as usize);
            let mut last_seq = 0;
            while samples_ns.len() < TICKS as usize {
                // Bounded wait: a lost tick FAILS the probe instead of hanging.
                let delivery: Arc<FanOutDelivery> = receiver
                    .recv_timeout(Duration::from_secs(10))
                    .expect("reader starved: a tick was never delivered");
                samples_ns.push(delivery.upstream_received_at.elapsed().as_nanos() as u64);
                assert!(delivery.tick.tick_seq > last_seq, "in-order delivery");
                last_seq = delivery.tick.tick_seq;
            }
            // Hand the receiver back: dropping it here would deregister the
            // subscriber (by design) before its drop count is read below.
            (samples_ns, receiver)
        }));
    }
    assert_eq!(f.runtime.lines_in_use(), symbols.len() as u32);
    assert_eq!(f.feed.calls().len(), symbols.len(), "one open per symbol");

    for seq in 1..=TICKS {
        let symbol = symbols[(seq % symbols.len() as u64) as usize];
        let report = f.runtime.ingest(tick(symbol, seq), Instant::now()).unwrap();
        assert_eq!(report.delivered as usize, SUBSCRIBERS);
    }

    let (per_reader, receivers): (Vec<Vec<u64>>, Vec<SubscriberReceiver>) =
        readers.into_iter().map(|r| r.join().unwrap()).unzip();
    let samples: Vec<u64> = per_reader.into_iter().flatten().collect();
    let dropped: u64 = (0..SUBSCRIBERS)
        .map(|n| {
            f.runtime
                .dropped_count(&id(&format!("strategy-{n}")))
                .unwrap()
        })
        .sum();
    // PERF-001's nearest-rank engine: every reported value is an observed sample.
    let pct = LatencyPercentiles::from_samples(&samples).unwrap();
    let max_ns = *samples.iter().max().unwrap();
    let budget_ns = SUBSCRIPTION_FANOUT_LATENCY_MS * 1_000_000;
    let within_budget = pct.get_ns(Percentile::P999) <= budget_ns;

    let report = format!(
        concat!(
            "{{\n",
            "  \"kind\": \"fan-out latency PROBE (not an SRS-MD-001 verification artifact)\",\n",
            "  \"feed\": \"fixture (in-process; no IB)\",\n",
            "  \"clock\": \"host monotonic (std::time::Instant); PTP not verified - SRS-PERF-001\",\n",
            "  \"boundary\": \"FanOutRuntime::ingest entry -> subscriber recv\",\n",
            "  \"subscribers\": {},\n",
            "  \"symbols\": {},\n",
            "  \"ticks\": {},\n",
            "  \"samples\": {},\n",
            "  \"dropped\": {},\n",
            "  \"latency_ns\": {{ \"p50\": {}, \"p95\": {}, \"p99\": {}, \"p99_9\": {}, \"max\": {} }},\n",
            "  \"budget_ns\": {},\n",
            "  \"p99_9_within_budget\": {}\n",
            "}}\n"
        ),
        SUBSCRIBERS,
        symbols.len(),
        TICKS,
        samples.len(),
        dropped,
        pct.get_ns(Percentile::P50),
        pct.get_ns(Percentile::P95),
        pct.get_ns(Percentile::P99),
        pct.get_ns(Percentile::P999),
        max_ns,
        budget_ns,
        within_budget,
    );
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../target/verification");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("srs_md_001_fanout_latency_probe.json"), &report).unwrap();
    println!("{report}");

    assert_eq!(samples.len(), SUBSCRIBERS * TICKS as usize);
    assert_eq!(dropped, 0, "the probe must not lose ticks");
    drop(receivers);
    assert!(
        within_budget,
        "p99.9 fan-out latency {} ns exceeds the {budget_ns} ns budget",
        pct.get_ns(Percentile::P999)
    );
}
