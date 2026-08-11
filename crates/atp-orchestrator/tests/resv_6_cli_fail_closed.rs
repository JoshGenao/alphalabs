//! SRS-RESV-006 / SyRS SYS-49e — the operator CLIs must fail closed at the PROCESS level.
//!
//! L4 boundary: spawns the real `resv003_hot_swap_trigger_cli` and
//! `resv006_hot_swap_cooldown_cli` binaries against real files. Shell automation is the
//! consumer that matters here — a wrapper script must not be able to read "the cool-down
//! could not be determined" as "go ahead".
//!
//! The exit-code contract, in one place:
//!
//! | situation                                  | exit |
//! |--------------------------------------------|------|
//! | window ACTIVE, automatic pass suppressed   | 0    (a working cool-down is healthy) |
//! | window UNKNOWN (unreadable / unconfigured) | 1    (a failed read is never an answer) |
//! | manual inside a window, unacknowledged     | 1    (recoverable: re-run --confirm-cooldown) |
//! | manual inside a window, acknowledged       | 0    |

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

const TRIGGER_BIN: &str = env!("CARGO_BIN_EXE_resv003_hot_swap_trigger_cli");
const COOLDOWN_BIN: &str = env!("CARGO_BIN_EXE_resv006_hot_swap_cooldown_cli");

const COMPLETED_AT: u64 = 1_715_000_000;
const DURING_COOLDOWN: u64 = COMPLETED_AT + 3_600;
const AFTER_COOLDOWN: u64 = COMPLETED_AT + 7 * 86_400 + 1;

/// A scratch directory unique per test and removed on drop (test-integrity rule 20: a
/// leaked, PID-recycled scratch dir lands as a phantom failure on absence assertions).
struct Scratch(PathBuf);

impl Scratch {
    fn new(tag: &str) -> Self {
        let dir = Path::new(env!("CARGO_TARGET_TMPDIR")).join(format!("resv006-cli-{tag}"));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("scratch dir");
        Self(dir)
    }

    fn path(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn run(bin: &str, args: &[&str]) -> Output {
    Command::new(bin).args(args).output().expect("run binary")
}

fn stdout_of(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).to_string()
}

/// Open a real window by recording a completion through the real writer.
fn open_window(state: &Path) {
    let output = run(
        COOLDOWN_BIN,
        &[
            "record-completion",
            "--state",
            state.to_str().unwrap(),
            "--demoted",
            "live-a",
            "--promoted",
            "cand-b",
            "--completed-at",
            &COMPLETED_AT.to_string(),
        ],
    );
    assert!(
        output.status.success(),
        "seeding the window failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn count_log_lines(log: &Path) -> usize {
    fs::read_to_string(log)
        .map(|s| s.lines().filter(|l| !l.trim().is_empty()).count())
        .unwrap_or(0)
}

// --------------------------------------------------------------------------- //
// The fail-closed posture: omitting the window is NOT permissive
// --------------------------------------------------------------------------- //

#[test]
fn resv_6_cli_evaluate_without_a_cooldown_state_suppresses_and_exits_nonzero() {
    // The operator-chosen posture. A surface never told where the window lives cannot say
    // whether a swap is inside one, so it must not fire and must not exit zero.
    let scratch = Scratch::new("no-state-eval");
    let log = scratch.path("t.jsonl");
    let output = run(
        TRIGGER_BIN,
        &[
            "evaluate",
            "--live",
            "live-a",
            "--top-ranked",
            "--rank",
            "cand-b:1:2.5:0.4",
            "--log",
            log.to_str().unwrap(),
            "--now",
            &DURING_COOLDOWN.to_string(),
        ],
    );
    assert!(
        !output.status.success(),
        "an unknown window must exit nonzero"
    );
    let stdout = stdout_of(&output);
    assert!(stdout.contains("cooldown-state:UNKNOWN"), "got:\n{stdout}");
    assert!(
        stdout.contains("cooldown-suppressed:true"),
        "got:\n{stdout}"
    );
    assert_eq!(count_log_lines(&log), 0, "a suppressed pass logs nothing");
}

#[test]
fn resv_6_cli_manual_without_a_cooldown_state_is_refused() {
    let scratch = Scratch::new("no-state-manual");
    let log = scratch.path("t.jsonl");
    let output = run(
        TRIGGER_BIN,
        &[
            "manual",
            "--demoting",
            "live-a",
            "--candidate",
            "cand-b",
            "--log",
            log.to_str().unwrap(),
            "--now",
            &DURING_COOLDOWN.to_string(),
        ],
    );
    assert!(!output.status.success());
    assert!(stdout_of(&output).contains("manual-refused:COOLDOWN_CONFIRMATION_REQUIRED"));
    assert_eq!(count_log_lines(&log), 0);
}

// --------------------------------------------------------------------------- //
// A real window, through both binaries
// --------------------------------------------------------------------------- //

#[test]
fn resv_6_cli_a_recorded_completion_suppresses_the_next_evaluate() {
    // The whole AC in one flow, across two processes and one file: record a completion,
    // then watch the automatic triggers stop firing — and stop LOGGING.
    let scratch = Scratch::new("suppress");
    let state = scratch.path("cd.json");
    let log = scratch.path("t.jsonl");
    let args = |now: &str| -> Vec<String> {
        vec![
            "evaluate".into(),
            "--live".into(),
            "live-a".into(),
            "--top-ranked".into(),
            "--rank".into(),
            "cand-b:1:2.5:0.4".into(),
            "--log".into(),
            log.to_str().unwrap().into(),
            "--cooldown-state".into(),
            state.to_str().unwrap().into(),
            "--now".into(),
            now.into(),
        ]
    };

    // Before any swap: fires, and logs exactly one record.
    let before = Command::new(TRIGGER_BIN)
        .args(args(&COMPLETED_AT.to_string()))
        .output()
        .expect("run");
    assert!(before.status.success());
    assert!(stdout_of(&before).contains("cooldown-state:NEVER_SWAPPED"));
    assert_eq!(count_log_lines(&log), 1);

    open_window(&state);

    // Inside the window: suppressed, exit ZERO (a working cool-down is healthy), and the
    // audit log is untouched — "ignored" has to mean ignored.
    let during = Command::new(TRIGGER_BIN)
        .args(args(&DURING_COOLDOWN.to_string()))
        .output()
        .expect("run");
    assert!(
        during.status.success(),
        "an ACTIVE window is healthy, not degraded — it must not exit nonzero"
    );
    let stdout = stdout_of(&during);
    assert!(stdout.contains("cooldown-state:ACTIVE"), "got:\n{stdout}");
    assert!(stdout.contains("selected:NONE"), "got:\n{stdout}");
    assert_eq!(
        count_log_lines(&log),
        1,
        "a suppressed pass must append no audit record"
    );

    // Past the window: fires again. Without this the test above passes on a wholly broken
    // evaluator.
    let after = Command::new(TRIGGER_BIN)
        .args(args(&AFTER_COOLDOWN.to_string()))
        .output()
        .expect("run");
    assert!(after.status.success());
    assert!(stdout_of(&after).contains("cooldown-state:EXPIRED"));
    assert_eq!(
        count_log_lines(&log),
        2,
        "an expired window must let it log again"
    );
}

#[test]
fn resv_6_cli_manual_inside_a_window_is_refused_then_fires_when_confirmed() {
    // The paired refusal/override, at the process level: the SAME command line plus one
    // flag must succeed, or "manual promotion is always available" is not true of the CLI.
    let scratch = Scratch::new("manual-pair");
    let state = scratch.path("cd.json");
    let log = scratch.path("t.jsonl");
    open_window(&state);

    let base: Vec<String> = vec![
        "manual".into(),
        "--demoting".into(),
        "live-a".into(),
        "--candidate".into(),
        "cand-c".into(),
        "--log".into(),
        log.to_str().unwrap().into(),
        "--cooldown-state".into(),
        state.to_str().unwrap().into(),
        "--now".into(),
        DURING_COOLDOWN.to_string(),
    ];

    let refused = Command::new(TRIGGER_BIN).args(&base).output().expect("run");
    assert!(!refused.status.success());
    let stdout = stdout_of(&refused);
    assert!(stdout.contains("manual-refused:COOLDOWN_CONFIRMATION_REQUIRED"));
    assert!(
        stdout.contains("cooldown-warning:"),
        "the refusal must carry the warning text, not just a code; got:\n{stdout}"
    );
    assert!(
        stdout.contains("cooldown-override-available:--confirm-cooldown"),
        "the refusal must name its own remedy; got:\n{stdout}"
    );
    assert!(
        stdout.contains("manual-always-available:true"),
        "the SRS-RESV-003 invariant must still be asserted; got:\n{stdout}"
    );
    assert_eq!(count_log_lines(&log), 0);

    let mut confirmed = base.clone();
    confirmed.push("--confirm-cooldown".into());
    let fired = Command::new(TRIGGER_BIN)
        .args(&confirmed)
        .output()
        .expect("run");
    assert!(
        fired.status.success(),
        "an acknowledged manual swap must fire: {}",
        String::from_utf8_lossy(&fired.stderr)
    );
    let stdout = stdout_of(&fired);
    assert!(stdout.contains("manual-logged:true"));
    assert!(
        stdout.contains("cooldown-override:true"),
        "a swap that overrode a live window must say so for the audit reader; got:\n{stdout}"
    );
    assert_eq!(count_log_lines(&log), 1);
}

#[test]
fn resv_6_cli_a_warning_stays_on_one_proof_line() {
    // The proof stream is line-oriented key:value and the Python arm splits it that way, so
    // a multi-line warning would silently become bogus extra keys.
    let scratch = Scratch::new("one-line");
    let state = scratch.path("cd.json");
    let log = scratch.path("t.jsonl");
    open_window(&state);

    let output = run(
        TRIGGER_BIN,
        &[
            "manual",
            "--demoting",
            "live-a",
            "--candidate",
            "cand-c",
            "--log",
            log.to_str().unwrap(),
            "--cooldown-state",
            state.to_str().unwrap(),
            "--now",
            &DURING_COOLDOWN.to_string(),
        ],
    );
    let stdout = stdout_of(&output);
    let warning_lines = stdout
        .lines()
        .filter(|line| line.starts_with("cooldown-warning:"))
        .count();
    assert_eq!(warning_lines, 1, "exactly one warning line; got:\n{stdout}");
    // And every emitted line is a well-formed `key:value` — a newline that escaped into a
    // value would show up here as a line with no colon.
    for line in stdout.lines().filter(|l| !l.is_empty()) {
        assert!(
            line.contains(':'),
            "a proof line lost its key, so a value carried a newline: {line:?}"
        );
    }
}

// --------------------------------------------------------------------------- //
// The clock
// --------------------------------------------------------------------------- //

#[test]
fn resv_6_cli_now_defaults_to_the_real_clock() {
    // The regression test for durable-writes rule 23. The tool used to hardcode
    // 1_715_000_000, which would make a scheduled evaluate see zero elapsed time forever —
    // every cool-down either permanent or never-starting.
    let scratch = Scratch::new("real-clock");
    let state = scratch.path("cd.json");
    let log = scratch.path("t.jsonl");

    let harness_now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("harness clock is after the epoch")
        .as_secs();

    let output = run(
        TRIGGER_BIN,
        &[
            "evaluate",
            "--live",
            "live-a",
            "--log",
            log.to_str().unwrap(),
            "--cooldown-state",
            state.to_str().unwrap(),
        ],
    );
    let stdout = stdout_of(&output);
    let observed: u64 = stdout
        .lines()
        .find_map(|line| line.strip_prefix("observed-at-seconds:"))
        .expect("the pass must report the instant it used")
        .trim()
        .parse()
        .expect("observed-at-seconds is a number");

    assert!(
        observed.abs_diff(harness_now) < 120,
        "the default instant must come from the real clock, not a frozen constant \
         (observed {observed}, harness {harness_now})"
    );
    assert_ne!(
        observed, 1_715_000_000,
        "the frozen demonstration constant must not be the default any more"
    );
}

// --------------------------------------------------------------------------- //
// resv006_hot_swap_cooldown_cli
// --------------------------------------------------------------------------- //

#[test]
fn resv_6_cli_status_reports_an_unreadable_window_as_unknown_and_exits_nonzero() {
    let scratch = Scratch::new("status-corrupt");
    let state = scratch.path("cd.json");
    fs::write(&state, "not our file").unwrap();

    let output = run(
        COOLDOWN_BIN,
        &[
            "status",
            "--state",
            state.to_str().unwrap(),
            "--now",
            &DURING_COOLDOWN.to_string(),
        ],
    );
    assert!(!output.status.success(), "a failed read is never an answer");
    let stdout = stdout_of(&output);
    assert!(stdout.contains("cooldown-state:UNKNOWN"), "got:\n{stdout}");
    assert!(
        stdout.contains("cooldown-in-effect:true"),
        "an unreadable window must read as IN EFFECT, never as clear; got:\n{stdout}"
    );
}

#[test]
fn resv_6_cli_status_on_an_absent_window_is_clear_and_exits_zero() {
    // The positive control for the test above: absent means no swap has ever completed.
    let scratch = Scratch::new("status-absent");
    let output = run(
        COOLDOWN_BIN,
        &[
            "status",
            "--state",
            scratch.path("cd.json").to_str().unwrap(),
            "--now",
            &DURING_COOLDOWN.to_string(),
        ],
    );
    assert!(output.status.success());
    let stdout = stdout_of(&output);
    assert!(stdout.contains("cooldown-state:NEVER_SWAPPED"));
    assert!(stdout.contains("cooldown-in-effect:false"));
    assert!(stdout.contains("cooldown-days-default:7"));
}

#[test]
fn resv_6_cli_an_older_completion_is_refused_and_names_both_timestamps() {
    let scratch = Scratch::new("older");
    let state = scratch.path("cd.json");
    open_window(&state);

    let output = run(
        COOLDOWN_BIN,
        &[
            "record-completion",
            "--state",
            state.to_str().unwrap(),
            "--demoted",
            "live-a",
            "--promoted",
            "cand-b",
            "--completed-at",
            &(COMPLETED_AT - 3_600).to_string(),
        ],
    );
    assert!(
        !output.status.success(),
        "a backwards clock must be operator-actionable, not a silent zero exit"
    );
    let stdout = stdout_of(&output);
    assert!(stdout.contains("completion-recorded:false"));
    assert!(stdout.contains(&format!("kept-stored-completion-at-seconds:{COMPLETED_AT}")));
}

#[test]
fn resv_6_cli_configure_refuses_a_zero_day_period_and_keeps_the_window() {
    let scratch = Scratch::new("zero-days");
    let state = scratch.path("cd.json");
    open_window(&state);
    let before = fs::read_to_string(&state).unwrap();

    let output = run(
        COOLDOWN_BIN,
        &[
            "configure",
            "--state",
            state.to_str().unwrap(),
            "--set-days",
            "0",
        ],
    );
    assert!(!output.status.success());
    assert_eq!(
        fs::read_to_string(&state).unwrap(),
        before,
        "a refused period must not have touched the durable window"
    );
}

#[test]
fn resv_6_cli_configure_preserves_the_completion_and_the_reread_agrees() {
    let scratch = Scratch::new("configure");
    let state = scratch.path("cd.json");
    open_window(&state);

    let output = run(
        COOLDOWN_BIN,
        &[
            "configure",
            "--state",
            state.to_str().unwrap(),
            "--set-days",
            "30",
        ],
    );
    assert!(output.status.success());
    let stdout = stdout_of(&output);
    assert!(stdout.contains("cooldown-days:30"));
    assert!(
        stdout.contains("completion-preserved:true"),
        "changing the period must not retire a running window; got:\n{stdout}"
    );
    assert!(
        stdout.contains("reread-cooldown-days:30"),
        "the reported result must be the bytes that landed; got:\n{stdout}"
    );
}

#[test]
fn resv_6_cli_record_completion_names_its_deferred_production_writer() {
    // The write path is real, but the caller that SHOULD invoke it is SRS-RESV-005's
    // promotion. Saying so on the proof stream is what keeps this surface from reading as
    // the shipped runtime binding.
    let scratch = Scratch::new("deferred");
    let state = scratch.path("cd.json");
    let output = run(
        COOLDOWN_BIN,
        &[
            "record-completion",
            "--state",
            state.to_str().unwrap(),
            "--demoted",
            "live-a",
            "--promoted",
            "cand-b",
            "--completed-at",
            &COMPLETED_AT.to_string(),
        ],
    );
    assert!(output.status.success());
    assert!(stdout_of(&output).contains("deferred-writer:SRS-RESV-005"));
}

#[test]
fn resv_6_cli_record_completion_refuses_a_self_swap() {
    let scratch = Scratch::new("self-swap");
    let output = run(
        COOLDOWN_BIN,
        &[
            "record-completion",
            "--state",
            scratch.path("cd.json").to_str().unwrap(),
            "--demoted",
            "same",
            "--promoted",
            "same",
            "--completed-at",
            &COMPLETED_AT.to_string(),
        ],
    );
    assert!(
        !output.status.success(),
        "demoting and promoting one strategy is not a swap and must not start a window"
    );
    assert!(
        !scratch.path("cd.json").exists(),
        "nothing may have been written"
    );
}
