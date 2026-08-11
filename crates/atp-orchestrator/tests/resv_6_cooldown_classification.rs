//! SRS-RESV-006 / SyRS SYS-49e — the cool-down classifier, in isolation.
//!
//! Pure: no clock, no filesystem, no ports. Every case injects `now` directly, so a
//! seven-day window is tested in microseconds and the boundary is exact rather than
//! approximately observed.
//!
//! These live in `tests/` rather than beside the code because `tools/mutation_verify.py`
//! reverts a source file wholesale: an inline `#[cfg(test)]` test is deleted along with
//! the function it guards, and the mutation run goes vacuously green
//! (test-integrity rule 23).

use atp_orchestrator::cooldown::{
    CooldownPeriodDays, CooldownState, ManualCooldownAcknowledgement, SwapCompletion,
    COOLDOWN_DAYS_DEFAULT, COOLDOWN_DAYS_MAX, SECONDS_PER_CALENDAR_DAY,
};
use atp_types::StrategyId;

/// An arbitrary but fixed "last swap completed" instant. Nothing depends on its value.
const COMPLETED_AT: u64 = 1_715_000_000;
const SEVEN_DAYS: u64 = 7 * SECONDS_PER_CALENDAR_DAY;

fn completion_at(seconds: u64) -> SwapCompletion {
    SwapCompletion {
        completed_at_seconds: seconds,
        demoted_strategy_id: StrategyId::new("alpha"),
        promoted_strategy_id: StrategyId::new("beta"),
    }
}

fn classify_at(now: u64) -> CooldownState {
    CooldownState::classify(
        Some(&completion_at(COMPLETED_AT)),
        CooldownPeriodDays::default(),
        now,
    )
}

// --------------------------------------------------------------------------- //
// The three states that are answers
// --------------------------------------------------------------------------- //

#[test]
fn resv_6_no_swap_history_is_never_swapped_not_active() {
    // A fresh install has never swapped, so no window can logically be open. Reading
    // "no history" as "in cool-down" would leave SRS-RESV-003's automatic triggers dead
    // on arrival with nothing to explain why.
    let state = CooldownState::classify(None, CooldownPeriodDays::default(), COMPLETED_AT);
    assert_eq!(state, CooldownState::NeverSwapped);
    assert!(state.proven_clear());
    assert_eq!(state.as_str(), "NEVER_SWAPPED");
    assert_eq!(state.started_at_seconds(), None);
}

#[test]
fn resv_6_a_swap_one_second_ago_is_active_with_nearly_the_full_window_left() {
    let state = classify_at(COMPLETED_AT + 1);
    assert_eq!(
        state,
        CooldownState::Active {
            started_at_seconds: COMPLETED_AT,
            expires_at_seconds: COMPLETED_AT + SEVEN_DAYS,
            remaining_seconds: SEVEN_DAYS - 1,
        }
    );
    assert!(!state.proven_clear());
    assert_eq!(state.as_str(), "ACTIVE");
}

// The boundary is a PAIR. One test alone pins only which side of `>=` was written, not
// that the window is half-open `[start, start + period)`.

#[test]
fn resv_6_one_second_before_seven_days_is_still_active() {
    let state = classify_at(COMPLETED_AT + SEVEN_DAYS - 1);
    assert!(!state.proven_clear(), "the window is still open: {state:?}");
    assert_eq!(state.as_str(), "ACTIVE");
}

#[test]
fn resv_6_exactly_seven_days_later_is_expired() {
    let state = classify_at(COMPLETED_AT + SEVEN_DAYS);
    assert_eq!(
        state,
        CooldownState::Expired {
            started_at_seconds: COMPLETED_AT,
            expires_at_seconds: COMPLETED_AT + SEVEN_DAYS,
        }
    );
    assert!(state.proven_clear());
    assert_eq!(state.as_str(), "EXPIRED");
}

// --------------------------------------------------------------------------- //
// The clock cannot be trusted to move forwards
// --------------------------------------------------------------------------- //

#[test]
fn resv_6_a_backwards_clock_cannot_expire_the_window() {
    // Mutation target: `now.saturating_sub(started)` -> `now - started`. A wrapping
    // subtraction reports ~5.8e11 years elapsed, which is >= any period, so the window
    // would read EXPIRED at the exact moment the clock became untrustworthy — a week-long
    // safety interval vanishing silently. Debug builds panic on the overflow instead,
    // which is also a failure, so this test discriminates in both profiles.
    let state = classify_at(COMPLETED_AT - 60);
    assert!(
        !state.proven_clear(),
        "a clock that stepped backwards must not retire the window: {state:?}"
    );
    assert_eq!(state.as_str(), "ACTIVE");
}

#[test]
fn resv_6_a_completion_timestamp_in_the_future_fails_closed_to_active() {
    // Same arithmetic, different cause: the recorded completion is ahead of `now`. Also
    // fails closed — an operator gets a suppressed trigger and a warning naming the
    // window, not a silent all-clear.
    let state = CooldownState::classify(
        Some(&completion_at(COMPLETED_AT + 86_400)),
        CooldownPeriodDays::default(),
        COMPLETED_AT,
    );
    assert!(!state.proven_clear());
    assert_eq!(state.as_str(), "ACTIVE");
}

#[test]
fn resv_6_a_window_end_that_overflows_u64_is_unknown_not_expired() {
    // Mutation target: `checked_add` -> `saturating_add`. Saturating yields
    // `expires = u64::MAX`, which classifies ACTIVE — safe, but it renders as a window
    // that never ends and tells the operator nothing about why. `Unknown` names the cause.
    let state = CooldownState::classify(
        Some(&completion_at(u64::MAX - 10)),
        CooldownPeriodDays::default(),
        COMPLETED_AT,
    );
    assert_eq!(state.as_str(), "UNKNOWN");
    assert!(!state.proven_clear());
    assert!(
        state
            .degraded_reason()
            .is_some_and(|r| r.contains("overflow")),
        "the reason must name the cause: {state:?}"
    );
}

// --------------------------------------------------------------------------- //
// Unknown is never an answer
// --------------------------------------------------------------------------- //

#[test]
fn resv_6_unknown_is_not_proven_clear() {
    // Mutation target: adding `Unknown` to `proven_clear`'s match. That single change is
    // the whole fail-open — an unreadable window would authorise an automatic
    // live-strategy swap. CLAUDE.md rule 3.
    let state = CooldownState::unknown("store is corrupt");
    assert!(!state.proven_clear());
    assert_eq!(state.as_str(), "UNKNOWN");
    assert_eq!(state.started_at_seconds(), None);
    assert_eq!(state.expires_at_seconds(), None);
}

#[test]
fn resv_6_only_unknown_reports_a_degraded_reason() {
    // An open window is a HEALTHY cool-down, not a degraded input: suppressing on it must
    // not make the CLI exit nonzero the way an unreadable store does.
    assert!(classify_at(COMPLETED_AT + 1).degraded_reason().is_none());
    assert!(classify_at(COMPLETED_AT + SEVEN_DAYS)
        .degraded_reason()
        .is_none());
    assert!(CooldownState::NeverSwapped.degraded_reason().is_none());
    assert!(CooldownState::unknown("x").degraded_reason().is_some());
}

#[test]
fn resv_6_confirmation_warning_is_some_exactly_when_not_proven_clear() {
    // The two consequences are derived from ONE predicate; this pins them together, so a
    // change that suppressed automatically but stopped warning manually cannot pass.
    for state in [
        CooldownState::NeverSwapped,
        classify_at(COMPLETED_AT + 1),
        classify_at(COMPLETED_AT + SEVEN_DAYS),
        CooldownState::unknown("unreadable"),
    ] {
        assert_eq!(
            state.confirmation_warning().is_some(),
            !state.proven_clear(),
            "warning and suppression disagreed for {state:?}"
        );
    }
}

#[test]
fn resv_6_the_active_warning_names_the_expiry_the_operator_is_overriding() {
    // The AC asks for a "confirmation warning", which is content, not a boolean: an
    // operator overriding a safety window must be told which window and until when.
    let warning = classify_at(COMPLETED_AT + 1)
        .confirmation_warning()
        .expect("an active window must warn");
    assert!(warning.contains(&(COMPLETED_AT + SEVEN_DAYS).to_string()));
    assert!(warning.contains("SYS-49e"));
}

#[test]
fn resv_6_the_unknown_warning_says_it_could_not_tell_rather_than_naming_a_window() {
    let warning = CooldownState::unknown("state file is unreadable")
        .confirmation_warning()
        .expect("an unknown window must warn");
    assert!(warning.contains("state file is unreadable"));
    assert!(
        warning.contains("could not be determined"),
        "an unknown window must not be described as a known one: {warning}"
    );
}

// --------------------------------------------------------------------------- //
// The configured period
// --------------------------------------------------------------------------- //

#[test]
fn resv_6_a_zero_day_period_is_refused() {
    // 0 would silently defeat SYS-49e while looking like configuration. The honest ways
    // to disable automatic swapping are HotSwapTriggerConfig and the manual arm.
    let error = CooldownPeriodDays::new(0).expect_err("0 days must be refused");
    assert_eq!(error.value, 0);
}

#[test]
fn resv_6_a_period_over_the_maximum_is_refused() {
    assert!(CooldownPeriodDays::new(COOLDOWN_DAYS_MAX + 1).is_err());
    assert!(CooldownPeriodDays::new(COOLDOWN_DAYS_MAX).is_ok());
    assert!(CooldownPeriodDays::new(1).is_ok());
}

#[test]
fn resv_6_the_default_period_is_seven_calendar_days() {
    assert_eq!(COOLDOWN_DAYS_DEFAULT, 7);
    assert_eq!(CooldownPeriodDays::default().as_seconds(), 604_800);
}

#[test]
fn resv_6_a_configured_period_replaces_the_default_in_both_directions() {
    // Non-vacuity for "configurable": a one-day window must EXPIRE where the default is
    // still open, and a 30-day window must still be OPEN where the default has expired.
    let one_day = CooldownPeriodDays::new(1).unwrap();
    let thirty = CooldownPeriodDays::new(30).unwrap();
    let two_days_later = COMPLETED_AT + 2 * SECONDS_PER_CALENDAR_DAY;

    let short =
        CooldownState::classify(Some(&completion_at(COMPLETED_AT)), one_day, two_days_later);
    assert!(
        short.proven_clear(),
        "a 1-day window must be closed by day 2"
    );

    let long = CooldownState::classify(
        Some(&completion_at(COMPLETED_AT)),
        thirty,
        COMPLETED_AT + SEVEN_DAYS + 1,
    );
    assert!(!long.proven_clear(), "a 30-day window must outlast day 7");
}

// --------------------------------------------------------------------------- //
// The window never reopens (L2-style sweep over a whole timeline)
// --------------------------------------------------------------------------- //

#[test]
fn resv_6_the_window_never_reopens_across_a_swept_timeline() {
    // A cool-down must be monotone: once `proven_clear` becomes true it stays true as
    // time advances. An off-by-one or a modular expiry would show up as a flip back.
    let period = CooldownPeriodDays::new(1).unwrap();
    let span = SECONDS_PER_CALENDAR_DAY;
    let mut flips = 0;
    let mut previous = CooldownState::classify(
        Some(&completion_at(COMPLETED_AT)),
        period,
        COMPLETED_AT.saturating_sub(5),
    )
    .proven_clear();

    for offset in 0..=(span + 5) {
        let clear = CooldownState::classify(
            Some(&completion_at(COMPLETED_AT)),
            period,
            COMPLETED_AT - 5 + offset,
        )
        .proven_clear();
        if clear != previous {
            flips += 1;
            assert!(
                clear,
                "the window reopened after closing at offset {offset}"
            );
            previous = clear;
        }
    }
    assert_eq!(flips, 1, "the window must close exactly once");
}

// --------------------------------------------------------------------------- //
// The acknowledgement type
// --------------------------------------------------------------------------- //

#[test]
fn resv_6_the_acknowledgement_is_explicit_in_both_directions() {
    assert!(ManualCooldownAcknowledgement::Acknowledged.is_acknowledged());
    assert!(!ManualCooldownAcknowledgement::NotAcknowledged.is_acknowledged());
}
