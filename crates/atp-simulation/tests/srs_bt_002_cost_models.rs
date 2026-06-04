//! SRS-BT-002 cost-model integration test (Rust crate-level).
//!
//! Drives the runnable [`BacktestEngine`] with the configurable transaction-cost
//! model family the way a real run would: the SyRS-default cost family
//! ([`CostConfig::default`]) is applied to fills, an operator overrides any model
//! per run without touching the strategy, the observed-spread and fallback
//! spread-impact paths are exercised, and the fail-closed branches (a negative
//! observed spread, a negative cost parameter) are verified. Every money
//! assertion is exact integer minor units.
//!
//! Acceptance (SRS-BT-002 / SyRS SYS-15a–d): defaults match the SyRS values; a
//! run can override commission, slippage, and spread-impact models without
//! changing strategy code.

use atp_simulation::backtest::{
    BacktestBar, BacktestDataSource, BacktestEngine, BacktestError, BacktestRequest,
    BacktestStrategy, BarSource, DateRange,
};
use atp_simulation::cost::{
    CommissionModel, CostConfig, CostError, SlippageModel, SpreadImpactModel,
};
use atp_types::StrategyId;

/// A system-data catalog of explicit bars (each may carry an observed spread).
struct FixtureCatalog {
    bars: Vec<BacktestBar>,
}

impl BarSource for FixtureCatalog {
    fn source(&self) -> BacktestDataSource {
        BacktestDataSource::SystemData
    }

    fn bars(
        &self,
        symbol: &str,
        range: &DateRange,
        max_bars: usize,
    ) -> Result<Vec<BacktestBar>, BacktestError> {
        let rows: Vec<BacktestBar> = self
            .bars
            .iter()
            .filter(|bar| bar.symbol == symbol && range.contains(bar.ts))
            .cloned()
            .collect();
        if rows.len() > max_bars {
            return Err(BacktestError::TooManyBars {
                count: rows.len(),
                limit: max_bars,
            });
        }
        Ok(rows)
    }
}

/// Buys `lot` shares on the first bar it sees, then holds.
struct BuyOnceAndHold {
    lot: i64,
    bought: bool,
}

impl BacktestStrategy for BuyOnceAndHold {
    fn on_bar(&mut self, _bar: &BacktestBar, _position: i64) -> Result<i64, BacktestError> {
        if self.bought {
            return Ok(0);
        }
        self.bought = true;
        Ok(self.lot)
    }
}

/// Buys `lot` on the first bar, sells the whole position on the second.
struct BuyThenSell {
    lot: i64,
    bars_seen: u32,
}

impl BacktestStrategy for BuyThenSell {
    fn on_bar(&mut self, _bar: &BacktestBar, position: i64) -> Result<i64, BacktestError> {
        self.bars_seen += 1;
        match self.bars_seen {
            1 => Ok(self.lot),
            2 => Ok(-position),
            _ => Ok(0),
        }
    }
}

fn bar(ts: u64, close_minor: i64, spread_minor: Option<i64>) -> BacktestBar {
    BacktestBar {
        symbol: "AAPL".to_string(),
        ts,
        close_minor,
        spread_minor,
    }
}

fn request(cost_config: CostConfig) -> BacktestRequest {
    BacktestRequest {
        strategy_id: StrategyId::new("bt-cost"),
        symbol: "AAPL".to_string(),
        data_source: BacktestDataSource::SystemData,
        range: DateRange::new(1, 5),
        starting_cash_minor: 10_000_000, // $100,000.00
        cost_config,
    }
}

#[test]
fn default_cost_family_matches_the_syrs_values() {
    // Buy 100 @ $100.00 (10_000 minor) on bar 1, no observed spread on the bar.
    let source = FixtureCatalog {
        bars: vec![bar(1, 10_000, None)],
    };
    let mut strategy = BuyOnceAndHold {
        lot: 100,
        bought: false,
    };
    let result = BacktestEngine::new()
        .run(&request(CostConfig::default()), &mut strategy, &source)
        .expect("backtest should run");

    assert_eq!(result.trade_log.len(), 1);
    let fill = &result.trade_log[0];
    // SYS-15a IB tiered: 100 shares * $0.0035 = $0.35 = 35 minor (above the $0.35
    // floor, below the 1% cap of $1,000).
    assert_eq!(fill.commission_minor, 35);
    // SYS-15b: 0.05% of $10,000 notional = 500 minor.
    assert_eq!(fill.slippage_minor, 500);
    // SYS-15c fallback (no observed spread): 0.10% of $10,000 = 1000 minor.
    assert_eq!(fill.spread_impact_minor, 1_000);

    // Costs reduce cash exactly: equity at the close = starting - costs.
    // cash = 10_000_000 - 1_000_000 (notional) - 1_535 (costs); equity adds back
    // the 1_000_000 of holdings, so equity = 10_000_000 - 1_535.
    assert_eq!(result.final_equity_minor, 10_000_000 - 1_535);
}

#[test]
fn observed_spread_is_used_when_the_bar_carries_one() {
    // Same buy, but the bar carries an observed bid-ask spread of 40 minor.
    let source = FixtureCatalog {
        bars: vec![bar(1, 10_000, Some(40))],
    };
    let mut strategy = BuyOnceAndHold {
        lot: 100,
        bought: false,
    };
    let result = BacktestEngine::new()
        .run(&request(CostConfig::default()), &mut strategy, &source)
        .expect("backtest should run");

    let fill = &result.trade_log[0];
    // SYS-15c observed path: half the spread per share = 20/share * 100 = 2000
    // minor — distinct from the 1000-minor fallback, proving the bar's observed
    // spread is actually used.
    assert_eq!(fill.spread_impact_minor, 2_000);
    assert_eq!(fill.commission_minor, 35);
    assert_eq!(fill.slippage_minor, 500);
}

#[test]
fn operator_overrides_each_model_without_changing_strategy_code() {
    // The SAME strategy instance class, only the cost_config differs — the
    // SYS-15d acceptance criterion (override without touching strategy code).
    let source = FixtureCatalog {
        bars: vec![bar(1, 10_000, Some(40))],
    };
    let overridden = CostConfig {
        commission: CommissionModel::PerTrade { fee_minor: 100 },
        slippage: SlippageModel::None,
        spread_impact: SpreadImpactModel::FixedBps { bps: 25 },
    };
    let mut strategy = BuyOnceAndHold {
        lot: 100,
        bought: false,
    };
    let result = BacktestEngine::new()
        .run(&request(overridden), &mut strategy, &source)
        .expect("backtest should run");

    let fill = &result.trade_log[0];
    // Flat per-trade fee; slippage disabled.
    assert_eq!(fill.commission_minor, 100);
    assert_eq!(fill.slippage_minor, 0);
    // FixedBps ignores the observed spread: 25 bps of $10,000 = 2500 minor.
    assert_eq!(fill.spread_impact_minor, 2_500);
}

#[test]
fn zero_cost_config_is_frictionless() {
    // The override that turns the whole family off reproduces the BT-001
    // frictionless ledger, proving the cost family is the only thing changing.
    let source = FixtureCatalog {
        bars: vec![bar(1, 10_000, Some(40))],
    };
    let mut strategy = BuyOnceAndHold {
        lot: 100,
        bought: false,
    };
    let result = BacktestEngine::new()
        .run(&request(CostConfig::zero()), &mut strategy, &source)
        .expect("backtest should run");

    let fill = &result.trade_log[0];
    assert_eq!(fill.commission_minor, 0);
    assert_eq!(fill.slippage_minor, 0);
    assert_eq!(fill.spread_impact_minor, 0);
    assert_eq!(result.final_equity_minor, 10_000_000);
}

#[test]
fn costs_strictly_reduce_equity_and_never_fabricate_cash() {
    // Buy then sell across rising prices, comparing the default-cost run to the
    // frictionless run with an identical strategy. Because the position path is
    // identical, the only difference in final equity is the total of every fill's
    // costs — which must be strictly positive (a cost can never add cash, not
    // even on the sell, whose proceeds are reduced rather than increased).
    let bars = vec![bar(1, 10_000, None), bar(2, 11_000, None)];

    let mut strategy_default = BuyThenSell {
        lot: 100,
        bars_seen: 0,
    };
    let with_costs = BacktestEngine::new()
        .run(
            &request(CostConfig::default()),
            &mut strategy_default,
            &FixtureCatalog { bars: bars.clone() },
        )
        .expect("backtest should run");

    let mut strategy_zero = BuyThenSell {
        lot: 100,
        bars_seen: 0,
    };
    let frictionless = BacktestEngine::new()
        .run(
            &request(CostConfig::zero()),
            &mut strategy_zero,
            &FixtureCatalog { bars },
        )
        .expect("backtest should run");

    // Two fills (a buy and a sell), each with strictly positive total cost.
    assert_eq!(with_costs.trade_log.len(), 2);
    let total_costs: i64 = with_costs
        .trade_log
        .iter()
        .map(|fill| fill.commission_minor + fill.slippage_minor + fill.spread_impact_minor)
        .sum();
    for fill in &with_costs.trade_log {
        let fill_cost = fill.commission_minor + fill.slippage_minor + fill.spread_impact_minor;
        assert!(fill_cost > 0, "every fill incurs a positive cost");
    }
    // The frictionless run keeps exactly `total_costs` more equity.
    assert_eq!(
        frictionless.final_equity_minor - with_costs.final_equity_minor,
        total_costs
    );
    assert!(with_costs.final_equity_minor < frictionless.final_equity_minor);
}

#[test]
fn identical_inputs_with_default_costs_are_deterministic() {
    let run = || {
        let source = FixtureCatalog {
            bars: vec![bar(1, 9_973, Some(13)), bar(2, 10_127, None)],
        };
        let mut strategy = BuyThenSell {
            lot: 137,
            bars_seen: 0,
        };
        BacktestEngine::new()
            .run(&request(CostConfig::default()), &mut strategy, &source)
            .expect("backtest should run")
    };
    assert_eq!(run(), run());
}

#[test]
fn negative_observed_spread_fails_closed() {
    // A corrupt negative spread must fail closed before any fill, so it cannot
    // drive a cash-fabricating spread cost (mirrors the non-positive-price guard).
    let source = FixtureCatalog {
        bars: vec![bar(1, 10_000, Some(-1))],
    };
    let mut strategy = BuyOnceAndHold {
        lot: 100,
        bought: false,
    };
    let outcome =
        BacktestEngine::new().run(&request(CostConfig::default()), &mut strategy, &source);
    assert_eq!(
        outcome,
        Err(BacktestError::NegativeSpread {
            ts: 1,
            spread_minor: -1,
        })
    );
}

#[test]
fn negative_cost_parameter_fails_closed_before_any_data_is_read() {
    // A misconfigured (negative) cost parameter is rejected up front, surfaced as
    // a Cost(NegativeParameter) — a negative cost would otherwise fabricate cash.
    let source = FixtureCatalog {
        bars: vec![bar(1, 10_000, None)],
    };
    let misconfigured = CostConfig {
        commission: CommissionModel::PerShare {
            rate_centiminor_per_share: -1,
            min_per_order_minor: 0,
        },
        ..CostConfig::default()
    };
    let mut strategy = BuyOnceAndHold {
        lot: 100,
        bought: false,
    };
    let outcome = BacktestEngine::new().run(&request(misconfigured), &mut strategy, &source);
    assert_eq!(
        outcome,
        Err(BacktestError::Cost(CostError::NegativeParameter {
            model: "commission",
            field: "rate_centiminor_per_share",
            value: -1,
        }))
    );
}
