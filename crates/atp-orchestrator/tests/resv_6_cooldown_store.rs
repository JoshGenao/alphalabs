//! SRS-RESV-006 / SyRS SYS-49e — the durable cool-down window.
//!
//! The three read states (absent / readable / unreadable) and the monotonicity rule, each
//! driven against a real file. The fail-closed direction is the point: every unreadable
//! shape must reach [`CooldownState::Unknown`] and none of them may reach `NeverSwapped`,
//! because "no cool-down" is an all-clear authorising an automatic live-strategy swap.

use atp_orchestrator::cooldown::{CooldownPeriodDays, CooldownState, SwapCompletion};
use atp_orchestrator::cooldown_store::{
    self, CompletionOutcome, CooldownRecord, CooldownStoreError,
};
use atp_types::StrategyId;
use std::fs;
use std::path::PathBuf;
use std::process;
use std::sync::atomic::{AtomicU64, Ordering};

const COMPLETED_AT: u64 = 1_715_000_000;
const SEVEN_DAYS: u64 = 7 * 86_400;

static SCRATCH_SEQ: AtomicU64 = AtomicU64::new(0);

/// A unique scratch directory, REMOVED on drop.
///
/// PID-qualified *and* sequence-qualified, and it cleans up after itself: macOS recycles
/// PIDs within ~99k, and a helper that never deletes leaves a fresh process inheriting a
/// POPULATED directory — which lands as a phantom failure on exactly the tests that assert
/// absence, in a crate the diff never touched (test-integrity rule 20, found live at
/// 57,131 leaked dirs).
struct Scratch(PathBuf);

impl Scratch {
    fn new(tag: &str) -> Self {
        let seq = SCRATCH_SEQ.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!(
            "atp-resv006-store-{tag}-{}-{seq}-{}",
            process::id(),
            line!()
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("create scratch dir");
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

fn completion_at(seconds: u64) -> SwapCompletion {
    SwapCompletion {
        completed_at_seconds: seconds,
        demoted_strategy_id: StrategyId::new("alpha"),
        promoted_strategy_id: StrategyId::new("beta"),
    }
}

// --------------------------------------------------------------------------- //
// The three read states
// --------------------------------------------------------------------------- //

#[test]
fn resv_6_an_absent_store_is_no_swap_history_not_an_error() {
    let scratch = Scratch::new("absent");
    let path = scratch.path("cooldown.json");
    assert_eq!(cooldown_store::load(&path), Ok(None));
    // And it classifies permissively: before any swap has completed, no window can be open,
    // or SRS-RESV-003's automatic triggers would be dead on a fresh install.
    assert_eq!(
        cooldown_store::resolve(Some(&path), COMPLETED_AT),
        CooldownState::NeverSwapped
    );
}

#[test]
fn resv_6_an_empty_file_is_corrupt_not_absent() {
    // The single case where "empty" must not read as "never configured": a present file
    // holding nothing is a torn write, and the last swap it was going to record is exactly
    // the fact whose loss is unsafe.
    let scratch = Scratch::new("empty");
    let path = scratch.path("cooldown.json");
    fs::write(&path, "   \n").unwrap();
    assert!(matches!(
        cooldown_store::load(&path),
        Err(CooldownStoreError::Malformed { .. })
    ));
    assert_eq!(
        cooldown_store::resolve(Some(&path), COMPLETED_AT).as_str(),
        "UNKNOWN"
    );
}

#[test]
fn resv_6_an_unreadable_store_resolves_to_unknown_never_to_never_swapped() {
    // THE fail-open this feature exists to prevent, in one assertion: a corrupt window that
    // resolved to NeverSwapped is a false all-clear that authorises an automatic swap.
    let scratch = Scratch::new("corrupt");
    let path = scratch.path("cooldown.json");
    for payload in [
        r#"{"magic":"SOMEONE-ELSES-FILE","schema_version":1,"cooldown_days":7}"#,
        r#"{"magic":"ATP-HOT-SWAP-COOLDOWN","schema_version":1,"cooldown_days":7,"surprise":1}"#,
        r#"{"magic":"ATP-HOT-SWAP-COOLDOWN","schema_version":1}"#,
        "not json at all",
        "{",
    ] {
        fs::write(&path, payload).unwrap();
        let state = cooldown_store::resolve(Some(&path), COMPLETED_AT);
        assert_eq!(state.as_str(), "UNKNOWN", "payload was: {payload}");
        assert!(!state.proven_clear(), "payload was: {payload}");
    }
}

#[test]
fn resv_6_a_future_schema_version_is_refused_rather_than_guessed() {
    let scratch = Scratch::new("version");
    let path = scratch.path("cooldown.json");
    fs::write(
        &path,
        r#"{"magic":"ATP-HOT-SWAP-COOLDOWN","schema_version":99,"cooldown_days":7}"#,
    )
    .unwrap();
    assert!(matches!(
        cooldown_store::load(&path),
        Err(CooldownStoreError::UnsupportedVersion { declared: 99, .. })
    ));
}

#[test]
fn resv_6_a_partial_completion_triple_is_malformed() {
    // A payload that half-remembers a swap has no half worth believing: a timestamp with no
    // ids is a window whose provenance is gone, and ids with no timestamp cannot be placed
    // on the timeline at all.
    let scratch = Scratch::new("partial");
    let path = scratch.path("cooldown.json");
    for payload in [
        r#"{"magic":"ATP-HOT-SWAP-COOLDOWN","schema_version":1,"cooldown_days":7,"last_completed_at_seconds":1715000000}"#,
        r#"{"magic":"ATP-HOT-SWAP-COOLDOWN","schema_version":1,"cooldown_days":7,"last_demoted_strategy_id":"alpha"}"#,
        r#"{"magic":"ATP-HOT-SWAP-COOLDOWN","schema_version":1,"cooldown_days":7,"last_completed_at_seconds":1715000000,"last_demoted_strategy_id":"alpha"}"#,
    ] {
        fs::write(&path, payload).unwrap();
        assert!(
            matches!(
                cooldown_store::load(&path),
                Err(CooldownStoreError::Malformed { .. })
            ),
            "payload was: {payload}"
        );
    }
}

#[test]
fn resv_6_a_persisted_zero_day_period_is_refused_on_read() {
    // A hand-edited file is exactly where a 0 enters, and a zero-length window would defeat
    // SYS-49e while looking like configuration. The newtype's invariant is re-checked on the
    // way IN, not merely on the way out.
    let scratch = Scratch::new("zeroday");
    let path = scratch.path("cooldown.json");
    fs::write(
        &path,
        r#"{"magic":"ATP-HOT-SWAP-COOLDOWN","schema_version":1,"cooldown_days":0}"#,
    )
    .unwrap();
    assert!(matches!(
        cooldown_store::load(&path),
        Err(CooldownStoreError::Malformed { .. })
    ));
    assert_eq!(
        cooldown_store::resolve(Some(&path), COMPLETED_AT).as_str(),
        "UNKNOWN"
    );
}

#[test]
fn resv_6_a_self_swap_completion_is_refused_on_read() {
    let scratch = Scratch::new("selfswap");
    let path = scratch.path("cooldown.json");
    fs::write(
        &path,
        r#"{"magic":"ATP-HOT-SWAP-COOLDOWN","schema_version":1,"cooldown_days":7,"last_completed_at_seconds":1715000000,"last_demoted_strategy_id":"alpha","last_promoted_strategy_id":"alpha"}"#,
    )
    .unwrap();
    assert!(matches!(
        cooldown_store::load(&path),
        Err(CooldownStoreError::Malformed { .. })
    ));
}

// --------------------------------------------------------------------------- //
// resolve()
// --------------------------------------------------------------------------- //

#[test]
fn resv_6_no_configured_path_is_unknown_not_permissive() {
    // A surface never told where the window lives cannot say whether a swap is inside one.
    let state = cooldown_store::resolve(None, COMPLETED_AT);
    assert_eq!(state.as_str(), "UNKNOWN");
    assert!(!state.proven_clear());
    assert!(state
        .degraded_reason()
        .is_some_and(|r| r.contains("--cooldown-state")));
}

// --------------------------------------------------------------------------- //
// Write paths
// --------------------------------------------------------------------------- //

#[test]
fn resv_6_a_recorded_completion_survives_a_reopen_and_opens_the_window() {
    let scratch = Scratch::new("record");
    let path = scratch.path("cooldown.json");

    let outcome = cooldown_store::record_completion(&path, &completion_at(COMPLETED_AT)).unwrap();
    assert_eq!(outcome, CompletionOutcome::Recorded { previous: None });

    // Re-read from disk — the point of a durable window is that a later process sees it.
    let state = cooldown_store::resolve(Some(&path), COMPLETED_AT + 60);
    assert!(!state.proven_clear());
    assert_eq!(state.started_at_seconds(), Some(COMPLETED_AT));
    assert_eq!(state.expires_at_seconds(), Some(COMPLETED_AT + SEVEN_DAYS));

    // ...and it closes on schedule rather than staying open forever.
    assert!(cooldown_store::resolve(Some(&path), COMPLETED_AT + SEVEN_DAYS).proven_clear());
}

#[test]
fn resv_6_the_window_starts_at_the_completion_timestamp_not_the_write_time() {
    // The AC's third clause, verbatim: "the cool-down start time is the timestamp of the
    // most recent successful swap completion". A store that stamped its own write time
    // would silently extend every window by the recording latency.
    let scratch = Scratch::new("starttime");
    let path = scratch.path("cooldown.json");
    let completed_long_ago = COMPLETED_AT - 6 * 86_400;
    cooldown_store::record_completion(&path, &completion_at(completed_long_ago)).unwrap();

    let state = cooldown_store::resolve(Some(&path), COMPLETED_AT);
    assert_eq!(state.started_at_seconds(), Some(completed_long_ago));
    // Six days in, one day left — not seven days from the write.
    assert!(!state.proven_clear());
    assert!(cooldown_store::resolve(Some(&path), completed_long_ago + SEVEN_DAYS).proven_clear());
}

#[test]
fn resv_6_an_older_completion_cannot_shorten_a_running_window() {
    // A cool-down moves forward only. A clock that stepped backwards between two swaps
    // would otherwise pull the window's start backwards and retire a live safety interval
    // early.
    let scratch = Scratch::new("monotone");
    let path = scratch.path("cooldown.json");
    cooldown_store::record_completion(&path, &completion_at(COMPLETED_AT)).unwrap();

    let outcome =
        cooldown_store::record_completion(&path, &completion_at(COMPLETED_AT - 3_600)).unwrap();
    match outcome {
        CompletionOutcome::KeptNewer { stored, offered } => {
            assert_eq!(stored.completed_at_seconds, COMPLETED_AT);
            assert_eq!(offered.completed_at_seconds, COMPLETED_AT - 3_600);
        }
        other => panic!("an older completion must be refused, got {other:?}"),
    }
    // The durable window is unchanged.
    assert_eq!(
        cooldown_store::resolve(Some(&path), COMPLETED_AT + 60).started_at_seconds(),
        Some(COMPLETED_AT)
    );
}

#[test]
fn resv_6_a_newer_completion_restarts_the_window_and_reports_the_previous() {
    let scratch = Scratch::new("restart");
    let path = scratch.path("cooldown.json");
    cooldown_store::record_completion(&path, &completion_at(COMPLETED_AT)).unwrap();

    let later = COMPLETED_AT + SEVEN_DAYS + 10;
    let outcome = cooldown_store::record_completion(&path, &completion_at(later)).unwrap();
    match outcome {
        CompletionOutcome::Recorded { previous } => {
            assert_eq!(previous.map(|p| p.completed_at_seconds), Some(COMPLETED_AT))
        }
        other => panic!("a newer completion must be recorded, got {other:?}"),
    }
    assert!(!cooldown_store::resolve(Some(&path), later + 1).proven_clear());
}

#[test]
fn resv_6_a_self_swap_completion_is_refused_before_it_is_written() {
    // Validated ahead of the durable write (durable-writes rule 4): a record this build's
    // own reader would refuse must never be published over a last-good window.
    let scratch = Scratch::new("refuse");
    let path = scratch.path("cooldown.json");
    cooldown_store::record_completion(&path, &completion_at(COMPLETED_AT)).unwrap();

    let bad = SwapCompletion {
        completed_at_seconds: COMPLETED_AT + 10,
        demoted_strategy_id: StrategyId::new("gamma"),
        promoted_strategy_id: StrategyId::new("gamma"),
    };
    assert!(cooldown_store::record_completion(&path, &bad).is_err());
    // The prior good window survived the refusal.
    assert_eq!(
        cooldown_store::resolve(Some(&path), COMPLETED_AT + 60).started_at_seconds(),
        Some(COMPLETED_AT)
    );
}

// --------------------------------------------------------------------------- //
// The writer must satisfy its own reader (adversarial review r2, finding 1)
// --------------------------------------------------------------------------- //
//
// `serialize` hand-builds one JSON line and `StrategyId::new` accepts ANY string, so an id
// carrying `"` or `\` used to produce a durable line this build's own reader could not
// parse — while `record_completion` returned Ok and the CLI reported success. The window
// then read UNKNOWN forever after, which fails closed but permanently suppresses the
// automatic triggers with nothing naming the cause.

#[test]
fn resv_6_an_id_that_would_break_the_json_line_is_refused_before_it_is_written() {
    let scratch = Scratch::new("json-escape-write");
    let path = scratch.path("cooldown.json");
    // Seed a good window, so the test also proves the refusal does not clobber it.
    cooldown_store::record_completion(&path, &completion_at(COMPLETED_AT)).unwrap();

    for (label, demoted, promoted) in [
        ("quote in demoted", "al\"pha", "beta"),
        ("quote in promoted", "alpha", "be\"ta"),
        ("backslash in demoted", "al\\pha", "beta"),
        ("backslash in promoted", "alpha", "be\\ta"),
        ("newline", "al\npha", "beta"),
        ("tab", "alpha", "be\tta"),
    ] {
        let bad = SwapCompletion {
            completed_at_seconds: COMPLETED_AT + 100,
            demoted_strategy_id: StrategyId::new(demoted),
            promoted_strategy_id: StrategyId::new(promoted),
        };
        assert!(
            matches!(
                cooldown_store::record_completion(&path, &bad),
                Err(CooldownStoreError::Malformed { .. })
            ),
            "{label}: an id this format cannot represent must be refused, not written"
        );
    }

    // The prior good window is untouched and still readable.
    let state = cooldown_store::resolve(Some(&path), COMPLETED_AT + 60);
    assert_eq!(state.as_str(), "ACTIVE");
    assert_eq!(state.started_at_seconds(), Some(COMPLETED_AT));
}

#[test]
fn resv_6_a_hand_written_escaped_id_is_refused_on_read() {
    // The other direction. A file written by a laxer build, a hand edit, or a partial
    // overwrite can still contain one — and serving it would hand a caller `al\"pha` as the
    // strategy id, which is not the strategy that swapped.
    let scratch = Scratch::new("json-escape-read");
    let path = scratch.path("cooldown.json");
    fs::write(
        &path,
        r#"{"magic":"ATP-HOT-SWAP-COOLDOWN","schema_version":1,"cooldown_days":7,"last_completed_at_seconds":1715000000,"last_demoted_strategy_id":"al\"pha","last_promoted_strategy_id":"beta"}"#,
    )
    .unwrap();
    assert!(matches!(
        cooldown_store::load(&path),
        Err(CooldownStoreError::Malformed { .. })
    ));
    assert_eq!(
        cooldown_store::resolve(Some(&path), COMPLETED_AT).as_str(),
        "UNKNOWN",
        "an unrepresentable id must fail closed, never be served"
    );
}

#[test]
fn resv_6_an_ordinary_id_with_punctuation_still_round_trips() {
    // The non-vacuity control: the refusal above must be NARROW. Hyphens, dots, colons,
    // underscores and slashes are all ordinary in a strategy id and must keep working, or
    // the fix for a corruption bug becomes an outage.
    let scratch = Scratch::new("json-escape-ok");
    let path = scratch.path("cooldown.json");
    let completion = SwapCompletion {
        completed_at_seconds: COMPLETED_AT,
        demoted_strategy_id: StrategyId::new("mean-rev.v2:eu/large_cap"),
        promoted_strategy_id: StrategyId::new("momo-3d.v11:us/small_cap"),
    };
    cooldown_store::record_completion(&path, &completion).unwrap();

    let record = cooldown_store::load(&path).unwrap().expect("a window");
    let stored = record.last_completion.expect("a completion");
    assert_eq!(
        stored.demoted_strategy_id.as_str(),
        "mean-rev.v2:eu/large_cap"
    );
    assert_eq!(
        stored.promoted_strategy_id.as_str(),
        "momo-3d.v11:us/small_cap"
    );
}

#[test]
fn resv_6_set_period_preserves_the_recorded_completion() {
    // A read-modify-write that dropped the completion would silently retire a running
    // window every time an operator adjusted the period.
    let scratch = Scratch::new("setperiod");
    let path = scratch.path("cooldown.json");
    cooldown_store::record_completion(&path, &completion_at(COMPLETED_AT)).unwrap();

    let record = cooldown_store::set_period(&path, CooldownPeriodDays::new(30).unwrap()).unwrap();
    assert_eq!(record.period.get(), 30);
    assert_eq!(
        record.last_completion.map(|c| c.completed_at_seconds),
        Some(COMPLETED_AT)
    );
    // A 30-day window outlasts day 7, where the default would have expired.
    assert!(!cooldown_store::resolve(Some(&path), COMPLETED_AT + SEVEN_DAYS + 1).proven_clear());
}

#[test]
fn resv_6_shortening_the_period_can_close_a_running_window() {
    // The counterpart, and the reason `set_period` is not merely additive: an operator who
    // shortens the cool-down expects the currently-running one to be judged by the new
    // length, not the one it started under.
    let scratch = Scratch::new("shorten");
    let path = scratch.path("cooldown.json");
    cooldown_store::record_completion(&path, &completion_at(COMPLETED_AT)).unwrap();
    let two_days_in = COMPLETED_AT + 2 * 86_400;
    assert!(!cooldown_store::resolve(Some(&path), two_days_in).proven_clear());

    cooldown_store::set_period(&path, CooldownPeriodDays::new(1).unwrap()).unwrap();
    assert!(cooldown_store::resolve(Some(&path), two_days_in).proven_clear());
}

#[test]
fn resv_6_set_period_refuses_to_overwrite_a_window_it_cannot_read() {
    // Every fallible read happens before the write, under the same guard: a corrupt window
    // must refuse the change rather than be silently replaced by it — otherwise the repair
    // path destroys the evidence of what went wrong.
    let scratch = Scratch::new("refusewrite");
    let path = scratch.path("cooldown.json");
    fs::write(&path, "{ this is not our file }").unwrap();
    assert!(cooldown_store::set_period(&path, CooldownPeriodDays::default()).is_err());
    assert_eq!(
        fs::read_to_string(&path).unwrap(),
        "{ this is not our file }",
        "the unreadable window must be left exactly as found"
    );
}

#[test]
fn resv_6_a_saved_record_is_published_atomically_and_leaves_no_scratch_behind() {
    let scratch = Scratch::new("atomic");
    let path = scratch.path("cooldown.json");
    cooldown_store::save(
        &path,
        &CooldownRecord {
            period: CooldownPeriodDays::default(),
            last_completion: Some(completion_at(COMPLETED_AT)),
        },
    )
    .unwrap();

    let leftovers: Vec<_> = fs::read_dir(path.parent().unwrap())
        .unwrap()
        .filter_map(Result::ok)
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .filter(|name| name.contains(".tmp") || name.ends_with(".lock"))
        .collect();
    assert!(
        leftovers.is_empty(),
        "scratch/lock left behind: {leftovers:?}"
    );
}
// NOTE: the relative-path case needs `set_current_dir`, which is process-global and would
// race the tests above (cargo runs one file's tests as threads in a single process). It
// lives alone in `resv_6_cooldown_relative_path.rs`, which cargo builds as its own binary.
