//! SRS-BT-006 factor tear-sheet operator CLI.
//!
//! The operator-facing surface of "produce factor analysis and tear-sheet outputs" (docs/SRS.md
//! SRS-5.6 SRS-BT-006; SyRS SYS-18; StRS SN-1.05). The tear-sheet *engine*
//! ([`atp_factor_pipeline::factor_analysis::compute_tear_sheet`]) already computes the three
//! SYS-18 deliverables — the information coefficient, the quantile factor returns, and the
//! quantile turnover — over a [`FactorPanel`]; what kept SRS-BT-006 `passes:false` was the
//! deferred operator SURFACE that renders a completed run. This binary ships the **CLI** half of
//! that surface, the same precedent as the SRS-BT-002 cost CLI, the SRS-BT-009 store CLI, and the
//! SRS-BT-010 reproducibility CLI: there is no Python<->Rust runtime bridge, so the operator
//! workflow the acceptance names is demonstrated here over the Rust core. The REST + dashboard
//! rendering (SRS-UI / SRS-API), the scheduled full-universe panel producer (SRS-FAC-001), and
//! the real factor/return data wiring (SRS-DATA-007) remain deferred.
//!
//! - `defaults` — print the CLI's published analysis parameters (the default quantile count, the
//!   fixture universe / period count, and the rebalance step) and PROVE that a default run
//!   surfaces all three SRS-BT-006 deliverables (the information coefficient, the factor returns,
//!   and the turnover analysis are all *available*). There is no SyRS-published numeric quantile
//!   constant, so — unlike the SRS-BT-002 cost CLI's `defaults` — this proves *deliverable
//!   availability*, not a numeric-constant match. Fails closed if any deliverable is missing.
//! - `run [--quantiles N] [--periods P] [--pattern monotone|flat] [--inject <fault>]` — build a
//!   deterministic fixture panel, run it through the REAL engine, and print the per-period and
//!   aggregate information coefficient, the per-quantile factor returns and long-short spread, and
//!   the per-quantile turnover. `--quantiles` is the override seam: it changes the bucketing of
//!   the SAME fixture (same securities, factor values, and forward returns), so an operator
//!   re-analyzes a completed run without touching the data.
//!
//! Safety core (the SRS-BT-006 analog of the SRS-BT-002 "no cash fabricated" rule): every
//! statistic the engine leaves *undefined* (`None` — a zero-dispersion IC, a cutoff-tied quantile,
//! a no-signal spread or turnover) is rendered as the literal token `undefined`, never as `0` or
//! `NaN`. A `--pattern flat` (constant-factor) run carries no ranking signal, so every statistic
//! is withheld as `undefined` rather than fabricated into a `SecurityKey`-ordering ladder an
//! operator could mistake for alpha. A degenerate / non-finite / duplicate / too-small panel
//! (`--inject`) and an invalid `--quantiles 1` flow into the engine's `FactorPanel::validate` and
//! are rejected before any statistic is printed — the run exits non-zero with no partial sheet.

use std::env;
use std::process::ExitCode;

use atp_factor_pipeline::factor_analysis::{
    compute_tear_sheet, FactorObservation, FactorPanel, FactorPeriod, FactorTearSheet,
};
use atp_types::{AssetClass, SecurityKey};

/// The CLI's published default quantile count: quintiles, the standard factor tear-sheet
/// bucketing. There is no SyRS-mandated default (the engine takes the quantile count as an
/// explicit argument), so this is the CLI's documented choice, overridable with `--quantiles`.
const DEFAULT_QUANTILES: usize = 5;
/// The fixture rebalance-period count (turnover needs at least two periods).
const DEFAULT_PERIODS: u64 = 3;
/// The fixture universe size: ten securities, exactly two per default quintile.
const UNIVERSE: usize = 10;
/// Forward-return per unit of factor rank (dimensionless; keeps the fixture's returns small and
/// exactly representable enough that the printed statistics are stable).
const RETURN_SCALE: f64 = 0.01;

const USAGE: &str = "\
factor_tear_sheet_cli — SRS-BT-006 factor analysis & tear-sheet operator workflow

USAGE:
    factor_tear_sheet_cli defaults
    factor_tear_sheet_cli run [--quantiles <n>] [--periods <p>]
                             [--pattern monotone|flat] [--inject <fault>]

COMMANDS:
    defaults  Print the CLI's published analysis parameters (default quantile count, fixture
              universe / period count, rebalance step) and prove a default run surfaces all three
              SRS-BT-006 deliverables — the information coefficient, the factor returns, and the
              turnover analysis are all available. (No SyRS numeric quantile constant exists, so
              this proves deliverable AVAILABILITY, not a numeric-constant match.)
    run       Build a deterministic fixture panel, run it through the real tear-sheet engine, and
              print the per-period + aggregate IC, the per-quantile factor returns + long-short
              spread, and the per-quantile turnover.

RUN FLAGS:
    --quantiles <n>           quantile buckets to sort the SAME fixture into (default 5). The
                              override seam: changes the analysis without changing the data.
    --periods <p>             rebalance periods in the fixture (default 3).
    --pattern monotone|flat   monotone (default): the factor strictly ranks returns, so every
                              deliverable is defined. flat: a constant factor carries no ranking
                              signal, so every statistic is withheld as `undefined`.
    --inject <fault>          inject a fault to exercise the fail-closed trust boundary:
                              nonfinite | duplicate | too-few | empty-period.

An undefined statistic is rendered as the literal `undefined`, never `0` or `NaN`. An invalid
--quantiles 1 or any --inject fault is rejected by the engine before any statistic is printed, so
the run exits non-zero with no partial tear sheet.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("factor_tear_sheet_cli: {err}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: &[String]) -> Result<(), String> {
    let (command, rest) = match args.split_first() {
        Some(parts) => parts,
        None => return Err(format!("missing subcommand\n\n{USAGE}")),
    };
    match command.as_str() {
        "defaults" => cmd_defaults(rest),
        "run" => cmd_run(rest),
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            Ok(())
        }
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    }
}

// --------------------------------------------------------------------------- //
// Subcommands
// --------------------------------------------------------------------------- //

/// Print the published analysis parameters and prove the default run surfaces all three
/// SRS-BT-006 deliverables (the "defaults" half of the acceptance criterion, made inspectable).
fn cmd_defaults(rest: &[String]) -> Result<(), String> {
    if !rest.is_empty() {
        return Err(format!("`defaults` takes no arguments\n\n{USAGE}"));
    }
    println!("DEFAULT_QUANTILES={DEFAULT_QUANTILES}");
    println!("DEFAULT_PERIODS={DEFAULT_PERIODS}");
    println!("FIXTURE_UNIVERSE={UNIVERSE}");
    println!("REBALANCE_STEP=1");

    let panel = fixture_panel(
        DEFAULT_PERIODS,
        DEFAULT_QUANTILES,
        Pattern::Monotone,
        Inject::None,
    )?;
    let sheet =
        compute_tear_sheet(&panel).map_err(|err| format!("tear-sheet run failed: {err}"))?;
    let availability = print_tear_sheet(&sheet);
    // The whole point of the AC: a completed run makes all three deliverables available. A default
    // run that fails to surface one is a contract violation, not a silent partial.
    if !availability.all_available() {
        return Err(format!(
            "default run did not surface all three SRS-BT-006 deliverables: ic={} returns={} turnover={}",
            availability.ic, availability.returns, availability.turnover
        ));
    }
    Ok(())
}

/// Build the fixture from the flags, run it through the engine, and print the tear sheet.
fn cmd_run(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    println!("analysis-quantiles:{}", parsed.quantiles);
    println!("analysis-periods:{}", parsed.periods);
    println!("analysis-pattern:{}", parsed.pattern.label());

    let panel = fixture_panel(
        parsed.periods,
        parsed.quantiles,
        parsed.pattern,
        parsed.inject,
    )?;
    // The engine validates the panel FIRST (a degenerate/non-finite/duplicate panel or an invalid
    // quantile count fails closed here), then computes every statistic. A run error propagates to a
    // non-zero exit — no statistic is printed, so an invalid run never fabricates a tear sheet.
    let sheet =
        compute_tear_sheet(&panel).map_err(|err| format!("tear-sheet run failed: {err}"))?;
    print_tear_sheet(&sheet);
    Ok(())
}

// --------------------------------------------------------------------------- //
// Tear-sheet rendering (None -> the literal `undefined`, never 0 or NaN)
// --------------------------------------------------------------------------- //

/// Which of the three SRS-BT-006 deliverables a completed run made available (each aggregate is
/// `Some`).
struct Availability {
    ic: bool,
    returns: bool,
    turnover: bool,
}

impl Availability {
    fn all_available(&self) -> bool {
        self.ic && self.returns && self.turnover
    }
}

/// Print the three deliverables and return which aggregates were available. An undefined value is
/// rendered as the literal `undefined` (the safety core): a withheld statistic is never presented
/// as a real `0` an operator could rank a factor on.
fn print_tear_sheet(sheet: &FactorTearSheet) -> Availability {
    println!("n-periods:{}", sheet.n_periods);
    println!("n-quantiles:{}", sheet.n_quantiles);

    // (1) Information coefficient.
    for (index, (ts, ic)) in sheet.ic.per_period.iter().enumerate() {
        println!("ic-per-period[{index}] ts={ts} ic={}", fmt_opt(*ic));
    }
    println!("ic-mean:{}", fmt_opt(sheet.ic.mean));
    println!("ic-std:{}", fmt_opt(sheet.ic.std));
    println!("ic-risk-adjusted:{}", fmt_opt(sheet.ic.risk_adjusted));

    // (2) Factor returns: per-quantile means, the long-short spread series, and its mean.
    for (index, quantile_means) in sheet.returns.per_quantile_mean.iter().enumerate() {
        let buckets: Vec<String> = quantile_means
            .iter()
            .enumerate()
            .map(|(q, mean)| format!("q{q}={}", fmt_opt(*mean)))
            .collect();
        println!("returns-per-quantile[{index}] {}", buckets.join(" "));
    }
    for (index, (ts, spread)) in sheet.returns.spread_per_period.iter().enumerate() {
        println!("spread[{index}] ts={ts} spread={}", fmt_opt(*spread));
    }
    println!("mean-spread:{}", fmt_opt(sheet.returns.mean_spread));

    // (3) Turnover: per-period top/bottom and the means.
    for (index, (ts, turnover)) in sheet.turnover.top_turnover.iter().enumerate() {
        println!(
            "top-turnover[{index}] ts={ts} turnover={}",
            fmt_opt(*turnover)
        );
    }
    for (index, (ts, turnover)) in sheet.turnover.bottom_turnover.iter().enumerate() {
        println!(
            "bottom-turnover[{index}] ts={ts} turnover={}",
            fmt_opt(*turnover)
        );
    }
    println!("mean-top-turnover:{}", fmt_opt(sheet.turnover.mean_top));
    println!(
        "mean-bottom-turnover:{}",
        fmt_opt(sheet.turnover.mean_bottom)
    );

    let availability = Availability {
        ic: sheet.ic.mean.is_some(),
        returns: sheet.returns.mean_spread.is_some(),
        turnover: sheet.turnover.mean_top.is_some() && sheet.turnover.mean_bottom.is_some(),
    };
    println!(
        "srs-bt-006-deliverables: ic={} returns={} turnover={}",
        availability.ic, availability.returns, availability.turnover
    );
    availability
}

/// Render an optional statistic with fixed precision, or the literal `undefined` when the engine
/// withheld it (`None`). Fixed precision keeps two fresh processes byte-identical and absorbs
/// floating-point noise into a stable, assertable string.
fn fmt_opt(value: Option<f64>) -> String {
    match value {
        Some(v) => format!("{v:.12}"),
        None => "undefined".to_string(),
    }
}

// --------------------------------------------------------------------------- //
// Argument parsing
// --------------------------------------------------------------------------- //

#[derive(Clone, Copy)]
enum Pattern {
    Monotone,
    Flat,
}

impl Pattern {
    fn label(self) -> &'static str {
        match self {
            Pattern::Monotone => "monotone",
            Pattern::Flat => "flat",
        }
    }
}

#[derive(Clone, Copy)]
enum Inject {
    None,
    Nonfinite,
    Duplicate,
    TooFew,
    EmptyPeriod,
}

struct ParsedArgs {
    quantiles: usize,
    periods: u64,
    pattern: Pattern,
    inject: Inject,
}

impl Default for ParsedArgs {
    fn default() -> Self {
        Self {
            quantiles: DEFAULT_QUANTILES,
            periods: DEFAULT_PERIODS,
            pattern: Pattern::Monotone,
            inject: Inject::None,
        }
    }
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = ParsedArgs::default();
        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--quantiles" => parsed.quantiles = take_usize(&mut iter, flag)?,
                "--periods" => parsed.periods = take_u64(&mut iter, flag)?,
                "--pattern" => parsed.pattern = parse_pattern(&take_value(&mut iter, flag)?)?,
                "--inject" => parsed.inject = parse_inject(&take_value(&mut iter, flag)?)?,
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        Ok(parsed)
    }
}

fn parse_pattern(spec: &str) -> Result<Pattern, String> {
    match spec {
        "monotone" => Ok(Pattern::Monotone),
        "flat" => Ok(Pattern::Flat),
        other => Err(format!(
            "unknown pattern '{other}' (expected monotone|flat)"
        )),
    }
}

fn parse_inject(spec: &str) -> Result<Inject, String> {
    match spec {
        "none" => Ok(Inject::None),
        "nonfinite" => Ok(Inject::Nonfinite),
        "duplicate" => Ok(Inject::Duplicate),
        "too-few" => Ok(Inject::TooFew),
        "empty-period" => Ok(Inject::EmptyPeriod),
        other => Err(format!(
            "unknown fault '{other}' (expected nonfinite|duplicate|too-few|empty-period)"
        )),
    }
}

fn take_value<'a>(
    iter: &mut impl Iterator<Item = &'a String>,
    flag: &str,
) -> Result<String, String> {
    iter.next()
        .map(|value| value.to_string())
        .ok_or_else(|| format!("{flag} expects a value"))
}

fn take_usize<'a>(
    iter: &mut impl Iterator<Item = &'a String>,
    flag: &str,
) -> Result<usize, String> {
    let raw = take_value(iter, flag)?;
    raw.parse::<usize>()
        .map_err(|_| format!("{flag} expects a non-negative integer, got '{raw}'"))
}

fn take_u64<'a>(iter: &mut impl Iterator<Item = &'a String>, flag: &str) -> Result<u64, String> {
    let raw = take_value(iter, flag)?;
    raw.parse::<u64>()
        .map_err(|_| format!("{flag} expects a non-negative integer, got '{raw}'"))
}

// --------------------------------------------------------------------------- //
// Deterministic fixture panel
// --------------------------------------------------------------------------- //

/// Build the fixture panel. `monotone`: each period assigns a strictly-ranked factor whose
/// ordering ROTATES one slot per period, so the factor perfectly predicts returns (IC == 1 each
/// period) while the top/bottom-quantile membership churns (non-zero, defined turnover). `flat`:
/// every security shares one factor value, so the factor carries no ranking signal and every
/// statistic is withheld. The `--quantiles` count flows straight into [`FactorPanel::new`] — the
/// override seam — without touching the observations. An `--inject` fault corrupts the panel to
/// exercise the engine's fail-closed trust boundary.
fn fixture_panel(
    periods: u64,
    quantiles: usize,
    pattern: Pattern,
    inject: Inject,
) -> Result<FactorPanel, String> {
    let mut factor_periods: Vec<FactorPeriod> = Vec::with_capacity(periods as usize);
    for p in 1..=periods {
        let mut observations: Vec<FactorObservation> = Vec::with_capacity(UNIVERSE);
        for i in 0..UNIVERSE {
            let (factor, forward_return) = match pattern {
                // Rotate the factor ordering one slot per period; the return is strictly increasing
                // in the factor within the period, so the factor perfectly ranks returns.
                Pattern::Monotone => {
                    let factor = ((i + (p as usize - 1)) % UNIVERSE) as f64;
                    (factor, factor * RETURN_SCALE)
                }
                // A constant factor: distinct returns, but no ranking signal.
                Pattern::Flat => (1.0, i as f64 * RETURN_SCALE),
            };
            observations.push(observation(&symbol(i), factor, forward_return)?);
        }
        factor_periods.push(FactorPeriod::new(p, observations));
    }
    apply_inject(&mut factor_periods, inject);
    Ok(FactorPanel::new(factor_periods, quantiles))
}

/// Corrupt the generated panel to exercise the engine's fail-closed trust boundary. Each fault
/// maps to one `FactorAnalysisError` the engine raises before computing any statistic.
fn apply_inject(periods: &mut [FactorPeriod], inject: Inject) {
    let Some(first) = periods.first_mut() else {
        return;
    };
    match inject {
        Inject::None => {}
        // A non-finite forward return -> FactorAnalysisError::NonFiniteInput.
        Inject::Nonfinite => {
            if let Some(observation) = first.observations.first_mut() {
                observation.forward_return = f64::INFINITY;
            }
        }
        // A repeated security in one period -> FactorAnalysisError::DuplicateSecurity.
        Inject::Duplicate => {
            if let Some(observation) = first.observations.first().cloned() {
                first.observations.push(observation);
            }
        }
        // Fewer securities than quantiles -> FactorAnalysisError::InsufficientSecurities.
        Inject::TooFew => {
            first.observations.truncate(1);
        }
        // A period with no observations -> FactorAnalysisError::EmptyPeriod.
        Inject::EmptyPeriod => {
            first.observations.clear();
        }
    }
}

fn symbol(index: usize) -> String {
    format!("S{index:02}")
}

fn observation(
    symbol: &str,
    factor: f64,
    forward_return: f64,
) -> Result<FactorObservation, String> {
    Ok(FactorObservation::new(key(symbol)?, factor, forward_return))
}

fn key(symbol: &str) -> Result<SecurityKey, String> {
    SecurityKey::new(symbol, AssetClass::Equity)
        .map_err(|err| format!("invalid fixture security '{symbol}': {err:?}"))
}
