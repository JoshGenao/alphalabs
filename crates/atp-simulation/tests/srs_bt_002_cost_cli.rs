//! SRS-BT-002 configurable-cost operator-CLI integration test (the operator override surface).
//!
//! Drives the `bt002_cost_cli` binary the way an operator would — in fresh OS processes via the
//! `CARGO_BIN_EXE_bt002_cost_cli` path Cargo wires for integration tests — and asserts the two
//! halves of the SRS-BT-002 acceptance criterion end to end:
//!
//!   1. "Defaults match the SyRS values" — `defaults` prints the published constants and proves
//!      `CostConfig::default() == CostConfig::syrs_defaults()`; a default `run` applies exactly the
//!      SyRS cost family to the fills (commission 35, slippage 500, observed-spread 2000 vs fallback
//!      1000), every figure an exact integer minor unit.
//!   2. "Override … without changing strategy code" — overriding the three models changes the costs
//!      while the SAME fixture strategy produces the SAME two fills (qty 100 then -100); the
//!      frictionless override zeroes every cost and recovers the starting cash.
//!
//! Plus the money-safety boundary: a negative override parameter fails closed (non-zero exit, no
//! fill or equity printed — a cost can never fabricate cash), and identical inputs in two fresh
//! processes are byte-identical (consistent with the SRS-BT-010 determinism property).

use std::process::{Command, Output};

/// The `[[bin]]` path Cargo wires for integration tests.
const CLI: &str = env!("CARGO_BIN_EXE_bt002_cost_cli");

const STARTING_CASH_MINOR: i64 = 10_000_000;

fn run(args: &[&str]) -> Output {
    Command::new(CLI)
        .args(args)
        .output()
        .expect("the bt002_cost_cli binary runs")
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("stdout is utf-8")
}

/// The value after a `prefix:` line (e.g. `total-cost-minor:`), failing if absent.
fn value_after(out: &str, prefix: &str) -> String {
    out.lines()
        .find(|line| line.starts_with(prefix))
        .map(|line| line[prefix.len()..].to_string())
        .unwrap_or_else(|| panic!("output missing a `{prefix}` line:\n{out}"))
}

fn int_after(out: &str, prefix: &str) -> i64 {
    value_after(out, prefix)
        .trim()
        .parse::<i64>()
        .unwrap_or_else(|_| panic!("`{prefix}` value is not an integer:\n{out}"))
}

/// All `fill[..]` lines, in order.
fn fill_lines(out: &str) -> Vec<String> {
    out.lines()
        .filter(|line| line.starts_with("fill["))
        .map(str::to_string)
        .collect()
}

/// Extract the `key=value` integer field from a fill line.
fn fill_field(line: &str, key: &str) -> i64 {
    let needle = format!("{key}=");
    let start = line
        .find(&needle)
        .unwrap_or_else(|| panic!("fill line missing `{key}`:\n{line}"))
        + needle.len();
    let rest = &line[start..];
    let end = rest.find(' ').unwrap_or(rest.len());
    rest[..end]
        .parse::<i64>()
        .unwrap_or_else(|_| panic!("`{key}` is not an integer in:\n{line}"))
}

#[test]
fn defaults_match_the_syrs_values() {
    let out = stdout(&run(&["defaults"]));
    // The published SyRS constants are surfaced for an operator to confirm.
    assert!(out.contains("DEFAULT_SLIPPAGE_BPS=5"), "{out}");
    assert!(out.contains("DEFAULT_SPREAD_FALLBACK_BPS=10"), "{out}");
    assert!(
        out.contains("IB_TIERED_RATE_CENTIMINOR_PER_SHARE=35"),
        "{out}"
    );
    assert!(out.contains("IB_TIERED_MIN_PER_ORDER_MINOR=35"), "{out}");
    assert!(out.contains("IB_TIERED_MAX_PCT_BPS=100"), "{out}");
    // The default model of each family is the named SyRS baseline...
    assert_eq!(value_after(&out, "default-commission:").trim(), "IbTiered");
    assert_eq!(
        value_after(&out, "default-slippage:").trim(),
        "NotionalBps(5)"
    );
    assert_eq!(
        value_after(&out, "default-spread:").trim(),
        "ObservedOrFallbackBps(10)"
    );
    // ...and the derived Default IS that family.
    assert_eq!(
        value_after(&out, "default-config-matches-syrs:").trim(),
        "true"
    );
}

#[test]
fn default_run_applies_the_syrs_cost_family() {
    let out = stdout(&run(&["run"]));
    let fills = fill_lines(&out);
    assert_eq!(fills.len(), 2, "one round trip = two fills:\n{out}");

    // BUY fill (bar 1 carries an observed spread of 40): commission 35 (IB tiered floor),
    // slippage 500 (5 bps of $10,000 notional), spread 2000 (half the 40-minor observed spread
    // per share * 100 shares) — the OBSERVED-spread path.
    assert_eq!(fill_field(&fills[0], "qty"), 100);
    assert_eq!(fill_field(&fills[0], "commission_minor"), 35);
    assert_eq!(fill_field(&fills[0], "slippage_minor"), 500);
    assert_eq!(fill_field(&fills[0], "spread_impact_minor"), 2_000);
    assert_eq!(fill_field(&fills[0], "total_minor"), 2_535);

    // SELL fill (bar 3 carries NO observed spread): spread 1000 = the 10-bps FALLBACK of $10,000.
    assert_eq!(fill_field(&fills[1], "qty"), -100);
    assert_eq!(fill_field(&fills[1], "commission_minor"), 35);
    assert_eq!(fill_field(&fills[1], "slippage_minor"), 500);
    assert_eq!(fill_field(&fills[1], "spread_impact_minor"), 1_000);
    assert_eq!(fill_field(&fills[1], "total_minor"), 1_535);

    assert_eq!(int_after(&out, "total-cost-minor:"), 4_070);
    // Prices are flat, so the final equity is exactly the starting cash minus the total cost.
    assert_eq!(
        int_after(&out, "final-equity-minor:"),
        STARTING_CASH_MINOR - 4_070
    );
}

#[test]
fn observed_spread_path_is_distinct_from_the_fallback() {
    // Non-vacuity for the SYS-15c default: the buy fill (observed spread present) charges 2000 and
    // the sell fill (no observed spread) charges the 1000 fallback — so the observed-spread path is
    // demonstrably USED, not collapsed to the fallback.
    let out = stdout(&run(&["run"]));
    let fills = fill_lines(&out);
    assert_ne!(
        fill_field(&fills[0], "spread_impact_minor"),
        fill_field(&fills[1], "spread_impact_minor"),
        "observed-spread fill must differ from the fallback fill:\n{out}"
    );
}

#[test]
fn override_changes_costs_without_changing_the_strategy() {
    let default_out = stdout(&run(&["run"]));
    let override_out = stdout(&run(&[
        "run",
        "--commission",
        "per-trade:99",
        "--slippage",
        "none",
        "--spread",
        "fixed:25",
    ]));

    // The cost config changed...
    assert!(
        override_out.contains("commission=PerTrade(99)"),
        "{override_out}"
    );
    assert!(
        override_out.contains("spread=FixedBps(25)"),
        "{override_out}"
    );

    // ...and the costs changed with it: PerTrade(99) per fill, no slippage, FixedBps(25) ignores
    // the observed spread (25 bps of $10,000 = 2500 on BOTH fills).
    let fills = fill_lines(&override_out);
    assert_eq!(fill_field(&fills[0], "commission_minor"), 99);
    assert_eq!(fill_field(&fills[0], "slippage_minor"), 0);
    assert_eq!(fill_field(&fills[0], "spread_impact_minor"), 2_500);
    assert_eq!(fill_field(&fills[1], "spread_impact_minor"), 2_500);
    assert_eq!(int_after(&override_out, "total-cost-minor:"), 5_198);
    assert_ne!(
        int_after(&override_out, "total-cost-minor:"),
        int_after(&default_out, "total-cost-minor:"),
        "an override must change the realized cost"
    );

    // ...but the STRATEGY did not: both runs produce the identical two fills (qty 100 then -100) at
    // the same bars and prices — the override lives on the request, not in the strategy (SYS-15d).
    let default_fills = fill_lines(&default_out);
    for (d, o) in default_fills.iter().zip(fills.iter()) {
        assert_eq!(fill_field(d, "ts"), fill_field(o, "ts"));
        assert_eq!(fill_field(d, "qty"), fill_field(o, "qty"));
        assert_eq!(fill_field(d, "price_minor"), fill_field(o, "price_minor"));
    }
}

#[test]
fn frictionless_override_zeroes_every_cost() {
    let out = stdout(&run(&[
        "run",
        "--commission",
        "none",
        "--slippage",
        "none",
        "--spread",
        "none",
    ]));
    for fill in fill_lines(&out) {
        assert_eq!(fill_field(&fill, "total_minor"), 0, "{fill}");
    }
    assert_eq!(int_after(&out, "total-cost-minor:"), 0);
    // No costs ⇒ the flat-price round trip recovers exactly the starting cash.
    assert_eq!(int_after(&out, "final-equity-minor:"), STARTING_CASH_MINOR);
}

#[test]
fn identical_inputs_are_byte_identical_across_processes() {
    // Two fresh processes over identical flags must agree byte-for-byte (the SRS-BT-010 determinism
    // property the cost math must honor — integer-only, no platform randomness).
    let first = stdout(&run(&["run"]));
    let second = stdout(&run(&["run"]));
    assert_eq!(first, second);
}

#[test]
fn negative_override_parameter_fails_closed() {
    // A negative per-share rate is rejected by CostConfig::validate() inside the engine BEFORE any
    // fill — the run exits non-zero and prints no fill or final equity, so a misconfigured cost can
    // never fabricate cash.
    let output = run(&["run", "--commission", "per-share:-1,35"]);
    assert!(
        !output.status.success(),
        "negative parameter must fail closed"
    );
    let out = stdout(&output);
    assert!(
        fill_lines(&out).is_empty(),
        "no fill may be printed:\n{out}"
    );
    assert!(
        !out.contains("final-equity-minor:"),
        "no equity may be printed on a rejected run:\n{out}"
    );
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

#[test]
fn malformed_cost_spec_fails_closed() {
    // A non-integer cost parameter is a parse error, not a silently-ignored flag.
    let output = run(&["run", "--slippage", "bps:abc"]);
    assert!(!output.status.success());
}
