//! SRS-DATA-020 — adjust live positions affected by corporate actions.
//!
//! When a corporate action becomes effective for a security with a live position,
//! this module recomputes that position's **quantity** and **average cost basis**
//! onto the post-action basis, remaps it to a successor security, or marks it
//! delisted — mirroring the resting-order sibling [`crate::corporate_action_orders`]
//! (SRS-DATA-019) but for positions rather than orders. Like that sibling it is a
//! PURE, total, fail-closed planner: it makes the DECISION over fixture positions
//! and a fixture corporate action; nothing here mutates a live account or performs
//! I/O. When an adjustment is not possible or not derivable — a non-positive split
//! factor, a fractional share count, an overflow, a dividend that would drive the
//! basis through zero, a merger with a cash leg, an invalid successor, or a
//! successor collision — it produces a [`PositionCorpActionOutcome::RequiresManualReview`]
//! carrying a structured reason and pages the operator.
//!
//! ## The money math is EXACT — there is no rounding
//!
//! Every transform is an exact integer operation on minor units, or it fails closed
//! to review. This is a deliberate strength: basis conservation is exact and
//! provable, and there is no rounding drift to reconcile against the historical /
//! paper basis (StRS SN-1.14).
//!
//!   * **Split `N`-for-`M`** (`numerator = N`, `denominator = M`): the quantity is
//!     scaled by `N / M` and MUST divide evenly — a fractional residual is broker
//!     cash-in-lieu at a price this planner does not have, so it is sent to review,
//!     never truncated. The total `cost_basis_minor` is **invariant**: a split never
//!     changes the total amount invested, it only re-expresses the per-unit average
//!     (which `cost_basis / quantity` re-derives automatically). A `1`-for-`1` split
//!     is a no-op.
//!   * **Cash dividend** (`amount_minor` per share): the quantity is unchanged and
//!     the basis is reduced ADDITIVELY by the actual dividend cash the position
//!     pays or receives, `cost_basis' = cost_basis − amount_minor · quantity`. A
//!     long receives `amount · q` and its basis falls; a short pays it and its
//!     proceeds-basis magnitude falls. This conserves value exactly (absolute P&L
//!     is invariant across the ex-date) and needs no cash side-channel — a
//!     multiplicative ratio factor from the share price, correct for a per-share
//!     series, does NOT transfer to a total-dollar basis and would leak P&L, so it
//!     is not used.
//!   * **Merger** (stock-for-stock): the position is remapped to the successor at
//!     `quantity · N / M` (exact) with the basis carried over intact. Any cash leg
//!     is a partial disposition needing a realized-P&L booking not derivable from
//!     the terms alone, so it is sent to review.
//!   * **Symbol change**: a pure relabel to the successor — quantity, basis, and
//!     status unchanged.
//!   * **Delisting**: the position is marked [`PositionStatus::Delisted`] with its
//!     quantity and basis frozen, and the operator is paged.
//!
//! ## Signed quantities and signed basis (the position-vs-order delta)
//!
//! A position is signed (`> 0` long, `< 0` short) and so is its `cost_basis_minor`
//! (positive for a long, negative for a short) — the source of truth for average
//! cost, exactly as [`atp_simulation`'s `VirtualPosition`]. The scaling helpers here
//! therefore never reject a negative result (a reverse split of a short must yield a
//! negative quantity); only overflow and a non-integral share count are failures.
//! A transform never flips `sign(quantity)`, and the average cost stays positive
//! because basis and quantity share a sign — a constructed position with a
//! sign-inconsistent basis (a negative average cost) is rejected.
//!
//! ## One position per canonical symbol (the collision guard)
//!
//! Unlike orders, positions are unique per canonical symbol. A merger or symbol
//! change that would remap a position onto a symbol another position already holds
//! is sent to review ([`PositionReviewReason::SuccessorCollision`]) rather than
//! silently producing two positions for one symbol — merging two cost bases is a
//! real operation the runtime performs, not something this planner fabricates.
//!
//! ## Neutral emission (execution stays independent of the notification crate)
//!
//! A delisting and every review page route through the [`PositionCorpActionAlertSink`]
//! port, exactly like [`crate::KillSwitchOperatorAlertSink`] /
//! [`crate::corporate_action_orders::RestingOrderCorpActionAlertSink`]: `atp-execution`
//! names no notification transport and never depends on `atp-notification`. A routine
//! adjust / remap does not page the operator (it does surface a
//! [`PositionChangeEvent`] for the running strategy — a distinct audience). The
//! concrete email/SMS binding and the `deliver_order_event` callback are the deferred
//! composition-root wiring (SRS-NOTIF-001 / SRS-SDK-004).
//!
//! ## Scope (this is the deterministic core; the live path is deferred)
//!
//! The corporate-action facts and the positions here are inputs (fixtures / CLI).
//! The production feed of live positions **carrying cost basis** is the brokerage
//! adapter account/positions sync (SRS-EXE-006 / API-5, which currently fails closed
//! `LIVE_WIRE_PROTOCOL_PENDING`) — today's live [`crate::LiveExecutionState`] tracks
//! positions as a bare symbol -> signed share count with no basis, so there is no
//! live basis to adjust yet. Real operator email/SMS is SRS-NOTIF-001; live callback
//! delivery is SRS-SDK-004. So SRS-DATA-020 lands `serialized` (`passes:false`) until
//! that end-to-end evidence exists.

// Self-contained: this planner defines its own position + corporate-action value
// types and imports nothing from atp_types — the composition root maps the data
// layer's surfaced events onto these inputs at the (deferred) wiring seam.

/// Whether a live position is still tradeable or has been delisted. A delisted
/// position is terminal: its quantity and basis are frozen and no further corporate
/// action reaches it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PositionStatus {
    /// Tradeable — the normal state.
    Active,
    /// The security was delisted; the position is frozen and flagged for the operator.
    Delisted,
}

/// A failure to construct a [`LivePosition`] from raw fields — a blank symbol, a
/// flat (zero) quantity, or a basis whose sign disagrees with the quantity (which
/// would imply a negative average cost).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LivePositionError {
    pub reason: String,
}

impl LivePositionError {
    pub fn new(reason: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
        }
    }
}

/// One live position in a single security: a signed `quantity` (`> 0` long, `< 0`
/// short) and a signed `cost_basis_minor` (positive for a long, negative for a
/// short — the source of truth for average cost), keyed by a canonical symbol and
/// tagged with a [`PositionStatus`]. Mirrors the fields of `atp_simulation`'s
/// `VirtualPosition`; every money figure is an integer minor unit.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LivePosition {
    symbol: String,
    quantity: i64,
    cost_basis_minor: i128,
    status: PositionStatus,
}

impl LivePosition {
    /// A live (Active) position. Fails closed on a blank symbol, a flat (zero)
    /// quantity — an open position is by definition non-flat, which also guarantees
    /// [`average_cost_minor`](Self::average_cost_minor) never divides by zero — or a
    /// `cost_basis_minor` whose sign disagrees with `quantity` (a negative average
    /// cost is economically impossible). A zero basis with a non-zero quantity is
    /// allowed (a zero-cost spinoff). The symbol is canonicalized (trim + upper-case)
    /// so one security's position is not split across casing / whitespace aliases.
    pub fn new(
        symbol: impl Into<String>,
        quantity: i64,
        cost_basis_minor: i128,
    ) -> Result<Self, LivePositionError> {
        Self::build(symbol, quantity, cost_basis_minor, PositionStatus::Active)
    }

    /// An already-delisted position — the same validation as [`new`](Self::new),
    /// used to model a position that was delisted by an earlier action (terminal).
    pub fn delisted(
        symbol: impl Into<String>,
        quantity: i64,
        cost_basis_minor: i128,
    ) -> Result<Self, LivePositionError> {
        Self::build(symbol, quantity, cost_basis_minor, PositionStatus::Delisted)
    }

    fn build(
        symbol: impl Into<String>,
        quantity: i64,
        cost_basis_minor: i128,
        status: PositionStatus,
    ) -> Result<Self, LivePositionError> {
        let symbol = canonical_symbol(&symbol.into());
        if symbol.is_empty() {
            return Err(LivePositionError::new("empty position symbol"));
        }
        if quantity == 0 {
            return Err(LivePositionError::new("flat (zero-quantity) open position"));
        }
        if cost_basis_minor != 0 && cost_basis_minor.signum() != i128::from(quantity).signum() {
            return Err(LivePositionError::new(
                "cost basis sign disagrees with quantity (negative average cost)",
            ));
        }
        Ok(Self {
            symbol,
            quantity,
            cost_basis_minor,
            status,
        })
    }

    /// The canonical symbol (trim + upper-case) this position is held under.
    pub fn symbol(&self) -> &str {
        &self.symbol
    }

    /// The signed quantity held (`> 0` long, `< 0` short).
    pub fn quantity(&self) -> i64 {
        self.quantity
    }

    /// The signed total cost basis in minor units — the source of truth for average
    /// cost (positive for a long, negative for a short).
    pub fn cost_basis_minor(&self) -> i128 {
        self.cost_basis_minor
    }

    /// The position's status (Active or Delisted).
    pub fn status(&self) -> PositionStatus {
        self.status
    }

    /// Whether the position has been delisted (terminal).
    pub fn is_delisted(&self) -> bool {
        matches!(self.status, PositionStatus::Delisted)
    }

    /// Average cost per unit in minor units (`cost_basis / quantity`, truncated
    /// toward zero), or `None` when flat. Positive for both longs and shorts because
    /// the basis and quantity share a sign. A DERIVED, truncated view of the exact
    /// signed `cost_basis_minor` (the source of truth) — mirrors
    /// `VirtualPosition::average_cost_minor`.
    pub fn average_cost_minor(&self) -> Option<i128> {
        if self.quantity == 0 {
            None
        } else {
            Some(self.cost_basis_minor / i128::from(self.quantity))
        }
    }

    /// This position frozen as delisted (quantity + basis unchanged). Internal — the
    /// only way a position becomes delisted is a [`PositionCorporateActionKind::Delisting`].
    fn frozen_delisted(&self) -> Self {
        Self {
            status: PositionStatus::Delisted,
            ..self.clone()
        }
    }

    /// This position rebased to a new quantity + basis, staying at its own symbol and
    /// Active. Internal — the invariants (sign agreement, non-flat) are guaranteed by
    /// the caller's exact arithmetic, so no re-validation is needed.
    fn rebased(&self, quantity: i64, cost_basis_minor: i128) -> Self {
        Self {
            symbol: self.symbol.clone(),
            quantity,
            cost_basis_minor,
            status: PositionStatus::Active,
        }
    }

    /// This position remapped to `successor` at a new quantity + basis, Active.
    /// Internal — `successor` is already canonical and the arithmetic exact.
    fn remapped(&self, successor: String, quantity: i64, cost_basis_minor: i128) -> Self {
        Self {
            symbol: successor,
            quantity,
            cost_basis_minor,
            status: PositionStatus::Active,
        }
    }
}

/// A corporate action bound to the symbol it affects. Publicly constructible; the
/// factors and dividend terms are re-validated at plan time, so an invalid input can
/// never divide-by-zero or silently miscompute.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PositionCorporateAction {
    /// The affected security. A position is transformed only when its symbol matches
    /// this (canonically) — a mixed book can never cross-contaminate.
    pub symbol: String,
    /// What happened to the security.
    pub kind: PositionCorporateActionKind,
}

impl PositionCorporateAction {
    /// An `N`-for-`M` split (forward when `N > M`, reverse when `N < M`).
    pub fn split(symbol: impl Into<String>, numerator: i64, denominator: i64) -> Self {
        Self {
            symbol: symbol.into(),
            kind: PositionCorporateActionKind::Split {
                numerator,
                denominator,
            },
        }
    }

    /// A cash dividend of `amount_minor` per share, referenced against `prev_close_minor`
    /// (used only for the fail-closed sanity guard, not the additive basis math).
    pub fn dividend(symbol: impl Into<String>, amount_minor: i64, prev_close_minor: i64) -> Self {
        Self {
            symbol: symbol.into(),
            kind: PositionCorporateActionKind::Dividend {
                amount_minor,
                prev_close_minor,
            },
        }
    }

    /// A merger into `successor` at `numerator` successor shares per `denominator`
    /// acquired shares, with `cash_per_share_minor` cash per acquired share (`0` for a
    /// pure stock-for-stock merger, which is the only case this planner adjusts).
    pub fn merger(
        symbol: impl Into<String>,
        successor: impl Into<String>,
        numerator: i64,
        denominator: i64,
        cash_per_share_minor: i64,
    ) -> Self {
        Self {
            symbol: symbol.into(),
            kind: PositionCorporateActionKind::Merger {
                successor: successor.into(),
                numerator,
                denominator,
                cash_per_share_minor,
            },
        }
    }

    /// A symbol change (relabel) to `successor`.
    pub fn symbol_change(symbol: impl Into<String>, successor: impl Into<String>) -> Self {
        Self {
            symbol: symbol.into(),
            kind: PositionCorporateActionKind::SymbolChange {
                successor: successor.into(),
            },
        }
    }

    /// A delisting — the position is frozen and flagged for the operator.
    pub fn delisting(symbol: impl Into<String>) -> Self {
        Self {
            symbol: symbol.into(),
            kind: PositionCorporateActionKind::Delisting,
        }
    }
}

/// The class of corporate action a live position can be affected by. Splits and cash
/// dividends adjust quantity and/or basis; mergers and symbol changes remap to a
/// successor; a delisting freezes the position. (A *stock* dividend is economically a
/// split — the composition root maps it to [`Split`](Self::Split), not
/// [`Dividend`](Self::Dividend).)
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PositionCorporateActionKind {
    /// An `N`-for-`M` split. Both factors are validated `> 0` at plan time.
    Split { numerator: i64, denominator: i64 },
    /// A cash dividend of `amount_minor` per share; `prev_close_minor` is the sanity
    /// reference (a dividend `>=` it is anomalous).
    Dividend {
        amount_minor: i64,
        prev_close_minor: i64,
    },
    /// A merger into `successor`. Only a pure stock-for-stock merger
    /// (`cash_per_share_minor == 0`, `numerator > 0`) is adjusted; any cash leg is
    /// sent to review.
    Merger {
        successor: String,
        numerator: i64,
        denominator: i64,
        cash_per_share_minor: i64,
    },
    /// A relabel to `successor` (quantity + basis unchanged).
    SymbolChange { successor: String },
    /// The security was delisted.
    Delisting,
}

/// Why a corporate action could not be applied to a position and was flagged for the
/// operator instead of adjusted / remapped — the structured, source-neutral reason the
/// operator alert carries.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PositionReviewReason {
    /// A split / merger carried a non-positive numerator or denominator (rejected
    /// before any arithmetic).
    NonPositiveFactor { numerator: i64, denominator: i64 },
    /// A (reverse) split or merger ratio did not divide the share count into a whole
    /// number — the fractional residual is cash-in-lieu, so the position is flagged
    /// rather than silently truncated.
    QuantityNotIntegral {
        before: i64,
        numerator: i64,
        denominator: i64,
    },
    /// An adjusted quantity or basis overflowed (never wraps).
    Overflow { context: &'static str },
    /// A dividend term is invalid — a non-positive `amount` or `prev_close`.
    InvalidDividendTerm {
        amount_minor: i64,
        prev_close_minor: i64,
    },
    /// A dividend that would drive the basis through zero — a per-share amount `>=`
    /// the reference close, or a total dividend that exceeds the basis (a
    /// return-of-capital event with realized-gain implications this planner does not
    /// book).
    BasisCrossingDividend {
        amount_minor: i64,
        prev_close_minor: i64,
    },
    /// A merger carried a cash consideration (a cash leg, a negative cash term, or a
    /// pure-cash acquisition) — a disposition needing a realized-P&L / cash-settlement
    /// booking not derivable from the terms alone.
    CashConsiderationNotSupported { cash_per_share_minor: i64 },
    /// A merger / symbol-change successor is blank or equals the predecessor.
    InvalidSuccessor { successor: String },
    /// A merger / symbol-change would remap a position onto a symbol another position
    /// already holds — merging the two bases is left to the runtime, not fabricated.
    SuccessorCollision { successor: String },
}

impl PositionReviewReason {
    /// Stable wire discriminator (the `reason` field on every surface).
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::NonPositiveFactor { .. } => "NON_POSITIVE_FACTOR",
            Self::QuantityNotIntegral { .. } => "QUANTITY_NOT_INTEGRAL",
            Self::Overflow { .. } => "OVERFLOW",
            Self::InvalidDividendTerm { .. } => "INVALID_DIVIDEND_TERM",
            Self::BasisCrossingDividend { .. } => "BASIS_CROSSING_DIVIDEND",
            Self::CashConsiderationNotSupported { .. } => "CASH_CONSIDERATION_NOT_SUPPORTED",
            Self::InvalidSuccessor { .. } => "INVALID_SUCCESSOR",
            Self::SuccessorCollision { .. } => "SUCCESSOR_COLLISION",
        }
    }
}

/// The per-position result of applying a corporate action. Pure, total, fail-closed —
/// every path returns one of these five and no adjusted / remapped position is
/// produced with unchecked arithmetic.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PositionCorpActionOutcome {
    /// Quantity and/or basis re-computed in place (split, dividend). Stays at the same
    /// symbol.
    Adjusted {
        symbol: String,
        before: LivePosition,
        after: LivePosition,
    },
    /// Remapped to a successor security (merger stock-for-stock, symbol change).
    Remapped {
        from_symbol: String,
        after: LivePosition,
    },
    /// Marked delisted — quantity + basis frozen, operator paged.
    Delisted { position: LivePosition },
    /// The action could not be applied — flagged for the operator with a structured
    /// reason.
    RequiresManualReview {
        symbol: String,
        reason: PositionReviewReason,
    },
    /// The action does not affect this position (symbol mismatch, already delisted, or
    /// a no-op such as a 1-for-1 split or a self relabel).
    Unaffected { symbol: String },
}

impl PositionCorpActionOutcome {
    /// The original held symbol this outcome concerns (the successor for a `Remapped`
    /// outcome is on `after`). Used for deterministic ordering and reporting.
    pub fn symbol(&self) -> &str {
        match self {
            Self::Adjusted { symbol, .. }
            | Self::RequiresManualReview { symbol, .. }
            | Self::Unaffected { symbol } => symbol,
            Self::Remapped { from_symbol, .. } => from_symbol,
            Self::Delisted { position } => &position.symbol,
        }
    }

    /// The operator alert for a `Delisted` or `RequiresManualReview` outcome, or
    /// `None`. A routine adjust / remap does not page the operator. This is the
    /// neutral value a [`PositionCorpActionAlertSink`] records and the deferred
    /// composition root maps onto the notification subsystem.
    pub fn alert(&self) -> Option<PositionCorpActionAlert> {
        match self {
            Self::Delisted { position } => Some(PositionCorpActionAlert {
                symbol: position.symbol.clone(),
                reason: PositionAlertReason::Delisted,
            }),
            Self::RequiresManualReview { symbol, reason } => Some(PositionCorpActionAlert {
                symbol: symbol.clone(),
                reason: PositionAlertReason::ManualReview(reason.clone()),
            }),
            Self::Adjusted { .. } | Self::Remapped { .. } | Self::Unaffected { .. } => None,
        }
    }

    /// The change event for the running strategy (SRS-SDK-004 analog) — `Some` when a
    /// position's symbol, quantity, or basis changed underneath the strategy
    /// (`Adjusted`, `Remapped`, `Delisted`), or `None`. A distinct audience from the
    /// operator alert: the strategy is told about every change, even the routine ones
    /// the operator is not paged about.
    pub fn strategy_callback(&self) -> Option<PositionChangeEvent> {
        match self {
            Self::Adjusted { symbol, after, .. } => Some(PositionChangeEvent {
                symbol: after.symbol.clone(),
                previous_symbol: symbol.clone(),
                kind: PositionChangeKind::Adjusted,
                new_quantity: after.quantity,
                new_cost_basis_minor: after.cost_basis_minor,
            }),
            Self::Remapped { from_symbol, after } => Some(PositionChangeEvent {
                symbol: after.symbol.clone(),
                previous_symbol: from_symbol.clone(),
                kind: PositionChangeKind::Remapped,
                new_quantity: after.quantity,
                new_cost_basis_minor: after.cost_basis_minor,
            }),
            Self::Delisted { position } => Some(PositionChangeEvent {
                symbol: position.symbol.clone(),
                previous_symbol: position.symbol.clone(),
                kind: PositionChangeKind::Delisted,
                new_quantity: position.quantity,
                new_cost_basis_minor: position.cost_basis_minor,
            }),
            Self::RequiresManualReview { .. } | Self::Unaffected { .. } => None,
        }
    }
}

/// The kind of change a [`PositionChangeEvent`] reports to the strategy.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PositionChangeKind {
    /// Quantity and/or basis re-computed in place.
    Adjusted,
    /// Remapped to a successor security.
    Remapped,
    /// Marked delisted.
    Delisted,
}

impl PositionChangeKind {
    /// Stable wire discriminator.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Adjusted => "ADJUSTED",
            Self::Remapped => "REMAPPED",
            Self::Delisted => "DELISTED",
        }
    }
}

/// A source-neutral notice to the running strategy that its position changed under a
/// corporate action — the value the deferred composition root maps onto an
/// `OrderEvent`-style strategy callback (SRS-SDK-004).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PositionChangeEvent {
    /// The resulting position symbol (the successor for a remap).
    pub symbol: String,
    /// The symbol the position was held under before the action (the predecessor for a
    /// remap; equal to `symbol` otherwise).
    pub previous_symbol: String,
    pub kind: PositionChangeKind,
    pub new_quantity: i64,
    pub new_cost_basis_minor: i128,
}

impl PositionChangeEvent {
    /// A short, non-secret summary for the strategy callback.
    pub fn summary(&self) -> String {
        match self.kind {
            PositionChangeKind::Adjusted => format!(
                "position {} adjusted by a corporate action to {} shares (cost basis {} minor)",
                self.symbol, self.new_quantity, self.new_cost_basis_minor
            ),
            PositionChangeKind::Remapped => format!(
                "position {} remapped to successor {} at {} shares (cost basis {} minor)",
                self.previous_symbol, self.symbol, self.new_quantity, self.new_cost_basis_minor
            ),
            PositionChangeKind::Delisted => format!(
                "position {} delisted; {} shares frozen (cost basis {} minor)",
                self.symbol, self.new_quantity, self.new_cost_basis_minor
            ),
        }
    }
}

/// The class of a [`PositionCorpActionAlert`] — a delisting page, or a page that a
/// corporate action needs manual review.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PositionAlertReason {
    /// The security was delisted; the position is frozen and flagged.
    Delisted,
    /// A corporate action could not be applied and needs manual review.
    ManualReview(PositionReviewReason),
}

impl PositionAlertReason {
    /// Stable wire discriminator for the alert class.
    pub const fn kind_str(&self) -> &'static str {
        match self {
            Self::Delisted => "DELISTED",
            Self::ManualReview(_) => "MANUAL_REVIEW",
        }
    }
}

/// A source-neutral operator alert for a corporate-action-driven position event —
/// carries exactly the fields needed to build a `NotificationTrigger::critical_failure`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PositionCorpActionAlert {
    pub symbol: String,
    pub reason: PositionAlertReason,
}

impl PositionCorpActionAlert {
    /// The operator-facing, non-secret summary — used as the notification trigger
    /// `summary`.
    pub fn operator_summary(&self) -> String {
        match &self.reason {
            PositionAlertReason::Delisted => format!(
                "position {} marked delisted: quantity and cost basis frozen, no further \
                 trading possible",
                self.symbol
            ),
            PositionAlertReason::ManualReview(reason) => review_summary(&self.symbol, reason),
        }
    }
}

/// The operator prose for each review reason.
fn review_summary(symbol: &str, reason: &PositionReviewReason) -> String {
    match reason {
        PositionReviewReason::NonPositiveFactor {
            numerator,
            denominator,
        } => format!(
            "position {symbol} needs review: invalid corporate-action factor {numerator}/{denominator}"
        ),
        PositionReviewReason::QuantityNotIntegral {
            before,
            numerator,
            denominator,
        } => format!(
            "position {symbol} needs review: {numerator}-for-{denominator} action leaves a \
             fractional share count from {before} shares (cash-in-lieu)"
        ),
        PositionReviewReason::Overflow { context } => {
            format!("position {symbol} needs review: {context} overflow applying the corporate action")
        }
        PositionReviewReason::InvalidDividendTerm {
            amount_minor,
            prev_close_minor,
        } => format!(
            "position {symbol} needs review: invalid dividend term amount {amount_minor} against \
             reference close {prev_close_minor}"
        ),
        PositionReviewReason::BasisCrossingDividend {
            amount_minor,
            prev_close_minor,
        } => format!(
            "position {symbol} needs review: dividend amount {amount_minor} would drive the cost \
             basis through zero (reference close {prev_close_minor})"
        ),
        PositionReviewReason::CashConsiderationNotSupported {
            cash_per_share_minor,
        } => format!(
            "position {symbol} needs review: merger cash consideration {cash_per_share_minor} per \
             share requires a realized-P&L booking not derivable from the terms"
        ),
        PositionReviewReason::InvalidSuccessor { successor } => format!(
            "position {symbol} needs review: invalid successor '{successor}'"
        ),
        PositionReviewReason::SuccessorCollision { successor } => format!(
            "position {symbol} needs review: successor '{successor}' is already held; merging the \
             two positions is a manual operation"
        ),
    }
}

/// The neutral port the deferred composition root binds to route a position corporate-
/// action alert onto the real notification subsystem. Declared in `atp-execution`
/// (never `atp-notification`), for the same SRS-ARCH-002 reason
/// [`crate::KillSwitchOperatorAlertSink`] is.
pub trait PositionCorpActionAlertSink {
    /// Dispatch one position corporate-action alert to the operator. **Fallible** — a
    /// missed page on a delisting or a review is itself a safety event, so a transport
    /// failure is surfaced rather than silently swallowed (the exact reason
    /// [`crate::KillSwitchOperatorAlertSink::dispatch`] is fallible). The concrete
    /// email/SMS binding is the deferred composition-root wiring (SRS-NOTIF-001).
    fn dispatch(&self, alert: PositionCorpActionAlert) -> Result<(), PositionAlertError>;
}

/// A failure to dispatch a position corporate-action alert to the operator — carries a
/// short reason (the typed transport taxonomy lands with SRS-NOTIF-001).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PositionAlertError {
    pub reason: String,
}

impl PositionAlertError {
    pub fn new(reason: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
        }
    }
}

/// An alert whose dispatch failed — surfaced by [`plan_and_emit`] so the composition
/// root escalates a missed page rather than treating the position event as fully
/// notified.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PositionAlertFailure {
    pub symbol: String,
    pub error: PositionAlertError,
}

/// The outcomes of a fan-out plan-and-dispatch: the per-position plan plus any operator
/// alerts that FAILED to dispatch (surfaced, never swallowed).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PositionCorpActionReport {
    pub outcomes: Vec<PositionCorpActionOutcome>,
    pub alert_failures: Vec<PositionAlertFailure>,
}

/// Fail modes of a single quantity-scaling step (signed-safe — a negative result is
/// NOT a failure; a short position scales to a negative quantity).
enum ScaleFail {
    /// The scaled value overflowed `i64`.
    Overflow,
    /// The quantity did not scale to a whole number of shares.
    NotIntegral,
}

/// Plan the outcome for ONE position against a corporate action. Pure — no mutation,
/// no I/O.
pub fn plan_position(
    position: &LivePosition,
    action: &PositionCorporateAction,
) -> PositionCorpActionOutcome {
    // A delisted position is terminal — frozen, no further action reaches it.
    if position.is_delisted() {
        return unaffected(position);
    }
    if !affects(position, action) {
        return unaffected(position);
    }
    match &action.kind {
        PositionCorporateActionKind::Delisting => PositionCorpActionOutcome::Delisted {
            position: position.frozen_delisted(),
        },
        PositionCorporateActionKind::Split {
            numerator,
            denominator,
        } => plan_split(position, *numerator, *denominator),
        PositionCorporateActionKind::Dividend {
            amount_minor,
            prev_close_minor,
        } => plan_dividend(position, *amount_minor, *prev_close_minor),
        PositionCorporateActionKind::Merger {
            successor,
            numerator,
            denominator,
            cash_per_share_minor,
        } => plan_merger(
            position,
            successor,
            *numerator,
            *denominator,
            *cash_per_share_minor,
        ),
        PositionCorporateActionKind::SymbolChange { successor } => {
            plan_symbol_change(position, successor)
        }
    }
}

/// Plan every position a corporate action can affect, one outcome per position, in
/// deterministic canonical-symbol order.
///
/// After the per-position plan, a **successor collision** is resolved: a merger /
/// symbol-change remaps the (single, symbol-unique) matched position onto a successor;
/// if another held position already occupies that successor symbol, the remap is
/// downgraded to [`PositionReviewReason::SuccessorCollision`] rather than producing two
/// positions for one canonical symbol. One corporate action is applied per call;
/// sequencing multiple actions is the caller's responsibility.
pub fn plan_positions(
    positions: &[LivePosition],
    action: &PositionCorporateAction,
) -> Vec<PositionCorpActionOutcome> {
    let mut outcomes: Vec<PositionCorpActionOutcome> = positions
        .iter()
        .map(|position| plan_position(position, action))
        .collect();

    // The canonical symbols currently held — a remap onto any OTHER held symbol
    // collides (positions are unique per canonical symbol). Index-aligned with
    // `outcomes` before the sort below.
    let held: Vec<String> = positions
        .iter()
        .map(|position| canonical_symbol(&position.symbol))
        .collect();
    for (index, outcome) in outcomes.iter_mut().enumerate() {
        if let PositionCorpActionOutcome::Remapped { from_symbol, after } = outcome {
            let target = canonical_symbol(&after.symbol);
            let collides = held
                .iter()
                .enumerate()
                .any(|(other, symbol)| other != index && *symbol == target);
            if collides {
                *outcome = PositionCorpActionOutcome::RequiresManualReview {
                    symbol: from_symbol.clone(),
                    reason: PositionReviewReason::SuccessorCollision {
                        successor: after.symbol.clone(),
                    },
                };
            }
        }
    }

    outcomes.sort_by(|left, right| {
        canonical_symbol(left.symbol()).cmp(&canonical_symbol(right.symbol()))
    });
    outcomes
}

/// Plan every position and dispatch an operator alert through `sink` for each
/// `Delisted` / `RequiresManualReview` outcome. A dispatch that fails is recorded in
/// [`PositionCorpActionReport::alert_failures`] (never swallowed) and does NOT abort the
/// remaining dispatches — continue-to-safety.
pub fn plan_and_emit<S: PositionCorpActionAlertSink>(
    positions: &[LivePosition],
    action: &PositionCorporateAction,
    sink: &S,
) -> PositionCorpActionReport {
    let outcomes = plan_positions(positions, action);
    let mut alert_failures = Vec::new();
    for outcome in &outcomes {
        if let Some(alert) = outcome.alert() {
            let symbol = alert.symbol.clone();
            if let Err(error) = sink.dispatch(alert) {
                alert_failures.push(PositionAlertFailure { symbol, error });
            }
        }
    }
    PositionCorpActionReport {
        outcomes,
        alert_failures,
    }
}

// --------------------------------------------------------------------------- //
// Per-action planning (pure)
// --------------------------------------------------------------------------- //

/// Split `N`-for-`M`: scale the quantity by `N / M` (exact), keep the total basis
/// invariant. `N == M` is a no-op.
fn plan_split(
    position: &LivePosition,
    numerator: i64,
    denominator: i64,
) -> PositionCorpActionOutcome {
    if numerator <= 0 || denominator <= 0 {
        return review(
            position,
            PositionReviewReason::NonPositiveFactor {
                numerator,
                denominator,
            },
        );
    }
    if numerator == denominator {
        return unaffected(position);
    }
    let new_quantity = match scale_quantity_exact(position.quantity, numerator, denominator) {
        Ok(quantity) => quantity,
        Err(ScaleFail::NotIntegral) => {
            return review(
                position,
                PositionReviewReason::QuantityNotIntegral {
                    before: position.quantity,
                    numerator,
                    denominator,
                },
            );
        }
        Err(ScaleFail::Overflow) => {
            return review(
                position,
                PositionReviewReason::Overflow {
                    context: "split quantity",
                },
            );
        }
    };
    // Total cost basis is invariant under a split — the per-unit average re-derives.
    let after = position.rebased(new_quantity, position.cost_basis_minor);
    adjusted(position, after)
}

/// Cash dividend: quantity unchanged, basis reduced additively by the actual dividend
/// cash (`cost_basis − amount · quantity`). Fails closed on an invalid term or a
/// dividend that would drive the basis through zero.
fn plan_dividend(
    position: &LivePosition,
    amount_minor: i64,
    prev_close_minor: i64,
) -> PositionCorpActionOutcome {
    if amount_minor <= 0 || prev_close_minor <= 0 {
        return review(
            position,
            PositionReviewReason::InvalidDividendTerm {
                amount_minor,
                prev_close_minor,
            },
        );
    }
    if amount_minor >= prev_close_minor {
        return review(
            position,
            PositionReviewReason::BasisCrossingDividend {
                amount_minor,
                prev_close_minor,
            },
        );
    }
    let cash = match i128::from(amount_minor).checked_mul(i128::from(position.quantity)) {
        Some(cash) => cash,
        None => {
            return review(
                position,
                PositionReviewReason::Overflow {
                    context: "dividend cash",
                },
            );
        }
    };
    let new_basis = match position.cost_basis_minor.checked_sub(cash) {
        Some(basis) => basis,
        None => {
            return review(
                position,
                PositionReviewReason::Overflow {
                    context: "dividend basis",
                },
            );
        }
    };
    // A dividend large enough to flip the basis sign relative to the quantity would
    // imply a negative average cost — a return-of-capital event this planner does not
    // book; flag it rather than fabricate.
    if new_basis != 0 && new_basis.signum() != i128::from(position.quantity).signum() {
        return review(
            position,
            PositionReviewReason::BasisCrossingDividend {
                amount_minor,
                prev_close_minor,
            },
        );
    }
    let after = position.rebased(position.quantity, new_basis);
    adjusted(position, after)
}

/// Merger: pure stock-for-stock remap to the successor at `quantity · N / M` (exact),
/// basis carried intact. Any cash leg, or a pure-cash acquisition, is sent to review.
fn plan_merger(
    position: &LivePosition,
    successor: &str,
    numerator: i64,
    denominator: i64,
    cash_per_share_minor: i64,
) -> PositionCorpActionOutcome {
    // Any cash consideration (a cash leg, a negative term, or a pure-cash acquisition
    // where no successor shares are issued) needs a realized-P&L booking not derivable
    // from the terms.
    if cash_per_share_minor != 0 || numerator == 0 {
        return review(
            position,
            PositionReviewReason::CashConsiderationNotSupported {
                cash_per_share_minor,
            },
        );
    }
    if numerator <= 0 || denominator <= 0 {
        return review(
            position,
            PositionReviewReason::NonPositiveFactor {
                numerator,
                denominator,
            },
        );
    }
    let successor_canonical = canonical_symbol(successor);
    if successor_canonical.is_empty() || successor_canonical == canonical_symbol(&position.symbol) {
        return review(
            position,
            PositionReviewReason::InvalidSuccessor {
                successor: successor.to_string(),
            },
        );
    }
    let new_quantity = match scale_quantity_exact(position.quantity, numerator, denominator) {
        Ok(quantity) => quantity,
        Err(ScaleFail::NotIntegral) => {
            return review(
                position,
                PositionReviewReason::QuantityNotIntegral {
                    before: position.quantity,
                    numerator,
                    denominator,
                },
            );
        }
        Err(ScaleFail::Overflow) => {
            return review(
                position,
                PositionReviewReason::Overflow {
                    context: "merger quantity",
                },
            );
        }
    };
    // Basis carries over intact — a pure stock merger realizes no gain.
    let after = position.remapped(successor_canonical, new_quantity, position.cost_basis_minor);
    PositionCorpActionOutcome::Remapped {
        from_symbol: position.symbol.clone(),
        after,
    }
}

/// Symbol change: a pure relabel to the successor (quantity + basis unchanged). A
/// relabel to the same canonical symbol is a no-op.
fn plan_symbol_change(position: &LivePosition, successor: &str) -> PositionCorpActionOutcome {
    let successor_canonical = canonical_symbol(successor);
    if successor_canonical.is_empty() {
        return review(
            position,
            PositionReviewReason::InvalidSuccessor {
                successor: successor.to_string(),
            },
        );
    }
    if successor_canonical == canonical_symbol(&position.symbol) {
        return unaffected(position);
    }
    let after = position.remapped(
        successor_canonical,
        position.quantity,
        position.cost_basis_minor,
    );
    PositionCorpActionOutcome::Remapped {
        from_symbol: position.symbol.clone(),
        after,
    }
}

// --------------------------------------------------------------------------- //
// Helpers
// --------------------------------------------------------------------------- //

fn adjusted(before: &LivePosition, after: LivePosition) -> PositionCorpActionOutcome {
    PositionCorpActionOutcome::Adjusted {
        symbol: before.symbol.clone(),
        before: before.clone(),
        after,
    }
}

fn review(position: &LivePosition, reason: PositionReviewReason) -> PositionCorpActionOutcome {
    PositionCorpActionOutcome::RequiresManualReview {
        symbol: position.symbol.clone(),
        reason,
    }
}

fn unaffected(position: &LivePosition) -> PositionCorpActionOutcome {
    PositionCorpActionOutcome::Unaffected {
        symbol: position.symbol.clone(),
    }
}

/// Whether `action` affects `position` — i.e. they name the SAME security. Matches on
/// the CANONICAL symbol (trim + upper-case), the same normalization
/// `atp_types::SecurityKey::new` applies at ingest, so a position on `aapl` / ` AAPL `
/// is still recognized as affected by an `AAPL` corporate action.
fn affects(position: &LivePosition, action: &PositionCorporateAction) -> bool {
    canonical_symbol(&position.symbol) == canonical_symbol(&action.symbol)
}

/// Canonicalize a symbol for matching — trim + upper-case, byte-identical to
/// `atp_types::SecurityKey::new`'s normalization.
fn canonical_symbol(symbol: &str) -> String {
    symbol.trim().to_uppercase()
}

/// A signed QUANTITY scaled by `numerator / denominator`, EXACT — a non-integral
/// residual fails closed (cash-in-lieu). Signed-safe: a negative result (a short) is a
/// valid quantity, NOT a failure; only overflow and a non-integral share count fail.
/// `numerator` and `denominator` are both `> 0` (validated by the caller).
fn scale_quantity_exact(quantity: i64, numerator: i64, denominator: i64) -> Result<i64, ScaleFail> {
    let scaled = i128::from(quantity)
        .checked_mul(i128::from(numerator))
        .ok_or(ScaleFail::Overflow)?;
    if scaled % i128::from(denominator) != 0 {
        return Err(ScaleFail::NotIntegral);
    }
    i64::try_from(scaled / i128::from(denominator)).map_err(|_| ScaleFail::Overflow)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn long(symbol: &str, quantity: i64, basis: i128) -> LivePosition {
        LivePosition::new(symbol, quantity, basis).expect("valid position")
    }

    #[test]
    fn forward_split_scales_quantity_and_keeps_basis_invariant() {
        let position = long("AAPL", 100, 500_000);
        let outcome = plan_position(&position, &PositionCorporateAction::split("AAPL", 4, 1));
        match outcome {
            PositionCorpActionOutcome::Adjusted { after, .. } => {
                assert_eq!(after.quantity(), 400);
                assert_eq!(
                    after.cost_basis_minor(),
                    500_000,
                    "basis invariant under split"
                );
                assert_eq!(after.average_cost_minor(), Some(1_250));
            }
            other => panic!("expected Adjusted, got {other:?}"),
        }
    }

    #[test]
    fn reverse_split_of_a_short_yields_a_negative_quantity_not_a_review() {
        let short = LivePosition::new("ZZZ", -100, -500_000).expect("valid short");
        let outcome = plan_position(&short, &PositionCorporateAction::split("ZZZ", 1, 10));
        match outcome {
            PositionCorpActionOutcome::Adjusted { after, .. } => {
                assert_eq!(after.quantity(), -10);
                assert_eq!(after.cost_basis_minor(), -500_000);
            }
            other => panic!("expected Adjusted short, got {other:?}"),
        }
    }

    #[test]
    fn dividend_reduces_basis_additively_for_long_and_short() {
        let long_pos = long("AAPL", 100, 500_000);
        match plan_position(
            &long_pos,
            &PositionCorporateAction::dividend("AAPL", 100, 4_000),
        ) {
            PositionCorpActionOutcome::Adjusted { after, .. } => {
                assert_eq!(after.quantity(), 100);
                assert_eq!(after.cost_basis_minor(), 490_000);
            }
            other => panic!("expected long Adjusted, got {other:?}"),
        }
        let short_pos = LivePosition::new("AAPL", -100, -500_000).expect("valid short");
        match plan_position(
            &short_pos,
            &PositionCorporateAction::dividend("AAPL", 100, 4_000),
        ) {
            PositionCorpActionOutcome::Adjusted { after, .. } => {
                assert_eq!(
                    after.cost_basis_minor(),
                    -490_000,
                    "short pays the dividend"
                );
            }
            other => panic!("expected short Adjusted, got {other:?}"),
        }
    }

    #[test]
    fn merger_remaps_to_successor_with_basis_intact() {
        let position = long("OLD", 200, 800_000);
        match plan_position(
            &position,
            &PositionCorporateAction::merger("OLD", "NEW", 3, 2, 0),
        ) {
            PositionCorpActionOutcome::Remapped { from_symbol, after } => {
                assert_eq!(from_symbol, "OLD");
                assert_eq!(after.symbol(), "NEW");
                assert_eq!(after.quantity(), 300);
                assert_eq!(after.cost_basis_minor(), 800_000);
            }
            other => panic!("expected Remapped, got {other:?}"),
        }
    }

    #[test]
    fn merger_with_cash_is_sent_to_review() {
        let position = long("OLD", 100, 500_000);
        match plan_position(
            &position,
            &PositionCorporateAction::merger("OLD", "NEW", 1, 1, 250),
        ) {
            PositionCorpActionOutcome::RequiresManualReview { reason, .. } => {
                assert_eq!(reason.as_str(), "CASH_CONSIDERATION_NOT_SUPPORTED");
            }
            other => panic!("expected review, got {other:?}"),
        }
    }

    #[test]
    fn delisting_freezes_and_alerts() {
        let position = long("DEAD", 100, 500_000);
        let outcome = plan_position(&position, &PositionCorporateAction::delisting("DEAD"));
        match &outcome {
            PositionCorpActionOutcome::Delisted { position } => {
                assert!(position.is_delisted());
                assert_eq!(position.quantity(), 100);
            }
            other => panic!("expected Delisted, got {other:?}"),
        }
        let alert = outcome.alert().expect("delisting pages the operator");
        assert_eq!(alert.reason.kind_str(), "DELISTED");
    }

    #[test]
    fn successor_collision_is_flagged() {
        let positions = vec![long("OLD", 100, 500_000), long("NEW", 50, 250_000)];
        let outcomes = plan_positions(
            &positions,
            &PositionCorporateAction::merger("OLD", "NEW", 1, 1, 0),
        );
        let old = outcomes
            .iter()
            .find(|o| o.symbol() == "OLD")
            .expect("OLD outcome");
        match old {
            PositionCorpActionOutcome::RequiresManualReview { reason, .. } => {
                assert_eq!(reason.as_str(), "SUCCESSOR_COLLISION");
            }
            other => panic!("expected collision review, got {other:?}"),
        }
    }

    #[test]
    fn already_delisted_position_is_terminal() {
        let position = LivePosition::delisted("DEAD", 100, 500_000).expect("valid delisted");
        let outcome = plan_position(&position, &PositionCorporateAction::split("DEAD", 2, 1));
        assert!(matches!(
            outcome,
            PositionCorpActionOutcome::Unaffected { .. }
        ));
        assert!(
            outcome.alert().is_none(),
            "no second alert on a delisted position"
        );
    }

    #[test]
    fn constructor_rejects_flat_and_sign_inconsistent() {
        assert!(LivePosition::new("AAPL", 0, 0).is_err(), "flat");
        assert!(LivePosition::new("", 1, 1).is_err(), "blank");
        assert!(
            LivePosition::new("AAPL", 100, -1).is_err(),
            "negative average cost"
        );
        assert!(
            LivePosition::new("AAPL", 100, 0).is_ok(),
            "zero-cost spinoff"
        );
    }
}
