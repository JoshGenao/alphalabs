//! SRS-ERR-001 structured order-error envelope operator-CLI integration test.
//!
//! Drives the `err001_error_envelope_cli` binary the way an operator would — in fresh OS processes via
//! the `CARGO_BIN_EXE_err001_error_envelope_cli` path Cargo wires for integration tests — and asserts
//! the SRS-ERR-001 acceptance criterion end to end: "Errors include type, human-readable message,
//! original order parameters, and one of the SyRS-defined error categories when applicable".
//!
//!   1. `categories` proves every SyRS SYS-64 OrderErrorCategory maps to a distinct, non-empty,
//!      UPPER_SNAKE wire string (`all-categories-mapped:true`).
//!   2. `envelope` proves every execution-boundary reject path returns a structured error carrying its
//!      category, a non-empty type, a non-empty message, and the unchanged original order
//!      (`envelope-complete:true`).
//!   3. `no-broker` proves every reject path reaches no brokerage — a rejected order never produces an
//!      IB side effect (`no-ib-side-effect:true`).
//!
//! Plus the non-vacuity boundary: `--inject authorized` runs the legitimately authorized
//! (Live+Connected+Fresh) path, which returns `Ok` and reaches the broker; each reject-proof subcommand
//! must fail closed under it (non-zero exit, NO proof line — a success path can never produce a reject
//! proof), and identical inputs in two fresh processes are byte-identical.

use std::process::{Command, Output};

/// The `[[bin]]` path Cargo wires for integration tests.
const CLI: &str = env!("CARGO_BIN_EXE_err001_error_envelope_cli");

/// Every `:true` proof line — none may appear under an injected fault or a rejected parse.
const PROOF_LINES: [&str; 4] = [
    "all-categories-mapped:true",
    "envelope-complete:true",
    "no-ib-side-effect:true",
    "authority-enforced:true",
];

/// The reject-proof subcommands that accept `--inject` (categories has no engine scenario to corrupt).
const REJECT_PROOF_SUBCOMMANDS: [&str; 3] = ["envelope", "no-broker", "authority"];

/// True if the output PRINTED a proof headline (a standalone `:true` line). The USAGE text *documents*
/// the proof headlines mid-line (e.g. `(all-categories-mapped:true)`), so a substring check would
/// false-positive on any error that prints usage — a printed proof is a whole line of its own.
fn printed_a_proof_line(out: &str) -> bool {
    out.lines().any(|line| PROOF_LINES.contains(&line.trim()))
}

fn run(args: &[&str]) -> Output {
    Command::new(CLI)
        .args(args)
        .output()
        .expect("the err001_error_envelope_cli binary runs")
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("stdout is utf-8")
}

fn combined(output: &Output) -> String {
    stdout(output) + &String::from_utf8(output.stderr.clone()).expect("stderr is utf-8")
}

/// The value after a `prefix` line (e.g. `envelope-complete:`), failing if absent.
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

/// The `prefix[name] ...` line (e.g. `envelope[live-stale-data]`), failing if absent.
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
fn categories_maps_every_syrs_64_category() {
    let out = stdout(&run(&["categories"]));
    // Every category line must carry a non-empty, upper-snake, distinct wire string.
    let category_lines: Vec<&str> = out
        .lines()
        .filter(|line| line.starts_with("category["))
        .collect();
    assert!(
        category_lines.len() >= 9,
        "the sweep must cover the SyRS SYS-64 vocabulary:\n{out}"
    );
    for line in &category_lines {
        assert_eq!(field(line, "upper-snake"), "true", "{line}");
        assert_eq!(field(line, "distinct"), "true", "{line}");
        // The wire string itself must be present and non-empty.
        assert!(!field(line, "wire").is_empty(), "{line}");
    }
    assert_eq!(value_after(&out, "all-categories-mapped:"), "true", "{out}");
}

#[test]
fn envelope_is_complete_on_every_reject_path() {
    let out = stdout(&run(&["envelope"]));
    // Each execution-boundary reject path must produce a complete envelope: an expected category, a
    // non-empty message, and the unchanged original order.
    for (name, category) in [
        ("authority-not-designated", "NON_LIVE_STRATEGY_SUBMISSION"),
        ("paper-non-live", "NON_LIVE_STRATEGY_SUBMISSION"),
        ("live-unreachable", "CONNECTIVITY_BLOCKED"),
        ("live-scheduled-restart", "CONNECTIVITY_BLOCKED"),
        ("live-stale-data", "MARKET_DATA_STALE"),
        ("duplicate-correlation", "DUPLICATE_CLIENT_CORRELATION_ID"),
    ] {
        let line = tagged_line(&out, "envelope", name);
        assert_eq!(field(&line, "category"), category, "{line}");
        assert_eq!(field(&line, "message-nonempty"), "true", "{line}");
        assert_eq!(field(&line, "original-order-unchanged"), "true", "{line}");
        assert_eq!(field(&line, "complete"), "true", "{line}");
    }
    assert_eq!(value_after(&out, "envelope-complete:"), "true", "{out}");
}

#[test]
fn reject_paths_make_no_broker_calls() {
    let out = stdout(&run(&["no-broker"]));
    let line = out
        .lines()
        .find(|l| l.starts_with("no-broker swept:"))
        .unwrap_or_else(|| panic!("no no-broker summary line:\n{out}"));
    // The sweep must be non-trivial and EVERY reject path clean (rejected + zero broker calls).
    let swept: u64 = field(line, "swept").parse().expect("swept count");
    let clean: u64 = field(line, "clean").parse().expect("clean count");
    assert!(
        swept > 1,
        "the sweep must cover several reject paths:\n{line}"
    );
    assert_eq!(
        clean, swept,
        "every reject path must reach no broker:\n{line}"
    );
    assert_eq!(
        field(line, "leaked"),
        "0",
        "no broker call may leak:\n{line}"
    );
    // Per-path: each must report zero broker calls.
    for name in [
        "authority-not-designated",
        "paper-non-live",
        "live-unreachable",
        "live-scheduled-restart",
        "live-stale-data",
        "duplicate-correlation",
    ] {
        let path = tagged_line(&out, "no-broker", name);
        assert_eq!(field(&path, "rejected"), "true", "{path}");
        assert_eq!(field(&path, "broker-calls"), "0", "{path}");
        assert_eq!(field(&path, "clean"), "true", "{path}");
    }
    assert_eq!(value_after(&out, "no-ib-side-effect:"), "true", "{out}");
}

#[test]
fn authority_enforces_single_live_designation() {
    let out = stdout(&run(&["authority"]));
    // A non-designated strategy is rejected through the production authority gate (route_order) with a
    // structured NonLiveStrategySubmission envelope and reaches no broker -- both when NO strategy is
    // live and when a DIFFERENT strategy is designated live (the single-live invariant).
    for name in ["none-designated", "other-strategy-live"] {
        let line = tagged_line(&out, "authority", name);
        assert_eq!(
            field(&line, "category"),
            "NON_LIVE_STRATEGY_SUBMISSION",
            "{line}"
        );
        assert_eq!(field(&line, "original-order-unchanged"), "true", "{line}");
        assert_eq!(field(&line, "broker-calls"), "0", "{line}");
        assert_eq!(field(&line, "rejected"), "true", "{line}");
    }
    // The single designated live strategy IS authorized: route_order reaches the broker exactly once.
    let designated = tagged_line(&out, "authority", "designated-live");
    assert_eq!(field(&designated, "authorized"), "true", "{designated}");
    assert_eq!(field(&designated, "broker-calls"), "1", "{designated}");
    assert_eq!(value_after(&out, "authority-enforced:"), "true", "{out}");
}

#[test]
fn identical_inputs_are_byte_identical_across_processes() {
    // The CLI is integer/enum-only with no clock or randomness, so two fresh processes over identical
    // flags agree byte-for-byte (deterministic operator evidence).
    for sub in [
        vec!["categories"],
        vec!["envelope"],
        vec!["no-broker"],
        vec!["authority"],
    ] {
        let first = stdout(&run(&sub));
        let second = stdout(&run(&sub));
        assert_eq!(first, second, "subcommand {sub:?} is not deterministic");
    }
}

// --------------------------------------------------------------------------- //
// Non-vacuity: `--inject authorized` makes every reject-proof subcommand fail closed
// --------------------------------------------------------------------------- //

/// `--inject authorized` runs the legitimately authorized path; the reject proof cannot be made, so
/// the subcommand must fail closed: non-zero exit, NO proof line, and an `inject=authorized` message.
fn assert_authorized_fails_closed(subcommand: &str) {
    let output = run(&[subcommand, "--inject", "authorized"]);
    assert!(
        !output.status.success(),
        "inject authorized on {subcommand} must fail closed"
    );
    let all = combined(&output);
    assert!(
        !printed_a_proof_line(&all),
        "no proof line may be printed for inject authorized on {subcommand}:\n{all}"
    );
    assert!(
        all.contains("inject=authorized"),
        "inject authorized on {subcommand} must report the injected fault:\n{all}"
    );
}

#[test]
fn authorized_submission_fails_closed_on_envelope() {
    assert_authorized_fails_closed("envelope");
}

#[test]
fn authorized_submission_fails_closed_on_no_broker() {
    assert_authorized_fails_closed("no-broker");
}

#[test]
fn authorized_fault_fails_closed_on_every_proof_subcommand() {
    // The fault is shared across the reject-proof subcommands, so an authorized submission must fail
    // closed on every one of them — none may leak a proof on a success path.
    for sub in REJECT_PROOF_SUBCOMMANDS {
        assert_authorized_fails_closed(sub);
    }
}

// --------------------------------------------------------------------------- //
// Argument hygiene
// --------------------------------------------------------------------------- //

#[test]
fn unknown_fault_fails_closed() {
    let output = run(&["envelope", "--inject", "route-to-ib"]);
    assert!(
        !output.status.success(),
        "an unknown fault must fail closed"
    );
    assert!(!printed_a_proof_line(&combined(&output)));
}

#[test]
fn unknown_subcommand_fails_closed() {
    let output = run(&["frobnicate"]);
    assert!(!output.status.success());
}

#[test]
fn unknown_flag_fails_closed() {
    let output = run(&["envelope", "--nope"]);
    assert!(!output.status.success());
}

#[test]
fn categories_rejects_a_flag() {
    // `categories` proves over the fixed enum and takes no flags; a stray token is rejected, not
    // silently ignored.
    let output = run(&["categories", "--inject", "authorized"]);
    assert!(!output.status.success());
    assert!(!printed_a_proof_line(&combined(&output)));
}

#[test]
fn missing_subcommand_fails_closed() {
    let output = run(&[]);
    assert!(!output.status.success());
}
