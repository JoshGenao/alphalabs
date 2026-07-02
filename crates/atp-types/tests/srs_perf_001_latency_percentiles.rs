//! SRS-PERF-001 acceptance exercise — the latency-percentile verification
//! substrate (SyRS §5.1 NFR-P1 / NFR-P4 / NFR-P5 / NFR-P6 / NFR-P9 / NFR-P10 +
//! SRS-MD-001 fan-out latency; StRS SN-1.01 / SN-2.03).
//!
//! Exercises the public `atp_types::perf` API end to end the way an NFR
//! verification would: builds a verification artifact from latency samples
//! against a PTP-disciplined clock, asserts it reports p50/p95/p99/p99.9 and a
//! documented offset bound, and proves the nearest-rank percentile invariants
//! over generated distributions (seeded LCG — deterministic, no dev-dependency,
//! since `atp-types` is dependency-free). Also pins the catalog to the seven AC
//! NFRs and the fail-closed construction paths.

use atp_types::perf::{
    nearest_rank_percentile_ns, LatencyNfr, LatencyPercentiles, LatencyVerificationArtifact,
    NfrVerification, Percentile, PerfMeasurementError, PtpClockDiscipline, ThresholdComparison,
    LATENCY_NFRS, REPORTED_PERCENTILES,
};

/// Tiny deterministic linear-congruential generator (Numerical Recipes
/// constants) so the property sweep is reproducible without pulling in a
/// proptest/quickcheck dependency (`atp-types` has none).
struct Lcg(u64);

impl Lcg {
    fn next_u64(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        self.0
    }

    /// A latency sample in `[0, bound)` nanoseconds.
    fn sample(&mut self, bound: u64) -> u64 {
        self.next_u64() % bound
    }
}

/// Independent characterization of the nearest-rank percentile: the returned
/// value is an observed sample, at least `rank` samples are `<= v`, and strictly
/// fewer than `rank` samples are `< v` (i.e. `v` is the rank-th order statistic).
fn assert_nearest_rank_characterization(samples: &[u64], p: Percentile, v: u64) {
    let n = samples.len() as u128;
    let rank = ((p.per_mille() as u128) * n).div_ceil(1_000).clamp(1, n) as usize;
    assert!(
        samples.contains(&v),
        "{} percentile {v} must be an observed sample",
        p.as_str()
    );
    let le = samples.iter().filter(|&&x| x <= v).count();
    let lt = samples.iter().filter(|&&x| x < v).count();
    assert!(
        le >= rank,
        "{}: count(<= {v})={le} must be >= rank {rank}",
        p.as_str()
    );
    assert!(
        lt < rank,
        "{}: count(< {v})={lt} must be < rank {rank}",
        p.as_str()
    );
}

#[test]
fn nearest_rank_invariants_hold_over_generated_distributions() {
    let mut rng = Lcg(0x5150_5245_5246_3031); // "PERF01"-ish seed
    for case in 0..500u64 {
        // Sizes spanning single-sample up to well past the p99.9 resolution floor.
        let n = 1 + (rng.sample(1_500)) as usize;
        let bound = 1 + rng.sample(5_000_000); // up to 5 ms in ns
        let samples: Vec<u64> = (0..n).map(|_| rng.sample(bound)).collect();

        let pct = LatencyPercentiles::from_samples(&samples)
            .unwrap_or_else(|e| panic!("case {case}: non-empty samples must build: {e}"));

        // Monotonic non-decreasing across the reported percentiles.
        assert!(pct.p50_ns() <= pct.p95_ns(), "case {case}: p50<=p95");
        assert!(pct.p95_ns() <= pct.p99_ns(), "case {case}: p95<=p99");
        assert!(pct.p99_ns() <= pct.p999_ns(), "case {case}: p99<=p99.9");

        let min = *samples.iter().min().unwrap();
        let max = *samples.iter().max().unwrap();
        for p in REPORTED_PERCENTILES {
            let v = pct.get_ns(p);
            assert!(
                v >= min && v <= max,
                "case {case}: {} in [{min},{max}]",
                p.as_str()
            );
            // The free function and the bundle agree.
            assert_eq!(nearest_rank_percentile_ns(&samples, p), Some(v));
            assert_nearest_rank_characterization(&samples, p, v);
        }

        // p99.9 resolution floor is exactly 1000 samples.
        assert_eq!(
            pct.resolves_p999(),
            n >= 1_000,
            "case {case}: p99.9 resolution floor"
        );
    }
}

#[test]
fn verification_artifact_reports_percentiles_offset_and_window() {
    // 1..=1000 ms expressed in ns — enough samples to resolve the p99.9 tail.
    let samples: Vec<u64> = (1..=1_000).map(|ms| ms * 1_000_000).collect();
    let artifact = LatencyVerificationArtifact::from_samples(
        LatencyNfr::OrderEventCallback,
        "live",
        PtpClockDiscipline::Disciplined { max_offset_ns: 750 },
        1_000,
        1_000 + 300_000_000_000, // a 300 s window
        &samples,
    )
    .expect("disciplined clock + samples + real window builds an artifact");

    assert_eq!(artifact.nfr_id(), "NFR-P4");
    assert_eq!(artifact.threshold_label(), "live");
    assert_eq!(artifact.max_clock_offset_ns(), 750);
    assert_eq!(artifact.window_duration_ns(), 300_000_000_000);
    let pct = artifact.percentiles();
    assert!(pct.resolves_p999());
    // ceil(0.95*1000)=950 → sorted[949] = 950 ms.
    assert_eq!(pct.get_ns(Percentile::P95), 950 * 1_000_000);
    assert_eq!(pct.get_ns(Percentile::P999), 999 * 1_000_000);

    let rendered = artifact.to_string();
    for needle in [
        "SRS-PERF-001",
        "NFR-P4",
        "p50",
        "p95",
        "p99.9",
        "max clock offset",
        "measurement window",
    ] {
        assert!(
            rendered.contains(needle),
            "artifact rendering must contain {needle:?}"
        );
    }
}

#[test]
fn artifact_construction_fails_closed() {
    let good_samples = [10_u64, 20, 30];
    // Undisciplined clock — no documented offset bound.
    assert_eq!(
        LatencyVerificationArtifact::from_samples(
            LatencyNfr::OrderSignalToAck,
            "",
            PtpClockDiscipline::Undisciplined,
            0,
            1_000,
            &good_samples,
        ),
        Err(PerfMeasurementError::ClockNotDisciplined)
    );
    // Inverted window.
    assert_eq!(
        LatencyVerificationArtifact::from_samples(
            LatencyNfr::OrderSignalToAck,
            "",
            PtpClockDiscipline::Disciplined { max_offset_ns: 1 },
            5,
            5,
            &good_samples,
        ),
        Err(PerfMeasurementError::EmptyMeasurementWindow {
            start_ns: 5,
            end_ns: 5
        })
    );
    // No samples.
    assert_eq!(
        LatencyVerificationArtifact::from_samples(
            LatencyNfr::OrderSignalToAck,
            "",
            PtpClockDiscipline::Disciplined { max_offset_ns: 1 },
            0,
            1_000,
            &[],
        ),
        Err(PerfMeasurementError::NoSamples)
    );
    // An unknown leg for a multi-leg NFR fails closed.
    assert!(matches!(
        LatencyVerificationArtifact::from_samples(
            LatencyNfr::OrderEventCallback,
            "neither",
            PtpClockDiscipline::Disciplined { max_offset_ns: 1 },
            0,
            1_000,
            &good_samples,
        ),
        Err(PerfMeasurementError::UnknownThresholdLeg { .. })
    ));
}

#[test]
fn multi_leg_nfr_needs_all_legs_to_verify() {
    let leg = |label: &str| {
        LatencyVerificationArtifact::from_samples(
            LatencyNfr::PeakLoadOrderLatency,
            label,
            PtpClockDiscipline::Disciplined { max_offset_ns: 1 },
            0,
            1_000,
            &[10, 20, 30],
        )
        .unwrap()
    };
    // NFR-P10 needs both order-latency and dashboard-refresh legs.
    assert!(matches!(
        NfrVerification::assemble(LatencyNfr::PeakLoadOrderLatency, vec![leg("order_latency")]),
        Err(PerfMeasurementError::IncompleteNfrVerification { .. })
    ));
    let verification = NfrVerification::assemble(
        LatencyNfr::PeakLoadOrderLatency,
        vec![leg("order_latency"), leg("dashboard_refresh")],
    )
    .expect("both legs present + same window → complete NFR-P10 verification");
    assert_eq!(verification.artifacts().len(), 2);
}

#[test]
fn nfr_p10_legs_must_be_simultaneous() {
    let leg = |label: &str, start: i64, end: i64| {
        LatencyVerificationArtifact::from_samples(
            LatencyNfr::PeakLoadOrderLatency,
            label,
            PtpClockDiscipline::Disciplined { max_offset_ns: 1 },
            start,
            end,
            &[10, 20, 30],
        )
        .unwrap()
    };
    // Disjoint windows cannot certify NFR-P10's simultaneity requirement.
    assert!(matches!(
        NfrVerification::assemble(
            LatencyNfr::PeakLoadOrderLatency,
            vec![
                leg("order_latency", 0, 1_000),
                leg("dashboard_refresh", 2_000, 3_000),
            ],
        ),
        Err(PerfMeasurementError::IncompleteNfrVerification { .. })
    ));
    // Overlapping windows are simultaneous → assembles.
    NfrVerification::assemble(
        LatencyNfr::PeakLoadOrderLatency,
        vec![
            leg("order_latency", 0, 2_000),
            leg("dashboard_refresh", 1_000, 3_000),
        ],
    )
    .expect("overlapping windows satisfy simultaneity");

    // NFR-P4's legs (live vs paper, different systems) need NOT be simultaneous:
    // disjoint windows still assemble.
    let p4_leg = |label: &str, start: i64, end: i64| {
        LatencyVerificationArtifact::from_samples(
            LatencyNfr::OrderEventCallback,
            label,
            PtpClockDiscipline::Disciplined { max_offset_ns: 1 },
            start,
            end,
            &[1, 2, 3],
        )
        .unwrap()
    };
    NfrVerification::assemble(
        LatencyNfr::OrderEventCallback,
        vec![p4_leg("live", 0, 1_000), p4_leg("paper", 5_000, 6_000)],
    )
    .expect("NFR-P4 live/paper legs are independent and need not be simultaneous");
}

#[test]
fn catalog_matches_the_seven_ac_nfrs() {
    let ids: Vec<&str> = LATENCY_NFRS.iter().map(|n| n.id()).collect();
    assert_eq!(
        ids,
        [
            "NFR-P1",
            "NFR-P4",
            "NFR-P5",
            "NFR-P6",
            "NFR-P9",
            "NFR-P10",
            "SRS-MD-001"
        ]
    );

    // stated percentile is PER LEG: any leg that states one states p95, and a
    // `<` budget is always p95 while a `<=` flat maximum never is. Every threshold
    // uses a `<`/`<=` matching its SyRS phrasing.
    for nfr in LATENCY_NFRS {
        for t in nfr.thresholds() {
            assert!(t.bound_ms > 0, "{}: positive budget", nfr.id());
            match t.comparison {
                ThresholdComparison::LessThan => assert_eq!(
                    t.stated_percentile,
                    Some(Percentile::P95),
                    "{} leg {:?}: `<` budget is p95",
                    nfr.id(),
                    t.label
                ),
                ThresholdComparison::LessThanOrEqual => assert_eq!(
                    t.stated_percentile,
                    None,
                    "{} leg {:?}: `<=` flat maximum is not a percentile budget",
                    nfr.id(),
                    t.label
                ),
            }
        }
        assert!(
            !nfr.boundary().is_empty(),
            "{}: non-empty boundary",
            nfr.id()
        );
    }

    // NFR-P5/P6/P9 are `<=` detection/delivery thresholds; NFR-P1/P4/P10 are `<`
    // p95 budgets.
    assert_eq!(
        LatencyNfr::HeartbeatStaleness.thresholds()[0].comparison,
        ThresholdComparison::LessThanOrEqual
    );
    assert_eq!(
        LatencyNfr::OrderSignalToAck.thresholds()[0].comparison,
        ThresholdComparison::LessThan
    );
    // The fan-out carries the SRS-MD-001 100 ms additional-latency budget (`<=`).
    let fanout = LatencyNfr::SubscriptionFanout.thresholds();
    assert_eq!(fanout.len(), 1);
    assert_eq!(fanout[0].bound_ms, 100);
    assert_eq!(fanout[0].comparison, ThresholdComparison::LessThanOrEqual);
}
