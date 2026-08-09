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
    ///
    /// A failed broker round trip lands here too, so this alone cannot tell a
    /// driver WHICH call failed — see [`FeedStep::poll_failed`].
    pub source_error: Option<FeedError>,
    /// Whether the TICK DRAIN specifically failed, as opposed to the broker
    /// probe.
    ///
    /// The two failures need opposite pacing, which is the only reason this is
    /// separate from [`FeedStep::source_error`]. A refused drain returns
    /// immediately, so a driver that loops on it spins — that is what
    /// [`degraded_backoff`] exists to slow down. A failed round trip has
    /// already spent its own wire deadline (15 s on the IB transport, the
    /// entire staleness budget) before it reports, so it is self-pacing;
    /// sleeping after it as well would only push the next snapshot write
    /// further past the threshold the dashboard ages the file by, and turn the
    /// per-feed stale evidence into a monitor-unavailable cell.
    pub poll_failed: bool,
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
        // Each parameter being individually under the threshold is NOT enough:
        // what the dashboard ages the snapshot by is the interval BETWEEN
        // writes, and a degraded loop spends a pause plus a drain in that
        // interval. `cadence=14999ms, poll_budget=14998ms` passes both checks
        // above and still leaves the file older than the 15 s guard, which the
        // reader reports as UNAVAILABLE rather than as the per-feed stale
        // verdict the acceptance criteria ask for. Refuse it here, where every
        // driver of this loop inherits the refusal.
        if poll_budget + heartbeat_cadence >= threshold {
            return Err(FeedError::Configuration(format!(
                "poll budget {poll_budget:?} plus heartbeat cadence {heartbeat_cadence:?} \
                 reaches the {threshold:?} staleness threshold — a degraded step could \
                 leave the snapshot older than the threshold it is meant to report on"
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
        let mut poll_failed = false;
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
            Err(err) => {
                source_error = Some(err);
                poll_failed = true;
            }
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
            poll_failed,
            evaluated_at_ns,
        }
    }

    fn probe_due(&self, now_ns: i64) -> bool {
        match self.last_probe_ns {
            None => true,
            Some(last) => now_ns.saturating_sub(last) >= self.heartbeat_cadence_ns,
        }
    }

    /// How long a driver should pause before the next [`step`](Self::step),
    /// given what this one did.
    ///
    /// The whole pacing rule lives here, with the timing state it depends on,
    /// rather than in each driver. Three conditions, and every one of them is
    /// about the same quantity — the interval between snapshot WRITES, which
    /// the dashboard ages the file by and reports `UNAVAILABLE` beyond:
    ///
    /// 1. **Only a failed tick drain is paced.** It returns immediately, so a
    ///    driver looping on it spins. A failed broker round trip has already
    ///    spent its own wire deadline before reporting and paces itself.
    /// 2. **The pause never crosses the next broker probe.** A probe can sit
    ///    until its transport deadline — 15 s on the IB wire, the entire
    ///    staleness budget — so a step that runs one has already committed the
    ///    whole interval, and a pause in front of it is precisely what pushes
    ///    the snapshot past the age guard. Pausing strictly less than the time
    ///    remaining until the probe is due keeps the pause and the probe in
    ///    different intervals instead of adding them together. A probe due now
    ///    yields no pause at all.
    /// 3. Otherwise [`degraded_backoff`], which reserves the following drain.
    ///
    /// Probes are due once per cadence, so a source outage is still paced on
    /// the steps in between — which is where the spin actually happens.
    pub fn pace_after(&self, step: &FeedStep, consecutive_poll_failures: u32) -> Duration {
        if !step.poll_failed {
            return Duration::ZERO;
        }
        let backoff = degraded_backoff(
            consecutive_poll_failures,
            self.heartbeat_cadence(),
            self.poll_budget,
        );
        backoff.min(self.until_next_probe(step.evaluated_at_ns))
    }

    /// How long a driver may wait before the broker probe comes due, measured
    /// from `now_ns` and kept STRICTLY short of it so a pause and a probe never
    /// land in the same write interval. Zero once a probe is due.
    fn until_next_probe(&self, now_ns: i64) -> Duration {
        let Some(last) = self.last_probe_ns else {
            return Duration::ZERO; // never probed: the next step will
        };
        let elapsed = now_ns.saturating_sub(last);
        let remaining = self.heartbeat_cadence_ns.saturating_sub(elapsed);
        u64::try_from(remaining)
            .map(Duration::from_nanos)
            .unwrap_or(Duration::ZERO)
            .saturating_sub(Duration::from_millis(1))
    }

    /// The configured broker-probe interval.
    pub fn heartbeat_cadence(&self) -> Duration {
        Duration::from_nanos(u64::try_from(self.heartbeat_cadence_ns).unwrap_or(0))
    }

    /// Read-only access to the monitor (health/status reads never publish).
    pub fn monitor(&self) -> &HeartbeatFreshnessMonitor {
        &self.monitor
    }
}

/// How long a driver should pause after `consecutive_failures` degraded steps.
///
/// [`LiveFeedLoop::step`] never sleeps — it is clock-free, and the caller owns
/// pacing. In the healthy case that pacing is implicit: the tick drain blocks
/// for its poll budget, so a step cannot complete faster than that. **A failing
/// source breaks the assumption**: a refused connection returns immediately, so
/// a driver that relies on the drain to pace it spins as fast as the failure
/// comes back — burning CPU and its step budget during exactly the outage this
/// feature exists to report. (Observed in the 2026-08-03 live window: with the
/// feed cut, a 400-step budget was gone in seconds.)
///
/// The delay doubles from `cadence / 8` and is capped by
/// [`max_degraded_backoff`]. **What must stay below the threshold is the
/// interval between snapshot WRITES, not the sleep on its own** — the pause and
/// the step that follows it are both part of that interval, so capping the
/// pause at the cadence alone is not enough. A driver that slept a full cadence
/// and then spent a full poll budget draining would leave the dashboard reading
/// a snapshot older than its own age guard, and the operator would get
/// `UNAVAILABLE` in place of the per-feed stale evidence this feature exists to
/// produce. So the cap subtracts the poll budget from the threshold, and
/// [`LiveFeedLoop::new`] refuses a configuration that leaves no room at all.
///
/// The driver still evaluates and rewrites the snapshot on EVERY step, so a
/// backed-off loop keeps re-dating the file rather than going quiet.
///
/// Zero failures means zero delay: this only paces a loop that is already
/// degraded, and it never re-times the healthy path that was live-verified.
///
/// NOT covered by this bound: the broker round trip's own wire deadline, which
/// the transport owns and this crate cannot see (the IB one is 15 s — the whole
/// staleness budget). A probe that sits to its deadline can stretch a write
/// interval past the threshold no matter what this returns. That predates this
/// pacing; see `heartbeat_freshness_contract.live_feed.write_interval` for the
/// exposure and its owner.
pub fn degraded_backoff(
    consecutive_failures: u32,
    cadence: Duration,
    poll_budget: Duration,
) -> Duration {
    if consecutive_failures == 0 {
        return Duration::ZERO;
    }
    let cap = max_degraded_backoff(cadence, poll_budget);
    let base = cadence / 8;
    // Saturating: a long outage must clamp to the cap, never wrap to a short
    // delay (which would silently restore the spin this function exists to stop).
    let delay = base
        .checked_mul(
            1_u32
                .checked_shl(consecutive_failures - 1)
                .unwrap_or(u32::MAX),
        )
        .unwrap_or(cap);
    delay.min(cap)
}

/// Slack reserved for evaluating and durably writing the snapshot, on top of
/// the two blocking calls a step makes. The write is an fsync + rename, so it
/// is small — but budgeting zero for it would put the bound exactly at the
/// threshold it has to stay under.
pub const SNAPSHOT_WRITE_SLACK: Duration = Duration::from_millis(500);

/// The longest a broker round trip's REPLY may be waited on if the step that
/// runs it is still to rewrite the snapshot inside the staleness threshold.
///
/// A transport's generic request/reply deadline is NOT this number. The IB one
/// is 15 s — sized for order operations, and equal to the entire staleness
/// budget — so a probe allowed to run that long guarantees the write interval
/// containing it exceeds the threshold, and the dashboard shows `UNAVAILABLE`
/// where the acceptance criteria require a per-feed stale verdict. A liveness
/// probe has to be bounded by the question it answers: "did the gateway reply
/// in time to still report on time?"
///
/// `send_allowance` is the caller's worst case for getting the REQUEST out —
/// the transport's socket write timeout, which a reply deadline does not cover
/// because it only starts once the request is away. A wedged peer can block a
/// send for that whole timeout, so leaving it out of the budget understates the
/// interval by exactly that much. It is a parameter rather than a constant
/// because the value belongs to the transport, which this crate must not name
/// (SRS-ARCH-001 dependency direction).
///
/// A composition layer that hands the result to its transport closes the gap;
/// one that uses the transport default does not.
pub fn broker_probe_deadline(poll_budget: Duration, send_allowance: Duration) -> Duration {
    Duration::from_millis(HEARTBEAT_STALENESS_THRESHOLD_MS)
        .saturating_sub(poll_budget)
        .saturating_sub(send_allowance)
        .saturating_sub(SNAPSHOT_WRITE_SLACK)
        .saturating_sub(Duration::from_millis(1)) // strictly below, never equal
}

/// Whether a drain budget and a send allowance leave ANY room for a reply
/// inside the staleness threshold.
///
/// [`broker_probe_deadline`] saturates at zero, and zero is not a usable
/// deadline — it is an unsatisfiable configuration wearing a number. A probe
/// given no time fails instantly, every time, and the broker line then ages
/// into a staleness alarm the monitor manufactured itself. A composition layer
/// must refuse to start instead of publishing that.
pub fn write_interval_fits(poll_budget: Duration, send_allowance: Duration) -> bool {
    broker_probe_deadline(poll_budget, send_allowance) > Duration::ZERO
}

/// The largest pause that still leaves a degraded step room to rewrite the
/// snapshot inside the staleness threshold.
///
/// `poll_budget` is subtracted because the pause is followed by a step that may
/// spend its whole drain budget before the next write. The result is also held
/// at or below `cadence`: pausing longer than the probe interval would starve
/// the broker round trip that the cadence exists to schedule.
pub fn max_degraded_backoff(cadence: Duration, poll_budget: Duration) -> Duration {
    Duration::from_millis(HEARTBEAT_STALENESS_THRESHOLD_MS)
        .saturating_sub(poll_budget)
        .saturating_sub(Duration::from_millis(1)) // strictly below, never equal
        .min(cadence)
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

    #[test]
    fn no_backoff_until_a_step_actually_fails() {
        // The healthy path is paced by the drain blocking for its poll budget,
        // and that path was verified against a live gateway. Pacing it here
        // would re-time proven behaviour to solve a problem it does not have.
        assert_eq!(
            degraded_backoff(0, Duration::from_millis(5_000), Duration::from_millis(500)),
            Duration::ZERO
        );
    }

    #[test]
    fn backoff_grows_with_consecutive_failures() {
        let cadence = Duration::from_millis(5_000);
        let budget = Duration::from_millis(500);
        let first = degraded_backoff(1, cadence, budget);
        let second = degraded_backoff(2, cadence, budget);
        let third = degraded_backoff(3, cadence, budget);

        assert!(first > Duration::ZERO, "a failed step must pause at all");
        assert!(second > first);
        assert!(third > second);
    }

    #[test]
    fn a_step_that_probes_still_rewrites_the_snapshot_inside_the_threshold() {
        // The write interval that CONTAINS a broker probe: drain + probe +
        // write slack. A transport default deadline (15 s on the IB wire — the
        // whole staleness budget) makes this interval exceed the threshold by
        // construction, whatever the poll budget is, and the dashboard reports
        // UNAVAILABLE instead of the broker-stale verdict the AC requires. The
        // composition layer must hand the transport THIS deadline instead.
        let threshold = Duration::from_millis(HEARTBEAT_STALENESS_THRESHOLD_MS);
        for budget_ms in [1_u64, 500, 1_500, 5_000, 14_000] {
            for send_ms in [0_u64, 1, 5_000, 14_000] {
                let poll_budget = Duration::from_millis(budget_ms);
                let send = Duration::from_millis(send_ms);
                // Everything a step can spend before the next write: getting
                // the request out, waiting for the reply, draining ticks, and
                // persisting the snapshot.
                if !write_interval_fits(poll_budget, send) {
                    // Unsatisfiable: the drain and the send already spend the
                    // whole budget. Reported as such so a composition refuses
                    // to start, rather than handed a zero deadline that would
                    // fail every probe and manufacture its own staleness.
                    assert_eq!(broker_probe_deadline(poll_budget, send), Duration::ZERO);
                    continue;
                }
                let worst_case = poll_budget
                    + send
                    + broker_probe_deadline(poll_budget, send)
                    + SNAPSHOT_WRITE_SLACK;
                assert!(
                    worst_case < threshold,
                    "a {budget_ms} ms drain, a {send_ms} ms send and a bounded probe \
                     plus the write slack give {worst_case:?}, at or past the \
                     {threshold:?} age guard"
                );
            }
        }
    }

    #[test]
    fn a_probe_deadline_is_never_the_whole_freshness_budget() {
        // The specific shape of the bug: a probe allowed to run as long as the
        // threshold cannot leave room for anything else in its own interval.
        let threshold = Duration::from_millis(HEARTBEAT_STALENESS_THRESHOLD_MS);
        let send = Duration::from_secs(5);
        assert!(broker_probe_deadline(Duration::from_millis(1_500), send) < threshold);
        assert_eq!(
            broker_probe_deadline(threshold, send),
            Duration::ZERO,
            "a drain that spends the entire budget must leave the probe none of it"
        );
        assert_eq!(
            broker_probe_deadline(Duration::from_millis(1_500), threshold),
            Duration::ZERO,
            "a send allowance that spends the whole budget leaves the reply none"
        );
        assert!(
            write_interval_fits(Duration::from_millis(1_500), Duration::from_secs(5)),
            "the shipped defaults must leave room for a reply"
        );
        assert!(
            !write_interval_fits(Duration::from_millis(1_500), threshold),
            "an unsatisfiable budget must be reported, not rounded to zero"
        );
    }

    #[test]
    fn a_due_broker_probe_cancels_the_pause_before_it() {
        // The composite case: the drain failed (so pacing would normally
        // apply) AND the next step will run a probe that can sit until its
        // transport deadline — 15 s on the IB wire, the entire staleness
        // budget. The interval to the next write is already fully committed,
        // so a pause on top is exactly what pushes the snapshot past the age
        // guard and turns per-feed stale evidence into UNAVAILABLE.
        let mut feed = loop_over(ScriptedSource::new(
            vec![
                Err(FeedError::Source("connection refused".to_string())),
                Err(FeedError::Source("connection refused".to_string())),
            ],
            vec![Ok(()), Ok(())],
        ));
        let sink = CountingSink::default();

        // The first step probes (none has run yet). A full cadence is then
        // left before the next one, so the spinning drain IS paced — and by
        // strictly less than that remaining time, so the pause cannot run into
        // the probe.
        let first = feed.step(|| SECOND_NS, &sink);
        assert!(first.poll_failed);
        let paused = feed.pace_after(&first, 1);
        assert!(
            paused > Duration::ZERO,
            "with the probe a whole cadence away, the spinning drain must be paced"
        );
        assert!(
            paused < Duration::from_secs(5),
            "the pause {paused:?} reached the probe it must stay short of"
        );

        // A step that lands with the probe IMMINENT — 100 ms of the cadence
        // left. The pause must shrink to fit inside that gap rather than stack
        // on top of a probe that may then take its whole transport deadline.
        // The backoff alone would be 1.25 s here, so a rule that ignored the
        // probe would put the pause and the probe in the same write interval.
        let almost = SECOND_NS + 5 * SECOND_NS - SECOND_NS / 10;
        let second = feed.step(|| almost, &sink);
        assert!(second.poll_failed);
        assert_eq!(
            second.broker_probe,
            BrokerProbe::NotDue,
            "the probe is imminent but has not run yet — that is the case under test"
        );
        let paused = feed.pace_after(&second, 2);
        assert!(
            paused < Duration::from_millis(100),
            "pause {paused:?} runs into a probe that is only 100 ms away"
        );
    }

    #[test]
    fn a_failed_broker_probe_is_not_a_step_a_driver_should_pause_after() {
        // The broker round trip spends its own wire deadline before it reports
        // — 15 s on the IB transport, the entire staleness budget. It is
        // already self-pacing, so a driver that also slept after it would push
        // the next snapshot write further past the age guard the dashboard
        // reads the file by, and the operator would get UNAVAILABLE in place of
        // the per-feed stale verdict the acceptance criteria require.
        let mut feed = loop_over(ScriptedSource::new(
            vec![Ok(Vec::new())],
            vec![Err(FeedError::Source("no answer".to_string()))],
        ));
        let sink = CountingSink::default();
        let step = feed.step(|| SECOND_NS, &sink);

        assert_eq!(step.broker_probe, BrokerProbe::Failed);
        assert!(step.source_error.is_some(), "the fault is still reported");
        assert!(
            !step.poll_failed,
            "a broker-probe failure must not be counted as the spinning drain"
        );
        assert_eq!(
            degraded_backoff(
                u32::from(step.poll_failed),
                Duration::from_secs(5),
                Duration::from_millis(1_500)
            ),
            Duration::ZERO,
            "a broker timeout must add NO pause to the write interval"
        );
    }

    #[test]
    fn a_failed_drain_is_the_step_a_driver_must_pause_after() {
        // The other half of the discriminator: a refused drain returns at once,
        // so without a pause the loop spins through its step budget during the
        // outage it is supposed to be reporting.
        let mut feed = loop_over(ScriptedSource::new(
            vec![Err(FeedError::Source("connection refused".to_string()))],
            vec![Ok(())],
        ));
        let sink = CountingSink::default();
        let step = feed.step(|| SECOND_NS, &sink);

        assert!(step.poll_failed, "a refused drain must be paced");
        assert!(
            degraded_backoff(
                u32::from(step.poll_failed),
                Duration::from_secs(5),
                Duration::from_millis(1_500)
            ) > Duration::ZERO
        );
    }

    #[test]
    fn a_pause_plus_a_drain_that_reaches_the_threshold_is_refused() {
        // Both values are individually legal — cadence is under the threshold
        // and the poll budget is under the cadence — but a degraded step pauses
        // and THEN drains, so the snapshot would be rewritten less often than
        // the 15 s guard the dashboard ages it by, and the operator would read
        // UNAVAILABLE instead of a per-feed stale verdict.
        let error = LiveFeedLoop::new(
            ScriptedSource::new(vec![], vec![]),
            vec![aapl()],
            Duration::from_millis(14_998),
            Duration::from_millis(14_999),
        )
        .expect_err("a sum that reaches the threshold must be refused");
        assert!(
            matches!(&error, FeedError::Configuration(detail) if detail.contains("staleness threshold")),
            "unexpected error: {error:?}"
        );
    }

    #[test]
    fn the_write_interval_stays_below_the_threshold_at_the_accepted_upper_bounds() {
        // The invariant the refusal above exists to protect, checked on the
        // configurations that ARE accepted: pause + drain must still leave the
        // snapshot younger than the threshold.
        let threshold = Duration::from_millis(HEARTBEAT_STALENESS_THRESHOLD_MS);
        for (cadence_ms, budget_ms) in [(5_000_u64, 1_500_u64), (14_000, 999), (200, 100), (2, 1)] {
            let cadence = Duration::from_millis(cadence_ms);
            let poll_budget = Duration::from_millis(budget_ms);
            assert!(
                LiveFeedLoop::new(
                    ScriptedSource::new(vec![], vec![]),
                    vec![aapl()],
                    poll_budget,
                    cadence,
                )
                .is_ok(),
                "{cadence_ms}/{budget_ms} should be an accepted configuration"
            );
            for failures in [1_u32, 8, 64, u32::MAX] {
                let worst_case = degraded_backoff(failures, cadence, poll_budget) + poll_budget;
                assert!(
                    worst_case < threshold,
                    "cadence {cadence_ms} ms + budget {budget_ms} ms after {failures} failures \
                     gives a {worst_case:?} write interval, at or past the {threshold:?} guard"
                );
            }
        }
    }

    #[test]
    fn backoff_never_delays_an_evaluation_past_the_staleness_threshold() {
        // THE safety property, held for EVERY pair a caller might pass — not
        // only the ones `LiveFeedLoop::new` accepts. `degraded_backoff` is
        // public, so its own contract has to be total: a backoff that outgrew
        // the threshold would suppress the detection it is pacing, and an
        // arithmetic overflow that wrapped would silently restore the spin.
        let threshold = Duration::from_millis(HEARTBEAT_STALENESS_THRESHOLD_MS);
        for cadence_ms in [1_u64, 250, 5_000, HEARTBEAT_STALENESS_THRESHOLD_MS - 1] {
            let cadence = Duration::from_millis(cadence_ms);
            for budget_ms in [1_u64, 100, 1_500, cadence_ms.saturating_sub(1).max(1)] {
                let poll_budget = Duration::from_millis(budget_ms);
                for failures in [1_u32, 2, 5, 33, 64, 1_000, u32::MAX] {
                    let delay = degraded_backoff(failures, cadence, poll_budget);
                    assert!(
                        delay <= cadence,
                        "backoff {delay:?} after {failures} failures exceeded the \
                         {cadence:?} cadence it must stay within"
                    );
                    assert!(
                        delay + poll_budget < threshold,
                        "backoff {delay:?} plus a {poll_budget:?} drain reaches the \
                         {threshold:?} staleness threshold — the snapshot would be \
                         rewritten less often than the guard that ages it"
                    );
                }
            }
        }
    }

    fn broker_status(step: &FeedStep) -> &HeartbeatStatus {
        step.statuses
            .iter()
            .find(|status| matches!(status.feed, HeartbeatFeed::Broker))
            .expect("the broker line is always watched")
    }
}
