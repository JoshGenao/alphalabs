//! Deterministic-backtest **verification** surface for **SRS-BT-010** — "produce
//! deterministic backtest results for identical inputs" (SyRS SYS-62; StRS SN-1.02).
//!
//! # Why this module exists
//!
//! The [`BacktestEngine`](crate::backtest::BacktestEngine) is already deterministic *by
//! construction* — it stable-sorts the replay window and does all money math in integer
//! minor units (`backtest.rs`). Every sibling numeric module (metrics, benchmark, cost,
//! store, factor) likewise *claims* determinism. What was missing is a way to make that
//! guarantee **falsifiable**: a canonical, comparable fingerprint of a completed run, and a
//! harness that runs a backtest twice and *localizes* any divergence. That is exactly the
//! SRS-BT-010 acceptance test — "repeated runs … produce identical trade logs, equity
//! curves, and metrics" — turned into code.
//!
//! # What is real here vs deferred
//!
//! This module is genuinely runnable in-process. [`digest_result`] / [`digest_run`] reduce a
//! [`BacktestResult`](crate::backtest::BacktestResult) (and an optional
//! [`PerformanceMetrics`](crate::metrics::PerformanceMetrics)) to a stable [`RunDigest`];
//! [`runs_match`] / [`metrics_match`] localize the first divergent artifact; and
//! [`verify_reproducible`] runs the engine twice over identical inputs and returns the run's
//! digest, failing closed with a localized [`DeterminismError`] if the two runs disagree.
//!
//! The **full** SRS-BT-010 guarantee — two identical runs producing identical results *under
//! the real Python strategy host* (the Rust↔Python boundary) and the operator repeated-run
//! workflow (POST `/api/v1/backtests` run twice → identical) — is deferred (it is recorded in
//! `architecture/runtime_services.json#backtest_determinism_contract.deferred`). So
//! `feature_list.json`
//! keeps SRS-BT-010 `passes:false`; this slice ships the in-process determinism *contract*.
//!
//! # The encoding boundary (why this is not a money-correctness leak)
//!
//! The result digest is **integer-exact**: it folds the trade log and equity curve as their
//! `i64` minor-unit fields, so there is no float-formatting nondeterminism in the money path.
//! The only `f64` in the picture is the *dimensionless metric ratios*, and those are folded
//! through their exact [`f64::to_bits`] payload (the same doctrine `metrics.rs` and
//! `backtest_store.rs` use) — a bit-identical comparison, never a lexical float compare. The
//! digest is computed with a fixed left-to-right byte fold, no parallelism, no random values,
//! and no wall-clock read, so the verifier itself honors the property it checks.

use std::fmt;

use crate::backtest::{
    BacktestEngine, BacktestError, BacktestRequest, BacktestResult, BacktestStrategy, BarSource,
};
use crate::metrics::{MetricsError, PerformanceMetrics};

/// Domain tag mixed into every run digest so a [`RunDigest`] can never collide with another
/// FNV-1a checksum in the crate (e.g. the `backtest_store` blob checksum, which uses a
/// different magic). Bumping [`DIGEST_SCHEMA_VERSION`] re-keys every digest.
const DIGEST_MAGIC: &str = "ATP-BACKTEST-RUN-DIGEST";

/// Version of the canonical digest body. Any change to the field set or ordering below must
/// bump this so a stored digest from an older layout is never silently compared against a new
/// one (it would simply differ, which is the honest answer).
pub const DIGEST_SCHEMA_VERSION: i64 = 1;

/// An opaque, stable fingerprint of one completed backtest run.
///
/// Two runs with identical observable output (trade log, equity curve, and — via
/// [`digest_run`] — metrics) produce the same `RunDigest`; any difference produces a
/// different one. It is a **non-cryptographic** 64-bit FNV-1a checksum: it detects accidental
/// divergence (a reordered fold, a changed value, a dropped fill), not a deliberate
/// digest-recomputing tamperer — defending against that needs a keyed MAC, out of scope for
/// the single-user, local-only baseline. Reproducibility, not authentication.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct RunDigest(u64);

impl RunDigest {
    /// The raw 64-bit digest value (for storage alongside a persisted result, SRS-BT-009).
    pub fn value(self) -> u64 {
        self.0
    }
}

impl fmt::Display for RunDigest {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "run-digest:{:016x}", self.0)
    }
}

/// A localized determinism failure. Each variant names *where* two runs (or two metric sets)
/// first diverged, so a nondeterminism source is pinpointed rather than reported as an opaque
/// "not reproducible". Carries no broker/vendor identifiers.
///
/// `Eq` is intentionally not derived: [`MetricsComputation`](Self::MetricsComputation) wraps a
/// [`MetricsError`], which carries an `f64` (so it is `PartialEq` but not `Eq`). `PartialEq` is
/// all the tests and callers need.
#[derive(Debug, Clone, PartialEq)]
pub enum DeterminismError {
    /// One of the two replays failed to complete (the engine returned a [`BacktestError`]).
    /// The harness cannot fingerprint a run that did not finish, so it fails closed rather
    /// than reporting a partial run reproducible.
    Run(BacktestError),
    /// The two results name different data sources (different catalogs). Part of a result's
    /// provenance, so [`runs_match`] rejects it even when the trades and equity coincide.
    DataSource,
    /// The two results cover different date ranges. Provenance, like [`Self::DataSource`].
    Range,
    /// The two runs marked a different number of bars processed.
    BarsProcessed { left: u64, right: u64 },
    /// The two trade logs had different lengths (a fill present in one run, absent in the other).
    TradeLogLength { left: usize, right: usize },
    /// The two trade logs first diverged at this fill index.
    TradeLog { index: usize },
    /// The two equity curves had different lengths.
    EquityCurveLength { left: usize, right: usize },
    /// The two equity curves first diverged at this mark index.
    EquityCurve { index: usize },
    /// The two runs reported a different final equity.
    FinalEquity { left: i64, right: i64 },
    /// The two metric sets first diverged at this named metric (compared via exact `to_bits`,
    /// so a `+0.0`/`-0.0` or `NaN`-bit difference is caught, never smoothed over).
    Metrics { metric: &'static str },
    /// Computing the metrics for a (successfully completed) run failed. The metrics-aware harness
    /// fails closed rather than silently skipping the metric clause of the SRS-BT-010 criterion
    /// when the metric family cannot be produced for the run.
    MetricsComputation(MetricsError),
    /// The structural comparison passed but the canonical digests still differ. Unreachable
    /// while [`runs_match`] covers every [`BacktestResult`] field; it exists so a field added
    /// to `BacktestResult` without updating `runs_match` is caught here rather than silently
    /// reported reproducible.
    Digest,
}

impl From<BacktestError> for DeterminismError {
    fn from(error: BacktestError) -> Self {
        Self::Run(error)
    }
}

impl From<MetricsError> for DeterminismError {
    fn from(error: MetricsError) -> Self {
        Self::MetricsComputation(error)
    }
}

impl fmt::Display for DeterminismError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Run(error) => write!(f, "a backtest replay failed to complete: {error}"),
            Self::DataSource => write!(f, "nondeterministic data source (different catalogs)"),
            Self::Range => write!(f, "nondeterministic date range"),
            Self::BarsProcessed { left, right } => write!(
                f,
                "nondeterministic bars_processed: {left} vs {right}"
            ),
            Self::TradeLogLength { left, right } => write!(
                f,
                "nondeterministic trade-log length: {left} vs {right} fills"
            ),
            Self::TradeLog { index } => {
                write!(f, "nondeterministic trade log: fills differ at index {index}")
            }
            Self::EquityCurveLength { left, right } => write!(
                f,
                "nondeterministic equity-curve length: {left} vs {right} marks"
            ),
            Self::EquityCurve { index } => {
                write!(f, "nondeterministic equity curve: marks differ at index {index}")
            }
            Self::FinalEquity { left, right } => {
                write!(f, "nondeterministic final equity: {left} vs {right} minor units")
            }
            Self::Metrics { metric } => {
                write!(f, "nondeterministic metric `{metric}`")
            }
            Self::MetricsComputation(error) => {
                // `MetricsError` does not implement `Display`, so render its `Debug` form rather
                // than reach into `metrics.rs` to add one (out of scope for this slice).
                write!(
                    f,
                    "metrics could not be computed for a completed run: {error:?}"
                )
            }
            Self::Digest => write!(
                f,
                "run digests differ though every compared field matched (a field is missing from runs_match)"
            ),
        }
    }
}

impl std::error::Error for DeterminismError {}

/// Fingerprint a completed run from its [`BacktestResult`] alone (trade log + equity curve +
/// provenance). Integer-exact: no `f64` enters this digest. Equal to `digest_run(result, None)`.
pub fn digest_result(result: &BacktestResult) -> RunDigest {
    digest_run(result, None)
}

/// Fingerprint a completed run, optionally bundling its [`PerformanceMetrics`] so the digest
/// spans all three artifacts SRS-BT-010 names (trade logs, equity curves, *and* metrics).
///
/// The body is prefix-free (strings and counts are length-prefixed) and carries a
/// `has_metrics` flag, so a run *with* metrics can never alias a run *without* them, and two
/// structurally different runs cannot encode to the same bytes.
pub fn digest_run(result: &BacktestResult, metrics: Option<&PerformanceMetrics>) -> RunDigest {
    let mut body = String::new();
    push_line(&mut body, DIGEST_MAGIC);
    push_i128(&mut body, i128::from(DIGEST_SCHEMA_VERSION));
    push_bool(&mut body, metrics.is_some());
    encode_result_body(&mut body, result);
    if let Some(metrics) = metrics {
        encode_metrics_body(&mut body, metrics);
    }
    RunDigest(checksum(body.as_bytes()))
}

/// Compare two backtest results field-by-field, returning the *first* divergence as a
/// localized [`DeterminismError`]. `Ok(())` means the two runs are bit-identical across every
/// observable output. Pure, order-stable, allocation-free.
pub fn runs_match(left: &BacktestResult, right: &BacktestResult) -> Result<(), DeterminismError> {
    // Provenance first: two results from different catalogs or date ranges are not the same run,
    // even if their trades and equity happen to coincide.
    if left.data_source != right.data_source {
        return Err(DeterminismError::DataSource);
    }
    if left.range != right.range {
        return Err(DeterminismError::Range);
    }
    if left.bars_processed != right.bars_processed {
        return Err(DeterminismError::BarsProcessed {
            left: left.bars_processed,
            right: right.bars_processed,
        });
    }
    if left.trade_log.len() != right.trade_log.len() {
        return Err(DeterminismError::TradeLogLength {
            left: left.trade_log.len(),
            right: right.trade_log.len(),
        });
    }
    for (index, (a, b)) in left.trade_log.iter().zip(&right.trade_log).enumerate() {
        if a != b {
            return Err(DeterminismError::TradeLog { index });
        }
    }
    if left.equity_curve.len() != right.equity_curve.len() {
        return Err(DeterminismError::EquityCurveLength {
            left: left.equity_curve.len(),
            right: right.equity_curve.len(),
        });
    }
    for (index, (a, b)) in left
        .equity_curve
        .iter()
        .zip(&right.equity_curve)
        .enumerate()
    {
        if a != b {
            return Err(DeterminismError::EquityCurve { index });
        }
    }
    if left.final_equity_minor != right.final_equity_minor {
        return Err(DeterminismError::FinalEquity {
            left: left.final_equity_minor,
            right: right.final_equity_minor,
        });
    }
    Ok(())
}

/// Compare two metric sets via exact [`f64::to_bits`], returning the first divergent metric.
/// Bit-level so `+0.0`/`-0.0` and differing `NaN` payloads are caught — the determinism
/// criterion demands identical bits, not merely numerically-equal floats.
pub fn metrics_match(
    left: &PerformanceMetrics,
    right: &PerformanceMetrics,
) -> Result<(), DeterminismError> {
    for (metric, a, b) in [
        ("sharpe_ratio", left.sharpe_ratio, right.sharpe_ratio),
        ("sortino_ratio", left.sortino_ratio, right.sortino_ratio),
        ("alpha", left.alpha, right.alpha),
        ("beta", left.beta, right.beta),
        ("max_drawdown", left.max_drawdown, right.max_drawdown),
        (
            "annualized_return",
            left.annualized_return,
            right.annualized_return,
        ),
        (
            "annualized_volatility",
            left.annualized_volatility,
            right.annualized_volatility,
        ),
        ("win_rate", left.win_rate, right.win_rate),
    ] {
        if opt_bits(a) != opt_bits(b) {
            return Err(DeterminismError::Metrics { metric });
        }
    }
    if left.benchmark_symbol != right.benchmark_symbol {
        return Err(DeterminismError::Metrics {
            metric: "benchmark_symbol",
        });
    }
    Ok(())
}

/// Run `engine` over `source` for `request` **twice** — building a fresh strategy from
/// `build_strategy` each time, since [`BacktestStrategy`] is driven by `&mut` — and verify the
/// two replays produced bit-identical [`BacktestResult`]s, returning the pair.
///
/// `runs_match` localizes the first divergent artifact; a replay that does not complete surfaces
/// as [`DeterminismError::Run`]. Both reproducibility harnesses build on this.
fn run_pair<S, B, F>(
    engine: &BacktestEngine,
    request: &BacktestRequest,
    mut build_strategy: F,
    source: &B,
) -> Result<(BacktestResult, BacktestResult), DeterminismError>
where
    S: BacktestStrategy,
    B: BarSource,
    F: FnMut() -> S,
{
    let mut first = build_strategy();
    let result_a = engine.run(request, &mut first, source)?;
    let mut second = build_strategy();
    let result_b = engine.run(request, &mut second, source)?;
    runs_match(&result_a, &result_b)?;
    Ok((result_a, result_b))
}

/// Verify the **trade log + equity curve** reproduce: run `engine` over `source` for `request`
/// twice over identical inputs and return the run's [`RunDigest`] on success.
///
/// A strategy that consults cross-run mutable state (or any other nondeterminism source) makes
/// the two runs disagree, and the harness fails closed with a localized [`DeterminismError`]
/// naming the first divergent artifact; a replay that does not complete surfaces as
/// [`DeterminismError::Run`]. This covers two of the three artifacts SRS-BT-010 names — use
/// [`verify_reproducible_with_metrics`] for the full trade-log + equity-curve + **metrics** check.
pub fn verify_reproducible<S, B, F>(
    engine: &BacktestEngine,
    request: &BacktestRequest,
    build_strategy: F,
    source: &B,
) -> Result<RunDigest, DeterminismError>
where
    S: BacktestStrategy,
    B: BarSource,
    F: FnMut() -> S,
{
    let (result_a, result_b) = run_pair(engine, request, build_strategy, source)?;
    let digest = digest_result(&result_a);
    if digest != digest_result(&result_b) {
        return Err(DeterminismError::Digest);
    }
    Ok(digest)
}

/// Verify all **three** artifacts SRS-BT-010 names — trade log, equity curve, **and metrics** —
/// reproduce: run `engine` twice over identical inputs, compute the [`PerformanceMetrics`] for
/// each completed run via `compute_metrics`, and return the [`digest_run`] fingerprint spanning
/// all three on success.
///
/// This is the full SRS-BT-010 acceptance test in code. `compute_metrics` is the caller-supplied
/// reduction from a run to its metric family (typically a closure over [`metrics::compute`](crate::metrics::compute)
/// with a fixed benchmark + config); it is run for **both** replays and the results compared via
/// [`metrics_match`], so a nondeterministic metric reduction is caught even when the underlying
/// `BacktestResult` is identical. The harness fails closed: a divergent metric surfaces as
/// [`DeterminismError::Metrics`], and a metric family that cannot be computed for a completed run
/// surfaces as [`DeterminismError::MetricsComputation`] rather than silently dropping the metric
/// clause. The returned digest cross-checks both runs ([`DeterminismError::Digest`]).
///
/// The two replays are **interleaved** — run A, then compute metrics A, *then* run B, then
/// compute metrics B — rather than running both backtests first. This matters when the metric
/// reduction observes state the backtest run can mutate (a shared benchmark/config/provider): if
/// both metric sets were computed only after both runs finished, they would both observe the
/// second run's final state and falsely match. Interleaving makes each metric set observe the
/// state as of *its own* run, so a run-induced metric state change is caught, not masked.
pub fn verify_reproducible_with_metrics<S, B, F, M>(
    engine: &BacktestEngine,
    request: &BacktestRequest,
    mut build_strategy: F,
    source: &B,
    compute_metrics: M,
) -> Result<RunDigest, DeterminismError>
where
    S: BacktestStrategy,
    B: BarSource,
    F: FnMut() -> S,
    M: Fn(&BacktestResult) -> Result<PerformanceMetrics, MetricsError>,
{
    let mut first = build_strategy();
    let result_a = engine.run(request, &mut first, source)?;
    let metrics_a = compute_metrics(&result_a)?;
    let mut second = build_strategy();
    let result_b = engine.run(request, &mut second, source)?;
    let metrics_b = compute_metrics(&result_b)?;

    runs_match(&result_a, &result_b)?;
    metrics_match(&metrics_a, &metrics_b)?;
    let digest = digest_run(&result_a, Some(&metrics_a));
    if digest != digest_run(&result_b, Some(&metrics_b)) {
        return Err(DeterminismError::Digest);
    }
    Ok(digest)
}

// --------------------------------------------------------------------------- //
// Canonical body encoders
// --------------------------------------------------------------------------- //

/// Encode the integer-exact result body: provenance, the trade log, and the equity curve.
/// Every field is pushed as an exact integer (or a length-prefixed symbol) — **no `f64`** —
/// so the money path carries no float-formatting nondeterminism.
fn encode_result_body(out: &mut String, result: &BacktestResult) {
    push_str(out, result.data_source.as_str());
    push_i128(out, i128::from(result.range.start));
    push_i128(out, i128::from(result.range.end));
    push_i128(out, i128::from(result.bars_processed));
    push_count(out, result.trade_log.len());
    for fill in &result.trade_log {
        push_i128(out, i128::from(fill.ts));
        push_str(out, &fill.symbol);
        push_i128(out, i128::from(fill.quantity));
        push_i128(out, i128::from(fill.price_minor));
        push_i128(out, i128::from(fill.commission_minor));
        push_i128(out, i128::from(fill.slippage_minor));
        push_i128(out, i128::from(fill.spread_impact_minor));
    }
    push_count(out, result.equity_curve.len());
    for point in &result.equity_curve {
        push_i128(out, i128::from(point.ts));
        push_i128(out, i128::from(point.equity_minor));
    }
    push_i128(out, i128::from(result.final_equity_minor));
}

/// Encode the metric body: the eight dimensionless ratios via their exact `to_bits` payload
/// plus the benchmark symbol. The only `f64` in any digest, and it round-trips bit-for-bit.
fn encode_metrics_body(out: &mut String, metrics: &PerformanceMetrics) {
    push_opt_f64(out, metrics.sharpe_ratio);
    push_opt_f64(out, metrics.sortino_ratio);
    push_opt_f64(out, metrics.alpha);
    push_opt_f64(out, metrics.beta);
    push_opt_f64(out, metrics.max_drawdown);
    push_opt_f64(out, metrics.annualized_return);
    push_opt_f64(out, metrics.annualized_volatility);
    push_opt_f64(out, metrics.win_rate);
    push_str(out, &metrics.benchmark_symbol);
}

// --------------------------------------------------------------------------- //
// Deterministic, dependency-free text codec (mirrors backtest_store.rs)
// --------------------------------------------------------------------------- //

fn opt_bits(value: Option<f64>) -> Option<u64> {
    value.map(f64::to_bits)
}

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

/// Append a length-prefixed string: the byte length on one line, then the bytes followed by a
/// newline — so any byte (spaces, an OCC option symbol) round-trips without escaping and the
/// encoding stays prefix-free.
fn push_str(out: &mut String, value: &str) {
    out.push_str(&value.len().to_string());
    out.push('\n');
    out.push_str(value);
    out.push('\n');
}

/// Append an optional `f64` ratio: `N` for `None`, else `S` followed by the value's exact
/// `to_bits` payload — so a dimensionless metric ratio folds bit-for-bit (no float-formatting
/// nondeterminism) and a `None` stays an honest "undefined".
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

/// A 64-bit FNV-1a checksum over the canonical body. Non-cryptographic, deterministic,
/// dependency-free, integer-only — the same primitive `backtest_store.rs` uses.
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

#[cfg(test)]
mod tests {
    use std::cell::Cell;
    use std::rc::Rc;

    use super::*;
    use crate::backtest::{
        BacktestBar, BacktestDataSource, BacktestResult, DateRange, EquityPoint, Fill,
    };
    use crate::cost::CostConfig;
    use crate::metrics::{compute, Benchmark, MetricsConfig};
    use atp_types::StrategyId;

    // ---- result/metrics fixtures ---------------------------------------- //

    fn fill(ts: u64, quantity: i64, price: i64) -> Fill {
        Fill {
            ts,
            symbol: "AAPL".to_string(),
            quantity,
            price_minor: price,
            commission_minor: 0,
            slippage_minor: 0,
            spread_impact_minor: 0,
        }
    }

    fn sample_result() -> BacktestResult {
        BacktestResult {
            data_source: BacktestDataSource::SystemData,
            range: DateRange::new(1, 3),
            bars_processed: 3,
            trade_log: vec![fill(1, 10, 100), fill(2, -5, 110)],
            equity_curve: vec![
                EquityPoint {
                    ts: 1,
                    equity_minor: 1000,
                },
                EquityPoint {
                    ts: 2,
                    equity_minor: 1050,
                },
                EquityPoint {
                    ts: 3,
                    equity_minor: 1200,
                },
            ],
            final_equity_minor: 1200,
        }
    }

    fn sample_metrics() -> PerformanceMetrics {
        PerformanceMetrics {
            sharpe_ratio: Some(1.25),
            sortino_ratio: Some(1.5),
            alpha: Some(0.01),
            beta: Some(0.9),
            max_drawdown: Some(-0.2),
            annualized_return: Some(0.15),
            annualized_volatility: Some(0.12),
            win_rate: Some(0.6),
            benchmark_symbol: "SPY".to_string(),
        }
    }

    #[test]
    fn digest_is_stable_and_result_equals_run_none() {
        let result = sample_result();
        assert_eq!(digest_result(&result), digest_result(&result));
        assert_eq!(digest_result(&result), digest_run(&result, None));
    }

    #[test]
    fn changing_one_fill_changes_the_digest() {
        let base = sample_result();
        let mut mutated = sample_result();
        mutated.trade_log[0].quantity += 1;
        assert_ne!(digest_result(&base), digest_result(&mutated));
    }

    #[test]
    fn changing_one_equity_mark_changes_the_digest() {
        let base = sample_result();
        let mut mutated = sample_result();
        mutated.equity_curve[1].equity_minor += 1;
        assert_ne!(digest_result(&base), digest_result(&mutated));
    }

    #[test]
    fn metrics_flag_separates_run_with_and_without_metrics() {
        let result = sample_result();
        let metrics = sample_metrics();
        assert_ne!(digest_result(&result), digest_run(&result, Some(&metrics)));
    }

    #[test]
    fn digest_run_distinguishes_a_one_ulp_metric_change() {
        let result = sample_result();
        let base = sample_metrics();
        let mut nudged = sample_metrics();
        // Smallest representable change to a finite ratio: still numerically ~equal, but a
        // different bit pattern — the digest must catch it.
        nudged.sharpe_ratio = Some(f64::from_bits(base.sharpe_ratio.unwrap().to_bits() + 1));
        assert_ne!(
            digest_run(&result, Some(&base)),
            digest_run(&result, Some(&nudged))
        );
    }

    #[test]
    fn runs_match_accepts_identical_results() {
        assert_eq!(runs_match(&sample_result(), &sample_result()), Ok(()));
    }

    #[test]
    fn runs_match_localizes_trade_log_content_divergence() {
        let a = sample_result();
        let mut b = sample_result();
        b.trade_log[1].price_minor += 1;
        assert_eq!(
            runs_match(&a, &b),
            Err(DeterminismError::TradeLog { index: 1 })
        );
    }

    #[test]
    fn runs_match_localizes_trade_log_length_divergence() {
        let a = sample_result();
        let mut b = sample_result();
        b.trade_log.pop();
        assert_eq!(
            runs_match(&a, &b),
            Err(DeterminismError::TradeLogLength { left: 2, right: 1 })
        );
    }

    #[test]
    fn runs_match_localizes_equity_curve_divergence() {
        let a = sample_result();
        let mut b = sample_result();
        b.equity_curve[2].equity_minor += 1;
        assert_eq!(
            runs_match(&a, &b),
            Err(DeterminismError::EquityCurve { index: 2 })
        );
    }

    #[test]
    fn runs_match_localizes_bars_and_final_equity() {
        let a = sample_result();
        let mut bars = sample_result();
        bars.bars_processed += 1;
        assert_eq!(
            runs_match(&a, &bars),
            Err(DeterminismError::BarsProcessed { left: 3, right: 4 })
        );
        let mut equity = sample_result();
        equity.final_equity_minor += 1;
        assert_eq!(
            runs_match(&a, &equity),
            Err(DeterminismError::FinalEquity {
                left: 1200,
                right: 1201
            })
        );
    }

    #[test]
    fn metrics_match_localizes_the_metric() {
        let a = sample_metrics();
        let mut b = sample_metrics();
        b.beta = Some(0.95);
        assert_eq!(
            metrics_match(&a, &b),
            Err(DeterminismError::Metrics { metric: "beta" })
        );
    }

    #[test]
    fn metrics_match_distinguishes_signed_zero() {
        // +0.0 == -0.0 numerically, but their bits differ — the verifier must catch it.
        let mut a = sample_metrics();
        a.alpha = Some(0.0);
        let mut b = sample_metrics();
        b.alpha = Some(-0.0);
        assert_eq!(
            metrics_match(&a, &b),
            Err(DeterminismError::Metrics { metric: "alpha" })
        );
    }

    // ---- harness fixtures ----------------------------------------------- //

    struct VecSource(Vec<BacktestBar>);

    impl BarSource for VecSource {
        fn source(&self) -> BacktestDataSource {
            BacktestDataSource::SystemData
        }

        fn bars(
            &self,
            symbol: &str,
            _range: &DateRange,
            _max_bars: usize,
        ) -> Result<Vec<BacktestBar>, BacktestError> {
            Ok(self
                .0
                .iter()
                .filter(|bar| bar.symbol == symbol)
                .cloned()
                .collect())
        }
    }

    fn bar(ts: u64, close_minor: i64) -> BacktestBar {
        BacktestBar {
            symbol: "AAPL".to_string(),
            ts,
            close_minor,
            spread_minor: None,
        }
    }

    fn request() -> BacktestRequest {
        BacktestRequest {
            strategy_id: StrategyId::new("det-1"),
            symbol: "AAPL".to_string(),
            data_source: BacktestDataSource::SystemData,
            range: DateRange::new(1, 3),
            starting_cash_minor: 1_000,
            cost_config: CostConfig::zero(),
        }
    }

    /// A deterministic strategy: buys `lot` on the first bar, then holds.
    struct BuyOnceAndHold {
        lot: i64,
        bought: bool,
    }

    impl BacktestStrategy for BuyOnceAndHold {
        fn on_bar(&mut self, _bar: &BacktestBar, _position: i64) -> Result<i64, BacktestError> {
            if self.bought {
                return Ok(0);
            }
            self.bought = true;
            Ok(self.lot)
        }
    }

    /// A nondeterministic strategy: its first-bar lot is read from a counter shared *across
    /// runs*, so the second replay sees a different value than the first.
    struct SharedCounterStrategy {
        counter: Rc<Cell<i64>>,
        decided: bool,
    }

    impl BacktestStrategy for SharedCounterStrategy {
        fn on_bar(&mut self, _bar: &BacktestBar, _position: i64) -> Result<i64, BacktestError> {
            if self.decided {
                return Ok(0);
            }
            self.decided = true;
            let lot = self.counter.get();
            self.counter.set(lot + 1);
            Ok(lot)
        }
    }

    #[test]
    fn verify_reproducible_returns_a_digest_for_a_deterministic_strategy() {
        let source = VecSource(vec![bar(1, 100), bar(2, 110), bar(3, 120)]);
        let engine = BacktestEngine::new();
        let digest = verify_reproducible(
            &engine,
            &request(),
            || BuyOnceAndHold {
                lot: 5,
                bought: false,
            },
            &source,
        )
        .expect("deterministic run reproduces");
        // The returned digest matches a one-off run's digest.
        let mut once = BuyOnceAndHold {
            lot: 5,
            bought: false,
        };
        let result = engine.run(&request(), &mut once, &source).expect("run");
        assert_eq!(digest, digest_result(&result));
    }

    #[test]
    fn verify_reproducible_catches_a_nondeterministic_strategy() {
        let source = VecSource(vec![bar(1, 100), bar(2, 110), bar(3, 120)]);
        let engine = BacktestEngine::new();
        let counter = Rc::new(Cell::new(1));
        let err = verify_reproducible(
            &engine,
            &request(),
            || SharedCounterStrategy {
                counter: Rc::clone(&counter),
                decided: false,
            },
            &source,
        )
        .expect_err("a cross-run-stateful strategy must be caught");
        // Run A buys 1 @ 100, run B buys 2 @ 100 — same length, first fill differs.
        assert_eq!(err, DeterminismError::TradeLog { index: 0 });
    }

    #[test]
    fn verify_reproducible_surfaces_a_run_error() {
        struct Failing;
        impl BacktestStrategy for Failing {
            fn on_bar(&mut self, bar: &BacktestBar, _position: i64) -> Result<i64, BacktestError> {
                Err(BacktestError::StrategyFailed {
                    ts: bar.ts,
                    reason: "boom".to_string(),
                })
            }
        }
        let source = VecSource(vec![bar(1, 100)]);
        let engine = BacktestEngine::new();
        let err = verify_reproducible(&engine, &request(), || Failing, &source)
            .expect_err("a failing replay must fail closed");
        assert_eq!(
            err,
            DeterminismError::Run(BacktestError::StrategyFailed {
                ts: 1,
                reason: "boom".to_string(),
            })
        );
    }

    #[test]
    fn a_hold_only_run_digests_with_an_empty_trade_log() {
        let source = VecSource(vec![bar(1, 100), bar(2, 110)]);
        let engine = BacktestEngine::new();
        let digest = verify_reproducible(
            &engine,
            &request(),
            || BuyOnceAndHold {
                lot: 0,
                bought: false,
            },
            &source,
        )
        .expect("hold-only run reproduces");
        let mut once = BuyOnceAndHold {
            lot: 0,
            bought: false,
        };
        let result = engine.run(&request(), &mut once, &source).expect("run");
        assert!(result.trade_log.is_empty());
        assert_eq!(digest, digest_result(&result));
    }

    /// A rising-price request whose buy-and-hold run yields a strictly-positive, strictly-
    /// increasing equity curve the metric family can be computed over.
    fn metrics_request() -> BacktestRequest {
        BacktestRequest {
            starting_cash_minor: 1_000_000,
            ..request()
        }
    }

    #[test]
    fn verify_reproducible_with_metrics_spans_all_three_artifacts() {
        let source = VecSource(vec![bar(1, 100), bar(2, 120), bar(3, 150)]);
        let req = metrics_request();
        let engine = BacktestEngine::new();

        let digest = verify_reproducible_with_metrics(
            &engine,
            &req,
            || BuyOnceAndHold {
                lot: 100,
                bought: false,
            },
            &source,
            |result| {
                compute(
                    req.starting_cash_minor,
                    &result.equity_curve,
                    &result.trade_log,
                    &Benchmark::spy(),
                    None,
                    &MetricsConfig::default(),
                )
            },
        )
        .expect("deterministic run + metrics reproduce");

        // The returned digest spans all three artifacts: it equals a one-off run's digest_run
        // (with metrics) and differs from the result-only digest.
        let mut once = BuyOnceAndHold {
            lot: 100,
            bought: false,
        };
        let result = engine.run(&req, &mut once, &source).expect("run");
        let metrics = compute(
            req.starting_cash_minor,
            &result.equity_curve,
            &result.trade_log,
            &Benchmark::spy(),
            None,
            &MetricsConfig::default(),
        )
        .expect("metrics");
        assert_eq!(digest, digest_run(&result, Some(&metrics)));
        assert_ne!(digest, digest_result(&result));
    }

    #[test]
    fn verify_reproducible_with_metrics_catches_a_nondeterministic_metric_reduction() {
        // Identical BacktestResults, but the metric REDUCTION consults a counter shared across
        // runs — so the metric family differs between the two computations. This is the case the
        // result-only harness cannot see and is exactly why the metric clause needs its own check.
        let source = VecSource(vec![bar(1, 100), bar(2, 120), bar(3, 150)]);
        let req = metrics_request();
        let engine = BacktestEngine::new();
        let counter = Rc::new(Cell::new(0.0_f64));
        let err = verify_reproducible_with_metrics(
            &engine,
            &req,
            || BuyOnceAndHold {
                lot: 100,
                bought: false,
            },
            &source,
            |_result| {
                let n = counter.get();
                counter.set(n + 1.0);
                Ok(PerformanceMetrics {
                    sharpe_ratio: Some(n),
                    ..sample_metrics()
                })
            },
        )
        .expect_err("a nondeterministic metric reduction must be caught on identical results");
        assert_eq!(
            err,
            DeterminismError::Metrics {
                metric: "sharpe_ratio"
            }
        );
    }

    #[test]
    fn verify_reproducible_with_metrics_surfaces_a_metrics_compute_failure() {
        // A metric family that cannot be computed for a completed run fails closed (the metric
        // clause is never silently dropped).
        let source = VecSource(vec![bar(1, 100), bar(2, 120)]);
        let engine = BacktestEngine::new();
        let err = verify_reproducible_with_metrics(
            &engine,
            &request(),
            || BuyOnceAndHold {
                lot: 1,
                bought: false,
            },
            &source,
            |_result| Err(MetricsError::EmptyEquityCurve),
        )
        .expect_err("a metrics-compute failure must fail closed");
        assert_eq!(
            err,
            DeterminismError::MetricsComputation(MetricsError::EmptyEquityCurve)
        );
    }

    #[test]
    fn runs_match_localizes_provenance_divergence() {
        // Provenance is part of a run's identity: two results from different catalogs or date
        // ranges are not the same run even when their trades and equity coincide.
        let base = sample_result();
        let mut other_source = sample_result();
        other_source.data_source = BacktestDataSource::UploadedData;
        assert_eq!(
            runs_match(&base, &other_source),
            Err(DeterminismError::DataSource)
        );
        let mut other_range = sample_result();
        other_range.range = DateRange::new(1, 99);
        assert_eq!(
            runs_match(&base, &other_range),
            Err(DeterminismError::Range)
        );
    }

    #[test]
    fn metrics_harness_catches_a_run_induced_metric_state_change() {
        // A strategy that bumps a counter shared across runs as a SIDE EFFECT (without changing
        // its trades) leaves the BacktestResults identical but advances state the metric reduction
        // reads. Interleaving (metrics A computed before run B starts) makes metrics A and B
        // observe DIFFERENT counter values, so the divergence is caught — a non-interleaved harness
        // computing both metric sets after both runs would observe the same final value and falsely
        // match. This is the masking regression for the interleaving fix.
        struct BumpingBuyOnce {
            counter: Rc<Cell<i64>>,
            bought: bool,
        }
        impl BacktestStrategy for BumpingBuyOnce {
            fn on_bar(&mut self, _bar: &BacktestBar, _position: i64) -> Result<i64, BacktestError> {
                if self.bought {
                    return Ok(0);
                }
                self.bought = true;
                self.counter.set(self.counter.get() + 1); // side effect; trades stay identical
                Ok(10)
            }
        }

        let source = VecSource(vec![bar(1, 100), bar(2, 110)]);
        let counter = Rc::new(Cell::new(0_i64));
        let read = Rc::clone(&counter);
        let err = verify_reproducible_with_metrics(
            &BacktestEngine::new(),
            &request(),
            || BumpingBuyOnce {
                counter: Rc::clone(&counter),
                bought: false,
            },
            &source,
            |_result| {
                Ok(PerformanceMetrics {
                    sharpe_ratio: Some(read.get() as f64),
                    ..sample_metrics()
                })
            },
        )
        .expect_err("interleaving must catch a run-induced metric state change");
        assert_eq!(
            err,
            DeterminismError::Metrics {
                metric: "sharpe_ratio"
            }
        );
    }
}
