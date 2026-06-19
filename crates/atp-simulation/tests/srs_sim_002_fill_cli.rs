//! SRS-SIM-002 configurable fill-model operator-CLI integration test.
//!
//! Drives the `sim002_fill_cli` binary the way an operator would — in fresh OS processes via the
//! `CARGO_BIN_EXE_sim002_fill_cli` path Cargo wires for integration tests — and asserts the
//! SRS-SIM-002 acceptance criterion end to end: "Market, limit, stop, and stop-limit simulated fills
//! follow SYS-83 defaults and per-strategy configuration; fill volume constraints are enforced".
//!
//!   1. `defaults` prints the SYS-83 default fill-model config (immediate-on-cross) and resolves all
//!      four order types on a clean snapshot.
//!   2. `rules` proves each SYS-83 reference price holds (market@ask/bid, crossed limit@limit,
//!      triggered stop@market, triggered stop-limit@limit) — `sys83-rules-correct:true`.
//!   3. `config` proves the per-strategy fill model is behavior-changing — the two limit models
//!      disagree on the SAME touch snapshot (`config-divergent:true`).
//!   4. `volume` proves the SYS-87b cap both per-order and in aggregate (`volume-capped:true`).
//!
//! Plus the money-safety boundary: each `--inject` fault makes the fill model fail closed before any
//! fill decision (non-zero exit, NO proof line — corrupt market data or a malformed order can never
//! produce a fill proof), the `volume` non-vacuity guards reject a request within the bar volume and
//! a degenerate bar, and identical inputs in two fresh processes are byte-identical.

use std::process::{Command, Output};

/// The `[[bin]]` path Cargo wires for integration tests.
const CLI: &str = env!("CARGO_BIN_EXE_sim002_fill_cli");

/// Every `:true` proof line — none may appear under an injected fault or a rejected parse.
const PROOF_LINES: [&str; 3] = [
    "sys83-rules-correct:true",
    "config-divergent:true",
    "volume-capped:true",
];

fn run(args: &[&str]) -> Output {
    Command::new(CLI)
        .args(args)
        .output()
        .expect("the sim002_fill_cli binary runs")
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("stdout is utf-8")
}

fn combined(output: &Output) -> String {
    stdout(output) + &String::from_utf8(output.stderr.clone()).expect("stderr is utf-8")
}

/// The value after a `prefix` line (e.g. `volume-capped:`), failing if absent.
fn value_after(out: &str, prefix: &str) -> String {
    out.lines()
        .find(|line| line.starts_with(prefix))
        .map(|line| line[prefix.len()..].trim().to_string())
        .unwrap_or_else(|| panic!("output missing a `{prefix}` line:\n{out}"))
}

/// The `rule[name] ...` line, failing if absent.
fn rule_line(out: &str, name: &str) -> String {
    let needle = format!("rule[{name}]");
    out.lines()
        .find(|line| line.starts_with(&needle))
        .map(str::to_string)
        .unwrap_or_else(|| panic!("output missing a `{needle}` line:\n{out}"))
}

/// Extract a `key:value` field from a space-separated line.
fn field<'a>(line: &'a str, key: &str) -> &'a str {
    let needle = format!("{key}:");
    let token = line
        .split_whitespace()
        .find(|tok| tok.starts_with(&needle))
        .unwrap_or_else(|| panic!("line missing `{key}`:\n{line}"));
    &token[needle.len()..]
}

// --------------------------------------------------------------------------- //
// Happy paths
// --------------------------------------------------------------------------- //

#[test]
fn defaults_print_the_syrs_baseline_and_resolve_all_order_types() {
    let out = stdout(&run(&["defaults"]));
    // The SYS-83b default limit model is immediate-on-cross, and the named default equals `Default`.
    assert_eq!(
        value_after(&out, "default-limit-fill-model:"),
        "immediate-on-cross",
        "{out}"
    );
    assert_eq!(
        value_after(&out, "default-config-is-syrs-baseline:"),
        "true",
        "{out}"
    );
    // All four order types resolve to a fill on the clean snapshot.
    for label in ["market", "limit", "stop", "stop-limit"] {
        let needle = format!("default[{label}] decision:filled");
        assert!(
            out.lines().any(|line| line.starts_with(&needle)),
            "default[{label}] must fill on the clean snapshot:\n{out}"
        );
    }
}

#[test]
fn rules_prove_every_sys83_reference_price() {
    let out = stdout(&run(&["rules"]));
    // Each SYS-83 rule fills at its predicted reference price.
    for name in [
        "market-buy",
        "market-sell",
        "limit-buy",
        "stop-buy",
        "stop-limit-buy",
    ] {
        let line = rule_line(&out, name);
        assert_eq!(field(&line, "correct"), "true", "rule {name} wrong:\n{line}");
        // The actual fill price must equal the expected SYS-83 reference price on the same line.
        assert_eq!(
            field(&line, "expected_fill_price_minor"),
            field(&line, "fill_price_minor"),
            "rule {name} did not fill at its SYS-83 reference price:\n{line}"
        );
    }
    // The market buy fills at the ask (10000) and the market sell at the bid (9990): the side-
    // dependent SYS-83a reference price, so the proof is not vacuous over one price.
    assert_eq!(
        field(&rule_line(&out, "market-buy"), "fill_price_minor"),
        "10000",
        "{out}"
    );
    assert_eq!(
        field(&rule_line(&out, "market-sell"), "fill_price_minor"),
        "9990",
        "{out}"
    );
    // A crossed limit fills at the LIMIT price (10050), not the ask — the conservative reference.
    assert_eq!(
        field(&rule_line(&out, "limit-buy"), "fill_price_minor"),
        "10050",
        "{out}"
    );
    assert_eq!(value_after(&out, "sys83-rules-correct:"), "true", "{out}");
}

#[test]
fn config_proves_the_two_models_diverge() {
    let out = stdout(&run(&["config"]));
    // On the touch snapshot, immediate-on-cross fills while require-through-cross does not.
    assert!(
        out.contains("config[immediate-on-cross] decision:filled"),
        "immediate-on-cross must fill the touch:\n{out}"
    );
    assert!(
        out.contains("config[require-through-cross] decision:no-fill reason:limit-not-crossed"),
        "require-through-cross must NOT fill the touch:\n{out}"
    );
    assert_eq!(value_after(&out, "config-divergent:"), "true", "{out}");
}

#[test]
fn volume_caps_single_and_aggregate() {
    let out = stdout(&run(&["volume"]));
    // Single order: requested 800 against a 500-share bar fills only 500 (a partial fill).
    let single = out
        .lines()
        .find(|l| l.starts_with("single "))
        .unwrap_or_else(|| panic!("no single line:\n{out}"));
    assert_eq!(field(single, "fill-quantity"), "500", "{single}");
    assert_eq!(field(single, "capped"), "true", "{single}");
    // Aggregate: the sum of fills across the bar equals the observed volume and never exceeds it.
    let agg = out
        .lines()
        .find(|l| l.starts_with("aggregate "))
        .unwrap_or_else(|| panic!("no aggregate line:\n{out}"));
    assert_eq!(field(agg, "aggregate-fill"), "500", "{agg}");
    assert_eq!(field(agg, "observed-volume"), "500", "{agg}");
    assert!(
        agg.contains("order-three:decision:no-fill reason:zero-volume"),
        "the trailing order must find no volume:\n{agg}"
    );
    assert_eq!(value_after(&out, "volume-capped:"), "true", "{out}");
}

#[test]
fn volume_respects_operator_qty_and_volume() {
    let out = stdout(&run(&["volume", "--qty", "1000", "--volume", "250"]));
    let single = out
        .lines()
        .find(|l| l.starts_with("single "))
        .unwrap_or_else(|| panic!("no single line:\n{out}"));
    assert_eq!(field(single, "fill-quantity"), "250", "{single}");
    assert_eq!(value_after(&out, "volume-capped:"), "true", "{out}");
}

#[test]
fn identical_inputs_are_byte_identical_across_processes() {
    // The fill model is integer-only with no clock or randomness, so two fresh processes over
    // identical flags agree byte-for-byte (deterministic operator evidence).
    for sub in [
        vec!["defaults"],
        vec!["rules"],
        vec!["config"],
        vec!["volume"],
    ] {
        let first = stdout(&run(&sub));
        let second = stdout(&run(&sub));
        assert_eq!(first, second, "subcommand {sub:?} is not deterministic");
    }
}

// --------------------------------------------------------------------------- //
// Fail-closed: every injected fault, on every proof subcommand
// --------------------------------------------------------------------------- //

/// An `--inject` fault must make the fill model fail closed: non-zero exit, NO proof line, and a
/// `failed closed` / `no fill produced` message — corrupt data can never produce a fill proof.
fn assert_inject_fails_closed(subcommand: &str, fault: &str) {
    let output = run(&[subcommand, "--inject", fault]);
    assert!(
        !output.status.success(),
        "inject {fault} on {subcommand} must fail closed"
    );
    let all = combined(&output);
    for proof in PROOF_LINES {
        assert!(
            !all.contains(proof),
            "no `{proof}` line may appear for inject {fault} on {subcommand}:\n{all}"
        );
    }
    assert!(
        all.contains("failed closed"),
        "inject {fault} on {subcommand} must report failing closed:\n{all}"
    );
    assert!(
        all.contains("no fill produced"),
        "inject {fault} on {subcommand} must report no fill produced:\n{all}"
    );
}

#[test]
fn every_fault_fails_closed_on_rules() {
    for fault in [
        "nonpositive-quote",
        "crossed-book",
        "negative-volume",
        "zero-quantity",
        "nonpositive-limit",
        "nonpositive-stop",
        "budget-mismatch",
    ] {
        assert_inject_fails_closed("rules", fault);
    }
}

#[test]
fn faults_fail_closed_on_config_and_volume() {
    // The fault handler is shared, so a representative fault must fail closed on every proof
    // subcommand (no subcommand can leak a proof under a fault).
    for sub in ["config", "volume"] {
        assert_inject_fails_closed(sub, "crossed-book");
        assert_inject_fails_closed(sub, "budget-mismatch");
    }
}

#[test]
fn unknown_fault_fails_closed() {
    let output = run(&["rules", "--inject", "make-money"]);
    assert!(!output.status.success(), "an unknown fault must fail closed");
    assert!(!stdout(&output).contains("sys83-rules-correct:true"));
}

// --------------------------------------------------------------------------- //
// Non-vacuity: the `volume` guards reject a degenerate proof
// --------------------------------------------------------------------------- //

#[test]
fn volume_qty_not_exceeding_bar_is_rejected() {
    // A request within the bar volume fills in full, so it cannot demonstrate the single-order cap.
    let output = run(&["volume", "--qty", "200", "--volume", "500"]);
    assert!(!output.status.success(), "qty <= volume must be rejected");
    assert!(
        !stdout(&output).contains("volume-capped:true"),
        "a non-capping request must never assert volume-capped:\n{}",
        stdout(&output)
    );
}

#[test]
fn volume_zero_qty_is_rejected() {
    let output = run(&["volume", "--qty", "0", "--volume", "500"]);
    assert!(!output.status.success(), "zero qty must be rejected");
}

#[test]
fn volume_degenerate_bar_is_rejected() {
    // A bar of one share cannot exercise a genuine aggregate cap with a zero-volume tail.
    let output = run(&["volume", "--volume", "1"]);
    assert!(!output.status.success(), "a degenerate bar must be rejected");
    assert!(!stdout(&output).contains("volume-capped:true"));
}

// --------------------------------------------------------------------------- //
// Argument hygiene
// --------------------------------------------------------------------------- //

#[test]
fn unknown_subcommand_fails_closed() {
    let output = run(&["frobnicate"]);
    assert!(!output.status.success());
}

#[test]
fn unknown_flag_fails_closed() {
    let output = run(&["rules", "--nope"]);
    assert!(!output.status.success());
}

#[test]
fn defaults_takes_no_arguments() {
    let output = run(&["defaults", "--inject", "crossed-book"]);
    assert!(
        !output.status.success(),
        "defaults takes no arguments and must reject flags"
    );
}

#[test]
fn missing_subcommand_fails_closed() {
    let output = run(&[]);
    assert!(!output.status.success());
}
