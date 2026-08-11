//! SRS-RESV-006 / SyRS SYS-49e — the Hot-Swap cool-down window.
//!
//! > After a successful swap, automatic triggers are ignored for the configured
//! > cool-down period (default 7 calendar days); a manual swap during cool-down
//! > requires a confirmation warning; the cool-down start time is the timestamp of
//! > the most recent successful swap completion.
//!
//! This module is the DECISION half and is deliberately pure: no clock read, no
//! filesystem, no ports. It turns *(last completion, configured period, now)* into a
//! [`CooldownState`], and [`CooldownState::proven_clear`] is the single predicate both
//! arms of SRS-RESV-003 consult:
//!
//!   * `evaluate_automatic_triggers` SUPPRESSES the whole pass when it is false, and
//!   * `request_manual_promotion` requires an operator ACKNOWLEDGEMENT when it is false.
//!
//! One predicate, two consequences. The asymmetry is only in the consequence: a
//! cool-down never *blocks* a manual swap — SRS-RESV-003 guarantees manual promotion is
//! always available — it adds a confirmation the operator can always satisfy. The
//! identical call carrying [`ManualCooldownAcknowledgement::Acknowledged`] fires.
//!
//! The durable half — reading and writing the window, and mapping a store failure to
//! [`CooldownState::Unknown`] — is [`crate::cooldown_store`]. Keeping the clock and the
//! I/O out of here is what lets the gate tests inject an `Active` or `Unknown` window
//! with no temp file and no sleeping for seven days.

use atp_types::StrategyId;
use std::fmt;

/// SyRS SYS-49e's default: automatic triggers are ignored for seven calendar days after
/// a successful swap unless the operator configured otherwise.
pub const COOLDOWN_DAYS_DEFAULT: u32 = 7;

/// A calendar day, in seconds. Unix time carries no leap seconds and UTC has no DST, so
/// "7 calendar days" is exact arithmetic here rather than an approximation of one.
pub const SECONDS_PER_CALENDAR_DAY: u64 = 86_400;

/// Upper bound on a configured period. Not taste: it keeps `days * 86_400` far inside
/// `u64` and catches a fat-fingered `70000` that would otherwise arm a 191-year window
/// nobody could tell apart from "automatic triggers are broken".
pub const COOLDOWN_DAYS_MAX: u32 = 365;

/// A configured cool-down length, in whole calendar days.
///
/// **Zero is refused, deliberately.** A 0-day period is a magic value that silently
/// defeats SYS-49e and is indistinguishable from a field nobody set — the shape
/// `CLAUDE.md` rule 3 exists to forbid. There are already two honest ways to get every
/// effect a zero would buy: disable the automatic triggers through
/// `HotSwapTriggerConfig` (which is what SYS-49a's default-disabled posture is *for*),
/// or use the manual arm, which is always available and only ever asks for an
/// acknowledgement. The sibling precedent is `DrawdownThresholdBps`, which likewise
/// refuses 0 rather than treating it as "no threshold".
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CooldownPeriodDays(u32);

/// A rejected [`CooldownPeriodDays`], carrying the value that was refused so an operator
/// surface can echo what it actually received.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CooldownPeriodError {
    pub value: u32,
}

impl fmt::Display for CooldownPeriodError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "cool-down period {} days is out of range; it must be between 1 and {} \
             (0 is refused: disable the automatic triggers through HotSwapTriggerConfig \
             instead of arming a zero-length window)",
            self.value, COOLDOWN_DAYS_MAX,
        )
    }
}

impl std::error::Error for CooldownPeriodError {}

impl CooldownPeriodDays {
    pub fn new(days: u32) -> Result<Self, CooldownPeriodError> {
        if days == 0 || days > COOLDOWN_DAYS_MAX {
            return Err(CooldownPeriodError { value: days });
        }
        Ok(Self(days))
    }

    pub const fn get(self) -> u32 {
        self.0
    }

    /// The window length in seconds. `u64` multiplication of a value bounded by
    /// [`COOLDOWN_DAYS_MAX`] cannot overflow.
    pub const fn as_seconds(self) -> u64 {
        self.0 as u64 * SECONDS_PER_CALENDAR_DAY
    }
}

impl Default for CooldownPeriodDays {
    fn default() -> Self {
        Self(COOLDOWN_DAYS_DEFAULT)
    }
}

/// A Hot-Swap that COMPLETED — the event SYS-49e starts the window from.
///
/// "Completion" means a promotion succeeded, not merely that a demotion finished.
/// SRS-RESV-004's demotion can end without any promotion (the SYS-49b demotion-pending
/// timeout), and that is not a swap: starting a window from it would suppress the
/// automatic triggers for a week because a changeover FAILED. Only the promotion side
/// (SRS-RESV-005) may record one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SwapCompletion {
    /// Unix seconds at which the swap completed. SYS-49e names this exact instant as the
    /// window's start — not the instant the record was written, which can lag it.
    pub completed_at_seconds: u64,
    /// The strategy that went to paper. A caller's claim, recorded for audit; it selects
    /// no state here.
    pub demoted_strategy_id: StrategyId,
    /// The strategy that went live. Same standing.
    pub promoted_strategy_id: StrategyId,
}

/// Where `now` sits relative to the cool-down window.
///
/// Four states, because three of the four possible answers to "may an automatic trigger
/// fire?" are distinct facts an operator needs told apart:
/// no swap has ever completed, a window is open, a window closed, and *this build cannot
/// say*. Collapsing the fourth into any of the others is the fail-open `CLAUDE.md` rule 3
/// forbids — an unreadable window that rendered as "no cool-down" is a false all-clear
/// authorising an automatic live-strategy swap.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CooldownState {
    /// No swap has ever completed, so no window can logically be open.
    NeverSwapped,
    /// `now` is inside `[started_at, expires_at)`.
    Active {
        started_at_seconds: u64,
        expires_at_seconds: u64,
        remaining_seconds: u64,
    },
    /// A swap completed and its window has closed.
    Expired {
        started_at_seconds: u64,
        expires_at_seconds: u64,
    },
    /// The window could not be determined — unconfigured, unreadable, corrupt, or
    /// arithmetically impossible. Never an answer, always a refusal.
    Unknown { reason: String },
}

impl CooldownState {
    /// Classify an instant against the recorded completion and the configured period.
    ///
    /// `completion: None` means the store was read successfully and holds no completion —
    /// [`CooldownState::NeverSwapped`]. A store that could not be READ must never reach
    /// this function as `None`; [`crate::cooldown_store::resolve`] maps that to
    /// [`CooldownState::Unknown`] instead.
    pub fn classify(
        completion: Option<&SwapCompletion>,
        period: CooldownPeriodDays,
        now_seconds: u64,
    ) -> Self {
        let Some(completion) = completion else {
            return Self::NeverSwapped;
        };
        let started = completion.completed_at_seconds;

        // `checked_add`, not `saturating_add`: a saturated `u64::MAX` expiry would render
        // as a window that is active essentially forever, with nothing to tell an
        // operator why the automatic triggers stopped firing. `Unknown` names the cause.
        let Some(expires) = started.checked_add(period.as_seconds()) else {
            return Self::unknown(format!(
                "cool-down window start {started}s plus {} days overflows u64; the \
                 recorded completion timestamp cannot be right",
                period.get(),
            ));
        };

        // `saturating_sub`, NOT `now_seconds - started`. A backwards NTP step (or a
        // completion timestamp in the future) makes `now < started`, and a wrapping
        // subtraction there yields ~5.8e11 years of "elapsed" time — the window would
        // report EXPIRED and a week-long safety interval would silently vanish at the
        // exact moment the clock became untrustworthy. Saturating to 0 elapsed keeps the
        // window OPEN, which is the safe direction to be wrong in.
        let elapsed = now_seconds.saturating_sub(started);
        if elapsed >= period.as_seconds() {
            Self::Expired {
                started_at_seconds: started,
                expires_at_seconds: expires,
            }
        } else {
            Self::Active {
                started_at_seconds: started,
                expires_at_seconds: expires,
                remaining_seconds: expires.saturating_sub(now_seconds),
            }
        }
    }

    pub fn unknown(reason: impl Into<String>) -> Self {
        Self::Unknown {
            reason: reason.into(),
        }
    }

    /// **The** predicate. True only when this build can PROVE no cool-down window covers
    /// the instant it classified.
    ///
    /// Both arms consult exactly this, so the automatic suppression and the manual
    /// confirmation can never drift apart — and a mutation to this one function reddens
    /// both families of regression test at once.
    pub fn proven_clear(&self) -> bool {
        matches!(self, Self::NeverSwapped | Self::Expired { .. })
    }

    /// The SYS-49e warning text for this window — always available, for any state.
    ///
    /// Total rather than optional so the manual gate can be written as a single
    /// `!proven_clear()` condition with no second predicate and no fallible unwrap in the
    /// refusal path. A "warning" for a clear window is simply never reached, because the
    /// gate that produces one is keyed on `proven_clear` alone.
    pub fn warning_text(&self) -> String {
        match self {
            Self::NeverSwapped => {
                "no Hot-Swap has ever completed, so no SyRS SYS-49e cool-down is in effect."
                    .to_string()
            }
            Self::Active {
                started_at_seconds,
                expires_at_seconds,
                remaining_seconds,
            } => format!(
                "a Hot-Swap cool-down is in effect (SyRS SYS-49e): the last swap completed \
                 at {started_at_seconds}s and the window does not expire until \
                 {expires_at_seconds}s ({remaining_seconds}s remaining). Automatic triggers \
                 are suppressed; confirm to swap manually anyway.",
            ),
            Self::Expired {
                started_at_seconds,
                expires_at_seconds,
            } => format!(
                "the Hot-Swap cool-down that began at {started_at_seconds}s expired at \
                 {expires_at_seconds}s (SyRS SYS-49e); no window is in effect.",
            ),
            Self::Unknown { reason } => format!(
                "the Hot-Swap cool-down window could not be determined, so this build \
                 cannot say whether a swap is within one (SyRS SYS-49e): {reason}. \
                 Confirm to swap manually anyway.",
            ),
        }
    }

    /// The warning an operator must acknowledge, or `None` when the window is provably
    /// clear.
    ///
    /// Defined IN TERMS OF [`Self::proven_clear`] rather than by a second match on the
    /// variants. Two independent matches over the same enum is exactly how a suppression
    /// rule and its warning drift apart: adding a fifth state, or moving one between
    /// "clear" and "not", would have had to be remembered in both places.
    pub fn confirmation_warning(&self) -> Option<String> {
        if self.proven_clear() {
            return None;
        }
        Some(self.warning_text())
    }

    /// Stable UPPER_SNAKE wire string, 1:1 with the variants. Pinned by
    /// `tools/hot_swap_cooldown_check.py` so a rename cannot silently change what an
    /// operator surface prints.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::NeverSwapped => "NEVER_SWAPPED",
            Self::Active { .. } => "ACTIVE",
            Self::Expired { .. } => "EXPIRED",
            Self::Unknown { .. } => "UNKNOWN",
        }
    }

    pub fn started_at_seconds(&self) -> Option<u64> {
        match self {
            Self::Active {
                started_at_seconds, ..
            }
            | Self::Expired {
                started_at_seconds, ..
            } => Some(*started_at_seconds),
            Self::NeverSwapped | Self::Unknown { .. } => None,
        }
    }

    pub fn expires_at_seconds(&self) -> Option<u64> {
        match self {
            Self::Active {
                expires_at_seconds, ..
            }
            | Self::Expired {
                expires_at_seconds, ..
            } => Some(*expires_at_seconds),
            Self::NeverSwapped | Self::Unknown { .. } => None,
        }
    }

    /// The degradation reason, when there is one. Non-`None` exactly for
    /// [`Self::Unknown`], so a caller can fold it into
    /// `TriggerEvaluation::degraded_inputs` without matching the variant itself.
    pub fn degraded_reason(&self) -> Option<&str> {
        match self {
            Self::Unknown { reason } => Some(reason.as_str()),
            _ => None,
        }
    }
}

/// The operator's SYS-49e acknowledgement for a manual swap during cool-down.
///
/// An enum rather than a `bool`: at the call site
/// `request_manual_promotion(a, b, &cooldown, true, &log, now)` is unreadable and one
/// character away from meaning its opposite, and this argument decides whether a
/// live-strategy changeover proceeds inside a safety window. It deliberately derives no
/// `Default` — there is no sensible "whichever" here, so a caller must state which one it
/// means. `tools/hot_swap_cooldown_check.py` asserts the missing `Default`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ManualCooldownAcknowledgement {
    /// The operator has not acknowledged a cool-down. A swap inside a window is refused
    /// with the warning — recoverable by repeating the call acknowledged.
    NotAcknowledged,
    /// The operator saw the warning and asked for the swap anyway.
    Acknowledged,
}

impl ManualCooldownAcknowledgement {
    pub const fn is_acknowledged(self) -> bool {
        matches!(self, Self::Acknowledged)
    }
}

#[cfg(test)]
mod resv006_cooldown_unit_tests {
    //! Round-trip and accessor checks only. Every BEHAVIOURAL test lives in
    //! `crates/atp-orchestrator/tests/resv_6_cooldown_classification.rs`, because
    //! `tools/mutation_verify.py` reverts a source file wholesale — a test sitting in the
    //! same file as its subject disappears with the code it is supposed to catch, and the
    //! run goes vacuously green (test-integrity rule 23).

    use super::*;

    #[test]
    fn wire_strings_are_one_to_one_with_the_variants() {
        let states = [
            CooldownState::NeverSwapped,
            CooldownState::Active {
                started_at_seconds: 1,
                expires_at_seconds: 2,
                remaining_seconds: 1,
            },
            CooldownState::Expired {
                started_at_seconds: 1,
                expires_at_seconds: 2,
            },
            CooldownState::unknown("x"),
        ];
        let mut seen: Vec<&str> = states.iter().map(CooldownState::as_str).collect();
        seen.sort_unstable();
        seen.dedup();
        assert_eq!(seen.len(), states.len(), "wire strings must be distinct");
    }

    #[test]
    fn the_default_period_is_seven_calendar_days() {
        assert_eq!(CooldownPeriodDays::default().get(), COOLDOWN_DAYS_DEFAULT);
        assert_eq!(CooldownPeriodDays::default().as_seconds(), 604_800);
    }
}
