//! Per-paper-strategy virtual position ledger for **SRS-SIM-003** — "maintain an
//! independent virtual position ledger for each paper strategy" (SyRS **SYS-84**;
//! StRS SN-1.29 / SN-1.07).
//!
//! # The seam this module fills
//!
//! [`simulate_fill`](crate::sim::PaperSimulationEngine::simulate_fill) (SRS-BT-003)
//! produces a [`PaperFill`] — a priced, cost-decomposed fill — and the minimal
//! [`PaperLedger`](crate::sim::PaperLedger) seam accumulates only cash, a single
//! signed position, and commission. SYS-84 requires more: a *per-symbol* virtual
//! position tracking **quantity, average cost, unrealized P&L (marked to market
//! using live data), realized P&L, and commission paid**, kept **independent per
//! paper strategy** and **independent of the IB account's actual positions**.
//! This module owns that ledger.
//!
//! Three layers, each isolated from the next:
//!
//!   * [`VirtualPosition`] — one symbol's signed quantity, signed cost basis
//!     (the source of truth for average cost), accumulated realized P&L, and the
//!     full accumulated transaction-cost decomposition (commission, slippage,
//!     spread impact). Applying a [`PaperFill`] updates them with average-cost
//!     accounting (see below); marking against a live [`MarketSnapshot`] yields
//!     unrealized P&L.
//!   * [`StrategyLedger`] — one paper strategy's positions, keyed by symbol.
//!   * [`VirtualLedgerBook`] — every paper strategy's ledger, keyed by
//!     [`StrategyId`]. Each strategy's ledger is a distinct map entry, so one
//!     strategy's fills can never touch another's quantities or P&L, and **no
//!     entry holds any brokerage / IB account position** — the book imports no
//!     broker, adapter, or live-position type, so a virtual position is
//!     structurally independent of the IB account (SYS-84).
//!
//! # Average-cost accounting (the money math)
//!
//! A position carries a **signed** `cost_basis_minor`: positive for a long (cash
//! laid out), negative for a short (proceeds received). Average cost per unit is
//! then `cost_basis / quantity`, which stays positive for both sides. Applying a
//! fill of signed `quantity` `q` at integer price `px` (minor units):
//!
//!   * **Open / add in the same direction** (flat, or `q` agrees in sign with the
//!     held quantity): add `q * px` to the cost basis and `q` to the quantity. No
//!     P&L is realized.
//!   * **Reduce / close** (`q` opposes the held quantity, `|q| <= |held|`):
//!     release the *proportional* slice of the cost basis,
//!     `cost_removed = cost_basis * |q| / |held|`, recognise
//!     `realized += -(q * px) - cost_removed`, then drop `cost_removed` from the
//!     basis and add `q` to the quantity. Closing the whole position releases
//!     exactly the whole basis, leaving the basis at zero.
//!   * **Flip through zero** (`q` opposes the held quantity, `|q| > |held|`):
//!     fully close the existing position (releasing all of its basis and
//!     recognising its realized P&L), then open the remainder
//!     `q_open = q + held` at `px` as a fresh position.
//!
//! **Rounding policy (chosen for exact basis conservation).** `cost_removed` uses
//! integer division truncated toward zero, and the truncation *remainder stays in
//! the residual basis*. This makes basis conservation exact: across any sequence
//! of fills, `realized P&L` plus the remaining `cost_basis` equals the running
//! cash from prices to the minor unit, and a full close always lands at
//! `cost_basis == 0` -- no value is created or destroyed. The deliberate
//! trade-off is that the *derived* per-unit `average_cost_minor` (a truncated view
//! of the exact basis) can shift by up to one minor unit on a partial close; the
//! alternative (holding the per-unit average fixed across reductions) would leak
//! the truncation remainder and break the money-conservation invariant the rest of
//! the engine depends on, so it is rejected. The conservation invariant is pinned
//! by a property-style test over many generated fill sequences.
//!
//! Realized P&L is kept **gross of all transaction costs**; commission, slippage,
//! and spread impact are accumulated **separately** (SYS-84 lists commission as a
//! distinct field, and tracking every component lets the ledger reconcile with the
//! simulator -- `realized_pnl_minor - transaction_cost_paid_minor` equals the sum
//! of the fills' `cash_delta_minor`, so no charged cost disappears). Unrealized
//! P&L marks
//! the open position to market: `mark * quantity - cost_basis`, where the mark is
//! the live [`MarketSnapshot`]'s last trade price (the SYS-84 "live data"
//! reference, reusing the SRS-SIM-002 snapshot). A flat position has zero
//! unrealized P&L.
//!
//! # What is real here vs deferred
//!
//! This is a **genuinely runnable**, deterministic ledger: `&mut self` mutation
//! with no clock or randomness, so identical fills always yield an identical
//! ledger. Every quantity and money figure is an **integer minor unit** —
//! intermediates are `i128`, accumulation is `checked_*`, and every guard fails
//! closed before mutating; the module contains **no floating point**.
//!
//! `apply_fill` is a **pure state transition** -- it applies exactly the fill it
//! is given. Deduplicating a replayed/duplicate delivery needs a unique execution
//! identifier on [`PaperFill`] (which it does not carry -- an SRS-BT-003 change)
//! and exactly-once delivery semantics owned by the orchestrator (SRS-EXE-002), so
//! fill idempotency is deferred to those owners rather than guessed at here.
//!
//! The halves that need unbuilt subsystems are deferred (see
//! `architecture/runtime_services.json#virtual_ledger_contract.deferred`): the
//! live bid/ask/last feed that drives the mark comes from the SYS-70 subscription
//! manager (the [`MarketSnapshot`] is an in-memory fixture today); corporate-action
//! adjustment of virtual positions (SYS-88) is SRS-DATA-021; ledger persistence
//! and restore (SYS-89) is SRS-SIM-004; the accumulated paper performance metrics
//! (SYS-85) are their own feature; the orchestrator routing of all non-live
//! strategies into this engine is SRS-EXE-002; and the Python strategy runtime is
//! still deferred. So `feature_list.json` keeps SRS-SIM-003 at `passes:false`.

use std::collections::HashMap;
use std::fmt;

use atp_types::StrategyId;

use crate::fill_model::MarketSnapshot;
use crate::sim::PaperFill;

/// Canonical per-symbol ledger key: trim surrounding whitespace and upper-case,
/// the SAME normalization policy as [`atp_types::SecurityKey`]. Keying the ledger
/// on this canonical form keeps one security's quantity and P&L in ONE position
/// instead of splitting it across casing/whitespace aliases (`AAPL` vs `aapl` vs
/// ` AAPL `).
///
/// The ledger's instrument identity is this canonical symbol *string*, which is
/// unique per instrument under the system's symbol conventions: equities by
/// ticker, options by the full OCC contract string (e.g. `AAPL  240119C00190000`),
/// so an option and its underlying do not share a key. A [`atp_types::SecurityKey`]
/// is not used directly because the simulated fill ([`PaperFill`]) carries a bare
/// symbol with no asset class and `SecurityKey` rejects option contracts that
/// SRS-SIM-001 intake accepts; formal `(symbol + asset_class + option-contract)`
/// keying needs the upstream `PaperFill` / `simulate_fill` cost-path contract
/// (SRS-BT-003 / SRS-SIM-001) to carry that identity and is **deferred** to that
/// owner rather than threaded through this ledger slice.
fn canonical_symbol(symbol: &str) -> String {
    symbol.trim().to_uppercase()
}

/// One paper strategy's virtual position in a single symbol (SYS-84).
///
/// Tracks the signed `quantity`, the signed `cost_basis_minor` (the source of
/// truth for average cost: positive for a long, negative for a short), the
/// accumulated `realized_pnl_minor` (gross of all transaction costs), and the
/// full accumulated transaction-cost decomposition (`commission_paid_minor`,
/// `slippage_paid_minor`, `spread_impact_paid_minor`). Average cost, unrealized
/// P&L, and the total transaction cost are derived on demand. All money figures
/// are integer minor units; intermediates are `i128`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct VirtualPosition {
    quantity: i64,
    cost_basis_minor: i128,
    realized_pnl_minor: i128,
    commission_paid_minor: i128,
    slippage_paid_minor: i128,
    spread_impact_paid_minor: i128,
}

impl VirtualPosition {
    /// A flat position: no quantity, no basis, no P&L, no commission.
    pub fn new() -> Self {
        Self::default()
    }

    /// Reconstruct a position from already-validated persisted components
    /// (SRS-SIM-004 restore). Crate-internal: the
    /// [`paper_state`](crate::paper_state) deserializer validates the field
    /// invariants (the quantity/basis biconditional, sign agreement, and
    /// non-negative cost components) BEFORE calling this, so this is a pure field
    /// constructor with no I/O and no clock. Kept `pub(crate)` so persistence can
    /// rebuild a position without widening the public mutation surface (the only
    /// public way to change a position stays [`apply_fill`](Self::apply_fill)).
    pub(crate) fn from_components(
        quantity: i64,
        cost_basis: i128,
        realized_pnl: i128,
        commission_paid: i128,
        slippage_paid: i128,
        spread_impact_paid: i128,
    ) -> Self {
        Self {
            quantity,
            cost_basis_minor: cost_basis,
            realized_pnl_minor: realized_pnl,
            commission_paid_minor: commission_paid,
            slippage_paid_minor: slippage_paid,
            spread_impact_paid_minor: spread_impact_paid,
        }
    }

    /// The signed quantity held (`> 0` long, `< 0` short, `0` flat).
    pub fn quantity(&self) -> i64 {
        self.quantity
    }

    /// The signed total cost basis of the open position in minor units (positive
    /// for a long, negative for a short, exactly `0` when flat). This is the
    /// source of truth; [`average_cost_minor`](Self::average_cost_minor) divides
    /// it by the quantity.
    pub fn cost_basis_minor(&self) -> i128 {
        self.cost_basis_minor
    }

    /// Accumulated realized P&L in minor units, **gross of all transaction
    /// costs** (it reflects only the move in price). The net economic result is
    /// this less [`transaction_cost_paid_minor`](Self::transaction_cost_paid_minor).
    pub fn realized_pnl_minor(&self) -> i128 {
        self.realized_pnl_minor
    }

    /// Accumulated commission paid in minor units, tracked separately from
    /// realized P&L (the SYS-84 named cost field).
    pub fn commission_paid_minor(&self) -> i128 {
        self.commission_paid_minor
    }

    /// Accumulated slippage cost paid in minor units (the SYS-15b cost-family
    /// component), tracked separately from realized P&L.
    pub fn slippage_paid_minor(&self) -> i128 {
        self.slippage_paid_minor
    }

    /// Accumulated spread-impact cost paid in minor units (the SYS-15c
    /// cost-family component), tracked separately from realized P&L.
    pub fn spread_impact_paid_minor(&self) -> i128 {
        self.spread_impact_paid_minor
    }

    /// Total transaction cost paid in minor units: commission + slippage +
    /// spread impact, the COMPLETE set of cost components the shared cost family
    /// charges on a [`PaperFill`]. Tracking every component (not just commission)
    /// keeps the ledger faithful to the simulator: a closed position's
    /// `realized_pnl_minor - transaction_cost_paid_minor` reconciles EXACTLY with
    /// the sum of the fills' `cash_delta_minor` (the simulator's actual cash
    /// impact), so charged costs never silently disappear from the ledger.
    /// Overflow-checked.
    pub fn transaction_cost_paid_minor(&self) -> Result<i128, LedgerError> {
        self.commission_paid_minor
            .checked_add(self.slippage_paid_minor)
            .and_then(|partial| partial.checked_add(self.spread_impact_paid_minor))
            .ok_or(LedgerError::Overflow)
    }

    /// Average cost per unit in minor units (`cost_basis / quantity`, truncated
    /// toward zero), or `None` when the position is flat. Positive for both longs
    /// and shorts because the basis and quantity share a sign.
    ///
    /// This is a DERIVED, truncated VIEW of the exact signed `cost_basis_minor`,
    /// which is the source of truth. Because a basis that does not divide evenly
    /// cannot be expressed as an exact per-unit integer, this derived figure can
    /// move by up to one minor unit across a partial close even though no new
    /// shares were acquired -- that is an inherent property of integer cost basis,
    /// not a lost or fabricated value. The underlying basis is always exact and
    /// conserved (see [`apply_fill`](Self::apply_fill)), so realized P&L and total
    /// P&L never leak; only the rounded per-unit display shifts. Callers that need
    /// exactness must read [`cost_basis_minor`](Self::cost_basis_minor).
    pub fn average_cost_minor(&self) -> Option<i128> {
        if self.quantity == 0 {
            None
        } else {
            Some(self.cost_basis_minor / i128::from(self.quantity))
        }
    }

    /// Unrealized P&L in minor units, marked to market against the live
    /// `snapshot`'s last trade price (SYS-84 "marked to market using live data").
    ///
    /// `mark * quantity - cost_basis`, which is correct for both longs and shorts
    /// and is exactly zero for a flat position. Fails closed with
    /// [`LedgerError::NonPositiveMark`] on a non-positive mark (corrupt live
    /// data), so a bad quote can never produce a fabricated mark-to-market value.
    ///
    /// This is the per-position PRIMITIVE: it marks *this* position against
    /// whatever snapshot it is handed, and a [`MarketSnapshot`] carries no
    /// instrument identity today (it is an identity-free SRS-SIM-002 fixture), so
    /// the caller is responsible for supplying this position's instrument quote.
    /// Prefer the symbol-keyed [`StrategyLedger::unrealized_pnl_minor`] /
    /// [`VirtualLedgerBook::unrealized_pnl_minor`] entry points, which select the
    /// position BY symbol so a quote is never applied to the wrong position.
    /// Rejecting a wrong-symbol quote by its content needs MarketSnapshot to carry
    /// identity, which is deferred with the SYS-70 live feed.
    pub fn unrealized_pnl_minor(&self, snapshot: &MarketSnapshot) -> Result<i128, LedgerError> {
        let mark_minor = snapshot.last_minor;
        if mark_minor <= 0 {
            return Err(LedgerError::NonPositiveMark { mark_minor });
        }
        if self.quantity == 0 {
            return Ok(0);
        }
        let marked = i128::from(mark_minor) * i128::from(self.quantity);
        marked
            .checked_sub(self.cost_basis_minor)
            .ok_or(LedgerError::Overflow)
    }

    /// Apply a priced [`PaperFill`] with average-cost accounting. Opens/adds,
    /// reduces/closes, or flips the position through zero per the module-level
    /// rules, and accumulates the fill's full transaction-cost decomposition
    /// (commission + slippage + spread impact, each gross of P&L).
    ///
    /// Fails closed BEFORE ANY mutation: every prospective value (quantity, cost
    /// basis, realized P&L, and each cost component) is computed into a local with
    /// checked arithmetic, and the position is committed only after EVERY checked
    /// op succeeds. So a non-positive fill price, a zero-quantity fill, a negative
    /// cost component, or a money-math overflow leaves the position byte-for-byte
    /// unchanged -- a rejected fill can never leave a partial mutation (e.g. a
    /// moved commission with an unmoved position, which a retry would then
    /// double-count).
    fn apply_fill(&mut self, fill: &PaperFill) -> Result<(), LedgerError> {
        if fill.price_minor <= 0 {
            return Err(LedgerError::NonPositiveFillPrice {
                price_minor: fill.price_minor,
            });
        }
        if fill.quantity == 0 {
            return Err(LedgerError::ZeroQuantityFill);
        }
        // The cost family guarantees non-negative cost components, but `PaperFill`
        // fields are public, so fail closed here too: a negative component would
        // DECREASE its accumulator, breaking the non-negative cost invariant the
        // accessors document (and could fabricate cash on reconciliation).
        for (component, minor_units) in [
            ("commission", fill.commission_minor),
            ("slippage", fill.slippage_minor),
            ("spread_impact", fill.spread_impact_minor),
        ] {
            if minor_units < 0 {
                return Err(LedgerError::NegativeCost {
                    component,
                    minor_units,
                });
            }
        }

        let q = fill.quantity;
        let px = fill.price_minor;
        let fill_notional = i128::from(q) * i128::from(px);

        // `PaperFill::cash_delta_minor` is a public field carrying the simulator's
        // signed cash impact (`-(notional) - total_cost`). The ledger does not
        // accumulate cash from it -- it derives everything from quantity, price,
        // and the validated cost components -- but it advertises that a closed
        // position reconciles with the SUM of cash_delta. Enforce that invariant
        // here so a mutated/malformed fill whose cash_delta disagrees with its own
        // notional and costs is rejected BEFORE any mutation, keeping the
        // reconciliation guarantee airtight rather than trusting the field.
        let total_cost_minor = i128::from(fill.commission_minor)
            + i128::from(fill.slippage_minor)
            + i128::from(fill.spread_impact_minor);
        let expected_cash_delta_minor = fill_notional
            .checked_neg()
            .ok_or(LedgerError::Overflow)?
            .checked_sub(total_cost_minor)
            .ok_or(LedgerError::Overflow)?;
        if expected_cash_delta_minor != i128::from(fill.cash_delta_minor) {
            return Err(LedgerError::InconsistentCashDelta {
                expected_minor: expected_cash_delta_minor,
                actual_minor: fill.cash_delta_minor,
            });
        }

        // The transaction-cost components accumulate regardless of how the
        // position moves, and stay out of realized P&L (which is gross). Computed
        // into locals -- NOT yet committed -- so a later overflow cannot leave a
        // moved cost behind. Tracking every component (not just commission) keeps
        // the ledger reconcilable with the simulator's cash_delta_minor.
        let new_commission = self
            .commission_paid_minor
            .checked_add(i128::from(fill.commission_minor))
            .ok_or(LedgerError::Overflow)?;
        let new_slippage = self
            .slippage_paid_minor
            .checked_add(i128::from(fill.slippage_minor))
            .ok_or(LedgerError::Overflow)?;
        let new_spread_impact = self
            .spread_impact_paid_minor
            .checked_add(i128::from(fill.spread_impact_minor))
            .ok_or(LedgerError::Overflow)?;

        let opening_in_same_direction = self.quantity == 0 || (q > 0) == (self.quantity > 0);

        // Resolve the prospective (quantity, cost basis, realized P&L) triple in
        // locals; nothing is assigned to `self` until all three -- and the
        // commission above -- are known good.
        let (new_quantity, new_cost_basis, new_realized) = if opening_in_same_direction {
            // Open or add: grow the basis and the quantity; realize nothing.
            let cost_basis = self
                .cost_basis_minor
                .checked_add(fill_notional)
                .ok_or(LedgerError::Overflow)?;
            let quantity = self.quantity.checked_add(q).ok_or(LedgerError::Overflow)?;
            (quantity, cost_basis, self.realized_pnl_minor)
        } else {
            // `q` opposes the held quantity. Compare magnitudes to decide reduce
            // vs flip. Magnitudes are taken in i128 so `i64::MIN` cannot overflow
            // its negation (an i128 can represent `-i64::MIN`).
            let held_abs = i128::from(self.quantity).abs();
            let fill_abs = i128::from(q).abs();

            if fill_abs <= held_abs {
                // Reduce or fully close: release the proportional slice of the
                // basis (a full close releases exactly the whole basis).
                let cost_removed = self
                    .cost_basis_minor
                    .checked_mul(fill_abs)
                    .ok_or(LedgerError::Overflow)?
                    / held_abs;
                // realized = proceeds of the closed portion (-(q*px), since q
                // opposes the position) minus the basis released. Gross of
                // commission.
                let realized = fill_notional
                    .checked_neg()
                    .ok_or(LedgerError::Overflow)?
                    .checked_sub(cost_removed)
                    .ok_or(LedgerError::Overflow)?;
                let new_realized = self
                    .realized_pnl_minor
                    .checked_add(realized)
                    .ok_or(LedgerError::Overflow)?;
                let cost_basis = self
                    .cost_basis_minor
                    .checked_sub(cost_removed)
                    .ok_or(LedgerError::Overflow)?;
                let quantity = self.quantity.checked_add(q).ok_or(LedgerError::Overflow)?;
                (quantity, cost_basis, new_realized)
            } else {
                // Flip through zero: fully close the existing position (releasing
                // ALL of its basis), then open the remainder at the fill price.
                // Closing the whole position realizes `held * px - cost_basis`.
                let close_value = i128::from(self.quantity)
                    .checked_mul(i128::from(px))
                    .ok_or(LedgerError::Overflow)?;
                let realized = close_value
                    .checked_sub(self.cost_basis_minor)
                    .ok_or(LedgerError::Overflow)?;
                let new_realized = self
                    .realized_pnl_minor
                    .checked_add(realized)
                    .ok_or(LedgerError::Overflow)?;
                let q_open = q.checked_add(self.quantity).ok_or(LedgerError::Overflow)?;
                let cost_basis = i128::from(q_open)
                    .checked_mul(i128::from(px))
                    .ok_or(LedgerError::Overflow)?;
                (q_open, cost_basis, new_realized)
            }
        };

        // Every checked op succeeded -- commit atomically.
        self.quantity = new_quantity;
        self.cost_basis_minor = new_cost_basis;
        self.realized_pnl_minor = new_realized;
        self.commission_paid_minor = new_commission;
        self.slippage_paid_minor = new_slippage;
        self.spread_impact_paid_minor = new_spread_impact;
        Ok(())
    }
}

/// One paper strategy's virtual ledger: its positions keyed by symbol (SYS-84).
///
/// Independent of every other strategy's ledger and of the IB account.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct StrategyLedger {
    positions: HashMap<String, VirtualPosition>,
}

impl StrategyLedger {
    /// An empty ledger with no positions.
    pub fn new() -> Self {
        Self::default()
    }

    /// Apply a simulated fill to this strategy's position in `fill.symbol`,
    /// creating the position on first touch. The symbol is canonicalized
    /// ([`canonical_symbol`]: trim + upper-case, the same policy as
    /// [`atp_types::SecurityKey`]) before keying, so `AAPL`, `aapl`, and ` AAPL `
    /// resolve to the SAME position rather than splitting one security's quantity
    /// and P&L across aliases.
    ///
    /// Fails closed WITHOUT creating any position record: an empty (post-trim)
    /// symbol is rejected up front, and on first touch the fill is applied to a
    /// fresh position that is inserted ONLY if it succeeds -- so a rejected fill
    /// (e.g. a non-positive price) never leaves a phantom symbol behind. An
    /// existing position is mutated in place, and its own
    /// [`VirtualPosition::apply_fill`] is atomic, so a rejected fill leaves it
    /// unchanged.
    pub fn apply_fill(&mut self, fill: &PaperFill) -> Result<(), LedgerError> {
        let symbol = canonical_symbol(&fill.symbol);
        if symbol.is_empty() {
            return Err(LedgerError::EmptySymbol);
        }
        match self.positions.get_mut(&symbol) {
            Some(position) => position.apply_fill(fill),
            None => {
                let mut position = VirtualPosition::new();
                position.apply_fill(fill)?;
                self.positions.insert(symbol, position);
                Ok(())
            }
        }
    }

    /// This strategy's virtual position in `symbol`, if any. The lookup is
    /// canonicalized the same way as the key, so it finds the position regardless
    /// of the caller's casing/whitespace.
    pub fn position(&self, symbol: &str) -> Option<&VirtualPosition> {
        self.positions.get(&canonical_symbol(symbol))
    }

    /// Mark this strategy's `symbol` position to market against `snapshot`,
    /// returning `None` if the strategy holds no such position. This is the
    /// SYMBOL-KEYED marking surface: the caller names the instrument and the
    /// position marked is the one keyed by that symbol, so a quote can never be
    /// applied to a DIFFERENT instrument's position by mistake. (Binding the
    /// snapshot's CONTENT to the instrument -- rejecting a wrong-symbol quote --
    /// needs the SRS-SIM-002 [`MarketSnapshot`] to carry instrument identity,
    /// which is deferred with the SYS-70 live feed; the snapshot is an
    /// identity-free fixture today.)
    pub fn unrealized_pnl_minor(
        &self,
        symbol: &str,
        snapshot: &MarketSnapshot,
    ) -> Option<Result<i128, LedgerError>> {
        self.position(symbol)
            .map(|position| position.unrealized_pnl_minor(snapshot))
    }

    /// The number of distinct symbols this strategy holds a position record for.
    pub fn symbol_count(&self) -> usize {
        self.positions.len()
    }

    /// Iterate this strategy's `(canonical symbol, position)` entries (SRS-SIM-004
    /// capture). Crate-internal; `HashMap` iteration order is unspecified, so the
    /// [`paper_state`](crate::paper_state) serializer sorts by symbol to produce a
    /// deterministic snapshot.
    pub(crate) fn positions_iter(&self) -> impl Iterator<Item = (&String, &VirtualPosition)> {
        self.positions.iter()
    }

    /// Reconstruct a strategy ledger from an already-validated, canonical-keyed
    /// position map (SRS-SIM-004 restore). Crate-internal.
    pub(crate) fn from_positions(positions: HashMap<String, VirtualPosition>) -> Self {
        Self { positions }
    }
}

/// Every paper strategy's virtual ledger, keyed by [`StrategyId`] (SYS-84).
///
/// Each strategy's [`StrategyLedger`] is a distinct map entry, so applying a fill
/// for one strategy can never read or mutate another strategy's positions, and
/// the book holds **no IB / broker account position** at all — a virtual position
/// is structurally independent of the live account.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct VirtualLedgerBook {
    ledgers: HashMap<StrategyId, StrategyLedger>,
}

impl VirtualLedgerBook {
    /// An empty book with no strategy ledgers.
    pub fn new() -> Self {
        Self::default()
    }

    /// Apply a simulated fill to `strategy`'s ledger, creating that strategy's
    /// ledger on first touch. Only the named strategy's entry is touched. Fails
    /// closed WITHOUT creating a ledger: on first touch the fill is applied to a
    /// fresh [`StrategyLedger`] that is inserted under `strategy` ONLY if it
    /// succeeds -- so a rejected first fill (e.g. an empty symbol or non-positive
    /// price) never leaves a phantom strategy behind to pollute later metrics or
    /// persistence. An existing strategy ledger's `apply_fill` is itself atomic.
    pub fn apply_fill(
        &mut self,
        strategy: &StrategyId,
        fill: &PaperFill,
    ) -> Result<(), LedgerError> {
        match self.ledgers.get_mut(strategy) {
            Some(ledger) => ledger.apply_fill(fill),
            None => {
                let mut ledger = StrategyLedger::new();
                ledger.apply_fill(fill)?;
                self.ledgers.insert(strategy.clone(), ledger);
                Ok(())
            }
        }
    }

    /// `strategy`'s ledger, if it has one.
    pub fn ledger(&self, strategy: &StrategyId) -> Option<&StrategyLedger> {
        self.ledgers.get(strategy)
    }

    /// `strategy`'s virtual position in `symbol`, if any. Convenience accessor.
    pub fn position(&self, strategy: &StrategyId, symbol: &str) -> Option<&VirtualPosition> {
        self.ledgers.get(strategy).and_then(|l| l.position(symbol))
    }

    /// Mark `strategy`'s `symbol` position to market against `snapshot`, returning
    /// `None` if that strategy holds no such position. The symbol-keyed marking
    /// surface (see [`StrategyLedger::unrealized_pnl_minor`]): the position marked
    /// is the one keyed by `(strategy, symbol)`, scoped to exactly that strategy.
    pub fn unrealized_pnl_minor(
        &self,
        strategy: &StrategyId,
        symbol: &str,
        snapshot: &MarketSnapshot,
    ) -> Option<Result<i128, LedgerError>> {
        self.ledgers
            .get(strategy)
            .and_then(|ledger| ledger.unrealized_pnl_minor(symbol, snapshot))
    }

    /// The number of distinct strategies the book tracks.
    pub fn strategy_count(&self) -> usize {
        self.ledgers.len()
    }

    /// Iterate the book's `(strategy, ledger)` entries (SRS-SIM-004 capture).
    /// Crate-internal; `HashMap` iteration order is unspecified, so the
    /// [`paper_state`](crate::paper_state) serializer sorts by strategy id to
    /// produce a deterministic snapshot.
    pub(crate) fn ledgers_iter(&self) -> impl Iterator<Item = (&StrategyId, &StrategyLedger)> {
        self.ledgers.iter()
    }

    /// Reconstruct a ledger book from an already-validated ledger map (SRS-SIM-004
    /// restore). Crate-internal: persistence rebuilds the book without widening the
    /// public mutation surface (the only public way to populate a book stays
    /// [`apply_fill`](Self::apply_fill)).
    pub(crate) fn from_ledgers(ledgers: HashMap<StrategyId, StrategyLedger>) -> Self {
        Self { ledgers }
    }
}

/// Fail-closed errors from the virtual ledger. Carries no broker/vendor
/// identifiers.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LedgerError {
    /// The fill referenced an empty / whitespace symbol.
    EmptySymbol,
    /// The fill referenced a non-positive price (corrupt fill). Rejected before
    /// any ledger mutation.
    NonPositiveFillPrice { price_minor: i64 },
    /// The fill carried a zero quantity (no-op fill). Rejected before any ledger
    /// mutation.
    ZeroQuantityFill,
    /// The fill carried a negative transaction-cost component (corrupt fill):
    /// `component` names which one (`commission` / `slippage` / `spread_impact`).
    /// Rejected before any mutation so it can never decrease that cost's
    /// non-negative accumulator (or fabricate cash on reconciliation).
    NegativeCost {
        component: &'static str,
        minor_units: i64,
    },
    /// The fill's `cash_delta_minor` did not equal `-(notional) - total_cost`
    /// (corrupt / inconsistent fill). Rejected before any mutation so the ledger's
    /// reconciliation guarantee (`realized - total_cost == sum of cash_delta`)
    /// cannot be silently broken by trusting the public field.
    InconsistentCashDelta {
        expected_minor: i128,
        actual_minor: i64,
    },
    /// A mark-to-market valuation referenced a non-positive mark (corrupt live
    /// data). Rejected before any valuation.
    NonPositiveMark { mark_minor: i64 },
    /// Ledger money math exceeded the `i128` minor-unit range.
    Overflow,
}

impl fmt::Display for LedgerError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptySymbol => write!(f, "virtual ledger fill symbol must not be empty"),
            Self::NonPositiveFillPrice { price_minor } => write!(
                f,
                "virtual ledger fill has a non-positive price {price_minor} minor units"
            ),
            Self::ZeroQuantityFill => {
                write!(f, "virtual ledger fill must have a non-zero quantity")
            }
            Self::NegativeCost {
                component,
                minor_units,
            } => write!(
                f,
                "virtual ledger fill has a negative {component} cost {minor_units} minor units"
            ),
            Self::InconsistentCashDelta {
                expected_minor,
                actual_minor,
            } => write!(
                f,
                "virtual ledger fill cash delta {actual_minor} minor units disagrees with the \
                 expected {expected_minor} (-(notional) - total cost)"
            ),
            Self::NonPositiveMark { mark_minor } => write!(
                f,
                "virtual ledger mark-to-market has a non-positive mark {mark_minor} minor units"
            ),
            Self::Overflow => write!(f, "virtual ledger money math overflowed i128 minor units"),
        }
    }
}

impl std::error::Error for LedgerError {}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sim::PaperSimulationEngine;

    /// A well-formed fill whose `cash_delta_minor` matches the simulate_fill
    /// invariant `-(notional) - total_cost`, so it passes the ledger's cash-delta
    /// consistency guard. Used by every test that exercises accepted fills.
    fn fill_full(
        symbol: &str,
        quantity: i64,
        price_minor: i64,
        commission_minor: i64,
        slippage_minor: i64,
        spread_impact_minor: i64,
    ) -> PaperFill {
        let total_cost = i128::from(commission_minor)
            + i128::from(slippage_minor)
            + i128::from(spread_impact_minor);
        let cash_delta = -(i128::from(quantity) * i128::from(price_minor)) - total_cost;
        PaperFill {
            ts: 0,
            symbol: symbol.to_string(),
            quantity,
            price_minor,
            commission_minor,
            slippage_minor,
            spread_impact_minor,
            cash_delta_minor: i64::try_from(cash_delta).expect("test fill cash_delta fits i64"),
        }
    }

    /// A bare priced fill (zero slippage/spread); `commission_minor` is per-test.
    fn fill(symbol: &str, quantity: i64, price_minor: i64, commission_minor: i64) -> PaperFill {
        fill_full(symbol, quantity, price_minor, commission_minor, 0, 0)
    }

    /// A fill with an explicit transaction-cost decomposition, for the
    /// cost-accumulation and fail-closed tests.
    fn fill_costs(
        quantity: i64,
        price_minor: i64,
        commission_minor: i64,
        slippage_minor: i64,
        spread_impact_minor: i64,
    ) -> PaperFill {
        fill_full(
            "AAPL",
            quantity,
            price_minor,
            commission_minor,
            slippage_minor,
            spread_impact_minor,
        )
    }

    fn snapshot(last_minor: i64) -> MarketSnapshot {
        MarketSnapshot {
            bid_minor: last_minor - 1,
            ask_minor: last_minor + 1,
            last_minor,
            bar_volume: 1_000,
        }
    }

    #[test]
    fn open_long_sets_basis_and_average_cost() {
        let mut pos = VirtualPosition::new();
        pos.apply_fill(&fill("AAPL", 100, 10_000, 0)).expect("open");
        assert_eq!(pos.quantity(), 100);
        assert_eq!(pos.cost_basis_minor(), 1_000_000);
        assert_eq!(pos.average_cost_minor(), Some(10_000));
        assert_eq!(pos.realized_pnl_minor(), 0);
    }

    #[test]
    fn adding_to_a_long_blends_average_cost() {
        let mut pos = VirtualPosition::new();
        pos.apply_fill(&fill("AAPL", 100, 10_000, 0))
            .expect("buy 1");
        pos.apply_fill(&fill("AAPL", 100, 12_000, 0))
            .expect("buy 2");
        assert_eq!(pos.quantity(), 200);
        // (100*10_000 + 100*12_000) / 200 = 11_000.
        assert_eq!(pos.average_cost_minor(), Some(11_000));
        assert_eq!(pos.realized_pnl_minor(), 0);
    }

    #[test]
    fn partial_sell_realizes_pnl_and_keeps_average_cost() {
        let mut pos = VirtualPosition::new();
        pos.apply_fill(&fill("AAPL", 100, 10_000, 0)).expect("buy");
        pos.apply_fill(&fill("AAPL", -40, 11_000, 0)).expect("sell");
        assert_eq!(pos.quantity(), 60);
        // realized = (11_000 - 10_000) * 40 = 40_000.
        assert_eq!(pos.realized_pnl_minor(), 40_000);
        // average cost of the remainder is unchanged.
        assert_eq!(pos.average_cost_minor(), Some(10_000));
        assert_eq!(pos.cost_basis_minor(), 600_000);
    }

    #[test]
    fn full_close_leaves_basis_exactly_zero() {
        let mut pos = VirtualPosition::new();
        pos.apply_fill(&fill("AAPL", 100, 10_000, 0)).expect("buy");
        pos.apply_fill(&fill("AAPL", -100, 11_000, 0))
            .expect("sell");
        assert_eq!(pos.quantity(), 0);
        assert_eq!(pos.cost_basis_minor(), 0);
        assert_eq!(pos.average_cost_minor(), None);
        assert_eq!(pos.realized_pnl_minor(), 100_000);
    }

    #[test]
    fn open_short_then_cover_realizes_pnl() {
        let mut pos = VirtualPosition::new();
        // Sell short 100 @ 10_000: basis is negative (proceeds received).
        pos.apply_fill(&fill("AAPL", -100, 10_000, 0))
            .expect("short");
        assert_eq!(pos.quantity(), -100);
        assert_eq!(pos.cost_basis_minor(), -1_000_000);
        assert_eq!(pos.average_cost_minor(), Some(10_000));
        // Cover 100 @ 9_000 (price fell): a short profits.
        pos.apply_fill(&fill("AAPL", 100, 9_000, 0)).expect("cover");
        assert_eq!(pos.quantity(), 0);
        assert_eq!(pos.cost_basis_minor(), 0);
        // realized = (10_000 - 9_000) * 100 = 100_000.
        assert_eq!(pos.realized_pnl_minor(), 100_000);
    }

    #[test]
    fn short_cover_at_higher_price_realizes_loss() {
        let mut pos = VirtualPosition::new();
        pos.apply_fill(&fill("AAPL", -100, 10_000, 0))
            .expect("short");
        pos.apply_fill(&fill("AAPL", 100, 11_000, 0))
            .expect("cover");
        assert_eq!(pos.realized_pnl_minor(), -100_000);
    }

    #[test]
    fn flip_through_zero_realizes_closed_portion_and_reopens() {
        let mut pos = VirtualPosition::new();
        pos.apply_fill(&fill("AAPL", 100, 10_000, 0)).expect("buy");
        // Sell 150 @ 11_000: closes 100 long (gain 100_000), opens 50 short.
        pos.apply_fill(&fill("AAPL", -150, 11_000, 0))
            .expect("flip");
        assert_eq!(pos.quantity(), -50);
        assert_eq!(pos.realized_pnl_minor(), 100_000);
        // The reopened short's basis/average cost is at the fill price.
        assert_eq!(pos.cost_basis_minor(), -550_000);
        assert_eq!(pos.average_cost_minor(), Some(11_000));
    }

    #[test]
    fn unrealized_marks_long_and_short_to_market() {
        let mut long = VirtualPosition::new();
        long.apply_fill(&fill("AAPL", 100, 10_000, 0)).expect("buy");
        // mark 10_500: (10_500 - 10_000) * 100 = 50_000.
        assert_eq!(long.unrealized_pnl_minor(&snapshot(10_500)), Ok(50_000));

        let mut short = VirtualPosition::new();
        short
            .apply_fill(&fill("AAPL", -100, 10_000, 0))
            .expect("short");
        // mark 9_500: a short gains as price falls -> (10_000 - 9_500) * 100.
        assert_eq!(short.unrealized_pnl_minor(&snapshot(9_500)), Ok(50_000));
    }

    #[test]
    fn flat_position_has_zero_unrealized() {
        let pos = VirtualPosition::new();
        assert_eq!(pos.unrealized_pnl_minor(&snapshot(10_000)), Ok(0));
    }

    #[test]
    fn commission_accumulates_separately_from_realized_pnl() {
        let mut pos = VirtualPosition::new();
        pos.apply_fill(&fill("AAPL", 100, 10_000, 35)).expect("buy");
        pos.apply_fill(&fill("AAPL", -100, 11_000, 35))
            .expect("sell");
        // Realized P&L is gross of commission.
        assert_eq!(pos.realized_pnl_minor(), 100_000);
        assert_eq!(pos.commission_paid_minor(), 70);
    }

    #[test]
    fn ledger_keeps_symbols_independent() {
        let mut ledger = StrategyLedger::new();
        ledger.apply_fill(&fill("AAPL", 100, 10_000, 0)).expect("a");
        ledger.apply_fill(&fill("MSFT", -50, 20_000, 0)).expect("m");
        assert_eq!(ledger.symbol_count(), 2);
        assert_eq!(ledger.position("AAPL").unwrap().quantity(), 100);
        assert_eq!(ledger.position("MSFT").unwrap().quantity(), -50);
    }

    #[test]
    fn book_isolates_strategies_holding_the_same_symbol() {
        let mut book = VirtualLedgerBook::new();
        let alpha = StrategyId::new("alpha");
        let beta = StrategyId::new("beta");
        book.apply_fill(&alpha, &fill("AAPL", 100, 10_000, 0))
            .expect("alpha");
        book.apply_fill(&beta, &fill("AAPL", -30, 9_000, 0))
            .expect("beta");
        // Same symbol, fully independent positions.
        assert_eq!(book.position(&alpha, "AAPL").unwrap().quantity(), 100);
        assert_eq!(book.position(&beta, "AAPL").unwrap().quantity(), -30);
        assert_eq!(book.strategy_count(), 2);
        // Mutating beta leaves alpha untouched.
        let alpha_before = book.position(&alpha, "AAPL").cloned();
        book.apply_fill(&beta, &fill("AAPL", -10, 9_500, 0))
            .expect("beta 2");
        assert_eq!(book.position(&alpha, "AAPL").cloned(), alpha_before);
    }

    #[test]
    fn symbol_keyed_marking_selects_the_named_position() {
        // The symbol-keyed marking surface marks the position keyed by the named
        // symbol -- a quote is never applied to a different instrument's position,
        // and an unheld symbol returns None.
        let mut book = VirtualLedgerBook::new();
        let strat = StrategyId::new("s");
        book.apply_fill(&strat, &fill("AAPL", 100, 10_000, 0))
            .expect("aapl");
        book.apply_fill(&strat, &fill("MSFT", -50, 20_000, 0))
            .expect("msft");

        // AAPL marked at 10_500 -> (10_500 - 10_000) * 100 = 50_000.
        assert_eq!(
            book.unrealized_pnl_minor(&strat, "AAPL", &snapshot(10_500)),
            Some(Ok(50_000))
        );
        // MSFT (a short) marked at 19_000 -> (20_000 - 19_000) * 50 = 50_000.
        assert_eq!(
            book.unrealized_pnl_minor(&strat, "msft", &snapshot(19_000)),
            Some(Ok(50_000))
        );
        // An unheld symbol marks nothing.
        assert_eq!(
            book.unrealized_pnl_minor(&strat, "TSLA", &snapshot(1_000)),
            None
        );
        // A non-positive mark still fails closed through the keyed surface.
        assert_eq!(
            book.unrealized_pnl_minor(&strat, "AAPL", &snapshot(0)),
            Some(Err(LedgerError::NonPositiveMark { mark_minor: 0 }))
        );
    }

    #[test]
    fn empty_symbol_fails_closed() {
        let mut ledger = StrategyLedger::new();
        assert_eq!(
            ledger.apply_fill(&fill("   ", 100, 10_000, 0)),
            Err(LedgerError::EmptySymbol)
        );
        assert_eq!(ledger.symbol_count(), 0);
    }

    #[test]
    fn non_positive_fill_price_fails_closed() {
        let mut pos = VirtualPosition::new();
        assert_eq!(
            pos.apply_fill(&fill("AAPL", 100, 0, 0)),
            Err(LedgerError::NonPositiveFillPrice { price_minor: 0 })
        );
        assert_eq!(pos, VirtualPosition::new());
    }

    #[test]
    fn zero_quantity_fill_fails_closed() {
        let mut pos = VirtualPosition::new();
        assert_eq!(
            pos.apply_fill(&fill("AAPL", 0, 10_000, 0)),
            Err(LedgerError::ZeroQuantityFill)
        );
    }

    #[test]
    fn non_positive_mark_fails_closed() {
        let mut pos = VirtualPosition::new();
        pos.apply_fill(&fill("AAPL", 100, 10_000, 0)).expect("buy");
        assert_eq!(
            pos.unrealized_pnl_minor(&snapshot(0)),
            Err(LedgerError::NonPositiveMark { mark_minor: 0 })
        );
    }

    #[test]
    fn inconsistent_cash_delta_fails_closed() {
        // A fill whose cash_delta_minor disagrees with -(notional) - total_cost is
        // rejected before any mutation, so it can never silently break the ledger's
        // reconciliation guarantee.
        let mut bad = fill_full("AAPL", 100, 10_000, 35, 0, 0);
        bad.cash_delta_minor += 1; // corrupt the cash impact by one minor unit
        let mut pos = VirtualPosition::new();
        assert_eq!(
            pos.apply_fill(&bad),
            Err(LedgerError::InconsistentCashDelta {
                expected_minor: -(100 * 10_000) - 35,
                actual_minor: -(100 * 10_000) - 35 + 1,
            })
        );
        assert_eq!(pos, VirtualPosition::new());
    }

    #[test]
    fn a_rejected_fill_rolls_back_atomically_including_commission() {
        // Seed a position with a known commission, then attempt a fill that fails a
        // late guard (an inconsistent cash delta). Because apply_fill commits only
        // after every check passes, NOTHING moves -- not the quantity, not the
        // basis, and crucially NOT the commission (a partial commission bump would
        // be double-counted on a retry).
        let mut pos = VirtualPosition::new();
        pos.apply_fill(&fill_full("AAPL", 100, 10_000, 35, 5, 7))
            .expect("seed");
        let before = pos.clone();
        assert_eq!(pos.commission_paid_minor(), 35);

        let mut bad = fill_full("AAPL", 50, 10_000, 9, 0, 0);
        bad.cash_delta_minor -= 3; // corrupt -> rejected at the cash-delta guard
        assert!(matches!(
            pos.apply_fill(&bad),
            Err(LedgerError::InconsistentCashDelta { .. })
        ));
        assert_eq!(pos, before);
        assert_eq!(pos.commission_paid_minor(), 35);
    }

    #[test]
    fn ledger_rejecting_an_invalid_first_fill_creates_no_phantom_symbol() {
        // A non-positive price is rejected by VirtualPosition::apply_fill; the
        // StrategyLedger must not leave a phantom (flat) position behind for the
        // symbol it never validly held.
        let mut ledger = StrategyLedger::new();
        assert_eq!(
            ledger.apply_fill(&fill("AAPL", 100, 0, 0)),
            Err(LedgerError::NonPositiveFillPrice { price_minor: 0 })
        );
        assert_eq!(ledger.symbol_count(), 0);
        assert!(ledger.position("AAPL").is_none());
    }

    #[test]
    fn book_rejecting_an_invalid_first_fill_creates_no_phantom_strategy() {
        // A rejected first fill for a brand-new strategy (empty symbol or
        // non-positive price) must not leave a phantom strategy ledger behind to
        // pollute later metrics / persistence / orchestrator accounting.
        let mut book = VirtualLedgerBook::new();
        let strat = StrategyId::new("reservoir-x");

        assert_eq!(
            book.apply_fill(&strat, &fill("   ", 100, 10_000, 0)),
            Err(LedgerError::EmptySymbol)
        );
        assert_eq!(book.strategy_count(), 0);
        assert!(book.ledger(&strat).is_none());

        assert_eq!(
            book.apply_fill(&strat, &fill("AAPL", 100, 0, 0)),
            Err(LedgerError::NonPositiveFillPrice { price_minor: 0 })
        );
        assert_eq!(book.strategy_count(), 0);
        assert!(book.ledger(&strat).is_none());
    }

    #[test]
    fn deterministic_for_identical_fills() {
        let build = || {
            let mut pos = VirtualPosition::new();
            pos.apply_fill(&fill("AAPL", 137, 9_973, 13)).unwrap();
            pos.apply_fill(&fill("AAPL", -59, 10_111, 7)).unwrap();
            pos
        };
        assert_eq!(build(), build());
    }

    #[test]
    fn aliased_symbols_resolve_to_one_position() {
        // AAPL / aapl / " AAPL " are the SAME security; they must not split into
        // separate positions (the symbol is canonicalized: trim + upper-case).
        let mut ledger = StrategyLedger::new();
        ledger.apply_fill(&fill("AAPL", 100, 10_000, 0)).expect("a");
        ledger.apply_fill(&fill("aapl", 100, 12_000, 0)).expect("b");
        ledger
            .apply_fill(&fill("  AAPL  ", -50, 11_000, 0))
            .expect("c");
        assert_eq!(ledger.symbol_count(), 1);
        let pos = ledger.position("aApL").expect("position via any casing");
        // 200 bought (avg 11_000) then 50 sold -> 150 left, avg unchanged.
        assert_eq!(pos.quantity(), 150);
        assert_eq!(pos.average_cost_minor(), Some(11_000));
    }

    #[test]
    fn negative_cost_component_fails_closed() {
        // Each transaction-cost component is validated; a negative one is rejected
        // before any mutation so it can never decrease its accumulator.
        for (bad, component) in [
            (fill_costs(100, 10_000, -1, 0, 0), "commission"),
            (fill_costs(100, 10_000, 0, -1, 0), "slippage"),
            (fill_costs(100, 10_000, 0, 0, -1), "spread_impact"),
        ] {
            let mut pos = VirtualPosition::new();
            assert_eq!(
                pos.apply_fill(&bad),
                Err(LedgerError::NegativeCost {
                    component,
                    minor_units: -1,
                })
            );
            assert_eq!(
                pos,
                VirtualPosition::new(),
                "{component} rejected w/o mutation"
            );
        }
    }

    #[test]
    fn accumulates_every_transaction_cost_component() {
        let mut pos = VirtualPosition::new();
        pos.apply_fill(&fill_costs(100, 10_000, 35, 500, 1_000))
            .expect("buy");
        assert_eq!(pos.commission_paid_minor(), 35);
        assert_eq!(pos.slippage_paid_minor(), 500);
        assert_eq!(pos.spread_impact_paid_minor(), 1_000);
        assert_eq!(pos.transaction_cost_paid_minor(), Ok(35 + 500 + 1_000));
        // Realized P&L stays gross of every cost component.
        assert_eq!(pos.realized_pnl_minor(), 0);
    }

    #[test]
    fn ledger_reconciles_with_simulated_cash_over_a_round_trip() {
        // A round trip driven by REAL fills from the cost family: the ledger's
        // net result (gross realized P&L minus the FULL transaction cost) must
        // reconcile EXACTLY with the sum of the fills' cash_delta_minor (the
        // simulator's actual cash impact). This proves no charged cost silently
        // disappears from the ledger -- including slippage and spread impact.
        let engine = PaperSimulationEngine::new();
        let buy = engine
            .simulate_fill(1, "AAPL", 100, 10_000, None)
            .expect("buy");
        let sell = engine
            .simulate_fill(2, "AAPL", -100, 11_000, None)
            .expect("sell");

        let mut pos = VirtualPosition::new();
        pos.apply_fill(&buy).expect("apply buy");
        pos.apply_fill(&sell).expect("apply sell");

        let net = pos.realized_pnl_minor() - pos.transaction_cost_paid_minor().expect("cost");
        let simulated_cash = i128::from(buy.cash_delta_minor) + i128::from(sell.cash_delta_minor);
        assert_eq!(
            net, simulated_cash,
            "ledger net P&L reconciles with simulated cash"
        );
        // The costs are real (non-zero), so this is not a vacuous reconciliation.
        assert!(pos.transaction_cost_paid_minor().unwrap() > 0);
    }

    #[test]
    fn same_price_round_trip_is_a_loss_equal_to_costs() {
        // Buying and selling at the SAME price yields zero GROSS realized P&L, but
        // the net result is a loss equal to the transaction costs charged -- the
        // costs do not vanish (the round 3 reconciliation concern).
        let engine = PaperSimulationEngine::new();
        let buy = engine
            .simulate_fill(1, "AAPL", 100, 10_000, None)
            .expect("buy");
        let sell = engine
            .simulate_fill(2, "AAPL", -100, 10_000, None)
            .expect("sell");
        let mut pos = VirtualPosition::new();
        pos.apply_fill(&buy).expect("buy");
        pos.apply_fill(&sell).expect("sell");

        assert_eq!(pos.realized_pnl_minor(), 0, "zero gross P&L at one price");
        let cost = pos.transaction_cost_paid_minor().expect("cost");
        assert!(cost > 0);
        let net = pos.realized_pnl_minor() - cost;
        assert_eq!(
            net,
            i128::from(buy.cash_delta_minor) + i128::from(sell.cash_delta_minor)
        );
        assert!(
            net < 0,
            "a same-price round trip nets a loss equal to costs"
        );
    }

    #[test]
    fn basis_is_conserved_to_zero_over_many_fill_sequences() {
        // Property-style invariant over many generated sequences: closing a
        // position back to exactly flat must leave cost_basis == 0 (the truncation
        // remainder is retained in the basis, never lost), and the realized P&L of
        // the round trip must equal the exact cash from prices (sells minus buys),
        // gross of commission. This pins the basis-conservation rounding policy
        // even when the basis does not divide evenly (e.g. 32 over 3).
        let qtys = [1_i64, 2, 3, 7, 50, 137, 999];
        let pxs = [3_i64, 10, 9_973, 10_111, 12_345];
        for &open_qty in &qtys {
            for &px_open in &pxs {
                for &px_close in &pxs {
                    // Open long `open_qty` @ px_open, then close it fully @ px_close.
                    let mut pos = VirtualPosition::new();
                    pos.apply_fill(&fill("AAPL", open_qty, px_open, 0)).unwrap();
                    pos.apply_fill(&fill("AAPL", -open_qty, px_close, 0))
                        .unwrap();
                    assert_eq!(pos.quantity(), 0, "flat after a full round trip");
                    assert_eq!(pos.cost_basis_minor(), 0, "basis conserved to zero");
                    // Exact round-trip P&L: (px_close - px_open) * open_qty.
                    let expected =
                        (i128::from(px_close) - i128::from(px_open)) * i128::from(open_qty);
                    assert_eq!(pos.realized_pnl_minor(), expected, "exact realized P&L");
                }
            }
        }
    }

    #[test]
    fn partial_closes_conserve_basis_even_when_indivisible() {
        // The classic indivisible case Codex raised: basis 32 over quantity 3.
        // Average cost is the truncated view 10, and may shift on a partial close,
        // but the basis stays EXACT and conserves to zero on a full close.
        let mut pos = VirtualPosition::new();
        // Three buys totalling basis 32 over 3 shares: 10 + 11 + 11.
        pos.apply_fill(&fill("X", 1, 10, 0)).unwrap();
        pos.apply_fill(&fill("X", 1, 11, 0)).unwrap();
        pos.apply_fill(&fill("X", 1, 11, 0)).unwrap();
        assert_eq!(pos.cost_basis_minor(), 32);
        assert_eq!(pos.average_cost_minor(), Some(10)); // 32 / 3 truncated
                                                        // Close one share: cost_removed = 32 * 1 / 3 = 10 (truncated); 22 remains.
        pos.apply_fill(&fill("X", -1, 20, 0)).unwrap();
        assert_eq!(pos.cost_basis_minor(), 22);
        // Close the rest: basis returns to exactly zero (no value lost).
        pos.apply_fill(&fill("X", -2, 20, 0)).unwrap();
        assert_eq!(pos.quantity(), 0);
        assert_eq!(pos.cost_basis_minor(), 0);
        // Total realized over the three sells: proceeds 3*20=60 minus basis 32 = 28.
        assert_eq!(pos.realized_pnl_minor(), 28);
    }
}
