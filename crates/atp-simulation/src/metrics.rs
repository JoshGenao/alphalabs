//! Performance-metric family for backtests, paper strategies, and live reporting
//! (SRS-BT-004 / SyRS SYS-16, SYS-86; StRS SN-1.04 / SN-1.05 / SN-1.29).
//!
//! SYS-16 requires the platform to compute, for a completed run, the standard
//! metric family: Sharpe ratio, Sortino ratio, alpha, beta, maximum drawdown,
//! annualized return, annualized volatility, and win rate. SYS-86 requires the
//! internal simulation engine to compute the SAME family for paper strategies that
//! the backtest engine and the live dashboard compute, so backtest, paper, and live
//! performance are directly comparable. This module is that single shared family:
//! it consumes the primitives the simulation/backtest engine already produces -- a
//! mark-to-market [`EquityPoint`] curve and a [`Fill`] trade log (both in integer
//! minor units) -- and returns one [`PerformanceMetrics`] value. The backtest engine
//! and the (deferred) paper/live accumulators each supply their own equity curve and
//! trade log, so all three report identical math.
//!
//! ## Numeric boundary (the headline design decision)
//!
//! Money stays in integer minor units everywhere it enters this module
//! ([`EquityPoint::equity_minor`] is `i64`, [`BenchmarkPoint::level_minor`] is `i64`,
//! every [`Fill`] money field is `i64`), exactly as the ledger and persistence paths
//! keep it. The eight metrics, by contrast, are DIMENSIONLESS RATIOS (a Sharpe ratio
//! is not money), so they are `f64`. Unlike `paper_state`, this module therefore does
//! contain `f64` -- that is the metric domain, not a money-correctness leak. The
//! float work is made DETERMINISTIC (the SRS-BT-010 criterion this family must honor):
//! every reduction is a fixed left-to-right fold over the timestamp-ordered series,
//! there is no parallelism, no platform RNG, and no wall-clock read, so identical
//! inputs always produce bit-identical metrics.
//!
//! ## Undefined vs fabricated
//!
//! A metric that is mathematically undefined on the given input -- a Sharpe ratio when
//! the return series has zero dispersion, alpha/beta when no benchmark was supplied,
//! a win rate when no trade ever closed -- is reported as `None`, NEVER as a
//! fabricated `0.0` and never as a leaked `NaN`/`inf`. A computed metric is verified
//! finite before it is returned ([`MetricsError::NonFiniteComputation`]), so a
//! pathological input fails closed rather than emitting a poison value that would
//! silently corrupt a ranking or a dashboard.
//!
//! ## Benchmark (SPY default)
//!
//! Alpha and beta are defined only against a benchmark (SYS-16 computes them relative
//! to the SYS-17 benchmark). [`Benchmark`] defaults to `SPY` when the operator selects
//! none, and the chosen symbol is echoed on [`PerformanceMetrics::benchmark_symbol`]
//! so a report identifies its benchmark. This slice computes alpha/beta from a
//! caller-supplied benchmark level series aligned 1:1 with the strategy's equity
//! curve; the full SRS-BT-005 benchmark-selection surface (resolving SPY's actual
//! price history, the dashboard/report wiring) is its own feature and remains
//! `passes:false`.
//!
//! ## Deferred (why SRS-BT-004 stays `passes:false`)
//!
//! End-to-end SRS-BT-004 needs the live dashboard reporting path and the paper/live
//! equity-and-trade accumulators that feed this family at runtime (the SRS-SIM-004
//! snapshot reserves a metrics slot for exactly that, still empty until an
//! accumulator exists), plus the SRS-BT-005 benchmark-resolution surface. This slice
//! ships the deterministic, dependency-free computation; those owners flip the
//! end-to-end status, so `feature_list.json` keeps SRS-BT-004 `passes:false`.

use std::collections::HashMap;

use crate::backtest::{EquityPoint, Fill};
use crate::virtual_ledger::canonical_symbol;

/// The annualization factor used when none is configured: 252 trading days per year.
pub const DEFAULT_PERIODS_PER_YEAR: u32 = 252;

/// The benchmark symbol used when the operator selects none (SYS-17): SPY.
pub const DEFAULT_BENCHMARK_SYMBOL: &str = "SPY";

/// A user-selected performance benchmark, defaulting to SPY (SRS-BT-005 / SYS-17).
///
/// Only the selection identity lives here; the benchmark's actual price history is a
/// caller-supplied [`BenchmarkPoint`] series. [`Default`] resolves to SPY so a run
/// that names no benchmark still has one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Benchmark {
    symbol: String,
}

impl Benchmark {
    /// The default benchmark: SPY.
    pub fn spy() -> Self {
        Self {
            symbol: DEFAULT_BENCHMARK_SYMBOL.to_string(),
        }
    }

    /// A user-selected benchmark. Fails closed on an empty/whitespace symbol or a
    /// non-canonical (non-uppercase) symbol, mirroring the ledger's canonical-symbol
    /// discipline so a report cannot identify a malformed benchmark.
    pub fn new(symbol: impl Into<String>) -> Result<Self, MetricsError> {
        let symbol = symbol.into();
        if symbol.trim().is_empty() {
            return Err(MetricsError::EmptyBenchmarkSymbol);
        }
        if symbol != symbol.to_uppercase() {
            return Err(MetricsError::NonCanonicalBenchmarkSymbol { symbol });
        }
        Ok(Self { symbol })
    }

    /// The selected benchmark symbol.
    pub fn symbol(&self) -> &str {
        &self.symbol
    }
}

impl Default for Benchmark {
    fn default() -> Self {
        Self::spy()
    }
}

/// One mark of the benchmark's price level at a strategy-curve timestamp, in integer
/// minor units (e.g. SPY's close in cents). The series must align 1:1 by timestamp
/// with the strategy's equity curve.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BenchmarkPoint {
    pub ts: u64,
    pub level_minor: i64,
}

/// Per-run metric configuration.
///
/// `periods_per_year` is the annualization factor (e.g. 252 for daily bars);
/// `risk_free_rate_per_period` is the per-period risk-free rate used in the Sharpe
/// and Sortino excess return (default 0.0).
#[derive(Debug, Clone, PartialEq)]
pub struct MetricsConfig {
    pub periods_per_year: u32,
    pub risk_free_rate_per_period: f64,
}

impl MetricsConfig {
    /// Fail closed on a zero annualization factor (an unannualizable cadence) or a
    /// non-finite / impossible (<= -100% per period) risk-free rate.
    pub fn new(
        periods_per_year: u32,
        risk_free_rate_per_period: f64,
    ) -> Result<Self, MetricsError> {
        if periods_per_year == 0 {
            return Err(MetricsError::NonPositivePeriodsPerYear);
        }
        if !risk_free_rate_per_period.is_finite() || risk_free_rate_per_period <= -1.0 {
            return Err(MetricsError::InvalidRiskFreeRate {
                value: risk_free_rate_per_period,
            });
        }
        Ok(Self {
            periods_per_year,
            risk_free_rate_per_period,
        })
    }
}

impl Default for MetricsConfig {
    fn default() -> Self {
        Self {
            periods_per_year: DEFAULT_PERIODS_PER_YEAR,
            risk_free_rate_per_period: 0.0,
        }
    }
}

/// The eight SYS-16 / SYS-86 performance metrics plus the benchmark identity.
///
/// Every metric is `Option<f64>`: `Some` when defined on the input, `None` when the
/// input is degenerate (too few points, zero dispersion, no benchmark, no closed
/// trade). A `None` is the honest "undefined", never a fabricated zero.
#[derive(Debug, Clone, PartialEq)]
pub struct PerformanceMetrics {
    pub sharpe_ratio: Option<f64>,
    pub sortino_ratio: Option<f64>,
    pub alpha: Option<f64>,
    pub beta: Option<f64>,
    pub max_drawdown: Option<f64>,
    pub annualized_return: Option<f64>,
    pub annualized_volatility: Option<f64>,
    pub win_rate: Option<f64>,
    pub benchmark_symbol: String,
}

/// Fail-closed metric errors. Carries no broker/vendor identifiers.
#[derive(Debug, Clone, PartialEq)]
pub enum MetricsError {
    /// The equity curve was empty -- nothing to measure.
    EmptyEquityCurve,
    /// The equity (or benchmark) timestamps were not strictly increasing. A
    /// non-monotonic or duplicated timestamp would make the period returns -- and
    /// thus every metric -- ambiguous and order-dependent, so it is rejected rather
    /// than silently reordered.
    NonMonotonicTimestamps { ts: u64 },
    /// An equity mark was non-positive. A return is `(E_i - E_{i-1}) / E_{i-1}`, so a
    /// zero or negative prior equity would divide by zero or invert the sign; the
    /// curve must be strictly positive.
    NonPositiveEquity { ts: u64, equity_minor: i64 },
    /// The configured annualization factor was zero.
    NonPositivePeriodsPerYear,
    /// The configured risk-free rate was non-finite or <= -100% per period.
    InvalidRiskFreeRate { value: f64 },
    /// The selected benchmark symbol was empty / whitespace.
    EmptyBenchmarkSymbol,
    /// The selected benchmark symbol was not canonical (uppercase).
    NonCanonicalBenchmarkSymbol { symbol: String },
    /// The benchmark level series did not have the same length as the equity curve,
    /// so the two return series cannot be aligned period-for-period.
    BenchmarkLengthMismatch {
        equity_len: usize,
        benchmark_len: usize,
    },
    /// A benchmark mark's timestamp did not equal the equity mark it must align with.
    BenchmarkTimestampMismatch { equity_ts: u64, benchmark_ts: u64 },
    /// A benchmark level was non-positive (same divide-by-zero hazard as equity).
    NonPositiveBenchmarkLevel { ts: u64, level_minor: i64 },
    /// A trade-log fill carried a non-positive price, which would corrupt the realized
    /// P&L sign the win rate is built from.
    NonPositiveFillPrice { ts: u64, price_minor: i64 },
    /// A trade-log fill carried a negative transaction-cost component (commission,
    /// slippage, or spread impact). A negative cost would inflate net realized P&L and
    /// fabricate a win, so it is rejected (the same non-negative-cost invariant the
    /// virtual ledger enforces).
    NegativeFillCost { ts: u64, minor_units: i64 },
    /// A trade-log fill's timestamp fell outside the run's equity-curve window
    /// [first mark ts, last mark ts]. The trade log and the equity curve must describe
    /// the SAME run; a fill before the first or after the last equity mark indicates a
    /// stale or mismatched trade log (e.g. an asynchronously-snapshotted later log
    /// paired with an earlier equity curve), which would let the win rate and the
    /// equity-derived metrics disagree -- rejected as incoherent.
    TradeLogOutsideRun {
        ts: u64,
        run_start: u64,
        run_end: u64,
    },
    /// The trade log was not ordered by non-decreasing timestamp. Round-trip win-rate
    /// accounting is sequence-dependent, so a fill whose timestamp goes backwards -- i.e.
    /// a reordered log -- is rejected rather than silently producing an order-dependent
    /// win rate.
    NonMonotonicTradeLog { ts: u64 },
    /// Money math on the trade log exceeded `i128` range.
    Overflow,
    /// A computed metric came out non-finite (NaN/inf). The guards above should make
    /// this unreachable; it exists so a pathological input fails closed rather than
    /// returning a poison value.
    NonFiniteComputation { metric: &'static str },
}

/// Compute the SYS-16 / SYS-86 metric family for one completed run.
///
/// `starting_equity_minor` is the run's PRE-TRADE BASELINE -- the equity (cash) before
/// any fill, in integer minor units, and a REQUIRED input so the first period can never
/// be silently omitted. `equity_curve` is the post-bar mark-to-market equity (integer
/// minor units), strictly increasing in timestamp and strictly positive. Period returns
/// are `[(equity_curve[0] - starting) / starting, ...]`, so the first period's P&L --
/// including the entry costs of the opening fills -- is always captured; drawdown's peak
/// also starts at the baseline. (Producers such as the backtest engine, whose
/// `BacktestResult.equity_curve` begins at the first post-fill mark, pass their starting
/// cash here as `starting_equity_minor` -- the metric never has to guess a baseline.)
///
/// `trade_log` is the run's fills (the win rate's source). `benchmark` names the
/// comparison benchmark (defaulting to SPY). `benchmark_levels`, when supplied, is the
/// benchmark's price level series and is required for alpha/beta: its FIRST point is the
/// benchmark's pre-trade baseline (pairing with `starting_equity_minor`) and the
/// remaining points align 1:1 by timestamp with `equity_curve`, so it has
/// `equity_curve.len() + 1` entries and yields the same number of returns as the
/// strategy. `config` carries the annualization factor and risk-free rate.
///
/// Deterministic: all reductions fold left-to-right over the ordered series; there is
/// no parallelism, RNG, or clock read. Fails closed on a degenerate curve, a
/// misaligned benchmark, or a non-finite result.
pub fn compute(
    starting_equity_minor: i64,
    equity_curve: &[EquityPoint],
    trade_log: &[Fill],
    benchmark: &Benchmark,
    benchmark_levels: Option<&[BenchmarkPoint]>,
    config: &MetricsConfig,
) -> Result<PerformanceMetrics, MetricsError> {
    // Re-validate the config even if it bypassed `MetricsConfig::new` (the fields are
    // public), so `compute` is self-guarding.
    if config.periods_per_year == 0 {
        return Err(MetricsError::NonPositivePeriodsPerYear);
    }
    if !config.risk_free_rate_per_period.is_finite() || config.risk_free_rate_per_period <= -1.0 {
        return Err(MetricsError::InvalidRiskFreeRate {
            value: config.risk_free_rate_per_period,
        });
    }

    let returns = period_returns(starting_equity_minor, equity_curve)?;
    let ppy = f64::from(config.periods_per_year);
    let rf = config.risk_free_rate_per_period;

    // Coherence: the trade log and the equity curve must describe the SAME run, so every
    // fill timestamp must fall within the equity curve's observed window. This catches a
    // stale or mismatched trade log paired with a different run's equity curve (the
    // asynchronously-snapshotted accumulator hazard) before any metric is reported.
    // (period_returns has already validated the curve is non-empty and ts-ordered.)
    let run_start = equity_curve[0].ts;
    let run_end = equity_curve[equity_curve.len() - 1].ts;
    for fill in trade_log {
        if fill.ts < run_start || fill.ts > run_end {
            return Err(MetricsError::TradeLogOutsideRun {
                ts: fill.ts,
                run_start,
                run_end,
            });
        }
    }

    let annualized_return = annualized_return(starting_equity_minor, equity_curve, ppy)?;
    let annualized_volatility = annualized_volatility(&returns, ppy)?;
    let sharpe_ratio = sharpe_ratio(&returns, rf, ppy)?;
    let sortino_ratio = sortino_ratio(&returns, rf, ppy)?;
    let max_drawdown = max_drawdown(starting_equity_minor, equity_curve)?;
    let (beta, alpha) = match benchmark_levels {
        Some(levels) => beta_and_alpha(equity_curve, &returns, levels, rf, ppy)?,
        None => (None, None),
    };
    let win_rate = win_rate(trade_log)?;

    Ok(PerformanceMetrics {
        sharpe_ratio,
        sortino_ratio,
        alpha,
        beta,
        max_drawdown,
        annualized_return,
        annualized_volatility,
        win_rate,
        benchmark_symbol: benchmark.symbol().to_string(),
    })
}

/// Validate the equity series and return its `n` period returns as `f64`, where the
/// series is the pre-trade `starting_equity_minor` baseline followed by the `n`
/// post-bar marks. The FIRST return is `(equity_curve[0] - starting) / starting`, so the
/// first period's P&L -- including the opening fills' entry costs -- is captured.
fn period_returns(
    starting_equity_minor: i64,
    equity_curve: &[EquityPoint],
) -> Result<Vec<f64>, MetricsError> {
    if equity_curve.is_empty() {
        return Err(MetricsError::EmptyEquityCurve);
    }
    if starting_equity_minor <= 0 {
        return Err(MetricsError::NonPositiveEquity {
            ts: 0,
            equity_minor: starting_equity_minor,
        });
    }
    let mut returns = Vec::with_capacity(equity_curve.len());
    let mut prev_equity = starting_equity_minor as f64;
    let mut prev_ts: Option<u64> = None;
    let last_index = equity_curve.len() - 1;
    for (index, point) in equity_curve.iter().enumerate() {
        // A mark that is the DENOMINATOR of a later return (every mark except the last,
        // plus the baseline above) must be strictly positive. The FINAL mark is only a
        // numerator, so a terminal zero -- total loss / bankruptcy, a defined -100%
        // final return -- is allowed and does NOT abort the run; a negative equity is
        // never valid (it makes the geometric annualized return non-finite).
        if index < last_index {
            if point.equity_minor <= 0 {
                return Err(MetricsError::NonPositiveEquity {
                    ts: point.ts,
                    equity_minor: point.equity_minor,
                });
            }
        } else if point.equity_minor < 0 {
            return Err(MetricsError::NonPositiveEquity {
                ts: point.ts,
                equity_minor: point.equity_minor,
            });
        }
        if let Some(previous_ts) = prev_ts {
            if point.ts <= previous_ts {
                return Err(MetricsError::NonMonotonicTimestamps { ts: point.ts });
            }
        }
        let curr_equity = point.equity_minor as f64;
        returns.push((curr_equity - prev_equity) / prev_equity);
        prev_equity = curr_equity;
        prev_ts = Some(point.ts);
    }
    Ok(returns)
}

/// Arithmetic mean, folded left-to-right for determinism. `xs` must be non-empty.
fn mean(xs: &[f64]) -> f64 {
    let mut sum = 0.0;
    for &x in xs {
        sum += x;
    }
    sum / xs.len() as f64
}

/// Sample (ddof=1) variance. `None` when fewer than two observations.
fn sample_variance(xs: &[f64], xs_mean: f64) -> Option<f64> {
    if xs.len() < 2 {
        return None;
    }
    let mut sum_sq = 0.0;
    for &x in xs {
        let deviation = x - xs_mean;
        sum_sq += deviation * deviation;
    }
    Some(sum_sq / (xs.len() - 1) as f64)
}

/// Relative tolerance below which a dispersion (a standard or downside deviation) is
/// treated as zero. Floating-point aggregation of near-identical returns can leave a
/// tiny nonzero dispersion; an exact `== 0.0` denominator check would miss it and
/// divide by ~1e-17, producing an ENORMOUS but finite ratio that passes the
/// [`finite`] guard and would corrupt a Sharpe/Sortino/beta ranking. The tolerance is
/// scaled by the magnitude of the series (the largest absolute observation, floored at
/// 1.0) so it is meaningful across return scales while still catching FP noise.
const DISPERSION_EPSILON: f64 = 1e-12;

/// Whether `dispersion` is negligible relative to the scale of `series` -- i.e. small
/// enough to be floating-point noise rather than real variation, so a ratio dividing
/// by it would be spurious. Treated as a zero denominator (the metric is undefined).
fn negligible_dispersion(dispersion: f64, series: &[f64]) -> bool {
    let scale = series
        .iter()
        .fold(0.0_f64, |acc, value| acc.max(value.abs()))
        .max(1.0);
    dispersion <= DISPERSION_EPSILON * scale
}

/// Geometric annualized return from the pre-trade baseline to the last equity mark
/// (so the first period, including entry costs, is included).
fn annualized_return(
    starting_equity_minor: i64,
    equity_curve: &[EquityPoint],
    ppy: f64,
) -> Result<Option<f64>, MetricsError> {
    if equity_curve.is_empty() {
        return Ok(None);
    }
    let first = starting_equity_minor as f64;
    let last = equity_curve[equity_curve.len() - 1].equity_minor as f64;
    let periods = equity_curve.len() as f64;
    let total_return = last / first;
    let annualized = total_return.powf(ppy / periods) - 1.0;
    finite("annualized_return", annualized).map(Some)
}

/// Annualized volatility: the sample standard deviation of period returns scaled by
/// `sqrt(periods_per_year)`. `None` when fewer than two returns.
fn annualized_volatility(returns: &[f64], ppy: f64) -> Result<Option<f64>, MetricsError> {
    let returns_mean = match returns.is_empty() {
        true => return Ok(None),
        false => mean(returns),
    };
    match sample_variance(returns, returns_mean) {
        Some(variance) => {
            let annualized = variance.sqrt() * ppy.sqrt();
            finite("annualized_volatility", annualized).map(Some)
        }
        None => Ok(None),
    }
}

/// Sharpe ratio: annualized mean excess return over annualized volatility. `None`
/// when fewer than two returns or the return series has zero dispersion.
fn sharpe_ratio(returns: &[f64], rf: f64, ppy: f64) -> Result<Option<f64>, MetricsError> {
    if returns.is_empty() {
        return Ok(None);
    }
    let returns_mean = mean(returns);
    let stddev = match sample_variance(returns, returns_mean) {
        Some(variance) => variance.sqrt(),
        None => return Ok(None),
    };
    if negligible_dispersion(stddev, returns) {
        return Ok(None);
    }
    let sharpe = (returns_mean - rf) / stddev * ppy.sqrt();
    finite("sharpe_ratio", sharpe).map(Some)
}

/// Sortino ratio: annualized mean excess return over the (sample) downside deviation
/// of returns below the risk-free target. `None` when fewer than two returns or no
/// downside risk (a zero downside deviation -- undefined, not infinite).
fn sortino_ratio(returns: &[f64], rf: f64, ppy: f64) -> Result<Option<f64>, MetricsError> {
    if returns.len() < 2 {
        return Ok(None);
    }
    let returns_mean = mean(returns);
    // Target semideviation: square only the shortfalls below `rf`, but average over
    // the sample (ddof=1) for parity with the volatility denominator.
    let mut downside_sq = 0.0;
    for &r in returns {
        let shortfall = (r - rf).min(0.0);
        downside_sq += shortfall * shortfall;
    }
    let downside_dev = (downside_sq / (returns.len() - 1) as f64).sqrt();
    if negligible_dispersion(downside_dev, returns) {
        return Ok(None);
    }
    let sortino = (returns_mean - rf) / downside_dev * ppy.sqrt();
    finite("sortino_ratio", sortino).map(Some)
}

/// Maximum drawdown: the largest peak-to-trough fractional decline of the equity
/// series (the pre-trade baseline followed by the marks), as a non-negative magnitude
/// (0.0 for a monotonically non-decreasing series). Starting the running peak at the
/// baseline captures an initial drawdown below the starting equity.
fn max_drawdown(
    starting_equity_minor: i64,
    equity_curve: &[EquityPoint],
) -> Result<Option<f64>, MetricsError> {
    if equity_curve.is_empty() {
        return Ok(None);
    }
    let mut peak = starting_equity_minor as f64;
    let mut max_dd = 0.0_f64;
    for point in equity_curve {
        let equity = point.equity_minor as f64;
        if equity > peak {
            peak = equity;
        }
        let drawdown = (peak - equity) / peak;
        if drawdown > max_dd {
            max_dd = drawdown;
        }
    }
    finite("max_drawdown", max_dd).map(Some)
}

/// Beta (`cov(strategy, benchmark) / var(benchmark)`) and Jensen's alpha
/// (`(mean(strategy) - rf) - beta * (mean(benchmark) - rf)`, on EXCESS returns over the
/// per-period risk-free rate, annualized arithmetically). Both `None` when fewer than
/// two aligned returns or the benchmark has zero variance.
///
/// `benchmark_levels` carries the benchmark's pre-trade baseline as its FIRST point and
/// the per-mark levels (aligned 1:1 by timestamp with `equity_curve`) after it, so it
/// has `equity_curve.len() + 1` entries and produces the same number of returns as the
/// strategy.
fn beta_and_alpha(
    equity_curve: &[EquityPoint],
    returns: &[f64],
    benchmark_levels: &[BenchmarkPoint],
    rf: f64,
    ppy: f64,
) -> Result<(Option<f64>, Option<f64>), MetricsError> {
    if benchmark_levels.len() != equity_curve.len() + 1 {
        return Err(MetricsError::BenchmarkLengthMismatch {
            equity_len: equity_curve.len(),
            benchmark_len: benchmark_levels.len(),
        });
    }
    // Every benchmark level must be strictly positive and strictly increasing in
    // timestamp; the levels AFTER the baseline must align period-for-period with the
    // equity curve.
    let mut prev_ts: Option<u64> = None;
    for (index, benchmark_point) in benchmark_levels.iter().enumerate() {
        if benchmark_point.level_minor <= 0 {
            return Err(MetricsError::NonPositiveBenchmarkLevel {
                ts: benchmark_point.ts,
                level_minor: benchmark_point.level_minor,
            });
        }
        if let Some(previous_ts) = prev_ts {
            if benchmark_point.ts <= previous_ts {
                return Err(MetricsError::NonMonotonicTimestamps {
                    ts: benchmark_point.ts,
                });
            }
        }
        prev_ts = Some(benchmark_point.ts);
        if index >= 1 {
            let equity_point = &equity_curve[index - 1];
            if benchmark_point.ts != equity_point.ts {
                return Err(MetricsError::BenchmarkTimestampMismatch {
                    equity_ts: equity_point.ts,
                    benchmark_ts: benchmark_point.ts,
                });
            }
        }
    }
    if returns.len() < 2 {
        return Ok((None, None));
    }
    let mut benchmark_returns = Vec::with_capacity(benchmark_levels.len() - 1);
    for index in 1..benchmark_levels.len() {
        let prev = benchmark_levels[index - 1].level_minor as f64;
        let curr = benchmark_levels[index].level_minor as f64;
        benchmark_returns.push((curr - prev) / prev);
    }
    let strategy_mean = mean(returns);
    let benchmark_mean = mean(&benchmark_returns);
    let benchmark_variance = match sample_variance(&benchmark_returns, benchmark_mean) {
        Some(variance) => variance,
        None => return Ok((None, None)),
    };
    // Beta divides by the benchmark variance; treat a negligible (FP-noise-level)
    // benchmark dispersion as zero so a flat benchmark cannot produce a spurious,
    // enormous beta.
    if negligible_dispersion(benchmark_variance.sqrt(), &benchmark_returns) {
        return Ok((None, None));
    }
    // Sample covariance (ddof=1); the ddof cancels in beta = cov / var.
    let mut covariance_sum = 0.0;
    for (strategy_return, benchmark_return) in returns.iter().zip(benchmark_returns.iter()) {
        covariance_sum += (strategy_return - strategy_mean) * (benchmark_return - benchmark_mean);
    }
    let covariance = covariance_sum / (returns.len() - 1) as f64;
    let beta = covariance / benchmark_variance;
    // Jensen's alpha on EXCESS returns over the per-period risk-free rate:
    // alpha = (mean_strategy - rf) - beta * (mean_benchmark - rf). This is the CAPM
    // residual; omitting rf only happens to be correct when rf == 0 or beta == 1, so
    // it must be carried explicitly. Annualized arithmetically (alpha_per_period * ppy)
    // so a per-period alpha at or below -100% cannot blow up a geometric power.
    let alpha = ((strategy_mean - rf) - beta * (benchmark_mean - rf)) * ppy;
    let beta = finite("beta", beta)?;
    let alpha = finite("alpha", alpha)?;
    Ok((Some(beta), Some(alpha)))
}

/// Win rate: the fraction of completed round trips whose NET P&L was strictly positive.
///
/// A "trade" is a COMPLETE round trip -- the span over which a symbol's position goes
/// from flat, through any number of fills, back to flat -- not an individual fill. This
/// makes the win rate invariant to FILL FRAGMENTATION: a position opened in one fill and
/// closed in three volume-capped partial fills (SRS-SIM-002) counts as the SAME one
/// winning/losing round trip as the aggregated execution, so backtest (aggregated) and
/// paper/live (fragmented) win rates stay comparable (SYS-86). This invariance is exact
/// for same-direction partial fills and for a reversal expressed as a close-to-flat fill
/// then an open fill. A single fill that REVERSES through zero (long to short or back) is
/// the one case carrying an inherent cost-attribution choice -- there is no
/// fragmentation-invariant way to split one fill's integer cost across the two round
/// trips it spans -- so the reversing fill's FULL cost is deterministically attributed to
/// the closing round trip (equivalent to writing the reversal as a close-then-open with
/// the cost on the close), settling it and opening the next at zero cost.
///
/// A round trip's NET P&L is its realized cash flow minus all transaction costs incurred
/// over the round trip: `net = -sum(signed_qty_i * price_i) - sum(cost_i)`. Because both
/// sums are taken over the whole flat-to-flat span, the result is INVARIANT to the order
/// of fills within the round trip -- so it cannot silently depend on intra-bar fill
/// ordering. A win is NET of costs (gross profit overwhelmed by commission/slippage/
/// spread is a loss). An open round trip still flat-open at the end of the log is not
/// counted. `None` when no round trip completed.
///
/// Symbols are canonicalized with the ledger's [`crate::virtual_ledger::canonical_symbol`]
/// policy (trim + upper-case), so an open on `AAPL` and a close on `aapl` close the same
/// round trip here exactly as they do in the ledger (SYS-86 parity). The trade log must be
/// time-ordered (a backwards timestamp is rejected,
/// [`MetricsError::NonMonotonicTradeLog`]), but MULTIPLE fills may share a timestamp --
/// several orders, or volume-capped partial fills, can fill against one bar -- and are
/// applied in TRADE-LOG (slice) ORDER, which is the producer's authoritative execution
/// sequence. The result is therefore deterministic for a given trade log; the metric
/// trusts that the producer emitted fills in execution order rather than rejecting
/// legitimate same-bar fills.
fn win_rate(trade_log: &[Fill]) -> Result<Option<f64>, MetricsError> {
    // Per canonical symbol: (signed quantity, accumulated realized cash flow, accumulated
    // transaction cost) of the CURRENTLY OPEN round trip, all in integer minor units.
    let mut round_trips: HashMap<String, (i64, i128, i128)> = HashMap::new();
    let mut completed: u64 = 0;
    let mut wins: u64 = 0;
    // The trade log must be a time-ordered event sequence: a backwards global timestamp
    // is rejected so the log is a valid event stream. Equal timestamps are permitted and
    // applied in slice order.
    let mut last_ts: Option<u64> = None;
    for fill in trade_log {
        if let Some(previous_ts) = last_ts {
            if fill.ts < previous_ts {
                return Err(MetricsError::NonMonotonicTradeLog { ts: fill.ts });
            }
        }
        last_ts = Some(fill.ts);
        if fill.quantity == 0 {
            continue;
        }
        if fill.price_minor <= 0 {
            return Err(MetricsError::NonPositiveFillPrice {
                ts: fill.ts,
                price_minor: fill.price_minor,
            });
        }
        // Total this fill's transaction cost, rejecting a negative component (the same
        // non-negative-cost invariant the ledger enforces) -- a negative cost would
        // fabricate a win.
        let mut fill_cost: i128 = 0;
        for component in [
            fill.commission_minor,
            fill.slippage_minor,
            fill.spread_impact_minor,
        ] {
            if component < 0 {
                return Err(MetricsError::NegativeFillCost {
                    ts: fill.ts,
                    minor_units: component,
                });
            }
            fill_cost += i128::from(component);
        }
        let symbol = canonical_symbol(&fill.symbol);
        // Fills sharing a timestamp (several orders or partial fills against one bar) are
        // applied in trade-log (slice) order -- the producer's execution sequence.
        let (qty, cash_flow, cost) = *round_trips.entry(symbol.clone()).or_insert((0, 0, 0));
        let q = fill.quantity;
        let px = i128::from(fill.price_minor);
        let crosses_zero =
            qty != 0 && (q > 0) != (qty > 0) && i128::from(q).abs() > i128::from(qty).abs();
        if crosses_zero {
            // Flip through zero: split the fill into the portion that closes the held
            // position (bringing quantity to exactly 0) and the portion that reopens the
            // opposite side. The reversing fill's FULL cost is attributed to the CLOSING
            // round trip (the trade it completes); the reopened round trip starts with
            // zero cost and accrues its own costs from its later fills. A proportional
            // integer split would FLOOR the closing leg's cost to zero on small flips
            // (e.g. close 1 of 3 with cost 1 -> floor(1/3) = 0), spuriously turning a
            // break-even close into a win; attributing the whole cost to the close is
            // exact, deterministic, and matches a reversal expressed as a close-to-flat
            // fill (cost on the close) then a zero-cost open. (Fragmentation invariance
            // is exact for same-direction partial fills and for reversals written as
            // close-then-open; a single fill that reverses through zero carries an
            // inherent cost-attribution choice, resolved here as cost-on-close.)
            let close_qty = qty.checked_neg().ok_or(MetricsError::Overflow)?;
            // Settle the closed round trip: realized cash flow is -(signed_qty * price)
            // summed over its fills; net subtracts the round trip's total cost plus this
            // reversing fill's full cost.
            let close_cash_flow = cash_flow
                .checked_add(closing_cash_flow(close_qty, px)?)
                .ok_or(MetricsError::Overflow)?;
            let net = close_cash_flow
                .checked_sub(cost.checked_add(fill_cost).ok_or(MetricsError::Overflow)?)
                .ok_or(MetricsError::Overflow)?;
            completed += 1;
            if net > 0 {
                wins += 1;
            }
            // Open the next round trip with the remainder, at zero cost so far.
            let open_qty = q.checked_add(qty).ok_or(MetricsError::Overflow)?;
            let open_cash_flow = closing_cash_flow(open_qty, px)?;
            round_trips.insert(symbol, (open_qty, open_cash_flow, 0));
        } else {
            // Open, add, or reduce within the current round trip (or close it exactly).
            let new_qty = qty.checked_add(q).ok_or(MetricsError::Overflow)?;
            let new_cash_flow = cash_flow
                .checked_add(closing_cash_flow(q, px)?)
                .ok_or(MetricsError::Overflow)?;
            let new_cost = cost.checked_add(fill_cost).ok_or(MetricsError::Overflow)?;
            if new_qty == 0 {
                // Round trip returned to flat: settle it.
                let net = new_cash_flow
                    .checked_sub(new_cost)
                    .ok_or(MetricsError::Overflow)?;
                completed += 1;
                if net > 0 {
                    wins += 1;
                }
                round_trips.insert(symbol, (0, 0, 0));
            } else {
                round_trips.insert(symbol, (new_qty, new_cash_flow, new_cost));
            }
        }
    }
    if completed == 0 {
        return Ok(None);
    }
    Ok(Some(wins as f64 / completed as f64))
}

/// The realized cash flow of a fill of signed `quantity` at `price`: `-(quantity *
/// price)` in minor units (a buy spends cash, a sell receives it). Summed over a
/// complete flat-to-flat round trip, this is the round trip's gross realized P&L,
/// independent of the order of its fills.
fn closing_cash_flow(quantity: i64, price: i128) -> Result<i128, MetricsError> {
    i128::from(quantity)
        .checked_mul(price)
        .ok_or(MetricsError::Overflow)?
        .checked_neg()
        .ok_or(MetricsError::Overflow)
}

/// Guard a computed metric: a non-finite result (NaN/inf) fails closed rather than
/// leaking a poison value into a ranking or dashboard.
fn finite(metric: &'static str, value: f64) -> Result<f64, MetricsError> {
    if value.is_finite() {
        Ok(value)
    } else {
        Err(MetricsError::NonFiniteComputation { metric })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn equity(points: &[(u64, i64)]) -> Vec<EquityPoint> {
        points
            .iter()
            .map(|&(ts, equity_minor)| EquityPoint { ts, equity_minor })
            .collect()
    }

    fn bench(points: &[(u64, i64)]) -> Vec<BenchmarkPoint> {
        points
            .iter()
            .map(|&(ts, level_minor)| BenchmarkPoint { ts, level_minor })
            .collect()
    }

    fn fill(ts: u64, symbol: &str, quantity: i64, price_minor: i64) -> Fill {
        Fill {
            ts,
            symbol: symbol.to_string(),
            quantity,
            price_minor,
            commission_minor: 0,
            slippage_minor: 0,
            spread_impact_minor: 0,
        }
    }

    fn costed_fill(
        ts: u64,
        symbol: &str,
        quantity: i64,
        price_minor: i64,
        commission_minor: i64,
    ) -> Fill {
        Fill {
            ts,
            symbol: symbol.to_string(),
            quantity,
            price_minor,
            commission_minor,
            slippage_minor: 0,
            spread_impact_minor: 0,
        }
    }

    fn approx(a: f64, b: f64) {
        assert!((a - b).abs() < 1e-9, "expected {b}, got {a}");
    }

    #[test]
    fn benchmark_defaults_to_spy() {
        assert_eq!(Benchmark::default().symbol(), "SPY");
        assert_eq!(Benchmark::spy().symbol(), "SPY");
        assert_eq!(DEFAULT_BENCHMARK_SYMBOL, "SPY");
    }

    #[test]
    fn benchmark_new_validates_symbol() {
        assert_eq!(Benchmark::new("QQQ").unwrap().symbol(), "QQQ");
        assert_eq!(
            Benchmark::new("  ").unwrap_err(),
            MetricsError::EmptyBenchmarkSymbol
        );
        assert!(matches!(
            Benchmark::new("spy").unwrap_err(),
            MetricsError::NonCanonicalBenchmarkSymbol { .. }
        ));
    }

    #[test]
    fn config_defaults_and_validation() {
        let default = MetricsConfig::default();
        assert_eq!(default.periods_per_year, 252);
        assert_eq!(default.risk_free_rate_per_period, 0.0);
        assert_eq!(
            MetricsConfig::new(0, 0.0).unwrap_err(),
            MetricsError::NonPositivePeriodsPerYear
        );
        assert!(matches!(
            MetricsConfig::new(252, f64::NAN).unwrap_err(),
            MetricsError::InvalidRiskFreeRate { .. }
        ));
        assert!(matches!(
            MetricsConfig::new(252, -1.0).unwrap_err(),
            MetricsError::InvalidRiskFreeRate { .. }
        ));
    }

    #[test]
    fn period_returns_are_exact_ratios() {
        // Baseline 100 then marks 100, 110, 99: the first return is the baseline period
        // (0), then +10%, then -10%.
        let returns = period_returns(100, &equity(&[(1, 100), (2, 110), (3, 99)])).unwrap();
        approx(returns[0], 0.0);
        approx(returns[1], 0.1);
        approx(returns[2], -0.1);
    }

    #[test]
    fn period_returns_capture_the_first_period_from_the_baseline() {
        // Baseline 1000, first mark 990 (a first-period loss from entry costs): the
        // first return is -1%, which a curve starting at the post-fill mark would omit.
        let returns = period_returns(1000, &equity(&[(1, 990), (2, 1010)])).unwrap();
        approx(returns[0], -0.01);
    }

    #[test]
    fn period_returns_reject_non_positive_equity() {
        // An INTERMEDIATE zero is the denominator of the next return -> rejected (a
        // terminal zero is allowed; that case is covered separately).
        let err = period_returns(100, &equity(&[(1, 100), (2, 0), (3, 50)])).unwrap_err();
        assert!(matches!(err, MetricsError::NonPositiveEquity { .. }));
        // A negative terminal mark makes the geometric return non-finite -> rejected.
        let err = period_returns(100, &equity(&[(1, -5)])).unwrap_err();
        assert!(matches!(err, MetricsError::NonPositiveEquity { .. }));
        // The baseline itself must be strictly positive.
        let err = period_returns(0, &equity(&[(1, 100)])).unwrap_err();
        assert!(matches!(err, MetricsError::NonPositiveEquity { .. }));
    }

    #[test]
    fn period_returns_reject_non_monotonic_timestamps() {
        let err = period_returns(100, &equity(&[(2, 100), (2, 110)])).unwrap_err();
        assert_eq!(err, MetricsError::NonMonotonicTimestamps { ts: 2 });
        let err = period_returns(100, &equity(&[(5, 100), (3, 110)])).unwrap_err();
        assert_eq!(err, MetricsError::NonMonotonicTimestamps { ts: 3 });
    }

    #[test]
    fn empty_curve_is_rejected() {
        assert_eq!(
            period_returns(100, &[]).unwrap_err(),
            MetricsError::EmptyEquityCurve
        );
    }

    #[test]
    fn annualized_return_is_geometric() {
        // Baseline 100 then +10% then +10%: total 1.21 over 2 periods, ppy=2 -> annualize
        // by ^(2/2) -> 0.21.
        let curve = equity(&[(1, 110), (2, 121)]);
        let value = annualized_return(100, &curve, 2.0).unwrap().unwrap();
        approx(value, 0.21);
    }

    #[test]
    fn annualized_return_defined_for_single_mark_from_baseline() {
        // One mark still has one period (baseline -> mark), so it is defined.
        let value = annualized_return(100, &equity(&[(1, 110)]), 1.0)
            .unwrap()
            .unwrap();
        approx(value, 0.1);
    }

    #[test]
    fn annualized_return_none_for_empty_curve() {
        assert_eq!(annualized_return(100, &[], 252.0).unwrap(), None);
    }

    #[test]
    fn max_drawdown_is_peak_to_trough() {
        // Baseline 100, marks 120, 90, 110 -> peak 120, trough 90 -> dd = 30/120 = 0.25.
        let curve = equity(&[(1, 120), (2, 90), (3, 110)]);
        approx(max_drawdown(100, &curve).unwrap().unwrap(), 0.25);
    }

    #[test]
    fn max_drawdown_captures_initial_drop_below_baseline() {
        // The first mark drops below the starting equity; the peak starts at the
        // baseline so the initial drawdown is captured: (120 - 90) / 120 = 0.25.
        approx(
            max_drawdown(120, &equity(&[(1, 90)])).unwrap().unwrap(),
            0.25,
        );
    }

    #[test]
    fn max_drawdown_zero_for_monotonic_curve() {
        let curve = equity(&[(1, 110), (2, 120)]);
        approx(max_drawdown(100, &curve).unwrap().unwrap(), 0.0);
    }

    #[test]
    fn volatility_and_sharpe_undefined_for_one_return() {
        // One mark -> one return (baseline -> mark) -> sample stats undefined.
        let returns = period_returns(100, &equity(&[(1, 110)])).unwrap();
        assert_eq!(returns.len(), 1);
        assert_eq!(annualized_volatility(&returns, 252.0).unwrap(), None);
        assert_eq!(sharpe_ratio(&returns, 0.0, 252.0).unwrap(), None);
    }

    #[test]
    fn sharpe_undefined_for_zero_dispersion() {
        // Baseline 100 then +10% then +10%: constant returns -> zero stddev -> Sharpe
        // undefined (not +inf).
        let returns = period_returns(100, &equity(&[(1, 110), (2, 121)])).unwrap();
        approx(returns[0], 0.1);
        approx(returns[1], 0.1);
        assert_eq!(sharpe_ratio(&returns, 0.0, 252.0).unwrap(), None);
        assert_eq!(sortino_ratio(&returns, 0.0, 252.0).unwrap(), None);
    }

    #[test]
    fn sharpe_and_sortino_undefined_for_negligible_dispersion() {
        // Near-identical returns differing only by floating-point noise (~1e-15) leave
        // a tiny nonzero stddev. An exact == 0.0 check would miss it and divide by
        // ~1e-15, producing an enormous but finite (and meaningless) Sharpe; the
        // scale-aware tolerance treats it as undefined instead.
        let returns = vec![0.05, 0.05 + 1e-15, 0.05 - 1e-15];
        let stddev = sample_variance(&returns, mean(&returns)).unwrap().sqrt();
        assert!(
            stddev > 0.0,
            "the FP dispersion must be nonzero for a real test"
        );
        assert!(stddev < 1e-12, "the dispersion must be below the tolerance");
        assert_eq!(sharpe_ratio(&returns, 0.0, 252.0).unwrap(), None);
        assert_eq!(sortino_ratio(&returns, 0.0, 252.0).unwrap(), None);
    }

    #[test]
    fn beta_undefined_for_negligible_benchmark_dispersion() {
        // A benchmark whose returns are constant has zero variance; dividing by it would
        // fabricate an enormous beta, so beta/alpha are None. Benchmark levels carry the
        // baseline first (n + 1 = 4 levels for a 3-mark curve).
        let curve = equity(&[(1, 100), (2, 105), (3, 110)]);
        let returns = period_returns(100, &curve).unwrap();
        let levels = bench(&[
            (0, 1_000_000),
            (1, 1_010_000),
            (2, 1_020_100),
            (3, 1_030_301),
        ]);
        let (beta, alpha) = beta_and_alpha(&curve, &returns, &levels, 0.0, 1.0).unwrap();
        assert_eq!(beta, None);
        assert_eq!(alpha, None);
    }

    #[test]
    fn negligible_dispersion_helper_is_scale_aware() {
        // FP-noise dispersion is negligible; a real dispersion at the return scale is
        // not, regardless of the magnitude of the returns.
        assert!(negligible_dispersion(1e-15, &[0.05, 0.05]));
        assert!(!negligible_dispersion(0.005, &[0.05, -0.05]));
        // Large-magnitude returns: the tolerance scales up but real dispersion stays.
        assert!(!negligible_dispersion(0.5, &[10.0, -10.0]));
    }

    #[test]
    fn sharpe_matches_hand_computation() {
        // Returns +0.10, -0.10: mean 0, sample stddev sqrt(0.02), ppy=1.
        let returns = vec![0.10, -0.10];
        let mean_r = mean(&returns);
        approx(mean_r, 0.0);
        let stddev = sample_variance(&returns, mean_r).unwrap().sqrt();
        approx(stddev, 0.02_f64.sqrt());
        let sharpe = sharpe_ratio(&returns, 0.0, 1.0).unwrap().unwrap();
        approx(sharpe, 0.0);
    }

    #[test]
    fn sortino_only_penalizes_downside() {
        // Returns +0.10, -0.10: downside shortfall only on -0.10.
        let returns = vec![0.10, -0.10];
        let downside_dev = (0.01_f64 / 1.0).sqrt();
        let expected = (mean(&returns) - 0.0) / downside_dev * 1.0_f64.sqrt();
        approx(
            sortino_ratio(&returns, 0.0, 1.0).unwrap().unwrap(),
            expected,
        );
    }

    #[test]
    fn beta_one_when_strategy_tracks_benchmark() {
        // Strategy and benchmark have identical returns [0, 0.1, -0.1] (incl. the
        // baseline period) -> beta 1, alpha 0 (with a zero risk-free rate). Benchmark
        // levels carry the baseline first (n + 1 = 4 levels).
        let curve = equity(&[(1, 100), (2, 110), (3, 99)]);
        let returns = period_returns(100, &curve).unwrap();
        let levels = bench(&[(0, 1000), (1, 1000), (2, 1100), (3, 990)]);
        let (beta, alpha) = beta_and_alpha(&curve, &returns, &levels, 0.0, 1.0).unwrap();
        approx(beta.unwrap(), 1.0);
        approx(alpha.unwrap(), 0.0);
    }

    #[test]
    fn alpha_uses_excess_returns_with_nonzero_risk_free() {
        // Strategy returns are exactly 2x the benchmark returns -> beta = 2. With a
        // non-zero risk-free rate and beta != 1, Jensen's alpha MUST use excess
        // returns: alpha = (mean_r - rf) - beta*(mean_b - rf). Here mean_r = 0.05,
        // mean_b = 0.025, rf = 0.01, ppy = 1 -> alpha = 0.04 - 2*0.015 = 0.01. The
        // rf-omitting formula would give mean_r - beta*mean_b = 0.0, so this test
        // pins the correctness of the excess-return form. Returns [0.20, -0.10] for the
        // strategy (baseline 100) and [0.10, -0.05] for the benchmark (baseline 1000).
        let curve = equity(&[(1, 120), (2, 108)]);
        let returns = period_returns(100, &curve).unwrap();
        let levels = bench(&[(0, 1000), (1, 1100), (2, 1045)]);
        let (beta, alpha) = beta_and_alpha(&curve, &returns, &levels, 0.01, 1.0).unwrap();
        approx(beta.unwrap(), 2.0);
        approx(alpha.unwrap(), 0.01);
    }

    #[test]
    fn beta_and_alpha_reject_misaligned_benchmark() {
        let curve = equity(&[(1, 100), (2, 110), (3, 99)]);
        let returns = period_returns(100, &curve).unwrap();
        // Wrong length (must be curve.len() + 1 = 4).
        let short = bench(&[(0, 1000), (1, 1000), (2, 1100)]);
        assert!(matches!(
            beta_and_alpha(&curve, &returns, &short, 0.0, 1.0).unwrap_err(),
            MetricsError::BenchmarkLengthMismatch { .. }
        ));
        // Misaligned timestamp (level after the baseline does not match the curve).
        let skewed = bench(&[(0, 1000), (1, 1000), (9, 1100), (3, 990)]);
        assert!(matches!(
            beta_and_alpha(&curve, &returns, &skewed, 0.0, 1.0).unwrap_err(),
            MetricsError::BenchmarkTimestampMismatch { .. }
        ));
        // Non-positive level.
        let bad_level = bench(&[(0, 1000), (1, 1000), (2, 0), (3, 990)]);
        assert!(matches!(
            beta_and_alpha(&curve, &returns, &bad_level, 0.0, 1.0).unwrap_err(),
            MetricsError::NonPositiveBenchmarkLevel { .. }
        ));
    }

    #[test]
    fn win_rate_counts_profitable_closes() {
        // Buy 10@100, sell 10@120 (win), buy 10@100, sell 10@90 (loss) -> 1/2.
        let log = vec![
            fill(1, "AAPL", 10, 100),
            fill(2, "AAPL", -10, 120),
            fill(3, "AAPL", 10, 100),
            fill(4, "AAPL", -10, 90),
        ];
        approx(win_rate(&log).unwrap().unwrap(), 0.5);
    }

    #[test]
    fn win_rate_none_without_closed_trades() {
        // Only opens, never closes.
        let log = vec![fill(1, "AAPL", 10, 100), fill(2, "AAPL", 5, 110)];
        assert_eq!(win_rate(&log).unwrap(), None);
    }

    #[test]
    fn win_rate_handles_flip_through_zero() {
        // Long 10@100, then sell 20@120: closes the long (realizes +200, a win) and
        // opens a short 10. One closed trade, a win -> 1.0.
        let log = vec![fill(1, "AAPL", 10, 100), fill(2, "AAPL", -20, 120)];
        approx(win_rate(&log).unwrap().unwrap(), 1.0);
    }

    #[test]
    fn win_rate_flip_attributes_reversing_cost_to_the_close() {
        // A single-fill reversal: long 1@100 (cost 0), then sell 3@101 with cost 1.
        // The reversing fill's full cost (1) is attributed to the CLOSING round trip, so
        // its net is (-100 + 101) - 1 = 0 -- a break-even close, NOT a win (a proportional
        // integer floor would have charged 0 and recorded a spurious win).
        let aggregated = vec![
            costed_fill(1, "AAPL", 1, 100, 0),
            costed_fill(2, "AAPL", -3, 101, 1),
        ];
        assert_eq!(win_rate(&aggregated).unwrap(), Some(0.0));
        // Writing the SAME reversal as a close-to-flat fill (cost on the close) then a
        // zero-cost open gives the identical result -- the flip attribution matches the
        // close-then-open form.
        let close_then_open = vec![
            costed_fill(1, "AAPL", 1, 100, 0),
            costed_fill(2, "AAPL", -1, 101, 1),
            costed_fill(3, "AAPL", -2, 101, 0),
        ];
        assert_eq!(
            win_rate(&close_then_open).unwrap(),
            win_rate(&aggregated).unwrap()
        );
    }

    #[test]
    fn win_rate_isolates_symbols() {
        // A win on AAPL, a loss on MSFT -> 1/2.
        let log = vec![
            fill(1, "AAPL", 10, 100),
            fill(2, "MSFT", 10, 100),
            fill(3, "AAPL", -10, 120),
            fill(4, "MSFT", -10, 80),
        ];
        approx(win_rate(&log).unwrap().unwrap(), 0.5);
    }

    #[test]
    fn win_rate_rejects_non_positive_price() {
        let log = vec![fill(1, "AAPL", 10, 0)];
        assert!(matches!(
            win_rate(&log).unwrap_err(),
            MetricsError::NonPositiveFillPrice { .. }
        ));
    }

    #[test]
    fn win_rate_rejects_out_of_order_fills() {
        // A fill whose timestamp goes backwards is not a valid time-ordered event
        // stream, so it is rejected rather than silently reconstructed.
        let log = vec![
            fill(5, "AAPL", 10, 100),
            fill(2, "AAPL", -10, 120), // ts 2 < ts 5: out of order
        ];
        assert_eq!(
            win_rate(&log).unwrap_err(),
            MetricsError::NonMonotonicTradeLog { ts: 2 }
        );
    }

    #[test]
    fn win_rate_applies_same_symbol_same_timestamp_in_slice_order() {
        // Several orders / partial fills can fill the same symbol against one bar (same
        // timestamp). They are applied in trade-log order and form one complete round
        // trip (open 10, close 10 -> a win), not rejected as ambiguous.
        let log = vec![
            fill(1, "AAPL", 10, 100),
            fill(1, "AAPL", -10, 120), // same symbol, same ts as the open
        ];
        approx(win_rate(&log).unwrap().unwrap(), 1.0);
    }

    #[test]
    fn win_rate_is_invariant_to_fill_fragmentation() {
        // A round trip closed in one fill vs three volume-capped partial fills (with the
        // same total cost) is the SAME one winning round trip, so backtest (aggregated)
        // and paper/live (fragmented) win rates stay comparable (SYS-86).
        let aggregated = vec![
            costed_fill(1, "AAPL", 10, 100, 6),
            costed_fill(2, "AAPL", -10, 120, 6),
        ];
        let fragmented = vec![
            costed_fill(1, "AAPL", 10, 100, 6),
            costed_fill(2, "AAPL", -3, 120, 2),
            costed_fill(3, "AAPL", -3, 120, 2),
            costed_fill(4, "AAPL", -4, 120, 2),
        ];
        assert_eq!(win_rate(&aggregated).unwrap(), Some(1.0));
        assert_eq!(
            win_rate(&aggregated).unwrap(),
            win_rate(&fragmented).unwrap()
        );
    }

    #[test]
    fn win_rate_allows_same_timestamp_across_symbols() {
        // Two different symbols can fill at the same bar timestamp; equal timestamps
        // are permitted and the per-symbol accounting stays correct (AAPL win, MSFT
        // loss -> 0.5). Determinism is unaffected because the symbols are independent.
        let log = vec![
            fill(1, "AAPL", 10, 100),
            fill(1, "MSFT", 10, 100),
            fill(2, "AAPL", -10, 120),
            fill(2, "MSFT", -10, 80),
        ];
        approx(win_rate(&log).unwrap().unwrap(), 0.5);
    }

    #[test]
    fn win_rate_is_net_of_transaction_costs() {
        // Buy 10@100 (commission 5), sell 10@101 (commission 10): gross realized is
        // +10 (a profit), but net of the 5 entry + 10 exit commission it is -5 -- a
        // LOSS. A gross-but-not-net win must NOT count as a win.
        let log = vec![
            costed_fill(1, "AAPL", 10, 100, 5),
            costed_fill(2, "AAPL", -10, 101, 10),
        ];
        approx(win_rate(&log).unwrap().unwrap(), 0.0);
        // Control: the same round trip with negligible cost is gross == net positive.
        let cheap = vec![
            costed_fill(1, "AAPL", 10, 100, 0),
            costed_fill(2, "AAPL", -10, 101, 1),
        ];
        approx(win_rate(&cheap).unwrap().unwrap(), 1.0);
    }

    #[test]
    fn win_rate_canonicalizes_symbols_like_the_ledger() {
        // Open on `AAPL`, close on `aapl` (and a whitespaced variant): the ledger
        // canonicalizes (trim + upper-case), so these are the SAME position and the
        // close realizes a win. Keying on the raw symbol would see no closed trade.
        let log = vec![fill(1, "AAPL", 10, 100), fill(2, " aapl ", -10, 120)];
        approx(win_rate(&log).unwrap().unwrap(), 1.0);
    }

    #[test]
    fn win_rate_rejects_negative_cost() {
        // A negative cost component would inflate net P&L and fabricate a win.
        let log = vec![costed_fill(1, "AAPL", 10, 100, -5)];
        assert!(matches!(
            win_rate(&log).unwrap_err(),
            MetricsError::NegativeFillCost { .. }
        ));
    }

    #[test]
    fn compute_is_deterministic() {
        // Baseline 1000, then five marks; benchmark carries its baseline first (6 levels
        // for a 5-mark curve).
        let curve = equity(&[(1, 1000), (2, 1200), (3, 900), (4, 1300), (5, 1250)]);
        let levels = bench(&[
            (0, 1000),
            (1, 1000),
            (2, 1050),
            (3, 980),
            (4, 1120),
            (5, 1100),
        ]);
        let log = vec![fill(1, "AAPL", 10, 100), fill(5, "AAPL", -10, 125)];
        let config = MetricsConfig::default();
        let benchmark = Benchmark::spy();
        let first = compute(1000, &curve, &log, &benchmark, Some(&levels), &config).unwrap();
        let second = compute(1000, &curve, &log, &benchmark, Some(&levels), &config).unwrap();
        assert_eq!(first, second);
        assert_eq!(first.benchmark_symbol, "SPY");
        assert!(first.sharpe_ratio.is_some());
        assert!(first.beta.is_some());
        assert!(first.max_drawdown.unwrap() > 0.0);
    }

    #[test]
    fn compute_without_benchmark_leaves_alpha_beta_undefined() {
        let curve = equity(&[(1, 100), (2, 120), (3, 110)]);
        let metrics = compute(
            100,
            &curve,
            &[],
            &Benchmark::spy(),
            None,
            &MetricsConfig::default(),
        )
        .unwrap();
        assert_eq!(metrics.alpha, None);
        assert_eq!(metrics.beta, None);
        assert_eq!(metrics.win_rate, None);
        assert_eq!(metrics.benchmark_symbol, "SPY");
    }

    #[test]
    fn compute_rejects_bad_config() {
        let curve = equity(&[(1, 100), (2, 110)]);
        let bad = MetricsConfig {
            periods_per_year: 0,
            risk_free_rate_per_period: 0.0,
        };
        assert_eq!(
            compute(100, &curve, &[], &Benchmark::spy(), None, &bad).unwrap_err(),
            MetricsError::NonPositivePeriodsPerYear
        );
    }

    #[test]
    fn terminal_zero_equity_is_a_defined_total_loss() {
        // Bankruptcy: a run whose FINAL mark is exactly zero is a valid completed run
        // (a -100% final return), not an error -- it must NOT abort all the metrics.
        // Baseline 1000, marks 500 then 0.
        let curve = equity(&[(1, 500), (2, 0)]);
        let returns = period_returns(1000, &curve).unwrap();
        approx(returns[0], -0.5);
        approx(returns[1], -1.0);
        let metrics = compute(
            1000,
            &curve,
            &[],
            &Benchmark::spy(),
            None,
            &MetricsConfig::default(),
        )
        .unwrap();
        // Annualized return is a total loss, drawdown is 100%, both finite and defined.
        approx(metrics.annualized_return.unwrap(), -1.0);
        approx(metrics.max_drawdown.unwrap(), 1.0);
        assert!(metrics.sharpe_ratio.is_some());
        assert!(metrics.annualized_volatility.is_some());
    }

    #[test]
    fn intermediate_zero_or_negative_equity_is_rejected() {
        // A non-final zero is the denominator of the next return (divide by zero), and a
        // negative equity makes the geometric return non-finite, so both are rejected.
        assert!(matches!(
            period_returns(1000, &equity(&[(1, 0), (2, 500)])).unwrap_err(),
            MetricsError::NonPositiveEquity { .. }
        ));
        assert!(matches!(
            period_returns(1000, &equity(&[(1, 500), (2, -5)])).unwrap_err(),
            MetricsError::NonPositiveEquity { .. }
        ));
    }

    #[test]
    fn compute_rejects_a_trade_log_outside_the_run_window() {
        // The trade log and the equity curve must describe the SAME run: a fill after the
        // last equity mark (a stale/mismatched or later log) is rejected as incoherent,
        // so the win rate and the equity-derived metrics cannot silently disagree.
        let curve = equity(&[(1, 1000), (2, 1010)]);
        let stale_log = vec![fill(1, "AAPL", 10, 100), fill(9, "AAPL", -10, 120)];
        assert!(matches!(
            compute(
                1000,
                &curve,
                &stale_log,
                &Benchmark::spy(),
                None,
                &MetricsConfig::default()
            )
            .unwrap_err(),
            MetricsError::TradeLogOutsideRun { ts: 9, .. }
        ));
        // A fill before the first equity mark is also out of the run window.
        let early_log = vec![fill(0, "AAPL", 10, 100)];
        assert!(matches!(
            compute(
                1000,
                &curve,
                &early_log,
                &Benchmark::spy(),
                None,
                &MetricsConfig::default()
            )
            .unwrap_err(),
            MetricsError::TradeLogOutsideRun { ts: 0, .. }
        ));
    }
}
