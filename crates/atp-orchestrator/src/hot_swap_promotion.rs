//! `SRS-RESV-005` — promote a selected paper strategy to live execution **only
//! after successful demotion** (SyRS SYS-49d / AC-14; StRS SN-1.25 / SN-1.30).
//!
//! # What the requirement actually demands
//!
//! > The promoted strategy starts live with no open IB positions, preserves prior
//! > paper performance history, and uses the same strategy code/API behavior.
//!
//! Three post-conditions and one ordering constraint. The ordering constraint is
//! the hard part: "only after successful demotion" has to be true of *every* way
//! a promotion can be reached, not just of the path the happy-case test walks.
//!
//! # Why the demotion acceptance is not enough on its own
//!
//! [`HotSwapDemotionResolved`] — the SRS-RESV-004 acceptance ERR-7 already ships —
//! has public fields and derives `Clone`. Any caller can write
//! `HotSwapDemotionResolved { promotion_allowed: true, .. }` by hand. If the
//! promotion gate consumed *that*, the requirement would be satisfiable by a
//! struct literal, which is no guarantee at all.
//!
//! It cannot simply be tightened: `tools/hot_swap_demotion_check.py` pins it
//! byte-for-byte (the `FlatBeforeTimeout` arm is asserted to be its sole
//! construction site) and ERR-7's tests depend on its shape. So this module
//! **layers a new authority on top and leaves the pinned primitive alone**:
//!
//!   * [`DemotionReceipt`] has private fields, no `Clone`, no `Default`, and no
//!     public constructor. It is mintable only by [`DemotionReceipt::mint`],
//!     which is `pub(crate)` and which refuses a `promotion_allowed: false`
//!     acceptance. A downstream crate cannot build one at all (E0603 on the
//!     constructor, private fields on the literal).
//!   * The promotion gate is `pub(crate)` and takes a receipt *by value*. The
//!     only public entry point is [`StrategyOrchestrator::execute_hot_swap`],
//!     which runs `resolve_demotion` first and mints the receipt from its `Ok`.
//!
//! The result is that "demote before promote" is a **compile error** to violate
//! rather than a convention a future call site can forget. This mirrors the
//! discipline `LiveDesignationConfirmation` (SRS-EXE-001) and `RollbackConfirmation`
//! (SRS-ORCH-005) already use for the same class of problem.
//!
//! # Fail-closed, everywhere
//!
//! Every port here is read-only (the gate observes; it cannot promote *through*
//! any of them) and three-way, because **unreadable, absent, and empty are three
//! different facts**. An unreadable position probe is not a flat account; a
//! missing paper history is not a preserved one. Each gets its own refusal
//! variant so the operator is sent to the right place.
//!
//! Every guard runs **before** the single designation write, and the two
//! post-conditions are re-read **after** it: if the paper history or the deployed
//! artifact moved while the promotion was in flight, the designation is rolled
//! back and the promotion is refused. A promotion that silently reset the paper
//! ledger would satisfy "it went live" and violate the requirement.
//!
//! # Scope — what is real here and what is deferred
//!
//! The gate, its ordering, and its refusals are real and proven offline. The
//! concrete producers behind two of the ports are deferred and enumerated in
//! `architecture/runtime_services.json` `hot_swap_promotion_contract.deferred[]`:
//! the real IB position feed (SRS-EXE-006) and the Reservoir ranking that names a
//! candidate (SRS-RESV-002).
//!
//! The cross-attempt lockout is no longer a gap. SRS-RESV-004 shipped
//! `DemotionPendingLock`, and `resolve_demotion` consults it BEFORE its probe, so
//! a swap attempted while a previous demotion is unresolved is refused before any
//! side effect fires — and an unreadable lockout blocks exactly like a held one.
//! Because the only path to this gate runs through `resolve_demotion`, that
//! protection is inherited here rather than reimplemented: `execute_hot_swap`
//! threads the lock through and cannot reach the promotion gate when it blocks.
//!
//! # The SYS-49e cool-down is enforced HERE, not merely upstream (SRS-RESV-006)
//!
//! SRS-RESV-006 gates the two SRS-RESV-003 trigger entry points, but those only
//! *mint a proposal* — nothing in the type graph requires a swap to have come from
//! one. `HotSwapDemotionRequest` is freely constructible and the CLI builds one
//! straight from argv, so a suppressed evaluation constrained nothing: this
//! module, `resv005_hot_swap_promote_cli swap` and `POST /api/v1/hot-swap` all
//! reached the demotion untouched. That was `cooldown-execution-bypass`, and the
//! fix is that [`CooldownControl`] is a **required, non-optional** parameter of
//! [`StrategyOrchestrator::execute_hot_swap`]: a caller cannot execute a swap
//! without stating what the window says, and `Unknown` refuses exactly as an
//! active window does.
//!
//! The same entry point also **starts** the next window, on its success arm only —
//! SYS-49e's "the timestamp of the most recent successful swap completion". A
//! demotion that ends without a promotion is a failed changeover, not a swap, and
//! must not open one.

use crate::cooldown::{CooldownState, ManualCooldownAcknowledgement, SwapCompletion};
use crate::{
    DemotionPendingLock, DeployedVersionRegistry, HotSwapDemotionEventSink,
    HotSwapDemotionResolved, HotSwapLiquidationProbe, HotSwapSideEffectError, OperatorAlertSink,
    StrategyOrchestrator, UnfilledOrderCanceller,
};
use atp_execution::designation::{
    LiveDesignation, LiveDesignationConfirmation, LiveDesignationError,
};
use atp_types::{
    DeployedVersion, HotSwapDemotionRequest, StrategyId, StructuredHotSwapDemotionError,
};
use std::fmt;

// --------------------------------------------------------------------------- //
// The acceptance token
// --------------------------------------------------------------------------- //

/// Proof that a **real** SRS-RESV-004 demotion reached flat before its timeout.
///
/// The only thing that can produce one is [`DemotionReceipt::mint`], which is
/// `pub(crate)` and is called from exactly one place: the `Ok` arm of
/// [`StrategyOrchestrator::execute_hot_swap`]'s `resolve_demotion` call.
///
/// Deliberately **not** `Clone` (a retained clone could authorize a second
/// promotion after the first consumed it — the same reasoning that keeps
/// `LiveDesignation` un-`Clone`-able), **not** `Default`, and its fields are
/// private so a downstream crate cannot build one with a struct literal either.
///
/// Both halves of that claim are compiler-enforced, and these doctests are the
/// proof — they compile as an *external* consumer of this crate, which is exactly
/// the position a future call site is in. A struct literal cannot reach the
/// private fields:
///
/// ```compile_fail
/// use atp_orchestrator::hot_swap_promotion::DemotionReceipt;
/// use atp_types::StrategyId;
/// let forged = DemotionReceipt {
///     demoting_strategy_id: StrategyId::new("live-alpha"),
///     candidate_strategy_id: StrategyId::new("paper-beta"),
///     elapsed_seconds: 0,
/// };
/// ```
///
/// and the minting constructor is `pub(crate)`, so it is not nameable either
/// (E0603):
///
/// ```compile_fail
/// use atp_orchestrator::hot_swap_promotion::DemotionReceipt;
/// let mint = DemotionReceipt::mint;
/// ```
///
/// Together with the gate being `pub(crate)`, that leaves
/// [`StrategyOrchestrator::execute_hot_swap`] — which always runs the demotion
/// first — as the only reachable promotion path.
#[derive(Debug, PartialEq, Eq)]
pub struct DemotionReceipt {
    demoting_strategy_id: StrategyId,
    candidate_strategy_id: StrategyId,
    elapsed_seconds: u64,
}

impl DemotionReceipt {
    /// Mint a receipt from a demotion acceptance.
    ///
    /// Returns `None` when the acceptance does not actually allow promotion.
    /// `resolve_demotion` only ever returns `Ok` with `promotion_allowed: true`,
    /// so this is defence in depth — but it is the cheap kind: if a future edit
    /// (or a rebase against the SRS-RESV-004 owner's branch) ever lets a
    /// `promotion_allowed: false` acceptance out of the demotion gate, the
    /// promotion path fails closed here instead of inheriting the mistake.
    pub(crate) fn mint(resolved: &HotSwapDemotionResolved) -> Option<Self> {
        if !resolved.promotion_allowed {
            return None;
        }
        Some(Self {
            demoting_strategy_id: resolved.demoting_strategy_id.clone(),
            candidate_strategy_id: resolved.candidate_strategy_id.clone(),
            elapsed_seconds: resolved.elapsed_seconds,
        })
    }

    /// The strategy whose live positions were confirmed flat.
    pub fn demoting_strategy(&self) -> &StrategyId {
        &self.demoting_strategy_id
    }

    /// The strategy this receipt authorizes for promotion.
    pub fn candidate_strategy(&self) -> &StrategyId {
        &self.candidate_strategy_id
    }

    /// How long the demotion took to reach flat, in seconds.
    pub fn elapsed_seconds(&self) -> u64 {
        self.elapsed_seconds
    }
}

// --------------------------------------------------------------------------- //
// Read-only ports
// --------------------------------------------------------------------------- //

/// One open position on the live account, as the probe reports it.
///
/// `quantity` is signed: a short position is negative, and both directions are
/// "open". Only an exactly-zero quantity is flat.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpenPosition {
    pub symbol: String,
    pub quantity: i64,
}

/// SYS-49d "starts live with no open IB positions" — the flat-start fact.
///
/// Read-only by construction: the gate cannot flatten an account through this
/// port, only observe one. The concrete producer is the deferred SRS-EXE-006 IB
/// position feed; `atp_execution::live_state::LiveExecutionState::open_positions`
/// is the in-process shape it reports.
///
/// `Err` means the account's position state could **not be read**. That is not a
/// flat account and the gate refuses on it
/// ([`HotSwapPromotionError::PositionsUnprovable`]) — "we could not check" and
/// "there is nothing there" are the two facts this requirement most needs kept
/// apart.
pub trait LivePositionProbe {
    fn open_positions(&self) -> Result<Vec<OpenPosition>, HotSwapSideEffectError>;
}

/// A fingerprint of a strategy's accumulated paper performance history.
///
/// Deliberately a plain value rather than an `atp-simulation` type: the gate must
/// be drivable without the simulation engine, and the requirement is about the
/// history being *unchanged*, which a fingerprint answers exactly. The concrete
/// source computes it from the real `PaperMetricsAccumulator` /
/// `PaperStateSnapshot` (SRS-SIM-004).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PaperHistoryFingerprint {
    /// Number of points on the accumulated equity curve.
    pub equity_points: u64,
    /// Number of recorded paper fills.
    pub trades: u64,
    /// Content digest over the accumulated history, so a same-length but
    /// rewritten history is still detected as drift.
    pub digest: String,
}

/// SYS-49d "preserves prior paper performance history".
///
/// `Ok(Some(_))` = a history exists and was read. `Ok(None)` = the source has **no
/// record** for this strategy — which the gate refuses
/// ([`HotSwapPromotionError::PaperHistoryMissing`]) rather than treating as a
/// trivially-preserved empty history: a promotion whose prior history cannot be
/// located has nothing to preserve, and reporting it as preserved would be the
/// false green this requirement exists to prevent. `Err` = the source could not
/// be read at all ([`HotSwapPromotionError::PaperHistoryUnreadable`]).
pub trait PaperHistorySource {
    fn fingerprint(
        &self,
        strategy_id: &StrategyId,
    ) -> Result<Option<PaperHistoryFingerprint>, HotSwapSideEffectError>;
}

/// The SYS-61 `hot_swap` **PROMOTION** audit record (the demotion half is
/// `HotSwapDemotionEventSink`).
///
/// Recorded on **both** outcomes — a blocked promotion is exactly the transition
/// an operator needs in the log — and treated as **best effort**, mirroring
/// `HotSwapDemotionEventSink`: by the time it is emitted the decision is made and
/// the designation write has already happened or already been refused, so a sink
/// failure must not roll it back. Durable delivery is the deferred SRS-LOG-001
/// sink's concern.
pub trait HotSwapPromotionEventSink {
    fn record(&self, event: HotSwapPromotionEvent) -> Result<(), HotSwapSideEffectError>;
}

/// The SYS-61 promotion state-transition record.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HotSwapPromotionEvent {
    pub demoting_strategy_id: StrategyId,
    pub candidate_strategy_id: StrategyId,
    /// Whether the candidate is now the designated live strategy.
    pub promoted: bool,
    /// The machine-readable refusal code when `promoted` is false; `None` on a
    /// successful promotion. Carried so the audit record says *why* a swap
    /// stopped, not merely that it did.
    pub refusal: Option<&'static str>,
    /// Whether the demotion reached flat before its timeout.
    pub flat_confirmed: bool,
    /// Whether the candidate's paper performance history was verified unchanged
    /// across the promotion.
    pub paper_history_preserved: bool,
    /// The candidate's deployed version identifier, when it could be resolved.
    pub deployed_version: Option<String>,
    pub observed_at_seconds: u64,
}

// --------------------------------------------------------------------------- //
// The SYS-49e cool-down (SRS-RESV-006)
// --------------------------------------------------------------------------- //

/// Where a **completed** swap starts its SyRS SYS-49e cool-down window.
///
/// A sink rather than a path, for the same reason every other dependency here is a
/// port: the gate must be drivable without a filesystem, and its ordering is the
/// thing under test. The concrete implementation is
/// [`crate::cooldown_store::record_completion`], bound at the composition root.
///
/// This is the ONLY mutator in the promotion path that is not the designation
/// write itself, and it is deliberately not part of [`PromotionPorts`] — that
/// bundle is pinned read-only by `tools/hot_swap_promotion_check.py`, and a
/// writer belongs nowhere near it.
///
/// `Err` carries an operator-facing reason. It does **not** roll the promotion
/// back: see [`CooldownWindowOutcome::NotStarted`].
pub trait SwapCompletionSink {
    /// Prove a completion COULD be recorded, before anything irreversible runs.
    ///
    /// Checked at the top of [`StrategyOrchestrator::execute_hot_swap`], because a
    /// swap that completes and then cannot record its window is a fail-open that
    /// nothing downstream can repair: the designation has moved and the book is
    /// flat, so it is far too late to refuse. Refusing HERE costs nothing — no
    /// demotion-side port has run — which is what makes the guarantee real rather
    /// than merely well-reported (adversarial review r4).
    fn probe_writable(&self) -> Result<(), String>;

    /// The instant the swap that just succeeded COMPLETED at.
    ///
    /// Read AFTER the promotion, and deliberately NOT the `observed_at_seconds` the
    /// call started with. SYS-49e says the window starts at "the timestamp of the
    /// most recent successful swap COMPLETION", and a swap is not instantaneous:
    /// `resolve_demotion` alone may legitimately run for the whole SYS-49b
    /// liquidation timeout (60s by default) before the promotion even begins.
    /// Stamping the window with the instant the attempt STARTED would therefore
    /// shorten a seven-day safety window by the duration of the swap, letting the
    /// automatic triggers resume that much early — the one direction a cool-down
    /// must never move (adversarial review r5).
    ///
    /// Fallible, and not defaulted: a clock that cannot be read must not silently
    /// become `observed_at_seconds` again, which is the bug wearing a fallback.
    fn completed_at_seconds(&self) -> Result<u64, String>;

    fn record_swap_completion(&self, completion: &SwapCompletion) -> Result<(), String>;
}

/// The SYS-49e inputs [`StrategyOrchestrator::execute_hot_swap`] requires.
///
/// One bundle, and **not** optional, because that is the whole anti-bypass
/// property: a caller cannot execute a swap without stating what the cool-down
/// window says. A composer with no window configured resolves
/// [`CooldownState::Unknown`] through [`crate::cooldown_store::resolve`], which
/// this gate refuses — "we could not tell" is never "no cool-down is in effect".
pub struct CooldownControl<'a, W>
where
    W: SwapCompletionSink,
{
    /// The window as [`crate::cooldown_store::resolve`] classified it. A resolved
    /// VALUE, not a port: the store read and the `Err -> Unknown` mapping happen
    /// in exactly one place, at the composition root.
    pub state: &'a CooldownState,
    /// Whether the operator has acknowledged the confirmation warning. An enum, so
    /// a bare `true` at the call site is not one character from the opposite.
    pub acknowledgement: ManualCooldownAcknowledgement,
    /// Where a successful swap records the completion that STARTS the next window.
    pub completions: &'a W,
}

/// Whether the completed swap actually started its next cool-down window.
///
/// Carried on [`HotSwapPromoted`] rather than swallowed, because the failure it
/// describes is a **fail-open**: the swap happened, so the next automatic
/// evaluation reads no window and does not suppress. The window cannot be started
/// BEFORE the designation write — a refused promotion would then have opened a
/// seven-day window for a swap that never happened, which contradicts SYS-49e's
/// "the timestamp of the most recent successful swap completion" and would let a
/// repeatedly-failing swap disable the automatic triggers indefinitely.
///
/// Nor can the promotion be rolled back on this failure: positions are flat and the
/// candidate is live: reverting the designation over an unwritable file would leave
/// the demoted strategy live with an emptied book, which is strictly worse. So the
/// honest handling is to make it LOUD — every surface reports it and exits
/// non-zero — and to record the residual rather than imply it does not exist
/// (`safety-paths.md` rule 41's own counter-rule).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CooldownWindowOutcome {
    /// The completion was recorded; automatic triggers are suppressed from here.
    Started { started_at_seconds: u64 },
    /// The swap completed and the window did NOT start. Automatic triggers are
    /// **not** suppressed until an operator repairs the store.
    NotStarted {
        reason: String,
        /// The instant the swap COMPLETED, when it was known.
        ///
        /// Carried so a repair instruction can name the right one. Without it the
        /// only timestamp a surface has to hand is the instant the attempt STARTED,
        /// and telling an operator to reopen the window at that value reintroduces
        /// exactly the defect r5 fixed — a seven-day window short by however long
        /// the swap took (adversarial review r7).
        ///
        /// `None` when the clock itself could not be read, which is the one case
        /// where no timestamped repair command can honestly be printed.
        completed_at_seconds: Option<u64>,
    },
}

/// A window that is DUE but not yet opened — the swap is decided, not yet durable.
///
/// [`StrategyOrchestrator::execute_hot_swap`] designates the candidate live *in
/// memory*; the caller then publishes that designation durably. Recording the
/// cool-down inside the gate meant a publish that failed before its rename left a
/// seven-day window suppressing the automatic triggers for a swap the durable
/// authority never accepted (adversarial review r6). So the gate now MINTS this
/// token and the caller redeems it with
/// [`StrategyOrchestrator::commit_cooldown_window`] **after** the publish.
///
/// The same shape as [`DemotionReceipt`], for the same reason: private fields, no
/// `Clone` (one swap opens one window), no `Default`, `pub(crate)` sole
/// constructor. A caller cannot fabricate a window, and — because
/// `execute_hot_swap` returns it inside [`HotSwapPromoted`] — cannot get one
/// without a swap that actually succeeded.
///
/// `#[must_use]` because dropping it is the fail-open r4 closed: a completed swap
/// whose window never opened.
#[must_use = "a swap that completed owes a cool-down window; redeem this with \
              StrategyOrchestrator::commit_cooldown_window after the durable publish"]
#[derive(Debug, PartialEq, Eq)]
pub struct PendingCooldownWindow {
    demoted_strategy_id: StrategyId,
    promoted_strategy_id: StrategyId,
}

impl PendingCooldownWindow {
    pub(crate) fn mint(demoted: &StrategyId, promoted: &StrategyId) -> Self {
        Self {
            demoted_strategy_id: demoted.clone(),
            promoted_strategy_id: promoted.clone(),
        }
    }
}

impl CooldownWindowOutcome {
    /// The operator-facing wire value.
    pub const fn as_str(&self) -> &'static str {
        match self {
            Self::Started { .. } => "STARTED",
            Self::NotStarted { .. } => "NOT_STARTED",
        }
    }

    /// True only when the next window is genuinely running.
    pub const fn started(&self) -> bool {
        matches!(self, Self::Started { .. })
    }
}

// --------------------------------------------------------------------------- //
// Outcome
// --------------------------------------------------------------------------- //

/// SRS-RESV-005 acceptance evidence: the candidate is the single designated live
/// strategy, and every AC clause was verified rather than assumed.
///
/// **Not `Clone`.** It carries a [`PendingCooldownWindow`], and a clonable
/// acceptance would let one swap redeem two windows — the same reasoning that keeps
/// [`DemotionReceipt`] un-`Clone`-able. The compiler enforces it: this derive cannot
/// name `Clone` while that field is present.
#[derive(Debug, PartialEq, Eq)]
pub struct HotSwapPromoted {
    pub demoted_strategy_id: StrategyId,
    pub promoted_strategy_id: StrategyId,
    /// Always `true` — carried explicitly so a surface renders the gate's
    /// decision instead of re-deriving it (the convention `HotSwapDemotionResolved`
    /// already sets with `promotion_allowed`).
    pub flat_confirmed: bool,
    /// The paper history fingerprint, identical before and after the promotion.
    pub paper_history: PaperHistoryFingerprint,
    /// The deployed artifact the candidate ran as paper and now runs as live —
    /// identical across the mode change.
    pub deployed_version: DeployedVersion,
    pub demotion_elapsed_seconds: u64,
    /// The SyRS SYS-49e window this swap OWES, not one it has already opened.
    ///
    /// Redeemed by [`StrategyOrchestrator::commit_cooldown_window`] once the caller
    /// has durably published the designation — see [`PendingCooldownWindow`] for why
    /// the gate must not record it itself.
    pub pending_cooldown: PendingCooldownWindow,
}

/// What the demotion half of a refused swap did — see
/// [`HotSwapPromotionError::demotion_outcome`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DemotionProof {
    /// No demotion-side port was touched; nothing mutated.
    NotStarted,
    /// The demotion ran and did not reach flat before its deadline (SYS-49b).
    TimedOut,
    /// The demotion reached flat; the refusal came afterwards.
    FlatConfirmed,
}

impl DemotionProof {
    /// The operator-facing wire value.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::NotStarted => "NOT_STARTED",
            Self::TimedOut => "DEMOTION_PENDING",
            Self::FlatConfirmed => "FLAT_CONFIRMED",
        }
    }
}

/// Why a Hot-Swap promotion did not happen. One variant per guard, each carrying
/// the fact an operator needs to act.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HotSwapPromotionError {
    /// The SRS-RESV-004 demotion did not reach flat: promotion never started.
    DemotionRefused(StructuredHotSwapDemotionError),
    /// The demotion gate returned an acceptance that does not allow promotion.
    /// Unreachable today (see [`DemotionReceipt::mint`]) and kept as a typed
    /// refusal rather than an `unwrap`.
    DemotionNotAccepted {
        demoting_strategy_id: StrategyId,
        candidate_strategy_id: StrategyId,
    },
    /// The receipt names a different swap than the attempt being executed.
    ReceiptMismatch {
        receipt_demoting: StrategyId,
        receipt_candidate: StrategyId,
        requested_demoting: StrategyId,
        requested_candidate: StrategyId,
    },
    /// Demoting and promoting the same strategy is not a swap.
    SameStrategy { strategy_id: StrategyId },
    /// The live account's positions could not be read — not the same as flat.
    PositionsUnprovable { reason: String },
    /// The live account still holds positions; a promoted strategy must start flat.
    PositionsOpen { symbols: Vec<String> },
    /// The candidate's paper performance history could not be read.
    PaperHistoryUnreadable { reason: String },
    /// No paper performance history exists for the candidate to preserve.
    PaperHistoryMissing { strategy_id: StrategyId },
    /// The paper history changed across the promotion — it was not preserved.
    PaperHistoryDrift {
        strategy_id: StrategyId,
        before: PaperHistoryFingerprint,
        after: PaperHistoryFingerprint,
    },
    /// The candidate's deployed version could not be read.
    CodeIdentityUnprovable { reason: String },
    /// The candidate has no recorded deployed version to hold constant.
    CodeIdentityMissing { strategy_id: StrategyId },
    /// The deployed artifact changed across the promotion — the promoted strategy
    /// is not running the code it ran as paper.
    CodeIdentityDrift {
        strategy_id: StrategyId,
        before: String,
        after: String,
    },
    /// Nothing holds the live slot, so there was no demotion to promote after.
    /// Promoting here would be a DESIGNATION (SRS-EXE-001's `promote-live` route),
    /// not a Hot-Swap.
    NoLiveStrategyToDemote { expected: StrategyId },
    /// A strategy other than the one this swap demoted holds the live slot. The
    /// execution-time revalidation SRS-RESV-003's contract hands to this layer:
    /// a trigger proposal records a *requested* demoting id, not a verified one.
    UnexpectedLiveStrategy {
        current: StrategyId,
        expected: StrategyId,
    },
    /// Releasing the demoted strategy's live designation failed.
    DemotionReleaseFailed(LiveDesignationError),
    /// The live-designation authority refused the promotion.
    DesignationRefused(LiveDesignationError),
    /// SyRS SYS-49e: a Hot-Swap cool-down window is in effect (or could not be
    /// read), and the operator has not acknowledged the confirmation warning.
    ///
    /// Never a permanent block — that is the requirement. The identical call with
    /// [`ManualCooldownAcknowledgement::Acknowledged`] proceeds, which is what
    /// makes this a *confirmation*, and what keeps a manual swap available during
    /// a window exactly as SYS-49a(a) requires.
    CooldownConfirmationRequired {
        state: CooldownState,
        warning: String,
    },
    /// SyRS SYS-49e: the cool-down window this swap would have to record cannot be
    /// written, so the swap is refused BEFORE it runs.
    ///
    /// Not a pedantic pre-check. A swap that completes and then cannot record its
    /// window leaves the automatic triggers armed against a strategy that has just
    /// been swapped in, and by then nothing can be undone. Refusing while nothing
    /// has happened is the only point at which the requirement can actually be
    /// guaranteed rather than reported on (adversarial review r4).
    CooldownWindowUnrecordable { reason: String },
}

impl HotSwapPromotionError {
    /// A stable machine-readable code for the operator surfaces (REST `detail`,
    /// CLI proof line, audit record). A closed vocabulary: surfaces must never
    /// have to parse the `Display` prose to route on the cause.
    pub fn machine_reason(&self) -> &'static str {
        match self {
            Self::DemotionRefused(_) => "DEMOTION_REFUSED",
            Self::DemotionNotAccepted { .. } => "DEMOTION_NOT_ACCEPTED",
            Self::ReceiptMismatch { .. } => "RECEIPT_MISMATCH",
            Self::SameStrategy { .. } => "SAME_STRATEGY_SWAP",
            Self::PositionsUnprovable { .. } => "LIVE_POSITIONS_UNPROVABLE",
            Self::PositionsOpen { .. } => "LIVE_POSITIONS_OPEN",
            Self::PaperHistoryUnreadable { .. } => "PAPER_HISTORY_UNREADABLE",
            Self::PaperHistoryMissing { .. } => "PAPER_HISTORY_MISSING",
            Self::PaperHistoryDrift { .. } => "PAPER_HISTORY_DRIFT",
            Self::CodeIdentityUnprovable { .. } => "CODE_IDENTITY_UNPROVABLE",
            Self::CodeIdentityMissing { .. } => "CODE_IDENTITY_MISSING",
            Self::CodeIdentityDrift { .. } => "CODE_IDENTITY_DRIFT",
            Self::NoLiveStrategyToDemote { .. } => "NO_LIVE_STRATEGY_TO_DEMOTE",
            Self::UnexpectedLiveStrategy { .. } => "UNEXPECTED_LIVE_STRATEGY",
            Self::DemotionReleaseFailed(_) => "DEMOTION_RELEASE_FAILED",
            Self::DesignationRefused(_) => "DESIGNATION_REFUSED",
            Self::CooldownConfirmationRequired { .. } => "HOT_SWAP_COOLDOWN_CONFIRMATION_REQUIRED",
            Self::CooldownWindowUnrecordable { .. } => "HOT_SWAP_COOLDOWN_UNRECORDABLE",
        }
    }

    /// What the demotion half of this swap actually did.
    ///
    /// THREE states, because it is a three-valued fact and a boolean over it kept
    /// producing wrong answers. It was `flat_confirmed() -> bool`, and the two
    /// rounds that followed were both consequences of that collapse: as a denylist
    /// it claimed a confirmed demotion for refusals that never ran one (r9), and
    /// once corrected to `false` those refusals were then reported as
    /// DEMOTION_PENDING — an accepted, mutating swap awaiting a lockout that was
    /// never engaged, which left the dashboard inert on a state that does not
    /// exist (r11). "Did not reach flat" and "never started" are different facts and
    /// need different wire values.
    ///
    /// An ALLOWLIST, so a variant added later defaults to `NotStarted` — the
    /// under-claim — rather than to a confirmed demotion.
    pub fn demotion_outcome(&self) -> DemotionProof {
        match self {
            // Reached only from inside the gate, which runs after the demotion gate
            // returned `Ok` — i.e. after flat was confirmed.
            Self::ReceiptMismatch { .. }
            | Self::SameStrategy { .. }
            | Self::PositionsUnprovable { .. }
            | Self::PositionsOpen { .. }
            | Self::PaperHistoryUnreadable { .. }
            | Self::PaperHistoryMissing { .. }
            | Self::PaperHistoryDrift { .. }
            | Self::CodeIdentityUnprovable { .. }
            | Self::CodeIdentityMissing { .. }
            | Self::CodeIdentityDrift { .. }
            | Self::DemotionReleaseFailed(_)
            | Self::DesignationRefused(_) => DemotionProof::FlatConfirmed,
            // The demotion RAN and did not reach flat: the SYS-49b timeout path,
            // where the demotion-pending lockout really was engaged.
            Self::DemotionRefused(_) => DemotionProof::TimedOut,
            // The demotion never started: the live slot was wrong or empty, or the
            // SYS-49e cool-down refused the attempt outright. All are refused
            // before any demotion-side port is touched. Nothing mutated, so no
            // lockout exists to wait on.
            Self::NoLiveStrategyToDemote { .. }
            | Self::UnexpectedLiveStrategy { .. }
            | Self::CooldownConfirmationRequired { .. }
            | Self::CooldownWindowUnrecordable { .. }
            | Self::DemotionNotAccepted { .. } => DemotionProof::NotStarted,
        }
    }
}

impl fmt::Display for HotSwapPromotionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DemotionRefused(error) => write!(
                formatter,
                "SRS-RESV-005: promotion blocked — the demotion did not succeed ({error})",
            ),
            Self::DemotionNotAccepted {
                demoting_strategy_id,
                candidate_strategy_id,
            } => write!(
                formatter,
                "SRS-RESV-005: the demotion of `{}` produced an acceptance that does not \
                 allow promoting `{}`; refusing to promote",
                demoting_strategy_id.as_str(),
                candidate_strategy_id.as_str(),
            ),
            Self::ReceiptMismatch {
                receipt_demoting,
                receipt_candidate,
                requested_demoting,
                requested_candidate,
            } => write!(
                formatter,
                "SRS-RESV-005: the demotion receipt authorizes `{}` -> `{}` but the swap \
                 being executed is `{}` -> `{}`; a receipt is not transferable between swaps",
                receipt_demoting.as_str(),
                receipt_candidate.as_str(),
                requested_demoting.as_str(),
                requested_candidate.as_str(),
            ),
            Self::SameStrategy { strategy_id } => write!(
                formatter,
                "SRS-RESV-005: `{}` is named as both the demoting and the candidate \
                 strategy; a Hot-Swap must name two different strategies",
                strategy_id.as_str(),
            ),
            Self::PositionsUnprovable { reason } => write!(
                formatter,
                "SRS-RESV-005: the live account's open positions could not be read ({reason}); \
                 an unprovable position state is not a flat one, so promotion is refused",
            ),
            Self::PositionsOpen { symbols } => write!(
                formatter,
                "SRS-RESV-005: the live account still holds open positions ({}); SYS-49d \
                 requires the promoted strategy to start flat",
                symbols.join(", "),
            ),
            Self::PaperHistoryUnreadable { reason } => write!(
                formatter,
                "SRS-RESV-005: the candidate's paper performance history could not be read \
                 ({reason}); preservation cannot be evidenced, so promotion is refused",
            ),
            Self::PaperHistoryMissing { strategy_id } => write!(
                formatter,
                "SRS-RESV-005: no paper performance history is recorded for `{}`; there is \
                 nothing to preserve and preservation cannot be claimed",
                strategy_id.as_str(),
            ),
            Self::PaperHistoryDrift {
                strategy_id,
                before,
                after,
            } => write!(
                formatter,
                "SRS-RESV-005: the paper performance history of `{}` changed across the \
                 promotion ({} points/{} trades/{} -> {} points/{} trades/{}); SYS-49d \
                 requires it to be preserved",
                strategy_id.as_str(),
                before.equity_points,
                before.trades,
                before.digest,
                after.equity_points,
                after.trades,
                after.digest,
            ),
            Self::CodeIdentityUnprovable { reason } => write!(
                formatter,
                "SRS-RESV-005: the candidate's deployed version could not be read ({reason}); \
                 'the same strategy code' cannot be evidenced, so promotion is refused",
            ),
            Self::CodeIdentityMissing { strategy_id } => write!(
                formatter,
                "SRS-RESV-005: no deployed version is recorded for `{}`; the promoted \
                 strategy's code identity cannot be held constant",
                strategy_id.as_str(),
            ),
            Self::CodeIdentityDrift {
                strategy_id,
                before,
                after,
            } => write!(
                formatter,
                "SRS-RESV-005: the deployed artifact of `{}` changed across the promotion \
                 ({before} -> {after}); SYS-49d requires the same strategy code",
                strategy_id.as_str(),
            ),
            Self::NoLiveStrategyToDemote { expected } => write!(
                formatter,
                "SRS-RESV-005: no strategy holds the live designation, so `{}` was never \
                 demoted and there is nothing to promote after; designating a strategy \
                 live with no swap is SRS-EXE-001's promote-live path",
                expected.as_str(),
            ),
            Self::UnexpectedLiveStrategy { current, expected } => write!(
                formatter,
                "SRS-RESV-005: `{}` holds the live designation but this swap demotes `{}`; \
                 refusing to promote over an unrelated live strategy",
                current.as_str(),
                expected.as_str(),
            ),
            Self::DemotionReleaseFailed(error) => write!(
                formatter,
                "SRS-RESV-005: the demoted strategy's live designation could not be \
                 released ({error}); promotion is refused rather than run over it",
            ),
            Self::DesignationRefused(error) => write!(
                formatter,
                "SRS-RESV-005: the live-designation authority refused the promotion ({error})",
            ),
            Self::CooldownConfirmationRequired { state, warning } => write!(
                formatter,
                "SRS-RESV-006: a Hot-Swap cool-down is in effect (window {}) and this swap was \
                 not confirmed against it — {warning}. Nothing was demoted and nothing was \
                 promoted. Re-run acknowledging the warning to swap anyway; SYS-49e permits a \
                 manual swap during the window, it only requires you to say so.",
                state.as_str(),
            ),
            Self::CooldownWindowUnrecordable { reason } => write!(
                formatter,
                "SRS-RESV-006: the Hot-Swap cool-down window cannot be recorded ({reason}), so \
                 this swap is refused BEFORE it runs. Nothing was demoted and nothing was \
                 promoted. A swap that completed and then could not open its window would \
                 leave the automatic triggers armed against the strategy just promoted, and \
                 nothing could undo it at that point — repair the cool-down state file first.",
            ),
        }
    }
}

impl std::error::Error for HotSwapPromotionError {}

// --------------------------------------------------------------------------- //
// Port bundle
// --------------------------------------------------------------------------- //

/// The four promotion-side dependencies, bundled so the gate's signature stays
/// readable. Every one is read-only or audit-only; none of them can promote.
pub struct PromotionPorts<'a, Q, H, R, S>
where
    Q: LivePositionProbe,
    H: PaperHistorySource,
    R: DeployedVersionRegistry,
    S: HotSwapPromotionEventSink,
{
    pub positions: &'a Q,
    pub paper_history: &'a H,
    pub versions: &'a R,
    pub events: &'a S,
}

impl StrategyOrchestrator {
    /// SRS-RESV-005 / SyRS SYS-49d — the **only** public path to a live promotion.
    ///
    /// Runs the SRS-RESV-004 demotion gate first and promotes only from its `Ok`:
    ///
    /// 1. `resolve_demotion(...)` — on `Err` this returns
    ///    [`HotSwapPromotionError::DemotionRefused`] and the promotion gate is
    ///    never reached (the demotion gate has already cancelled the unfilled
    ///    liquidation order, paged the operator on all three channels, and
    ///    recorded the demotion-pending transition);
    /// 2. [`DemotionReceipt::mint`] from the acceptance — the token no caller can
    ///    forge;
    /// 3. the promotion gate below.
    ///
    /// The SYS-61 promotion audit record is emitted on **both** outcomes, so a
    /// blocked swap is as visible in the log as a completed one.
    ///
    /// **Scope.** One attempt. A timeout blocks promotion for *this* call; the
    /// durable demotion-pending lockout that would also block a later retry is
    /// SRS-RESV-004's (see the module docs).
    #[allow(clippy::too_many_arguments)]
    pub fn execute_hot_swap<P, C, A, E, L, Q, H, R, S, W>(
        &self,
        request: HotSwapDemotionRequest,
        liquidation: &P,
        canceller: &C,
        alerts: &A,
        demotion_events: &E,
        lock: &L,
        ports: PromotionPorts<'_, Q, H, R, S>,
        cooldown: CooldownControl<'_, W>,
        designation: &mut LiveDesignation,
        confirmation: LiveDesignationConfirmation,
        observed_at_seconds: u64,
    ) -> Result<HotSwapPromoted, HotSwapPromotionError>
    where
        P: HotSwapLiquidationProbe,
        C: UnfilledOrderCanceller,
        A: OperatorAlertSink,
        E: HotSwapDemotionEventSink,
        L: DemotionPendingLock,
        Q: LivePositionProbe,
        H: PaperHistorySource,
        R: DeployedVersionRegistry,
        S: HotSwapPromotionEventSink,
        W: SwapCompletionSink,
    {
        let demoting = request.demoting_strategy_id.clone();
        let candidate = request.candidate_strategy_id.clone();

        // PROVE THE NEXT WINDOW CAN BE RECORDED, BEFORE EVERYTHING ELSE.
        //
        // The confirmation gate below stops a swap the window FORBIDS. This one stops
        // a swap the window could not be UPDATED by — the other half of the same
        // requirement, and the one that cannot be repaired after the fact: on the
        // success arm, a failed completion write leaves the designation moved, the
        // book flat, and the automatic triggers armed against the strategy just
        // promoted, with rolling back strictly worse than living with it.
        //
        // FIRST of the two, because this one is UNWAIVABLE and the confirmation is
        // not. An operator inside a window whose store is also broken would
        // otherwise be told to acknowledge, acknowledge, and only then hit the wall
        // that no acknowledgement can move.
        //
        // Refusing HERE costs nothing — no demotion-side port has run, nothing has
        // mutated — which is what turns "we report the fail-open loudly" into "the
        // fail-open is unreachable by this cause". Raised by adversarial review r4
        // as the residual the loud reporting still left open.
        //
        // The residual that REMAINS is a genuine race: a store that is writable here
        // and not writable a few seconds later. `CooldownWindowOutcome::NotStarted`
        // still covers it, still exits non-zero, and it is stated in
        // `hot_swap_cooldown_contract.deferred` rather than implied away.
        if let Err(reason) = cooldown.completions.probe_writable() {
            let error = HotSwapPromotionError::CooldownWindowUnrecordable { reason };
            let _ = ports.events.record(HotSwapPromotionEvent {
                demoting_strategy_id: demoting,
                candidate_strategy_id: candidate,
                promoted: false,
                refusal: Some(error.machine_reason()),
                flat_confirmed: false,
                paper_history_preserved: false,
                deployed_version: None,
                observed_at_seconds,
            });
            return Err(error);
        }

        // THEN the SYS-49e CONFIRMATION gate.
        //
        // SYS-49e says that during the window no automatic trigger "shall be ACTED
        // UPON". SRS-RESV-006 landed the same `proven_clear()` predicate on the two
        // SRS-RESV-003 entry points, but those only MINT a proposal — nothing forces
        // a swap to have come from one. `HotSwapDemotionRequest` is freely
        // constructible and the CLI builds one straight from argv, so a suppressed
        // evaluation constrained nothing at all: this function, the CLI's `swap`
        // subcommand and `POST /api/v1/hot-swap` reached the demotion untouched.
        // Enforcing here is what makes the window mean something, because this is
        // the single chokepoint all three of those surfaces pass through.
        // Raised by adversarial review r2 as `cooldown-execution-bypass`.
        //
        // FIRST, ahead of the stale-slot revalidation below, for two reasons. It
        // consults no port, so ordering it first costs nothing; and an operator
        // blocked by a seven-day window who is told "no strategy holds the live
        // designation" is being sent to fix the wrong thing. Both refusals are
        // pre-side-effect, so the order is an honesty question, not a safety one.
        //
        // The predicate is `proven_clear()`, identical to the two trigger-path arms
        // — so `Unknown` (unreadable, absent path, corrupt) refuses here exactly as
        // it suppresses there, and one mutation reddens all three test families.
        if !cooldown.state.proven_clear() && !cooldown.acknowledgement.is_acknowledged() {
            let error = HotSwapPromotionError::CooldownConfirmationRequired {
                state: cooldown.state.clone(),
                warning: cooldown.state.warning_text(),
            };
            let _ = ports.events.record(HotSwapPromotionEvent {
                demoting_strategy_id: demoting,
                candidate_strategy_id: candidate,
                promoted: false,
                refusal: Some(error.machine_reason()),
                // Nothing ran: the refusal is ahead of every demotion-side port.
                flat_confirmed: false,
                paper_history_preserved: false,
                deployed_version: None,
                observed_at_seconds,
            });
            return Err(error);
        }

        // REVALIDATE THE LIVE SLOT BEFORE ANY DEMOTION-SIDE PORT RUNS.
        //
        // `resolve_demotion` is not read-only: on its timeout branch it engages the
        // durable demotion-pending lockout, cancels unfilled liquidation orders, and
        // pages the operator on three channels. Checking the slot only afterwards (in
        // `promote_after_demotion`) meant a swap aimed at a STALE demoting id — one
        // that queued behind another swap which had already promoted something else —
        // could fire all of that against a strategy that was no longer live, and only
        // then be refused.
        //
        // So the same two refusals run here, first. The copies inside the gate stay:
        // they are what a caller reaching the gate by any other route still passes
        // through, and they own the slot RELEASE that must be ordered before the
        // promote. Raised by /codex adversarial review r7 [critical].
        let stale = match designation.designated() {
            None => Some(HotSwapPromotionError::NoLiveStrategyToDemote {
                expected: demoting.clone(),
            }),
            Some(current) if current != &demoting => {
                Some(HotSwapPromotionError::UnexpectedLiveStrategy {
                    current: current.clone(),
                    expected: demoting.clone(),
                })
            }
            Some(_) => None,
        };
        if let Some(error) = stale {
            let _ = ports.events.record(HotSwapPromotionEvent {
                demoting_strategy_id: demoting,
                candidate_strategy_id: candidate,
                promoted: false,
                refusal: Some(error.machine_reason()),
                // The demotion never ran, so nothing about it was confirmed.
                flat_confirmed: false,
                paper_history_preserved: false,
                deployed_version: None,
                observed_at_seconds,
            });
            return Err(error);
        }

        let outcome = match self.resolve_demotion(
            request,
            liquidation,
            canceller,
            alerts,
            demotion_events,
            lock,
            observed_at_seconds,
        ) {
            Ok(resolved) => match DemotionReceipt::mint(&resolved) {
                Some(receipt) => self.promote_after_demotion(
                    receipt,
                    &demoting,
                    &candidate,
                    ports.positions,
                    ports.paper_history,
                    ports.versions,
                    designation,
                    confirmation,
                ),
                None => Err(HotSwapPromotionError::DemotionNotAccepted {
                    demoting_strategy_id: demoting.clone(),
                    candidate_strategy_id: candidate.clone(),
                }),
            },
            Err(refused) => Err(HotSwapPromotionError::DemotionRefused(refused)),
        };

        // START THE NEXT WINDOW — SYS-49e's third clause, on the success arm ONLY.
        //
        // "The cool-down start time shall be the timestamp of the most recent
        // successful swap COMPLETION", so the completion is recorded here, after the
        // designation write, with `observed_at_seconds` as the start. Two arms that
        // deliberately do NOT write:
        //
        // * any refusal — a demotion that ended without a promotion is a failed
        //   changeover, not a swap (SRS-RESV-004's SYS-49b timeout reaches exactly
        //   that state). Starting a window there would suppress the automatic
        //   triggers for seven days over a swap that never happened, and would let a
        //   repeatedly-failing swap disable them indefinitely;
        // * anything before the designation write, for the same reason in the other
        //   direction.
        //
        // A failed write is NOT swallowed and NOT a rollback — see
        // `CooldownWindowOutcome::NotStarted` for why neither is available here.

        // Best-effort audit on both arms (see `HotSwapPromotionEventSink`): the
        // decision is already made and the designation write has already
        // happened or already been refused, so a sink failure changes nothing.
        let _ = ports.events.record(match &outcome {
            Ok(promoted) => HotSwapPromotionEvent {
                demoting_strategy_id: demoting,
                candidate_strategy_id: candidate,
                promoted: true,
                refusal: None,
                flat_confirmed: true,
                paper_history_preserved: true,
                deployed_version: Some(promoted.deployed_version.version_identifier()),
                observed_at_seconds,
            },
            Err(error) => HotSwapPromotionEvent {
                demoting_strategy_id: demoting,
                candidate_strategy_id: candidate,
                promoted: false,
                refusal: Some(error.machine_reason()),
                flat_confirmed: error.demotion_outcome() == DemotionProof::FlatConfirmed,
                paper_history_preserved: false,
                deployed_version: None,
                observed_at_seconds,
            },
        });

        outcome
    }

    /// Redeem a [`PendingCooldownWindow`] — open the SYS-49e window a completed swap
    /// owes, AFTER its designation has been durably published.
    ///
    /// Separate from [`Self::execute_hot_swap`] because the ordering is the whole
    /// point. That call designates the candidate live *in memory*; the caller then
    /// persists it. Recording the cool-down inside the gate meant a publish that
    /// failed before its rename left a seven-day window suppressing the automatic
    /// triggers for a swap the durable authority never accepted — a partial-failure
    /// state with nothing to reconcile against (adversarial review r6).
    ///
    /// So the caller redeems this only on the paths where the swap really is
    /// durable, and simply drops the token on the paths where it is not. Dropping
    /// warns (`#[must_use]`), which is what stops the *other* direction — a
    /// completed swap whose window silently never opened.
    ///
    /// Takes the token **by value**: one swap, one window.
    pub fn commit_cooldown_window<W>(
        &self,
        pending: PendingCooldownWindow,
        completions: &W,
    ) -> CooldownWindowOutcome
    where
        W: SwapCompletionSink,
    {
        // The COMPLETION instant, read HERE — after the promotion, after the durable
        // publish. Everything before this may have taken the whole SYS-49b
        // liquidation timeout, and a window stamped with the attempt's START would be
        // that much shorter than the seven days SYS-49e requires (review r5).
        let completed_at_seconds = match completions.completed_at_seconds() {
            Ok(seconds) => seconds,
            Err(reason) => {
                return CooldownWindowOutcome::NotStarted {
                    reason,
                    completed_at_seconds: None,
                }
            }
        };
        let completion = SwapCompletion {
            completed_at_seconds,
            demoted_strategy_id: pending.demoted_strategy_id,
            promoted_strategy_id: pending.promoted_strategy_id,
        };
        match completions.record_swap_completion(&completion) {
            Ok(()) => CooldownWindowOutcome::Started {
                started_at_seconds: completed_at_seconds,
            },
            Err(reason) => CooldownWindowOutcome::NotStarted {
                reason,
                completed_at_seconds: Some(completed_at_seconds),
            },
        }
    }

    /// The promotion gate proper. `pub(crate)` on purpose: a caller outside this
    /// crate cannot mint a [`DemotionReceipt`], and giving them a way to call
    /// this without one would reintroduce exactly the bypass the receipt exists
    /// to close (the discipline SRS-EXE-004 already applies to `submit_live_order`).
    ///
    /// Every guard runs **before** the single designation write, in a fixed
    /// order, so a refused promotion leaves no trace:
    ///
    /// 1. the receipt must name *this* swap, and a swap must name two strategies;
    /// 2. the candidate's paper performance history must be readable and present;
    /// 3. the candidate's deployed version must be readable and present;
    /// 4. the live account must be **provably** flat (SYS-49d clause 1);
    /// 5. the live slot must be free, or held by the strategy this swap demoted —
    ///    releasing it is then the last act of the demotion, ordered strictly
    ///    before any promote. A third strategy refuses;
    /// 6. the single write: `designate`;
    /// 7. the two post-conditions are re-read; drift rolls the designation back
    ///    and refuses (SYS-49d clauses 2 and 3).
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn promote_after_demotion<Q, H, R>(
        &self,
        receipt: DemotionReceipt,
        requested_demoting: &StrategyId,
        requested_candidate: &StrategyId,
        positions: &Q,
        paper_history: &H,
        versions: &R,
        designation: &mut LiveDesignation,
        confirmation: LiveDesignationConfirmation,
    ) -> Result<HotSwapPromoted, HotSwapPromotionError>
    where
        Q: LivePositionProbe,
        H: PaperHistorySource,
        R: DeployedVersionRegistry,
    {
        // --- 1. identity ---------------------------------------------------- //
        if receipt.demoting_strategy() != requested_demoting
            || receipt.candidate_strategy() != requested_candidate
        {
            return Err(HotSwapPromotionError::ReceiptMismatch {
                receipt_demoting: receipt.demoting_strategy_id.clone(),
                receipt_candidate: receipt.candidate_strategy_id.clone(),
                requested_demoting: requested_demoting.clone(),
                requested_candidate: requested_candidate.clone(),
            });
        }
        if requested_demoting.as_str() == requested_candidate.as_str() {
            return Err(HotSwapPromotionError::SameStrategy {
                strategy_id: requested_candidate.clone(),
            });
        }

        // --- 2. paper history (AC clause 2, captured) ----------------------- //
        let history_before = read_paper_history(paper_history, requested_candidate)?;

        // --- 3. code identity (AC clause 3, captured) ----------------------- //
        let version_before = read_deployed_version(versions, requested_candidate)?;

        // --- 4. flat start (AC clause 1) ------------------------------------ //
        // Read BEFORE the designation write: a promotion refused for open
        // positions must not have briefly designated the candidate live.
        let open = positions.open_positions().map_err(|error| {
            HotSwapPromotionError::PositionsUnprovable {
                reason: error.reason,
            }
        })?;
        let held: Vec<String> = open
            .iter()
            .filter(|position| position.quantity != 0)
            .map(|position| position.symbol.clone())
            .collect();
        if !held.is_empty() {
            return Err(HotSwapPromotionError::PositionsOpen { symbols: held });
        }

        // --- 5. execution-time live revalidation ---------------------------- //
        // SRS-RESV-003's contract is explicit that a trigger proposal records a
        // REQUESTED demoting id, not a verified one, and that this layer must
        // revalidate at execution time. So the slot is checked against the
        // strategy this swap actually demoted, never trusted from the proposal.
        match designation.designated() {
            // Refused in the SHARED gate, not in the REST wrapper. A rule enforced
            // only at the outermost surface leaves the CLI and the Rust API able to
            // violate it, and the CLI is a declared SYS-49a operator arm. "Only
            // after successful demotion" cannot hold when nothing was live to demote.
            None => {
                return Err(HotSwapPromotionError::NoLiveStrategyToDemote {
                    expected: requested_demoting.clone(),
                });
            }
            Some(current) if current == requested_demoting => {
                // Releasing the slot is the last act of the demotion, and it is
                // ordered strictly before the promote below — the sequence the
                // requirement names.
                designation
                    .demote(requested_demoting)
                    .map_err(HotSwapPromotionError::DemotionReleaseFailed)?;
            }
            Some(current) => {
                return Err(HotSwapPromotionError::UnexpectedLiveStrategy {
                    current: current.clone(),
                    expected: requested_demoting.clone(),
                });
            }
        }

        // --- 6. the single write -------------------------------------------- //
        designation
            .designate(requested_candidate.clone(), confirmation)
            .map_err(HotSwapPromotionError::DesignationRefused)?;

        // --- 7. post-conditions --------------------------------------------- //
        // Re-read, and roll the designation back on drift. A promotion that went
        // live having reset the paper ledger or swapped the artifact satisfies
        // "it is live" and violates SYS-49d; refusing it is the whole point.
        let history_after = match read_paper_history(paper_history, requested_candidate) {
            Ok(fingerprint) => fingerprint,
            Err(error) => return Err(rollback(designation, requested_candidate, error)),
        };
        if history_after != history_before {
            return Err(rollback(
                designation,
                requested_candidate,
                HotSwapPromotionError::PaperHistoryDrift {
                    strategy_id: requested_candidate.clone(),
                    before: history_before,
                    after: history_after,
                },
            ));
        }
        let version_after = match read_deployed_version(versions, requested_candidate) {
            Ok(version) => version,
            Err(error) => return Err(rollback(designation, requested_candidate, error)),
        };
        if version_after.version_identifier() != version_before.version_identifier() {
            return Err(rollback(
                designation,
                requested_candidate,
                HotSwapPromotionError::CodeIdentityDrift {
                    strategy_id: requested_candidate.clone(),
                    before: version_before.version_identifier(),
                    after: version_after.version_identifier(),
                },
            ));
        }

        Ok(HotSwapPromoted {
            demoted_strategy_id: receipt.demoting_strategy_id,
            promoted_strategy_id: receipt.candidate_strategy_id,
            flat_confirmed: true,
            paper_history: history_before,
            deployed_version: version_before,
            demotion_elapsed_seconds: receipt.elapsed_seconds,
            // The window this swap OWES. Minted here, redeemed by the caller after
            // the durable publish — see `PendingCooldownWindow`.
            pending_cooldown: PendingCooldownWindow::mint(
                &requested_demoting.clone(),
                &requested_candidate.clone(),
            ),
        })
    }
}

/// Undo the designation write after a post-condition failed, and return the
/// original refusal.
///
/// The rollback's own failure is deliberately NOT substituted for `error`: the
/// operator needs the reason the promotion was refused, and a `demote` that fails
/// here means the slot no longer holds the candidate, which is the state the
/// refusal already implies.
fn rollback(
    designation: &mut LiveDesignation,
    candidate: &StrategyId,
    error: HotSwapPromotionError,
) -> HotSwapPromotionError {
    let _ = designation.demote(candidate);
    error
}

/// Read a paper history fingerprint, mapping both fail-closed outcomes.
fn read_paper_history<H: PaperHistorySource>(
    source: &H,
    strategy_id: &StrategyId,
) -> Result<PaperHistoryFingerprint, HotSwapPromotionError> {
    match source.fingerprint(strategy_id) {
        Ok(Some(fingerprint)) => Ok(fingerprint),
        Ok(None) => Err(HotSwapPromotionError::PaperHistoryMissing {
            strategy_id: strategy_id.clone(),
        }),
        Err(error) => Err(HotSwapPromotionError::PaperHistoryUnreadable {
            reason: error.reason,
        }),
    }
}

/// Read the recorded deployed version, mapping both fail-closed outcomes.
fn read_deployed_version<R: DeployedVersionRegistry>(
    registry: &R,
    strategy_id: &StrategyId,
) -> Result<DeployedVersion, HotSwapPromotionError> {
    match registry.lookup(strategy_id) {
        Ok(Some(version)) => Ok(version),
        Ok(None) => Err(HotSwapPromotionError::CodeIdentityMissing {
            strategy_id: strategy_id.clone(),
        }),
        Err(error) => Err(HotSwapPromotionError::CodeIdentityUnprovable {
            reason: error.reason,
        }),
    }
}
