//! Internal-simulation paper-fill path for **SRS-BT-003** — "use the same
//! transaction-cost model family for internal simulation and backtesting unless
//! configured otherwise" (SyRS SYS-15e / SYS-83d; StRS SN-1.03 / SN-1.29).
//!
//! # The shared transaction-cost model family
//!
//! The configurable cost-model *family* lives in [`crate::cost`] (SRS-BT-002) and
//! the [`backtest`](crate::backtest) engine is its first consumer. SRS-BT-003
//! mandates that the internal simulation engine apply *the same family* to paper
//! fills "unless explicitly configured otherwise", so this module consumes the
//! **identical** [`CostConfig`] type and calls the **identical** entry point —
//! [`CostConfig::cost_breakdown`] — that `BacktestEngine::run` calls. The
//! [`PaperSimulationEngine`]'s default cost family is provably
//! [`CostConfig::default`] (it derives [`Default`] over a single `cost_config`
//! field whose own default is the SyRS baseline, SYS-15e), and an operator
//! overrides it per strategy via [`PaperSimulationEngine::with_cost_config`]
//! (the "unless explicitly configured otherwise" seam) without touching strategy
//! code.
//!
//! # What is real here vs deferred
//!
//! This module is a **genuinely runnable** paper-fill cost path: given a fill
//! `(quantity, price_minor, observed_spread_minor)` it computes the same
//! [`CostBreakdown`](crate::cost::CostBreakdown) the backtest engine would, and
//! folds it into a signed `cash_delta_minor` that — exactly like the backtest
//! engine — **always subtracts** the cost, so a cost can never fabricate cash
//! (not even on a sell, whose proceeds are reduced rather than increased). The
//! minimal [`PaperLedger`] accumulates cash, position, and commission paid.
//!
//! SRS-BT-003 is `passes:true`: its acceptance criterion ("a paper strategy and
//! backtest using identical cost configuration compute fills and commissions from
//! the same model family") is single-context — both engines are built — and is
//! proven fill-for-fill by `srs_bt_003_shared_cost_family` and demonstrated at the
//! operator workflow by the `bt003_shared_cost_cli` binary (`compare` runs the same
//! fixture strategy through BOTH engines and prints `cost-family-match:true`). The
//! items in `architecture/runtime_services.json#sim_cost_contract.deferred` are
//! ADJACENT simulation features, each its own requirement and NOT part of this AC:
//! the full SYS-84 virtual ledger (average cost, realized/unrealized P&L) is
//! SRS-SIM-003; the SYS-83 limit/stop/stop-limit fill models, fill-probability, and
//! bar-volume cap are SRS-SIM-002; live market-data-driven fills, paper-state
//! persistence (SYS-89), the REST/dashboard override surface, and the Python
//! strategy host are their own features. This module ships the shared-cost-family
//! fill computation that SRS-BT-003 turns on.
//!
//! # Money math
//!
//! Integer minor units (cents) everywhere: `i128` intermediates, `checked_*`
//! arithmetic, and `i64::try_from` narrowing (overflow surfaces as
//! [`SimError::Overflow`]) — never floating point — so identical inputs always
//! yield identical fills (the determinism shared with the backtest engine).

use std::fmt;

use crate::cost::{CostConfig, CostError};

/// The internal simulation engine's paper-fill cost path (SRS-BT-003).
///
/// Holds the per-strategy [`CostConfig`]. [`Default`] is the **shared** SyRS cost
/// family — identical to the backtest engine's default ([`CostConfig::default`],
/// SYS-15e) — because the single `cost_config` field defaults to the SyRS
/// baseline.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct PaperSimulationEngine {
    cost_config: CostConfig,
}

impl PaperSimulationEngine {
    /// A paper engine on the shared SyRS-default cost family (SYS-15e).
    pub fn new() -> Self {
        Self::default()
    }

    /// A paper engine whose cost family is explicitly overridden for this
    /// strategy (the SYS-15e "unless explicitly configured otherwise" seam). The
    /// override lives on the engine, not in strategy code, and never mutates the
    /// shared [`CostConfig::default`] family used by other strategies.
    pub fn with_cost_config(cost_config: CostConfig) -> Self {
        Self { cost_config }
    }

    /// The cost family this engine applies to paper fills.
    pub fn cost_config(&self) -> &CostConfig {
        &self.cost_config
    }

    /// Simulate a single paper fill of `quantity` shares (`> 0` buy, `< 0` sell)
    /// at `price_minor`, given the bar's optional observed bid-ask spread.
    ///
    /// Applies the **same** transaction-cost model family the backtest engine
    /// applies — the identical [`CostConfig::cost_breakdown`] call — and records
    /// the decomposition plus the signed `cash_delta_minor`. The cost is always
    /// **subtracted** from the cash delta, so it can never fabricate cash. Fails
    /// closed (before computing any cost) on an empty symbol, a non-positive
    /// price, a negative observed spread, a misconfigured (negative) cost
    /// parameter, or money-math overflow.
    pub fn simulate_fill(
        &self,
        ts: u64,
        symbol: &str,
        quantity: i64,
        price_minor: i64,
        observed_spread_minor: Option<i64>,
    ) -> Result<PaperFill, SimError> {
        if symbol.trim().is_empty() {
            return Err(SimError::EmptySymbol);
        }
        // A non-positive price would let a buy (negative notional) fabricate cash
        // when the cost is subtracted — reject it before any fill (mirrors the
        // backtest engine's NonPositivePrice guard).
        if price_minor <= 0 {
            return Err(SimError::NonPositivePrice { ts, price_minor });
        }
        // A corrupt negative observed spread would drive a cash-fabricating
        // spread-impact cost — reject it before any fill (mirrors the backtest
        // engine's NegativeSpread guard) rather than relying on the cost family's
        // own guard, so the simulation surfaces a fill-native error.
        if let Some(spread_minor) = observed_spread_minor {
            if spread_minor < 0 {
                return Err(SimError::NegativeSpread { ts, spread_minor });
            }
        }
        // Fail closed on a misconfigured cost model before any fill: a negative
        // cost parameter would otherwise fabricate cash (SRS-BT-002).
        self.cost_config.validate()?;

        // Cash decreases by the signed notional of the trade (a buy spends cash,
        // a sell adds it); the cost is then SUBTRACTED in either direction.
        let notional_minor = checked_notional(quantity, price_minor)?;
        let breakdown =
            self.cost_config
                .cost_breakdown(quantity, price_minor, observed_spread_minor)?;
        let total_cost_minor = breakdown.total_minor()?;
        let cash_delta_minor = notional_minor
            .checked_neg()
            .ok_or(SimError::Overflow)?
            .checked_sub(total_cost_minor)
            .ok_or(SimError::Overflow)?;

        Ok(PaperFill {
            ts,
            symbol: symbol.to_string(),
            quantity,
            price_minor,
            commission_minor: breakdown.commission_minor,
            slippage_minor: breakdown.slippage_minor,
            spread_impact_minor: breakdown.spread_impact_minor,
            cash_delta_minor,
        })
    }
}

/// A single simulated paper fill.
///
/// Carries the same transaction-cost decomposition as the backtest engine's
/// `Fill` (each component non-negative) plus the signed `cash_delta_minor` the
/// fill applies to the virtual ledger — a buy spends the trade notional plus the
/// cost, a sell receives the proceeds less the cost, so the cost reduces the
/// delta in both directions.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PaperFill {
    pub ts: u64,
    pub symbol: String,
    pub quantity: i64,
    pub price_minor: i64,
    pub commission_minor: i64,
    pub slippage_minor: i64,
    pub spread_impact_minor: i64,
    pub cash_delta_minor: i64,
}

impl PaperFill {
    /// The total transaction cost charged for this fill, overflow-checked.
    pub fn total_cost_minor(&self) -> Result<i64, SimError> {
        self.commission_minor
            .checked_add(self.slippage_minor)
            .and_then(|partial| partial.checked_add(self.spread_impact_minor))
            .ok_or(SimError::Overflow)
    }
}

/// A minimal per-strategy virtual ledger seam (SYS-84).
///
/// Tracks the cash balance, signed position, and accumulated commission paid for
/// one paper strategy, independent of any other strategy and of the IB account.
/// The full SYS-84 ledger (per-symbol average cost, realized and unrealized P&L)
/// is SRS-SIM-003's responsibility and is built in [`virtual_ledger`](crate::virtual_ledger)
/// (closed: `passes:true`); this minimal seam exists so the shared cost family's
/// commissions accumulate somewhere even where a caller does not need the full ledger.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PaperLedger {
    pub cash_minor: i64,
    pub position: i64,
    pub commission_paid_minor: i64,
}

impl PaperLedger {
    /// A fresh ledger with `starting_cash_minor`, a flat position, and no
    /// commission paid.
    pub fn new(starting_cash_minor: i64) -> Self {
        Self {
            cash_minor: starting_cash_minor,
            position: 0,
            commission_paid_minor: 0,
        }
    }

    /// Apply a simulated fill: move cash by the fill's signed delta, move the
    /// position by the fill quantity, and accumulate the commission paid. All
    /// moves are overflow-checked.
    pub fn apply_fill(&mut self, fill: &PaperFill) -> Result<(), SimError> {
        self.cash_minor = self
            .cash_minor
            .checked_add(fill.cash_delta_minor)
            .ok_or(SimError::Overflow)?;
        self.position = self
            .position
            .checked_add(fill.quantity)
            .ok_or(SimError::Overflow)?;
        self.commission_paid_minor = self
            .commission_paid_minor
            .checked_add(fill.commission_minor)
            .ok_or(SimError::Overflow)?;
        Ok(())
    }
}

/// Fail-closed errors from a simulated paper fill. Carries no broker/vendor
/// identifiers.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SimError {
    /// The fill symbol was empty / whitespace.
    EmptySymbol,
    /// The fill referenced a non-positive price (corrupt market data). Rejected
    /// before any fill so a negative price can never fabricate cash.
    NonPositivePrice { ts: u64, price_minor: i64 },
    /// The bar carried a negative observed bid-ask spread (corrupt quote data).
    /// Rejected before any fill so a negative spread can never produce a
    /// cash-fabricating spread-impact cost.
    NegativeSpread { ts: u64, spread_minor: i64 },
    /// Money math exceeded `i64` minor-unit range.
    Overflow,
    /// The shared transaction-cost model family rejected the configuration or the
    /// fill (e.g. a negative parameter or cost-math overflow). Carries the
    /// underlying [`CostError`].
    Cost(CostError),
}

impl From<CostError> for SimError {
    fn from(error: CostError) -> Self {
        Self::Cost(error)
    }
}

impl fmt::Display for SimError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptySymbol => write!(f, "simulated fill symbol must not be empty"),
            Self::NonPositivePrice { ts, price_minor } => write!(
                f,
                "simulated fill at ts {ts} has a non-positive price {price_minor} minor units"
            ),
            Self::NegativeSpread { ts, spread_minor } => write!(
                f,
                "simulated fill at ts {ts} has a negative observed spread {spread_minor} minor units"
            ),
            Self::Overflow => write!(f, "simulated fill money math overflowed i64 minor units"),
            Self::Cost(error) => write!(f, "simulated fill cost model rejected the fill: {error}"),
        }
    }
}

impl std::error::Error for SimError {}

/// Exact integer notional `quantity * price_minor`, computed in `i128` to detect
/// overflow before narrowing back to `i64` minor units. Money math never uses
/// floating point. (Mirrors the backtest engine's `checked_notional`, so both
/// engines value a trade identically.)
fn checked_notional(quantity: i64, price_minor: i64) -> Result<i64, SimError> {
    let product = i128::from(quantity) * i128::from(price_minor);
    i64::try_from(product).map_err(|_| SimError::Overflow)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cost::{CommissionModel, SlippageModel, SpreadImpactModel};

    #[test]
    fn default_engine_shares_the_backtest_default_cost_family() {
        // SYS-15e: the sim engine's default cost family IS the backtest default.
        assert_eq!(
            PaperSimulationEngine::default().cost_config(),
            &CostConfig::default()
        );
        assert_eq!(
            PaperSimulationEngine::new().cost_config(),
            &CostConfig::default()
        );
    }

    #[test]
    fn simulate_fill_applies_the_shared_default_family() {
        // Buy 100 @ $100.00 (10_000 minor), no observed spread. These are the
        // exact SYS-15a/b/c default numbers the backtest engine produces.
        let fill = PaperSimulationEngine::new()
            .simulate_fill(1, "AAPL", 100, 10_000, None)
            .expect("fill");
        // SYS-15a/b/c defaults for a 100-share buy at $100.00: IB tiered floor
        // $0.35, 0.05% slippage of $10,000, 0.10% fallback spread.
        assert_eq!(fill.commission_minor, 35);
        assert_eq!(fill.slippage_minor, 500);
        assert_eq!(fill.spread_impact_minor, 1_000);
        // A buy pays the trade notional plus the cost: -(1_000_000) - 1_535.
        assert_eq!(fill.cash_delta_minor, -(1_000_000) - 1_535);
        assert_eq!(fill.total_cost_minor(), Ok(1_535));
    }

    #[test]
    fn observed_spread_path_is_used_when_present() {
        // Observed spread 40 -> half per share (20) * 100 = 2000, distinct from
        // the 1000 fallback.
        let fill = PaperSimulationEngine::new()
            .simulate_fill(1, "AAPL", 100, 10_000, Some(40))
            .expect("fill");
        assert_eq!(fill.spread_impact_minor, 2_000);
    }

    #[test]
    fn override_engine_does_not_mutate_the_shared_default() {
        let overridden = PaperSimulationEngine::with_cost_config(CostConfig {
            commission: CommissionModel::PerTrade { fee_minor: 99 },
            slippage: SlippageModel::None,
            spread_impact: SpreadImpactModel::None,
        });
        let fill = overridden
            .simulate_fill(1, "AAPL", 100, 10_000, Some(40))
            .expect("fill");
        assert_eq!(fill.commission_minor, 99);
        assert_eq!(fill.slippage_minor, 0);
        assert_eq!(fill.spread_impact_minor, 0);
        // The shared default family is untouched.
        assert_eq!(CostConfig::default(), CostConfig::syrs_defaults());
    }

    #[test]
    fn cost_never_fabricates_cash_on_a_sell() {
        // Selling 100 @ $100.00: proceeds are 1_000_000; the cost REDUCES the
        // cash delta below the raw proceeds (a cost can never add cash).
        let fill = PaperSimulationEngine::new()
            .simulate_fill(1, "AAPL", -100, 10_000, None)
            .expect("fill");
        let total_cost_minor = fill.total_cost_minor().expect("total");
        assert!(total_cost_minor > 0);
        assert_eq!(fill.cash_delta_minor, 1_000_000 - total_cost_minor);
        assert!(fill.cash_delta_minor < 1_000_000);
    }

    #[test]
    fn empty_symbol_fails_closed() {
        assert_eq!(
            PaperSimulationEngine::new().simulate_fill(1, "   ", 100, 10_000, None),
            Err(SimError::EmptySymbol)
        );
    }

    #[test]
    fn non_positive_price_fails_closed() {
        assert_eq!(
            PaperSimulationEngine::new().simulate_fill(7, "AAPL", 100, 0, None),
            Err(SimError::NonPositivePrice {
                ts: 7,
                price_minor: 0,
            })
        );
    }

    #[test]
    fn negative_spread_fails_closed() {
        assert_eq!(
            PaperSimulationEngine::new().simulate_fill(3, "AAPL", 100, 10_000, Some(-1)),
            Err(SimError::NegativeSpread {
                ts: 3,
                spread_minor: -1,
            })
        );
    }

    #[test]
    fn negative_cost_parameter_fails_closed() {
        let engine = PaperSimulationEngine::with_cost_config(CostConfig {
            commission: CommissionModel::PerShare {
                rate_centiminor_per_share: -1,
                min_per_order_minor: 0,
            },
            ..CostConfig::default()
        });
        assert_eq!(
            engine.simulate_fill(1, "AAPL", 100, 10_000, None),
            Err(SimError::Cost(CostError::NegativeParameter {
                model: "commission",
                field: "rate_centiminor_per_share",
                value: -1,
            }))
        );
    }

    #[test]
    fn deterministic_for_identical_inputs() {
        let engine = PaperSimulationEngine::new();
        let first = engine.simulate_fill(1, "AAPL", 137, 9_973, Some(13));
        let second = engine.simulate_fill(1, "AAPL", 137, 9_973, Some(13));
        assert_eq!(first, second);
    }

    #[test]
    fn ledger_accumulates_cash_position_and_commission() {
        let engine = PaperSimulationEngine::new();
        let mut ledger = PaperLedger::new(10_000_000);
        let buy = engine
            .simulate_fill(1, "AAPL", 100, 10_000, None)
            .expect("buy");
        ledger.apply_fill(&buy).expect("apply buy");
        assert_eq!(ledger.position, 100);
        assert_eq!(ledger.commission_paid_minor, buy.commission_minor);
        assert_eq!(ledger.cash_minor, 10_000_000 + buy.cash_delta_minor);

        let sell = engine
            .simulate_fill(2, "AAPL", -100, 11_000, None)
            .expect("sell");
        ledger.apply_fill(&sell).expect("apply sell");
        assert_eq!(ledger.position, 0);
        assert_eq!(
            ledger.commission_paid_minor,
            buy.commission_minor + sell.commission_minor
        );
    }
}
