//! SRS-MD-007 integration test — tick-sequence gap detection + staleness.
//!
//! "The market-data subscription manager shall detect sequence gaps in IB tick
//! streams and reflect gap state in heartbeat/staleness." (SRS-5.4 SRS-MD-007;
//! SyRS SYS-39 / SYS-39a / SYS-70; NFR-P5; StRS SN-2.03 / SN-2.04.)
//!
//! Drives the public [`SequenceGapDetector`] through its acceptance criteria
//! with a spy [`SequenceGapEventSink`]:
//!   * a forward sequence skip is detected as a GAP, logged with symbol /
//!     expected / observed / timestamp, and marks the affected line STALE;
//!   * the line recovers on a fresh monotonic tick OR an operator-acknowledged
//!     resync (the two SRS-MD-007 recovery conditions);
//!   * the stale [`MarketDataFreshness`] the detector reports is exactly the
//!     value the SRS-MD-004 execution gate (`submit_live_order` via the
//!     `MarketDataFreshnessProbe` port) rejects `MARKET_DATA_STALE` on — the
//!     in-process seam MD-007 fills for MD-004;
//!   * uncanonicalizable ticks (empty symbol / option) fail closed;
//!   * staleness is isolated per canonical security.
//!
//! The Python L7 domain test `tests/domain/test_sequence_gap_stale.py` shells
//! out to these by exact name so the deterministic critic recognizes a paired
//! `tests/domain/` safety test for the stale-data change.

use std::cell::RefCell;

use atp_market_data::{
    GapObservation, ResyncOutcome, SequenceGapDetector, SequenceGapEventSink,
    SequenceGapPublishError, SubscriptionRegistryError,
};
use atp_types::{AssetClass, MarketDataFreshness, MarketDataTick, SecurityKey, SequenceGapEvent};

const T0: i64 = 1_700_000_000_000_000_000;

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

/// Sink whose durable write always fails — models a failed SRS-LOG-001 /
/// dashboard publication.
struct FailingGapSink;

impl SequenceGapEventSink for FailingGapSink {
    fn record(&self, _event: SequenceGapEvent) -> Result<(), SequenceGapPublishError> {
        Err(SequenceGapPublishError::new("durable log write failed"))
    }
}

fn tick(symbol: &str, tick_seq: u64) -> MarketDataTick {
    MarketDataTick {
        symbol: symbol.to_string(),
        asset_class: AssetClass::Equity,
        tick_seq,
    }
}

fn eq_key(symbol: &str) -> SecurityKey {
    SecurityKey::new(symbol, AssetClass::Equity).expect("non-empty symbol")
}

#[test]
fn sequence_gap_marks_line_stale_and_logs_event() {
    let mut detector = SequenceGapDetector::new();
    let sink = GapSinkSpy::default();

    // Baseline, then a forward skip: 6 and 7 are missing.
    assert_eq!(
        detector.observe_tick(&tick("AAPL", 5), T0, &sink).unwrap(),
        GapObservation::Baseline
    );
    assert_eq!(
        detector
            .observe_tick(&tick("AAPL", 8), T0 + 1, &sink)
            .unwrap(),
        GapObservation::Gap {
            expected: 6,
            observed: 8,
            published: Ok(())
        }
    );

    // Affected subscription is now stale.
    assert!(detector.is_stale(&eq_key("AAPL")));
    assert_eq!(detector.stale_since_ns(&eq_key("AAPL")), Some(T0 + 1));

    // The gap event carries exactly the four SRS-MD-007 acceptance fields.
    let events = sink.events.borrow();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].symbol, "AAPL");
    assert_eq!(events[0].expected_sequence, 6);
    assert_eq!(events[0].observed_sequence, 8);
    assert_eq!(events[0].observed_at_ns, T0 + 1);
}

#[test]
fn monotonic_fresh_tick_recovers_the_line() {
    let mut detector = SequenceGapDetector::new();
    let sink = GapSinkSpy::default();
    detector.observe_tick(&tick("AAPL", 5), T0, &sink).unwrap();
    detector
        .observe_tick(&tick("AAPL", 8), T0 + 1, &sink)
        .unwrap();
    assert!(detector.is_stale(&eq_key("AAPL")));

    // 9 == 8 + 1 → monotonic → recovery condition #1.
    assert_eq!(
        detector
            .observe_tick(&tick("AAPL", 9), T0 + 2, &sink)
            .unwrap(),
        GapObservation::InSequence { recovered: true }
    );
    assert_eq!(
        detector.freshness(&eq_key("AAPL")),
        MarketDataFreshness::Fresh
    );
    assert_eq!(detector.stale_since_ns(&eq_key("AAPL")), None);
    assert_eq!(sink.events.borrow().len(), 1, "recovery adds no gap event");
}

#[test]
fn operator_resync_recovers_the_line() {
    let mut detector = SequenceGapDetector::new();
    let sink = GapSinkSpy::default();
    detector.observe_tick(&tick("AAPL", 5), T0, &sink).unwrap();
    detector
        .observe_tick(&tick("AAPL", 8), T0 + 1, &sink)
        .unwrap();
    assert!(detector.is_stale(&eq_key("AAPL")));

    // Recovery condition #2: operator-acknowledged resync.
    assert_eq!(
        detector.acknowledge_resync(&eq_key("AAPL")),
        ResyncOutcome::Acknowledged
    );
    assert_eq!(
        detector.freshness(&eq_key("AAPL")),
        MarketDataFreshness::Fresh
    );

    // The resynced feed may resume at any sequence without a false gap.
    assert_eq!(
        detector
            .observe_tick(&tick("AAPL", 100), T0 + 2, &sink)
            .unwrap(),
        GapObservation::Baseline
    );
    assert_eq!(
        detector.freshness(&eq_key("AAPL")),
        MarketDataFreshness::Fresh
    );
    assert_eq!(sink.events.borrow().len(), 1);
}

#[test]
fn stale_freshness_is_the_value_the_md_004_gate_blocks_on() {
    // The whole point of MD-007: it produces the MarketDataFreshness a
    // consolidated line is in. The SRS-MD-004 execution gate
    // (ExecutionEngine::submit_live_order, via MarketDataFreshnessProbe)
    // switches on exactly this enum — Stale ⇒ MARKET_DATA_STALE for live AND
    // paper submissions. This test pins that the detector emits Stale on a gap
    // and Fresh after recovery, in the shared atp-types vocabulary the gate
    // consumes. The deferred runtime adapter that bridges the two must make the
    // (currently symbol-only) MarketDataFreshnessProbe port security-aware — the
    // detector is keyed on the full SecurityKey; see sequence_gap_contract
    // .deferred[].
    let mut detector = SequenceGapDetector::new();
    let sink = GapSinkSpy::default();
    detector.observe_tick(&tick("AAPL", 1), T0, &sink).unwrap();
    assert_eq!(
        detector.freshness(&eq_key("AAPL")),
        MarketDataFreshness::Fresh,
        "a healthy line trades"
    );

    detector
        .observe_tick(&tick("AAPL", 7), T0 + 1, &sink)
        .unwrap();
    assert_eq!(
        detector.freshness(&eq_key("AAPL")),
        MarketDataFreshness::Stale,
        "a gap blocks order submission (MD-004)"
    );

    detector.acknowledge_resync(&eq_key("AAPL"));
    assert_eq!(
        detector.freshness(&eq_key("AAPL")),
        MarketDataFreshness::Fresh,
        "an operator resync re-enables order submission"
    );

    // Fail-closed default: an unsubscribed symbol is Stale (blocks trading).
    assert_eq!(
        detector.freshness(&eq_key("TSLA")),
        MarketDataFreshness::Stale,
        "a line with no data is not tradable"
    );
}

#[test]
fn uncanonicalizable_ticks_fail_closed() {
    let mut detector = SequenceGapDetector::new();
    let sink = GapSinkSpy::default();
    assert_eq!(
        detector.observe_tick(
            &MarketDataTick {
                symbol: String::new(),
                asset_class: AssetClass::Equity,
                tick_seq: 1,
            },
            T0,
            &sink,
        ),
        Err(SubscriptionRegistryError::EmptySymbol)
    );
    assert_eq!(
        detector.observe_tick(
            &MarketDataTick {
                symbol: "AAPL".to_string(),
                asset_class: AssetClass::Option,
                tick_seq: 1,
            },
            T0,
            &sink,
        ),
        Err(SubscriptionRegistryError::OptionContractUnsupported)
    );
    assert!(sink.events.borrow().is_empty());
    assert!(!detector.is_tracked(&eq_key("AAPL")));
}

#[test]
fn gaps_are_isolated_per_security() {
    let mut detector = SequenceGapDetector::new();
    let sink = GapSinkSpy::default();
    detector.observe_tick(&tick("AAPL", 1), T0, &sink).unwrap();
    detector.observe_tick(&tick("MSFT", 1), T0, &sink).unwrap();
    // Gap on AAPL, MSFT stays contiguous.
    detector
        .observe_tick(&tick("AAPL", 9), T0 + 1, &sink)
        .unwrap();
    detector
        .observe_tick(&tick("MSFT", 2), T0 + 1, &sink)
        .unwrap();
    assert_eq!(
        detector.freshness(&eq_key("AAPL")),
        MarketDataFreshness::Stale
    );
    assert_eq!(
        detector.freshness(&eq_key("MSFT")),
        MarketDataFreshness::Fresh
    );
    let events = sink.events.borrow();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].symbol, "AAPL");
}

#[test]
fn gap_publication_failure_is_fail_closed_and_surfaced() {
    // When the SRS-LOG-001 / dashboard sink fails to publish the gap event, the
    // line is STILL marked stale (fail closed — the order-block is committed
    // before publication) and the failure is surfaced to the caller on
    // `published` so the runtime can alert on the lost audit evidence.
    let mut detector = SequenceGapDetector::new();
    detector
        .observe_tick(&tick("AAPL", 1), T0, &FailingGapSink)
        .unwrap();
    let observation = detector
        .observe_tick(&tick("AAPL", 5), T0 + 1, &FailingGapSink)
        .unwrap();
    match observation {
        GapObservation::Gap {
            expected,
            observed,
            published,
        } => {
            assert_eq!(expected, 2);
            assert_eq!(observed, 5);
            assert!(published.is_err(), "a failed publication must surface Err");
        }
        other => panic!("expected a Gap, got {other:?}"),
    }
    assert_eq!(
        detector.freshness(&eq_key("AAPL")),
        MarketDataFreshness::Stale,
        "a failed gap publication must still block orders (fail closed)"
    );
}

#[test]
fn repeated_gaps_preserve_the_original_stale_onset_time() {
    // stale_since_ns records when the line FIRST went stale, so the
    // heartbeat/dashboard staleness age is not reset by a later gap.
    let mut detector = SequenceGapDetector::new();
    let sink = GapSinkSpy::default();
    detector.observe_tick(&tick("AAPL", 5), T0, &sink).unwrap();
    detector
        .observe_tick(&tick("AAPL", 8), T0 + 1, &sink)
        .unwrap();
    assert_eq!(detector.stale_since_ns(&eq_key("AAPL")), Some(T0 + 1));
    detector
        .observe_tick(&tick("AAPL", 40), T0 + 999, &sink)
        .unwrap();
    assert_eq!(
        detector.stale_since_ns(&eq_key("AAPL")),
        Some(T0 + 1),
        "a repeated gap must not reset the original stale-onset time"
    );
}
