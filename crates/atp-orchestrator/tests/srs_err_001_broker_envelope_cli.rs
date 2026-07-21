//! SRS-ERR-001 BROKER-SIDE structured-error envelope operator-CLI integration test.
//!
//! Drives the `err001_broker_envelope_cli` binary the way an operator would — in fresh OS processes
//! via the `CARGO_BIN_EXE_err001_broker_envelope_cli` path Cargo wires for integration tests — and
//! asserts the half of the SRS-ERR-001 acceptance criterion the execution-boundary CLI cannot reach:
//! the SyRS SYS-64 **broker-validation** categories arriving inside a `StructuredOrderError`.
//!
//!   1. `broker-categories` proves each vendor code the SRS-EXE-006 classifier maps produces an
//!      envelope carrying the applicable SyRS SYS-64 category, a non-empty type, a message retaining
//!      the vendor's own text, and the unchanged original order (`broker-envelope-complete:true`).
//!   2. `unmapped` proves a rejection the classifier does NOT map is surfaced under `BROKER_REJECTED`
//!      with its vendor detail intact — never dropped, and never relabelled as `INVALID_SYMBOL`
//!      (`unmapped-surfaced-not-fabricated:true`).
//!   3. `parity` proves the live and paper arms reject the same malformed order with identical
//!      envelope fields — SyRS SYS-64's "identical for live and paper execution modes"
//!      (`live-paper-parity:true`).
//!
//! Plus the non-vacuity boundary: `--inject accepted` swaps in an ACCEPTING transport, so nothing is
//! rejected and no proof can be derived; each proof subcommand must fail closed under it (non-zero
//! exit, NO proof line), and identical inputs in two fresh processes are byte-identical.

use std::process::{Command, Output};

/// The `[[bin]]` path Cargo wires for integration tests.
const CLI: &str = env!("CARGO_BIN_EXE_err001_broker_envelope_cli");

/// Every `:true` proof line — none may appear under an injected fault or a rejected parse.
const PROOF_LINES: [&str; 3] = [
    "broker-envelope-complete:true",
    "unmapped-surfaced-not-fabricated:true",
    "live-paper-parity:true",
];

/// Every subcommand that asserts a proof (all of them accept `--inject`).
const PROOF_SUBCOMMANDS: [&str; 3] = ["broker-categories", "unmapped", "parity"];

/// True if the output PRINTED a proof headline (a standalone `:true` line). The USAGE text
/// *documents* the proof headlines mid-line, so a substring check would false-positive on any error
/// that prints usage — a printed proof is a whole line of its own.
fn printed_a_proof_line(out: &str) -> bool {
    out.lines().any(|line| PROOF_LINES.contains(&line.trim()))
}

fn run(args: &[&str]) -> Output {
    Command::new(CLI)
        .args(args)
        .output()
        .expect("the err001_broker_envelope_cli binary runs")
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

/// The `prefix[name] ...` line (e.g. `broker[max-rate-exceeded]`), failing if absent.
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
fn broker_rejections_map_to_syrs64_categories() {
    let output = run(&["broker-categories"]);
    assert!(
        output.status.success(),
        "broker-categories must succeed:\n{}",
        combined(&output)
    );
    let out = stdout(&output);

    // The three SyRS SYS-64 broker-validation categories SRS-ERR-001 was blocked on, each derived by
    // the REAL classifier from the vendor code a real gateway would carry.
    for (label, code, category) in [
        ("no-security-definition", "200", "INVALID_SYMBOL"),
        ("security-not-available", "203", "INVALID_SYMBOL"),
        (
            "insufficient-buying-power",
            "201",
            "INSUFFICIENT_BUYING_POWER",
        ),
        ("max-rate-exceeded", "100", "RATE_LIMITED"),
    ] {
        let line = tagged_line(&out, "broker", label);
        assert_eq!(field(&line, "code"), code, "wrong vendor code:\n{line}");
        assert_eq!(
            field(&line, "category"),
            category,
            "vendor code {code} must classify as {category}:\n{line}"
        );
        // The full SRS-ERR-001 envelope: type + human-readable message + original order parameters.
        assert_ne!(
            field(&line, "type"),
            "\"\"",
            "the envelope must carry a non-empty error type:\n{line}"
        );
        assert_eq!(field(&line, "message-nonempty"), "true", "{line}");
        assert_eq!(
            field(&line, "vendor-detail"),
            "true",
            "the message must retain the vendor's own text:\n{line}"
        );
        assert_eq!(field(&line, "original-order-unchanged"), "true", "{line}");
        // The rejection must be genuinely BROKER-side: the order reached the wire exactly once.
        // A gate that short-circuited before the broker would show 0 here, and its rejection could
        // never be evidence of the broker's own classification.
        assert_eq!(
            field(&line, "wire-attempts"),
            "1",
            "the order must actually reach the broker to be rejected by it:\n{line}"
        );
        assert_eq!(field(&line, "complete"), "true", "{line}");
    }

    // The proofs above route through ExecutionEngine::route_order, so the live-designation authority
    // must be load-bearing: a NON-designated strategy is refused at the gate and never reaches the
    // wire (SRS-EXE-001 single-live invariant), carrying the authority category, not the broker's.
    let gate = tagged_line(&out, "broker", "authority-not-designated");
    assert_eq!(
        field(&gate, "category"),
        "NON_LIVE_STRATEGY_SUBMISSION",
        "a non-designated strategy must be refused by the authority gate:\n{gate}"
    );
    assert_eq!(
        field(&gate, "wire-attempts"),
        "0",
        "the authority gate must refuse BEFORE the brokerage bridge is consulted:\n{gate}"
    );
    assert_eq!(field(&gate, "original-order-unchanged"), "true", "{gate}");
    assert_eq!(field(&gate, "gate-holds"), "true", "{gate}");

    // Four mapped categories + the authority gate: the SYS-64 broker-validation set is covered, and
    // it is reached through the production boundary rather than around it.
    let covered: Vec<&str> = out
        .lines()
        .filter(|line| line.starts_with("broker["))
        .collect();
    assert_eq!(
        covered.len(),
        5,
        "every mapped broker reject path plus the authority gate must be driven:\n{out}"
    );
    assert!(
        out.lines()
            .any(|l| l.trim() == "broker-envelope-complete:true"),
        "missing the proof headline:\n{out}"
    );
}

#[test]
fn unmapped_rejections_are_surfaced_never_fabricated() {
    let output = run(&["unmapped"]);
    assert!(
        output.status.success(),
        "unmapped must succeed:\n{}",
        combined(&output)
    );
    let out = stdout(&output);

    for (label, code) in [
        ("generic-order-rejection", "201"),
        ("cancel-code-on-submit", "202"),
        ("unrecognised-vendor-code", "321"),
    ] {
        let line = tagged_line(&out, "unmapped", label);
        assert_eq!(field(&line, "code"), code, "{line}");
        // The regression this proof exists to prevent: an unmapped rejection silently reported as an
        // invalid symbol. The AC requires a SyRS category only "when applicable".
        assert_eq!(
            field(&line, "category"),
            "BROKER_REJECTED",
            "an unmapped rejection must not borrow an inapplicable SyRS category:\n{line}"
        );
        assert_ne!(
            field(&line, "category"),
            "INVALID_SYMBOL",
            "an unmapped rejection must never be relabelled INVALID_SYMBOL:\n{line}"
        );
        assert_eq!(
            field(&line, "surfaced"),
            "true",
            "the vendor code and text must survive into the message:\n{line}"
        );
        assert_eq!(field(&line, "not-fabricated"), "true", "{line}");
        assert_eq!(field(&line, "original-order-unchanged"), "true", "{line}");
        assert_eq!(field(&line, "honest"), "true", "{line}");
    }
    assert!(
        out.lines()
            .any(|l| l.trim() == "unmapped-surfaced-not-fabricated:true"),
        "missing the proof headline:\n{out}"
    );
}

#[test]
fn live_and_paper_arms_share_one_error_contract() {
    let output = run(&["parity"]);
    assert!(
        output.status.success(),
        "parity must succeed:\n{}",
        combined(&output)
    );
    let out = stdout(&output);

    // Both arms reject the same malformed order with the same, CORRECT category — a malformed order
    // parameter, not a borrowed INVALID_SYMBOL.
    for arm in ["live", "paper"] {
        let line = tagged_line(&out, "parity", arm);
        assert_eq!(
            field(&line, "category"),
            "ORDER_PARAMETERS_INVALID",
            "the {arm} arm must reject a malformed order as invalid parameters:\n{line}"
        );
        assert_eq!(field(&line, "type"), "\"NonPositiveQuantity\"", "{line}");
        assert_eq!(field(&line, "message-nonempty"), "true", "{line}");
        assert_eq!(field(&line, "original-order-unchanged"), "true", "{line}");
    }

    let contract = tagged_line(&out, "parity", "contract");
    for key in [
        "same-category",
        "same-type",
        "same-message",
        "originals-unchanged",
        "correct-category",
        "identical",
    ] {
        assert_eq!(
            field(&contract, key),
            "true",
            "SyRS SYS-64 requires an identical live/paper error contract ({key}):\n{contract}"
        );
    }
    assert!(
        out.lines().any(|l| l.trim() == "live-paper-parity:true"),
        "missing the proof headline:\n{out}"
    );
}

#[test]
fn identical_inputs_are_byte_identical_across_processes() {
    for sub in PROOF_SUBCOMMANDS {
        let first = stdout(&run(&[sub]));
        let second = stdout(&run(&[sub]));
        assert_eq!(first, second, "subcommand {sub:?} is not deterministic");
    }
}

// --------------------------------------------------------------------------- //
// Fail-closed boundary (non-vacuity)
// --------------------------------------------------------------------------- //

/// An ACCEPTING transport rejects nothing, so no proof may be derived from it.
fn assert_accepted_fails_closed(subcommand: &str) {
    let output = run(&[subcommand, "--inject", "accepted"]);
    assert!(
        !output.status.success(),
        "inject accepted on {subcommand} must fail closed"
    );
    let all = combined(&output);
    assert!(
        !printed_a_proof_line(&all),
        "no proof line may be printed for inject accepted on {subcommand}:\n{all}"
    );
    assert!(
        all.contains("inject=accepted"),
        "inject accepted on {subcommand} must report the injected fault:\n{all}"
    );
}

#[test]
fn accepted_transport_fails_closed_on_broker_categories() {
    assert_accepted_fails_closed("broker-categories");
}

#[test]
fn accepted_transport_fails_closed_on_unmapped() {
    assert_accepted_fails_closed("unmapped");
}

#[test]
fn accepted_transport_fails_closed_on_parity() {
    assert_accepted_fails_closed("parity");
}

#[test]
fn accepted_fault_fails_closed_on_every_proof_subcommand() {
    for sub in PROOF_SUBCOMMANDS {
        assert_accepted_fails_closed(sub);
    }
}

#[test]
fn unknown_fault_fails_closed() {
    let output = run(&["unmapped", "--inject", "route-to-ib"]);
    assert!(
        !output.status.success(),
        "an unknown fault must fail closed"
    );
    let all = combined(&output);
    assert!(
        !printed_a_proof_line(&all),
        "an unknown fault must print no proof line:\n{all}"
    );
}

#[test]
fn unknown_subcommand_fails_closed() {
    let output = run(&["frobnicate"]);
    assert!(
        !output.status.success(),
        "an unknown subcommand must fail closed"
    );
    assert!(!printed_a_proof_line(&combined(&output)));
}

#[test]
fn unknown_flag_fails_closed() {
    let output = run(&["parity", "--nope"]);
    assert!(!output.status.success(), "an unknown flag must fail closed");
    assert!(!printed_a_proof_line(&combined(&output)));
}

#[test]
fn inject_without_a_value_fails_closed() {
    let output = run(&["parity", "--inject"]);
    assert!(
        !output.status.success(),
        "a valueless --inject must fail closed"
    );
    assert!(!printed_a_proof_line(&combined(&output)));
}

#[test]
fn missing_subcommand_fails_closed() {
    let output = run(&[]);
    assert!(
        !output.status.success(),
        "a missing subcommand must fail closed"
    );
    assert!(!printed_a_proof_line(&combined(&output)));
}
