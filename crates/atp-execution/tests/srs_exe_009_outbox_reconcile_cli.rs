//! SRS-EXE-009 — end-to-end coverage of the durable-outbox restart-reconciliation
//! operator CLI (`exe009_outbox_reconcile_cli`).
//!
//! Drives the built binary — via the `CARGO_BIN_EXE_exe009_outbox_reconcile_cli`
//! path Cargo wires for integration tests — and asserts each subcommand prints its
//! non-vacuous proof token on the happy path, each `--inject <fault>` proves the
//! fail-closed property (a bound/ambiguous intent is never resubmitted), and every
//! malformed invocation (unknown subcommand / flag / fault, a valueless flag, an
//! inapplicable fault, a stray flag on a no-flag subcommand) exits non-zero with no
//! proof token.

use std::process::{Command, Output};

const CLI: &str = env!("CARGO_BIN_EXE_exe009_outbox_reconcile_cli");

fn run(args: &[&str]) -> Output {
    Command::new(CLI)
        .args(args)
        .output()
        .expect("run exe009_outbox_reconcile_cli")
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("stdout is utf-8")
}

fn combined(output: &Output) -> String {
    stdout(output) + &String::from_utf8(output.stderr.clone()).expect("stderr is utf-8")
}

/// A happy-path subcommand prints its proof token and exits zero.
fn assert_proves(args: &[&str], token: &str) {
    let output = run(args);
    let out = stdout(&output);
    assert!(
        output.status.success(),
        "{args:?} must exit zero:\n{}",
        combined(&output)
    );
    assert!(
        out.lines().any(|line| line.trim() == token),
        "{args:?} must print `{token}`:\n{out}"
    );
}

/// A malformed / fault-injected invocation exits non-zero and prints NO success
/// token (fail closed).
fn assert_fails_closed(args: &[&str], forbidden_token: &str) {
    let output = run(args);
    assert!(
        !output.status.success(),
        "{args:?} must exit non-zero:\n{}",
        combined(&output)
    );
    let all = combined(&output);
    assert!(
        !all.lines().any(|line| line.trim() == forbidden_token),
        "{args:?} must NOT print `{forbidden_token}`:\n{all}"
    );
}

#[test]
fn write_ahead_proves_durable_commit_before_submission() {
    assert_proves(&["write-ahead"], "write-ahead-durable:true");
}

#[test]
fn restart_skip_bound_never_resubmits() {
    assert_proves(&["restart-skip-bound"], "bound-intent-not-resubmitted:true");
}

#[test]
fn restart_adopt_binds_without_resubmitting() {
    assert_proves(&["restart-adopt"], "unacked-intent-adopted:true");
}

#[test]
fn restart_resubmit_only_under_full_coverage() {
    assert_proves(&["restart-resubmit"], "unlanded-intent-resubmitted:true");
}

#[test]
fn retention_releases_only_terminal_entries() {
    assert_proves(&["retention"], "retained-until-terminal:true");
}

#[test]
fn broker_error_makes_no_decision() {
    assert_proves(&["broker-error"], "no-decision-on-broker-error:true");
}

#[test]
fn injected_duplicate_replay_is_rejected() {
    assert_proves(
        &["write-ahead", "--inject", "duplicate-replay"],
        "duplicate-replay-rejected:true",
    );
}

#[test]
fn injected_id_conflict_never_resubmits() {
    let args = &["restart-skip-bound", "--inject", "id-conflict"];
    assert_proves(args, "no-resubmit-on-id-conflict:true");
    // Crucially it must NOT have emitted the plain skip proof for a conflicted view.
    let out = stdout(&run(args));
    assert!(!out.contains("bound-intent-not-resubmitted:true"), "{out}");
}

#[test]
fn injected_partial_coverage_never_resubmits() {
    assert_proves(
        &["restart-resubmit", "--inject", "partial-coverage"],
        "no-resubmit-on-partial-view:true",
    );
}

#[test]
fn malformed_invocations_fail_closed() {
    // Unknown subcommand.
    assert_fails_closed(&["bogus"], "write-ahead-durable:true");
    // A no-flag subcommand rejects a stray flag.
    assert_fails_closed(&["retention", "--stray"], "retained-until-terminal:true");
    assert_fails_closed(
        &["restart-adopt", "--inject", "id-conflict"],
        "unacked-intent-adopted:true",
    );
    // Unknown fault, and a valueless --inject.
    assert_fails_closed(
        &["write-ahead", "--inject", "nonsense"],
        "write-ahead-durable:true",
    );
    assert_fails_closed(&["write-ahead", "--inject"], "write-ahead-durable:true");
    // A fault applied to the wrong subcommand.
    assert_fails_closed(
        &["restart-skip-bound", "--inject", "partial-coverage"],
        "bound-intent-not-resubmitted:true",
    );
    assert_fails_closed(
        &["restart-resubmit", "--inject", "id-conflict"],
        "unlanded-intent-resubmitted:true",
    );
}
