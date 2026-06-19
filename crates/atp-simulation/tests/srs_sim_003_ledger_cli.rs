//! SRS-SIM-003 virtual-ledger operator-CLI integration test.
//!
//! Drives the `sim003_ledger_cli` binary the way an operator would — in fresh OS processes via the
//! `CARGO_BIN_EXE_sim003_ledger_cli` path Cargo wires for integration tests — and asserts the
//! SRS-SIM-003 acceptance criterion end to end: "quantity, average cost, unrealized P&L, realized
//! P&L, and commission paid are isolated per paper strategy and independent of IB account positions".
//!
//!   1. `defaults` shows a fresh ledger book (zero strategies, no IB account position) and a flat
//!      virtual position (zero quantity / no average cost / zero realized P&L / zero commission).
//!   2. `isolate` opens the SAME symbol under two paper strategies with DIFFERENT real fills, prints
//!      each strategy's five quantities, and proves the ledgers are isolated per strategy
//!      (`account-independent:true`, `ledger-isolation:true`) — with the `--full` reconciliation
//!      agreeing.
//!
//! Plus the money-safety boundary: each `--inject` fault makes the ledger fail closed before any
//! mutation (non-zero exit, no isolation line — a corrupt fill can never produce a proof), a
//! non-positive lot is rejected at parse, and identical inputs in two fresh processes are
//! byte-identical.

use std::process::{Command, Output};

/// The `[[bin]]` path Cargo wires for integration tests.
const CLI: &str = env!("CARGO_BIN_EXE_sim003_ledger_cli");

fn run(args: &[&str]) -> Output {
    Command::new(CLI)
        .args(args)
        .output()
        .expect("the sim003_ledger_cli binary runs")
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("stdout is utf-8")
}

/// The value after a `prefix` line (e.g. `ledger-isolation:`), failing if absent.
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

/// The `strategy[name] ...` line, failing if absent.
fn strategy_line(out: &str, name: &str) -> String {
    let needle = format!("strategy[{name}]");
    out.lines()
        .find(|line| line.starts_with(&needle))
        .map(str::to_string)
        .unwrap_or_else(|| panic!("output missing a `{needle}` line:\n{out}"))
}

/// Extract the integer `key:value` field from a space-separated strategy line.
fn field(line: &str, key: &str) -> i64 {
    let needle = format!("{key}:");
    let token = line
        .split_whitespace()
        .find(|tok| tok.starts_with(&needle))
        .unwrap_or_else(|| panic!("line missing `{key}`:\n{line}"));
    token[needle.len()..]
        .parse::<i64>()
        .unwrap_or_else(|_| panic!("`{key}` is not an integer in:\n{line}"))
}

#[test]
fn defaults_show_a_flat_book_and_no_ib_account() {
    let out = stdout(&run(&["defaults"]));
    // A fresh book has no strategy ledgers and (structurally) no IB account position.
    assert_eq!(int_after(&out, "book-strategy-count:"), 0, "{out}");
    assert_eq!(value_after(&out, "ib-account-positions:"), "none", "{out}");
    // A fresh virtual position is flat across all five AC quantities.
    assert_eq!(int_after(&out, "default-position-quantity:"), 0, "{out}");
    assert_eq!(
        value_after(&out, "default-position-average-cost-minor:"),
        "none",
        "{out}"
    );
    assert_eq!(
        int_after(&out, "default-position-realized-pnl-minor:"),
        0,
        "{out}"
    );
    assert_eq!(
        int_after(&out, "default-position-commission-paid-minor:"),
        0,
        "{out}"
    );
}

#[test]
fn isolate_proves_per_strategy_isolation() {
    let out = stdout(&run(&["isolate"]));
    assert_eq!(int_after(&out, "strategy-count:"), 2, "{out}");
    assert_eq!(value_after(&out, "account-independent:"), "true", "{out}");
    assert_eq!(value_after(&out, "ledger-isolation:"), "true", "{out}");
}

#[test]
fn isolate_prints_all_five_quantities_per_strategy() {
    // Each strategy line carries the five SRS-SIM-003 quantities; both strategies hold the SAME
    // symbol but the figures are genuinely populated and different, so this is not a vacuous proof.
    let out = stdout(&run(&["isolate"]));
    let alpha = strategy_line(&out, "alpha");
    let beta = strategy_line(&out, "beta");
    for line in [&alpha, &beta] {
        for key in [
            "quantity",
            "average-cost-minor",
            "unrealized-pnl-minor",
            "realized-pnl-minor",
            "commission-paid-minor",
        ] {
            // Every quantity must be present and parse as an integer.
            let _ = field(line, key);
        }
    }
    // alpha went long then partially sold: a positive remaining quantity, a realized gain, and a
    // non-zero commission across its two fills.
    assert!(
        field(&alpha, "quantity") > 0,
        "alpha holds a long:\n{alpha}"
    );
    assert!(
        field(&alpha, "realized-pnl-minor") > 0,
        "alpha realized a gain on its partial sell:\n{alpha}"
    );
    assert!(
        field(&alpha, "commission-paid-minor") > 0,
        "alpha paid commission on real fills:\n{alpha}"
    );
    // beta went short: a negative quantity, no realized P&L yet, and its own commission.
    assert!(field(&beta, "quantity") < 0, "beta holds a short:\n{beta}");
    assert!(
        field(&beta, "commission-paid-minor") > 0,
        "beta paid its own commission:\n{beta}"
    );
}

#[test]
fn alpha_and_beta_hold_independent_positions() {
    // The same symbol resolves to genuinely different positions per strategy — the structural
    // signature of a per-strategy virtual ledger rather than one shared (IB) account position.
    let out = stdout(&run(&["isolate"]));
    let alpha = strategy_line(&out, "alpha");
    let beta = strategy_line(&out, "beta");
    assert_ne!(
        field(&alpha, "quantity"),
        field(&beta, "quantity"),
        "two strategies must not share one account quantity:\n{out}"
    );
    assert_eq!(value_after(&out, "account-independent:"), "true", "{out}");
}

#[test]
fn isolate_respects_operator_lots_and_symbol() {
    let out = stdout(&run(&[
        "isolate", "--lot-a", "200", "--lot-b", "50", "--symbol", "msft", "--mark", "9900",
    ]));
    assert_eq!(value_after(&out, "symbol:"), "msft", "{out}");
    assert_eq!(int_after(&out, "mark-minor:"), 9_900, "{out}");
    assert_eq!(value_after(&out, "ledger-isolation:"), "true", "{out}");
    // beta shorts the full --lot-b.
    let beta = strategy_line(&out, "beta");
    assert_eq!(field(&beta, "quantity"), -50, "{beta}");
}

#[test]
fn full_reconciles_alphas_actual_ledger_with_simulated_cash() {
    // With `--full`, ALPHA'S OWN ledger (the one the isolation workflow mutated) is closed to flat and
    // reconciled: gross realized P&L minus the FULL transaction cost equals the sum of every alpha
    // fill's cash delta, with a non-zero cost. Asserting recon-strategy is alpha pins that the
    // reconciliation covers the real workflow ledger, not a fresh stand-in strategy.
    let out = stdout(&run(&["isolate", "--full"]));
    assert_eq!(value_after(&out, "ledger-isolation:"), "true", "{out}");
    assert_eq!(value_after(&out, "recon-strategy:"), "alpha", "{out}");
    assert_eq!(int_after(&out, "recon-quantity:"), 0, "{out}");
    assert!(
        int_after(&out, "recon-transaction-cost-minor:") > 0,
        "{out}"
    );
    assert_eq!(
        int_after(&out, "recon-net-minor:"),
        int_after(&out, "recon-simulated-cash-minor:"),
        "alpha's net P&L must equal its simulated cash:\n{out}"
    );
    assert_eq!(value_after(&out, "recon-reconciles:"), "true", "{out}");
}

#[test]
fn identical_inputs_are_byte_identical_across_processes() {
    // The ledger is integer-only with no clock or randomness, so two fresh processes over identical
    // flags agree byte-for-byte.
    let first = stdout(&run(&["isolate", "--full"]));
    let second = stdout(&run(&["isolate", "--full"]));
    assert_eq!(first, second);
}

/// An `--inject` fault must make the ledger fail closed: non-zero exit, NO isolation/independence
/// line, and a `failed closed` message — a corrupt fill can never produce an isolation proof.
fn assert_inject_fails_closed(fault: &str) {
    let output = run(&["isolate", "--inject", fault]);
    assert!(!output.status.success(), "inject {fault} must fail closed");
    let out = stdout(&output);
    assert!(
        !out.contains("ledger-isolation:"),
        "no isolation line may be printed for inject {fault}:\n{out}"
    );
    assert!(
        !out.contains("account-independent:"),
        "no independence line may be printed for inject {fault}:\n{out}"
    );
    let combined = out + &String::from_utf8(output.stderr).expect("stderr utf-8");
    assert!(
        combined.contains("failed closed"),
        "inject {fault} must report the ledger failing closed:\n{combined}"
    );
    // EVERY injected fault is rejected BEFORE any mutation, so the book is left empty -- no fault
    // path (not even nonpositive-mark) may apply a "setup" fill that mutates the ledger first.
    assert!(
        combined.contains("book unchanged (strategy-count=0)"),
        "inject {fault} must leave the book unchanged at strategy-count=0:\n{combined}"
    );
}

#[test]
fn inject_nonpositive_price_fails_closed() {
    assert_inject_fails_closed("nonpositive-price");
}

#[test]
fn inject_zero_quantity_fails_closed() {
    assert_inject_fails_closed("zero-quantity");
}

#[test]
fn inject_empty_symbol_fails_closed() {
    assert_inject_fails_closed("empty-symbol");
}

#[test]
fn inject_nonpositive_mark_fails_closed() {
    assert_inject_fails_closed("nonpositive-mark");
}

#[test]
fn inject_negative_commission_fails_closed() {
    assert_inject_fails_closed("negative-commission");
}

#[test]
fn unknown_fault_fails_closed() {
    let output = run(&["isolate", "--inject", "make-money"]);
    assert!(!output.status.success());
}

#[test]
fn nonpositive_lot_a_fails_closed_with_no_isolation_claim() {
    // A non-positive lot leaves nothing to isolate; the CLI must NOT print a vacuous isolation proof.
    let output = run(&["isolate", "--lot-a", "0"]);
    assert!(!output.status.success(), "zero lot-a must fail closed");
    let out = stdout(&output);
    assert!(
        !out.contains("ledger-isolation:true"),
        "a zero-lot run must never assert isolation:\n{out}"
    );
}

#[test]
fn negative_lot_b_fails_closed() {
    let output = run(&["isolate", "--lot-b", "-50"]);
    assert!(!output.status.success(), "negative lot-b must fail closed");
    assert!(!stdout(&output).contains("ledger-isolation:true"));
}

/// A `--mark` that cannot build a valid (strictly-positive, non-overflowing) bid/ask book must be
/// rejected at parse: non-zero exit, NO isolation line — the proof never runs over a corrupt quote.
fn assert_mark_rejected(mark: &str) {
    let output = run(&["isolate", "--mark", mark]);
    assert!(!output.status.success(), "mark {mark} must fail closed");
    let out = stdout(&output);
    assert!(
        !out.contains("ledger-isolation:"),
        "mark {mark} must print no isolation line:\n{out}"
    );
}

#[test]
fn mark_zero_fails_closed() {
    // mark 0 is a non-positive quote.
    assert_mark_rejected("0");
}

#[test]
fn mark_one_builds_a_nonpositive_bid_and_fails_closed() {
    // mark 1 -> bid = mark - 1 = 0, which the simulation layer rejects as a non-positive quote, so
    // the CLI must fail closed rather than prove isolation over an impossible book.
    assert_mark_rejected("1");
}

#[test]
fn mark_overflow_boundary_fails_closed() {
    // mark i64::MAX -> ask = mark + 1 would overflow; rejected at parse.
    assert_mark_rejected(&i64::MAX.to_string());
}

#[test]
fn mark_two_is_the_valid_lower_boundary() {
    // mark 2 -> bid = 1 (> 0), ask = 3: the smallest mark that builds a valid book, so isolation
    // proves normally.
    let out = stdout(&run(&["isolate", "--mark", "2"]));
    assert_eq!(value_after(&out, "ledger-isolation:"), "true", "{out}");
}

#[test]
fn unknown_flag_fails_closed() {
    let output = run(&["isolate", "--nope", "1"]);
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
        vec!["isolate", "--help"],
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
