//! SRS-BT-005 benchmark-comparison operator-CLI integration test (the operator rendering surface).
//!
//! Drives the `benchmark_comparison_cli` binary the way an operator would — in fresh OS processes via
//! the `CARGO_BIN_EXE_benchmark_comparison_cli` path Cargo wires for integration tests — and asserts
//! the three halves of the SRS-BT-005 acceptance criterion end to end:
//!
//!   1. "If no benchmark is selected, SPY is used" — `defaults` proves
//!      `BenchmarkSelection::unselected()` resolves to and identifies SPY; a default `run` renders the
//!      comparison and metrics against SPY with `is_default_benchmark=true`.
//!   2. "Alpha and beta are computed against the selected benchmark" — a default run produces defined
//!      alpha/beta against SPY, and they equal the metric family's alpha/beta; `--benchmark QQQ`
//!      computes against QQQ instead.
//!   3. "Dashboard and backtest reports identify the benchmark" — the rendered report names its
//!      benchmark on both the comparison and the metric family, defaulting to SPY and echoing a
//!      user-selected symbol.
//!
//! Plus the safety boundary: an undefined statistic renders as the literal `undefined` (never a
//! fabricated 0 or leaked NaN); every `--inject` trust-boundary fault (substituted symbol, misaligned
//! or length-mismatched series, non-positive level, operational source failure, foreign window) fails
//! closed with no partial report; and identical inputs in two fresh processes are byte-identical
//! (consistent with the SRS-BT-010 determinism property).

use std::process::{Command, Output};

/// The `[[bin]]` path Cargo wires for integration tests.
const CLI: &str = env!("CARGO_BIN_EXE_benchmark_comparison_cli");

fn run(args: &[&str]) -> Output {
    Command::new(CLI)
        .args(args)
        .output()
        .expect("the benchmark_comparison_cli binary runs")
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("stdout is utf-8")
}

/// The value after a `prefix` line (e.g. `comparison-alpha:`), failing if absent.
fn value_after(out: &str, prefix: &str) -> String {
    out.lines()
        .find(|line| line.starts_with(prefix))
        .map(|line| line[prefix.len()..].to_string())
        .unwrap_or_else(|| panic!("output missing a `{prefix}` line:\n{out}"))
}

fn float_after(out: &str, prefix: &str) -> f64 {
    value_after(out, prefix)
        .trim()
        .parse::<f64>()
        .unwrap_or_else(|_| panic!("`{prefix}` value is not a float:\n{out}"))
}

/// True when the output contains NO report body — neither a `comparison-` statistic nor a `metric-`
/// line — i.e. the run failed closed before rendering anything.
fn has_no_report(out: &str) -> bool {
    !out.lines()
        .any(|line| line.starts_with("comparison-") || line.starts_with("metric-"))
}

#[test]
fn defaults_resolve_unselected_to_spy() {
    let out = stdout(&run(&["defaults"]));
    assert!(out.contains("DEFAULT_BENCHMARK_SYMBOL=SPY"), "{out}");
    // The acceptance core: an unselected benchmark IS the SPY default and is identified as default.
    assert_eq!(
        value_after(&out, "default-selection-resolves-to:").trim(),
        "SPY"
    );
    assert_eq!(
        value_after(&out, "default-selection-is-default:").trim(),
        "true"
    );
    assert_eq!(value_after(&out, "spy-benchmark-symbol:").trim(), "SPY");
    // The identity fields a report renders are surfaced for an operator to confirm.
    assert!(
        value_after(&out, "comparison-identity-fields:").contains("benchmark_symbol"),
        "{out}"
    );
    assert_eq!(
        value_after(&out, "report-identifies-benchmark:").trim(),
        "true"
    );
}

#[test]
fn default_run_identifies_spy_and_computes_alpha_beta() {
    let out = stdout(&run(&["run"]));
    // No benchmark selected ⇒ the report identifies and defaults to SPY (SYS-17).
    assert!(
        out.contains("comparison: benchmark_symbol=SPY is_default_benchmark=true"),
        "{out}"
    );
    assert_eq!(value_after(&out, "metrics-benchmark-symbol:").trim(), "SPY");
    // Alpha and beta are computed against SPY (defined on the dispersed fixture curve).
    assert_ne!(
        value_after(&out, "comparison-alpha:").trim(),
        "undefined",
        "{out}"
    );
    assert_ne!(
        value_after(&out, "comparison-beta:").trim(),
        "undefined",
        "{out}"
    );
}

#[test]
fn user_selected_benchmark_is_identified_not_the_default() {
    let out = stdout(&run(&["run", "--benchmark", "QQQ"]));
    // The report identifies the USER-selected benchmark, not SPY, and marks it non-default.
    assert!(
        out.contains("comparison: benchmark_symbol=QQQ is_default_benchmark=false"),
        "{out}"
    );
    assert_eq!(value_after(&out, "metrics-benchmark-symbol:").trim(), "QQQ");
    assert_eq!(
        value_after(&out, "selection: benchmark=").trim(),
        "QQQ is_default=false"
    );
}

#[test]
fn comparison_and_metric_alpha_beta_agree() {
    // The comparison's CAPM coefficients are the same values the metric family echoes (one source).
    let out = stdout(&run(&["run"]));
    assert_eq!(
        float_after(&out, "comparison-alpha:"),
        float_after(&out, "metric-alpha:"),
    );
    assert_eq!(
        float_after(&out, "comparison-beta:"),
        float_after(&out, "metric-beta:"),
    );
}

#[test]
fn excess_return_is_strategy_minus_benchmark() {
    let out = stdout(&run(&["run"]));
    let strategy = float_after(&out, "comparison-strategy-total-return:");
    let benchmark = float_after(&out, "comparison-benchmark-total-return:");
    let excess = float_after(&out, "comparison-excess-return:");
    assert!(
        (excess - (strategy - benchmark)).abs() < 1e-9,
        "excess must equal strategy - benchmark:\n{out}"
    );
}

#[test]
fn undefined_statistic_renders_as_the_literal_undefined() {
    // A run whose position never closes has no realized round trip, so the win rate is mathematically
    // undefined. It must render as the literal `undefined`, never a fabricated 0 or a leaked NaN.
    let out = stdout(&run(&["run", "--sell-ts", "99"]));
    assert_eq!(
        value_after(&out, "metric-win-rate:").trim(),
        "undefined",
        "{out}"
    );
    // The defined statistics on the SAME run are still real numbers (not collapsed to undefined).
    assert_ne!(
        value_after(&out, "metric-sharpe-ratio:").trim(),
        "undefined",
        "{out}"
    );
    // No statistic leaked a NaN/inf token.
    assert!(!out.to_lowercase().contains("nan"), "{out}");
    assert!(!out.to_lowercase().contains("inf"), "{out}");
}

#[test]
fn identical_inputs_are_byte_identical_across_processes() {
    // Two fresh processes over identical flags must agree byte-for-byte (the SRS-BT-010 determinism
    // property the comparison math must honor — fixed-precision rendering, no platform randomness).
    let first = stdout(&run(&["run"]));
    let second = stdout(&run(&["run"]));
    assert_eq!(first, second);
}

#[test]
fn substituted_benchmark_series_fails_closed() {
    // The source labels a well-formed series with a DIFFERENT symbol than selected. Identity is bound
    // to the returned payload, so the comparison fails closed rather than reporting the wrong
    // benchmark — no partial report.
    let output = run(&["run", "--inject", "symbol-mismatch"]);
    assert!(!output.status.success(), "substitution must fail closed");
    assert!(has_no_report(&stdout(&output)));
}

#[test]
fn selected_benchmark_still_fails_closed_on_substitution() {
    // Even with an explicit selection, a substituted series is rejected (the trust boundary is not
    // bypassed by selecting a benchmark).
    let output = run(&["run", "--benchmark", "QQQ", "--inject", "symbol-mismatch"]);
    assert!(!output.status.success());
    assert!(has_no_report(&stdout(&output)));
}

#[test]
fn misaligned_source_fails_closed() {
    let output = run(&["run", "--inject", "length-mismatch"]);
    assert!(
        !output.status.success(),
        "a misaligned series must fail closed"
    );
    assert!(has_no_report(&stdout(&output)));
}

#[test]
fn nonpositive_benchmark_level_fails_closed() {
    let output = run(&["run", "--inject", "nonpositive-level"]);
    assert!(!output.status.success());
    assert!(has_no_report(&stdout(&output)));
}

#[test]
fn operational_source_failure_fails_closed() {
    // Each typed operational read failure surfaces as a fail-closed exit with no partial report.
    for fault in ["unavailable", "not-found", "timeout", "stale"] {
        let output = run(&["run", "--inject", fault]);
        assert!(
            !output.status.success(),
            "source fault `{fault}` must fail closed"
        );
        assert!(
            has_no_report(&stdout(&output)),
            "fault `{fault}` printed a partial report"
        );
    }
}

#[test]
fn foreign_window_fails_closed() {
    // A window that does not contain the equity curve measures the benchmark over a different period
    // than the strategy, so the comparison is rejected.
    let output = run(&["run", "--inject", "foreign-window"]);
    assert!(!output.status.success());
    assert!(has_no_report(&stdout(&output)));
}

#[test]
fn invalid_benchmark_symbol_fails_closed() {
    // A non-canonical (lowercase) symbol is rejected at selection, before any run.
    let output = run(&["run", "--benchmark", "spy"]);
    assert!(!output.status.success());
    assert!(has_no_report(&stdout(&output)));
}

#[test]
fn invalid_metrics_config_fails_closed() {
    // A zero annualization factor is an unannualizable cadence — rejected before any report.
    let output = run(&["run", "--periods-per-year", "0"]);
    assert!(!output.status.success());
    assert!(has_no_report(&stdout(&output)));
}

#[test]
fn unknown_inject_fault_fails_closed() {
    let output = run(&["run", "--inject", "frobnicate"]);
    assert!(!output.status.success());
}

#[test]
fn unknown_flag_fails_closed() {
    let output = run(&["run", "--nope", "1"]);
    assert!(!output.status.success());
}

#[test]
fn unknown_subcommand_fails_closed() {
    let output = run(&["frobnicate"]);
    assert!(!output.status.success());
}

#[test]
fn missing_subcommand_fails_closed() {
    let output = run(&[]);
    assert!(!output.status.success());
}
