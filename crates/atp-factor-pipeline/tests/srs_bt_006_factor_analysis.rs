//! SRS-BT-006 end-to-end factor-analysis integration test (Rust crate-level).
//!
//! Drives [`atp_factor_pipeline::factor_analysis::compute_tear_sheet`] over a multi-period
//! fixture panel the way a completed factor-analysis run would: build a
//! [`FactorPanel`] of per-period `(SecurityKey, factor value, forward return)`
//! observations and assert the three SRS-BT-006 deliverables come out coherent -- the
//! per-period Spearman information coefficient, the quantile factor-return spread, and the
//! quantile turnover -- plus the fail-closed trust boundary and determinism. Factor scores
//! and returns are dimensionless f64; an undefined statistic is `None`, never a fabricated
//! zero.

use atp_factor_pipeline::factor_analysis::{
    compute_tear_sheet, FactorAnalysisError, FactorObservation, FactorPanel, FactorPeriod,
};
use atp_types::{AssetClass, SecurityKey};

fn key(symbol: &str) -> SecurityKey {
    SecurityKey::new(symbol, AssetClass::Equity).expect("equity key")
}

fn observation(symbol: &str, factor: f64, forward_return: f64) -> FactorObservation {
    FactorObservation::new(key(symbol), factor, forward_return)
}

/// A six-security cross-section whose factor perfectly ranks the forward returns.
fn aligned_period(ts: u64) -> FactorPeriod {
    FactorPeriod::new(
        ts,
        vec![
            observation("AAA", 1.0, 0.01),
            observation("BBB", 2.0, 0.02),
            observation("CCC", 3.0, 0.03),
            observation("DDD", 4.0, 0.04),
            observation("EEE", 5.0, 0.05),
            observation("FFF", 6.0, 0.06),
        ],
    )
}

#[test]
fn end_to_end_tear_sheet_is_coherent() {
    // Three periods, three quantiles. Each period's factor perfectly ranks returns, and the
    // factor ordering is identical every period, so: IC == 1.0 every period, the top-minus-
    // bottom spread is positive and stable, and turnover is zero (membership never changes).
    let panel = FactorPanel::new(
        vec![aligned_period(1), aligned_period(2), aligned_period(3)],
        3,
    );
    let sheet = compute_tear_sheet(&panel).expect("tear sheet");

    assert_eq!(sheet.n_periods, 3);
    assert_eq!(sheet.n_quantiles, 3);

    // (1) Information coefficient: defined and == 1.0 for every period; mean == 1.0.
    assert_eq!(sheet.ic.per_period.len(), 3);
    for (_, ic) in &sheet.ic.per_period {
        let value = ic.expect("defined ic");
        assert!((value - 1.0).abs() < 1e-9, "ic = {value}");
    }
    let mean_ic = sheet.ic.mean.expect("mean ic");
    assert!((mean_ic - 1.0).abs() < 1e-9);
    // Perfectly stable IC -> zero dispersion -> risk-adjusted IC is undefined (None), never
    // a fabricated value.
    assert_eq!(sheet.ic.risk_adjusted, None);

    // (2) Factor returns: 6 securities / 3 quantiles -> buckets of 2; bottom {AAA,BBB} mean
    // 0.015, top {EEE,FFF} mean 0.055 -> spread 0.04, stable across periods.
    assert_eq!(sheet.returns.per_quantile_mean.len(), 3);
    for quantile_means in &sheet.returns.per_quantile_mean {
        assert_eq!(quantile_means.len(), 3);
        // The factor strictly ranks, so every bucket is clean and its mean is defined.
        assert!(quantile_means.iter().all(Option::is_some));
    }
    for (_, spread) in &sheet.returns.spread_per_period {
        // The factor strictly ranks every period, so each spread is defined.
        let spread = spread.expect("defined spread");
        assert!((spread - 0.04).abs() < 1e-9, "spread = {spread}");
    }
    let mean_spread = sheet.returns.mean_spread.expect("mean spread");
    assert!((mean_spread - 0.04).abs() < 1e-9);

    // (3) Turnover: stable membership -> zero churn for the two later periods.
    assert_eq!(sheet.turnover.top_turnover.len(), 2);
    assert_eq!(sheet.turnover.bottom_turnover.len(), 2);
    for (_, turnover) in &sheet.turnover.top_turnover {
        assert!(turnover.expect("defined turnover").abs() < 1e-9);
    }
    assert!(sheet.turnover.mean_top.expect("mean top").abs() < 1e-9);
}

#[test]
fn turnover_tracks_membership_churn_across_periods() {
    // Period 2 inverts the factor ordering, so the top and bottom quantiles fully swap.
    let inverted = FactorPeriod::new(
        2,
        vec![
            observation("AAA", 6.0, 0.01),
            observation("BBB", 5.0, 0.02),
            observation("CCC", 4.0, 0.03),
            observation("DDD", 3.0, 0.04),
            observation("EEE", 2.0, 0.05),
            observation("FFF", 1.0, 0.06),
        ],
    );
    let panel = FactorPanel::new(vec![aligned_period(1), inverted], 3);
    let sheet = compute_tear_sheet(&panel).expect("tear sheet");

    // Top was {EEE,FFF}, now {AAA,BBB}: complete churn. Both periods strictly rank the
    // factor, so the turnover is defined.
    assert!((sheet.turnover.top_turnover[0].1.expect("top") - 1.0).abs() < 1e-9);
    assert!((sheet.turnover.bottom_turnover[0].1.expect("bottom") - 1.0).abs() < 1e-9);
}

#[test]
fn computation_is_deterministic_across_runs() {
    let panel = FactorPanel::new(vec![aligned_period(1), aligned_period(2)], 3);
    let first = compute_tear_sheet(&panel).expect("first");
    let second = compute_tear_sheet(&panel).expect("second");
    // Bit-identical (PartialEq over the whole tear sheet) on identical inputs (SRS-BT-010).
    assert_eq!(first, second);
}

#[test]
fn degenerate_panel_fails_closed() {
    // A period with fewer securities than quantiles cannot fill every bucket.
    let panel = FactorPanel::new(
        vec![FactorPeriod::new(
            1,
            vec![observation("AAA", 1.0, 0.1), observation("BBB", 2.0, 0.2)],
        )],
        3,
    );
    assert_eq!(
        compute_tear_sheet(&panel).unwrap_err(),
        FactorAnalysisError::InsufficientSecurities {
            ts: 1,
            securities: 2,
            quantiles: 3,
        }
    );
}

#[test]
fn non_finite_input_fails_closed() {
    let panel = FactorPanel::new(
        vec![FactorPeriod::new(
            7,
            vec![
                observation("AAA", 1.0, f64::INFINITY),
                observation("BBB", 2.0, 0.2),
                observation("CCC", 3.0, 0.3),
            ],
        )],
        2,
    );
    assert_eq!(
        compute_tear_sheet(&panel).unwrap_err(),
        FactorAnalysisError::NonFiniteInput { ts: 7 }
    );
}

#[test]
fn constant_factor_withholds_spread_and_turnover() {
    // A factor that gives every security the same score carries no ranking signal, so the
    // top/bottom quantile split is decided purely by SecurityKey. The spread and turnover are
    // therefore withheld (None) -- never a fabricated number presented as factor performance.
    let flat = |ts: u64| {
        FactorPeriod::new(
            ts,
            vec![
                observation("AAA", 1.0, 0.01),
                observation("BBB", 1.0, 0.02),
                observation("CCC", 1.0, 0.03),
                observation("DDD", 1.0, 0.04),
            ],
        )
    };
    let sheet =
        compute_tear_sheet(&FactorPanel::new(vec![flat(1), flat(2)], 2)).expect("tear sheet");

    // IC undefined (no rank signal), and crucially the spread is withheld, not fabricated.
    assert_eq!(sheet.ic.per_period[0].1, None);
    assert_eq!(sheet.returns.spread_per_period[0].1, None);
    assert_eq!(sheet.returns.spread_per_period[1].1, None);
    assert_eq!(sheet.returns.mean_spread, None);
    // EVERY quantile bucket mean is withheld too -- a constant factor must not expose an
    // identity-driven quantile-return ladder.
    for quantile_means in &sheet.returns.per_quantile_mean {
        assert!(quantile_means.iter().all(Option::is_none));
    }
    // Turnover is likewise withheld for the non-factor-driven membership.
    assert_eq!(sheet.turnover.top_turnover[0].1, None);
    assert_eq!(sheet.turnover.mean_top, None);
    assert_eq!(sheet.turnover.mean_bottom, None);
}

#[test]
fn extreme_returns_in_a_quantile_fail_closed() {
    // Two near-f64::MAX returns in the median (non-edge) quantile overflow its mean to +inf.
    // The spread uses only the top and bottom buckets, so the infinity would otherwise slip
    // into a "successful" tear sheet; the per-quantile finiteness guard fails it closed.
    let period = FactorPeriod::new(
        1,
        vec![
            observation("AAA", 1.0, 0.01),
            observation("BBB", 2.0, 0.02),
            observation("CCC", 3.0, f64::MAX),
            observation("DDD", 4.0, f64::MAX),
            observation("EEE", 5.0, 0.05),
            observation("FFF", 6.0, 0.06),
        ],
    );
    assert_eq!(
        compute_tear_sheet(&FactorPanel::new(vec![period], 3)).unwrap_err(),
        FactorAnalysisError::NonFiniteComputation {
            metric: "quantile_mean"
        }
    );
}

#[test]
fn turnover_counts_removals_when_the_universe_shrinks() {
    // P2's top quantile is a strict subset of P1's (the universe shrank), so a one-sided
    // "fraction of new names" measure would hide the removals as 0 churn. The symmetric
    // measure reports the dropped names as turnover, so transaction-cost drag is not
    // understated.
    let p1 = FactorPeriod::new(
        1,
        vec![
            observation("AAA", 1.0, 0.01),
            observation("BBB", 2.0, 0.02),
            observation("CCC", 3.0, 0.03),
            observation("DDD", 4.0, 0.04),
            observation("EEE", 5.0, 0.05),
            observation("FFF", 6.0, 0.06),
            observation("GGG", 7.0, 0.07),
            observation("HHH", 8.0, 0.08),
        ],
    );
    let p2 = FactorPeriod::new(
        2,
        vec![
            observation("EEE", 1.0, 0.05),
            observation("FFF", 2.0, 0.06),
            observation("GGG", 3.0, 0.07),
            observation("HHH", 4.0, 0.08),
        ],
    );
    let sheet = compute_tear_sheet(&FactorPanel::new(vec![p1, p2], 2)).expect("tear sheet");
    let top = sheet.turnover.top_turnover[0].1.expect("top turnover");
    assert!(
        top > 0.0,
        "a pure-removal rebalance must not report zero churn"
    );
    // Equal-weight book traded 50% (two of four names dropped, the rest doubled in weight) --
    // a set-membership ratio would understate this as 1/3.
    assert!((top - 0.5).abs() < 1e-9, "top turnover = {top}");
}

#[test]
fn inner_cutoff_tie_withholds_spread_with_three_quantiles() {
    // 6 securities, 3 quantiles: the factor value 2.0 straddles the q0|q1 cutoff, so the bottom
    // bucket is composed by SecurityKey even though the extremes look separated. The spread must
    // be withheld -- the extremes-only check missed this for 3+ quantiles.
    let period = FactorPeriod::new(
        1,
        vec![
            observation("AAA", 1.0, 0.1),
            observation("BBB", 2.0, 0.2),
            observation("CCC", 2.0, 0.3),
            observation("DDD", 3.0, 0.4),
            observation("EEE", 4.0, 0.5),
            observation("FFF", 5.0, 0.6),
        ],
    );
    let sheet = compute_tear_sheet(&FactorPanel::new(vec![period], 3)).expect("tear sheet");
    assert_eq!(sheet.returns.spread_per_period[0].1, None);
    assert_eq!(sheet.returns.mean_spread, None);
}

#[test]
fn undefined_period_withholds_its_spread_but_mean_spans_the_defined_ones() {
    // Periods 1 and 3 rank the factor; period 2 is a constant factor (undefined spread). The
    // undefined period's spread is withheld (None), while mean_spread -- an average over the
    // DEFINED periods (a horizon-agnostic statistic, not a compounded path) -- remains.
    let ranked = |ts: u64| {
        FactorPeriod::new(
            ts,
            vec![
                observation("AAA", 1.0, 0.1),
                observation("BBB", 2.0, 0.2),
                observation("CCC", 3.0, 0.3),
                observation("DDD", 4.0, 0.4),
            ],
        )
    };
    let flat = FactorPeriod::new(
        2,
        vec![
            observation("AAA", 5.0, 0.1),
            observation("BBB", 5.0, 0.2),
            observation("CCC", 5.0, 0.3),
            observation("DDD", 5.0, 0.4),
        ],
    );
    let sheet = compute_tear_sheet(&FactorPanel::new(vec![ranked(1), flat, ranked(3)], 2))
        .expect("tear sheet");
    assert!(sheet.returns.spread_per_period[0].1.is_some());
    assert_eq!(sheet.returns.spread_per_period[1].1, None);
    assert!(sheet.returns.mean_spread.is_some());
}

/// A tiny deterministic LCG so the property sweep below is reproducible without pulling in
/// an external `rand` dependency (which would itself be a nondeterminism smell in this
/// crate). Numerical Recipes constants.
struct Lcg(u64);

impl Lcg {
    fn next_u64(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        self.0
    }

    /// A value in `[0, 1)` from the top 53 bits (the f64 mantissa width).
    fn next_unit(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 / (1u64 << 53) as f64
    }
}

/// Generate a valid panel: `periods` strictly-increasing cross-sections, each holding
/// `securities` distinct names (>= `quantiles`) with finite random factor scores and
/// returns.
fn generated_panel(seed: u64, periods: usize, securities: usize, quantiles: usize) -> FactorPanel {
    let mut lcg = Lcg(seed);
    let period_vec = (0..periods)
        .map(|p| {
            let observations = (0..securities)
                .map(|s| {
                    let factor = lcg.next_unit() * 10.0 - 5.0;
                    let forward_return = lcg.next_unit() * 0.2 - 0.1;
                    observation(&format!("S{s:03}"), factor, forward_return)
                })
                .collect();
            FactorPeriod::new((p as u64) + 1, observations)
        })
        .collect();
    FactorPanel::new(period_vec, quantiles)
}

#[test]
fn invariants_hold_over_generated_panels() {
    // Property sweep (L2-style, in-crate so it can exercise the Rust surface directly):
    // over many generated valid panels, the per-period IC stays in its [-1, 1] domain, every
    // quantile turnover stays in [0, 1], the spread is exactly the top-minus-bottom quantile
    // difference, the result is deterministic, and it is invariant to observation order.
    for seed in 0..64u64 {
        let periods = 2 + (seed as usize % 4); // 2..=5
        let securities = 6 + (seed as usize % 7); // 6..=12
        let quantiles = 2 + (seed as usize % 3); // 2..=4
        let panel = generated_panel(
            seed.wrapping_mul(2_654_435_761),
            periods,
            securities,
            quantiles,
        );
        let sheet = compute_tear_sheet(&panel).expect("valid panel");

        for (_, ic) in &sheet.ic.per_period {
            if let Some(value) = ic {
                assert!(
                    (-1.0..=1.0).contains(value),
                    "ic {value} outside [-1, 1] (seed {seed})"
                );
            }
        }
        for (_, turnover) in sheet
            .turnover
            .top_turnover
            .iter()
            .chain(sheet.turnover.bottom_turnover.iter())
        {
            // Defined turnovers (factor-driven periods) must sit in [0, 1].
            if let Some(value) = turnover {
                assert!(
                    (0.0..=1.0).contains(value),
                    "turnover {value} outside [0, 1] (seed {seed})"
                );
            }
        }
        for (i, (_, spread)) in sheet.returns.spread_per_period.iter().enumerate() {
            // A defined spread must be exactly the top-minus-bottom quantile difference. When the
            // spread is defined the extreme buckets are clean, so their means are defined too.
            if let Some(value) = spread {
                let means = &sheet.returns.per_quantile_mean[i];
                let top = means[means.len() - 1].expect("clean top bucket");
                let bottom = means[0].expect("clean bottom bucket");
                let expected = top - bottom;
                assert!(
                    (value - expected).abs() < 1e-12,
                    "spread != top - bottom (seed {seed})"
                );
            }
        }

        // Determinism: a second compute is bit-identical.
        assert_eq!(compute_tear_sheet(&panel).expect("again"), sheet);

        // Observation-order invariance: reversing each period's cross-section changes nothing.
        let reversed = FactorPanel::new(
            panel
                .periods
                .iter()
                .map(|period| {
                    let mut observations = period.observations.clone();
                    observations.reverse();
                    FactorPeriod::new(period.ts, observations)
                })
                .collect(),
            panel.quantiles,
        );
        assert_eq!(compute_tear_sheet(&reversed).expect("reversed"), sheet);
    }
}
