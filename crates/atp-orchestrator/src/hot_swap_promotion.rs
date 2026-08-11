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
//! concrete producers behind three of the ports are deferred and enumerated in
//! `architecture/runtime_services.json` `hot_swap_promotion_contract.deferred[]`:
//! the real IB position feed (SRS-EXE-006), the durable cross-attempt
//! demotion-pending lockout (SRS-RESV-004), the cool-down window (SRS-RESV-006),
//! and the Reservoir ranking that names a candidate (SRS-RESV-002).
//!
//! The cross-attempt lockout is no longer a gap. SRS-RESV-004 shipped
//! `DemotionPendingLock`, and `resolve_demotion` consults it BEFORE its probe, so
//! a swap attempted while a previous demotion is unresolved is refused before any
//! side effect fires — and an unreadable lockout blocks exactly like a held one.
//! Because the only path to this gate runs through `resolve_demotion`, that
//! protection is inherited here rather than reimplemented: `execute_hot_swap`
//! threads the lock through and cannot reach the promotion gate when it blocks.

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
// Outcome
// --------------------------------------------------------------------------- //

/// SRS-RESV-005 acceptance evidence: the candidate is the single designated live
/// strategy, and every AC clause was verified rather than assumed.
#[derive(Debug, Clone, PartialEq, Eq)]
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
        }
    }

    /// Whether the demotion half of this swap reached flat before its timeout.
    ///
    /// An ALLOWLIST of the refusals that can only occur AFTER a demotion has
    /// already been confirmed flat — deliberately not the inverse denylist it used
    /// to be. That denylist read "everything except `DemotionRefused`", so the two
    /// live-slot refusals, which are now raised BEFORE `resolve_demotion` runs,
    /// silently inherited `true`: the CLI printed `demotion-outcome:FLAT_CONFIRMED`
    /// and the REST body reported `demotion_state: DEMOTED` for a swap in which no
    /// demotion had happened at all. (Raised by /codex adversarial review r9
    /// [critical] — a defect introduced by r7's own fix.)
    ///
    /// Written this way round so the failure mode of forgetting a variant is an
    /// UNDER-claim, not a false claim of a successful demotion.
    pub fn flat_confirmed(&self) -> bool {
        match self {
            // Reached only from inside the gate, which runs after the demotion
            // gate returned `Ok` — i.e. after flat was confirmed.
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
            | Self::DesignationRefused(_) => true,
            // The demotion ran and did NOT reach flat.
            Self::DemotionRefused(_)
            // The demotion never ran: the live slot was wrong or empty, and these
            // are refused before any demotion-side port is touched.
            | Self::NoLiveStrategyToDemote { .. }
            | Self::UnexpectedLiveStrategy { .. }
            // Defence-in-depth arm; no demotion acceptance was produced.
            | Self::DemotionNotAccepted { .. } => false,
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
    pub fn execute_hot_swap<P, C, A, E, L, Q, H, R, S>(
        &self,
        request: HotSwapDemotionRequest,
        liquidation: &P,
        canceller: &C,
        alerts: &A,
        demotion_events: &E,
        lock: &L,
        ports: PromotionPorts<'_, Q, H, R, S>,
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
    {
        let demoting = request.demoting_strategy_id.clone();
        let candidate = request.candidate_strategy_id.clone();

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
                flat_confirmed: error.flat_confirmed(),
                paper_history_preserved: false,
                deployed_version: None,
                observed_at_seconds,
            },
        });

        outcome
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
