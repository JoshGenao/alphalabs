//! SRS-BT-010 deterministic-backtest verification integration test (Rust crate-level).
//!
//! Drives the [`determinism`](atp_simulation::determinism) surface through the public crate
//! API the way a reproducibility check would: run the engine over fixture inputs, fingerprint
//! the result, and prove that identical inputs yield a bit-identical
//! [`RunDigest`](atp_simulation::determinism::RunDigest) — across repeated runs, across
//! source-iteration-order shuffles, and spanning the metric family — while a nondeterministic
//! strategy is caught and localized. Includes an L2-style property sweep over many
//! fixed-seed-but-varied inputs. Money assertions are exact integer minor units.

use atp_simulation::backtest::{
    BacktestBar, BacktestDataSource, BacktestEngine, BacktestError, BacktestRequest,
    BacktestStrategy, BarSource, DateRange,
};
use atp_simulation::cost::CostConfig;
use atp_simulation::determinism::{
    digest_result, digest_run, metrics_match, runs_match, verify_reproducible,
    verify_reproducible_with_metrics, DeterminismError,
};
use atp_simulation::metrics::{compute, Benchmark, MetricsConfig};
use atp_types::StrategyId;

// --------------------------------------------------------------------------- //
// Fixtures
// --------------------------------------------------------------------------- //

/// A fixture catalog that returns its bars in the order they are stored — so two catalogs
/// holding the SAME bars in DIFFERENT order can prove the engine's output is order-invariant.
struct OrderedCatalog {
    bars: Vec<BacktestBar>,
}

impl BarSource for OrderedCatalog {
    fn source(&self) -> BacktestDataSource {
        BacktestDataSource::SystemData
    }

    fn bars(
        &self,
        symbol: &str,
        range: &DateRange,
        _max_bars: usize,
    ) -> Result<Vec<BacktestBar>, BacktestError> {
        Ok(self
            .bars
            .iter()
            .filter(|bar| bar.symbol == symbol && range.contains(bar.ts))
            .cloned()
            .collect())
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

fn request(range: DateRange) -> BacktestRequest {
    BacktestRequest {
        strategy_id: StrategyId::new("det-it"),
        symbol: "AAPL".to_string(),
        data_source: BacktestDataSource::SystemData,
        range,
        starting_cash_minor: 1_000_000,
        cost_config: CostConfig::zero(),
    }
}

/// Buys `lot` on the first bar, then holds. Deterministic.
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

/// A deterministic oscillator: buys 1 on even-ts bars, sells 1 on odd-ts bars. Produces a
/// non-trivial trade log whose determinism is purely a function of the inputs.
struct Oscillator;

impl BacktestStrategy for Oscillator {
    fn on_bar(&mut self, bar: &BacktestBar, _position: i64) -> Result<i64, BacktestError> {
        Ok(if bar.ts % 2 == 0 { 1 } else { -1 })
    }
}

/// A tiny deterministic LCG, so the property sweep generates fixed-but-varied inputs without
/// any external crate (the workspace has zero third-party dependencies) and without a clock.
struct Lcg {
    state: u64,
}

impl Lcg {
    fn new(seed: u64) -> Self {
        Self {
            state: seed ^ 0x9e37_79b9_7f4a_7c15,
        }
    }

    fn next_u64(&mut self) -> u64 {
        // Numerical Recipes LCG constants.
        self.state = self
            .state
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.state
    }

    /// A close price in `[50, 549]` minor units (always strictly positive).
    fn next_price(&mut self) -> i64 {
        50 + (self.next_u64() % 500) as i64
    }
}

/// Build `n` bars at ts `1..=n` with LCG-derived strictly-positive prices.
fn random_bars(seed: u64, n: u64) -> Vec<BacktestBar> {
    let mut rng = Lcg::new(seed);
    (1..=n).map(|ts| bar(ts, rng.next_price())).collect()
}

/// Fisher–Yates shuffle driven by the LCG — to permute a catalog's iteration order.
fn shuffled(mut bars: Vec<BacktestBar>, seed: u64) -> Vec<BacktestBar> {
    let mut rng = Lcg::new(seed ^ 0xdead_beef);
    for i in (1..bars.len()).rev() {
        let j = (rng.next_u64() % (i as u64 + 1)) as usize;
        bars.swap(i, j);
    }
    bars
}

// --------------------------------------------------------------------------- //
// Tests
// --------------------------------------------------------------------------- //

#[test]
fn identical_runs_produce_identical_results_and_digest() {
    let run = || {
        let source = OrderedCatalog {
            bars: vec![bar(1, 100), bar(2, 110), bar(3, 120), bar(4, 130)],
        };
        let mut strategy = BuyOnceAndHold {
            lot: 10,
            bought: false,
        };
        BacktestEngine::new()
            .run(&request(DateRange::new(1, 4)), &mut strategy, &source)
            .expect("backtest runs")
    };
    let a = run();
    let b = run();
    assert_eq!(a, b);
    assert_eq!(runs_match(&a, &b), Ok(()));
    assert_eq!(digest_result(&a), digest_result(&b));
}

#[test]
fn verify_reproducible_returns_the_one_off_run_digest() {
    let source = OrderedCatalog {
        bars: vec![bar(1, 100), bar(2, 90), bar(3, 130)],
    };
    let engine = BacktestEngine::new();
    let digest = verify_reproducible(
        &engine,
        &request(DateRange::new(1, 3)),
        || Oscillator,
        &source,
    )
    .expect("deterministic oscillator reproduces");

    let mut once = Oscillator;
    let result = engine
        .run(&request(DateRange::new(1, 3)), &mut once, &source)
        .expect("run");
    assert_eq!(digest, digest_result(&result));
    // The oscillator actually traded on every bar — a non-trivial log.
    assert_eq!(result.trade_log.len(), 3);
}

#[test]
fn verify_reproducible_catches_a_nondeterministic_strategy() {
    use std::cell::Cell;
    use std::rc::Rc;

    /// First-bar lot read from a counter shared across runs → the second replay diverges.
    struct SharedCounter {
        counter: Rc<Cell<i64>>,
        decided: bool,
    }
    impl BacktestStrategy for SharedCounter {
        fn on_bar(&mut self, _bar: &BacktestBar, _position: i64) -> Result<i64, BacktestError> {
            if self.decided {
                return Ok(0);
            }
            self.decided = true;
            let lot = self.counter.get();
            self.counter.set(lot + 1);
            Ok(lot)
        }
    }

    let source = OrderedCatalog {
        bars: vec![bar(1, 100), bar(2, 110)],
    };
    let counter = Rc::new(Cell::new(1));
    let err = verify_reproducible(
        &BacktestEngine::new(),
        &request(DateRange::new(1, 2)),
        || SharedCounter {
            counter: Rc::clone(&counter),
            decided: false,
        },
        &source,
    )
    .expect_err("a cross-run-stateful strategy must be caught and localized");
    assert_eq!(err, DeterminismError::TradeLog { index: 0 });
}

#[test]
fn digest_is_invariant_to_source_iteration_order() {
    // The engine stable-sorts the replay window by ts, so a catalog that hands the engine its
    // bars in a different order must still produce the same result — the determinism criterion's
    // "floating-point ordering / parallelism do not introduce nondeterminism" analog.
    let bars = random_bars(7, 32);
    let ordered = OrderedCatalog { bars: bars.clone() };
    let permuted = OrderedCatalog {
        bars: shuffled(bars, 7),
    };
    let engine = BacktestEngine::new();
    let run = |source: &OrderedCatalog| {
        let mut strategy = Oscillator;
        engine
            .run(&request(DateRange::new(1, 32)), &mut strategy, source)
            .expect("run")
    };
    let from_ordered = run(&ordered);
    let from_permuted = run(&permuted);
    assert_eq!(runs_match(&from_ordered, &from_permuted), Ok(()));
    assert_eq!(digest_result(&from_ordered), digest_result(&from_permuted));
}

#[test]
fn digest_run_spans_the_metric_family() {
    // A clean rising-price buy-and-hold gives a strictly-positive, strictly-increasing equity
    // curve the metric family can be computed over.
    let source = OrderedCatalog {
        bars: vec![bar(1, 100), bar(2, 120), bar(3, 150), bar(4, 200)],
    };
    let req = request(DateRange::new(1, 4));
    let engine = BacktestEngine::new();

    let metrics_of = |req: &BacktestRequest, source: &OrderedCatalog| {
        let mut strategy = BuyOnceAndHold {
            lot: 100,
            bought: false,
        };
        let result = engine.run(req, &mut strategy, source).expect("run");
        let metrics = compute(
            req.starting_cash_minor,
            &result.equity_curve,
            &result.trade_log,
            &Benchmark::spy(),
            None,
            &MetricsConfig::default(),
        )
        .expect("metrics");
        (result, metrics)
    };

    let (result_a, metrics_a) = metrics_of(&req, &source);
    let (result_b, metrics_b) = metrics_of(&req, &source);

    // The metric family is deterministic, and digest_run bundles all three named artifacts.
    assert_eq!(metrics_match(&metrics_a, &metrics_b), Ok(()));
    assert_eq!(
        digest_run(&result_a, Some(&metrics_a)),
        digest_run(&result_b, Some(&metrics_b))
    );
    // Bundling metrics changes the fingerprint (domain separation), so the metrics clause is
    // genuinely covered and cannot be silently dropped.
    assert_ne!(
        digest_run(&result_a, Some(&metrics_a)),
        digest_result(&result_a)
    );
}

#[test]
fn metrics_harness_verifies_all_three_artifacts() {
    // verify_reproducible_with_metrics runs the engine twice AND computes + compares the metric
    // family for both runs, returning a digest that spans all three SRS-BT-010 artifacts.
    let source = OrderedCatalog {
        bars: vec![bar(1, 100), bar(2, 120), bar(3, 150), bar(4, 200)],
    };
    let req = request(DateRange::new(1, 4));
    let engine = BacktestEngine::new();
    let digest = verify_reproducible_with_metrics(
        &engine,
        &req,
        || BuyOnceAndHold {
            lot: 100,
            bought: false,
        },
        &source,
        &Benchmark::spy(),
        None,
        &MetricsConfig::default(),
    )
    .expect("deterministic run + metric family reproduce");

    let mut once = BuyOnceAndHold {
        lot: 100,
        bought: false,
    };
    let result = engine.run(&req, &mut once, &source).expect("run");
    let metrics = compute(
        req.starting_cash_minor,
        &result.equity_curve,
        &result.trade_log,
        &Benchmark::spy(),
        None,
        &MetricsConfig::default(),
    )
    .expect("metrics");
    assert_eq!(digest, digest_run(&result, Some(&metrics)));
}

#[test]
fn runs_match_rejects_provenance_divergence() {
    // runs_match compares the result PROVENANCE, not just trades + equity: two results from
    // different catalogs or date ranges are not the same run even when their content coincides.
    let source = OrderedCatalog {
        bars: vec![bar(1, 100), bar(2, 110), bar(3, 120)],
    };
    let engine = BacktestEngine::new();
    let mut strat = BuyOnceAndHold {
        lot: 5,
        bought: false,
    };
    let base = engine
        .run(&request(DateRange::new(1, 3)), &mut strat, &source)
        .expect("run");

    let mut other = base.clone();
    other.data_source = BacktestDataSource::UploadedData;
    assert_eq!(runs_match(&base, &other), Err(DeterminismError::DataSource));

    let mut ranged = base.clone();
    ranged.range = DateRange::new(1, 99);
    assert_eq!(runs_match(&base, &ranged), Err(DeterminismError::Range));
}

#[test]
fn property_sweep_reproducibility_and_order_invariance() {
    let engine = BacktestEngine::new();
    for seed in 0..64u64 {
        let n = 8 + seed % 25; // 8..=32 bars
        let bars = random_bars(seed, n);

        // (1) Repeated runs over identical inputs reproduce bit-for-bit.
        let ordered = OrderedCatalog { bars: bars.clone() };
        let digest = verify_reproducible(
            &engine,
            &request(DateRange::new(1, n)),
            || Oscillator,
            &ordered,
        )
        .unwrap_or_else(|err| panic!("seed {seed}: deterministic run must reproduce, got {err}"));

        // (2) Shuffling the source's iteration order does not change the digest.
        let permuted = OrderedCatalog {
            bars: shuffled(bars, seed),
        };
        let mut strategy = Oscillator;
        let from_permuted = engine
            .run(&request(DateRange::new(1, n)), &mut strategy, &permuted)
            .unwrap_or_else(|err| panic!("seed {seed}: permuted run failed: {err}"));
        assert_eq!(
            digest,
            digest_result(&from_permuted),
            "seed {seed}: digest must be invariant to source iteration order"
        );
    }
}
