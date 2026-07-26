//! Walk-forward analysis for backtests (SRS-BT-008 / SyRS SYS-20; StRS SN-1.17).
//!
//! The acceptance criterion is one sentence with three named obligations: "**In-sample
//! windows are optimized**, **out-of-sample windows are evaluated**, and outputs
//! **preserve the parameter set and metrics per window**." Each maps to a type here:
//!
//! - [`WalkForwardSchedule`] — the sequence of [`WalkForwardWindow`] folds. Every window
//!   pairs an in-sample [`DateRange`] with an out-of-sample [`DateRange`], and the schedule
//!   enforces the walk-forward **no-lookahead invariant**: the out-of-sample window lies
//!   strictly after the in-sample window (`in_sample.end < out_of_sample.start`). Leaking
//!   any in-sample bar into the out-of-sample measurement would report performance the
//!   optimizer already saw — a fabricated edge an operator could size capital on — so a
//!   lookahead window fails closed with [`WalkForwardError::LookaheadWindow`].
//! - [`WalkForwardRunner::run`] — the orchestration. For each fold it **optimizes the
//!   in-sample window** by running the shipped SRS-BT-007 grid search over it
//!   ([`SweepRunner::run`]) and taking the rank-1 point, then **evaluates the
//!   out-of-sample window** by re-running that single winning point through the SAME
//!   sweep chain over the out-of-sample range. Reusing [`SweepRunner`] for both phases
//!   means a walk-forward result is exactly what a standalone sweep / backtest of that
//!   point would report — there is no parallel re-implementation of the shipped backtest
//!   engine + benchmark-comparison chain (this module names neither directly; it drives
//!   both only through [`SweepRunner::run`]).
//! - [`WalkForwardFold`] / [`WalkForwardReport`] — the ranked-per-window output: each fold
//!   **preserves the selected parameter set and both windows' metrics** (in-sample and
//!   out-of-sample), and [`WalkForwardReport::total_folds`] equals the number of scheduled
//!   folds so every window is provably accounted for.
//!
//! Fail-closed decisions (each pinned by `srs_bt_008_walk_forward`):
//!
//! - A schedule with no folds, an inverted sub-range, a lookahead window, non-forward
//!   folds, or overlapping out-of-sample windows is rejected before any backtest runs —
//!   a malformed walk-forward must never silently produce a partial or misleading report.
//! - An in-sample window whose every point has an undefined objective yields no rankable
//!   winner, so the fold fails closed with [`WalkForwardError::NoOptimum`] naming the
//!   window rather than silently selecting an unranked point.
//! - The out-of-sample objective is honestly `Option`: `Some` when the metric is defined
//!   on the out-of-sample window, `None` (never a fabricated stand-in) when it is not —
//!   the [`SweepReport`](crate::sweep::SweepReport) unranked contract, preserved.
//! - Any per-window sweep failure aborts the WHOLE walk-forward with the offending window
//!   named ([`WalkForwardError::InSampleSweepFailed`] /
//!   [`WalkForwardError::OutOfSampleEvalFailed`]) — a partial report could mis-rank a
//!   configuration, and the analysis needs all-or-error reproducibility per fold.
//! - Evaluation is deterministic: it reuses the sweep's sequential, no-parallelism / no-RNG
//!   / no-clock evaluation, so identical inputs produce an identical report (SRS-BT-010).
//!
//! The strategy side of the boundary is the SAME [`SweepStrategyFactory`] BT-007 defines
//! (walk-forward is its named downstream consumer): the REAL factory is the deferred
//! Python strategy host; fixture factories realize the seam solo, exactly as the SRS-BT-008
//! verification step specifies ("fixture market data, provider mocks"). The REST/dashboard
//! surface (SRS-API-001 / SRS-UI) and sweep/fold persistence (SRS-BT-009) are adjacent
//! owners; this module deliberately stays a pure function of its inputs.

use std::error::Error;
use std::fmt;

use crate::backtest::{BacktestRequest, BarSource, DateRange};
use crate::backtest_store::StrategyParameters;
use crate::benchmark::BenchmarkComparison;
use crate::metrics::PerformanceMetrics;
use crate::sweep::{
    ObjectiveFunction, ParameterAxis, ParameterSpace, SweepError, SweepEvaluation, SweepRequest,
    SweepRunner, SweepStrategyFactory,
};

/// The default upper bound on a schedule's fold count. A walk-forward analysis is a bounded
/// operator workflow, not an unbounded compute job: an operator-supplied fold count is checked
/// against this cap BEFORE any window is allocated, so a pathological / malicious `count` (e.g.
/// `usize::MAX` from a CLI flag) fails closed with a typed error instead of a capacity-overflow
/// panic or a huge allocation. Mirrors [`crate::sweep::MAX_SWEEP_POINTS`].
pub const MAX_WALK_FORWARD_FOLDS: usize = 10_000;

/// Fail-closed walk-forward errors. Carries no broker/vendor identifiers (SRS-BT-008).
#[derive(Debug, Clone, PartialEq)]
pub enum WalkForwardError {
    /// The schedule had no folds. Walk-forward over nothing is not an analysis.
    EmptySchedule,
    /// A window's in-sample or out-of-sample sub-range was inverted (`start > end`).
    InvalidWindow { start: u64, end: u64 },
    /// A window's out-of-sample range was not strictly after its in-sample range: the
    /// walk-forward no-lookahead invariant. An out-of-sample measurement that overlaps
    /// (or precedes) the in-sample optimization window reports data the optimizer already
    /// saw — a fabricated out-of-sample edge.
    LookaheadWindow {
        in_sample_end: u64,
        out_of_sample_start: u64,
    },
    /// Two consecutive folds did not advance: a fold's in-sample start must be strictly
    /// greater than the previous fold's, or the schedule revisits the same window instead
    /// of marching forward through time.
    NonMonotonicFolds {
        prev_in_sample_start: u64,
        next_in_sample_start: u64,
    },
    /// Two folds' out-of-sample windows overlapped: walk-forward tiles the out-of-sample
    /// period forward without double-counting a bar's performance across folds.
    OverlappingFolds {
        prev_out_of_sample_end: u64,
        next_out_of_sample_start: u64,
    },
    /// Rolling-schedule boundary arithmetic exceeded `u64`. Detected via `checked_add`/
    /// `checked_mul` before any window is built.
    ScheduleOverflow,
    /// A rolling schedule requested a zero-length in-sample or out-of-sample window.
    ZeroLength,
    /// A rolling schedule requested a zero step: consecutive folds would not advance.
    ZeroStep,
    /// A rolling schedule requested zero folds.
    ZeroFolds,
    /// A schedule's fold count exceeded the cap ([`MAX_WALK_FORWARD_FOLDS`]). Detected BEFORE
    /// any window is allocated, so an unbounded operator-supplied `count` can never trigger a
    /// capacity-overflow panic or a huge allocation — it fails closed with this typed error.
    TooManyFolds { count: usize, limit: usize },
    /// An in-sample window produced no rankable point (every enumerated point's objective
    /// was undefined), so there is no optimum to carry into the out-of-sample evaluation.
    /// Fail closed rather than silently select an unranked point.
    NoOptimum { window: WalkForwardWindow },
    /// The in-sample optimization sweep failed for a fold (space, factory, engine, or
    /// benchmark error). The whole walk-forward aborts, naming the window.
    InSampleSweepFailed {
        window: WalkForwardWindow,
        source: SweepError,
    },
    /// The out-of-sample evaluation sweep failed for a fold. The whole walk-forward aborts,
    /// naming the window.
    OutOfSampleEvalFailed {
        window: WalkForwardWindow,
        source: SweepError,
    },
    /// Rebuilding the single-point parameter space for the out-of-sample evaluation failed.
    /// Unreachable when the in-sample winner is a valid canonical point (non-empty,
    /// uniquely-named, non-empty-valued entries); kept as a fail-closed mapping rather than
    /// an `unwrap`, and used if the singleton evaluation somehow produced no point.
    SingletonRebuild { reason: String },
}

impl fmt::Display for WalkForwardError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            WalkForwardError::EmptySchedule => write!(f, "walk-forward schedule has no folds"),
            WalkForwardError::InvalidWindow { start, end } => {
                write!(f, "invalid walk-forward window: start {start} > end {end}")
            }
            WalkForwardError::LookaheadWindow {
                in_sample_end,
                out_of_sample_start,
            } => write!(
                f,
                "lookahead window: out-of-sample start {out_of_sample_start} is not strictly \
                 after in-sample end {in_sample_end}"
            ),
            WalkForwardError::NonMonotonicFolds {
                prev_in_sample_start,
                next_in_sample_start,
            } => write!(
                f,
                "non-monotonic folds: in-sample start {next_in_sample_start} does not advance \
                 past {prev_in_sample_start}"
            ),
            WalkForwardError::OverlappingFolds {
                prev_out_of_sample_end,
                next_out_of_sample_start,
            } => write!(
                f,
                "overlapping folds: out-of-sample start {next_out_of_sample_start} overlaps the \
                 prior fold's out-of-sample end {prev_out_of_sample_end}"
            ),
            WalkForwardError::ScheduleOverflow => {
                write!(f, "walk-forward schedule bounds overflowed u64")
            }
            WalkForwardError::ZeroLength => {
                write!(f, "walk-forward window length must be positive")
            }
            WalkForwardError::ZeroStep => write!(f, "walk-forward step must be positive"),
            WalkForwardError::ZeroFolds => write!(f, "walk-forward fold count must be positive"),
            WalkForwardError::TooManyFolds { count, limit } => write!(
                f,
                "walk-forward schedule has {count} folds, exceeding the cap of {limit}"
            ),
            WalkForwardError::NoOptimum { window } => write!(
                f,
                "walk-forward fold {} produced no rankable in-sample optimum (every point's \
                 objective was undefined)",
                fmt_window(window)
            ),
            WalkForwardError::InSampleSweepFailed { window, source } => write!(
                f,
                "walk-forward fold {} in-sample optimization failed: {source}",
                fmt_window(window)
            ),
            WalkForwardError::OutOfSampleEvalFailed { window, source } => write!(
                f,
                "walk-forward fold {} out-of-sample evaluation failed: {source}",
                fmt_window(window)
            ),
            WalkForwardError::SingletonRebuild { reason } => {
                write!(f, "out-of-sample single-point evaluation failed: {reason}")
            }
        }
    }
}

impl Error for WalkForwardError {}

fn fmt_window(window: &WalkForwardWindow) -> String {
    format!(
        "in_sample=[{},{}] out_of_sample=[{},{}]",
        window.in_sample.start,
        window.in_sample.end,
        window.out_of_sample.start,
        window.out_of_sample.end,
    )
}

/// One walk-forward fold: an in-sample optimization window paired with the out-of-sample
/// evaluation window that immediately follows it.
///
/// The fields are `pub` for read access, but the safety gate is [`WalkForwardSchedule::new`],
/// which re-validates every window regardless of how it was constructed — so an unvalidated
/// window can never reach [`WalkForwardRunner::run`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WalkForwardWindow {
    pub in_sample: DateRange,
    pub out_of_sample: DateRange,
}

impl WalkForwardWindow {
    /// Build a validated window, failing closed on an inverted sub-range or a lookahead
    /// window (out-of-sample not strictly after in-sample).
    pub fn new(in_sample: DateRange, out_of_sample: DateRange) -> Result<Self, WalkForwardError> {
        let window = Self {
            in_sample,
            out_of_sample,
        };
        window.validate()?;
        Ok(window)
    }

    /// Fail closed on an inverted in-sample / out-of-sample sub-range, or on a lookahead
    /// window (`in_sample.end >= out_of_sample.start`). A single-instant sub-range
    /// (`start == end`) is valid; the two windows abutting (`in_sample.end + 1 ==
    /// out_of_sample.start`) is valid — only overlap or precedence is a lookahead.
    pub fn validate(&self) -> Result<(), WalkForwardError> {
        for range in [self.in_sample, self.out_of_sample] {
            if range.start > range.end {
                return Err(WalkForwardError::InvalidWindow {
                    start: range.start,
                    end: range.end,
                });
            }
        }
        if self.in_sample.end >= self.out_of_sample.start {
            return Err(WalkForwardError::LookaheadWindow {
                in_sample_end: self.in_sample.end,
                out_of_sample_start: self.out_of_sample.start,
            });
        }
        Ok(())
    }
}

/// The walk-forward schedule: the ordered sequence of folds marched forward through time.
/// Held as validated windows so the runner consumes only a well-formed, no-lookahead,
/// forward-tiling schedule.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WalkForwardSchedule {
    windows: Vec<WalkForwardWindow>,
}

impl WalkForwardSchedule {
    /// Build a validated schedule, failing closed on an empty schedule, any invalid /
    /// lookahead window, non-monotonic folds (a fold whose in-sample start does not advance
    /// past the previous fold's), or overlapping out-of-sample windows (walk-forward tiles
    /// the out-of-sample period forward without double-counting a bar across folds).
    pub fn new(windows: Vec<WalkForwardWindow>) -> Result<Self, WalkForwardError> {
        if windows.is_empty() {
            return Err(WalkForwardError::EmptySchedule);
        }
        if windows.len() > MAX_WALK_FORWARD_FOLDS {
            return Err(WalkForwardError::TooManyFolds {
                count: windows.len(),
                limit: MAX_WALK_FORWARD_FOLDS,
            });
        }
        for window in &windows {
            window.validate()?;
        }
        for pair in windows.windows(2) {
            let (prev, next) = (&pair[0], &pair[1]);
            if next.in_sample.start <= prev.in_sample.start {
                return Err(WalkForwardError::NonMonotonicFolds {
                    prev_in_sample_start: prev.in_sample.start,
                    next_in_sample_start: next.in_sample.start,
                });
            }
            if next.out_of_sample.start <= prev.out_of_sample.end {
                return Err(WalkForwardError::OverlappingFolds {
                    prev_out_of_sample_end: prev.out_of_sample.end,
                    next_out_of_sample_start: next.out_of_sample.start,
                });
            }
        }
        Ok(Self { windows })
    }

    /// Build the canonical rolling schedule: `fold_count` folds, each with an in-sample
    /// window of `in_sample_len` timestamps beginning at `start + fold * step`, immediately
    /// followed by an out-of-sample window of `out_of_sample_len` timestamps. All boundary
    /// arithmetic is overflow-checked (fail closed [`WalkForwardError::ScheduleOverflow`]),
    /// and a zero length / step / fold_count fails closed. The generated windows funnel
    /// through [`WalkForwardSchedule::new`], so a config that would produce overlapping
    /// out-of-sample windows (`step < out_of_sample_len`) is rejected by the one rule set.
    pub fn rolling(
        start: u64,
        in_sample_len: u64,
        out_of_sample_len: u64,
        step: u64,
        fold_count: usize,
    ) -> Result<Self, WalkForwardError> {
        if in_sample_len == 0 || out_of_sample_len == 0 {
            return Err(WalkForwardError::ZeroLength);
        }
        if step == 0 {
            return Err(WalkForwardError::ZeroStep);
        }
        if fold_count == 0 {
            return Err(WalkForwardError::ZeroFolds);
        }
        // Bound the fold count BEFORE allocating: an unbounded operator-supplied `fold_count`
        // (e.g. `usize::MAX` parsed from a CLI flag) must fail closed with a typed error, never
        // panic on `Vec::with_capacity` capacity overflow or attempt a huge allocation.
        if fold_count > MAX_WALK_FORWARD_FOLDS {
            return Err(WalkForwardError::TooManyFolds {
                count: fold_count,
                limit: MAX_WALK_FORWARD_FOLDS,
            });
        }
        let mut windows = Vec::with_capacity(fold_count);
        for fold in 0..fold_count {
            let offset = (fold as u64)
                .checked_mul(step)
                .ok_or(WalkForwardError::ScheduleOverflow)?;
            let is_start = start
                .checked_add(offset)
                .ok_or(WalkForwardError::ScheduleOverflow)?;
            // Inclusive windows: a length-N window spans [s, s + N - 1]. The zero-length
            // guard above makes `len - 1` safe.
            let is_end = is_start
                .checked_add(in_sample_len - 1)
                .ok_or(WalkForwardError::ScheduleOverflow)?;
            let oos_start = is_end
                .checked_add(1)
                .ok_or(WalkForwardError::ScheduleOverflow)?;
            let oos_end = oos_start
                .checked_add(out_of_sample_len - 1)
                .ok_or(WalkForwardError::ScheduleOverflow)?;
            windows.push(WalkForwardWindow::new(
                DateRange::new(is_start, is_end),
                DateRange::new(oos_start, oos_end),
            )?);
        }
        Self::new(windows)
    }

    /// The folds, in forward-marching order.
    pub fn windows(&self) -> &[WalkForwardWindow] {
        &self.windows
    }
}

/// A walk-forward launch request: the shared launch configuration (symbol, cash, cost
/// model — held constant across every fold so the only varying inputs are the window and
/// the parameter point), the parameter space to optimize over in-sample, the objective to
/// optimize and evaluate by, and the fold schedule.
#[derive(Debug, Clone, PartialEq)]
pub struct WalkForwardRequest {
    pub base: BacktestRequest,
    pub space: ParameterSpace,
    pub objective: ObjectiveFunction,
    pub schedule: WalkForwardSchedule,
}

/// One completed fold's result: the window, the in-sample-optimized parameter set, and both
/// windows' full metric families — so the output "preserves the parameter set and metrics
/// per window" (the SRS-BT-008 acceptance).
///
/// The in-sample objective is always defined (it is the rank-1 winner's objective value);
/// the out-of-sample objective is honestly `Option` — `None` when the selected metric is
/// mathematically undefined on the out-of-sample window (never a fabricated stand-in).
#[derive(Debug, Clone, PartialEq)]
pub struct WalkForwardFold {
    pub window: WalkForwardWindow,
    pub selected_parameters: StrategyParameters,
    pub in_sample_objective: f64,
    pub in_sample_metrics: PerformanceMetrics,
    pub in_sample_comparison: BenchmarkComparison,
    pub out_of_sample_objective: Option<f64>,
    pub out_of_sample_metrics: PerformanceMetrics,
    pub out_of_sample_comparison: BenchmarkComparison,
}

/// The SRS-BT-008 walk-forward output: one [`WalkForwardFold`] per scheduled window, in
/// forward order. `total_folds` equals `folds.len()`, so every scheduled fold is provably
/// accounted for — the analysis can never silently drop a window (any per-window failure
/// aborts the whole run instead).
#[derive(Debug, Clone, PartialEq)]
pub struct WalkForwardReport {
    pub objective: ObjectiveFunction,
    pub total_folds: usize,
    pub folds: Vec<WalkForwardFold>,
}

/// The walk-forward orchestrator. It owns an internal [`SweepRunner`] and evaluates every
/// fold through it — the in-sample optimization is a full grid search over the in-sample
/// window, and the out-of-sample evaluation is that same sweep chain run over the single
/// winning point on the out-of-sample window. There is no separate engine.
pub struct WalkForwardRunner {
    sweep: SweepRunner,
}

impl Default for WalkForwardRunner {
    fn default() -> Self {
        Self::new()
    }
}

impl WalkForwardRunner {
    /// A runner over a default [`SweepRunner`] (the default cardinality cap).
    pub fn new() -> Self {
        Self {
            sweep: SweepRunner::new(),
        }
    }

    /// A runner whose in-sample sweeps use an explicit cardinality cap (test seam, mirroring
    /// [`SweepRunner::with_max_points`]).
    pub fn with_max_points(max_points: usize) -> Self {
        Self {
            sweep: SweepRunner::with_max_points(max_points),
        }
    }

    /// Run the full walk-forward analysis: for each fold, optimize the in-sample window and
    /// evaluate the winning parameter set out-of-sample, preserving both windows' metrics.
    ///
    /// Any per-fold failure aborts the whole analysis with the offending window named; an
    /// in-sample window with no rankable optimum fails closed ([`WalkForwardError::NoOptimum`]).
    /// Deterministic: it reuses the sweep's sequential, no-parallelism / no-RNG / no-clock
    /// evaluation, so identical inputs produce an identical report (SRS-BT-010).
    pub fn run<F: SweepStrategyFactory>(
        &self,
        request: &WalkForwardRequest,
        factory: &F,
        bars: &impl BarSource,
        eval: &SweepEvaluation<'_>,
    ) -> Result<WalkForwardReport, WalkForwardError> {
        let windows = request.schedule.windows();
        let total_folds = windows.len();
        let mut folds: Vec<WalkForwardFold> = Vec::with_capacity(total_folds);

        for window in windows {
            // ---- in-sample optimization: a full grid search over the in-sample window ----
            let in_sample_request = SweepRequest {
                base: BacktestRequest {
                    range: window.in_sample,
                    ..request.base.clone()
                },
                space: request.space.clone(),
                objective: request.objective,
            };
            let in_report = self
                .sweep
                .run(&in_sample_request, factory, bars, eval)
                .map_err(|source| WalkForwardError::InSampleSweepFailed {
                    window: *window,
                    source,
                })?;
            // Rank 1 is the optimized point. An empty ranking means every in-sample point's
            // objective was undefined — no optimum to carry forward; fail closed rather than
            // reach into the unranked bucket.
            let best = match in_report.ranked.first() {
                Some(point) => point,
                None => return Err(WalkForwardError::NoOptimum { window: *window }),
            };

            // ---- out-of-sample evaluation: the SAME sweep chain over the single winner ----
            // Rebuilding a one-point space from the winner and running it through
            // SweepRunner keeps the out-of-sample number exactly what a standalone backtest
            // of that point would report — no parallel evaluation path.
            let singleton = singleton_space(&best.parameters)?;
            let oos_request = SweepRequest {
                base: BacktestRequest {
                    range: window.out_of_sample,
                    ..request.base.clone()
                },
                space: singleton,
                objective: request.objective,
            };
            let oos_report =
                self.sweep
                    .run(&oos_request, factory, bars, eval)
                    .map_err(|source| WalkForwardError::OutOfSampleEvalFailed {
                        window: *window,
                        source,
                    })?;

            // A singleton space enumerates exactly one point, which the sweep routes to
            // either `ranked` (objective defined) or `unranked` (objective undefined) — never
            // both, never neither. Preserve the out-of-sample objective honestly: `Some` when
            // defined, `None` when undefined, never a fabricated stand-in.
            let (out_of_sample_objective, out_of_sample_metrics, out_of_sample_comparison) =
                if let Some(point) = oos_report.ranked.first() {
                    (
                        Some(point.objective_value),
                        point.metrics.clone(),
                        point.comparison.clone(),
                    )
                } else if let Some(point) = oos_report.unranked.first() {
                    (None, point.metrics.clone(), point.comparison.clone())
                } else {
                    return Err(WalkForwardError::SingletonRebuild {
                        reason: "out-of-sample single-point sweep produced no evaluated point"
                            .to_string(),
                    });
                };

            folds.push(WalkForwardFold {
                window: *window,
                selected_parameters: best.parameters.clone(),
                in_sample_objective: best.objective_value,
                in_sample_metrics: best.metrics.clone(),
                in_sample_comparison: best.comparison.clone(),
                out_of_sample_objective,
                out_of_sample_metrics,
                out_of_sample_comparison,
            });
        }

        Ok(WalkForwardReport {
            objective: request.objective,
            total_folds,
            folds,
        })
    }
}

/// Rebuild a single-point [`ParameterSpace`] from a winning point's canonical entries — one
/// axis per entry, each carrying the single winning value — so the out-of-sample evaluation
/// runs the identical sweep chain over exactly that point. Fails closed
/// ([`WalkForwardError::SingletonRebuild`]) rather than `unwrap`: a point that just ranked
/// in-sample has non-empty, uniquely-named, non-empty-valued entries, so the axis / space
/// validation cannot legitimately fail — but if it somehow does, that surfaces as an error,
/// never a panic.
fn singleton_space(point: &StrategyParameters) -> Result<ParameterSpace, WalkForwardError> {
    let mut axes = Vec::with_capacity(point.entries().len());
    for (key, value) in point.entries() {
        let axis = ParameterAxis::new(key.clone(), vec![value.clone()]).map_err(|source| {
            WalkForwardError::SingletonRebuild {
                reason: format!("axis '{key}': {source}"),
            }
        })?;
        axes.push(axis);
    }
    ParameterSpace::new(axes).map_err(|source| WalkForwardError::SingletonRebuild {
        reason: source.to_string(),
    })
}
