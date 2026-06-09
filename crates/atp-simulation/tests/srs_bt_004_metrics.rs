//! SRS-BT-004 end-to-end performance-metric integration test (Rust crate-level).
//!
//! Drives [`atp_simulation::metrics::compute`] the way a real report would: run a
//! deterministic backtest through the public [`BacktestEngine`] surface, then compute
//! the SYS-16 / SYS-86 metric family from the produced equity curve and trade log,
//! against an SPY-default benchmark. The defined-metric, undefined-metric (None),
//! determinism, SPY-default, win-rate-vs-ledger, and fail-closed (misaligned
//! benchmark) paths are all exercised from real engine output. Money inputs are exact
//! integer minor units; the metrics are dimensionless f64 ratios.

use atp_simulation::backtest::{
    BacktestBar, BacktestDataSource, BacktestEngine, BacktestError, BacktestRequest,
    BacktestResult, BacktestStrategy, BarSource, DateRange, EquityPoint, Fill,
};
use atp_simulation::cost::CostConfig;
use atp_simulation::metrics::{compute, Benchmark, BenchmarkPoint, MetricsConfig, MetricsError};
use atp_types::StrategyId;

/// A fixture catalog of close-only bars that honors the requested window.
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

/// Opens `lot` shares on the first bar, then fully closes on `sell_ts` -- one round
/// trip, so the trade log carries a closeable position for the win rate.
struct RoundTrip {
    lot: i64,
    sell_ts: u64,
}

impl BacktestStrategy for RoundTrip {
    fn on_bar(&mut self, bar: &BacktestBar, position: i64) -> Result<i64, BacktestError> {
        if bar.ts == self.sell_ts {
            return Ok(-position);
        }
        if position == 0 {
            return Ok(self.lot);
        }
        Ok(0)
    }
}

fn bar(ts: u64, close_minor: i64) -> BacktestBar {
    BacktestBar {
        symbol: "AAPL".to_string(),
        ts,
        close_minor,
        spread_minor: None,
    }
}

/// The pre-trade starting equity the backtest is launched with; passed to `compute` as
/// the baseline so the first period's P&L is captured.
const STARTING_CASH_MINOR: i64 = 1_000_000;

fn run_backtest(bars: Vec<BacktestBar>, lot: i64, sell_ts: u64) -> BacktestResult {
    let catalog = FixtureCatalog { bars };
    let request = BacktestRequest {
        strategy_id: StrategyId::new("bt-004"),
        symbol: "AAPL".to_string(),
        data_source: BacktestDataSource::SystemData,
        range: DateRange::new(0, 100),
        starting_cash_minor: STARTING_CASH_MINOR,
        cost_config: CostConfig::default(),
    };
    let mut strategy = RoundTrip { lot, sell_ts };
    BacktestEngine::new()
        .run(&request, &mut strategy, &catalog)
        .expect("backtest runs")
}

/// A benchmark level series for `compute`: the `baseline_level` (pairing with the
/// strategy's starting equity) at ts 0, then the per-mark `levels` aligned 1:1 by
/// timestamp with the equity curve. The result has `equity_curve.len() + 1` entries.
fn aligned_benchmark(
    result: &BacktestResult,
    baseline_level: i64,
    levels: &[i64],
) -> Vec<BenchmarkPoint> {
    assert_eq!(result.equity_curve.len(), levels.len());
    let mut series = vec![BenchmarkPoint {
        ts: 0,
        level_minor: baseline_level,
    }];
    series.extend(
        result
            .equity_curve
            .iter()
            .zip(levels.iter())
            .map(|(point, &level_minor)| BenchmarkPoint {
                ts: point.ts,
                level_minor,
            }),
    );
    series
}

#[test]
fn srs_bt_004_metrics_computed_from_real_backtest() {
    // Five closes with a real drawdown (120 -> 90 while long), a profitable round
    // trip (buy 10 @ 100, sell 10 @ 125).
    let bars = vec![
        bar(1, 100),
        bar(2, 120),
        bar(3, 90),
        bar(4, 130),
        bar(5, 125),
    ];
    let result = run_backtest(bars, 10, 5);
    let benchmark = Benchmark::spy();
    let levels = aligned_benchmark(&result, 400, &[400, 420, 380, 430, 425]);
    let metrics = compute(
        STARTING_CASH_MINOR,
        &result.equity_curve,
        &result.trade_log,
        &benchmark,
        Some(&levels),
        &MetricsConfig::default(),
    )
    .expect("metrics computed");

    // The full family is defined for a five-point curve with a benchmark.
    assert_eq!(metrics.benchmark_symbol, "SPY");
    assert!(metrics.sharpe_ratio.is_some());
    assert!(metrics.sortino_ratio.is_some());
    assert!(metrics.annualized_return.is_some());
    assert!(metrics.annualized_volatility.is_some());
    assert!(metrics.alpha.is_some());
    assert!(metrics.beta.is_some());
    // A peak-to-trough decline occurred while holding, so drawdown is strictly > 0.
    assert!(metrics.max_drawdown.unwrap() > 0.0);
    // One closed trade, profitable (gross +250) -> win rate 1.0.
    assert_eq!(metrics.win_rate, Some(1.0));
    // No metric leaked a NaN/inf.
    for value in [
        metrics.sharpe_ratio,
        metrics.sortino_ratio,
        metrics.alpha,
        metrics.beta,
        metrics.max_drawdown,
        metrics.annualized_return,
        metrics.annualized_volatility,
        metrics.win_rate,
    ]
    .into_iter()
    .flatten()
    {
        assert!(value.is_finite());
    }
}

#[test]
fn srs_bt_004_metrics_are_deterministic() {
    let bars = vec![
        bar(1, 100),
        bar(2, 120),
        bar(3, 90),
        bar(4, 130),
        bar(5, 125),
    ];
    let result = run_backtest(bars, 10, 5);
    let benchmark = Benchmark::spy();
    let levels = aligned_benchmark(&result, 400, &[400, 420, 380, 430, 425]);
    let config = MetricsConfig::default();
    let first = compute(
        STARTING_CASH_MINOR,
        &result.equity_curve,
        &result.trade_log,
        &benchmark,
        Some(&levels),
        &config,
    )
    .unwrap();
    let second = compute(
        STARTING_CASH_MINOR,
        &result.equity_curve,
        &result.trade_log,
        &benchmark,
        Some(&levels),
        &config,
    )
    .unwrap();
    assert_eq!(first, second);
}

#[test]
fn srs_bt_004_benchmark_defaults_to_spy() {
    let result = run_backtest(vec![bar(1, 100), bar(2, 110), bar(3, 105)], 10, 3);
    // No benchmark series supplied: alpha/beta undefined, but the default benchmark
    // identity is still SPY.
    let metrics = compute(
        STARTING_CASH_MINOR,
        &result.equity_curve,
        &result.trade_log,
        &Benchmark::default(),
        None,
        &MetricsConfig::default(),
    )
    .unwrap();
    assert_eq!(metrics.benchmark_symbol, "SPY");
    assert_eq!(metrics.alpha, None);
    assert_eq!(metrics.beta, None);
}

#[test]
fn srs_bt_004_undefined_metrics_are_none() {
    // One bar, position opened but never closed: a single return (baseline -> mark), so
    // sample-dispersion metrics are undefined, and no closed round trip (so win rate is
    // undefined). Annualized return and drawdown are still defined from the baseline.
    let result = run_backtest(vec![bar(1, 100)], 10, 99);
    assert_eq!(result.equity_curve.len(), 1);
    let metrics = compute(
        STARTING_CASH_MINOR,
        &result.equity_curve,
        &result.trade_log,
        &Benchmark::spy(),
        None,
        &MetricsConfig::default(),
    )
    .unwrap();
    assert_eq!(metrics.sharpe_ratio, None);
    assert_eq!(metrics.sortino_ratio, None);
    assert_eq!(metrics.annualized_volatility, None);
    assert_eq!(metrics.win_rate, None);
    assert!(metrics.annualized_return.is_some());
    assert!(metrics.max_drawdown.is_some());
}

#[test]
fn srs_bt_004_fails_closed_on_misaligned_benchmark() {
    let result = run_backtest(
        vec![bar(1, 100), bar(2, 120), bar(3, 90), bar(4, 130)],
        10,
        4,
    );
    // A benchmark series of the wrong length cannot align period-for-period.
    let short = vec![
        BenchmarkPoint {
            ts: result.equity_curve[0].ts,
            level_minor: 400,
        },
        BenchmarkPoint {
            ts: result.equity_curve[1].ts,
            level_minor: 420,
        },
    ];
    let err = compute(
        STARTING_CASH_MINOR,
        &result.equity_curve,
        &result.trade_log,
        &Benchmark::spy(),
        Some(&short),
        &MetricsConfig::default(),
    )
    .unwrap_err();
    assert!(matches!(err, MetricsError::BenchmarkLengthMismatch { .. }));
}

#[test]
fn srs_bt_004_win_rate_matches_ledger_accounting() {
    // A losing round trip: buy 10 @ 120, sell 10 @ 90 (gross -300) -> win rate 0.0.
    let result = run_backtest(vec![bar(1, 120), bar(2, 100), bar(3, 90)], 10, 3);
    let metrics = compute(
        STARTING_CASH_MINOR,
        &result.equity_curve,
        &result.trade_log,
        &Benchmark::spy(),
        None,
        &MetricsConfig::default(),
    )
    .unwrap();
    assert_eq!(metrics.win_rate, Some(0.0));
}

fn manual_fill(ts: u64, symbol: &str, quantity: i64, price_minor: i64) -> Fill {
    Fill {
        ts,
        symbol: symbol.to_string(),
        quantity,
        price_minor,
        commission_minor: 0,
        slippage_minor: 0,
        spread_impact_minor: 0,
    }
}

#[test]
fn srs_bt_004_alpha_uses_risk_free_rate() {
    // Strategy returns are exactly 2x the benchmark returns (beta = 2). With a
    // non-zero risk-free rate and beta != 1, Jensen's alpha must use EXCESS returns:
    // alpha = (mean_r - rf) - beta*(mean_b - rf) = 0.01 here, whereas the
    // rf-omitting form would report 0.0. The metric path must carry the rf through.
    // Baseline 100 then marks 120, 108 -> strategy returns [0.20, -0.10]; benchmark
    // baseline 1000 then 1100, 1045 -> returns [0.10, -0.05] (exactly half).
    let curve = vec![
        EquityPoint {
            ts: 1,
            equity_minor: 120,
        },
        EquityPoint {
            ts: 2,
            equity_minor: 108,
        },
    ];
    let levels = vec![
        BenchmarkPoint {
            ts: 0,
            level_minor: 1000,
        },
        BenchmarkPoint {
            ts: 1,
            level_minor: 1100,
        },
        BenchmarkPoint {
            ts: 2,
            level_minor: 1045,
        },
    ];
    let config = MetricsConfig::new(1, 0.01).unwrap();
    let metrics = compute(100, &curve, &[], &Benchmark::spy(), Some(&levels), &config).unwrap();
    assert!((metrics.beta.unwrap() - 2.0).abs() < 1e-9);
    let alpha = metrics.alpha.unwrap();
    assert!(
        (alpha - 0.01).abs() < 1e-9,
        "alpha {alpha} must use excess returns over the risk-free rate"
    );
}

#[test]
fn srs_bt_004_win_rate_rejects_out_of_order_fills() {
    // A trade log whose timestamp goes backwards is not a valid time-ordered event
    // stream, so it must fail closed rather than silently produce an order-dependent win
    // rate. (The equity window spans [2, 5] so the coherence check passes and the
    // monotonic guard is what fires.)
    let curve = vec![
        EquityPoint {
            ts: 2,
            equity_minor: 1000,
        },
        EquityPoint {
            ts: 5,
            equity_minor: 1010,
        },
    ];
    let log = vec![
        manual_fill(5, "AAPL", 10, 100),
        manual_fill(2, "AAPL", -10, 120),
    ];
    let err = compute(
        1000,
        &curve,
        &log,
        &Benchmark::spy(),
        None,
        &MetricsConfig::default(),
    )
    .unwrap_err();
    assert!(matches!(err, MetricsError::NonMonotonicTradeLog { ts: 2 }));
}

fn costed_manual_fill(
    ts: u64,
    symbol: &str,
    quantity: i64,
    price_minor: i64,
    commission_minor: i64,
) -> Fill {
    Fill {
        ts,
        symbol: symbol.to_string(),
        quantity,
        price_minor,
        commission_minor,
        slippage_minor: 0,
        spread_impact_minor: 0,
    }
}

#[test]
fn srs_bt_004_win_rate_is_net_of_transaction_costs() {
    // Gross-positive but net-negative round trip: buy 10 @ 100 (commission 5), sell
    // 10 @ 101 (commission 10). Gross realized +10, net -5 -> a LOSS, win rate 0.0.
    // Counting the gross profit as a win would overstate the strategy.
    let curve = vec![
        EquityPoint {
            ts: 1,
            equity_minor: 1000,
        },
        EquityPoint {
            ts: 2,
            equity_minor: 1005,
        },
    ];
    let log = vec![
        costed_manual_fill(1, "AAPL", 10, 100, 5),
        costed_manual_fill(2, "AAPL", -10, 101, 10),
    ];
    let metrics = compute(
        1000,
        &curve,
        &log,
        &Benchmark::spy(),
        None,
        &MetricsConfig::default(),
    )
    .unwrap();
    assert_eq!(metrics.win_rate, Some(0.0));
}

#[test]
fn srs_bt_004_win_rate_canonicalizes_symbols() {
    // Open on `AAPL`, close on ` aapl ` (casing + whitespace alias). The virtual
    // ledger canonicalizes symbols, so these must close the SAME position here for
    // SYS-86 parity -> one closed (winning) trade, win rate 1.0.
    let curve = vec![
        EquityPoint {
            ts: 1,
            equity_minor: 1000,
        },
        EquityPoint {
            ts: 2,
            equity_minor: 1200,
        },
    ];
    let log = vec![
        manual_fill(1, "AAPL", 10, 100),
        manual_fill(2, " aapl ", -10, 120),
    ];
    let metrics = compute(
        1000,
        &curve,
        &log,
        &Benchmark::spy(),
        None,
        &MetricsConfig::default(),
    )
    .unwrap();
    assert_eq!(metrics.win_rate, Some(1.0));
}

#[test]
fn srs_bt_004_win_rate_is_invariant_to_fill_fragmentation() {
    // SYS-86 comparability: a round trip closed in one fill (a backtest) and the same
    // round trip closed in three volume-capped partial fills (paper/live, SRS-SIM-002)
    // with the same total cost are the SAME one winning trade -> identical win rate.
    let curve = vec![
        EquityPoint {
            ts: 1,
            equity_minor: 1000,
        },
        EquityPoint {
            ts: 4,
            equity_minor: 1200,
        },
    ];
    let aggregated = vec![
        costed_manual_fill(1, "AAPL", 10, 100, 6),
        costed_manual_fill(2, "AAPL", -10, 120, 6),
    ];
    let fragmented = vec![
        costed_manual_fill(1, "AAPL", 10, 100, 6),
        costed_manual_fill(2, "AAPL", -3, 120, 2),
        costed_manual_fill(3, "AAPL", -3, 120, 2),
        costed_manual_fill(4, "AAPL", -4, 120, 2),
    ];
    let agg = compute(
        1000,
        &curve,
        &aggregated,
        &Benchmark::spy(),
        None,
        &MetricsConfig::default(),
    )
    .unwrap();
    let frag = compute(
        1000,
        &curve,
        &fragmented,
        &Benchmark::spy(),
        None,
        &MetricsConfig::default(),
    )
    .unwrap();
    assert_eq!(agg.win_rate, Some(1.0));
    assert_eq!(agg.win_rate, frag.win_rate);
}

#[test]
fn srs_bt_004_win_rate_applies_same_timestamp_fills_in_order() {
    // Several orders / partial fills can fill one symbol against a single bar (same
    // timestamp). They are applied in trade-log (execution) order and form one complete
    // round trip -- a winning trade -- rather than being rejected as ambiguous.
    let curve = vec![
        EquityPoint {
            ts: 1,
            equity_minor: 1000,
        },
        EquityPoint {
            ts: 2,
            equity_minor: 1010,
        },
    ];
    let log = vec![
        manual_fill(1, "AAPL", 10, 100),
        manual_fill(1, "AAPL", -10, 120),
    ];
    let metrics = compute(
        1000,
        &curve,
        &log,
        &Benchmark::spy(),
        None,
        &MetricsConfig::default(),
    )
    .unwrap();
    assert_eq!(metrics.win_rate, Some(1.0));
}

#[test]
fn srs_bt_004_rejects_trade_log_outside_the_run_window() {
    // The trade log and equity curve must describe the same run: a fill after the last
    // equity mark (a stale or mismatched log) is rejected as incoherent.
    let curve = vec![
        EquityPoint {
            ts: 1,
            equity_minor: 1000,
        },
        EquityPoint {
            ts: 2,
            equity_minor: 1010,
        },
    ];
    let log = vec![
        manual_fill(1, "AAPL", 10, 100),
        manual_fill(9, "AAPL", -10, 120),
    ];
    let err = compute(
        1000,
        &curve,
        &log,
        &Benchmark::spy(),
        None,
        &MetricsConfig::default(),
    )
    .unwrap_err();
    assert!(matches!(
        err,
        MetricsError::TradeLogOutsideRun { ts: 9, .. }
    ));
}

#[test]
fn srs_bt_004_first_period_captured_from_the_baseline() {
    // The pre-trade baseline is a REQUIRED `compute` input, so the first period's P&L
    // (here a loss: starting equity 1000 -> first post-entry mark 990) is always
    // captured. Passing the true baseline (1000) yields a lower annualized return and a
    // larger drawdown than incorrectly starting from the first post-fill mark (990) --
    // proving the first-fill cost flows into the return-based metrics and the drawdown.
    let config = MetricsConfig::default();
    let curve = vec![
        EquityPoint {
            ts: 1,
            equity_minor: 990,
        },
        EquityPoint {
            ts: 2,
            equity_minor: 1010,
        },
    ];
    let from_baseline = compute(1000, &curve, &[], &Benchmark::spy(), None, &config).unwrap();
    let from_first_mark = compute(990, &curve, &[], &Benchmark::spy(), None, &config).unwrap();
    // The first-period loss lowers the annualized return.
    assert!(
        from_baseline.annualized_return.unwrap() < from_first_mark.annualized_return.unwrap(),
        "the baseline must capture the first-period loss"
    );
    // ...and the initial drawdown below the starting equity is only seen from the
    // baseline: (1000 - 990) / 1000 = 0.01, vs 0 when starting at the first mark.
    assert!((from_baseline.max_drawdown.unwrap() - 0.01).abs() < 1e-9);
    assert_eq!(from_first_mark.max_drawdown, Some(0.0));
}

#[test]
fn srs_bt_004_terminal_zero_equity_is_a_total_loss() {
    // Bankruptcy: a completed run whose FINAL equity is exactly zero is a defined -100%
    // total loss (and 100% drawdown), NOT an error -- one bad mark must not abort all
    // metrics. Baseline 1000, marks 500 then 0.
    let curve = vec![
        EquityPoint {
            ts: 1,
            equity_minor: 500,
        },
        EquityPoint {
            ts: 2,
            equity_minor: 0,
        },
    ];
    let metrics = compute(
        1000,
        &curve,
        &[],
        &Benchmark::spy(),
        None,
        &MetricsConfig::default(),
    )
    .expect("a terminal-zero run still computes");
    assert!((metrics.annualized_return.unwrap() - (-1.0)).abs() < 1e-9);
    assert!((metrics.max_drawdown.unwrap() - 1.0).abs() < 1e-9);
    assert!(metrics.sharpe_ratio.is_some());
    assert!(metrics.annualized_volatility.is_some());
}

#[test]
fn srs_bt_004_win_rate_flip_attributes_cost_to_close() {
    // A single-fill reversal (long 1@100, sell 3@101 cost 1) and the same reversal
    // written as a close-to-flat fill then a zero-cost open produce the IDENTICAL win
    // rate (0.0): the reversing fill's full cost is attributed to the closing round
    // trip, so the small close is a break-even (not a spurious win from a floored cost).
    let curve = vec![
        EquityPoint {
            ts: 1,
            equity_minor: 1000,
        },
        EquityPoint {
            ts: 4,
            equity_minor: 1000,
        },
    ];
    let flip = vec![
        costed_manual_fill(1, "AAPL", 1, 100, 0),
        costed_manual_fill(2, "AAPL", -3, 101, 1),
    ];
    let close_then_open = vec![
        costed_manual_fill(1, "AAPL", 1, 100, 0),
        costed_manual_fill(2, "AAPL", -1, 101, 1),
        costed_manual_fill(3, "AAPL", -2, 101, 0),
    ];
    let config = MetricsConfig::default();
    let flip_wr = compute(1000, &curve, &flip, &Benchmark::spy(), None, &config)
        .unwrap()
        .win_rate;
    let cto_wr = compute(
        1000,
        &curve,
        &close_then_open,
        &Benchmark::spy(),
        None,
        &config,
    )
    .unwrap()
    .win_rate;
    assert_eq!(flip_wr, Some(0.0));
    assert_eq!(flip_wr, cto_wr);
}
