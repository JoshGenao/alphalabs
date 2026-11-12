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
//! * **A refresh must prove new data arrived.** On a board that tracks gaps,
//!   only a tick whose sequence the detector accepts as new refreshes a
//!   line's age; a duplicate or replayed tick refreshes nothing, and a
//!   sequence-less observation is refused.
//! * **Fail closed everywhere.** An unwatched or never-observed line, an
//!   identity that cannot be canonicalized (an option without contract
//!   identity), a poisoned lock, and a clock that cannot read the time all
//!   read `Stale` with no age. A write
//!   that records nothing says so ([`BoardWriteError`]).
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
/// caught up again. They must also **keep counting while the host is
/// suspended**: `Instant` stops during sleep on macOS and Linux, so a line
/// observed before a long suspend would read seconds old on resume. A wall
/// clock (`SystemTime`) satisfies the second rule and not the first; the
/// production clock in the composition layer combines both. Units are
/// nanoseconds.
///
/// `None` means the clock cannot vouch for the current time (for example the
/// wall clock it needs to see a suspend is unreadable). The board then reads
/// every line `Stale` and refuses writes: an unknown time is never treated as
/// "no time has passed".
pub trait FreshnessClock: Send + Sync {
    fn now_ns(&self) -> Option<i64>;
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
    /// known: the line was never observed, is not watched, cannot be named, the
    /// board's state is unreadable, or its clock cannot read the time.
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

/// Why a write to a [`LineFreshnessBoard`] recorded nothing. Each case
/// leaves the line as it was, which ages it toward `Stale`; the caller is
/// told rather than left to assume the write landed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BoardWriteError {
    /// A thread panicked while holding the board's lock. The board reads
    /// every line `Stale` and accepts no writes until the process restarts.
    Poisoned,
    /// A sequence-less observation was offered to a board that tracks gaps;
    /// use `observe_tick`, whose sequence the gap detector can check.
    SequenceRequired,
    /// The tick could not name a security (blank symbol, or an option a
    /// `SecurityKey` cannot carry yet).
    Uncanonicalizable(SubscriptionRegistryError),
    /// The board's clock cannot vouch for the current time, so no observation
    /// can be stamped honestly.
    ClockUnavailable,
}

impl From<SubscriptionRegistryError> for BoardWriteError {
    fn from(error: SubscriptionRegistryError) -> Self {
        Self::Uncanonicalizable(error)
    }
}

impl fmt::Display for BoardWriteError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Poisoned => write!(
                formatter,
                "SRS-MD-004: the freshness board is poisoned; nothing was recorded"
            ),
            Self::SequenceRequired => write!(
                formatter,
                "SRS-MD-004: this board tracks sequence gaps; an observation needs a tick sequence"
            ),
            Self::Uncanonicalizable(error) => write!(
                formatter,
                "SRS-MD-004: the tick names no security ({error:?}); nothing was recorded"
            ),
            Self::ClockUnavailable => write!(
                formatter,
                "SRS-MD-004: the freshness clock cannot read the current time; nothing was recorded"
            ),
        }
    }
}

impl std::error::Error for BoardWriteError {}

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
    /// poisoned board reads every line `Stale` and refuses every write
    /// ([`BoardWriteError::Poisoned`]), so it blocks all orders until the
    /// process restarts. That is the safe
    /// direction; this accessor lets a health surface say why.
    pub fn is_poisoned(&self) -> bool {
        self.state.is_poisoned()
    }

    /// Register a subscribed line without an observation, so it reads
    /// `Stale` from the moment it is supposed to be flowing.
    pub fn watch(&self, key: SecurityKey) -> Result<(), BoardWriteError> {
        let mut state = self.lock().ok_or(BoardWriteError::Poisoned)?;
        state.monitor.watch_security(key);
        Ok(())
    }

    /// Record that `key`'s line delivered data now, for a provider that
    /// carries NO upstream sequence. It says which line was fed and invents
    /// nothing.
    ///
    /// Refused on a [`GapTracking::Tracked`] board: there, a refresh must come
    /// with a sequence the gap detector can check ([`observe_tick`]), or a
    /// caller could keep a line fresh with no proof that new data arrived.
    ///
    /// [`observe_tick`]: Self::observe_tick
    pub fn observe_security(&self, key: SecurityKey) -> Result<(), BoardWriteError> {
        let mut state = self.lock().ok_or(BoardWriteError::Poisoned)?;
        if matches!(state.gaps, GapState::Tracked(_)) {
            return Err(BoardWriteError::SequenceRequired);
        }
        let now_ns = self
            .clock
            .now_ns()
            .ok_or(BoardWriteError::ClockUnavailable)?;
        state.monitor.observe_security(key, now_ns);
        Ok(())
    }

    /// Record one delivered tick. Returns the gap detector's classification
    /// on a [`GapTracking::Tracked`] board, `None` on an untracked one (where
    /// no sequence is read).
    ///
    /// On an untracked board every tick is a time observation. On a tracked
    /// board the sequence is classified FIRST, and only a tick that carries
    /// new data refreshes the line's age: a baseline, an in-sequence tick, or
    /// a gap (which leaves the line gap-stale anyway). A duplicate or
    /// backwards tick ([`GapObservation::NonMonotonic`]) refreshes nothing, so
    /// a replayed tick can never keep a silent line tradable.
    ///
    /// A gap event is handed to `events` AFTER the lock is released; the
    /// returned [`GapObservation::Gap::published`] carries that real result.
    /// The line is already `Stale` by then, so a failing sink cannot make a
    /// gapped line tradable.
    pub fn observe_tick<S: SequenceGapEventSink>(
        &self,
        tick: &MarketDataTick,
        events: &S,
    ) -> Result<Option<GapObservation>, BoardWriteError> {
        let pending = DeferredGapSink::default();
        let observation = {
            let mut state = self.lock().ok_or(BoardWriteError::Poisoned)?;
            let now_ns = self
                .clock
                .now_ns()
                .ok_or(BoardWriteError::ClockUnavailable)?;
            let BoardState { monitor, gaps } = &mut *state;
            match gaps {
                GapState::Unavailable => {
                    monitor.observe_tick(tick, now_ns)?;
                    None
                }
                GapState::Tracked(detector) => {
                    let observation = detector.observe_tick(tick, now_ns, &pending)?;
                    if !matches!(observation, GapObservation::NonMonotonic { .. }) {
                        monitor.observe_tick(tick, now_ns)?;
                    }
                    Some(observation)
                }
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
    /// reports [`ResyncOutcome::NotTracked`].
    pub fn acknowledge_resync(&self, key: &SecurityKey) -> Result<ResyncOutcome, BoardWriteError> {
        let mut state = self.lock().ok_or(BoardWriteError::Poisoned)?;
        Ok(match &mut state.gaps {
            GapState::Tracked(detector) => detector.acknowledge_resync(key),
            GapState::Unavailable => ResyncOutcome::NotTracked,
        })
    }

    /// The verdict for one canonical line, evaluated now.
    pub fn line_freshness(&self, key: &SecurityKey) -> LineVerdict {
        let Some(state) = self.lock() else {
            return LineVerdict::UNKNOWN;
        };
        // Read the clock while holding the lock, so no observation can be
        // stamped between this reading and the evaluation below.
        let Some(now_ns) = self.clock.now_ns() else {
            return LineVerdict::UNKNOWN;
        };
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
