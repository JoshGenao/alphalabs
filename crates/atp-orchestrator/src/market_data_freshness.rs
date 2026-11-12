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

use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use atp_execution::MarketDataFreshnessProbe;
use atp_market_data::{FreshnessClock, GapTracking, LineFreshnessBoard};
use atp_types::{AssetClass, MarketDataFreshness};

/// The production freshness clock: epoch nanoseconds that never go backwards,
/// always advance at least as fast as real awake time, and DO advance while
/// the host is suspended.
///
/// Two sources, because neither is enough alone:
///
/// * the monotonic clock (`Instant`) never steps backwards, but on macOS
///   (`CLOCK_UPTIME_RAW`) and Linux (`CLOCK_MONOTONIC`) it stops while the
///   host sleeps, so a line observed before a five-minute suspend would read
///   as seconds old on resume and orders would pass on dead data;
/// * the wall clock (`SystemTime`) keeps counting through a suspend, but an
///   NTP correction or a manual change can step it in either direction.
///
/// The clock is LOGICAL: each reading advances it by the larger of the two
/// sources' deltas since the previous reading, where a wall delta that went
/// backwards counts as zero. So:
///
/// * a suspend advances it by the wall delta (silent lines go stale);
/// * a forward wall jump advances it by the jump (lines go stale sooner, the
///   safe direction);
/// * a backwards wall step, including the correction of an earlier forward
///   jump, advances it by the monotonic delta, so silent lines keep ageing.
///   Clamping ABSOLUTE readings instead would freeze the clock after a
///   corrected jump and read a silent line as age zero for as long as the
///   jump was (adversarial review round 2).
///
/// If the wall clock cannot be read, the monotonic delta alone is used.
///
/// Both the feed side (stamping observations) and the order side (evaluating
/// them) must read the SAME instance, which [`LineFreshnessBoard`] guarantees
/// by owning its clock.
pub struct MonotonicEpochClock {
    sources: Box<dyn ClockSources>,
    state: Mutex<ClockState>,
}

#[derive(Debug, Clone, Copy)]
struct ClockState {
    /// The logical reading handed out last, epoch nanoseconds.
    logical_ns: i64,
    last_monotonic: Duration,
    /// `None` until the wall clock has been read successfully.
    last_wall: Option<Duration>,
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
            sources,
            state: Mutex::new(ClockState {
                logical_ns: anchor_epoch_ns,
                last_monotonic: Duration::ZERO,
                last_wall: Some(Duration::ZERO),
            }),
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
            .field("state", &*self.lock_state())
            .finish_non_exhaustive()
    }
}

impl MonotonicEpochClock {
    fn lock_state(&self) -> MutexGuard<'_, ClockState> {
        // The state is three plain values assigned together after every
        // computation, so a panic elsewhere cannot leave it half-written;
        // recovering keeps the clock advancing rather than wedging every
        // board that reads it.
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

fn duration_ns(duration: Duration) -> i64 {
    i64::try_from(duration.as_nanos()).unwrap_or(i64::MAX)
}

impl FreshnessClock for MonotonicEpochClock {
    fn now_ns(&self) -> i64 {
        let mut state = self.lock_state();
        let monotonic = self.sources.monotonic_elapsed();
        let wall = self.sources.wall_elapsed();
        let monotonic_delta = monotonic.saturating_sub(state.last_monotonic);
        let wall_delta = match (wall, state.last_wall) {
            (Some(now), Some(before)) => now.saturating_sub(before),
            _ => Duration::ZERO,
        };
        state.logical_ns = state
            .logical_ns
            .saturating_add(duration_ns(monotonic_delta.max(wall_delta)));
        state.last_monotonic = monotonic;
        if wall.is_some() {
            state.last_wall = wall;
        }
        state.logical_ns
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
    fn a_corrected_forward_jump_never_goes_back_and_keeps_ageing() {
        let (clock, script) = scripted();
        script.set(secs(5), Some(secs(500)));
        let jumped = clock.now_ns();
        assert_eq!(
            jumped,
            ANCHOR_NS + duration_ns(secs(500)),
            "the jump ages lines faster"
        );
        // NTP corrects the wall clock back; 16 s of real time pass.
        script.set(secs(21), Some(secs(21)));
        assert_eq!(
            clock.now_ns(),
            jumped + duration_ns(secs(16)),
            "after the correction the clock keeps advancing by real time"
        );
    }

    #[test]
    fn a_corrected_forward_jump_cannot_keep_a_silent_line_fresh() {
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
        // A tick lands while the wall clock is 495 s ahead.
        script.set(secs(5), Some(secs(500)));
        board
            .observe_security(SecurityKey::new("AAPL", AssetClass::Equity).unwrap())
            .unwrap();
        // The wall clock is corrected; the line then stays silent for 16 s.
        script.set(secs(21), Some(secs(21)));
        assert_eq!(
            probe.freshness("AAPL", AssetClass::Equity),
            MarketDataFreshness::Stale,
            "16 s of silence is stale whatever the wall clock did"
        );
        assert_eq!(
            probe.staleness_seconds("AAPL", AssetClass::Equity),
            Some(16)
        );
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
