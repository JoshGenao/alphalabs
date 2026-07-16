//! SRS-MD-003 integration test — continuous heartbeat freshness monitoring.
//!
//! "The software shall monitor market data and broker heartbeat freshness
//! continuously." AC: staleness OVER 15 seconds is detected, logged,
//! displayed, and reflected in system health status. (SRS-5.4 SRS-MD-003;
//! SyRS SYS-39, NFR-P5; StRS SN-2.03.)
//!
//! Drives the public [`HeartbeatFreshnessMonitor`] through its acceptance
//! criteria with spy / failing [`HeartbeatEventSink`]s:
//!   * an observation age strictly over the NFR-P5 15 000 ms threshold marks
//!     the feed STALE and publishes a `HEARTBEAT_STALE` transition event —
//!     an age of exactly 15 000 ms is still fresh (the AC says OVER);
//!   * a watched feed that has NEVER been observed is stale fail-closed,
//!     with no fabricated age (`staleness_ms: None`);
//!   * a fresh observation recovers the feed and publishes a
//!     `HEARTBEAT_RECOVERED` event;
//!   * transition events fire ONCE per flip, never per evaluation;
//!   * the staleness state is committed BEFORE publication, so a failing
//!     sink still leaves the feed stale (fail closed, never silently
//!     tradable) with the lost-audit failure surfaced on the status row;
//!   * the broker heartbeat is tracked independently of market-data lines;
//!   * [`HeartbeatFreshnessMonitor::combined_line_freshness`] merges MD-007
//!     gap staleness with MD-003 time staleness (stale iff either);
//!   * the `md003_heartbeat_cli` fixture binary demonstrates the whole flow
//!     in fresh OS processes with a fail-closed directive parser.
//!
//! The Python L7 domain test `tests/domain/test_heartbeat_staleness.py`
//! shells out to these by exact name so the deterministic critic recognizes
//! a paired `tests/domain/` safety test for the heartbeat-freshness change.

use std::cell::RefCell;
use std::io::Write as _;
use std::process::{Command, Stdio};

use atp_market_data::{
    heartbeat_age_ns_is_stale, HeartbeatEventSink, HeartbeatFreshnessMonitor,
    HeartbeatPublishError, SequenceGapDetector, SequenceGapEventSink, SequenceGapPublishError,
};
use atp_types::{
    AssetClass, HeartbeatFeed, HeartbeatStalenessEvent, HeartbeatTransition, MarketDataFreshness,
    MarketDataTick, SecurityKey, SequenceGapEvent, HEARTBEAT_STALENESS_THRESHOLD_MS,
};

const T0: i64 = 1_700_000_000_000_000_000;
/// The NFR-P5 budget in nanoseconds.
const THRESHOLD_NS: i64 = (HEARTBEAT_STALENESS_THRESHOLD_MS as i64) * 1_000_000;
const CLI: &str = env!("CARGO_BIN_EXE_md003_heartbeat_cli");

#[derive(Default)]
struct HeartbeatSinkSpy {
    events: RefCell<Vec<HeartbeatStalenessEvent>>,
}

impl HeartbeatEventSink for HeartbeatSinkSpy {
    fn record(&self, event: HeartbeatStalenessEvent) -> Result<(), HeartbeatPublishError> {
        self.events.borrow_mut().push(event);
        Ok(())
    }
}

/// Sink whose durable write always fails — models a failed SRS-LOG-001 /
/// dashboard publication.
struct FailingHeartbeatSink;

impl HeartbeatEventSink for FailingHeartbeatSink {
    fn record(&self, _event: HeartbeatStalenessEvent) -> Result<(), HeartbeatPublishError> {
        Err(HeartbeatPublishError::new("durable log write failed"))
    }
}

/// Gap sink that always publishes Ok (the composition test only needs the
/// detector's staleness side effect).
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

fn eq_key(symbol: &str) -> SecurityKey {
    SecurityKey::new(symbol, AssetClass::Equity).expect("non-empty symbol")
}

fn eq_feed(symbol: &str) -> HeartbeatFeed {
    HeartbeatFeed::MarketData {
        symbol: symbol.to_string(),
        asset_class: AssetClass::Equity,
    }
}

fn tick(symbol: &str, tick_seq: u64) -> MarketDataTick {
    MarketDataTick {
        symbol: symbol.to_string(),
        asset_class: AssetClass::Equity,
        tick_seq,
    }
}

#[test]
fn heartbeat_staleness_over_15s_marks_feed_stale_and_publishes_event() {
    let mut monitor = HeartbeatFreshnessMonitor::new();
    let sink = HeartbeatSinkSpy::default();
    monitor
        .observe_tick(&tick("AAPL", 1), T0)
        .expect("canonical tick");

    // Baseline evaluation: fresh, no event.
    let statuses = monitor.evaluate(T0 + 1, &sink);
    assert_eq!(statuses.len(), 1);
    assert!(!statuses[0].freshness.is_stale());
    assert!(sink.events.borrow().is_empty());

    // One nanosecond OVER the threshold: stale, one HEARTBEAT_STALE event.
    let statuses = monitor.evaluate(T0 + THRESHOLD_NS + 1, &sink);
    assert!(statuses[0].freshness.is_stale());
    assert_eq!(
        statuses[0].transition,
        Some(HeartbeatTransition::BecameStale)
    );
    let events = sink.events.borrow();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].feed, eq_feed("AAPL"));
    assert_eq!(events[0].transition, HeartbeatTransition::BecameStale);
    assert_eq!(events[0].staleness_ms, Some(15_000));
    assert_eq!(events[0].last_observation_ns, Some(T0));
    assert_eq!(events[0].evaluated_at_ns, T0 + THRESHOLD_NS + 1);
    assert_eq!(events[0].threshold_ms, HEARTBEAT_STALENESS_THRESHOLD_MS);
}

#[test]
fn staleness_of_exactly_15_seconds_is_not_stale() {
    // AC boundary: staleness OVER 15 seconds. Exactly 15.000 s is fresh.
    assert!(!heartbeat_age_ns_is_stale(THRESHOLD_NS as u64));
    assert!(heartbeat_age_ns_is_stale(THRESHOLD_NS as u64 + 1));

    let mut monitor = HeartbeatFreshnessMonitor::new();
    let sink = HeartbeatSinkSpy::default();
    monitor.observe_broker_heartbeat(T0);

    let statuses = monitor.evaluate(T0 + THRESHOLD_NS, &sink);
    assert!(
        !statuses[0].freshness.is_stale(),
        "exactly 15.000s must be Fresh"
    );
    assert!(sink.events.borrow().is_empty());

    let statuses = monitor.evaluate(T0 + THRESHOLD_NS + 1, &sink);
    assert!(
        statuses[0].freshness.is_stale(),
        "15.000s + 1ns is OVER 15 seconds"
    );
    assert_eq!(sink.events.borrow().len(), 1);
}

#[test]
fn never_observed_feed_is_stale_with_no_fabricated_age() {
    let mut monitor = HeartbeatFreshnessMonitor::new();
    let sink = HeartbeatSinkSpy::default();
    monitor.watch_security(eq_key("MSFT"));

    // Direct fail-closed reads before any evaluation.
    assert!(monitor.freshness(&eq_feed("MSFT"), T0).is_stale());
    assert_eq!(monitor.staleness_ms(&eq_feed("MSFT"), T0), None);

    // The FIRST evaluation announces (and publishes) the fail-closed stale.
    let statuses = monitor.evaluate(T0, &sink);
    assert_eq!(statuses.len(), 1);
    assert!(statuses[0].freshness.is_stale());
    assert_eq!(statuses[0].staleness_ms, None, "no fabricated age");
    assert_eq!(statuses[0].last_observation_ns, None);
    assert_eq!(
        statuses[0].transition,
        Some(HeartbeatTransition::BecameStale)
    );
    let events = sink.events.borrow();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].staleness_ms, None);
    assert_eq!(events[0].last_observation_ns, None);
}

#[test]
fn fresh_observation_recovers_stale_feed_and_publishes_recovery() {
    let mut monitor = HeartbeatFreshnessMonitor::new();
    let sink = HeartbeatSinkSpy::default();
    monitor
        .observe_tick(&tick("AAPL", 1), T0)
        .expect("canonical tick");
    monitor.evaluate(T0 + THRESHOLD_NS + 1, &sink); // goes stale

    monitor
        .observe_tick(&tick("AAPL", 2), T0 + THRESHOLD_NS + 2)
        .expect("canonical tick");
    let statuses = monitor.evaluate(T0 + THRESHOLD_NS + 3, &sink);
    assert!(!statuses[0].freshness.is_stale());
    assert_eq!(statuses[0].transition, Some(HeartbeatTransition::Recovered));
    let events = sink.events.borrow();
    assert_eq!(events.len(), 2);
    assert_eq!(events[1].transition, HeartbeatTransition::Recovered);
    assert_eq!(events[1].staleness_ms, Some(0));
}

#[test]
fn transitions_publish_once_not_every_evaluation() {
    let mut monitor = HeartbeatFreshnessMonitor::new();
    let sink = HeartbeatSinkSpy::default();
    monitor.observe_broker_heartbeat(T0);

    // Three consecutive stale evaluations: exactly ONE event.
    for step in 1..=3 {
        monitor.evaluate(T0 + THRESHOLD_NS + step, &sink);
    }
    assert_eq!(
        sink.events.borrow().len(),
        1,
        "steady-state staleness must not spam the log"
    );

    // Recovery, then three fresh evaluations: exactly one more event.
    monitor.observe_broker_heartbeat(T0 + THRESHOLD_NS + 10);
    for step in 11..=13 {
        monitor.evaluate(T0 + THRESHOLD_NS + step, &sink);
    }
    assert_eq!(sink.events.borrow().len(), 2);
}

#[test]
fn stale_state_committed_before_failing_publication() {
    let mut monitor = HeartbeatFreshnessMonitor::new();
    monitor.watch_broker();

    let statuses = monitor.evaluate(T0, &FailingHeartbeatSink);
    // The feed is stale even though the sink failed — fail closed.
    assert!(statuses[0].freshness.is_stale());
    assert_eq!(
        statuses[0].transition,
        Some(HeartbeatTransition::BecameStale)
    );
    let published = statuses[0]
        .published
        .as_ref()
        .expect("a transition fired, so a publish was attempted");
    assert!(
        published.is_err(),
        "the lost audit event must be surfaced, not swallowed"
    );
    // Direct read agrees: the failing sink could not un-stale the feed.
    assert!(monitor.freshness(&HeartbeatFeed::Broker, T0).is_stale());

    // And the failed publication is NOT retried as a new transition: the
    // state was committed, so the next evaluation is steady-state.
    let statuses = monitor.evaluate(T0 + 1, &FailingHeartbeatSink);
    assert_eq!(statuses[0].transition, None);
    assert_eq!(statuses[0].published, None);
}

#[test]
fn broker_heartbeat_tracked_independently_of_market_data_lines() {
    let mut monitor = HeartbeatFreshnessMonitor::new();
    let sink = HeartbeatSinkSpy::default();
    monitor
        .observe_tick(&tick("AAPL", 1), T0)
        .expect("canonical tick");
    monitor.observe_broker_heartbeat(T0 + THRESHOLD_NS);

    // At T0 + threshold + 1ns: AAPL's age is over the threshold, the
    // broker's age is 1ns — one stale, one fresh.
    let statuses = monitor.evaluate(T0 + THRESHOLD_NS + 1, &sink);
    assert_eq!(statuses.len(), 2);
    let broker = statuses
        .iter()
        .find(|s| s.feed == HeartbeatFeed::Broker)
        .expect("broker row");
    let line = statuses
        .iter()
        .find(|s| s.feed == eq_feed("AAPL"))
        .expect("AAPL row");
    assert!(!broker.freshness.is_stale());
    assert!(line.freshness.is_stale());
    let events = sink.events.borrow();
    assert_eq!(events.len(), 1, "only the market-data line transitions");
    assert_eq!(events[0].feed, eq_feed("AAPL"));
}

#[test]
fn gap_stale_line_stays_stale_in_combined_view_despite_fresh_heartbeats() {
    let mut monitor = HeartbeatFreshnessMonitor::new();
    let mut gaps = SequenceGapDetector::new();
    let gap_sink = GapSinkSpy::default();

    // Baseline then a forward gap: the line is GAP-stale.
    gaps.observe_tick(&tick("AAPL", 1), T0, &gap_sink)
        .expect("canonical tick");
    gaps.observe_tick(&tick("AAPL", 5), T0 + 2, &gap_sink)
        .expect("canonical tick");
    assert!(gaps.is_stale(&eq_key("AAPL")));

    // Ticks keep ARRIVING (time-fresh) — the gap must still dominate.
    monitor
        .observe_tick(&tick("AAPL", 5), T0 + 2)
        .expect("canonical tick");
    assert!(!monitor.freshness(&eq_feed("AAPL"), T0 + 3).is_stale());
    assert!(monitor
        .combined_line_freshness(&gaps, &eq_key("AAPL"), T0 + 3)
        .is_stale());

    // Conversely: gap recovers (monotonic tick) but the line goes SILENT —
    // time staleness must dominate.
    gaps.observe_tick(&tick("AAPL", 6), T0 + 4, &gap_sink)
        .expect("canonical tick");
    monitor
        .observe_tick(&tick("AAPL", 6), T0 + 4)
        .expect("canonical tick");
    assert!(!gaps.is_stale(&eq_key("AAPL")));
    let silent_later = T0 + 4 + THRESHOLD_NS + 1;
    assert!(monitor
        .combined_line_freshness(&gaps, &eq_key("AAPL"), silent_later)
        .is_stale());

    // Both healthy => Fresh.
    assert_eq!(
        monitor.combined_line_freshness(&gaps, &eq_key("AAPL"), T0 + 5),
        MarketDataFreshness::Fresh
    );

    // A line neither detector has seen fails closed in the merge too.
    assert!(monitor
        .combined_line_freshness(&gaps, &eq_key("NVDA"), T0 + 5)
        .is_stale());
}

#[test]
fn staleness_sweep_across_ages_matches_strict_threshold() {
    // Pseudo-property sweep: for a spread of observation ages around the
    // boundary, the monitor's verdict must equal the single strict-threshold
    // predicate — no off-by-one between the boundary function and the
    // evaluation path.
    let ages_ns: [i64; 12] = [
        0,
        1,
        1_000_000,
        14_999_000_000,
        THRESHOLD_NS - 1,
        THRESHOLD_NS,
        THRESHOLD_NS + 1,
        THRESHOLD_NS + 1_000_000,
        16_000_000_000,
        60_000_000_000,
        3_600_000_000_000,
        86_400_000_000_000,
    ];
    for age_ns in ages_ns {
        let mut monitor = HeartbeatFreshnessMonitor::new();
        let sink = HeartbeatSinkSpy::default();
        monitor.observe_broker_heartbeat(T0);
        let statuses = monitor.evaluate(T0 + age_ns, &sink);
        let expect_stale = heartbeat_age_ns_is_stale(age_ns as u64);
        assert_eq!(
            statuses[0].freshness.is_stale(),
            expect_stale,
            "age {age_ns}ns: monitor verdict must match the strict predicate"
        );
        assert_eq!(
            statuses[0].staleness_ms,
            Some((age_ns / 1_000_000) as u64),
            "age {age_ns}ns: display value is the floor-ms age"
        );
    }
}

// ------------------------------------------------------------------------ //
// CLI process tests — the fixture-driven operator workflow (fresh OS
// processes, err001 pattern).
// ------------------------------------------------------------------------ //

fn run_cli_stdin(script: &str) -> (String, String, Option<i32>) {
    let mut child = Command::new(CLI)
        .arg("-")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn md003_heartbeat_cli");
    child
        .stdin
        .as_mut()
        .expect("piped stdin")
        .write_all(script.as_bytes())
        .expect("write script");
    let output = child.wait_with_output().expect("cli terminates");
    (
        String::from_utf8(output.stdout).expect("utf8 stdout"),
        String::from_utf8(output.stderr).expect("utf8 stderr"),
        output.status.code(),
    )
}

#[test]
fn cli_demonstrates_detection_boundary_and_recovery() {
    let script = "\
watch-security AAPL equity
watch-broker
tick AAPL equity 1 1700000000000000000
broker-heartbeat 1700000000000000000
evaluate 1700000015000000000
evaluate 1700000015000000001
tick AAPL equity 2 1700000016000000000
broker-heartbeat 1700000016000000000
evaluate 1700000016000000001
";
    let (stdout, stderr, code) = run_cli_stdin(script);
    assert_eq!(code, Some(0), "stderr: {stderr}");

    let stale_events: Vec<&str> = stdout
        .lines()
        .filter(|l| l.starts_with("event kind=HEARTBEAT_STALE"))
        .collect();
    let recovered_events: Vec<&str> = stdout
        .lines()
        .filter(|l| l.starts_with("event kind=HEARTBEAT_RECOVERED"))
        .collect();
    assert_eq!(stale_events.len(), 2, "one per feed at +15.000s+1ns");
    assert_eq!(recovered_events.len(), 2, "one per feed after fresh obs");

    // At exactly +15.000 s every status row is fresh (AC boundary).
    let at_boundary: Vec<&str> = stdout
        .lines()
        .filter(|l| l.starts_with("status") && l.ends_with("evaluated_at_ns=1700000015000000000"))
        .collect();
    assert_eq!(at_boundary.len(), 2);
    assert!(at_boundary.iter().all(|l| l.contains(" stale=false")));

    // One nanosecond later every row is stale.
    let over_boundary: Vec<&str> = stdout
        .lines()
        .filter(|l| l.starts_with("status") && l.ends_with("evaluated_at_ns=1700000015000000001"))
        .collect();
    assert_eq!(over_boundary.len(), 2);
    assert!(over_boundary.iter().all(|l| l.contains(" stale=true")));
    assert!(over_boundary
        .iter()
        .all(|l| l.contains(" time_stale=true") && l.contains(" threshold_ms=15000")));
}

#[test]
fn cli_reports_never_observed_feed_with_no_fabricated_age() {
    let script = "\
watch-broker
evaluate 1700000000000000000
";
    let (stdout, _stderr, code) = run_cli_stdin(script);
    assert_eq!(code, Some(0));
    let row = stdout
        .lines()
        .find(|l| l.starts_with("status feed=broker"))
        .expect("broker status row");
    assert!(row.contains(" never_observed=true"));
    assert!(row.contains(" staleness_ms=none"));
    assert!(row.contains(" last_observation_ns=none"));
    assert!(row.contains(" stale=true"));
    assert!(stdout
        .lines()
        .any(|l| l.starts_with("event kind=HEARTBEAT_STALE feed=broker")));
}

#[test]
fn cli_merges_gap_staleness_into_the_line_verdict() {
    let script = "\
tick AAPL equity 1 1700000000000000000
tick AAPL equity 5 1700000000000000002
evaluate 1700000000000000003
";
    let (stdout, _stderr, code) = run_cli_stdin(script);
    assert_eq!(code, Some(0));
    assert!(stdout
        .lines()
        .any(|l| l.starts_with("event kind=SEQUENCE_GAP symbol=AAPL")));
    let row = stdout
        .lines()
        .find(|l| l.starts_with("status feed=market_data symbol=AAPL"))
        .expect("AAPL status row");
    assert!(
        row.contains(" time_stale=false") && row.contains(" gap_stale=true"),
        "ticks arrive (time-fresh) but the sequence gapped: {row}"
    );
    assert!(row.contains(" stale=true"), "the merge fails closed: {row}");
}

#[test]
fn cli_fails_closed_on_malformed_directives() {
    for bad in [
        "frobnicate\n",
        "tick AAPL equity notanumber 1\n",
        "tick AAPL equity 1\n",
        "watch-security AAPL crypto\n",
        "evaluate\n",
        "watch-security  equity\n",
    ] {
        let (_stdout, stderr, code) = run_cli_stdin(bad);
        assert_eq!(code, Some(2), "directive {bad:?} must be refused");
        assert!(
            stderr.contains("md003_heartbeat_cli: line"),
            "structured stderr for {bad:?}: {stderr}"
        );
    }
}
