//! SRS-BT-010 cross-process reproducibility test (the platform-generated-randomness closure).
//!
//! The in-process verifier (`determinism::verify_reproducible`) runs two replays in ONE process,
//! so it cannot catch nondeterminism that is stable within a process but varies across a restart
//! (a process-seeded random value, hash-map iteration order seeded once per process, etc.) — the
//! SRS-BT-010 "platform-generated random values do not introduce nondeterminism" clause. This test
//! closes that gap the only way it can be closed: it spawns the `bt010_repro_cli` binary in two
//! genuinely separate OS processes and asserts their fingerprints are byte-identical.
//!
//! Cargo exports the built binary path as `CARGO_BIN_EXE_bt010_repro_cli`, so each `digest`
//! invocation is a real fresh process — not an in-process call.

use std::process::{Command, Output};

/// The `[[bin]]` path Cargo wires for integration tests.
const CLI: &str = env!("CARGO_BIN_EXE_bt010_repro_cli");

fn run(args: &[&str]) -> Output {
    Command::new(CLI)
        .args(args)
        .output()
        .expect("the bt010_repro_cli binary runs")
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("stdout is utf-8")
}

/// Extract the single line beginning with `prefix` (e.g. `run-digest:`), failing if absent.
fn line_with(out: &str, prefix: &str) -> String {
    out.lines()
        .find(|line| line.starts_with(prefix))
        .unwrap_or_else(|| panic!("output missing a `{prefix}` line:\n{out}"))
        .to_string()
}

#[test]
fn cross_process_identical_inputs_produce_identical_digests() {
    // Two SEPARATE OS processes over identical inputs. If any process-seeded nondeterminism existed
    // (a per-process RNG, address-space layout leaking into a hash, an unstable map iteration), the
    // two fresh processes would disagree — the in-process double-run could not see it.
    let first = run(&["digest"]);
    let second = run(&["digest"]);
    assert!(
        first.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&first.stderr)
    );
    assert!(second.status.success());

    let a = stdout(&first);
    let b = stdout(&second);
    assert_eq!(
        line_with(&a, "run-digest:"),
        line_with(&b, "run-digest:"),
        "two fresh processes must produce an identical run-digest (SRS-BT-010 cross-process)"
    );
    assert_eq!(
        line_with(&a, "run-manifest:"),
        line_with(&b, "run-manifest:"),
        "two fresh processes must produce an identical input manifest digest"
    );
    // The whole stdout is byte-identical, not merely the two digest lines.
    assert_eq!(a, b, "two fresh-process digest runs must be byte-identical");
}

#[test]
fn cross_process_different_input_changes_the_run_digest() {
    // A different lot trades differently, so the run-digest MUST differ — proof the fingerprint is
    // a function of the inputs, not a constant that would make the identity check vacuous.
    let base = stdout(&run(&["digest"]));
    let other = stdout(&run(&["digest", "--lot", "11"]));
    assert_ne!(
        line_with(&base, "run-digest:"),
        line_with(&other, "run-digest:"),
        "a different lot must change the run-digest"
    );
    assert_ne!(
        line_with(&base, "run-manifest:"),
        line_with(&other, "run-manifest:"),
        "a different lot must change the input manifest"
    );
}

#[test]
fn cross_process_seed_changes_manifest_but_not_run_digest() {
    // The seed is recorded in the manifest for provenance, but the deterministic engine consumes no
    // platform randomness — so a different seed changes the input manifest yet leaves the run-digest
    // unchanged. That IS the "platform-generated random values do not introduce nondeterminism"
    // guarantee, made observable across two fresh processes.
    let seed_one = stdout(&run(&["digest", "--seed", "1"]));
    let seed_two = stdout(&run(&["digest", "--seed", "2"]));
    assert_ne!(
        line_with(&seed_one, "run-manifest:"),
        line_with(&seed_two, "run-manifest:"),
        "a different seed is a different input, so the manifest digest must differ"
    );
    assert_eq!(
        line_with(&seed_one, "run-digest:"),
        line_with(&seed_two, "run-digest:"),
        "the engine is seed-independent: the run output must not depend on the platform RNG seed"
    );
}

#[test]
fn verify_subcommand_reports_reproducible() {
    let output = run(&["verify"]);
    assert!(
        output.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let out = stdout(&output);
    assert!(
        out.contains("reproducible run-digest:"),
        "verify must report the run reproducible:\n{out}"
    );
}

#[test]
fn full_renders_every_artifact() {
    let out = stdout(&run(&["digest", "--full"]));
    for marker in [
        "run-manifest:",
        "run-digest:",
        "manifest:",
        "trade_log:",
        "equity_curve:",
        "metrics:",
    ] {
        assert!(
            out.contains(marker),
            "--full output must render {marker}:\n{out}"
        );
    }
}

#[test]
fn unknown_subcommand_fails_closed() {
    let output = run(&["frobnicate"]);
    assert!(
        !output.status.success(),
        "an unknown subcommand must exit non-zero, not silently succeed"
    );
}

#[test]
fn unknown_flag_fails_closed() {
    let output = run(&["digest", "--nope", "1"]);
    assert!(
        !output.status.success(),
        "an unknown flag must exit non-zero, not silently ignore it"
    );
}
