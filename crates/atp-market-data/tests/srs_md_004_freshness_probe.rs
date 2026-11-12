//! SRS-MD-004 contract obligation C1 - the production freshness verdict.
//!
//! "Given a subscribed instrument whose combined line freshness from
//! HeartbeatFreshnessMonitor is older than the 15 s staleness threshold, the
//! production probe reports it stale; after a tick newer than the threshold it
//! reports it fresh. Exercised with an injected clock, no sleeps."
//!
//! The probe's decision lives in [`LineFreshnessBoard::freshness_for`]; the
//! orchestrator's `BoardFreshnessProbe` only forwards it to the execution port.
//! Every test drives a manual clock, so each boundary is exact to the
//! nanosecond and nothing sleeps.

use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use atp_market_data::{
    BoardWriteError, FreshnessClock, GapObservation, GapTracking, LineFreshnessBoard,
    ResyncOutcome, SequenceGapEventSink, SequenceGapPublishError,
};
use atp_types::{AssetClass, MarketDataFreshness, MarketDataTick, SecurityKey, SequenceGapEvent};

const SECOND_NS: i64 = 1_000_000_000;
const THRESHOLD_NS: i64 = 15 * SECOND_NS;
const START_NS: i64 = 1_700_000_000 * SECOND_NS;

/// A clock the test sets by hand. `panic_on_read` lets a test poison the
/// board's lock the way a real fault would: a panic while the lock is held.
#[derive(Default)]
struct ManualClock {
    now_ns: AtomicI64,
    panic_on_read: AtomicBool,
    unavailable: AtomicBool,
}

impl ManualClock {
    fn at(now_ns: i64) -> Arc<Self> {
        let clock = Arc::new(Self::default());
        clock.set(now_ns);
        clock
    }

    fn set(&self, now_ns: i64) {
        self.now_ns.store(now_ns, Ordering::SeqCst);
    }
}

impl FreshnessClock for ManualClock {
    fn now_ns(&self) -> Option<i64> {
        assert!(
            !self.panic_on_read.load(Ordering::SeqCst),
            "injected clock fault"
        );
        if self.unavailable.load(Ordering::SeqCst) {
            return None;
        }
        Some(self.now_ns.load(Ordering::SeqCst))
    }
}

#[derive(Default)]
struct RecordingGapSink {
    events: Mutex<Vec<SequenceGapEvent>>,
    fail: bool,
}

impl SequenceGapEventSink for RecordingGapSink {
    fn record(&self, event: SequenceGapEvent) -> Result<(), SequenceGapPublishError> {
        self.events.lock().unwrap().push(event);
        if self.fail {
            Err(SequenceGapPublishError::new("audit store unavailable"))
        } else {
            Ok(())
        }
    }
}

fn board(clock: &Arc<ManualClock>, gaps: GapTracking) -> LineFreshnessBoard {
    LineFreshnessBoard::new(clock.clone(), gaps)
}

fn aapl() -> SecurityKey {
    SecurityKey::new("AAPL", AssetClass::Equity).unwrap()
}

fn tick(symbol: &str, tick_seq: u64) -> MarketDataTick {
    MarketDataTick {
        symbol: symbol.to_string(),
        asset_class: AssetClass::Equity,
        tick_seq,
    }
}

fn verdict(board: &LineFreshnessBoard) -> MarketDataFreshness {
    board.freshness_for("AAPL", AssetClass::Equity).freshness
}

#[test]
fn c1_a_watched_line_that_never_ticked_is_stale_with_no_age() {
    let clock = ManualClock::at(START_NS);
    let board = board(&clock, GapTracking::Unavailable);
    board.watch(aapl()).unwrap();

    let read = board.freshness_for("AAPL", AssetClass::Equity);
    assert_eq!(read.freshness, MarketDataFreshness::Stale);
    assert_eq!(read.age, None, "a never-observed line has no age to report");
    assert_eq!(read.age_seconds(), None);
}

#[test]
fn c1_a_line_older_than_15_seconds_is_stale_and_a_new_tick_makes_it_fresh() {
    let clock = ManualClock::at(START_NS);
    let board = board(&clock, GapTracking::Unavailable);
    board.observe_security(aapl()).unwrap();

    // Exactly 15.000 s is still fresh: the threshold is "over 15 seconds".
    clock.set(START_NS + THRESHOLD_NS);
    assert_eq!(verdict(&board), MarketDataFreshness::Fresh);

    // One nanosecond more is stale, and the age is reported.
    clock.set(START_NS + THRESHOLD_NS + 1);
    let stale = board.freshness_for("AAPL", AssetClass::Equity);
    assert_eq!(stale.freshness, MarketDataFreshness::Stale);
    assert_eq!(stale.age_seconds(), Some(15));

    // Much later it is still stale: nothing cached a verdict.
    clock.set(START_NS + 40 * SECOND_NS);
    let older = board.freshness_for("AAPL", AssetClass::Equity);
    assert_eq!(older.freshness, MarketDataFreshness::Stale);
    assert_eq!(older.age_seconds(), Some(40));

    // A tick newer than the threshold resumes the line at once.
    board.observe_security(aapl()).unwrap();
    let fresh = board.freshness_for("AAPL", AssetClass::Equity);
    assert_eq!(fresh.freshness, MarketDataFreshness::Fresh);
    assert_eq!(fresh.age, Some(Duration::ZERO));
}

#[test]
fn c1_the_order_symbol_is_canonicalized_like_the_subscription_registry() {
    let clock = ManualClock::at(START_NS);
    let board = board(&clock, GapTracking::Unavailable);
    board.observe_security(aapl()).unwrap();

    for spelling in ["AAPL", "aapl", "  Aapl "] {
        assert_eq!(
            board.freshness_for(spelling, AssetClass::Equity).freshness,
            MarketDataFreshness::Fresh,
            "`{spelling}` names the AAPL line"
        );
    }
    // A different line is not made fresh by AAPL's ticks.
    let msft = board.freshness_for("MSFT", AssetClass::Equity);
    assert_eq!(msft.freshness, MarketDataFreshness::Stale);
    assert_eq!(msft.age, None);
}

#[test]
fn c1_identities_the_board_cannot_name_fail_closed() {
    let clock = ManualClock::at(START_NS);
    let board = board(&clock, GapTracking::Unavailable);
    board.observe_security(aapl()).unwrap();

    // Fresh AAPL equity data says nothing about an AAPL option's quotes.
    let option = board.freshness_for("AAPL", AssetClass::Option);
    assert_eq!(option.freshness, MarketDataFreshness::Stale);
    assert_eq!(option.age, None);

    // A composite leg's contract key, as the multi-leg gate passes it.
    let leg = board.freshness_for("AAPL:20261218:C:15000", AssetClass::Option);
    assert_eq!(leg.freshness, MarketDataFreshness::Stale);

    let blank = board.freshness_for("   ", AssetClass::Equity);
    assert_eq!(blank.freshness, MarketDataFreshness::Stale);
    assert_eq!(blank.age, None);
}

#[test]
fn c1_combined_line_freshness_a_gap_makes_a_time_fresh_line_stale_until_recovery() {
    let clock = ManualClock::at(START_NS);
    let board = board(&clock, GapTracking::Tracked);
    assert_eq!(board.gap_tracking(), Some(GapTracking::Tracked));
    let sink = RecordingGapSink::default();

    assert!(matches!(
        board.observe_tick(&tick("AAPL", 1), &sink),
        Ok(Some(GapObservation::Baseline))
    ));
    assert_eq!(verdict(&board), MarketDataFreshness::Fresh);

    // Sequence 2 and 3 are lost: the line is gap-stale although it just ticked.
    clock.set(START_NS + SECOND_NS);
    let gap = board.observe_tick(&tick("AAPL", 4), &sink).unwrap();
    assert!(matches!(
        gap,
        Some(GapObservation::Gap {
            expected: 2,
            observed: 4,
            published: Ok(())
        })
    ));
    assert_eq!(
        sink.events.lock().unwrap().len(),
        1,
        "the gap was published"
    );
    clock.set(START_NS + 3 * SECOND_NS);
    let gapped = board.freshness_for("AAPL", AssetClass::Equity);
    assert_eq!(gapped.freshness, MarketDataFreshness::Stale);
    assert_eq!(
        gapped.age_seconds(),
        Some(2),
        "a gap-stale line ages from the gap onset"
    );

    // A fresh monotonic tick is the SRS-MD-007 recovery condition.
    assert!(matches!(
        board.observe_tick(&tick("AAPL", 5), &sink),
        Ok(Some(GapObservation::InSequence { recovered: true }))
    ));
    assert_eq!(verdict(&board), MarketDataFreshness::Fresh);

    // And the time half still applies on a tracked board.
    clock.set(START_NS + 3 * SECOND_NS + THRESHOLD_NS + 1);
    assert_eq!(verdict(&board), MarketDataFreshness::Stale);
}

#[test]
fn c1_operator_resync_recovers_a_gap_stale_line() {
    let clock = ManualClock::at(START_NS);
    let board = board(&clock, GapTracking::Tracked);
    let sink = RecordingGapSink::default();
    board.observe_tick(&tick("AAPL", 1), &sink).unwrap();
    board.observe_tick(&tick("AAPL", 9), &sink).unwrap();
    assert_eq!(verdict(&board), MarketDataFreshness::Stale);

    assert_eq!(
        board.acknowledge_resync(&aapl()),
        Ok(ResyncOutcome::Acknowledged)
    );
    assert_eq!(verdict(&board), MarketDataFreshness::Fresh);
}

#[test]
fn c1_a_failing_gap_sink_is_reported_and_the_line_stays_stale() {
    let clock = ManualClock::at(START_NS);
    let board = board(&clock, GapTracking::Tracked);
    let sink = RecordingGapSink {
        fail: true,
        ..RecordingGapSink::default()
    };
    board.observe_tick(&tick("AAPL", 1), &sink).unwrap();
    let gap = board.observe_tick(&tick("AAPL", 3), &sink).unwrap();
    assert!(
        matches!(
            gap,
            Some(GapObservation::Gap {
                published: Err(_),
                ..
            })
        ),
        "the real sink's failure is returned, not the buffer's success"
    );
    assert_eq!(verdict(&board), MarketDataFreshness::Stale);
}

#[test]
fn c1_without_gap_tracking_ticks_are_time_observations_only() {
    // IB carries no upstream sequence. A board built for it must not read
    // every line stale (an empty gap detector would), and must not invent a
    // gap from a sequence the provider never supplied.
    let clock = ManualClock::at(START_NS);
    let board = board(&clock, GapTracking::Unavailable);
    let sink = RecordingGapSink::default();
    assert!(matches!(
        board.observe_tick(&tick("AAPL", 1), &sink),
        Ok(None)
    ));
    assert!(matches!(
        board.observe_tick(&tick("AAPL", 50), &sink),
        Ok(None)
    ));
    assert!(sink.events.lock().unwrap().is_empty());
    assert_eq!(verdict(&board), MarketDataFreshness::Fresh);
    assert_eq!(
        board.acknowledge_resync(&aapl()),
        Ok(ResyncOutcome::NotTracked)
    );
}

#[test]
fn c1_a_poisoned_board_reads_every_line_stale_and_drops_observations() {
    let clock = ManualClock::at(START_NS);
    let board = board(&clock, GapTracking::Unavailable);
    board.observe_security(aapl()).unwrap();
    assert_eq!(verdict(&board), MarketDataFreshness::Fresh);

    // A panic while the lock is held poisons it.
    clock.panic_on_read.store(true, Ordering::SeqCst);
    let writer = board.clone();
    assert!(catch_unwind(AssertUnwindSafe(|| writer.observe_security(aapl()))).is_err());
    clock.panic_on_read.store(false, Ordering::SeqCst);
    assert!(board.is_poisoned());

    let read = board.freshness_for("AAPL", AssetClass::Equity);
    assert_eq!(read.freshness, MarketDataFreshness::Stale);
    assert_eq!(read.age, None);
    assert_eq!(
        board.observe_security(aapl()),
        Err(BoardWriteError::Poisoned),
        "a write to a poisoned board is refused, not silently dropped"
    );
    assert_eq!(board.watch(aapl()), Err(BoardWriteError::Poisoned));
    assert_eq!(
        board.acknowledge_resync(&aapl()),
        Err(BoardWriteError::Poisoned)
    );
    assert_eq!(
        verdict(&board),
        MarketDataFreshness::Stale,
        "a write to a poisoned board cannot revive a line"
    );
}

#[test]
fn c1_a_feed_thread_and_an_order_thread_share_one_verdict() {
    let clock = ManualClock::at(START_NS);
    let board = board(&clock, GapTracking::Unavailable);
    board.watch(aapl()).unwrap();
    assert_eq!(verdict(&board), MarketDataFreshness::Stale);

    let feed = board.clone();
    thread::spawn(move || feed.observe_security(aapl()))
        .join()
        .unwrap()
        .unwrap();
    assert_eq!(
        verdict(&board),
        MarketDataFreshness::Fresh,
        "a tick written on the feed thread is visible to the order thread"
    );
}

// --------------------------------------------------------------------------- //
// Round-1 review: a refresh must prove that new data arrived.
// --------------------------------------------------------------------------- //

#[test]
fn c1_a_replayed_tick_does_not_revive_a_time_stale_tracked_line() {
    let clock = ManualClock::at(START_NS);
    let board = board(&clock, GapTracking::Tracked);
    let sink = RecordingGapSink::default();
    board.observe_tick(&tick("AAPL", 10), &sink).unwrap();
    clock.set(START_NS + THRESHOLD_NS + 1);
    assert_eq!(verdict(&board), MarketDataFreshness::Stale);

    // The same sequence again, then an older one: no new data, so the line
    // stays stale however often they repeat.
    for replay in [10, 10, 7, 10] {
        assert!(matches!(
            board.observe_tick(&tick("AAPL", replay), &sink),
            Ok(Some(GapObservation::NonMonotonic { .. }))
        ));
        assert_eq!(
            verdict(&board),
            MarketDataFreshness::Stale,
            "replayed sequence {replay} must not refresh the line"
        );
    }
    assert_eq!(
        board
            .freshness_for("AAPL", AssetClass::Equity)
            .age_seconds(),
        Some(15),
        "the age still counts from the last tick that carried new data"
    );

    // The next real tick recovers it.
    board.observe_tick(&tick("AAPL", 11), &sink).unwrap();
    assert_eq!(verdict(&board), MarketDataFreshness::Fresh);
}

#[test]
fn c1_a_tracked_board_refuses_a_refresh_without_a_sequence() {
    let clock = ManualClock::at(START_NS);
    let board = board(&clock, GapTracking::Tracked);
    let sink = RecordingGapSink::default();
    board.observe_tick(&tick("AAPL", 1), &sink).unwrap();
    clock.set(START_NS + THRESHOLD_NS + 1);
    assert_eq!(
        board.observe_security(aapl()),
        Err(BoardWriteError::SequenceRequired)
    );
    assert_eq!(verdict(&board), MarketDataFreshness::Stale);
}

#[test]
fn c1_an_unnameable_tick_is_refused_and_records_nothing() {
    let clock = ManualClock::at(START_NS);
    for gaps in [GapTracking::Unavailable, GapTracking::Tracked] {
        let board = board(&clock, gaps);
        let result = board.observe_tick(&tick("   ", 1), &RecordingGapSink::default());
        assert!(
            matches!(result, Err(BoardWriteError::Uncanonicalizable(_))),
            "{gaps:?}: {result:?}"
        );
    }
}

#[test]
fn c1_a_clock_that_cannot_read_the_time_blocks_reads_and_refuses_writes() {
    let clock = ManualClock::at(START_NS);
    let board = board(&clock, GapTracking::Unavailable);
    board.observe_security(aapl()).unwrap();
    assert_eq!(verdict(&board), MarketDataFreshness::Fresh);

    clock.unavailable.store(true, Ordering::SeqCst);
    let read = board.freshness_for("AAPL", AssetClass::Equity);
    assert_eq!(
        read.freshness,
        MarketDataFreshness::Stale,
        "unknown time is not zero age"
    );
    assert_eq!(read.age, None);
    assert_eq!(
        board.observe_security(aapl()),
        Err(BoardWriteError::ClockUnavailable)
    );

    clock.unavailable.store(false, Ordering::SeqCst);
    assert_eq!(verdict(&board), MarketDataFreshness::Fresh);
}
