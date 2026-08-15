//! SRS-RESV-005 / SyRS SYS-49d — the Hot-Swap promotion gate.
//!
//! The requirement is an ordering constraint plus three post-conditions, so these
//! tests are organised the same way:
//!
//!   * **ordering** — a demotion that did not reach flat promotes NOTHING, and the
//!     live designation is byte-unchanged afterwards;
//!   * **clause 1 (flat start)** — open positions refuse, and an *unreadable*
//!     position probe refuses with a DIFFERENT reason (unprovable is not flat);
//!   * **clause 2 (paper history preserved)** — missing, unreadable, and drifted
//!     history each refuse, and drift rolls the designation back;
//!   * **clause 3 (same strategy code)** — missing and drifted deployed versions
//!     each refuse, and drift rolls the designation back;
//!   * **execution-time revalidation** — a third strategy holding the live slot
//!     refuses rather than being promoted over.
//!
//! Every refusal asserts the designation state as well as the error, because a
//! gate that refuses *after* briefly designating the candidate live has already
//! violated the requirement it is reporting on.

use atp_execution::designation::{LiveDesignation, LiveDesignationConfirmation};
use atp_orchestrator::cooldown::{CooldownState, ManualCooldownAcknowledgement, SwapCompletion};
use atp_orchestrator::demotion_pending_store::{DemotionPendingRecord, DemotionPendingState};
use atp_orchestrator::hot_swap_promotion::{
    CooldownControl, DemotionProof, HotSwapCooldownPort, HotSwapPromotionError,
    HotSwapPromotionEvent, HotSwapPromotionEventSink, LivePositionProbe, OpenPosition,
    PaperHistoryFingerprint, PaperHistorySource, PromotionPorts, SwapOrigin,
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

const OBSERVED_AT: u64 = 1_715_000_000;
const TIMEOUT_S: u64 = 60;
const DEMOTING: &str = "live-alpha";
const CANDIDATE: &str = "paper-beta";
const HASH_A: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const HASH_B: &str = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

// --------------------------------------------------------------------------- //
// Demotion-side stubs (the SRS-RESV-004 ports the gate composes)
// --------------------------------------------------------------------------- //

struct Probe(HotSwapDemotionOutcome);
impl HotSwapLiquidationProbe for Probe {
    fn await_flat_or_timeout(&self, _request: &HotSwapDemotionRequest) -> HotSwapDemotionOutcome {
        self.0
    }
}

fn flat() -> Probe {
    Probe(HotSwapDemotionOutcome::FlatBeforeTimeout { elapsed_seconds: 4 })
}

fn timed_out() -> Probe {
    Probe(HotSwapDemotionOutcome::TimedOutDemotionPending {
        elapsed_seconds: 61,
        timeout_seconds: TIMEOUT_S,
    })
}

/// Every demotion-side port, wired to PANIC. A stale demoting id must reach none of
/// them: `resolve_demotion` engages the durable lockout, cancels unfilled liquidation
/// orders and pages the operator on its timeout branch, so a guard that runs after it
/// is too late for exactly the effects that matter.
struct ForbiddenProbe;
impl HotSwapLiquidationProbe for ForbiddenProbe {
    fn await_flat_or_timeout(&self, _request: &HotSwapDemotionRequest) -> HotSwapDemotionOutcome {
        panic!("the liquidation probe ran for a strategy that is not live");
    }
}

struct ForbiddenCanceller;
impl UnfilledOrderCanceller for ForbiddenCanceller {
    fn cancel_unfilled_liquidation_orders(
        &self,
        _request: &HotSwapDemotionRequest,
    ) -> Result<(), HotSwapSideEffectError> {
        panic!("an unfilled-order cancel ran for a strategy that is not live");
    }
}

struct ForbiddenAlerts;
impl OperatorAlertSink for ForbiddenAlerts {
    fn dispatch(&self, _event: OperatorAlertEvent) -> Result<(), HotSwapSideEffectError> {
        panic!("an operator page went out for a strategy that is not live");
    }
}

struct ForbiddenLock;
impl DemotionPendingLock for ForbiddenLock {
    fn state(&self) -> DemotionPendingState {
        panic!("the demotion lockout was consulted for a strategy that is not live");
    }

    fn engage(&self, _record: DemotionPendingRecord) -> Result<(), HotSwapSideEffectError> {
        panic!("the demotion lockout was ENGAGED for a strategy that is not live");
    }

    fn amend(&self, _record: DemotionPendingRecord) -> Result<(), HotSwapSideEffectError> {
        panic!("the demotion lockout was amended for a strategy that is not live");
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

/// A CLEAR SRS-RESV-004 lockout: nothing unresolved, so `resolve_demotion` proceeds
/// to its probe. The BLOCKING behaviour is SRS-RESV-004's own gate and is covered by
/// its `resv_4_demotion_pending_store` suite; what matters here is that the promotion
/// path threads a real lock through and therefore inherits it.
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

// --------------------------------------------------------------------------- //
// Promotion-side stubs
// --------------------------------------------------------------------------- //

enum PositionAnswer {
    Flat,
    Open(Vec<OpenPosition>),
    Unreadable(&'static str),
}

struct Positions(PositionAnswer);
impl LivePositionProbe for Positions {
    fn open_positions(&self) -> Result<Vec<OpenPosition>, HotSwapSideEffectError> {
        match &self.0 {
            PositionAnswer::Flat => Ok(Vec::new()),
            PositionAnswer::Open(held) => Ok(held.clone()),
            PositionAnswer::Unreadable(reason) => Err(HotSwapSideEffectError::new(*reason)),
        }
    }
}

fn fingerprint(points: u64, trades: u64, digest: &str) -> PaperHistoryFingerprint {
    PaperHistoryFingerprint {
        equity_points: points,
        trades,
        digest: digest.to_string(),
    }
}

/// Answers a queue of results, one per call, so a test can make the SECOND read
/// (the post-condition re-read) differ from the first.
struct PaperHistory {
    answers: RefCell<Vec<Result<Option<PaperHistoryFingerprint>, &'static str>>>,
}

impl PaperHistory {
    fn stable() -> Self {
        Self::queued(vec![
            Ok(Some(fingerprint(12, 3, "digest-a"))),
            Ok(Some(fingerprint(12, 3, "digest-a"))),
        ])
    }

    fn queued(answers: Vec<Result<Option<PaperHistoryFingerprint>, &'static str>>) -> Self {
        Self {
            answers: RefCell::new(answers),
        }
    }
}

impl PaperHistorySource for PaperHistory {
    fn fingerprint(
        &self,
        _strategy_id: &StrategyId,
    ) -> Result<Option<PaperHistoryFingerprint>, HotSwapSideEffectError> {
        let mut answers = self.answers.borrow_mut();
        assert!(
            !answers.is_empty(),
            "the gate read the paper history more times than the test scripted"
        );
        match answers.remove(0) {
            Ok(value) => Ok(value),
            Err(reason) => Err(HotSwapSideEffectError::new(reason)),
        }
    }
}

/// Same queue discipline for the deployed-version registry.
struct Versions {
    answers: RefCell<Vec<Result<Option<DeployedVersion>, &'static str>>>,
}

impl Versions {
    fn stable() -> Self {
        Self::queued(vec![Ok(Some(version(HASH_A))), Ok(Some(version(HASH_A)))])
    }

    fn queued(answers: Vec<Result<Option<DeployedVersion>, &'static str>>) -> Self {
        Self {
            answers: RefCell::new(answers),
        }
    }
}

fn version(hash: &str) -> DeployedVersion {
    DeployedVersion::new(SourceHash::new(hash), 1_700_000_000)
}

impl DeployedVersionRegistry for Versions {
    fn record(
        &self,
        _strategy_id: &StrategyId,
        _version: DeployedVersion,
    ) -> Result<(), DeployedVersionRegistryError> {
        panic!("SRS-RESV-005: the promotion gate must never WRITE a deployed version");
    }

    fn lookup(
        &self,
        _strategy_id: &StrategyId,
    ) -> Result<Option<DeployedVersion>, DeployedVersionRegistryError> {
        let mut answers = self.answers.borrow_mut();
        assert!(
            !answers.is_empty(),
            "the gate read the deployed version more times than the test scripted"
        );
        match answers.remove(0) {
            Ok(value) => Ok(value),
            Err(reason) => Err(DeployedVersionRegistryError::new(reason)),
        }
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

// --------------------------------------------------------------------------- //
// Harness
// --------------------------------------------------------------------------- //

fn request() -> HotSwapDemotionRequest {
    HotSwapDemotionRequest {
        demoting_strategy_id: StrategyId::new(DEMOTING),
        candidate_strategy_id: StrategyId::new(CANDIDATE),
        timeout_seconds: TIMEOUT_S,
    }
}

fn confirmation(strategy: &str) -> LiveDesignationConfirmation {
    LiveDesignationConfirmation::from_operator(
        StrategyId::new(strategy),
        "operator confirmed the Hot-Swap promotion",
    )
    .expect("a non-empty acknowledgement yields a confirmation token")
}

/// A designation already holding `strategy` as the live one.
fn designation_holding(strategy: &str) -> LiveDesignation {
    let mut designation = LiveDesignation::new();
    designation
        .designate(StrategyId::new(strategy), confirmation(strategy))
        .expect("seeding the live designation must succeed");
    designation
}

/// SRS-RESV-006's window writer, recording rather than persisting.
///
/// These are SRS-RESV-005's tests: every case here runs with a PROVEN-CLEAR window
/// so the promotion gate's own behaviour is what they measure. The cool-down's own
/// refusals and its write ordering are pinned by
/// `crates/atp-orchestrator/tests/resv_6_cooldown_execution.rs`.
#[derive(Default)]
struct Completions {
    recorded: RefCell<Vec<SwapCompletion>>,
}

impl HotSwapCooldownPort for Completions {
    fn resolve_window(&self, _now_seconds: u64) -> CooldownState {
        // These are SRS-RESV-005's tests: a proven-clear window keeps what they
        // measure the PROMOTION gate. The cool-down's own refusals live in
        // `resv_6_cooldown_execution.rs`.
        CooldownState::NeverSwapped
    }

    fn probe_recordable(&self, _d: &StrategyId, _p: &StrategyId) -> Result<(), String> {
        Ok(())
    }

    fn completed_at_seconds(&self) -> Result<u64, String> {
        Ok(OBSERVED_AT)
    }

    fn begin_provisional_window(
        &self,
        _completion: &SwapCompletion,
        _attempt_id: &str,
    ) -> Result<(), String> {
        Ok(())
    }

    fn confirm_window(&self, completion: &SwapCompletion) -> Result<(), String> {
        self.recorded.borrow_mut().push(completion.clone());
        Ok(())
    }

    fn abandon_provisional_window(&self, _completion: &SwapCompletion, _attempt_id: &str) {}
}

/// A window no swap has ever opened, plus a sink — the "nothing is in effect" case.
fn clear_cooldown(store: &Completions) -> CooldownControl<'_, Completions> {
    CooldownControl {
        origin: SwapOrigin::Manual(ManualCooldownAcknowledgement::NotAcknowledged),
        store,
    }
}

struct Outcome {
    result: Result<atp_orchestrator::hot_swap_promotion::CompletedHotSwap, HotSwapPromotionError>,
    designated_after: Option<String>,
    events: Vec<HotSwapPromotionEvent>,
}

#[allow(clippy::too_many_arguments)]
fn run_swap(
    probe: Probe,
    positions: Positions,
    paper: PaperHistory,
    versions: Versions,
    designation: &mut LiveDesignation,
) -> Outcome {
    let orchestrator = StrategyOrchestrator;
    let events = PromotionEvents::default();
    let completions = Completions::default();
    let result = orchestrator.execute_hot_swap(
        request(),
        &probe,
        &Canceller,
        &Alerts,
        &DemotionEvents,
        &ClearLock,
        PromotionPorts {
            positions: &positions,
            paper_history: &paper,
            versions: &versions,
            events: &events,
        },
        clear_cooldown(&completions),
        designation,
        confirmation(CANDIDATE),
        OBSERVED_AT,
    );
    Outcome {
        designated_after: designation.designated().map(|id| id.as_str().to_string()),
        // `HotSwapPromoted` is opaque: reading what the swap did REQUIRES redeeming
        // its SYS-49e window, which is the guarantee r8 asked for.
        result: result.map(|promoted| promoted.into_completed(&completions)),
        events: events.recorded.into_inner(),
    }
}

fn refusal(outcome: &Outcome) -> &HotSwapPromotionError {
    outcome
        .result
        .as_ref()
        .expect_err("this swap must be refused")
}

// --------------------------------------------------------------------------- //
// Ordering — the requirement itself
// --------------------------------------------------------------------------- //

#[test]
fn a_timed_out_demotion_promotes_nothing() {
    let mut designation = designation_holding(DEMOTING);
    let outcome = run_swap(
        timed_out(),
        Positions(PositionAnswer::Flat),
        PaperHistory::stable(),
        Versions::stable(),
        &mut designation,
    );

    assert_eq!(refusal(&outcome).machine_reason(), "DEMOTION_REFUSED");
    // The live slot is untouched: the demoting strategy is STILL live. A gate
    // that released it on a failed demotion would leave the account unattended.
    assert_eq!(outcome.designated_after.as_deref(), Some(DEMOTING));
    assert_eq!(
        refusal(&outcome).demotion_outcome(),
        DemotionProof::TimedOut
    );
}

#[test]
fn a_flat_demotion_promotes_the_candidate() {
    let mut designation = designation_holding(DEMOTING);
    let outcome = run_swap(
        flat(),
        Positions(PositionAnswer::Flat),
        PaperHistory::stable(),
        Versions::stable(),
        &mut designation,
    );

    let promoted = outcome.result.as_ref().expect("a flat demotion promotes");
    assert_eq!(promoted.promoted_strategy_id.as_str(), CANDIDATE);
    assert_eq!(promoted.demoted_strategy_id.as_str(), DEMOTING);
    assert!(promoted.flat_confirmed);
    // Exactly one strategy is live, and it is the candidate (SYS-2a).
    assert_eq!(outcome.designated_after.as_deref(), Some(CANDIDATE));
}

#[test]
fn the_promotion_audit_record_is_written_on_both_outcomes() {
    for (probe, expect_promoted) in [(flat(), true), (timed_out(), false)] {
        let mut designation = designation_holding(DEMOTING);
        let outcome = run_swap(
            probe,
            Positions(PositionAnswer::Flat),
            PaperHistory::stable(),
            Versions::stable(),
            &mut designation,
        );
        assert_eq!(outcome.events.len(), 1, "exactly one promotion record");
        let event = &outcome.events[0];
        assert_eq!(event.promoted, expect_promoted);
        assert_eq!(event.refusal.is_none(), expect_promoted);
        assert_eq!(event.candidate_strategy_id.as_str(), CANDIDATE);
        assert_eq!(event.observed_at_seconds, OBSERVED_AT);
    }
}

// --------------------------------------------------------------------------- //
// AC clause 1 — starts live with no open IB positions
// --------------------------------------------------------------------------- //

#[test]
fn open_positions_refuse_and_never_designate_the_candidate() {
    let mut designation = designation_holding(DEMOTING);
    let outcome = run_swap(
        flat(),
        Positions(PositionAnswer::Open(vec![OpenPosition {
            symbol: "AAPL".to_string(),
            quantity: -100,
        }])),
        PaperHistory::queued(vec![Ok(Some(fingerprint(12, 3, "digest-a")))]),
        Versions::queued(vec![Ok(Some(version(HASH_A)))]),
        &mut designation,
    );

    match refusal(&outcome) {
        HotSwapPromotionError::PositionsOpen { symbols } => {
            assert_eq!(symbols, &vec!["AAPL".to_string()]);
        }
        other => panic!("expected PositionsOpen, got {other:?}"),
    }
    assert_eq!(outcome.designated_after.as_deref(), Some(DEMOTING));
}

#[test]
fn an_unreadable_position_probe_is_not_a_flat_account() {
    let mut designation = designation_holding(DEMOTING);
    let outcome = run_swap(
        flat(),
        Positions(PositionAnswer::Unreadable("IB position feed unreachable")),
        PaperHistory::queued(vec![Ok(Some(fingerprint(12, 3, "digest-a")))]),
        Versions::queued(vec![Ok(Some(version(HASH_A)))]),
        &mut designation,
    );

    // The distinct reason is the point: "we could not check" must not be
    // reported, or handled, as "there is nothing there".
    assert_eq!(
        refusal(&outcome).machine_reason(),
        "LIVE_POSITIONS_UNPROVABLE"
    );
    assert_ne!(refusal(&outcome).machine_reason(), "LIVE_POSITIONS_OPEN");
    assert_eq!(outcome.designated_after.as_deref(), Some(DEMOTING));
}

#[test]
fn a_zero_quantity_position_is_flat() {
    let mut designation = designation_holding(DEMOTING);
    let outcome = run_swap(
        flat(),
        Positions(PositionAnswer::Open(vec![OpenPosition {
            symbol: "AAPL".to_string(),
            quantity: 0,
        }])),
        PaperHistory::stable(),
        Versions::stable(),
        &mut designation,
    );

    outcome
        .result
        .as_ref()
        .expect("a zero quantity is not an open position");
    assert_eq!(outcome.designated_after.as_deref(), Some(CANDIDATE));
}

// --------------------------------------------------------------------------- //
// AC clause 2 — preserves prior paper performance history
// --------------------------------------------------------------------------- //

#[test]
fn a_missing_paper_history_refuses_rather_than_reading_as_preserved() {
    let mut designation = designation_holding(DEMOTING);
    let outcome = run_swap(
        flat(),
        Positions(PositionAnswer::Flat),
        PaperHistory::queued(vec![Ok(None)]),
        Versions::queued(vec![]),
        &mut designation,
    );

    assert_eq!(refusal(&outcome).machine_reason(), "PAPER_HISTORY_MISSING");
    assert_eq!(outcome.designated_after.as_deref(), Some(DEMOTING));
}

#[test]
fn an_unreadable_paper_history_refuses_distinctly_from_a_missing_one() {
    let mut designation = designation_holding(DEMOTING);
    let outcome = run_swap(
        flat(),
        Positions(PositionAnswer::Flat),
        PaperHistory::queued(vec![Err("paper state store unreadable")]),
        Versions::queued(vec![]),
        &mut designation,
    );

    assert_eq!(
        refusal(&outcome).machine_reason(),
        "PAPER_HISTORY_UNREADABLE"
    );
    assert_eq!(outcome.designated_after.as_deref(), Some(DEMOTING));
}

#[test]
fn paper_history_drift_rolls_the_designation_back() {
    let mut designation = designation_holding(DEMOTING);
    let outcome = run_swap(
        flat(),
        Positions(PositionAnswer::Flat),
        // The re-read after the designation write disagrees with the capture.
        PaperHistory::queued(vec![
            Ok(Some(fingerprint(12, 3, "digest-a"))),
            Ok(Some(fingerprint(12, 3, "digest-REWRITTEN"))),
        ]),
        Versions::queued(vec![Ok(Some(version(HASH_A)))]),
        &mut designation,
    );

    assert_eq!(refusal(&outcome).machine_reason(), "PAPER_HISTORY_DRIFT");
    // Rolled back: the candidate must NOT be left live after a refused promotion.
    assert_eq!(outcome.designated_after, None);
}

// --------------------------------------------------------------------------- //
// AC clause 3 — same strategy code / API behavior
// --------------------------------------------------------------------------- //

#[test]
fn a_missing_deployed_version_refuses() {
    let mut designation = designation_holding(DEMOTING);
    let outcome = run_swap(
        flat(),
        Positions(PositionAnswer::Flat),
        PaperHistory::queued(vec![Ok(Some(fingerprint(12, 3, "digest-a")))]),
        Versions::queued(vec![Ok(None)]),
        &mut designation,
    );

    assert_eq!(refusal(&outcome).machine_reason(), "CODE_IDENTITY_MISSING");
    assert_eq!(outcome.designated_after.as_deref(), Some(DEMOTING));
}

#[test]
fn code_identity_drift_rolls_the_designation_back() {
    let mut designation = designation_holding(DEMOTING);
    let outcome = run_swap(
        flat(),
        Positions(PositionAnswer::Flat),
        PaperHistory::stable(),
        // The artifact changed between capture and re-read: the promoted
        // strategy would not be running the code it ran as paper.
        Versions::queued(vec![Ok(Some(version(HASH_A))), Ok(Some(version(HASH_B)))]),
        &mut designation,
    );

    assert_eq!(refusal(&outcome).machine_reason(), "CODE_IDENTITY_DRIFT");
    assert_eq!(outcome.designated_after, None);
}

// --------------------------------------------------------------------------- //
// Execution-time revalidation of the live strategy
// --------------------------------------------------------------------------- //

#[test]
fn a_third_live_strategy_is_never_promoted_over() {
    // SRS-RESV-003's contract states a trigger proposal records a REQUESTED
    // demoting id, not a verified one — so the slot is revalidated here.
    let mut designation = designation_holding("live-gamma");
    let outcome = run_swap(
        flat(),
        Positions(PositionAnswer::Flat),
        PaperHistory::queued(vec![Ok(Some(fingerprint(12, 3, "digest-a")))]),
        Versions::queued(vec![Ok(Some(version(HASH_A)))]),
        &mut designation,
    );

    match refusal(&outcome) {
        HotSwapPromotionError::UnexpectedLiveStrategy { current, expected } => {
            assert_eq!(current.as_str(), "live-gamma");
            assert_eq!(expected.as_str(), DEMOTING);
        }
        other => panic!("expected UnexpectedLiveStrategy, got {other:?}"),
    }
    // The unrelated live strategy keeps the slot — untouched.
    assert_eq!(outcome.designated_after.as_deref(), Some("live-gamma"));
}

#[test]
fn an_empty_live_slot_is_refused_not_promoted_into() {
    // This test previously asserted the OPPOSITE, and that is the point: the gate
    // accepted an empty designation and promoted, so "only after successful
    // demotion" did not hold on the CLI or Rust arms even though the REST wrapper
    // refused it. A rule enforced only at the outermost surface is not enforced.
    // Raised by /codex adversarial review r5 [critical].
    let mut designation = LiveDesignation::new();
    let outcome = run_swap(
        flat(),
        Positions(PositionAnswer::Flat),
        PaperHistory::queued(vec![Ok(Some(fingerprint(12, 3, "digest-a")))]),
        Versions::queued(vec![Ok(Some(version(HASH_A)))]),
        &mut designation,
    );

    assert_eq!(
        refusal(&outcome).machine_reason(),
        "NO_LIVE_STRATEGY_TO_DEMOTE"
    );
    assert_eq!(outcome.designated_after, None, "nothing may be designated");
}

#[test]
fn a_stale_demoting_id_reaches_no_demotion_side_port() {
    // A swap that queued behind another one can arrive with a demoting id that is no
    // longer live. `resolve_demotion` is NOT read-only on that path — it engages the
    // durable lockout, cancels resting liquidation orders and pages the operator — so
    // refusing only afterwards fires all of it against the wrong strategy.
    //
    // Every demotion-side port here panics if touched, which is the strongest
    // available form of "was never called".
    // Raised by /codex adversarial review r7 [critical].
    let orchestrator = StrategyOrchestrator;
    let events = PromotionEvents::default();
    let completions = Completions::default();
    let positions = Positions(PositionAnswer::Flat);
    let paper = PaperHistory::queued(vec![]);
    let versions = Versions::queued(vec![]);
    let mut designation = designation_holding("live-gamma"); // NOT the demoting id

    let result = orchestrator.execute_hot_swap(
        request(),
        &ForbiddenProbe,
        &ForbiddenCanceller,
        &ForbiddenAlerts,
        &DemotionEvents,
        &ForbiddenLock,
        PromotionPorts {
            positions: &positions,
            paper_history: &paper,
            versions: &versions,
            events: &events,
        },
        clear_cooldown(&completions),
        &mut designation,
        confirmation(CANDIDATE),
        OBSERVED_AT,
    );

    let error = result.expect_err("a stale demoting id must be refused");
    assert_eq!(error.machine_reason(), "UNEXPECTED_LIVE_STRATEGY");
    assert_eq!(
        designation.designated().map(|id| id.as_str()),
        Some("live-gamma")
    );
    // The refusal is still audited — a swap that was refused before it began is a
    // transition an operator needs to see.
    let recorded = events.recorded.into_inner();
    assert_eq!(recorded.len(), 1);
    assert!(!recorded[0].promoted);
    assert!(
        !recorded[0].flat_confirmed,
        "no demotion ran, so nothing was confirmed flat"
    );
}

#[test]
fn an_empty_live_slot_reaches_no_demotion_side_port_either() {
    let orchestrator = StrategyOrchestrator;
    let events = PromotionEvents::default();
    let completions = Completions::default();
    let positions = Positions(PositionAnswer::Flat);
    let paper = PaperHistory::queued(vec![]);
    let versions = Versions::queued(vec![]);
    let mut designation = LiveDesignation::new();

    let result = orchestrator.execute_hot_swap(
        request(),
        &ForbiddenProbe,
        &ForbiddenCanceller,
        &ForbiddenAlerts,
        &DemotionEvents,
        &ForbiddenLock,
        PromotionPorts {
            positions: &positions,
            paper_history: &paper,
            versions: &versions,
            events: &events,
        },
        clear_cooldown(&completions),
        &mut designation,
        confirmation(CANDIDATE),
        OBSERVED_AT,
    );

    assert_eq!(
        result
            .expect_err("an empty slot must be refused")
            .machine_reason(),
        "NO_LIVE_STRATEGY_TO_DEMOTE"
    );
    assert_eq!(designation.designated(), None);
}

#[test]
fn a_refusal_that_never_ran_a_demotion_never_claims_one() {
    // r7 moved the live-slot guards ahead of `resolve_demotion`; `flat_confirmed()`
    // was a denylist ("everything except DemotionRefused"), so those two refusals
    // inherited `true` and the operator-facing proof stream claimed a successful
    // demotion for a swap in which none had run.
    // Raised by /codex adversarial review r9 [critical].
    for (mut designation, expected) in [
        (LiveDesignation::new(), "NO_LIVE_STRATEGY_TO_DEMOTE"),
        (
            designation_holding("live-gamma"),
            "UNEXPECTED_LIVE_STRATEGY",
        ),
    ] {
        let outcome = run_swap(
            flat(),
            Positions(PositionAnswer::Flat),
            PaperHistory::queued(vec![]),
            Versions::queued(vec![]),
            &mut designation,
        );
        let error = refusal(&outcome);
        assert_eq!(error.machine_reason(), expected);
        assert_eq!(
            error.demotion_outcome(),
            DemotionProof::NotStarted,
            "{expected} is refused BEFORE any demotion runs: not flat, and not pending \
             either — nothing started, so there is no lockout to wait on"
        );
        assert!(!outcome.events[0].flat_confirmed);
    }
}

#[test]
fn every_post_demotion_refusal_still_reports_its_confirmed_demotion() {
    // The other direction, so the allowlist cannot be "fixed" by returning false
    // everywhere: a refusal that happened AFTER a confirmed-flat demotion must
    // still say so, or the operator loses the fact that the demotion succeeded.
    let mut designation = designation_holding(DEMOTING);
    let outcome = run_swap(
        flat(),
        Positions(PositionAnswer::Open(vec![OpenPosition {
            symbol: "AAPL".to_string(),
            quantity: 5,
        }])),
        PaperHistory::queued(vec![Ok(Some(fingerprint(12, 3, "digest-a")))]),
        Versions::queued(vec![Ok(Some(version(HASH_A)))]),
        &mut designation,
    );

    let error = refusal(&outcome);
    assert_eq!(error.machine_reason(), "LIVE_POSITIONS_OPEN");
    assert_eq!(
        error.demotion_outcome(),
        DemotionProof::FlatConfirmed,
        "the demotion DID reach flat before this refusal"
    );
}
