//! SRS-BT-003 shared-cost-family operator-CLI integration test.
//!
//! Drives the `bt003_shared_cost_cli` binary the way an operator would — in fresh OS processes via
//! the `CARGO_BIN_EXE_bt003_shared_cost_cli` path Cargo wires for integration tests — and asserts
//! the SRS-BT-003 acceptance criterion end to end: "a paper strategy and backtest using identical
//! cost configuration compute fills and commissions from the same model family".
//!
//!   1. `defaults` proves the paper engine's default cost family IS the backtest engine's default,
//!      which IS the SyRS baseline (SYS-15e).
//!   2. `compare` runs the SAME fixture strategy over the SAME bars through BOTH engines under one
//!      cost config and proves the per-fill commission / slippage / spread-impact are EQUAL fill for
//!      fill (`cost-family-match:true`) — under the default family, under an operator override, and
//!      with the `--full` ledger/equity agreeing.
//!
//! Plus the shared money-safety boundary: each `--inject` fault is rejected by BOTH engines before
//! any fill (non-zero exit, no comparison line — a cash-fabricating fault can never produce a
//! report), and identical inputs in two fresh processes are byte-identical.

use std::process::{Command, Output};

/// The `[[bin]]` path Cargo wires for integration tests.
const CLI: &str = env!("CARGO_BIN_EXE_bt003_shared_cost_cli");

fn run(args: &[&str]) -> Output {
    Command::new(CLI)
        .args(args)
        .output()
        .expect("the bt003_shared_cost_cli binary runs")
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("stdout is utf-8")
}

/// The value after a `prefix` line (e.g. `cost-family-match:`), failing if absent.
fn value_after(out: &str, prefix: &str) -> String {
    out.lines()
        .find(|line| line.starts_with(prefix))
        .map(|line| line[prefix.len()..].trim().to_string())
        .unwrap_or_else(|| panic!("output missing a `{prefix}` line:\n{out}"))
}

fn int_after(out: &str, prefix: &str) -> i64 {
    value_after(out, prefix)
        .parse::<i64>()
        .unwrap_or_else(|_| panic!("`{prefix}` value is not an integer:\n{out}"))
}

/// All `fill[..]` comparison lines, in order.
fn fill_lines(out: &str) -> Vec<String> {
    out.lines()
        .filter(|line| line.starts_with("fill["))
        .map(str::to_string)
        .collect()
}

/// A fill comparison line is `fill[i] ... | backtest ... | paper ... | match=..`; split on " | ".
fn fill_segments(line: &str) -> Vec<&str> {
    line.split(" | ").collect()
}

/// Extract the `key=value` integer field from a `key=value`-formatted segment.
fn field(segment: &str, key: &str) -> i64 {
    let needle = format!("{key}=");
    let start = segment
        .find(&needle)
        .unwrap_or_else(|| panic!("segment missing `{key}`:\n{segment}"))
        + needle.len();
    let rest = &segment[start..];
    let end = rest.find(' ').unwrap_or(rest.len());
    rest[..end]
        .parse::<i64>()
        .unwrap_or_else(|_| panic!("`{key}` is not an integer in:\n{segment}"))
}

#[test]
fn defaults_prove_the_same_cost_family() {
    let out = stdout(&run(&["defaults"]));
    // The paper engine's default model of each family is the named SyRS baseline...
    assert_eq!(value_after(&out, "sim-default-commission:"), "IbTiered", "{out}");
    assert_eq!(value_after(&out, "sim-default-slippage:"), "NotionalBps(5)", "{out}");
    assert_eq!(
        value_after(&out, "sim-default-spread:"),
        "ObservedOrFallbackBps(10)",
        "{out}"
    );
    // ...and it IS the backtest default, which IS the SyRS baseline (SYS-15e).
    assert_eq!(value_after(&out, "sim-default-matches-backtest-default:"), "true", "{out}");
    assert_eq!(value_after(&out, "backtest-default-matches-syrs:"), "true", "{out}");
    assert_eq!(value_after(&out, "same-cost-family:"), "true", "{out}");
}

#[test]
fn default_compare_matches_fill_for_fill() {
    let out = stdout(&run(&["compare"]));
    let fills = fill_lines(&out);
    assert_eq!(fills.len(), 2, "one round trip = two fills:\n{out}");
    for fill in &fills {
        assert!(fill.ends_with("match=true"), "each fill must agree:\n{fill}");
    }
    assert_eq!(int_after(&out, "fills-compared:"), 2);
    assert_eq!(value_after(&out, "cost-family-match:"), "true", "{out}");
}

#[test]
fn each_fill_shows_both_engines_agree_with_nonzero_costs() {
    // Non-vacuity: the two engines' cost decompositions are EQUAL component-by-component, and the
    // default family actually charges a cost (so `match=true` is not the trivial 0 == 0).
    let out = stdout(&run(&["compare"]));
    let fills = fill_lines(&out);
    assert_eq!(fills.len(), 2, "{out}");
    for fill in &fills {
        let segments = fill_segments(fill);
        let backtest = segments
            .iter()
            .find(|s| s.starts_with("backtest "))
            .unwrap_or_else(|| panic!("missing backtest segment:\n{fill}"));
        let paper = segments
            .iter()
            .find(|s| s.starts_with("paper "))
            .unwrap_or_else(|| panic!("missing paper segment:\n{fill}"));
        for key in ["comm", "slip", "spread"] {
            assert_eq!(
                field(backtest, key),
                field(paper, key),
                "engines disagree on {key}:\n{fill}"
            );
        }
        // The default family charges a positive commission and slippage on every fill.
        assert!(field(backtest, "comm") > 0, "expected a real commission:\n{fill}");
        assert!(field(backtest, "slip") > 0, "expected real slippage:\n{fill}");
    }
}

#[test]
fn observed_spread_path_is_distinct_from_the_fallback() {
    // The buy fill (bar 1 carries an observed spread) charges a different spread impact than the
    // sell fill (no observed spread → the fallback), proving the observed-spread path is USED — and
    // both engines agree on each (shared family).
    let out = stdout(&run(&["compare"]));
    let fills = fill_lines(&out);
    let spread = |line: &str| {
        let seg = fill_segments(line)
            .into_iter()
            .find(|s| s.starts_with("backtest "))
            .expect("backtest segment");
        field(seg, "spread")
    };
    assert_ne!(
        spread(&fills[0]),
        spread(&fills[1]),
        "observed-spread fill must differ from the fallback fill:\n{out}"
    );
}

#[test]
fn override_is_shared_by_both_engines() {
    let out = stdout(&run(&[
        "compare",
        "--commission",
        "per-trade:100",
        "--slippage",
        "none",
        "--spread",
        "fixed:25",
    ]));
    // The single override config is echoed and applied to BOTH engines...
    assert!(out.contains("commission=PerTrade(100)"), "{out}");
    assert!(out.contains("spread=FixedBps(25)"), "{out}");
    // ...and they still agree fill for fill (the override is shared, not strategy-specific).
    assert_eq!(value_after(&out, "cost-family-match:"), "true", "{out}");
    for fill in fill_lines(&out) {
        let segments = fill_segments(&fill);
        let backtest = segments.iter().find(|s| s.starts_with("backtest ")).unwrap();
        // PerTrade(100), no slippage, FixedBps(25) of $10,000 = 2500 on every fill.
        assert_eq!(field(backtest, "comm"), 100, "{fill}");
        assert_eq!(field(backtest, "slip"), 0, "{fill}");
        assert_eq!(field(backtest, "spread"), 2_500, "{fill}");
    }
}

#[test]
fn full_reports_equal_equity_and_cash_for_the_round_trip() {
    // With flat prices, the round trip returns to a flat position; both engines moved cash by exactly
    // the same total cost, so the backtest final equity EQUALS the paper ledger cash.
    let out = stdout(&run(&["compare", "--full"]));
    assert_eq!(value_after(&out, "cost-family-match:"), "true", "{out}");
    assert_eq!(
        int_after(&out, "backtest-final-equity-minor:"),
        int_after(&out, "paper-ledger-cash-minor:"),
        "the two engines must agree on the economics:\n{out}"
    );
    assert_eq!(int_after(&out, "paper-ledger-position:"), 0, "{out}");
    // The ledger accumulated a positive commission (both fills charged the IB floor).
    assert!(int_after(&out, "paper-ledger-commission-paid-minor:") > 0, "{out}");
}

#[test]
fn identical_inputs_are_byte_identical_across_processes() {
    // Both engines are integer-only with no platform randomness, so two fresh processes over
    // identical flags agree byte-for-byte (the SRS-BT-010 determinism property the shared family
    // must honor).
    let first = stdout(&run(&["compare", "--full"]));
    let second = stdout(&run(&["compare", "--full"]));
    assert_eq!(first, second);
}

/// A `--inject` fault must be rejected by BOTH engines before any fill: non-zero exit, no
/// comparison line, and a `both engines failed closed` message — a cash-fabricating fault can never
/// produce a report.
fn assert_inject_fails_closed(fault: &str) {
    let output = run(&["compare", "--inject", fault]);
    assert!(!output.status.success(), "inject {fault} must fail closed");
    let out = stdout(&output);
    assert!(
        fill_lines(&out).is_empty(),
        "no comparison line may be printed for inject {fault}:\n{out}"
    );
    assert!(
        !out.contains("cost-family-match:"),
        "no cost-family-match line may be printed for inject {fault}:\n{out}"
    );
    let combined = out + &String::from_utf8(output.stderr).expect("stderr utf-8");
    assert!(
        combined.contains("both engines failed closed"),
        "inject {fault} must report both engines failing closed:\n{combined}"
    );
}

#[test]
fn inject_nonpositive_price_fails_closed_in_both_engines() {
    assert_inject_fails_closed("nonpositive-price");
}

#[test]
fn inject_negative_spread_fails_closed_in_both_engines() {
    assert_inject_fails_closed("negative-spread");
}

#[test]
fn inject_negative_commission_fails_closed_in_both_engines() {
    assert_inject_fails_closed("negative-commission");
}

#[test]
fn unknown_fault_fails_closed() {
    let output = run(&["compare", "--inject", "make-money"]);
    assert!(!output.status.success());
}

#[test]
fn zero_lot_fails_closed_with_no_vacuous_match() {
    // A zero lot trades nothing, so both engines would produce zero fills. The CLI must NOT print a
    // vacuous cost-family-match:true over zero comparisons — it must fail closed.
    let output = run(&["compare", "--lot", "0"]);
    assert!(!output.status.success(), "zero lot must fail closed");
    let out = stdout(&output);
    assert!(
        !out.contains("cost-family-match:true"),
        "a zero-fill run must never assert a match:\n{out}"
    );
    assert!(
        fill_lines(&out).is_empty(),
        "a zero-lot run must compare no fills:\n{out}"
    );
}

#[test]
fn negative_lot_fails_closed() {
    let output = run(&["compare", "--lot", "-100"]);
    assert!(!output.status.success(), "negative lot must fail closed");
    assert!(!stdout(&output).contains("cost-family-match:true"));
}

#[test]
fn unknown_flag_fails_closed() {
    let output = run(&["compare", "--nope", "1"]);
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

#[test]
fn help_paths_exit_zero_with_usage() {
    // The documented help surface must be discoverable from the conventional paths — top-level and
    // per-subcommand — exiting 0 with the usage text.
    for args in [
        vec!["help"],
        vec!["--help"],
        vec!["-h"],
        vec!["compare", "--help"],
        vec!["defaults", "--help"],
    ] {
        let output = run(&args);
        assert!(output.status.success(), "help path {args:?} must exit 0");
        assert!(
            stdout(&output).contains("USAGE:"),
            "help path {args:?} must print usage"
        );
    }
}
