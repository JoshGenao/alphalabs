//! SRS-MD-004 - the shared line-freshness store the order path reads.
//!
//! [`HeartbeatFreshnessMonitor`] and [`SequenceGapDetector`] are clock-free,
//! single-threaded models: whoever owns them decides what time it is and who
//! may touch them. The order path cannot own them. Orders arrive on the live
//! host's connection threads while ticks arrive on the feed thread, so the
//! verdict an order is judged against has to live somewhere both can reach.
//! [`LineFreshnessBoard`] is that place.
//!
//! The rules it keeps, and why each one matters to a gate that lets orders out:
//!
//! * **One clock for writers and readers.** The board owns its
//!   [`FreshnessClock`]. An observation is stamped with it and a verdict is
//!   evaluated with it, so the two can never be in different clock domains. A
//!   reader evaluating on a clock that runs behind the writer's would compute a
//!   negative age, which the monitor clamps to zero: a dead line would read as
//!   "just updated" for as long as the gap between the clocks.
//! * **The verdict is computed when it is read.** Nothing caches "fresh". A
//!   line that stops delivering crosses the 15 s threshold at the instant the
//!   clock passes it, whether or not anyone evaluated in between.
//! * **No gap source is not an empty gap source.** A provider either supplies
//!   an upstream sequence (gap detection is possible) or it does not (IB's tick
//!   stream). [`GapTracking`] makes the operator of the board say which. An
//!   empty [`SequenceGapDetector`] reports every line `Stale` (it fails closed on
//!   lines it never saw), so silently pairing one with a sequence-less feed
//!   would block every order forever; silently skipping it for a sequenced feed
//!   would ignore every gap.
//! * **Fail closed everywhere.** An unwatched or never-observed line, an
//!   identity that cannot be canonicalized (an option without contract
//!   identity), and a poisoned lock all read `Stale` with no age.
//! * **No I/O under the lock.** The order path holds the lock for one map
//!   lookup. Gap events are published to their sink only after the lock is
//!   released, so a slow audit sink can never sit inside an order's latency
//!   budget (NFR-P1).

use std::cell::RefCell;
use std::fmt;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use atp_types::{
    AssetClass, HeartbeatFeed, MarketDataFreshness, MarketDataTick, SecurityKey, SequenceGapEvent,
};

use crate::{
    GapObservation, HeartbeatFreshnessMonitor, ResyncOutcome, SequenceGapDetector,
    SequenceGapEventSink, SequenceGapPublishError, SubscriptionRegistryError,
};

/// The time source a [`LineFreshnessBoard`] stamps observations with and
/// evaluates verdicts against.
///
/// Implementations must be **monotonic**: a reading must never be smaller than
/// an earlier one. The monitor clamps a negative age to zero, so a clock that
/// steps backwards would make every silent line read fresh until real time
/// caught up again. A wall clock (`SystemTime`) is not monotonic; production
/// binds a monotonic clock in the composition layer. Units are nanoseconds.
pub trait FreshnessClock: Send + Sync {
    fn now_ns(&self) -> i64;
}

/// Whether the provider feeding a [`LineFreshnessBoard`] carries an upstream
/// sequence that gap detection can read. Chosen when the board is built, so
/// the choice is explicit and cannot change under a running feed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GapTracking {
    /// The provider has no upstream sequence (IB's tick stream). A line's
    /// verdict is its time freshness alone.
    Unavailable,
    /// The provider supplies an upstream sequence (`MarketDataTick::tick_seq`
    /// from the provider's own feed, per SRS-MD-007). A line is stale if it
    /// is time-stale OR gap-stale
    /// ([`HeartbeatFreshnessMonitor::combined_line_freshness`]).
    Tracked,
}

/// One line's freshness at the instant it was read.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LineVerdict {
    /// The decision. `Stale` blocks orders.
    pub freshness: MarketDataFreshness,
    /// How long the line has been without trustworthy data, for display only.
    /// For a time-stale line it is the age of the last observation; for a
    /// gap-stale line it is the time since the gap opened (data after a gap is
    /// not trustworthy until recovery); when both apply, the larger. For a
    /// fresh line it is the age of the last observation. `None` when no age is
    /// known: the line was never observed, is not watched, cannot be named, or
    /// the board's state is unreadable.
    pub age: Option<Duration>,
}

impl LineVerdict {
    const UNKNOWN: Self = Self {
        freshness: MarketDataFreshness::Stale,
        age: None,
    };

    /// The age in whole seconds, rounded down, or `None` when unknown.
    pub fn age_seconds(&self) -> Option<u64> {
        self.age.map(|age| age.as_secs())
    }
}

#[derive(Debug)]
enum GapState {
    Unavailable,
    Tracked(SequenceGapDetector),
}

#[derive(Debug)]
struct BoardState {
    monitor: HeartbeatFreshnessMonitor,
    gaps: GapState,
}

/// Thread-safe, cloneable handle to one set of market-data line freshness
/// verdicts. Clones share the same state: the feed side writes through one
/// clone, the order side reads through another.
#[derive(Clone)]
pub struct LineFreshnessBoard {
    state: Arc<Mutex<BoardState>>,
    clock: Arc<dyn FreshnessClock>,
}

impl fmt::Debug for LineFreshnessBoard {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("LineFreshnessBoard")
            .field("poisoned", &self.is_poisoned())
            .finish_non_exhaustive()
    }
}

impl LineFreshnessBoard {
    /// An empty board: every line reads `Stale` until it is observed.
    pub fn new(clock: Arc<dyn FreshnessClock>, gap_tracking: GapTracking) -> Self {
        let gaps = match gap_tracking {
            GapTracking::Unavailable => GapState::Unavailable,
            GapTracking::Tracked => GapState::Tracked(SequenceGapDetector::new()),
        };
        Self {
            state: Arc::new(Mutex::new(BoardState {
                monitor: HeartbeatFreshnessMonitor::new(),
                gaps,
            })),
            clock,
        }
    }

    /// Which gap-tracking mode this board was built with.
    pub fn gap_tracking(&self) -> Option<GapTracking> {
        self.lock().map(|state| match state.gaps {
            GapState::Unavailable => GapTracking::Unavailable,
            GapState::Tracked(_) => GapTracking::Tracked,
        })
    }

    /// `true` once a thread panicked while holding the board's lock. A
    /// poisoned board reads every line `Stale` and drops every observation,
    /// so it blocks all orders until the process restarts. That is the safe
    /// direction; this accessor lets a health surface say why.
    pub fn is_poisoned(&self) -> bool {
        self.state.is_poisoned()
    }

    /// Register a subscribed line without an observation, so it reads
    /// `Stale` from the moment it is supposed to be flowing.
    pub fn watch(&self, key: SecurityKey) {
        if let Some(mut state) = self.lock() {
            state.monitor.watch_security(key);
        }
    }

    /// Record that `key`'s line delivered data now. This is the feed's entry
    /// point when the provider carries no upstream sequence: it says which
    /// line was fed and invents nothing. On a poisoned board the observation
    /// is dropped, and the line ages into `Stale`.
    pub fn observe_security(&self, key: SecurityKey) {
        if let Some(mut state) = self.lock() {
            let now_ns = self.clock.now_ns();
            state.monitor.observe_security(key, now_ns);
        }
    }

    /// Record one delivered tick: a time observation for its line, and, on a
    /// [`GapTracking::Tracked`] board, a sequence observation for the gap
    /// detector. Returns the gap detector's classification (`None` on an
    /// untracked board, where no sequence is read).
    ///
    /// A gap event is handed to `events` AFTER the lock is released; the
    /// returned [`GapObservation::Gap::published`] carries that real result.
    /// The line is already `Stale` by then, so a failing sink cannot make a
    /// gapped line tradable.
    ///
    /// Fails closed on a tick that cannot name a security, exactly as the
    /// monitor and detector do. A poisoned board drops the observation and
    /// returns `Ok(None)`: nothing was recorded, and the line ages into
    /// `Stale`.
    pub fn observe_tick<S: SequenceGapEventSink>(
        &self,
        tick: &MarketDataTick,
        events: &S,
    ) -> Result<Option<GapObservation>, SubscriptionRegistryError> {
        let pending = DeferredGapSink::default();
        let observation = {
            let Some(mut state) = self.lock() else {
                return Ok(None);
            };
            let now_ns = self.clock.now_ns();
            let BoardState { monitor, gaps } = &mut *state;
            monitor.observe_tick(tick, now_ns)?;
            match gaps {
                GapState::Unavailable => None,
                GapState::Tracked(detector) => Some(detector.observe_tick(tick, now_ns, &pending)?),
            }
        };
        Ok(observation.map(|observation| match observation {
            GapObservation::Gap {
                expected, observed, ..
            } => {
                let published = match pending.take() {
                    Some(event) => events.record(event),
                    None => Err(SequenceGapPublishError::new(
                        "the gap detector reported a gap without an event",
                    )),
                };
                GapObservation::Gap {
                    expected,
                    observed,
                    published,
                }
            }
            other => other,
        }))
    }

    /// SRS-MD-007 operator-acknowledged resync for a gap-stale line. On a
    /// board without gap tracking there is no gap state to clear, so it
    /// reports [`ResyncOutcome::NotTracked`]; a poisoned board does the same.
    pub fn acknowledge_resync(&self, key: &SecurityKey) -> ResyncOutcome {
        match self.lock() {
            Some(mut state) => match &mut state.gaps {
                GapState::Tracked(detector) => detector.acknowledge_resync(key),
                GapState::Unavailable => ResyncOutcome::NotTracked,
            },
            None => ResyncOutcome::NotTracked,
        }
    }

    /// The verdict for one canonical line, evaluated now.
    pub fn line_freshness(&self, key: &SecurityKey) -> LineVerdict {
        let Some(state) = self.lock() else {
            return LineVerdict::UNKNOWN;
        };
        // Read the clock while holding the lock, so no observation can be
        // stamped between this reading and the evaluation below.
        let now_ns = self.clock.now_ns();
        let feed = HeartbeatFeed::MarketData {
            symbol: key.symbol().to_string(),
            asset_class: key.asset_class(),
        };
        let observation_age = state
            .monitor
            .staleness_ms(&feed, now_ns)
            .map(Duration::from_millis);
        match &state.gaps {
            GapState::Unavailable => LineVerdict {
                freshness: state.monitor.freshness(&feed, now_ns),
                age: observation_age,
            },
            GapState::Tracked(detector) => {
                let freshness = state.monitor.combined_line_freshness(detector, key, now_ns);
                let gap_age = detector.stale_since_ns(key).map(|since_ns| {
                    Duration::from_nanos(now_ns.saturating_sub(since_ns).max(0) as u64)
                });
                let time_stale = state.monitor.freshness(&feed, now_ns).is_stale();
                let age = match (time_stale, gap_age) {
                    (true, Some(gap)) => observation_age.map(|age| age.max(gap)).or(Some(gap)),
                    (false, Some(gap)) => Some(gap),
                    (_, None) => observation_age,
                };
                LineVerdict { freshness, age }
            }
        }
    }

    /// The verdict for the line an order names, evaluated now. Canonicalizes
    /// `symbol` the way the subscription registry does (trimmed, upper-case);
    /// an identity that cannot be canonicalized (blank symbol, or an option
    /// whose full contract identity a [`SecurityKey`] does not yet carry)
    /// reads `Stale` with no age.
    pub fn freshness_for(&self, symbol: &str, asset_class: AssetClass) -> LineVerdict {
        match SecurityKey::new(symbol, asset_class) {
            Ok(key) => self.line_freshness(&key),
            Err(_) => LineVerdict::UNKNOWN,
        }
    }

    fn lock(&self) -> Option<MutexGuard<'_, BoardState>> {
        // A poisoned lock means a thread panicked mid-update; the state may be
        // half-written. Refuse to read it: callers fall through to `Stale`.
        self.state.lock().ok()
    }
}

/// Holds a gap event inside the lock so it can be published after release.
#[derive(Default)]
struct DeferredGapSink {
    event: RefCell<Option<SequenceGapEvent>>,
}

impl DeferredGapSink {
    fn take(&self) -> Option<SequenceGapEvent> {
        self.event.borrow_mut().take()
    }
}

impl SequenceGapEventSink for DeferredGapSink {
    fn record(&self, event: SequenceGapEvent) -> Result<(), SequenceGapPublishError> {
        *self.event.borrow_mut() = Some(event);
        Ok(())
    }
}
