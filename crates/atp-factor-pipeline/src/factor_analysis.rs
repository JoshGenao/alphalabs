//! Factor analysis & tear-sheet outputs for completed factor-analysis runs
//! (SRS-BT-006 / SyRS SYS-18; StRS SN-1.05).
//!
//! SYS-18 requires the platform to provide factor analysis and tear-sheet reporting,
//! "including at minimum: factor returns, information coefficient, and turnover
//! analysis." This module is the deterministic, dependency-free core that computes those
//! three named deliverables from a [`FactorPanel`] -- a per-rebalance-period panel of
//! `(security, factor value, forward return)` observations -- and bundles them into one
//! [`FactorTearSheet`]:
//!
//! 1. [`InformationCoefficient`] -- the per-period Spearman rank correlation between the
//!    factor value at *t* and the realized forward return, plus the mean IC, the IC
//!    standard deviation, and the risk-adjusted IC (the "IC information ratio",
//!    mean / std). The IC measures how well the factor *ranks* future returns.
//! 2. [`FactorReturns`] -- the per-period quantile-sorted portfolio returns: securities
//!    are sorted into `quantiles` buckets by factor value, each bucket's mean forward
//!    return is reported, and the top-minus-bottom long-short **spread** (the period
//!    return of a dollar-neutral portfolio long the top quantile and short the bottom)
//!    is tracked, with its mean and cumulative (compounded) value.
//! 3. [`TurnoverAnalysis`] -- the per-period membership churn of the top (and bottom)
//!    quantile between consecutive rebalances, i.e. the fraction of names that changed.
//!    High turnover implies high transaction-cost drag on the factor.
//!
//! ## Numeric boundary (the headline design decision)
//!
//! A factor value is a dimensionless *score* and a forward return is a dimensionless
//! *ratio* -- neither is money -- so this module's domain is `f64` end to end. Unlike the
//! ledger / persistence paths (which keep money in integer minor units), no integer-minor
//! money ever enters here, so the `f64` work is the factor domain, not a money-correctness
//! leak (the same boundary [`crate`]'s sibling `metrics` family draws for its dimensionless
//! ratios). The float work is made **deterministic** -- the SRS-BT-010 criterion this
//! family must honor: every reduction is a fixed left-to-right fold over the
//! timestamp-ordered periods, securities are sorted by a total order
//! `(factor_value, SecurityKey)` so ties break the same way every run, ranks use
//! averaged tie ranks, and there is no parallelism, no platform RNG, and no wall-clock
//! read -- so identical inputs always produce bit-identical outputs.
//!
//! ## Undefined vs fabricated
//!
//! A statistic that is mathematically undefined on the given input -- the IC of a period
//! whose factor values (or returns) have zero rank dispersion, the mean IC when no period
//! had a defined IC, the IC std with fewer than two defined ICs, turnover with only one
//! rebalance -- is reported as `None`, NEVER a fabricated `0.0` and never a leaked
//! `NaN`/`inf`. Each computed aggregate is verified finite before it is returned
//! ([`FactorAnalysisError::NonFiniteComputation`]), and each per-period IC is clamped to
//! its mathematical `[-1, 1]` domain, so a pathological input fails closed rather than
//! emitting a poison value that would corrupt a ranking or a tear-sheet.
//!
//! ## Trust boundary (fail-closed [`FactorPanel::validate`])
//!
//! A factor-analysis run's inputs come from the (deferred) factor pipeline and data
//! layer, so the panel is validated at the boundary before any statistic is computed: the
//! panel is non-empty, `quantiles >= 2`, every period is non-empty with finite factor
//! values and returns, no security appears twice in one period (which would double-count a
//! name in a quantile), period timestamps strictly increase, and every period holds at
//! least `quantiles` securities so each quantile bucket is non-empty. A panel that fails
//! any of these is rejected rather than silently producing a degenerate or
//! order-dependent tear-sheet.
//!
//! ## Deferred (why SRS-BT-006 stays `passes:false`)
//!
//! This slice ships only the deterministic computation surface. The scheduled
//! full-universe factor job that produces the panel (SRS-FAC-001), wiring the real factor
//! values and forward returns from the unified historical data interface (SRS-DATA-007),
//! rendering the tear-sheet to an operator (SRS-UI / SRS-API), and bundling the
//! SRS-BT-004 `PerformanceMetrics` family into one cross-crate report are each their own
//! feature and remain `passes:false`.

use std::collections::HashSet;

use atp_types::SecurityKey;

/// One security's factor observation for a single rebalance period: its factor *score*
/// and the *forward return* realized over the period that follows the factor's
/// measurement. Both are dimensionless `f64` and must be finite.
#[derive(Debug, Clone, PartialEq)]
pub struct FactorObservation {
    /// The security this observation is for. Identity drives quantile membership and the
    /// turnover set intersection, and breaks factor-value ties deterministically.
    pub security: SecurityKey,
    /// The factor score at the start of the period (higher = more exposure to the factor).
    pub factor_value: f64,
    /// The return realized over the period that follows the factor measurement.
    pub forward_return: f64,
}

impl FactorObservation {
    /// Build an observation.
    pub fn new(security: SecurityKey, factor_value: f64, forward_return: f64) -> Self {
        Self {
            security,
            factor_value,
            forward_return,
        }
    }
}

/// All securities' observations for one rebalance period, stamped with the period's
/// timestamp. Periods are ordered (strictly increasing `ts`) across a [`FactorPanel`].
#[derive(Debug, Clone, PartialEq)]
pub struct FactorPeriod {
    /// The rebalance timestamp this cross-section belongs to.
    pub ts: u64,
    /// The cross-section of `(security, factor, forward return)` observations.
    pub observations: Vec<FactorObservation>,
}

impl FactorPeriod {
    /// Build a period.
    pub fn new(ts: u64, observations: Vec<FactorObservation>) -> Self {
        Self { ts, observations }
    }
}

/// A completed factor-analysis run's input: the ordered per-period panel plus the number
/// of quantile buckets to sort securities into for factor-return and turnover analysis.
#[derive(Debug, Clone, PartialEq)]
pub struct FactorPanel {
    /// The rebalance periods, in strictly increasing timestamp order.
    pub periods: Vec<FactorPeriod>,
    /// The number of quantile buckets (>= 2). Bucket `0` is the bottom (lowest factor),
    /// bucket `quantiles - 1` is the top (highest factor).
    pub quantiles: usize,
}

impl FactorPanel {
    /// Build a panel.
    pub fn new(periods: Vec<FactorPeriod>, quantiles: usize) -> Self {
        Self { periods, quantiles }
    }

    /// Validate the panel at the trust boundary, fail-closed. Returns the first violation
    /// (see the module-level "Trust boundary" note for the full invariant set).
    pub fn validate(&self) -> Result<(), FactorAnalysisError> {
        if self.periods.is_empty() {
            return Err(FactorAnalysisError::EmptyPanel);
        }
        if self.quantiles < 2 {
            return Err(FactorAnalysisError::InvalidQuantileCount {
                quantiles: self.quantiles,
            });
        }

        let mut previous_ts: Option<u64> = None;
        for period in &self.periods {
            if let Some(prev) = previous_ts {
                if period.ts <= prev {
                    return Err(FactorAnalysisError::NonMonotonicPeriods { ts: period.ts });
                }
            }
            previous_ts = Some(period.ts);

            if period.observations.is_empty() {
                return Err(FactorAnalysisError::EmptyPeriod { ts: period.ts });
            }
            if period.observations.len() < self.quantiles {
                return Err(FactorAnalysisError::InsufficientSecurities {
                    ts: period.ts,
                    securities: period.observations.len(),
                    quantiles: self.quantiles,
                });
            }

            let mut seen: HashSet<&SecurityKey> = HashSet::with_capacity(period.observations.len());
            for observation in &period.observations {
                if !observation.factor_value.is_finite() || !observation.forward_return.is_finite()
                {
                    return Err(FactorAnalysisError::NonFiniteInput { ts: period.ts });
                }
                if !seen.insert(&observation.security) {
                    return Err(FactorAnalysisError::DuplicateSecurity {
                        ts: period.ts,
                        symbol: observation.security.symbol().to_string(),
                    });
                }
            }
        }
        Ok(())
    }
}

/// The information-coefficient analysis: the per-period Spearman rank correlation between
/// factor value and forward return, with its summary statistics. A period whose IC is
/// mathematically undefined (zero rank dispersion in either series) carries `None`.
#[derive(Debug, Clone, PartialEq)]
pub struct InformationCoefficient {
    /// `(period ts, IC)` in period order; `None` where the IC is undefined for the period.
    pub per_period: Vec<(u64, Option<f64>)>,
    /// Mean of the defined per-period ICs; `None` when no period had a defined IC.
    pub mean: Option<f64>,
    /// Sample (ddof=1) standard deviation of the defined ICs; `None` with fewer than two.
    pub std: Option<f64>,
    /// The risk-adjusted IC (IC information ratio) `mean / std`; `None` when undefined.
    pub risk_adjusted: Option<f64>,
}

/// The factor-return analysis: per-period quantile mean returns and the top-minus-bottom
/// long-short spread series with its summary statistics.
#[derive(Debug, Clone, PartialEq)]
pub struct FactorReturns {
    /// Per period, the mean forward return of each quantile bucket (length `quantiles`,
    /// bucket `0` = bottom, last = top). Outer vector is in period order.
    pub per_quantile_mean: Vec<Vec<f64>>,
    /// `(period ts, spread)` where spread = top-quantile mean − bottom-quantile mean, the
    /// per-period return of the dollar-neutral long-short factor portfolio.
    pub spread_per_period: Vec<(u64, f64)>,
    /// Arithmetic mean of the per-period spreads; `None` when there are no periods.
    pub mean_spread: Option<f64>,
    /// Cumulative (compounded) spread `∏(1 + spread_t) − 1`; `None` when there are no
    /// periods.
    pub cumulative_spread: Option<f64>,
}

/// The turnover analysis: per-period membership churn of the top and bottom quantiles
/// between consecutive rebalances, with the mean of each. The churn fraction is
/// `1 − |members_t ∩ members_{t−1}| / |members_t|`.
#[derive(Debug, Clone, PartialEq)]
pub struct TurnoverAnalysis {
    /// `(period ts, top-quantile turnover)` for each period after the first.
    pub top_turnover: Vec<(u64, f64)>,
    /// `(period ts, bottom-quantile turnover)` for each period after the first.
    pub bottom_turnover: Vec<(u64, f64)>,
    /// Mean top-quantile turnover; `None` with fewer than two periods.
    pub mean_top: Option<f64>,
    /// Mean bottom-quantile turnover; `None` with fewer than two periods.
    pub mean_bottom: Option<f64>,
}

/// The completed factor-analysis tear-sheet: the three SRS-BT-006 deliverables bundled
/// for one run.
#[derive(Debug, Clone, PartialEq)]
pub struct FactorTearSheet {
    /// The information-coefficient analysis.
    pub ic: InformationCoefficient,
    /// The quantile factor-return analysis.
    pub returns: FactorReturns,
    /// The quantile turnover analysis.
    pub turnover: TurnoverAnalysis,
    /// The number of periods analyzed.
    pub n_periods: usize,
    /// The number of quantile buckets used.
    pub n_quantiles: usize,
}

/// Why a factor-analysis run could not be computed. Every variant fails closed -- the
/// statistic is never fabricated past one of these.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FactorAnalysisError {
    /// The panel had no periods -- nothing to analyze.
    EmptyPanel,
    /// A period had no observations.
    EmptyPeriod { ts: u64 },
    /// Period timestamps were not strictly increasing. A non-monotonic or duplicated
    /// period timestamp would make the turnover series (which pairs consecutive periods)
    /// and the period ordering ambiguous, so it is rejected rather than silently sorted.
    NonMonotonicPeriods { ts: u64 },
    /// A security appeared more than once within a single period, which would double-count
    /// it in a quantile bucket and in the turnover set.
    DuplicateSecurity { ts: u64, symbol: String },
    /// The requested quantile count was below 2 (a single bucket has no top/bottom spread
    /// and no cross-sectional sort).
    InvalidQuantileCount { quantiles: usize },
    /// A period held fewer securities than `quantiles`, so at least one bucket would be
    /// empty and its mean return undefined.
    InsufficientSecurities {
        ts: u64,
        securities: usize,
        quantiles: usize,
    },
    /// A factor value or forward return was non-finite (NaN/inf).
    NonFiniteInput { ts: u64 },
    /// A computed statistic came out non-finite. The guards above should make this
    /// unreachable; it exists so a pathological input fails closed rather than returning a
    /// poison value.
    NonFiniteComputation { metric: &'static str },
}

impl std::fmt::Display for FactorAnalysisError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::EmptyPanel => write!(formatter, "SRS-BT-006: factor panel had no periods"),
            Self::EmptyPeriod { ts } => {
                write!(
                    formatter,
                    "SRS-BT-006: factor period {ts} had no observations"
                )
            }
            Self::NonMonotonicPeriods { ts } => write!(
                formatter,
                "SRS-BT-006: factor period timestamps must strictly increase (offending ts {ts})"
            ),
            Self::DuplicateSecurity { ts, symbol } => write!(
                formatter,
                "SRS-BT-006: security {symbol} appeared more than once in factor period {ts}"
            ),
            Self::InvalidQuantileCount { quantiles } => write!(
                formatter,
                "SRS-BT-006: quantile count must be >= 2 (got {quantiles})"
            ),
            Self::InsufficientSecurities {
                ts,
                securities,
                quantiles,
            } => write!(
                formatter,
                "SRS-BT-006: factor period {ts} had {securities} securities, fewer than the \
                 {quantiles} quantiles requested"
            ),
            Self::NonFiniteInput { ts } => write!(
                formatter,
                "SRS-BT-006: factor period {ts} carried a non-finite factor value or return"
            ),
            Self::NonFiniteComputation { metric } => write!(
                formatter,
                "SRS-BT-006: computed factor statistic {metric} was non-finite"
            ),
        }
    }
}

impl std::error::Error for FactorAnalysisError {}

/// Relative tolerance below which a rank dispersion is treated as zero. A constant rank
/// vector yields exactly `0.0` variance, but averaged tie ranks can leave floating-point
/// noise; an exact `== 0.0` check would miss it and divide by ~1e-17, producing an
/// enormous but finite correlation that passes the finiteness guard. Mirrors the
/// `metrics` family's dispersion epsilon.
const DISPERSION_EPSILON: f64 = 1e-12;

/// Whether `dispersion` is negligible relative to the scale of `series` -- small enough to
/// be floating-point noise rather than real variation, so a correlation dividing by it
/// would be spurious. Treated as a zero denominator (the IC is undefined).
fn negligible_dispersion(dispersion: f64, series: &[f64]) -> bool {
    let scale = series
        .iter()
        .fold(0.0_f64, |acc, value| acc.max(value.abs()))
        .max(1.0);
    dispersion <= DISPERSION_EPSILON * scale * scale
}

/// Arithmetic mean, folded left-to-right for determinism. `xs` must be non-empty.
fn mean(xs: &[f64]) -> f64 {
    let mut sum = 0.0;
    for &x in xs {
        sum += x;
    }
    sum / xs.len() as f64
}

/// Sample (ddof=1) standard deviation, folded left-to-right. `None` with fewer than two
/// observations.
fn sample_std(xs: &[f64]) -> Option<f64> {
    if xs.len() < 2 {
        return None;
    }
    let m = mean(xs);
    let mut sum_sq = 0.0;
    for &x in xs {
        let deviation = x - m;
        sum_sq += deviation * deviation;
    }
    Some((sum_sq / (xs.len() - 1) as f64).sqrt())
}

/// Average tie ranks (1-based) of `xs`, returned aligned to the input order. Equal values
/// receive the average of the ranks they span -- the standard Spearman tie correction.
fn average_ranks(xs: &[f64]) -> Vec<f64> {
    let n = xs.len();
    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by(|&a, &b| xs[a].total_cmp(&xs[b]));

    let mut ranks = vec![0.0_f64; n];
    let mut i = 0;
    while i < n {
        let mut j = i + 1;
        while j < n && xs[order[j]] == xs[order[i]] {
            j += 1;
        }
        // 1-based ranks (i+1)..=j average to (first + last) / 2.
        let average = ((i + 1 + j) as f64) / 2.0;
        for &original_index in &order[i..j] {
            ranks[original_index] = average;
        }
        i = j;
    }
    ranks
}

/// Pearson correlation of two equal-length series, folded left-to-right. `None` when fewer
/// than two points or when either series has negligible dispersion (undefined correlation).
fn pearson(a: &[f64], b: &[f64]) -> Option<f64> {
    if a.len() != b.len() || a.len() < 2 {
        return None;
    }
    let mean_a = mean(a);
    let mean_b = mean(b);
    let mut covariance = 0.0;
    let mut variance_a = 0.0;
    let mut variance_b = 0.0;
    for (&x, &y) in a.iter().zip(b.iter()) {
        let deviation_a = x - mean_a;
        let deviation_b = y - mean_b;
        covariance += deviation_a * deviation_b;
        variance_a += deviation_a * deviation_a;
        variance_b += deviation_b * deviation_b;
    }
    if negligible_dispersion(variance_a, a) || negligible_dispersion(variance_b, b) {
        return None;
    }
    Some(covariance / (variance_a.sqrt() * variance_b.sqrt()))
}

/// Guard a computed statistic: a non-finite result fails closed.
fn finite(metric: &'static str, value: f64) -> Result<f64, FactorAnalysisError> {
    if value.is_finite() {
        Ok(value)
    } else {
        Err(FactorAnalysisError::NonFiniteComputation { metric })
    }
}

/// The quantile index in `[0, quantiles)` for the security at sorted `position` of `count`
/// total, lowest factor in bucket `0`. Bucket sizes differ by at most one and every bucket
/// is non-empty when `count >= quantiles` (guaranteed by [`FactorPanel::validate`]).
fn quantile_of(position: usize, count: usize, quantiles: usize) -> usize {
    (position * quantiles) / count
}

/// The per-period analysis derived once from a single cross-section, reused by the factor
/// returns and the turnover.
struct PeriodBuckets {
    /// Mean forward return of each quantile bucket (length `quantiles`).
    quantile_means: Vec<f64>,
    /// The securities in the bottom (`0`) and top (`quantiles - 1`) buckets.
    bottom_members: HashSet<SecurityKey>,
    top_members: HashSet<SecurityKey>,
}

/// Sort a period's cross-section by the total order `(factor_value, SecurityKey)` and
/// compute its quantile means and top/bottom membership.
fn bucket_period(period: &FactorPeriod, quantiles: usize) -> PeriodBuckets {
    let mut sorted: Vec<&FactorObservation> = period.observations.iter().collect();
    sorted.sort_by(|a, b| {
        a.factor_value
            .total_cmp(&b.factor_value)
            .then_with(|| a.security.cmp(&b.security))
    });

    let count = sorted.len();
    let mut bucket_returns: Vec<Vec<f64>> = vec![Vec::new(); quantiles];
    let mut bottom_members: HashSet<SecurityKey> = HashSet::new();
    let mut top_members: HashSet<SecurityKey> = HashSet::new();

    for (position, observation) in sorted.iter().enumerate() {
        let bucket = quantile_of(position, count, quantiles);
        bucket_returns[bucket].push(observation.forward_return);
        if bucket == 0 {
            bottom_members.insert(observation.security.clone());
        }
        if bucket == quantiles - 1 {
            top_members.insert(observation.security.clone());
        }
    }

    // Every bucket is non-empty (validated), so each mean is defined.
    let quantile_means = bucket_returns.iter().map(|returns| mean(returns)).collect();

    PeriodBuckets {
        quantile_means,
        bottom_members,
        top_members,
    }
}

/// Membership churn of `current` relative to `previous`:
/// `1 − |current ∩ previous| / |current|`. `current` is non-empty (a validated quantile).
fn membership_turnover(current: &HashSet<SecurityKey>, previous: &HashSet<SecurityKey>) -> f64 {
    let retained = current.iter().filter(|key| previous.contains(*key)).count();
    1.0 - (retained as f64) / (current.len() as f64)
}

/// Compute the SRS-BT-006 factor-analysis tear-sheet for one completed run.
///
/// Validates the panel fail-closed, then computes the information coefficient, the
/// quantile factor returns, and the turnover analysis. Deterministic: every reduction is a
/// left-to-right fold over the timestamp-ordered periods, cross-sections are sorted by the
/// total order `(factor_value, SecurityKey)`, and there is no parallelism, RNG, or clock.
/// Fails closed on an invalid panel or a non-finite aggregate.
pub fn compute_tear_sheet(panel: &FactorPanel) -> Result<FactorTearSheet, FactorAnalysisError> {
    panel.validate()?;
    let quantiles = panel.quantiles;

    let mut ic_per_period: Vec<(u64, Option<f64>)> = Vec::with_capacity(panel.periods.len());
    let mut defined_ic: Vec<f64> = Vec::new();
    let mut per_quantile_mean: Vec<Vec<f64>> = Vec::with_capacity(panel.periods.len());
    let mut spread_per_period: Vec<(u64, f64)> = Vec::with_capacity(panel.periods.len());
    let mut top_turnover: Vec<(u64, f64)> = Vec::new();
    let mut bottom_turnover: Vec<(u64, f64)> = Vec::new();

    let mut previous_top: Option<HashSet<SecurityKey>> = None;
    let mut previous_bottom: Option<HashSet<SecurityKey>> = None;

    for period in &panel.periods {
        // Information coefficient: Spearman rank correlation of factor vs forward return.
        let factor_values: Vec<f64> = period
            .observations
            .iter()
            .map(|observation| observation.factor_value)
            .collect();
        let forward_returns: Vec<f64> = period
            .observations
            .iter()
            .map(|observation| observation.forward_return)
            .collect();
        let ic = pearson(
            &average_ranks(&factor_values),
            &average_ranks(&forward_returns),
        )
        .map(|correlation| correlation.clamp(-1.0, 1.0));
        if let Some(value) = ic {
            defined_ic.push(value);
        }
        ic_per_period.push((period.ts, ic));

        // Quantile factor returns + turnover membership (one cross-section sort).
        let buckets = bucket_period(period, quantiles);
        let spread = finite(
            "factor_return_spread",
            buckets.quantile_means[quantiles - 1] - buckets.quantile_means[0],
        )?;
        spread_per_period.push((period.ts, spread));
        per_quantile_mean.push(buckets.quantile_means);

        if let Some(prior_top) = &previous_top {
            top_turnover.push((
                period.ts,
                finite(
                    "top_turnover",
                    membership_turnover(&buckets.top_members, prior_top),
                )?,
            ));
        }
        if let Some(prior_bottom) = &previous_bottom {
            bottom_turnover.push((
                period.ts,
                finite(
                    "bottom_turnover",
                    membership_turnover(&buckets.bottom_members, prior_bottom),
                )?,
            ));
        }
        previous_top = Some(buckets.top_members);
        previous_bottom = Some(buckets.bottom_members);
    }

    // IC aggregates over the DEFINED per-period ICs.
    let ic_mean = match defined_ic.as_slice() {
        [] => None,
        values => Some(finite("ic_mean", mean(values))?),
    };
    let ic_std = match sample_std(&defined_ic) {
        Some(std) => Some(finite("ic_std", std)?),
        None => None,
    };
    let ic_risk_adjusted = match (ic_mean, ic_std) {
        (Some(mean_value), Some(std_value)) if !negligible_dispersion(std_value, &defined_ic) => {
            Some(finite("ic_risk_adjusted", mean_value / std_value)?)
        }
        _ => None,
    };

    // Spread aggregates.
    let spreads: Vec<f64> = spread_per_period.iter().map(|&(_, value)| value).collect();
    let mean_spread = match spreads.as_slice() {
        [] => None,
        values => Some(finite("mean_spread", mean(values))?),
    };
    let cumulative_spread = if spreads.is_empty() {
        None
    } else {
        let mut compounded = 1.0_f64;
        for &spread in &spreads {
            compounded *= 1.0 + spread;
        }
        Some(finite("cumulative_spread", compounded - 1.0)?)
    };

    // Turnover aggregates.
    let mean_top = match top_turnover.as_slice() {
        [] => None,
        values => {
            let series: Vec<f64> = values.iter().map(|&(_, v)| v).collect();
            Some(finite("mean_top_turnover", mean(&series))?)
        }
    };
    let mean_bottom = match bottom_turnover.as_slice() {
        [] => None,
        values => {
            let series: Vec<f64> = values.iter().map(|&(_, v)| v).collect();
            Some(finite("mean_bottom_turnover", mean(&series))?)
        }
    };

    Ok(FactorTearSheet {
        ic: InformationCoefficient {
            per_period: ic_per_period,
            mean: ic_mean,
            std: ic_std,
            risk_adjusted: ic_risk_adjusted,
        },
        returns: FactorReturns {
            per_quantile_mean,
            spread_per_period,
            mean_spread,
            cumulative_spread,
        },
        turnover: TurnoverAnalysis {
            top_turnover,
            bottom_turnover,
            mean_top,
            mean_bottom,
        },
        n_periods: panel.periods.len(),
        n_quantiles: quantiles,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use atp_types::AssetClass;

    fn key(symbol: &str) -> SecurityKey {
        SecurityKey::new(symbol, AssetClass::Equity).expect("equity key")
    }

    fn observation(symbol: &str, factor: f64, ret: f64) -> FactorObservation {
        FactorObservation::new(key(symbol), factor, ret)
    }

    /// A 4-security period with monotone factor and returns: perfect positive IC.
    fn perfect_period(ts: u64) -> FactorPeriod {
        FactorPeriod::new(
            ts,
            vec![
                observation("AAA", 1.0, 0.1),
                observation("BBB", 2.0, 0.2),
                observation("CCC", 3.0, 0.3),
                observation("DDD", 4.0, 0.4),
            ],
        )
    }

    fn approx(a: f64, b: f64) {
        assert!((a - b).abs() < 1e-9, "expected {b}, got {a}");
    }

    #[test]
    fn perfect_positive_information_coefficient() {
        let panel = FactorPanel::new(vec![perfect_period(1)], 2);
        let sheet = compute_tear_sheet(&panel).expect("tear sheet");
        let (ts, ic) = sheet.ic.per_period[0];
        assert_eq!(ts, 1);
        approx(ic.expect("defined ic"), 1.0);
        approx(sheet.ic.mean.expect("mean ic"), 1.0);
    }

    #[test]
    fn perfect_negative_information_coefficient() {
        let period = FactorPeriod::new(
            1,
            vec![
                observation("AAA", 1.0, 0.4),
                observation("BBB", 2.0, 0.3),
                observation("CCC", 3.0, 0.2),
                observation("DDD", 4.0, 0.1),
            ],
        );
        let sheet = compute_tear_sheet(&FactorPanel::new(vec![period], 2)).expect("tear sheet");
        approx(sheet.ic.per_period[0].1.expect("ic"), -1.0);
    }

    #[test]
    fn information_coefficient_handles_ties() {
        // factor ranks: 1, 2.5, 2.5, 4 ; return ranks: 1, 2, 3, 4 -> Spearman ~= 0.948683
        let period = FactorPeriod::new(
            1,
            vec![
                observation("AAA", 1.0, 0.1),
                observation("BBB", 2.0, 0.2),
                observation("CCC", 2.0, 0.3),
                observation("DDD", 3.0, 0.4),
            ],
        );
        let sheet = compute_tear_sheet(&FactorPanel::new(vec![period], 2)).expect("tear sheet");
        approx(
            sheet.ic.per_period[0].1.expect("ic"),
            0.948_683_298_050_513_8,
        );
    }

    #[test]
    fn constant_factor_yields_undefined_ic() {
        let period = FactorPeriod::new(
            1,
            vec![
                observation("AAA", 5.0, 0.1),
                observation("BBB", 5.0, 0.2),
                observation("CCC", 5.0, 0.3),
                observation("DDD", 5.0, 0.4),
            ],
        );
        let sheet = compute_tear_sheet(&FactorPanel::new(vec![period], 2)).expect("tear sheet");
        assert_eq!(sheet.ic.per_period[0].1, None);
        // No defined IC anywhere -> mean/std/risk-adjusted are all None, never fabricated 0.
        assert_eq!(sheet.ic.mean, None);
        assert_eq!(sheet.ic.std, None);
        assert_eq!(sheet.ic.risk_adjusted, None);
    }

    #[test]
    fn quantile_spread_is_top_minus_bottom() {
        let sheet =
            compute_tear_sheet(&FactorPanel::new(vec![perfect_period(1)], 2)).expect("tear sheet");
        // bottom bucket {AAA,BBB} mean 0.15, top {CCC,DDD} mean 0.35 -> spread 0.20.
        let means = &sheet.returns.per_quantile_mean[0];
        approx(means[0], 0.15);
        approx(means[1], 0.35);
        approx(sheet.returns.spread_per_period[0].1, 0.20);
        approx(sheet.returns.mean_spread.expect("mean spread"), 0.20);
        approx(sheet.returns.cumulative_spread.expect("cumulative"), 0.20);
    }

    #[test]
    fn uneven_quantiles_keep_every_bucket_nonempty() {
        // 10 securities into 3 quantiles -> sizes 4,3,3, all defined.
        let observations: Vec<FactorObservation> = (0..10)
            .map(|i| observation(&format!("S{i:02}"), i as f64, (i as f64) / 100.0))
            .collect();
        let sheet = compute_tear_sheet(&FactorPanel::new(
            vec![FactorPeriod::new(1, observations)],
            3,
        ))
        .expect("tear sheet");
        assert_eq!(sheet.returns.per_quantile_mean[0].len(), 3);
        for mean_value in &sheet.returns.per_quantile_mean[0] {
            assert!(mean_value.is_finite());
        }
    }

    #[test]
    fn turnover_full_churn_when_membership_inverts() {
        // P1: AAA<BBB<CCC<DDD (top {CCC,DDD}, bottom {AAA,BBB}).
        // P2: DDD<CCC<BBB<AAA (top {AAA,BBB}, bottom {CCC,DDD}). Top/bottom fully swap.
        let p1 = perfect_period(1);
        let p2 = FactorPeriod::new(
            2,
            vec![
                observation("AAA", 4.0, 0.1),
                observation("BBB", 3.0, 0.2),
                observation("CCC", 2.0, 0.3),
                observation("DDD", 1.0, 0.4),
            ],
        );
        let sheet = compute_tear_sheet(&FactorPanel::new(vec![p1, p2], 2)).expect("tear sheet");
        // One turnover point (for the second period).
        assert_eq!(sheet.turnover.top_turnover.len(), 1);
        approx(sheet.turnover.top_turnover[0].1, 1.0);
        approx(sheet.turnover.bottom_turnover[0].1, 1.0);
        approx(sheet.turnover.mean_top.expect("mean top"), 1.0);
        approx(sheet.turnover.mean_bottom.expect("mean bottom"), 1.0);
    }

    #[test]
    fn turnover_zero_when_membership_stable() {
        let sheet = compute_tear_sheet(&FactorPanel::new(
            vec![perfect_period(1), perfect_period(2)],
            2,
        ))
        .expect("tear sheet");
        approx(sheet.turnover.top_turnover[0].1, 0.0);
        approx(sheet.turnover.bottom_turnover[0].1, 0.0);
    }

    #[test]
    fn single_period_leaves_turnover_undefined() {
        let sheet =
            compute_tear_sheet(&FactorPanel::new(vec![perfect_period(1)], 2)).expect("tear sheet");
        assert!(sheet.turnover.top_turnover.is_empty());
        assert_eq!(sheet.turnover.mean_top, None);
        assert_eq!(sheet.turnover.mean_bottom, None);
    }

    #[test]
    fn ic_std_and_risk_adjusted_defined_over_multiple_periods() {
        let p1 = perfect_period(1); // ic 1.0
        let p2 = FactorPeriod::new(
            2,
            vec![
                observation("AAA", 1.0, 0.4),
                observation("BBB", 2.0, 0.3),
                observation("CCC", 3.0, 0.1),
                observation("DDD", 4.0, 0.2),
            ],
        );
        let sheet = compute_tear_sheet(&FactorPanel::new(vec![p1, p2], 2)).expect("tear sheet");
        assert!(sheet.ic.mean.is_some());
        assert!(sheet.ic.std.is_some());
        assert!(sheet.ic.risk_adjusted.is_some());
    }

    #[test]
    fn computation_is_deterministic_across_runs_and_input_order() {
        let ordered = perfect_period(1);
        let shuffled = FactorPeriod::new(
            1,
            vec![
                observation("DDD", 4.0, 0.4),
                observation("AAA", 1.0, 0.1),
                observation("CCC", 3.0, 0.3),
                observation("BBB", 2.0, 0.2),
            ],
        );
        let a = compute_tear_sheet(&FactorPanel::new(vec![ordered], 2)).expect("a");
        let b = compute_tear_sheet(&FactorPanel::new(vec![shuffled], 2)).expect("b");
        // Bit-identical regardless of observation order within the period.
        assert_eq!(a, b);
    }

    #[test]
    fn empty_panel_is_rejected() {
        assert_eq!(
            compute_tear_sheet(&FactorPanel::new(vec![], 2)).unwrap_err(),
            FactorAnalysisError::EmptyPanel
        );
    }

    #[test]
    fn empty_period_is_rejected() {
        let err = compute_tear_sheet(&FactorPanel::new(vec![FactorPeriod::new(1, vec![])], 2))
            .unwrap_err();
        assert_eq!(err, FactorAnalysisError::EmptyPeriod { ts: 1 });
    }

    #[test]
    fn non_monotonic_periods_are_rejected() {
        let err = compute_tear_sheet(&FactorPanel::new(
            vec![perfect_period(2), perfect_period(2)],
            2,
        ))
        .unwrap_err();
        assert_eq!(err, FactorAnalysisError::NonMonotonicPeriods { ts: 2 });
    }

    #[test]
    fn duplicate_security_is_rejected() {
        let period = FactorPeriod::new(
            1,
            vec![
                observation("AAA", 1.0, 0.1),
                observation("AAA", 2.0, 0.2),
                observation("BBB", 3.0, 0.3),
            ],
        );
        let err = compute_tear_sheet(&FactorPanel::new(vec![period], 2)).unwrap_err();
        assert_eq!(
            err,
            FactorAnalysisError::DuplicateSecurity {
                ts: 1,
                symbol: "AAA".to_string(),
            }
        );
    }

    #[test]
    fn invalid_quantile_count_is_rejected() {
        let err = compute_tear_sheet(&FactorPanel::new(vec![perfect_period(1)], 1)).unwrap_err();
        assert_eq!(
            err,
            FactorAnalysisError::InvalidQuantileCount { quantiles: 1 }
        );
    }

    #[test]
    fn insufficient_securities_for_quantiles_is_rejected() {
        let period = FactorPeriod::new(
            1,
            vec![observation("AAA", 1.0, 0.1), observation("BBB", 2.0, 0.2)],
        );
        let err = compute_tear_sheet(&FactorPanel::new(vec![period], 3)).unwrap_err();
        assert_eq!(
            err,
            FactorAnalysisError::InsufficientSecurities {
                ts: 1,
                securities: 2,
                quantiles: 3,
            }
        );
    }

    #[test]
    fn non_finite_input_is_rejected() {
        let period = FactorPeriod::new(
            1,
            vec![
                observation("AAA", f64::NAN, 0.1),
                observation("BBB", 2.0, 0.2),
            ],
        );
        let err = compute_tear_sheet(&FactorPanel::new(vec![period], 2)).unwrap_err();
        assert_eq!(err, FactorAnalysisError::NonFiniteInput { ts: 1 });
    }

    #[test]
    fn option_asset_class_cannot_build_a_factor_key() {
        // Defensive: the factor panel inherits SecurityKey's fail-closed option rejection.
        assert!(SecurityKey::new("AAPL", AssetClass::Option).is_err());
    }
}
