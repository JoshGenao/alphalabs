//! Latency-percentile verification substrate (SRS-PERF-001, SyRS §5.1 NFR-P1 /
//! NFR-P4 / NFR-P5 / NFR-P6 / NFR-P9 / NFR-P10; StRS SN-1.01 / SN-2.03).
//!
//! SRS-PERF-001 requires the software to *measure latency-sensitive performance
//! metrics against a Precision Time Protocol (PTP)–disciplined system clock with
//! documented offset bounds* and to *report p50, p95, p99, and p99.9 percentiles
//! in verification artifacts* for the latency NFRs (NFR-P1, NFR-P4, NFR-P5,
//! NFR-P6, NFR-P9, NFR-P10) and the SRS-MD-001 subscription fan-out latency.
//!
//! This module is the vendor- and runtime-neutral **measurement substrate** every
//! NFR verification consumes:
//!
//!   * [`nearest_rank_percentile_ns`] / [`LatencyPercentiles`] — the pure,
//!     deterministic percentile engine (p50/p95/p99/p99.9 over a latency sample
//!     set, in nanoseconds), using the **nearest-rank** method so every reported
//!     percentile is an actually-observed sample (no interpolation, no fabricated
//!     value between samples).
//!   * [`PtpClockDiscipline`] — the measurement clock's PTP-sync state carrying
//!     the *documented maximum observed offset bound* for the window. A
//!     verification artifact can only be built against a `Disciplined` clock, so
//!     the offset bound the SRS requires is always present.
//!   * [`LatencyNfr`] / [`LATENCY_NFRS`] — the catalog binding each NFR id to its
//!     SyRS §5.1 measurement boundary + threshold(s), so an artifact's
//!     measurement boundaries match the SyRS measurement conditions.
//!   * [`LatencyVerificationArtifact`] — the report type. Its constructor
//!     ([`LatencyVerificationArtifact::from_samples`]) **fails closed** when the
//!     clock is not PTP-disciplined, when there are no samples, or when the
//!     measurement window is empty/inverted — so a produced artifact always
//!     carries the four percentiles, a documented clock offset bound, and a
//!     non-empty window.
//!
//! ## What this module deliberately does NOT do
//!
//! It performs no measurement itself: it neither reads a wall clock nor times any
//! operation (the crate is dependency-free and I/O-free — the same discipline
//! [`crate::SequenceGapEvent`] follows by taking a caller-supplied `observed_at_ns`).
//! The *samples* and the *clock offset bound* are supplied by the runtime systems
//! whose latency each NFR measures — the live order path (SRS-EXE-001), the
//! order-event dispatchers (SRS-SDK-004 / SRS-SIM-001), the heartbeat monitor
//! (SRS-MD-003), the notification dispatcher (SRS-NOTIF-001), the orchestrator
//! startup path (SRS-ORCH-001), and the subscription fan-out (SRS-MD-001). Those
//! runtimes, plus a PTP-disciplined host clock, are required to produce the *real*
//! verification artifacts end to end, so **SRS-PERF-001 stays `passes:false`** in
//! `feature_list.json` until they land; this substrate is the prerequisite surface
//! they consume. Pass/fail *evaluation* of a reported percentile against a
//! threshold is likewise the verification activity performed when real data
//! exists; the catalog exposes the thresholds so that evaluation has a single
//! source of truth, but this module reports — it does not adjudicate — a metric.

use std::fmt;

use crate::order_event::{LIVE_CALLBACK_LATENCY_P95_MS, PAPER_CALLBACK_LATENCY_P95_MS};
use crate::STRATEGY_STARTUP_DEADLINE_MS;

// --------------------------------------------------------------------------- //
// NFR threshold constants (SyRS §5.1)
// --------------------------------------------------------------------------- //
//
// The single source of truth for each threshold is SyRS §5.1; `tools/
// perf_measurement_check.py` parses the SyRS performance table and asserts the
// catalog below (and the `perf_measurement_contract` metadata block) match it,
// so a future NFR revision changes the number in exactly one place (the SyRS)
// and the drift is caught. Values are milliseconds to match the SyRS statement;
// samples and percentiles are nanoseconds (finer than the millisecond budgets so
// no measurement resolution is lost).

/// NFR-P1 order signal-to-acknowledgement latency budget: `< 1,000 ms` at p95.
pub const ORDER_SIGNAL_TO_ACK_LATENCY_P95_MS: u64 = 1_000;

/// NFR-P5 heartbeat staleness detection threshold: `≤ 15,000 ms`. No dedicated
/// constant existed in the core before SRS-PERF-001 (only the
/// [`crate::StaleDataEvent`] surface, which carries an observed age, not the
/// budget); this is the budget the SyRS states.
pub const HEARTBEAT_STALENESS_THRESHOLD_MS: u64 = 15_000;

/// NFR-P6 connectivity-loss notification delivery budget: `≤ 60,000 ms` from
/// detection to dispatch via email and SMS. The authoritative runtime constant
/// is `atp_notification::event::DISPATCH_SLA_MS` (also `60_000`); it lives in a
/// crate *above* `atp-types` (which is dependency-free) so it cannot be imported
/// here. Both trace SyRS NFR-P6 and are pinned to it by
/// `tools/perf_measurement_check.py`, so they cannot silently diverge.
pub const CONNECTIVITY_NOTIFICATION_SLA_MS: u64 = 60_000;

/// NFR-P2 dashboard refresh latency budget: `≤ 5,000 ms`. Named here because
/// SRS-PERF-001's NFR-P10 requires meeting NFR-P1 order latency AND NFR-P2
/// dashboard refresh *simultaneously* under the NFR-SC1 peak-load baseline, so
/// the NFR-P10 catalog entry carries both legs (order latency + dashboard
/// refresh); reporting only the order-latency leg would leave the NFR-P10
/// dashboard-refresh requirement unverified.
pub const DASHBOARD_REFRESH_LATENCY_MS: u64 = 5_000;

/// SRS-MD-001 consolidated subscription fan-out latency budget: `≤ 100 ms`
/// additional latency relative to the IB feed, per the SRS-MD-001 acceptance
/// criterion ("each subscriber receives fan-out data with no more than 100 ms
/// additional latency"; docs/SRS.md). SRS-PERF-001 names SRS-MD-001 fan-out
/// latency as one of the reported metrics, so this is a real budget (in the SRS
/// requirement row, not the SyRS §5.1 NFR table); `tools/perf_measurement_check.py`
/// pins it to docs/SRS.md.
pub const SUBSCRIPTION_FANOUT_LATENCY_MS: u64 = 100;

// NFR-P4 live/paper callback budgets reuse the existing order-event constants
// (`LIVE_CALLBACK_LATENCY_P95_MS` = 1_000, `PAPER_CALLBACK_LATENCY_P95_MS` = 100)
// and NFR-P9 reuses `STRATEGY_STARTUP_DEADLINE_MS` (= 30_000) — no duplication.

// --------------------------------------------------------------------------- //
// Percentiles
// --------------------------------------------------------------------------- //

/// The four latency percentiles SRS-PERF-001 requires every verification
/// artifact to report.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Percentile {
    P50,
    P95,
    P99,
    /// The 99.9th percentile — the tail SRS-PERF-001 names explicitly.
    P999,
}

/// The exact percentile set SRS-PERF-001 requires, in ascending order. Pinned to
/// the `perf_measurement_contract.reported_percentiles` metadata by the check
/// tool so the reported set cannot drift.
pub const REPORTED_PERCENTILES: [Percentile; 4] = [
    Percentile::P50,
    Percentile::P95,
    Percentile::P99,
    Percentile::P999,
];

impl Percentile {
    /// The canonical wire label (`"p50"`, `"p95"`, `"p99"`, `"p99.9"`).
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::P50 => "p50",
            Self::P95 => "p95",
            Self::P99 => "p99",
            Self::P999 => "p99.9",
        }
    }

    /// The percentile expressed in parts-per-thousand (`500`, `950`, `990`,
    /// `999`). Integer-valued so the nearest-rank computation is exact and never
    /// depends on floating-point rounding — `p99.9` is `999`, not `0.999`.
    pub const fn per_mille(self) -> u32 {
        match self {
            Self::P50 => 500,
            Self::P95 => 950,
            Self::P99 => 990,
            Self::P999 => 999,
        }
    }
}

impl fmt::Display for Percentile {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// The **nearest-rank** percentile of a latency sample set, in nanoseconds.
///
/// Returns `None` for an empty slice (fail closed — with no data there is no
/// percentile to report). Otherwise the samples are sorted ascending and the
/// result is the sample at 1-indexed rank `ceil(per_mille/1000 · N)`, clamped to
/// `[1, N]`. Because the rank is a real index, the returned value is always an
/// actually-observed sample: no interpolation, so an integer-nanosecond latency
/// distribution yields an integer-nanosecond percentile and the harness never
/// fabricates a value that was never measured.
///
/// **Tail resolution:** `p99.9` can only resolve to a value distinct from the
/// maximum once `N ≥ 1000` (below that, `ceil(0.999·N)` is `N`); with fewer
/// samples it collapses to the observed maximum. [`LatencyPercentiles`] carries
/// the `sample_count` so a report reader can judge tail resolution — see
/// [`LatencyPercentiles::resolves_p999`].
pub fn nearest_rank_percentile_ns(samples: &[u64], p: Percentile) -> Option<u64> {
    if samples.is_empty() {
        return None;
    }
    let mut sorted = samples.to_vec();
    sorted.sort_unstable();
    Some(nearest_rank_on_sorted(&sorted, p))
}

/// Nearest-rank on an already-ascending slice. Panics only on an empty slice,
/// which the public entry points never pass in.
fn nearest_rank_on_sorted(sorted: &[u64], p: Percentile) -> u64 {
    debug_assert!(!sorted.is_empty());
    let n = sorted.len() as u128;
    // rank = ceil(per_mille · N / 1000), computed in u128 so `per_mille · N`
    // cannot overflow for any realistic sample count.
    let rank = (p.per_mille() as u128 * n).div_ceil(1_000);
    let rank = rank.clamp(1, n) as usize;
    sorted[rank - 1]
}

/// The p50/p95/p99/p99.9 latencies (nanoseconds) computed over one sample set,
/// with the `sample_count` they were derived from. Constructed via
/// [`LatencyPercentiles::from_samples`], which fails closed on an empty set. By
/// construction `p50 ≤ p95 ≤ p99 ≤ p99.9` (increasing rank over sorted data).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LatencyPercentiles {
    p50_ns: u64,
    p95_ns: u64,
    p99_ns: u64,
    p999_ns: u64,
    sample_count: usize,
}

impl LatencyPercentiles {
    /// Compute the four percentiles from raw latency samples (nanoseconds).
    /// Returns [`PerfMeasurementError::NoSamples`] on an empty slice.
    pub fn from_samples(samples: &[u64]) -> Result<Self, PerfMeasurementError> {
        if samples.is_empty() {
            return Err(PerfMeasurementError::NoSamples);
        }
        let mut sorted = samples.to_vec();
        sorted.sort_unstable();
        Ok(Self {
            p50_ns: nearest_rank_on_sorted(&sorted, Percentile::P50),
            p95_ns: nearest_rank_on_sorted(&sorted, Percentile::P95),
            p99_ns: nearest_rank_on_sorted(&sorted, Percentile::P99),
            p999_ns: nearest_rank_on_sorted(&sorted, Percentile::P999),
            sample_count: sorted.len(),
        })
    }

    /// The requested percentile, in nanoseconds.
    pub const fn get_ns(&self, p: Percentile) -> u64 {
        match p {
            Percentile::P50 => self.p50_ns,
            Percentile::P95 => self.p95_ns,
            Percentile::P99 => self.p99_ns,
            Percentile::P999 => self.p999_ns,
        }
    }

    /// The requested percentile in milliseconds (for comparison against the
    /// SyRS millisecond budgets). Lossy `f64` — for exact work use [`get_ns`].
    ///
    /// [`get_ns`]: Self::get_ns
    pub fn get_millis_f64(&self, p: Percentile) -> f64 {
        self.get_ns(p) as f64 / 1_000_000.0
    }

    pub const fn p50_ns(&self) -> u64 {
        self.p50_ns
    }
    pub const fn p95_ns(&self) -> u64 {
        self.p95_ns
    }
    pub const fn p99_ns(&self) -> u64 {
        self.p99_ns
    }
    pub const fn p999_ns(&self) -> u64 {
        self.p999_ns
    }
    pub const fn sample_count(&self) -> usize {
        self.sample_count
    }

    /// Whether the sample count is large enough for `p99.9` to resolve to a
    /// value distinct from the observed maximum under the nearest-rank method
    /// (`N ≥ 1000`). Documented, not enforced: the runtime that supplies the
    /// samples decides sizing, but a report reader must know whether the tail is
    /// meaningfully resolved.
    pub const fn resolves_p999(&self) -> bool {
        self.sample_count >= 1_000
    }
}

// --------------------------------------------------------------------------- //
// PTP clock discipline
// --------------------------------------------------------------------------- //

/// The PTP-sync state of the host clock the latency samples were measured
/// against, carrying the documented offset bound SRS-PERF-001 requires.
///
/// A latency measurement is only as trustworthy as the clock it is timed on;
/// the SRS mandates a **PTP-disciplined** clock with **documented offset
/// bounds**. `Disciplined { max_offset_ns }` records the maximum absolute clock
/// offset observed during the measurement window — the bound that goes into the
/// artifact. `Undisciplined` means no bounded offset is available, so an artifact
/// built against it cannot claim NFR conformance and
/// [`LatencyVerificationArtifact::from_samples`] rejects it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PtpClockDiscipline {
    /// PTP-disciplined: `max_offset_ns` is the maximum absolute offset observed
    /// during the measurement window (the documented offset bound).
    Disciplined { max_offset_ns: u64 },
    /// Not PTP-disciplined — no bounded offset. Latency artifacts fail closed.
    Undisciplined,
}

impl PtpClockDiscipline {
    /// Whether the clock is PTP-disciplined (has a documented offset bound).
    pub const fn is_disciplined(self) -> bool {
        matches!(self, Self::Disciplined { .. })
    }

    /// The documented maximum observed offset in nanoseconds, or `None` when the
    /// clock is not disciplined.
    pub const fn max_offset_ns(self) -> Option<u64> {
        match self {
            Self::Disciplined { max_offset_ns } => Some(max_offset_ns),
            Self::Undisciplined => None,
        }
    }
}

// --------------------------------------------------------------------------- //
// NFR catalog (measurement boundaries + thresholds)
// --------------------------------------------------------------------------- //

/// How a reported latency is compared against its budget, matching the SyRS
/// statement (`<` for the strict p95 budgets, `≤` for the detection/delivery
/// thresholds).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ThresholdComparison {
    LessThan,
    LessThanOrEqual,
}

impl ThresholdComparison {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::LessThan => "<",
            Self::LessThanOrEqual => "<=",
        }
    }
}

/// One SyRS §5.1 latency budget for a single **leg** of an NFR. Most NFRs have a
/// single bound; NFR-P4 carries two (`"live"` and `"paper"`) and NFR-P10 two
/// (`"order_latency"` and `"dashboard_refresh"`). `label` is empty for a
/// single-leg NFR.
///
/// `stated_percentile` is **per leg**, because a single NFR can mix budget
/// semantics: NFR-P10's `order_latency` leg is a p95 budget (`Some(P95)`) while
/// its `dashboard_refresh` leg is a flat maximum (`None`). Carrying it per leg
/// stops a verifier from applying p95 evaluation to a flat-threshold leg.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LatencyThreshold {
    /// Disambiguates multi-leg NFRs (`"live"` / `"paper"` for NFR-P4,
    /// `"order_latency"` / `"dashboard_refresh"` for NFR-P10); empty otherwise.
    pub label: &'static str,
    /// The budget in milliseconds (the SyRS unit).
    pub bound_ms: u64,
    pub comparison: ThresholdComparison,
    /// The percentile the SyRS states THIS leg's budget against, when it states
    /// one (`Some(P95)` for the p95 budgets; `None` for a flat detection/delivery
    /// maximum). All four percentiles are reported regardless — this records
    /// which one the budget is written against so the flat-max legs (NFR-P5/P6/P9
    /// and NFR-P10's dashboard-refresh) are never evaluated as p95.
    pub stated_percentile: Option<Percentile>,
}

static P1_THRESHOLDS: [LatencyThreshold; 1] = [LatencyThreshold {
    label: "",
    bound_ms: ORDER_SIGNAL_TO_ACK_LATENCY_P95_MS,
    comparison: ThresholdComparison::LessThan,
    stated_percentile: Some(Percentile::P95),
}];

static P4_THRESHOLDS: [LatencyThreshold; 2] = [
    LatencyThreshold {
        label: "live",
        bound_ms: LIVE_CALLBACK_LATENCY_P95_MS as u64,
        comparison: ThresholdComparison::LessThan,
        stated_percentile: Some(Percentile::P95),
    },
    LatencyThreshold {
        label: "paper",
        bound_ms: PAPER_CALLBACK_LATENCY_P95_MS as u64,
        comparison: ThresholdComparison::LessThan,
        stated_percentile: Some(Percentile::P95),
    },
];

static P5_THRESHOLDS: [LatencyThreshold; 1] = [LatencyThreshold {
    label: "",
    bound_ms: HEARTBEAT_STALENESS_THRESHOLD_MS,
    comparison: ThresholdComparison::LessThanOrEqual,
    stated_percentile: None,
}];

static P6_THRESHOLDS: [LatencyThreshold; 1] = [LatencyThreshold {
    label: "",
    bound_ms: CONNECTIVITY_NOTIFICATION_SLA_MS,
    comparison: ThresholdComparison::LessThanOrEqual,
    stated_percentile: None,
}];

static P9_THRESHOLDS: [LatencyThreshold; 1] = [LatencyThreshold {
    label: "",
    bound_ms: STRATEGY_STARTUP_DEADLINE_MS,
    comparison: ThresholdComparison::LessThanOrEqual,
    stated_percentile: None,
}];

// NFR-P10 is a simultaneity property: NFR-P1 order latency AND NFR-P2 dashboard
// refresh must both hold under the NFR-SC1 baseline, so it carries both legs —
// with MIXED semantics: order_latency is p95, dashboard_refresh is a flat max.
static P10_THRESHOLDS: [LatencyThreshold; 2] = [
    LatencyThreshold {
        label: "order_latency",
        bound_ms: ORDER_SIGNAL_TO_ACK_LATENCY_P95_MS,
        comparison: ThresholdComparison::LessThan,
        stated_percentile: Some(Percentile::P95),
    },
    LatencyThreshold {
        label: "dashboard_refresh",
        bound_ms: DASHBOARD_REFRESH_LATENCY_MS,
        comparison: ThresholdComparison::LessThanOrEqual,
        stated_percentile: None,
    },
];

static FANOUT_THRESHOLDS: [LatencyThreshold; 1] = [LatencyThreshold {
    label: "",
    bound_ms: SUBSCRIPTION_FANOUT_LATENCY_MS,
    comparison: ThresholdComparison::LessThanOrEqual,
    stated_percentile: None,
}];

/// The latency NFRs SRS-PERF-001 produces verification artifacts for. Each binds
/// an id to its SyRS §5.1 measurement boundary and budget(s).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum LatencyNfr {
    /// NFR-P1 — order signal-to-acknowledgement latency.
    OrderSignalToAck,
    /// NFR-P4 — order event callback delivery latency (live + paper).
    OrderEventCallback,
    /// NFR-P5 — heartbeat staleness detection threshold.
    HeartbeatStaleness,
    /// NFR-P6 — connectivity-loss notification delivery latency.
    ConnectivityNotification,
    /// NFR-P9 — strategy container startup time.
    StrategyStartup,
    /// NFR-P10 — order latency (NFR-P1) under the NFR-SC1 peak-load baseline.
    PeakLoadOrderLatency,
    /// SRS-MD-001 — consolidated subscription fan-out delivery latency.
    SubscriptionFanout,
}

/// The full catalog, in AC-declaration order (NFR-P1, NFR-P4, NFR-P5, NFR-P6,
/// NFR-P9, NFR-P10, SRS-MD-001 fan-out).
pub const LATENCY_NFRS: [LatencyNfr; 7] = [
    LatencyNfr::OrderSignalToAck,
    LatencyNfr::OrderEventCallback,
    LatencyNfr::HeartbeatStaleness,
    LatencyNfr::ConnectivityNotification,
    LatencyNfr::StrategyStartup,
    LatencyNfr::PeakLoadOrderLatency,
    LatencyNfr::SubscriptionFanout,
];

impl LatencyNfr {
    /// The canonical requirement id (`"NFR-P1"` … `"NFR-P10"`, `"SRS-MD-001"`).
    pub const fn id(self) -> &'static str {
        match self {
            Self::OrderSignalToAck => "NFR-P1",
            Self::OrderEventCallback => "NFR-P4",
            Self::HeartbeatStaleness => "NFR-P5",
            Self::ConnectivityNotification => "NFR-P6",
            Self::StrategyStartup => "NFR-P9",
            Self::PeakLoadOrderLatency => "NFR-P10",
            Self::SubscriptionFanout => "SRS-MD-001",
        }
    }

    /// A short human-readable metric name.
    pub const fn metric(self) -> &'static str {
        match self {
            Self::OrderSignalToAck => "order signal-to-acknowledgement latency",
            Self::OrderEventCallback => "order event callback delivery latency",
            Self::HeartbeatStaleness => "heartbeat staleness detection threshold",
            Self::ConnectivityNotification => "connectivity-loss notification delivery latency",
            Self::StrategyStartup => "strategy container startup time",
            Self::PeakLoadOrderLatency => "peak-load order latency (NFR-P1 under NFR-SC1)",
            Self::SubscriptionFanout => "consolidated subscription fan-out delivery latency",
        }
    }

    /// The SyRS §5.1 measurement boundary — the start and end points the latency
    /// is measured between. Kept faithful to the SyRS "Condition" column so an
    /// artifact's measurement boundary matches the SyRS measurement condition;
    /// `tools/perf_measurement_check.py` asserts a distinctive phrase of each of
    /// these appears in the corresponding SyRS row.
    pub const fn boundary(self) -> &'static str {
        match self {
            Self::OrderSignalToAck => {
                "from the live strategy container's invocation of the order submission API to \
                 the strategy container's receipt of the order acknowledgement callback, \
                 excluding IB-to-exchange network round-trip time, under the reference hardware \
                 baseline with 1 live and at least 30 paper strategy containers active"
            }
            Self::OrderEventCallback => {
                "from broker fill acknowledgement (live) or simulated fill (paper) to user \
                 strategy code callback"
            }
            Self::HeartbeatStaleness => "continuous during market hours",
            Self::ConnectivityNotification => {
                "from detection to notification dispatch via email and SMS"
            }
            Self::StrategyStartup => {
                "from orchestrator start command to strategy ready (warm-up excluded)"
            }
            Self::PeakLoadOrderLatency => {
                "order latency (NFR-P1) with the baseline active strategy set defined by NFR-SC1 \
                 processing market data events (measured simultaneously with dashboard refresh, \
                 NFR-P2)"
            }
            Self::SubscriptionFanout => {
                "from a received upstream tick on a consolidated security line to fan-out \
                 delivery at each subscriber (SRS-MD-001 / SyRS SYS-70)"
            }
        }
    }

    /// The latency budget(s) for this NFR. Most NFRs have one; NFR-P4 has two
    /// (`live` + `paper`) and NFR-P10 has two (`order_latency` +
    /// `dashboard_refresh`) — a multi-leg NFR is only fully verified when every
    /// leg is measured (see [`NfrVerification`]).
    pub fn thresholds(self) -> &'static [LatencyThreshold] {
        match self {
            Self::OrderSignalToAck => &P1_THRESHOLDS,
            Self::OrderEventCallback => &P4_THRESHOLDS,
            Self::HeartbeatStaleness => &P5_THRESHOLDS,
            Self::ConnectivityNotification => &P6_THRESHOLDS,
            Self::StrategyStartup => &P9_THRESHOLDS,
            Self::PeakLoadOrderLatency => &P10_THRESHOLDS,
            Self::SubscriptionFanout => &FANOUT_THRESHOLDS,
        }
    }

    /// The leg labels of this NFR (e.g. `[""]` for a single-leg NFR,
    /// `["live", "paper"]` for NFR-P4, `["order_latency", "dashboard_refresh"]`
    /// for NFR-P10). A complete [`NfrVerification`] must cover every leg.
    pub fn threshold_labels(self) -> Vec<&'static str> {
        self.thresholds().iter().map(|t| t.label).collect()
    }

    /// The threshold for `leg`, or `None` when `leg` is not one of this NFR's
    /// legs — the fail-closed check a [`LatencyVerificationArtifact`] applies so
    /// samples cannot be attributed to a leg the NFR does not define.
    pub fn threshold_for_leg(self, leg: &str) -> Option<&'static LatencyThreshold> {
        self.thresholds().iter().find(|t| t.label == leg)
    }

    /// Whether this NFR's legs must be measured **simultaneously** (their
    /// measurement windows must overlap). True only for NFR-P10, whose SyRS
    /// condition requires NFR-P1 order latency and NFR-P2 dashboard refresh to
    /// hold *simultaneously* under the NFR-SC1 peak-load baseline. NFR-P4's legs
    /// (`live` on the broker path, `paper` on the simulation path) are distinct
    /// systems measured independently, so they are NOT required to be
    /// simultaneous. [`NfrVerification::assemble`] enforces window overlap when
    /// this is true.
    pub const fn requires_simultaneous_legs(self) -> bool {
        matches!(self, Self::PeakLoadOrderLatency)
    }
}
// NOTE: the percentile a budget is stated against is PER LEG — see
// `LatencyThreshold::stated_percentile`. It is intentionally NOT an NFR-wide
// method, because NFR-P10 mixes semantics (order_latency = p95,
// dashboard_refresh = flat max), so a single NFR-wide value would let a verifier
// evaluate the flat dashboard-refresh leg as a p95 budget.

// --------------------------------------------------------------------------- //
// Verification artifact
// --------------------------------------------------------------------------- //

/// Fail-closed construction errors for a [`LatencyVerificationArtifact`] and an
/// [`NfrVerification`] bundle.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PerfMeasurementError {
    /// No latency samples were supplied — there is nothing to report a
    /// percentile over.
    NoSamples,
    /// The measurement clock was not PTP-disciplined, so no documented offset
    /// bound is available and the latency cannot be claimed against a
    /// PTP-disciplined host clock (SRS-PERF-001).
    ClockNotDisciplined,
    /// The measurement window was empty or inverted (`end_ns <= start_ns`); a
    /// verification artifact must document a real, non-empty window.
    EmptyMeasurementWindow { start_ns: i64, end_ns: i64 },
    /// The measurement window's duration (`end_ns - start_ns`) overflows `i64` —
    /// the endpoints are individually valid and ordered but too far apart to
    /// represent a duration, so the documented window would be corrupt. Rejected
    /// at construction rather than panicking / wrapping when the duration is read.
    MeasurementWindowOverflow { start_ns: i64, end_ns: i64 },
    /// The supplied threshold leg label is not one of the NFR's legs (e.g. a
    /// label other than `"live"`/`"paper"` for NFR-P4, or `"order_latency"`/
    /// `"dashboard_refresh"` for NFR-P10). A multi-leg NFR's samples must name the
    /// leg they measured so the artifact cannot mis-attribute or conflate legs.
    UnknownThresholdLeg { nfr_id: &'static str, leg: String },
    /// An [`NfrVerification`] bundle for `nfr_id` is not a complete match of the
    /// NFR's legs: a leg is missing, a leg is duplicated, or an artifact belongs
    /// to a different NFR. A multi-leg NFR (NFR-P4 live+paper, NFR-P10
    /// order-latency+dashboard-refresh) is only verified when EVERY leg is
    /// present exactly once (`detail` names the specific problem).
    IncompleteNfrVerification {
        nfr_id: &'static str,
        detail: String,
    },
}

impl fmt::Display for PerfMeasurementError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoSamples => write!(f, "no latency samples supplied"),
            Self::ClockNotDisciplined => write!(
                f,
                "measurement clock is not PTP-disciplined; no documented offset bound is \
                 available (SRS-PERF-001 requires a PTP-disciplined host clock)"
            ),
            Self::EmptyMeasurementWindow { start_ns, end_ns } => write!(
                f,
                "measurement window is empty or inverted (start_ns={start_ns}, end_ns={end_ns})"
            ),
            Self::MeasurementWindowOverflow { start_ns, end_ns } => write!(
                f,
                "measurement window duration overflows i64 (start_ns={start_ns}, end_ns={end_ns})"
            ),
            Self::UnknownThresholdLeg { nfr_id, leg } => write!(
                f,
                "{nfr_id} has no threshold leg {leg:?} (samples must name a valid leg)"
            ),
            Self::IncompleteNfrVerification { nfr_id, detail } => {
                write!(f, "incomplete {nfr_id} verification: {detail}")
            }
        }
    }
}

impl std::error::Error for PerfMeasurementError {}

/// An SRS-PERF-001 latency verification artifact for one **leg** of one NFR: the
/// four reported percentiles, the documented maximum clock offset, and the
/// measurement window, bound to the NFR + threshold leg whose boundary the
/// measurement observed.
///
/// Single-leg NFRs (NFR-P1/P5/P6/P9, SRS-MD-001 fan-out) have the sole leg `""`;
/// multi-leg NFRs bind to a named leg (NFR-P4 `"live"`/`"paper"`, NFR-P10
/// `"order_latency"`/`"dashboard_refresh"`) so the samples cannot be
/// mis-attributed or conflated across legs. A complete verification of a
/// multi-leg NFR is an [`NfrVerification`] bundle covering every leg.
///
/// The only constructor, [`from_samples`](Self::from_samples), fails closed
/// unless the clock is PTP-disciplined, the leg is one of the NFR's legs, the
/// window is real, and the sample set is non-empty — so every constructed
/// artifact reports p50/p95/p99/p99.9 (via [`percentiles`](Self::percentiles))
/// and documents the maximum observed clock offset (via
/// [`max_clock_offset_ns`](Self::max_clock_offset_ns)) for a known leg.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LatencyVerificationArtifact {
    nfr: LatencyNfr,
    /// Which threshold leg of `nfr` these samples measure (canonical label from
    /// the catalog; `""` for a single-leg NFR).
    threshold_label: &'static str,
    max_clock_offset_ns: u64,
    window_start_ns: i64,
    window_end_ns: i64,
    /// Validated at construction via `checked_sub` (always `> 0`); stored so the
    /// accessor never re-subtracts (which could overflow for extreme endpoints).
    window_duration_ns: i64,
    percentiles: LatencyPercentiles,
}

impl LatencyVerificationArtifact {
    /// Build an artifact from raw latency samples (nanoseconds) for `nfr`'s `leg`,
    /// measured over `[window_start_ns, window_end_ns]` against `clock`.
    ///
    /// Fails closed, in order, on: a non-PTP-disciplined clock
    /// ([`PerfMeasurementError::ClockNotDisciplined`] — the PTP requirement is
    /// primary), a `leg` that is not one of `nfr`'s legs
    /// ([`PerfMeasurementError::UnknownThresholdLeg`]), an empty/inverted window
    /// ([`PerfMeasurementError::EmptyMeasurementWindow`]), a window whose duration
    /// overflows `i64` ([`PerfMeasurementError::MeasurementWindowOverflow`]), or an
    /// empty sample set ([`PerfMeasurementError::NoSamples`]).
    pub fn from_samples(
        nfr: LatencyNfr,
        leg: &str,
        clock: PtpClockDiscipline,
        window_start_ns: i64,
        window_end_ns: i64,
        samples: &[u64],
    ) -> Result<Self, PerfMeasurementError> {
        let max_clock_offset_ns = clock
            .max_offset_ns()
            .ok_or(PerfMeasurementError::ClockNotDisciplined)?;
        let threshold_label = nfr
            .threshold_for_leg(leg)
            .ok_or_else(|| PerfMeasurementError::UnknownThresholdLeg {
                nfr_id: nfr.id(),
                leg: leg.to_string(),
            })?
            .label;
        if window_end_ns <= window_start_ns {
            return Err(PerfMeasurementError::EmptyMeasurementWindow {
                start_ns: window_start_ns,
                end_ns: window_end_ns,
            });
        }
        // Endpoints are ordered but may be too far apart to represent a duration;
        // validate once here so the accessor never overflows.
        let window_duration_ns = window_end_ns.checked_sub(window_start_ns).ok_or(
            PerfMeasurementError::MeasurementWindowOverflow {
                start_ns: window_start_ns,
                end_ns: window_end_ns,
            },
        )?;
        let percentiles = LatencyPercentiles::from_samples(samples)?;
        Ok(Self {
            nfr,
            threshold_label,
            max_clock_offset_ns,
            window_start_ns,
            window_end_ns,
            window_duration_ns,
            percentiles,
        })
    }

    pub const fn nfr(&self) -> LatencyNfr {
        self.nfr
    }

    pub const fn nfr_id(&self) -> &'static str {
        self.nfr.id()
    }

    /// The threshold leg these samples measure (`""` for a single-leg NFR).
    pub const fn threshold_label(&self) -> &'static str {
        self.threshold_label
    }

    /// The budget this leg is measured against.
    pub fn threshold(&self) -> &'static LatencyThreshold {
        self.nfr
            .threshold_for_leg(self.threshold_label)
            .expect("threshold_label is a validated leg of nfr")
    }

    /// The documented maximum observed clock offset for the measurement window,
    /// in nanoseconds (always present — construction requires a disciplined
    /// clock). This is the SRS-PERF-001 "documented offset bound".
    pub const fn max_clock_offset_ns(&self) -> u64 {
        self.max_clock_offset_ns
    }

    pub const fn percentiles(&self) -> &LatencyPercentiles {
        &self.percentiles
    }

    pub const fn measurement_window_ns(&self) -> (i64, i64) {
        (self.window_start_ns, self.window_end_ns)
    }

    /// The measurement window duration in nanoseconds (always `> 0`; validated
    /// against `i64` overflow at construction).
    pub const fn window_duration_ns(&self) -> i64 {
        self.window_duration_ns
    }
}

impl fmt::Display for LatencyVerificationArtifact {
    /// A stable, inspectable rendering of the artifact — the human-readable
    /// verification artifact content SRS-PERF-001 requires.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let leg = self.threshold_label;
        let leg_suffix = if leg.is_empty() {
            String::new()
        } else {
            format!(" [leg: {leg}]")
        };
        writeln!(
            f,
            "SRS-PERF-001 latency verification artifact: {} ({}){}",
            self.nfr.id(),
            self.nfr.metric(),
            leg_suffix,
        )?;
        writeln!(f, "  boundary: {}", self.nfr.boundary())?;
        let t = self.threshold();
        writeln!(f, "  budget: {} {} ms", t.comparison.as_str(), t.bound_ms)?;
        for p in REPORTED_PERCENTILES {
            writeln!(
                f,
                "  {}: {} ns ({:.3} ms)",
                p.as_str(),
                self.percentiles.get_ns(p),
                self.percentiles.get_millis_f64(p),
            )?;
        }
        writeln!(
            f,
            "  samples: {} (p99.9 tail resolved: {})",
            self.percentiles.sample_count(),
            self.percentiles.resolves_p999(),
        )?;
        writeln!(
            f,
            "  max clock offset (PTP-disciplined): {} ns",
            self.max_clock_offset_ns,
        )?;
        write!(
            f,
            "  measurement window: [{}, {}] ns ({} ns)",
            self.window_start_ns,
            self.window_end_ns,
            self.window_duration_ns(),
        )
    }
}

/// A complete SRS-PERF-001 verification of ONE NFR: one
/// [`LatencyVerificationArtifact`] per threshold leg. For a single-leg NFR this
/// is one artifact; for a multi-leg NFR (NFR-P4 `live`+`paper`, NFR-P10
/// `order_latency`+`dashboard_refresh`) [`assemble`](Self::assemble) fails closed
/// unless EVERY leg is present exactly once — so a runtime cannot certify a
/// multi-leg NFR while measuring only one leg (or conflating legs). This is the
/// unit that satisfies "verification reports … match each NFR measurement
/// condition": all of the NFR's measurement conditions, not just one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NfrVerification {
    nfr: LatencyNfr,
    legs: Vec<LatencyVerificationArtifact>,
}

impl NfrVerification {
    /// Assemble a complete verification of `nfr` from per-leg artifacts. Fails
    /// closed (all [`PerfMeasurementError::IncompleteNfrVerification`]) unless
    /// every artifact is for `nfr` and the covered legs are EXACTLY the NFR's
    /// legs, each present once — no missing leg, no duplicate, no foreign NFR.
    pub fn assemble(
        nfr: LatencyNfr,
        legs: Vec<LatencyVerificationArtifact>,
    ) -> Result<Self, PerfMeasurementError> {
        let incomplete = |detail: String| PerfMeasurementError::IncompleteNfrVerification {
            nfr_id: nfr.id(),
            detail,
        };
        let mut seen: Vec<&'static str> = Vec::with_capacity(legs.len());
        for artifact in &legs {
            if artifact.nfr() != nfr {
                return Err(incomplete(format!(
                    "artifact for {} cannot verify {}",
                    artifact.nfr_id(),
                    nfr.id()
                )));
            }
            let label = artifact.threshold_label();
            if seen.contains(&label) {
                return Err(incomplete(format!(
                    "threshold leg {label:?} covered more than once"
                )));
            }
            seen.push(label);
        }
        for required in nfr.threshold_labels() {
            if !seen.contains(&required) {
                return Err(incomplete(format!("missing threshold leg {required:?}")));
            }
        }
        // NFR-P10 requires its legs measured SIMULTANEOUSLY (SyRS: NFR-P1 and
        // NFR-P2 hold at the same time under the NFR-SC1 baseline). Enforce that
        // the leg windows overlap so artifacts from disjoint runs cannot be
        // assembled as one "simultaneous" verification. (The deeper fact — that
        // the run occurred under the real NFR-SC1 peak-load baseline — is a
        // property of the deferred measurement runtime, not representable here.)
        if nfr.requires_simultaneous_legs() && legs.len() > 1 {
            let max_start = legs
                .iter()
                .map(|a| a.measurement_window_ns().0)
                .max()
                .expect("legs is non-empty");
            let min_end = legs
                .iter()
                .map(|a| a.measurement_window_ns().1)
                .min()
                .expect("legs is non-empty");
            if max_start >= min_end {
                return Err(incomplete(format!(
                    "legs measured in disjoint windows (latest start {max_start} >= earliest end \
                     {min_end}); {} requires its legs measured simultaneously (SyRS NFR-P10 / \
                     NFR-SC1)",
                    nfr.id()
                )));
            }
        }
        Ok(Self { nfr, legs })
    }

    pub const fn nfr(&self) -> LatencyNfr {
        self.nfr
    }

    /// The per-leg artifacts (one per threshold leg of the NFR).
    pub fn artifacts(&self) -> &[LatencyVerificationArtifact] {
        &self.legs
    }

    /// The artifact for `leg`, or `None` if `leg` is not one of the NFR's legs.
    pub fn artifact_for_leg(&self, leg: &str) -> Option<&LatencyVerificationArtifact> {
        self.legs.iter().find(|a| a.threshold_label() == leg)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn per_mille_values_are_the_srs_perf_001_percentiles() {
        assert_eq!(Percentile::P50.per_mille(), 500);
        assert_eq!(Percentile::P95.per_mille(), 950);
        assert_eq!(Percentile::P99.per_mille(), 990);
        assert_eq!(Percentile::P999.per_mille(), 999);
        assert_eq!(Percentile::P999.as_str(), "p99.9");
        assert_eq!(
            REPORTED_PERCENTILES,
            [
                Percentile::P50,
                Percentile::P95,
                Percentile::P99,
                Percentile::P999
            ]
        );
    }

    #[test]
    fn nearest_rank_is_an_observed_sample_on_a_known_distribution() {
        // 1..=100 ns. Nearest-rank ceil(p·N): p50→rank 50, p95→95, p99→99,
        // p99.9→ceil(99.9)=100.
        let samples: Vec<u64> = (1..=100).collect();
        assert_eq!(
            nearest_rank_percentile_ns(&samples, Percentile::P50),
            Some(50)
        );
        assert_eq!(
            nearest_rank_percentile_ns(&samples, Percentile::P95),
            Some(95)
        );
        assert_eq!(
            nearest_rank_percentile_ns(&samples, Percentile::P99),
            Some(99)
        );
        assert_eq!(
            nearest_rank_percentile_ns(&samples, Percentile::P999),
            Some(100)
        );
    }

    #[test]
    fn percentile_is_order_independent() {
        let ascending: Vec<u64> = (1..=100).collect();
        let mut descending = ascending.clone();
        descending.reverse();
        let a = LatencyPercentiles::from_samples(&ascending).unwrap();
        let b = LatencyPercentiles::from_samples(&descending).unwrap();
        assert_eq!(a, b);
    }

    #[test]
    fn empty_samples_fail_closed() {
        assert_eq!(nearest_rank_percentile_ns(&[], Percentile::P50), None);
        assert_eq!(
            LatencyPercentiles::from_samples(&[]),
            Err(PerfMeasurementError::NoSamples)
        );
    }

    #[test]
    fn single_sample_reports_that_sample_for_every_percentile() {
        let p = LatencyPercentiles::from_samples(&[42]).unwrap();
        for pct in REPORTED_PERCENTILES {
            assert_eq!(p.get_ns(pct), 42);
        }
        assert_eq!(p.sample_count(), 1);
        assert!(!p.resolves_p999());
    }

    #[test]
    fn p999_resolves_only_at_a_thousand_samples() {
        let small = LatencyPercentiles::from_samples(&[1, 2, 3]).unwrap();
        assert!(!small.resolves_p999());
        let big: Vec<u64> = (1..=1000).collect();
        assert!(LatencyPercentiles::from_samples(&big)
            .unwrap()
            .resolves_p999());
    }

    #[test]
    fn percentiles_are_monotonic_non_decreasing() {
        let samples: Vec<u64> = (1..=1000).collect();
        let p = LatencyPercentiles::from_samples(&samples).unwrap();
        assert!(p.p50_ns() <= p.p95_ns());
        assert!(p.p95_ns() <= p.p99_ns());
        assert!(p.p99_ns() <= p.p999_ns());
    }

    #[test]
    fn artifact_fails_closed_on_undisciplined_clock() {
        let err = LatencyVerificationArtifact::from_samples(
            LatencyNfr::OrderSignalToAck,
            "",
            PtpClockDiscipline::Undisciplined,
            0,
            1_000,
            &[10, 20, 30],
        )
        .unwrap_err();
        assert_eq!(err, PerfMeasurementError::ClockNotDisciplined);
    }

    #[test]
    fn artifact_fails_closed_on_empty_window() {
        for (start, end) in [(1_000_i64, 1_000_i64), (1_000, 999)] {
            let err = LatencyVerificationArtifact::from_samples(
                LatencyNfr::OrderSignalToAck,
                "",
                PtpClockDiscipline::Disciplined { max_offset_ns: 500 },
                start,
                end,
                &[10, 20, 30],
            )
            .unwrap_err();
            assert_eq!(
                err,
                PerfMeasurementError::EmptyMeasurementWindow {
                    start_ns: start,
                    end_ns: end
                }
            );
        }
    }

    #[test]
    fn artifact_fails_closed_on_window_duration_overflow() {
        // Endpoints are ordered (end > start) but their span exceeds i64, so the
        // duration cannot be represented — reject rather than panic/wrap on read.
        let err = LatencyVerificationArtifact::from_samples(
            LatencyNfr::OrderSignalToAck,
            "",
            PtpClockDiscipline::Disciplined { max_offset_ns: 1 },
            i64::MIN,
            i64::MAX,
            &[10, 20, 30],
        )
        .unwrap_err();
        assert_eq!(
            err,
            PerfMeasurementError::MeasurementWindowOverflow {
                start_ns: i64::MIN,
                end_ns: i64::MAX,
            }
        );
    }

    #[test]
    fn artifact_fails_closed_on_empty_samples() {
        let err = LatencyVerificationArtifact::from_samples(
            LatencyNfr::OrderSignalToAck,
            "",
            PtpClockDiscipline::Disciplined { max_offset_ns: 500 },
            0,
            1_000,
            &[],
        )
        .unwrap_err();
        assert_eq!(err, PerfMeasurementError::NoSamples);
    }

    #[test]
    fn a_valid_artifact_documents_offset_percentiles_and_window() {
        let samples: Vec<u64> = (1..=100).map(|n| n * 1_000_000).collect(); // 1..=100 ms in ns
        let artifact = LatencyVerificationArtifact::from_samples(
            LatencyNfr::OrderSignalToAck,
            "",
            PtpClockDiscipline::Disciplined { max_offset_ns: 250 },
            10,
            10 + 60_000_000_000,
            &samples,
        )
        .unwrap();
        assert_eq!(artifact.nfr_id(), "NFR-P1");
        assert_eq!(artifact.max_clock_offset_ns(), 250);
        assert_eq!(artifact.measurement_window_ns(), (10, 10 + 60_000_000_000));
        assert_eq!(artifact.window_duration_ns(), 60_000_000_000);
        assert_eq!(artifact.percentiles().p95_ns(), 95_000_000);
        // Rendering carries the required fields.
        let rendered = artifact.to_string();
        assert!(rendered.contains("NFR-P1"));
        assert!(rendered.contains("p99.9"));
        assert!(rendered.contains("max clock offset"));
        assert!(rendered.contains("measurement window"));
    }

    fn disciplined_artifact(nfr: LatencyNfr, leg: &str) -> LatencyVerificationArtifact {
        LatencyVerificationArtifact::from_samples(
            nfr,
            leg,
            PtpClockDiscipline::Disciplined { max_offset_ns: 1 },
            0,
            1_000,
            &[1, 2, 3],
        )
        .unwrap()
    }

    #[test]
    fn artifact_rejects_a_leg_the_nfr_does_not_define() {
        // NFR-P4's legs are "live"/"paper" — anything else fails closed.
        let err = LatencyVerificationArtifact::from_samples(
            LatencyNfr::OrderEventCallback,
            "bogus",
            PtpClockDiscipline::Disciplined { max_offset_ns: 1 },
            0,
            1_000,
            &[1, 2, 3],
        )
        .unwrap_err();
        assert_eq!(
            err,
            PerfMeasurementError::UnknownThresholdLeg {
                nfr_id: "NFR-P4",
                leg: "bogus".to_string(),
            }
        );
        // A valid leg is recorded on the artifact.
        assert_eq!(
            disciplined_artifact(LatencyNfr::OrderEventCallback, "paper").threshold_label(),
            "paper"
        );
    }

    #[test]
    fn multi_leg_nfr_verification_requires_every_leg_once() {
        let live = disciplined_artifact(LatencyNfr::OrderEventCallback, "live");
        let paper = disciplined_artifact(LatencyNfr::OrderEventCallback, "paper");

        // Missing the paper leg → fail closed.
        assert!(matches!(
            NfrVerification::assemble(LatencyNfr::OrderEventCallback, vec![live.clone()]),
            Err(PerfMeasurementError::IncompleteNfrVerification { .. })
        ));
        // Duplicated leg → fail closed.
        assert!(matches!(
            NfrVerification::assemble(
                LatencyNfr::OrderEventCallback,
                vec![live.clone(), live.clone()]
            ),
            Err(PerfMeasurementError::IncompleteNfrVerification { .. })
        ));
        // An artifact for a different NFR → fail closed.
        let foreign = disciplined_artifact(LatencyNfr::OrderSignalToAck, "");
        assert!(matches!(
            NfrVerification::assemble(LatencyNfr::OrderEventCallback, vec![live.clone(), foreign]),
            Err(PerfMeasurementError::IncompleteNfrVerification { .. })
        ));
        // Both legs present exactly once → assembles.
        let verification =
            NfrVerification::assemble(LatencyNfr::OrderEventCallback, vec![live, paper]).unwrap();
        assert_eq!(verification.artifacts().len(), 2);
        assert!(verification.artifact_for_leg("live").is_some());
        assert!(verification.artifact_for_leg("paper").is_some());
        assert!(verification.artifact_for_leg("bogus").is_none());
    }

    #[test]
    fn single_leg_nfr_verification_is_one_artifact() {
        let a = disciplined_artifact(LatencyNfr::HeartbeatStaleness, "");
        let verification =
            NfrVerification::assemble(LatencyNfr::HeartbeatStaleness, vec![a]).unwrap();
        assert_eq!(verification.artifacts().len(), 1);
        assert_eq!(verification.nfr(), LatencyNfr::HeartbeatStaleness);
    }

    #[test]
    fn catalog_covers_the_seven_ac_nfrs_with_syrs_budgets() {
        let ids: Vec<&str> = LATENCY_NFRS.iter().map(|n| n.id()).collect();
        assert_eq!(
            ids,
            [
                "NFR-P1",
                "NFR-P4",
                "NFR-P5",
                "NFR-P6",
                "NFR-P9",
                "NFR-P10",
                "SRS-MD-001"
            ]
        );
        // NFR-P4 carries both live and paper budgets, reusing the order-event
        // constants.
        let p4 = LatencyNfr::OrderEventCallback.thresholds();
        assert_eq!(p4.len(), 2);
        assert_eq!(p4[0].label, "live");
        assert_eq!(p4[0].bound_ms, LIVE_CALLBACK_LATENCY_P95_MS as u64);
        assert_eq!(p4[1].label, "paper");
        assert_eq!(p4[1].bound_ms, PAPER_CALLBACK_LATENCY_P95_MS as u64);
        // NFR-P9 reuses the startup deadline constant.
        assert_eq!(
            LatencyNfr::StrategyStartup.thresholds()[0].bound_ms,
            STRATEGY_STARTUP_DEADLINE_MS
        );
        // NFR-P10 is a simultaneity property: it carries BOTH the NFR-P1 order
        // latency and the NFR-P2 dashboard-refresh legs.
        let p10 = LatencyNfr::PeakLoadOrderLatency.thresholds();
        assert_eq!(p10.len(), 2);
        assert_eq!(p10[0].label, "order_latency");
        assert_eq!(p10[0].bound_ms, ORDER_SIGNAL_TO_ACK_LATENCY_P95_MS);
        assert_eq!(p10[1].label, "dashboard_refresh");
        assert_eq!(p10[1].bound_ms, DASHBOARD_REFRESH_LATENCY_MS);
        assert_eq!(p10[1].comparison, ThresholdComparison::LessThanOrEqual);
        // NFR-P10 has MIXED per-leg percentile semantics: order_latency is p95,
        // dashboard_refresh is a flat max (NOT p95).
        assert_eq!(p10[0].stated_percentile, Some(Percentile::P95));
        assert_eq!(p10[1].stated_percentile, None);
        // The fan-out carries the SRS-MD-001 100 ms additional-latency budget.
        let fanout = LatencyNfr::SubscriptionFanout.thresholds();
        assert_eq!(fanout.len(), 1);
        assert_eq!(fanout[0].bound_ms, SUBSCRIPTION_FANOUT_LATENCY_MS);
        assert_eq!(fanout[0].comparison, ThresholdComparison::LessThanOrEqual);
        assert_eq!(fanout[0].stated_percentile, None);
        // stated percentile is per leg: p95 budgets carry Some(P95), flat maxima None.
        assert_eq!(
            LatencyNfr::OrderSignalToAck.thresholds()[0].stated_percentile,
            Some(Percentile::P95)
        );
        assert_eq!(
            LatencyNfr::HeartbeatStaleness.thresholds()[0].stated_percentile,
            None
        );
    }
}
