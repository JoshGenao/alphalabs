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
///   corrected jump (adversarial review round 2).
///
/// The wall clock is read as ABSOLUTE time since the Unix epoch, never as an
/// offset from when this clock was built: a correction to before start-up is
/// still a readable wall time, so a suspend after it is still seen (round 3).
/// If the wall clock cannot be read at all (set before 1970), the clock
/// returns `None`, because it could not see a suspend: the board then reads
/// every line `Stale` until the wall clock is readable again. Deltas resume
/// from the last good reading, so a suspend during the outage still counts.
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
    /// The last readable wall time since the epoch; `None` until one is read.
    last_wall: Option<Duration>,
}

/// The two readings the clock combines. A seam so tests can script a suspend
/// or a wall-clock step deterministically.
trait ClockSources: Send + Sync {
    /// Monotonic time since an arbitrary fixed origin.
    fn monotonic(&self) -> Duration;
    /// Wall-clock time since the Unix epoch; `None` when it cannot be read.
    fn wall_since_epoch(&self) -> Option<Duration>;
}

struct SystemSources {
    origin: Instant,
}

impl ClockSources for SystemSources {
    fn monotonic(&self) -> Duration {
        self.origin.elapsed()
    }

    fn wall_since_epoch(&self) -> Option<Duration> {
        SystemTime::now().duration_since(UNIX_EPOCH).ok()
    }
}

impl MonotonicEpochClock {
    pub fn new() -> Self {
        Self::with_sources(Box::new(SystemSources {
            origin: Instant::now(),
        }))
    }

    fn with_sources(sources: Box<dyn ClockSources>) -> Self {
        let wall = sources.wall_since_epoch();
        let state = ClockState {
            // The epoch anchor only keeps readings recognizable as times; no
            // decision depends on it. Ages come from deltas.
            logical_ns: wall.map_or(0, duration_ns),
            last_monotonic: sources.monotonic(),
            last_wall: wall,
        };
        Self {
            sources,
            state: Mutex::new(state),
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
    fn now_ns(&self) -> Option<i64> {
        let mut state = self.lock_state();
        // Unreadable wall time: this reading cannot see a suspend, so it
        // vouches for nothing. State is left alone, so the deltas taken when
        // the wall clock returns span the outage.
        let wall = self.sources.wall_since_epoch()?;
        let monotonic = self.sources.monotonic();
        let monotonic_delta = monotonic.saturating_sub(state.last_monotonic);
        let wall_delta = state
            .last_wall
            .map_or(Duration::ZERO, |before| wall.saturating_sub(before));
        state.logical_ns = state
            .logical_ns
            .saturating_add(duration_ns(monotonic_delta.max(wall_delta)));
        state.last_monotonic = monotonic;
        state.last_wall = Some(wall);
        Some(state.logical_ns)
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
    use atp_types::SecurityKey;

    /// 2025-06-15T00:00:00Z, the scripted wall clock's starting point.
    const BASE_S: i64 = 1_749_945_600;

    /// Scripted sources. Monotonic seconds since the script began, and wall
    /// seconds relative to `BASE_S` (negative = before it; `None` = unreadable).
    #[derive(Clone, Default)]
    struct Script(Arc<Mutex<(u64, Option<i64>)>>);

    impl Script {
        fn at(monotonic_s: u64, wall_rel_s: Option<i64>) -> Self {
            let script = Self::default();
            script.set(monotonic_s, wall_rel_s);
            script
        }
        fn set(&self, monotonic_s: u64, wall_rel_s: Option<i64>) {
            *self.0.lock().unwrap() = (monotonic_s, wall_rel_s);
        }
    }

    impl ClockSources for Script {
        fn monotonic(&self) -> Duration {
            Duration::from_secs(self.0.lock().unwrap().0)
        }
        fn wall_since_epoch(&self) -> Option<Duration> {
            let rel = self.0.lock().unwrap().1?;
            u64::try_from(BASE_S + rel).ok().map(Duration::from_secs)
        }
    }

    const S: i64 = 1_000_000_000;

    fn clock(script: &Script) -> MonotonicEpochClock {
        MonotonicEpochClock::with_sources(Box::new(script.clone()))
    }

    fn rig(script: &Script) -> (LineFreshnessBoard, BoardFreshnessProbe) {
        let board = LineFreshnessBoard::new(Arc::new(clock(script)), GapTracking::Unavailable);
        let probe = BoardFreshnessProbe::new(board.clone());
        (board, probe)
    }

    fn tick(board: &LineFreshnessBoard) {
        board
            .observe_security(SecurityKey::new("AAPL", AssetClass::Equity).unwrap())
            .unwrap();
    }

    fn aapl(probe: &BoardFreshnessProbe) -> (MarketDataFreshness, Option<u64>) {
        (
            probe.freshness("AAPL", AssetClass::Equity),
            probe.staleness_seconds("AAPL", AssetClass::Equity),
        )
    }

    #[test]
    fn readings_start_at_epoch_time() {
        let script = Script::at(0, Some(0));
        assert_eq!(clock(&script).now_ns(), Some(BASE_S * S));
    }

    #[test]
    fn a_host_suspend_advances_the_clock() {
        // The monotonic source stood still for a 5-minute sleep; the wall
        // clock kept counting. The reading follows the wall clock.
        let script = Script::at(0, Some(0));
        let clock = clock(&script);
        script.set(10, Some(10));
        assert_eq!(clock.now_ns(), Some((BASE_S + 10) * S));
        script.set(10, Some(310));
        assert_eq!(clock.now_ns(), Some((BASE_S + 310) * S));
    }

    #[test]
    fn a_backwards_wall_step_advances_by_monotonic_time() {
        let script = Script::at(0, Some(0));
        let clock = clock(&script);
        script.set(100, Some(100));
        let before = clock.now_ns().unwrap();
        script.set(101, Some(-3_600)); // NTP steps back an hour
        assert_eq!(clock.now_ns(), Some(before + S));
        script.set(102, Some(-3_599));
        assert_eq!(clock.now_ns(), Some(before + 2 * S));
    }

    #[test]
    fn a_corrected_forward_jump_never_goes_back_and_keeps_ageing() {
        let script = Script::at(0, Some(0));
        let clock = clock(&script);
        script.set(5, Some(500));
        let jumped = clock.now_ns().unwrap();
        assert_eq!(jumped, (BASE_S + 500) * S, "the jump ages lines faster");
        script.set(21, Some(21)); // corrected; 16 s of real time pass
        assert_eq!(clock.now_ns(), Some(jumped + 16 * S));
    }

    #[test]
    fn an_unreadable_wall_clock_vouches_for_nothing_and_resumes_across_the_outage() {
        let script = Script::at(0, Some(0));
        let clock = clock(&script);
        script.set(10, None);
        assert_eq!(clock.now_ns(), None);
        // Back after a 60 s suspend (monotonic moved 1 s): the outage counts.
        script.set(11, Some(70));
        assert_eq!(clock.now_ns(), Some((BASE_S + 70) * S));
    }

    #[test]
    fn a_suspend_makes_the_board_block_until_a_new_tick() {
        let script = Script::at(1, Some(1));
        let (board, probe) = rig(&script);
        tick(&board);
        assert_eq!(aapl(&probe).0, MarketDataFreshness::Fresh);
        script.set(2, Some(61)); // a one-minute sleep: only the wall moves
        assert_eq!(aapl(&probe), (MarketDataFreshness::Stale, Some(60)));
        tick(&board);
        assert_eq!(aapl(&probe).0, MarketDataFreshness::Fresh);
    }

    #[test]
    fn a_corrected_forward_jump_cannot_keep_a_silent_line_fresh() {
        let script = Script::at(0, Some(0));
        let (board, probe) = rig(&script);
        script.set(5, Some(500)); // a tick lands while the wall is 495 s ahead
        tick(&board);
        script.set(21, Some(21)); // corrected; then 16 s of silence
        assert_eq!(aapl(&probe), (MarketDataFreshness::Stale, Some(16)));
    }

    #[test]
    fn a_correction_before_start_up_then_a_suspend_still_blocks() {
        // Round 3: the wall clock is corrected to BEFORE the moment this clock
        // was built, a tick lands, then the host sleeps for a minute.
        let script = Script::at(0, Some(0));
        let (board, probe) = rig(&script);
        script.set(2, Some(-600));
        tick(&board);
        assert_eq!(aapl(&probe).0, MarketDataFreshness::Fresh);
        script.set(3, Some(-540)); // 60 s of wall time, 1 s of monotonic time
        assert_eq!(aapl(&probe), (MarketDataFreshness::Stale, Some(60)));
    }

    #[test]
    fn an_unreadable_wall_clock_blocks_every_order() {
        let script = Script::at(0, Some(0));
        let (board, probe) = rig(&script);
        tick(&board);
        script.set(1, None);
        assert_eq!(aapl(&probe), (MarketDataFreshness::Stale, None));
    }

    #[test]
    fn the_system_clock_never_goes_backwards_and_reads_as_epoch_time() {
        let clock = MonotonicEpochClock::new();
        let mut previous = clock.now_ns().expect("the host wall clock is readable");
        for _ in 0..10_000 {
            let now = clock.now_ns().unwrap();
            assert!(now >= previous, "the board clock went backwards");
            previous = now;
        }
        // After 2020-01-01: the anchor is an epoch reading, not zero.
        assert!(previous > 1_577_836_800_000_000_000);
    }
}
