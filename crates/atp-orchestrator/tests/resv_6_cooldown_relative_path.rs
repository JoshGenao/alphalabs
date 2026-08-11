//! SRS-RESV-006 — a relative cool-down path is still published durably.
//!
//! **One test, alone in its own file, deliberately.** It changes the process working
//! directory, and cargo runs the tests within a single file as threads of one process — so
//! sharing a file with any other test would make both order-dependent. Each test *binary*
//! is its own process, so a file with exactly one test cannot race anything
//! (test-integrity rule 21: an intermittent red is no more evidence than a test that
//! cannot fail).
//!
//! What it guards: a relative `cooldown.json` has an EMPTY parent, not an absent one.
//! Filtering the empty parent away places the scratch file correctly and then silently
//! skips the final directory `fsync`, so the rename that publishes the window stops being
//! crash-durable — while every other test still passes. `.` is that directory. The sibling
//! `trigger_config_store` shipped this exact bug once and fixed it; this pins that the
//! copy in `cooldown_store::save` did not reintroduce it.

use atp_orchestrator::cooldown::SwapCompletion;
use atp_orchestrator::cooldown_store;
use atp_types::StrategyId;
use std::fs;
use std::path::{Path, PathBuf};
use std::process;

const COMPLETED_AT: u64 = 1_715_000_000;

struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn resv_6_a_relative_path_still_publishes_durably() {
    let dir = std::env::temp_dir().join(format!("atp-resv006-relative-{}", process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).expect("create scratch dir");
    let scratch = Scratch(dir.clone());

    let original = std::env::current_dir().expect("read cwd");
    std::env::set_current_dir(&scratch.0).expect("enter scratch dir");

    // Restore the cwd before asserting: a panic between here and the restore would leave
    // every LATER test binary in this process... none, since this file holds one test — but
    // capturing the results first keeps the restore unconditional regardless.
    let relative = Path::new("relative-cooldown.json");
    let recorded = cooldown_store::record_completion(
        relative,
        &SwapCompletion {
            completed_at_seconds: COMPLETED_AT,
            demoted_strategy_id: StrategyId::new("alpha"),
            promoted_strategy_id: StrategyId::new("beta"),
        },
    );
    let readback = cooldown_store::resolve(Some(relative), COMPLETED_AT + 60);
    let landed_in_scratch = scratch.0.join("relative-cooldown.json").is_file();

    std::env::set_current_dir(original).expect("restore cwd");

    assert!(recorded.is_ok(), "relative record failed: {recorded:?}");
    assert!(
        landed_in_scratch,
        "the window was not written where the relative path pointed"
    );
    assert_eq!(
        readback.started_at_seconds(),
        Some(COMPLETED_AT),
        "a relative path must round-trip through the durable publish"
    );
}
