//! SRS-MD-004 — the production market-data freshness probe.
//!
//! `atp-execution` declares the [`MarketDataFreshnessProbe`] port and
//! `atp-market-data` owns the freshness state ([`LineFreshnessBoard`]); neither
//! may see the other (SRS-ARCH-002). This composition layer binds them:
//! [`BoardFreshnessProbe`] answers the execution gate from the board, so a
//! live or paper order is judged against the same verdict the feed keeps.
//!
//! [`MonotonicEpochClock`] is the clock a production board is built with. The
//! board stamps observations and evaluates verdicts on one clock, and that
//! clock must never step backwards and must keep counting while the host is
//! suspended (see [`FreshnessClock`]).
//!
//! What is NOT here: feeding the board from the live IB tick stream. The
//! SRS-MD-003 feed loop (`atp_market_data::live_feed::LiveFeedLoop`) keeps its
//! own monitor today; composing it into a shared board inside the live
//! execution host is the host's IB-tier wiring, owned by SRS-EXE-001.

use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use atp_execution::MarketDataFreshnessProbe;
use atp_market_data::{FreshnessClock, GapTracking, LineFreshnessBoard};
use atp_types::{AssetClass, MarketDataFreshness};

/// The production freshness clock: epoch nanoseconds that never go backwards
/// and DO advance while the host is suspended.
///
/// Two sources, because neither is enough alone:
///
/// * the monotonic clock (`Instant`) never steps backwards, but on macOS
///   (`CLOCK_UPTIME_RAW`) and Linux (`CLOCK_MONOTONIC`) it stops while the
///   host sleeps, so a line observed before a five-minute suspend would read
///   as seconds old on resume and orders would pass on dead data;
/// * the wall clock (`SystemTime`) keeps counting through a suspend, but an
///   NTP correction or a manual change can step it backwards, which would
///   make a silent line's age negative (clamped to zero) and read it fresh.
///
/// Each reading takes the LARGER of the two elapsed times since construction
/// and never returns less than the previous reading. A suspend or a forward
/// wall-clock jump can therefore only age lines faster (they go stale sooner,
/// the safe direction); a backwards wall-clock step changes nothing, because
/// the monotonic elapsed time then dominates. If the wall clock cannot be
/// read, the monotonic source alone is used.
///
/// Both the feed side (stamping observations) and the order side (evaluating
/// them) must read the SAME instance, which [`LineFreshnessBoard`] guarantees
/// by owning its clock.
pub struct MonotonicEpochClock {
    anchor_epoch_ns: i64,
    sources: Box<dyn ClockSources>,
    high_water_ns: AtomicI64,
}

/// The two elapsed-time readings the clock combines. A seam so tests can
/// script a suspend or a wall-clock step deterministically.
trait ClockSources: Send + Sync {
    /// Monotonic time since construction.
    fn monotonic_elapsed(&self) -> Duration;
    /// Wall-clock time since construction; `None` when it went backwards past
    /// the anchor or cannot be read.
    fn wall_elapsed(&self) -> Option<Duration>;
}

struct SystemSources {
    anchor: Instant,
    anchor_wall: SystemTime,
}

impl ClockSources for SystemSources {
    fn monotonic_elapsed(&self) -> Duration {
        self.anchor.elapsed()
    }

    fn wall_elapsed(&self) -> Option<Duration> {
        SystemTime::now().duration_since(self.anchor_wall).ok()
    }
}

impl MonotonicEpochClock {
    pub fn new() -> Self {
        let anchor_wall = SystemTime::now();
        let anchor_epoch_ns = anchor_wall
            .duration_since(UNIX_EPOCH)
            .map(duration_ns)
            // A wall clock set before 1970 gives a zero anchor. Ages stay
            // correct, because they come from elapsed time.
            .unwrap_or(0);
        Self::with_sources(
            anchor_epoch_ns,
            Box::new(SystemSources {
                anchor: Instant::now(),
                anchor_wall,
            }),
        )
    }

    fn with_sources(anchor_epoch_ns: i64, sources: Box<dyn ClockSources>) -> Self {
        Self {
            anchor_epoch_ns,
            sources,
            high_water_ns: AtomicI64::new(anchor_epoch_ns),
        }
    }
}

impl Default for MonotonicEpochClock {
    fn default() -> Self {
        Self::new()
    }
}

impl std::fmt::Debug for MonotonicEpochClock {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("MonotonicEpochClock")
            .field("anchor_epoch_ns", &self.anchor_epoch_ns)
            .field("high_water_ns", &self.high_water_ns.load(Ordering::SeqCst))
            .finish_non_exhaustive()
    }
}

fn duration_ns(duration: Duration) -> i64 {
    i64::try_from(duration.as_nanos()).unwrap_or(i64::MAX)
}

impl FreshnessClock for MonotonicEpochClock {
    fn now_ns(&self) -> i64 {
        let monotonic = self.sources.monotonic_elapsed();
        let elapsed = self
            .sources
            .wall_elapsed()
            .map_or(monotonic, |wall| wall.max(monotonic));
        let candidate = self.anchor_epoch_ns.saturating_add(duration_ns(elapsed));
        // Never below an earlier reading: a wall clock that jumped forward and
        // then was corrected back must not hand out a smaller value.
        let previous = self.high_water_ns.fetch_max(candidate, Ordering::SeqCst);
        previous.max(candidate)
    }
}

/// A production [`LineFreshnessBoard`] on a [`MonotonicEpochClock`].
pub fn production_board(gap_tracking: GapTracking) -> LineFreshnessBoard {
    LineFreshnessBoard::new(Arc::new(MonotonicEpochClock::new()), gap_tracking)
}

/// The execution gate's [`MarketDataFreshnessProbe`], answered from a shared
/// [`LineFreshnessBoard`]. Cloning shares the board.
///
/// Every call reads the board at that instant; nothing is cached, so a line
/// that goes silent blocks the next order after it crosses the threshold and
/// a line that recovers admits the next order after its tick lands.
#[derive(Debug, Clone)]
pub struct BoardFreshnessProbe {
    board: LineFreshnessBoard,
}

impl BoardFreshnessProbe {
    pub fn new(board: LineFreshnessBoard) -> Self {
        Self { board }
    }

    pub fn board(&self) -> &LineFreshnessBoard {
        &self.board
    }
}

impl MarketDataFreshnessProbe for BoardFreshnessProbe {
    fn freshness(&self, symbol: &str, asset_class: AssetClass) -> MarketDataFreshness {
        self.board.freshness_for(symbol, asset_class).freshness
    }

    fn staleness_seconds(&self, symbol: &str, asset_class: AssetClass) -> Option<u64> {
        self.board.freshness_for(symbol, asset_class).age_seconds()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    const ANCHOR_NS: i64 = 1_750_000_000_000_000_000;
    const SECOND: Duration = Duration::from_secs(1);

    /// Scripted sources: the test sets what each clock says has elapsed.
    #[derive(Clone, Default)]
    struct Script(Arc<Mutex<(Duration, Option<Duration>)>>);

    impl Script {
        fn set(&self, monotonic: Duration, wall: Option<Duration>) {
            *self.0.lock().unwrap() = (monotonic, wall);
        }
    }

    impl ClockSources for Script {
        fn monotonic_elapsed(&self) -> Duration {
            self.0.lock().unwrap().0
        }
        fn wall_elapsed(&self) -> Option<Duration> {
            self.0.lock().unwrap().1
        }
    }

    fn scripted() -> (MonotonicEpochClock, Script) {
        let script = Script::default();
        let clock = MonotonicEpochClock::with_sources(ANCHOR_NS, Box::new(script.clone()));
        (clock, script)
    }

    fn secs(n: u64) -> Duration {
        SECOND * u32::try_from(n).unwrap()
    }

    #[test]
    fn a_host_suspend_advances_the_clock_so_a_silent_line_goes_stale() {
        // The monotonic source stood still for a 5-minute sleep; the wall
        // clock kept counting. The reading follows the wall clock.
        let (clock, script) = scripted();
        script.set(secs(10), Some(secs(10)));
        assert_eq!(clock.now_ns(), ANCHOR_NS + duration_ns(secs(10)));
        script.set(secs(10), Some(secs(310)));
        assert_eq!(clock.now_ns(), ANCHOR_NS + duration_ns(secs(310)));
    }

    #[test]
    fn a_backwards_wall_clock_step_does_not_move_the_clock_back() {
        let (clock, script) = scripted();
        script.set(secs(100), Some(secs(100)));
        let before = clock.now_ns();
        // NTP steps the wall clock back an hour: the monotonic time rules.
        script.set(secs(101), None);
        assert_eq!(clock.now_ns(), before + duration_ns(SECOND));
        script.set(secs(102), Some(secs(40)));
        assert_eq!(clock.now_ns(), before + duration_ns(secs(2)));
    }

    #[test]
    fn a_corrected_forward_jump_never_hands_out_a_smaller_reading() {
        let (clock, script) = scripted();
        script.set(secs(5), Some(secs(500)));
        let jumped = clock.now_ns();
        script.set(secs(6), Some(secs(6)));
        assert_eq!(clock.now_ns(), jumped, "readings are non-decreasing");
    }

    #[test]
    fn a_suspend_makes_the_production_board_block_until_a_new_tick() {
        use atp_types::SecurityKey;
        let script = Script::default();
        let board = LineFreshnessBoard::new(
            Arc::new(MonotonicEpochClock::with_sources(
                ANCHOR_NS,
                Box::new(script.clone()),
            )),
            GapTracking::Unavailable,
        );
        let probe = BoardFreshnessProbe::new(board.clone());
        let aapl = SecurityKey::new("AAPL", AssetClass::Equity).unwrap();
        script.set(secs(1), Some(secs(1)));
        board.observe_security(aapl.clone()).unwrap();
        assert_eq!(
            probe.freshness("AAPL", AssetClass::Equity),
            MarketDataFreshness::Fresh
        );
        // The host sleeps for a minute: only the wall clock moves.
        script.set(secs(2), Some(secs(61)));
        assert_eq!(
            probe.freshness("AAPL", AssetClass::Equity),
            MarketDataFreshness::Stale
        );
        assert_eq!(
            probe.staleness_seconds("AAPL", AssetClass::Equity),
            Some(60)
        );
        board.observe_security(aapl).unwrap();
        assert_eq!(
            probe.freshness("AAPL", AssetClass::Equity),
            MarketDataFreshness::Fresh
        );
    }

    #[test]
    fn the_system_clock_never_goes_backwards_and_reads_as_epoch_time() {
        let clock = MonotonicEpochClock::new();
        let mut previous = clock.now_ns();
        for _ in 0..10_000 {
            let now = clock.now_ns();
            assert!(now >= previous, "the board clock went backwards");
            previous = now;
        }
        // After 2020-01-01: the anchor is an epoch reading, not zero.
        assert!(previous > 1_577_836_800_000_000_000);
    }
}
