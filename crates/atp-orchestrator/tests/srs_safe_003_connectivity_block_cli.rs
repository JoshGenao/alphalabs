//! SRS-SAFE-003 connectivity-block operator-CLI integration test.
//!
//! Drives the `safe003_connectivity_block_cli` binary the way an operator would — in fresh OS
//! processes via the `CARGO_BIN_EXE_safe003_connectivity_block_cli` path Cargo wires for integration
//! tests — and asserts the SRS-SAFE-003 acceptance criterion through the production authority chain:
//! an IB-unreachable (or scheduled-restart) state refuses a designated-live submission with
//! `CONNECTIVITY_BLOCKED`, creates ZERO IB orders, requests one reconnect, and publishes one
//! `ConnectivityEvent`; a `Connected` gate routes the same order through; and a non-designated paper
//! order never touches IB.
//!
//! Plus the non-vacuity boundary: `prove-block --inject connected` makes the gate reachable, so the
//! order routes through and no block can be derived (fail closed, NO proof line); symmetrically
//! `routes-when-connected --inject <blocked>` forces a block. Unknown subcommands, flags, states, and
//! faults fail closed, and identical inputs are byte-identical across processes.

use std::process::{Command, Output};

/// The `[[bin]]` path Cargo wires for integration tests.
const CLI: &str = env!("CARGO_BIN_EXE_safe003_connectivity_block_cli");

/// Every `:true` proof headline — none may appear under an injected fault or a rejected parse.
const PROOF_LINES: [&str; 2] = [
    "connectivity-block-proven:true",
    "connectivity-routes-when-connected:true",
];

/// True if EITHER exact success sentinel appears anywhere in the output, even mid-line. A
/// safety-evidence CLI must never emit a success token on a failure path, because an operator or CI
/// check that greps the RAW output (rather than parsing standalone lines + exit status) would
/// otherwise false-positive. The USAGE/error text must therefore not contain the `:true` sentinels
/// either — so a success line is BOTH a standalone `:true` line AND the only place the sentinel
/// appears at all.
fn contains_success_sentinel(out: &str) -> bool {
    PROOF_LINES.iter().any(|sentinel| out.contains(sentinel))
}

fn run(args: &[&str]) -> Output {
    Command::new(CLI)
        .args(args)
        .output()
        .expect("the safe003_connectivity_block_cli binary runs")
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("stdout is utf-8")
}

fn combined(output: &Output) -> String {
    stdout(output) + &String::from_utf8(output.stderr.clone()).expect("stderr is utf-8")
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

/// The first line starting with `needle`, failing if absent.
fn line_with<'a>(out: &'a str, needle: &str) -> &'a str {
    out.lines()
        .find(|line| line.starts_with(needle))
        .unwrap_or_else(|| panic!("output missing a line starting `{needle}`:\n{out}"))
}

/// The `prefix[name] ...` line (e.g. `contrast[non-designated]`), failing if absent.
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

/// Shared assertions for a `prove-block` success under a blocked `state` whose event's
/// `scheduled_restart` flag must equal `scheduled`.
fn assert_block_proof(state: &str, scheduled: &str) {
    let output = run(&["prove-block", "--state", state]);
    assert!(
        output.status.success(),
        "prove-block --state {state} must succeed:\n{}",
        combined(&output)
    );
    let out = stdout(&output);

    let srs = line_with(&out, "srs:");
    assert_eq!(field(srs, "state"), state, "{srs}");
    assert_eq!(field(srs, "transports"), "FIXTURE", "{srs}");
    assert_eq!(field(srs, "designated"), "live-alpha", "{srs}");

    let outcome = line_with(&out, "outcome:");
    assert_eq!(field(outcome, "outcome"), "BLOCKED", "{outcome}");
    assert_eq!(
        field(outcome, "category"),
        "CONNECTIVITY_BLOCKED",
        "{outcome}"
    );
    assert_eq!(
        field(outcome, "error_type"),
        "IbGatewayUnreachable",
        "{outcome}"
    );
    assert_eq!(field(outcome, "message-nonempty"), "true", "{outcome}");
    assert_eq!(field(outcome, "traces-safe003"), "true", "{outcome}");

    let witness = line_with(&out, "witness ");
    // The wire-attempt witness: a blocked submission created NO IB order (not merely "no receipt").
    assert_eq!(field(witness, "ib-orders-created"), "0", "{witness}");
    assert_eq!(field(witness, "reconnects"), "1", "{witness}");
    assert_eq!(field(witness, "events"), "1", "{witness}");
    assert_eq!(field(witness, "scheduled_restart"), scheduled, "{witness}");
    assert_eq!(field(witness, "event-strategy"), "live-alpha", "{witness}");

    let contrast = tagged_line(&out, "contrast", "non-designated");
    assert_eq!(
        field(&contrast, "route"),
        "internal_simulation",
        "{contrast}"
    );
    assert!(
        field(&contrast, "sim-receipt").starts_with("paper-"),
        "the non-designated contrast must simulate:\n{contrast}"
    );

    assert!(
        out.lines()
            .any(|l| l.trim() == "connectivity-block-proven:true"),
        "missing the proof headline:\n{out}"
    );
}

#[test]
fn unreachable_state_blocks_with_full_envelope() {
    assert_block_proof("unreachable", "false");
}

#[test]
fn scheduled_restart_state_sets_the_suppression_flag() {
    assert_block_proof("scheduled-restart", "true");
}

#[test]
fn default_state_is_unreachable() {
    let output = run(&["prove-block"]);
    assert!(
        output.status.success(),
        "bare prove-block must succeed:\n{}",
        combined(&output)
    );
    let out = stdout(&output);
    assert_eq!(field(line_with(&out, "srs:"), "state"), "unreachable");
    assert!(out
        .lines()
        .any(|l| l.trim() == "connectivity-block-proven:true"));
}

#[test]
fn connected_state_routes_the_live_order_through() {
    let output = run(&["routes-when-connected"]);
    assert!(
        output.status.success(),
        "routes-when-connected must succeed:\n{}",
        combined(&output)
    );
    let out = stdout(&output);

    let outcome = line_with(&out, "outcome:");
    assert_eq!(field(outcome, "outcome"), "ROUTED_THROUGH", "{outcome}");
    assert_eq!(field(outcome, "broker-id-nonempty"), "true", "{outcome}");

    let witness = line_with(&out, "witness ");
    assert_eq!(
        field(witness, "ib-orders-created"),
        "1",
        "a Connected order must create exactly one IB order:\n{witness}"
    );
    assert_eq!(field(witness, "reconnects"), "0", "{witness}");
    assert_eq!(field(witness, "events"), "0", "{witness}");

    let contrast = tagged_line(&out, "contrast", "non-designated");
    assert!(
        field(&contrast, "sim-receipt").starts_with("paper-"),
        "{contrast}"
    );

    assert!(
        out.lines()
            .any(|l| l.trim() == "connectivity-routes-when-connected:true"),
        "missing the proof headline:\n{out}"
    );
}

#[test]
fn identical_inputs_are_byte_identical_across_processes() {
    for args in [
        vec!["prove-block", "--state", "unreachable"],
        vec!["prove-block", "--state", "scheduled-restart"],
        vec!["routes-when-connected"],
    ] {
        let first = stdout(&run(&args));
        let second = stdout(&run(&args));
        assert_eq!(first, second, "subcommand {args:?} is not deterministic");
    }
}

// --------------------------------------------------------------------------- //
// Fail-closed boundary (non-vacuity)
// --------------------------------------------------------------------------- //

#[test]
fn prove_block_inject_connected_fails_closed() {
    // A reachable gate routes the order through, so no block can be derived.
    let output = run(&["prove-block", "--inject", "connected"]);
    assert!(
        !output.status.success(),
        "inject connected on prove-block must fail closed"
    );
    let all = combined(&output);
    assert!(
        !contains_success_sentinel(&all),
        "no success sentinel may appear anywhere for inject connected:\n{all}"
    );
    assert!(
        all.contains("inject=connected"),
        "inject connected must report the injected fault:\n{all}"
    );
}

#[test]
fn routes_inject_unreachable_fails_closed() {
    let output = run(&["routes-when-connected", "--inject", "unreachable"]);
    assert!(
        !output.status.success(),
        "inject unreachable on routes-when-connected must fail closed"
    );
    let all = combined(&output);
    assert!(
        !contains_success_sentinel(&all),
        "no success sentinel:\n{all}"
    );
    assert!(all.contains("inject=unreachable"), "{all}");
}

#[test]
fn routes_inject_scheduled_restart_fails_closed() {
    let output = run(&["routes-when-connected", "--inject", "scheduled-restart"]);
    assert!(!output.status.success());
    let all = combined(&output);
    assert!(
        !contains_success_sentinel(&all),
        "no success sentinel:\n{all}"
    );
    assert!(all.contains("inject=scheduled-restart"), "{all}");
}

// --------------------------------------------------------------------------- //
// Fail-closed parsing
// --------------------------------------------------------------------------- //

/// Assert a bad invocation exits non-zero and emits no success sentinel anywhere in its output.
fn assert_rejected(args: &[&str]) {
    let output = run(args);
    let all = combined(&output);
    assert!(
        !output.status.success(),
        "invocation {args:?} must fail closed:\n{all}"
    );
    assert!(
        !contains_success_sentinel(&all),
        "invocation {args:?} must emit no success sentinel anywhere:\n{all}"
    );
}

#[test]
fn prove_block_state_connected_fails_closed() {
    // `connected` is not a blocked state; prove-block must refuse it (use routes-when-connected).
    assert_rejected(&["prove-block", "--state", "connected"]);
}

#[test]
fn prove_block_inject_non_opposite_class_fails_closed() {
    // The only valid prove-block injection is the opposite class (`connected`).
    assert_rejected(&["prove-block", "--inject", "unreachable"]);
}

#[test]
fn routes_inject_connected_fails_closed() {
    // `connected` is not a fault for routes-when-connected (it is the proof state).
    assert_rejected(&["routes-when-connected", "--inject", "connected"]);
}

#[test]
fn unknown_subcommand_fails_closed() {
    assert_rejected(&["frobnicate"]);
}

#[test]
fn unknown_flag_fails_closed() {
    assert_rejected(&["prove-block", "--nope"]);
}

#[test]
fn state_without_a_value_fails_closed() {
    assert_rejected(&["prove-block", "--state"]);
}

#[test]
fn inject_without_a_value_fails_closed() {
    assert_rejected(&["prove-block", "--inject"]);
}

#[test]
fn unknown_state_value_fails_closed() {
    assert_rejected(&["prove-block", "--state", "bogus"]);
}

#[test]
fn missing_subcommand_fails_closed() {
    assert_rejected(&[]);
}

#[test]
fn help_and_usage_output_carry_no_success_sentinel() {
    // A safety-evidence CLI must never print an exact `:true` success token except on a real
    // success. `help` exits zero and prints USAGE, and every parse error also prints USAGE, so USAGE
    // must not embed the sentinels — else a grep on any usage-printing path would false-positive.
    for args in [vec!["help"], vec!["--help"], vec!["-h"]] {
        let output = run(&args);
        assert!(output.status.success(), "help must succeed: {args:?}");
        let all = combined(&output);
        assert!(
            !contains_success_sentinel(&all),
            "help/usage output must not contain a success sentinel:\n{all}"
        );
    }
}

#[test]
fn sole_help_after_a_subcommand_succeeds() {
    // A lone help token after a subcommand shows usage and exits zero.
    for args in [
        vec!["prove-block", "--help"],
        vec!["prove-block", "help"],
        vec!["routes-when-connected", "--help"],
        vec!["routes-when-connected", "-h"],
    ] {
        let output = run(&args);
        assert!(output.status.success(), "sole help must succeed: {args:?}");
        assert!(
            !contains_success_sentinel(&combined(&output)),
            "sole help must not emit a success sentinel: {args:?}"
        );
    }
}

#[test]
fn help_mixed_with_invalid_args_fails_closed() {
    // A help token must never RESCUE a malformed proof invocation into a false success — parsing
    // must reject the invalid/incompatible arguments non-zero even when `--help`/`help` is present.
    assert_rejected(&["prove-block", "--nope", "--help"]);
    assert_rejected(&["prove-block", "--help", "--nope"]);
    assert_rejected(&["routes-when-connected", "--bad", "--help"]);
    assert_rejected(&["prove-block", "--state", "connected", "--help"]);
    assert_rejected(&["--help", "--nope"]);
}
