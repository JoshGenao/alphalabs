//! SRS-SIM-004 paper-state persistence operator-CLI integration test.
//!
//! Drives the `sim004_persist_cli` binary the way an operator would — in fresh OS processes via the
//! `CARGO_BIN_EXE_sim004_persist_cli` path Cargo wires for integration tests — and asserts the
//! SRS-SIM-004 acceptance criterion end to end via *fault injection*: "Virtual positions, pending
//! simulated orders, accumulated metrics, and user state are persisted every 60 seconds by default
//! and restored within 30 seconds of container restart, excluding warm-up."
//!
//!   1. `persist` (one process) then `restore` (a SEPARATE process) proves the persisted state
//!      survives process death — the process-level analog of a container restart — and that the
//!      restored ledger, metrics, and user-state match the captured fixture (`state-matches-capture:
//!      true`), within the SYS-89 30s deadline (`restored-within-deadline:true`).
//!   2. `roundtrip` proves the same in one process (`state-survived-restart:true`).
//!   3. Every `--inject <fault>` makes the restore fail closed (non-zero exit, no survival line) — a
//!      missing / corrupt / truncated / tampered / over-deadline / non-dictionary snapshot can never
//!      produce a "restored" proof.
//!   4. Determinism: the persisted bytes are byte-identical across two fresh processes.
//!   5. Fail closed on an unknown subcommand / flag / fault and a missing `--dir`.

use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};

/// The `[[bin]]` path Cargo wires for integration tests.
const CLI: &str = env!("CARGO_BIN_EXE_sim004_persist_cli");

fn run(args: &[&str]) -> Output {
    Command::new(CLI)
        .args(args)
        .output()
        .expect("the sim004_persist_cli binary runs")
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("stdout is utf-8")
}

fn stderr(output: &Output) -> String {
    String::from_utf8(output.stderr.clone()).expect("stderr is utf-8")
}

/// A unique store directory per test (no `tempfile` dep; `<pid>-<seq>`).
fn temp_dir(label: &str) -> PathBuf {
    static SEQ: AtomicU64 = AtomicU64::new(0);
    let seq = SEQ.fetch_add(1, Ordering::Relaxed);
    std::env::temp_dir().join(format!(
        "atp-sim004-cli-{label}-{}-{seq}",
        std::process::id()
    ))
}

fn dir_str(dir: &Path) -> String {
    dir.to_string_lossy().into_owned()
}

#[test]
fn srs_sim_004_persist_then_restore_survives_a_fresh_process() {
    let dir = temp_dir("cross-process");
    let d = dir_str(&dir);

    // Process A: persist.
    let persisted = run(&["persist", "--dir", &d]);
    assert!(
        persisted.status.success(),
        "persist failed: {}",
        stderr(&persisted)
    );
    let pout = stdout(&persisted);
    assert!(pout.contains("persisted:true"), "{pout}");
    assert!(pout.contains("strategies:2"), "{pout}");
    assert!(pout.contains("metrics-strategies:2"), "{pout}");
    assert!(pout.contains("user-state-strategies:2"), "{pout}");

    // Process B (fresh): restore. The state must survive process death.
    let restored = run(&["restore", "--dir", &d]);
    assert!(
        restored.status.success(),
        "restore failed: {}",
        stderr(&restored)
    );
    let rout = stdout(&restored);
    assert!(rout.contains("restored:true"), "{rout}");
    assert!(rout.contains("restored-within-deadline:true"), "{rout}");
    assert!(rout.contains("state-matches-capture:true"), "{rout}");
    assert!(rout.contains("strategies:2"), "{rout}");
    assert!(rout.contains("metrics-strategies:2"), "{rout}");
    assert!(rout.contains("user-state-strategies:2"), "{rout}");

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn srs_sim_004_roundtrip_reports_state_survived_restart() {
    let dir = temp_dir("roundtrip");
    let out = run(&["roundtrip", "--dir", &dir_str(&dir)]);
    assert!(out.status.success(), "roundtrip failed: {}", stderr(&out));
    assert!(
        stdout(&out).contains("state-survived-restart:true"),
        "{}",
        stdout(&out)
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn srs_sim_004_every_inject_fault_fails_closed() {
    for fault in [
        "missing-dir",
        "corrupt-file",
        "truncated",
        "tampered-checksum",
        "deadline-exceeded",
        "non-json-user-state",
    ] {
        let dir = temp_dir(&format!("fault-{fault}"));
        std::fs::create_dir_all(&dir).unwrap();
        let out = run(&["roundtrip", "--dir", &dir_str(&dir), "--inject", fault]);
        assert!(
            !out.status.success(),
            "inject {fault} must fail closed (non-zero exit)\nstdout:\n{}",
            stdout(&out)
        );
        // The survival proof line must NEVER appear under a fault.
        assert!(
            !stdout(&out).contains("state-survived-restart:true"),
            "inject {fault} printed a survival line despite failing:\n{}",
            stdout(&out)
        );
        assert!(
            stderr(&out).contains("fault rejected fail-closed"),
            "inject {fault} did not report a fail-closed rejection:\n{}",
            stderr(&out)
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}

#[test]
fn srs_sim_004_persisted_bytes_are_deterministic_across_processes() {
    // Two fresh processes persisting the same fixture write byte-identical store files.
    let dir_a = temp_dir("det-a");
    let dir_b = temp_dir("det-b");
    run(&["persist", "--dir", &dir_str(&dir_a)]);
    run(&["persist", "--dir", &dir_str(&dir_b)]);
    let a = std::fs::read(dir_a.join("paper_sim_state.snapshot")).expect("store a");
    let b = std::fs::read(dir_b.join("paper_sim_state.snapshot")).expect("store b");
    assert_eq!(
        a, b,
        "the persisted snapshot must be byte-identical across processes"
    );
    let _ = std::fs::remove_dir_all(&dir_a);
    let _ = std::fs::remove_dir_all(&dir_b);
}

#[test]
fn srs_sim_004_cli_rejects_bad_invocations() {
    // Unknown subcommand.
    assert!(!run(&["frobnicate"]).status.success());
    // Missing --dir.
    assert!(!run(&["persist"]).status.success());
    // Unknown flag.
    assert!(!run(&["persist", "--nope", "x"]).status.success());
    // Unknown fault.
    let dir = temp_dir("bad-fault");
    assert!(
        !run(&["roundtrip", "--dir", &dir_str(&dir), "--inject", "bogus"])
            .status
            .success()
    );
    // --inject is only valid for roundtrip.
    assert!(!run(&[
        "persist",
        "--dir",
        &dir_str(&dir),
        "--inject",
        "corrupt-file"
    ])
    .status
    .success());
}
