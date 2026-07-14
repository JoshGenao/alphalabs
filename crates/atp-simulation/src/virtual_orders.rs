//! SRS-DATA-021 — the paper engine's virtual RESTING-order store.
//!
//! The SRS-SIM-001 intake path routes a paper order to the internal simulation
//! engine without retaining it, and the SRS-SIM-002 fill path decides a fill from
//! a routed order plus a market snapshot — so until now no accepted-but-unfilled
//! paper order existed anywhere (the SRS-SIM-004 snapshot reserves an always-empty
//! slot for exactly this store). SRS-DATA-021's acceptance criterion makes the
//! resting order a real thing the corporate-action path must reach ("virtual
//! orders for delisted securities are canceled"), so this module introduces the
//! store: a [`VirtualOrderBook`] of [`VirtualRestingOrder`]s, each an intake-valid
//! [`OrderLeg`] held for a [`StrategyId`] under a book-assigned
//! [`VirtualOrderId`], either [`VirtualOrderStatus::Open`] or terminally
//! [`VirtualOrderStatus::Cancelled`] with a structured
//! [`VirtualOrderCancelReason`].
//!
//! An order enters the book through the ENGINE'S OWN intake:
//! [`VirtualOrderBook::place_accepted`] submits a [`PaperOrderRequest`] through
//! the real [`PaperSimulationEngine::accept_order`] (the SRS-SIM-001 validation
//! + internal-simulation routing every paper order takes) and rests each
//! accepted leg — so a corporate action reaches exactly the orders the intake
//! path accepted, never a shape constructed around it. The single-leg
//! [`VirtualOrderBook::place`] primitive applies the same `validate_leg`
//! authority (one rule, not a copy). Symbols are matched CANONICALLY (trim +
//! upper-case, the [`crate::virtual_ledger`] policy) at corporate-action time,
//! but the leg is held verbatim — the same discipline as the live
//! `OrderSubmission` path (SRS-DATA-019). A cancelled order is TERMINAL: no
//! later corporate action or cancel reaches it, and the book never deletes it
//! (an auditable record, not a tombstone).
//!
//! Like the SYS-84 [`crate::virtual_ledger::VirtualLedgerBook`] (SRS-SIM-003) —
//! which is fed by the engine's real fill output and held by the caller — this
//! book is CALLER-HELD state fed by the engine's real intake output: the
//! stateless [`PaperSimulationEngine`] owns neither. The corporate-action
//! application itself lives in [`crate::corporate_actions`]; this store offers
//! the crate-internal mutation seams it needs
//! ([`VirtualOrderBook::orders_mut`]). Evolving the fill path to trigger fills
//! FROM resting orders (a limit order resting until its price crosses —
//! today's SRS-SIM-002 path decides fills per routed order + snapshot) and
//! persisting the book into the SRS-SIM-004 snapshot's reserved slot are those
//! owners' evolutions, not contexts inside SRS-DATA-021's acceptance criterion
//! ("virtual orders for delisted securities are canceled").

use std::fmt;

use atp_types::StrategyId;

use crate::paper_order::{validate_leg, OrderError, OrderLeg, OrderRouting, PaperOrderRequest};
use crate::sim::PaperSimulationEngine;

/// A book-assigned identifier for one virtual resting order — unique within its
/// [`VirtualOrderBook`], strictly increasing in placement order (so iteration by
/// id is placement order).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct VirtualOrderId(u64);

impl VirtualOrderId {
    /// The raw numeric id (stable wire form).
    pub fn value(self) -> u64 {
        self.0
    }
}

impl fmt::Display for VirtualOrderId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// Whether a virtual resting order is still working or has been terminally
/// cancelled.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VirtualOrderStatus {
    /// Working — the order rests until filled or cancelled.
    Open,
    /// Terminally cancelled with a structured reason. No later corporate action
    /// or cancel reaches a cancelled order.
    Cancelled { reason: VirtualOrderCancelReason },
}

/// Why a virtual resting order was cancelled by the corporate-action path —
/// every reason is FAIL-CLOSED: an order that can no longer rest at a
/// well-defined quantity and price is cancelled, never left resting on a stale
/// basis.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VirtualOrderCancelReason {
    /// The security was delisted — the order can never fill (the SRS-DATA-021
    /// acceptance criterion's named cancel).
    Delisting,
    /// The security was acquired in a merger — its series terminates at the
    /// effective instant, so the resting order is cancelled exactly like a
    /// delisting (fail-closed even for a cash or malformed merger: the
    /// termination is certain regardless of the conversion terms).
    MergerTermination,
    /// A split carried a non-positive factor — rejected before any arithmetic,
    /// and the order cannot be left resting on the old basis.
    NonPositiveFactor { numerator: i64, denominator: i64 },
    /// A split ratio did not divide the order quantity into a whole number —
    /// the residual is cash-in-lieu territory (SRS-DATA-019 discipline), so the
    /// order is cancelled rather than truncated.
    QuantityNotIntegral {
        before: i64,
        numerator: i64,
        denominator: i64,
    },
    /// A split-adjusted limit/stop price rounded to a non-positive value — an
    /// unpriceable order is cancelled.
    PriceRoundedNonPositive { field: &'static str },
    /// A split-adjusted quantity or price overflowed (never wraps).
    Overflow { context: &'static str },
    /// A symbol change carried a blank successor — the order's instrument
    /// identity is gone, so it cannot be relabeled and must not keep resting
    /// under a retired symbol.
    InvalidSuccessor,
}

impl VirtualOrderCancelReason {
    /// Stable wire discriminator (the `reason` field on every surface).
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Delisting => "DELISTING",
            Self::MergerTermination => "MERGER_TERMINATION",
            Self::NonPositiveFactor { .. } => "NON_POSITIVE_FACTOR",
            Self::QuantityNotIntegral { .. } => "QUANTITY_NOT_INTEGRAL",
            Self::PriceRoundedNonPositive { .. } => "PRICE_ROUNDED_NON_POSITIVE",
            Self::Overflow { .. } => "OVERFLOW",
            Self::InvalidSuccessor => "INVALID_SUCCESSOR",
        }
    }
}

/// One virtual resting order: an intake-valid [`OrderLeg`] held for a paper
/// strategy under a book-assigned id. Fields are private — the only mutation
/// paths are the crate-internal corporate-action seams, so an order can never be
/// edited into an intake-invalid shape from outside.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VirtualRestingOrder {
    id: VirtualOrderId,
    strategy: StrategyId,
    leg: OrderLeg,
    status: VirtualOrderStatus,
}

impl VirtualRestingOrder {
    /// The book-assigned order id.
    pub fn id(&self) -> VirtualOrderId {
        self.id
    }

    /// The paper strategy the order belongs to.
    pub fn strategy(&self) -> &StrategyId {
        &self.strategy
    }

    /// The order leg as placed (symbol verbatim; matching is canonical).
    pub fn leg(&self) -> &OrderLeg {
        &self.leg
    }

    /// The order's status.
    pub fn status(&self) -> &VirtualOrderStatus {
        &self.status
    }

    /// Whether the order is still working.
    pub fn is_open(&self) -> bool {
        matches!(self.status, VirtualOrderStatus::Open)
    }

    /// Terminally cancel this order (crate-internal: the corporate-action path).
    pub(crate) fn cancel(&mut self, reason: VirtualOrderCancelReason) {
        self.status = VirtualOrderStatus::Cancelled { reason };
    }

    /// Replace this order's leg with a corporate-action-adjusted one
    /// (crate-internal: the split-adjustment path, whose arithmetic guarantees
    /// the new leg stays intake-valid).
    pub(crate) fn set_leg(&mut self, leg: OrderLeg) {
        self.leg = leg;
    }
}

/// Every paper strategy's virtual resting orders, in placement order. Unlike the
/// position ledger (one position per canonical symbol per strategy), many orders
/// may rest on one symbol — cancellation and adjustment apply to each
/// independently.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct VirtualOrderBook {
    orders: Vec<VirtualRestingOrder>,
    next_id: u64,
}

impl VirtualOrderBook {
    /// An empty book.
    pub fn new() -> Self {
        Self::default()
    }

    /// Place a resting order for `strategy`, applying the SAME fail-closed
    /// intake validation as the SRS-SIM-001 paper order path ([`OrderError`] on
    /// an empty symbol, non-positive quantity, or non-positive trigger/limit
    /// price). Nothing is stored when validation fails. Prefer
    /// [`place_accepted`](Self::place_accepted), which routes through the
    /// engine's own intake; this is the single-leg primitive it rests each
    /// accepted leg with.
    pub fn place(
        &mut self,
        strategy: &StrategyId,
        leg: OrderLeg,
    ) -> Result<VirtualOrderId, OrderError> {
        validate_leg(&leg)?;
        let id = VirtualOrderId(self.next_id);
        self.next_id += 1;
        self.orders.push(VirtualRestingOrder {
            id,
            strategy: strategy.clone(),
            leg,
            status: VirtualOrderStatus::Open,
        });
        Ok(id)
    }

    /// Submit `request` through the REAL SRS-SIM-001 intake path
    /// ([`PaperSimulationEngine::accept_order`] — the same validation and
    /// internal-simulation routing every paper order takes) and rest every leg
    /// of the accepted routing in this book. This is the wiring that makes an
    /// ACCEPTED paper order reachable by the SRS-DATA-021 corporate-action
    /// path: an order enters the book only by passing the engine's own intake,
    /// never by construction around it. Fail-closed and atomic: a rejected
    /// request ([`OrderError`]) rests nothing.
    ///
    /// The ids are returned in leg order; a multi-leg composite rests one order
    /// per leg (each leg is independently cancellable by a corporate action on
    /// its own symbol, exactly like the position ledger keys per symbol).
    pub fn place_accepted(
        &mut self,
        strategy: &StrategyId,
        engine: &PaperSimulationEngine,
        request: &PaperOrderRequest,
    ) -> Result<Vec<VirtualOrderId>, OrderError> {
        let routing = engine.accept_order(request)?;
        let OrderRouting::InternalSimulation { legs, .. } = routing;
        // accept_order validated every leg, so the per-leg place() re-validation
        // cannot fail here — the whole request rests or none of it does.
        let ids = legs
            .into_iter()
            .map(|leg| self.place(strategy, leg))
            .collect::<Result<Vec<_>, _>>()?;
        Ok(ids)
    }

    /// The order with `id`, if it exists.
    pub fn order(&self, id: VirtualOrderId) -> Option<&VirtualRestingOrder> {
        self.orders.iter().find(|order| order.id == id)
    }

    /// Every order (open and cancelled), in placement order.
    pub fn orders(&self) -> &[VirtualRestingOrder] {
        &self.orders
    }

    /// The still-working orders, in placement order.
    pub fn open_orders(&self) -> impl Iterator<Item = &VirtualRestingOrder> {
        self.orders.iter().filter(|order| order.is_open())
    }

    /// The number of still-working orders.
    pub fn open_count(&self) -> usize {
        self.open_orders().count()
    }

    /// Mutable iteration for the crate-internal corporate-action path.
    pub(crate) fn orders_mut(&mut self) -> impl Iterator<Item = &mut VirtualRestingOrder> {
        self.orders.iter_mut()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::paper_order::{AssetClass, OrderType, Side};

    fn leg(symbol: &str, quantity: i64, order_type: OrderType) -> OrderLeg {
        OrderLeg {
            symbol: symbol.to_string(),
            asset_class: AssetClass::Equity,
            side: Side::Buy,
            quantity,
            order_type,
        }
    }

    #[test]
    fn place_assigns_increasing_ids_and_orders_are_open() {
        let mut book = VirtualOrderBook::new();
        let strategy = StrategyId::new("alpha");
        let first = book
            .place(&strategy, leg("AAPL", 100, OrderType::Market))
            .expect("valid order");
        let second = book
            .place(
                &strategy,
                leg(
                    "MSFT",
                    50,
                    OrderType::Limit {
                        limit_price_minor: 40_000,
                    },
                ),
            )
            .expect("valid order");
        assert!(first < second);
        assert_eq!(book.open_count(), 2);
        assert!(book.order(first).expect("stored").is_open());
    }

    #[test]
    fn place_applies_intake_validation_and_stores_nothing_on_failure() {
        let mut book = VirtualOrderBook::new();
        let strategy = StrategyId::new("alpha");
        assert_eq!(
            book.place(&strategy, leg("  ", 100, OrderType::Market)),
            Err(OrderError::EmptySymbol)
        );
        assert_eq!(
            book.place(&strategy, leg("AAPL", 0, OrderType::Market)),
            Err(OrderError::NonPositiveQuantity { quantity: 0 })
        );
        assert_eq!(
            book.place(
                &strategy,
                leg(
                    "AAPL",
                    100,
                    OrderType::Limit {
                        limit_price_minor: 0
                    }
                )
            ),
            Err(OrderError::NonPositiveLimitPrice { price_minor: 0 })
        );
        assert!(book.orders().is_empty(), "rejected orders never enter");
    }

    #[test]
    fn cancelled_order_is_terminal_and_leaves_open_count() {
        let mut book = VirtualOrderBook::new();
        let strategy = StrategyId::new("alpha");
        let id = book
            .place(&strategy, leg("AAPL", 100, OrderType::Market))
            .expect("valid order");
        for order in book.orders_mut() {
            order.cancel(VirtualOrderCancelReason::Delisting);
        }
        assert_eq!(book.open_count(), 0);
        let order = book.order(id).expect("still recorded");
        assert!(!order.is_open());
        assert_eq!(
            order.status(),
            &VirtualOrderStatus::Cancelled {
                reason: VirtualOrderCancelReason::Delisting
            }
        );
    }
}
