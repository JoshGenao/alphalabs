//! Completed-backtest result persistence + query for **SRS-BT-009** — "persist
//! completed backtest results" (SyRS **SYS-21** / **SYS-79**; StRS SN-1.02 / SN-1.04).
//!
//! # What SRS-BT-009 asks for
//!
//! The acceptance criterion: *"Parameters, metrics, trade log, equity curve, benchmark
//! comparison, strategy code version, and timestamp are queryable by strategy, date
//! range, and parameter set."* This module owns the **deterministic, dependency-free**
//! record that bundles those seven artifacts into one [`BacktestRecord`], a
//! [`BacktestResultStore`] that answers the three query axes (by strategy, by date range,
//! by parameter set), and a fail-closed serialize/restore codec that round-trips the
//! whole store byte-for-byte.
//!
//! # Building by wrapping, not reinventing
//!
//! Every artifact the acceptance names already exists as a producer type elsewhere in
//! this crate, so this slice **wraps** them rather than recomputing anything:
//!
//!   * *parameters / parameter set* — [`BacktestRequest`](crate::backtest::BacktestRequest)
//!     (strategy id, symbol, data source, run window, starting cash, cost family);
//!   * *metrics* — [`PerformanceMetrics`](crate::metrics::PerformanceMetrics) (SRS-BT-004);
//!   * *trade log* — `Vec<`[`Fill`](crate::backtest::Fill)`>` (integer minor units);
//!   * *equity curve* — `Vec<`[`EquityPoint`](crate::backtest::EquityPoint)`>`;
//!   * *benchmark comparison* — [`BenchmarkComparison`](crate::benchmark::BenchmarkComparison)
//!     (SRS-BT-005) — the record's benchmark identity;
//!   * *strategy code version* — [`CodeVersion`], an opaque caller-supplied provenance label;
//!   * *timestamp* — `completed_at_ts`, a **producer-supplied** completion instant.
//!
//! # What is real here vs deferred
//!
//! This module is a genuinely runnable persistence + query layer: a record is built,
//! validated fail-closed, inserted into a store that rejects a duplicate run id, queried
//! by all three axes in a deterministic order, serialized to a checksummed text blob, and
//! restored fail-closed. The pieces required to flip SRS-BT-009 to `passes:true` are
//! deferred (see `architecture/runtime_services.json#sim_backtest_store_contract.deferred`):
//! writing the serialized record to the **SSD-primary / NAS-archival tier** is SRS-DATA-008;
//! rendering the queryable history **to an operator** in the dashboard / backtest report is
//! SRS-UI-004 / SRS-API; and a full orchestrated run that stamps a real `run_id`,
//! `code_version` (container image digest), and `completed_at_ts` end-to-end is SRS-BT-001 /
//! the orchestrator (this slice accepts and persists caller-supplied provenance). So
//! `feature_list.json` keeps SRS-BT-009 at `passes:false`.
//!
//! # Determinism (the headline invariant)
//!
//! The store holds records in a single canonical order — sorted by
//! `(completed_at_ts, run_id)`, a total order because run ids are unique — so a query
//! returns the same records in the same order every time, the serialized form is
//! **byte-identical** for the same set of records regardless of insertion order, and
//! `restore(serialize(store)) == store`. The work uses fixed left-to-right folds with no
//! parallelism, RNG, or wall-clock read (SRS-BT-010); the completion timestamp is supplied
//! by the producer, never read from a wall clock in this pure core.
//!
//! # Fail-closed restore
//!
//! [`BacktestResultStore::restore`] validates the magic header, the body integrity
//! checksum (BEFORE building any state), the schema version, and every record invariant,
//! building the whole store in a local before returning — so a corrupt or truncated blob (any
//! change that does not *also* recompute the checksum) returns an [`Err`] and yields **no
//! partially-restored store**. The integrity guarantee is scoped to **accidental** corruption:
//! the checksum is a non-cryptographic FNV-1a, so a deliberate tamperer who recomputes it is
//! NOT detected by the checksum — defending against that needs a keyed MAC + key management,
//! out of scope for the single-user, local-only release baseline (the same stance as
//! [`crate::paper_state`]). The per-record invariant + domain checks are an independent
//! defense that still rejects a checksum-recomputed blob whose values are impossible.
//!
//! # Money math
//!
//! Trade-log and equity-curve money stays in **integer minor units** (`i64`) end to end;
//! the metric/comparison ratios are dimensionless `f64` (the metric domain, inherited from
//! SRS-BT-004/005 — not a money leak) and round-trip **exactly** via
//! [`f64::to_bits`]/[`f64::from_bits`], each verified finite on restore so a NaN/inf never
//! re-enters a ranking. No `serde` / external dependency — the same zero-dependency
//! discipline as [`crate::paper_state`].

use std::collections::HashSet;
use std::fmt;
use std::fs;
use std::io::{self, Write};
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};

use atp_types::StrategyId;

use crate::backtest::{
    BacktestDataSource, BacktestRequest, BacktestResult, DateRange, EquityPoint, Fill,
};
use crate::benchmark::BenchmarkComparison;
use crate::cost::{CommissionModel, CostConfig, SlippageModel, SpreadImpactModel};
use crate::metrics::PerformanceMetrics;

/// The record schema version. Bumped only on a backward-incompatible layout change;
/// [`BacktestResultStore::restore`] rejects any other version loudly
/// ([`StoreError::UnknownSchemaVersion`]) rather than silently mis-reading.
pub const SCHEMA_VERSION: i64 = 1;

/// The magic header line that prefixes every serialized store, so a foreign or truncated
/// blob is rejected before any field is parsed.
pub const MAGIC: &str = "ATP-BACKTEST-RECORD";

/// File name of the durable store within its configured directory
/// ([`BacktestResultStore::save_to_path`] / [`BacktestResultStore::load_from_path`]).
pub const STORE_FILENAME: &str = "backtest_results.store";

/// Base name of the scratch file an atomic save writes (and fsyncs) before renaming it onto
/// [`STORE_FILENAME`]. The actual scratch file appends a per-process, per-call suffix
/// (`<base>.<pid>.<seq>`) so two writers persisting to the same directory cannot rename over
/// each other's scratch file. The suffix is a pid + a process-local counter, NOT a clock / RNG
/// read, so the persisted *content* stays byte-deterministic.
pub const STORE_TMP_FILENAME: &str = "backtest_results.store.tmp";

/// Process-local monotonic counter that disambiguates concurrent scratch files within one
/// process (combined with the pid for cross-process uniqueness). Affects only the scratch file
/// name, never the persisted bytes.
static SCRATCH_SEQ: AtomicU64 = AtomicU64::new(0);

/// Absolute tolerance for the `excess_return == strategy_total_return - benchmark_total_return`
/// internal-consistency check. The producer computes the identity in the same `f64` domain, so
/// the residual is at worst a few ULPs; this generous bound (the ratios are O(1)) only rejects a
/// genuinely contradictory value, never a float-rounding artifact.
const EXCESS_RETURN_TOLERANCE: f64 = 1e-9;

/// A persisted backtest run's identity (SRS-BT-009 record key). A producer-supplied,
/// non-empty label; the full run-snapshot provenance (a run id atomically binding the
/// equity curve, trade log, and benchmark window) is the deferred SRS-BT-001 / orchestrator
/// owner, the same boundary as SRS-BT-004/005.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RunId(String);

impl RunId {
    /// A run id. Fails closed on an empty/whitespace label so a record cannot be keyed by
    /// an unidentifiable run.
    pub fn new(id: impl Into<String>) -> Result<Self, StoreError> {
        let id = id.into();
        if id.trim().is_empty() {
            return Err(StoreError::InconsistentField {
                context: "empty run id",
            });
        }
        Ok(Self(id))
    }

    /// The run id string.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// The strategy code version persisted with a record (SRS-BT-009 "strategy code version").
/// An opaque, caller-supplied provenance label (e.g. the strategy container image digest or
/// a git sha): this slice persists it, the orchestrator (SRS-BT-001 / SRS-EXE) supplies the
/// real value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CodeVersion(String);

impl CodeVersion {
    /// A code version. Fails closed on an empty/whitespace label so a persisted result
    /// cannot claim an unidentifiable strategy version.
    pub fn new(version: impl Into<String>) -> Result<Self, StoreError> {
        let version = version.into();
        if version.trim().is_empty() {
            return Err(StoreError::InconsistentField {
                context: "empty code version",
            });
        }
        Ok(Self(version))
    }

    /// The code version string.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// A strategy's tuned parameter set — the SRS-BT-009 "parameter set" query axis.
///
/// This is the named configuration that distinguishes one point of a parameter sweep from
/// another (e.g. a lookback window or a signal threshold), NOT the launch configuration in
/// [`BacktestRequest`] (symbol, window, costs). Two runs of the same strategy over the same
/// window with different tuned parameters share an identical [`BacktestRequest`] but a
/// different [`StrategyParameters`], so the parameter set is the only axis that tells them
/// apart — querying by it is exactly the SYS-21 backtest-history use case.
///
/// Held as a **canonical** map: entries sorted by key with unique, non-empty keys, so two
/// equal parameter sets compare and serialize identically regardless of insertion order. The
/// real values are supplied by the deferred Python strategy runtime (which knows a strategy's
/// declared parameters); this slice carries, validates, persists, and queries them. The
/// inner map is private, so a [`StrategyParameters`] can only be built canonically.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct StrategyParameters {
    entries: Vec<(String, String)>,
}

impl StrategyParameters {
    /// An empty parameter set (a strategy with no tuned parameters).
    pub fn new() -> Self {
        Self::default()
    }

    /// Build a canonical parameter set from `(name, value)` pairs, failing closed on an
    /// empty/whitespace key or a duplicate key (either would make the set ambiguous to query
    /// against). Entries are sorted by key so the set is order-independent.
    pub fn from_pairs(
        pairs: impl IntoIterator<Item = (String, String)>,
    ) -> Result<Self, StoreError> {
        let mut entries: Vec<(String, String)> = pairs.into_iter().collect();
        if entries.iter().any(|(key, _)| key.trim().is_empty()) {
            return Err(StoreError::InconsistentField {
                context: "empty strategy parameter key",
            });
        }
        entries.sort_by(|a, b| a.0.cmp(&b.0));
        if entries.windows(2).any(|pair| pair[0].0 == pair[1].0) {
            return Err(StoreError::InconsistentField {
                context: "duplicate strategy parameter key",
            });
        }
        Ok(Self { entries })
    }

    /// The canonical `(name, value)` entries, sorted by key.
    pub fn entries(&self) -> &[(String, String)] {
        &self.entries
    }

    /// Whether the parameter set is empty.
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// The number of parameters in the set.
    pub fn len(&self) -> usize {
        self.entries.len()
    }
}

/// One completed backtest's persisted result — the SRS-BT-009 artifacts bundled into a
/// single queryable record.
///
/// `completed_at_ts` is **producer-supplied** (SRS-BT-010 forbids a wall-clock read in this
/// pure core); it is the record's completion date, the axis
/// [`BacktestResultStore::query_by_completion_window`] filters on (distinct from the SYS-21
/// run-window axis [`BacktestResultStore::query_by_run_window`], which filters the backtest's
/// tested period). Not [`Eq`]: [`PerformanceMetrics`] / [`BenchmarkComparison`] carry `f64`.
#[derive(Debug, Clone, PartialEq)]
pub struct BacktestRecord {
    /// The run's identity (the record key).
    pub run_id: RunId,
    /// The launch parameters (strategy, symbol, source, window, cash, costs).
    pub request: BacktestRequest,
    /// The strategy's tuned parameter set — the SRS-BT-009 "parameter set" query axis,
    /// distinct from the launch [`BacktestRequest`].
    pub parameters: StrategyParameters,
    /// The eight SYS-16 performance metrics (SRS-BT-004).
    pub metrics: PerformanceMetrics,
    /// The strategy-versus-benchmark comparison and its benchmark identity (SRS-BT-005).
    pub comparison: BenchmarkComparison,
    /// The deterministic trade log (integer minor units).
    pub trade_log: Vec<Fill>,
    /// The mark-to-market equity curve (integer minor units).
    pub equity_curve: Vec<EquityPoint>,
    /// The strategy code version (opaque provenance label).
    pub code_version: CodeVersion,
    /// The producer-supplied completion timestamp (the record's date axis).
    pub completed_at_ts: u64,
}

impl BacktestRecord {
    /// Build a validated record, failing closed on any incoherent field (an inverted run
    /// window, a negative starting cash, an invalid cost family, a trade-log fill that does
    /// not match the run symbol/window or carries a negative cost, an equity mark outside the
    /// run window, a missing or contradictory benchmark identity, or a non-finite ratio).
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        run_id: RunId,
        request: BacktestRequest,
        parameters: StrategyParameters,
        metrics: PerformanceMetrics,
        comparison: BenchmarkComparison,
        trade_log: Vec<Fill>,
        equity_curve: Vec<EquityPoint>,
        code_version: CodeVersion,
        completed_at_ts: u64,
    ) -> Result<Self, StoreError> {
        let record = Self {
            run_id,
            request,
            parameters,
            metrics,
            comparison,
            trade_log,
            equity_curve,
            code_version,
            completed_at_ts,
        };
        validate_record(&record)?;
        Ok(record)
    }

    /// Build a record from the authoritative [`BacktestResult`] that produced the trade log and
    /// equity curve — the SAFE producer path.
    ///
    /// It binds the persisted artifacts to that result (the trade log and equity curve are taken
    /// FROM the result, not passed independently) and verifies the run provenance — the result's
    /// `data_source` and `range` must match the `request` — so a request can never be persisted
    /// against artifacts from a different run (e.g. a `SystemData` request paired with an
    /// `UploadedData` result). [`new`](Self::new) remains for reconstructing a record from
    /// already-persisted pieces (the [`BacktestResultStore::restore`] path), where no
    /// `BacktestResult` is available. (Binding the metrics/comparison to the same run is the
    /// deferred run-snapshot-identity boundary; this binds the artifacts + provenance.)
    #[allow(clippy::too_many_arguments)]
    pub fn from_result(
        run_id: RunId,
        request: BacktestRequest,
        parameters: StrategyParameters,
        metrics: PerformanceMetrics,
        comparison: BenchmarkComparison,
        result: &BacktestResult,
        code_version: CodeVersion,
        completed_at_ts: u64,
    ) -> Result<Self, StoreError> {
        if result.data_source != request.data_source {
            return Err(StoreError::InconsistentField {
                context: "backtest result data source does not match the request",
            });
        }
        if result.range != request.range {
            return Err(StoreError::InconsistentField {
                context: "backtest result window does not match the request",
            });
        }
        Self::new(
            run_id,
            request,
            parameters,
            metrics,
            comparison,
            result.trade_log.clone(),
            result.equity_curve.clone(),
            code_version,
            completed_at_ts,
        )
    }
}

/// The total order the store and codec canonicalize on: by completion timestamp, then run
/// id. Run ids are unique within a store, so this is a strict total order.
fn order_key(record: &BacktestRecord) -> (u64, &str) {
    (record.completed_at_ts, record.run_id.as_str())
}

/// Fail-closed coherence validation shared by [`BacktestRecord::new`] and
/// [`BacktestResultStore::restore`] — the single place a record's invariants are checked.
fn validate_record(record: &BacktestRecord) -> Result<(), StoreError> {
    if record.run_id.as_str().trim().is_empty() {
        return Err(StoreError::InconsistentField {
            context: "empty run id",
        });
    }
    if record.code_version.as_str().trim().is_empty() {
        return Err(StoreError::InconsistentField {
            context: "empty code version",
        });
    }
    if record.request.symbol.trim().is_empty() {
        return Err(StoreError::InconsistentField {
            context: "empty request symbol",
        });
    }
    if record.request.strategy_id.as_str().trim().is_empty() {
        return Err(StoreError::InconsistentField {
            context: "empty strategy id",
        });
    }
    if record.request.range.start > record.request.range.end {
        return Err(StoreError::InconsistentField {
            context: "inverted run window",
        });
    }
    // A backtest is launched with strictly positive starting capital; the metric family uses
    // it as the first-period return's denominator (metrics::compute), so a zero or negative
    // baseline could not have produced the stored metrics.
    if record.request.starting_cash_minor <= 0 {
        return Err(StoreError::InconsistentField {
            context: "non-positive starting cash",
        });
    }
    // The cost family's signed override parameters must be non-negative (a cost can never be
    // negative); reuse the SRS-BT-002 validation rather than re-deriving it.
    record
        .request
        .cost_config
        .validate()
        .map_err(|_| StoreError::InconsistentField {
            context: "invalid cost config",
        })?;
    // A persisted result must IDENTIFY its benchmark (SRS-BT-005), so neither the metric
    // family's nor the comparison's benchmark symbol may be empty.
    if record.metrics.benchmark_symbol.trim().is_empty() {
        return Err(StoreError::InconsistentField {
            context: "empty metrics benchmark symbol",
        });
    }
    if record.comparison.benchmark_symbol.trim().is_empty() {
        return Err(StoreError::InconsistentField {
            context: "empty comparison benchmark symbol",
        });
    }
    // The metric family and the comparison are produced together from ONE resolved benchmark
    // (an SRS-BT-005 BenchmarkReport), so they must identify the SAME benchmark and share the
    // SAME CAPM coefficients. A record whose two halves disagree would label and rank one run
    // against two conflicting benchmarks.
    if record.metrics.benchmark_symbol != record.comparison.benchmark_symbol {
        return Err(StoreError::InconsistentField {
            context: "benchmark identity mismatch between metrics and comparison",
        });
    }
    if record.metrics.alpha != record.comparison.alpha {
        return Err(StoreError::InconsistentField {
            context: "alpha disagreement between metrics and comparison",
        });
    }
    if record.metrics.beta != record.comparison.beta {
        return Err(StoreError::InconsistentField {
            context: "beta disagreement between metrics and comparison",
        });
    }
    // The comparison's excess return is, by definition, the strategy's total return minus the
    // benchmark's (SRS-BT-005). When all three are present, enforce that algebraic identity --
    // the same class of internal-consistency guard as the alpha/beta agreement above, derived
    // purely from the comparison's own stored ratios (NOT a value recompute against the curve,
    // which is the deferred boundary) -- so a record cannot claim an excess return that
    // contradicts its own total returns.
    if let (Some(strategy), Some(benchmark), Some(excess)) = (
        record.comparison.strategy_total_return,
        record.comparison.benchmark_total_return,
        record.comparison.excess_return,
    ) {
        if (strategy - benchmark - excess).abs() > EXCESS_RETURN_TOLERANCE {
            return Err(StoreError::InconsistentField {
                context: "excess return does not equal strategy minus benchmark total return",
            });
        }
    }
    // Equity-curve coherence — the SRS-BT-004 metric producer's invariants (metrics::compute /
    // period_returns), so the stored curve is structurally coherent with the metric producer's
    // input contract. (This is the in-scope STRUCTURAL guard; full value-level verification --
    // recomputing every metric and confirming it matches -- needs the MetricsConfig and the
    // benchmark level series, which SRS-BT-009 does not persist, so it is the deferred
    // run-snapshot-identity / SRS-BT-004-producer boundary.) The curve is
    // non-empty, strictly increasing in timestamp, bound to the run window (the SRS-BT-005
    // coherence guard), and strictly positive — except the FINAL mark, which may be zero (a
    // terminal total loss, a defined -100% final return) but never negative.
    if record.equity_curve.is_empty() {
        return Err(StoreError::InconsistentField {
            context: "empty equity curve",
        });
    }
    let last_index = record.equity_curve.len() - 1;
    let mut prev_equity_ts: Option<u64> = None;
    for (index, point) in record.equity_curve.iter().enumerate() {
        if !record.request.range.contains(point.ts) {
            return Err(StoreError::InconsistentField {
                context: "equity mark outside run window",
            });
        }
        let mark_ok = if index < last_index {
            point.equity_minor > 0
        } else {
            point.equity_minor >= 0
        };
        if !mark_ok {
            return Err(StoreError::InconsistentField {
                context: "non-positive equity mark",
            });
        }
        if let Some(previous) = prev_equity_ts {
            if point.ts <= previous {
                return Err(StoreError::InconsistentField {
                    context: "non-monotonic equity timestamps",
                });
            }
        }
        prev_equity_ts = Some(point.ts);
    }
    // Trade-log coherence — the SAME producer invariants (metrics::compute): every fill belongs
    // to THIS run (the run symbol + the equity-curve window `[run_start, run_end]`, the window
    // the win rate is measured over), is ordered by non-decreasing timestamp, and is a real,
    // fail-closed trade. A fill from another symbol/window, a reordered log, or a negative cost
    // would survive restore and let the win rate disagree with the equity-derived metrics.
    let run_start = record.equity_curve[0].ts;
    let run_end = record.equity_curve[last_index].ts;
    let mut prev_fill_ts: Option<u64> = None;
    for fill in &record.trade_log {
        if fill.symbol != record.request.symbol {
            return Err(StoreError::InconsistentField {
                context: "trade-log fill symbol does not match the run symbol",
            });
        }
        if fill.ts < run_start || fill.ts > run_end {
            return Err(StoreError::InconsistentField {
                context: "trade-log fill outside run window",
            });
        }
        if let Some(previous) = prev_fill_ts {
            if fill.ts < previous {
                return Err(StoreError::InconsistentField {
                    context: "non-monotonic trade log",
                });
            }
        }
        prev_fill_ts = Some(fill.ts);
        if fill.quantity == 0 {
            return Err(StoreError::InconsistentField {
                context: "zero-quantity trade-log fill",
            });
        }
        if fill.price_minor <= 0 {
            return Err(StoreError::InconsistentField {
                context: "non-positive trade-log fill price",
            });
        }
        if fill.commission_minor < 0 || fill.slippage_minor < 0 || fill.spread_impact_minor < 0 {
            return Err(StoreError::InconsistentField {
                context: "negative trade-log fill cost component",
            });
        }
    }
    // Defense in depth: every metric/comparison ratio is finite, so a NaN/inf can never be
    // persisted and later re-ranked.
    for value in metric_ratios(&record.metrics) {
        assert_finite(value, "non-finite metric ratio")?;
    }
    for value in comparison_ratios(&record.comparison) {
        assert_finite(value, "non-finite comparison ratio")?;
    }
    // Per-metric DOMAIN bounds — a metric outside its mathematically-valid range is impossible
    // regardless of the inputs (the same per-value sanity class as the finiteness guard above,
    // NOT a value recompute), so it is rejected before it can be ranked. Per metrics::compute:
    // the win rate and the max drawdown ((peak-equity)/peak) are fractions in [0, 1], and the
    // annualized volatility is a non-negative dispersion.
    if let Some(win_rate) = record.metrics.win_rate {
        if !(0.0..=1.0).contains(&win_rate) {
            return Err(StoreError::InconsistentField {
                context: "win rate outside [0, 1]",
            });
        }
    }
    if let Some(drawdown) = record.metrics.max_drawdown {
        if !(0.0..=1.0).contains(&drawdown) {
            return Err(StoreError::InconsistentField {
                context: "max drawdown outside [0, 1]",
            });
        }
    }
    if let Some(volatility) = record.metrics.annualized_volatility {
        if volatility < 0.0 {
            return Err(StoreError::InconsistentField {
                context: "negative annualized volatility",
            });
        }
    }
    Ok(())
}

/// The eight metric ratios, in serialized order.
fn metric_ratios(metrics: &PerformanceMetrics) -> [Option<f64>; 8] {
    [
        metrics.sharpe_ratio,
        metrics.sortino_ratio,
        metrics.alpha,
        metrics.beta,
        metrics.max_drawdown,
        metrics.annualized_return,
        metrics.annualized_volatility,
        metrics.win_rate,
    ]
}

/// The five comparison ratios, in serialized order.
fn comparison_ratios(comparison: &BenchmarkComparison) -> [Option<f64>; 5] {
    [
        comparison.alpha,
        comparison.beta,
        comparison.strategy_total_return,
        comparison.benchmark_total_return,
        comparison.excess_return,
    ]
}

fn assert_finite(value: Option<f64>, context: &'static str) -> Result<(), StoreError> {
    match value {
        Some(v) if !v.is_finite() => Err(StoreError::NonFiniteRatio { context }),
        _ => Ok(()),
    }
}

/// Whether two inclusive `[start, end]` windows overlap (share any instant). Used by the
/// SYS-21 run-window date-range axis: a run is returned when its tested period intersects the
/// queried period at all.
fn windows_overlap(a: DateRange, b: DateRange) -> bool {
    a.start <= b.end && a.end >= b.start
}

/// A combined query over the SRS-BT-009 axes. An unset axis matches every record, so
/// `RecordQuery::default()` returns the whole store and any subset of axes AND-combines.
#[derive(Debug, Clone, Default)]
pub struct RecordQuery {
    /// Match a single strategy.
    pub strategy_id: Option<StrategyId>,
    /// Match records whose **backtest run window** (`request.range`) overlaps this range —
    /// the SYS-21 "date range" axis (find a historical run by the period it tested).
    pub run_window: Option<DateRange>,
    /// Match records whose **completion timestamp** falls within this inclusive window
    /// (the secondary "when was it run" axis).
    pub completed_within: Option<DateRange>,
    /// Match records run with exactly this strategy parameter set.
    pub parameter_set: Option<StrategyParameters>,
}

impl RecordQuery {
    fn matches(&self, record: &BacktestRecord) -> bool {
        // MSRV 1.75: `Option::is_none_or` (1.82) is unavailable, so use `map_or(true, ..)` —
        // an unset axis matches every record.
        self.strategy_id
            .as_ref()
            .map_or(true, |s| record.request.strategy_id == *s)
            && self
                .run_window
                .as_ref()
                .map_or(true, |w| windows_overlap(record.request.range, *w))
            && self
                .completed_within
                .as_ref()
                .map_or(true, |w| w.contains(record.completed_at_ts))
            && self
                .parameter_set
                .as_ref()
                .map_or(true, |p| record.parameters == *p)
    }
}

/// A queryable, persistable collection of completed-backtest results (SRS-BT-009).
///
/// Records are held in a single canonical order (`(completed_at_ts, run_id)`), so every
/// query answers deterministically and the serialized form is byte-identical for the same
/// set of records. [`insert`](Self::insert) rejects a duplicate run id.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct BacktestResultStore {
    records: Vec<BacktestRecord>,
}

impl BacktestResultStore {
    /// An empty store.
    pub fn new() -> Self {
        Self::default()
    }

    /// The number of persisted records.
    pub fn len(&self) -> usize {
        self.records.len()
    }

    /// Whether the store holds no records.
    pub fn is_empty(&self) -> bool {
        self.records.is_empty()
    }

    /// All records in canonical `(completed_at_ts, run_id)` order.
    pub fn records(&self) -> &[BacktestRecord] {
        &self.records
    }

    /// Persist a validated record, failing closed on a duplicate run id so two results can
    /// never share an identity. The record is inserted in canonical order, so the store
    /// stays sorted and every query is deterministic.
    pub fn insert(&mut self, record: BacktestRecord) -> Result<(), StoreError> {
        validate_record(&record)?;
        if self
            .records
            .iter()
            .any(|existing| existing.run_id == record.run_id)
        {
            return Err(StoreError::DuplicateRunId {
                run_id: record.run_id.as_str().to_string(),
            });
        }
        let position = self
            .records
            .partition_point(|existing| order_key(existing) < order_key(&record));
        self.records.insert(position, record);
        Ok(())
    }

    /// All records for `strategy_id`, in canonical order (SRS-BT-009 "by strategy").
    pub fn query_by_strategy(&self, strategy_id: &StrategyId) -> Vec<&BacktestRecord> {
        self.records
            .iter()
            .filter(|record| record.request.strategy_id == *strategy_id)
            .collect()
    }

    /// All records whose **backtest run window** (`request.range`) overlaps `range`, in
    /// canonical order — the SYS-21 "by date range" axis (find a historical run by the period
    /// it tested, e.g. every 2020 backtest, regardless of when it was executed). Overlap
    /// semantics: a run is returned when its tested period intersects the queried period.
    pub fn query_by_run_window(&self, range: DateRange) -> Vec<&BacktestRecord> {
        self.records
            .iter()
            .filter(|record| windows_overlap(record.request.range, range))
            .collect()
    }

    /// All records whose **completion timestamp** falls within the inclusive `range`, in
    /// canonical order — the secondary "when was it run" axis (distinct from the run window).
    pub fn query_by_completion_window(&self, range: DateRange) -> Vec<&BacktestRecord> {
        self.records
            .iter()
            .filter(|record| range.contains(record.completed_at_ts))
            .collect()
    }

    /// All records run with exactly the strategy parameter set `params`, in canonical order
    /// (SRS-BT-009 "by parameter set" — the [`StrategyParameters`] tuned configuration, the
    /// axis that tells two points of a parameter sweep apart).
    pub fn query_by_parameter_set(&self, params: &StrategyParameters) -> Vec<&BacktestRecord> {
        self.records
            .iter()
            .filter(|record| record.parameters == *params)
            .collect()
    }

    /// All records matching every set axis of `query` (strategy AND date range AND parameter
    /// set), in canonical order.
    pub fn query(&self, query: &RecordQuery) -> Vec<&BacktestRecord> {
        self.records
            .iter()
            .filter(|record| query.matches(record))
            .collect()
    }

    /// Serialize the whole store to the deterministic, dependency-free text form.
    ///
    /// Records are emitted in canonical order, variable-length strings are length-prefixed,
    /// and `f64` ratios are emitted as their exact `to_bits` payload, so the output is
    /// byte-identical for the same set of records and round-trips losslessly. A `MAGIC`
    /// header and an integrity checksum over the body let [`restore`](Self::restore) detect
    /// any later byte change.
    pub fn serialize(&self) -> String {
        let mut body = String::new();
        push_i128(&mut body, i128::from(SCHEMA_VERSION));
        push_count(&mut body, self.records.len());
        for record in &self.records {
            encode_record(&mut body, record);
        }

        let mut out = String::with_capacity(body.len() + MAGIC.len() + 32);
        push_line(&mut out, MAGIC);
        push_i128(&mut out, i128::from(checksum(body.as_bytes())));
        out.push_str(&body);
        out
    }

    /// Restore a store produced by [`serialize`](Self::serialize), failing closed on any
    /// malformation and building the whole store in a local before returning — so a corrupt or
    /// truncated blob (any change that does not recompute the FNV-1a checksum) returns an
    /// [`Err`] and yields no partially-restored store. The checksum is non-cryptographic, so it
    /// catches **accidental** corruption, not a deliberate checksum-recomputing tamperer (that
    /// needs a keyed MAC, out of scope for the single-user, local-only baseline).
    pub fn restore(serialized: &str) -> Result<Self, StoreError> {
        let mut cursor = Cursor::new(serialized);

        let magic = cursor.read_line("magic header")?;
        if magic != MAGIC {
            return Err(StoreError::CorruptRecord {
                context: "magic header",
            });
        }
        // Integrity check FIRST: the checksum covers the entire body that follows.
        let stored_checksum = cursor.read_u64("checksum")?;
        let body = cursor.remaining();
        if checksum(body) != stored_checksum {
            return Err(StoreError::ChecksumMismatch);
        }

        let schema_version = cursor.read_i64("schema version")?;
        if schema_version != SCHEMA_VERSION {
            return Err(StoreError::UnknownSchemaVersion {
                found: schema_version,
            });
        }

        // Decode into a temporary Vec — validating each record and detecting duplicates via the
        // HashSet in O(n) — then sort ONCE by `order_key` (O(n log n)) and construct the store
        // directly. (Calling `insert` per record would re-scan for duplicates and shift the Vec
        // into sorted position on every record, making restore of a large history O(n²).) The
        // store is built in a local before returning, so any malformation yields no partial store.
        let record_count = cursor.read_count("record count")?;
        let mut records: Vec<BacktestRecord> = Vec::new();
        let mut seen: HashSet<String> = HashSet::new();
        for _ in 0..record_count {
            let record = decode_record(&mut cursor)?;
            validate_record(&record)?;
            if !seen.insert(record.run_id.as_str().to_string()) {
                return Err(StoreError::DuplicateRunId {
                    run_id: record.run_id.as_str().to_string(),
                });
            }
            records.push(record);
        }
        cursor.expect_end()?;
        // Canonicalize once. The serialized form is already in `order_key` order, so this is
        // typically a no-op, but sorting defends against a reordered (checksum-recomputed) blob
        // and guarantees the store invariant regardless of byte order.
        records.sort_by(|a, b| order_key(a).cmp(&order_key(b)));
        Ok(Self { records })
    }

    /// Durably persist the whole store to `STORE_FILENAME` under `dir`, creating `dir` if
    /// absent. This is the SRS-BT-009 "persist" verb at the filesystem level — it wraps the
    /// existing [`serialize`](Self::serialize) codec (no new format, no `serde`).
    ///
    /// The write is **crash-durable and atomically published**: it writes the blob to a
    /// per-call-unique scratch file, `fsync`s the scratch file so its bytes reach disk, then
    /// `rename`s it onto the live store (an atomic replace — a reader never sees a half-written
    /// blob), and finally `fsync`s the parent directory so the rename itself survives a crash.
    /// The scratch name carries a `<pid>.<seq>` suffix so two writers persisting to the same
    /// directory cannot rename over each other's scratch file. Every `std::io` failure surfaces
    /// as a fail-closed [`StoreError::Io`].
    ///
    /// Guarantee scope: this is a **single-logical-writer** durable store. Two writers racing to
    /// publish *different* stores to the same directory never corrupt the file (each scratch is
    /// unique and the rename is atomic), but the last publish wins — coordinating genuinely
    /// concurrent writers (a lock / single-writer guard) is out of scope for the single-user,
    /// local-only baseline, the same boundary as the deferred SRS-DATA-008 tiered store. The
    /// SSD-primary / NAS-archival *tiering*, eviction, and failover of this directory likewise
    /// remain the deferred SRS-DATA-008 owner; BT-009 owns only the durable file write to a
    /// caller-supplied directory.
    pub fn save_to_path(&self, dir: &Path) -> Result<(), StoreError> {
        fs::create_dir_all(dir).map_err(|err| io_error("create store directory", &err))?;
        let seq = SCRATCH_SEQ.fetch_add(1, Ordering::Relaxed);
        let tmp_path = dir.join(format!("{STORE_TMP_FILENAME}.{}.{seq}", std::process::id()));
        let final_path = dir.join(STORE_FILENAME);

        // Write the blob to the scratch file and fsync it, so its bytes are durably on disk
        // BEFORE we publish it — otherwise a crash could leave the renamed file referencing
        // unwritten (zero/garbage) data.
        let mut scratch = fs::File::create(&tmp_path)
            .map_err(|err| io_error("create store scratch file", &err))?;
        if let Err(err) = scratch
            .write_all(self.serialize().as_bytes())
            .and_then(|()| scratch.sync_all())
        {
            let _ = fs::remove_file(&tmp_path);
            return Err(io_error("write store scratch file", &err));
        }
        drop(scratch);

        // Atomic publish: rename replaces the live store in one step, so a reader never sees a
        // partially written blob.
        fs::rename(&tmp_path, &final_path).map_err(|err| {
            // Best-effort cleanup so a failed publish does not leave the scratch file lying around.
            let _ = fs::remove_file(&tmp_path);
            io_error("publish store file", &err)
        })?;

        // fsync the directory so the rename (a directory-entry change) is itself durable — a
        // crash right after the rename must not roll back to the pre-rename directory state.
        let dir_handle =
            fs::File::open(dir).map_err(|err| io_error("open store directory", &err))?;
        dir_handle
            .sync_all()
            .map_err(|err| io_error("sync store directory", &err))?;
        Ok(())
    }

    /// Load a store previously written by [`save_to_path`](Self::save_to_path) from `dir`.
    ///
    /// Fail-closed taxonomy (a persisted history must never be silently lost):
    /// - The configured `dir` is **absent or not a directory** → [`StoreError::Io`]. An
    ///   unmounted / deleted / misconfigured results path is a configuration failure, NOT an
    ///   empty history — restoring empty here would silently erase a previously persisted store.
    /// - `dir` exists but holds **no store file** → an empty store. This is the one legitimate
    ///   "fresh install has never persisted a result" case (the provisioned directory is there).
    /// - A **present** file is decoded through the fail-closed [`restore`](Self::restore) codec,
    ///   so a corrupt / truncated / checksum-mismatching blob returns an [`Err`] (never a partial
    ///   store). Any other I/O failure (a permission error, an unreadable path) surfaces as
    ///   [`StoreError::Io`].
    pub fn load_from_path(dir: &Path) -> Result<Self, StoreError> {
        // The configured directory must be provisioned. A missing directory is a misconfigured /
        // unmounted / deleted storage path, which must fail closed rather than masquerade as an
        // empty history — only a missing FILE inside a present directory is a fresh install.
        if !dir.is_dir() {
            return Err(StoreError::Io {
                context: "store directory is missing or not a directory",
            });
        }
        let final_path = dir.join(STORE_FILENAME);
        match fs::read_to_string(&final_path) {
            Ok(contents) => Self::restore(&contents),
            Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(Self::new()),
            Err(err) => Err(io_error("read store file", &err)),
        }
    }
}

/// Map a `std::io::Error` to the fail-closed [`StoreError::Io`]. `StoreError` derives
/// `Clone`/`PartialEq`/`Eq` (which `io::Error` does not), so the variant carries a `'static`
/// context label naming the operation rather than the source error.
fn io_error(context: &'static str, _err: &io::Error) -> StoreError {
    StoreError::Io { context }
}

// --------------------------------------------------------------------------- //
// Record encode / decode
// --------------------------------------------------------------------------- //

fn encode_record(body: &mut String, record: &BacktestRecord) {
    push_str(body, record.run_id.as_str());
    push_str(body, record.code_version.as_str());
    push_i128(body, i128::from(record.completed_at_ts));
    encode_request(body, &record.request);
    encode_parameters(body, &record.parameters);
    encode_metrics(body, &record.metrics);
    encode_comparison(body, &record.comparison);
    push_count(body, record.trade_log.len());
    for fill in &record.trade_log {
        encode_fill(body, fill);
    }
    push_count(body, record.equity_curve.len());
    for point in &record.equity_curve {
        push_i128(body, i128::from(point.ts));
        push_i128(body, i128::from(point.equity_minor));
    }
}

fn decode_record(cursor: &mut Cursor<'_>) -> Result<BacktestRecord, StoreError> {
    let run_id = RunId::new(cursor.read_str("run id")?)?;
    let code_version = CodeVersion::new(cursor.read_str("code version")?)?;
    let completed_at_ts = cursor.read_u64("completed_at_ts")?;
    let request = decode_request(cursor)?;
    let parameters = decode_parameters(cursor)?;
    let metrics = decode_metrics(cursor)?;
    let comparison = decode_comparison(cursor)?;

    // Counts are read from the blob and are NOT trusted: a tampered (checksum-recomputed) or
    // accidentally-corrupted count must never drive an eager allocation, so the vectors grow
    // incrementally (never pre-sized from the count) and a count larger than the remaining data
    // simply exhausts the cursor and fails closed — never an out-of-memory abort.
    let trade_count = cursor.read_count("trade log count")?;
    let mut trade_log = Vec::new();
    for _ in 0..trade_count {
        trade_log.push(decode_fill(cursor)?);
    }
    let equity_count = cursor.read_count("equity curve count")?;
    let mut equity_curve = Vec::new();
    for _ in 0..equity_count {
        let ts = cursor.read_u64("equity ts")?;
        let equity_minor = cursor.read_i64("equity_minor")?;
        equity_curve.push(EquityPoint { ts, equity_minor });
    }

    // Build the raw record, then validate fail-closed via insert (in restore) — the single
    // validation site. Returned unvalidated here; the caller validates on insert.
    Ok(BacktestRecord {
        run_id,
        request,
        parameters,
        metrics,
        comparison,
        trade_log,
        equity_curve,
        code_version,
        completed_at_ts,
    })
}

/// Encode the strategy parameter set: the entry count, then each sorted `(key, value)` as a
/// pair of length-prefixed strings.
fn encode_parameters(body: &mut String, parameters: &StrategyParameters) {
    push_count(body, parameters.len());
    for (key, value) in parameters.entries() {
        push_str(body, key);
        push_str(body, value);
    }
}

/// Decode the strategy parameter set, re-canonicalizing fail-closed via
/// [`StrategyParameters::from_pairs`] (which rejects an empty or duplicate key in a tampered
/// blob).
fn decode_parameters(cursor: &mut Cursor<'_>) -> Result<StrategyParameters, StoreError> {
    let count = cursor.read_count("strategy parameter count")?;
    // Untrusted count: grow incrementally rather than pre-allocating (see decode_record).
    let mut pairs = Vec::new();
    for _ in 0..count {
        let key = cursor.read_str("strategy parameter key")?;
        let value = cursor.read_str("strategy parameter value")?;
        pairs.push((key, value));
    }
    StrategyParameters::from_pairs(pairs)
}

fn encode_request(body: &mut String, request: &BacktestRequest) {
    push_str(body, request.strategy_id.as_str());
    push_str(body, request.symbol.as_str());
    push_i128(body, data_source_tag(request.data_source));
    push_i128(body, i128::from(request.range.start));
    push_i128(body, i128::from(request.range.end));
    push_i128(body, i128::from(request.starting_cash_minor));
    encode_cost_config(body, &request.cost_config);
}

fn decode_request(cursor: &mut Cursor<'_>) -> Result<BacktestRequest, StoreError> {
    let strategy_id = StrategyId::new(cursor.read_str("strategy id")?);
    let symbol = cursor.read_str("symbol")?;
    let data_source = data_source_from_tag(cursor.read_i64("data source tag")?)?;
    let start = cursor.read_u64("range start")?;
    let end = cursor.read_u64("range end")?;
    let starting_cash_minor = cursor.read_i64("starting_cash_minor")?;
    let cost_config = decode_cost_config(cursor)?;
    Ok(BacktestRequest {
        strategy_id,
        symbol,
        data_source,
        range: DateRange { start, end },
        starting_cash_minor,
        cost_config,
    })
}

fn data_source_tag(source: BacktestDataSource) -> i128 {
    match source {
        BacktestDataSource::SystemData => 0,
        BacktestDataSource::UploadedData => 1,
    }
}

fn data_source_from_tag(tag: i64) -> Result<BacktestDataSource, StoreError> {
    match tag {
        0 => Ok(BacktestDataSource::SystemData),
        1 => Ok(BacktestDataSource::UploadedData),
        _ => Err(StoreError::CorruptRecord {
            context: "unknown data source tag",
        }),
    }
}

fn encode_cost_config(body: &mut String, config: &CostConfig) {
    match config.commission {
        CommissionModel::IbTiered => push_i128(body, 0),
        CommissionModel::PerShare {
            rate_centiminor_per_share,
            min_per_order_minor,
        } => {
            push_i128(body, 1);
            push_i128(body, i128::from(rate_centiminor_per_share));
            push_i128(body, i128::from(min_per_order_minor));
        }
        CommissionModel::PerTrade { fee_minor } => {
            push_i128(body, 2);
            push_i128(body, i128::from(fee_minor));
        }
        CommissionModel::None => push_i128(body, 3),
    }
    match config.slippage {
        SlippageModel::NotionalBps { bps } => {
            push_i128(body, 0);
            push_i128(body, i128::from(bps));
        }
        SlippageModel::None => push_i128(body, 1),
    }
    match config.spread_impact {
        SpreadImpactModel::ObservedOrFallbackBps { fallback_bps } => {
            push_i128(body, 0);
            push_i128(body, i128::from(fallback_bps));
        }
        SpreadImpactModel::FixedBps { bps } => {
            push_i128(body, 1);
            push_i128(body, i128::from(bps));
        }
        SpreadImpactModel::None => push_i128(body, 2),
    }
}

fn decode_cost_config(cursor: &mut Cursor<'_>) -> Result<CostConfig, StoreError> {
    let commission = match cursor.read_i64("commission tag")? {
        0 => CommissionModel::IbTiered,
        1 => CommissionModel::PerShare {
            rate_centiminor_per_share: cursor.read_i64("commission rate")?,
            min_per_order_minor: cursor.read_i64("commission min")?,
        },
        2 => CommissionModel::PerTrade {
            fee_minor: cursor.read_i64("commission fee")?,
        },
        3 => CommissionModel::None,
        _ => {
            return Err(StoreError::CorruptRecord {
                context: "unknown commission model tag",
            })
        }
    };
    let slippage = match cursor.read_i64("slippage tag")? {
        0 => SlippageModel::NotionalBps {
            bps: cursor.read_u32("slippage bps")?,
        },
        1 => SlippageModel::None,
        _ => {
            return Err(StoreError::CorruptRecord {
                context: "unknown slippage model tag",
            })
        }
    };
    let spread_impact = match cursor.read_i64("spread tag")? {
        0 => SpreadImpactModel::ObservedOrFallbackBps {
            fallback_bps: cursor.read_u32("spread fallback bps")?,
        },
        1 => SpreadImpactModel::FixedBps {
            bps: cursor.read_u32("spread bps")?,
        },
        2 => SpreadImpactModel::None,
        _ => {
            return Err(StoreError::CorruptRecord {
                context: "unknown spread impact model tag",
            })
        }
    };
    Ok(CostConfig {
        commission,
        slippage,
        spread_impact,
    })
}

fn encode_metrics(body: &mut String, metrics: &PerformanceMetrics) {
    for value in metric_ratios(metrics) {
        push_opt_f64(body, value);
    }
    push_str(body, metrics.benchmark_symbol.as_str());
}

fn decode_metrics(cursor: &mut Cursor<'_>) -> Result<PerformanceMetrics, StoreError> {
    let sharpe_ratio = cursor.read_opt_f64("sharpe_ratio")?;
    let sortino_ratio = cursor.read_opt_f64("sortino_ratio")?;
    let alpha = cursor.read_opt_f64("metrics alpha")?;
    let beta = cursor.read_opt_f64("metrics beta")?;
    let max_drawdown = cursor.read_opt_f64("max_drawdown")?;
    let annualized_return = cursor.read_opt_f64("annualized_return")?;
    let annualized_volatility = cursor.read_opt_f64("annualized_volatility")?;
    let win_rate = cursor.read_opt_f64("win_rate")?;
    let benchmark_symbol = cursor.read_str("metrics benchmark symbol")?;
    Ok(PerformanceMetrics {
        sharpe_ratio,
        sortino_ratio,
        alpha,
        beta,
        max_drawdown,
        annualized_return,
        annualized_volatility,
        win_rate,
        benchmark_symbol,
    })
}

fn encode_comparison(body: &mut String, comparison: &BenchmarkComparison) {
    push_str(body, comparison.benchmark_symbol.as_str());
    push_bool(body, comparison.is_default_benchmark);
    for value in comparison_ratios(comparison) {
        push_opt_f64(body, value);
    }
}

fn decode_comparison(cursor: &mut Cursor<'_>) -> Result<BenchmarkComparison, StoreError> {
    let benchmark_symbol = cursor.read_str("comparison benchmark symbol")?;
    let is_default_benchmark = cursor.read_bool("is_default_benchmark")?;
    let alpha = cursor.read_opt_f64("comparison alpha")?;
    let beta = cursor.read_opt_f64("comparison beta")?;
    let strategy_total_return = cursor.read_opt_f64("strategy_total_return")?;
    let benchmark_total_return = cursor.read_opt_f64("benchmark_total_return")?;
    let excess_return = cursor.read_opt_f64("excess_return")?;
    Ok(BenchmarkComparison {
        benchmark_symbol,
        is_default_benchmark,
        alpha,
        beta,
        strategy_total_return,
        benchmark_total_return,
        excess_return,
    })
}

fn encode_fill(body: &mut String, fill: &Fill) {
    push_i128(body, i128::from(fill.ts));
    push_str(body, fill.symbol.as_str());
    push_i128(body, i128::from(fill.quantity));
    push_i128(body, i128::from(fill.price_minor));
    push_i128(body, i128::from(fill.commission_minor));
    push_i128(body, i128::from(fill.slippage_minor));
    push_i128(body, i128::from(fill.spread_impact_minor));
}

fn decode_fill(cursor: &mut Cursor<'_>) -> Result<Fill, StoreError> {
    Ok(Fill {
        ts: cursor.read_u64("fill ts")?,
        symbol: cursor.read_str("fill symbol")?,
        quantity: cursor.read_i64("fill quantity")?,
        price_minor: cursor.read_i64("fill price_minor")?,
        commission_minor: cursor.read_i64("fill commission_minor")?,
        slippage_minor: cursor.read_i64("fill slippage_minor")?,
        spread_impact_minor: cursor.read_i64("fill spread_impact_minor")?,
    })
}

// --------------------------------------------------------------------------- //
// Deterministic, dependency-free text codec
// --------------------------------------------------------------------------- //

/// Append `value` as its own line.
fn push_line(out: &mut String, value: &str) {
    out.push_str(value);
    out.push('\n');
}

/// Append a decimal integer as its own line.
fn push_i128(out: &mut String, value: i128) {
    out.push_str(&value.to_string());
    out.push('\n');
}

/// Append a non-negative count as its own line.
fn push_count(out: &mut String, value: usize) {
    out.push_str(&value.to_string());
    out.push('\n');
}

/// Append a boolean as `0`/`1` on its own line.
fn push_bool(out: &mut String, value: bool) {
    push_line(out, if value { "1" } else { "0" });
}

/// Append a length-prefixed string: the byte length on one line, then the bytes followed by
/// a newline — so any byte (spaces, an OCC option symbol, etc.) round-trips without escaping.
fn push_str(out: &mut String, value: &str) {
    out.push_str(&value.len().to_string());
    out.push('\n');
    out.push_str(value);
    out.push('\n');
}

/// Append an optional `f64` ratio: `N` for `None`, else `S` followed by the value's exact
/// `to_bits` payload — so a dimensionless metric ratio round-trips bit-for-bit (no
/// float-formatting nondeterminism) and a `None` stays an honest "undefined".
fn push_opt_f64(out: &mut String, value: Option<f64>) {
    match value {
        None => push_line(out, "N"),
        Some(v) => {
            let mut line = String::from("S");
            line.push_str(&v.to_bits().to_string());
            push_line(out, &line);
        }
    }
}

/// A 64-bit FNV-1a integrity checksum over the serialized body.
///
/// A NON-cryptographic checksum: it detects *accidental* corruption (bit flips, truncation,
/// a value changed to another structurally-valid value) so a damaged blob fails closed
/// instead of restoring fabricated results. It is NOT a security MAC — defending against a
/// deliberate tamperer who recomputes the checksum needs a keyed MAC and key management,
/// out of scope for the single-user, local-only release baseline. Deterministic,
/// dependency-free, integer-only.
fn checksum(bytes: &[u8]) -> u64 {
    const OFFSET_BASIS: u64 = 0xcbf29ce484222325;
    const PRIME: u64 = 0x0000_0100_0000_01b3;
    let mut hash = OFFSET_BASIS;
    for &byte in bytes {
        hash ^= u64::from(byte);
        hash = hash.wrapping_mul(PRIME);
    }
    hash
}

/// A forward-only cursor over a serialized store's bytes. Reads are exact and fail closed:
/// a missing newline, a malformed integer, a truncated length-prefixed string, or trailing
/// garbage all surface as [`StoreError::CorruptRecord`].
struct Cursor<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Cursor<'a> {
    fn new(serialized: &'a str) -> Self {
        Self {
            bytes: serialized.as_bytes(),
            pos: 0,
        }
    }

    /// The not-yet-consumed bytes (used to checksum the body after the header).
    fn remaining(&self) -> &'a [u8] {
        &self.bytes[self.pos..]
    }

    /// Read up to (and consuming) the next `\n`, returning the line without it.
    fn read_line(&mut self, context: &'static str) -> Result<&'a str, StoreError> {
        let start = self.pos;
        while self.pos < self.bytes.len() && self.bytes[self.pos] != b'\n' {
            self.pos += 1;
        }
        if self.pos >= self.bytes.len() {
            return Err(StoreError::CorruptRecord { context });
        }
        let line = &self.bytes[start..self.pos];
        self.pos += 1; // consume the '\n'
        std::str::from_utf8(line).map_err(|_| StoreError::CorruptRecord { context })
    }

    fn read_i64(&mut self, context: &'static str) -> Result<i64, StoreError> {
        self.read_line(context)?
            .parse::<i64>()
            .map_err(|_| StoreError::CorruptRecord { context })
    }

    fn read_u64(&mut self, context: &'static str) -> Result<u64, StoreError> {
        self.read_line(context)?
            .parse::<u64>()
            .map_err(|_| StoreError::CorruptRecord { context })
    }

    fn read_u32(&mut self, context: &'static str) -> Result<u32, StoreError> {
        self.read_line(context)?
            .parse::<u32>()
            .map_err(|_| StoreError::CorruptRecord { context })
    }

    fn read_bool(&mut self, context: &'static str) -> Result<bool, StoreError> {
        match self.read_line(context)? {
            "0" => Ok(false),
            "1" => Ok(true),
            _ => Err(StoreError::CorruptRecord { context }),
        }
    }

    /// Read a non-negative count line (a `usize`).
    fn read_count(&mut self, context: &'static str) -> Result<usize, StoreError> {
        self.read_line(context)?
            .parse::<usize>()
            .map_err(|_| StoreError::CorruptRecord { context })
    }

    /// Read a length-prefixed string: a byte-length line, then exactly that many bytes, then
    /// a terminating `\n`.
    fn read_str(&mut self, context: &'static str) -> Result<String, StoreError> {
        let len = self.read_count(context)?;
        let end = self
            .pos
            .checked_add(len)
            .ok_or(StoreError::CorruptRecord { context })?;
        if end >= self.bytes.len() || self.bytes[end] != b'\n' {
            return Err(StoreError::CorruptRecord { context });
        }
        let value = std::str::from_utf8(&self.bytes[self.pos..end])
            .map_err(|_| StoreError::CorruptRecord { context })?
            .to_string();
        self.pos = end + 1; // consume the trailing '\n'
        Ok(value)
    }

    /// Read an optional `f64` ratio (`N` / `S<bits>`), failing closed on a non-finite
    /// restored value so a NaN/inf can never re-enter a ranking from a tampered blob.
    fn read_opt_f64(&mut self, context: &'static str) -> Result<Option<f64>, StoreError> {
        let line = self.read_line(context)?;
        if line == "N" {
            return Ok(None);
        }
        let bits = line
            .strip_prefix('S')
            .ok_or(StoreError::CorruptRecord { context })?;
        let bits = bits
            .parse::<u64>()
            .map_err(|_| StoreError::CorruptRecord { context })?;
        let value = f64::from_bits(bits);
        if !value.is_finite() {
            return Err(StoreError::NonFiniteRatio { context });
        }
        Ok(Some(value))
    }

    /// Confirm the cursor is exhausted; trailing bytes mean the blob is corrupt.
    fn expect_end(&self) -> Result<(), StoreError> {
        if self.pos == self.bytes.len() {
            Ok(())
        } else {
            Err(StoreError::CorruptRecord {
                context: "trailing data",
            })
        }
    }
}

/// Fail-closed errors from backtest-record persistence. Carries no broker/vendor identifiers.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StoreError {
    /// The serialized blob was malformed: a bad magic header, a missing newline, a
    /// non-integer where an integer was expected, a truncated length-prefixed string, an
    /// unknown enum tag, or trailing data. `context` names where parsing failed.
    CorruptRecord { context: &'static str },
    /// The blob's schema version is not one this reader understands. Rejected loudly rather
    /// than mis-read.
    UnknownSchemaVersion { found: i64 },
    /// A field violated a record invariant (an empty id, an inverted run window, a negative
    /// starting cash, an invalid cost family, an equity mark outside the run window, or a
    /// missing benchmark identity). `context` names the violation.
    InconsistentField { context: &'static str },
    /// Two records shared a run id, so a result's identity would be ambiguous.
    DuplicateRunId { run_id: String },
    /// A restored metric/comparison ratio decoded to a non-finite value, so the blob was
    /// corrupted or tampered. `context` names the ratio.
    NonFiniteRatio { context: &'static str },
    /// The blob's integrity checksum did not match the body, so the bytes were corrupted or
    /// tampered after serialization. Rejected before any state is built.
    ChecksumMismatch,
    /// A filesystem operation behind [`BacktestResultStore::save_to_path`] /
    /// [`load_from_path`](BacktestResultStore::load_from_path) failed (a directory could not be
    /// created, the store file could not be written/published, or a present file could not be
    /// read). `context` names the operation. A *missing* store file is NOT this error — it
    /// restores an empty store; this variant is a real I/O failure that must fail closed rather
    /// than be mistaken for "fresh install".
    Io { context: &'static str },
}

impl fmt::Display for StoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::CorruptRecord { context } => {
                write!(f, "corrupt backtest record: {context}")
            }
            Self::UnknownSchemaVersion { found } => {
                write!(f, "unknown backtest-record schema version: {found}")
            }
            Self::InconsistentField { context } => {
                write!(f, "inconsistent backtest-record field: {context}")
            }
            Self::DuplicateRunId { run_id } => {
                write!(f, "duplicate backtest run id: {run_id}")
            }
            Self::NonFiniteRatio { context } => {
                write!(f, "non-finite backtest-record ratio: {context}")
            }
            Self::ChecksumMismatch => {
                write!(f, "backtest-record checksum mismatch")
            }
            Self::Io { context } => {
                write!(f, "backtest-store I/O failure: {context}")
            }
        }
    }
}

impl std::error::Error for StoreError {}

#[cfg(test)]
mod tests {
    use super::*;

    // Metric values are kept internally consistent with the strictly-RISING fixture equity
    // curve (1_000_000 -> 1_250_000): a monotonic rise has zero drawdown, and there is no closed
    // round trip, so win_rate is undefined. (The persistence layer does not re-derive metric
    // values -- that is the deferred run-snapshot boundary -- but the fixtures avoid modelling a
    // self-contradictory record.)
    fn metrics(benchmark: &str) -> PerformanceMetrics {
        PerformanceMetrics {
            sharpe_ratio: Some(1.5),
            sortino_ratio: Some(2.0),
            alpha: Some(0.01),
            beta: Some(0.9),
            max_drawdown: Some(0.0),
            annualized_return: Some(0.18),
            annualized_volatility: Some(0.12),
            win_rate: None,
            benchmark_symbol: benchmark.to_string(),
        }
    }

    fn comparison(benchmark: &str, is_default: bool) -> BenchmarkComparison {
        BenchmarkComparison {
            benchmark_symbol: benchmark.to_string(),
            is_default_benchmark: is_default,
            alpha: Some(0.01),
            beta: Some(0.9),
            strategy_total_return: Some(0.25),
            benchmark_total_return: Some(0.2),
            excess_return: Some(0.05),
        }
    }

    fn request(strategy: &str, symbol: &str, start: u64, end: u64) -> BacktestRequest {
        BacktestRequest {
            strategy_id: StrategyId::new(strategy),
            symbol: symbol.to_string(),
            data_source: BacktestDataSource::SystemData,
            range: DateRange { start, end },
            starting_cash_minor: 1_000_000,
            cost_config: CostConfig::default(),
        }
    }

    fn fill(ts: u64) -> Fill {
        Fill {
            ts,
            symbol: "AAPL".to_string(),
            quantity: 10,
            price_minor: 12_000,
            commission_minor: 100,
            slippage_minor: 5,
            spread_impact_minor: 3,
        }
    }

    fn params(pairs: &[(&str, &str)]) -> StrategyParameters {
        StrategyParameters::from_pairs(pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())))
            .unwrap()
    }

    fn record(run: &str, strategy: &str, completed_at: u64) -> BacktestRecord {
        record_with_params(run, strategy, completed_at, StrategyParameters::new())
    }

    fn record_with_params(
        run: &str,
        strategy: &str,
        completed_at: u64,
        parameters: StrategyParameters,
    ) -> BacktestRecord {
        BacktestRecord::new(
            RunId::new(run).unwrap(),
            request(strategy, "AAPL", 1, 100),
            parameters,
            metrics("SPY"),
            comparison("SPY", true),
            vec![fill(2), fill(50)],
            vec![
                EquityPoint {
                    ts: 1,
                    equity_minor: 1_000_000,
                },
                EquityPoint {
                    ts: 100,
                    equity_minor: 1_250_000,
                },
            ],
            CodeVersion::new("sha:abc123").unwrap(),
            completed_at,
        )
        .unwrap()
    }

    #[test]
    fn run_id_and_code_version_reject_empty() {
        assert!(RunId::new("  ").is_err());
        assert!(CodeVersion::new("").is_err());
        assert!(RunId::new("run-1").is_ok());
        assert!(CodeVersion::new("v1").is_ok());
    }

    /// A unique scratch directory under the OS temp dir. The suffix is a fixed per-test label,
    /// not a clock/RNG read, so the persistence layer itself stays deterministic; each test owns
    /// a distinct label so parallel test runs do not collide.
    fn temp_store_dir(label: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("atp_bt009_store_{label}"));
        let _ = fs::remove_dir_all(&dir);
        dir
    }

    #[test]
    fn save_then_load_round_trips_through_disk() {
        let dir = temp_store_dir("round_trip");
        let mut store = BacktestResultStore::new();
        store.insert(record("run-a", "alpha", 10)).unwrap();
        store
            .insert(record_with_params(
                "run-b",
                "beta",
                20,
                params(&[("lookback", "30")]),
            ))
            .unwrap();

        store.save_to_path(&dir).unwrap();
        // The durable file exists on disk, and the atomic publish left no scratch file behind:
        // the directory holds exactly the final store file.
        let names: Vec<String> = fs::read_dir(&dir)
            .unwrap()
            .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        assert_eq!(names, vec![STORE_FILENAME.to_string()]);

        let loaded = BacktestResultStore::load_from_path(&dir).unwrap();
        assert_eq!(loaded, store);
        assert_eq!(loaded.len(), 2);

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn load_missing_file_in_present_dir_is_empty_not_error() {
        let dir = temp_store_dir("missing_file");
        // The directory is provisioned but no result has ever been persisted: a fresh install.
        fs::create_dir_all(&dir).unwrap();
        let loaded = BacktestResultStore::load_from_path(&dir).unwrap();
        assert!(loaded.is_empty());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn load_missing_directory_fails_closed() {
        let dir = temp_store_dir("missing_dir");
        // The configured directory does not exist (unmounted / deleted / misconfigured). This
        // must fail closed, NOT masquerade as an empty history that silently drops persisted runs.
        assert!(matches!(
            BacktestResultStore::load_from_path(&dir),
            Err(StoreError::Io { .. })
        ));
    }

    #[test]
    fn load_corrupt_file_fails_closed() {
        let dir = temp_store_dir("corrupt");
        let mut store = BacktestResultStore::new();
        store.insert(record("run-a", "alpha", 10)).unwrap();
        store.save_to_path(&dir).unwrap();

        // Flip a byte in the persisted body so the checksum no longer matches: load must fail
        // closed rather than silently drop the run or return a partial store.
        let path = dir.join(STORE_FILENAME);
        let mut bytes = fs::read(&path).unwrap();
        *bytes.last_mut().unwrap() ^= 0xFF;
        fs::write(&path, &bytes).unwrap();

        assert!(BacktestResultStore::load_from_path(&dir).is_err());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn load_garbage_file_fails_closed() {
        let dir = temp_store_dir("garbage");
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join(STORE_FILENAME), "not a store blob").unwrap();
        assert!(BacktestResultStore::load_from_path(&dir).is_err());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn new_rejects_incoherent_records() {
        // Inverted run window.
        let bad = BacktestRecord::new(
            RunId::new("r").unwrap(),
            request("s", "AAPL", 100, 1),
            StrategyParameters::new(),
            metrics("SPY"),
            comparison("SPY", true),
            vec![],
            vec![],
            CodeVersion::new("v").unwrap(),
            10,
        );
        assert!(matches!(bad, Err(StoreError::InconsistentField { .. })));

        // Equity mark outside the run window.
        let bad = BacktestRecord::new(
            RunId::new("r").unwrap(),
            request("s", "AAPL", 1, 50),
            StrategyParameters::new(),
            metrics("SPY"),
            comparison("SPY", true),
            vec![],
            vec![EquityPoint {
                ts: 999,
                equity_minor: 1,
            }],
            CodeVersion::new("v").unwrap(),
            10,
        );
        assert!(matches!(bad, Err(StoreError::InconsistentField { .. })));

        // Missing benchmark identity.
        let bad = BacktestRecord::new(
            RunId::new("r").unwrap(),
            request("s", "AAPL", 1, 50),
            StrategyParameters::new(),
            metrics("  "),
            comparison("SPY", true),
            vec![],
            vec![],
            CodeVersion::new("v").unwrap(),
            10,
        );
        assert!(matches!(bad, Err(StoreError::InconsistentField { .. })));
    }

    fn valid_equity() -> Vec<EquityPoint> {
        vec![
            EquityPoint {
                ts: 1,
                equity_minor: 1_000_000,
            },
            EquityPoint {
                ts: 100,
                equity_minor: 1_250_000,
            },
        ]
    }

    #[test]
    fn new_rejects_incoherent_trade_log_and_benchmark() {
        // A fill from a different symbol than the run (with a valid curve so the fill loop is
        // reached).
        let mut bad_fill = fill(2);
        bad_fill.symbol = "MSFT".to_string();
        let bad = BacktestRecord::new(
            RunId::new("r").unwrap(),
            request("s", "AAPL", 1, 100),
            StrategyParameters::new(),
            metrics("SPY"),
            comparison("SPY", true),
            vec![bad_fill],
            valid_equity(),
            CodeVersion::new("v").unwrap(),
            10,
        );
        assert!(matches!(bad, Err(StoreError::InconsistentField { .. })));

        // A fill with a negative cost component.
        let mut neg_cost = fill(2);
        neg_cost.commission_minor = -1;
        let bad = BacktestRecord::new(
            RunId::new("r").unwrap(),
            request("s", "AAPL", 1, 100),
            StrategyParameters::new(),
            metrics("SPY"),
            comparison("SPY", true),
            vec![neg_cost],
            valid_equity(),
            CodeVersion::new("v").unwrap(),
            10,
        );
        assert!(matches!(bad, Err(StoreError::InconsistentField { .. })));

        // A reordered (non-monotonic) trade log.
        let bad = BacktestRecord::new(
            RunId::new("r").unwrap(),
            request("s", "AAPL", 1, 100),
            StrategyParameters::new(),
            metrics("SPY"),
            comparison("SPY", true),
            vec![fill(50), fill(2)],
            valid_equity(),
            CodeVersion::new("v").unwrap(),
            10,
        );
        assert!(matches!(bad, Err(StoreError::InconsistentField { .. })));

        // A fill outside the equity-curve window [1, 100].
        let bad = BacktestRecord::new(
            RunId::new("r").unwrap(),
            request("s", "AAPL", 1, 100),
            StrategyParameters::new(),
            metrics("SPY"),
            comparison("SPY", true),
            vec![fill(1)],
            vec![
                EquityPoint {
                    ts: 10,
                    equity_minor: 1_000_000,
                },
                EquityPoint {
                    ts: 100,
                    equity_minor: 1_250_000,
                },
            ],
            CodeVersion::new("v").unwrap(),
            10,
        );
        assert!(matches!(bad, Err(StoreError::InconsistentField { .. })));

        // A metrics/comparison benchmark-identity mismatch (QQQ vs SPY).
        let bad = BacktestRecord::new(
            RunId::new("r").unwrap(),
            request("s", "AAPL", 1, 100),
            StrategyParameters::new(),
            metrics("QQQ"),
            comparison("SPY", true),
            vec![],
            vec![],
            CodeVersion::new("v").unwrap(),
            10,
        );
        assert!(matches!(bad, Err(StoreError::InconsistentField { .. })));

        // A metrics/comparison alpha disagreement (same benchmark, conflicting coefficient).
        let mut disagreeing = comparison("SPY", true);
        disagreeing.alpha = Some(0.99);
        let bad = BacktestRecord::new(
            RunId::new("r").unwrap(),
            request("s", "AAPL", 1, 100),
            StrategyParameters::new(),
            metrics("SPY"),
            disagreeing,
            vec![],
            vec![],
            CodeVersion::new("v").unwrap(),
            10,
        );
        assert!(matches!(bad, Err(StoreError::InconsistentField { .. })));
    }

    #[test]
    fn new_enforces_metric_producer_equity_invariants() {
        // Empty curve: the metric producer (metrics::compute) rejects it, so a record cannot
        // carry genuine metrics over no curve.
        let bad = BacktestRecord::new(
            RunId::new("r").unwrap(),
            request("s", "AAPL", 1, 100),
            StrategyParameters::new(),
            metrics("SPY"),
            comparison("SPY", true),
            vec![],
            vec![],
            CodeVersion::new("v").unwrap(),
            10,
        );
        assert!(matches!(bad, Err(StoreError::InconsistentField { .. })));

        // Non-monotonic timestamps would make the period returns ambiguous.
        let bad = BacktestRecord::new(
            RunId::new("r").unwrap(),
            request("s", "AAPL", 1, 100),
            StrategyParameters::new(),
            metrics("SPY"),
            comparison("SPY", true),
            vec![],
            vec![
                EquityPoint {
                    ts: 5,
                    equity_minor: 100,
                },
                EquityPoint {
                    ts: 3,
                    equity_minor: 100,
                },
            ],
            CodeVersion::new("v").unwrap(),
            10,
        );
        assert!(matches!(bad, Err(StoreError::InconsistentField { .. })));

        // A non-positive INTERIOR mark (a return denominator) is rejected.
        let bad = BacktestRecord::new(
            RunId::new("r").unwrap(),
            request("s", "AAPL", 1, 100),
            StrategyParameters::new(),
            metrics("SPY"),
            comparison("SPY", true),
            vec![],
            vec![
                EquityPoint {
                    ts: 1,
                    equity_minor: 0,
                },
                EquityPoint {
                    ts: 2,
                    equity_minor: 100,
                },
            ],
            CodeVersion::new("v").unwrap(),
            10,
        );
        assert!(matches!(bad, Err(StoreError::InconsistentField { .. })));

        // ...but a TERMINAL zero (a total loss, a defined -100% final return) is accepted.
        let ok = BacktestRecord::new(
            RunId::new("r").unwrap(),
            request("s", "AAPL", 1, 100),
            StrategyParameters::new(),
            metrics("SPY"),
            comparison("SPY", true),
            vec![],
            vec![
                EquityPoint {
                    ts: 1,
                    equity_minor: 100,
                },
                EquityPoint {
                    ts: 2,
                    equity_minor: 0,
                },
            ],
            CodeVersion::new("v").unwrap(),
            10,
        );
        assert!(ok.is_ok());
    }

    #[test]
    fn restore_fails_closed_on_oversized_count() {
        // A checksum-valid blob claiming an enormous record count must fail closed: the decode
        // loops grow incrementally (never pre-sized from an untrusted count), so a count beyond
        // the data exhausts the cursor rather than aborting on an out-of-memory alloc.
        let blob = BacktestResultStore::new().serialize();
        let mut lines: Vec<&str> = blob.lines().collect();
        // Empty-store body is "1\n0\n": lines[2] = schema version, lines[3] = record count.
        assert_eq!(lines[3], "0");
        lines[3] = "999999999999";
        let body = lines[2..].join("\n") + "\n";
        let new_checksum = checksum(body.as_bytes());
        let rebuilt = format!("{MAGIC}\n{new_checksum}\n{body}");
        assert!(matches!(
            BacktestResultStore::restore(&rebuilt),
            Err(StoreError::CorruptRecord { .. })
        ));
    }

    #[test]
    fn new_rejects_contradictory_excess_return() {
        // excess_return must equal strategy_total_return - benchmark_total_return (0.25 - 0.2 =
        // 0.05); a contradicting value is rejected (internal-consistency guard).
        let mut bad_comp = comparison("SPY", true);
        bad_comp.excess_return = Some(0.99);
        let bad = BacktestRecord::new(
            RunId::new("r").unwrap(),
            request("s", "AAPL", 1, 100),
            StrategyParameters::new(),
            metrics("SPY"),
            bad_comp,
            vec![],
            valid_equity(),
            CodeVersion::new("v").unwrap(),
            10,
        );
        assert!(matches!(bad, Err(StoreError::InconsistentField { .. })));
    }

    #[test]
    fn new_rejects_out_of_domain_metrics() {
        // win_rate and max_drawdown are fractions in [0, 1]; annualized_volatility is
        // non-negative. A value outside its domain is impossible and rejected.
        let cases: [fn(&mut PerformanceMetrics); 4] = [
            |m| m.win_rate = Some(1.5),
            |m| m.max_drawdown = Some(-0.1),
            |m| m.max_drawdown = Some(1.5),
            |m| m.annualized_volatility = Some(-0.01),
        ];
        for mutate in cases {
            let mut bad_metrics = metrics("SPY");
            mutate(&mut bad_metrics);
            let bad = BacktestRecord::new(
                RunId::new("r").unwrap(),
                request("s", "AAPL", 1, 100),
                StrategyParameters::new(),
                bad_metrics,
                comparison("SPY", true),
                vec![],
                valid_equity(),
                CodeVersion::new("v").unwrap(),
                10,
            );
            assert!(matches!(bad, Err(StoreError::InconsistentField { .. })));
        }

        // The valid-domain boundaries (win_rate 0 and 1, drawdown 1, volatility 0) are accepted.
        let mut edge = metrics("SPY");
        edge.win_rate = Some(1.0);
        edge.max_drawdown = Some(1.0);
        edge.annualized_volatility = Some(0.0);
        let ok = BacktestRecord::new(
            RunId::new("r").unwrap(),
            request("s", "AAPL", 1, 100),
            StrategyParameters::new(),
            edge,
            comparison("SPY", true),
            vec![],
            valid_equity(),
            CodeVersion::new("v").unwrap(),
            10,
        );
        assert!(ok.is_ok());
    }

    #[test]
    fn from_result_binds_artifacts_and_rejects_provenance_mismatch() {
        let req = request("s", "AAPL", 1, 100);
        let result = BacktestResult {
            data_source: BacktestDataSource::SystemData,
            range: DateRange { start: 1, end: 100 },
            bars_processed: 2,
            trade_log: vec![fill(2), fill(50)],
            equity_curve: valid_equity(),
            final_equity_minor: 1_250_000,
        };
        // Matching provenance: the record is built from the result's artifacts.
        let ok = BacktestRecord::from_result(
            RunId::new("r").unwrap(),
            req.clone(),
            StrategyParameters::new(),
            metrics("SPY"),
            comparison("SPY", true),
            &result,
            CodeVersion::new("v").unwrap(),
            10,
        )
        .unwrap();
        assert_eq!(ok.trade_log, result.trade_log);
        assert_eq!(ok.equity_curve, result.equity_curve);

        // A request with a different data source than the result is rejected.
        let mut wrong = req.clone();
        wrong.data_source = BacktestDataSource::UploadedData;
        let bad = BacktestRecord::from_result(
            RunId::new("r").unwrap(),
            wrong,
            StrategyParameters::new(),
            metrics("SPY"),
            comparison("SPY", true),
            &result,
            CodeVersion::new("v").unwrap(),
            10,
        );
        assert!(matches!(bad, Err(StoreError::InconsistentField { .. })));
    }

    #[test]
    fn restore_round_trips_a_large_store() {
        // Bulk restore must stay correct (and O(n log n), not O(n^2)): decode -> dedup -> sort
        // once -> construct.
        let mut store = BacktestResultStore::new();
        for i in 0..300u64 {
            store
                .insert(record(&format!("run-{i}"), "alpha", i))
                .unwrap();
        }
        assert_eq!(store.len(), 300);

        let restored = BacktestResultStore::restore(&store.serialize()).unwrap();
        assert_eq!(restored, store);
        // Canonical (completed_at_ts, run_id) order is preserved across the bulk round trip.
        let order: Vec<u64> = restored
            .records()
            .iter()
            .map(|r| r.completed_at_ts)
            .collect();
        assert!(order.windows(2).all(|w| w[0] <= w[1]));
    }

    #[test]
    fn strategy_parameters_canonicalize_and_reject_bad_keys() {
        // Order-independent: two sets built in different orders are equal.
        let a = params(&[("lookback", "20"), ("threshold", "0.5")]);
        let b = params(&[("threshold", "0.5"), ("lookback", "20")]);
        assert_eq!(a, b);
        // Reject a duplicate key and an empty key.
        assert!(StrategyParameters::from_pairs([
            ("k".to_string(), "1".to_string()),
            ("k".to_string(), "2".to_string()),
        ])
        .is_err());
        assert!(StrategyParameters::from_pairs([("  ".to_string(), "1".to_string())]).is_err());
    }

    #[test]
    fn query_by_parameter_set_distinguishes_sweep_points() {
        // Same strategy, same window (identical BacktestRequest), different tuned parameters.
        let mut store = BacktestResultStore::new();
        store
            .insert(record_with_params(
                "p10",
                "sweep",
                10,
                params(&[("lookback", "10")]),
            ))
            .unwrap();
        store
            .insert(record_with_params(
                "p20",
                "sweep",
                20,
                params(&[("lookback", "20")]),
            ))
            .unwrap();

        // Both share an identical BacktestRequest, so only the parameter set tells them apart.
        let ten = store.query_by_parameter_set(&params(&[("lookback", "10")]));
        assert_eq!(ten.len(), 1);
        assert_eq!(ten[0].run_id.as_str(), "p10");
        let twenty = store.query_by_parameter_set(&params(&[("lookback", "20")]));
        assert_eq!(twenty.len(), 1);
        assert_eq!(twenty[0].run_id.as_str(), "p20");
        // A parameter set no run used matches nothing.
        assert!(store
            .query_by_parameter_set(&params(&[("lookback", "99")]))
            .is_empty());
    }

    #[test]
    fn insert_rejects_duplicate_run_id() {
        let mut store = BacktestResultStore::new();
        store.insert(record("dup", "alpha", 10)).unwrap();
        let err = store.insert(record("dup", "beta", 20)).unwrap_err();
        assert!(matches!(err, StoreError::DuplicateRunId { .. }));
        assert_eq!(store.len(), 1);
    }

    #[test]
    fn store_holds_records_in_canonical_order() {
        let mut store = BacktestResultStore::new();
        // Insert out of order; the store canonicalizes by (completed_at_ts, run_id).
        store.insert(record("r3", "alpha", 30)).unwrap();
        store.insert(record("r1", "alpha", 10)).unwrap();
        store.insert(record("r2", "alpha", 20)).unwrap();
        let order: Vec<u64> = store.records().iter().map(|r| r.completed_at_ts).collect();
        assert_eq!(order, vec![10, 20, 30]);
    }

    #[test]
    fn queries_filter_each_axis() {
        let mut store = BacktestResultStore::new();
        store.insert(record("a1", "alpha", 10)).unwrap();
        store.insert(record("a2", "alpha", 40)).unwrap();
        store.insert(record("b1", "beta", 20)).unwrap();

        // By strategy.
        let alpha = store.query_by_strategy(&StrategyId::new("alpha"));
        assert_eq!(alpha.len(), 2);
        for r in &alpha {
            assert_eq!(r.request.strategy_id.as_str(), "alpha");
        }

        // By completion window (the completion-timestamp axis).
        let windowed = store.query_by_completion_window(DateRange { start: 15, end: 45 });
        let ids: Vec<&str> = windowed.iter().map(|r| r.run_id.as_str()).collect();
        assert_eq!(ids, vec!["b1", "a2"]); // canonical order: (20,b1) then (40,a2)

        // By parameter set: these records all carry the empty parameter set, so it matches
        // all three (the sweep-distinguishing case is covered in its own test).
        let by_params = store.query_by_parameter_set(&StrategyParameters::new());
        assert_eq!(by_params.len(), 3);

        // Empty / no-match.
        let none = store.query_by_strategy(&StrategyId::new("missing"));
        assert!(none.is_empty());
    }

    #[test]
    fn combined_query_ands_axes() {
        let mut store = BacktestResultStore::new();
        store.insert(record("a1", "alpha", 10)).unwrap();
        store.insert(record("a2", "alpha", 40)).unwrap();
        store.insert(record("b1", "beta", 20)).unwrap();

        let query = RecordQuery {
            strategy_id: Some(StrategyId::new("alpha")),
            completed_within: Some(DateRange { start: 0, end: 25 }),
            ..Default::default()
        };
        let result = store.query(&query);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].run_id.as_str(), "a1");

        // Default query returns everything.
        assert_eq!(store.query(&RecordQuery::default()).len(), 3);
    }

    fn record_with_range(
        run: &str,
        strategy: &str,
        completed_at: u64,
        start: u64,
        end: u64,
    ) -> BacktestRecord {
        // A coherent record over the run window [start, end] with no fills (so the only varying
        // axis is the tested period).
        BacktestRecord::new(
            RunId::new(run).unwrap(),
            request(strategy, "AAPL", start, end),
            StrategyParameters::new(),
            metrics("SPY"),
            comparison("SPY", true),
            vec![],
            vec![
                EquityPoint {
                    ts: start,
                    equity_minor: 1_000_000,
                },
                EquityPoint {
                    ts: end,
                    equity_minor: 1_100_000,
                },
            ],
            CodeVersion::new("v").unwrap(),
            completed_at,
        )
        .unwrap()
    }

    #[test]
    fn query_by_run_window_finds_by_tested_period() {
        // Two runs over different tested periods, completed in the other period's order — so the
        // run-window axis and the completion axis disagree (the SYS-21 motivation).
        let mut store = BacktestResultStore::new();
        store
            .insert(record_with_range("r2020", "s", 100, 2020, 2021))
            .unwrap();
        store
            .insert(record_with_range("r2023", "s", 200, 2023, 2024))
            .unwrap();

        // A 2020 date-range query finds the 2020 run regardless of when it was executed.
        let y2020 = store.query_by_run_window(DateRange::new(2020, 2020));
        assert_eq!(y2020.len(), 1);
        assert_eq!(y2020[0].run_id.as_str(), "r2020");
        // Overlap: a span covering both tested periods finds both.
        assert_eq!(
            store.query_by_run_window(DateRange::new(2019, 2025)).len(),
            2
        );
        // A period no run tested matches nothing.
        assert!(store
            .query_by_run_window(DateRange::new(2030, 2031))
            .is_empty());

        // The completion-window axis is DISTINCT — it filters the completion timestamp.
        let by_completion = store.query_by_completion_window(DateRange::new(150, 250));
        assert_eq!(by_completion.len(), 1);
        assert_eq!(by_completion[0].run_id.as_str(), "r2023");

        // Combined: the 2020 run window AND a completion window it does NOT fall in -> none.
        let mismatched = store.query(&RecordQuery {
            run_window: Some(DateRange::new(2020, 2021)),
            completed_within: Some(DateRange::new(150, 250)),
            ..Default::default()
        });
        assert!(mismatched.is_empty());
    }

    #[test]
    fn serialize_round_trips_the_store() {
        let mut store = BacktestResultStore::new();
        store.insert(record("r1", "alpha", 10)).unwrap();
        store.insert(record("r2", "beta", 20)).unwrap();

        let blob = store.serialize();
        let restored = BacktestResultStore::restore(&blob).unwrap();
        assert_eq!(restored, store);

        // Deterministic: serializing the restored store yields byte-identical output.
        assert_eq!(restored.serialize(), blob);
    }

    #[test]
    fn serialize_is_insertion_order_independent() {
        let mut a = BacktestResultStore::new();
        a.insert(record("r1", "alpha", 10)).unwrap();
        a.insert(record("r2", "beta", 20)).unwrap();

        let mut b = BacktestResultStore::new();
        b.insert(record("r2", "beta", 20)).unwrap();
        b.insert(record("r1", "alpha", 10)).unwrap();

        assert_eq!(a.serialize(), b.serialize());
        assert_eq!(a, b);
    }

    #[test]
    fn restore_fails_closed_on_corruption() {
        let mut store = BacktestResultStore::new();
        store.insert(record("r1", "alpha", 10)).unwrap();
        let blob = store.serialize();

        // Bad magic.
        let foreign = blob.replacen("ATP-BACKTEST-RECORD", "ATP-OTHER", 1);
        assert!(matches!(
            BacktestResultStore::restore(&foreign),
            Err(StoreError::CorruptRecord { .. })
        ));

        // Truncation.
        let truncated = &blob[..blob.len() / 2];
        assert!(BacktestResultStore::restore(truncated).is_err());

        // Tamper a body byte without fixing the checksum (flip a digit in the run id length
        // or a metric) -> checksum mismatch. Flip the first occurrence of "alpha".
        let tampered = blob.replacen("alpha", "alphb", 1);
        assert!(matches!(
            BacktestResultStore::restore(&tampered),
            Err(StoreError::ChecksumMismatch)
        ));
    }

    #[test]
    fn restore_rejects_unknown_schema_version() {
        let mut store = BacktestResultStore::new();
        store.insert(record("r1", "alpha", 10)).unwrap();
        let blob = store.serialize();
        // The schema version is the first body line after MAGIC + checksum. Rebuild a blob
        // whose schema line is 999 with a matching checksum so we exercise the version guard
        // (not the checksum guard).
        let mut lines: Vec<&str> = blob.lines().collect();
        // lines[0] = MAGIC, lines[1] = checksum, lines[2] = schema version.
        assert_eq!(lines[2], "1");
        lines[2] = "999";
        let body = lines[2..].join("\n") + "\n";
        let new_checksum = checksum(body.as_bytes());
        let rebuilt = format!("{MAGIC}\n{new_checksum}\n{body}");
        assert!(matches!(
            BacktestResultStore::restore(&rebuilt),
            Err(StoreError::UnknownSchemaVersion { found: 999 })
        ));
    }

    #[test]
    fn f64_ratios_round_trip_exactly_including_none() {
        let mut store = BacktestResultStore::new();
        let mut rec = record("r1", "alpha", 10);
        // A precise irrational-ish value and a None must both survive exactly. (Use metric
        // fields not bound by the excess-return identity, so the round-trip is what's exercised.)
        rec.metrics.sharpe_ratio = Some(1.234_567_890_123_456_7);
        rec.metrics.win_rate = None;
        rec.metrics.annualized_return = Some(-0.000_000_000_1);
        store.records.clear();
        store.insert(rec).unwrap();

        let restored = BacktestResultStore::restore(&store.serialize()).unwrap();
        let r = &restored.records()[0];
        assert_eq!(r.metrics.sharpe_ratio, Some(1.234_567_890_123_456_7));
        assert_eq!(r.metrics.win_rate, None);
        assert_eq!(r.metrics.annualized_return, Some(-0.000_000_000_1));
    }

    #[test]
    fn restore_rejects_non_finite_ratio() {
        // A blob whose sharpe ratio decodes to +inf must fail closed (a tampered ranking
        // input). Build a one-record blob, then replace the sharpe `S<bits>` payload with
        // the bits of f64::INFINITY and recompute the checksum.
        let mut store = BacktestResultStore::new();
        store.insert(record("r1", "alpha", 10)).unwrap();
        let blob = store.serialize();

        let good_bits = 1.5_f64.to_bits().to_string();
        let inf_bits = f64::INFINITY.to_bits().to_string();
        let tampered_body_full =
            blob.replacen(&format!("S{good_bits}"), &format!("S{inf_bits}"), 1);
        // Recompute the checksum over the tampered body so we hit the finite guard, not the
        // checksum guard.
        let mut lines: Vec<&str> = tampered_body_full.lines().collect();
        let body = lines.split_off(2).join("\n") + "\n";
        let new_checksum = checksum(body.as_bytes());
        let rebuilt = format!("{MAGIC}\n{new_checksum}\n{body}");
        assert!(matches!(
            BacktestResultStore::restore(&rebuilt),
            Err(StoreError::NonFiniteRatio { .. })
        ));
    }

    #[test]
    fn cost_config_overrides_round_trip() {
        let mut store = BacktestResultStore::new();
        let mut rec = record("r1", "alpha", 10);
        rec.request.cost_config = CostConfig {
            commission: CommissionModel::PerShare {
                rate_centiminor_per_share: 35,
                min_per_order_minor: 100,
            },
            slippage: SlippageModel::None,
            spread_impact: SpreadImpactModel::FixedBps { bps: 7 },
        };
        store.records.clear();
        store.insert(rec).unwrap();

        let restored = BacktestResultStore::restore(&store.serialize()).unwrap();
        assert_eq!(restored, store);
    }
}
