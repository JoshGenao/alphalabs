//! SRS-MD-005 / SyRS SYS-75(a) — market-data requests are suspended during the
//! scheduled IB Gateway restart window, at BOTH subscription admission points.
//!
//! The acceptance criterion suspends "order submission **and market data
//! requests**" together. The order half was already gated by the ERR-2
//! connectivity guard in `atp-execution`; this file covers the half that had no
//! surface at all before SRS-MD-005.
//!
//! Post-conditions:
//!   * `MarketDataSubscriptionManager::request_subscription` refuses while the
//!     window suspends, with `ConnectivityBlocked` / `ScheduledRestartWindow`
//!     — the SAME category the live-order gate uses for this window, so an
//!     operator reads one event class rather than two.
//!   * The refusal happens BEFORE the line-limit probe: the counter is never
//!     consulted and no `SubscriptionLimitEvent` is published. A suspended
//!     request must not appear in the operator's line-exhaustion trail.
//!   * `ConsolidatedSubscriptionRegistry::subscribe` — the MUTATING admission
//!     point — refuses too, and leaves the registry byte-identical. Gating only
//!     the outer manager would leave this one as a bypass.
//!   * **Non-vacuity, in both directions.** Every refusal above is paired with
//!     the identical caller SUCCEEDING while the window is open. A gate that
//!     refused unconditionally would silently disable market data altogether
//!     and still pass a suppression-only test suite.
//!   * The gate is driven by a REAL `atp_types::RestartWindow` over injected
//!     instants, not by a hand-written boolean, so the phase arithmetic and the
//!     admission decision are proven to agree.

use atp_market_data::{
    ConsolidatedSubscriptionRegistry, MarketDataSubscriptionManager, RestartWindowGate,
    SubscriptionChangeSink, SubscriptionLimitEventSink, SubscriptionLineCounter,
    SubscriptionRegistryError,
};
use atp_types::{
    AssetClass, MarketDataAdmission, OrderErrorCategory, RestartWindow, SecurityKey, StrategyId,
    SubscriptionChange, SubscriptionChangeEvent, SubscriptionLimitEvent, SubscriptionLimitState,
    SubscriptionRequest, DEFAULT_RESTART_SUSPEND_LEAD_SECONDS, NANOS_PER_SECOND,
};
use std::cell::{Cell, RefCell};

/// 2026-09-04T03:45:00Z = 23:45 America/New_York on 2026-09-03 (EDT).
const RESTART_NS: i64 = 1_788_493_500 * NANOS_PER_SECOND;
const LEAD_NS: i64 = DEFAULT_RESTART_SUSPEND_LEAD_SECONDS * NANOS_PER_SECOND;

/// The production shape of the port: the answer is DERIVED from a real
/// `RestartWindow` at an injected instant, never asserted by the test.
///
/// This is what makes the suspension cases evidence rather than description —
/// a bug in `RestartWindow::admits_market_data_requests` fails these tests, and
/// a hand-written `false` could not.
struct WindowAt {
    window: RestartWindow,
    now_ns: i64,
    reachable: bool,
}

impl WindowAt {
    /// The gateway is still answering. SYS-75(a) suspends anyway during the
    /// lead, and pinning reachability TRUE is what proves that suspension is
    /// driven by the SCHEDULE rather than by an observed disconnect.
    fn at(now_ns: i64) -> Self {
        Self::new(now_ns, true)
    }

    fn new(now_ns: i64, reachable: bool) -> Self {
        Self {
            window: RestartWindow::with_defaults(RESTART_NS).expect("SYS-75 defaults are valid"),
            now_ns,
            reachable,
        }
    }
}

impl RestartWindowGate for WindowAt {
    fn admission(&self) -> MarketDataAdmission {
        self.window
            .market_data_admission(self.now_ns, self.reachable)
    }
}

/// Counts probes so the test can prove the limit gate was never reached.
#[derive(Default)]
struct CountingCounter {
    try_acquire_calls: Cell<u32>,
}

impl SubscriptionLineCounter for CountingCounter {
    fn lines_in_use(&self) -> u32 {
        0
    }

    fn line_limit(&self) -> u32 {
        100
    }

    fn try_acquire(&self, _request: &SubscriptionRequest) -> SubscriptionLimitState {
        self.try_acquire_calls.set(self.try_acquire_calls.get() + 1);
        SubscriptionLimitState::WithinLimit
    }
}

/// Panics if consulted — a suspended request is not a limit breach and must
/// never reach the line-exhaustion event channel.
struct ForbiddenLimitSink;

impl SubscriptionLimitEventSink for ForbiddenLimitSink {
    fn record(&self, _event: SubscriptionLimitEvent) {
        panic!(
            "SRS-MD-005: a request suspended for the scheduled restart window \
             must not publish a SubscriptionLimitEvent"
        );
    }
}

#[derive(Default)]
struct ChangeSinkSpy {
    events: RefCell<Vec<SubscriptionChangeEvent>>,
}

impl SubscriptionChangeSink for ChangeSinkSpy {
    fn record(&self, event: SubscriptionChangeEvent) {
        self.events.borrow_mut().push(event);
    }
}

fn sub(strategy: &str, symbol: &str) -> SubscriptionRequest {
    SubscriptionRequest {
        strategy_id: StrategyId::new(strategy),
        symbol: symbol.to_string(),
        asset_class: AssetClass::Equity,
    }
}

fn eq_key(symbol: &str) -> SecurityKey {
    SecurityKey::new(symbol, AssetClass::Equity).expect("non-empty symbol")
}

#[test]
fn srs_md_005_a_subscription_request_is_refused_during_the_restart_window() {
    // SyRS SYS-75(a): 30 s before the expected restart, inside the 60 s lead.
    let manager = MarketDataSubscriptionManager;
    let counter = CountingCounter::default();
    let request = sub("live-alpha", "AAPL");

    let error = manager
        .request_subscription(
            request.clone(),
            &WindowAt::at(RESTART_NS - 30 * NANOS_PER_SECOND),
            &counter,
            &ForbiddenLimitSink,
        )
        .expect_err("SRS-MD-005: the restart window must suspend market-data requests");

    assert_eq!(
        error.category,
        OrderErrorCategory::ConnectivityBlocked,
        "SRS-MD-005 reuses the live-order gate's category so one window is one event class"
    );
    assert_eq!(error.error_type, "ScheduledRestartWindow");
    assert!(
        error.message.contains("SRS-MD-005"),
        "message must trace SRS-MD-005; got {}",
        error.message
    );
    assert!(
        error.message.contains("SYS-75"),
        "message must cite SyRS SYS-75; got {}",
        error.message
    );
    assert_eq!(
        error.original_request, request,
        "the suspended request is echoed back unchanged — it is retryable as-is"
    );
    assert_eq!(
        counter.try_acquire_calls.get(),
        0,
        "SRS-MD-005: the suspension precedes the line-limit probe, so a suspended \
         request must consume no line accounting at all"
    );
}

#[test]
fn srs_md_005_the_same_request_is_admitted_outside_the_window() {
    // The non-vacuity control for the test above. Without it, a gate that
    // refused EVERY request would pass the suppression case while silently
    // disabling market data — the failure this pairing exists to catch.
    let manager = MarketDataSubscriptionManager;
    let counter = CountingCounter::default();
    let sink = ForbiddenLimitSink;
    let request = sub("live-alpha", "AAPL");

    let accepted = manager
        .request_subscription(
            request.clone(),
            // One nanosecond before the lead begins.
            &WindowAt::at(RESTART_NS - LEAD_NS - 1),
            &counter,
            &sink,
        )
        .expect("outside the window the identical request must be admitted");

    assert_eq!(accepted.strategy_id, request.strategy_id);
    assert_eq!(accepted.symbol, request.symbol);
    assert_eq!(
        counter.try_acquire_calls.get(),
        1,
        "outside the window the ERR-4 line-limit probe runs exactly once, unchanged"
    );
}

#[test]
fn srs_md_005_the_mutating_admission_point_is_gated_too() {
    // `subscribe` is the call that actually opens an upstream IB line. Gating
    // only `request_subscription` would leave this as a bypass, which is the
    // "validate at EVERY entry point, not the outermost" class.
    let mut registry = ConsolidatedSubscriptionRegistry::new(100);
    let sink = ChangeSinkSpy::default();

    // Inside the 60 s lead, where suspension is unconditional. Deliberately
    // NOT an instant inside the restart window itself with a reachable
    // gateway: SYS-75(d) says that case RESUMES, and the truth table below
    // pins it.
    let error = registry
        .subscribe(
            &sub("live-alpha", "AAPL"),
            &WindowAt::at(RESTART_NS - 30 * NANOS_PER_SECOND),
            &sink,
        )
        .expect_err("SRS-MD-005: the registry must refuse to open a line during the window");

    assert_eq!(
        error,
        SubscriptionRegistryError::SuspendedForScheduledRestart
    );
    assert_ne!(
        error,
        SubscriptionRegistryError::LineLimitReached {
            configured_limit: 100
        },
        "a suspension is not a line-budget problem — conflating them sends the \
         operator hunting for a limit that is not exhausted"
    );
    assert!(
        error.to_string().contains("SYS-75"),
        "the rendered refusal must name the requirement; got {error}"
    );

    // Nothing was registered: no line, no subscriber, no change event.
    assert_eq!(
        registry.distinct_subscriptions(),
        0,
        "a refused subscribe must leave the registry exactly as it found it"
    );
    assert_eq!(registry.subscriber_count(&eq_key("AAPL")), 0);
    assert!(!registry.is_subscribed(&StrategyId::new("live-alpha"), &eq_key("AAPL")));
    assert!(
        sink.events.borrow().is_empty(),
        "a refused subscribe must publish no SubscriptionChangeEvent"
    );
}

#[test]
fn srs_md_005_the_mutating_admission_point_still_opens_lines_outside_the_window() {
    // The non-vacuity partner for the registry gate: the same call, the same
    // registry, one instant outside the window.
    let mut registry = ConsolidatedSubscriptionRegistry::new(100);
    let sink = ChangeSinkSpy::default();

    let change = registry
        .subscribe(
            &sub("live-alpha", "AAPL"),
            &WindowAt::at(RESTART_NS - LEAD_NS - 1),
            &sink,
        )
        .expect("outside the window the registry must still open the line");

    assert_eq!(change, SubscriptionChange::Opened);
    assert_eq!(registry.distinct_subscriptions(), 1);
    assert_eq!(sink.events.borrow().len(), 1);
}

#[test]
fn a_refusal_after_the_window_is_labelled_an_outage_not_maintenance() {
    // The same refusal, two meanings. Inside the window "suspended" tells the
    // operator to wait because it is scheduled; after the window the identical
    // refusal is a genuine outage they are being paged about. Returning one
    // boolean from the gate would have collapsed the two and told the operator
    // to wait out an incident — which is why the port carries a reason.
    let manager = MarketDataSubscriptionManager;

    let maintenance = manager
        .request_subscription(
            sub("live-alpha", "AAPL"),
            &WindowAt::new(RESTART_NS + NANOS_PER_SECOND, false),
            &CountingCounter::default(),
            &ForbiddenLimitSink,
        )
        .expect_err("inside the window a dead gateway is planned maintenance");
    assert_eq!(maintenance.error_type, "ScheduledRestartWindow");
    assert!(maintenance.message.contains("planned maintenance"));

    let outage = manager
        .request_subscription(
            sub("live-alpha", "AAPL"),
            &WindowAt::new(RESTART_NS + 300 * NANOS_PER_SECOND, false),
            &CountingCounter::default(),
            &ForbiddenLimitSink,
        )
        .expect_err("after the window the same dead gateway is an outage");
    assert_eq!(
        outage.error_type, "IbGatewayUnreachable",
        "after the window the refusal must name the outage, not the window"
    );
    assert!(
        outage.message.contains("SYS-45"),
        "the escalated refusal must trace the connectivity requirement; got {}",
        outage.message
    );
    assert!(
        !outage.message.contains("planned maintenance"),
        "an outage must never be described as planned maintenance; got {}",
        outage.message
    );

    // Both use the same category, matching the live-order gate, so an operator
    // reads one event class and tells the two apart by error_type.
    assert_eq!(
        maintenance.category,
        OrderErrorCategory::ConnectivityBlocked
    );
    assert_eq!(outage.category, OrderErrorCategory::ConnectivityBlocked);
    assert_ne!(maintenance.error_type, outage.error_type);
}

#[test]
fn the_registry_tells_the_two_refusals_apart_too() {
    // The mutating admission point must make the same distinction, or the two
    // surfaces disagree about the same instant.
    let mut registry = ConsolidatedSubscriptionRegistry::new(100);
    let sink = ChangeSinkSpy::default();

    assert_eq!(
        registry
            .subscribe(
                &sub("live-alpha", "AAPL"),
                &WindowAt::new(RESTART_NS + NANOS_PER_SECOND, false),
                &sink,
            )
            .expect_err("inside the window"),
        SubscriptionRegistryError::SuspendedForScheduledRestart
    );
    assert_eq!(
        registry
            .subscribe(
                &sub("live-alpha", "AAPL"),
                &WindowAt::new(RESTART_NS + 300 * NANOS_PER_SECOND, false),
                &sink,
            )
            .expect_err("after the window"),
        SubscriptionRegistryError::ConnectivityLost
    );
    assert!(
        registry
            .subscribe(
                &sub("live-alpha", "AAPL"),
                &WindowAt::new(RESTART_NS + 300 * NANOS_PER_SECOND, false),
                &sink,
            )
            .unwrap_err()
            .to_string()
            .contains("SYS-45"),
        "the outage refusal must render the connectivity requirement"
    );
    assert_eq!(
        registry.distinct_subscriptions(),
        0,
        "neither refusal may register a line"
    );
}

#[test]
fn a_permanently_invalid_request_is_refused_on_its_own_terms_during_the_window() {
    // Precedence. The suspension tells the operator to retry once the window
    // closes; for an option, an empty symbol or an empty strategy id that is
    // false — those are refused in every phase — and answering "wait five
    // minutes" sends them to retry something that can never succeed.
    let mut registry = ConsolidatedSubscriptionRegistry::new(100);
    let sink = ChangeSinkSpy::default();
    let suspended = WindowAt::at(RESTART_NS - 30 * NANOS_PER_SECOND);

    let option = SubscriptionRequest {
        strategy_id: StrategyId::new("live-alpha"),
        symbol: "AAPL".to_string(),
        asset_class: AssetClass::Option,
    };
    assert_eq!(
        registry
            .subscribe(&option, &suspended, &sink)
            .expect_err("options fail closed"),
        SubscriptionRegistryError::OptionContractUnsupported,
        "an option is unsupported in every phase; the window must not relabel it"
    );

    let empty_symbol = SubscriptionRequest {
        strategy_id: StrategyId::new("live-alpha"),
        symbol: "   ".to_string(),
        asset_class: AssetClass::Equity,
    };
    assert_eq!(
        registry
            .subscribe(&empty_symbol, &suspended, &sink)
            .expect_err("empty symbol"),
        SubscriptionRegistryError::EmptySymbol
    );

    let empty_strategy = SubscriptionRequest {
        strategy_id: StrategyId::new("  "),
        symbol: "AAPL".to_string(),
        asset_class: AssetClass::Equity,
    };
    assert_eq!(
        registry
            .subscribe(&empty_strategy, &suspended, &sink)
            .expect_err("empty strategy id"),
        SubscriptionRegistryError::EmptyStrategyId
    );

    // Non-vacuity, and the property that must NOT regress: a request that WOULD
    // have been admitted is still suspended. Without this the fix could have
    // been "stop suspending anything".
    assert_eq!(
        registry
            .subscribe(&sub("live-alpha", "AAPL"), &suspended, &sink)
            .expect_err("a valid request is suspended"),
        SubscriptionRegistryError::SuspendedForScheduledRestart
    );
    assert_eq!(
        registry.distinct_subscriptions(),
        0,
        "no refusal may register a line"
    );
}

#[test]
fn srs_md_005_admission_follows_the_sys_75_phases_over_both_reachabilities() {
    // The full truth table, so a change to either the phase arithmetic or the
    // admission rule has to move both. Reachability is a dimension rather than
    // a constant because SYS-75 uses it differently in each phase, and reading
    // the four interesting rows together is what makes the requirement legible:
    //
    //   * the LEAD suspends whether or not the gateway answers — SYS-75(a) is
    //     pre-emptive, and a rule derived from reachability could never fire
    //     there, since the gateway is by definition still up;
    //   * inside the WINDOW a gateway that answers RESUMES — SYS-75(c)/(d);
    //   * after the window a dead gateway is no longer maintenance. Market data
    //     is still refused, but now because connectivity is genuinely lost
    //     (SYS-45), which is the escalation the operator gets paged for.
    let window_ns = 300 * NANOS_PER_SECOND;
    let manager = MarketDataSubscriptionManager;
    let cases = [
        (RESTART_NS - LEAD_NS - 1, true, true, "before the lead, up"),
        (
            RESTART_NS - LEAD_NS - 1,
            false,
            false,
            "before the lead, down",
        ),
        (
            RESTART_NS - LEAD_NS,
            true,
            false,
            "first instant of the lead, up",
        ),
        (
            RESTART_NS - LEAD_NS,
            false,
            false,
            "first instant of the lead, down",
        ),
        (RESTART_NS - 1, true, false, "last instant of the lead, up"),
        (RESTART_NS, false, false, "the restart instant, down"),
        (
            RESTART_NS + 1,
            true,
            true,
            "inside the window, back up (SYS-75(d))",
        ),
        (
            RESTART_NS + window_ns - 1,
            false,
            false,
            "last instant of the window, down",
        ),
        (
            RESTART_NS + window_ns,
            true,
            true,
            "first instant after the window, up",
        ),
        (
            RESTART_NS + window_ns,
            false,
            false,
            "first instant after the window, still down (escalation)",
        ),
    ];

    let mut admitted = 0;
    let mut refused = 0;
    for (now_ns, reachable, expect_admitted, label) in cases {
        let counter = CountingCounter::default();
        let outcome = manager.request_subscription(
            sub("live-alpha", "AAPL"),
            &WindowAt::new(now_ns, reachable),
            &counter,
            &ForbiddenLimitSink,
        );
        assert_eq!(
            outcome.is_ok(),
            expect_admitted,
            "SRS-MD-005: wrong admission at {label} (now_ns={now_ns}, reachable={reachable})"
        );
        if expect_admitted {
            admitted += 1;
        } else {
            refused += 1;
        }
    }

    // The table would still pass if every row expected the same answer, so pin
    // that it exercises both outcomes.
    assert!(
        admitted >= 3 && refused >= 3,
        "the phase table must cover both admission and refusal \
         (admitted={admitted}, refused={refused})"
    );
}
