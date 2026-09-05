//! SRS-MD-005 / SyRS SYS-75 — the scheduled IB Gateway restart-window
//! connectivity producer.
//!
//! This is the thing that was missing. `ConnectivityState::ScheduledRestartWindow`
//! has existed since SRS-SAFE-003, the execution engine already refuses live
//! submissions on it, and the notification dispatcher already suppresses
//! connectivity alerts for it — `atp-notification`'s own docs say "the
//! restart-window *decision* is owned by SRS-MD-005; this enum is the seam the
//! dispatcher honours." Until now nothing made that decision: every
//! `impl BrokerageConnectivity` in the tree was a test fixture.
//!
//! [`ScheduledRestartConnectivity`] is the first real one. It composes three
//! things that are not allowed to see each other:
//!
//! ```text
//!   atp_types::RestartWindow        the schedule + the phase arithmetic (pure)
//!   GatewayReachability             the adapter's bounded TCP probe
//!   an injected Fn() -> i64         the instant, read at the outermost edge
//! ```
//!
//! It lives in `atp-orchestrator` because that is the only layer permitted to
//! bind the execution ports to an adapter (SRS-ARCH-002): `atp-execution` must
//! not depend on `atp-adapters`, so the composition has nowhere else to go.
//!
//! ## What it does NOT claim
//!
//! * It is not the general connectivity producer. An ordinary IB disconnect
//!   outside the restart window maps to `Unreachable` here too, but the
//!   detection loop that watches the gateway continuously — rather than
//!   sampling it when an order happens to be routed — is SRS-EXE-001's named
//!   scope. This type answers when asked.
//! * `request_reconnect` records the ATTEMPT and its NFR-R2 deadline; it does
//!   not itself re-establish a wire session. Re-opening the API session is the
//!   transport's job and is gated behind the operator-only live feature.
//! * A `Reachable` probe means the gateway accepts TCP, not that the API is
//!   ready — the gateway listens before it will answer a handshake. Folding
//!   readiness into `Connected` is ERR-9 / SRS-MD-006's scope, and claiming it
//!   here would be the false green those features exist to prevent.

use std::sync::Mutex;
use std::time::Duration;

use atp_adapters::gateway_reachability::{GatewayReachability, ReachabilityOutcome};
use atp_execution::BrokerageConnectivity;
use atp_types::{ConnectivityState, MarketDataAdmission, RestartPhase, RestartWindow};

/// NFR-R2: "Reconnection attempt within 15 seconds of detection."
///
/// The declared budget, published so an operator (and a later enforcement path)
/// reads one number rather than a literal in prose. It is a deadline for the
/// ATTEMPT, not for success — a gateway mid-restart will refuse for minutes,
/// and SYS-75 is precisely the rule that says not to page about it yet.
///
/// **Nothing here enforces it, and saying so is the point.** The ledger records
/// when each reconnect was requested and in which phase; it does not compare
/// those instants to this budget, because the thing being timed — re-opening
/// the IB API session — is the transport's job and sits behind the
/// operator-only live feature. Measuring detection-to-attempt latency belongs
/// with that wire-level reconnect, and both are recorded together in
/// `connectivity_contract.restart_window.deferred[]`. A constant that looked
/// enforced would be worse than an absent one.
pub const RECONNECT_ATTEMPT_BUDGET: Duration = Duration::from_secs(15);

/// How long an UNREACHABLE observation may be reused before the gateway is
/// asked again.
///
/// The execution engine consults this port INLINE on the live submission path,
/// so without reuse a burst of orders would each pay
/// `REACHABILITY_PROBE_TIMEOUT` against a black-holing endpoint and spend the
/// NFR-P1 order budget on the gate rather than on the order.
///
/// **Both outcomes are cached; the ASYMMETRY is the design.** A stale
/// `Unreachable` errs toward BLOCKING, which is the safe direction, so it may
/// be reused for this full second. A stale `Connected` errs toward handing an
/// order to a dead gateway, so it is capped at `REACHABLE_CACHE_TTL_NS`, two
/// orders of magnitude shorter. An earlier version of this constant cached the
/// negative ONLY and argued that a successful connect returns in microseconds
/// so there was nothing to protect. True about latency, and beside the point:
/// the gate is read once per submission, so a healthy order stream opened one
/// TCP connection per order against the gateway this module elsewhere refuses
/// to probe because it is scarce.
///
/// The phase is never cached either — that clock read is free, and the two
/// instants that matter (the start of the suspension and the end of the window)
/// must be exact.
///
/// The residual, stated: a gateway that comes back can be reported unreachable
/// for up to one second. Against a 300 s default window that is two orders of
/// magnitude smaller, and it is comfortably inside NFR-R2's 15 s
/// detection-to-attempt budget.
pub const REACHABILITY_CACHE_TTL_NS: i64 = 1_000_000_000;

/// How long a REACHABLE observation may be reused.
///
/// Two orders of magnitude shorter than the negative TTL, and the asymmetry is
/// still the point — but the first version of this design cached nothing on the
/// healthy path, and that was wrong in the other direction. `state()` is called
/// once per live submission, so a steady order rate against a healthy gateway
/// drove one connect/close per order into the IB Gateway API port: unbounded
/// churn against a resource this very module calls scarce when it refuses to
/// probe during the lead. Arguing the positive path purely on latency
/// ("a successful connect returns in microseconds") answered the wrong
/// question.
///
/// 100 ms bounds that to ten probes a second per producer while keeping the
/// stale-`Connected` window an order of magnitude below the NFR-P1 order budget
/// and four orders below the 300 s restart window. The residual, stated: for up
/// to 100 ms after the gateway dies, one order can still be routed rather than
/// refused with `CONNECTIVITY_BLOCKED` — it then fails at the broker instead of
/// at the gate. That is a real weakening of the gate's promptness, traded
/// knowingly against unbounded churn on the path that runs constantly.
pub const REACHABLE_CACHE_TTL_NS: i64 = 100_000_000;

// The asymmetry is the design, so it is enforced by the COMPILER rather than by
// the two constants happening to differ. A stale `Connected` is the dangerous
// direction: it must expire far sooner than a stale `Unreachable`, and the
// window it opens must stay well inside NFR-P1's 1,000 ms order budget.
const _: () = assert!(REACHABLE_CACHE_TTL_NS * 5 < REACHABILITY_CACHE_TTL_NS);
const _: () = assert!(REACHABLE_CACHE_TTL_NS * 4 <= 1_000_000_000);
const _: () = assert!(REACHABLE_CACHE_TTL_NS > 0);

/// One recorded reconnection attempt.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReconnectAttempt {
    /// When the engine asked for a reconnect.
    pub requested_at_ns: i64,
    /// The phase the window was in at that instant — the difference between
    /// "reconnecting after planned maintenance" and "reconnecting after an
    /// outage", which an operator reading the ledger needs.
    pub phase: RestartPhase,
}

/// How many reconnect attempts the ledger retains.
///
/// The ledger is written on EVERY blocked live submission, so a sustained
/// outage against a retrying strategy would grow it for the life of the
/// process — and this type documents itself as the production connectivity
/// producer. A bounded ring keeps the recent history an operator actually reads
/// while the total count, which is what the evidence asserts on, stays exact.
pub const RECONNECT_LEDGER_CAPACITY: usize = 256;

/// The reconnect ledger: what was asked for, and when.
///
/// `requested` is the true total and never saturates in practice (`u64`);
/// `attempts` is the bounded tail. Reporting a truncated COUNT would be the
/// worse trade: an operator asking "how long has this been retrying" needs the
/// number, and the individual instants past the most recent few are noise.
#[derive(Debug, Default)]
struct ReconnectLedger {
    attempts: std::collections::VecDeque<ReconnectAttempt>,
    requested: u64,
}

/// The SRS-MD-005 connectivity producer.
///
/// `Clock` is an injected `Fn() -> i64` returning epoch nanoseconds, matching
/// `atp_market_data::live_feed::LiveFeedLoop::step` rather than introducing
/// another clock trait. The single wall-clock read belongs at the outermost
/// edge, which keeps every decision here reproducible from its inputs.
#[derive(Debug)]
pub struct ScheduledRestartConnectivity<P, C>
where
    P: GatewayReachability,
    C: Fn() -> i64,
{
    window: RestartWindow,
    probe: P,
    clock: C,
    ttl_ns: i64,
    ledger: Mutex<ReconnectLedger>,
    /// The last observation and the instant it was taken.
    last_outcome: Mutex<Option<(i64, ReachabilityOutcome)>>,
}

impl<P, C> ScheduledRestartConnectivity<P, C>
where
    P: GatewayReachability,
    C: Fn() -> i64,
{
    /// Bind a validated window to a reachability probe and a clock.
    ///
    /// Takes a `RestartWindow` that is already constructed, so a malformed
    /// schedule fails at configuration load rather than at the first blocked
    /// order.
    pub fn new(window: RestartWindow, probe: P, clock: C) -> Self {
        Self {
            window,
            probe,
            clock,
            ttl_ns: REACHABILITY_CACHE_TTL_NS,
            ledger: Mutex::new(ReconnectLedger::default()),
            last_outcome: Mutex::new(None),
        }
    }

    /// Reuse a reachability observation for up to `ttl_ns` instead of the
    /// default.
    ///
    /// **UP TO.** A REACHABLE observation is additionally capped at
    /// [`REACHABLE_CACHE_TTL_NS`] (100 ms), so a caller passing 2 s gets 2 s for
    /// an unreachable gateway and 100 ms for a reachable one. The cap is not
    /// negotiable through this builder: a stale `Connected` is what hands a live
    /// order to a dead gateway, and no caller may raise that ceiling. Pass a
    /// SHORTER value and both directions shorten, because the cap is a `min`.
    ///
    /// Zero disables reuse in both directions - every read probes. Useful for a
    /// test that wants to count probes, and honest for a caller that would
    /// rather pay the deadline than tolerate any staleness.
    pub fn with_probe_ttl(mut self, ttl_ns: i64) -> Self {
        self.ttl_ns = ttl_ns.max(0);
        self
    }

    /// The window this producer enforces.
    pub fn window(&self) -> RestartWindow {
        self.window
    }

    /// The phase at the current instant.
    pub fn phase(&self) -> RestartPhase {
        self.window.phase((self.clock)())
    }

    /// Observe the gateway once, returning the full outcome rather than the
    /// collapsed boolean, so a caller that wants to log WHY can.
    ///
    /// Prefer [`observe_if_needed`](Self::observe_if_needed) for reporting: this
    /// one probes unconditionally, and during the SYS-75(a) lead that spends the
    /// gateway's single API-client slot on a question the phase already answers.
    pub fn observe(&self) -> ReachabilityOutcome {
        self.probe.probe()
    }

    /// Observe the gateway only when the current phase actually needs the
    /// answer; `None` means the phase decided without asking.
    ///
    /// The reporting counterpart of the probe-skip. An evidence path that used
    /// `observe()` would probe during the lead — exactly what
    /// `the_lead_suspends_without_spending_a_probe` asserts never happens — so
    /// the guarantee would hold in the gates and be broken by the tool that
    /// reports on them.
    pub fn observe_if_needed(&self) -> Option<ReachabilityOutcome> {
        let now_ns = (self.clock)();
        match self.window.phase(now_ns) {
            RestartPhase::Suspending => None,
            RestartPhase::Normal | RestartPhase::Restarting | RestartPhase::Elapsed => {
                Some(self.observe_cached(now_ns))
            }
        }
    }

    /// Whether market-data requests may be admitted right now, and the reason
    /// when they may not — the same decision the order gate takes, read
    /// through the same window.
    ///
    /// This is what the composition binds to `atp_market_data::RestartWindowGate`,
    /// so the two suspensions cannot disagree about the same instant.
    pub fn market_data_admission(&self) -> MarketDataAdmission {
        // ONE clock read, exactly as `state()` does. Sampling it again after the
        // probe would classify against an instant up to
        // REACHABILITY_PROBE_TIMEOUT later, so the two gates could put the same
        // wall-clock moment in different SYS-75 phases — which is the
        // disagreement this producer exists to make impossible.
        let now_ns = (self.clock)();
        match self.observe_for(self.window.phase(now_ns), now_ns) {
            Some(reachable) => self.window.market_data_admission(now_ns, reachable),
            None => MarketDataAdmission::SuspendedForScheduledRestart,
        }
    }

    /// The last reachability observation, while it is still within ITS reuse
    /// window; `None` once that window has passed.
    ///
    /// Both outcomes are retained, under the same asymmetric bounds the gate
    /// uses: [`REACHABLE_CACHE_TTL_NS`] for a positive, and the longer
    /// [`REACHABILITY_CACHE_TTL_NS`] for a negative. So `None` means "no
    /// observation current enough to report", NOT "the gateway is healthy" -
    /// an earlier version of this method cached negatives only and this
    /// rustdoc said `None` meant the gateway had answered again, which stopped
    /// being true one round before the text did. Ask
    /// [`observe_if_needed`](Self::observe_if_needed) for the current answer.
    ///
    /// It exists because the collapsed boolean loses the difference between
    /// "the gateway said no" and "we could not ask" — a `ProbeFailed` from
    /// exhausted descriptors or a denied permission becomes `Unreachable` and
    /// pages the operator about IB when the fault is local. `ConnectivityEvent`
    /// cannot carry the reason (its field set is pinned by the ERR-2 contract,
    /// which forbids transport detail), so the detail surfaces here and through
    /// the operator CLI's `reachability:` field instead. Recorded in
    /// `deferred[]`: routing it into the alert needs the envelope change that
    /// contract owns.
    pub fn last_outcome(&self) -> Option<ReachabilityOutcome> {
        let now_ns = (self.clock)();
        self.last_outcome
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .as_ref()
            .filter(|(taken_at_ns, outcome)| {
                // Enforce the freshness the doc promises. Dropping the instant
                // meant that after an outage at T-90s the `Suspending` phase
                // skips probing and this still returned an 89-second-old
                // observation to a caller told to expect one no older than the
                // TTL - a stale fact wearing a fresh label, which is the shape
                // of every other bug this feature has spent rounds on.
                //
                // The SAME per-outcome bound as `observe_cached`, not the
                // negative TTL for both. Once positives began to be cached, a
                // reporting surface filtered by `ttl_ns` alone would have let a
                // `Reachable` escape here for a full second - ten times the
                // bound this module installs as its safety property, through
                // the one method whose whole job is to describe that fact to an
                // operator.
                (0..Self::ttl_for(outcome, self.ttl_ns))
                    .contains(&now_ns.saturating_sub(*taken_at_ns))
            })
            .map(|(_, outcome)| outcome.clone())
    }

    /// Whether this phase needs a probe at all, and its answer if so.
    ///
    /// `None` means the phase decides on its own. Both gates route through this
    /// ONE helper deliberately: the probe-skip started life inside `state()`,
    /// and `market_data_admission` — added in the same feature — silently did
    /// not inherit it, so every subscription request in the lead paid a blocking
    /// TCP connect for an answer the phase already fixed. A new code path does
    /// not inherit the old one's guarantees; putting the decision in one place
    /// is what makes it impossible for a third gate to miss.
    fn observe_for(&self, phase: RestartPhase, now_ns: i64) -> Option<bool> {
        match phase {
            // SYS-75(a) is pre-emptive: the gateway is still up 60 s before its
            // own restart, so asking cannot change the answer — and the gateway
            // serves ONE API client, so asking spends the slot the reconnect is
            // waiting for.
            RestartPhase::Suspending => None,
            RestartPhase::Normal | RestartPhase::Restarting | RestartPhase::Elapsed => {
                Some(self.observe_cached(now_ns).is_reachable())
            }
        }
    }

    /// How long THIS outcome may be reused.
    ///
    /// One function, because the bound is the safety property and two copies of
    /// it drifted within a single round: `observe_cached` capped a positive at
    /// 100 ms while `last_outcome`, the method that reports the fact to an
    /// operator, still filtered on the 1 s negative TTL.
    ///
    /// `with_probe_ttl(0)` disables reuse entirely, in both directions, because
    /// the cap is a `min` rather than a constant.
    fn ttl_for(outcome: &ReachabilityOutcome, configured_ttl_ns: i64) -> i64 {
        if outcome.is_reachable() {
            configured_ttl_ns.min(REACHABLE_CACHE_TTL_NS)
        } else {
            configured_ttl_ns
        }
    }

    /// Probe, or reuse an observation younger than its per-outcome TTL.
    ///
    /// The cache is checked and refreshed under ONE lock hold, so two threads
    /// cannot both decide the entry is stale and both pay the deadline.
    fn observe_cached(&self, now_ns: i64) -> ReachabilityOutcome {
        let mut cached = self
            .last_outcome
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if let Some((taken_at_ns, outcome)) = cached.as_ref() {
            let age_ns = now_ns.saturating_sub(*taken_at_ns);
            let ttl_ns = Self::ttl_for(outcome, self.ttl_ns);
            // A NEGATIVE age means the clock stepped backwards, not that the
            // entry is fresh. (`saturating_sub` on i64 saturates at the type's
            // bounds — it does not clamp to zero — so the earlier comment
            // claiming otherwise was wrong, and it made `with_probe_ttl(0)`
            // reuse an entry it had promised never to.) Re-probe: a fresh fact
            // is the right answer when the clock is untrustworthy, and it costs
            // one probe rather than a stampede.
            if (0..ttl_ns).contains(&age_ns) {
                return outcome.clone();
            }
        }
        let outcome = self.probe.probe();
        // Both are stored; the TTL above is what differs. A positive is held
        // for REACHABLE_CACHE_TTL_NS so the healthy order path does not open a
        // fresh connection per order, a negative for the full window so a
        // black-holing endpoint costs one probe deadline rather than one per
        // submission.
        *cached = Some((now_ns, outcome.clone()));
        outcome
    }

    /// The collapsed predicate, for callers that only need the decision.
    pub fn admits_market_data_requests(&self) -> bool {
        self.market_data_admission().is_admitted()
    }

    /// The most recent reconnection attempts, oldest first, capped at
    /// [`RECONNECT_LEDGER_CAPACITY`].
    pub fn reconnect_attempts(&self) -> Vec<ReconnectAttempt> {
        self.lock().attempts.iter().copied().collect()
    }

    /// How many reconnection attempts have been requested in total.
    ///
    /// The TOTAL, not the retained tail: an operator asking how long a gateway
    /// has been refusing needs the real number, and truncating it would
    /// under-report an outage — the wrong direction.
    pub fn reconnect_count(&self) -> u64 {
        self.lock().requested
    }

    /// How many attempts were dropped from the retained tail.
    pub fn reconnect_attempts_dropped(&self) -> u64 {
        let ledger = self.lock();
        ledger.requested - ledger.attempts.len() as u64
    }

    /// A poisoned mutex must not take down the order path: a panic while
    /// recording one attempt would otherwise make every later blocked
    /// submission panic too, turning a bookkeeping fault into an unusable
    /// engine. The ledger is append-only, so recovering the inner value cannot
    /// observe a torn write.
    fn lock(&self) -> std::sync::MutexGuard<'_, ReconnectLedger> {
        self.ledger
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

impl<P, C> BrokerageConnectivity for ScheduledRestartConnectivity<P, C>
where
    P: GatewayReachability,
    C: Fn() -> i64,
{
    /// The authoritative state, recomputed from the clock on every read over a
    /// reachability observation no older than its per-outcome TTL.
    ///
    /// The STATE itself is never cached, and the phase is always recomputed:
    /// the two instants that matter most here - the start of the suspension and
    /// the end of the window - are exactly when a cached state would still be
    /// reporting the previous answer. What may be reused is the sampled FACT,
    /// under `REACHABLE_CACHE_TTL_NS` (100 ms) for a positive and
    /// `REACHABILITY_CACHE_TTL_NS` (1 s) for a negative. This rustdoc said
    /// "a fresh probe on every read" for one round after that stopped being
    /// true, on the trait method the ERR-2 order gate reads.
    fn state(&self) -> ConnectivityState {
        let now_ns = (self.clock)();
        match self.observe_for(self.window.phase(now_ns), now_ns) {
            Some(reachable) => self.window.connectivity_state(now_ns, reachable),
            None => ConnectivityState::ScheduledRestartWindow,
        }
    }

    fn request_reconnect(&self) {
        let now_ns = (self.clock)();
        let phase = self.window.phase(now_ns);
        let mut ledger = self.lock();
        ledger.requested = ledger.requested.saturating_add(1);
        if ledger.attempts.len() == RECONNECT_LEDGER_CAPACITY {
            ledger.attempts.pop_front();
        }
        ledger.attempts.push_back(ReconnectAttempt {
            requested_at_ns: now_ns,
            phase,
        });
    }
}

/// The market-data half of SYS-75(a), bound to the SAME producer as the order
/// half.
///
/// This impl is the whole reason the two suspensions cannot drift: the
/// subscription manager and the execution engine are handed one object, so
/// there is one window, one clock and one probe behind both answers. Two
/// separately-configured gates over one requirement is a defect waiting for a
/// deployment where only one of them was updated.
impl<P, C> atp_market_data::RestartWindowGate for ScheduledRestartConnectivity<P, C>
where
    P: GatewayReachability,
    C: Fn() -> i64,
{
    fn admission(&self) -> MarketDataAdmission {
        Self::market_data_admission(self)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;
    use std::sync::atomic::{AtomicI64, Ordering};

    use atp_types::{DEFAULT_RESTART_SUSPEND_LEAD_SECONDS, NANOS_PER_SECOND};

    /// 2026-09-04T03:45:00Z = 23:45 America/New_York on 2026-09-03 (EDT).
    const RESTART_NS: i64 = 1_788_493_500 * NANOS_PER_SECOND;
    const LEAD_NS: i64 = DEFAULT_RESTART_SUSPEND_LEAD_SECONDS * NANOS_PER_SECOND;
    const WINDOW_NS: i64 = 300 * NANOS_PER_SECOND;

    /// A probe with a fixed answer that counts how often it was asked.
    struct CountingProbe {
        reachable: bool,
        calls: Cell<u32>,
    }

    impl CountingProbe {
        fn new(reachable: bool) -> Self {
            Self {
                reachable,
                calls: Cell::new(0),
            }
        }
    }

    impl GatewayReachability for CountingProbe {
        fn probe(&self) -> ReachabilityOutcome {
            self.calls.set(self.calls.get() + 1);
            if self.reachable {
                ReachabilityOutcome::Reachable
            } else {
                ReachabilityOutcome::Unreachable {
                    detail: "test double".to_string(),
                }
            }
        }
    }

    fn window() -> RestartWindow {
        RestartWindow::with_defaults(RESTART_NS).expect("SYS-75 defaults are valid")
    }

    fn at(
        now_ns: i64,
        reachable: bool,
    ) -> ScheduledRestartConnectivity<CountingProbe, impl Fn() -> i64> {
        ScheduledRestartConnectivity::new(window(), CountingProbe::new(reachable), move || now_ns)
    }

    #[test]
    fn the_lead_suspends_without_spending_a_probe() {
        // SYS-75(a) is pre-emptive, so the answer cannot depend on the gateway
        // — and asking anyway would spend a socket the reconnect needs, on a
        // gateway that serves ONE API client.
        let producer = at(RESTART_NS - 30 * NANOS_PER_SECOND, true);
        assert_eq!(
            producer.state(),
            ConnectivityState::ScheduledRestartWindow,
            "the 60 s lead suspends even while the gateway still answers"
        );
        assert_eq!(
            producer.probe.calls.get(),
            0,
            "the lead must not probe: the answer cannot change the decision"
        );
    }

    #[test]
    fn the_market_data_gate_inherits_the_probe_skip() {
        // The probe-skip started life inside `state()` only, and this second
        // gate — added by the same feature — silently did not inherit it, so
        // every subscription request during the lead paid a blocking TCP
        // connect for an answer the phase already fixed. A new code path does
        // not inherit the old one's guarantees; both now route through one
        // helper, and this pins that they do.
        let producer = at(RESTART_NS - 30 * NANOS_PER_SECOND, true);
        assert_eq!(
            producer.market_data_admission(),
            MarketDataAdmission::SuspendedForScheduledRestart
        );
        assert_eq!(
            producer.probe.calls.get(),
            0,
            "the market-data gate must skip the probe during the lead, exactly as the \
             order gate does"
        );

        // Non-vacuity: outside the lead this gate DOES probe, so "0 calls" is a
        // fact about the lead rather than about a gate that never probes.
        let outside = at(RESTART_NS - LEAD_NS - 1, true);
        assert_eq!(
            outside.market_data_admission(),
            MarketDataAdmission::Admitted
        );
        assert_eq!(outside.probe.calls.get(), 1);
    }

    #[test]
    fn outside_the_window_the_probe_decides() {
        // The non-vacuity partner for the test above: here the probe IS
        // consulted, so "0 calls" during the lead is a fact about the lead
        // rather than about a producer that never probes at all.
        let up = at(RESTART_NS - LEAD_NS - 1, true);
        assert_eq!(up.state(), ConnectivityState::Connected);
        assert_eq!(up.probe.calls.get(), 1);

        let down = at(RESTART_NS - LEAD_NS - 1, false);
        assert_eq!(down.state(), ConnectivityState::Unreachable);
        assert_eq!(down.probe.calls.get(), 1);
    }

    #[test]
    fn a_gateway_that_returns_inside_the_window_reports_connected() {
        // SYS-75(c)/(d): reconnect once available, resume normal operations.
        let back = at(RESTART_NS + NANOS_PER_SECOND, true);
        assert_eq!(back.state(), ConnectivityState::Connected);

        let still_down = at(RESTART_NS + NANOS_PER_SECOND, false);
        assert_eq!(
            still_down.state(),
            ConnectivityState::ScheduledRestartWindow,
            "inside the window a dead gateway is planned maintenance, not an outage"
        );
    }

    #[test]
    fn a_gateway_still_dead_after_the_window_escalates() {
        // The SYS-75 escalation clause. `Unreachable` is what carries
        // scheduled_restart:false into ConnectivityEvent, which is what makes
        // the notification dispatcher page instead of suppress.
        let escalated = at(RESTART_NS + WINDOW_NS, false);
        assert_eq!(escalated.state(), ConnectivityState::Unreachable);

        // Both non-vacuity directions: one nanosecond earlier the same dead
        // gateway is still maintenance, and a live one after the window is
        // simply connected.
        assert_eq!(
            at(RESTART_NS + WINDOW_NS - 1, false).state(),
            ConnectivityState::ScheduledRestartWindow
        );
        assert_eq!(
            at(RESTART_NS + WINDOW_NS, true).state(),
            ConnectivityState::Connected
        );
    }

    #[test]
    fn a_reconnect_request_records_the_instant_and_the_phase() {
        // The ledger is what distinguishes "reconnecting after planned
        // maintenance" from "reconnecting after an outage" once an operator
        // reads it back. A count alone could not.
        let producer = at(RESTART_NS + NANOS_PER_SECOND, false);
        assert_eq!(producer.reconnect_count(), 0);

        producer.request_reconnect();
        producer.request_reconnect();

        let attempts = producer.reconnect_attempts();
        assert_eq!(attempts.len(), 2);
        assert_eq!(attempts[0].requested_at_ns, RESTART_NS + NANOS_PER_SECOND);
        assert_eq!(attempts[0].phase, RestartPhase::Restarting);

        // And the escalated phase is recorded distinctly, so the two are
        // told apart in the record rather than by the reader's memory.
        let after = at(RESTART_NS + WINDOW_NS, false);
        after.request_reconnect();
        assert_eq!(after.reconnect_attempts()[0].phase, RestartPhase::Elapsed);
    }

    #[test]
    fn the_state_is_recomputed_rather_than_cached_across_the_boundary() {
        // The two instants a cache would get wrong are exactly the ones that
        // matter: the start of the suspension and the end of the window. Drive
        // one producer across both with a moving clock.
        let now = AtomicI64::new(RESTART_NS - LEAD_NS - 1);
        let producer =
            ScheduledRestartConnectivity::new(window(), CountingProbe::new(false), || {
                now.load(Ordering::SeqCst)
            });

        assert_eq!(producer.state(), ConnectivityState::Unreachable);
        now.store(RESTART_NS - LEAD_NS, Ordering::SeqCst);
        assert_eq!(producer.state(), ConnectivityState::ScheduledRestartWindow);
        now.store(RESTART_NS + WINDOW_NS - 1, Ordering::SeqCst);
        assert_eq!(producer.state(), ConnectivityState::ScheduledRestartWindow);
        now.store(RESTART_NS + WINDOW_NS, Ordering::SeqCst);
        assert_eq!(
            producer.state(),
            ConnectivityState::Unreachable,
            "the escalation must be visible the instant the window closes"
        );
    }

    #[test]
    fn market_data_admission_reads_the_same_window_as_the_order_gate() {
        // Binding both suspensions to one decision is what stops them
        // drifting; this pins the agreement rather than restating the table.
        for (now_ns, reachable) in [
            (RESTART_NS - LEAD_NS - 1, true),
            (RESTART_NS - LEAD_NS - 1, false),
            (RESTART_NS - 1, true),
            (RESTART_NS + NANOS_PER_SECOND, true),
            (RESTART_NS + NANOS_PER_SECOND, false),
            (RESTART_NS + WINDOW_NS, false),
        ] {
            let producer = at(now_ns, reachable);
            let admits = producer.admits_market_data_requests();
            let connected = at(now_ns, reachable).state() == ConnectivityState::Connected;
            assert_eq!(
                admits, connected,
                "market-data admission disagreed with the order gate at \
                 now_ns={now_ns} reachable={reachable}"
            );
        }
    }

    #[test]
    fn consecutive_submissions_do_not_each_pay_the_probe_deadline() {
        // The NFR-P1 property. The execution engine consults this port INLINE on
        // the live submission path, so without reuse a burst of orders would
        // each spend REACHABILITY_PROBE_TIMEOUT against a black-holing endpoint
        // — a paused Gateway VM, a DROP rule, or the gateway mid-restart
        // holding the socket unaccepted, which are exactly the conditions this
        // feature exists for.
        let now = AtomicI64::new(RESTART_NS + WINDOW_NS);
        let producer =
            ScheduledRestartConnectivity::new(window(), CountingProbe::new(false), || {
                now.load(Ordering::SeqCst)
            });
        for _ in 0..25 {
            assert_eq!(producer.state(), ConnectivityState::Unreachable);
        }
        assert_eq!(
            producer.probe.calls.get(),
            1,
            "25 submissions inside the TTL must cost ONE probe, not 25"
        );

        // Past the TTL the gateway is asked again — the cache is a latency
        // bound, not a memory of a fact that stopped being true.
        now.store(
            RESTART_NS + WINDOW_NS + REACHABILITY_CACHE_TTL_NS,
            Ordering::SeqCst,
        );
        assert_eq!(producer.state(), ConnectivityState::Unreachable);
        assert_eq!(producer.probe.calls.get(), 2);
    }

    #[test]
    fn a_zero_ttl_probes_every_time_and_a_backwards_clock_re_probes() {
        // Non-vacuity for the test above: with reuse disabled the SAME sequence
        // costs one probe per read, so "1 probe" there is a fact about the cache
        // rather than about a producer that never probes.
        let now = AtomicI64::new(RESTART_NS + WINDOW_NS);
        let eager = ScheduledRestartConnectivity::new(window(), CountingProbe::new(false), || {
            now.load(Ordering::SeqCst)
        })
        .with_probe_ttl(0);
        for _ in 0..5 {
            let _ = eager.state();
        }
        assert_eq!(eager.probe.calls.get(), 5);

        // A clock that steps BACKWARDS yields a NEGATIVE age, which means the
        // clock is untrustworthy — not that the entry is fresh. Re-probe: a
        // fresh fact is the right answer, and it costs one probe. (An earlier
        // version claimed `saturating_sub` clamps to zero here; on i64 it
        // saturates at the type's bounds, so a negative age read as "inside any
        // TTL" and `with_probe_ttl(0)` reused an entry it had promised not to.)
        let cached = ScheduledRestartConnectivity::new(window(), CountingProbe::new(false), || {
            now.load(Ordering::SeqCst)
        });
        let _ = cached.state();
        now.store(RESTART_NS + WINDOW_NS - 5_000_000_000, Ordering::SeqCst);
        let _ = cached.state();
        assert_eq!(cached.probe.calls.get(), 2);
    }

    #[test]
    fn the_retained_outcome_expires_with_the_reuse_window() {
        // `last_outcome` promises an observation no older than the TTL. Dropping
        // the instant meant that after an outage at T-90s the Suspending phase
        // skips probing and this still returned an 89-second-old observation —
        // a stale fact wearing a fresh label, which is the shape of nearly
        // every bug this feature spent a round on.
        let now = AtomicI64::new(RESTART_NS - LEAD_NS - 1);
        let producer =
            ScheduledRestartConnectivity::new(window(), CountingProbe::new(false), || {
                now.load(Ordering::SeqCst)
            });
        assert_eq!(producer.state(), ConnectivityState::Unreachable);
        assert!(
            producer.last_outcome().is_some(),
            "fresh, so it is reported"
        );

        // Move into the lead, where nothing probes. The retained entry is now
        // older than the TTL and must not be handed back as current.
        now.store(RESTART_NS - 1, Ordering::SeqCst);
        assert_eq!(producer.state(), ConnectivityState::ScheduledRestartWindow);
        assert!(
            producer.last_outcome().is_none(),
            "an observation older than the reuse window is not an observation"
        );
    }

    #[test]
    fn with_probe_ttl_can_shorten_the_positive_cap_but_never_raise_it() {
        // The builder's rustdoc promised "reuse for `ttl_ns`" for a round after
        // `ttl_for` began capping a positive at 100 ms. Pin the real contract:
        // a caller asking for MORE gets the cap; a caller asking for LESS gets
        // what they asked for, in both directions.
        let now = AtomicI64::new(RESTART_NS + WINDOW_NS);
        let generous =
            ScheduledRestartConnectivity::new(window(), CountingProbe::new(true), || {
                now.load(Ordering::SeqCst)
            })
            .with_probe_ttl(2 * NANOS_PER_SECOND);

        assert_eq!(generous.state(), ConnectivityState::Connected);
        now.store(
            RESTART_NS + WINDOW_NS + REACHABLE_CACHE_TTL_NS,
            Ordering::SeqCst,
        );
        assert_eq!(
            generous.probe.calls.get(),
            1,
            "precondition: one probe so far"
        );
        let _ = generous.state();
        assert_eq!(
            generous.probe.calls.get(),
            2,
            "a 2 s request must not raise the 100 ms cap on a REACHABLE outcome"
        );

        // The same generous TTL does apply in full to a negative.
        let neg = ScheduledRestartConnectivity::new(window(), CountingProbe::new(false), || {
            now.load(Ordering::SeqCst)
        })
        .with_probe_ttl(2 * NANOS_PER_SECOND);
        assert_eq!(neg.state(), ConnectivityState::Unreachable);
        now.store(
            RESTART_NS + WINDOW_NS + REACHABLE_CACHE_TTL_NS + NANOS_PER_SECOND,
            Ordering::SeqCst,
        );
        let _ = neg.state();
        assert_eq!(
            neg.probe.calls.get(),
            1,
            "an UNREACHABLE outcome honours the full configured TTL"
        );

        // And shortening still shortens both.
        let strict = ScheduledRestartConnectivity::new(window(), CountingProbe::new(true), || {
            now.load(Ordering::SeqCst)
        })
        .with_probe_ttl(0);
        let _ = strict.state();
        let _ = strict.state();
        assert_eq!(strict.probe.calls.get(), 2, "ttl 0 must disable reuse");
    }

    #[test]
    fn the_reporting_surface_honours_the_same_bound_as_the_gate() {
        // `last_outcome()` describes the reachability fact to an operator. When
        // positives began to be cached it still filtered on the NEGATIVE TTL,
        // so a `Reachable` could escape here for a full second - ten times the
        // bound the module installs as its safety property, through the one
        // method whose job is to report that fact honestly. Both now route
        // through `ttl_for`.
        let now = AtomicI64::new(RESTART_NS + WINDOW_NS);
        let producer =
            ScheduledRestartConnectivity::new(window(), CountingProbe::new(true), || {
                now.load(Ordering::SeqCst)
            });
        assert_eq!(producer.state(), ConnectivityState::Connected);
        assert!(
            producer.last_outcome().is_some(),
            "the observation was just taken"
        );

        now.store(
            RESTART_NS + WINDOW_NS + REACHABLE_CACHE_TTL_NS,
            Ordering::SeqCst,
        );
        assert!(
            producer.last_outcome().is_none(),
            "a reachable observation past its bound must not be reported as current"
        );

        // Non-vacuity: a NEGATIVE at the same age is still inside ITS bound, so
        // this test fails if `ttl_for` collapses to one TTL for both.
        let neg = ScheduledRestartConnectivity::new(window(), CountingProbe::new(false), || {
            now.load(Ordering::SeqCst)
        });
        assert_eq!(neg.state(), ConnectivityState::Unreachable);
        now.store(
            RESTART_NS + WINDOW_NS + REACHABLE_CACHE_TTL_NS * 2,
            Ordering::SeqCst,
        );
        assert!(
            neg.last_outcome().is_some(),
            "an unreachable observation is still within the 1 s negative TTL"
        );
    }

    #[test]
    fn a_reachable_outcome_is_reused_only_briefly_and_the_bound_is_what_protects_the_gate() {
        // This replaces an earlier `a_reachable_outcome_is_never_reused`, which
        // pinned a design that was wrong in the other direction: caching
        // nothing on the healthy path meant `state()` — called once per live
        // submission — opened and dropped a fresh connection per order against
        // a gateway this module elsewhere calls a scarce resource.
        //
        // The safety property is no longer "never reused" but "not reused for
        // long", and the BOUND is what now carries it, so both halves are
        // pinned: reuse inside the window, and a re-probe just past it.
        struct FlippingProbe {
            reachable: Cell<bool>,
            calls: Cell<u32>,
        }
        impl GatewayReachability for FlippingProbe {
            fn probe(&self) -> ReachabilityOutcome {
                self.calls.set(self.calls.get() + 1);
                if self.reachable.get() {
                    ReachabilityOutcome::Reachable
                } else {
                    ReachabilityOutcome::Unreachable {
                        detail: "died".to_string(),
                    }
                }
            }
        }
        let now = AtomicI64::new(RESTART_NS + WINDOW_NS);
        let producer = ScheduledRestartConnectivity::new(
            window(),
            FlippingProbe {
                reachable: Cell::new(true),
                calls: Cell::new(0),
            },
            || now.load(Ordering::SeqCst),
        );

        // A burst of healthy submissions costs ONE probe, not one each.
        for _ in 0..20 {
            assert_eq!(producer.state(), ConnectivityState::Connected);
        }
        assert_eq!(
            producer.probe.calls.get(),
            1,
            "20 healthy orders must not open 20 connections to the gateway"
        );

        // The gateway dies. Inside the short positive window the gate is still
        // stale — that residual is real and documented.
        producer.probe.reachable.set(false);
        now.store(
            RESTART_NS + WINDOW_NS + REACHABLE_CACHE_TTL_NS - 1,
            Ordering::SeqCst,
        );
        assert_eq!(producer.state(), ConnectivityState::Connected);

        // One nanosecond past the bound it re-probes and refuses. THIS is the
        // assertion that keeps the churn fix from becoming a safety hole.
        now.store(
            RESTART_NS + WINDOW_NS + REACHABLE_CACHE_TTL_NS,
            Ordering::SeqCst,
        );
        assert_eq!(
            producer.state(),
            ConnectivityState::Unreachable,
            "a reachable observation must expire quickly, or the gate routes \
             live orders to a gateway that has already died"
        );
    }

    #[test]
    fn the_phase_is_never_cached_even_though_reachability_is() {
        // The two instants a cache would get wrong are exactly the ones that
        // matter. Only reachability is reused; the phase is recomputed from the
        // clock on every read, so the escalation boundary stays exact.
        let now = AtomicI64::new(RESTART_NS - LEAD_NS - 1);
        let producer =
            ScheduledRestartConnectivity::new(window(), CountingProbe::new(true), || {
                now.load(Ordering::SeqCst)
            });
        assert_eq!(producer.state(), ConnectivityState::Connected);
        // One nanosecond later — well inside the reachability TTL — the phase
        // has moved and the verdict must move with it.
        now.store(RESTART_NS - LEAD_NS, Ordering::SeqCst);
        assert_eq!(
            producer.state(),
            ConnectivityState::ScheduledRestartWindow,
            "a cached REACHABILITY must never hold a stale PHASE"
        );
    }

    #[test]
    fn the_reconnect_ledger_is_bounded_but_the_count_is_exact() {
        // The ledger is written on EVERY blocked live submission, so a
        // sustained outage against a retrying strategy would grow it for the
        // life of the process. Truncating the COUNT instead would under-report
        // an outage, which is the wrong direction — an operator asking how long
        // a gateway has been refusing needs the real number.
        let producer = at(RESTART_NS + WINDOW_NS, false);
        let total = RECONNECT_LEDGER_CAPACITY + 17;
        for _ in 0..total {
            producer.request_reconnect();
        }
        assert_eq!(
            producer.reconnect_count(),
            total as u64,
            "the total must be exact"
        );
        assert_eq!(
            producer.reconnect_attempts().len(),
            RECONNECT_LEDGER_CAPACITY,
            "the retained tail must stay bounded"
        );
        assert_eq!(producer.reconnect_attempts_dropped(), 17);

        // Non-vacuity: below the cap nothing is dropped, so the bound is a fact
        // about the cap rather than about a ledger that always truncates.
        let small = at(RESTART_NS + WINDOW_NS, false);
        small.request_reconnect();
        assert_eq!(small.reconnect_attempts().len(), 1);
        assert_eq!(small.reconnect_attempts_dropped(), 0);
    }

    #[test]
    fn a_local_probe_failure_stays_distinguishable_from_an_outage() {
        // Collapsing the outcome to a bool loses the difference between "the
        // gateway said no" and "we could not ask". A ProbeFailed from a local
        // resource limit still fails closed — which is right — but the operator
        // must be able to see it was OUR fault, not IB's.
        struct FailingProbe;
        impl GatewayReachability for FailingProbe {
            fn probe(&self) -> ReachabilityOutcome {
                ReachabilityOutcome::ProbeFailed {
                    detail: "no file descriptors".to_string(),
                }
            }
        }
        let producer =
            ScheduledRestartConnectivity::new(window(), FailingProbe, || RESTART_NS + WINDOW_NS);
        assert_eq!(
            producer.state(),
            ConnectivityState::Unreachable,
            "a probe we could not run must still fail closed"
        );
        let outcome = producer.last_outcome().expect("the outcome is retained");
        assert_eq!(outcome.as_str(), "PROBE_FAILED");
        assert!(
            outcome.detail().contains("file descriptors"),
            "the reason must survive for the operator; got {outcome:?}"
        );
    }

    #[test]
    fn the_nfr_r2_attempt_budget_is_fifteen_seconds() {
        assert_eq!(RECONNECT_ATTEMPT_BUDGET, Duration::from_secs(15));
    }
}
