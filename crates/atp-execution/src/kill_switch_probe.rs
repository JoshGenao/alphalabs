//! SRS-SAFE-002 / SyRS SYS-44b — the concrete `KillSwitchLiquidationProbe`:
//! the 30-second wait loop awaiting fill confirmation on a kill-switch
//! liquidation order.
//!
//! [`PollingLiquidationProbe`] polls a [`BrokerOpenOrderSource`] (the same
//! broker order-state seam the SRS-EXE-009 outbox reconciliation consumes, so
//! one future live SRS-EXE-006 wire source serves both consumers) on an
//! injected [`KillSwitchProbeClock`] until the liquidation order confirms
//! `Filled` or `request.timeout_seconds` elapses. Timing is injected — tests
//! and the operator CLI drive a simulated clock, so a full 30 s drill
//! completes instantly and no test ever sleeps.
//!
//! Fail-closed rules (each pinned by
//! `crates/atp-execution/tests/srs_safe_002_polling_probe.rs`):
//!
//! * A source error maps to the typed [`KillSwitchProbeError`] immediately —
//!   `ConnectivityBlocked` → `connectivity_blocked`, `Timeout` →
//!   `probe_timeout`, `StaleData` / `MalformedSnapshot` / `Unavailable` →
//!   `order_state_unavailable`. No retry: the gate must fail closed on an
//!   unconfirmable order state, not guess.
//! * An order ABSENT from a [`SnapshotCoverage::OpenOnly`] snapshot is
//!   ambiguous (it may have filled or been purged) → `order_state_unavailable`.
//!   Absent under `OpenAndRecentlyCompleted` is a complete-enough view that
//!   the order is provably not filled-and-recent → keep polling to the
//!   deadline (never guess a fill).
//! * Present-but-not-`Filled` (including `PartiallyFilled` and the terminal
//!   `Cancelled` / `Rejected` / `Expired`) keeps polling to the deadline: only
//!   a broker-confirmed `Filled` is a fill, and the probe NEVER reports
//!   `TimedOutUnfilled` before the deadline — the gate's outcome-consistency
//!   hardening would reject that as a probe inconsistency.
//! * The deadline is `request.timeout_seconds`, converted to milliseconds,
//!   and it is enforced BEFORE each poll: a fill is only ever accepted when
//!   observed strictly inside the window, so a real clock's final sleep
//!   overshooting under scheduler jitter can never smuggle a post-deadline
//!   fill through second-truncation. The loop waits
//!   `min(poll_interval, remaining)` between polls, and
//!   `elapsed_seconds >= timeout_seconds` always holds on the timeout
//!   outcome.

use atp_types::{KillSwitchLiquidationOutcome, KillSwitchTimeoutRequest, OrderState};

use crate::outbox::{BrokerOpenOrderSource, BrokerReconcileError, SnapshotCoverage};
use crate::{KillSwitchLiquidationProbe, KillSwitchProbeError};

/// Milliseconds between fill-confirmation polls. 500 ms keeps the probe well
/// inside the 30 s SYS-44b budget (61 polls) without hammering the broker
/// order-state source.
pub const KILL_SWITCH_FILL_POLL_INTERVAL_MS: u64 = 500;

/// The probe's injected timing authority. Production uses a monotonic
/// wall-clock (`RealProbeClock` in the orchestrator composition); tests and
/// the operator CLI use a simulated clock whose `wait_ms` advances the
/// reading instead of sleeping, so the 30 s wait loop is exercised for real
/// without any test ever blocking.
pub trait KillSwitchProbeClock {
    /// Monotonic milliseconds. Only differences are meaningful.
    fn monotonic_ms(&self) -> u64;

    /// Block (or, on a simulated clock, advance) for `ms` milliseconds
    /// between fill polls.
    fn wait_ms(&self, ms: u64);
}

/// The concrete SRS-SAFE-002 wait loop. Generic over the clock and the broker
/// order-state source so the SYS-44b scenario drives it with a scripted fill
/// feed and the live runtime binds the real SRS-EXE-006 wire source.
pub struct PollingLiquidationProbe<'a, C: KillSwitchProbeClock, S: BrokerOpenOrderSource> {
    clock: &'a C,
    fills: &'a S,
    poll_interval_ms: u64,
}

impl<'a, C: KillSwitchProbeClock, S: BrokerOpenOrderSource> PollingLiquidationProbe<'a, C, S> {
    pub fn new(clock: &'a C, fills: &'a S) -> Self {
        Self::with_poll_interval(clock, fills, KILL_SWITCH_FILL_POLL_INTERVAL_MS)
    }

    /// Custom poll cadence. Exposed for tests; production uses
    /// [`KILL_SWITCH_FILL_POLL_INTERVAL_MS`]. A zero interval is clamped to
    /// 1 ms so the loop always makes progress.
    pub fn with_poll_interval(clock: &'a C, fills: &'a S, poll_interval_ms: u64) -> Self {
        Self {
            clock,
            fills,
            poll_interval_ms: poll_interval_ms.max(1),
        }
    }

    /// One poll: `Ok(Some(fill_confirmed))` when the source can vouch for the
    /// order, `Ok(None)`… never — an unconfirmable state is an `Err` (fail
    /// closed), and "present but not filled / provably absent" is
    /// `Ok(false)` (keep waiting).
    fn poll_filled(
        &self,
        request: &KillSwitchTimeoutRequest,
    ) -> Result<bool, KillSwitchProbeError> {
        let snapshot = self.fills.open_orders().map_err(|error| match error {
            BrokerReconcileError::ConnectivityBlocked { reason } => {
                KillSwitchProbeError::connectivity_blocked(reason)
            }
            BrokerReconcileError::Timeout { reason } => KillSwitchProbeError::probe_timeout(reason),
            BrokerReconcileError::StaleData { reason }
            | BrokerReconcileError::MalformedSnapshot { reason }
            | BrokerReconcileError::Unavailable { reason } => {
                KillSwitchProbeError::order_state_unavailable(reason)
            }
        })?;
        let order_id = request.unfilled_order.order_id.as_str();
        let order = snapshot
            .orders()
            .iter()
            .find(|order| order.key.to_string() == order_id);
        match order {
            Some(order) => Ok(order.state == OrderState::Filled),
            None => match snapshot.coverage() {
                // An open-only view cannot distinguish "filled" from
                // "cancelled/purged" for an absent order — unconfirmable.
                SnapshotCoverage::OpenOnly => {
                    Err(KillSwitchProbeError::order_state_unavailable(format!(
                        "liquidation order {order_id} absent from an OpenOnly broker snapshot — \
                         cannot confirm whether it filled"
                    )))
                }
                // A complete-enough view proves the order is not
                // filled-and-recent; keep waiting for it to appear/fill.
                SnapshotCoverage::OpenAndRecentlyCompleted => Ok(false),
            },
        }
    }
}

impl<C: KillSwitchProbeClock, S: BrokerOpenOrderSource> KillSwitchLiquidationProbe
    for PollingLiquidationProbe<'_, C, S>
{
    fn await_filled_or_timeout(
        &self,
        request: &KillSwitchTimeoutRequest,
    ) -> Result<KillSwitchLiquidationOutcome, KillSwitchProbeError> {
        let started_ms = self.clock.monotonic_ms();
        let deadline_ms = request.timeout_seconds.saturating_mul(1_000);
        loop {
            let elapsed_ms = self.clock.monotonic_ms().saturating_sub(started_ms);
            // The deadline is enforced BEFORE polling: SYS-44b asks whether a
            // fill confirmation was received WITHIN the window, so once the
            // deadline has passed — including via a real clock's final sleep
            // overshooting under scheduler jitter — a fill first observed now
            // must NOT be accepted. (Accepting it would also defeat the
            // gate's over-deadline normalisation: second-truncation could
            // report a 30.4 s fill as elapsed_seconds == 30.)
            if elapsed_ms >= deadline_ms {
                // Guarantee: elapsed_ms >= timeout_seconds * 1000, so the
                // truncated division reports elapsed_seconds >=
                // timeout_seconds and the outcome always passes the gate's
                // outcome-consistency hardening.
                return Ok(KillSwitchLiquidationOutcome::TimedOutUnfilled {
                    elapsed_seconds: elapsed_ms / 1_000,
                    timeout_seconds: request.timeout_seconds,
                });
            }
            if self.poll_filled(request)? {
                return Ok(KillSwitchLiquidationOutcome::FilledBeforeTimeout {
                    // elapsed_ms < deadline_ms here, so the truncated
                    // division always reports an in-window
                    // elapsed_seconds <= timeout_seconds.
                    elapsed_seconds: elapsed_ms / 1_000,
                });
            }
            let remaining_ms = deadline_ms - elapsed_ms;
            self.clock.wait_ms(self.poll_interval_ms.min(remaining_ms));
        }
    }
}
