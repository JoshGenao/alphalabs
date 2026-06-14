//! Order lifecycle state machine for **SRS-EXE-008** — "implement an order
//! lifecycle state machine with documented states and transitions, and use a
//! strategy-supplied client correlation ID as the idempotency key for live and
//! paper order submissions" (SyRS SYS-3 / SYS-7 / SYS-64 / SYS-90, NFR-R3;
//! StRS SN-1.08 / SN-1.22).
//!
//! # Why this lives in `atp-types`
//!
//! The acceptance criterion requires the lifecycle and its correlation-ID
//! idempotency to be **identical for live and paper order submissions**. The
//! live path is owned by the execution engine (`atp-execution`) and the paper
//! path by the internal simulation engine (`atp-simulation`); those are
//! *sibling* crates — neither may depend on the other (AGENTS.md module flow,
//! SRS-ARCH-002 dependency direction). So the single source of truth for the
//! state graph and the idempotency key lives here, in the leaf crate both
//! sides already depend on — exactly where [`OrderSubmission`](crate::OrderSubmission)
//! and [`StructuredOrderError`](crate::StructuredOrderError) live, and for the
//! same reason.
//!
//! # The documented transition graph
//!
//! The nine [`OrderState`] values and their legal transitions are the
//! authoritative graph. [`OrderState::allowed_next`] is the single in-code
//! definition; the machine-readable mirror in
//! `architecture/runtime_services.json#order_lifecycle_contract.transitions`
//! is pinned against it by `tools/order_lifecycle_check.py`, so the
//! documentation and the code cannot drift.
//!
//! * `NEW → {PENDING_SUBMIT, REJECTED}` — the engine either durably commits the
//!   intent and begins submission, or pre-submit validation fails before any
//!   broker is contacted.
//! * `PENDING_SUBMIT → {ACKED, REJECTED, PARTIALLY_FILLED, FILLED, CANCEL_PENDING,
//!   EXPIRED}` — the broker acknowledges or rejects the order; but IB callbacks
//!   are not strictly ordered, so **pre-ack races** are modelled too: a fill
//!   (partial or full), a cancel request, or a TIF expiry that lands before the
//!   acknowledgement is honoured rather than rejected (rejecting it would
//!   otherwise lose a cancel the operator requested or a fill the broker
//!   reported, diverging the lifecycle from broker reality).
//! * `ACKED → {PARTIALLY_FILLED, FILLED, CANCEL_PENDING, REJECTED, EXPIRED}` — a
//!   working order can fill (in part or full), have a cancel requested, be
//!   **rejected post-ack** by the broker (a risk / compliance rejection after
//!   acknowledgement — otherwise the order would be left falsely active), or
//!   reach its time-in-force (DAY close / GTD).
//! * `PARTIALLY_FILLED → {PARTIALLY_FILLED, FILLED, CANCEL_PENDING, CANCELLED,
//!   EXPIRED}` — re-entrant: each additional partial execution re-enters the
//!   state; the remainder can still complete, have a cancel requested, be
//!   **cancelled directly** (a cancel-acknowledgement that lands after a partial
//!   fill raced an in-flight cancel — the remainder is cancelled with the
//!   partial fills retained), or expire.
//! * `CANCEL_PENDING → {CANCELLED, FILLED, PARTIALLY_FILLED, EXPIRED}` — the
//!   cancel is confirmed, **or a fill races the in-flight cancel** and the
//!   order completes (in part or full) before the cancel takes, or it expires.
//!   A partial fill here lands in `PARTIALLY_FILLED`, which can still reach
//!   `CANCELLED` when the cancel of the remainder is acknowledged.
//! * `FILLED`, `CANCELLED`, `REJECTED`, `EXPIRED` are terminal: no outgoing
//!   transition. A terminal order can never be resurrected.
//!
//! # Idempotency key (the AC's spine)
//!
//! Each order is identified by an [`OrderKey`] = the submitting
//! [`StrategyId`](crate::StrategyId) **plus** the strategy-supplied
//! [`ClientCorrelationId`]. Keying by the pair — not the correlation id alone —
//! is deliberate: the platform runs one live plus up to ~30 paper strategies
//! concurrently and each supplies its *own* client ids, so two strategies that
//! happen to choose the same local id must not collide. The correlation id is
//! **client-assigned** (not engine-generated), so a strategy can *reproduce* its
//! id deterministically across a restart — that is the property the AC's "stable
//! across restarts" idempotency key relies on. Within a single process,
//! [`OrderLedger::submit`] rejects a duplicate `(strategy, correlation id)`
//! **idempotently** — the existing order is left untouched and the caller gets a
//! [`StructuredOrderError`](crate::StructuredOrderError) in the SRS-ERR-001
//! envelope, category
//! [`DuplicateClientCorrelationId`](crate::OrderErrorCategory::DuplicateClientCorrelationId).
//!
//! Scope it honestly: **recognising** a re-submitted id as a duplicate *across a
//! process restart* requires a durable lookup — the [`OrderLedger`] here is a
//! fresh in-memory map after construction, so cross-restart recognition is the
//! SRS-EXE-009 durable outbox's job (see *deferred* below). This slice provides
//! the stable key and the *within-process* recognition, not the durable store.
//!
//! # Two admission paths, one authority
//!
//! An order enters the ledger exactly once, through one of two entry points,
//! and **both register in this same ledger** — so the idempotency authority is
//! never bypassed:
//!
//! * a *fresh* order is admitted by [`OrderLedger::submit`];
//! * a cancel-replace *replacement* is admitted by
//!   [`OrderLedger::cancel_replace`] (it is registered held, then driven by
//!   [`OrderLedger::transition`]).
//!
//! After admission an order is never re-submitted — a redundant `submit` of an
//! already-tracked key is correctly rejected as a duplicate. Wiring these two
//! admission paths into the real live (`submit_live_order`) and paper
//! (`accept_order`) submission gates is the deferred runtime (SRS-EXE-001 /
//! SRS-SIM-001 + the orchestrator); this slice ships the shared authority both
//! gates will consult.
//!
//! # Cancel-replace (cancel-then-new, fail-safe against doubled exposure)
//!
//! [`OrderLedger::cancel_replace`] requests cancellation of the original (it
//! moves to `CANCEL_PENDING`) and registers a *new* order under a *new*
//! correlation id whose [`OrderLifecycle::replaces`] retains the original key
//! for audit. Safety properties (all tested):
//!
//! * the replacement is **held**: [`OrderLedger::transition`] refuses to move it
//!   to `PENDING_SUBMIT` until the original reaches `CANCELLED`, and a
//!   non-cancelled terminal original (it filled / expired / was rejected)
//!   auto-suppresses the held replacement to `REJECTED` — a replacement can
//!   never reach the broker while its original is still live or already filled;
//! * an original is cancel-replaced **at most once**, even if it bounces back to
//!   a cancellable state (`CANCEL_PENDING` → `PARTIALLY_FILLED`) — a second
//!   replacement would re-open doubled exposure;
//! * cancel-replace never mutates an order in place and never reuses the
//!   original id for the replacement.
//!
//! # What is real here vs deferred
//!
//! This slice is the **pure state machine + idempotency authority**: the graph,
//! the `(strategy, correlation id)` keying, idempotent duplicate rejection, and
//! cancel-then-new — all deterministic, dependency-free, and fully
//! unit/contract/domain tested. Deliberately deferred (see
//! `architecture/runtime_services.json#order_lifecycle_contract.deferred`):
//!
//! * **Durable persistence of the ledger across a *process* restart** — the
//!   SRS-EXE-009 durable outbox / SRS-EXE-005 state recovery (SyRS SYS-90,
//!   NFR-R3). The ledger here is in-memory; restart stability is guaranteed at
//!   the client-assigned *key* level, not yet at the *store* level.
//! * **Wiring the two admission paths into the real live/paper submission
//!   gates** so every `submit_live_order` / paper `accept_order` consults this
//!   ledger — the SRS-EXE-001 / SRS-SIM-001 runtime plus the orchestrator.
//! * **Driving transitions from real broker order events** (ACK / fill / cancel
//!   / reject callbacks) — the IB adapter (SRS-EXE-006) and the sim fill model
//!   (SRS-SIM-002).
//! * **The finer-grained broker-rejection edges** — `PARTIALLY_FILLED → REJECTED`
//!   (whether a partially-executed order can be rejected for its remainder) and
//!   the `CANCEL_PENDING` *cancel-rejected vs order-rejected* disambiguation (a
//!   rejected cancel returns the order to its working state; a rejected order is
//!   terminal). These opposite-meaning semantics are authoritatively defined by
//!   the IB adapter's event mapping (SRS-EXE-006 / SRS-SDK-004), so only the
//!   unambiguous `ACKED → REJECTED` post-ack rejection is modelled here; the rest
//!   is deferred to the adapter rather than guessed.
//!
//! So `feature_list.json` keeps SRS-EXE-008 at `passes: false`, and
//! `tools/order_lifecycle_check.py` reports an `SRS-EXE-008 SDK-SURFACE PASS`
//! (the contract half), never a full requirement pass.

use std::collections::HashMap;
use std::fmt;

use crate::{OrderErrorCategory, OrderSubmission, StrategyId, StructuredOrderError};

/// The nine order lifecycle states (SRS-EXE-008 acceptance criterion). The wire
/// strings ([`OrderState::as_str`]) are the stable cross-surface vocabulary
/// (Rust / Python / REST / WebSocket).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum OrderState {
    New,
    PendingSubmit,
    Acked,
    PartiallyFilled,
    Filled,
    CancelPending,
    Cancelled,
    Rejected,
    Expired,
}

impl OrderState {
    /// Stable wire string for this state (SyRS SYS-64 vocabulary alignment).
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::New => "NEW",
            Self::PendingSubmit => "PENDING_SUBMIT",
            Self::Acked => "ACKED",
            Self::PartiallyFilled => "PARTIALLY_FILLED",
            Self::Filled => "FILLED",
            Self::CancelPending => "CANCEL_PENDING",
            Self::Cancelled => "CANCELLED",
            Self::Rejected => "REJECTED",
            Self::Expired => "EXPIRED",
        }
    }

    /// `true` for the four terminal states (FILLED, CANCELLED, REJECTED,
    /// EXPIRED). A terminal order has no outgoing transition and can never be
    /// resurrected.
    pub const fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Filled | Self::Cancelled | Self::Rejected | Self::Expired
        )
    }

    /// The documented transition graph: the states this state may transition
    /// into. This is the single in-code source of truth; the JSON mirror in
    /// `order_lifecycle_contract.transitions` is pinned against it.
    pub const fn allowed_next(self) -> &'static [OrderState] {
        match self {
            Self::New => &[Self::PendingSubmit, Self::Rejected],
            Self::PendingSubmit => &[
                Self::Acked,
                Self::Rejected,
                Self::PartiallyFilled,
                Self::Filled,
                Self::CancelPending,
                Self::Expired,
            ],
            Self::Acked => &[
                Self::PartiallyFilled,
                Self::Filled,
                Self::CancelPending,
                Self::Rejected,
                Self::Expired,
            ],
            Self::PartiallyFilled => &[
                Self::PartiallyFilled,
                Self::Filled,
                Self::CancelPending,
                Self::Cancelled,
                Self::Expired,
            ],
            Self::CancelPending => &[
                Self::Cancelled,
                Self::Filled,
                Self::PartiallyFilled,
                Self::Expired,
            ],
            // Terminal states have no outgoing transition.
            Self::Filled | Self::Cancelled | Self::Rejected | Self::Expired => &[],
        }
    }

    /// Whether `next` is a legal transition from `self` per [`Self::allowed_next`].
    pub fn can_transition_to(self, next: OrderState) -> bool {
        self.allowed_next().contains(&next)
    }
}

impl fmt::Display for OrderState {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// Strategy-supplied client correlation ID (SRS-EXE-008). Client-assigned (never
/// engine-generated), so the strategy can reproduce the same id deterministically
/// across a restart. It is namespaced by the submitting strategy inside an
/// [`OrderKey`], so two strategies may safely choose the same local id. The
/// inner string is private so an id cannot be forged empty;
/// [`ClientCorrelationId::new`] fails closed on a blank value.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct ClientCorrelationId(String);

impl ClientCorrelationId {
    /// Build a correlation id, rejecting an empty / whitespace-only value (an
    /// idempotency key must be a real, non-blank identifier).
    pub fn new(value: impl Into<String>) -> Result<Self, OrderLifecycleError> {
        let value = value.into();
        if value.trim().is_empty() {
            return Err(OrderLifecycleError::EmptyCorrelationId);
        }
        Ok(Self(value))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for ClientCorrelationId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

/// The idempotency key for an order: the submitting [`StrategyId`] plus the
/// strategy-supplied [`ClientCorrelationId`]. Keying by the pair prevents
/// cross-strategy collision — two concurrently running strategies may each use
/// the same local correlation id without one's order being mistaken for the
/// other's duplicate.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct OrderKey {
    strategy_id: StrategyId,
    correlation_id: ClientCorrelationId,
}

impl OrderKey {
    pub fn new(strategy_id: StrategyId, correlation_id: ClientCorrelationId) -> Self {
        Self {
            strategy_id,
            correlation_id,
        }
    }

    pub fn strategy_id(&self) -> &StrategyId {
        &self.strategy_id
    }

    pub fn correlation_id(&self) -> &ClientCorrelationId {
        &self.correlation_id
    }
}

impl fmt::Display for OrderKey {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{}/{}",
            self.strategy_id.as_str(),
            self.correlation_id.as_str()
        )
    }
}

/// One order's lifecycle: its [`OrderKey`] (strategy + correlation id), the
/// order intent ([`OrderSubmission`] — symbol / quantity, available for routing
/// and audit), its current [`OrderState`], and — for a cancel-replace
/// replacement — the key of the order it replaced, retained for audit.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrderLifecycle {
    key: OrderKey,
    submission: OrderSubmission,
    state: OrderState,
    replaces: Option<OrderKey>,
}

impl OrderLifecycle {
    /// A freshly admitted order: state [`OrderState::New`], replacing nothing.
    pub fn new(key: OrderKey, submission: OrderSubmission) -> Self {
        Self {
            key,
            submission,
            state: OrderState::New,
            replaces: None,
        }
    }

    pub fn key(&self) -> &OrderKey {
        &self.key
    }

    /// The order intent (symbol / quantity) carried by this order — available
    /// for later routing and audit.
    pub fn submission(&self) -> &OrderSubmission {
        &self.submission
    }

    pub fn strategy_id(&self) -> &StrategyId {
        self.key.strategy_id()
    }

    pub fn correlation_id(&self) -> &ClientCorrelationId {
        self.key.correlation_id()
    }

    pub fn state(&self) -> OrderState {
        self.state
    }

    /// The key of the order this one replaced (a cancel-replace audit link), or
    /// `None` for an order that was submitted directly.
    pub fn replaces(&self) -> Option<&OrderKey> {
        self.replaces.as_ref()
    }

    /// Apply a transition, enforcing the documented graph. Returns the new
    /// state on success, or [`OrderLifecycleError::IllegalTransition`] if the
    /// edge is not in [`OrderState::allowed_next`] (the state is left
    /// unchanged).
    pub fn transition_to(&mut self, next: OrderState) -> Result<OrderState, OrderLifecycleError> {
        if self.state.can_transition_to(next) {
            self.state = next;
            Ok(self.state)
        } else {
            Err(OrderLifecycleError::IllegalTransition {
                from: self.state,
                to: next,
            })
        }
    }
}

/// The idempotency ledger: one [`OrderLifecycle`] per [`OrderKey`]. This is the
/// authority that makes the `(strategy, correlation id)` pair an *idempotency
/// key* — a duplicate submission is rejected without creating a second order.
#[derive(Debug, Default)]
pub struct OrderLedger {
    orders: HashMap<OrderKey, OrderLifecycle>,
}

impl OrderLedger {
    pub fn new() -> Self {
        Self::default()
    }

    /// Number of orders tracked.
    pub fn len(&self) -> usize {
        self.orders.len()
    }

    pub fn is_empty(&self) -> bool {
        self.orders.is_empty()
    }

    /// The lifecycle for `key`, if any.
    pub fn get(&self, key: &OrderKey) -> Option<&OrderLifecycle> {
        self.orders.get(key)
    }

    /// The current state of `key`, if it is tracked.
    pub fn state(&self, key: &OrderKey) -> Option<OrderState> {
        self.orders.get(key).map(OrderLifecycle::state)
    }

    /// Admit a *fresh* order under `(submission.strategy_id, correlation_id)`
    /// (state [`OrderState::New`]). Returns the order's [`OrderKey`] via the
    /// returned lifecycle.
    ///
    /// **Idempotency (SRS-EXE-008 / SRS-ERR-001):** if the key is already
    /// tracked, the order is *not* created twice — the existing order is left
    /// untouched and a [`StructuredOrderError`] is returned in the SRS-ERR-001
    /// envelope with category
    /// [`OrderErrorCategory::DuplicateClientCorrelationId`]. The `submission`
    /// supplies the strategy half of the key (so two strategies may reuse the
    /// same local id) and populates the error's `original_order`. The contract
    /// is identical for live and paper submissions.
    pub fn submit(
        &mut self,
        correlation_id: ClientCorrelationId,
        submission: &OrderSubmission,
    ) -> Result<&OrderLifecycle, StructuredOrderError> {
        let key = OrderKey::new(submission.strategy_id.clone(), correlation_id);
        if self.orders.contains_key(&key) {
            return Err(StructuredOrderError {
                category: OrderErrorCategory::DuplicateClientCorrelationId,
                error_type: "DuplicateClientCorrelationId".to_string(),
                message: format!(
                    "order submission rejected: {} is already tracked (idempotent \
                     duplicate-submission rejection — the existing order is unchanged)",
                    key
                ),
                original_order: submission.clone(),
            });
        }
        let lifecycle = OrderLifecycle::new(key.clone(), submission.clone());
        self.orders.insert(key.clone(), lifecycle);
        Ok(self.orders.get(&key).expect("order was just inserted"))
    }

    /// Apply a transition to the order tracked under `key`, enforcing both the
    /// per-order graph and the cross-order cancel-replace safety gate.
    ///
    /// Returns [`OrderLifecycleError::UnknownOrder`] if the key is not tracked,
    /// [`OrderLifecycleError::IllegalTransition`] if the edge is not in the
    /// documented graph, or
    /// [`OrderLifecycleError::ReplacementBlockedUntilOriginalCancelled`] if this
    /// is a cancel-replace replacement being moved to [`OrderState::PendingSubmit`]
    /// before its original reached [`OrderState::Cancelled`].
    ///
    /// **Cancel-replace safety (no doubled exposure).** A replacement order
    /// (one whose [`OrderLifecycle::replaces`] is set) may not go live until the
    /// original it replaces is confirmed `CANCELLED` — otherwise the original
    /// (still resting in `CANCEL_PENDING`, which can still fill) and the
    /// replacement could *both* reach the broker. Conversely, when an order that
    /// has a held replacement reaches a non-cancelled terminal (it `FILLED`,
    /// `EXPIRED`, or was `REJECTED` instead of cancelling), the held replacement
    /// is auto-suppressed to `REJECTED`.
    pub fn transition(
        &mut self,
        key: &OrderKey,
        next: OrderState,
    ) -> Result<OrderState, OrderLifecycleError> {
        let (current_state, replaces) = match self.orders.get(key) {
            Some(order) => (order.state, order.replaces.clone()),
            None => return Err(OrderLifecycleError::UnknownOrder(key.clone())),
        };
        // Per-order graph legality is checked FIRST, so an illegal edge (e.g. an
        // already-terminal replacement) reports IllegalTransition rather than the
        // cross-order gate below.
        if !current_state.can_transition_to(next) {
            return Err(OrderLifecycleError::IllegalTransition {
                from: current_state,
                to: next,
            });
        }
        // Cancel-replace gate: a held replacement may only go live once the
        // original it replaces is confirmed CANCELLED.
        if next == OrderState::PendingSubmit {
            if let Some(original_key) = &replaces {
                match self.orders.get(original_key) {
                    None => return Err(OrderLifecycleError::UnknownOrder(original_key.clone())),
                    Some(original) if original.state != OrderState::Cancelled => {
                        return Err(
                            OrderLifecycleError::ReplacementBlockedUntilOriginalCancelled {
                                replacement: key.clone(),
                                original: original_key.clone(),
                                original_state: original.state,
                            },
                        );
                    }
                    Some(_) => {}
                }
            }
        }
        let new_state = self
            .orders
            .get_mut(key)
            .expect("presence checked above")
            .transition_to(next)
            .expect("legality pre-checked above");
        // Auto-suppress a held replacement if its original ended anywhere other
        // than CANCELLED (it filled / expired / was rejected) — never let a
        // replacement act on top of the original's outcome.
        if new_state.is_terminal() && new_state != OrderState::Cancelled {
            let held_replacement = self
                .orders
                .iter()
                .find(|(_, order)| {
                    order.replaces.as_ref() == Some(key) && !order.state.is_terminal()
                })
                .map(|(id, _)| id.clone());
            if let Some(replacement_key) = held_replacement {
                self.orders
                    .get_mut(&replacement_key)
                    .expect("just located")
                    .transition_to(OrderState::Rejected)
                    .expect("a held replacement is NEW, and NEW -> REJECTED is legal");
            }
        }
        Ok(new_state)
    }

    /// Cancel-replace as **cancel-then-new** (SRS-EXE-008): request cancellation
    /// of the order under `original` (it moves to [`OrderState::CancelPending`])
    /// and register a *new* order under the same strategy with
    /// `replacement_correlation_id`, whose [`OrderLifecycle::replaces`] retains
    /// `original` for audit. Returns the replacement.
    ///
    /// Refused (the ledger is left unchanged) when:
    /// * `original` is not tracked ([`OrderLifecycleError::UnknownOrder`]);
    /// * the original already has a replacement
    ///   ([`OrderLifecycleError::OriginalAlreadyReplaced`]) — an original may be
    ///   cancel-replaced **at most once**, even if it bounces back to a
    ///   cancellable state (`CANCEL_PENDING` → `PARTIALLY_FILLED`);
    /// * the replacement belongs to a different strategy than the original
    ///   ([`OrderLifecycleError::ReplacementStrategyMismatch`]) — a strategy may
    ///   only replace its own order;
    /// * the original is not in a cancellable (working) state
    ///   ([`OrderLifecycleError::IllegalTransition`]);
    /// * the replacement reuses the original correlation id
    ///   ([`OrderLifecycleError::ReplacementReusesOriginalId`]);
    /// * the replacement key is already tracked
    ///   ([`OrderLifecycleError::DuplicateReplacementId`]).
    ///
    /// `replacement` carries the *new* order intent (symbol / quantity); it is
    /// retained on the replacement [`OrderLifecycle`] so the new intent is
    /// available for routing and audit after the original is cancelled.
    pub fn cancel_replace(
        &mut self,
        original: &OrderKey,
        replacement: &OrderSubmission,
        replacement_correlation_id: ClientCorrelationId,
    ) -> Result<&OrderLifecycle, OrderLifecycleError> {
        let original_state = match self.orders.get(original) {
            Some(order) => order.state,
            None => return Err(OrderLifecycleError::UnknownOrder(original.clone())),
        };
        // A strategy may only replace its OWN order.
        if replacement.strategy_id != *original.strategy_id() {
            return Err(OrderLifecycleError::ReplacementStrategyMismatch {
                original: original.strategy_id().clone(),
                replacement: replacement.strategy_id.clone(),
            });
        }
        // At most one replacement per original, ever — a second would let two
        // held replacements both pass the cancel-replace gate once the original
        // reaches CANCELLED (doubled exposure).
        if self
            .orders
            .values()
            .any(|order| order.replaces.as_ref() == Some(original))
        {
            return Err(OrderLifecycleError::OriginalAlreadyReplaced(
                original.clone(),
            ));
        }
        if !original_state.can_transition_to(OrderState::CancelPending) {
            return Err(OrderLifecycleError::IllegalTransition {
                from: original_state,
                to: OrderState::CancelPending,
            });
        }
        let replacement_key =
            OrderKey::new(original.strategy_id().clone(), replacement_correlation_id);
        if replacement_key == *original {
            return Err(OrderLifecycleError::ReplacementReusesOriginalId(
                replacement_key,
            ));
        }
        if self.orders.contains_key(&replacement_key) {
            return Err(OrderLifecycleError::DuplicateReplacementId(replacement_key));
        }
        // cancel: request cancellation of the original (validated above).
        self.orders
            .get_mut(original)
            .expect("original presence checked above")
            .transition_to(OrderState::CancelPending)
            .expect("transition to CancelPending validated above");
        // new: a fresh held order that retains the original key for audit and
        // the new order intent for routing.
        let replacement_order = OrderLifecycle {
            key: replacement_key.clone(),
            submission: replacement.clone(),
            state: OrderState::New,
            replaces: Some(original.clone()),
        };
        self.orders
            .insert(replacement_key.clone(), replacement_order);
        Ok(self
            .orders
            .get(&replacement_key)
            .expect("replacement was just inserted"))
    }
}

/// Errors raised by the order lifecycle machine for *internal* invariant
/// violations. The idempotent duplicate-submission rejection is reported
/// instead as a [`StructuredOrderError`] (SRS-ERR-001), the contract the
/// strategy-facing API requires.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OrderLifecycleError {
    /// A correlation id was empty / whitespace-only.
    EmptyCorrelationId,
    /// A transition not present in the documented graph was attempted.
    IllegalTransition { from: OrderState, to: OrderState },
    /// A transition / cancel-replace targeted an untracked order key.
    UnknownOrder(OrderKey),
    /// A cancel-replace replacement reused the original key.
    ReplacementReusesOriginalId(OrderKey),
    /// A cancel-replace replacement key is already tracked.
    DuplicateReplacementId(OrderKey),
    /// A cancel-replace was attempted on an original that already has a
    /// replacement (an original may be cancel-replaced at most once).
    OriginalAlreadyReplaced(OrderKey),
    /// A cancel-replace replacement belongs to a different strategy than the
    /// original (a strategy may only replace its own order).
    ReplacementStrategyMismatch {
        original: StrategyId,
        replacement: StrategyId,
    },
    /// A cancel-replace replacement was moved toward submission before the
    /// original it replaces reached `CANCELLED` (the cancel-replace safety gate
    /// that prevents the original and the replacement both reaching the broker).
    ReplacementBlockedUntilOriginalCancelled {
        replacement: OrderKey,
        original: OrderKey,
        original_state: OrderState,
    },
}

impl fmt::Display for OrderLifecycleError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyCorrelationId => {
                formatter.write_str("client correlation id must be a non-empty identifier")
            }
            Self::IllegalTransition { from, to } => write!(
                formatter,
                "illegal order transition {} -> {} (not in the documented graph)",
                from.as_str(),
                to.as_str()
            ),
            Self::UnknownOrder(key) => {
                write!(formatter, "no order tracked for {key}")
            }
            Self::ReplacementReusesOriginalId(key) => write!(
                formatter,
                "cancel-replace replacement reuses the original key {key} — a \
                 replacement must be a new order",
            ),
            Self::DuplicateReplacementId(key) => {
                write!(
                    formatter,
                    "cancel-replace replacement key {key} is already tracked"
                )
            }
            Self::OriginalAlreadyReplaced(key) => write!(
                formatter,
                "order {key} already has a replacement — an original may be \
                 cancel-replaced at most once",
            ),
            Self::ReplacementStrategyMismatch {
                original,
                replacement,
            } => write!(
                formatter,
                "cancel-replace replacement strategy {:?} does not match the \
                 original's strategy {:?} — a strategy may only replace its own order",
                replacement.as_str(),
                original.as_str()
            ),
            Self::ReplacementBlockedUntilOriginalCancelled {
                replacement,
                original,
                original_state,
            } => write!(
                formatter,
                "replacement {} cannot go live: its original {} is {} (must be \
                 CANCELLED before a cancel-replace replacement may be submitted)",
                replacement,
                original,
                original_state.as_str()
            ),
        }
    }
}

impl std::error::Error for OrderLifecycleError {}

#[cfg(test)]
mod tests {
    use super::*;

    fn corr(value: &str) -> ClientCorrelationId {
        ClientCorrelationId::new(value).expect("non-empty test id")
    }

    const STRAT: &str = "strat-1";

    fn key(value: &str) -> OrderKey {
        OrderKey::new(StrategyId::new(STRAT), corr(value))
    }

    fn submission() -> OrderSubmission {
        OrderSubmission {
            strategy_id: StrategyId::new(STRAT),
            symbol: "AAPL".to_string(),
            quantity: 10,
        }
    }

    fn acked(ledger: &mut OrderLedger, id: &str) {
        ledger.submit(corr(id), &submission()).unwrap();
        ledger
            .transition(&key(id), OrderState::PendingSubmit)
            .unwrap();
        ledger.transition(&key(id), OrderState::Acked).unwrap();
    }

    #[test]
    fn nine_states_carry_stable_wire_strings() {
        let pairs = [
            (OrderState::New, "NEW"),
            (OrderState::PendingSubmit, "PENDING_SUBMIT"),
            (OrderState::Acked, "ACKED"),
            (OrderState::PartiallyFilled, "PARTIALLY_FILLED"),
            (OrderState::Filled, "FILLED"),
            (OrderState::CancelPending, "CANCEL_PENDING"),
            (OrderState::Cancelled, "CANCELLED"),
            (OrderState::Rejected, "REJECTED"),
            (OrderState::Expired, "EXPIRED"),
        ];
        for (state, wire) in pairs {
            assert_eq!(state.as_str(), wire);
        }
    }

    #[test]
    fn terminal_states_have_no_outgoing_transitions() {
        for terminal in [
            OrderState::Filled,
            OrderState::Cancelled,
            OrderState::Rejected,
            OrderState::Expired,
        ] {
            assert!(terminal.is_terminal());
            assert!(
                terminal.allowed_next().is_empty(),
                "terminal {terminal} must have no outgoing transition"
            );
            for any in [
                OrderState::New,
                OrderState::Acked,
                OrderState::Filled,
                OrderState::Cancelled,
            ] {
                assert!(
                    !terminal.can_transition_to(any),
                    "terminal {terminal} must not transition to {any}"
                );
            }
        }
    }

    #[test]
    fn non_terminal_states_are_not_terminal() {
        for state in [
            OrderState::New,
            OrderState::PendingSubmit,
            OrderState::Acked,
            OrderState::PartiallyFilled,
            OrderState::CancelPending,
        ] {
            assert!(!state.is_terminal());
            assert!(!state.allowed_next().is_empty());
        }
    }

    #[test]
    fn happy_path_new_to_filled_is_legal() {
        let mut order = OrderLifecycle::new(key("c-1"), submission());
        assert_eq!(order.state(), OrderState::New);
        assert_eq!(
            order.transition_to(OrderState::PendingSubmit).unwrap(),
            OrderState::PendingSubmit
        );
        assert_eq!(
            order.transition_to(OrderState::Acked).unwrap(),
            OrderState::Acked
        );
        assert_eq!(
            order.transition_to(OrderState::PartiallyFilled).unwrap(),
            OrderState::PartiallyFilled
        );
        // re-entrant partial fill
        assert_eq!(
            order.transition_to(OrderState::PartiallyFilled).unwrap(),
            OrderState::PartiallyFilled
        );
        assert_eq!(
            order.transition_to(OrderState::Filled).unwrap(),
            OrderState::Filled
        );
        assert!(order.state().is_terminal());
    }

    #[test]
    fn illegal_transition_is_refused_and_leaves_state_unchanged() {
        let mut order = OrderLifecycle::new(key("c-2"), submission());
        // NEW cannot jump straight to FILLED.
        let err = order.transition_to(OrderState::Filled).unwrap_err();
        assert_eq!(
            err,
            OrderLifecycleError::IllegalTransition {
                from: OrderState::New,
                to: OrderState::Filled
            }
        );
        assert_eq!(order.state(), OrderState::New);
    }

    #[test]
    fn cancel_pending_can_be_raced_by_a_fill() {
        let mut order = OrderLifecycle::new(key("c-3"), submission());
        order.transition_to(OrderState::PendingSubmit).unwrap();
        order.transition_to(OrderState::Acked).unwrap();
        order.transition_to(OrderState::CancelPending).unwrap();
        // a fill arrives before the cancel takes
        assert_eq!(
            order.transition_to(OrderState::Filled).unwrap(),
            OrderState::Filled
        );
    }

    #[test]
    fn partial_fill_racing_a_pending_cancel_can_still_be_cancelled() {
        // CANCEL_PENDING -> PARTIALLY_FILLED (a fill races the cancel) -> CANCELLED
        // (the cancel of the remainder is acknowledged). The pending cancel must
        // not be lost by the racing partial fill.
        let mut order = OrderLifecycle::new(key("c-4"), submission());
        order.transition_to(OrderState::PendingSubmit).unwrap();
        order.transition_to(OrderState::Acked).unwrap();
        order.transition_to(OrderState::CancelPending).unwrap();
        order.transition_to(OrderState::PartiallyFilled).unwrap();
        assert_eq!(
            order.transition_to(OrderState::Cancelled).unwrap(),
            OrderState::Cancelled
        );
    }

    #[test]
    fn empty_correlation_id_is_rejected() {
        assert_eq!(
            ClientCorrelationId::new("").unwrap_err(),
            OrderLifecycleError::EmptyCorrelationId
        );
        assert_eq!(
            ClientCorrelationId::new("   ").unwrap_err(),
            OrderLifecycleError::EmptyCorrelationId
        );
        assert!(ClientCorrelationId::new("c-ok").is_ok());
    }

    #[test]
    fn duplicate_submission_is_rejected_idempotently() {
        let mut ledger = OrderLedger::new();
        let order_submission = submission();
        assert_eq!(
            ledger
                .submit(corr("dup"), &order_submission)
                .unwrap()
                .state(),
            OrderState::New
        );
        // advance the first order so we can prove the duplicate does not reset it
        ledger
            .transition(&key("dup"), OrderState::PendingSubmit)
            .unwrap();
        ledger.transition(&key("dup"), OrderState::Acked).unwrap();

        for _ in 0..3 {
            let err = ledger.submit(corr("dup"), &order_submission).unwrap_err();
            assert_eq!(
                err.category,
                OrderErrorCategory::DuplicateClientCorrelationId
            );
            assert_eq!(err.category.as_str(), "DUPLICATE_CLIENT_CORRELATION_ID");
            assert_eq!(err.original_order, order_submission);
            // idempotent: the existing order is untouched and no second order created
            assert_eq!(ledger.state(&key("dup")).unwrap(), OrderState::Acked);
            assert_eq!(ledger.len(), 1);
        }
    }

    #[test]
    fn the_same_correlation_id_under_different_strategies_does_not_collide() {
        let mut ledger = OrderLedger::new();
        let sub_a = OrderSubmission {
            strategy_id: StrategyId::new("strat-a"),
            symbol: "AAPL".to_string(),
            quantity: 1,
        };
        let sub_b = OrderSubmission {
            strategy_id: StrategyId::new("strat-b"),
            symbol: "AAPL".to_string(),
            quantity: 1,
        };
        // both strategies use the SAME local id "order-1"
        ledger.submit(corr("order-1"), &sub_a).unwrap();
        // strat-b's identical local id is a distinct order, not a duplicate
        assert!(ledger.submit(corr("order-1"), &sub_b).is_ok());
        assert_eq!(ledger.len(), 2);
        // and each is independently tracked
        let key_a = OrderKey::new(StrategyId::new("strat-a"), corr("order-1"));
        let key_b = OrderKey::new(StrategyId::new("strat-b"), corr("order-1"));
        assert_eq!(ledger.state(&key_a).unwrap(), OrderState::New);
        assert_eq!(ledger.state(&key_b).unwrap(), OrderState::New);
    }

    #[test]
    fn cancel_replace_is_cancel_then_new_retaining_original_id() {
        let mut ledger = OrderLedger::new();
        acked(&mut ledger, "orig");

        let replacement = ledger
            .cancel_replace(&key("orig"), &submission(), corr("repl"))
            .unwrap();
        assert_eq!(replacement.state(), OrderState::New);
        assert_eq!(replacement.correlation_id().as_str(), "repl");
        assert_eq!(replacement.replaces(), Some(&key("orig")));

        // cancel: the original moved to CANCEL_PENDING and is retained for audit
        assert_eq!(
            ledger.state(&key("orig")).unwrap(),
            OrderState::CancelPending
        );
        assert_eq!(ledger.len(), 2);
    }

    #[test]
    fn cancel_replace_of_a_non_cancellable_order_is_refused() {
        let mut ledger = OrderLedger::new();
        ledger.submit(corr("new-only"), &submission()).unwrap();
        // NEW cannot reach CANCEL_PENDING -> cancel-replace is illegal
        let err = ledger
            .cancel_replace(&key("new-only"), &submission(), corr("repl"))
            .unwrap_err();
        assert_eq!(
            err,
            OrderLifecycleError::IllegalTransition {
                from: OrderState::New,
                to: OrderState::CancelPending
            }
        );
        assert_eq!(ledger.len(), 1);
    }

    #[test]
    fn cancel_replace_of_a_terminal_order_is_refused() {
        let mut ledger = OrderLedger::new();
        ledger.submit(corr("done"), &submission()).unwrap();
        ledger
            .transition(&key("done"), OrderState::PendingSubmit)
            .unwrap();
        ledger
            .transition(&key("done"), OrderState::Rejected)
            .unwrap();
        let err = ledger
            .cancel_replace(&key("done"), &submission(), corr("repl"))
            .unwrap_err();
        assert_eq!(
            err,
            OrderLifecycleError::IllegalTransition {
                from: OrderState::Rejected,
                to: OrderState::CancelPending
            }
        );
    }

    #[test]
    fn cancel_replace_reusing_or_colliding_ids_is_refused() {
        let mut ledger = OrderLedger::new();
        acked(&mut ledger, "a");
        ledger.submit(corr("b"), &submission()).unwrap();

        // replacement reuses the original id
        assert_eq!(
            ledger
                .cancel_replace(&key("a"), &submission(), corr("a"))
                .unwrap_err(),
            OrderLifecycleError::ReplacementReusesOriginalId(key("a"))
        );
        // replacement collides with an existing order
        assert_eq!(
            ledger
                .cancel_replace(&key("a"), &submission(), corr("b"))
                .unwrap_err(),
            OrderLifecycleError::DuplicateReplacementId(key("b"))
        );
        // original untouched after refusals
        assert_eq!(ledger.state(&key("a")).unwrap(), OrderState::Acked);
    }

    #[test]
    fn transition_on_unknown_order_is_refused() {
        let mut ledger = OrderLedger::new();
        assert_eq!(
            ledger
                .transition(&key("ghost"), OrderState::Acked)
                .unwrap_err(),
            OrderLifecycleError::UnknownOrder(key("ghost"))
        );
    }

    #[test]
    fn replacement_is_blocked_until_the_original_is_cancelled() {
        let mut ledger = OrderLedger::new();
        acked(&mut ledger, "orig");
        ledger
            .cancel_replace(&key("orig"), &submission(), corr("repl"))
            .unwrap();

        // the replacement cannot go live while the original rests in CANCEL_PENDING
        let err = ledger
            .transition(&key("repl"), OrderState::PendingSubmit)
            .unwrap_err();
        assert_eq!(
            err,
            OrderLifecycleError::ReplacementBlockedUntilOriginalCancelled {
                replacement: key("repl"),
                original: key("orig"),
                original_state: OrderState::CancelPending,
            }
        );
        assert_eq!(ledger.state(&key("repl")).unwrap(), OrderState::New);

        // once the original is CANCELLED, the replacement may go live
        ledger
            .transition(&key("orig"), OrderState::Cancelled)
            .unwrap();
        assert_eq!(
            ledger
                .transition(&key("repl"), OrderState::PendingSubmit)
                .unwrap(),
            OrderState::PendingSubmit
        );
    }

    #[test]
    fn a_filled_original_auto_suppresses_its_held_replacement() {
        let mut ledger = OrderLedger::new();
        acked(&mut ledger, "orig");
        ledger
            .cancel_replace(&key("orig"), &submission(), corr("repl"))
            .unwrap();

        // the cancel loses the race: the original fully fills instead of cancelling
        ledger.transition(&key("orig"), OrderState::Filled).unwrap();

        // the held replacement is auto-suppressed so it can never double exposure
        assert_eq!(ledger.state(&key("repl")).unwrap(), OrderState::Rejected);
        // and it certainly cannot go live now
        assert!(matches!(
            ledger
                .transition(&key("repl"), OrderState::PendingSubmit)
                .unwrap_err(),
            OrderLifecycleError::IllegalTransition { .. }
        ));
    }

    #[test]
    fn an_original_can_be_cancel_replaced_at_most_once() {
        let mut ledger = OrderLedger::new();
        acked(&mut ledger, "orig");
        ledger
            .cancel_replace(&key("orig"), &submission(), corr("repl-1"))
            .unwrap();
        // a partial fill bounces the original back to a cancellable state
        ledger
            .transition(&key("orig"), OrderState::PartiallyFilled)
            .unwrap();
        // a second cancel-replace is refused (would create a second held
        // replacement that could doubled-expose once the original is CANCELLED)
        assert_eq!(
            ledger
                .cancel_replace(&key("orig"), &submission(), corr("repl-2"))
                .unwrap_err(),
            OrderLifecycleError::OriginalAlreadyReplaced(key("orig"))
        );
        assert!(ledger.get(&key("repl-2")).is_none());
        assert_eq!(ledger.len(), 2);
    }

    #[test]
    fn an_acked_order_can_be_rejected_post_ack() {
        // A broker rejection after acknowledgement (risk / compliance) must be
        // representable, else the order is left falsely active.
        let mut order = OrderLifecycle::new(key("pa-rej"), submission());
        order.transition_to(OrderState::PendingSubmit).unwrap();
        order.transition_to(OrderState::Acked).unwrap();
        assert_eq!(
            order.transition_to(OrderState::Rejected).unwrap(),
            OrderState::Rejected
        );
        assert!(order.state().is_terminal());
    }

    #[test]
    fn pending_submit_handles_pre_ack_races() {
        // IB callbacks are not strictly ordered: a fill / cancel / expiry can
        // land before the acknowledgement. Each must be honoured from PENDING_SUBMIT.
        for race in [
            OrderState::PartiallyFilled,
            OrderState::Filled,
            OrderState::CancelPending,
            OrderState::Expired,
        ] {
            let mut order = OrderLifecycle::new(key("pa"), submission());
            order.transition_to(OrderState::PendingSubmit).unwrap();
            assert_eq!(order.transition_to(race).unwrap(), race);
        }
    }

    #[test]
    fn cancel_replace_with_a_different_strategy_is_refused() {
        let mut ledger = OrderLedger::new();
        acked(&mut ledger, "orig");
        let foreign = OrderSubmission {
            strategy_id: StrategyId::new("other-strat"),
            symbol: "AAPL".to_string(),
            quantity: 10,
        };
        assert_eq!(
            ledger
                .cancel_replace(&key("orig"), &foreign, corr("repl"))
                .unwrap_err(),
            OrderLifecycleError::ReplacementStrategyMismatch {
                original: StrategyId::new(STRAT),
                replacement: StrategyId::new("other-strat"),
            }
        );
        // original untouched
        assert_eq!(ledger.state(&key("orig")).unwrap(), OrderState::Acked);
        assert_eq!(ledger.len(), 1);
    }

    #[test]
    fn orders_retain_their_intent_for_routing_and_audit() {
        let mut ledger = OrderLedger::new();
        let original = OrderSubmission {
            strategy_id: StrategyId::new(STRAT),
            symbol: "AAPL".to_string(),
            quantity: 100,
        };
        ledger.submit(corr("o"), &original).unwrap();
        ledger
            .transition(&key("o"), OrderState::PendingSubmit)
            .unwrap();
        ledger.transition(&key("o"), OrderState::Acked).unwrap();
        assert_eq!(ledger.get(&key("o")).unwrap().submission(), &original);

        // the replacement carries the NEW intent (a re-priced, smaller order)
        let replacement = OrderSubmission {
            strategy_id: StrategyId::new(STRAT),
            symbol: "AAPL".to_string(),
            quantity: 60,
        };
        ledger
            .cancel_replace(&key("o"), &replacement, corr("o-2"))
            .unwrap();
        assert_eq!(ledger.get(&key("o-2")).unwrap().submission(), &replacement);
        assert_eq!(ledger.get(&key("o-2")).unwrap().submission().quantity, 60);
    }
}
