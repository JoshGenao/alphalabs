//! SRS-MD-003 — the LIVE feed loop: the producer that turns a real broker
//! connection into continuous freshness evidence.
//!
//! [`HeartbeatFreshnessMonitor`](crate::HeartbeatFreshnessMonitor) is clock-free
//! and source-free by design: something has to call `observe_*` per genuinely
//! delivered tick and per genuine gateway round trip, then `evaluate` on a
//! cadence. That something is [`LiveFeedLoop`]. It talks to the broker through
//! the [`LiveTickSource`] port, so this crate stays free of vendor adapter code
//! (the IB implementation lives in the composition layer, over the
//! `ib-live-transport` transport).
//!
//! Three properties carry the whole design:
//!
//! * **Only evidence counts.** A tick observation requires a tick the gateway
//!   actually delivered; a broker observation requires a round trip that
//!   actually completed. A failed probe records NOTHING — it does not refresh
//!   the line, and the silence it leaves is what the 15 s threshold is for.
//! * **A step always evaluates.** Even when the source errors, the loop still
//!   evaluates and republishes. Bailing out early would freeze the last
//!   snapshot, and a frozen snapshot is what a *healthy* feed looks like to
//!   anyone reading it.
//! * **The snapshot is self-dating.** Every write stamps the instant it was
//!   evaluated, so a reader can tell a live verdict from the leftovers of a
//!   dead daemon. See `SnapshotHeartbeatSource` on the dashboard side.
//!
//! ## What this source cannot prove (SRS-MD-007)
//!
//! The TWS tick stream carries no upstream sequence number, so a live IB line
//! has no gap-detectable ordering: SRS-MD-007's `SequenceGapDetector` has
//! nothing to consume here, and rendered rows report `gap_stale=false` because
//! no gap detection is RUNNING, not because a detector cleared the line. Time
//! freshness — which is all SRS-MD-003 asserts — is fully evaluated.

use crate::{HeartbeatEventSink, HeartbeatFreshnessMonitor, HeartbeatStatus};
use atp_types::{
    AssetClass, HeartbeatFeed, HeartbeatTransition, SecurityKey, HEARTBEAT_STALENESS_THRESHOLD_MS,
};
use std::collections::VecDeque;
use std::fmt;
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::Duration;

/// Schema version of the persisted freshness snapshot (SRS-DATA-015 registry
/// entity `md003-heartbeat-snapshot`).
pub const SNAPSHOT_SCHEMA_VERSION: u64 = 1;

/// First token of a snapshot's header line — the format's magic. A file that
/// does not begin with it is not this format and is refused rather than
/// half-parsed.
pub const SNAPSHOT_MAGIC: &str = "atp-md003-snapshot";

/// Failure modes of one feed step. Both variants are reported, never swallowed:
/// the loop's caller decides whether to keep running, and the snapshot records
/// that the step was degraded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FeedError {
    /// The broker source failed (transport fault, withheld stream, ...).
    Source(String),
    /// The snapshot could not be persisted durably.
    Snapshot(String),
    /// The loop was configured with parameters that cannot honestly monitor.
    Configuration(String),
}

impl fmt::Display for FeedError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Source(detail) => write!(formatter, "live feed source failed: {detail}"),
            Self::Snapshot(detail) => write!(formatter, "freshness snapshot failed: {detail}"),
            Self::Configuration(detail) => write!(formatter, "live feed misconfigured: {detail}"),
        }
    }
}

impl std::error::Error for FeedError {}

/// The live producer seam: a broker connection, reduced to the two things
/// freshness needs from it.
pub trait LiveTickSource {
    /// Drain up to `budget` of delivered market-data ticks, returning the
    /// consolidated security line each one arrived on.
    ///
    /// An empty result means the lines were quiet, which is a normal outcome
    /// and MUST NOT be reported as an error — quiet is precisely the condition
    /// the freshness monitor exists to judge.
    fn poll_observations(&mut self, budget: Duration) -> Result<Vec<SecurityKey>, FeedError>;

    /// Complete one brokerage round trip (IB: `reqCurrentTime` →
    /// `currentTime`). `Ok` means the gateway ANSWERED; anything else means we
    /// have no evidence and the broker line must not be refreshed.
    fn broker_round_trip(&mut self) -> Result<(), FeedError>;
}

/// One Fresh↔Stale flip, kept so a reader that samples the snapshot can still
/// see transitions that happened between its samples.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransitionRecord {
    pub feed: HeartbeatFeed,
    pub transition: HeartbeatTransition,
    pub staleness_ms: Option<u64>,
    pub last_observation_ns: Option<i64>,
    pub evaluated_at_ns: i64,
}

/// How many past transitions the snapshot carries.
///
/// Transitions are rare — one per flip, not per evaluation — so a small journal
/// spans a long wall-clock window, and the dashboard only has to poll before it
/// overflows rather than before the next step overwrites the file.
const TRANSITION_JOURNAL_LIMIT: usize = 64;

/// What one [`LiveFeedLoop::step`] did.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FeedStep {
    /// The per-feed verdicts this evaluation produced (the display surface).
    pub statuses: Vec<HeartbeatStatus>,
    /// Recent Fresh↔Stale flips, oldest first — THIS step's and the preceding
    /// ones still in the journal.
    ///
    /// The status rows describe the present, so a reader that samples them sees
    /// only the state at sampling instants. A feed that went stale and
    /// recovered entirely between two samples would leave every sampled row
    /// fresh, and SRS-MD-003's "logged" leg would silently lose a real
    /// incident. The journal is what makes those flips survive sampling.
    pub transitions: Vec<TransitionRecord>,
    /// How many delivered ticks were observed this step.
    pub observed_ticks: usize,
    /// Whether a broker round trip was attempted, and whether it answered.
    pub broker_probe: BrokerProbe,
    /// The source failure this step hit, if any. The step still evaluated.
    pub source_error: Option<FeedError>,
    /// The instant these verdicts were evaluated at — read AFTER the step's
    /// blocking I/O (see [`LiveFeedLoop::step`]).
    ///
    /// Publish the snapshot with THIS, never with a reading the caller took
    /// before calling `step`: the header instant is what a reader ages the
    /// file by, so a pre-I/O stamp would disagree with the row verdicts it
    /// carries.
    pub evaluated_at_ns: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BrokerProbe {
    /// Not due yet on the configured cadence.
    NotDue,
    /// Attempted and the gateway answered — the only case that observes.
    Answered,
    /// Attempted and it did not answer.
    Failed,
}

/// Drives a [`HeartbeatFreshnessMonitor`] from a live broker source.
#[derive(Debug)]
pub struct LiveFeedLoop<S: LiveTickSource> {
    source: S,
    monitor: HeartbeatFreshnessMonitor,
    poll_budget: Duration,
    heartbeat_cadence_ns: i64,
    last_probe_ns: Option<i64>,
    /// Rolling window of recent flips (see [`FeedStep::transitions`]).
    journal: VecDeque<TransitionRecord>,
}

impl<S: LiveTickSource> LiveFeedLoop<S> {
    /// Build a loop watching `watched` market-data lines plus the broker line.
    ///
    /// Both watches are registered up front and WITHOUT an observation, so a
    /// line that never delivers anything is stale from the first evaluation
    /// rather than invisible until its first tick.
    ///
    /// Refuses a configuration that cannot monitor honestly:
    ///
    /// * a heartbeat cadence at or above the staleness threshold would make the
    ///   broker line go stale because of how slowly we PROBE it — a false alarm
    ///   manufactured by the monitor itself;
    /// * a poll budget at or above the cadence would let a single drain consume
    ///   the whole probe interval, so the broker round trip could never run on
    ///   time;
    /// * an empty watch set would report a healthy "0 stale feeds" while
    ///   monitoring nothing at all.
    pub fn new(
        source: S,
        watched: Vec<SecurityKey>,
        poll_budget: Duration,
        heartbeat_cadence: Duration,
    ) -> Result<Self, FeedError> {
        if watched.is_empty() {
            return Err(FeedError::Configuration(
                "no market-data lines to watch — an empty watch set reports healthy while \
                 monitoring nothing"
                    .to_string(),
            ));
        }
        let threshold = Duration::from_millis(HEARTBEAT_STALENESS_THRESHOLD_MS);
        if heartbeat_cadence >= threshold {
            return Err(FeedError::Configuration(format!(
                "heartbeat cadence {heartbeat_cadence:?} is not below the {threshold:?} staleness \
                 threshold — the broker line would go stale from the probe interval alone"
            )));
        }
        if poll_budget >= heartbeat_cadence {
            return Err(FeedError::Configuration(format!(
                "poll budget {poll_budget:?} is not below the heartbeat cadence \
                 {heartbeat_cadence:?} — one drain would consume the whole probe interval"
            )));
        }
        let mut monitor = HeartbeatFreshnessMonitor::new();
        for key in watched {
            monitor.watch_security(key);
        }
        monitor.watch_broker();
        Ok(Self {
            source,
            monitor,
            poll_budget,
            heartbeat_cadence_ns: duration_to_ns(heartbeat_cadence),
            last_probe_ns: None,
            journal: VecDeque::new(),
        })
    }

    /// One iteration: drain ticks, probe the broker when due, evaluate.
    ///
    /// `clock` is the caller's injected wall-clock reader (this crate reads no
    /// wall clock itself), and `events` receives one event per Fresh↔Stale
    /// transition.
    ///
    /// **The clock is read TWICE, and that is the point.** A step blocks: the
    /// tick drain spends its poll budget, and the broker probe can sit on the
    /// wire until its operation deadline expires. Evaluating with the reading
    /// taken before all that would date the verdict to a moment that has
    /// already passed — so a broker whose last answer was near the threshold
    /// could be judged fresh when the real clock had already crossed 15 s, and
    /// the dashboard would stay green through exactly the window this feature
    /// exists to catch. Observations keep the pre-I/O reading (stamping them
    /// early only ages a line faster, which is the fail-closed direction);
    /// the EVALUATION uses a reading taken after the I/O.
    pub fn step<K: HeartbeatEventSink, C: Fn() -> i64>(
        &mut self,
        clock: C,
        events: &K,
    ) -> FeedStep {
        let now_ns = clock();
        let mut source_error = None;
        let mut observed_ticks = 0;

        match self.source.poll_observations(self.poll_budget) {
            Ok(lines) => {
                observed_ticks = lines.len();
                for key in lines {
                    self.monitor.observe_security(key, now_ns);
                }
            }
            // Record the fault and carry on to evaluation: an unreported,
            // unevaluated step would leave the last verdict standing as if it
            // were current.
            Err(err) => source_error = Some(err),
        }

        let broker_probe = if self.probe_due(now_ns) {
            self.last_probe_ns = Some(now_ns);
            match self.source.broker_round_trip() {
                Ok(()) => {
                    self.monitor.observe_broker_heartbeat(now_ns);
                    BrokerProbe::Answered
                }
                Err(err) => {
                    // No answer, no observation. The broker line ages exactly
                    // as if nothing had been sent, which is the truth.
                    source_error.get_or_insert(err);
                    BrokerProbe::Failed
                }
            }
        } else {
            BrokerProbe::NotDue
        };

        // Re-read the clock now that every blocking call is behind us. Never
        // earlier than the observations we just recorded: a wall clock that
        // steps backwards must not produce a verdict that predates its own
        // evidence.
        let evaluated_at_ns = clock().max(now_ns);

        let statuses = self.monitor.evaluate(evaluated_at_ns, events);
        for status in &statuses {
            let Some(transition) = status.transition else {
                continue;
            };
            if self.journal.len() >= TRANSITION_JOURNAL_LIMIT {
                self.journal.pop_front();
            }
            self.journal.push_back(TransitionRecord {
                feed: status.feed.clone(),
                transition,
                staleness_ms: status.staleness_ms,
                last_observation_ns: status.last_observation_ns,
                evaluated_at_ns,
            });
        }

        FeedStep {
            statuses,
            transitions: self.journal.iter().cloned().collect(),
            observed_ticks,
            broker_probe,
            source_error,
            evaluated_at_ns,
        }
    }

    fn probe_due(&self, now_ns: i64) -> bool {
        match self.last_probe_ns {
            None => true,
            Some(last) => now_ns.saturating_sub(last) >= self.heartbeat_cadence_ns,
        }
    }

    /// Read-only access to the monitor (health/status reads never publish).
    pub fn monitor(&self) -> &HeartbeatFreshnessMonitor {
        &self.monitor
    }
}

fn duration_to_ns(duration: Duration) -> i64 {
    i64::try_from(duration.as_nanos()).unwrap_or(i64::MAX)
}

// --------------------------------------------------------------------------- //
// Snapshot rendering + durable write
// --------------------------------------------------------------------------- //

fn asset_class_str(class: AssetClass) -> &'static str {
    match class {
        AssetClass::Equity => "equity",
        AssetClass::Option => "option",
    }
}

fn feed_kv(feed: &HeartbeatFeed) -> String {
    match feed {
        HeartbeatFeed::MarketData {
            symbol,
            asset_class,
        } => format!(
            "feed=market_data symbol={} asset_class={}",
            symbol,
            asset_class_str(*asset_class)
        ),
        HeartbeatFeed::Broker => "feed=broker".to_string(),
    }
}

fn opt_u64(value: Option<u64>) -> String {
    value.map_or_else(|| "none".to_string(), |v| v.to_string())
}

fn opt_i64(value: Option<i64>) -> String {
    value.map_or_else(|| "none".to_string(), |v| v.to_string())
}

/// Render one status row in the operator kv line format the dashboard bridge
/// already parses.
///
/// `gap_stale` is supplied by the caller because only the caller knows whether
/// a [`SequenceGapDetector`](crate::SequenceGapDetector) is actually running:
/// the live IB source has no upstream sequence to detect gaps in and passes
/// `false`, meaning "no gap detection is running", not "a detector cleared it".
pub fn render_status_line(
    status: &HeartbeatStatus,
    gap_stale: bool,
    evaluated_at_ns: i64,
) -> String {
    let time_stale = status.freshness.is_stale();
    format!(
        "status {} last_observation_ns={} staleness_ms={} never_observed={} time_stale={} \
         gap_stale={} stale={} threshold_ms={} evaluated_at_ns={}",
        feed_kv(&status.feed),
        opt_i64(status.last_observation_ns),
        opt_u64(status.staleness_ms),
        status.last_observation_ns.is_none(),
        time_stale,
        gap_stale,
        time_stale || gap_stale,
        HEARTBEAT_STALENESS_THRESHOLD_MS,
        evaluated_at_ns
    )
}

/// The full snapshot text for one evaluation: a header line carrying the
/// format's identity and the instant it was evaluated, then one status row per
/// watched feed.
pub fn render_snapshot(step: &FeedStep, evaluated_at_ns: i64) -> String {
    let mut out = format!(
        "{SNAPSHOT_MAGIC} schema_version={SNAPSHOT_SCHEMA_VERSION} \
         evaluated_at_ns={evaluated_at_ns} threshold_ms={HEARTBEAT_STALENESS_THRESHOLD_MS} \
         observed_ticks={} broker_probe={} gap_detection=unavailable degraded={}\n",
        step.observed_ticks,
        match step.broker_probe {
            BrokerProbe::NotDue => "not_due",
            BrokerProbe::Answered => "answered",
            BrokerProbe::Failed => "failed",
        },
        step.source_error.is_some(),
    );
    for status in &step.statuses {
        // gap_stale=false: see `render_status_line` — no detector runs here.
        out.push_str(&render_status_line(status, false, evaluated_at_ns));
        out.push('\n');
    }
    for record in &step.transitions {
        out.push_str(&render_transition_line(record));
        out.push('\n');
    }
    out
}

/// Render one journal entry in the operator kv event format.
///
/// Carries its OWN `evaluated_at_ns` — the instant the flip happened, not the
/// instant this snapshot was written — so a reader can tell a historical
/// transition from one that occurred at sampling time, and log each exactly
/// once.
pub fn render_transition_line(record: &TransitionRecord) -> String {
    format!(
        "event kind={} {} staleness_ms={} last_observation_ns={} evaluated_at_ns={} \
         threshold_ms={}",
        match record.transition {
            HeartbeatTransition::BecameStale => "HEARTBEAT_STALE",
            HeartbeatTransition::Recovered => "HEARTBEAT_RECOVERED",
        },
        feed_kv(&record.feed),
        opt_u64(record.staleness_ms),
        opt_i64(record.last_observation_ns),
        record.evaluated_at_ns,
        HEARTBEAT_STALENESS_THRESHOLD_MS,
    )
}

/// Persist one snapshot durably: unique scratch file → fsync → atomic rename →
/// parent directory fsync.
///
/// A reader must never observe a half-written verdict, and a crash must leave
/// either the previous snapshot or the new one — never a torn file that would
/// parse as a partial (and therefore wrong) set of feeds.
pub fn write_snapshot(path: &Path, step: &FeedStep, evaluated_at_ns: i64) -> Result<(), FeedError> {
    let parent = path.parent().ok_or_else(|| {
        FeedError::Snapshot(format!("snapshot path {path:?} has no parent directory"))
    })?;
    let scratch = scratch_path(path, evaluated_at_ns);
    let body = render_snapshot(step, evaluated_at_ns);

    let write = || -> std::io::Result<()> {
        let mut file = File::create(&scratch)?;
        file.write_all(body.as_bytes())?;
        file.sync_all()?;
        drop(file);
        fs::rename(&scratch, path)?;
        File::open(parent)?.sync_all()
    };
    write().map_err(|err| {
        // Never leave a scratch file behind to accumulate or be mistaken for
        // the real snapshot.
        let _ = fs::remove_file(&scratch);
        FeedError::Snapshot(format!("couldn't durably write {path:?}: {err}"))
    })
}

fn scratch_path(path: &Path, evaluated_at_ns: i64) -> PathBuf {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(format!(".tmp.{}.{evaluated_at_ns}", std::process::id()));
    path.with_file_name(name)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::HeartbeatPublishError;
    use atp_types::HeartbeatStalenessEvent;
    use std::cell::RefCell;

    #[derive(Default)]
    struct CountingSink {
        events: RefCell<Vec<HeartbeatStalenessEvent>>,
    }

    impl HeartbeatEventSink for CountingSink {
        fn record(&self, event: HeartbeatStalenessEvent) -> Result<(), HeartbeatPublishError> {
            self.events.borrow_mut().push(event);
            Ok(())
        }
    }

    #[derive(Debug)]
    struct ScriptedSource {
        polls: Vec<Result<Vec<SecurityKey>, FeedError>>,
        round_trips: Vec<Result<(), FeedError>>,
        round_trip_calls: usize,
    }

    impl ScriptedSource {
        fn new(
            polls: Vec<Result<Vec<SecurityKey>, FeedError>>,
            round_trips: Vec<Result<(), FeedError>>,
        ) -> Self {
            Self {
                polls,
                round_trips,
                round_trip_calls: 0,
            }
        }
    }

    impl LiveTickSource for ScriptedSource {
        fn poll_observations(&mut self, _budget: Duration) -> Result<Vec<SecurityKey>, FeedError> {
            if self.polls.is_empty() {
                return Ok(Vec::new());
            }
            self.polls.remove(0)
        }

        fn broker_round_trip(&mut self) -> Result<(), FeedError> {
            self.round_trip_calls += 1;
            if self.round_trips.is_empty() {
                return Ok(());
            }
            self.round_trips.remove(0)
        }
    }

    fn aapl() -> SecurityKey {
        SecurityKey::new("AAPL", AssetClass::Equity).unwrap()
    }

    const SECOND_NS: i64 = 1_000_000_000;

    fn loop_over(source: ScriptedSource) -> LiveFeedLoop<ScriptedSource> {
        LiveFeedLoop::new(
            source,
            vec![aapl()],
            Duration::from_millis(500),
            Duration::from_secs(5),
        )
        .expect("valid configuration")
    }

    #[test]
    fn a_delivered_tick_refreshes_its_line() {
        let mut feed = loop_over(ScriptedSource::new(vec![Ok(vec![aapl()])], vec![Ok(())]));
        let sink = CountingSink::default();
        let step = feed.step(|| SECOND_NS, &sink);

        assert_eq!(step.observed_ticks, 1);
        assert_eq!(step.broker_probe, BrokerProbe::Answered);
        assert!(step.statuses.iter().all(|s| !s.freshness.is_stale()));
    }

    #[test]
    fn a_failed_round_trip_does_not_refresh_the_broker_line() {
        // The whole point of the round trip: only an ANSWER is evidence. A
        // failed probe must leave the broker line ageing toward stale.
        // BOTH probes fail explicitly: leaning on the "script exhausted" default
        // would silently answer the second one and refresh the very line under
        // test.
        let mut feed = loop_over(ScriptedSource::new(
            vec![Ok(vec![aapl()]), Ok(vec![aapl()])],
            vec![
                Err(FeedError::Source("gateway silent".into())),
                Err(FeedError::Source("gateway silent".into())),
            ],
        ));
        let sink = CountingSink::default();

        let first = feed.step(|| SECOND_NS, &sink);
        assert_eq!(first.broker_probe, BrokerProbe::Failed);
        assert!(first.source_error.is_some());

        // 20 s later — past the 15 s threshold with no successful probe.
        let later = feed.step(|| SECOND_NS + 20 * SECOND_NS, &sink);
        let broker = later
            .statuses
            .iter()
            .find(|s| matches!(s.feed, HeartbeatFeed::Broker))
            .expect("broker feed is watched");
        assert!(
            broker.freshness.is_stale(),
            "a broker line with no answered round trip must go stale"
        );
    }

    #[test]
    fn a_source_error_still_produces_an_evaluation() {
        // A step that bails out early would freeze the snapshot, and a frozen
        // snapshot reads exactly like a healthy one.
        let mut feed = loop_over(ScriptedSource::new(
            vec![Err(FeedError::Source("transport reset".into()))],
            vec![Ok(())],
        ));
        let sink = CountingSink::default();
        let step = feed.step(|| SECOND_NS, &sink);

        assert!(step.source_error.is_some());
        assert_eq!(step.observed_ticks, 0);
        assert_eq!(
            step.statuses.len(),
            2,
            "both watched feeds are still evaluated"
        );
    }

    #[test]
    fn a_never_delivered_line_is_stale_from_the_first_evaluation() {
        let mut feed = loop_over(ScriptedSource::new(vec![Ok(Vec::new())], vec![Ok(())]));
        let sink = CountingSink::default();
        let step = feed.step(|| SECOND_NS, &sink);

        let market = step
            .statuses
            .iter()
            .find(|s| matches!(s.feed, HeartbeatFeed::MarketData { .. }))
            .expect("the watched line is evaluated");
        assert!(market.freshness.is_stale());
        assert_eq!(market.staleness_ms, None, "no fabricated age");
    }

    #[test]
    fn the_broker_is_probed_on_cadence_not_every_step() {
        let mut feed = loop_over(ScriptedSource::new(Vec::new(), Vec::new()));
        let sink = CountingSink::default();

        feed.step(|| SECOND_NS, &sink); // first step always probes
        let early = feed.step(|| SECOND_NS + SECOND_NS, &sink); // 1 s later: not due
        assert_eq!(early.broker_probe, BrokerProbe::NotDue);
        let due = feed.step(|| SECOND_NS + 6 * SECOND_NS, &sink); // 6 s later: due
        assert_eq!(due.broker_probe, BrokerProbe::Answered);
    }

    #[test]
    fn a_cadence_at_or_above_the_threshold_is_refused() {
        let error = LiveFeedLoop::new(
            ScriptedSource::new(Vec::new(), Vec::new()),
            vec![aapl()],
            Duration::from_millis(500),
            Duration::from_millis(HEARTBEAT_STALENESS_THRESHOLD_MS),
        )
        .expect_err("a cadence at the threshold manufactures staleness");
        assert!(matches!(error, FeedError::Configuration(_)));
    }

    #[test]
    fn a_poll_budget_at_or_above_the_cadence_is_refused() {
        let error = LiveFeedLoop::new(
            ScriptedSource::new(Vec::new(), Vec::new()),
            vec![aapl()],
            Duration::from_secs(5),
            Duration::from_secs(5),
        )
        .expect_err("a drain that eats the probe interval is refused");
        assert!(matches!(error, FeedError::Configuration(_)));
    }

    #[test]
    fn an_empty_watch_set_is_refused() {
        let error = LiveFeedLoop::new(
            ScriptedSource::new(Vec::new(), Vec::new()),
            Vec::new(),
            Duration::from_millis(500),
            Duration::from_secs(5),
        )
        .expect_err("monitoring nothing must not report healthy");
        assert!(matches!(error, FeedError::Configuration(_)));
    }

    #[test]
    fn snapshot_round_trips_through_a_durable_write() {
        let dir = std::env::temp_dir().join(format!("atp-md003-snap-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("heartbeat.snapshot");

        let mut feed = loop_over(ScriptedSource::new(vec![Ok(vec![aapl()])], vec![Ok(())]));
        let sink = CountingSink::default();
        let step = feed.step(|| SECOND_NS, &sink);
        write_snapshot(&path, &step, SECOND_NS).expect("durable write");

        let text = fs::read_to_string(&path).unwrap();
        assert!(text.starts_with(SNAPSHOT_MAGIC));
        assert!(text.contains(&format!("schema_version={SNAPSHOT_SCHEMA_VERSION}")));
        assert!(text.contains(&format!("evaluated_at_ns={SECOND_NS}")));
        assert!(text.contains("feed=market_data symbol=AAPL"));
        assert!(text.contains("feed=broker"));
        // No scratch file survives a successful write.
        let leftovers: Vec<_> = fs::read_dir(&dir)
            .unwrap()
            .filter_map(Result::ok)
            .filter(|entry| entry.file_name().to_string_lossy().contains(".tmp."))
            .collect();
        assert!(leftovers.is_empty(), "scratch files must not accumulate");

        fs::remove_dir_all(&dir).ok();
    }

    /// A clock that returns each scripted reading in turn, so a test can make
    /// wall time advance ACROSS a step the way blocking I/O does.
    struct SteppingClock {
        readings: RefCell<Vec<i64>>,
    }

    impl SteppingClock {
        fn new(readings: Vec<i64>) -> Self {
            Self {
                readings: RefCell::new(readings),
            }
        }

        fn read(&self) -> i64 {
            let mut readings = self.readings.borrow_mut();
            if readings.len() == 1 {
                readings[0]
            } else {
                readings.remove(0)
            }
        }
    }

    #[test]
    fn a_broker_timeout_that_crosses_the_threshold_is_stale_immediately() {
        // The step blocks: the probe can sit on the wire until its operation
        // deadline expires. If the verdict were dated to the reading taken
        // BEFORE that wait, a broker whose last answer was just inside the
        // budget would be judged fresh while the real clock had already passed
        // 15 s — and the dashboard would stay green for exactly the window
        // this feature exists to catch.
        let mut feed = loop_over(ScriptedSource::new(
            vec![Ok(Vec::new()), Ok(Vec::new())],
            vec![Ok(()), Err(FeedError::Source("no answer".to_string()))],
        ));
        let sink = CountingSink::default();

        // Step 1 at T: the gateway answers, so the broker line is fresh.
        let first = feed.step(|| SECOND_NS, &sink);
        assert_eq!(first.broker_probe, BrokerProbe::Answered);
        assert!(!broker_status(&first).freshness.is_stale());

        // Step 2 enters at T+14 s (inside the budget) and the probe hangs
        // until its deadline: by the time there is a verdict to make, the
        // clock reads T+18 s and the broker line is 17 s old.
        let entered = SECOND_NS + 14 * SECOND_NS;
        let after_timeout = SECOND_NS + 18 * SECOND_NS;
        let clock = SteppingClock::new(vec![entered, after_timeout]);
        let second = feed.step(|| clock.read(), &sink);

        assert_eq!(second.broker_probe, BrokerProbe::Failed);
        assert_eq!(
            second.evaluated_at_ns, after_timeout,
            "the verdict must be dated after the blocking probe, not before it"
        );
        assert!(
            broker_status(&second).freshness.is_stale(),
            "a broker line 17 s without an answer must be STALE the moment the \
             probe times out, not one whole step later"
        );
        assert!(
            sink.events
                .borrow()
                .iter()
                .any(|event| event.transition == HeartbeatTransition::BecameStale),
            "the flip must be published, not merely rendered"
        );
    }

    fn broker_status(step: &FeedStep) -> &HeartbeatStatus {
        step.statuses
            .iter()
            .find(|status| matches!(status.feed, HeartbeatFeed::Broker))
            .expect("the broker line is always watched")
    }
}
