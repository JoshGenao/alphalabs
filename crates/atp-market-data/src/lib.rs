use std::collections::BTreeMap;
use std::fmt;

use atp_types::{
    MarketDataTick, OrderErrorCategory, RuntimeService, SecurityKey, SecurityKeyError, StrategyId,
    StructuredSubscriptionError, SubscriptionChange, SubscriptionChangeEvent,
    SubscriptionLimitEvent, SubscriptionLimitState, SubscriptionRequest,
};

#[derive(Debug, Default)]
pub struct MarketDataSubscriptionManager;

// --------------------------------------------------------------------------- //
// Subscription manager ports (SRS-MD-002 / SyRS SYS-70 / SYS-64)
// --------------------------------------------------------------------------- //
//
// The subscription manager owns the IB line accounting; ERR-4's gate
// consults two ports:
//
//   * `SubscriptionLineCounter` — exposes the configured limit, the
//     current in-use count, and a `try_acquire` probe that the gate
//     consults before admitting a request. Concrete implementations
//     (deferred to SRS-MD-001 / SRS-MD-007) hold the actual subscription
//     set and the operator-configured `ATP_MARKET_DATA_LINE_LIMIT` value.
//     `try_acquire` is read-only with respect to the registry — admission
//     happens after the manager observes `WithinLimit`.
//
//   * `SubscriptionLimitEventSink` — the structured-event publication
//     channel. Concrete sinks (deferred) route events to logs
//     (SRS-LOG-001), the dashboard WebSocket alert pane, and the
//     notification dispatcher per SyRS SYS-70's "alert the operator on
//     the dashboard" clause.
//
// Both traits live in `atp-market-data` (not `atp-execution`) because
// the consumer — `MarketDataSubscriptionManager::request_subscription` —
// lives here. Placing them in `atp-execution` would invert the
// SRS-ARCH-002 dependency direction.
pub trait SubscriptionLineCounter {
    /// Number of IB market-data lines currently in use by the
    /// consolidated subscription set.
    fn lines_in_use(&self) -> u32;

    /// Operator-configured ceiling from `ATP_MARKET_DATA_LINE_LIMIT`.
    fn line_limit(&self) -> u32;

    /// Probe the limit without mutating the subscription registry.
    /// Returns `ExceededLimit` if admitting `request` would push the
    /// in-use count past the configured ceiling.
    fn try_acquire(&self, request: &SubscriptionRequest) -> SubscriptionLimitState;
}

pub trait SubscriptionLimitEventSink {
    fn record(&self, event: SubscriptionLimitEvent);
}

/// Happy-path admission envelope. Echoes back the request identity so the
/// caller can correlate the acceptance with the originating strategy.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubscriptionAccepted {
    pub strategy_id: StrategyId,
    pub symbol: String,
}

impl MarketDataSubscriptionManager {
    pub fn service(&self) -> RuntimeService {
        RuntimeService::MarketDataSubscriptionManager
    }

    pub fn owns_subscription_fanout(&self) -> bool {
        true
    }

    /// SRS-MD-002 / SyRS SYS-70 subscription-limit gate. Matches on the
    /// counter's `try_acquire` probe; `WithinLimit` returns
    /// `SubscriptionAccepted`; `ExceededLimit` emits a structured
    /// `SubscriptionLimitEvent` through the sink AND returns a
    /// `StructuredSubscriptionError` whose category is
    /// `OrderErrorCategory::SubscriptionLimitReached` (wire string
    /// `SUBSCRIPTION_LIMIT_REACHED`).
    ///
    /// **Invariants** (statically checked by
    /// `tools/subscription_limit_check.py`):
    ///
    /// * The `ExceededLimit` arm MUST call `events.record(`.
    /// * The `ExceededLimit` arm MUST produce
    ///   `OrderErrorCategory::SubscriptionLimitReached`.
    /// * The `ExceededLimit` arm MUST NOT mutate the subscription
    ///   registry (no `registry.insert(`, `subscriptions.insert(`, or
    ///   `request.acquire(` calls inside the rejection leaf). The
    ///   rejected request must leave the registry exactly as it found
    ///   it.
    /// * `WithinLimit` is the only call site of `SubscriptionAccepted {`.
    ///
    /// The gate takes no `StrategyMode` parameter: SyRS SYS-64 mandates
    /// an identical error contract for live and paper modes, and SyRS
    /// SYS-70 places the gate over the consolidated subscription set
    /// for all active strategies regardless of mode.
    pub fn request_subscription<C, S>(
        &self,
        request: SubscriptionRequest,
        counter: &C,
        events: &S,
    ) -> Result<SubscriptionAccepted, StructuredSubscriptionError>
    where
        C: SubscriptionLineCounter,
        S: SubscriptionLimitEventSink,
    {
        match counter.try_acquire(&request) {
            SubscriptionLimitState::WithinLimit => Ok(SubscriptionAccepted {
                strategy_id: request.strategy_id,
                symbol: request.symbol,
            }),
            SubscriptionLimitState::ExceededLimit => {
                let current_lines = counter.lines_in_use();
                let configured_limit = counter.line_limit();
                events.record(SubscriptionLimitEvent {
                    state: SubscriptionLimitState::ExceededLimit,
                    strategy_id: request.strategy_id.clone(),
                    symbol: request.symbol.clone(),
                    current_lines,
                    configured_limit,
                });
                Err(StructuredSubscriptionError::limit_reached(
                    request,
                    current_lines,
                    configured_limit,
                ))
            }
        }
    }
}

// Re-export to satisfy the static checker — references the
// `OrderErrorCategory` variant by name so a workspace-level dead-code
// scan cannot drop the link between the wire string and this crate.
#[doc(hidden)]
pub const _SUBSCRIPTION_LIMIT_CATEGORY: OrderErrorCategory =
    OrderErrorCategory::SubscriptionLimitReached;

// --------------------------------------------------------------------------- //
// Consolidated subscription registry + fan-out (SRS-MD-001 / SyRS SYS-70)
// --------------------------------------------------------------------------- //
//
// SRS-MD-001 is the consolidation + fan-out half of SYS-70 (the line-limit
// half is SRS-MD-002, above). The acceptance criterion: "Multiple strategies
// subscribing to the same security consume one IB subscription; each
// subscriber receives fan-out data ...". `ConsolidatedSubscriptionRegistry`
// owns the live subscription set and enforces the structural invariant; the
// <=100 ms fan-out latency NFR and the real IB feed are deferred runtime
// halves (see `architecture/runtime_services.json` ->
// `subscription_fanout_contract.deferred[]`).

/// Structured-event publication channel for consolidated-subscription
/// changes. Concrete sinks (deferred to the SRS-MD-001 runtime) route
/// `SubscriptionChangeEvent`s to SRS-LOG-001 (Source.MARKET_DATA,
/// event_type `subscription_change`), the dashboard subscription pane, and
/// any consumer that tracks live line usage. Mirrors
/// `SubscriptionLimitEventSink`: publication is a port so the registry
/// stays free of logging / transport concerns.
pub trait SubscriptionChangeSink {
    fn record(&self, event: SubscriptionChangeEvent);
}

/// Precondition / admission violations the consolidated registry rejects at
/// its public boundary. The registry is the seam between untrusted
/// strategy-supplied identifiers and the consolidated IB subscription set, so
/// it fails closed rather than registering a bad key, fanning a tick out
/// under an empty symbol, or opening a line past the configured ceiling.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SubscriptionRegistryError {
    /// A subscription / fan-out routing key was empty (or whitespace). An
    /// empty symbol can never name a tradable security and must not open an
    /// upstream IB line.
    EmptySymbol,
    /// A subscriber identifier was empty (or whitespace). Fan-out delivery
    /// requires a non-empty strategy identity.
    EmptyStrategyId,
    /// Opening a NEW upstream subscription would exceed the operator-
    /// configured IB market-data line limit. SRS-MD-001 enforces this in the
    /// same mutable borrow that performs the insert, so a rejected request
    /// never registers a line (no probe-then-mutate race window).
    LineLimitReached { configured_limit: u32 },
    /// The request named `AssetClass::Option`, whose full contract identity
    /// (underlying + expiration + strike + right) is not yet modeled — the
    /// manager fails closed on options (deferred to SRS-DATA-004 /
    /// SRS-EXE-004) rather than conflating distinct contracts on one
    /// underlying onto a single upstream line.
    OptionContractUnsupported,
}

impl From<SecurityKeyError> for SubscriptionRegistryError {
    fn from(error: SecurityKeyError) -> Self {
        match error {
            SecurityKeyError::EmptySymbol => Self::EmptySymbol,
            SecurityKeyError::OptionContractIdentityRequired => Self::OptionContractUnsupported,
        }
    }
}

impl fmt::Display for SubscriptionRegistryError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptySymbol => {
                formatter.write_str("SRS-MD-001: subscription symbol must be non-empty")
            }
            Self::EmptyStrategyId => {
                formatter.write_str("SRS-MD-001: subscriber strategy_id must be non-empty")
            }
            Self::LineLimitReached { configured_limit } => write!(
                formatter,
                "SRS-MD-001/SYS-70: a new upstream subscription would exceed the IB line limit ({configured_limit})"
            ),
            Self::OptionContractUnsupported => formatter.write_str(
                "SRS-MD-001: option subscriptions are not yet supported \
                 (deferred to SRS-DATA-004 / SRS-EXE-004)",
            ),
        }
    }
}

impl std::error::Error for SubscriptionRegistryError {}

/// SRS-MD-001 / SyRS SYS-70 consolidated market-data subscription registry.
///
/// Owns the live subscription set the subscription manager consolidates
/// across all active strategy containers (live and paper). Maintains the
/// SRS-MD-001 core invariant: **for any security with one or more
/// subscribers there is exactly ONE upstream IB market-data subscription**,
/// regardless of how many strategy containers subscribe. The consolidated
/// set is keyed on a canonical [`SecurityKey`] (normalized symbol +
/// asset class), so `AAPL` / `aapl` / ` AAPL ` share one line while an
/// equity and an option on the same display ticker stay distinct lines.
/// Received market data is fanned out to every subscriber of the tick's
/// security — and to no other subscriber.
///
/// Admission is ATOMIC: `subscribe` enforces the configured line ceiling in
/// the same `&mut self` borrow that performs the insert, so neither a direct
/// caller nor a probe-then-mutate race can push the consolidated set past the
/// IB line cap. The registry is also the concrete `SubscriptionLineCounter`
/// the SRS-MD-002 gate consumes: `lines_in_use()` returns the number of
/// DISTINCT upstream subscriptions and `try_acquire` is the dedup-aware
/// read-only probe the gate uses to produce its structured
/// `SUBSCRIPTION_LIMIT_REACHED` envelope before admission.
///
/// Deferred to the SRS-MD-001 runtime (see the architecture metadata
/// `subscription_fanout_contract.deferred[]`): the real IB upstream
/// `reqMktData` binding (SRS-EXE-006 adapter), live tick ingestion + async
/// fan-out transport, the <=100 ms fan-out latency NFR (SRS-PERF-001
/// measurement), and concurrency / locking. This struct is single-threaded
/// and models the structural dedup + fan-out + line-accounting contract.
#[derive(Debug, Default)]
pub struct ConsolidatedSubscriptionRegistry {
    // SecurityKey -> subscribers in subscription order, kept duplicate-free
    // by `subscribe`. BTreeMap gives deterministic key iteration for the line
    // accounting; the per-key Vec preserves fan-out order.
    subscribers: BTreeMap<SecurityKey, Vec<StrategyId>>,
    line_limit: u32,
}

impl ConsolidatedSubscriptionRegistry {
    /// Build a registry with the operator-configured IB line ceiling
    /// (`ATP_MARKET_DATA_LINE_LIMIT`, wired by SRS-ARCH-005 config — the
    /// concrete plumbing is deferred). `subscribe` enforces this ceiling
    /// atomically; the SRS-MD-002 gate additionally probes it via
    /// `try_acquire` to produce the operator-facing structured error.
    pub fn new(line_limit: u32) -> Self {
        Self {
            subscribers: BTreeMap::new(),
            line_limit,
        }
    }

    /// Number of DISTINCT upstream IB subscriptions currently held — one
    /// line per security with at least one subscriber. This IS the
    /// SRS-MD-001 consolidation evidence and the count the SRS-MD-002 limit
    /// gate reads through `lines_in_use`.
    pub fn distinct_subscriptions(&self) -> u32 {
        self.subscribers.len() as u32
    }

    /// Number of subscribers fanned out to for `key` (0 if none).
    pub fn subscriber_count(&self, key: &SecurityKey) -> u32 {
        self.subscribers.get(key).map_or(0, |s| s.len() as u32)
    }

    /// True when `strategy_id` is a registered subscriber of `key`.
    pub fn is_subscribed(&self, strategy_id: &StrategyId, key: &SecurityKey) -> bool {
        self.subscribers
            .get(key)
            .is_some_and(|s| s.contains(strategy_id))
    }

    /// Register `request.strategy_id` as a subscriber of the canonical
    /// security named by `request`, returning the `SubscriptionChange`
    /// describing the effect. Every line-affecting / dedup transition (i.e.
    /// everything but the idempotent `AlreadySubscribed` no-op) is published
    /// as a `SubscriptionChangeEvent` through `events`.
    ///
    /// SRS-MD-001 dedup invariant: a second (and subsequent) subscriber to
    /// the same security does NOT open a new upstream subscription — the
    /// return is `SubscriberAdded` and `distinct_subscriptions()` is
    /// unchanged. Only the FIRST subscriber returns `Opened` and adds a line,
    /// and only when the configured ceiling has headroom — otherwise
    /// `subscribe` returns `LineLimitReached` WITHOUT registering anything.
    pub fn subscribe<S: SubscriptionChangeSink>(
        &mut self,
        request: &SubscriptionRequest,
        events: &S,
    ) -> Result<SubscriptionChange, SubscriptionRegistryError> {
        let key = request.security_key()?;
        Self::validate_strategy_id(&request.strategy_id)?;

        let change = if let Some(existing) = self.subscribers.get_mut(&key) {
            if existing.contains(&request.strategy_id) {
                SubscriptionChange::AlreadySubscribed
            } else {
                // Dedup: additional subscriber, SAME upstream line.
                existing.push(request.strategy_id.clone());
                SubscriptionChange::SubscriberAdded
            }
        } else {
            // First subscriber for this security → one NEW upstream line.
            // Enforce the configured ceiling ATOMICALLY in the same &mut
            // borrow that performs the insert: a new line past the limit is
            // refused here, so no caller — and no probe-then-mutate race —
            // can push the consolidated set past the IB line cap.
            if self.subscribers.len() as u32 >= self.line_limit {
                return Err(SubscriptionRegistryError::LineLimitReached {
                    configured_limit: self.line_limit,
                });
            }
            self.subscribers
                .insert(key.clone(), vec![request.strategy_id.clone()]);
            SubscriptionChange::Opened
        };
        self.publish(change, &request.strategy_id, &key, events);
        Ok(change)
    }

    /// Remove `strategy_id` from `key`'s subscriber set, returning the
    /// `SubscriptionChange`. When the LAST subscriber leaves, the upstream
    /// subscription is released (`Closed`, `distinct_subscriptions()`
    /// decremented). Publishes every transition but the `NotSubscribed`
    /// no-op.
    pub fn unsubscribe<S: SubscriptionChangeSink>(
        &mut self,
        strategy_id: &StrategyId,
        key: &SecurityKey,
        events: &S,
    ) -> Result<SubscriptionChange, SubscriptionRegistryError> {
        Self::validate_strategy_id(strategy_id)?;

        let change = match self.subscribers.get_mut(key) {
            None => SubscriptionChange::NotSubscribed,
            Some(existing) => {
                let before = existing.len();
                existing.retain(|s| s != strategy_id);
                if existing.len() == before {
                    SubscriptionChange::NotSubscribed
                } else if existing.is_empty() {
                    // Last subscriber left → release the upstream line.
                    self.subscribers.remove(key);
                    SubscriptionChange::Closed
                } else {
                    SubscriptionChange::SubscriberRemoved
                }
            }
        };
        self.publish(change, strategy_id, key, events);
        Ok(change)
    }

    /// Fan a received tick out to every subscriber of its security, in
    /// subscription order. Returns the recipient list (empty when no strategy
    /// subscribes to the tick's security). SRS-MD-001 isolation invariant: a
    /// subscriber of one security NEVER receives a tick for another — the
    /// routing key is the tick's canonical `SecurityKey`, so a tick whose
    /// symbol normalizes differently or whose asset class differs reaches
    /// only the matching subscribers.
    pub fn fan_out(
        &self,
        tick: &MarketDataTick,
    ) -> Result<Vec<StrategyId>, SubscriptionRegistryError> {
        let key = tick.security_key()?;
        Ok(self.subscribers.get(&key).cloned().unwrap_or_default())
    }

    fn publish<S: SubscriptionChangeSink>(
        &self,
        change: SubscriptionChange,
        strategy_id: &StrategyId,
        key: &SecurityKey,
        events: &S,
    ) {
        if !change.is_published() {
            return;
        }
        events.record(SubscriptionChangeEvent {
            change,
            strategy_id: strategy_id.clone(),
            symbol: key.symbol().to_string(),
            asset_class: key.asset_class(),
            subscriber_count: self.subscriber_count(key),
            lines_in_use: self.distinct_subscriptions(),
        });
    }

    fn validate_strategy_id(strategy_id: &StrategyId) -> Result<(), SubscriptionRegistryError> {
        if strategy_id.as_str().trim().is_empty() {
            return Err(SubscriptionRegistryError::EmptyStrategyId);
        }
        Ok(())
    }
}

/// The consolidated registry IS the concrete line counter the SRS-MD-002
/// limit gate consumes — this impl closes the
/// `subscription_limit_contract.deferred[]` item "Concrete
/// SubscriptionLineCounter impl backed by ... the live subscription set
/// (owner: SRS-MD-001 / SRS-MD-007)". The methods are read-only with
/// respect to the registry: the gate probes here to build its structured
/// `SUBSCRIPTION_LIMIT_REACHED` envelope, while `subscribe` independently
/// enforces the same ceiling atomically at insert time.
impl SubscriptionLineCounter for ConsolidatedSubscriptionRegistry {
    fn lines_in_use(&self) -> u32 {
        self.distinct_subscriptions()
    }

    fn line_limit(&self) -> u32 {
        self.line_limit
    }

    fn try_acquire(&self, request: &SubscriptionRequest) -> SubscriptionLimitState {
        // A request that cannot be canonicalized — an empty symbol, or an
        // option whose full contract identity is not yet modeled — is NEVER
        // admissible. Fail closed so the SRS-MD-002 gate rejects it rather
        // than reporting capacity headroom for a request the registry's
        // `subscribe` would refuse (`OptionContractUnsupported`). The gate
        // maps this to SUBSCRIPTION_LIMIT_REACHED; the precise option error
        // surfaces at `subscribe`, and a dedicated gate-level validation
        // stage is deferred with the runtime.
        let Ok(key) = request.security_key() else {
            return SubscriptionLimitState::ExceededLimit;
        };
        // Dedup-aware probe: an already-subscribed security consumes no new
        // line, so admitting it is unconditionally within limit. A new
        // security would consume one line — within limit only while the
        // current distinct count is below the configured ceiling.
        if self.subscribers.contains_key(&key) {
            return SubscriptionLimitState::WithinLimit;
        }
        if self.distinct_subscriptions() < self.line_limit {
            SubscriptionLimitState::WithinLimit
        } else {
            SubscriptionLimitState::ExceededLimit
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use atp_types::AssetClass;
    use std::cell::{Cell, RefCell};

    #[test]
    fn identifies_market_data_subscription_manager() {
        let manager = MarketDataSubscriptionManager;
        assert_eq!(
            manager.service(),
            RuntimeService::MarketDataSubscriptionManager
        );
        assert!(manager.owns_subscription_fanout());
    }

    struct StubCounter {
        state: SubscriptionLimitState,
        current: u32,
        limit: u32,
    }

    impl SubscriptionLineCounter for StubCounter {
        fn lines_in_use(&self) -> u32 {
            self.current
        }
        fn line_limit(&self) -> u32 {
            self.limit
        }
        fn try_acquire(&self, _request: &SubscriptionRequest) -> SubscriptionLimitState {
            self.state
        }
    }

    #[derive(Default)]
    struct StubSink {
        events: RefCell<Vec<SubscriptionLimitEvent>>,
    }

    impl SubscriptionLimitEventSink for StubSink {
        fn record(&self, event: SubscriptionLimitEvent) {
            self.events.borrow_mut().push(event);
        }
    }

    #[test]
    fn within_limit_state_returns_accepted_and_emits_no_event() {
        let manager = MarketDataSubscriptionManager;
        let counter = StubCounter {
            state: SubscriptionLimitState::WithinLimit,
            current: 50,
            limit: 100,
        };
        let sink = StubSink::default();
        let request = SubscriptionRequest {
            strategy_id: StrategyId::new("paper-alpha-1"),
            symbol: "AAPL".to_string(),
            asset_class: AssetClass::Equity,
        };

        let accepted = manager
            .request_subscription(request, &counter, &sink)
            .expect("WithinLimit must accept the request");
        assert_eq!(accepted.strategy_id.as_str(), "paper-alpha-1");
        assert_eq!(accepted.symbol, "AAPL");
        assert!(
            sink.events.borrow().is_empty(),
            "WithinLimit must not emit a SubscriptionLimitEvent"
        );
    }

    #[test]
    fn exceeded_limit_state_rejects_with_subscription_limit_reached() {
        let manager = MarketDataSubscriptionManager;
        let counter = StubCounter {
            state: SubscriptionLimitState::ExceededLimit,
            current: 100,
            limit: 100,
        };
        let sink = StubSink::default();
        let request = SubscriptionRequest {
            strategy_id: StrategyId::new("live-alpha"),
            symbol: "AAPL".to_string(),
            asset_class: AssetClass::Equity,
        };

        let error = manager
            .request_subscription(request.clone(), &counter, &sink)
            .expect_err("ExceededLimit must reject the request");
        assert_eq!(
            error.category,
            OrderErrorCategory::SubscriptionLimitReached
        );
        assert_eq!(error.category.as_str(), "SUBSCRIPTION_LIMIT_REACHED");
        assert_eq!(error.original_request, request);
        let events = sink.events.borrow();
        assert_eq!(events.len(), 1, "exactly one event per rejected request");
        assert_eq!(events[0].state, SubscriptionLimitState::ExceededLimit);
        assert_eq!(events[0].current_lines, 100);
        assert_eq!(events[0].configured_limit, 100);
    }

    #[test]
    fn exceeded_limit_state_does_not_consult_counter_twice() {
        // Sanity check: the gate must consult `try_acquire` exactly once.
        // A future refactor that probes the counter inside both arms
        // would silently degrade dashboard accuracy (and double-count
        // events). Wrap the StubCounter in a call-counter and assert.
        struct CountingCounter {
            inner: StubCounter,
            try_acquire_calls: Cell<u32>,
        }
        impl SubscriptionLineCounter for CountingCounter {
            fn lines_in_use(&self) -> u32 {
                self.inner.current
            }
            fn line_limit(&self) -> u32 {
                self.inner.limit
            }
            fn try_acquire(&self, request: &SubscriptionRequest) -> SubscriptionLimitState {
                self.try_acquire_calls
                    .set(self.try_acquire_calls.get() + 1);
                self.inner.try_acquire(request)
            }
        }

        let manager = MarketDataSubscriptionManager;
        let counter = CountingCounter {
            inner: StubCounter {
                state: SubscriptionLimitState::ExceededLimit,
                current: 200,
                limit: 100,
            },
            try_acquire_calls: Cell::new(0),
        };
        let sink = StubSink::default();
        let request = SubscriptionRequest {
            strategy_id: StrategyId::new("live-alpha"),
            symbol: "MSFT".to_string(),
            asset_class: AssetClass::Equity,
        };
        let _ = manager.request_subscription(request, &counter, &sink);
        assert_eq!(
            counter.try_acquire_calls.get(),
            1,
            "the gate must probe try_acquire exactly once per request"
        );
    }

    // ----------------------------------------------------------------- //
    // SRS-MD-001 consolidated registry + fan-out
    // ----------------------------------------------------------------- //

    #[derive(Default)]
    struct ChangeSinkSpy {
        events: RefCell<Vec<SubscriptionChangeEvent>>,
    }

    impl SubscriptionChangeSink for ChangeSinkSpy {
        fn record(&self, event: SubscriptionChangeEvent) {
            self.events.borrow_mut().push(event);
        }
    }

    /// Sink that panics if consulted — proves an idempotent no-op
    /// publishes nothing.
    struct ForbiddenChangeSink;

    impl SubscriptionChangeSink for ForbiddenChangeSink {
        fn record(&self, _event: SubscriptionChangeEvent) {
            panic!("SRS-MD-001: idempotent no-op must not publish a SubscriptionChangeEvent");
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

    fn tick(symbol: &str, tick_seq: u64) -> MarketDataTick {
        MarketDataTick {
            symbol: symbol.to_string(),
            asset_class: AssetClass::Equity,
            tick_seq,
        }
    }

    #[test]
    fn duplicate_subscriptions_consume_one_upstream_line() {
        // SRS-MD-001 core AC: three strategies subscribing to AAPL consume
        // exactly ONE upstream IB subscription.
        let mut registry = ConsolidatedSubscriptionRegistry::new(100);
        let sink = ChangeSinkSpy::default();

        assert_eq!(
            registry.subscribe(&sub("live-a", "AAPL"), &sink).unwrap(),
            SubscriptionChange::Opened
        );
        assert_eq!(
            registry.subscribe(&sub("paper-b", "AAPL"), &sink).unwrap(),
            SubscriptionChange::SubscriberAdded
        );
        assert_eq!(
            registry.subscribe(&sub("paper-c", "AAPL"), &sink).unwrap(),
            SubscriptionChange::SubscriberAdded
        );

        assert_eq!(
            registry.distinct_subscriptions(),
            1,
            "three subscribers must still consume one upstream line"
        );
        assert_eq!(registry.subscriber_count(&eq_key("AAPL")), 3);
    }

    #[test]
    fn idempotent_resubscribe_is_a_silent_noop() {
        let mut registry = ConsolidatedSubscriptionRegistry::new(100);
        registry
            .subscribe(&sub("live-a", "AAPL"), &ChangeSinkSpy::default())
            .unwrap();
        // The second identical subscribe must NOT publish (ForbiddenSink
        // panics if it does) and must not double-count.
        assert_eq!(
            registry
                .subscribe(&sub("live-a", "AAPL"), &ForbiddenChangeSink)
                .unwrap(),
            SubscriptionChange::AlreadySubscribed
        );
        assert_eq!(registry.subscriber_count(&eq_key("AAPL")), 1);
        assert_eq!(registry.distinct_subscriptions(), 1);
    }

    #[test]
    fn fan_out_routes_only_to_symbol_subscribers() {
        // SRS-MD-001 isolation invariant: an AAPL tick reaches AAPL
        // subscribers only — never the MSFT subscriber.
        let mut registry = ConsolidatedSubscriptionRegistry::new(100);
        let sink = ChangeSinkSpy::default();
        registry.subscribe(&sub("live-a", "AAPL"), &sink).unwrap();
        registry.subscribe(&sub("paper-b", "AAPL"), &sink).unwrap();
        registry.subscribe(&sub("paper-c", "MSFT"), &sink).unwrap();

        let recipients = registry.fan_out(&tick("AAPL", 1)).unwrap();
        let ids: Vec<&str> = recipients.iter().map(StrategyId::as_str).collect();
        assert_eq!(ids, vec!["live-a", "paper-b"]);
        assert!(
            !ids.contains(&"paper-c"),
            "the MSFT subscriber must not receive an AAPL tick"
        );
    }

    #[test]
    fn fan_out_to_unsubscribed_symbol_reaches_no_one() {
        let registry = ConsolidatedSubscriptionRegistry::new(100);
        let recipients = registry.fan_out(&tick("NFLX", 9)).unwrap();
        assert!(recipients.is_empty());
    }

    #[test]
    fn last_unsubscribe_releases_the_upstream_line() {
        let mut registry = ConsolidatedSubscriptionRegistry::new(100);
        let sink = ChangeSinkSpy::default();
        registry.subscribe(&sub("live-a", "AAPL"), &sink).unwrap();
        registry.subscribe(&sub("paper-b", "AAPL"), &sink).unwrap();

        assert_eq!(
            registry
                .unsubscribe(&StrategyId::new("live-a"), &eq_key("AAPL"), &sink)
                .unwrap(),
            SubscriptionChange::SubscriberRemoved
        );
        assert_eq!(registry.distinct_subscriptions(), 1, "line still held");

        assert_eq!(
            registry
                .unsubscribe(&StrategyId::new("paper-b"), &eq_key("AAPL"), &sink)
                .unwrap(),
            SubscriptionChange::Closed
        );
        assert_eq!(
            registry.distinct_subscriptions(),
            0,
            "last subscriber leaving must release the upstream line"
        );
    }

    #[test]
    fn unsubscribe_unknown_is_a_silent_noop() {
        let mut registry = ConsolidatedSubscriptionRegistry::new(100);
        assert_eq!(
            registry
                .unsubscribe(
                    &StrategyId::new("ghost"),
                    &eq_key("AAPL"),
                    &ForbiddenChangeSink,
                )
                .unwrap(),
            SubscriptionChange::NotSubscribed
        );
    }

    #[test]
    fn change_event_carries_post_transition_counts() {
        let mut registry = ConsolidatedSubscriptionRegistry::new(100);
        let sink = ChangeSinkSpy::default();
        registry.subscribe(&sub("live-a", "AAPL"), &sink).unwrap();
        registry.subscribe(&sub("paper-b", "AAPL"), &sink).unwrap();

        let events = sink.events.borrow();
        assert_eq!(events.len(), 2);
        assert_eq!(events[0].change, SubscriptionChange::Opened);
        assert_eq!(events[0].subscriber_count, 1);
        assert_eq!(events[0].lines_in_use, 1);
        assert_eq!(events[1].change, SubscriptionChange::SubscriberAdded);
        assert_eq!(events[1].subscriber_count, 2);
        // Dedup: second subscriber, still one upstream line.
        assert_eq!(events[1].lines_in_use, 1);
    }

    #[test]
    fn empty_symbol_and_strategy_id_are_rejected() {
        let mut registry = ConsolidatedSubscriptionRegistry::new(100);
        let sink = ChangeSinkSpy::default();
        assert_eq!(
            registry.subscribe(&sub("live-a", "   "), &sink),
            Err(SubscriptionRegistryError::EmptySymbol)
        );
        assert_eq!(
            registry.subscribe(&sub("", "AAPL"), &sink),
            Err(SubscriptionRegistryError::EmptyStrategyId)
        );
        assert_eq!(
            registry.fan_out(&MarketDataTick {
                symbol: String::new(),
                asset_class: AssetClass::Equity,
                tick_seq: 1,
            }),
            Err(SubscriptionRegistryError::EmptySymbol)
        );
        // A rejected subscribe must not register anything.
        assert_eq!(registry.distinct_subscriptions(), 0);
        assert!(sink.events.borrow().is_empty());
    }

    #[test]
    fn registry_is_the_concrete_line_counter_for_the_md_002_gate() {
        // The dedup registry plugs straight into the SRS-MD-002 gate as
        // its SubscriptionLineCounter — a NEW symbol at the ceiling is
        // rejected, but a DUPLICATE of an existing symbol is admitted
        // (it consumes no new line). This is the cross-feature seam
        // SRS-MD-002 deferred to SRS-MD-001.
        let mut registry = ConsolidatedSubscriptionRegistry::new(1);
        let sink = ChangeSinkSpy::default();
        registry.subscribe(&sub("live-a", "AAPL"), &sink).unwrap();

        // Duplicate of the already-subscribed security → within limit.
        assert_eq!(
            registry.try_acquire(&sub("paper-b", "AAPL")),
            SubscriptionLimitState::WithinLimit
        );
        // A different security would need a 2nd line against a limit of 1.
        assert_eq!(
            registry.try_acquire(&sub("paper-b", "MSFT")),
            SubscriptionLimitState::ExceededLimit
        );

        let manager = MarketDataSubscriptionManager;
        let limit_sink = StubSink::default();
        // Routed through the real gate: the duplicate is accepted ...
        let dup = manager.request_subscription(sub("paper-b", "AAPL"), &registry, &limit_sink);
        assert!(
            dup.is_ok(),
            "a duplicate subscription is admitted (no new line)"
        );
        // ... and the new symbol is rejected with SUBSCRIPTION_LIMIT_REACHED.
        let err = manager
            .request_subscription(sub("paper-b", "MSFT"), &registry, &limit_sink)
            .expect_err("a new symbol at the ceiling must be rejected");
        assert_eq!(err.category, OrderErrorCategory::SubscriptionLimitReached);
        assert_eq!(registry.lines_in_use(), 1);
        assert_eq!(registry.line_limit(), 1);
    }

    // --- Codex adversarial-review follow-up: TDD tests (RED first) --- //

    #[test]
    fn case_and_whitespace_variants_dedup_onto_one_line() {
        // SRS-MD-001: "AAPL", "  aapl ", "Aapl" name the SAME security and
        // must consolidate onto ONE upstream line. A raw-string key would
        // open three lines and silently drop fan-out for the variants.
        let mut registry = ConsolidatedSubscriptionRegistry::new(100);
        let sink = ChangeSinkSpy::default();
        registry.subscribe(&sub("live-a", "AAPL"), &sink).unwrap();
        assert_eq!(
            registry
                .subscribe(&sub("paper-b", "  aapl "), &sink)
                .unwrap(),
            SubscriptionChange::SubscriberAdded,
            "a case/whitespace variant must dedup onto the existing line"
        );
        assert_eq!(
            registry.distinct_subscriptions(),
            1,
            "case/whitespace variants must not open extra upstream lines"
        );
    }

    #[test]
    fn subscribe_enforces_line_limit_atomically() {
        // SRS-MD-001 / SyRS SYS-70: the mutating admission path must itself
        // refuse to open a new upstream line past the configured ceiling —
        // not rely on a separate read-only probe the caller might skip or
        // race against.
        let mut registry = ConsolidatedSubscriptionRegistry::new(1);
        let sink = ChangeSinkSpy::default();
        registry.subscribe(&sub("live-a", "AAPL"), &sink).unwrap();
        let result = registry.subscribe(&sub("paper-b", "MSFT"), &sink);
        assert!(
            result.is_err(),
            "a NEW security past the line limit must be rejected by subscribe itself"
        );
        assert_eq!(
            registry.distinct_subscriptions(),
            1,
            "a rejected over-limit subscribe must not register a line"
        );
    }

    #[test]
    fn option_request_is_rejected_at_the_md_002_gate() {
        // Codex round-3: an uncanonicalizable option must NOT be admitted by
        // the SRS-MD-002 gate even with ample capacity. try_acquire fails
        // closed so request_subscription rejects it instead of returning
        // SubscriptionAccepted (a request the registry's subscribe would
        // itself refuse).
        let registry = ConsolidatedSubscriptionRegistry::new(100);
        let manager = MarketDataSubscriptionManager;
        let limit_sink = StubSink::default();
        let option = SubscriptionRequest {
            strategy_id: StrategyId::new("opt-strat"),
            symbol: "AAPL".to_string(),
            asset_class: AssetClass::Option,
        };
        // Below capacity, yet the probe fails closed — no false acceptance.
        assert_eq!(
            registry.try_acquire(&option),
            SubscriptionLimitState::ExceededLimit
        );
        assert!(
            manager
                .request_subscription(option, &registry, &limit_sink)
                .is_err(),
            "the gate must not admit an unsupported option even with capacity headroom"
        );
        // And the registry's own admission path rejects it precisely.
        assert_eq!(
            registry.try_acquire(&sub("equity-strat", "AAPL")),
            SubscriptionLimitState::WithinLimit,
            "a canonicalizable equity is still admissible with capacity"
        );
    }

    #[test]
    fn interleaved_probe_then_subscribe_cannot_exceed_the_limit() {
        // The probe-then-mutate window Codex flagged: a caller probes a
        // symbol while there is headroom, another subscription fills the
        // last line, and the first caller then subscribes. Because subscribe
        // re-checks the ceiling atomically at insert time, the stale probe
        // cannot push the set past the cap.
        let mut registry = ConsolidatedSubscriptionRegistry::new(1);
        let sink = ChangeSinkSpy::default();
        // Probe MSFT while the registry is empty → WithinLimit (stale).
        assert_eq!(
            registry.try_acquire(&sub("paper-b", "MSFT")),
            SubscriptionLimitState::WithinLimit
        );
        // Another request consumes the only line.
        registry.subscribe(&sub("live-a", "AAPL"), &sink).unwrap();
        // The stale-probed subscribe must now be refused atomically.
        assert_eq!(
            registry.subscribe(&sub("paper-b", "MSFT"), &sink),
            Err(SubscriptionRegistryError::LineLimitReached {
                configured_limit: 1
            })
        );
        assert_eq!(registry.distinct_subscriptions(), 1);
    }

    #[test]
    fn option_subscriptions_fail_closed() {
        // SRS-MD-001 fail-closed: a real option contract is identified by
        // underlying + expiration + strike + right, which the platform does
        // not yet model (deferred to SRS-DATA-004 / SRS-EXE-004). Keying an
        // option by its underlying symbol alone would conflate distinct
        // contracts onto one line, so the manager REJECTS option
        // subscriptions and fan-out instead of silently consolidating them.
        // The equity path on the same ticker is unaffected.
        let mut registry = ConsolidatedSubscriptionRegistry::new(100);
        let sink = ChangeSinkSpy::default();
        let option = SubscriptionRequest {
            strategy_id: StrategyId::new("option-strat"),
            symbol: "AAPL".to_string(),
            asset_class: AssetClass::Option,
        };
        assert_eq!(
            registry.subscribe(&option, &sink),
            Err(SubscriptionRegistryError::OptionContractUnsupported)
        );
        let option_tick = MarketDataTick {
            symbol: "AAPL".to_string(),
            asset_class: AssetClass::Option,
            tick_seq: 1,
        };
        assert_eq!(
            registry.fan_out(&option_tick),
            Err(SubscriptionRegistryError::OptionContractUnsupported)
        );
        // Nothing registered; the equity AAPL line is independent and works.
        assert_eq!(registry.distinct_subscriptions(), 0);
        registry
            .subscribe(&sub("equity-strat", "AAPL"), &sink)
            .unwrap();
        assert_eq!(registry.distinct_subscriptions(), 1);
        assert!(sink.events.borrow()[0].change.changes_line_count());
        assert_eq!(sink.events.borrow()[0].asset_class, AssetClass::Equity);
    }
}
