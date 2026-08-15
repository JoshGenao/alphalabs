//! SRS-RESV-006 / SyRS SYS-49e — the durable cool-down window.
//!
//! The three read states (absent / readable / unreadable) and the monotonicity rule, each
//! driven against a real file. The fail-closed direction is the point: every unreadable
//! shape must reach [`CooldownState::Unknown`] and none of them may reach `NeverSwapped`,
//! because "no cool-down" is an all-clear authorising an automatic live-strategy swap.

use atp_orchestrator::cooldown::{CooldownPeriodDays, CooldownState, SwapCompletion};
use atp_orchestrator::cooldown_store::{
    self, CompletionOutcome, CooldownStoreError, ProvisionalAttempt,
};
use atp_types::StrategyId;
use std::fs;
use std::path::PathBuf;
use std::process;
use std::sync::atomic::{AtomicU64, Ordering};

const COMPLETED_AT: u64 = 1_715_000_000;
/// The attempt identity most cases do not vary; the ones that DO name their own.
const ATTEMPT: &str = "attempt-1";
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

/// The provisional record `ATTEMPT` would have written for `completion`.
fn marker(completion: SwapCompletion) -> ProvisionalAttempt {
    ProvisionalAttempt {
        attempt_id: ATTEMPT.to_string(),
        completion,
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
    // Through the SHIPPED writer, not the raw publish primitive: `save` is
    // `pub(crate)` since review r24 (it bypasses every invariant), and a test that
    // reached around the production path would be asserting about a door nobody uses.
    cooldown_store::record_completion(&path, &completion_at(COMPLETED_AT)).unwrap();

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
#[test]
fn resv_6_an_unanswerable_provisional_question_is_never_answered_false() {
    // CLAUDE.md rule 3, at the accessor r13 added. "Not provisional" is a claim about
    // a window that was READ; absent, empty and corrupt support no claims. Reporting
    // `false` for any of them would render an unreadable store as a healthy completed
    // swap on every operator surface that shows the flag.
    let dir = Scratch::new("provisional-unknown");

    let missing = dir.path("not-there.json");
    assert_eq!(
        cooldown_store::completion_is_provisional(&missing),
        None,
        "an absent store cannot say whether a swap completed"
    );

    let corrupt = dir.path("corrupt.json");
    fs::write(&corrupt, "{not json at all").unwrap();
    assert_eq!(
        cooldown_store::completion_is_provisional(&corrupt),
        None,
        "an unreadable store cannot say whether a swap completed"
    );

    // A store that exists and has been READ, with no completion in it, is also not a
    // confirmed swap — it is a configured window that no swap has ever opened.
    let empty = dir.path("empty.json");
    // A configured window that no swap has ever opened — which is what `set_period`
    // produces, and the only supported way to reach that state.
    cooldown_store::set_period(&empty, CooldownPeriodDays::default()).unwrap();
    assert_eq!(
        cooldown_store::completion_is_provisional(&empty),
        None,
        "no completion means the question does not apply, which is not `false`"
    );
}

#[test]
fn resv_6_the_provisional_flag_survives_the_round_trip_in_both_states() {
    // The non-vacuity control for the case above: an accessor that returned `None`
    // unconditionally would satisfy every assertion there.
    let dir = Scratch::new("provisional-roundtrip");
    for provisional in [true, false] {
        let path = dir.path(&format!("cd-{provisional}.json"));
        cooldown_store::record_completion(&path, &completion_at(COMPLETED_AT)).unwrap();
        if provisional {
            cooldown_store::begin_provisional(&path, &completion_at(COMPLETED_AT + 5), ATTEMPT)
                .unwrap();
        }
        assert_eq!(
            cooldown_store::completion_is_provisional(&path),
            Some(provisional),
            "the flag an operator reads must be the one on disk"
        );
    }
}

#[test]
fn resv_6_a_failed_in_window_swap_cannot_delete_the_window_it_ran_inside() {
    // Adversarial review r15 [critical]. The scenario, exactly:
    //
    //   1. swap A completes and opens a seven-day window;
    //   2. an operator ACKNOWLEDGES a manual swap B inside it — SYS-49a(a) guarantees
    //      that is allowed, so this is a normal operating sequence, not an edge case;
    //   3. B opens its provisional record;
    //   4. B FAILS, and abandons it.
    //
    // The first two-phase draft shared one slot with a `provisional` flag, so step 3
    // OVERWROTE A's completion (B's attempt instant is newer) and step 4 then wrote
    // `last_completion: None` — deleting a cool-down that was still in force and
    // resuming the automatic triggers days early. The fail-open, restored by the fix
    // for a different fail-open.
    let scratch = Scratch::new("r15-in-window");
    let path = scratch.path("cooldown.json");

    let a = SwapCompletion {
        completed_at_seconds: COMPLETED_AT,
        demoted_strategy_id: StrategyId::new("alpha"),
        promoted_strategy_id: StrategyId::new("beta"),
    };
    cooldown_store::record_completion(&path, &a).unwrap();

    let b = SwapCompletion {
        completed_at_seconds: COMPLETED_AT + 3_600,
        demoted_strategy_id: StrategyId::new("beta"),
        promoted_strategy_id: StrategyId::new("gamma"),
    };
    cooldown_store::begin_provisional(&path, &b, ATTEMPT).unwrap();

    // A's completion is untouched while B is in flight — that is the whole fix.
    let mid = cooldown_store::load(&path).unwrap().expect("record");
    assert_eq!(
        mid.last_completion.as_ref(),
        Some(&a),
        "an in-flight attempt must not displace the completion it is running inside"
    );

    cooldown_store::abandon_provisional(&path, &b, ATTEMPT).unwrap();

    let after = cooldown_store::load(&path).unwrap().expect("record");
    assert_eq!(
        after.last_completion.as_ref(),
        Some(&a),
        "abandoning a failed swap must not delete the window it ran inside"
    );
    assert_eq!(after.provisional, None, "B's marker is gone");

    // ...and the window A opened still suppresses, read through the production resolver.
    let state = cooldown_store::resolve(Some(&path), COMPLETED_AT + 7_200);
    assert!(
        !state.proven_clear(),
        "the automatic triggers must still be suppressed: A's seven days are running"
    );
    assert_eq!(state.started_at_seconds(), Some(COMPLETED_AT));
}

#[test]
fn resv_6_an_in_flight_attempt_extends_the_window_it_never_shortens() {
    // The other direction. Both slots suppress, so the pair must never resolve to LESS
    // suppression than either alone — a resolver that simply preferred `last_completion`
    // would satisfy the case above and under-suppress here, which is the direction a
    // cool-down must never move.
    let scratch = Scratch::new("r15-later-wins");
    let path = scratch.path("cooldown.json");

    let old = SwapCompletion {
        completed_at_seconds: COMPLETED_AT,
        demoted_strategy_id: StrategyId::new("alpha"),
        promoted_strategy_id: StrategyId::new("beta"),
    };
    cooldown_store::record_completion(&path, &old).unwrap();
    let newer = SwapCompletion {
        completed_at_seconds: COMPLETED_AT + SEVEN_DAYS - 10,
        demoted_strategy_id: StrategyId::new("beta"),
        promoted_strategy_id: StrategyId::new("gamma"),
    };
    cooldown_store::begin_provisional(&path, &newer, ATTEMPT).unwrap();

    // An instant PAST the confirmed window but well inside the in-flight one.
    let state = cooldown_store::resolve(Some(&path), COMPLETED_AT + SEVEN_DAYS + 60);
    assert!(
        !state.proven_clear(),
        "the later of the two windows governs; a swap may have gone live moments ago"
    );
    assert_eq!(
        state.started_at_seconds(),
        Some(COMPLETED_AT + SEVEN_DAYS - 10)
    );
}

#[test]
fn resv_6_confirming_retires_this_swaps_marker_and_leaves_another_swaps_alone() {
    // Phase two clears the marker it supersedes, or every completed swap would leave a
    // stranded provisional record that every surface reports as an interruption. It
    // must clear ONLY its own: a marker belonging to a different swap is another
    // attempt's business.
    let scratch = Scratch::new("r15-confirm-clears");
    let path = scratch.path("cooldown.json");

    let ours = SwapCompletion {
        completed_at_seconds: COMPLETED_AT,
        demoted_strategy_id: StrategyId::new("alpha"),
        promoted_strategy_id: StrategyId::new("beta"),
    };
    cooldown_store::begin_provisional(&path, &ours, ATTEMPT).unwrap();
    let confirmed = SwapCompletion {
        completed_at_seconds: COMPLETED_AT + 42,
        ..ours.clone()
    };
    cooldown_store::record_completion(&path, &confirmed).unwrap();
    let after = cooldown_store::load(&path).unwrap().expect("record");
    assert_eq!(after.last_completion.as_ref(), Some(&confirmed));
    assert_eq!(
        after.provisional, None,
        "a confirmed swap must not leave its own marker behind"
    );

    // Someone ELSE's marker survives an unrelated completion.
    let theirs = SwapCompletion {
        completed_at_seconds: COMPLETED_AT + 100,
        demoted_strategy_id: StrategyId::new("beta"),
        promoted_strategy_id: StrategyId::new("gamma"),
    };
    cooldown_store::begin_provisional(&path, &theirs, ATTEMPT).unwrap();
    let unrelated = SwapCompletion {
        completed_at_seconds: COMPLETED_AT + 200,
        demoted_strategy_id: StrategyId::new("delta"),
        promoted_strategy_id: StrategyId::new("epsilon"),
    };
    cooldown_store::record_completion(&path, &unrelated).unwrap();
    assert_eq!(
        cooldown_store::load(&path).unwrap().unwrap().provisional,
        Some(marker(theirs)),
        "one swap's completion must not retire another's in-flight marker"
    );
}

#[test]
fn resv_6_an_older_provisional_cannot_shorten_an_in_flight_window() {
    // The monotonicity rule applies WITHIN the provisional slot too, or a retry under a
    // backwards clock would pull an in-flight window's start backwards.
    let scratch = Scratch::new("r15-provisional-monotone");
    let path = scratch.path("cooldown.json");

    let newer = completion_at(COMPLETED_AT + 1_000);
    cooldown_store::begin_provisional(&path, &newer, ATTEMPT).unwrap();
    let older = completion_at(COMPLETED_AT);
    let outcome = cooldown_store::begin_provisional(&path, &older, ATTEMPT).unwrap();
    assert!(
        matches!(outcome, CompletionOutcome::KeptNewer { .. }),
        "an older attempt must not replace a newer in-flight one, got {outcome:?}"
    );
    assert_eq!(
        cooldown_store::load(&path).unwrap().unwrap().provisional,
        Some(marker(newer))
    );
}

#[test]
fn resv_6_a_retry_that_wrote_nothing_cannot_clear_the_attempt_that_did() {
    // Adversarial review r18 [critical]. The scenario, exactly:
    //
    //   1. attempt 1 of swap A->B opens a provisional window at T+1000;
    //   2. attempt 2 of the SAME pair runs with a clock that stepped backwards, so
    //      `begin_provisional` keeps the newer record (`KeptNewer`) and attempt 2
    //      writes NOTHING;
    //   3. attempt 2 fails and abandons.
    //
    // Matching a provisional record on the strategy pair alone made step 3 delete
    // attempt 1's marker — removing the only window suppressing the automatic
    // triggers after an interrupted swap. An attempt now clears exactly the record it
    // wrote, so one that wrote nothing clears nothing.
    let scratch = Scratch::new("r18-retry");
    let path = scratch.path("cooldown.json");

    let first = completion_at(COMPLETED_AT + 1_000);
    cooldown_store::begin_provisional(&path, &first, ATTEMPT).unwrap();

    // A RETRY is a different attempt and says so (review r26). Under the old model it
    // was indistinguishable from the first — same pair, and a seconds-resolution
    // instant that `--now` can pin — which is what let its cleanup clear the marker it
    // never wrote.
    let retry = completion_at(COMPLETED_AT); // same pair, older instant
    let kept = cooldown_store::begin_provisional(&path, &retry, "attempt-2").unwrap();
    assert!(
        matches!(kept, CompletionOutcome::KeptNewer { .. }),
        "the retry must be kept out, or this test is not exercising r18: {kept:?}"
    );

    cooldown_store::abandon_provisional(&path, &retry, "attempt-2").unwrap();

    assert_eq!(
        cooldown_store::load(&path).unwrap().unwrap().provisional,
        Some(marker(first)),
        "an attempt that wrote nothing must not clear the marker another attempt wrote"
    );
    let state = cooldown_store::resolve(Some(&path), COMPLETED_AT + 2_000);
    assert!(
        !state.proven_clear(),
        "the surviving window must still suppress the automatic triggers"
    );
}

#[test]
fn resv_6_the_attempt_that_did_write_still_clears_its_own_marker() {
    // The non-vacuity control, and it is load-bearing: an `abandon_provisional` that
    // never cleared anything would satisfy the case above and resurrect r6 — a failed
    // changeover leaving seven days of suppression behind it.
    let scratch = Scratch::new("r18-owner-clears");
    let path = scratch.path("cooldown.json");

    let mine = completion_at(COMPLETED_AT + 1_000);
    cooldown_store::begin_provisional(&path, &mine, ATTEMPT).unwrap();
    cooldown_store::abandon_provisional(&path, &mine, ATTEMPT).unwrap();

    assert_eq!(
        cooldown_store::load(&path).unwrap().unwrap().provisional,
        None,
        "the attempt that wrote the marker must be able to clear it"
    );
}

#[test]
fn resv_6_a_confirmation_cannot_shorten_a_newer_provisional_window() {
    // Adversarial review r19 [high]. The r18 fix stopped a failed retry DELETING a
    // newer marker; this is the same hole one slot over, on the success arm. The
    // monotonicity rule compared the offered completion against `last_completion`
    // only, so a confirmation whose clock had stepped backwards cleared a NEWER
    // provisional marker on its way past and wrote the older completion — shortening
    // the suppression an interrupted attempt had established.
    //
    // The rule is about the window IN FORCE, which is whichever slot runs later.
    let scratch = Scratch::new("r19-confirm-shorten");
    let path = scratch.path("cooldown.json");

    let interrupted = completion_at(COMPLETED_AT + 5_000);
    cooldown_store::begin_provisional(&path, &interrupted, ATTEMPT).unwrap();

    let stale_confirm = completion_at(COMPLETED_AT);
    let outcome = cooldown_store::record_completion(&path, &stale_confirm).unwrap();
    assert!(
        matches!(outcome, CompletionOutcome::KeptNewer { .. }),
        "an older completion must not displace the window in force, got {outcome:?}"
    );

    let after = cooldown_store::load(&path).unwrap().expect("record");
    assert_eq!(
        after.provisional,
        Some(marker(interrupted.clone())),
        "the newer marker must survive — it is the window that is suppressing"
    );
    assert_eq!(
        cooldown_store::resolve(Some(&path), COMPLETED_AT + 6_000).started_at_seconds(),
        Some(COMPLETED_AT + 5_000),
        "and it must still be the window an operator and the gate both read"
    );
}

#[test]
fn resv_6_a_confirmation_newer_than_the_marker_still_lands() {
    // The non-vacuity control. A store that refused every confirmation would satisfy
    // the case above and never open a window at all — which is the fail-open the whole
    // feature exists to prevent, arrived at from the opposite direction.
    let scratch = Scratch::new("r19-confirm-lands");
    let path = scratch.path("cooldown.json");

    let attempt = completion_at(COMPLETED_AT);
    cooldown_store::begin_provisional(&path, &attempt, ATTEMPT).unwrap();
    let completed = completion_at(COMPLETED_AT + 42);
    let outcome = cooldown_store::record_completion(&path, &completed).unwrap();
    assert!(
        matches!(outcome, CompletionOutcome::Recorded { .. }),
        "the ordinary phase two — completion after attempt — must land: {outcome:?}"
    );

    let after = cooldown_store::load(&path).unwrap().expect("record");
    assert_eq!(after.last_completion, Some(completed));
    assert_eq!(
        after.provisional, None,
        "and it retires its own marker on the way"
    );
}

#[test]
fn resv_6_the_window_a_writer_guards_is_the_window_a_reader_resolves() {
    // The two used to be separate copies of "which slot governs", and r19 is what
    // happens when they drift. This pins that they agree, across every arrangement of
    // the two slots — including the one where the in-flight marker is the later.
    let scratch = Scratch::new("r19-read-write-agree");
    let path = scratch.path("cooldown.json");

    cooldown_store::record_completion(&path, &completion_at(COMPLETED_AT)).unwrap();
    cooldown_store::begin_provisional(&path, &completion_at(COMPLETED_AT + 900), ATTEMPT).unwrap();

    // The reader says the later slot governs...
    assert_eq!(
        cooldown_store::resolve(Some(&path), COMPLETED_AT + 1_000).started_at_seconds(),
        Some(COMPLETED_AT + 900),
    );
    // ...so the writer must refuse anything older than THAT, not merely older than the
    // confirmed completion, which this offer is not.
    let between = completion_at(COMPLETED_AT + 500);
    assert!(
        matches!(
            cooldown_store::record_completion(&path, &between).unwrap(),
            CompletionOutcome::KeptNewer { .. }
        ),
        "a writer guarding only `last_completion` would accept this and shorten the window"
    );
}

#[test]
fn resv_6_the_preflight_cannot_roll_back_a_concurrent_period_change() {
    // Adversarial review r20 [high]. `probe_writable` proved the store was writable by
    // reading the period OUTSIDE the lock and handing it to `set_period`, which
    // reacquires the lock and writes it back. An operator running
    // `configure --set-days 30` in that gap had their change silently reverted, and
    // the window the gate then enforced was the OLD period — a shorter cool-down than
    // the one the operator configured and is watching.
    //
    // Interleaved for real, not simulated: probes run on background threads while the
    // period is changed underneath them. The assertion can only fail if the probe can
    // still write a stale period, so a correct build never reddens here.
    use std::sync::Arc;
    use std::thread;

    let scratch = Scratch::new("r20-preflight-race");
    let path = Arc::new(scratch.path("cooldown.json"));
    cooldown_store::set_period(&path, CooldownPeriodDays::default()).unwrap();

    let demoted = StrategyId::new("alpha");
    let promoted = StrategyId::new("beta");
    let probers: Vec<_> = (0..4)
        .map(|_| {
            let path = Arc::clone(&path);
            let (demoted, promoted) = (demoted.clone(), promoted.clone());
            thread::spawn(move || {
                for _ in 0..150 {
                    cooldown_store::probe_recordable(&path, &demoted, &promoted).unwrap();
                }
            })
        })
        .collect();

    let changed = CooldownPeriodDays::new(30).unwrap();
    cooldown_store::set_period(&path, changed).unwrap();

    for prober in probers {
        prober.join().expect("prober thread");
    }

    assert_eq!(
        cooldown_store::load(&path).unwrap().unwrap().period.get(),
        30,
        "a writability pre-flight must not be able to CHANGE the window it is probing — \
         reverting the period silently shortens the cool-down an operator configured"
    );
}

#[test]
fn resv_6_the_preflight_preserves_a_swap_completion_it_finds() {
    // The other half of "a probe must not change what it probes". A pre-flight that
    // wrote a fresh record would satisfy the period assertion above while erasing the
    // running window entirely — which is the fail-open, not a config regression.
    let scratch = Scratch::new("r20-preflight-preserves");
    let path = scratch.path("cooldown.json");

    let completion = completion_at(COMPLETED_AT);
    cooldown_store::record_completion(&path, &completion).unwrap();
    cooldown_store::begin_provisional(&path, &completion_at(COMPLETED_AT + 500), ATTEMPT).unwrap();
    let before = cooldown_store::load(&path).unwrap().expect("record");

    cooldown_store::probe_recordable(&path, &StrategyId::new("alpha"), &StrategyId::new("beta"))
        .unwrap();

    assert_eq!(
        cooldown_store::load(&path).unwrap().expect("record"),
        before,
        "the pre-flight must write back exactly what it read — both slots and the period"
    );
}

#[test]
fn resv_6_a_new_swap_cannot_displace_another_swaps_unconfirmed_marker() {
    // Adversarial review r21 [block]. The fail-open reached between two PROVISIONAL
    // records — the one arrangement r13/r15/r18/r19 had not covered:
    //
    //   1. swap A->B runs, phase one writes P1=(A,B)@T1, the caller publishes the
    //      designation durably (B is live), and the process is killed before phase
    //      two. P1 is now the only thing suppressing the automatic triggers;
    //   2. an operator acknowledges a manual swap B->C, which SYS-49a(a) permits
    //      inside a running window. Phase one writes P2=(B,C)@T2 and DISCARDS P1 —
    //      which looks harmless, because P2 is newer and suppresses for longer;
    //   3. B->C fails for any ordinary reason, so it abandons P2;
    //   4. nothing is left. B was promoted durably in step 1 and its automatic
    //      triggers are armed again.
    //
    // The invariant is already asserted on the other writer — one swap's completion
    // must not retire another's in-flight marker. This is the same rule here.
    let scratch = Scratch::new("r21-two-provisionals");
    let path = scratch.path("cooldown.json");

    let stranded = SwapCompletion {
        completed_at_seconds: COMPLETED_AT,
        demoted_strategy_id: StrategyId::new("alpha"),
        promoted_strategy_id: StrategyId::new("beta"),
    };
    cooldown_store::begin_provisional(&path, &stranded, ATTEMPT).unwrap();

    let follow_up = SwapCompletion {
        completed_at_seconds: COMPLETED_AT + 3_600,
        demoted_strategy_id: StrategyId::new("beta"),
        promoted_strategy_id: StrategyId::new("gamma"),
    };
    let refused = cooldown_store::begin_provisional(&path, &follow_up, ATTEMPT)
        .expect_err("a different swap's unconfirmed marker must not be replaced");
    let message = refused.to_string();
    assert!(
        message.contains("unconfirmed swap is already recorded"),
        "the refusal must name the state an operator has to resolve: {message}"
    );
    assert!(
        message.contains("record-completion"),
        "...and how to resolve it: {message}"
    );

    assert_eq!(
        cooldown_store::load(&path).unwrap().unwrap().provisional,
        Some(marker(stranded)),
        "the stranded marker survives — it may be the only thing suppressing a \
         strategy that went live before it was interrupted"
    );
}

#[test]
fn resv_6_the_same_swap_may_still_reopen_its_own_marker() {
    // The non-vacuity control. A store that refused EVERY second provisional would
    // break the ordinary retry: an operator re-running the same swap after a
    // transient failure would be told to reconcile a marker that is their own.
    let scratch = Scratch::new("r21-same-swap-retry");
    let path = scratch.path("cooldown.json");

    let first = completion_at(COMPLETED_AT);
    cooldown_store::begin_provisional(&path, &first, ATTEMPT).unwrap();
    let retry = completion_at(COMPLETED_AT + 60);
    cooldown_store::begin_provisional(&path, &retry, ATTEMPT)
        .expect("the same swap may reopen its own marker");

    assert_eq!(
        cooldown_store::load(&path).unwrap().unwrap().provisional,
        Some(marker(retry)),
        "and the retry's newer instant governs, because it suppresses for longer"
    );
}

#[test]
fn resv_6_a_confirmed_window_does_not_block_an_acknowledged_manual_swap() {
    // The refusal is about UNCONFIRMED markers only. SYS-49a(a) guarantees an
    // acknowledged manual swap stays available inside a running cool-down, so a
    // CONFIRMED completion must not be turned into a blocker by the r21 fix.
    let scratch = Scratch::new("r21-confirmed-not-a-blocker");
    let path = scratch.path("cooldown.json");

    cooldown_store::record_completion(
        &path,
        &SwapCompletion {
            completed_at_seconds: COMPLETED_AT,
            demoted_strategy_id: StrategyId::new("alpha"),
            promoted_strategy_id: StrategyId::new("beta"),
        },
    )
    .unwrap();

    cooldown_store::begin_provisional(
        &path,
        &SwapCompletion {
            completed_at_seconds: COMPLETED_AT + 3_600,
            demoted_strategy_id: StrategyId::new("beta"),
            promoted_strategy_id: StrategyId::new("gamma"),
        },
        ATTEMPT,
    )
    .expect("an acknowledged manual swap inside a confirmed window must proceed");
}

#[test]
fn resv_6_a_same_second_retry_cannot_clear_another_attempts_marker() {
    // Adversarial review r26 [high]. The last place ownership was still INFERRED rather
    // than stated. `abandon_provisional` took the strategy pair plus
    // `completed_at_seconds` as proof — but that instant is seconds-resolution and
    // `--now` can pin it, so two attempts at the same swap starting in the same second
    // are indistinguishable. Attempt A publishes its live designation and is
    // interrupted before confirming; attempt B starts in the same second, fails, and
    // its cleanup matches A's marker exactly. B clears the only thing suppressing the
    // automatic triggers against a strategy A had just promoted.
    //
    // An attempt now says WHO it is, and only that attempt can retire its marker. Note
    // the completions here are byte-identical — the whole point is that they no longer
    // decide anything.
    let scratch = Scratch::new("r26-same-second");
    let path = scratch.path("cooldown.json");

    let same_instant = completion_at(COMPLETED_AT);
    cooldown_store::begin_provisional(&path, &same_instant, "attempt-a").unwrap();

    // B's cleanup, naming an identical completion but a different attempt.
    cooldown_store::abandon_provisional(&path, &same_instant, "attempt-b").unwrap();

    assert_eq!(
        cooldown_store::load(&path).unwrap().unwrap().provisional,
        Some(ProvisionalAttempt {
            attempt_id: "attempt-a".to_string(),
            completion: same_instant.clone(),
        }),
        "a same-second retry must not clear a marker it did not write — the attempt \
         that did may already have published its live designation"
    );
    assert!(
        !cooldown_store::resolve(Some(&path), COMPLETED_AT + 60).proven_clear(),
        "and the window that marker holds must still suppress"
    );

    // The non-vacuity control: the attempt that DID write still clears its own, or the
    // r18/r22 direction breaks and a failed changeover leaves seven days of suppression.
    cooldown_store::abandon_provisional(&path, &same_instant, "attempt-a").unwrap();
    assert_eq!(
        cooldown_store::load(&path).unwrap().unwrap().provisional,
        None
    );
}

#[test]
fn resv_6_a_marker_without_an_identity_is_refused_on_read() {
    // The record is a QUAD: the completion and the attempt that owns it are written
    // together or not at all. A marker with no identity cannot say who may retire it,
    // so a payload carrying one is corruption — and corruption reads as UNKNOWN, which
    // suppresses, rather than as a marker anybody could clear.
    let scratch = Scratch::new("r26-half-record");
    let path = scratch.path("cooldown.json");
    cooldown_store::begin_provisional(&path, &completion_at(COMPLETED_AT), ATTEMPT).unwrap();

    let payload = fs::read_to_string(&path).unwrap();
    let stripped = payload.replace(&format!(",\"provisional_attempt_id\":\"{ATTEMPT}\""), "");
    assert_ne!(
        stripped, payload,
        "the attempt id must have been in the payload"
    );
    fs::write(&path, &stripped).unwrap();

    let error = cooldown_store::load(&path).expect_err("a half-present record must be refused");
    assert!(
        error.to_string().contains("half-present"),
        "the refusal must name what is missing: {error}"
    );
    assert_eq!(
        cooldown_store::resolve(Some(&path), COMPLETED_AT + 60).as_str(),
        "UNKNOWN",
        "and an unreadable window suppresses — it is never 'no cool-down'"
    );
}

// NOTE: the relative-path case needs `set_current_dir`, which is process-global and would
// race the tests above (cargo runs one file's tests as threads in a single process). It
// lives alone in `resv_6_cooldown_relative_path.rs`, which cargo builds as its own binary.
