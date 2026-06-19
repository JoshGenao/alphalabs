//! Configurable transaction-cost model family for **SRS-BT-002** — "apply
//! configurable commission, slippage, and spread-impact models to backtests"
//! (SyRS SYS-15a / SYS-15b / SYS-15c / SYS-15d; StRS SN-1.03).
//!
//! # The shared cost-model family
//!
//! This module is the single home of the transaction-cost model *family*. The
//! [`backtest`](crate::backtest) engine applies it to backtest fills here
//! (SRS-BT-002); **SRS-BT-003** mandates that the internal simulation engine use
//! *the same family* for paper fills "unless explicitly configured otherwise", so
//! the same [`CostConfig`] type is the seam both engines share. The sim-fill
//! consumer now exists ([`sim::PaperSimulationEngine::simulate_fill`](crate::sim)
//! calls the identical [`CostConfig::cost_breakdown`] entry point), and SRS-BT-003
//! is `passes:true` — see
//! `architecture/runtime_services.json#sim_cost_contract`.
//!
//! # Defaults match the SyRS values
//!
//! [`CostConfig::default`] is the SyRS baseline, encoded once as named constants
//! so it is provably the published default:
//!
//! * commission — [`CommissionModel::IbTiered`]: the published Interactive
//!   Brokers tiered schedule ([`IB_TIERED_RATE_CENTIMINOR_PER_SHARE`] per share,
//!   floored at [`IB_TIERED_MIN_PER_ORDER_MINOR`] per order, capped at
//!   [`IB_TIERED_MAX_PCT_BPS`] of trade value) — SYS-15a.
//! * slippage — [`SlippageModel::NotionalBps`] at [`DEFAULT_SLIPPAGE_BPS`]
//!   (0.05% of trade notional per trade) — SYS-15b.
//! * spread impact — [`SpreadImpactModel::ObservedOrFallbackBps`]: half the
//!   observed bid-ask spread per share when the bar carries one, else
//!   [`DEFAULT_SPREAD_FALLBACK_BPS`] (0.10% of notional) — SYS-15c.
//!
//! An operator overrides any of the three for an individual run (SYS-15d) by
//! building a different [`CostConfig`] on the [`BacktestRequest`](crate::backtest::BacktestRequest)
//! — no strategy code changes. [`CostConfig::zero`] is the frictionless config.
//!
//! # Money math
//!
//! Every cost is computed in **integer minor units** (cents): `i128`
//! intermediates, deterministic round-half-up division, and `try_from` narrowing
//! back to `i64` (overflow surfaces as [`CostError::Overflow`]) — never floating
//! point, so identical inputs always yield identical costs (the SRS-BT-010
//! determinism property). Every model returns a **non-negative** cost, and the
//! engine always *subtracts* it from cash, so a cost can never fabricate cash.

use std::fmt;

/// Default slippage in basis points — SYS-15b (0.05% of trade notional = 5 bps).
pub const DEFAULT_SLIPPAGE_BPS: u32 = 5;

/// Default fallback spread impact in basis points when a bar carries no observed
/// bid-ask spread — SYS-15c (0.10% of trade notional = 10 bps).
pub const DEFAULT_SPREAD_FALLBACK_BPS: u32 = 10;

/// IB tiered base commission rate in **hundredths of a minor unit per share**
/// (centi-minor/share) — SYS-15a. $0.0035/share = 0.35 cent/share = 35
/// centi-minor/share (the published base ≤300k-shares/month tier). Expressing the
/// sub-cent per-share rate in centi-minor keeps commission math exact in integers.
pub const IB_TIERED_RATE_CENTIMINOR_PER_SHARE: i64 = 35;

/// IB tiered per-order minimum commission in minor units — SYS-15a ($0.35 = 35).
pub const IB_TIERED_MIN_PER_ORDER_MINOR: i64 = 35;

/// IB tiered maximum commission as a fraction of trade value, in basis points —
/// SYS-15a (1% of trade value = 100 bps).
pub const IB_TIERED_MAX_PCT_BPS: u32 = 100;

/// Scale of the per-share commission rate: rates are expressed in 1/100 of a
/// minor unit, so `commission_minor = round(shares * rate / COMMISSION_RATE_SCALE)`.
pub const COMMISSION_RATE_SCALE: i64 = 100;

/// Basis-point denominator (1 bp = 1/10000).
const BPS_DENOMINATOR: i128 = 10_000;

/// Fail-closed errors from cost-model configuration or computation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CostError {
    /// A configured cost parameter was negative. A transaction cost can never be
    /// negative (that would fabricate cash), so a negative parameter is rejected
    /// before any fill rather than silently producing a cash-adding "cost".
    NegativeParameter {
        model: &'static str,
        field: &'static str,
        value: i64,
    },
    /// An observed bid-ask spread was negative (corrupt quote data). A negative
    /// spread would otherwise produce a negative (cash-fabricating) spread cost.
    NegativeSpread { spread_minor: i64 },
    /// Cost arithmetic exceeded `i64` minor-unit range.
    Overflow,
}

impl fmt::Display for CostError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NegativeParameter {
                model,
                field,
                value,
            } => write!(
                f,
                "{model} cost model parameter `{field}` is negative ({value}); a cost cannot be negative"
            ),
            Self::NegativeSpread { spread_minor } => write!(
                f,
                "observed bid-ask spread is negative ({spread_minor} minor units)"
            ),
            Self::Overflow => write!(f, "transaction-cost math overflowed i64 minor units"),
        }
    }
}

impl std::error::Error for CostError {}

/// Commission model (SYS-15a). Default: the published IB tiered schedule.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum CommissionModel {
    /// SYS-15a default — the published IB tiered schedule: a per-share rate
    /// ([`IB_TIERED_RATE_CENTIMINOR_PER_SHARE`]) floored at a per-order minimum
    /// ([`IB_TIERED_MIN_PER_ORDER_MINOR`]) and capped at a fraction of trade value
    /// ([`IB_TIERED_MAX_PCT_BPS`]). The monthly-volume tier dimension is fixed to
    /// the published base tier; cumulative-volume tier tracking is deferred (it
    /// needs cross-run state — see `backtest_cost_contract.deferred`).
    #[default]
    IbTiered,
    /// Override — a flat per-share rate (centi-minor/share) with a per-order floor.
    PerShare {
        rate_centiminor_per_share: i64,
        min_per_order_minor: i64,
    },
    /// Override — a flat fee per trade, in minor units.
    PerTrade { fee_minor: i64 },
    /// Override — no commission.
    None,
}

/// Slippage model (SYS-15b). Default: 0.05% of trade notional per trade.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SlippageModel {
    /// A fraction of trade notional, in basis points. SYS-15b default is
    /// [`DEFAULT_SLIPPAGE_BPS`] (5 bps = 0.05%).
    NotionalBps { bps: u32 },
    /// Override — no slippage.
    None,
}

impl Default for SlippageModel {
    fn default() -> Self {
        Self::NotionalBps {
            bps: DEFAULT_SLIPPAGE_BPS,
        }
    }
}

/// Bid-ask spread-impact model (SYS-15c). Default: half the observed spread per
/// share when available, else a fallback fraction of notional.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SpreadImpactModel {
    /// SYS-15c default — when the bar carries an observed bid-ask spread, charge
    /// half of it per share (a single execution crosses half the spread from the
    /// mid); when no spread is observed, fall back to `fallback_bps` of notional
    /// ([`DEFAULT_SPREAD_FALLBACK_BPS`] = 10 bps = 0.10%).
    ObservedOrFallbackBps { fallback_bps: u32 },
    /// Override — always a fixed fraction of notional, in basis points (ignores
    /// any observed spread).
    FixedBps { bps: u32 },
    /// Override — no spread impact.
    None,
}

impl Default for SpreadImpactModel {
    fn default() -> Self {
        Self::ObservedOrFallbackBps {
            fallback_bps: DEFAULT_SPREAD_FALLBACK_BPS,
        }
    }
}

/// The per-fill cost decomposition (all non-negative minor units).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct CostBreakdown {
    pub commission_minor: i64,
    pub slippage_minor: i64,
    pub spread_impact_minor: i64,
}

impl CostBreakdown {
    /// The total cost charged for the fill, overflow-checked.
    pub fn total_minor(&self) -> Result<i64, CostError> {
        self.commission_minor
            .checked_add(self.slippage_minor)
            .and_then(|partial| partial.checked_add(self.spread_impact_minor))
            .ok_or(CostError::Overflow)
    }
}

/// The configurable transaction-cost model family for one backtest run. The
/// [`Default`] is the SyRS baseline; an operator overrides any field for an
/// individual run (SYS-15d). The internal simulation engine shares this exact
/// type for paper fills (SRS-BT-003).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct CostConfig {
    pub commission: CommissionModel,
    pub slippage: SlippageModel,
    pub spread_impact: SpreadImpactModel,
}

impl CostConfig {
    /// The SyRS default cost family (SYS-15a/b/c) — identical to [`Default`], named
    /// for call-site intent.
    pub fn syrs_defaults() -> Self {
        Self::default()
    }

    /// A frictionless config: no commission, slippage, or spread impact. Used by
    /// engine tests that assert the raw replay/ledger mechanics in isolation.
    pub fn zero() -> Self {
        Self {
            commission: CommissionModel::None,
            slippage: SlippageModel::None,
            spread_impact: SpreadImpactModel::None,
        }
    }

    /// Fail closed on a negative configured parameter before any fill (a cost can
    /// never be negative). The basis-point fields are `u32`, so only the
    /// signed-integer override parameters need checking.
    pub fn validate(&self) -> Result<(), CostError> {
        if let CommissionModel::PerShare {
            rate_centiminor_per_share,
            min_per_order_minor,
        } = self.commission
        {
            if rate_centiminor_per_share < 0 {
                return Err(CostError::NegativeParameter {
                    model: "commission",
                    field: "rate_centiminor_per_share",
                    value: rate_centiminor_per_share,
                });
            }
            if min_per_order_minor < 0 {
                return Err(CostError::NegativeParameter {
                    model: "commission",
                    field: "min_per_order_minor",
                    value: min_per_order_minor,
                });
            }
        }
        if let CommissionModel::PerTrade { fee_minor } = self.commission {
            if fee_minor < 0 {
                return Err(CostError::NegativeParameter {
                    model: "commission",
                    field: "fee_minor",
                    value: fee_minor,
                });
            }
        }
        Ok(())
    }

    /// Compute the non-negative [`CostBreakdown`] for a fill of `quantity` shares
    /// at `price_minor`, given the bar's optional observed bid-ask spread. A
    /// zero-quantity (no-trade) call costs nothing.
    pub fn cost_breakdown(
        &self,
        quantity: i64,
        price_minor: i64,
        observed_spread_minor: Option<i64>,
    ) -> Result<CostBreakdown, CostError> {
        if quantity == 0 {
            return Ok(CostBreakdown::default());
        }
        let commission_minor = self.commission.commission_minor(quantity, price_minor)?;
        let slippage_minor = self.slippage.slippage_minor(quantity, price_minor)?;
        let spread_impact_minor =
            self.spread_impact
                .spread_impact_minor(quantity, price_minor, observed_spread_minor)?;
        Ok(CostBreakdown {
            commission_minor,
            slippage_minor,
            spread_impact_minor,
        })
    }
}

impl CommissionModel {
    /// Non-negative commission charged for a fill, in minor units.
    pub fn commission_minor(&self, quantity: i64, price_minor: i64) -> Result<i64, CostError> {
        let shares = abs_shares(quantity);
        if shares == 0 {
            return Ok(0);
        }
        match *self {
            Self::IbTiered => ib_tiered_commission(quantity, price_minor),
            Self::PerShare {
                rate_centiminor_per_share,
                min_per_order_minor,
            } => {
                if rate_centiminor_per_share < 0 || min_per_order_minor < 0 {
                    return Err(CostError::NegativeParameter {
                        model: "commission",
                        field: "per_share",
                        value: rate_centiminor_per_share.min(min_per_order_minor),
                    });
                }
                let per_share = div_round_half_up(
                    shares * i128::from(rate_centiminor_per_share),
                    i128::from(COMMISSION_RATE_SCALE),
                );
                narrow(per_share.max(i128::from(min_per_order_minor)))
            }
            Self::PerTrade { fee_minor } => {
                if fee_minor < 0 {
                    return Err(CostError::NegativeParameter {
                        model: "commission",
                        field: "fee_minor",
                        value: fee_minor,
                    });
                }
                Ok(fee_minor)
            }
            Self::None => Ok(0),
        }
    }
}

impl SlippageModel {
    /// Non-negative slippage charged for a fill, in minor units.
    pub fn slippage_minor(&self, quantity: i64, price_minor: i64) -> Result<i64, CostError> {
        match *self {
            Self::NotionalBps { bps } => notional_bps_cost(quantity, price_minor, bps),
            Self::None => Ok(0),
        }
    }
}

impl SpreadImpactModel {
    /// Non-negative spread-impact cost for a fill, in minor units.
    pub fn spread_impact_minor(
        &self,
        quantity: i64,
        price_minor: i64,
        observed_spread_minor: Option<i64>,
    ) -> Result<i64, CostError> {
        match *self {
            Self::ObservedOrFallbackBps { fallback_bps } => match observed_spread_minor {
                Some(spread_minor) => observed_half_spread_cost(quantity, spread_minor),
                None => notional_bps_cost(quantity, price_minor, fallback_bps),
            },
            Self::FixedBps { bps } => notional_bps_cost(quantity, price_minor, bps),
            Self::None => Ok(0),
        }
    }
}

/// `|quantity|` as a non-negative `i128` share count (safe even for `i64::MIN`).
fn abs_shares(quantity: i64) -> i128 {
    i128::from(quantity).abs()
}

/// `|quantity * price_minor|` as a non-negative `i128`. The `i64 * i64` product
/// always fits in `i128`, so this never overflows here; the caller narrows the
/// result back to `i64` and surfaces [`CostError::Overflow`] if it does not fit.
fn abs_trade_value(quantity: i64, price_minor: i64) -> i128 {
    (i128::from(quantity) * i128::from(price_minor)).abs()
}

/// Deterministic round-half-up division of a **non-negative** numerator by a
/// positive denominator. Ties (exactly `.5`) round up. Integer + total ordering
/// make it reproducible (the SRS-BT-010 determinism property).
fn div_round_half_up(numerator: i128, denominator: i128) -> i128 {
    debug_assert!(numerator >= 0 && denominator > 0);
    (numerator + denominator / 2) / denominator
}

/// Narrow a non-negative `i128` minor-unit value back to `i64`, failing closed on
/// overflow.
fn narrow(value: i128) -> Result<i64, CostError> {
    i64::try_from(value).map_err(|_| CostError::Overflow)
}

/// `bps` of `|quantity * price_minor|`, round-half-up, in minor units. The trade
/// value must fit `i64` (the same bound the engine's notional respects).
fn notional_bps_cost(quantity: i64, price_minor: i64, bps: u32) -> Result<i64, CostError> {
    let trade_value = narrow(abs_trade_value(quantity, price_minor))?;
    let cost = div_round_half_up(i128::from(trade_value) * i128::from(bps), BPS_DENOMINATOR);
    narrow(cost)
}

/// Half of the observed bid-ask spread per share (a single execution crosses half
/// the spread from the mid). A negative spread is corrupt quote data and fails
/// closed.
fn observed_half_spread_cost(quantity: i64, spread_minor: i64) -> Result<i64, CostError> {
    if spread_minor < 0 {
        return Err(CostError::NegativeSpread { spread_minor });
    }
    let cost = div_round_half_up(abs_shares(quantity) * i128::from(spread_minor), 2);
    narrow(cost)
}

/// The published IB tiered commission: per-share rate, floored at the per-order
/// minimum, then capped at the maximum percent of trade value. The cap is a hard
/// upper bound, so for a sub-minimum tiny trade the percent cap wins over the
/// per-order floor (documented clamp order: `min(max(rate, floor), cap)`).
fn ib_tiered_commission(quantity: i64, price_minor: i64) -> Result<i64, CostError> {
    let shares = abs_shares(quantity);
    let per_share = div_round_half_up(
        shares * i128::from(IB_TIERED_RATE_CENTIMINOR_PER_SHARE),
        i128::from(COMMISSION_RATE_SCALE),
    );
    let floored = per_share.max(i128::from(IB_TIERED_MIN_PER_ORDER_MINOR));
    let trade_value = narrow(abs_trade_value(quantity, price_minor))?;
    let cap = div_round_half_up(
        i128::from(trade_value) * i128::from(IB_TIERED_MAX_PCT_BPS),
        BPS_DENOMINATOR,
    );
    narrow(floored.min(cap))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_config_is_the_syrs_baseline() {
        let config = CostConfig::default();
        assert_eq!(config.commission, CommissionModel::IbTiered);
        assert_eq!(
            config.slippage,
            SlippageModel::NotionalBps {
                bps: DEFAULT_SLIPPAGE_BPS
            }
        );
        assert_eq!(
            config.spread_impact,
            SpreadImpactModel::ObservedOrFallbackBps {
                fallback_bps: DEFAULT_SPREAD_FALLBACK_BPS
            }
        );
        // The constants encode the published SyRS defaults exactly.
        assert_eq!(DEFAULT_SLIPPAGE_BPS, 5); // SYS-15b: 0.05%
        assert_eq!(DEFAULT_SPREAD_FALLBACK_BPS, 10); // SYS-15c: 0.10%
        assert_eq!(IB_TIERED_RATE_CENTIMINOR_PER_SHARE, 35); // SYS-15a: $0.0035/share
        assert_eq!(IB_TIERED_MIN_PER_ORDER_MINOR, 35); // SYS-15a: $0.35/order
        assert_eq!(IB_TIERED_MAX_PCT_BPS, 100); // SYS-15a: 1% of trade value
        assert_eq!(config, CostConfig::syrs_defaults());
    }

    #[test]
    fn zero_config_is_frictionless() {
        let breakdown = CostConfig::zero()
            .cost_breakdown(100, 10_000, Some(50))
            .expect("zero config never errors");
        assert_eq!(breakdown, CostBreakdown::default());
        assert_eq!(breakdown.total_minor(), Ok(0));
    }

    #[test]
    fn ib_tiered_default_commission_matches_published_rate() {
        // 1000 shares @ $50.00 (5000 minor): $0.0035 * 1000 = $3.50 = 350 minor,
        // above the $0.35 floor and below the 1% ($500) cap.
        let commission = CommissionModel::IbTiered
            .commission_minor(1000, 5_000)
            .expect("commission");
        assert_eq!(commission, 350);
    }

    #[test]
    fn ib_tiered_applies_the_per_order_minimum() {
        // 10 shares @ $50.00: $0.0035 * 10 = $0.035 -> rounds to 4 minor, below the
        // $0.35 floor, and well below the 1% cap ($5.00) -> floored to 35 minor.
        let commission = CommissionModel::IbTiered
            .commission_minor(10, 5_000)
            .expect("commission");
        assert_eq!(commission, 35);
    }

    #[test]
    fn ib_tiered_caps_at_one_percent_of_a_tiny_trade() {
        // 1 share @ $1.00 (100 minor): the 1% cap is 1 minor, below the $0.35
        // floor -> the hard cap wins.
        let commission = CommissionModel::IbTiered
            .commission_minor(1, 100)
            .expect("commission");
        assert_eq!(commission, 1);
    }

    #[test]
    fn slippage_default_is_five_basis_points() {
        // 100 shares @ $100.00 = $10,000 notional (1,000,000 minor); 5 bps = 500 minor.
        let slippage = SlippageModel::default()
            .slippage_minor(100, 10_000)
            .expect("slippage");
        assert_eq!(slippage, 500);
    }

    #[test]
    fn slippage_is_direction_independent() {
        let buy = SlippageModel::default().slippage_minor(100, 10_000);
        let sell = SlippageModel::default().slippage_minor(-100, 10_000);
        assert_eq!(buy, sell);
        assert_eq!(buy, Ok(500));
    }

    #[test]
    fn spread_uses_half_the_observed_spread_when_available() {
        // 100 shares, observed spread 20 minor -> half = 10/share -> 1000 minor.
        let cost = SpreadImpactModel::default()
            .spread_impact_minor(100, 10_000, Some(20))
            .expect("spread");
        assert_eq!(cost, 1_000);
    }

    #[test]
    fn spread_falls_back_to_ten_bps_when_unavailable() {
        // No observed spread -> 10 bps of $10,000 notional = 1000 minor.
        let cost = SpreadImpactModel::default()
            .spread_impact_minor(100, 10_000, None)
            .expect("spread");
        assert_eq!(cost, 1_000);
    }

    #[test]
    fn negative_observed_spread_fails_closed() {
        assert_eq!(
            SpreadImpactModel::default().spread_impact_minor(100, 10_000, Some(-1)),
            Err(CostError::NegativeSpread { spread_minor: -1 })
        );
    }

    #[test]
    fn override_models_change_costs_without_touching_defaults() {
        let config = CostConfig {
            commission: CommissionModel::PerTrade { fee_minor: 99 },
            slippage: SlippageModel::None,
            spread_impact: SpreadImpactModel::FixedBps { bps: 25 },
        };
        let breakdown = config
            .cost_breakdown(100, 10_000, Some(20))
            .expect("breakdown");
        assert_eq!(breakdown.commission_minor, 99);
        assert_eq!(breakdown.slippage_minor, 0);
        // FixedBps ignores the observed spread: 25 bps of $10,000 = 2500 minor.
        assert_eq!(breakdown.spread_impact_minor, 2_500);
    }

    #[test]
    fn negative_commission_parameter_fails_validation() {
        let config = CostConfig {
            commission: CommissionModel::PerShare {
                rate_centiminor_per_share: -1,
                min_per_order_minor: 0,
            },
            ..CostConfig::default()
        };
        assert_eq!(
            config.validate(),
            Err(CostError::NegativeParameter {
                model: "commission",
                field: "rate_centiminor_per_share",
                value: -1,
            })
        );
    }

    #[test]
    fn negative_per_trade_fee_fails_validation() {
        let config = CostConfig {
            commission: CommissionModel::PerTrade { fee_minor: -5 },
            ..CostConfig::default()
        };
        assert_eq!(
            config.validate(),
            Err(CostError::NegativeParameter {
                model: "commission",
                field: "fee_minor",
                value: -5,
            })
        );
    }

    #[test]
    fn costs_are_deterministic_for_identical_inputs() {
        let config = CostConfig::default();
        let first = config.cost_breakdown(137, 9_973, Some(13));
        let second = config.cost_breakdown(137, 9_973, Some(13));
        assert_eq!(first, second);
    }

    #[test]
    fn total_cost_is_the_sum_of_components() {
        let breakdown = CostConfig::default()
            .cost_breakdown(100, 10_000, None)
            .expect("breakdown");
        let expected =
            breakdown.commission_minor + breakdown.slippage_minor + breakdown.spread_impact_minor;
        assert_eq!(breakdown.total_minor(), Ok(expected));
    }

    #[test]
    fn no_trade_costs_nothing() {
        let breakdown = CostConfig::default()
            .cost_breakdown(0, 10_000, Some(20))
            .expect("breakdown");
        assert_eq!(breakdown, CostBreakdown::default());
    }

    #[test]
    fn commission_overflow_fails_closed() {
        // A per-share rate at i64::MAX over a huge share count overflows the narrow.
        let result = CommissionModel::PerShare {
            rate_centiminor_per_share: i64::MAX,
            min_per_order_minor: 0,
        }
        .commission_minor(i64::MAX, 1);
        assert_eq!(result, Err(CostError::Overflow));
    }
}
