//! SRS-BT-003 shared-cost-family integration test (Rust crate-level).
//!
//! Proves the acceptance criterion directly: "A paper strategy and backtest
//! using identical cost configuration compute fills and commissions from the same
//! model family" (SyRS SYS-15e / SYS-83d). The same [`CostConfig`] is fed to both
//! the runnable [`BacktestEngine`] and the [`PaperSimulationEngine`], and the
//! per-fill commission / slippage / spread-impact decomposition is asserted
//! **equal** between the two engines — under the SyRS default family, under an
//! operator override, and on the observed-spread path. Every assertion is exact
//! integer minor units.

use atp_simulation::backtest::{
    BacktestBar, BacktestDataSource, BacktestEngine, BacktestError, BacktestRequest,
    BacktestStrategy, BarSource, DateRange, Fill,
};
use atp_simulation::cost::{CommissionModel, CostConfig, SlippageModel, SpreadImpactModel};
use atp_simulation::sim::{PaperSimulationEngine, SimError};
use atp_types::StrategyId;

const SYMBOL: &str = "AAPL";

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

fn bar(ts: u64, close_minor: i64, spread_minor: Option<i64>) -> BacktestBar {
    BacktestBar {
        symbol: SYMBOL.to_string(),
        ts,
        close_minor,
        spread_minor,
    }
}

fn request(cost_config: CostConfig) -> BacktestRequest {
    BacktestRequest {
        strategy_id: StrategyId::new("bt-003"),
        symbol: SYMBOL.to_string(),
        data_source: BacktestDataSource::SystemData,
        range: DateRange::new(1, 5),
        starting_cash_minor: 10_000_000, // $100,000.00
        cost_config,
    }
}

/// Run a single-buy backtest and return the one resulting [`Fill`].
fn backtest_one_fill(cost_config: CostConfig, lot: i64, the_bar: BacktestBar) -> Fill {
    let source = FixtureCatalog {
        bars: vec![the_bar],
    };
    let mut strategy = BuyOnceAndHold { lot, bought: false };
    let result = BacktestEngine::new()
        .run(&request(cost_config), &mut strategy, &source)
        .expect("backtest should run");
    assert_eq!(result.trade_log.len(), 1, "expected exactly one fill");
    result.trade_log[0].clone()
}

/// Assert the simulated paper fill's cost decomposition equals the backtest
/// fill's, for the same `(quantity, price, spread)` and the same cost config.
fn assert_same_costs(cost_config: CostConfig, lot: i64, close_minor: i64, spread: Option<i64>) {
    let bt_fill = backtest_one_fill(cost_config, lot, bar(1, close_minor, spread));
    let sim_fill = PaperSimulationEngine::with_cost_config(cost_config)
        .simulate_fill(1, SYMBOL, lot, close_minor, spread)
        .expect("paper fill");

    assert_eq!(
        sim_fill.commission_minor, bt_fill.commission_minor,
        "commission must come from the same model family"
    );
    assert_eq!(
        sim_fill.slippage_minor, bt_fill.slippage_minor,
        "slippage must come from the same model family"
    );
    assert_eq!(
        sim_fill.spread_impact_minor, bt_fill.spread_impact_minor,
        "spread impact must come from the same model family"
    );
}

#[test]
fn sim_default_cost_family_equals_backtest_default() {
    // SYS-15e: the internal simulation engine's default cost family IS the
    // backtest engine's default (CostConfig::default()), "unless explicitly
    // configured otherwise".
    assert_eq!(
        PaperSimulationEngine::default().cost_config(),
        &CostConfig::default()
    );
}

#[test]
fn identical_default_config_produces_identical_fills_and_commissions() {
    // The headline acceptance criterion: identical (default) cost configuration
    // -> identical fills and commissions in both engines.
    assert_same_costs(CostConfig::default(), 100, 10_000, None);
}

#[test]
fn override_config_is_identical_across_both_engines() {
    // The same per-run override applied to both engines (SYS-15d/SYS-15e): still
    // identical, because both call the same cost-model family.
    let overridden = CostConfig {
        commission: CommissionModel::PerTrade { fee_minor: 100 },
        slippage: SlippageModel::None,
        spread_impact: SpreadImpactModel::FixedBps { bps: 25 },
    };
    assert_same_costs(overridden, 100, 10_000, Some(40));
}

#[test]
fn observed_spread_path_matches_between_engines() {
    // The bar carries an observed spread; both engines use the SYS-15c observed
    // half-spread path identically.
    assert_same_costs(CostConfig::default(), 100, 10_000, Some(40));
}

#[test]
fn sim_cost_never_fabricates_cash_on_a_sell() {
    // A sell's proceeds are reduced by the cost, never increased — the same
    // money-safety invariant the backtest engine enforces.
    let fill = PaperSimulationEngine::new()
        .simulate_fill(1, SYMBOL, -100, 10_000, None)
        .expect("paper fill");
    let total_cost_minor = fill.total_cost_minor().expect("total cost");
    assert!(
        total_cost_minor > 0,
        "default family charges a positive cost"
    );
    assert_eq!(fill.cash_delta_minor, 1_000_000 - total_cost_minor);
    assert!(fill.cash_delta_minor < 1_000_000);
}

#[test]
fn per_strategy_override_does_not_mutate_the_shared_default() {
    // Overriding one engine's family leaves the shared default untouched, so
    // other strategies keep the SyRS baseline ("unless explicitly configured").
    let _overridden = PaperSimulationEngine::with_cost_config(CostConfig::zero());
    assert_eq!(
        PaperSimulationEngine::default().cost_config(),
        &CostConfig::default()
    );
    assert_ne!(CostConfig::zero(), CostConfig::default());
}

#[test]
fn negative_observed_spread_fails_closed_in_simulation() {
    assert_eq!(
        PaperSimulationEngine::new().simulate_fill(1, SYMBOL, 100, 10_000, Some(-1)),
        Err(SimError::NegativeSpread {
            ts: 1,
            spread_minor: -1,
        })
    );
}

#[test]
fn negative_cost_parameter_fails_closed_in_simulation() {
    let engine = PaperSimulationEngine::with_cost_config(CostConfig {
        commission: CommissionModel::PerShare {
            rate_centiminor_per_share: -1,
            min_per_order_minor: 0,
        },
        ..CostConfig::default()
    });
    let outcome = engine.simulate_fill(1, SYMBOL, 100, 10_000, None);
    assert!(matches!(outcome, Err(SimError::Cost(_))));
}

#[test]
fn identical_inputs_are_deterministic() {
    let engine = PaperSimulationEngine::new();
    let run = || engine.simulate_fill(1, SYMBOL, 137, 9_973, Some(13));
    assert_eq!(run(), run());
}
