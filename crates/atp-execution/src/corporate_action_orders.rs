//! SRS-DATA-019 — adjust or cancel live resting orders affected by corporate actions.
//!
//! When a split or reverse split becomes effective for a security with live orders,
//! this module recomputes each **broker-acknowledged** (`OrderState::Acked`) order's
//! **quantity** and **limit / stop prices** onto the post-action basis. Any other
//! affected order that could still rest or fill at the stale basis — a partially-
//! filled remainder, or an unacknowledged in-flight (`New` / `PendingSubmit`) intent
//! — is instead **cancelled fail-closed** (its correct basis needs fill-aware live
//! state; see [`plan_resting_orders`] for the per-state routing). When an
//! adjustment is not meaningful or not possible — a **delisting**, a reverse
//! split that leaves a **fractional share count**, a price that would round to a
//! **non-positive** value, a **non-positive split factor**, or an **overflow** —
//! it produces a **cancel** decision carrying a structured reason. That reason is
//! everything the operator notification (SRS-NOTIF-001) and the Python strategy
//! callback (SRS-SDK-004) need: see [`RestingOrderOutcome::alert`] and
//! [`RestingOrderCorpActionAlert`].
//!
//! ## The scaling convention (byte-stable with the data layer)
//!
//! Mirrors `atp-data`'s corporate-action normalization exactly so the live
//! order-adjustment basis matches the historical / paper basis (StRS SN-1.14
//! requires the SAME corporate-action data drive all three): for an `N`-for-`M`
//! split (`numerator = N`, `denominator = M`),
//!
//! ```text
//!   new_price    = round_half_to_even( price · M / N )   // price factor DEN/NUM
//!   new_quantity =                     quantity · N / M   // quantity factor NUM/DEN (exact)
//! ```
//!
//! A 4-for-1 forward split (`{4, 1}`) DIVIDES the resting limit by 4 and
//! MULTIPLIES the share count by 4; a 1-for-10 reverse split (`{1, 10}`) does the
//! inverse. All arithmetic is `i128`-intermediate with `checked_mul`; the final
//! narrowing back to `i64` fails closed on overflow. Non-positive factors are
//! rejected before any arithmetic — a zero denominator would zero a price and a
//! zero numerator used as the price divisor would divide-by-zero. This is the
//! same discipline `atp-data::normalization::split_adjust_record` applies to a
//! publicly-constructible `SplitEvent`.
//!
//! ## Prices round, quantities must be exact (the load-bearing asymmetry)
//!
//! Money is quoted to the minor unit, so a scaled price is **rounded**
//! (round-half-to-even, matching the vendor convention DATA-011/012 use). A share
//! count cannot be fractional: a reverse split whose ratio does not divide the
//! resting quantity evenly leaves a fractional residual the broker settles as
//! cash-in-lieu, which is precisely "adjustment not possible" for the resting
//! order — it is **cancelled**, never silently truncated or rounded to a
//! different position size.
//!
//! ## Adjust = cancel-then-new (no in-place mutation)
//!
//! [`OrderSubmission`] is a value type; an `Adjusted` outcome carries a fresh,
//! already-`validate()`-clean `new_submission`. Applying it against a live
//! [`OrderLedger`] is a [`OrderLedger::cancel_replace`] (cancel-then-new), which
//! holds the replacement until the original is confirmed `CANCELLED` — so an
//! adjustment can never leave the pre-split and post-split order both live.
//!
//! ## Neutral emission (execution stays independent of the notification crate)
//!
//! Cancellation notifications route through the [`RestingOrderCorpActionAlertSink`]
//! port, exactly like [`crate::ConnectivityEventSink`] /
//! [`crate::KillSwitchOperatorAlertSink`]: `atp-execution` names no notification
//! transport and never depends on `atp-notification`. The concrete binding —
//! mapping a [`RestingOrderCorpActionAlert`] onto
//! `NotificationTrigger::critical_failure` + `OperatorNotifier::dispatch`, and
//! onto a `deliver_order_event` `OrderEvent(CANCELLED)` — is the deferred
//! composition-root wiring (SRS-NOTIF-001 / SRS-SDK-004); the notification
//! subsystem's own dispatch-within-SLA over email + SMS is proven by
//! SRS-NOTIF-001's tests. The `srs_data_019_corp_action_notify` integration test
//! proves the emission half here: every cancel — and only a cancel — is routed
//! through the port carrying the symbol + reason the trigger and callback need.
//!
//! ## Scope (this is the deterministic core; the live path is deferred)
//!
//! The corporate-action facts here are inputs (fixtures / CLI). The production
//! feed of live resting-order state and the routing of the cancel / cancel-replace
//! to the real IB adapter (`BrokerageAdapter::cancel_order`, which currently fails
//! closed `LIVE_WIRE_PROTOCOL_PENDING`) is the deferred SRS-EXE-001 / SRS-EXE-006
//! runtime; real operator email/SMS is SRS-NOTIF-001; live in-container callback
//! delivery is SRS-SDK-004. So SRS-DATA-019 lands `serialized` (`passes:false`)
//! until that end-to-end evidence exists.

use atp_types::{
    OrderKey, OrderLedger, OrderState, OrderSubmission, OrderType, RestingOrderCancel,
};

/// A corporate action bound to the symbol it affects. Publicly constructible; the
/// split factors are re-validated at plan time (mirroring `SplitEvent`, which
/// re-checks its factors because it too is publicly constructible), so an invalid
/// factor can never divide-by-zero or silently miscompute.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RestingOrderCorporateAction {
    /// The affected security. A resting order is adjusted / cancelled only when
    /// its `symbol` equals this — a mixed batch can never cross-contaminate.
    pub symbol: String,
    /// What happened to the security.
    pub kind: CorporateActionKind,
}

impl RestingOrderCorporateAction {
    /// An `N`-for-`M` split (forward when `N > M`, reverse when `N < M`).
    pub fn split(symbol: impl Into<String>, numerator: i64, denominator: i64) -> Self {
        Self {
            symbol: symbol.into(),
            kind: CorporateActionKind::Split {
                numerator,
                denominator,
            },
        }
    }

    /// A delisting — no price or quantity adjustment is meaningful; resting orders
    /// are cancelled.
    pub fn delisting(symbol: impl Into<String>) -> Self {
        Self {
            symbol: symbol.into(),
            kind: CorporateActionKind::Delisting,
        }
    }
}

/// The class of corporate action a resting order can be affected by. Splits
/// (forward and reverse) adjust; a delisting cancels. Dividends, mergers, and
/// symbol changes affect *positions*, not resting-order price or quantity, and are
/// SRS-DATA-020's concern — they are deliberately not represented here.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CorporateActionKind {
    /// An `N`-for-`M` split. Both factors are validated `> 0` at plan time.
    Split { numerator: i64, denominator: i64 },
    /// The security was delisted.
    Delisting,
}

/// Which price on the order type rounded to a non-positive value.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PriceField {
    Limit,
    Stop,
}

impl PriceField {
    /// Stable wire string.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Limit => "LIMIT",
            Self::Stop => "STOP",
        }
    }
}

/// Why a resting order was cancelled rather than adjusted — the structured,
/// source-neutral reason the operator alert and the strategy callback carry.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RestingOrderCancelReason {
    /// The security was delisted; no adjustment is meaningful.
    Delisting,
    /// The split carried a non-positive numerator or denominator (rejected
    /// before any arithmetic).
    NonPositiveFactor { numerator: i64, denominator: i64 },
    /// A limit / stop price scaled to a non-positive value — a resting order can
    /// never carry a non-positive price (`validate_prices` would reject it, and a
    /// zero-price limit is a fill-at-any-price hazard).
    PriceRoundedNonPositive {
        field: PriceField,
        before_minor: i64,
    },
    /// A (reverse) split did not divide the resting quantity into a whole share
    /// count — the fractional residual is cash-in-lieu, so the order is cancelled
    /// rather than resized to a different position.
    QuantityNotIntegral {
        before: i64,
        numerator: i64,
        denominator: i64,
    },
    /// An adjusted quantity or price overflowed `i64` (never wraps).
    Overflow { context: &'static str },
    /// A partially-filled order affected by the corporate action is cancelled
    /// fail-closed: its live resting remainder is `quantity − cumulative_filled`,
    /// which this planner (working from the `OrderSubmission` alone, without fill
    /// state) cannot re-size correctly. Rather than leave it working at the
    /// pre-action basis or re-size to the wrong quantity, the remaining order is
    /// cancelled + the operator notified; fill-aware re-sizing lands with the
    /// deferred live wiring (SRS-EXE-001).
    PartiallyFilledNotAdjustable,
    /// An order not yet acknowledged by the broker (`New` / `PendingSubmit`) when
    /// the corporate action took effect is cancelled fail-closed. Its broker state
    /// is ambiguous — a `PendingSubmit` intent may still ack OR fill (both are legal
    /// transitions) — so it can neither be adjusted (no confirmed resting order to
    /// re-price) nor left alone (it could rest / fill at the stale pre-action basis).
    /// Cancelling closes the pre-ack race; the strategy re-submits at the new basis.
    UnacknowledgedNotAdjustable,
}

impl RestingOrderCancelReason {
    /// Stable wire discriminator (the `reason` field on every surface).
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Delisting => "DELISTING",
            Self::NonPositiveFactor { .. } => "NON_POSITIVE_FACTOR",
            Self::PriceRoundedNonPositive { .. } => "PRICE_ROUNDED_NON_POSITIVE",
            Self::QuantityNotIntegral { .. } => "QUANTITY_NOT_INTEGRAL",
            Self::Overflow { .. } => "OVERFLOW",
            Self::PartiallyFilledNotAdjustable => "PARTIALLY_FILLED_NOT_ADJUSTABLE",
            Self::UnacknowledgedNotAdjustable => "UNACKNOWLEDGED_NOT_ADJUSTABLE",
        }
    }
}

/// The per-order result of applying a corporate action. Pure, total, fail-closed
/// — every path returns one of these three, and no `Adjusted` is produced with an
/// unchecked price or quantity.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RestingOrderOutcome {
    /// Re-priced / re-sized. `new_submission` is already `validate()`-clean
    /// (positive quantity, positive prices) — it is the replacement a
    /// cancel-then-new applies through [`OrderLedger::cancel_replace`].
    Adjusted {
        key: OrderKey,
        old: OrderSubmission,
        new_submission: OrderSubmission,
    },
    /// Adjustment not possible — cancel + notify. Carries the structured reason.
    Cancelled {
        key: OrderKey,
        symbol: String,
        reason: RestingOrderCancelReason,
    },
    /// The action does not affect this order (symbol mismatch, or the order is
    /// terminal / not resting).
    Unaffected { key: OrderKey },
}

impl RestingOrderOutcome {
    /// The order this outcome concerns.
    pub fn key(&self) -> &OrderKey {
        match self {
            Self::Adjusted { key, .. } | Self::Cancelled { key, .. } | Self::Unaffected { key } => {
                key
            }
        }
    }

    /// The operator alert for a `Cancelled` outcome, or `None` — the neutral
    /// value a [`RestingOrderCorpActionAlertSink`] records and the deferred
    /// composition root maps onto the notification + callback subsystems.
    pub fn alert(&self) -> Option<RestingOrderCorpActionAlert> {
        match self {
            Self::Cancelled {
                key,
                symbol,
                reason,
            } => Some(RestingOrderCorpActionAlert {
                order_id: key.to_string(),
                symbol: symbol.clone(),
                reason: *reason,
            }),
            Self::Adjusted { .. } | Self::Unaffected { .. } => None,
        }
    }

    /// The [`RestingOrderCancel`] the execution runtime routes to the broker for a
    /// `Cancelled` outcome, or `None`. `broker_order_id` is `None` here — the
    /// engine plans the decision; the runtime binds the broker handle.
    pub fn resting_order_cancel(&self) -> Option<RestingOrderCancel> {
        match self {
            Self::Cancelled { key, symbol, .. } => Some(RestingOrderCancel {
                order_id: key.to_string(),
                symbol: symbol.clone(),
                broker_order_id: None,
            }),
            Self::Adjusted { .. } | Self::Unaffected { .. } => None,
        }
    }
}

/// A source-neutral operator alert for a corporate-action-driven resting-order
/// cancel. Carries exactly the fields needed to build BOTH a
/// `NotificationTrigger::critical_failure` (the notification subsystem) and an
/// `OrderEvent(CANCELLED, reason)` (the strategy callback).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RestingOrderCorpActionAlert {
    /// The [`OrderKey`] display form (`strategy/correlation-id`).
    pub order_id: String,
    pub symbol: String,
    pub reason: RestingOrderCancelReason,
}

impl RestingOrderCorpActionAlert {
    /// The operator-facing, non-secret summary — used as BOTH the notification
    /// trigger `summary` and the callback `reason`.
    pub fn operator_summary(&self) -> String {
        match self.reason {
            RestingOrderCancelReason::Delisting => format!(
                "resting order {} on {} cancelled: security delisted, adjustment not possible",
                self.order_id, self.symbol
            ),
            RestingOrderCancelReason::NonPositiveFactor {
                numerator,
                denominator,
            } => format!(
                "resting order {} on {} cancelled: invalid corporate-action split factor {}/{}",
                self.order_id, self.symbol, numerator, denominator
            ),
            RestingOrderCancelReason::PriceRoundedNonPositive {
                field,
                before_minor,
            } => format!(
                "resting order {} on {} cancelled: {} price {} rounds to a non-positive value under \
                 the split",
                self.order_id,
                self.symbol,
                field.as_str(),
                before_minor
            ),
            RestingOrderCancelReason::QuantityNotIntegral {
                before,
                numerator,
                denominator,
            } => format!(
                "resting order {} on {} cancelled: {}-for-{} split leaves a fractional share count \
                 from {} shares",
                self.order_id, self.symbol, numerator, denominator, before
            ),
            RestingOrderCancelReason::Overflow { context } => format!(
                "resting order {} on {} cancelled: {} overflow applying the split",
                self.order_id, self.symbol, context
            ),
            RestingOrderCancelReason::PartiallyFilledNotAdjustable => format!(
                "resting order {} on {} cancelled: partially-filled order affected by a corporate \
                 action cannot be safely re-sized; the remaining working quantity is cancelled",
                self.order_id, self.symbol
            ),
            RestingOrderCancelReason::UnacknowledgedNotAdjustable => format!(
                "resting order {} on {} cancelled: order not yet broker-acknowledged when the \
                 corporate action took effect; cancelled fail-closed to prevent it resting or \
                 filling at the pre-action basis",
                self.order_id, self.symbol
            ),
        }
    }

    /// The reason string the `OrderEvent(CANCELLED)` strategy callback carries
    /// (SRS-SDK-004 requires a non-empty `reason` on a cancel event).
    pub fn callback_reason(&self) -> String {
        self.operator_summary()
    }
}

/// The neutral port the deferred composition root binds to route a
/// resting-order-cancel alert onto the real notification + callback subsystems.
/// Declared in `atp-execution` (never `atp-notification`) for the SRS-ARCH-002
/// reason [`crate::KillSwitchOperatorAlertSink`] / [`crate::ConnectivityEventSink`]
/// are: the execution layer owns the decision and emits a neutral event; the
/// transport is a higher-layer binding.
pub trait RestingOrderCorpActionAlertSink {
    /// Dispatch one resting-order-cancel alert to the operator (notification
    /// subsystem) and the strategy callback. **Fallible** — a missed page / dropped
    /// callback on a corp-action cancel is itself a safety event, so a transport
    /// failure is surfaced rather than silently swallowed (the exact reason
    /// [`crate::KillSwitchOperatorAlertSink::dispatch`] is fallible). The concrete
    /// email/SMS + `deliver_order_event` binding is the deferred composition-root
    /// wiring (SRS-NOTIF-001 / SRS-SDK-004).
    fn dispatch(&self, alert: RestingOrderCorpActionAlert) -> Result<(), RestingOrderAlertError>;
}

/// A failure to dispatch a resting-order-cancel alert to the operator / strategy —
/// carries a short reason (the typed transport taxonomy lands with the deferred
/// SRS-NOTIF-001 / SRS-SDK-004 runtimes, mirroring `KillSwitchSideEffectError`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RestingOrderAlertError {
    pub reason: String,
}

impl RestingOrderAlertError {
    pub fn new(reason: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
        }
    }
}

/// A cancel whose operator alert failed to dispatch — surfaced by [`plan_and_emit`]
/// so the composition root escalates (a missed cancel notification is never
/// silently dropped) rather than treating the cancel as fully notified.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RestingOrderAlertFailure {
    pub order_id: String,
    pub symbol: String,
    pub error: RestingOrderAlertError,
}

/// Fail modes of a single scaling step.
enum ScaleFail {
    /// The scaled value overflowed `i64`.
    Overflow,
    /// The scaled value is `<= 0` (a resting order can carry neither a
    /// non-positive price nor a non-positive quantity).
    NonPositive,
    /// The quantity did not scale to a whole number of shares.
    NotIntegral,
}

/// Plan the outcome for ONE resting order against a corporate action. Pure — no
/// ledger mutation, no I/O. The caller vouches the order is resting; a terminal
/// order is filtered by [`plan_resting_orders`].
pub fn plan_resting_order(
    key: &OrderKey,
    submission: &OrderSubmission,
    action: &RestingOrderCorporateAction,
) -> RestingOrderOutcome {
    if !affects(submission, action) {
        return RestingOrderOutcome::Unaffected { key: key.clone() };
    }

    let (numerator, denominator) = match action.kind {
        CorporateActionKind::Delisting => {
            return cancel(key, submission, RestingOrderCancelReason::Delisting);
        }
        CorporateActionKind::Split {
            numerator,
            denominator,
        } => (numerator, denominator),
    };

    // Re-validate the publicly-constructible factor BEFORE any arithmetic.
    if numerator <= 0 || denominator <= 0 {
        return cancel(
            key,
            submission,
            RestingOrderCancelReason::NonPositiveFactor {
                numerator,
                denominator,
            },
        );
    }
    let num = i128::from(numerator);
    let den = i128::from(denominator);

    // Quantity: NUM / DEN, exact — a fractional residual cancels.
    let new_quantity = match scale_quantity_exact(submission.quantity, num, den) {
        Ok(quantity) => quantity,
        Err(ScaleFail::NotIntegral) | Err(ScaleFail::NonPositive) => {
            return cancel(
                key,
                submission,
                RestingOrderCancelReason::QuantityNotIntegral {
                    before: submission.quantity,
                    numerator,
                    denominator,
                },
            );
        }
        Err(ScaleFail::Overflow) => {
            return cancel(
                key,
                submission,
                RestingOrderCancelReason::Overflow {
                    context: "quantity",
                },
            );
        }
    };

    // Prices: DEN / NUM, round-half-to-even — a non-positive result cancels.
    let new_order_type = match scale_order_type(submission.order_type, num, den) {
        Ok(order_type) => order_type,
        Err(reason) => return cancel(key, submission, reason),
    };

    let new_submission = OrderSubmission::new(
        submission.strategy_id.clone(),
        submission.symbol.clone(),
        new_quantity,
        submission.asset_class,
        submission.side,
        new_order_type,
    );

    // Belt-and-suspenders: the guards above already guarantee positivity, but the
    // rebuilt order must pass the SRS-EXE-003 gate before it is a valid replacement.
    match new_submission.validate() {
        Ok(()) => RestingOrderOutcome::Adjusted {
            key: key.clone(),
            old: submission.clone(),
            new_submission,
        },
        Err(_) => cancel(
            key,
            submission,
            RestingOrderCancelReason::Overflow {
                context: "revalidation",
            },
        ),
    }
}

/// Plan every order a corporate action can affect, returning one outcome per
/// tracked order in deterministic `OrderKey`-display order. An order on another
/// symbol, or in a state the action cannot reach, is `Unaffected`.
///
/// State routing is by broker exposure, NOT "non-terminal" — an affected order is
/// only ADJUSTED when it is unambiguously resting with a known working quantity;
/// every other affected order that could still rest or fill at the stale basis is
/// **cancelled fail-closed**:
///   * `Acked` — fully working, `submission.quantity` is the working quantity, so
///     it is adjusted or cancelled by [`plan_resting_order`] (exact math).
///   * `PartiallyFilled` — a live resting remainder of `quantity − cumulative_filled`
///     that this planner (working from the `OrderSubmission` alone) cannot re-size,
///     so it is cancelled fail-closed (`PartiallyFilledNotAdjustable`).
///   * `New` / `PendingSubmit` — not yet broker-acknowledged, but a `PendingSubmit`
///     intent can still ack OR fill; leaving it would let it rest / fill at the
///     stale pre-action basis, so it is cancelled fail-closed
///     (`UnacknowledgedNotAdjustable`) — closing the pre-ack race.
///   * `CancelPending` — already being cancelled; acting again would double-cancel
///     or resurrect a re-priced replacement against an order that is going away.
///   * terminal states — nothing to act on.
///
/// Fill-aware re-sizing of a partially-filled order, and re-basing an unacknowledged
/// order in place rather than cancelling it, need the fill-aware live-execution state
/// and land with the deferred live wiring (SRS-EXE-001).
pub fn plan_resting_orders(
    ledger: &OrderLedger,
    action: &RestingOrderCorporateAction,
) -> Vec<RestingOrderOutcome> {
    let mut outcomes: Vec<RestingOrderOutcome> = ledger
        .orders_iter()
        .map(|order| plan_ledger_order(order.state(), order.key(), order.submission(), action))
        .collect();
    outcomes.sort_by_key(|outcome| outcome.key().to_string());
    outcomes
}

/// Route one ledger order by its state (see [`plan_resting_orders`]): `Acked` ->
/// full planning; an affected in-flight (`New` / `PendingSubmit`) or partially-filled
/// order -> fail-closed cancel; `CancelPending` + terminal -> `Unaffected`.
fn plan_ledger_order(
    state: OrderState,
    key: &OrderKey,
    submission: &OrderSubmission,
    action: &RestingOrderCorporateAction,
) -> RestingOrderOutcome {
    match state {
        // Confirmed resting with a known working quantity: adjust (or cancel).
        OrderState::Acked => plan_resting_order(key, submission, action),
        // A partially-filled remainder affected by the action — its remaining working
        // quantity is unknown here, so cancel fail-closed rather than mis-adjust.
        OrderState::PartiallyFilled if affects(submission, action) => cancel(
            key,
            submission,
            RestingOrderCancelReason::PartiallyFilledNotAdjustable,
        ),
        // An unacknowledged in-flight intent affected by the action — it can still
        // ack OR fill, so cancel fail-closed to close the pre-ack race.
        OrderState::New | OrderState::PendingSubmit if affects(submission, action) => cancel(
            key,
            submission,
            RestingOrderCancelReason::UnacknowledgedNotAdjustable,
        ),
        // CancelPending (already terminating), terminal states, and any order on an
        // unaffected symbol: untouched.
        _ => RestingOrderOutcome::Unaffected { key: key.clone() },
    }
}

/// The outcomes of a fan-out plan-and-dispatch: the per-order plan plus any operator
/// alerts that FAILED to dispatch (surfaced so the composition root escalates a
/// missed cancel notification rather than treating the cancel as fully notified).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RestingOrderCorpActionReport {
    pub outcomes: Vec<RestingOrderOutcome>,
    pub alert_failures: Vec<RestingOrderAlertFailure>,
}

/// Plan every resting order and dispatch an operator alert through `sink` for each
/// `Cancelled` outcome. A dispatch that fails is recorded in
/// [`RestingOrderCorpActionReport::alert_failures`] (never swallowed) and does NOT
/// abort the remaining dispatches — continue-to-safety, so one unreachable channel
/// cannot suppress the others.
pub fn plan_and_emit<S: RestingOrderCorpActionAlertSink>(
    ledger: &OrderLedger,
    action: &RestingOrderCorporateAction,
    sink: &S,
) -> RestingOrderCorpActionReport {
    let outcomes = plan_resting_orders(ledger, action);
    let mut alert_failures = Vec::new();
    for outcome in &outcomes {
        if let Some(alert) = outcome.alert() {
            let order_id = alert.order_id.clone();
            let symbol = alert.symbol.clone();
            if let Err(error) = sink.dispatch(alert) {
                alert_failures.push(RestingOrderAlertFailure {
                    order_id,
                    symbol,
                    error,
                });
            }
        }
    }
    RestingOrderCorpActionReport {
        outcomes,
        alert_failures,
    }
}

/// Build a `Cancelled` outcome from an order + reason.
fn cancel(
    key: &OrderKey,
    submission: &OrderSubmission,
    reason: RestingOrderCancelReason,
) -> RestingOrderOutcome {
    RestingOrderOutcome::Cancelled {
        key: key.clone(),
        symbol: submission.symbol.clone(),
        reason,
    }
}

/// Whether `action` affects `submission` — i.e. they name the SAME security.
///
/// Matches on the CANONICAL symbol (trim + upper-case), the same normalization
/// `atp_types::SecurityKey::new` applies at the ingest boundary. `OrderSubmission`
/// is not itself canonicalized (its `validate` only rejects a blank symbol), so a
/// resting order for `aapl` / ` AAPL ` must still be recognized as affected by an
/// `AAPL` corporate action — a raw compare would silently leave it un-cancelled.
fn affects(submission: &OrderSubmission, action: &RestingOrderCorporateAction) -> bool {
    canonical_symbol(&submission.symbol) == canonical_symbol(&action.symbol)
}

/// Canonicalize a symbol for matching — trim + upper-case, byte-identical to
/// `atp_types::SecurityKey::new`'s normalization.
fn canonical_symbol(symbol: &str) -> String {
    symbol.trim().to_uppercase()
}

/// Scale each present price on the order type by `DEN / NUM` (round-half-to-even).
/// Absent prices pass through. On failure, reports which price field failed and
/// why.
fn scale_order_type(
    order_type: OrderType,
    num: i128,
    den: i128,
) -> Result<OrderType, RestingOrderCancelReason> {
    let scale = |price_minor: i64, field: PriceField| -> Result<i64, RestingOrderCancelReason> {
        scale_price_minor(price_minor, num, den).map_err(|fail| match fail {
            ScaleFail::NonPositive => RestingOrderCancelReason::PriceRoundedNonPositive {
                field,
                before_minor: price_minor,
            },
            // Prices are not exact-divided, so NotIntegral cannot arise here; both
            // remaining fail modes are overflow.
            ScaleFail::Overflow | ScaleFail::NotIntegral => {
                RestingOrderCancelReason::Overflow { context: "price" }
            }
        })
    };
    Ok(match order_type {
        OrderType::Market => OrderType::Market,
        OrderType::Limit { limit_price_minor } => OrderType::Limit {
            limit_price_minor: scale(limit_price_minor, PriceField::Limit)?,
        },
        OrderType::Stop { stop_price_minor } => OrderType::Stop {
            stop_price_minor: scale(stop_price_minor, PriceField::Stop)?,
        },
        OrderType::StopLimit {
            stop_price_minor,
            limit_price_minor,
        } => OrderType::StopLimit {
            stop_price_minor: scale(stop_price_minor, PriceField::Stop)?,
            limit_price_minor: scale(limit_price_minor, PriceField::Limit)?,
        },
    })
}

/// A limit / stop PRICE scaled by `DEN / NUM` (a 4-for-1 forward split divides the
/// price by 4), rounded half-to-even. Fails closed on overflow or a non-positive
/// result. `num` and `den` are both `> 0` (validated by the caller).
fn scale_price_minor(price_minor: i64, num: i128, den: i128) -> Result<i64, ScaleFail> {
    let scaled = i128::from(price_minor)
        .checked_mul(den)
        .ok_or(ScaleFail::Overflow)?;
    let rounded = div_round_half_even(scaled, num);
    let value = i64::try_from(rounded).map_err(|_| ScaleFail::Overflow)?;
    if value <= 0 {
        return Err(ScaleFail::NonPositive);
    }
    Ok(value)
}

/// A QUANTITY scaled by `NUM / DEN` (a 4-for-1 forward split multiplies shares by
/// 4), EXACT — a non-integral residual fails closed (cash-in-lieu, cancel). Fails
/// closed on overflow or a non-positive result. `num` and `den` are both `> 0`.
fn scale_quantity_exact(quantity: i64, num: i128, den: i128) -> Result<i64, ScaleFail> {
    let scaled = i128::from(quantity)
        .checked_mul(num)
        .ok_or(ScaleFail::Overflow)?;
    if scaled % den != 0 {
        return Err(ScaleFail::NotIntegral);
    }
    let value = i64::try_from(scaled / den).map_err(|_| ScaleFail::Overflow)?;
    if value <= 0 {
        return Err(ScaleFail::NonPositive);
    }
    Ok(value)
}

/// `numer / denom` rounded half-to-even (banker's rounding), integer-exact.
/// Copied BYTE-STABLE from `atp-data::normalization::div_round_half_even` so the
/// live order-adjustment rounding cannot drift from the historical / paper basis
/// (StRS SN-1.14 requires the same corporate-action data drive all three). `denom`
/// MUST be `> 0` (guaranteed: it is a validated-positive split factor).
fn div_round_half_even(numer: i128, denom: i128) -> i128 {
    debug_assert!(denom > 0, "denominator must be positive");
    let quotient = numer.div_euclid(denom);
    let remainder = numer.rem_euclid(denom); // 0 <= remainder < denom
                                             // Compare 2*remainder with denom WITHOUT computing 2*remainder (which could overflow for a huge
                                             // denom): 2r < d  <=>  r < d-r, and d-r is in (0, denom] so the subtraction is overflow-free.
    let complement = denom - remainder;
    if remainder < complement {
        quotient // closer to the floor
    } else if remainder > complement {
        quotient + 1 // closer to the ceiling
    } else if quotient.rem_euclid(2) == 0 {
        quotient // exact half -> round to the even neighbour
    } else {
        quotient + 1
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Pinned identical to atp-data::normalization::round_half_to_even_breaks_ties_to_even
    // so a reviewer can diff the byte-stable copy.
    #[test]
    fn div_round_half_even_matches_normalization() {
        assert_eq!(div_round_half_even(5, 2), 2, "2.5 -> even 2");
        assert_eq!(div_round_half_even(7, 2), 4, "3.5 -> even 4");
        assert_eq!(div_round_half_even(9, 2), 4, "4.5 -> even 4");
        assert_eq!(div_round_half_even(11, 2), 6, "5.5 -> even 6");
        assert_eq!(div_round_half_even(8, 3), 3, "2.66 -> 3");
        assert_eq!(div_round_half_even(7, 3), 2, "2.33 -> 2");
        assert_eq!(div_round_half_even(-5, 2), -2, "-2.5 -> even -2");
    }
}
