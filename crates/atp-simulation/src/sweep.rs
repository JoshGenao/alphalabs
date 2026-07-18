//! Grid search and multidimensional parameter sweeps for backtests (SRS-BT-007 /
//! SyRS SYS-19; StRS SN-1.16).
//!
//! The acceptance criterion is one sentence with three named artifacts: "A **parameter
//! space definition** produces **ranked backtest results** by the **selected objective
//! function**." Each maps to a type here:
//!
//! - [`ParameterSpace`] — the parameter space definition: validated named axes whose
//!   Cartesian product enumerates every combination as a canonical
//!   [`StrategyParameters`] point (the SRS-BT-009 parameter-set identity, so a sweep
//!   point is queryable in backtest history by exactly the axis that produced it).
//! - [`ObjectiveFunction`] — the selected objective function: one of the eight SYS-16
//!   metrics ([`ObjectiveMetric`]) plus a [`Direction`] (SYS-19 names "maximize Sharpe
//!   ratio, minimize maximum drawdown" as the canonical examples; the selector is
//!   uniform over the whole SRS-BT-004 family).
//! - [`SweepReport`] — the ranked backtest results: every point accounted for, best
//!   first, with 1-based ranks.
//!
//! [`SweepRunner::run`] evaluates each point through the SAME shipped producer chain a
//! single backtest uses — [`BacktestEngine::run`] then [`benchmark::compare`] — so a
//! sweep result is exactly what a standalone run of that point would report. Points are
//! evaluated strictly sequentially in enumeration order with no parallelism, RNG, or
//! clock, so identical inputs produce an identical report (SRS-BT-010).
//!
//! Fail-closed decisions (each pinned by `srs_bt_007_parameter_sweep`):
//!
//! - A point whose objective metric is mathematically undefined (`None` — e.g. win rate
//!   with zero trades) is reported in [`SweepReport::unranked`] with
//!   [`UnrankedReason::ObjectiveUndefined`], never ranked last (that would fabricate the
//!   ordering claim "worse than every defined value"), never a fabricated 0, and never
//!   silently dropped ([`SweepReport::total_points`] proves the accounting).
//! - Any per-point failure (factory rejection, engine error, benchmark error) aborts the
//!   WHOLE sweep with [`SweepError::PointFailed`] naming the offending point: a partial
//!   ranking over a space the user mis-defined could silently mis-rank, and the
//!   walk-forward consumer (SRS-BT-008) needs all-or-error reproducibility per window.
//! - The space's cardinality is bounded ([`MAX_SWEEP_POINTS`], `checked_mul` in `u128`)
//!   and enforced BEFORE any point is materialized or any backtest runs.
//! - Equal objective values are ordered by the points' canonical
//!   [`StrategyParameters::entries`] (already key-sorted; lexicographic — deterministic,
//!   not numeric), so ties can never make the ranking order-dependent.
//!
//! The strategy side of the boundary is [`SweepStrategyFactory`]: the bridge that turns
//! one [`StrategyParameters`] point into a configured [`BacktestStrategy`]. It fails
//! closed on a missing / unknown / unparseable parameter — a point the strategy cannot
//! interpret must never silently run with defaults, because its ranked row would then
//! misattribute a default run's performance to the labeled parameters. The REAL factory
//! is the deferred Python strategy host (which knows a strategy's declared parameters);
//! fixture factories realize the seam solo, exactly as the SRS-BT-007 verification step
//! specifies ("fixture market data, provider mocks"). The REST/dashboard sweep surface
//! (SRS-API-001 / SRS-UI) and walk-forward reuse (SRS-BT-008) are the adjacent owners.
//!
//! Persistence is deliberately NOT in the runner: the report carries parameters,
//! metrics, and comparison per point, so a caller that wants sweep points in backtest
//! history composes them with the SRS-BT-009 store (`BacktestRecord::from_result`)
//! itself. That keeps the sweep a pure function of its inputs and keeps future BT-008
//! in-sample optimization loops from flooding the persisted history.

use std::error::Error;
use std::fmt;

use crate::backtest::{BacktestEngine, BacktestRequest, BacktestStrategy, BarSource};
use crate::backtest_store::StrategyParameters;
use crate::benchmark::{compare, BenchmarkComparison, BenchmarkSelection, BenchmarkSource};
use crate::metrics::{MetricsConfig, PerformanceMetrics};

/// The default upper bound on a sweep's cardinality. A grid search is a bounded
/// operator workflow, not an unbounded compute job: the product of axis sizes is
/// checked against this cap (via `checked_mul`, so cardinality arithmetic itself can
/// never overflow) BEFORE any point is materialized or any backtest runs.
pub const MAX_SWEEP_POINTS: usize = 10_000;

/// Fail-closed sweep errors. Carries no broker/vendor identifiers (SRS-BT-007).
#[derive(Debug, Clone, PartialEq)]
pub enum SweepError {
    /// The space had no axes. A grid search over nothing is not a parameter space —
    /// an accidental empty definition must not "succeed" with a single empty point.
    EmptySpace,
    /// An axis name was empty / whitespace.
    EmptyAxisName,
    /// Two axes shared a name; the product would be ambiguous.
    DuplicateAxis { name: String },
    /// An axis had no values; the product would be empty by accident.
    EmptyAxisValues { name: String },
    /// An axis value token was empty / whitespace.
    EmptyAxisValue { name: String },
    /// One axis listed the same value twice: it would enumerate two identical
    /// [`StrategyParameters`] points, and identical points make ranking ambiguous.
    DuplicateAxisValue { name: String, value: String },
    /// The space's cardinality exceeded the runner's cap. Detected via `checked_mul`
    /// before materializing a single point (`count` saturates at `u128::MAX` if the
    /// product overflows even `u128` — definitionally over any real cap).
    TooManyPoints { count: u128, limit: usize },
    /// The objective metric token was not one of the eight SYS-16 metric names.
    UnknownMetric { token: String },
    /// The objective direction token was not `max` / `min`.
    UnknownDirection { token: String },
    /// A factory could not build a strategy: the point omitted a required parameter.
    MissingParameter { name: String },
    /// A factory could not build a strategy: the point carried a parameter the
    /// strategy does not declare. Running anyway would silently ignore an axis.
    UnknownParameter { name: String },
    /// A factory could not build a strategy: a parameter value failed to parse or
    /// violated the strategy's domain (e.g. a non-positive lot).
    InvalidParameterValue {
        name: String,
        value: String,
        reason: String,
    },
    /// A point failed to evaluate (factory rejection, engine error, or benchmark
    /// error). The whole sweep aborts, naming the offending point, rather than emit
    /// a partial ranking that could silently mis-rank.
    PointFailed {
        parameters: StrategyParameters,
        reason: String,
    },
    /// An objective value extracted from the metrics was non-finite. The metric
    /// family already guarantees finiteness; this is the sweep's own defense-in-depth
    /// re-check at its trust boundary.
    NonFiniteObjective { metric: ObjectiveMetric },
    /// Canonicalizing a point into [`StrategyParameters`] failed. Unreachable when the
    /// space validated (axis names are unique and non-empty), kept as a fail-closed
    /// mapping rather than an `unwrap`.
    InvalidPoint { reason: String },
}

impl fmt::Display for SweepError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            SweepError::EmptySpace => {
                write!(f, "parameter space has no axes")
            }
            SweepError::EmptyAxisName => write!(f, "axis name is empty"),
            SweepError::DuplicateAxis { name } => {
                write!(f, "duplicate axis '{name}'")
            }
            SweepError::EmptyAxisValues { name } => {
                write!(f, "axis '{name}' has no values")
            }
            SweepError::EmptyAxisValue { name } => {
                write!(f, "axis '{name}' has an empty value token")
            }
            SweepError::DuplicateAxisValue { name, value } => {
                write!(f, "axis '{name}' lists value '{value}' twice")
            }
            SweepError::TooManyPoints { count, limit } => {
                write!(
                    f,
                    "parameter space has {count} points, exceeding the cap of {limit}"
                )
            }
            SweepError::UnknownMetric { token } => {
                write!(
                    f,
                    "unknown objective metric '{token}' (expected one of: {})",
                    ObjectiveMetric::ALL
                        .iter()
                        .map(|metric| metric.as_str())
                        .collect::<Vec<_>>()
                        .join(", ")
                )
            }
            SweepError::UnknownDirection { token } => {
                write!(
                    f,
                    "unknown objective direction '{token}' (expected 'max' or 'min')"
                )
            }
            SweepError::MissingParameter { name } => {
                write!(f, "strategy parameter '{name}' is missing from the point")
            }
            SweepError::UnknownParameter { name } => {
                write!(f, "strategy does not declare parameter '{name}'")
            }
            SweepError::InvalidParameterValue {
                name,
                value,
                reason,
            } => {
                write!(
                    f,
                    "invalid value '{value}' for parameter '{name}': {reason}"
                )
            }
            SweepError::PointFailed { parameters, reason } => {
                let point = parameters
                    .entries()
                    .iter()
                    .map(|(key, value)| format!("{key}={value}"))
                    .collect::<Vec<_>>()
                    .join(", ");
                write!(f, "sweep point [{point}] failed: {reason}")
            }
            SweepError::NonFiniteObjective { metric } => {
                write!(
                    f,
                    "objective metric '{}' produced a non-finite value",
                    metric.as_str()
                )
            }
            SweepError::InvalidPoint { reason } => {
                write!(f, "sweep point could not be canonicalized: {reason}")
            }
        }
    }
}

impl Error for SweepError {}

/// One named dimension of a parameter space: an axis name plus its candidate values in
/// declared order. Values are opaque strings — the same representation
/// [`StrategyParameters`] persists — because the sweep core does not know a strategy's
/// parameter types; the [`SweepStrategyFactory`] is the typed boundary that parses them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParameterAxis {
    name: String,
    values: Vec<String>,
}

impl ParameterAxis {
    /// Build a validated axis, failing closed on an empty/whitespace name, an empty
    /// value list, an empty/whitespace value token, or a duplicate value (which would
    /// enumerate two identical points and make the ranking ambiguous).
    pub fn new(name: impl Into<String>, values: Vec<String>) -> Result<Self, SweepError> {
        let name = name.into();
        if name.trim().is_empty() {
            return Err(SweepError::EmptyAxisName);
        }
        if values.is_empty() {
            return Err(SweepError::EmptyAxisValues { name });
        }
        for value in &values {
            if value.trim().is_empty() {
                return Err(SweepError::EmptyAxisValue { name });
            }
        }
        for (index, value) in values.iter().enumerate() {
            if values[..index].contains(value) {
                return Err(SweepError::DuplicateAxisValue {
                    name,
                    value: value.clone(),
                });
            }
        }
        Ok(Self { name, values })
    }

    /// The axis name.
    pub fn name(&self) -> &str {
        &self.name
    }

    /// The candidate values in declared order.
    pub fn values(&self) -> &[String] {
        &self.values
    }
}

/// The SRS-BT-007 "parameter space definition": validated named axes whose Cartesian
/// product enumerates every parameter combination. Axes are held sorted by name so two
/// definitions of the same space compare and enumerate identically regardless of
/// declaration order — the same canonicalization discipline as [`StrategyParameters`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParameterSpace {
    axes: Vec<ParameterAxis>,
}

impl ParameterSpace {
    /// Build a validated space, failing closed on zero axes or a duplicate axis name.
    /// (Per-axis validation already happened in [`ParameterAxis::new`].)
    pub fn new(axes: Vec<ParameterAxis>) -> Result<Self, SweepError> {
        if axes.is_empty() {
            return Err(SweepError::EmptySpace);
        }
        let mut axes = axes;
        axes.sort_by(|a, b| a.name.cmp(&b.name));
        if let Some(pair) = axes.windows(2).find(|pair| pair[0].name == pair[1].name) {
            return Err(SweepError::DuplicateAxis {
                name: pair[0].name.clone(),
            });
        }
        Ok(Self { axes })
    }

    /// The axes, sorted by name.
    pub fn axes(&self) -> &[ParameterAxis] {
        &self.axes
    }

    /// The space's cardinality: the product of the axis sizes, computed with
    /// `checked_mul` in `u128` so the arithmetic itself can never overflow. A product
    /// beyond even `u128` saturates to `u128::MAX` — definitionally over any real cap.
    pub fn point_count(&self) -> u128 {
        self.axes
            .iter()
            .try_fold(1u128, |product, axis| {
                product.checked_mul(axis.values.len() as u128)
            })
            .unwrap_or(u128::MAX)
    }

    /// Enumerate the full Cartesian product as canonical [`StrategyParameters`] points,
    /// failing closed with [`SweepError::TooManyPoints`] BEFORE materializing anything
    /// if the cardinality exceeds `max_points`. Enumeration is deterministic: axes in
    /// name order, values in declared order, the last (name-ordered) axis varying
    /// fastest — so identical definitions always enumerate identically (SRS-BT-010).
    pub fn points(&self, max_points: usize) -> Result<Vec<StrategyParameters>, SweepError> {
        let count = self.point_count();
        if count > max_points as u128 {
            return Err(SweepError::TooManyPoints {
                count,
                limit: max_points,
            });
        }
        let mut points = Vec::with_capacity(count as usize);
        let mut indices = vec![0usize; self.axes.len()];
        loop {
            let pairs = self
                .axes
                .iter()
                .zip(&indices)
                .map(|(axis, &index)| (axis.name.clone(), axis.values[index].clone()));
            // Axis names are unique and non-empty (validated in new), so canonicalizing
            // cannot fail; the mapping stays fail-closed rather than an unwrap.
            let point =
                StrategyParameters::from_pairs(pairs).map_err(|err| SweepError::InvalidPoint {
                    reason: err.to_string(),
                })?;
            points.push(point);

            // Odometer increment, last axis fastest; carry left. Done when it wraps.
            let mut position = self.axes.len();
            loop {
                if position == 0 {
                    return Ok(points);
                }
                position -= 1;
                indices[position] += 1;
                if indices[position] < self.axes[position].values.len() {
                    break;
                }
                indices[position] = 0;
            }
        }
    }
}

/// The metric an objective ranks by — the full SRS-BT-004 / SYS-16 family. SYS-19
/// names Sharpe and maximum drawdown as examples ("e.g."); the selector is uniform
/// over all eight, and unknown tokens fail closed in [`ObjectiveMetric::parse`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ObjectiveMetric {
    SharpeRatio,
    SortinoRatio,
    Alpha,
    Beta,
    MaxDrawdown,
    AnnualizedReturn,
    AnnualizedVolatility,
    WinRate,
}

impl ObjectiveMetric {
    /// Every selectable metric, in the [`PerformanceMetrics`] field order.
    pub const ALL: [ObjectiveMetric; 8] = [
        ObjectiveMetric::SharpeRatio,
        ObjectiveMetric::SortinoRatio,
        ObjectiveMetric::Alpha,
        ObjectiveMetric::Beta,
        ObjectiveMetric::MaxDrawdown,
        ObjectiveMetric::AnnualizedReturn,
        ObjectiveMetric::AnnualizedVolatility,
        ObjectiveMetric::WinRate,
    ];

    /// The canonical token (the [`PerformanceMetrics`] field name).
    pub fn as_str(&self) -> &'static str {
        match self {
            ObjectiveMetric::SharpeRatio => "sharpe_ratio",
            ObjectiveMetric::SortinoRatio => "sortino_ratio",
            ObjectiveMetric::Alpha => "alpha",
            ObjectiveMetric::Beta => "beta",
            ObjectiveMetric::MaxDrawdown => "max_drawdown",
            ObjectiveMetric::AnnualizedReturn => "annualized_return",
            ObjectiveMetric::AnnualizedVolatility => "annualized_volatility",
            ObjectiveMetric::WinRate => "win_rate",
        }
    }

    /// Parse a canonical token, failing closed on anything outside the allowlist.
    pub fn parse(token: &str) -> Result<Self, SweepError> {
        Self::ALL
            .iter()
            .copied()
            .find(|metric| metric.as_str() == token)
            .ok_or_else(|| SweepError::UnknownMetric {
                token: token.to_string(),
            })
    }

    /// Select this metric's value from a computed [`PerformanceMetrics`]. `None` means
    /// the metric is mathematically undefined for that run (the SRS-BT-004 contract:
    /// undefined is reported honestly, never fabricated) — the sweep routes such points
    /// to [`SweepReport::unranked`].
    pub fn value(&self, metrics: &PerformanceMetrics) -> Option<f64> {
        match self {
            ObjectiveMetric::SharpeRatio => metrics.sharpe_ratio,
            ObjectiveMetric::SortinoRatio => metrics.sortino_ratio,
            ObjectiveMetric::Alpha => metrics.alpha,
            ObjectiveMetric::Beta => metrics.beta,
            ObjectiveMetric::MaxDrawdown => metrics.max_drawdown,
            ObjectiveMetric::AnnualizedReturn => metrics.annualized_return,
            ObjectiveMetric::AnnualizedVolatility => metrics.annualized_volatility,
            ObjectiveMetric::WinRate => metrics.win_rate,
        }
    }
}

/// Whether the objective prefers larger or smaller metric values.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Direction {
    Maximize,
    Minimize,
}

impl Direction {
    /// The canonical token.
    pub fn as_str(&self) -> &'static str {
        match self {
            Direction::Maximize => "max",
            Direction::Minimize => "min",
        }
    }

    /// Parse `max` / `min`, failing closed on anything else. There is deliberately no
    /// per-metric default direction: guessing that (say) drawdown "must" be minimized
    /// would silently invert a ranking when the guess is wrong.
    pub fn parse(token: &str) -> Result<Self, SweepError> {
        match token {
            "max" => Ok(Direction::Maximize),
            "min" => Ok(Direction::Minimize),
            other => Err(SweepError::UnknownDirection {
                token: other.to_string(),
            }),
        }
    }
}

/// The SRS-BT-007 "selected objective function": a metric plus a direction.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ObjectiveFunction {
    pub metric: ObjectiveMetric,
    pub direction: Direction,
}

impl ObjectiveFunction {
    /// The first SYS-19 named example: maximize the Sharpe ratio.
    pub fn maximize_sharpe() -> Self {
        Self {
            metric: ObjectiveMetric::SharpeRatio,
            direction: Direction::Maximize,
        }
    }

    /// The second SYS-19 named example: minimize the maximum drawdown.
    pub fn minimize_max_drawdown() -> Self {
        Self {
            metric: ObjectiveMetric::MaxDrawdown,
            direction: Direction::Minimize,
        }
    }
}

/// The bridge from one [`StrategyParameters`] point to a configured
/// [`BacktestStrategy`] — the seam the deferred Python strategy host will implement
/// (it knows a strategy's declared parameters), and the seam SRS-BT-008 walk-forward
/// reuses per in-sample window.
///
/// `build` MUST fail closed on a missing, unknown, or unparseable parameter rather
/// than fall back to a default: a ranked row silently produced by a default run would
/// misattribute that run's performance to the labeled parameter point.
pub trait SweepStrategyFactory {
    type Strategy: BacktestStrategy;

    /// Build a configured strategy for one parameter point.
    fn build(&self, params: &StrategyParameters) -> Result<Self::Strategy, SweepError>;
}

/// A sweep launch request: the shared launch configuration (one symbol, window, cash,
/// cost model — held constant across the sweep so the parameter axes are the ONLY
/// varying input), the space to search, and the objective to rank by.
#[derive(Debug, Clone, PartialEq)]
pub struct SweepRequest {
    pub base: BacktestRequest,
    pub space: ParameterSpace,
    pub objective: ObjectiveFunction,
}

/// The per-point evaluation dependencies (benchmark selection, benchmark source, and
/// metric configuration), grouped so [`SweepRunner::run`] stays at four arguments.
pub struct SweepEvaluation<'a> {
    pub selection: &'a BenchmarkSelection,
    pub source: &'a dyn BenchmarkSource,
    pub metrics_config: &'a MetricsConfig,
}

/// One ranked sweep result: the point, its 1-based rank, its finite objective value,
/// and the full metric/comparison family a standalone run would report. Only bounded
/// per-point summaries are kept (final equity, trade count) — never the full trade log
/// or equity curve, so a cap-sized sweep's report stays small; a caller that needs a
/// point's full artifacts re-runs that single point (deterministic by SRS-BT-010).
#[derive(Debug, Clone, PartialEq)]
pub struct RankedPoint {
    pub rank: usize,
    pub parameters: StrategyParameters,
    pub objective_value: f64,
    pub metrics: PerformanceMetrics,
    pub comparison: BenchmarkComparison,
    pub final_equity_minor: i64,
    pub trade_count: usize,
}

/// Why a point is reported outside the ranking.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UnrankedReason {
    /// The selected objective metric was mathematically undefined (`None`) for this
    /// point's run. Ranking it anywhere would fabricate an ordering claim.
    ObjectiveUndefined,
}

impl UnrankedReason {
    pub fn as_str(&self) -> &'static str {
        match self {
            UnrankedReason::ObjectiveUndefined => "objective_undefined",
        }
    }
}

/// A point that evaluated successfully but cannot be ranked, with its full metrics so
/// the operator can see WHY (e.g. zero trades → undefined win rate).
#[derive(Debug, Clone, PartialEq)]
pub struct UnrankedPoint {
    pub parameters: StrategyParameters,
    pub metrics: PerformanceMetrics,
    pub comparison: BenchmarkComparison,
    pub reason: UnrankedReason,
}

/// The SRS-BT-007 "ranked backtest results". `total_points` always equals
/// `ranked.len() + unranked.len()`, so every enumerated point is provably accounted
/// for — a sweep can never silently drop a combination.
#[derive(Debug, Clone, PartialEq)]
pub struct SweepReport {
    pub objective: ObjectiveFunction,
    pub total_points: usize,
    /// Best first: ordered by the objective (via `f64::total_cmp`, descending for
    /// [`Direction::Maximize`], ascending for [`Direction::Minimize`]), ties broken by
    /// the points' canonical parameter entries, ranks assigned 1-based by position.
    pub ranked: Vec<RankedPoint>,
    /// Points whose objective was undefined, in enumeration order.
    pub unranked: Vec<UnrankedPoint>,
}

/// The sweep orchestrator: evaluates every point of a [`SweepRequest`] through the
/// shipped [`BacktestEngine`] + [`benchmark::compare`] chain and ranks the results.
pub struct SweepRunner {
    engine: BacktestEngine,
    max_points: usize,
}

impl Default for SweepRunner {
    fn default() -> Self {
        Self::new()
    }
}

impl SweepRunner {
    /// A runner with the default [`MAX_SWEEP_POINTS`] cardinality cap.
    pub fn new() -> Self {
        Self::with_max_points(MAX_SWEEP_POINTS)
    }

    /// A runner with an explicit cardinality cap (test seam, mirroring
    /// `BacktestEngine::with_max_bars`).
    pub fn with_max_points(max_points: usize) -> Self {
        Self {
            engine: BacktestEngine::new(),
            max_points,
        }
    }

    /// Run the full sweep: enumerate the space (cap-checked first), evaluate every
    /// point sequentially in enumeration order, and rank by the selected objective.
    ///
    /// Any per-point failure aborts the whole sweep with the offending point named
    /// ([`SweepError::PointFailed`]); an undefined objective routes the point to
    /// [`SweepReport::unranked`]. Deterministic: no parallelism, RNG, or clock.
    pub fn run<F: SweepStrategyFactory>(
        &self,
        request: &SweepRequest,
        factory: &F,
        bars: &impl BarSource,
        eval: &SweepEvaluation<'_>,
    ) -> Result<SweepReport, SweepError> {
        let points = request.space.points(self.max_points)?;
        let total_points = points.len();

        // (parameters, objective value, metrics, comparison, final equity, trades)
        let mut evaluated: Vec<(
            StrategyParameters,
            f64,
            PerformanceMetrics,
            BenchmarkComparison,
            i64,
            usize,
        )> = Vec::new();
        let mut unranked: Vec<UnrankedPoint> = Vec::new();

        for parameters in points {
            let point_failed = |reason: String| SweepError::PointFailed {
                parameters: parameters.clone(),
                reason,
            };

            let mut strategy = factory
                .build(&parameters)
                .map_err(|err| point_failed(err.to_string()))?;
            let result = self
                .engine
                .run(&request.base, &mut strategy, bars)
                .map_err(|err| point_failed(format!("backtest failed: {err:?}")))?;
            let report = compare(
                request.base.starting_cash_minor,
                result.range,
                &result.equity_curve,
                &result.trade_log,
                eval.selection,
                eval.source,
                eval.metrics_config,
            )
            .map_err(|err| point_failed(format!("benchmark comparison failed: {err:?}")))?;

            match request.objective.metric.value(&report.metrics) {
                Some(value) if !value.is_finite() => {
                    // Defense-in-depth: compare() already guarantees every emitted
                    // ratio is finite; re-check at this trust boundary anyway.
                    return Err(SweepError::NonFiniteObjective {
                        metric: request.objective.metric,
                    });
                }
                Some(value) => evaluated.push((
                    parameters,
                    value,
                    report.metrics,
                    report.comparison,
                    result.final_equity_minor,
                    result.trade_log.len(),
                )),
                None => unranked.push(UnrankedPoint {
                    parameters,
                    metrics: report.metrics,
                    comparison: report.comparison,
                    reason: UnrankedReason::ObjectiveUndefined,
                }),
            }
        }

        // Best first by the selected objective. total_cmp is a total order over f64
        // (no NaN escape hatch), and equal objective values fall through to the
        // points' canonical entries — a strict total order because every point is
        // pairwise distinct — so the ranking is deterministic (SRS-BT-010).
        let direction = request.objective.direction;
        evaluated.sort_by(|a, b| {
            let primary = match direction {
                Direction::Maximize => b.1.total_cmp(&a.1),
                Direction::Minimize => a.1.total_cmp(&b.1),
            };
            primary.then_with(|| a.0.entries().cmp(b.0.entries()))
        });

        let ranked = evaluated
            .into_iter()
            .enumerate()
            .map(
                |(index, (parameters, value, metrics, comparison, final_equity, trades))| {
                    RankedPoint {
                        rank: index + 1,
                        parameters,
                        objective_value: value,
                        metrics,
                        comparison,
                        final_equity_minor: final_equity,
                        trade_count: trades,
                    }
                },
            )
            .collect();

        Ok(SweepReport {
            objective: request.objective,
            total_points,
            ranked,
            unranked,
        })
    }
}
