//! SRS-DATA-021 — apply corporate actions to paper strategy virtual positions
//! and orders.
//!
//! The acceptance criterion (docs/SRS.md SRS-DATA-021): *"Paper strategy virtual
//! positions and average cost are adjusted for splits, dividends, and mergers;
//! virtual orders for delisted securities are canceled using the same
//! corporate-action data source as live trading and backtesting."* This module is
//! the paper sibling of the live planners
//! (`atp-execution::corporate_action_positions`, SRS-DATA-020, and
//! `atp-execution::corporate_action_orders`, SRS-DATA-019) with one deliberate
//! difference: the paper engine OWNS its state, so instead of planning outcomes
//! for a deferred live wire to apply, [`apply_corporate_action`] transforms the
//! [`VirtualLedgerBook`] and [`VirtualOrderBook`] in place and reports what it
//! did.
//!
//! ## The same data source (the SYS-88 clause)
//!
//! [`actions_from_facts`] maps [`atp_data::CorporateActionFact`] — the
//! coverage-gated fact read `MarketDataStore::query_corporate_action_facts`, the
//! SAME store, gate, and crate-internal extraction path the backtest engine's
//! adjusted bar reads apply — onto [`PaperCorporateAction`] inputs. So paper
//! adjustment, live planning (whose composition roots map the same facts), and
//! backtest prices all derive from ONE corporate-action record set; no surface
//! grows a parallel parser that can drift.
//!
//! ## The money math is EXACT — identical to the live position planner
//!
//! Every position transform is an exact integer operation on minor units or it
//! fails closed to [`PaperPositionOutcomeKind::RequiresManualReview`] WITHOUT
//! mutating that position (StRS SN-1.14 requires the same corporate-action data
//! drive live, paper, and backtest — so the paper math is kept semantically
//! byte-stable with SRS-DATA-020):
//!
//!   * **Split `N`-for-`M`**: quantity scales by `N / M` and MUST divide evenly
//!     (a fractional residual is cash-in-lieu — review, never truncate); the
//!     total `cost_basis_minor` is INVARIANT (a split re-expresses the per-unit
//!     average, which `cost_basis / quantity` re-derives). Signed-safe: a short's
//!     reverse split yields a negative quantity, not a review.
//!   * **Cash dividend**: quantity unchanged; basis reduced ADDITIVELY by the
//!     actual dividend cash, `cost_basis' = cost_basis − amount_minor · quantity`
//!     (a long receives it, a short pays it) — value-conserving and exact, unlike
//!     a multiplicative price ratio factor, which does not transfer to a
//!     total-dollar basis. A dividend that would drive the basis through zero
//!     (relative to the quantity's sign) is a return-of-capital event this module
//!     does not book — review.
//!   * **Merger** (pure stock-for-stock): the position remaps to the successor at
//!     `quantity · N / M` (exact) with basis, realized P&L, and the cost
//!     decomposition carried intact; any cash leg is a partial disposition
//!     needing a realized-P&L booking — review. A remap onto a symbol the SAME
//!     strategy already holds a record for is a SuccessorCollision review
//!     (merging two bases and histories is the runtime's operation, never
//!     fabricated here).
//!   * **Symbol change**: a pure relabel of the ledger key (all components
//!     unchanged); the same collision guard.
//!   * **Delisting**: the position is REPORTED frozen
//!     ([`PaperPositionOutcomeKind::DelistedHold`], quantity + basis unchanged)
//!     and the operator is paged. `VirtualPosition` carries no status field (a
//!     formal frozen status is the live model's, SRS-DATA-020); the paper
//!     delisting's actionable half is the ORDER cancel below, and a delisted
//!     symbol has no market data to fill against.
//!
//! A FLAT position record (quantity 0 — a closed position holding only realized
//! history) is never transformed: it has no economic exposure to adjust, and
//! relabeling closed history onto a successor is bookkeeping the runtime owns.
//!
//! ## Virtual orders (the cancel clause)
//!
//! Orders on the affected symbol (canonical match) transform fail-closed, the
//! SRS-DATA-019 discipline:
//!
//!   * **Delisting / merger**: every open order is CANCELLED
//!     ([`VirtualOrderCancelReason::Delisting`] /
//!     [`VirtualOrderCancelReason::MergerTermination`]) — the acquired series
//!     terminates regardless of the conversion terms, so the merger cancel does
//!     not depend on term validity.
//!   * **Split**: quantity scales `N / M` exact-and-positive, limit/stop prices
//!     scale `M / N` rounded half-to-even ([`div_round_half_even`], byte-stable
//!     with `atp-data::normalization` and SRS-DATA-019) — any non-integral
//!     quantity, non-positive price, overflow, or non-positive factor CANCELS the
//!     order rather than leaving it resting on a stale basis.
//!   * **Symbol change**: the order relabels to the successor (a blank successor
//!     cancels — an order must not rest under a retired symbol).
//!   * **Dividend**: orders are unaffected — a cash dividend changes no share
//!     count, and a resting price is the trader's stated intent (SRS-DATA-019
//!     scopes order-affecting kinds the same way).
//!
//! An equity action never touches an option order: an OCC contract symbol
//! (`AAPL  240119C00190000`) is a DIFFERENT canonical symbol from its underlying
//! ticker, and OCC contract adjustment is its own (deferred) domain.
//!
//! ## Neutral emission
//!
//! Delistings, reviews, and order cancels page the operator through the FALLIBLE
//! [`PaperCorpActionAlertSink`] port (the `KillSwitchOperatorAlertSink` /
//! SRS-DATA-019/020 pattern): a missed page is itself a safety event, so
//! [`apply_and_emit`] surfaces every dispatch failure in
//! [`PaperCorpActionReport::alert_failures`] and continues (one bad channel
//! cannot suppress the rest). `atp-simulation` names no notification transport;
//! the concrete email/SMS binding is the composition root's (SRS-NOTIF-001).

use std::collections::HashMap;

use atp_data::CorporateActionFact;
use atp_types::StrategyId;

use crate::paper_order::{OrderLeg, OrderType};
use crate::virtual_ledger::{canonical_symbol, StrategyLedger, VirtualLedgerBook, VirtualPosition};
use crate::virtual_orders::{VirtualOrderBook, VirtualOrderCancelReason, VirtualOrderId};

/// A corporate action bound to the symbol it affects — the paper application
/// input. Publicly constructible; every factor and term is re-validated at apply
/// time, so an invalid input can never divide-by-zero or silently miscompute.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PaperCorporateAction {
    /// The affected security. A position/order is transformed only when its
    /// symbol matches this (canonically) — a mixed book never cross-contaminates.
    pub symbol: String,
    /// What happened to the security.
    pub kind: PaperCorporateActionKind,
}

impl PaperCorporateAction {
    /// An `N`-for-`M` split (forward when `N > M`, reverse when `N < M`).
    pub fn split(symbol: impl Into<String>, numerator: i64, denominator: i64) -> Self {
        Self {
            symbol: symbol.into(),
            kind: PaperCorporateActionKind::Split {
                numerator,
                denominator,
            },
        }
    }

    /// A cash dividend of `amount_minor` per share, referenced against
    /// `prev_close_minor` (used only for the fail-closed sanity guard, not the
    /// additive basis math).
    pub fn dividend(symbol: impl Into<String>, amount_minor: i64, prev_close_minor: i64) -> Self {
        Self {
            symbol: symbol.into(),
            kind: PaperCorporateActionKind::Dividend {
                amount_minor,
                prev_close_minor,
            },
        }
    }

    /// A merger into `successor` at `numerator` successor shares per
    /// `denominator` acquired shares, with `cash_per_share_minor` cash per
    /// acquired share (`0` for the pure stock-for-stock case, the only one a
    /// position adjusts under; orders on the acquired symbol cancel regardless).
    pub fn merger(
        symbol: impl Into<String>,
        successor: impl Into<String>,
        numerator: i64,
        denominator: i64,
        cash_per_share_minor: i64,
    ) -> Self {
        Self {
            symbol: symbol.into(),
            kind: PaperCorporateActionKind::Merger {
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
            kind: PaperCorporateActionKind::SymbolChange {
                successor: successor.into(),
            },
        }
    }

    /// A delisting — held positions are reported frozen and every open virtual
    /// order on the symbol is cancelled.
    pub fn delisting(symbol: impl Into<String>) -> Self {
        Self {
            symbol: symbol.into(),
            kind: PaperCorporateActionKind::Delisting,
        }
    }
}

/// The class of corporate action the paper books can be affected by — the same
/// taxonomy as the live position planner (SRS-DATA-020). (A *stock* dividend is
/// economically a split — map it to [`Split`](Self::Split).)
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PaperCorporateActionKind {
    /// An `N`-for-`M` split. Both factors are validated `> 0` at apply time.
    Split { numerator: i64, denominator: i64 },
    /// A cash dividend of `amount_minor` per share; `prev_close_minor` is the
    /// sanity reference (a dividend `>=` it is anomalous).
    Dividend {
        amount_minor: i64,
        prev_close_minor: i64,
    },
    /// A merger into `successor`. Positions adjust only for the pure
    /// stock-for-stock case (`cash_per_share_minor == 0`, `numerator > 0`);
    /// orders on the acquired symbol cancel regardless of the terms.
    Merger {
        successor: String,
        numerator: i64,
        denominator: i64,
        cash_per_share_minor: i64,
    },
    /// A relabel to `successor` (all position components unchanged).
    SymbolChange { successor: String },
    /// The security was delisted.
    Delisting,
}

/// Map the coverage-gated corporate-action FACTS (the
/// `MarketDataStore::query_corporate_action_facts` read — the same
/// corporate-action data source live trading's planners and the backtest
/// engine's adjusted reads consume) onto paper application inputs, preserving
/// the facts' `effective_ts`-ascending order so the caller applies them in
/// event sequence. Total: every fact variant has exactly one mapping.
pub fn actions_from_facts(facts: &[CorporateActionFact]) -> Vec<PaperCorporateAction> {
    facts
        .iter()
        .map(|fact| match fact {
            CorporateActionFact::Split {
                symbol,
                numerator,
                denominator,
                ..
            } => PaperCorporateAction::split(symbol.clone(), *numerator, *denominator),
            CorporateActionFact::Dividend {
                symbol,
                amount_minor,
                prev_close_minor,
                ..
            } => PaperCorporateAction::dividend(symbol.clone(), *amount_minor, *prev_close_minor),
            CorporateActionFact::Merger {
                symbol,
                successor,
                numerator,
                denominator,
                cash_per_share_minor,
                ..
            } => PaperCorporateAction::merger(
                symbol.clone(),
                successor.clone(),
                *numerator,
                *denominator,
                *cash_per_share_minor,
            ),
            CorporateActionFact::SymbolChange {
                predecessor,
                successor,
                ..
            } => PaperCorporateAction::symbol_change(predecessor.clone(), successor.clone()),
            CorporateActionFact::Delisting { symbol, .. } => {
                PaperCorporateAction::delisting(symbol.clone())
            }
        })
        .collect()
}

/// Why a corporate action could not be applied to a paper position and was
/// flagged for the operator instead — the structured reason the alert carries
/// (the SRS-DATA-020 taxonomy; the position is left untouched).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PaperReviewReason {
    /// A split / merger carried a non-positive numerator or denominator.
    NonPositiveFactor { numerator: i64, denominator: i64 },
    /// A ratio did not divide the share count into a whole number — the
    /// fractional residual is cash-in-lieu, so the position is flagged rather
    /// than silently truncated.
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
    /// A dividend that would drive the basis through zero — a per-share amount
    /// `>=` the reference close, or a total dividend exceeding the basis (a
    /// return-of-capital event with realized-gain implications not booked here).
    BasisCrossingDividend {
        amount_minor: i64,
        prev_close_minor: i64,
    },
    /// A merger carried a cash consideration — a disposition needing a
    /// realized-P&L booking not derivable from the terms alone.
    CashConsiderationNotSupported { cash_per_share_minor: i64 },
    /// A merger / symbol-change successor is blank or equals the predecessor.
    InvalidSuccessor { successor: String },
    /// A merger / symbol-change would remap a position onto a symbol this
    /// strategy already holds a record for — merging two bases (or a closed
    /// record's history) is the runtime's operation, not fabricated here.
    SuccessorCollision { successor: String },
}

impl PaperReviewReason {
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

/// What happened to ONE strategy's position in the affected symbol. Positions
/// the action does not affect (other symbols, other strategies, flat records)
/// produce no outcome — the report enumerates work done, not the whole book.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PaperPositionOutcome {
    /// The strategy whose position this outcome concerns.
    pub strategy: StrategyId,
    /// The canonical symbol the position was held under before the action.
    pub symbol: String,
    pub kind: PaperPositionOutcomeKind,
}

/// The per-position result kind (the SRS-DATA-020 outcome taxonomy, applied).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PaperPositionOutcomeKind {
    /// Quantity and/or basis re-computed in place (split, dividend).
    Adjusted {
        quantity_before: i64,
        quantity_after: i64,
        cost_basis_before_minor: i128,
        cost_basis_after_minor: i128,
    },
    /// Remapped to a successor security (merger stock-for-stock, symbol change),
    /// realized P&L and cost decomposition carried intact.
    Remapped {
        successor: String,
        quantity_after: i64,
        cost_basis_after_minor: i128,
    },
    /// The security was delisted: the position is REPORTED frozen (quantity +
    /// basis unchanged — there is no market to fill against) and the operator
    /// paged. The actionable half of a paper delisting is the order cancel.
    DelistedHold {
        quantity: i64,
        cost_basis_minor: i128,
    },
    /// The action could not be applied — the position is untouched and the
    /// operator paged with a structured reason.
    RequiresManualReview { reason: PaperReviewReason },
}

/// What happened to ONE virtual resting order under the action. Unaffected
/// orders (other symbols, already-cancelled) produce no outcome.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PaperOrderOutcome {
    pub id: VirtualOrderId,
    /// The strategy the order belongs to.
    pub strategy: StrategyId,
    /// The canonical symbol the order rested on before the action.
    pub symbol: String,
    pub kind: PaperOrderOutcomeKind,
}

/// The per-order result kind.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PaperOrderOutcomeKind {
    /// The order's quantity and prices were re-based (split) or its symbol relabeled
    /// (symbol change); it keeps resting.
    Adjusted { leg_after: OrderLeg },
    /// The order was terminally cancelled (fail-closed).
    Cancelled { reason: VirtualOrderCancelReason },
}

/// The class of a [`PaperCorpActionAlert`] — what the operator is paged about.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PaperAlertReason {
    /// A delisted security is still held; the position is frozen by report.
    DelistedHold,
    /// A corporate action could not be applied to a position and needs manual
    /// review.
    ManualReview(PaperReviewReason),
    /// A virtual resting order was cancelled by the corporate-action path.
    OrderCancelled(VirtualOrderCancelReason),
}

impl PaperAlertReason {
    /// Stable wire discriminator for the alert class.
    pub const fn kind_str(&self) -> &'static str {
        match self {
            Self::DelistedHold => "DELISTED_HOLD",
            Self::ManualReview(_) => "MANUAL_REVIEW",
            Self::OrderCancelled(_) => "ORDER_CANCELLED",
        }
    }
}

/// A source-neutral operator alert for a paper corporate-action event — the
/// value the composition root maps onto the notification subsystem
/// (SRS-NOTIF-001), mirroring the SRS-DATA-019/020 alert shapes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PaperCorpActionAlert {
    /// The paper strategy the alert concerns.
    pub strategy: StrategyId,
    /// The affected canonical symbol.
    pub symbol: String,
    pub reason: PaperAlertReason,
}

impl PaperCorpActionAlert {
    /// The operator-facing, non-secret summary — the notification trigger text.
    pub fn operator_summary(&self) -> String {
        let strategy = self.strategy.as_str();
        let symbol = &self.symbol;
        match &self.reason {
            PaperAlertReason::DelistedHold => format!(
                "paper strategy {strategy} still holds delisted {symbol}: position frozen, \
                 no further fills possible"
            ),
            PaperAlertReason::ManualReview(reason) => format!(
                "paper strategy {strategy} position {symbol} needs review: {}",
                reason.as_str()
            ),
            PaperAlertReason::OrderCancelled(reason) => format!(
                "paper strategy {strategy} virtual order on {symbol} cancelled by corporate \
                 action: {}",
                reason.as_str()
            ),
        }
    }
}

/// The neutral port the composition root binds to route a paper corporate-action
/// alert onto the real notification subsystem. Declared here (never a dependency
/// on `atp-notification`), for the same SRS-ARCH-002 reason as
/// `KillSwitchOperatorAlertSink` and the SRS-DATA-019/020 sinks.
pub trait PaperCorpActionAlertSink {
    /// Dispatch one alert to the operator. **Fallible** — a missed page on a
    /// cancel or review is itself a safety event, so a transport failure is
    /// surfaced rather than silently swallowed.
    fn dispatch(&self, alert: PaperCorpActionAlert) -> Result<(), PaperAlertError>;
}

/// A failure to dispatch a paper corporate-action alert (the typed transport
/// taxonomy is SRS-NOTIF-001's).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PaperAlertError {
    pub reason: String,
}

impl PaperAlertError {
    pub fn new(reason: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
        }
    }
}

/// An alert whose dispatch failed — surfaced by [`apply_and_emit`] so the
/// composition root escalates a missed page rather than treating the event as
/// fully notified.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PaperAlertFailure {
    pub strategy: StrategyId,
    pub symbol: String,
    pub error: PaperAlertError,
}

/// The result of applying one corporate action to the paper books: what changed
/// (positions, orders) and any operator alerts that FAILED to dispatch
/// (surfaced, never swallowed).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PaperCorpActionReport {
    /// Per-position outcomes, `(strategy, symbol)`-ascending (deterministic).
    pub position_outcomes: Vec<PaperPositionOutcome>,
    /// Per-order outcomes, placement (id) order.
    pub order_outcomes: Vec<PaperOrderOutcome>,
    /// Alerts whose dispatch failed (empty for [`apply_corporate_action`], which
    /// dispatches nothing).
    pub alert_failures: Vec<PaperAlertFailure>,
}

impl PaperCorpActionReport {
    /// Every operator alert this report's outcomes warrant (delisted holds,
    /// reviews, order cancels), in the report's deterministic order.
    pub fn alerts(&self) -> Vec<PaperCorpActionAlert> {
        let mut alerts = Vec::new();
        for outcome in &self.position_outcomes {
            let reason = match &outcome.kind {
                PaperPositionOutcomeKind::DelistedHold { .. } => PaperAlertReason::DelistedHold,
                PaperPositionOutcomeKind::RequiresManualReview { reason } => {
                    PaperAlertReason::ManualReview(reason.clone())
                }
                PaperPositionOutcomeKind::Adjusted { .. }
                | PaperPositionOutcomeKind::Remapped { .. } => continue,
            };
            alerts.push(PaperCorpActionAlert {
                strategy: outcome.strategy.clone(),
                symbol: outcome.symbol.clone(),
                reason,
            });
        }
        for outcome in &self.order_outcomes {
            if let PaperOrderOutcomeKind::Cancelled { reason } = &outcome.kind {
                alerts.push(PaperCorpActionAlert {
                    strategy: outcome.strategy.clone(),
                    symbol: outcome.symbol.clone(),
                    reason: PaperAlertReason::OrderCancelled(reason.clone()),
                });
            }
        }
        alerts
    }
}

/// Fail modes of a single scaling step (shared by the position and order paths).
enum ScaleFail {
    Overflow,
    NotIntegral,
    NonPositive,
}

/// What the position transform decided, before it is applied to the ledger map.
enum PositionPlan {
    Rebase {
        quantity: i64,
        basis: i128,
    },
    Remap {
        successor: String,
        quantity: i64,
        basis: i128,
    },
    DelistedHold,
    Review(PaperReviewReason),
    NoOp,
}

/// Apply ONE corporate action to every paper strategy's virtual positions and
/// resting orders, in place. Per-position and per-order fail-closed: a position
/// whose transform cannot be computed exactly is left untouched (reported for
/// review); an order that cannot be re-based exactly is cancelled. Sequencing
/// multiple actions is the caller's responsibility — apply them in
/// `effective_ts` order (the order [`actions_from_facts`] preserves).
pub fn apply_corporate_action(
    book: &mut VirtualLedgerBook,
    orders: &mut VirtualOrderBook,
    action: &PaperCorporateAction,
) -> PaperCorpActionReport {
    let target = canonical_symbol(&action.symbol);
    let mut report = PaperCorpActionReport::default();
    if target.is_empty() {
        // A blank action symbol can match nothing (ledger keys are non-empty
        // canonical symbols); apply nothing rather than guess.
        return report;
    }

    apply_to_positions(book, action, &target, &mut report);
    apply_to_orders(orders, action, &target, &mut report);
    report
}

/// [`apply_corporate_action`], then dispatch every warranted operator alert
/// through `sink`. A dispatch that fails is recorded in
/// [`PaperCorpActionReport::alert_failures`] (never swallowed) and does NOT
/// abort the remaining dispatches — continue-to-safety.
pub fn apply_and_emit<S: PaperCorpActionAlertSink>(
    book: &mut VirtualLedgerBook,
    orders: &mut VirtualOrderBook,
    action: &PaperCorporateAction,
    sink: &S,
) -> PaperCorpActionReport {
    let mut report = apply_corporate_action(book, orders, action);
    for alert in report.alerts() {
        let strategy = alert.strategy.clone();
        let symbol = alert.symbol.clone();
        if let Err(error) = sink.dispatch(alert) {
            report.alert_failures.push(PaperAlertFailure {
                strategy,
                symbol,
                error,
            });
        }
    }
    report
}

// --------------------------------------------------------------------------- //
// Positions
// --------------------------------------------------------------------------- //

fn apply_to_positions(
    book: &mut VirtualLedgerBook,
    action: &PaperCorporateAction,
    target: &str,
    report: &mut PaperCorpActionReport,
) {
    // Deterministic strategy order (HashMap iteration order is unspecified).
    let mut strategies: Vec<StrategyId> = book
        .ledgers_iter()
        .map(|(strategy, _)| strategy.clone())
        .collect();
    strategies.sort_by(|left, right| left.as_str().cmp(right.as_str()));

    let mut new_ledgers: HashMap<StrategyId, StrategyLedger> = HashMap::new();
    for strategy in strategies {
        let ledger = book
            .ledger(&strategy)
            .expect("strategy id was just enumerated from the book");
        let mut positions: HashMap<String, VirtualPosition> = ledger
            .positions_iter()
            .map(|(symbol, position)| (symbol.clone(), position.clone()))
            .collect();

        // A FLAT record (quantity 0) holds only closed history — no exposure to
        // adjust, so it is deliberately not transformed (module docs).
        let held = positions
            .get(target)
            .filter(|position| position.quantity() != 0)
            .cloned();
        if let Some(position) = held {
            let plan = plan_position(&position, &positions, target, &action.kind);
            let kind = match plan {
                PositionPlan::NoOp => None,
                PositionPlan::Review(reason) => {
                    Some(PaperPositionOutcomeKind::RequiresManualReview { reason })
                }
                PositionPlan::DelistedHold => Some(PaperPositionOutcomeKind::DelistedHold {
                    quantity: position.quantity(),
                    cost_basis_minor: position.cost_basis_minor(),
                }),
                PositionPlan::Rebase { quantity, basis } => {
                    let rebased = rebuilt(&position, quantity, basis);
                    positions.insert(target.to_string(), rebased);
                    Some(PaperPositionOutcomeKind::Adjusted {
                        quantity_before: position.quantity(),
                        quantity_after: quantity,
                        cost_basis_before_minor: position.cost_basis_minor(),
                        cost_basis_after_minor: basis,
                    })
                }
                PositionPlan::Remap {
                    successor,
                    quantity,
                    basis,
                } => {
                    let remapped = rebuilt(&position, quantity, basis);
                    positions.remove(target);
                    positions.insert(successor.clone(), remapped);
                    Some(PaperPositionOutcomeKind::Remapped {
                        successor,
                        quantity_after: quantity,
                        cost_basis_after_minor: basis,
                    })
                }
            };
            if let Some(kind) = kind {
                report.position_outcomes.push(PaperPositionOutcome {
                    strategy: strategy.clone(),
                    symbol: target.to_string(),
                    kind,
                });
            }
        }
        new_ledgers.insert(strategy, StrategyLedger::from_positions(positions));
    }
    *book = VirtualLedgerBook::from_ledgers(new_ledgers);
}

/// Decide the transform for one strategy's non-flat position in the affected
/// symbol. Pure; `positions` is that strategy's full (canonical-keyed) map, used
/// only for the successor-collision guard.
fn plan_position(
    position: &VirtualPosition,
    positions: &HashMap<String, VirtualPosition>,
    target: &str,
    kind: &PaperCorporateActionKind,
) -> PositionPlan {
    match kind {
        PaperCorporateActionKind::Delisting => PositionPlan::DelistedHold,
        PaperCorporateActionKind::Split {
            numerator,
            denominator,
        } => plan_split(position, *numerator, *denominator),
        PaperCorporateActionKind::Dividend {
            amount_minor,
            prev_close_minor,
        } => plan_dividend(position, *amount_minor, *prev_close_minor),
        PaperCorporateActionKind::Merger {
            successor,
            numerator,
            denominator,
            cash_per_share_minor,
        } => {
            if *cash_per_share_minor != 0 || *numerator == 0 {
                return PositionPlan::Review(PaperReviewReason::CashConsiderationNotSupported {
                    cash_per_share_minor: *cash_per_share_minor,
                });
            }
            if *numerator <= 0 || *denominator <= 0 {
                return PositionPlan::Review(PaperReviewReason::NonPositiveFactor {
                    numerator: *numerator,
                    denominator: *denominator,
                });
            }
            let quantity =
                match scale_quantity_signed(position.quantity(), *numerator, *denominator) {
                    Ok(quantity) => quantity,
                    Err(fail) => {
                        return scale_fail_review(position, *numerator, *denominator, fail)
                    }
                };
            plan_remap(position, positions, target, successor, quantity)
        }
        PaperCorporateActionKind::SymbolChange { successor } => {
            if canonical_symbol(successor) == target {
                return PositionPlan::NoOp;
            }
            plan_remap(position, positions, target, successor, position.quantity())
        }
    }
}

/// Split `N`-for-`M`: quantity scales exactly, total basis INVARIANT.
fn plan_split(position: &VirtualPosition, numerator: i64, denominator: i64) -> PositionPlan {
    if numerator <= 0 || denominator <= 0 {
        return PositionPlan::Review(PaperReviewReason::NonPositiveFactor {
            numerator,
            denominator,
        });
    }
    if numerator == denominator {
        return PositionPlan::NoOp;
    }
    match scale_quantity_signed(position.quantity(), numerator, denominator) {
        Ok(quantity) => PositionPlan::Rebase {
            quantity,
            basis: position.cost_basis_minor(),
        },
        Err(fail) => scale_fail_review(position, numerator, denominator, fail),
    }
}

/// Cash dividend: quantity unchanged, basis reduced additively by the actual
/// dividend cash (`cost_basis − amount · quantity`).
fn plan_dividend(
    position: &VirtualPosition,
    amount_minor: i64,
    prev_close_minor: i64,
) -> PositionPlan {
    if amount_minor <= 0 || prev_close_minor <= 0 {
        return PositionPlan::Review(PaperReviewReason::InvalidDividendTerm {
            amount_minor,
            prev_close_minor,
        });
    }
    if amount_minor >= prev_close_minor {
        return PositionPlan::Review(PaperReviewReason::BasisCrossingDividend {
            amount_minor,
            prev_close_minor,
        });
    }
    let cash = match i128::from(amount_minor).checked_mul(i128::from(position.quantity())) {
        Some(cash) => cash,
        None => {
            return PositionPlan::Review(PaperReviewReason::Overflow {
                context: "dividend cash",
            })
        }
    };
    let basis = match position.cost_basis_minor().checked_sub(cash) {
        Some(basis) => basis,
        None => {
            return PositionPlan::Review(PaperReviewReason::Overflow {
                context: "dividend basis",
            })
        }
    };
    // A dividend large enough to flip the basis sign relative to the quantity
    // would imply a negative average cost — a return-of-capital event this module
    // does not book; flag it rather than fabricate.
    if basis != 0 && basis.signum() != i128::from(position.quantity()).signum() {
        return PositionPlan::Review(PaperReviewReason::BasisCrossingDividend {
            amount_minor,
            prev_close_minor,
        });
    }
    PositionPlan::Rebase {
        quantity: position.quantity(),
        basis,
    }
}

/// Remap to `successor` at `quantity` (basis + history intact), with the
/// successor validity and same-strategy collision guards.
fn plan_remap(
    position: &VirtualPosition,
    positions: &HashMap<String, VirtualPosition>,
    target: &str,
    successor: &str,
    quantity: i64,
) -> PositionPlan {
    let successor_canonical = canonical_symbol(successor);
    if successor_canonical.is_empty() || successor_canonical == target {
        return PositionPlan::Review(PaperReviewReason::InvalidSuccessor {
            successor: successor.to_string(),
        });
    }
    // ANY existing record under the successor key — open or flat — collides: an
    // open one would need a basis merge, a flat one holds closed history that a
    // relabel would overwrite. Both are the runtime's operations.
    if positions.contains_key(&successor_canonical) {
        return PositionPlan::Review(PaperReviewReason::SuccessorCollision {
            successor: successor_canonical,
        });
    }
    PositionPlan::Remap {
        successor: successor_canonical,
        quantity,
        basis: position.cost_basis_minor(),
    }
}

fn scale_fail_review(
    position: &VirtualPosition,
    numerator: i64,
    denominator: i64,
    fail: ScaleFail,
) -> PositionPlan {
    PositionPlan::Review(match fail {
        ScaleFail::NotIntegral => PaperReviewReason::QuantityNotIntegral {
            before: position.quantity(),
            numerator,
            denominator,
        },
        ScaleFail::Overflow | ScaleFail::NonPositive => PaperReviewReason::Overflow {
            context: "scaled quantity",
        },
    })
}

/// The position rebuilt at a new quantity + basis with its realized P&L and full
/// transaction-cost decomposition carried INTACT — a corporate action re-expresses
/// exposure, it never creates or destroys recognized history.
fn rebuilt(position: &VirtualPosition, quantity: i64, basis: i128) -> VirtualPosition {
    VirtualPosition::from_components(
        quantity,
        basis,
        position.realized_pnl_minor(),
        position.commission_paid_minor(),
        position.slippage_paid_minor(),
        position.spread_impact_paid_minor(),
    )
}

/// A signed QUANTITY scaled by `numerator / denominator`, EXACT — non-integral
/// fails closed (cash-in-lieu). Signed-safe: a negative result (a short) is a
/// valid quantity, NOT a failure (the SRS-DATA-020 position discipline). Both
/// factors are `> 0` (validated by the caller).
fn scale_quantity_signed(
    quantity: i64,
    numerator: i64,
    denominator: i64,
) -> Result<i64, ScaleFail> {
    let scaled = i128::from(quantity)
        .checked_mul(i128::from(numerator))
        .ok_or(ScaleFail::Overflow)?;
    if scaled % i128::from(denominator) != 0 {
        return Err(ScaleFail::NotIntegral);
    }
    i64::try_from(scaled / i128::from(denominator)).map_err(|_| ScaleFail::Overflow)
}

// --------------------------------------------------------------------------- //
// Orders
// --------------------------------------------------------------------------- //

fn apply_to_orders(
    orders: &mut VirtualOrderBook,
    action: &PaperCorporateAction,
    target: &str,
    report: &mut PaperCorpActionReport,
) {
    for order in orders.orders_mut() {
        if !order.is_open() || canonical_symbol(&order.leg().symbol) != target {
            continue;
        }
        let decision = plan_order(order.leg(), &action.kind);
        let kind = match decision {
            OrderPlan::NoOp => continue,
            OrderPlan::Cancel(reason) => {
                order.cancel(reason.clone());
                PaperOrderOutcomeKind::Cancelled { reason }
            }
            OrderPlan::Replace(leg) => {
                order.set_leg(leg.clone());
                PaperOrderOutcomeKind::Adjusted { leg_after: leg }
            }
        };
        report.order_outcomes.push(PaperOrderOutcome {
            id: order.id(),
            strategy: order.strategy().clone(),
            symbol: target.to_string(),
            kind,
        });
    }
}

enum OrderPlan {
    NoOp,
    Cancel(VirtualOrderCancelReason),
    Replace(OrderLeg),
}

/// Decide the transform for one OPEN order on the affected symbol. Pure.
fn plan_order(leg: &OrderLeg, kind: &PaperCorporateActionKind) -> OrderPlan {
    match kind {
        // A cash dividend changes no share count; the resting price is the
        // trader's stated intent (SRS-DATA-019 scopes order-affecting kinds the
        // same way).
        PaperCorporateActionKind::Dividend { .. } => OrderPlan::NoOp,
        PaperCorporateActionKind::Delisting => {
            OrderPlan::Cancel(VirtualOrderCancelReason::Delisting)
        }
        // The acquired series terminates whatever the conversion terms are, so
        // the cancel does not depend on their validity (fail-closed).
        PaperCorporateActionKind::Merger { .. } => {
            OrderPlan::Cancel(VirtualOrderCancelReason::MergerTermination)
        }
        PaperCorporateActionKind::SymbolChange { successor } => {
            let successor_canonical = canonical_symbol(successor);
            if successor_canonical.is_empty() {
                return OrderPlan::Cancel(VirtualOrderCancelReason::InvalidSuccessor);
            }
            if successor_canonical == canonical_symbol(&leg.symbol) {
                return OrderPlan::NoOp;
            }
            let mut relabeled = leg.clone();
            relabeled.symbol = successor_canonical;
            OrderPlan::Replace(relabeled)
        }
        PaperCorporateActionKind::Split {
            numerator,
            denominator,
        } => plan_order_split(leg, *numerator, *denominator),
    }
}

/// Split an open order `N`-for-`M`: quantity scales `N / M` exact-and-positive,
/// limit/stop prices scale `M / N` rounded half-to-even — any failure cancels
/// (the SRS-DATA-019 resting-order discipline).
fn plan_order_split(leg: &OrderLeg, numerator: i64, denominator: i64) -> OrderPlan {
    if numerator <= 0 || denominator <= 0 {
        return OrderPlan::Cancel(VirtualOrderCancelReason::NonPositiveFactor {
            numerator,
            denominator,
        });
    }
    if numerator == denominator {
        return OrderPlan::NoOp;
    }
    let num = i128::from(numerator);
    let den = i128::from(denominator);
    let quantity = match scale_order_quantity(leg.quantity, num, den) {
        Ok(quantity) => quantity,
        Err(ScaleFail::NotIntegral) => {
            return OrderPlan::Cancel(VirtualOrderCancelReason::QuantityNotIntegral {
                before: leg.quantity,
                numerator,
                denominator,
            })
        }
        Err(ScaleFail::Overflow | ScaleFail::NonPositive) => {
            return OrderPlan::Cancel(VirtualOrderCancelReason::Overflow {
                context: "split quantity",
            })
        }
    };
    let order_type = match scale_order_prices(leg.order_type, num, den) {
        Ok(order_type) => order_type,
        Err(cancel) => return OrderPlan::Cancel(cancel),
    };
    let mut adjusted = leg.clone();
    adjusted.quantity = quantity;
    adjusted.order_type = order_type;
    OrderPlan::Replace(adjusted)
}

/// An order QUANTITY scaled by `NUM / DEN`, exact and strictly positive (order
/// quantities are unsigned-by-invariant; direction lives in `Side`).
fn scale_order_quantity(quantity: i64, num: i128, den: i128) -> Result<i64, ScaleFail> {
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

/// Every trigger/limit price on the order type scaled by `DEN / NUM`, rounded
/// half-to-even, failing closed to a cancel reason.
fn scale_order_prices(
    order_type: OrderType,
    num: i128,
    den: i128,
) -> Result<OrderType, VirtualOrderCancelReason> {
    let scale = |price_minor: i64, field: &'static str| {
        let scaled =
            i128::from(price_minor)
                .checked_mul(den)
                .ok_or(VirtualOrderCancelReason::Overflow {
                    context: "split price",
                })?;
        let rounded = div_round_half_even(scaled, num);
        let value = i64::try_from(rounded).map_err(|_| VirtualOrderCancelReason::Overflow {
            context: "split price",
        })?;
        if value <= 0 {
            return Err(VirtualOrderCancelReason::PriceRoundedNonPositive { field });
        }
        Ok(value)
    };
    Ok(match order_type {
        OrderType::Market => OrderType::Market,
        OrderType::Limit { limit_price_minor } => OrderType::Limit {
            limit_price_minor: scale(limit_price_minor, "limit")?,
        },
        OrderType::Stop { stop_price_minor } => OrderType::Stop {
            stop_price_minor: scale(stop_price_minor, "stop")?,
        },
        OrderType::StopLimit {
            stop_price_minor,
            limit_price_minor,
        } => OrderType::StopLimit {
            stop_price_minor: scale(stop_price_minor, "stop")?,
            limit_price_minor: scale(limit_price_minor, "limit")?,
        },
    })
}

/// `numer / denom` rounded half-to-even (banker's rounding), integer-exact.
/// Copied BYTE-STABLE from `atp-data::normalization::div_round_half_even` (and
/// `atp-execution::corporate_action_orders`) so the paper order-adjustment
/// rounding cannot drift from the historical / live basis (StRS SN-1.14 requires
/// the same corporate-action data drive all three). `denom` MUST be `> 0`
/// (guaranteed: it is a validated-positive split factor).
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
    use crate::paper_order::{AssetClass, Side};
    use crate::sim::PaperFill;

    /// A book with `strategy` holding `quantity` of `symbol` at an exact basis,
    /// built through the REAL fill path (so the fixture cannot drift from the
    /// ledger's invariants).
    fn book_with(
        strategy: &str,
        symbol: &str,
        quantity: i64,
        price_minor: i64,
    ) -> VirtualLedgerBook {
        let mut book = VirtualLedgerBook::new();
        book.apply_fill(
            &StrategyId::new(strategy),
            &fill(symbol, quantity, price_minor),
        )
        .expect("fixture fill applies");
        book
    }

    fn fill(symbol: &str, quantity: i64, price_minor: i64) -> PaperFill {
        let notional = i128::from(quantity) * i128::from(price_minor);
        PaperFill {
            ts: 1,
            symbol: symbol.to_string(),
            quantity,
            price_minor,
            commission_minor: 0,
            slippage_minor: 0,
            spread_impact_minor: 0,
            cash_delta_minor: i64::try_from(-notional).expect("fixture fits"),
        }
    }

    fn market_leg(symbol: &str, quantity: i64) -> OrderLeg {
        OrderLeg {
            symbol: symbol.to_string(),
            asset_class: AssetClass::Equity,
            side: Side::Buy,
            quantity,
            order_type: OrderType::Market,
        }
    }

    #[test]
    fn split_scales_quantity_keeps_basis_and_history() {
        let mut book = book_with("alpha", "AAPL", 100, 5_000);
        let mut orders = VirtualOrderBook::new();
        let report = apply_corporate_action(
            &mut book,
            &mut orders,
            &PaperCorporateAction::split("AAPL", 4, 1),
        );
        assert_eq!(report.position_outcomes.len(), 1);
        let position = book
            .position(&StrategyId::new("alpha"), "AAPL")
            .expect("still held");
        assert_eq!(position.quantity(), 400);
        assert_eq!(position.cost_basis_minor(), 500_000, "basis invariant");
        assert_eq!(position.average_cost_minor(), Some(1_250));
    }

    #[test]
    fn dividend_reduces_basis_additively() {
        let mut book = book_with("alpha", "AAPL", 100, 5_000);
        let mut orders = VirtualOrderBook::new();
        apply_corporate_action(
            &mut book,
            &mut orders,
            &PaperCorporateAction::dividend("AAPL", 100, 4_000),
        );
        let position = book
            .position(&StrategyId::new("alpha"), "AAPL")
            .expect("still held");
        assert_eq!(position.quantity(), 100);
        assert_eq!(position.cost_basis_minor(), 490_000);
    }

    #[test]
    fn review_leaves_the_position_untouched() {
        let mut book = book_with("alpha", "AAPL", 101, 5_000);
        let mut orders = VirtualOrderBook::new();
        let report = apply_corporate_action(
            &mut book,
            &mut orders,
            &PaperCorporateAction::split("AAPL", 1, 2),
        );
        assert!(matches!(
            report.position_outcomes[0].kind,
            PaperPositionOutcomeKind::RequiresManualReview {
                reason: PaperReviewReason::QuantityNotIntegral { .. }
            }
        ));
        let position = book
            .position(&StrategyId::new("alpha"), "AAPL")
            .expect("still held");
        assert_eq!(position.quantity(), 101, "untouched on review");
    }

    #[test]
    fn delisting_cancels_orders_and_reports_the_hold() {
        let mut book = book_with("alpha", "DEAD", 100, 5_000);
        let mut orders = VirtualOrderBook::new();
        let strategy = StrategyId::new("alpha");
        orders
            .place(&strategy, market_leg("DEAD", 10))
            .expect("valid order");
        orders
            .place(&strategy, market_leg("LIVE", 10))
            .expect("valid order");
        let report = apply_corporate_action(
            &mut book,
            &mut orders,
            &PaperCorporateAction::delisting("DEAD"),
        );
        assert!(matches!(
            report.position_outcomes[0].kind,
            PaperPositionOutcomeKind::DelistedHold { quantity: 100, .. }
        ));
        assert_eq!(report.order_outcomes.len(), 1, "only DEAD orders cancel");
        assert_eq!(orders.open_count(), 1, "LIVE order keeps resting");
        let alerts = report.alerts();
        assert_eq!(alerts.len(), 2, "hold page + cancel page");
    }
}
