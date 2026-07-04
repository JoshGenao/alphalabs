//! SRS-RESV-003 / SyRS SYS-49a — the operator CLI arm must FAIL CLOSED at the
//! PROCESS level: a rejected audit-log record (or a degraded input) makes the
//! command exit NONZERO, so shell automation cannot treat an unlogged Hot-Swap
//! trigger as a successful command. "All swap triggers are logged" is load-bearing
//! all the way to the exit status.
//!
//! L4 boundary test: spawns the real `resv003_hot_swap_trigger_cli` binary. A
//! `--log` path whose PARENT directory does not exist makes the durable append
//! fail, so the sink rejects the record.

use std::path::{Path, PathBuf};
use std::process::Command;

const BIN: &str = env!("CARGO_BIN_EXE_resv003_hot_swap_trigger_cli");

fn unwritable_log(name: &str) -> PathBuf {
    // Parent directory intentionally does not exist → OpenOptions::open fails.
    Path::new(env!("CARGO_TARGET_TMPDIR"))
        .join("resv003-nonexistent-dir")
        .join(name)
}

#[test]
fn resv_3_cli_manual_exits_nonzero_when_log_rejected() {
    let status = Command::new(BIN)
        .args([
            "manual",
            "--demoting",
            "live-a",
            "--candidate",
            "cand-b",
            "--log",
        ])
        .arg(unwritable_log("manual.jsonl"))
        .status()
        .expect("run resv003_hot_swap_trigger_cli");
    assert!(
        !status.success(),
        "a rejected manual audit-log record must make the command exit nonzero"
    );
}

#[test]
fn resv_3_cli_manual_exits_zero_on_healthy_log() {
    // Positive control: a writable --log path accepts the record → exit zero.
    let good_log = Path::new(env!("CARGO_TARGET_TMPDIR")).join("resv003-manual-ok.jsonl");
    let _ = std::fs::remove_file(&good_log);
    let status = Command::new(BIN)
        .args([
            "manual",
            "--demoting",
            "live-a",
            "--candidate",
            "cand-b",
            "--log",
        ])
        .arg(&good_log)
        .status()
        .expect("run resv003_hot_swap_trigger_cli");
    assert!(status.success(), "a healthy manual log must exit zero");
}

#[test]
fn resv_3_cli_evaluate_surfaces_sink_failure_cause() {
    // The CLI must surface the CONCRETE sink failure cause (not just a count) so an
    // operator can repair the degraded audit path — the reason travels end to end
    // through the automatic evaluation path.
    let output = Command::new(BIN)
        .args([
            "evaluate",
            "--live",
            "live-a",
            "--top-ranked",
            "--rank",
            "cand-b:1:2.5:0.4",
            "--log",
        ])
        .arg(unwritable_log("cause.jsonl"))
        .output()
        .expect("run resv003_hot_swap_trigger_cli");
    assert!(!output.status.success());
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        combined.contains("reason:"),
        "the unlogged line must carry the sink reason; got:\n{combined}"
    );
    assert!(
        combined.contains("cannot open log file") || combined.contains("No such file"),
        "the concrete sink failure cause must be surfaced; got:\n{combined}"
    );
}

#[test]
fn resv_3_cli_manual_no_log_exits_nonzero() {
    // Manual always fires. With NO --log there is no audit sink, so the record is
    // rejected and the command must fail closed (a trigger must never be reported
    // logged when nothing was persisted).
    let status = Command::new(BIN)
        .args(["manual", "--demoting", "live-a", "--candidate", "cand-b"])
        .status()
        .expect("run resv003_hot_swap_trigger_cli");
    assert!(
        !status.success(),
        "a firing manual command with no --log sink must exit nonzero"
    );
}

#[test]
fn resv_3_cli_evaluate_firing_no_log_exits_nonzero() {
    // An evaluate pass that FIRES a trigger with no --log sink fails closed.
    let status = Command::new(BIN)
        .args([
            "evaluate",
            "--live",
            "live-a",
            "--top-ranked",
            "--rank",
            "cand-b:1:2.5:0.4",
        ])
        .status()
        .expect("run resv003_hot_swap_trigger_cli");
    assert!(
        !status.success(),
        "an evaluate pass that fires a trigger with no --log sink must exit nonzero"
    );
}

#[test]
fn resv_3_cli_evaluate_no_fire_no_log_exits_zero() {
    // Positive control: an evaluate pass that fires NOTHING (default-disabled via
    // --inject disabled) needs no sink and exits zero.
    let status = Command::new(BIN)
        .args([
            "evaluate",
            "--live",
            "live-a",
            "--live-drawdown",
            "5000",
            "--drawdown-threshold",
            "1000",
            "--rank",
            "cand-b:1:2.5:0.4",
            "--inject",
            "disabled",
        ])
        .status()
        .expect("run resv003_hot_swap_trigger_cli");
    assert!(
        status.success(),
        "a no-fire evaluate pass needs no sink and must exit zero"
    );
}

#[test]
fn resv_3_cli_evaluate_exits_nonzero_when_log_rejected() {
    // An evaluate pass with a firing trigger + an unwritable --log rejects the
    // record (unlogged non-empty) → the command fails closed with a nonzero exit.
    let status = Command::new(BIN)
        .args([
            "evaluate",
            "--live",
            "live-a",
            "--top-ranked",
            "--rank",
            "cand-b:1:2.5:0.4",
            "--log",
        ])
        .arg(unwritable_log("evaluate.jsonl"))
        .status()
        .expect("run resv003_hot_swap_trigger_cli");
    assert!(
        !status.success(),
        "an evaluate pass with a rejected trigger log must exit nonzero"
    );
}
