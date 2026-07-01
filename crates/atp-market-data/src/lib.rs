use std::collections::BTreeMap;
use std::fmt;

use atp_types::{
    MarketDataFreshness, MarketDataTick, OrderErrorCategory, RuntimeService, SecurityKey,
    SecurityKeyError, SequenceGapEvent, StrategyId, StructuredSubscriptionError,
    SubscriptionChange, SubscriptionChangeEvent, SubscriptionLimitEvent, SubscriptionLimitState,
    SubscriptionRequest,
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
    /// The request named `AssetClass::Option`. Its full contract identity
    /// (underlying + expiration + strike + right) is now modeled by SRS-EXE-004
    /// (`atp_types::OptionContractIdentity`, serialized), but the subscription
    /// `SecurityKey` does not yet CARRY it, so the manager fails closed on
    /// options (keying the identity into the line is SRS-MD-001 / SRS-DATA-004's
    /// follow-up) rather than conflating distinct contracts on one underlying
    /// onto a single upstream line.
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

// --------------------------------------------------------------------------- //
// Tick-sequence gap detection + per-security staleness (SRS-MD-007)
// (SyRS SYS-39 / SYS-39a / SYS-70, NFR-P5; StRS SN-2.03 / SN-2.04)
// --------------------------------------------------------------------------- //
//
// SRS-MD-007: "The market-data subscription manager shall detect sequence gaps
// in IB tick streams and reflect gap state in heartbeat/staleness." The
// consolidated registry above owns dedup + fan-out + line accounting;
// `SequenceGapDetector` owns the ORTHOGONAL per-security sequence/staleness
// state. Both key on the canonical [`SecurityKey`] so a runtime that fans a
// tick out (registry) also feeds it to the detector (staleness) with one key.
//
// PRODUCER CONTRACT: the detector finds a gap as a SKIP in `MarketDataTick
// .tick_seq`, so gap detection is only meaningful if `tick_seq` is the UPSTREAM
// PROVIDER sequence — the value the ingestion adapter reads from IB's own feed,
// where a dropped upstream tick leaves a hole — NOT a counter re-numbered per
// delivered callback (which would be gap-free by construction and silently
// defeat detection). Populating `tick_seq` from the provider origin sequence is
// the ingestion adapter's obligation (deferred SRS-EXE-006 / feed loop; see
// `sequence_gap_contract`). SRS-MD-001 deliberately deferred `tick_seq`'s gap
// semantics to SRS-MD-007, which defines them (on `MarketDataTick::tick_seq`).
//
// The detector closes the loop the `freshness_contract` deferred to
// SRS-MD-001 / SRS-MD-007: it produces the [`MarketDataFreshness`] a security's
// consolidated line is in, in the SAME `atp-types` vocabulary the SRS-MD-004
// execution gate (`ExecutionEngine::submit_live_order`, via the
// `MarketDataFreshnessProbe` port) rejects `MARKET_DATA_STALE` on.
//
// The runtime adapter that implements `MarketDataFreshnessProbe` from this
// detector is the deferred orchestrator-layer half (atp-execution must not
// depend on atp-market-data per SRS-ARCH-002). Note a real seam gap it must
// close: the CURRENT `MarketDataFreshnessProbe` is SYMBOL-ONLY
// (`freshness(&self, symbol: &str)`), while the detector is keyed on the full
// [`SecurityKey`] (symbol + asset class). Faithfully bridging the two requires
// the port to become security-aware — carry the `asset_class` the
// `OrderSubmission` already holds, or a `SecurityKey` — which is a deferred
// change on the SRS-MD-004 / ERR-3 port surface (see
// `runtime_services.json` `sequence_gap_contract.deferred[]`). Until then an
// equity and an option sharing a display ticker cannot be distinguished by the
// symbol-only probe; the detector fails closed on options (they are never
// tracked, so `freshness` returns `Stale`) and options are rejected upstream,
// so equities — the only currently-tradable class — are unaffected.

/// Why a concrete [`SequenceGapEventSink`] failed to publish a gap event — a
/// durable SRS-LOG-001 write error, a dashboard-transport failure, etc. Opaque
/// to the detector: the detector does not interpret or retry it, it only
/// surfaces the failure to the caller (on [`GapObservation::Gap::published`])
/// so the runtime can alert on lost audit evidence. The `reason` is a
/// human-readable message the concrete sink supplies.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SequenceGapPublishError {
    pub reason: String,
}

impl SequenceGapPublishError {
    pub fn new(reason: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
        }
    }
}

impl fmt::Display for SequenceGapPublishError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "SRS-MD-007: failed to publish sequence-gap event: {}",
            self.reason
        )
    }
}

impl std::error::Error for SequenceGapPublishError {}

/// Structured-event publication channel for SRS-MD-007 sequence-gap events.
/// Concrete sinks (deferred to the SRS-LOG-001 runtime) route
/// `SequenceGapEvent`s to the persistent system log (Source.MARKET_DATA,
/// event_type `SEQUENCE_GAP`), the dashboard staleness / heartbeat pane, and
/// the notification dispatcher.
///
/// `record` is FALLIBLE. SRS-MD-007 makes logging a first-class acceptance
/// criterion ("gap events ARE logged via SRS-LOG-001 ... and are visible on the
/// dashboard"), so a durable-write / transport failure MUST be surfaceable
/// rather than silently swallowed — the runtime needs to fail closed and alert
/// on lost audit evidence. The failure is reported to the caller on
/// [`GapObservation::Gap`]'s `published` field, NOT by aborting the observation:
/// a gap that could not be logged is still a detected gap, and the line is
/// still marked stale.
///
/// Crucially, the SAFETY outcome does NOT depend on this publication:
/// [`SequenceGapDetector::observe_tick`] commits the `Stale` state (which is
/// what blocks SRS-MD-004 order submission) BEFORE it calls `record`, so a sink
/// that drops, fails, or errors still leaves the gapped line stale and
/// tradeless — the failure mode is fail-CLOSED, never a silently-tradable gap.
/// DURABLE, retryable, fail-closed persistence of the audit record (the
/// `atp_logging.persistence` store already fsyncs and fails closed) plus the
/// operator page on a persistence failure are the concrete SRS-LOG-001 /
/// SRS-NOTIF-001 sink's job; this port's contract is only to let those failures
/// propagate.
pub trait SequenceGapEventSink {
    fn record(&self, event: SequenceGapEvent) -> Result<(), SequenceGapPublishError>;
}

/// Classification of what observing one tick did to a security's sequence
/// stream. Returned by [`SequenceGapDetector::observe_tick`] so a caller can
/// react (log, alert, unblock) without re-deriving the transition.
///
/// Not `Copy`: the `Gap` variant carries the sink's publish `Result`, whose
/// error is not `Copy`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GapObservation {
    /// First tick after start OR an operator resync — the sequence baseline is
    /// established at the observed value. No gap is possible on a baseline
    /// (there is no prior sequence to compare against), so the line is Fresh.
    Baseline,
    /// Monotonic in-sequence tick (`observed == last + 1`). `recovered` is
    /// true iff this tick satisfied the SRS-MD-007 recovery condition — the
    /// line was gap-stale and a fresh tick with a monotonic sequence cleared
    /// it back to Fresh.
    InSequence { recovered: bool },
    /// A forward skip (`observed > last + 1`): one or more ticks are missing.
    /// The line enters (or stays) stale — always, BEFORE publication, so the
    /// SRS-MD-004 order-block is fail-closed regardless of the sink — and a
    /// `SequenceGapEvent` was handed to the sink. `published` carries the
    /// sink's result: `Ok(())` on success, `Err(..)` if the audit event could
    /// not be logged / surfaced (the line is still stale; the runtime should
    /// alert on the lost audit evidence).
    Gap {
        expected: u64,
        observed: u64,
        published: Result<(), SequenceGapPublishError>,
    },
    /// A duplicate or backwards sequence (`observed <= last`): NOT a gap and
    /// NOT a recovery (recovery requires a monotonic advance). The staleness
    /// state is left exactly as it was — a replayed / late tick can neither
    /// clear a real gap nor open a new one.
    NonMonotonic { last: u64, observed: u64 },
}

/// Outcome of an operator-acknowledged resync (the second SRS-MD-007 recovery
/// condition).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ResyncOutcome {
    /// The line was tracked; it is now Fresh and awaiting a new baseline.
    Acknowledged,
    /// The security is not tracked (no tick ever observed) — nothing to
    /// resync. Fails safe: an operator cannot fabricate a Fresh state for a
    /// line that was never subscribed.
    NotTracked,
}

#[derive(Debug, Clone, Copy)]
struct SecurityStreamState {
    /// `None` until the first tick after start / operator resync establishes
    /// the baseline. A reconnect can legitimately jump the sequence, so the
    /// next tick re-baselines rather than reporting a false gap.
    last_sequence: Option<u64>,
    freshness: MarketDataFreshness,
    /// Epoch-ns the line went gap-stale; `None` while Fresh. Lets a runtime
    /// freshness-probe adapter derive `staleness_seconds` against its own
    /// clock without the detector owning wall-clock I/O.
    stale_since_ns: Option<i64>,
}

impl SecurityStreamState {
    fn fresh_awaiting_baseline() -> Self {
        Self {
            last_sequence: None,
            freshness: MarketDataFreshness::Fresh,
            stale_since_ns: None,
        }
    }
}

/// SRS-MD-007 tick-sequence gap detector.
///
/// Tracks, per canonical [`SecurityKey`], the last observed tick sequence and
/// the resulting [`MarketDataFreshness`] of that security's consolidated
/// upstream line. `observe_tick` classifies each delivery:
///
/// * `observed == last + 1` — in-sequence; if the line was gap-stale this
///   fresh monotonic tick RECOVERS it to Fresh (recovery condition #1).
/// * `observed > last + 1` — a GAP: missing ticks. Publishes a
///   `SequenceGapEvent` (symbol, expected, observed, timestamp) and marks the
///   line Stale.
/// * `observed <= last` — a duplicate / backwards tick: neither a gap nor a
///   recovery; state unchanged.
///
/// The other recovery path is [`acknowledge_resync`](Self::acknowledge_resync)
/// (recovery condition #2). `freshness` fails CLOSED: a security never observed
/// returns `Stale` so the SRS-MD-004 bridge blocks orders on a silent line.
///
/// The detector is single-threaded and models the structural sequence/
/// staleness contract; the async feed loop, wall clock, `MarketDataFreshness
/// Probe` bridge to `atp-execution`, and the SRS-LOG-001 / dashboard sinks are
/// the deferred runtime (see `runtime_services.json`
/// `sequence_gap_contract.deferred[]`).
#[derive(Debug, Default)]
pub struct SequenceGapDetector {
    securities: BTreeMap<SecurityKey, SecurityStreamState>,
}

impl SequenceGapDetector {
    pub fn new() -> Self {
        Self::default()
    }

    /// Observe one delivered tick for its security, updating the sequence /
    /// staleness state and publishing a `SequenceGapEvent` through `events`
    /// when (and only when) a forward gap is detected. `observed_at_ns` is the
    /// caller's clock reading (epoch nanoseconds) stamped onto any gap event —
    /// injected rather than read here so the detector stays free of wall-clock
    /// I/O and every observation is deterministically reproducible.
    ///
    /// Fails closed on an uncanonicalizable tick (empty symbol, or an option
    /// whose full contract identity is not yet modeled) with the same
    /// [`SubscriptionRegistryError`] the registry's `fan_out` returns — a tick
    /// that cannot name a security can neither advance a sequence nor open a
    /// gap.
    pub fn observe_tick<S: SequenceGapEventSink>(
        &mut self,
        tick: &MarketDataTick,
        observed_at_ns: i64,
        events: &S,
    ) -> Result<GapObservation, SubscriptionRegistryError> {
        let key = tick.security_key()?;
        let observed = tick.tick_seq;
        let state = self
            .securities
            .entry(key.clone())
            .or_insert_with(SecurityStreamState::fresh_awaiting_baseline);

        let Some(last) = state.last_sequence else {
            // First tick after start / operator resync → establish baseline.
            state.last_sequence = Some(observed);
            state.freshness = MarketDataFreshness::Fresh;
            state.stale_since_ns = None;
            return Ok(GapObservation::Baseline);
        };

        // `checked_sub` classifies the delivery without any add that could
        // overflow at u64::MAX: None/Some(0) => observed <= last (non-
        // monotonic), Some(1) => in-sequence, Some(>=2) => a forward gap.
        match observed.checked_sub(last) {
            None | Some(0) => Ok(GapObservation::NonMonotonic { last, observed }),
            Some(1) => {
                let recovered = state.freshness.is_stale();
                state.last_sequence = Some(observed);
                state.freshness = MarketDataFreshness::Fresh;
                state.stale_since_ns = None;
                Ok(GapObservation::InSequence { recovered })
            }
            Some(_) => {
                // observed >= last + 2, so `last + 1` cannot overflow.
                let expected = last + 1;
                // Commit the stale state — the SRS-MD-004 order-block
                // mechanism — BEFORE publishing, so even a failing / dropping
                // sink leaves the gapped line `Stale` (fail closed), never
                // silently tradable.
                //
                // `stale_since_ns` records when the line FIRST went stale
                // (Fresh -> Stale). Preserve it across REPEATED gaps on an
                // already-stale line so the heartbeat / dashboard staleness age
                // reflects the ORIGINAL gap onset, not the latest gap.
                let was_fresh = !state.freshness.is_stale();
                state.last_sequence = Some(observed);
                state.freshness = MarketDataFreshness::Stale;
                if was_fresh {
                    state.stale_since_ns = Some(observed_at_ns);
                }
                let published = events.record(SequenceGapEvent {
                    symbol: key.symbol().to_string(),
                    asset_class: key.asset_class(),
                    expected_sequence: expected,
                    observed_sequence: observed,
                    observed_at_ns,
                });
                Ok(GapObservation::Gap {
                    expected,
                    observed,
                    published,
                })
            }
        }
    }

    /// SRS-MD-007 recovery condition #2 — operator-acknowledged resync. After
    /// an operator confirms the feed for `key` is resynced, the line returns to
    /// Fresh and its sequence baseline is FORGOTTEN: the next observed tick
    /// re-establishes the baseline at whatever sequence the resynced feed
    /// resumes at, so a legitimate post-reconnect jump is not reported as a new
    /// gap. Returns [`ResyncOutcome::NotTracked`] for an unsubscribed security
    /// (fails safe — no Fresh state is fabricated for a line never observed).
    pub fn acknowledge_resync(&mut self, key: &SecurityKey) -> ResyncOutcome {
        match self.securities.get_mut(key) {
            Some(state) => {
                state.last_sequence = None;
                state.freshness = MarketDataFreshness::Fresh;
                state.stale_since_ns = None;
                ResyncOutcome::Acknowledged
            }
            None => ResyncOutcome::NotTracked,
        }
    }

    /// SRS-MD-004 freshness view of a security's consolidated line. Fails
    /// CLOSED: a security the detector has never observed a tick for returns
    /// [`MarketDataFreshness::Stale`] (no fresh data ⇒ not tradable), so the
    /// `MarketDataFreshnessProbe` adapter that bridges this detector to the
    /// execution gate blocks orders on an unsubscribed / silent line rather
    /// than admitting them.
    pub fn freshness(&self, key: &SecurityKey) -> MarketDataFreshness {
        self.securities
            .get(key)
            .map_or(MarketDataFreshness::Stale, |state| state.freshness)
    }

    /// Convenience predicate over [`freshness`](Self::freshness).
    pub fn is_stale(&self, key: &SecurityKey) -> bool {
        self.freshness(key).is_stale()
    }

    /// Epoch-ns the security's line went gap-stale, or `None` while Fresh /
    /// untracked. A runtime freshness-probe adapter reads this against its own
    /// clock to compute `MarketDataFreshnessProbe::staleness_seconds`.
    pub fn stale_since_ns(&self, key: &SecurityKey) -> Option<i64> {
        self.securities
            .get(key)
            .and_then(|state| state.stale_since_ns)
    }

    /// Whether the detector has observed at least one tick for `key`.
    pub fn is_tracked(&self, key: &SecurityKey) -> bool {
        self.securities.contains_key(key)
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
        assert_eq!(error.category, OrderErrorCategory::SubscriptionLimitReached);
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
                self.try_acquire_calls.set(self.try_acquire_calls.get() + 1);
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

    // ----------------------------------------------------------------- //
    // SRS-MD-007 tick-sequence gap detection + staleness
    // ----------------------------------------------------------------- //

    #[derive(Default)]
    struct GapSinkSpy {
        events: RefCell<Vec<SequenceGapEvent>>,
    }

    impl SequenceGapEventSink for GapSinkSpy {
        fn record(&self, event: SequenceGapEvent) -> Result<(), SequenceGapPublishError> {
            self.events.borrow_mut().push(event);
            Ok(())
        }
    }

    /// Sink that panics if consulted — proves a non-gap observation publishes
    /// nothing.
    struct ForbiddenGapSink;

    impl SequenceGapEventSink for ForbiddenGapSink {
        fn record(&self, _event: SequenceGapEvent) -> Result<(), SequenceGapPublishError> {
            panic!("SRS-MD-007: a non-gap observation must not publish a SequenceGapEvent");
        }
    }

    /// Sink that FAILS every publication — models a concrete SRS-LOG-001 /
    /// dashboard sink whose durable write or transport failed. Used to prove
    /// the stale (order-blocking) state does not depend on successful
    /// publication and that the failure is surfaced to the caller.
    struct FailingGapSink;

    impl SequenceGapEventSink for FailingGapSink {
        fn record(&self, _event: SequenceGapEvent) -> Result<(), SequenceGapPublishError> {
            Err(SequenceGapPublishError::new("durable log write failed"))
        }
    }

    const T0: i64 = 1_700_000_000_000_000_000; // fixed epoch-ns for determinism

    #[test]
    fn first_tick_establishes_baseline_and_is_fresh() {
        // A security's first tick has no prior sequence to compare against, so
        // it establishes the baseline — never a gap — and the line is Fresh.
        let mut detector = SequenceGapDetector::new();
        assert_eq!(
            detector
                .observe_tick(&tick("AAPL", 5), T0, &ForbiddenGapSink)
                .unwrap(),
            GapObservation::Baseline
        );
        assert_eq!(
            detector.freshness(&eq_key("AAPL")),
            MarketDataFreshness::Fresh
        );
        assert!(detector.is_tracked(&eq_key("AAPL")));
        assert_eq!(detector.stale_since_ns(&eq_key("AAPL")), None);
    }

    #[test]
    fn forward_skip_is_a_gap_that_marks_the_line_stale() {
        // SRS-MD-007 core AC: an observed sequence that skips ahead of the
        // expected next value is a gap — logged with symbol / expected /
        // observed / timestamp — and the affected line enters the stale state.
        let mut detector = SequenceGapDetector::new();
        let sink = GapSinkSpy::default();
        detector.observe_tick(&tick("AAPL", 5), T0, &sink).unwrap();
        // Next expected is 6; 8 arrives → 6 and 7 are missing.
        assert_eq!(
            detector
                .observe_tick(&tick("AAPL", 8), T0 + 1, &sink)
                .unwrap(),
            GapObservation::Gap {
                expected: 6,
                observed: 8,
                published: Ok(())
            }
        );
        assert_eq!(
            detector.freshness(&eq_key("AAPL")),
            MarketDataFreshness::Stale
        );
        assert!(detector.is_stale(&eq_key("AAPL")));
        assert_eq!(detector.stale_since_ns(&eq_key("AAPL")), Some(T0 + 1));

        let events = sink.events.borrow();
        assert_eq!(events.len(), 1, "exactly one gap event per detected gap");
        let event = &events[0];
        assert_eq!(event.symbol, "AAPL");
        assert_eq!(event.asset_class, AssetClass::Equity);
        assert_eq!(event.expected_sequence, 6);
        assert_eq!(event.observed_sequence, 8);
        assert_eq!(event.observed_at_ns, T0 + 1);
    }

    #[test]
    fn monotonic_fresh_tick_recovers_from_a_gap() {
        // SRS-MD-007 recovery condition #1: a fresh tick with a monotonic
        // sequence (the next expected value) clears the stale state.
        let mut detector = SequenceGapDetector::new();
        let sink = GapSinkSpy::default();
        detector.observe_tick(&tick("AAPL", 5), T0, &sink).unwrap();
        detector
            .observe_tick(&tick("AAPL", 8), T0 + 1, &sink)
            .unwrap();
        assert!(detector.is_stale(&eq_key("AAPL")));

        // 9 == 8 + 1 → in-sequence monotonic advance → recover. No new event.
        assert_eq!(
            detector
                .observe_tick(&tick("AAPL", 9), T0 + 2, &ForbiddenGapSink)
                .unwrap(),
            GapObservation::InSequence { recovered: true }
        );
        assert_eq!(
            detector.freshness(&eq_key("AAPL")),
            MarketDataFreshness::Fresh
        );
        assert_eq!(detector.stale_since_ns(&eq_key("AAPL")), None);
        assert_eq!(sink.events.borrow().len(), 1, "recovery adds no gap event");
    }

    #[test]
    fn in_sequence_ticks_on_a_healthy_line_stay_fresh() {
        // Negative control: contiguous ticks never publish and never go stale.
        let mut detector = SequenceGapDetector::new();
        detector
            .observe_tick(&tick("AAPL", 1), T0, &ForbiddenGapSink)
            .unwrap();
        for seq in 2..=10 {
            assert_eq!(
                detector
                    .observe_tick(&tick("AAPL", seq), T0, &ForbiddenGapSink)
                    .unwrap(),
                GapObservation::InSequence { recovered: false }
            );
        }
        assert_eq!(
            detector.freshness(&eq_key("AAPL")),
            MarketDataFreshness::Fresh
        );
    }

    #[test]
    fn duplicate_or_backwards_tick_is_a_non_monotonic_noop() {
        // A replayed (duplicate) or out-of-order (backwards) tick is neither a
        // gap nor a recovery: it must NOT publish and must NOT change staleness.
        let mut detector = SequenceGapDetector::new();
        detector
            .observe_tick(&tick("AAPL", 5), T0, &ForbiddenGapSink)
            .unwrap();
        // Duplicate of the last sequence.
        assert_eq!(
            detector
                .observe_tick(&tick("AAPL", 5), T0, &ForbiddenGapSink)
                .unwrap(),
            GapObservation::NonMonotonic {
                last: 5,
                observed: 5
            }
        );
        // Backwards.
        assert_eq!(
            detector
                .observe_tick(&tick("AAPL", 3), T0, &ForbiddenGapSink)
                .unwrap(),
            GapObservation::NonMonotonic {
                last: 5,
                observed: 3
            }
        );
        assert_eq!(
            detector.freshness(&eq_key("AAPL")),
            MarketDataFreshness::Fresh
        );
    }

    #[test]
    fn a_stale_line_is_not_recovered_by_a_duplicate_tick() {
        // Safety: only a MONOTONIC fresh tick (or operator resync) recovers a
        // gap-stale line. A duplicate / backwards tick while stale must leave
        // the line stale — otherwise a replayed tick could silently unblock
        // trading on a line that is still missing data.
        let mut detector = SequenceGapDetector::new();
        let sink = GapSinkSpy::default();
        detector.observe_tick(&tick("AAPL", 5), T0, &sink).unwrap();
        detector
            .observe_tick(&tick("AAPL", 9), T0 + 1, &sink)
            .unwrap();
        assert!(detector.is_stale(&eq_key("AAPL")));
        // A duplicate of the gap tick, and a backwards tick — neither recovers.
        detector
            .observe_tick(&tick("AAPL", 9), T0 + 2, &ForbiddenGapSink)
            .unwrap();
        detector
            .observe_tick(&tick("AAPL", 4), T0 + 3, &ForbiddenGapSink)
            .unwrap();
        assert!(
            detector.is_stale(&eq_key("AAPL")),
            "a duplicate / backwards tick must not clear a real gap"
        );
    }

    #[test]
    fn operator_resync_recovers_and_rebaselines_at_a_jumped_sequence() {
        // SRS-MD-007 recovery condition #2: an operator-acknowledged resync
        // returns the line to Fresh and forgets the baseline, so the resynced
        // feed can resume at ANY sequence (e.g. after an IB reconnect) without
        // the first post-resync tick being mis-reported as a fresh gap.
        let mut detector = SequenceGapDetector::new();
        let sink = GapSinkSpy::default();
        detector.observe_tick(&tick("AAPL", 5), T0, &sink).unwrap();
        detector
            .observe_tick(&tick("AAPL", 8), T0 + 1, &sink)
            .unwrap();
        assert!(detector.is_stale(&eq_key("AAPL")));

        assert_eq!(
            detector.acknowledge_resync(&eq_key("AAPL")),
            ResyncOutcome::Acknowledged
        );
        assert_eq!(
            detector.freshness(&eq_key("AAPL")),
            MarketDataFreshness::Fresh
        );
        assert_eq!(detector.stale_since_ns(&eq_key("AAPL")), None);

        // Feed resumes at a far-jumped sequence → baseline, NOT a gap.
        assert_eq!(
            detector
                .observe_tick(&tick("AAPL", 100), T0 + 2, &ForbiddenGapSink)
                .unwrap(),
            GapObservation::Baseline
        );
        assert_eq!(
            detector.freshness(&eq_key("AAPL")),
            MarketDataFreshness::Fresh
        );
        // Only the original gap event was ever published.
        assert_eq!(sink.events.borrow().len(), 1);
    }

    #[test]
    fn resync_of_an_untracked_security_fails_safe() {
        // An operator cannot resync a line that was never subscribed: the
        // outcome is NotTracked and the fail-closed freshness stays Stale.
        let mut detector = SequenceGapDetector::new();
        assert_eq!(
            detector.acknowledge_resync(&eq_key("AAPL")),
            ResyncOutcome::NotTracked
        );
        assert_eq!(
            detector.freshness(&eq_key("AAPL")),
            MarketDataFreshness::Stale
        );
        assert!(!detector.is_tracked(&eq_key("AAPL")));
    }

    #[test]
    fn unobserved_security_is_stale_fail_closed() {
        // SRS-MD-004 fail-closed default: a security the detector has never
        // seen a tick for is Stale, so the freshness bridge blocks orders on a
        // silent / unsubscribed line rather than admitting them.
        let detector = SequenceGapDetector::new();
        assert_eq!(
            detector.freshness(&eq_key("NFLX")),
            MarketDataFreshness::Stale
        );
        assert!(detector.is_stale(&eq_key("NFLX")));
        assert_eq!(detector.stale_since_ns(&eq_key("NFLX")), None);
    }

    #[test]
    fn each_gap_publishes_its_own_event_and_the_line_stays_stale() {
        // Two successive forward skips are two distinct loss events; each is
        // logged with its own expected/observed and the line remains stale
        // until a real recovery.
        let mut detector = SequenceGapDetector::new();
        let sink = GapSinkSpy::default();
        detector.observe_tick(&tick("AAPL", 5), T0, &sink).unwrap();
        detector
            .observe_tick(&tick("AAPL", 8), T0 + 1, &sink)
            .unwrap(); // gap 6..8
        detector
            .observe_tick(&tick("AAPL", 20), T0 + 2, &sink)
            .unwrap(); // gap 9..20
        let events = sink.events.borrow();
        assert_eq!(events.len(), 2);
        assert_eq!(events[0].expected_sequence, 6);
        assert_eq!(events[0].observed_sequence, 8);
        assert_eq!(events[1].expected_sequence, 9);
        assert_eq!(events[1].observed_sequence, 20);
        assert!(detector.is_stale(&eq_key("AAPL")));
    }

    #[test]
    fn gap_stale_state_is_fail_closed_when_publication_fails() {
        // Safety invariant: the SRS-MD-004 order-block (the line going Stale) is
        // committed independently of the gap event's publication. When the sink
        // FAILS the publication (a failed durable SRS-LOG-001 write / dashboard
        // transport), the gapped line is still Stale — fail CLOSED, never a
        // silently-tradable gap — AND the failure is surfaced to the caller on
        // `published` so the runtime can alert on the lost audit evidence.
        let mut detector = SequenceGapDetector::new();
        detector
            .observe_tick(&tick("AAPL", 1), T0, &FailingGapSink)
            .unwrap();
        let observation = detector
            .observe_tick(&tick("AAPL", 6), T0 + 1, &FailingGapSink)
            .unwrap();
        match observation {
            GapObservation::Gap {
                expected,
                observed,
                published,
            } => {
                assert_eq!(expected, 2);
                assert_eq!(observed, 6);
                assert!(
                    published.is_err(),
                    "a failed publication must surface Err to the caller"
                );
            }
            other => panic!("expected a Gap, got {other:?}"),
        }
        assert_eq!(
            detector.freshness(&eq_key("AAPL")),
            MarketDataFreshness::Stale,
            "a failed gap-event publication must still leave the line stale (fail closed)"
        );
        assert_eq!(detector.stale_since_ns(&eq_key("AAPL")), Some(T0 + 1));
    }

    #[test]
    fn repeated_gaps_preserve_the_original_stale_onset_time() {
        // stale_since_ns must record when the line FIRST went stale, not the
        // latest gap. A second gap on an already-stale line must NOT reset the
        // staleness age (which would underreport it on the heartbeat/dashboard).
        let mut detector = SequenceGapDetector::new();
        let sink = GapSinkSpy::default();
        detector.observe_tick(&tick("AAPL", 5), T0, &sink).unwrap();
        // First gap → stale, onset recorded at T0 + 1.
        detector
            .observe_tick(&tick("AAPL", 8), T0 + 1, &sink)
            .unwrap();
        assert_eq!(detector.stale_since_ns(&eq_key("AAPL")), Some(T0 + 1));
        // Second gap on the still-stale line at a LATER time must not move it.
        detector
            .observe_tick(&tick("AAPL", 20), T0 + 500, &sink)
            .unwrap();
        assert_eq!(
            detector.stale_since_ns(&eq_key("AAPL")),
            Some(T0 + 1),
            "a repeated gap must preserve the original stale-onset time"
        );
        // A monotonic recovery clears it; the next gap starts a fresh onset.
        detector
            .observe_tick(&tick("AAPL", 21), T0 + 600, &sink)
            .unwrap();
        assert_eq!(detector.stale_since_ns(&eq_key("AAPL")), None);
        detector
            .observe_tick(&tick("AAPL", 30), T0 + 700, &sink)
            .unwrap();
        assert_eq!(
            detector.stale_since_ns(&eq_key("AAPL")),
            Some(T0 + 700),
            "after recovery a new gap records a fresh onset time"
        );
    }

    #[test]
    fn producer_contract_a_delivery_renumbered_stream_hides_gaps() {
        // PRODUCER CONTRACT (SRS-MD-007): the detector finds gaps as SKIPS in
        // tick_seq, so it only works if tick_seq is the UPSTREAM provider
        // sequence. This test documents the failure mode the producer must
        // avoid: if the ingestion adapter re-numbered ticks 1,2,3,4 per
        // DELIVERED callback (dropping upstream ticks silently), the stream is
        // gap-free by construction and NO gap is ever detected — even though
        // upstream ticks were lost. Contrast with the upstream-sequence stream
        // below, where the same drop leaves a hole and IS detected.
        let mut renumbered = SequenceGapDetector::new();
        let sink = GapSinkSpy::default();
        // Contiguous delivery counter (what a WRONG producer would supply).
        for seq in 1..=5 {
            renumbered
                .observe_tick(&tick("AAPL", seq), T0 + seq as i64, &sink)
                .unwrap();
        }
        assert!(
            !renumbered.is_stale(&eq_key("AAPL")),
            "a contiguous delivery-renumbered stream never gaps — the drop is hidden"
        );
        assert!(sink.events.borrow().is_empty());

        // Upstream provider sequence with the same lost tick (4 is missing).
        let mut upstream = SequenceGapDetector::new();
        let sink2 = GapSinkSpy::default();
        upstream.observe_tick(&tick("AAPL", 1), T0, &sink2).unwrap();
        upstream
            .observe_tick(&tick("AAPL", 2), T0 + 1, &sink2)
            .unwrap();
        upstream
            .observe_tick(&tick("AAPL", 3), T0 + 2, &sink2)
            .unwrap();
        // Upstream 4 dropped; 5 arrives → a detectable gap.
        upstream
            .observe_tick(&tick("AAPL", 5), T0 + 3, &sink2)
            .unwrap();
        assert!(
            upstream.is_stale(&eq_key("AAPL")),
            "an upstream provider sequence exposes the drop as a gap"
        );
        assert_eq!(sink2.events.borrow().len(), 1);
    }

    #[test]
    fn gaps_are_isolated_per_security() {
        // A gap on one security's line never marks another security's line
        // stale — staleness is per canonical SecurityKey.
        let mut detector = SequenceGapDetector::new();
        let sink = GapSinkSpy::default();
        detector.observe_tick(&tick("AAPL", 1), T0, &sink).unwrap();
        detector.observe_tick(&tick("MSFT", 1), T0, &sink).unwrap();
        // Gap on AAPL only.
        detector
            .observe_tick(&tick("AAPL", 5), T0 + 1, &sink)
            .unwrap();
        // MSFT continues in-sequence.
        detector
            .observe_tick(&tick("MSFT", 2), T0 + 1, &sink)
            .unwrap();
        assert_eq!(
            detector.freshness(&eq_key("AAPL")),
            MarketDataFreshness::Stale
        );
        assert_eq!(
            detector.freshness(&eq_key("MSFT")),
            MarketDataFreshness::Fresh
        );
        assert_eq!(sink.events.borrow().len(), 1);
        assert_eq!(sink.events.borrow()[0].symbol, "AAPL");
    }

    #[test]
    fn case_and_whitespace_variants_share_one_sequence_stream() {
        // The detector keys on the canonical SecurityKey, so "AAPL" and
        // " aapl " are ONE line — a gap seen under either spelling marks the
        // same stale line (consistent with the registry's dedup).
        let mut detector = SequenceGapDetector::new();
        let sink = GapSinkSpy::default();
        detector.observe_tick(&tick("AAPL", 5), T0, &sink).unwrap();
        assert_eq!(
            detector
                .observe_tick(&tick("  aapl ", 9), T0 + 1, &sink)
                .unwrap(),
            GapObservation::Gap {
                expected: 6,
                observed: 9,
                published: Ok(())
            }
        );
        assert_eq!(
            detector.freshness(&eq_key("AAPL")),
            MarketDataFreshness::Stale
        );
    }

    #[test]
    fn uncanonicalizable_ticks_fail_closed() {
        // An empty-symbol or option tick cannot name a security — it can
        // neither advance a sequence nor open a gap. The detector rejects it
        // with the same error taxonomy the registry uses and registers nothing.
        let mut detector = SequenceGapDetector::new();
        assert_eq!(
            detector.observe_tick(
                &MarketDataTick {
                    symbol: String::new(),
                    asset_class: AssetClass::Equity,
                    tick_seq: 1,
                },
                T0,
                &ForbiddenGapSink,
            ),
            Err(SubscriptionRegistryError::EmptySymbol)
        );
        assert_eq!(
            detector.observe_tick(
                &MarketDataTick {
                    symbol: "AAPL".to_string(),
                    asset_class: AssetClass::Option,
                    tick_seq: 1,
                },
                T0,
                &ForbiddenGapSink,
            ),
            Err(SubscriptionRegistryError::OptionContractUnsupported)
        );
        assert!(!detector.is_tracked(&eq_key("AAPL")));
    }

    #[test]
    fn generated_streams_keep_the_staleness_invariant() {
        // Property sweep (seeded LCG, zero-dep): drive many randomized tick
        // streams and assert the core invariants hold on every step —
        //   * a forward skip publishes exactly one event and goes stale;
        //   * an in-sequence tick recovers a stale line and never publishes;
        //   * a duplicate/backwards tick changes neither event count nor state;
        //   * the published event's (expected, observed) always brackets a
        //     real gap (expected < observed, expected == last + 1).
        let mut lcg: u64 = 0x9E37_79B9_7F4A_7C15;
        let mut next = || {
            lcg = lcg
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            lcg >> 33
        };
        for _ in 0..400 {
            let mut detector = SequenceGapDetector::new();
            let sink = GapSinkSpy::default();
            let mut last: Option<u64> = None;
            let mut expected_events = 0usize;
            let mut expect_stale = false;
            let steps = 5 + (next() % 40);
            for step in 0..steps {
                // Choose the next sequence relative to `last`, mixing forward
                // skips, contiguous advances, duplicates and regressions.
                let seq = match last {
                    None => next() % 1000,
                    Some(l) => {
                        let roll = next() % 4;
                        match roll {
                            0 => l + 1,                              // in-sequence
                            1 => l + 2 + (next() % 50),              // forward gap
                            2 => l,                                  // duplicate
                            _ => l.saturating_sub(1 + (next() % 5)), // backwards
                        }
                    }
                };
                let before = sink.events.borrow().len();
                let obs = detector
                    .observe_tick(&tick("AAPL", seq), T0 + step as i64, &sink)
                    .unwrap();
                let after = sink.events.borrow().len();
                // Borrow `obs` (GapObservation is no longer Copy — Gap carries
                // the sink's publish Result).
                match &obs {
                    GapObservation::Baseline => {
                        assert_eq!(after, before, "baseline must not publish");
                        expect_stale = false;
                    }
                    GapObservation::InSequence { recovered } => {
                        assert_eq!(after, before, "in-sequence must not publish");
                        assert_eq!(*recovered, expect_stale, "recovered iff was stale");
                        expect_stale = false;
                    }
                    GapObservation::Gap {
                        expected,
                        observed,
                        published,
                    } => {
                        assert_eq!(after, before + 1, "a gap publishes exactly one event");
                        assert_eq!(*expected, last.unwrap() + 1);
                        assert!(*observed > *expected, "a gap is a strict forward skip");
                        assert!(published.is_ok(), "the spy sink always publishes Ok");
                        let ev = sink.events.borrow();
                        let ev = ev.last().unwrap();
                        assert_eq!(ev.expected_sequence, *expected);
                        assert_eq!(ev.observed_sequence, *observed);
                        expected_events += 1;
                        expect_stale = true;
                    }
                    GapObservation::NonMonotonic { .. } => {
                        assert_eq!(after, before, "non-monotonic must not publish");
                        // staleness unchanged: expect_stale carries over.
                    }
                }
                // The observable freshness always agrees with the tracked
                // expectation.
                assert_eq!(
                    detector.is_stale(&eq_key("AAPL")),
                    expect_stale,
                    "freshness must track the last gap/recovery"
                );
                // Advance our shadow `last` exactly as the detector does.
                last = match &obs {
                    GapObservation::NonMonotonic { .. } => last,
                    _ => Some(seq),
                };
            }
            assert_eq!(sink.events.borrow().len(), expected_events);
        }
    }
}
