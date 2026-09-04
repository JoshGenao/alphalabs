//! SRS-MD-001 / SyRS SYS-70 — the consolidated market-data subscription
//! registry, in its OWN module so its private state is genuinely private.
//!
//! The file boundary here is load-bearing, not tidiness. `subscribers` is the
//! consolidated subscription set, and SRS-MD-005 requires every path that can
//! ADMIT a subscription to consult the scheduled-restart window first. A static
//! guard can only enforce that over a set it can bound — and while the registry
//! lived in `lib.rs`, that set was unbounded in a way three successive versions
//! of the guard each missed: Rust makes a module's private items visible to its
//! DESCENDANTS, so `live_feed` (a child module) could have written
//! `registry.subscribers` and opened an upstream line straight through the
//! suspension, with no scan over `lib.rs` able to see it.
//!
//! Privacy runs parent-to-child, never child-to-parent. Moving the field here
//! makes `lib.rs` and every sibling module unable to name it, so "the functions
//! in this file" really is the complete set of code that can reach the
//! consolidated set. The guard in `tools/connectivity_check.py` scans exactly
//! this file, and the property it relies on is now the compiler's rather than
//! an assertion about one.
//!
//! Keep it that way: anything that needs the subscriber map belongs in this
//! file, gated, not in a module that reaches in.
//!
//! The boundary is proved by the compiler, not asserted. An external consumer
//! cannot reach the field:
//!
//! ```compile_fail
//! use atp_market_data::ConsolidatedSubscriptionRegistry;
//! let mut registry = ConsolidatedSubscriptionRegistry::new(100);
//! // error[E0616]: field `subscribers` ... is private
//! let _ = &mut registry.subscribers;
//! ```
//!
//! and neither can anything inside the crate, which is the case that actually
//! bit — a sibling module reaching in was invisible to every scan over the
//! crate root:
//!
//! ```compile_fail
//! # use atp_market_data::live_feed as _sibling;
//! use atp_market_data::ConsolidatedSubscriptionRegistry;
//! fn sibling_reach(registry: &mut ConsolidatedSubscriptionRegistry) {
//!     registry.subscribers.clear();
//! }
//! ```
//!
//! Both are mutation-verifiable: make the field `pub` and they fail with
//! "Test compiled successfully, but it is marked `compile_fail`".

use std::collections::BTreeMap;
use std::fmt;

use atp_types::{
    MarketDataAdmission, MarketDataTick, SecurityKey, SecurityKeyError, StrategyId,
    SubscriptionChange, SubscriptionChangeEvent, SubscriptionLimitState, SubscriptionRequest,
};

use crate::{RestartWindowGate, SubscriptionLineCounter};

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
    /// The scheduled IB Gateway restart window is suspending market-data
    /// requests (SRS-MD-005, SyRS SYS-75(a)). Distinct from the line-limit
    /// refusal on purpose: nothing was consumed and nothing is misconfigured, so the
    /// identical request succeeds unchanged once the window closes. Conflating
    /// the two would send an operator hunting for a line-budget problem that
    /// does not exist.
    SuspendedForScheduledRestart,
    /// The IB Gateway is unreachable (SyRS SYS-45). Distinct from the
    /// scheduled-restart refusal above: this one is an incident the operator is
    /// paged about, and reporting it as planned maintenance would tell them to
    /// wait out an outage.
    ///
    /// The neighbouring doc comments deliberately name the sibling refusals in
    /// prose rather than as `Variant` tokens. `tools/subscription_fanout_check.py`
    /// looks for each variant by name inside this enum body, and a sibling test
    /// proves the check notices a DELETED declaration — a prose mention of the
    /// same token would keep it alive and silently disarm that guard.
    ConnectivityLost,
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
            Self::SuspendedForScheduledRestart => formatter.write_str(
                "SRS-MD-005/SYS-75: market-data requests are suspended for the \
                 scheduled IB Gateway restart window (planned maintenance)",
            ),
            Self::ConnectivityLost => formatter.write_str(
                "SRS-SAFE-003/SYS-45: market-data requests are blocked — IB Gateway \
                 is unreachable",
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
    /// SRS-MD-005 / SyRS SYS-75(a): `window` is a required port, not an
    /// option. This is the mutating admission point — the one that actually
    /// opens an upstream IB line — so a caller that could reach it without
    /// passing the window would open lines straight through the suspension
    /// that `request_subscription` refuses. Gating only the outer manager
    /// would leave exactly that bypass, so both entry points take the port and
    /// both consult it before touching any state.
    pub fn subscribe<S: SubscriptionChangeSink, W: RestartWindowGate>(
        &mut self,
        request: &SubscriptionRequest,
        window: &W,
        events: &S,
    ) -> Result<SubscriptionChange, SubscriptionRegistryError> {
        // Ahead of validation and ahead of every mutation: a refusal here must
        // leave the registry exactly as it found it, and the cheapest way to
        // guarantee that is to refuse before anything is borrowed mutably.
        match window.admission() {
            MarketDataAdmission::Admitted => {}
            MarketDataAdmission::SuspendedForScheduledRestart => {
                return Err(SubscriptionRegistryError::SuspendedForScheduledRestart);
            }
            MarketDataAdmission::ConnectivityLost => {
                return Err(SubscriptionRegistryError::ConnectivityLost);
            }
        }
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
