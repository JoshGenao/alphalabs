//! Benchmark selection, resolution, and comparison for **SRS-BT-005** -- "compare
//! strategy performance against a user-selected benchmark defaulting to SPY"
//! (docs/SRS.md SRS-5.6 SRS-BT-005; traces SyRS SYS-17 / SYS-36 / SYS-37; StRS
//! SN-1.04).
//!
//! # What this module adds over the SRS-BT-004 metric family
//!
//! The SESSION 44 [`metrics`](crate::metrics) slice already ships the *math*: given a
//! [`Benchmark`] (SPY default) and a [`BenchmarkPoint`] level series,
//! [`metrics::compute`](crate::metrics::compute) produces alpha and beta against that
//! benchmark. SRS-BT-005 is the three pieces that *wrap* that math:
//!
//!   1. **Selection** -- [`BenchmarkSelection`] resolves to SPY when the operator
//!      selects no benchmark (the acceptance criterion "if no benchmark is selected,
//!      SPY is used") and validates a user-selected ticker.
//!   2. **Resolution** -- [`BenchmarkSource`] is the port that turns a selected
//!      benchmark into the integer-minor level series `compute` needs (aligned 1:1 by
//!      timestamp with the strategy's equity curve, with a pre-trade baseline first).
//!      Mirrors the [`BarSource`](crate::backtest::BarSource) deferral seam: the real
//!      stored-data resolver is deferred (owner: SRS-DATA-007, the unified historical
//!      data interface); a fixture implementation drives the tests.
//!   3. **Comparison report** -- [`BenchmarkComparison`] is the data shape a report
//!      renders to *identify* and *contrast against* its benchmark (the benchmark
//!      symbol plus alpha, beta, and total/excess return). [`compare`] ties the three
//!      together.
//!
//! # Numeric boundary and determinism (inherited from the metric family)
//!
//! Money/level inputs stay in **integer minor units** ([`EquityPoint::equity_minor`]
//! and [`BenchmarkPoint::level_minor`] are `i64`); the comparison outputs are
//! dimensionless `f64` ratios -- the `f64` is the metric domain, not a money-correctness
//! leak. The work is deterministic (SRS-BT-010): every reduction is a fixed
//! left-to-right fold with no parallelism, RNG, or wall-clock read, and every emitted
//! ratio is verified finite ([`BenchmarkError::NonFiniteComparison`]) so a pathological
//! input fails closed rather than leaking NaN/inf into a ranking or dashboard.
//!
//! # Trust boundary
//!
//! [`BenchmarkSource`] is a trust boundary exactly like the backtest's bar reader: a
//! real resolver reads an external catalog and could return a malformed or misaligned
//! series. [`compare`] therefore validates the resolved series (symbol, length,
//! per-timestamp alignment, strict positivity) and fails closed with a
//! source-attributed [`BenchmarkError`] *before* handing it to `compute`, which
//! re-validates as defense-in-depth (its [`MetricsError`] is wrapped as
//! [`BenchmarkError::Metrics`]).
//!
//! # What stays deferred (SRS-BT-005 keeps `passes:false`)
//!
//! This slice ships the deterministic, dependency-free selection + resolution-seam +
//! comparison surface. Two acceptance pieces are genuinely blocked on unbuilt features
//! and are deferred (so `feature_list.json` keeps SRS-BT-005 at `passes:false`):
//! resolving SPY's (or a user-selected benchmark's) *actual* historical level series
//! from stored data is the **SRS-DATA-007** owner behind [`BenchmarkSource`]; and the
//! dashboard/backtest *report rendering* that identifies the benchmark to an operator
//! at the SYS-36 (<=5s) refresh is the **SRS-UI / SRS-API** owner consuming
//! [`BenchmarkComparison`].

use crate::backtest::{DateRange, EquityPoint, Fill};
use crate::metrics::{
    compute, Benchmark, BenchmarkPoint, MetricsConfig, MetricsError, PerformanceMetrics,
};

/// An operator's benchmark choice for a run.
///
/// Defaults to "no explicit selection", which [`resolve`](BenchmarkSelection::resolve)
/// turns into SPY -- the SRS-BT-005 acceptance criterion "if no benchmark is selected,
/// SPY is used" (SYS-17). A user-selected benchmark is validated through
/// [`Benchmark::new`] so a report can never identify a malformed (empty / non-canonical)
/// benchmark.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct BenchmarkSelection {
    /// `None` means "operator selected nothing" -- resolves to SPY.
    selected: Option<Benchmark>,
}

impl BenchmarkSelection {
    /// No explicit selection: resolves to the SPY default (SYS-17). Equivalent to
    /// [`BenchmarkSelection::default`].
    pub fn unselected() -> Self {
        Self { selected: None }
    }

    /// An explicit, already-validated benchmark selection.
    pub fn of(benchmark: Benchmark) -> Self {
        Self {
            selected: Some(benchmark),
        }
    }

    /// A user-selected benchmark by ticker symbol. Fails closed (mapped to
    /// [`BenchmarkError::UnknownBenchmark`]) on an empty/whitespace or non-canonical
    /// (non-uppercase) symbol, so the selection surface cannot admit a benchmark a
    /// report could not honestly identify.
    pub fn from_symbol(symbol: impl Into<String>) -> Result<Self, BenchmarkError> {
        let symbol = symbol.into();
        match Benchmark::new(symbol.clone()) {
            Ok(benchmark) => Ok(Self::of(benchmark)),
            Err(_) => Err(BenchmarkError::UnknownBenchmark { symbol }),
        }
    }

    /// Resolve the selection to a concrete [`Benchmark`]: the explicit selection if one
    /// was made, otherwise SPY. This is the single place the SPY default is applied.
    pub fn resolve(&self) -> Benchmark {
        self.selected.clone().unwrap_or_default()
    }

    /// Whether this selection falls back to the SPY default (the operator selected
    /// nothing). Lets a report state that the comparison used the default benchmark.
    pub fn is_default(&self) -> bool {
        self.selected.is_none()
    }
}

/// Why a [`BenchmarkSource`] could not resolve the benchmark's levels.
///
/// A real SRS-DATA-007-backed resolver reads an external/stored data layer, which can
/// fail operationally in ways that are NOT a malformed series: the read can time out, the
/// catalog can be unreachable, the symbol/window can be absent, or the data can be present
/// but too stale to trust (the platform's stale-data safeguard). The port must be able to
/// surface these distinctly so a caller can retry, alert, or fail closed correctly,
/// rather than misclassifying an operational failure as a corrupt series. This is the
/// adapter-boundary failure contract for the deferred resolver.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SourceFailure {
    /// The data-layer read timed out.
    Timeout,
    /// The data layer / benchmark catalog was unreachable or unavailable.
    Unavailable,
    /// The benchmark symbol or requested window has no stored data.
    NotFound,
    /// The resolved data was present but too stale to trust (stale-data blocking).
    StaleData,
}

impl SourceFailure {
    /// Stable wire token for logs / API payloads.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Timeout => "timeout",
            Self::Unavailable => "unavailable",
            Self::NotFound => "not_found",
            Self::StaleData => "stale_data",
        }
    }
}

/// A benchmark series resolved by a [`BenchmarkSource`], with the symbol the source
/// **actually** resolved bound to the returned data.
///
/// The identity lives ON the payload, not in a separate pre-fetch declaration: [`compare`]
/// validates `symbol` equals the selected benchmark *after* the fetch, so a buggy cache or
/// resolver that returns one benchmark's levels cannot be reported as another. (A resolver
/// that lies about both the symbol AND the data is the deferred provenance/run-identity
/// concern -- the same boundary deferred by SRS-BT-004; this binds the check to the data
/// that was actually returned, closing the decoupled-pre-fetch gap.)
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedBenchmark {
    /// The benchmark symbol the returned `levels` are for (the source's own label).
    pub symbol: String,
    /// The pre-trade baseline level then one level per equity-curve mark, in order.
    pub levels: Vec<BenchmarkPoint>,
}

/// The deferred benchmark-resolution seam: turns a selected [`Benchmark`] into the
/// integer-minor level series the metric family compares against.
///
/// A real implementation reads the benchmark's stored historical price levels (owner:
/// SRS-DATA-007, the unified historical data interface) for `baseline_ts` followed by
/// each timestamp in `axis` (the strategy equity curve's marks). The returned
/// [`ResolvedBenchmark`] must carry the symbol it actually resolved plus its
/// pre-trade baseline level (paired with the strategy's starting equity) at `baseline_ts`,
/// then one level per `axis` timestamp in order -- i.e. `axis.len() + 1` points -- so it
/// aligns 1:1 with the equity curve for [`metrics::compute`](crate::metrics::compute). A
/// source that cannot align, or that resolves a different symbol than requested, is a trust
/// boundary: [`compare`] re-validates the response and fails closed rather than silently
/// comparing against a partial overlap or a substituted benchmark.
///
/// The error type is the narrow [`SourceFailure`] (not the broad [`BenchmarkError`]): a
/// source can ONLY report a typed operational read failure (timeout / unavailable /
/// not-found / stale), and [`compare`] maps it to [`BenchmarkError::SourceUnavailable`].
/// This is compiler-enforced -- a source cannot return a consumer-only error
/// ([`BenchmarkError::Metrics`], [`BenchmarkError::SourceSymbolMismatch`], etc.) that it
/// has no business producing.
///
/// Timeout/cancellation ENFORCEMENT (a hard deadline on a hung resolver) is the I/O
/// adapter's responsibility, not this pure, synchronous, deterministic (SRS-BT-010)
/// function: the deferred SRS-DATA-007 resolver performs the real external read and the
/// async SRS-UI / SRS-API layer owns the SYS-36 (<=5s) budget. [`SourceFailure::Timeout`]
/// is the contract by which a deadline-aware adapter REPORTS a timeout into this core.
pub trait BenchmarkSource {
    /// Resolve `symbol`'s level series for the run `window` (e.g. `BacktestResult.range`)
    /// aligned to `axis` (the equity-curve mark timestamps). The returned
    /// [`ResolvedBenchmark`] must carry the symbol it actually resolved and
    /// `axis.len() + 1` levels: a **pre-window baseline** observation first (at a timestamp
    /// strictly before the first mark -- the prior close, NOT necessarily `window.start`,
    /// since the inclusive window may open exactly on the first mark), then one level per
    /// `axis` timestamp in order. A failure can only be a typed [`SourceFailure`], which
    /// [`compare`] maps to [`BenchmarkError::SourceUnavailable`].
    fn levels(
        &self,
        symbol: &str,
        window: DateRange,
        axis: &[u64],
    ) -> Result<ResolvedBenchmark, SourceFailure>;
}

/// A strategy-versus-benchmark comparison -- the data a dashboard or backtest report
/// renders to identify and contrast against its benchmark (SRS-BT-005 acceptance:
/// "dashboard and backtest reports identify the benchmark").
///
/// `alpha` and `beta` are the CAPM coefficients from the metric family (the same values
/// echoed on [`PerformanceMetrics`]); the total/excess returns are dimensionless `f64`
/// ratios contrasting the strategy against the benchmark over the same window. Each is
/// `Option<f64>`: `None` when undefined on the input (e.g. no benchmark variance for
/// beta, or too few points for a return), never a fabricated zero.
#[derive(Debug, Clone, PartialEq)]
pub struct BenchmarkComparison {
    /// The benchmark the comparison is against (SPY by default) -- the report's identity.
    pub benchmark_symbol: String,
    /// `true` when the comparison used the SPY default because no benchmark was selected.
    pub is_default_benchmark: bool,
    /// Jensen's alpha against `benchmark_symbol` (excess-return CAPM residual).
    pub alpha: Option<f64>,
    /// Beta against `benchmark_symbol`.
    pub beta: Option<f64>,
    /// The strategy's total return over the run: `(final_equity - starting) / starting`.
    pub strategy_total_return: Option<f64>,
    /// The benchmark's total return over the same window: `(last - baseline) / baseline`.
    pub benchmark_total_return: Option<f64>,
    /// The strategy's excess total return over the benchmark
    /// (`strategy_total_return - benchmark_total_return`); `None` when either side is.
    pub excess_return: Option<f64>,
}

/// A completed benchmark comparison: the full SRS-BT-004 metric family plus the
/// SRS-BT-005 [`BenchmarkComparison`] computed against the resolved benchmark.
#[derive(Debug, Clone, PartialEq)]
pub struct BenchmarkReport {
    /// The eight SYS-16 metrics (alpha/beta computed against the resolved benchmark).
    pub metrics: PerformanceMetrics,
    /// The strategy-versus-benchmark comparison the report identifies.
    pub comparison: BenchmarkComparison,
}

/// Fail-closed benchmark-comparison errors. Carries no broker/vendor identifiers.
#[derive(Debug, Clone, PartialEq)]
pub enum BenchmarkError {
    /// A user-selected benchmark symbol was empty/whitespace or non-canonical, so it is
    /// not a benchmark a report could honestly identify.
    UnknownBenchmark { symbol: String },
    /// The strategy equity curve was empty, so there is no run to compare.
    EmptyEquityCurve,
    /// The evaluation window was inverted (`start > end`); it cannot describe a run.
    InvalidWindow { start: u64, end: u64 },
    /// An equity-curve mark fell outside the run's evaluation window. The comparison is
    /// bound to the strategy run window: a mark outside it means the equity curve does
    /// not describe this run, so the benchmark would be measured over a different period.
    EquityMarkOutsideWindow {
        ts: u64,
        window_start: u64,
        window_end: u64,
    },
    /// The source's baseline observation was not strictly before the run's first mark. The
    /// benchmark baseline is the pre-trade observation -- the benchmark's level immediately
    /// before the first equity mark (the prior close) -- so the benchmark's first-period
    /// return spans the same `[pre-first-bar, first_mark]` interval as the strategy's first
    /// return, and the benchmark level series is strictly increasing in timestamp. The
    /// run window is INCLUSIVE, so the first mark may land exactly on `window.start`; the
    /// baseline (prior close) is then before `window.start`, which is valid. Verifying the
    /// baseline is the IMMEDIATE prior close (not an arbitrarily stale earlier observation)
    /// requires the data layer's bar grid and is the deferred SRS-DATA-007 resolver's
    /// responsibility; this in-scope guard is that it precedes the first mark.
    BaselineNotBeforeRun { baseline_ts: u64, first_ts: u64 },
    /// The benchmark source could not resolve its levels for an operational reason
    /// (timeout, unavailable, not-found, stale). Carries the typed [`SourceFailure`] so a
    /// caller can retry / alert / fail closed rather than treating a read failure as a
    /// malformed series.
    SourceUnavailable { failure: SourceFailure },
    /// The source resolved a different benchmark than the selection. The source is a
    /// trust boundary: it must never substitute a benchmark other than the one the
    /// report identifies.
    SourceSymbolMismatch { requested: String, returned: String },
    /// The resolved series did not have `equity_curve.len() + 1` points, so it cannot
    /// align period-for-period with the strategy (baseline + one level per mark).
    SourceLengthMismatch { expected: usize, found: usize },
    /// A resolved level's timestamp did not equal the baseline / equity mark it must
    /// align with, so the two return series cannot be paired.
    SourceTimestampMismatch { expected: u64, found: u64 },
    /// A resolved benchmark level was non-positive (a return divides by the prior level,
    /// so a zero/negative level would divide by zero or invert the sign).
    NonPositiveSourceLevel { ts: u64, level_minor: i64 },
    /// A comparison ratio was non-finite (NaN/inf), which would corrupt a ranking. The
    /// named quantity failed the finite check and the comparison fails closed.
    NonFiniteComparison { quantity: &'static str },
    /// The underlying metric computation failed closed; carries the metric family's
    /// fail-closed reason (defense-in-depth re-validation of the resolved series).
    Metrics(MetricsError),
}

impl std::fmt::Display for BenchmarkError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::UnknownBenchmark { symbol } => {
                write!(f, "unknown or non-canonical benchmark symbol: {symbol:?}")
            }
            Self::EmptyEquityCurve => write!(f, "empty equity curve: nothing to compare"),
            Self::InvalidWindow { start, end } => {
                write!(f, "evaluation window [{start}, {end}] is inverted")
            }
            Self::EquityMarkOutsideWindow {
                ts,
                window_start,
                window_end,
            } => write!(
                f,
                "equity mark ts {ts} falls outside the run window [{window_start}, {window_end}]"
            ),
            Self::BaselineNotBeforeRun {
                baseline_ts,
                first_ts,
            } => write!(
                f,
                "benchmark baseline ts {baseline_ts} must be strictly before the first equity mark ts {first_ts} (the pre-trade prior close)"
            ),
            Self::SourceUnavailable { failure } => {
                write!(f, "benchmark source unavailable: {}", failure.as_str())
            }
            Self::SourceSymbolMismatch {
                requested,
                returned,
            } => write!(
                f,
                "benchmark source resolved {returned:?} but the selection is {requested:?}"
            ),
            Self::SourceLengthMismatch { expected, found } => write!(
                f,
                "benchmark source returned {found} levels, expected {expected} (baseline + one per mark)"
            ),
            Self::SourceTimestampMismatch { expected, found } => write!(
                f,
                "benchmark level ts {found} is misaligned with the equity mark ts {expected}"
            ),
            Self::NonPositiveSourceLevel { ts, level_minor } => write!(
                f,
                "benchmark level {level_minor} at ts {ts} is non-positive"
            ),
            Self::NonFiniteComparison { quantity } => {
                write!(f, "benchmark comparison produced a non-finite {quantity}")
            }
            Self::Metrics(error) => write!(f, "metric computation failed: {error:?}"),
        }
    }
}

impl std::error::Error for BenchmarkError {}

/// Compare a completed strategy run against a selected benchmark (SRS-BT-005).
///
/// Resolves `selection` to a [`Benchmark`] (SPY by default), asks `source` for that
/// benchmark's aligned level series, validates the resolved series at the trust
/// boundary, computes the SYS-16 metric family against it via
/// [`metrics::compute`](crate::metrics::compute), and packages a [`BenchmarkReport`]
/// whose [`BenchmarkComparison`] identifies the benchmark.
///
/// The comparison is **bound to the strategy run's evaluation window** (`window`, e.g.
/// `BacktestResult.range`): every equity mark must fall within it
/// ([`BenchmarkError::EquityMarkOutsideWindow`]), and the benchmark baseline is resolved
/// **as-of `window.start`** (the pre-trade instant), which must open strictly before the
/// first mark ([`BenchmarkError::WindowStartNotBeforeRun`]). This prevents a stale or
/// unrelated baseline from measuring the benchmark over a different period than the
/// strategy. (The full atomic run-snapshot identity -- a producer-stamped run id binding
/// equity curve, trade log, and benchmark window into one consistent snapshot -- is the
/// deferred accumulator's responsibility, the same boundary as SRS-BT-004; this window
/// binding is the in-scope coherence guard.)
///
/// `starting_equity_minor` is the pre-trade baseline value (the same required baseline
/// the metric family takes), measured as-of `window.start`. Deterministic and
/// fail-closed: an operational source failure ([`BenchmarkError::SourceUnavailable`]) or
/// any malformed resolution is rejected with a source-attributed [`BenchmarkError`]
/// before a metric is reported.
pub fn compare(
    starting_equity_minor: i64,
    window: DateRange,
    equity_curve: &[EquityPoint],
    trade_log: &[Fill],
    selection: &BenchmarkSelection,
    source: &dyn BenchmarkSource,
    config: &MetricsConfig,
) -> Result<BenchmarkReport, BenchmarkError> {
    let benchmark = selection.resolve();

    if equity_curve.is_empty() {
        return Err(BenchmarkError::EmptyEquityCurve);
    }
    if window.start > window.end {
        return Err(BenchmarkError::InvalidWindow {
            start: window.start,
            end: window.end,
        });
    }
    // Run coherence: every equity mark must fall within the run's evaluation window, so
    // the equity curve provably describes a run over this window (and the benchmark,
    // resolved over the same window, is measured over the strategy's period).
    for point in equity_curve {
        if !window.contains(point.ts) {
            return Err(BenchmarkError::EquityMarkOutsideWindow {
                ts: point.ts,
                window_start: window.start,
                window_end: window.end,
            });
        }
    }
    let first_ts = equity_curve[0].ts;

    let axis: Vec<u64> = equity_curve.iter().map(|point| point.ts).collect();
    // The source resolves the benchmark for the run window aligned to the axis. It can
    // only fail with a typed SourceFailure (an operational read failure); compare owns the
    // mapping to BenchmarkError::SourceUnavailable, so a source can never inject a
    // consumer-only error variant.
    let resolved = source
        .levels(benchmark.symbol(), window, &axis)
        .map_err(|failure| BenchmarkError::SourceUnavailable { failure })?;

    // Identity is bound to the RETURNED data: the symbol the source actually resolved
    // must equal the selected benchmark, validated AFTER the fetch. This rejects a buggy
    // cache/resolver that returns one benchmark's levels while the report identifies
    // another -- the check is on what was returned, not a decoupled pre-fetch declaration.
    if resolved.symbol != benchmark.symbol() {
        return Err(BenchmarkError::SourceSymbolMismatch {
            requested: benchmark.symbol().to_string(),
            returned: resolved.symbol,
        });
    }
    let levels = resolved.levels;

    // Trust-boundary re-validation of the resolved series, BEFORE compute, so a
    // malformed resolution surfaces as a source-attributed BenchmarkError rather than a
    // metric error. (compute re-validates the same invariants as defense-in-depth.)
    let expected = axis.len() + 1;
    if levels.len() != expected {
        return Err(BenchmarkError::SourceLengthMismatch {
            expected,
            found: levels.len(),
        });
    }
    // The baseline (levels[0]) is the pre-trade observation -- the benchmark's prior close,
    // strictly before the first equity mark -- so the benchmark's first-period return spans
    // the same [pre-first-bar, first_mark] interval as the strategy's first return and the
    // level series is strictly increasing in ts. The run window is INCLUSIVE: the first
    // mark may land exactly on window.start, in which case the prior-close baseline is
    // before window.start (valid). Verifying it is the IMMEDIATE prior close (not an
    // arbitrarily stale earlier observation) needs the data layer's bar grid and is the
    // deferred SRS-DATA-007 resolver's responsibility; the in-scope guard is that it
    // precedes the first mark.
    if levels[0].ts >= first_ts {
        return Err(BenchmarkError::BaselineNotBeforeRun {
            baseline_ts: levels[0].ts,
            first_ts,
        });
    }
    for (index, point) in levels.iter().enumerate() {
        if point.level_minor <= 0 {
            return Err(BenchmarkError::NonPositiveSourceLevel {
                ts: point.ts,
                level_minor: point.level_minor,
            });
        }
        // levels[0] is the pre-window baseline (its ordering vs the first mark is checked
        // above); levels[1..] must align 1:1 by timestamp with the equity marks.
        if index >= 1 {
            let expected_ts = axis[index - 1];
            if point.ts != expected_ts {
                return Err(BenchmarkError::SourceTimestampMismatch {
                    expected: expected_ts,
                    found: point.ts,
                });
            }
        }
    }

    let metrics = compute(
        starting_equity_minor,
        equity_curve,
        trade_log,
        &benchmark,
        Some(&levels),
        config,
    )
    .map_err(BenchmarkError::Metrics)?;

    // compute() succeeded, so the baseline and every level are validated strictly
    // positive and the curve is coherent; the total-return ratios below are therefore
    // safe to form. They are dimensionless f64 ratios (the metric domain).
    let strategy_total_return = finite_opt(
        "strategy_total_return",
        total_return(starting_equity_minor, equity_curve),
    )?;
    let benchmark_total_return =
        finite_opt("benchmark_total_return", benchmark_total_return(&levels))?;
    let excess_return = match (strategy_total_return, benchmark_total_return) {
        (Some(strategy), Some(bench)) => finite_opt("excess_return", Some(strategy - bench))?,
        _ => None,
    };

    let comparison = BenchmarkComparison {
        benchmark_symbol: benchmark.symbol().to_string(),
        is_default_benchmark: selection.is_default(),
        alpha: metrics.alpha,
        beta: metrics.beta,
        strategy_total_return,
        benchmark_total_return,
        excess_return,
    };

    Ok(BenchmarkReport {
        metrics,
        comparison,
    })
}

/// The strategy's total return over the run: `(final_equity - starting) / starting`.
/// `starting` is the pre-trade baseline (a strictly positive denominator once `compute`
/// has validated the curve). A terminal-zero final equity is a defined -100% total loss.
fn total_return(starting_equity_minor: i64, equity_curve: &[EquityPoint]) -> Option<f64> {
    let last = equity_curve.last()?;
    let start = starting_equity_minor as f64;
    Some((last.equity_minor as f64 - start) / start)
}

/// The benchmark's total return over the resolved series:
/// `(last_level - baseline_level) / baseline_level`. `None` when there are fewer than
/// two points (no return is defined).
fn benchmark_total_return(levels: &[BenchmarkPoint]) -> Option<f64> {
    if levels.len() < 2 {
        return None;
    }
    let baseline = levels[0].level_minor as f64;
    let last = levels[levels.len() - 1].level_minor as f64;
    Some((last - baseline) / baseline)
}

/// Verify an optional comparison ratio is finite, failing closed with the named
/// quantity rather than leaking a NaN/inf into a ranking or dashboard.
fn finite_opt(quantity: &'static str, value: Option<f64>) -> Result<Option<f64>, BenchmarkError> {
    match value {
        Some(value) if !value.is_finite() => Err(BenchmarkError::NonFiniteComparison { quantity }),
        other => Ok(other),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A fixture benchmark source: returns a pre-baked level series labeled with
    /// `resolved_symbol` (the symbol the payload claims, which a substitution test can set
    /// to differ from the requested benchmark), or -- when constructed with
    /// [`FixtureSource::failing`] -- a typed operational failure.
    struct FixtureSource {
        resolved_symbol: String,
        outcome: Result<Vec<BenchmarkPoint>, SourceFailure>,
    }

    impl FixtureSource {
        /// A well-formed source: the `baseline` level then one level per `marks` entry,
        /// timestamped to `baseline_ts` + `axis`.
        fn aligned(
            symbol: &str,
            baseline_ts: u64,
            axis: &[u64],
            baseline: i64,
            marks: &[i64],
        ) -> Self {
            assert_eq!(axis.len(), marks.len());
            let mut levels = vec![BenchmarkPoint {
                ts: baseline_ts,
                level_minor: baseline,
            }];
            levels.extend(
                axis.iter()
                    .zip(marks.iter())
                    .map(|(&ts, &level_minor)| BenchmarkPoint { ts, level_minor }),
            );
            Self {
                resolved_symbol: symbol.to_string(),
                outcome: Ok(levels),
            }
        }

        fn with_levels(symbol: &str, levels: Vec<BenchmarkPoint>) -> Self {
            Self {
                resolved_symbol: symbol.to_string(),
                outcome: Ok(levels),
            }
        }

        /// A source whose read fails operationally (the deferred data layer is down,
        /// timed out, has no data, or is stale).
        fn failing(symbol: &str, failure: SourceFailure) -> Self {
            Self {
                resolved_symbol: symbol.to_string(),
                outcome: Err(failure),
            }
        }
    }

    impl BenchmarkSource for FixtureSource {
        fn levels(
            &self,
            _symbol: &str,
            _window: DateRange,
            _axis: &[u64],
        ) -> Result<ResolvedBenchmark, SourceFailure> {
            let levels = self.outcome.clone()?;
            Ok(ResolvedBenchmark {
                symbol: self.resolved_symbol.clone(),
                levels,
            })
        }
    }

    fn point(ts: u64, equity_minor: i64) -> EquityPoint {
        EquityPoint { ts, equity_minor }
    }

    fn curve() -> Vec<EquityPoint> {
        vec![point(1, 1000), point(2, 1100), point(3, 1050)]
    }

    /// A run window that opens before the first mark (ts 1) and contains every mark.
    fn window() -> DateRange {
        DateRange::new(0, 100)
    }

    // ---- selection -------------------------------------------------------- //

    #[test]
    fn unselected_resolves_to_spy() {
        assert_eq!(BenchmarkSelection::unselected().resolve().symbol(), "SPY");
        assert_eq!(BenchmarkSelection::default().resolve().symbol(), "SPY");
        assert!(BenchmarkSelection::default().is_default());
    }

    #[test]
    fn explicit_selection_is_resolved_and_not_default() {
        let selection = BenchmarkSelection::from_symbol("QQQ").unwrap();
        assert_eq!(selection.resolve().symbol(), "QQQ");
        assert!(!selection.is_default());
    }

    #[test]
    fn from_symbol_rejects_malformed_symbol() {
        assert!(matches!(
            BenchmarkSelection::from_symbol("  ").unwrap_err(),
            BenchmarkError::UnknownBenchmark { .. }
        ));
        assert!(matches!(
            BenchmarkSelection::from_symbol("spy").unwrap_err(),
            BenchmarkError::UnknownBenchmark { .. }
        ));
    }

    // ---- happy path ------------------------------------------------------- //

    #[test]
    fn compare_default_benchmark_matches_direct_compute() {
        let curve = curve();
        let axis = [1u64, 2, 3];
        let source = FixtureSource::aligned("SPY", 0, &axis, 400, &[404, 440, 420]);
        let config = MetricsConfig::default();
        let report = compare(
            1000,
            window(),
            &curve,
            &[],
            &BenchmarkSelection::unselected(),
            &source,
            &config,
        )
        .unwrap();

        // The report identifies SPY as the default benchmark.
        assert_eq!(report.comparison.benchmark_symbol, "SPY");
        assert!(report.comparison.is_default_benchmark);
        assert_eq!(report.metrics.benchmark_symbol, "SPY");

        // alpha/beta on the comparison equal the metric family's, which equals a direct
        // compute against the same resolved series.
        let levels = source.outcome.clone().unwrap();
        let direct = compute(1000, &curve, &[], &Benchmark::spy(), Some(&levels), &config).unwrap();
        assert_eq!(report.metrics, direct);
        assert_eq!(report.comparison.alpha, direct.alpha);
        assert_eq!(report.comparison.beta, direct.beta);
    }

    #[test]
    fn total_and_excess_returns_are_computed() {
        let curve = curve(); // 1000 -> 1050 final, baseline 1000 => +5%
        let axis = [1u64, 2, 3];
        // benchmark 400 baseline -> 420 final => +5% as well, so excess == 0.
        let source = FixtureSource::aligned("SPY", 0, &axis, 400, &[404, 440, 420]);
        let report = compare(
            1000,
            window(),
            &curve,
            &[],
            &BenchmarkSelection::unselected(),
            &source,
            &MetricsConfig::default(),
        )
        .unwrap();
        let strategy = report.comparison.strategy_total_return.unwrap();
        let bench = report.comparison.benchmark_total_return.unwrap();
        let excess = report.comparison.excess_return.unwrap();
        assert!((strategy - 0.05).abs() < 1e-9);
        assert!((bench - 0.05).abs() < 1e-9);
        assert!(excess.abs() < 1e-9);
        // excess == strategy - benchmark by construction.
        assert!((excess - (strategy - bench)).abs() < 1e-12);
    }

    #[test]
    fn excess_return_reflects_outperformance() {
        // Strategy +5% (1000 -> 1050), benchmark +2.5% (400 -> 410) => excess +2.5%.
        let curve = curve();
        let axis = [1u64, 2, 3];
        let source = FixtureSource::aligned("SPY", 0, &axis, 400, &[402, 415, 410]);
        let report = compare(
            1000,
            window(),
            &curve,
            &[],
            &BenchmarkSelection::unselected(),
            &source,
            &MetricsConfig::default(),
        )
        .unwrap();
        assert!((report.comparison.excess_return.unwrap() - 0.025).abs() < 1e-9);
    }

    #[test]
    fn compare_is_deterministic() {
        let curve = curve();
        let axis = [1u64, 2, 3];
        let source = FixtureSource::aligned("SPY", 0, &axis, 400, &[404, 440, 420]);
        let config = MetricsConfig::default();
        let first = compare(
            1000,
            window(),
            &curve,
            &[],
            &BenchmarkSelection::unselected(),
            &source,
            &config,
        )
        .unwrap();
        let second = compare(
            1000,
            window(),
            &curve,
            &[],
            &BenchmarkSelection::unselected(),
            &source,
            &config,
        )
        .unwrap();
        assert_eq!(first, second);
    }

    // ---- run-window binding (SRS-BT-005 R1 coherence) --------------------- //

    #[test]
    fn empty_curve_is_rejected() {
        let source = FixtureSource::with_levels("SPY", vec![]);
        assert!(matches!(
            compare(
                1000,
                window(),
                &[],
                &[],
                &BenchmarkSelection::unselected(),
                &source,
                &MetricsConfig::default(),
            )
            .unwrap_err(),
            BenchmarkError::EmptyEquityCurve
        ));
    }

    #[test]
    fn inverted_window_is_rejected() {
        let curve = curve();
        let source = FixtureSource::aligned("SPY", 0, &[1u64, 2, 3], 400, &[404, 440, 420]);
        assert!(matches!(
            compare(
                1000,
                DateRange::new(100, 0),
                &curve,
                &[],
                &BenchmarkSelection::unselected(),
                &source,
                &MetricsConfig::default(),
            )
            .unwrap_err(),
            BenchmarkError::InvalidWindow { start: 100, end: 0 }
        ));
    }

    #[test]
    fn equity_mark_outside_window_is_rejected() {
        // The comparison is bound to the run window: a foreign/stale window whose bounds
        // do not contain the equity curve is rejected, so the benchmark cannot be measured
        // over a different period than the strategy (Codex R1).
        let curve = curve(); // marks at ts 1, 2, 3
        let source = FixtureSource::aligned("SPY", 0, &[1u64, 2, 3], 400, &[404, 440, 420]);
        // Window [0, 2] excludes the ts-3 mark.
        assert!(matches!(
            compare(
                1000,
                DateRange::new(0, 2),
                &curve,
                &[],
                &BenchmarkSelection::unselected(),
                &source,
                &MetricsConfig::default(),
            )
            .unwrap_err(),
            BenchmarkError::EquityMarkOutsideWindow {
                ts: 3,
                window_start: 0,
                window_end: 2
            }
        ));
    }

    #[test]
    fn baseline_not_before_run_is_rejected() {
        // The baseline must be strictly before the first mark (the pre-trade prior close).
        // A source returning a baseline AT the first mark (ts 1) leaves no pre-trade gap and
        // is rejected.
        let curve = curve(); // first mark at ts 1
        let source = FixtureSource::aligned("SPY", 1, &[1u64, 2, 3], 400, &[404, 440, 420]);
        assert!(matches!(
            compare(
                1000,
                DateRange::new(0, 100),
                &curve,
                &[],
                &BenchmarkSelection::unselected(),
                &source,
                &MetricsConfig::default(),
            )
            .unwrap_err(),
            BenchmarkError::BaselineNotBeforeRun {
                baseline_ts: 1,
                first_ts: 1
            }
        ));
    }

    #[test]
    fn inclusive_window_opening_on_first_mark_is_accepted() {
        // Codex R6 (locked): DateRange is inclusive, so a valid backtest can emit its first
        // equity mark exactly at window.start. The comparison accepts this -- the benchmark
        // baseline is the prior close (ts 0), which is before window.start (ts 1); the
        // baseline need not be at window.start.
        let curve = curve(); // marks at ts 1, 2, 3
        let source = FixtureSource::aligned("SPY", 0, &[1u64, 2, 3], 400, &[404, 440, 420]);
        // Window opens exactly on the first mark (ts 1); baseline (prior close) is at ts 0.
        let report = compare(
            1000,
            DateRange::new(1, 100),
            &curve,
            &[],
            &BenchmarkSelection::unselected(),
            &source,
            &MetricsConfig::default(),
        )
        .expect("an inclusive window opening on the first mark is valid");
        assert_eq!(report.comparison.benchmark_symbol, "SPY");
    }

    // ---- trust-boundary / fail-closed ------------------------------------ //

    #[test]
    fn source_unavailable_is_propagated() {
        // A real (deferred) resolver that times out / is unavailable / has no data /
        // is stale surfaces a typed SourceFailure, not a malformed series (Codex R2).
        let curve = curve();
        for failure in [
            SourceFailure::Timeout,
            SourceFailure::Unavailable,
            SourceFailure::NotFound,
            SourceFailure::StaleData,
        ] {
            let source = FixtureSource::failing("SPY", failure);
            let err = compare(
                1000,
                window(),
                &curve,
                &[],
                &BenchmarkSelection::unselected(),
                &source,
                &MetricsConfig::default(),
            )
            .unwrap_err();
            assert_eq!(err, BenchmarkError::SourceUnavailable { failure });
        }
    }

    #[test]
    fn source_symbol_mismatch_is_rejected() {
        let curve = curve();
        let axis = [1u64, 2, 3];
        // Selection resolves to SPY but the source returns a series LABELED QQQ; the
        // post-fetch identity check on the returned payload rejects the substitution.
        let source = FixtureSource::aligned("QQQ", 0, &axis, 400, &[404, 440, 420]);
        assert!(matches!(
            compare(
                1000,
                window(),
                &curve,
                &[],
                &BenchmarkSelection::unselected(),
                &source,
                &MetricsConfig::default(),
            )
            .unwrap_err(),
            BenchmarkError::SourceSymbolMismatch { .. }
        ));
    }

    #[test]
    fn source_length_mismatch_is_rejected() {
        let curve = curve();
        // Only two levels for a three-mark curve (expected four).
        let source = FixtureSource::with_levels(
            "SPY",
            vec![
                BenchmarkPoint {
                    ts: 0,
                    level_minor: 400,
                },
                BenchmarkPoint {
                    ts: 1,
                    level_minor: 404,
                },
            ],
        );
        assert!(matches!(
            compare(
                1000,
                window(),
                &curve,
                &[],
                &BenchmarkSelection::unselected(),
                &source,
                &MetricsConfig::default(),
            )
            .unwrap_err(),
            BenchmarkError::SourceLengthMismatch {
                expected: 4,
                found: 2
            }
        ));
    }

    #[test]
    fn source_timestamp_mismatch_is_rejected() {
        let curve = curve();
        // Right length, but the second mark's ts (2) is wrong (9).
        let source = FixtureSource::with_levels(
            "SPY",
            vec![
                BenchmarkPoint {
                    ts: 0,
                    level_minor: 400,
                },
                BenchmarkPoint {
                    ts: 1,
                    level_minor: 404,
                },
                BenchmarkPoint {
                    ts: 9,
                    level_minor: 440,
                },
                BenchmarkPoint {
                    ts: 3,
                    level_minor: 420,
                },
            ],
        );
        assert!(matches!(
            compare(
                1000,
                window(),
                &curve,
                &[],
                &BenchmarkSelection::unselected(),
                &source,
                &MetricsConfig::default(),
            )
            .unwrap_err(),
            BenchmarkError::SourceTimestampMismatch {
                expected: 2,
                found: 9
            }
        ));
    }

    #[test]
    fn non_positive_source_level_is_rejected() {
        let curve = curve();
        let source = FixtureSource::with_levels(
            "SPY",
            vec![
                BenchmarkPoint {
                    ts: 0,
                    level_minor: 400,
                },
                BenchmarkPoint {
                    ts: 1,
                    level_minor: 0,
                },
                BenchmarkPoint {
                    ts: 2,
                    level_minor: 440,
                },
                BenchmarkPoint {
                    ts: 3,
                    level_minor: 420,
                },
            ],
        );
        assert!(matches!(
            compare(
                1000,
                window(),
                &curve,
                &[],
                &BenchmarkSelection::unselected(),
                &source,
                &MetricsConfig::default(),
            )
            .unwrap_err(),
            BenchmarkError::NonPositiveSourceLevel {
                ts: 1,
                level_minor: 0
            }
        ));
    }

    #[test]
    fn metric_failure_is_wrapped() {
        // A non-positive (non-final) equity mark makes compute fail closed; the metric
        // error is wrapped as BenchmarkError::Metrics.
        let curve = vec![point(1, 1000), point(2, -5), point(3, 1050)];
        let axis = [1u64, 2, 3];
        let source = FixtureSource::aligned("SPY", 0, &axis, 400, &[404, 440, 420]);
        assert!(matches!(
            compare(
                1000,
                window(),
                &curve,
                &[],
                &BenchmarkSelection::unselected(),
                &source,
                &MetricsConfig::default(),
            )
            .unwrap_err(),
            BenchmarkError::Metrics(MetricsError::NonPositiveEquity { .. })
        ));
    }
}
