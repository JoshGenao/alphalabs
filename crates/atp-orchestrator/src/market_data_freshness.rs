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

use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use atp_execution::MarketDataFreshnessProbe;
use atp_market_data::{FreshnessClock, GapTracking, LineFreshnessBoard};
use atp_types::{AssetClass, MarketDataFreshness};

/// The production freshness clock: epoch nanoseconds read from the operating
/// system's SUSPEND-INCLUSIVE monotonic clock.
///
/// A freshness age has to count every second the market data has been silent,
/// including seconds the host spent asleep. The obvious sources each fail one
/// way:
///
/// * `Instant` never steps backwards, but on macOS (`CLOCK_UPTIME_RAW`) and
///   Linux (`CLOCK_MONOTONIC`) it stops while the host sleeps, so a line
///   observed before a five-minute suspend reads seconds old on resume;
/// * the wall clock (`SystemTime`) counts through a suspend, but an NTP
///   correction or a manual change can step it in either direction, and a
///   suspend combined with a backward correction can cancel out by any amount.
///   Review rounds 2-5 found four such combinations in a clock rebuilt from
///   the two; none of them can be closed from those sources.
///
/// So the clock reads the one source that has both properties: `CLOCK_BOOTTIME`
/// on Linux and `CLOCK_MONOTONIC` on macOS (which, unlike Linux's, counts
/// through sleep), via `rustix` because the workspace forbids the unsafe code a
/// raw `libc` call needs. It never steps backwards, no wall-clock change moves
/// it, and a suspend advances it by the time slept.
///
/// The epoch reading taken at construction only keeps values recognizable as
/// times; no decision depends on it. On a platform without such a clock the
/// clock returns `None` on every reading, and the board reads every line
/// `Stale` and refuses writes (fail closed).
///
/// Both the feed side (stamping observations) and the order side (evaluating
/// them) must read the SAME instance, which [`LineFreshnessBoard`] guarantees
/// by owning its clock.
pub struct MonotonicEpochClock {
    anchor_epoch_ns: i64,
    /// The suspend-inclusive reading at construction; `None` = unsupported.
    anchor: Option<Duration>,
    source: Box<dyn SuspendInclusiveSource>,
}

/// A monotonic time source that keeps counting while the host is suspended.
/// A seam so tests can script elapsed time deterministically.
trait SuspendInclusiveSource: Send + Sync {
    /// Time since an arbitrary fixed origin; `None` when unavailable.
    fn now(&self) -> Option<Duration>;
}

/// The operating system's suspend-inclusive monotonic clock.
struct OsBootClock;

impl SuspendInclusiveSource for OsBootClock {
    fn now(&self) -> Option<Duration> {
        let reading = rustix::time::clock_gettime(platform_clock_id()?);
        let secs = u64::try_from(reading.tv_sec).ok()?;
        let nanos = u32::try_from(reading.tv_nsec).ok()?;
        (nanos < 1_000_000_000).then(|| Duration::new(secs, nanos))
    }
}

/// The clock id that counts through suspend on this platform, or `None` where
/// no supported one exists.
#[cfg(any(target_os = "linux", target_os = "android"))]
fn platform_clock_id() -> Option<rustix::time::ClockId> {
    Some(rustix::time::ClockId::Boottime)
}

/// The clock id that counts through suspend on this platform, or `None` where
/// no supported one exists. Darwin's `CLOCK_MONOTONIC` counts through sleep
/// (its `CLOCK_UPTIME_RAW`, which `Instant` uses, does not).
#[cfg(target_vendor = "apple")]
fn platform_clock_id() -> Option<rustix::time::ClockId> {
    Some(rustix::time::ClockId::Monotonic)
}

/// No supported suspend-inclusive clock on this platform: fail closed.
#[cfg(not(any(target_os = "linux", target_os = "android", target_vendor = "apple")))]
fn platform_clock_id() -> Option<rustix::time::ClockId> {
    None
}

impl MonotonicEpochClock {
    pub fn new() -> Self {
        Self::with_source(Box::new(OsBootClock))
    }

    fn with_source(source: Box<dyn SuspendInclusiveSource>) -> Self {
        let anchor_epoch_ns = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(duration_ns)
            .unwrap_or(0);
        Self {
            anchor_epoch_ns,
            anchor: source.now(),
            source,
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
            .field("anchor", &self.anchor)
            .finish_non_exhaustive()
    }
}

fn duration_ns(duration: Duration) -> i64 {
    i64::try_from(duration.as_nanos()).unwrap_or(i64::MAX)
}

impl FreshnessClock for MonotonicEpochClock {
    fn now_ns(&self) -> Option<i64> {
        // `checked_sub`: the source is monotonic, so a reading below the
        // anchor means the source is broken, and a broken clock vouches for
        // nothing.
        let elapsed = self.source.now()?.checked_sub(self.anchor?)?;
        Some(self.anchor_epoch_ns.saturating_add(duration_ns(elapsed)))
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
    use std::sync::Mutex;

    /// Scripted suspend-inclusive time in seconds (`None` = unavailable).
    #[derive(Clone, Default)]
    struct Script(Arc<Mutex<Option<u64>>>);

    impl Script {
        fn at(secs: Option<u64>) -> Self {
            let script = Self::default();
            script.set(secs);
            script
        }
        fn set(&self, secs: Option<u64>) {
            *self.0.lock().unwrap() = secs;
        }
    }

    impl SuspendInclusiveSource for Script {
        fn now(&self) -> Option<Duration> {
            self.0.lock().unwrap().map(Duration::from_secs)
        }
    }

    fn rig(script: &Script) -> (LineFreshnessBoard, BoardFreshnessProbe) {
        let clock = MonotonicEpochClock::with_source(Box::new(script.clone()));
        let board = LineFreshnessBoard::new(Arc::new(clock), GapTracking::Unavailable);
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
    fn the_platform_clock_counts_through_suspend() {
        // Instant's clocks (CLOCK_MONOTONIC on Linux, CLOCK_UPTIME_RAW on
        // macOS) stop during sleep; these do not.
        #[cfg(any(target_os = "linux", target_os = "android"))]
        assert_eq!(platform_clock_id(), Some(rustix::time::ClockId::Boottime));
        #[cfg(target_vendor = "apple")]
        assert_eq!(platform_clock_id(), Some(rustix::time::ClockId::Monotonic));
    }

    #[test]
    fn a_suspend_makes_the_board_block_until_a_new_tick() {
        // The source counts through sleep, so a one-minute suspend is simply
        // sixty seconds of silence.
        let script = Script::at(Some(100));
        let (board, probe) = rig(&script);
        tick(&board);
        assert_eq!(aapl(&probe), (MarketDataFreshness::Fresh, Some(0)));
        script.set(Some(160));
        assert_eq!(aapl(&probe), (MarketDataFreshness::Stale, Some(60)));
        tick(&board);
        assert_eq!(aapl(&probe).0, MarketDataFreshness::Fresh);
    }

    #[test]
    fn the_threshold_is_crossed_by_suspend_inclusive_time_alone() {
        let script = Script::at(Some(0));
        let (board, probe) = rig(&script);
        tick(&board);
        script.set(Some(15));
        assert_eq!(aapl(&probe).0, MarketDataFreshness::Fresh);
        script.set(Some(16));
        assert_eq!(aapl(&probe), (MarketDataFreshness::Stale, Some(16)));
    }

    #[test]
    fn an_unavailable_clock_blocks_every_order_and_refuses_writes() {
        let script = Script::at(Some(0));
        let (board, probe) = rig(&script);
        tick(&board);
        script.set(None);
        assert_eq!(aapl(&probe), (MarketDataFreshness::Stale, None));
        assert_eq!(
            board.observe_security(SecurityKey::new("AAPL", AssetClass::Equity).unwrap()),
            Err(atp_market_data::BoardWriteError::ClockUnavailable)
        );
    }

    #[test]
    fn a_clock_with_no_anchor_never_vouches_for_time() {
        // A platform without a suspend-inclusive clock: unavailable at
        // construction, so every reading is None even if a later read works.
        let script = Script::at(None);
        let clock = MonotonicEpochClock::with_source(Box::new(script.clone()));
        script.set(Some(5));
        assert_eq!(clock.now_ns(), None);
    }

    #[test]
    fn a_reading_below_the_anchor_vouches_for_nothing() {
        let script = Script::at(Some(100));
        let clock = MonotonicEpochClock::with_source(Box::new(script.clone()));
        script.set(Some(99));
        assert_eq!(clock.now_ns(), None);
    }

    #[test]
    fn the_system_clock_is_readable_never_goes_backwards_and_reads_as_epoch_time() {
        let clock = MonotonicEpochClock::new();
        let mut previous = clock
            .now_ns()
            .expect("this platform has a suspend-inclusive clock");
        for _ in 0..10_000 {
            let now = clock.now_ns().unwrap();
            assert!(now >= previous, "the board clock went backwards");
            previous = now;
        }
        // After 2020-01-01: the anchor is an epoch reading, not zero.
        assert!(previous > 1_577_836_800_000_000_000);
    }
}
