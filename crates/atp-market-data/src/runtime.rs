//! SRS-MD-001 runtime: the thread-safe fan-out manager that wraps the
//! single-threaded [`ConsolidatedSubscriptionRegistry`].
//!
//! The registry decides WHO receives a tick and HOW MANY upstream lines are
//! held. This runtime adds the three things the registry deliberately left
//! out (see `subscription_fanout_contract.deferred[]` "SRS-MD-001-runtime"):
//!
//!   * Upstream binding: the first subscriber of a security opens exactly ONE
//!     upstream line through the [`UpstreamMarketDataFeed`] port, and the last
//!     subscriber leaving closes it. The port is vendor-neutral; the IB
//!     adapter bridge lives in a composition root (this crate may depend only
//!     on `atp-types`, per SRS-ARCH-002).
//!   * Delivery transport: each strategy container owns one bounded queue.
//!     [`FanOutRuntime::ingest`] wraps a tick once in an `Arc` and offers it
//!     to every subscriber with a NON-BLOCKING `try_send`, so one slow reader
//!     can never delay the others (operator decision: drop + mark lagging).
//!   * Thread safety: all state sits behind one `Mutex`; the lock is held only
//!     for routing and non-blocking sends.
//!
//! Fail-closed rules:
//!   * Opening an upstream line IS a market-data request, so SRS-MD-005's
//!     restart window (and SYS-45 connectivity) is consulted through the
//!     [`RestartWindowGate`] port BEFORE `open`. A suspended or disconnected
//!     gateway is never sent a new request. The registry consults the same
//!     port again when it registers the subscriber; if the window closed in
//!     between, the just-opened line is closed again.
//!   * A subscriber is never registered without an upstream line: the line is
//!     opened BEFORE the registry records the first subscriber, and a failed
//!     open registers nothing and publishes nothing.
//!   * `subscribe` requires a registered delivery queue, so no subscription can
//!     exist that nobody would receive.
//!   * A poisoned lock rejects every mutation ([`FanOutRuntimeError::Poisoned`])
//!     and makes the SRS-MD-002 probe report `ExceededLimit`.
//!
//! The runtime never names the registry's private subscriber map (it lives in
//! the `subscriptions` module, whose admission set `tools/connectivity_check.py`
//! bounds). It goes through the registry's gated public API only, and keeps
//! its own per-subscriber key set to know which lines a departing strategy
//! held.
//!
//! Dropped ticks surface to the strategy as `tick_seq` gaps; turning a gap
//! into a stale state is SRS-MD-007's concern, not this module's.

use std::collections::{BTreeSet, HashMap};
use std::fmt;
use std::sync::mpsc::{
    self, Receiver, RecvError, RecvTimeoutError, SyncSender, TryRecvError, TrySendError,
};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use atp_types::{
    AssetClass, MarketDataAdmission, MarketDataTick, SecurityKey, StrategyId, SubscriptionChange,
    SubscriptionLimitState, SubscriptionRequest,
};

use crate::{
    ConsolidatedSubscriptionRegistry, RestartWindowGate, SubscriptionChangeSink,
    SubscriptionLineCounter, SubscriptionRegistryError,
};

/// Vendor-neutral port to the single upstream market-data connection.
///
/// The runtime calls `open` exactly once per security when its FIRST
/// subscriber arrives and `close` exactly once when its LAST subscriber
/// leaves. Both are called while the runtime lock is held, so an
/// implementation must not block on the network: it should enqueue the
/// request (the IB API is asynchronous - the request is written to the socket
/// and data arrives later through `ingest`).
///
/// **No re-entry, one lock order.** Every port the runtime holds (this one,
/// the [`RestartWindowGate`], the [`SubscriptionChangeSink`] and the
/// [`SubscriberLagSink`]) is called with the runtime's non-reentrant lock
/// held. An implementation must never call back into the runtime from inside
/// a port method (that self-deadlocks), and any lock it takes inside a port
/// method must never be held while calling INTO the runtime, for example a
/// tick thread that holds the bridge's lock while calling
/// [`FanOutRuntime::ingest`]. The order is always runtime lock first, then
/// the port's own lock.
///
/// **`close` is infallible by contract, and the bridge owns delivering it.**
/// The runtime stops counting a line the moment its last subscriber leaves.
/// That is only true of the real gateway if the cancel arrives: IB's cancel has
/// no acknowledgement on a live connection, and a lost connection has already
/// dropped every line on the gateway side. So an implementation that cannot
/// hand a cancel to the connection right now must queue it and deliver it
/// before it sends any later `open` on that connection, so the gateway never
/// holds more lines than the runtime counts.
pub trait UpstreamMarketDataFeed: Send + Sync {
    fn open(&self, key: &SecurityKey) -> Result<(), UpstreamFeedError>;
    fn close(&self, key: &SecurityKey);
}

/// The upstream feed refused to open a line. The subscription that needed it
/// is rejected and nothing is registered.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UpstreamFeedError {
    pub reason: String,
}

impl fmt::Display for UpstreamFeedError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "upstream market-data feed refused to open: {}",
            self.reason
        )
    }
}

impl std::error::Error for UpstreamFeedError {}

/// One tick as delivered to a subscriber. `upstream_received_at` is the
/// moment the upstream feed handed the tick to [`FanOutRuntime::ingest`];
/// the subscriber's receive time minus this value is the fan-out latency the
/// SRS-MD-001 <=100 ms criterion bounds.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FanOutDelivery {
    pub tick: MarketDataTick,
    pub upstream_received_at: Instant,
}

/// Published once at the START of each lagging episode: the subscriber's
/// queue was full and a tick was dropped for it. The episode ends at the next
/// successful delivery; a later overflow starts a new episode and publishes
/// again. Concrete sinks (deferred) route this to SRS-LOG-001 and the
/// dashboard.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubscriberLagEvent {
    pub strategy_id: StrategyId,
    pub symbol: String,
    pub asset_class: AssetClass,
    /// `tick_seq` of the first tick dropped in this episode.
    pub tick_seq: u64,
    /// Total ticks dropped for this subscriber since it registered.
    pub dropped_total: u64,
}

/// Publication port for [`SubscriberLagEvent`]s. Called while the runtime
/// lock is held, so it must not block and must not call back into the
/// runtime (see the lock-order rule on [`UpstreamMarketDataFeed`]).
pub trait SubscriberLagSink: Send + Sync {
    fn record(&self, event: SubscriberLagEvent);
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FanOutRuntimeError {
    /// The underlying registry rejected the request (empty identifiers, an
    /// option contract, the line ceiling, the scheduled-restart window, or a
    /// lost gateway).
    Registry(SubscriptionRegistryError),
    /// The upstream feed could not open the line; nothing was registered.
    Upstream(UpstreamFeedError),
    /// `subscribe` was called for a strategy with no delivery queue.
    SubscriberNotRegistered,
    /// `register_subscriber` was called twice for the same strategy.
    SubscriberAlreadyRegistered,
    /// A delivery queue needs room for at least one tick.
    ZeroCapacity,
    /// A thread panicked while holding the runtime lock; state may be
    /// inconsistent, so every mutation fails closed.
    Poisoned,
}

impl From<SubscriptionRegistryError> for FanOutRuntimeError {
    fn from(error: SubscriptionRegistryError) -> Self {
        Self::Registry(error)
    }
}

impl fmt::Display for FanOutRuntimeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Registry(error) => error.fmt(formatter),
            Self::Upstream(error) => error.fmt(formatter),
            Self::SubscriberNotRegistered => formatter
                .write_str("SRS-MD-001: subscriber has no delivery queue; register it first"),
            Self::SubscriberAlreadyRegistered => {
                formatter.write_str("SRS-MD-001: subscriber is already registered")
            }
            Self::ZeroCapacity => {
                formatter.write_str("SRS-MD-001: delivery queue capacity must be at least 1")
            }
            Self::Poisoned => formatter
                .write_str("SRS-MD-001: fan-out runtime lock is poisoned; refusing to mutate"),
        }
    }
}

impl std::error::Error for FanOutRuntimeError {}

/// Outcome of one [`FanOutRuntime::ingest`] call.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct IngestReport {
    /// Subscribers the tick was queued for.
    pub delivered: u32,
    /// Subscribers whose queue was full (tick dropped for them only).
    pub dropped: u32,
    /// Subscribers whose receiver was gone; they were deregistered and their
    /// lines released.
    pub disconnected: u32,
}

/// Departure notes left by dropped receivers: `(strategy, generation)`.
/// A separate lock from the runtime's, taken only briefly and never while
/// calling anything else, so a receiver can be dropped from any thread at any
/// time without deadlocking against a runtime call in progress.
type Departures = Arc<Mutex<Vec<(StrategyId, u64)>>>;

/// The receiving end of one strategy container's delivery queue. Dropping it
/// deregisters the subscriber and releases every line it held alone. The drop
/// leaves a departure note that the very next runtime call acts on (any call,
/// not only a tick for one of its securities), so a line on a security that
/// never ticks again cannot stay open and keep counting toward the IB line
/// limit. [`FanOutRuntime::reap_departed`] acts on the notes explicitly.
#[derive(Debug)]
pub struct SubscriberReceiver {
    strategy_id: StrategyId,
    /// Which registration this receiver belongs to. A strategy that
    /// re-registers gets a new generation, so a late note from its OLD
    /// receiver can never tear down the new queue (identity, not name).
    generation: u64,
    departures: Departures,
    receiver: Receiver<Arc<FanOutDelivery>>,
}

impl Drop for SubscriberReceiver {
    fn drop(&mut self) {
        // A poisoned departures lock still holds a valid Vec (pushes cannot
        // leave it half-written), so the note is recorded either way.
        self.departures
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .push((self.strategy_id.clone(), self.generation));
    }
}

impl SubscriberReceiver {
    pub fn strategy_id(&self) -> &StrategyId {
        &self.strategy_id
    }

    pub fn recv(&self) -> Result<Arc<FanOutDelivery>, RecvError> {
        self.receiver.recv()
    }

    pub fn recv_timeout(&self, timeout: Duration) -> Result<Arc<FanOutDelivery>, RecvTimeoutError> {
        self.receiver.recv_timeout(timeout)
    }

    pub fn try_recv(&self) -> Result<Arc<FanOutDelivery>, TryRecvError> {
        self.receiver.try_recv()
    }
}

#[derive(Debug)]
struct SubscriberQueue {
    sender: SyncSender<Arc<FanOutDelivery>>,
    generation: u64,
    /// Securities this subscriber holds, so deregistration can release them
    /// without reading the registry's private map.
    keys: BTreeSet<SecurityKey>,
    dropped: u64,
    lagging: bool,
}

#[derive(Debug)]
struct Inner {
    registry: ConsolidatedSubscriptionRegistry,
    queues: HashMap<StrategyId, SubscriberQueue>,
    next_generation: u64,
}

/// SRS-MD-001 thread-safe fan-out runtime. Share it across threads with an
/// `Arc`; every method takes `&self`.
pub struct FanOutRuntime<F, W, C, L> {
    inner: Mutex<Inner>,
    departures: Departures,
    feed: F,
    window: W,
    changes: C,
    lag: L,
}

impl<F, W, C, L> FanOutRuntime<F, W, C, L>
where
    F: UpstreamMarketDataFeed,
    W: RestartWindowGate + Send + Sync,
    C: SubscriptionChangeSink + Send + Sync,
    L: SubscriberLagSink,
{
    /// Build a runtime with the operator-configured IB line ceiling and the
    /// SRS-MD-005 restart-window / connectivity gate.
    pub fn new(line_limit: u32, feed: F, window: W, changes: C, lag: L) -> Self {
        Self {
            inner: Mutex::new(Inner {
                registry: ConsolidatedSubscriptionRegistry::new(line_limit),
                queues: HashMap::new(),
                next_generation: 0,
            }),
            departures: Arc::new(Mutex::new(Vec::new())),
            feed,
            window,
            changes,
            lag,
        }
    }

    /// Create the delivery queue for one strategy container. `capacity` is
    /// how many undelivered ticks the queue holds before the runtime starts
    /// dropping ticks for this subscriber.
    pub fn register_subscriber(
        &self,
        strategy_id: StrategyId,
        capacity: usize,
    ) -> Result<SubscriberReceiver, FanOutRuntimeError> {
        if strategy_id.as_str().trim().is_empty() {
            return Err(SubscriptionRegistryError::EmptyStrategyId.into());
        }
        // sync_channel(0) is a rendezvous channel: try_send would fail unless
        // the reader is already waiting, so every tick would be dropped.
        if capacity == 0 {
            return Err(FanOutRuntimeError::ZeroCapacity);
        }
        let mut inner = self.lock()?;
        if inner.queues.contains_key(&strategy_id) {
            return Err(FanOutRuntimeError::SubscriberAlreadyRegistered);
        }
        let (sender, receiver) = mpsc::sync_channel(capacity);
        let generation = inner.next_generation;
        inner.next_generation += 1;
        inner.queues.insert(
            strategy_id.clone(),
            SubscriberQueue {
                sender,
                generation,
                keys: BTreeSet::new(),
                dropped: 0,
                lagging: false,
            },
        );
        Ok(SubscriberReceiver {
            strategy_id,
            generation,
            departures: Arc::clone(&self.departures),
            receiver,
        })
    }

    /// Remove a strategy's delivery queue and every subscription it holds,
    /// closing each upstream line it was the last subscriber of. Returns the
    /// per-security transitions in key order (empty when it held none).
    pub fn deregister_subscriber(
        &self,
        strategy_id: &StrategyId,
    ) -> Result<Vec<SubscriptionChange>, FanOutRuntimeError> {
        let mut inner = self.lock()?;
        let Some(queue) = inner.queues.remove(strategy_id) else {
            return Err(FanOutRuntimeError::SubscriberNotRegistered);
        };
        Ok(self.release_all(&mut inner, strategy_id, &queue.keys))
    }

    /// Subscribe a registered strategy to one security. The first subscriber
    /// opens the upstream line (BEFORE it is registered); later subscribers
    /// share it. Every request, first or not, is refused while the
    /// restart window suspends market-data requests or the gateway is lost.
    pub fn subscribe(
        &self,
        request: &SubscriptionRequest,
    ) -> Result<SubscriptionChange, FanOutRuntimeError> {
        // Structural validation first (SRS-MD-005 precedence): a request that
        // can never succeed gets its own error in every phase.
        let key = request
            .security_key()
            .map_err(SubscriptionRegistryError::from)?;
        let mut inner = self.lock()?;
        if !inner.queues.contains_key(&request.strategy_id) {
            return Err(FanOutRuntimeError::SubscriberNotRegistered);
        }

        let change = if inner.registry.subscriber_count(&key) > 0 {
            // Joining an existing line sends nothing upstream; the registry
            // still consults the window, so a suspended request is refused.
            inner
                .registry
                .subscribe(request, &self.window, &self.changes)?
        } else {
            // First subscriber: every refusal the registry would give must
            // happen BEFORE the upstream request, or a refused subscription
            // would still have sent one (open-then-close).
            match self.window.admission() {
                MarketDataAdmission::Admitted => {}
                MarketDataAdmission::SuspendedForScheduledRestart => {
                    return Err(SubscriptionRegistryError::SuspendedForScheduledRestart.into());
                }
                MarketDataAdmission::ConnectivityLost => {
                    return Err(SubscriptionRegistryError::ConnectivityLost.into());
                }
            }
            if inner.registry.try_acquire(request) == SubscriptionLimitState::ExceededLimit {
                return Err(SubscriptionRegistryError::LineLimitReached {
                    configured_limit: inner.registry.line_limit(),
                }
                .into());
            }
            self.feed.open(&key).map_err(FanOutRuntimeError::Upstream)?;
            match inner
                .registry
                .subscribe(request, &self.window, &self.changes)
            {
                Ok(change) => change,
                Err(error) => {
                    // The window can close between the check above and the
                    // registry's own check. Release the line so the upstream
                    // never outlives the registration.
                    self.feed.close(&key);
                    return Err(error.into());
                }
            }
        };
        if let Some(queue) = inner.queues.get_mut(&request.strategy_id) {
            queue.keys.insert(key);
        }
        Ok(change)
    }

    /// Unsubscribe a strategy from one security. The last subscriber leaving
    /// closes the upstream line. Never gated: releasing a line is always safe
    /// (the registry's own `unsubscribe` exemption).
    pub fn unsubscribe(
        &self,
        strategy_id: &StrategyId,
        key: &SecurityKey,
    ) -> Result<SubscriptionChange, FanOutRuntimeError> {
        let mut inner = self.lock()?;
        let change = inner
            .registry
            .unsubscribe(strategy_id, key, &self.changes)?;
        if change == SubscriptionChange::Closed {
            self.feed.close(key);
        }
        if let Some(queue) = inner.queues.get_mut(strategy_id) {
            queue.keys.remove(key);
        }
        Ok(change)
    }

    /// Fan one upstream tick out to every subscriber of its security. Never
    /// blocks on a subscriber: a full queue drops the tick for that
    /// subscriber only; a gone receiver deregisters that subscriber.
    pub fn ingest(
        &self,
        tick: MarketDataTick,
        upstream_received_at: Instant,
    ) -> Result<IngestReport, FanOutRuntimeError> {
        let mut inner = self.lock_unreaped()?;
        let reaped = self.reap(&mut inner);
        let recipients = inner.registry.fan_out(&tick)?;
        let mut report = IngestReport {
            disconnected: reaped,
            ..IngestReport::default()
        };
        if recipients.is_empty() {
            return Ok(report);
        }

        let key = tick
            .security_key()
            .map_err(SubscriptionRegistryError::from)?;
        let tick_seq = tick.tick_seq;
        let delivery = Arc::new(FanOutDelivery {
            tick,
            upstream_received_at,
        });
        let mut gone = Vec::new();
        for strategy_id in recipients {
            let Some(queue) = inner.queues.get_mut(&strategy_id) else {
                // subscribe requires a queue and deregistration removes the
                // subscriptions, so this cannot happen; treat it as gone.
                gone.push(strategy_id);
                continue;
            };
            match queue.sender.try_send(Arc::clone(&delivery)) {
                Ok(()) => {
                    queue.lagging = false;
                    report.delivered += 1;
                }
                Err(TrySendError::Full(_)) => {
                    queue.dropped += 1;
                    report.dropped += 1;
                    if !queue.lagging {
                        queue.lagging = true;
                        self.lag.record(SubscriberLagEvent {
                            strategy_id: strategy_id.clone(),
                            symbol: key.symbol().to_string(),
                            asset_class: key.asset_class(),
                            tick_seq,
                            dropped_total: queue.dropped,
                        });
                    }
                }
                Err(TrySendError::Disconnected(_)) => gone.push(strategy_id),
            }
        }
        for strategy_id in gone {
            let keys = inner
                .queues
                .remove(&strategy_id)
                .map(|queue| queue.keys)
                .unwrap_or_default();
            self.release_all(&mut inner, &strategy_id, &keys);
            report.disconnected += 1;
        }
        Ok(report)
    }

    /// Act on every departure note left by a dropped receiver: deregister the
    /// subscriber and close the lines it held alone. Every other runtime call
    /// already does this first; this is for a caller that wants it done on a
    /// cadence even when nothing else is happening. Returns how many
    /// subscribers were deregistered.
    pub fn reap_departed(&self) -> Result<u32, FanOutRuntimeError> {
        let mut inner = self.lock_unreaped()?;
        Ok(self.reap(&mut inner))
    }

    /// Number of distinct upstream lines currently open.
    ///
    /// Like every read here, this reaps dropped receivers first on a healthy
    /// lock, so it can CLOSE upstream lines (and publish their change events):
    /// a count must never include a line only a dropped receiver holds. On a
    /// poisoned lock it reports without reaping, because reaping mutates.
    pub fn lines_in_use(&self) -> u32 {
        self.read().registry.distinct_subscriptions()
    }

    /// Number of subscribers of `key` (0 if none).
    pub fn subscriber_count(&self, key: &SecurityKey) -> u32 {
        self.read().registry.subscriber_count(key)
    }

    /// Total ticks dropped for `strategy_id`, or `None` if it has no queue.
    pub fn dropped_count(&self, strategy_id: &StrategyId) -> Option<u64> {
        self.read().queues.get(strategy_id).map(|q| q.dropped)
    }

    /// True while `strategy_id` is in a lagging episode.
    pub fn is_lagging(&self, strategy_id: &StrategyId) -> bool {
        self.read()
            .queues
            .get(strategy_id)
            .is_some_and(|q| q.lagging)
    }

    fn release_all(
        &self,
        inner: &mut Inner,
        strategy_id: &StrategyId,
        keys: &BTreeSet<SecurityKey>,
    ) -> Vec<SubscriptionChange> {
        let mut changes = Vec::new();
        for key in keys {
            // The id was validated at registration, so unsubscribe cannot
            // fail here; a key the registry no longer holds is NotSubscribed.
            if let Ok(change) = inner.registry.unsubscribe(strategy_id, key, &self.changes) {
                if change == SubscriptionChange::Closed {
                    self.feed.close(key);
                }
                changes.push(change);
            }
        }
        changes
    }

    /// Deregister every subscriber whose receiver was dropped. A note whose
    /// generation no longer matches the live queue belongs to an earlier
    /// registration (deregistered, or replaced by a re-registration) and is
    /// discarded.
    fn reap(&self, inner: &mut Inner) -> u32 {
        let notes = std::mem::take(
            &mut *self
                .departures
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner),
        );
        let mut reaped = 0;
        for (strategy_id, generation) in notes {
            let current = inner
                .queues
                .get(&strategy_id)
                .is_some_and(|queue| queue.generation == generation);
            if !current {
                continue;
            }
            if let Some(queue) = inner.queues.remove(&strategy_id) {
                self.release_all(inner, &strategy_id, &queue.keys);
                reaped += 1;
            }
        }
        reaped
    }

    /// The runtime lock, with departed subscribers already reaped, so no
    /// decision is ever made over a line a dropped receiver still holds.
    fn lock(&self) -> Result<MutexGuard<'_, Inner>, FanOutRuntimeError> {
        let mut inner = self.lock_unreaped()?;
        self.reap(&mut inner);
        Ok(inner)
    }

    fn lock_unreaped(&self) -> Result<MutexGuard<'_, Inner>, FanOutRuntimeError> {
        self.inner.lock().map_err(|_| FanOutRuntimeError::Poisoned)
    }

    /// Read-only access survives poisoning: reporting a count cannot make
    /// inconsistent state worse, and the operator still needs to see it. A
    /// healthy lock reaps first, so a count never includes a line held only
    /// by a dropped receiver.
    fn read(&self) -> MutexGuard<'_, Inner> {
        match self.lock() {
            Ok(inner) => inner,
            Err(_) => self
                .inner
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner),
        }
    }
}

/// The runtime is the live `SubscriptionLineCounter` the SRS-MD-002 gate
/// consults. A poisoned lock fails the probe closed (`ExceededLimit`).
impl<F, W, C, L> SubscriptionLineCounter for FanOutRuntime<F, W, C, L>
where
    F: UpstreamMarketDataFeed,
    W: RestartWindowGate + Send + Sync,
    C: SubscriptionChangeSink + Send + Sync,
    L: SubscriberLagSink,
{
    fn lines_in_use(&self) -> u32 {
        self.read().registry.distinct_subscriptions()
    }

    fn line_limit(&self) -> u32 {
        self.read().registry.line_limit()
    }

    fn try_acquire(&self, request: &SubscriptionRequest) -> SubscriptionLimitState {
        match self.lock() {
            Ok(inner) => inner.registry.try_acquire(request),
            Err(_) => SubscriptionLimitState::ExceededLimit,
        }
    }
}
