//! `SRS-RESV-006` / SyRS SYS-49e at the **EXECUTION** handoff.
//!
//! Adversarial review round 2 (`cooldown-execution-bypass`): SRS-RESV-006 gated the
//! two SRS-RESV-003 trigger entry points, but those only MINT a proposal. Nothing in
//! the type graph requires a swap to have come from one — `HotSwapDemotionRequest` is
//! freely constructible and the CLI builds one straight from argv — so a suppressed
//! evaluation constrained nothing at all. `execute_hot_swap`, the CLI's `swap`
//! subcommand and `POST /api/v1/hot-swap` all reached the demotion untouched.
//!
//! SYS-49e says that during the window no automatic trigger *"shall be **acted
//! upon**"*. These cases are what makes "acted upon" mean acted upon:
//!
//!   * a refused swap touches **no demotion-side port**. Every one of them is wired
//!     to `panic!` here, which is the strongest available form of "was never
//!     called" — a `Vec::is_empty()` assertion would pass on a gate that called
//!     them and discarded the results;
//!   * the same call with `Acknowledged` **fires**, because SYS-49a(a) requires a
//!     manual swap to stay available during a window. A gate that refused both
//!     would satisfy every suppression assertion above and be wrong;
//!   * a SUCCESSFUL swap **records the completion**, and its timestamp is the
//!     window start (SYS-49e's third clause, verbatim);
//!   * a REFUSED swap records **nothing** — a demotion that ends without a
//!     promotion is a failed changeover, not a swap, and a window opened there
//!     would suppress the automatic triggers for seven days over something that
//!     never happened.

use atp_execution::designation::{LiveDesignation, LiveDesignationConfirmation};
use atp_orchestrator::cooldown::{
    CooldownPeriodDays, CooldownState, ManualCooldownAcknowledgement, SwapCompletion,
};
use atp_orchestrator::demotion_pending_store::{DemotionPendingRecord, DemotionPendingState};
use atp_orchestrator::hot_swap_promotion::{
    CooldownControl, CooldownWindowOutcome, DemotionProof, HotSwapPromotionError,
    HotSwapPromotionEvent, HotSwapPromotionEventSink, LivePositionProbe, OpenPosition,
    PaperHistoryFingerprint, PaperHistorySource, PromotionPorts, SwapCompletionSink,
};
use atp_orchestrator::{
    DemotionPendingLock, DeployedVersionRegistry, DeployedVersionRegistryError,
    HotSwapDemotionEventSink, HotSwapLiquidationProbe, HotSwapSideEffectError, OperatorAlertSink,
    StrategyOrchestrator, UnfilledOrderCanceller,
};
use atp_types::{
    DeployedVersion, HotSwapDemotionEvent, HotSwapDemotionOutcome, HotSwapDemotionRequest,
    OperatorAlertEvent, SourceHash, StrategyId,
};
use std::cell::RefCell;

const COMPLETED_AT: u64 = 1_715_000_000;
const SEVEN_DAYS: u64 = 7 * 86_400;
const DEMOTING: &str = "live-alpha";
const CANDIDATE: &str = "paper-beta";
const HASH_A: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

// --------------------------------------------------------------------------- //
// Every demotion-side port, wired to PANIC
// --------------------------------------------------------------------------- //

struct ForbiddenProbe;
impl HotSwapLiquidationProbe for ForbiddenProbe {
    fn await_flat_or_timeout(&self, _request: &HotSwapDemotionRequest) -> HotSwapDemotionOutcome {
        panic!("the liquidation probe ran during a cool-down");
    }
}

struct ForbiddenCanceller;
impl UnfilledOrderCanceller for ForbiddenCanceller {
    fn cancel_unfilled_liquidation_orders(
        &self,
        _request: &HotSwapDemotionRequest,
    ) -> Result<(), HotSwapSideEffectError> {
        panic!("resting orders were cancelled during a cool-down");
    }
}

struct ForbiddenAlerts;
impl OperatorAlertSink for ForbiddenAlerts {
    fn dispatch(&self, _event: OperatorAlertEvent) -> Result<(), HotSwapSideEffectError> {
        panic!("an operator page went out during a cool-down");
    }
}

struct ForbiddenLock;
impl DemotionPendingLock for ForbiddenLock {
    fn state(&self) -> DemotionPendingState {
        panic!("the demotion lockout was consulted during a cool-down");
    }
    fn engage(&self, _record: DemotionPendingRecord) -> Result<(), HotSwapSideEffectError> {
        panic!("the demotion lockout was ENGAGED during a cool-down");
    }
    fn amend(&self, _record: DemotionPendingRecord) -> Result<(), HotSwapSideEffectError> {
        panic!("the demotion lockout was amended during a cool-down");
    }
}

// --------------------------------------------------------------------------- //
// Working stubs, for the arms that are supposed to proceed
// --------------------------------------------------------------------------- //

struct FlatProbe;
impl HotSwapLiquidationProbe for FlatProbe {
    fn await_flat_or_timeout(&self, _request: &HotSwapDemotionRequest) -> HotSwapDemotionOutcome {
        HotSwapDemotionOutcome::FlatBeforeTimeout { elapsed_seconds: 4 }
    }
}

struct Canceller;
impl UnfilledOrderCanceller for Canceller {
    fn cancel_unfilled_liquidation_orders(
        &self,
        _request: &HotSwapDemotionRequest,
    ) -> Result<(), HotSwapSideEffectError> {
        Ok(())
    }
}

struct Alerts;
impl OperatorAlertSink for Alerts {
    fn dispatch(&self, _event: OperatorAlertEvent) -> Result<(), HotSwapSideEffectError> {
        Ok(())
    }
}

struct ClearLock;
impl DemotionPendingLock for ClearLock {
    fn state(&self) -> DemotionPendingState {
        DemotionPendingState::Clear
    }
    fn engage(&self, _record: DemotionPendingRecord) -> Result<(), HotSwapSideEffectError> {
        Ok(())
    }
    fn amend(&self, _record: DemotionPendingRecord) -> Result<(), HotSwapSideEffectError> {
        Ok(())
    }
}

struct DemotionEvents;
impl HotSwapDemotionEventSink for DemotionEvents {
    fn record(&self, _event: HotSwapDemotionEvent) -> Result<(), HotSwapSideEffectError> {
        Ok(())
    }
}

struct FlatPositions;
impl LivePositionProbe for FlatPositions {
    fn open_positions(&self) -> Result<Vec<OpenPosition>, HotSwapSideEffectError> {
        Ok(Vec::new())
    }
}

struct StablePaper;
impl PaperHistorySource for StablePaper {
    fn fingerprint(
        &self,
        _strategy_id: &StrategyId,
    ) -> Result<Option<PaperHistoryFingerprint>, HotSwapSideEffectError> {
        Ok(Some(PaperHistoryFingerprint {
            equity_points: 12,
            trades: 3,
            digest: "digest-a".to_string(),
        }))
    }
}

struct StableVersions;
impl DeployedVersionRegistry for StableVersions {
    fn record(
        &self,
        _strategy_id: &StrategyId,
        _version: DeployedVersion,
    ) -> Result<(), DeployedVersionRegistryError> {
        panic!("the promotion gate must never WRITE a deployed version");
    }

    fn lookup(
        &self,
        _strategy_id: &StrategyId,
    ) -> Result<Option<DeployedVersion>, DeployedVersionRegistryError> {
        Ok(Some(DeployedVersion::new(
            SourceHash::new(HASH_A),
            1_700_000_000,
        )))
    }
}

#[derive(Default)]
struct PromotionEvents {
    recorded: RefCell<Vec<HotSwapPromotionEvent>>,
}
impl HotSwapPromotionEventSink for PromotionEvents {
    fn record(&self, event: HotSwapPromotionEvent) -> Result<(), HotSwapSideEffectError> {
        self.recorded.borrow_mut().push(event);
        Ok(())
    }
}

/// The SYS-49e writer, recording rather than persisting — and optionally FAILING,
/// which is the fail-open arm no surface may swallow.
struct Completions {
    recorded: RefCell<Vec<SwapCompletion>>,
    fail_with: Option<&'static str>,
    /// Whether the PRE-FLIGHT probe succeeds. Separate from `fail_with`, because the
    /// two model different worlds: a store that was unwritable all along (caught
    /// before the swap runs) versus one that becomes unwritable mid-swap (the
    /// residual race, which can only be reported).
    probe_fails_with: Option<&'static str>,
    /// The instant `completed_at_seconds()` reports. `None` = "same as the caller's
    /// observation instant", which is the DEGENERATE case a real clock never hits.
    completed_at: Option<u64>,
}

impl Completions {
    fn working() -> Self {
        Self {
            recorded: RefCell::new(Vec::new()),
            fail_with: None,
            probe_fails_with: None,
            completed_at: None,
        }
    }
    /// Writable at pre-flight, unwritable by the time the swap completes — the race.
    fn failing() -> Self {
        Self {
            recorded: RefCell::new(Vec::new()),
            fail_with: Some("the cool-down state file is read-only"),
            probe_fails_with: None,
            completed_at: None,
        }
    }
    /// Unwritable from the start — the case the pre-flight must catch.
    fn unwritable() -> Self {
        Self {
            recorded: RefCell::new(Vec::new()),
            fail_with: Some("the cool-down state file is read-only"),
            probe_fails_with: Some("cannot write hot-swap cool-down window: permission denied"),
            completed_at: None,
        }
    }

    /// A clock that has ADVANCED by the time the swap completes.
    fn with_completion_clock(completed_at: u64) -> Self {
        Self {
            recorded: RefCell::new(Vec::new()),
            fail_with: None,
            probe_fails_with: None,
            completed_at: Some(completed_at),
        }
    }
    fn count(&self) -> usize {
        self.recorded.borrow().len()
    }
}

impl SwapCompletionSink for Completions {
    fn probe_writable(&self) -> Result<(), String> {
        match self.probe_fails_with {
            Some(reason) => Err(reason.to_string()),
            None => Ok(()),
        }
    }

    fn completed_at_seconds(&self) -> Result<u64, String> {
        Ok(self.completed_at.unwrap_or(COMPLETED_AT))
    }

    fn record_swap_completion(&self, completion: &SwapCompletion) -> Result<(), String> {
        if let Some(reason) = self.fail_with {
            return Err(reason.to_string());
        }
        self.recorded.borrow_mut().push(completion.clone());
        Ok(())
    }
}

// --------------------------------------------------------------------------- //
// Fixtures
// --------------------------------------------------------------------------- //

fn request() -> HotSwapDemotionRequest {
    HotSwapDemotionRequest {
        demoting_strategy_id: StrategyId::new(DEMOTING),
        candidate_strategy_id: StrategyId::new(CANDIDATE),
        timeout_seconds: 60,
    }
}

fn confirmation(strategy: &str) -> LiveDesignationConfirmation {
    LiveDesignationConfirmation::from_operator(StrategyId::new(strategy), "test")
        .expect("a valid confirmation")
}

fn live_designation() -> LiveDesignation {
    let mut designation = LiveDesignation::new();
    designation
        .designate(StrategyId::new(DEMOTING), confirmation(DEMOTING))
        .expect("seeding the live designation must succeed");
    designation
}

fn active_window(now: u64) -> CooldownState {
    let state = CooldownState::classify(
        Some(&SwapCompletion {
            completed_at_seconds: COMPLETED_AT,
            demoted_strategy_id: StrategyId::new("older-a"),
            promoted_strategy_id: StrategyId::new("older-b"),
        }),
        CooldownPeriodDays::default(),
        now,
    );
    assert_eq!(state.as_str(), "ACTIVE", "fixture must be an ACTIVE window");
    state
}

/// A swap attempt whose demotion-side ports all PANIC — so reaching any of them is
/// a test failure rather than an assertion about a counter.
fn refused_swap(
    cooldown: &CooldownState,
    acknowledgement: ManualCooldownAcknowledgement,
    events: &PromotionEvents,
    completions: &Completions,
) -> Result<atp_orchestrator::hot_swap_promotion::HotSwapPromoted, HotSwapPromotionError> {
    let mut designation = live_designation();
    StrategyOrchestrator.execute_hot_swap(
        request(),
        &ForbiddenProbe,
        &ForbiddenCanceller,
        &ForbiddenAlerts,
        &DemotionEvents,
        &ForbiddenLock,
        PromotionPorts {
            positions: &FlatPositions,
            paper_history: &StablePaper,
            versions: &StableVersions,
            events,
        },
        CooldownControl {
            state: cooldown,
            acknowledgement,
            completions,
        },
        &mut designation,
        confirmation(CANDIDATE),
        COMPLETED_AT + 3_600,
    )
}

/// A swap attempt with every port WORKING, so it proceeds unless the cool-down stops it.
#[allow(clippy::type_complexity)]
fn live_swap(
    cooldown: &CooldownState,
    acknowledgement: ManualCooldownAcknowledgement,
    events: &PromotionEvents,
    completions: &Completions,
    now: u64,
) -> (
    Result<atp_orchestrator::hot_swap_promotion::HotSwapPromoted, HotSwapPromotionError>,
    Option<String>,
) {
    let mut designation = live_designation();
    let outcome = StrategyOrchestrator.execute_hot_swap(
        request(),
        &FlatProbe,
        &Canceller,
        &Alerts,
        &DemotionEvents,
        &ClearLock,
        PromotionPorts {
            positions: &FlatPositions,
            paper_history: &StablePaper,
            versions: &StableVersions,
            events,
        },
        CooldownControl {
            state: cooldown,
            acknowledgement,
            completions,
        },
        &mut designation,
        confirmation(CANDIDATE),
        now,
    );
    let designated = designation.designated().map(|id| id.as_str().to_string());
    (outcome, designated)
}

// --------------------------------------------------------------------------- //
// The gate: a window suppresses the EXECUTION, not merely the proposal
// --------------------------------------------------------------------------- //

#[test]
fn resv_6_an_active_window_refuses_the_swap_and_touches_no_demotion_side_port() {
    let events = PromotionEvents::default();
    let completions = Completions::working();
    let error = refused_swap(
        &active_window(COMPLETED_AT + 3_600),
        ManualCooldownAcknowledgement::NotAcknowledged,
        &events,
        &completions,
    )
    .expect_err("an ACTIVE window must refuse an unacknowledged swap");

    assert_eq!(
        error.machine_reason(),
        "HOT_SWAP_COOLDOWN_CONFIRMATION_REQUIRED"
    );
    // Nothing ran, so the demotion half is NOT_STARTED — not a timeout, which would
    // tell an operator to wait on a lockout that was never engaged.
    assert_eq!(error.demotion_outcome(), DemotionProof::NotStarted);
    assert_eq!(
        completions.count(),
        0,
        "a refused swap must not open a window"
    );

    // The refusal IS audited — a swap stopped before it began is a transition an
    // operator needs to see.
    let recorded = events.recorded.borrow();
    assert_eq!(recorded.len(), 1);
    assert!(!recorded[0].promoted);
    assert!(!recorded[0].flat_confirmed);
    assert!(!recorded[0].cooldown_window_started);
    assert_eq!(
        recorded[0].refusal,
        Some("HOT_SWAP_COOLDOWN_CONFIRMATION_REQUIRED")
    );
}

#[test]
fn resv_6_an_unknown_window_refuses_the_swap_too() {
    // "We could not tell" is never "no cool-down is in effect" (CLAUDE.md rule 3).
    // This is the arm an omitted --cooldown-state reaches.
    let events = PromotionEvents::default();
    let completions = Completions::working();
    let error = refused_swap(
        &CooldownState::unknown("no cool-down state path configured"),
        ManualCooldownAcknowledgement::NotAcknowledged,
        &events,
        &completions,
    )
    .expect_err("an UNKNOWN window must refuse, never proceed");

    assert_eq!(
        error.machine_reason(),
        "HOT_SWAP_COOLDOWN_CONFIRMATION_REQUIRED"
    );
    assert_eq!(completions.count(), 0);
}

#[test]
fn resv_6_the_warning_names_the_window_the_operator_would_be_overriding() {
    let events = PromotionEvents::default();
    let completions = Completions::working();
    let error = refused_swap(
        &active_window(COMPLETED_AT + 3_600),
        ManualCooldownAcknowledgement::NotAcknowledged,
        &events,
        &completions,
    )
    .expect_err("an ACTIVE window must refuse");

    let rendered = error.to_string();
    assert!(
        rendered.contains("ACTIVE"),
        "the refusal must name the window state: {rendered}"
    );
    assert!(
        rendered.contains("Nothing was demoted and nothing was promoted"),
        "the refusal must say what did NOT happen, or an operator cannot tell \
         whether to reconcile: {rendered}"
    );
}

// --------------------------------------------------------------------------- //
// The non-vacuity controls: the gate must let the right things THROUGH
// --------------------------------------------------------------------------- //

#[test]
fn resv_6_an_acknowledged_swap_during_a_window_fires() {
    // SYS-49a(a): a manual swap stays AVAILABLE during a cool-down; SYS-49e only
    // requires a confirmation warning. Without this case, a gate that refused every
    // swap unconditionally would pass every suppression assertion above.
    let events = PromotionEvents::default();
    let completions = Completions::working();
    let now = COMPLETED_AT + 3_600;
    let (outcome, designated) = live_swap(
        &active_window(now),
        ManualCooldownAcknowledgement::Acknowledged,
        &events,
        &completions,
        now,
    );

    let promoted = outcome.expect("an acknowledged swap must fire");
    assert_eq!(promoted.promoted_strategy_id.as_str(), CANDIDATE);
    assert_eq!(designated.as_deref(), Some(CANDIDATE));
    assert!(promoted.cooldown_window.started());
}

#[test]
fn resv_6_an_expired_window_needs_no_acknowledgement() {
    let events = PromotionEvents::default();
    let completions = Completions::working();
    let now = COMPLETED_AT + SEVEN_DAYS;
    let expired = CooldownState::classify(
        Some(&SwapCompletion {
            completed_at_seconds: COMPLETED_AT,
            demoted_strategy_id: StrategyId::new("older-a"),
            promoted_strategy_id: StrategyId::new("older-b"),
        }),
        CooldownPeriodDays::default(),
        now,
    );
    assert_eq!(expired.as_str(), "EXPIRED");

    let (outcome, designated) = live_swap(
        &expired,
        ManualCooldownAcknowledgement::NotAcknowledged,
        &events,
        &completions,
        now,
    );
    assert!(outcome.is_ok(), "an EXPIRED window must not suppress");
    assert_eq!(designated.as_deref(), Some(CANDIDATE));
}

#[test]
fn resv_6_a_first_ever_swap_is_not_suppressed() {
    let events = PromotionEvents::default();
    let completions = Completions::working();
    let now = COMPLETED_AT;
    let (outcome, _) = live_swap(
        &CooldownState::NeverSwapped,
        ManualCooldownAcknowledgement::NotAcknowledged,
        &events,
        &completions,
        now,
    );
    assert!(
        outcome.is_ok(),
        "NEVER_SWAPPED must not suppress, or the automatic triggers are dead on a \
         fresh install with nothing on any surface explaining why"
    );
}

// --------------------------------------------------------------------------- //
// The writer: SYS-49e's third clause
// --------------------------------------------------------------------------- //

#[test]
fn resv_6_a_successful_swap_starts_the_window_at_its_own_completion_timestamp() {
    // "The cool-down start time shall be the timestamp of the most recent successful
    // swap COMPLETION" — asserted on the value actually handed to the store, not on
    // a re-read that could agree with a wrong write.
    //
    // The completion instant is the SINK's, read after the promotion. Here it equals
    // the observation instant, which is the degenerate case; the case that matters
    // is the next test.
    let events = PromotionEvents::default();
    let completions = Completions::working();
    let (outcome, _) = live_swap(
        &CooldownState::NeverSwapped,
        ManualCooldownAcknowledgement::NotAcknowledged,
        &events,
        &completions,
        COMPLETED_AT,
    );
    let promoted = outcome.expect("this swap must succeed");

    assert_eq!(
        promoted.cooldown_window,
        CooldownWindowOutcome::Started {
            started_at_seconds: COMPLETED_AT
        }
    );
    let recorded = completions.recorded.borrow();
    assert_eq!(recorded.len(), 1, "exactly one window per swap");
    assert_eq!(recorded[0].completed_at_seconds, COMPLETED_AT);
    assert_eq!(recorded[0].demoted_strategy_id.as_str(), DEMOTING);
    assert_eq!(recorded[0].promoted_strategy_id.as_str(), CANDIDATE);

    let audit = events.recorded.borrow();
    assert!(audit[0].promoted);
    assert!(audit[0].cooldown_window_started);
}

#[test]
fn resv_6_the_window_starts_when_the_swap_completed_not_when_it_was_requested() {
    // Adversarial review r5 [high]. A swap is not instantaneous: `resolve_demotion`
    // alone may legitimately run for the whole SYS-49b liquidation timeout (60s by
    // default) before the promotion even begins. Stamping the window with the instant
    // the ATTEMPT STARTED shortens a seven-day safety window by the duration of the
    // swap, letting the automatic triggers resume that much early — the one direction
    // a cool-down must never move.
    //
    // The clock advances by a full SYS-49b timeout between the observation instant and
    // the completion, and the recorded window must follow the LATTER.
    const SWAP_TOOK: u64 = 60;
    let events = PromotionEvents::default();
    let completions = Completions::with_completion_clock(COMPLETED_AT + SWAP_TOOK);
    let (outcome, _) = live_swap(
        &CooldownState::NeverSwapped,
        ManualCooldownAcknowledgement::NotAcknowledged,
        &events,
        &completions,
        COMPLETED_AT, // the instant the attempt was OBSERVED
    );
    let promoted = outcome.expect("this swap must succeed");

    let recorded = completions.recorded.borrow();
    assert_eq!(
        recorded[0].completed_at_seconds,
        COMPLETED_AT + SWAP_TOOK,
        "the window must start when the swap COMPLETED, not when it was requested"
    );
    assert_eq!(
        promoted.cooldown_window,
        CooldownWindowOutcome::Started {
            started_at_seconds: COMPLETED_AT + SWAP_TOOK
        },
        "the reported start must agree with the recorded one"
    );

    // And the consequence the requirement is actually about: the window still has its
    // FULL seven days left at the moment the swap completed.
    let window = CooldownState::classify(
        Some(&recorded[0]),
        CooldownPeriodDays::default(),
        COMPLETED_AT + SWAP_TOOK,
    );
    match window {
        CooldownState::Active {
            remaining_seconds, ..
        } => assert_eq!(
            remaining_seconds, SEVEN_DAYS,
            "a swap that took {SWAP_TOOK}s must not have spent {SWAP_TOOK}s of its own \
             cool-down before it started"
        ),
        other => panic!("expected an ACTIVE window, got {other:?}"),
    }
}

#[test]
fn resv_6_a_completion_clock_that_cannot_be_read_does_not_fall_back_to_the_start() {
    // The fix must not reintroduce the bug as a fallback. An unreadable clock is an
    // unstarted window — loud — never a silent reuse of the observation instant.
    struct NoClock;
    impl SwapCompletionSink for NoClock {
        fn probe_writable(&self) -> Result<(), String> {
            Ok(())
        }
        fn completed_at_seconds(&self) -> Result<u64, String> {
            Err("system clock reports a time before the Unix epoch".to_string())
        }
        fn record_swap_completion(&self, _completion: &SwapCompletion) -> Result<(), String> {
            panic!("nothing may be recorded against a completion instant nobody could read");
        }
    }

    let events = PromotionEvents::default();
    let mut designation = live_designation();
    let outcome = StrategyOrchestrator.execute_hot_swap(
        request(),
        &FlatProbe,
        &Canceller,
        &Alerts,
        &DemotionEvents,
        &ClearLock,
        PromotionPorts {
            positions: &FlatPositions,
            paper_history: &StablePaper,
            versions: &StableVersions,
            events: &events,
        },
        CooldownControl {
            state: &CooldownState::NeverSwapped,
            acknowledgement: ManualCooldownAcknowledgement::NotAcknowledged,
            completions: &NoClock,
        },
        &mut designation,
        confirmation(CANDIDATE),
        COMPLETED_AT,
    );

    let promoted = outcome.expect("the swap itself still succeeded");
    match &promoted.cooldown_window {
        CooldownWindowOutcome::NotStarted { reason } => {
            assert!(reason.contains("Unix epoch"), "{reason}");
        }
        other => panic!("expected NotStarted, got {other:?}"),
    }
}

#[test]
fn resv_6_a_refused_swap_opens_no_window() {
    // A demotion that ends without a promotion is a FAILED CHANGEOVER, not a swap.
    // Opening a window there would suppress the automatic triggers for seven days
    // over something that never happened — and a repeatedly-failing swap would
    // disable them indefinitely.
    let events = PromotionEvents::default();
    let completions = Completions::working();
    let mut designation = live_designation();

    struct OpenPositions;
    impl LivePositionProbe for OpenPositions {
        fn open_positions(&self) -> Result<Vec<OpenPosition>, HotSwapSideEffectError> {
            Ok(vec![OpenPosition {
                symbol: "AAPL".to_string(),
                quantity: 100,
            }])
        }
    }

    let outcome = StrategyOrchestrator.execute_hot_swap(
        request(),
        &FlatProbe,
        &Canceller,
        &Alerts,
        &DemotionEvents,
        &ClearLock,
        PromotionPorts {
            positions: &OpenPositions,
            paper_history: &StablePaper,
            versions: &StableVersions,
            events: &events,
        },
        CooldownControl {
            state: &CooldownState::NeverSwapped,
            acknowledgement: ManualCooldownAcknowledgement::NotAcknowledged,
            completions: &completions,
        },
        &mut designation,
        confirmation(CANDIDATE),
        COMPLETED_AT,
    );

    assert_eq!(
        outcome
            .expect_err("open positions must refuse the promotion")
            .machine_reason(),
        "LIVE_POSITIONS_OPEN"
    );
    assert_eq!(
        completions.count(),
        0,
        "a swap that did not complete must not open a cool-down window"
    );
}

#[test]
fn resv_6_a_swap_is_refused_when_its_window_could_not_be_recorded() {
    // Adversarial review r4 [critical]. Reporting the fail-open loudly is not the
    // same as preventing it: once the swap has completed there is nothing left to
    // undo, so the only place the requirement can actually be GUARANTEED is before
    // anything runs. Every demotion-side port panics here, which is the strongest
    // available proof that the refusal happened first.
    let events = PromotionEvents::default();
    let completions = Completions::unwritable();
    let error = refused_swap(
        &CooldownState::NeverSwapped, // the window itself is CLEAR — only the write fails
        ManualCooldownAcknowledgement::NotAcknowledged,
        &events,
        &completions,
    )
    .expect_err("a swap whose window cannot be recorded must be refused before it runs");

    assert_eq!(error.machine_reason(), "HOT_SWAP_COOLDOWN_UNRECORDABLE");
    assert_eq!(error.demotion_outcome(), DemotionProof::NotStarted);
    assert!(
        error.to_string().contains("Nothing was demoted"),
        "the refusal must say what did NOT happen: {error}"
    );
    assert_eq!(completions.count(), 0);

    let recorded = events.recorded.borrow();
    assert_eq!(recorded.len(), 1);
    assert!(!recorded[0].promoted);
    assert!(!recorded[0].cooldown_window_started);
}

#[test]
fn resv_6_an_acknowledged_swap_is_still_refused_if_the_window_is_unrecordable() {
    // The acknowledgement waives the WARNING, not the requirement. An operator may
    // choose to swap during a window; nobody may choose to swap without one.
    let events = PromotionEvents::default();
    let completions = Completions::unwritable();
    let error = refused_swap(
        &active_window(COMPLETED_AT + 3_600),
        ManualCooldownAcknowledgement::Acknowledged,
        &events,
        &completions,
    )
    .expect_err("acknowledging a window does not make an unwritable store writable");
    assert_eq!(error.machine_reason(), "HOT_SWAP_COOLDOWN_UNRECORDABLE");
}

#[test]
fn resv_6_the_unwaivable_refusal_is_reported_before_the_waivable_one() {
    // Both refusals apply here: the window is ACTIVE and unacknowledged, AND the
    // store cannot be written. The operator must be told the one no acknowledgement
    // can move — otherwise they confirm, re-send, and only then hit the wall.
    //
    // Placement, not preference: `probe_writable` runs ahead of the confirmation
    // gate in `execute_hot_swap`, and this is what pins that order.
    let events = PromotionEvents::default();
    let completions = Completions::unwritable();
    let error = refused_swap(
        &active_window(COMPLETED_AT + 3_600),
        ManualCooldownAcknowledgement::NotAcknowledged,
        &events,
        &completions,
    )
    .expect_err("both refusals apply; one of them must win");

    assert_eq!(
        error.machine_reason(),
        "HOT_SWAP_COOLDOWN_UNRECORDABLE",
        "the UNWAIVABLE refusal must be reported ahead of the one an operator can \
         clear by confirming"
    );
}

#[test]
fn resv_6_a_window_that_fails_to_start_is_reported_not_swallowed() {
    // The fail-open. The swap HAPPENED (the designation moved and the book is flat),
    // so it cannot be rolled back over an unwritable file — reverting would leave the
    // demoted strategy live with an emptied book, which is strictly worse. It is made
    // LOUD instead, and the surfaces exit non-zero on it.
    let events = PromotionEvents::default();
    let completions = Completions::failing();
    let now = COMPLETED_AT;
    let (outcome, designated) = live_swap(
        &CooldownState::NeverSwapped,
        ManualCooldownAcknowledgement::NotAcknowledged,
        &events,
        &completions,
        now,
    );

    let promoted = outcome.expect("the swap itself still succeeded");
    // The promotion is NOT rolled back...
    assert_eq!(designated.as_deref(), Some(CANDIDATE));
    // ...and the failure is carried, with the reason, rather than being dropped.
    match &promoted.cooldown_window {
        CooldownWindowOutcome::NotStarted { reason } => {
            assert!(
                reason.contains("read-only"),
                "the reason the window did not start must survive: {reason}"
            );
        }
        other => panic!("expected NotStarted, got {other:?}"),
    }
    assert!(!promoted.cooldown_window.started());

    // The AUDIT record carries the fail-open: `promoted:true` with
    // `cooldown_window_started:false` is the pair that needs an operator.
    let audit = events.recorded.borrow();
    assert!(audit[0].promoted);
    assert!(
        !audit[0].cooldown_window_started,
        "an audit trail must not be silent about an unsuppressed trigger path"
    );
}

// --------------------------------------------------------------------------- //
// The predicate is SHARED with the trigger arms
// --------------------------------------------------------------------------- //

#[test]
fn resv_6_execution_and_the_trigger_arms_agree_on_every_window() {
    // One predicate — `proven_clear()` — drives all three arms, so this asserts the
    // execution gate never disagrees with the suppression the trigger layer applies.
    // A future edit that special-cases one window here reddens this immediately.
    let windows = [
        CooldownState::NeverSwapped,
        active_window(COMPLETED_AT + 3_600),
        CooldownState::classify(
            Some(&SwapCompletion {
                completed_at_seconds: COMPLETED_AT,
                demoted_strategy_id: StrategyId::new("older-a"),
                promoted_strategy_id: StrategyId::new("older-b"),
            }),
            CooldownPeriodDays::default(),
            COMPLETED_AT + SEVEN_DAYS,
        ),
        CooldownState::unknown("unreadable"),
    ];

    for window in windows {
        let events = PromotionEvents::default();
        let completions = Completions::working();
        let refuses_unacknowledged = if window.proven_clear() {
            let (outcome, _) = live_swap(
                &window,
                ManualCooldownAcknowledgement::NotAcknowledged,
                &events,
                &completions,
                COMPLETED_AT + 3_600,
            );
            outcome.is_err()
        } else {
            refused_swap(
                &window,
                ManualCooldownAcknowledgement::NotAcknowledged,
                &events,
                &completions,
            )
            .is_err()
        };

        assert_eq!(
            refuses_unacknowledged,
            !window.proven_clear(),
            "the execution gate must refuse exactly when the trigger arms suppress \
             (window {})",
            window.as_str()
        );
    }
}
