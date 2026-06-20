//! SRS-SIM-001 paper order-intake operator-CLI integration test.
//!
//! Drives the `sim001_paper_order_cli` binary the way an operator would — in fresh OS processes via
//! the `CARGO_BIN_EXE_sim001_paper_order_cli` path Cargo wires for integration tests — and asserts
//! the SRS-SIM-001 acceptance criterion end to end: "Market, limit, stop, stop-limit, equity, option,
//! and multi-leg orders are processed by the simulation engine and create no IB API order calls".
//!
//!   1. `types` accepts each SYS-3 order type as a single equity order and proves each routes to the
//!      internal simulation engine (`all-order-types-routed:true`).
//!   2. `assets` accepts an equity and an option order and proves both route internally
//!      (`both-asset-classes-routed:true`).
//!   3. `multileg` accepts a two-leg option spread and proves it routes as ONE composite (SYS-4)
//!      (`composite-routed:true`).
//!   4. `no-broker` sweeps every order shape and proves every routing is the internal simulation
//!      engine — none reaches a brokerage (`no-ib-order-calls:true`).
//!
//! Plus the safety boundary: each `--inject` fault makes intake fail closed before any routing
//! (non-zero exit, NO proof line — a malformed order can never produce a routing proof), and identical
//! inputs in two fresh processes are byte-identical.

use std::process::{Command, Output};

/// The `[[bin]]` path Cargo wires for integration tests.
const CLI: &str = env!("CARGO_BIN_EXE_sim001_paper_order_cli");

/// Every `:true` proof line — none may appear under an injected fault or a rejected parse.
const PROOF_LINES: [&str; 4] = [
    "all-order-types-routed:true",
    "both-asset-classes-routed:true",
    "composite-routed:true",
    "no-ib-order-calls:true",
];

/// Every fault the CLI accepts.
const FAULTS: [&str; 7] = [
    "empty-symbol",
    "nonpositive-quantity",
    "nonpositive-limit",
    "nonpositive-stop",
    "empty-multileg",
    "single-leg-composite",
    "non-option-composite-leg",
];

fn run(args: &[&str]) -> Output {
    Command::new(CLI)
        .args(args)
        .output()
        .expect("the sim001_paper_order_cli binary runs")
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("stdout is utf-8")
}

fn combined(output: &Output) -> String {
    stdout(output) + &String::from_utf8(output.stderr.clone()).expect("stderr is utf-8")
}

/// The value after a `prefix` line (e.g. `no-ib-order-calls:`), failing if absent.
fn value_after(out: &str, prefix: &str) -> String {
    out.lines()
        .find(|line| line.starts_with(prefix))
        .map(|line| line[prefix.len()..].trim().to_string())
        .unwrap_or_else(|| panic!("output missing a `{prefix}` line:\n{out}"))
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

/// The `prefix[name] ...` line (e.g. `type[market]`), failing if absent.
fn tagged_line(out: &str, prefix: &str, name: &str) -> String {
    let needle = format!("{prefix}[{name}]");
    out.lines()
        .find(|line| line.starts_with(&needle))
        .map(str::to_string)
        .unwrap_or_else(|| panic!("output missing a `{needle}` line:\n{out}"))
}

// --------------------------------------------------------------------------- //
// Happy paths
// --------------------------------------------------------------------------- //

#[test]
fn types_route_every_order_type_internally() {
    let out = stdout(&run(&["types"]));
    // Each SYS-3 order type routes to the internal simulation engine as one non-composite leg.
    for name in ["market", "limit", "stop", "stop-limit"] {
        let line = tagged_line(&out, "type", name);
        assert_eq!(field(&line, "routing"), "internal-simulation", "{line}");
        assert_eq!(field(&line, "legs"), "1", "{line}");
        assert_eq!(field(&line, "composite"), "false", "{line}");
        assert_eq!(field(&line, "routed"), "true", "{line}");
    }
    assert_eq!(
        value_after(&out, "all-order-types-routed:"),
        "true",
        "{out}"
    );
}

#[test]
fn assets_route_both_classes_internally() {
    let out = stdout(&run(&["assets"]));
    for name in ["equity", "option"] {
        let line = tagged_line(&out, "asset", name);
        assert_eq!(field(&line, "routing"), "internal-simulation", "{line}");
        assert_eq!(field(&line, "routed"), "true", "{line}");
    }
    assert_eq!(
        value_after(&out, "both-asset-classes-routed:"),
        "true",
        "{out}"
    );
}

#[test]
fn multileg_routes_as_one_composite() {
    let out = stdout(&run(&["multileg"]));
    let line = out
        .lines()
        .find(|l| l.starts_with("multileg "))
        .unwrap_or_else(|| panic!("no multileg line:\n{out}"));
    // A multi-leg order routes as ONE composite carrying exactly the two atomic legs (SYS-4).
    assert_eq!(field(line, "routing"), "internal-simulation", "{line}");
    assert_eq!(field(line, "legs"), "2", "{line}");
    assert_eq!(field(line, "composite"), "true", "{line}");
    assert_eq!(value_after(&out, "composite-routed:"), "true", "{out}");
}

#[test]
fn no_broker_sweep_routes_everything_internally() {
    let out = stdout(&run(&["no-broker"]));
    let line = out
        .lines()
        .find(|l| l.starts_with("no-broker "))
        .unwrap_or_else(|| panic!("no no-broker line:\n{out}"));
    // The sweep must be non-trivial (more than one order) and EVERY routing the internal simulation
    // engine, with ZERO reaching a brokerage.
    let swept: u64 = field(line, "swept").parse().expect("swept count");
    let internal: u64 = field(line, "internal-simulation")
        .parse()
        .expect("internal count");
    assert!(swept > 1, "the sweep must cover many order shapes:\n{line}");
    assert_eq!(
        internal, swept,
        "every order must route internally:\n{line}"
    );
    assert_eq!(
        field(line, "broker"),
        "0",
        "no order may reach a broker:\n{line}"
    );
    assert_eq!(value_after(&out, "no-ib-order-calls:"), "true", "{out}");
}

#[test]
fn identical_inputs_are_byte_identical_across_processes() {
    // Intake is integer-only with no clock or randomness, so two fresh processes over identical flags
    // agree byte-for-byte (deterministic operator evidence).
    for sub in [
        vec!["types"],
        vec!["assets"],
        vec!["multileg"],
        vec!["no-broker"],
    ] {
        let first = stdout(&run(&sub));
        let second = stdout(&run(&sub));
        assert_eq!(first, second, "subcommand {sub:?} is not deterministic");
    }
}

// --------------------------------------------------------------------------- //
// Fail-closed: every injected fault, on every proof subcommand
// --------------------------------------------------------------------------- //

/// An `--inject` fault must make intake fail closed: non-zero exit, NO proof line, and a
/// `failed closed` / `no order routed` message — a malformed order can never produce a routing proof.
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
        all.contains("no order routed"),
        "inject {fault} on {subcommand} must report no order routed:\n{all}"
    );
}

#[test]
fn every_fault_fails_closed_on_types() {
    for fault in FAULTS {
        assert_inject_fails_closed("types", fault);
    }
}

#[test]
fn faults_fail_closed_on_every_subcommand() {
    // The fault handler is shared, so a representative fault must fail closed on every proof
    // subcommand (no subcommand can leak a proof under a malformed order).
    for sub in ["assets", "multileg", "no-broker"] {
        assert_inject_fails_closed(sub, "empty-symbol");
        assert_inject_fails_closed(sub, "non-option-composite-leg");
    }
}

#[test]
fn unknown_fault_fails_closed() {
    let output = run(&["types", "--inject", "route-to-ib"]);
    assert!(
        !output.status.success(),
        "an unknown fault must fail closed"
    );
    assert!(!stdout(&output).contains("all-order-types-routed:true"));
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
    let output = run(&["types", "--nope"]);
    assert!(!output.status.success());
}

#[test]
fn missing_subcommand_fails_closed() {
    let output = run(&[]);
    assert!(!output.status.success());
}
