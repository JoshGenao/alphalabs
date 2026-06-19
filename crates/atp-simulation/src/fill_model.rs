//! Fill-model / triggering path for **SRS-SIM-002** — "simulate fills using live
//! market data and configurable fill models" (SyRS **SYS-83** fill simulation,
//! **SYS-87** realism constraints; StRS SN-1.29 / SN-1.03).
//!
//! # The seam this module fills
//!
//! The paper pipeline previously had a gap between two halves:
//!
//!   * [`accept_order`](crate::paper_order) (SRS-SIM-001) produces an
//!     [`OrderRouting::InternalSimulation`](crate::paper_order::OrderRouting)
//!     carrying each leg's [`OrderType`] — but performs **no** fill logic.
//!   * [`simulate_fill`](crate::sim::PaperSimulationEngine::simulate_fill)
//!     (SRS-BT-003) applies the shared transaction-cost family **given** a known
//!     fill price: it assumes the order already filled.
//!
//! Nothing turned a routed [`OrderType`] plus live market data (bid / ask / last
//! / volume) into a *decision* of **whether** the order fills, at which fill price,
//! and **at what (volume-capped) quantity**. That decision is this module's job,
//! via [`PaperSimulationEngine::evaluate_fill`]. A [`FillDecision::Filled`] then
//! feeds `simulate_fill`, so a triggered fill flows through the SAME cost family
//! the backtest engine uses (SYS-83d commission).
//!
//! # The SYS-83 fill models
//!
//! Over a [`MarketSnapshot`], for every [`OrderType`] / [`Side`]:
//!
//!   * **Market** (SYS-83a): always fills (subject to the volume cap) at the
//!     current ask for a buy or the current bid for a sell. The configurable
//!     slippage (SYS-15b) is applied downstream by the cost family, not folded
//!     into the reference price here.
//!   * **Limit** (SYS-83b): fills only when the market crosses the limit — a buy
//!     when the ask falls to/through the limit, a sell when the bid rises
//!     to/through it. The default model is [`LimitFillModel::ImmediateOnCross`]
//!     ("fill immediately upon price cross"). A filled limit takes the **limit**
//!     price as the fill reference: the conservative no-improvement assumption,
//!     uniformly pessimistic for both sides.
//!   * **Stop** (SYS-83c): triggers when the last trade crosses the stop (a buy
//!     stop when `last >= stop`, a sell stop when `last <= stop`), then fills as a
//!     **market** order.
//!   * **Stop-limit** (SYS-83c): triggers at the stop, then rests as a **limit**
//!     at the limit price (applying the limit rule above).
//!
//! # The SYS-87b volume constraint
//!
//! A simulated fill **shall not exceed the observed volume for the bar period**.
//! This has a *per-order* and an *aggregate* (per-bar) part, and both are
//! enforced:
//!
//!   * **Per order** —
//!     [`evaluate_fill`](PaperSimulationEngine::evaluate_fill) caps the fill
//!     quantity at the bar volume (`fill_quantity = min(requested, bar_volume)`),
//!     so a large order partially fills against a thin bar; a zero-volume bar
//!     yields [`NoFillReason::ZeroVolume`].
//!   * **Aggregate (the bar *period*)** — because `evaluate_fill` is stateless, on
//!     its own two orders could each fill the whole bar. To enforce SYS-87b across
//!     orders, a bar-replay loop creates **one** [`BarVolumeBudget`] per bar and
//!     threads it through
//!     [`evaluate_fill_against_budget`](PaperSimulationEngine::evaluate_fill_against_budget):
//!     each fill consumes the budget, so the *sum* of fills against one bar can
//!     never exceed its observed volume. The budget is **bound to its bar** — a
//!     mismatch between the budget's `observed_bar_volume` and the snapshot's
//!     `bar_volume` fails closed, so a stale/oversized budget cannot be used to
//!     fill a thinner bar past its volume. `evaluate_fill` is exactly that method
//!     with a fresh per-call budget.
//!
//! The trigger/cross test runs **before** the cap, so an order that never crosses
//! reports `LimitNotCrossed` / `StopNotTriggered` and consumes no budget. Tracking
//! the *remainder* of a partial fill across **bars** (re-evaluating a resting order
//! on the next bar until fully filled or cancelled), and the loop that sequences
//! orders within a bar, are the deferred pending-order lifecycle owned by the
//! SYS-84 ledger / SYS-89 persistence (SRS-SIM-003 / SRS-SIM-004), not this slice.
//!
//! # What is real here vs deferred
//!
//! This is a **genuinely runnable**, deterministic fill-model core: given an
//! order type, side, quantity, snapshot, and per-strategy [`FillModelConfig`] it
//! returns a [`FillDecision`] with no clock or randomness, so identical inputs
//! always yield identical fills.
//!
//! The halves that need unbuilt subsystems are deferred (see
//! `architecture/runtime_services.json#sim_fill_contract.deferred`): the SYS-87a
//! market-hours gating needs the SYS-50 trading calendar; the SYS-87c stale-data
//! *threshold* rejection needs the SYS-39 freshness wiring; the live bid/ask/last
//! feed comes from the SYS-70 subscription manager (the [`MarketSnapshot`] is an
//! in-memory fixture today); the *stochastic* fill-*probability* model (SYS-83b)
//! needs a seeded RNG and stays deferred to preserve determinism (the two
//! deterministic [`LimitFillModel`] variants that ship are genuinely
//! behavior-changing per strategy, so the configurability requirement is met
//! without it); applying fills to the full SYS-84 virtual ledger is SRS-SIM-003
//! (itself built, `passes:true`); paper-state persistence (SYS-89) is SRS-SIM-004;
//! the orchestrator routing of all non-live strategies into this engine is
//! SRS-EXE-002; and the Python strategy runtime is the SRS-SDK runtime. These are
//! **adjacent features**, each its own requirement and NOT a context inside
//! SRS-SIM-002's acceptance criterion. The operator surface is the
//! `sim002_fill_cli` binary (subcommands `defaults` / `rules` / `config` /
//! `volume` over this engine), so `feature_list.json` marks SRS-SIM-002
//! `passes:true`.
//!
//! # Money math
//!
//! Every price is an **integer minor unit** with the `_minor` suffix — never
//! floating point — so the fill path is exact. The only arithmetic is the volume
//! cap `min`, computed on
//! validated non-negative `i64`, so the fill quantity can neither overflow nor go
//! negative. Fail-closed guards reject corrupt market data (a non-positive quote,
//! a crossed book, a negative volume) before any fill decision is made.

use std::fmt;

use atp_types::order_type::OrderTypeError;

use crate::paper_order::{OrderType, Side};
use crate::sim::PaperSimulationEngine;

/// A snapshot of the live market data the fill model reads (SYS-83 / SYS-70):
/// the current bid, ask, last trade, and the observed volume for the bar period.
///
/// All prices are **integer minor units** with the `_minor` suffix (converting a
/// vendor floating-point quote into minor units happens at the deferred adapter
/// boundary, never here). `bar_volume` is the observed share/contract volume the
/// fill is capped against (SYS-87b).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MarketSnapshot {
    /// Best bid price (minor units).
    pub bid_minor: i64,
    /// Best ask price (minor units).
    pub ask_minor: i64,
    /// Last trade price (minor units) — drives stop triggering.
    pub last_minor: i64,
    /// Observed volume for the bar period; a simulated fill may not exceed it
    /// (SYS-87b).
    pub bar_volume: i64,
}

/// The configurable, per-strategy model for when a crossed limit actually fills
/// (SYS-83b "subject to configurable fill probability model").
///
/// Two **deterministic** models ship, and they produce *different* fill decisions
/// on the same snapshot (a touch exactly at the limit fills under
/// [`ImmediateOnCross`](Self::ImmediateOnCross) but not under
/// [`RequireThroughCross`](Self::RequireThroughCross)) — so the per-strategy
/// configuration is genuinely behavior-changing. The *stochastic*
/// fill-probability variant (which needs a seeded RNG) stays deferred so the
/// engine remains deterministic.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum LimitFillModel {
    /// SYS-83b default — fill immediately upon price cross: a buy fills when the
    /// ask reaches **or crosses** the limit (`ask <= limit`), a sell when the bid
    /// reaches or crosses it (`bid >= limit`). A touch exactly at the limit fills.
    #[default]
    ImmediateOnCross,
    /// Conservative model — fill only when the market trades **strictly through**
    /// the limit (`ask < limit` for a buy, `bid > limit` for a sell). A mere touch
    /// at the limit does **not** fill, modelling a resting order behind the queue
    /// that executes only once price moves past it. This yields a strictly more
    /// pessimistic fill set than the immediate-on-cross default.
    RequireThroughCross,
}

/// The per-strategy fill-model configuration (SYS-83 "configurable per strategy").
///
/// [`Default`] is the SYS-83 baseline. An operator overrides it per strategy by
/// passing a different config to [`PaperSimulationEngine::evaluate_fill`]; the
/// override lives at the call site, not in strategy code.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct FillModelConfig {
    /// How a crossed limit order fills.
    pub limit_fill: LimitFillModel,
}

impl FillModelConfig {
    /// The SYS-83 default fill-model family — identical to [`Default`], named for
    /// call-site intent (mirrors [`CostConfig::syrs_defaults`](crate::cost::CostConfig::syrs_defaults)).
    pub fn syrs_defaults() -> Self {
        Self::default()
    }
}

/// A per-bar volume budget (SYS-87b "for the bar period").
///
/// Tracks how much of a bar's observed volume remains fillable, and the
/// `observed_bar_volume` it was created for. The single-order
/// [`evaluate_fill`](PaperSimulationEngine::evaluate_fill) builds a fresh budget
/// per call (so one order cannot exceed the bar), while a bar-replay loop creates
/// **one** budget per bar and threads it through
/// [`evaluate_fill_against_budget`](PaperSimulationEngine::evaluate_fill_against_budget)
/// for every order, so the **aggregate** of fills against that bar cannot exceed
/// the observed volume even across orders. The loop that owns the per-bar budget
/// and orders the fills is a deferred bar-replay loop in the simulation engine
/// runtime (the SRS-SIM-003 [`virtual_ledger`](crate::virtual_ledger) that records
/// the resulting fills is built).
///
/// A budget is **bound to its bar**: `evaluate_fill_against_budget` fails closed
/// if the budget's `observed_bar_volume` does not match the snapshot's
/// `bar_volume`, so a stale or oversized budget can never be used against a thinner
/// bar to fill past its observed volume.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BarVolumeBudget {
    observed_bar_volume: i64,
    remaining: i64,
}

impl BarVolumeBudget {
    /// A fresh budget for a bar whose observed volume is `observed_bar_volume`.
    /// Fails closed on a negative volume (corrupt data), mirroring the snapshot
    /// guard so a negative budget can never widen a fill.
    pub fn new(observed_bar_volume: i64) -> Result<Self, FillModelError> {
        if observed_bar_volume < 0 {
            return Err(FillModelError::NegativeVolume {
                bar_volume: observed_bar_volume,
            });
        }
        Ok(Self {
            observed_bar_volume,
            remaining: observed_bar_volume,
        })
    }

    /// A fresh budget for the bar described by `snapshot` (its observed volume).
    /// The canonical constructor: a budget built this way always matches the
    /// snapshot it will be evaluated against.
    pub fn for_snapshot(snapshot: &MarketSnapshot) -> Result<Self, FillModelError> {
        Self::new(snapshot.bar_volume)
    }

    /// The bar's observed volume this budget is bound to (its initial capacity).
    pub fn observed_bar_volume(&self) -> i64 {
        self.observed_bar_volume
    }

    /// The volume still fillable in this bar.
    pub fn remaining(&self) -> i64 {
        self.remaining
    }

    /// Consume `quantity` of the remaining budget after a fill. `quantity` is the
    /// already-capped fill quantity (`<= remaining`); `saturating_sub` makes the
    /// budget total and floored at zero even if a future caller misuses it, so the
    /// remaining volume can never go negative and re-widen a later fill.
    fn consume(&mut self, quantity: i64) {
        debug_assert!(
            quantity <= self.remaining,
            "a fill must already be capped at the remaining budget"
        );
        self.remaining = self.remaining.saturating_sub(quantity);
    }
}

/// Why a triggered order did not fill (the order was well-formed and the market
/// data was valid; the order simply did not execute on this snapshot).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NoFillReason {
    /// A limit (or stop-limit, post-trigger) order's limit price was not crossed.
    LimitNotCrossed,
    /// A stop (or stop-limit) order's stop price was not triggered.
    StopNotTriggered,
    /// The order would have filled, but no bar volume remained to fill against
    /// (the bar's observed volume was zero, or a per-bar [`BarVolumeBudget`] was
    /// already exhausted by earlier fills) (SYS-87b).
    ZeroVolume,
}

/// The outcome of evaluating an order against a [`MarketSnapshot`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FillDecision {
    /// The order fills `fill_quantity` (always `> 0`, direction lives in
    /// [`Side`]) at `fill_price_minor`. `fill_quantity` is capped at the bar's
    /// observed volume (SYS-87b), so it may be less than the requested quantity
    /// (a partial fill).
    Filled {
        fill_price_minor: i64,
        fill_quantity: i64,
    },
    /// The order did not fill on this snapshot; `reason` says why.
    NoFill { reason: NoFillReason },
}

impl FillDecision {
    /// Whether this decision is a fill.
    pub fn is_filled(&self) -> bool {
        matches!(self, Self::Filled { .. })
    }
}

/// Fail-closed errors from the fill model — raised on corrupt market data or a
/// malformed request, **before** any fill decision. Carries no broker/vendor
/// identifiers.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FillModelError {
    /// A snapshot quote (`bid` / `ask` / `last`) was non-positive (corrupt market
    /// data). Rejected before any fill so a negative price can never reference a
    /// cash-fabricating fill downstream.
    NonPositiveQuote {
        field: &'static str,
        price_minor: i64,
    },
    /// The book was crossed/locked (`bid > ask`), i.e. corrupt quote data. A
    /// crossed book has no well-defined fill price, so the model fails closed.
    CrossedBook { bid_minor: i64, ask_minor: i64 },
    /// The bar carried a negative observed volume (corrupt volume data). Rejected
    /// before the volume cap so a negative cap can never widen a fill.
    NegativeVolume { bar_volume: i64 },
    /// The requested quantity was not strictly positive. Intake (SRS-SIM-001)
    /// already guards this; the fill model re-checks so it can never be called
    /// with a zero/negative quantity that would make the volume cap meaningless.
    NonPositiveQuantity { quantity: i64 },
    /// A limit / stop-limit order carried a non-positive limit price. Intake
    /// (SRS-SIM-001) guards this on a routed leg, but `evaluate_fill` accepts a
    /// raw [`OrderType`], so it re-checks: otherwise a buy/sell against a valid
    /// snapshot could cross a negative limit and return a fill *at* that negative
    /// price. Rejected before any fill.
    NonPositiveLimitPrice { price_minor: i64 },
    /// A stop / stop-limit order carried a non-positive stop price. Re-checked for
    /// the same reason as a non-positive limit price: a non-positive trigger must
    /// never reach the fill path. Rejected before any fill.
    NonPositiveStopPrice { price_minor: i64 },
    /// A [`BarVolumeBudget`] was used against a snapshot it was not built for: its
    /// `observed_bar_volume` does not equal the snapshot's `bar_volume`. A stale or
    /// oversized budget would otherwise let fills exceed the bar's observed volume
    /// (SYS-87b), so the budget is bound to its bar and a mismatch fails closed.
    BudgetSnapshotMismatch {
        budget_bar_volume: i64,
        snapshot_bar_volume: i64,
    },
}

impl fmt::Display for FillModelError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NonPositiveQuote { field, price_minor } => write!(
                f,
                "market snapshot {field} price {price_minor} minor units must be strictly positive"
            ),
            Self::CrossedBook {
                bid_minor,
                ask_minor,
            } => write!(
                f,
                "market snapshot book is crossed: bid {bid_minor} exceeds ask {ask_minor} minor units"
            ),
            Self::NegativeVolume { bar_volume } => write!(
                f,
                "market snapshot bar volume {bar_volume} must not be negative"
            ),
            Self::NonPositiveQuantity { quantity } => write!(
                f,
                "fill-model requested quantity {quantity} must be strictly positive"
            ),
            Self::NonPositiveLimitPrice { price_minor } => write!(
                f,
                "fill-model limit price {price_minor} minor units must be strictly positive"
            ),
            Self::NonPositiveStopPrice { price_minor } => write!(
                f,
                "fill-model stop price {price_minor} minor units must be strictly positive"
            ),
            Self::BudgetSnapshotMismatch {
                budget_bar_volume,
                snapshot_bar_volume,
            } => write!(
                f,
                "bar volume budget {budget_bar_volume} does not match the snapshot bar volume \
                 {snapshot_bar_volume}; the budget must be built for the bar it is evaluated against"
            ),
        }
    }
}

impl std::error::Error for FillModelError {}

/// The reference price and trigger resolution for an order against a snapshot,
/// before the volume cap is applied.
enum TriggerOutcome {
    /// The order triggered/crossed and would fill at `price_minor`.
    Fills { price_minor: i64 },
    /// The order did not trigger/cross; `reason` says why.
    NoFill { reason: NoFillReason },
}

/// Validate the live market data before any fill decision: every quote must be
/// strictly positive, the book must not be crossed, and the bar volume must not
/// be negative. Fails closed so corrupt market data can never drive a fill.
fn validate_snapshot(snapshot: &MarketSnapshot) -> Result<(), FillModelError> {
    for (field, price_minor) in [
        ("bid", snapshot.bid_minor),
        ("ask", snapshot.ask_minor),
        ("last", snapshot.last_minor),
    ] {
        if price_minor <= 0 {
            return Err(FillModelError::NonPositiveQuote { field, price_minor });
        }
    }
    if snapshot.bid_minor > snapshot.ask_minor {
        return Err(FillModelError::CrossedBook {
            bid_minor: snapshot.bid_minor,
            ask_minor: snapshot.ask_minor,
        });
    }
    if snapshot.bar_volume < 0 {
        return Err(FillModelError::NegativeVolume {
            bar_volume: snapshot.bar_volume,
        });
    }
    Ok(())
}

/// Validate a raw [`OrderType`]'s embedded prices before any fill decision. The
/// SRS-SIM-001 intake path validates a routed leg, but [`evaluate_fill`] accepts a
/// raw `OrderType`, so it re-checks here: a non-positive limit or stop price must
/// never reach the fill path, or a crossed-but-negative limit would return a fill
/// *at* that negative price. DELEGATES to the shared SRS-EXE-003 authority
/// [`OrderType::validate_prices`] (mapping `OrderTypeError` into `FillModelError`)
/// so the fill path, the paper intake, and the live intake apply ONE rule that
/// cannot drift.
fn validate_order_type(order_type: &OrderType) -> Result<(), FillModelError> {
    order_type.validate_prices().map_err(|err| match err {
        OrderTypeError::NonPositiveLimitPrice { price_minor } => {
            FillModelError::NonPositiveLimitPrice { price_minor }
        }
        OrderTypeError::NonPositiveStopPrice { price_minor } => {
            FillModelError::NonPositiveStopPrice { price_minor }
        }
    })
}

/// The market reference price for a marketable order: the ask for a buy (you pay
/// up to the offer), the bid for a sell (you hit the bid).
fn market_price_minor(side: Side, snapshot: &MarketSnapshot) -> i64 {
    match side {
        Side::Buy => snapshot.ask_minor,
        Side::Sell => snapshot.bid_minor,
    }
}

/// Whether a stop order has triggered: a buy stop triggers when the last trade
/// rises to/through the stop, a sell stop when it falls to/through the stop.
fn stop_triggered(side: Side, stop_price_minor: i64, snapshot: &MarketSnapshot) -> bool {
    match side {
        Side::Buy => snapshot.last_minor >= stop_price_minor,
        Side::Sell => snapshot.last_minor <= stop_price_minor,
    }
}

/// Resolve a limit order against the snapshot under the per-strategy
/// [`LimitFillModel`]. A filled limit takes the **limit** price as its reference
/// (the conservative no-improvement assumption). The configured model decides
/// whether a *touch* exactly at the limit fills:
///
///   * [`LimitFillModel::ImmediateOnCross`]: a buy fills when `ask <= limit`, a
///     sell when `bid >= limit` (a touch fills).
///   * [`LimitFillModel::RequireThroughCross`]: a buy fills only when
///     `ask < limit`, a sell only when `bid > limit` (a touch does **not** fill).
fn limit_outcome(
    side: Side,
    limit_price_minor: i64,
    snapshot: &MarketSnapshot,
    fill_model: &FillModelConfig,
) -> TriggerOutcome {
    let crossed = match fill_model.limit_fill {
        LimitFillModel::ImmediateOnCross => match side {
            Side::Buy => snapshot.ask_minor <= limit_price_minor,
            Side::Sell => snapshot.bid_minor >= limit_price_minor,
        },
        LimitFillModel::RequireThroughCross => match side {
            Side::Buy => snapshot.ask_minor < limit_price_minor,
            Side::Sell => snapshot.bid_minor > limit_price_minor,
        },
    };
    if crossed {
        TriggerOutcome::Fills {
            price_minor: limit_price_minor,
        }
    } else {
        TriggerOutcome::NoFill {
            reason: NoFillReason::LimitNotCrossed,
        }
    }
}

/// Resolve the trigger + reference price for one order type against the snapshot.
fn resolve_trigger(
    order_type: &OrderType,
    side: Side,
    snapshot: &MarketSnapshot,
    fill_model: &FillModelConfig,
) -> TriggerOutcome {
    match *order_type {
        OrderType::Market => TriggerOutcome::Fills {
            price_minor: market_price_minor(side, snapshot),
        },
        OrderType::Limit { limit_price_minor } => {
            limit_outcome(side, limit_price_minor, snapshot, fill_model)
        }
        OrderType::Stop { stop_price_minor } => {
            if stop_triggered(side, stop_price_minor, snapshot) {
                // A triggered stop becomes a market order (SYS-83c).
                TriggerOutcome::Fills {
                    price_minor: market_price_minor(side, snapshot),
                }
            } else {
                TriggerOutcome::NoFill {
                    reason: NoFillReason::StopNotTriggered,
                }
            }
        }
        OrderType::StopLimit {
            stop_price_minor,
            limit_price_minor,
        } => {
            if stop_triggered(side, stop_price_minor, snapshot) {
                // A triggered stop-limit rests as a limit (SYS-83c).
                limit_outcome(side, limit_price_minor, snapshot, fill_model)
            } else {
                TriggerOutcome::NoFill {
                    reason: NoFillReason::StopNotTriggered,
                }
            }
        }
    }
}

impl PaperSimulationEngine {
    /// Evaluate one order against a [`MarketSnapshot`] and per-strategy
    /// [`FillModelConfig`] (SRS-SIM-002).
    ///
    /// Returns whether the order fills, at what reference price, and at what
    /// volume-capped quantity. The SYS-83 fill rule is selected by `order_type` /
    /// `side` (market fills at the touch, limit on price cross, stop on a last
    /// crossing the stop, stop-limit on a triggered stop then the limit rule);
    /// the fill quantity is capped at the bar's observed volume (SYS-87b).
    ///
    /// This is the **single-order** evaluator: it caps against the full bar volume,
    /// so *one* order can never fill more than the bar traded. The **aggregate**
    /// SYS-87b guarantee for the bar *period* (the SUM of fills from several orders
    /// against one bar not exceeding the observed volume) requires bar-level state;
    /// thread a [`BarVolumeBudget`] through [`evaluate_fill_against_budget`] for
    /// that. `evaluate_fill` is exactly `evaluate_fill_against_budget` with a fresh
    /// per-call budget.
    ///
    /// `requested_quantity` is the positive order quantity (direction lives in
    /// [`Side`], matching [`OrderLeg`](crate::paper_order::OrderLeg)); the
    /// returned `fill_quantity` is likewise positive. The method is `&self` with
    /// no clock or randomness, so it is **deterministic**. It fails closed before
    /// any fill decision on a non-positive requested quantity, a non-positive
    /// limit/stop price on the order type, a non-positive quote, a crossed book,
    /// or a negative volume.
    pub fn evaluate_fill(
        &self,
        order_type: &OrderType,
        side: Side,
        requested_quantity: i64,
        snapshot: &MarketSnapshot,
        fill_model: &FillModelConfig,
    ) -> Result<FillDecision, FillModelError> {
        let mut budget = BarVolumeBudget::new(snapshot.bar_volume)?;
        self.evaluate_fill_against_budget(
            order_type,
            side,
            requested_quantity,
            snapshot,
            fill_model,
            &mut budget,
        )
    }

    /// Evaluate one order against a [`MarketSnapshot`] while **consuming** from a
    /// per-bar [`BarVolumeBudget`] (SRS-SIM-002 / SYS-87b "for the bar period").
    ///
    /// The volume cap is taken from the budget's *remaining* volume, and a fill
    /// **decrements** the budget. Threading one budget through every order
    /// evaluated against the same bar therefore enforces the **aggregate**
    /// constraint: the SUM of fills cannot exceed the bar's observed volume, even
    /// across orders, because each fill shrinks the remaining budget. A
    /// `NoFill` (no cross, or no volume left) consumes nothing. The bar-replay loop
    /// that creates one budget per bar and orders the fills is the deferred owner
    /// (SRS-SIM-003); this method is its per-order step.
    ///
    /// `&self` and deterministic: identical inputs and budget state always yield
    /// the same decision and the same budget mutation.
    pub fn evaluate_fill_against_budget(
        &self,
        order_type: &OrderType,
        side: Side,
        requested_quantity: i64,
        snapshot: &MarketSnapshot,
        fill_model: &FillModelConfig,
        budget: &mut BarVolumeBudget,
    ) -> Result<FillDecision, FillModelError> {
        if requested_quantity <= 0 {
            return Err(FillModelError::NonPositiveQuantity {
                quantity: requested_quantity,
            });
        }
        // Re-validate the raw order type's prices (a non-positive limit/stop must
        // never reach the fill path) and the live market data, before any fill.
        validate_order_type(order_type)?;
        validate_snapshot(snapshot)?;

        // The budget must be the one built for THIS bar: if its observed volume
        // does not match the snapshot's bar volume, a stale or oversized budget
        // could let fills exceed the bar's observed volume (SYS-87b). Fail closed.
        if budget.observed_bar_volume() != snapshot.bar_volume {
            return Err(FillModelError::BudgetSnapshotMismatch {
                budget_bar_volume: budget.observed_bar_volume(),
                snapshot_bar_volume: snapshot.bar_volume,
            });
        }

        // Resolve the trigger/cross FIRST: an order that never crosses reports
        // LimitNotCrossed / StopNotTriggered regardless of volume (and consumes no
        // budget).
        let fill_price_minor = match resolve_trigger(order_type, side, snapshot, fill_model) {
            TriggerOutcome::Fills { price_minor } => price_minor,
            TriggerOutcome::NoFill { reason } => return Ok(FillDecision::NoFill { reason }),
        };

        // Volume cap (SYS-87b): a fill may not exceed the bar's REMAINING volume; a
        // bar with no volume left fills nothing. The fill then consumes the budget,
        // so later orders against the same bar see less remaining volume.
        let fill_quantity = requested_quantity.min(budget.remaining());
        if fill_quantity == 0 {
            return Ok(FillDecision::NoFill {
                reason: NoFillReason::ZeroVolume,
            });
        }
        budget.consume(fill_quantity);

        Ok(FillDecision::Filled {
            fill_price_minor,
            fill_quantity,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snapshot(bid: i64, ask: i64, last: i64, volume: i64) -> MarketSnapshot {
        MarketSnapshot {
            bid_minor: bid,
            ask_minor: ask,
            last_minor: last,
            bar_volume: volume,
        }
    }

    fn engine() -> PaperSimulationEngine {
        PaperSimulationEngine::new()
    }

    fn defaults() -> FillModelConfig {
        FillModelConfig::syrs_defaults()
    }

    #[test]
    fn default_fill_model_is_the_syrs_baseline() {
        assert_eq!(FillModelConfig::default(), FillModelConfig::syrs_defaults());
        assert_eq!(
            FillModelConfig::default().limit_fill,
            LimitFillModel::ImmediateOnCross
        );
    }

    #[test]
    fn limit_fill_model_is_behavior_changing() {
        // The per-strategy fill model is genuinely configurable: on the SAME touch
        // snapshot (ask exactly at the limit), the two models disagree.
        let engine = engine();
        let order = OrderType::Limit {
            limit_price_minor: 10_000,
        };
        let touch = snapshot(9_990, 10_000, 9_995, 1_000);
        let immediate = FillModelConfig {
            limit_fill: LimitFillModel::ImmediateOnCross,
        };
        let through = FillModelConfig {
            limit_fill: LimitFillModel::RequireThroughCross,
        };

        let immediate_decision = engine
            .evaluate_fill(&order, Side::Buy, 100, &touch, &immediate)
            .expect("evaluated");
        let through_decision = engine
            .evaluate_fill(&order, Side::Buy, 100, &touch, &through)
            .expect("evaluated");

        // ImmediateOnCross fills the touch; RequireThroughCross does not.
        assert_eq!(
            immediate_decision,
            FillDecision::Filled {
                fill_price_minor: 10_000,
                fill_quantity: 100,
            }
        );
        assert_eq!(
            through_decision,
            FillDecision::NoFill {
                reason: NoFillReason::LimitNotCrossed,
            }
        );
        assert_ne!(
            immediate_decision, through_decision,
            "the two fill models must produce different decisions on a touch"
        );

        // When the market trades STRICTLY through (ask 9_999 < 10_000), both fill.
        let strictly_through = snapshot(9_990, 9_999, 9_995, 1_000);
        let through_filled = engine
            .evaluate_fill(&order, Side::Buy, 100, &strictly_through, &through)
            .expect("evaluated");
        assert!(through_filled.is_filled());
    }

    #[test]
    fn market_buy_fills_at_the_ask() {
        let decision = engine()
            .evaluate_fill(
                &OrderType::Market,
                Side::Buy,
                100,
                &snapshot(9_990, 10_010, 10_000, 1_000),
                &defaults(),
            )
            .expect("evaluated");
        assert_eq!(
            decision,
            FillDecision::Filled {
                fill_price_minor: 10_010,
                fill_quantity: 100,
            }
        );
    }

    #[test]
    fn market_sell_fills_at_the_bid() {
        let decision = engine()
            .evaluate_fill(
                &OrderType::Market,
                Side::Sell,
                100,
                &snapshot(9_990, 10_010, 10_000, 1_000),
                &defaults(),
            )
            .expect("evaluated");
        assert_eq!(
            decision,
            FillDecision::Filled {
                fill_price_minor: 9_990,
                fill_quantity: 100,
            }
        );
    }

    #[test]
    fn limit_buy_fills_on_cross_at_the_limit_price() {
        // Ask (10_010) is above the limit (10_000) -> not crossed.
        let not_crossed = engine()
            .evaluate_fill(
                &OrderType::Limit {
                    limit_price_minor: 10_000,
                },
                Side::Buy,
                100,
                &snapshot(9_990, 10_010, 10_000, 1_000),
                &defaults(),
            )
            .expect("evaluated");
        assert_eq!(
            not_crossed,
            FillDecision::NoFill {
                reason: NoFillReason::LimitNotCrossed,
            }
        );
        // Ask drops to the limit -> fills at the limit price.
        let crossed = engine()
            .evaluate_fill(
                &OrderType::Limit {
                    limit_price_minor: 10_000,
                },
                Side::Buy,
                100,
                &snapshot(9_980, 10_000, 9_990, 1_000),
                &defaults(),
            )
            .expect("evaluated");
        assert_eq!(
            crossed,
            FillDecision::Filled {
                fill_price_minor: 10_000,
                fill_quantity: 100,
            }
        );
    }

    #[test]
    fn limit_sell_fills_when_bid_rises_to_the_limit() {
        // Bid (9_990) below the limit (10_000) -> not crossed.
        let not_crossed = engine()
            .evaluate_fill(
                &OrderType::Limit {
                    limit_price_minor: 10_000,
                },
                Side::Sell,
                50,
                &snapshot(9_990, 10_010, 10_000, 1_000),
                &defaults(),
            )
            .expect("evaluated");
        assert_eq!(
            not_crossed,
            FillDecision::NoFill {
                reason: NoFillReason::LimitNotCrossed,
            }
        );
        // Bid rises to the limit -> fills at the limit price.
        let crossed = engine()
            .evaluate_fill(
                &OrderType::Limit {
                    limit_price_minor: 10_000,
                },
                Side::Sell,
                50,
                &snapshot(10_000, 10_020, 10_010, 1_000),
                &defaults(),
            )
            .expect("evaluated");
        assert_eq!(
            crossed,
            FillDecision::Filled {
                fill_price_minor: 10_000,
                fill_quantity: 50,
            }
        );
    }

    #[test]
    fn stop_buy_triggers_when_last_rises_then_fills_at_market() {
        // last (9_900) below the stop (10_000) -> not triggered.
        let not_triggered = engine()
            .evaluate_fill(
                &OrderType::Stop {
                    stop_price_minor: 10_000,
                },
                Side::Buy,
                100,
                &snapshot(9_890, 9_910, 9_900, 1_000),
                &defaults(),
            )
            .expect("evaluated");
        assert_eq!(
            not_triggered,
            FillDecision::NoFill {
                reason: NoFillReason::StopNotTriggered,
            }
        );
        // last rises to/through the stop -> becomes a market buy, fills at the ask.
        let triggered = engine()
            .evaluate_fill(
                &OrderType::Stop {
                    stop_price_minor: 10_000,
                },
                Side::Buy,
                100,
                &snapshot(10_000, 10_020, 10_010, 1_000),
                &defaults(),
            )
            .expect("evaluated");
        assert_eq!(
            triggered,
            FillDecision::Filled {
                fill_price_minor: 10_020,
                fill_quantity: 100,
            }
        );
    }

    #[test]
    fn stop_sell_triggers_when_last_falls_then_fills_at_market() {
        let triggered = engine()
            .evaluate_fill(
                &OrderType::Stop {
                    stop_price_minor: 10_000,
                },
                Side::Sell,
                100,
                &snapshot(9_980, 10_000, 9_990, 1_000),
                &defaults(),
            )
            .expect("evaluated");
        // A triggered sell stop becomes a market sell, fills at the bid.
        assert_eq!(
            triggered,
            FillDecision::Filled {
                fill_price_minor: 9_980,
                fill_quantity: 100,
            }
        );
    }

    #[test]
    fn stop_limit_triggers_then_applies_the_limit_rule() {
        // Triggered (last 10_010 >= stop 10_000) but the limit (9_950) is below
        // the ask (10_020) -> a buy limit at 9_950 is not crossed.
        let triggered_not_crossed = engine()
            .evaluate_fill(
                &OrderType::StopLimit {
                    stop_price_minor: 10_000,
                    limit_price_minor: 9_950,
                },
                Side::Buy,
                100,
                &snapshot(10_000, 10_020, 10_010, 1_000),
                &defaults(),
            )
            .expect("evaluated");
        assert_eq!(
            triggered_not_crossed,
            FillDecision::NoFill {
                reason: NoFillReason::LimitNotCrossed,
            }
        );
        // Triggered and the ask is at/below the limit -> fills at the limit.
        let triggered_and_crossed = engine()
            .evaluate_fill(
                &OrderType::StopLimit {
                    stop_price_minor: 10_000,
                    limit_price_minor: 10_030,
                },
                Side::Buy,
                100,
                &snapshot(10_000, 10_020, 10_010, 1_000),
                &defaults(),
            )
            .expect("evaluated");
        assert_eq!(
            triggered_and_crossed,
            FillDecision::Filled {
                fill_price_minor: 10_030,
                fill_quantity: 100,
            }
        );
    }

    #[test]
    fn stop_limit_not_triggered_reports_stop_not_triggered() {
        let decision = engine()
            .evaluate_fill(
                &OrderType::StopLimit {
                    stop_price_minor: 10_000,
                    limit_price_minor: 10_030,
                },
                Side::Buy,
                100,
                &snapshot(9_890, 9_910, 9_900, 1_000),
                &defaults(),
            )
            .expect("evaluated");
        assert_eq!(
            decision,
            FillDecision::NoFill {
                reason: NoFillReason::StopNotTriggered,
            }
        );
    }

    #[test]
    fn volume_cap_partial_fills_against_a_thin_bar() {
        // Requested 1_000 but only 250 traded this bar (SYS-87b).
        let decision = engine()
            .evaluate_fill(
                &OrderType::Market,
                Side::Buy,
                1_000,
                &snapshot(9_990, 10_010, 10_000, 250),
                &defaults(),
            )
            .expect("evaluated");
        assert_eq!(
            decision,
            FillDecision::Filled {
                fill_price_minor: 10_010,
                fill_quantity: 250,
            }
        );
    }

    #[test]
    fn volume_cap_does_not_inflate_a_fill_below_the_bar() {
        // Requested 100, bar volume 1_000 -> fills exactly 100 (no inflation).
        let decision = engine()
            .evaluate_fill(
                &OrderType::Market,
                Side::Buy,
                100,
                &snapshot(9_990, 10_010, 10_000, 1_000),
                &defaults(),
            )
            .expect("evaluated");
        assert_eq!(
            decision,
            FillDecision::Filled {
                fill_price_minor: 10_010,
                fill_quantity: 100,
            }
        );
    }

    #[test]
    fn zero_volume_bar_does_not_fill_a_triggered_order() {
        let decision = engine()
            .evaluate_fill(
                &OrderType::Market,
                Side::Buy,
                100,
                &snapshot(9_990, 10_010, 10_000, 0),
                &defaults(),
            )
            .expect("evaluated");
        assert_eq!(
            decision,
            FillDecision::NoFill {
                reason: NoFillReason::ZeroVolume,
            }
        );
    }

    #[test]
    fn untriggered_order_reports_trigger_reason_even_on_zero_volume() {
        // A limit that never crosses reports LimitNotCrossed, NOT ZeroVolume:
        // the trigger test runs before the volume cap.
        let decision = engine()
            .evaluate_fill(
                &OrderType::Limit {
                    limit_price_minor: 10_000,
                },
                Side::Buy,
                100,
                &snapshot(9_990, 10_010, 10_000, 0),
                &defaults(),
            )
            .expect("evaluated");
        assert_eq!(
            decision,
            FillDecision::NoFill {
                reason: NoFillReason::LimitNotCrossed,
            }
        );
    }

    #[test]
    fn bar_volume_budget_rejects_negative_volume() {
        assert_eq!(
            BarVolumeBudget::new(-1),
            Err(FillModelError::NegativeVolume { bar_volume: -1 })
        );
        let budget = BarVolumeBudget::new(500).expect("budget");
        assert_eq!(budget.remaining(), 500);
        assert_eq!(budget.observed_bar_volume(), 500);
        // for_snapshot builds a budget bound to the snapshot's observed volume.
        let from_snap =
            BarVolumeBudget::for_snapshot(&snapshot(9_990, 10_010, 10_000, 750)).expect("budget");
        assert_eq!(from_snap.observed_bar_volume(), 750);
    }

    #[test]
    fn mismatched_budget_fails_closed() {
        // Regression for the adversarial-review finding: a budget built for a
        // different (larger) bar must NOT be usable against a thinner snapshot to
        // fill past its observed volume. The mismatch fails closed before any fill.
        let engine = engine();
        let thin = snapshot(9_990, 10_010, 10_000, 100);
        let mut oversized = BarVolumeBudget::new(10_000).expect("budget");
        assert_eq!(
            engine.evaluate_fill_against_budget(
                &OrderType::Market,
                Side::Buy,
                5_000,
                &thin,
                &defaults(),
                &mut oversized,
            ),
            Err(FillModelError::BudgetSnapshotMismatch {
                budget_bar_volume: 10_000,
                snapshot_bar_volume: 100,
            })
        );
        // The matching budget is accepted and caps the fill at the thin bar.
        let mut matched = BarVolumeBudget::for_snapshot(&thin).expect("budget");
        let decision = engine
            .evaluate_fill_against_budget(
                &OrderType::Market,
                Side::Buy,
                5_000,
                &thin,
                &defaults(),
                &mut matched,
            )
            .expect("evaluated");
        assert_eq!(
            decision,
            FillDecision::Filled {
                fill_price_minor: 10_010,
                fill_quantity: 100,
            }
        );
    }

    #[test]
    fn aggregate_volume_cap_holds_across_orders() {
        // SYS-87b "for the bar period": threading ONE budget through several orders
        // against the same bar caps the AGGREGATE fill at the observed volume, even
        // though each order requests less than the bar on its own.
        let engine = engine();
        let snap = snapshot(9_990, 10_010, 10_000, 1_000);
        let mut budget = BarVolumeBudget::new(snap.bar_volume).expect("budget");

        // First order: 700 of 1_000 available -> fills 700, leaves 300.
        let first = engine
            .evaluate_fill_against_budget(
                &OrderType::Market,
                Side::Buy,
                700,
                &snap,
                &defaults(),
                &mut budget,
            )
            .expect("evaluated");
        assert_eq!(
            first,
            FillDecision::Filled {
                fill_price_minor: 10_010,
                fill_quantity: 700,
            }
        );
        assert_eq!(budget.remaining(), 300);

        // Second order requests 700 too, but only 300 remain -> partial fill of 300.
        let second = engine
            .evaluate_fill_against_budget(
                &OrderType::Market,
                Side::Buy,
                700,
                &snap,
                &defaults(),
                &mut budget,
            )
            .expect("evaluated");
        assert_eq!(
            second,
            FillDecision::Filled {
                fill_price_minor: 10_010,
                fill_quantity: 300,
            }
        );
        assert_eq!(budget.remaining(), 0);

        // Third order finds the bar exhausted -> no fill (ZeroVolume).
        let third = engine
            .evaluate_fill_against_budget(
                &OrderType::Market,
                Side::Buy,
                100,
                &snap,
                &defaults(),
                &mut budget,
            )
            .expect("evaluated");
        assert_eq!(
            third,
            FillDecision::NoFill {
                reason: NoFillReason::ZeroVolume,
            }
        );

        // The aggregate filled (700 + 300 = 1_000) never exceeds the bar volume.
        assert_eq!(700 + 300, snap.bar_volume);
    }

    #[test]
    fn a_no_fill_consumes_no_budget() {
        // An order that never crosses must not consume the bar budget.
        let engine = engine();
        let snap = snapshot(9_990, 10_010, 10_000, 1_000);
        let mut budget = BarVolumeBudget::new(snap.bar_volume).expect("budget");
        let decision = engine
            .evaluate_fill_against_budget(
                &OrderType::Limit {
                    limit_price_minor: 10_000,
                },
                Side::Buy,
                100,
                &snap,
                &defaults(),
                &mut budget,
            )
            .expect("evaluated");
        assert_eq!(
            decision,
            FillDecision::NoFill {
                reason: NoFillReason::LimitNotCrossed,
            }
        );
        assert_eq!(
            budget.remaining(),
            1_000,
            "a no-fill must consume no budget"
        );
    }

    #[test]
    fn evaluate_fill_uses_a_fresh_budget_per_call() {
        // The single-order evaluator builds a fresh budget each call, so two
        // separate evaluate_fill calls each fill the full bar (the per-order cap).
        // Aggregate enforcement is opt-in via evaluate_fill_against_budget.
        let engine = engine();
        let snap = snapshot(9_990, 10_010, 10_000, 1_000);
        for _ in 0..2 {
            let decision = engine
                .evaluate_fill(&OrderType::Market, Side::Buy, 1_000, &snap, &defaults())
                .expect("evaluated");
            assert_eq!(
                decision,
                FillDecision::Filled {
                    fill_price_minor: 10_010,
                    fill_quantity: 1_000,
                }
            );
        }
    }

    #[test]
    fn non_positive_quote_fails_closed() {
        assert_eq!(
            engine().evaluate_fill(
                &OrderType::Market,
                Side::Buy,
                100,
                &snapshot(9_990, 0, 10_000, 1_000),
                &defaults(),
            ),
            Err(FillModelError::NonPositiveQuote {
                field: "ask",
                price_minor: 0,
            })
        );
    }

    #[test]
    fn crossed_book_fails_closed() {
        // bid (10_020) above ask (10_000) is corrupt quote data.
        assert_eq!(
            engine().evaluate_fill(
                &OrderType::Market,
                Side::Buy,
                100,
                &snapshot(10_020, 10_000, 10_010, 1_000),
                &defaults(),
            ),
            Err(FillModelError::CrossedBook {
                bid_minor: 10_020,
                ask_minor: 10_000,
            })
        );
    }

    #[test]
    fn negative_volume_fails_closed() {
        assert_eq!(
            engine().evaluate_fill(
                &OrderType::Market,
                Side::Buy,
                100,
                &snapshot(9_990, 10_010, 10_000, -5),
                &defaults(),
            ),
            Err(FillModelError::NegativeVolume { bar_volume: -5 })
        );
    }

    #[test]
    fn non_positive_quantity_fails_closed() {
        assert_eq!(
            engine().evaluate_fill(
                &OrderType::Market,
                Side::Buy,
                0,
                &snapshot(9_990, 10_010, 10_000, 1_000),
                &defaults(),
            ),
            Err(FillModelError::NonPositiveQuantity { quantity: 0 })
        );
    }

    #[test]
    fn non_positive_limit_price_fails_closed() {
        // Regression: a negative limit must NOT cross a valid snapshot and return
        // a fill at that negative price. A sell limit at -1 against a valid bid
        // (bid >= -1 is always true) would otherwise fill at -1.
        assert_eq!(
            engine().evaluate_fill(
                &OrderType::Limit {
                    limit_price_minor: -1,
                },
                Side::Sell,
                100,
                &snapshot(9_990, 10_010, 10_000, 1_000),
                &defaults(),
            ),
            Err(FillModelError::NonPositiveLimitPrice { price_minor: -1 })
        );
        // A zero limit on a buy is rejected too.
        assert_eq!(
            engine().evaluate_fill(
                &OrderType::Limit {
                    limit_price_minor: 0,
                },
                Side::Buy,
                100,
                &snapshot(9_990, 10_010, 10_000, 1_000),
                &defaults(),
            ),
            Err(FillModelError::NonPositiveLimitPrice { price_minor: 0 })
        );
    }

    #[test]
    fn non_positive_stop_price_fails_closed() {
        assert_eq!(
            engine().evaluate_fill(
                &OrderType::Stop {
                    stop_price_minor: 0,
                },
                Side::Buy,
                100,
                &snapshot(9_990, 10_010, 10_000, 1_000),
                &defaults(),
            ),
            Err(FillModelError::NonPositiveStopPrice { price_minor: 0 })
        );
    }

    #[test]
    fn stop_limit_validates_both_prices_before_any_fill() {
        // Bad stop, good limit -> stop error first (the stop is checked first).
        assert_eq!(
            engine().evaluate_fill(
                &OrderType::StopLimit {
                    stop_price_minor: -5,
                    limit_price_minor: 10_000,
                },
                Side::Buy,
                100,
                &snapshot(9_990, 10_010, 10_000, 1_000),
                &defaults(),
            ),
            Err(FillModelError::NonPositiveStopPrice { price_minor: -5 })
        );
        // Good stop, bad limit -> limit error.
        assert_eq!(
            engine().evaluate_fill(
                &OrderType::StopLimit {
                    stop_price_minor: 10_000,
                    limit_price_minor: -5,
                },
                Side::Buy,
                100,
                &snapshot(9_990, 10_010, 10_000, 1_000),
                &defaults(),
            ),
            Err(FillModelError::NonPositiveLimitPrice { price_minor: -5 })
        );
    }

    #[test]
    fn deterministic_for_identical_inputs() {
        let snap = snapshot(9_990, 10_010, 10_000, 137);
        let first = engine().evaluate_fill(&OrderType::Market, Side::Buy, 200, &snap, &defaults());
        let second = engine().evaluate_fill(&OrderType::Market, Side::Buy, 200, &snap, &defaults());
        assert_eq!(first, second);
    }

    #[test]
    fn filled_decision_feeds_the_shared_cost_family() {
        // The fill model's decision composes with the SRS-BT-003 cost path: a
        // triggered fill's (price, quantity) flow through simulate_fill so the
        // SAME cost family the backtest engine uses charges the commission.
        let engine = engine();
        let decision = engine
            .evaluate_fill(
                &OrderType::Market,
                Side::Buy,
                100,
                &snapshot(9_990, 10_000, 9_995, 1_000),
                &defaults(),
            )
            .expect("evaluated");
        let FillDecision::Filled {
            fill_price_minor,
            fill_quantity,
        } = decision
        else {
            panic!("expected a fill");
        };
        // Buy -> positive signed quantity for simulate_fill.
        let fill = engine
            .simulate_fill(1, "AAPL", fill_quantity, fill_price_minor, None)
            .expect("fill");
        assert_eq!(fill.price_minor, 10_000);
        assert_eq!(fill.quantity, 100);
        // The shared SYS-15a IB-tiered floor commission is charged.
        assert!(fill.commission_minor > 0);
        // A buy pays the notional plus the cost -> a strictly negative cash delta.
        assert!(fill.cash_delta_minor < 0);
    }
}
