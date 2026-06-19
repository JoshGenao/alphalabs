//! SRS-BT-006 factor tear-sheet operator-CLI integration test (the operator rendering surface).
//!
//! Drives the `factor_tear_sheet_cli` binary the way an operator would — in fresh OS processes via
//! the `CARGO_BIN_EXE_factor_tear_sheet_cli` path Cargo wires for integration tests — and asserts
//! the SRS-BT-006 acceptance criterion end to end: "Factor returns, information coefficient, and
//! turnover analysis are available for completed factor-analysis runs."
//!
//!   1. "Available for completed runs" — `defaults` surfaces all three deliverables, and a default
//!      `run` prints a defined IC (mean 1.0), factor returns (long-short spread 0.08), and turnover
//!      (0.5 per rebalance) — the three SYS-18 deliverables, each a defined statistic.
//!   2. Operator re-analysis without changing the data — `--quantiles` re-buckets the SAME fixture
//!      (same securities / factors / returns), changing the spread and turnover while every
//!      deliverable stays available.
//!
//! Plus the trustworthiness boundary (the SRS-BT-006 analog of the SRS-BT-002 money-safety rule):
//! an undefined statistic renders as the literal `undefined`, never a fabricated `0` or `NaN`; a
//! no-signal (constant-factor) run withholds EVERY statistic rather than fabricating a
//! SecurityKey-ordering ladder; a degenerate / non-finite / duplicate / too-small panel and an
//! invalid quantile count fail closed with no partial sheet; and identical inputs in two fresh
//! processes are byte-identical (the SRS-BT-010 determinism property the analysis must honor).

use std::process::{Command, Output};

/// The `[[bin]]` path Cargo wires for integration tests.
const CLI: &str = env!("CARGO_BIN_EXE_factor_tear_sheet_cli");

fn run(args: &[&str]) -> Output {
    Command::new(CLI)
        .args(args)
        .output()
        .expect("the factor_tear_sheet_cli binary runs")
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("stdout is utf-8")
}

/// The value after a `prefix` line (e.g. `mean-spread:`), failing if absent.
fn value_after(out: &str, prefix: &str) -> String {
    out.lines()
        .find(|line| line.starts_with(prefix))
        .map(|line| line[prefix.len()..].to_string())
        .unwrap_or_else(|| panic!("output missing a `{prefix}` line:\n{out}"))
}

/// Parse a defined statistic after `prefix` as an f64, failing if it is `undefined` or absent.
fn float_after(out: &str, prefix: &str) -> f64 {
    let raw = value_after(out, prefix);
    raw.trim()
        .parse::<f64>()
        .unwrap_or_else(|_| panic!("`{prefix}` value `{raw}` is not a defined float:\n{out}"))
}

/// A sheet line is any aggregate-statistic line; a fail-closed run must print none of them.
fn has_sheet_lines(out: &str) -> bool {
    out.lines().any(|line| {
        line.starts_with("ic-mean:")
            || line.starts_with("mean-spread:")
            || line.starts_with("mean-top-turnover:")
    })
}

/// Assert a run failed closed: non-zero exit AND no partial tear sheet printed.
fn assert_fails_closed(args: &[&str]) {
    let output = run(args);
    assert!(
        !output.status.success(),
        "{args:?} must fail closed (non-zero exit)"
    );
    let out = stdout(&output);
    assert!(
        !has_sheet_lines(&out),
        "{args:?} must print no partial tear sheet:\n{out}"
    );
}

#[test]
fn defaults_surface_all_three_deliverables() {
    let out = stdout(&run(&["defaults"]));
    // The CLI's published analysis parameters, surfaced for an operator to confirm.
    assert!(out.contains("DEFAULT_QUANTILES=5"), "{out}");
    assert!(out.contains("DEFAULT_PERIODS=3"), "{out}");
    assert!(out.contains("FIXTURE_UNIVERSE=10"), "{out}");
    // The deliverable-availability proof: a default run makes all three SYS-18 deliverables
    // available (the AC half made inspectable — there is no SyRS numeric quantile constant to
    // match, so availability is the proof).
    assert!(
        out.contains("srs-bt-006-deliverables: ic=true returns=true turnover=true"),
        "{out}"
    );
}

#[test]
fn default_run_yields_the_three_defined_deliverables() {
    let out = stdout(&run(&["run"]));
    // (1) Information coefficient: the factor perfectly ranks the returns every period, so IC == 1.
    assert!((float_after(&out, "ic-mean:") - 1.0).abs() < 1e-9, "{out}");
    // (2) Factor returns: top quintile mean 0.085, bottom 0.005 -> long-short spread 0.08.
    assert!(
        (float_after(&out, "mean-spread:") - 0.08).abs() < 1e-9,
        "{out}"
    );
    // (3) Turnover: the factor ordering rotates one slot per period -> 0.5 target turnover.
    assert!(
        (float_after(&out, "mean-top-turnover:") - 0.5).abs() < 1e-9,
        "{out}"
    );
    assert!(
        (float_after(&out, "mean-bottom-turnover:") - 0.5).abs() < 1e-9,
        "{out}"
    );
    assert!(
        out.contains("srs-bt-006-deliverables: ic=true returns=true turnover=true"),
        "{out}"
    );
}

#[test]
fn undefined_statistic_renders_as_undefined_not_zero() {
    // The safety core: a perfectly stable IC has zero dispersion, so the risk-adjusted IC is
    // mathematically undefined. It must render as the literal `undefined`, never a fabricated 0 or
    // NaN that an operator could read as a real risk-adjusted signal.
    let out = stdout(&run(&["run"]));
    assert_eq!(
        value_after(&out, "ic-risk-adjusted:").trim(),
        "undefined",
        "{out}"
    );
    assert!(
        !out.contains("NaN"),
        "no statistic may render as NaN:\n{out}"
    );
}

#[test]
fn override_quantiles_changes_the_analysis_on_the_same_fixture() {
    let default_out = stdout(&run(&["run"]));
    let override_out = stdout(&run(&["run", "--quantiles", "2"]));

    // The analysis re-bucketed (5 -> 2 quantiles)...
    assert_eq!(value_after(&default_out, "n-quantiles:").trim(), "5");
    assert_eq!(value_after(&override_out, "n-quantiles:").trim(), "2");
    // ...the spread changed with it (halves of 0.00..0.09: top mean 0.07, bottom 0.02 -> 0.05)...
    assert!(
        (float_after(&override_out, "mean-spread:") - 0.05).abs() < 1e-9,
        "{override_out}"
    );
    assert!(
        (float_after(&override_out, "mean-spread:") - float_after(&default_out, "mean-spread:"))
            .abs()
            > 1e-9,
        "an override must change the realized analysis"
    );
    // ...and every deliverable is still available — the override changed the bucketing of the SAME
    // fixture, not the data.
    assert!(
        override_out.contains("srs-bt-006-deliverables: ic=true returns=true turnover=true"),
        "{override_out}"
    );
}

#[test]
fn flat_factor_withholds_every_statistic_as_undefined() {
    // A constant factor carries no ranking signal: the IC, the quantile returns, the spread, and
    // the turnover are all mathematically undefined. They must be WITHHELD as `undefined`, never
    // fabricated into a SecurityKey-ordering ladder an operator could mistake for alpha. The panel
    // is valid, so the run still exits 0 — withheld, not rejected.
    let output = run(&["run", "--pattern", "flat"]);
    assert!(output.status.success(), "a constant-factor panel is valid");
    let out = stdout(&output);
    assert_eq!(value_after(&out, "ic-mean:").trim(), "undefined", "{out}");
    assert_eq!(
        value_after(&out, "mean-spread:").trim(),
        "undefined",
        "{out}"
    );
    assert_eq!(
        value_after(&out, "mean-top-turnover:").trim(),
        "undefined",
        "{out}"
    );
    assert!(
        out.contains("srs-bt-006-deliverables: ic=false returns=false turnover=false"),
        "{out}"
    );
}

#[test]
fn identical_inputs_are_byte_identical_across_processes() {
    // Two fresh processes over identical flags must agree byte-for-byte (the SRS-BT-010
    // determinism property: fixed folds, a total-order sort, no parallelism / RNG / clock).
    let first = stdout(&run(&["run"]));
    let second = stdout(&run(&["run"]));
    assert_eq!(first, second);
}

#[test]
fn invalid_quantile_count_fails_closed() {
    // A single bucket has no top/bottom spread; the engine rejects it before any statistic.
    assert_fails_closed(&["run", "--quantiles", "1"]);
}

#[test]
fn nonfinite_input_fails_closed() {
    assert_fails_closed(&["run", "--inject", "nonfinite"]);
}

#[test]
fn duplicate_security_fails_closed() {
    assert_fails_closed(&["run", "--inject", "duplicate"]);
}

#[test]
fn too_few_securities_fails_closed() {
    assert_fails_closed(&["run", "--inject", "too-few"]);
}

#[test]
fn empty_period_fails_closed() {
    assert_fails_closed(&["run", "--inject", "empty-period"]);
}

#[test]
fn unknown_flag_fails_closed() {
    assert_fails_closed(&["run", "--nope", "1"]);
}

#[test]
fn unknown_pattern_fails_closed() {
    assert_fails_closed(&["run", "--pattern", "bogus"]);
}

#[test]
fn unknown_subcommand_fails_closed() {
    assert_fails_closed(&["frobnicate"]);
}

#[test]
fn missing_subcommand_fails_closed() {
    assert_fails_closed(&[]);
}
