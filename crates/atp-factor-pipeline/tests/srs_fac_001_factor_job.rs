//! SRS-FAC-001 end-to-end scheduled-factor-job integration test (Rust crate-level).
//!
//! Drives [`atp_factor_pipeline::factor_job`] the way a scheduled factor run would: a concrete
//! [`TradingCalendar`] (a business-day calendar over session ordinals), a user-defined
//! [`FactorModel`] over both market and fundamental inputs, and a full US-equity-scale universe
//! (8,000+ securities). It asserts the four SRS-FAC-001 acceptance facets are coherent --
//! full-universe ranking, market+fundamental scoring (with auditable skips), schedule resolution
//! through the calendar (SYS-51), and the deadline instant read from an injected clock (NFR-P7) --
//! plus the producer bridge to SRS-BT-006 (a regular [`FactorPanel`] that feeds
//! `compute_tear_sheet`) and determinism.

use atp_factor_pipeline::factor_analysis::compute_tear_sheet;
use atp_factor_pipeline::factor_job::{
    assemble_regular_panel, run_factor_job, Clock, FactorJobConfig, FactorJobError,
    FactorJobOutcome, FactorModel, FactorSkipReason, FundamentalFactorInput, Instant,
    MarketFactorInput, MinutesOfDay, RealizedFactorSession, RealizedObservation,
    SecurityFactorInputs, SessionOrdinal, TradingCalendar, FULL_UNIVERSE_MIN,
};
use atp_types::{AssetClass, SecurityKey};

fn inst(session: SessionOrdinal, minute: MinutesOfDay) -> Instant {
    Instant { session, minute }
}

/// A deterministic clock fixed at a single session-aware instant -- the default tests use a minute
/// within the schedule(100) window (start 450, deadline 565).
struct FixedClock(Instant);

impl Clock for FixedClock {
    fn now(&self) -> Instant {
        self.0
    }
}

/// A deterministic clock returning `start` on the first read and `finish` after -- models a run
/// that begins at `start` and completes at `finish`, exercising the start/completion deadline
/// checks independently (interior `Cell`, fixed sequence).
struct StartFinishClock {
    start: Instant,
    finish: Instant,
    read: std::cell::Cell<u32>,
}

impl StartFinishClock {
    fn new(start: Instant, finish: Instant) -> Self {
        Self {
            start,
            finish,
            read: std::cell::Cell::new(0),
        }
    }
}

impl Clock for StartFinishClock {
    fn now(&self) -> Instant {
        let n = self.read.get();
        self.read.set(n + 1);
        if n == 0 {
            self.start
        } else {
            self.finish
        }
    }
}

/// A clock on the scheduled session, within its window (start 450, deadline 565).
fn clock() -> FixedClock {
    FixedClock(inst(100, 500))
}

/// A business-day calendar: ordinals whose `ordinal % 7 < 5` are sessions (a 5-on / 2-off
/// week), so `next_session` steps over the two-day "weekend". Concrete enough to exercise
/// calendar-resolved schedule + rebalance-interval resolution without the deferred real
/// US-equity calendar.
struct BusinessWeekCalendar;

impl BusinessWeekCalendar {
    fn is_weekday(ordinal: SessionOrdinal) -> bool {
        ordinal % 7 < 5
    }
}

impl TradingCalendar for BusinessWeekCalendar {
    fn is_session(&self, session: SessionOrdinal) -> bool {
        Self::is_weekday(session)
    }
    fn session_open(&self, session: SessionOrdinal) -> Option<MinutesOfDay> {
        Self::is_weekday(session).then_some(570)
    }
    fn session_close(&self, session: SessionOrdinal) -> Option<MinutesOfDay> {
        Self::is_weekday(session).then_some(960)
    }
    fn is_early_close(&self, _session: SessionOrdinal) -> bool {
        false
    }
    fn next_session(&self, session: SessionOrdinal) -> Option<SessionOrdinal> {
        let mut next = session + 1;
        while !Self::is_weekday(next) {
            next += 1;
        }
        Some(next)
    }
}

/// A value+momentum factor: earnings yield plus trailing return, penalized by volatility. It
/// abstains when volatility is exactly zero (a degenerate input). Pure and deterministic.
struct ValueMomentumFactor;

impl FactorModel for ValueMomentumFactor {
    fn compute(
        &self,
        market: &MarketFactorInput,
        fundamental: &FundamentalFactorInput,
    ) -> Option<f64> {
        if market.realized_volatility == 0.0 {
            return None;
        }
        Some(fundamental.earnings_yield + market.trailing_return - 0.5 * market.realized_volatility)
    }
}

fn key(index: usize) -> SecurityKey {
    SecurityKey::new(&format!("SEC{index:05}"), AssetClass::Equity).expect("equity key")
}

/// A deterministic linear-congruential generator so the "property" sweep varies inputs across
/// seeds WITHOUT any real RNG (the run itself must stay deterministic).
fn lcg(state: &mut u64) -> f64 {
    *state = state
        .wrapping_mul(6_364_136_223_846_793_005)
        .wrapping_add(1_442_695_040_888_963_407);
    // Map the high bits to a fraction in [0, 1).
    ((*state >> 11) as f64) / ((1u64 << 53) as f64)
}

/// Build a full-universe fixture of `n` securities seeded by `seed`.
fn build_universe(n: usize, seed: u64) -> Vec<SecurityFactorInputs> {
    let mut state = seed.wrapping_add(0x9E37_79B9_7F4A_7C15);
    (0..n)
        .map(|i| SecurityFactorInputs {
            security: key(i),
            market: Some(MarketFactorInput {
                trailing_return: lcg(&mut state) * 0.4 - 0.2,
                realized_volatility: lcg(&mut state) * 0.3 + 0.01,
            }),
            fundamental: Some(FundamentalFactorInput {
                earnings_yield: lcg(&mut state) * 0.1,
                book_to_price: lcg(&mut state) * 2.0,
            }),
        })
        .collect()
}

fn schedule(session: SessionOrdinal) -> atp_factor_pipeline::factor_job::FactorJobSchedule {
    atp_factor_pipeline::factor_job::FactorJobSchedule {
        session,
        start_minutes_before_open: 120,
        deadline_minutes_before_open: 5,
    }
}

/// Require at least one scored security (the coverage policy).
fn full_universe_config() -> FactorJobConfig {
    FactorJobConfig {
        min_scored_ratio: 0.5,
    }
}

#[test]
fn ranks_full_universe_through_calendar_within_deadline() {
    let universe = build_universe(8_000, 1);
    // Session 100: 100 % 7 == 2 -> a weekday (trading session).
    let outcome = run_factor_job(
        &schedule(100),
        &BusinessWeekCalendar,
        &full_universe_config(),
        &ValueMomentumFactor,
        &clock(),
        &universe,
    )
    .expect("run");
    let FactorJobOutcome::WithinDeadline(set) = outcome else {
        panic!("expected completion within the deadline");
    };
    assert_eq!(set.universe_size, 8_000);
    assert_eq!(set.scores.len(), 8_000); // every security scored on both sources
    assert!(set.skipped.is_empty());
    // Ranks form a dense, strictly-decreasing-by-factor 1..=n with no gaps.
    for (i, score) in set.scores.iter().enumerate() {
        assert_eq!(score.rank as usize, i + 1);
        if i > 0 {
            assert!(set.scores[i - 1].factor_value >= score.factor_value);
        }
    }
}

#[test]
fn run_is_deterministic_and_order_independent_across_seeds() {
    for seed in 0..64u64 {
        let mut universe = build_universe(8_000, seed);
        let first = run_factor_job(
            &schedule(100),
            &BusinessWeekCalendar,
            &full_universe_config(),
            &ValueMomentumFactor,
            &clock(),
            &universe,
        )
        .expect("run 1");
        let again = run_factor_job(
            &schedule(100),
            &BusinessWeekCalendar,
            &full_universe_config(),
            &ValueMomentumFactor,
            &clock(),
            &universe,
        )
        .expect("run 2");
        assert_eq!(first, again, "seed {seed}: repeated run differed");

        // Shuffling the input order must not change the ranked output (the run re-sorts by the
        // total order).
        universe.reverse();
        let reversed = run_factor_job(
            &schedule(100),
            &BusinessWeekCalendar,
            &full_universe_config(),
            &ValueMomentumFactor,
            &clock(),
            &universe,
        )
        .expect("run reversed");
        assert_eq!(
            first, reversed,
            "seed {seed}: input order changed the ranking"
        );
    }
}

#[test]
fn skips_securities_missing_either_source() {
    let mut universe = build_universe(8_000, 7);
    universe[10].market = None;
    universe[20].fundamental = None;
    universe[30].market = Some(MarketFactorInput {
        trailing_return: 0.05,
        realized_volatility: 0.0, // factor abstains
    });
    let FactorJobOutcome::WithinDeadline(set) = run_factor_job(
        &schedule(100),
        &BusinessWeekCalendar,
        &full_universe_config(),
        &ValueMomentumFactor,
        &clock(),
        &universe,
    )
    .expect("run") else {
        panic!("expected completion");
    };
    assert_eq!(set.scores.len(), 7_997);
    assert_eq!(set.skipped.len(), 3);
    let reasons: Vec<FactorSkipReason> = set.skipped.iter().map(|s| s.reason).collect();
    assert!(reasons.contains(&FactorSkipReason::MissingMarketData));
    assert!(reasons.contains(&FactorSkipReason::MissingFundamentalData));
    assert!(reasons.contains(&FactorSkipReason::FactorAbstained));
}

#[test]
fn fails_closed_below_full_universe_floor() {
    // One short of the hard floor -- the floor is the platform constant, not a caller config, so
    // a smaller universe cannot attest full-universe coverage.
    let universe = build_universe(FULL_UNIVERSE_MIN - 1, 1);
    let error = run_factor_job(
        &schedule(100),
        &BusinessWeekCalendar,
        &full_universe_config(),
        &ValueMomentumFactor,
        &clock(),
        &universe,
    )
    .expect_err("below floor");
    assert_eq!(
        error,
        FactorJobError::UniverseBelowMinimum {
            actual: FULL_UNIVERSE_MIN - 1,
            required: FULL_UNIVERSE_MIN,
        }
    );
}

/// SYS-51 regression (Codex finding): the schedule must be resolved through the calendar's
/// `session_open`, so a calendar with no resolvable open for the day fails closed even if a
/// caller might think the day is "valid".
#[test]
fn fails_closed_when_calendar_has_no_session_open() {
    struct NoOpenCalendar;
    impl TradingCalendar for NoOpenCalendar {
        fn is_session(&self, _session: SessionOrdinal) -> bool {
            true
        }
        fn session_open(&self, _session: SessionOrdinal) -> Option<MinutesOfDay> {
            None
        }
        fn session_close(&self, _session: SessionOrdinal) -> Option<MinutesOfDay> {
            None
        }
        fn is_early_close(&self, _session: SessionOrdinal) -> bool {
            false
        }
        fn next_session(&self, session: SessionOrdinal) -> Option<SessionOrdinal> {
            Some(session + 1)
        }
    }
    let universe = build_universe(8_000, 1);
    let error = run_factor_job(
        &schedule(100),
        &NoOpenCalendar,
        &full_universe_config(),
        &ValueMomentumFactor,
        &clock(),
        &universe,
    )
    .expect_err("no session open");
    assert_eq!(error, FactorJobError::NotASession { session: 100 });
}

/// NFR-P7 / SYS-51 regression (Codex finding): the before-open lead is resolved against the
/// concrete session-open instant; a lead that precedes the start of the trading day cannot
/// resolve to a valid start instant and fails closed.
#[test]
fn fails_closed_when_lead_precedes_day_start() {
    // BusinessWeekCalendar opens at minute 570; a 600-minute lead underflows the day.
    let bad = atp_factor_pipeline::factor_job::FactorJobSchedule {
        session: 100,
        start_minutes_before_open: 600,
        deadline_minutes_before_open: 5,
    };
    let universe = build_universe(8_000, 1);
    let error = run_factor_job(
        &bad,
        &BusinessWeekCalendar,
        &full_universe_config(),
        &ValueMomentumFactor,
        &clock(),
        &universe,
    )
    .expect_err("lead before day start");
    assert_eq!(
        error,
        FactorJobError::ScheduleBeforeDayStart {
            session: 100,
            session_open: 570,
            lead: 600,
        }
    );
}

#[test]
fn fails_closed_on_non_session_schedule() {
    let universe = build_universe(8_000, 1);
    // Session 5: 5 % 7 == 5 -> a weekend (not a trading session).
    let error = run_factor_job(
        &schedule(5),
        &BusinessWeekCalendar,
        &full_universe_config(),
        &ValueMomentumFactor,
        &clock(),
        &universe,
    )
    .expect_err("non-session");
    assert_eq!(error, FactorJobError::NotASession { session: 5 });
}

#[test]
fn reports_deadline_exceeded_when_run_overruns() {
    let universe = build_universe(8_000, 1);
    // Session 100 opens at 570, deadline at 565. The run starts at 560 (within) but its
    // scoring + ranking + finalization completes at 570 -> past the deadline, fail-closed.
    let outcome = run_factor_job(
        &schedule(100),
        &BusinessWeekCalendar,
        &full_universe_config(),
        &ValueMomentumFactor,
        &StartFinishClock::new(inst(100, 560), inst(100, 570)),
        &universe,
    )
    .expect("run");
    assert_eq!(
        outcome,
        FactorJobOutcome::DeadlineExceeded {
            session: 100,
            deadline: inst(100, 565),
            observed: inst(100, 570),
            late_start: false,
        }
    );
}

#[test]
fn rejects_late_start() {
    let universe = build_universe(8_000, 1);
    // Invoked at minute 600, after the 565 deadline -> fail-closed before any work.
    let outcome = run_factor_job(
        &schedule(100),
        &BusinessWeekCalendar,
        &full_universe_config(),
        &ValueMomentumFactor,
        &FixedClock(inst(100, 600)),
        &universe,
    )
    .expect("run");
    assert!(matches!(
        outcome,
        FactorJobOutcome::DeadlineExceeded {
            late_start: true,
            observed: Instant {
                session: 100,
                minute: 600
            },
            ..
        }
    ));
}

#[test]
fn rejects_run_fired_on_a_later_session() {
    // A run for session 100 invoked on session 101 (a later day), even at an early minute, is past
    // the deadline -- the session-aware instant catches it where a bare minute-of-day would not.
    let universe = build_universe(8_000, 1);
    let outcome = run_factor_job(
        &schedule(100),
        &BusinessWeekCalendar,
        &full_universe_config(),
        &ValueMomentumFactor,
        &FixedClock(inst(101, 500)),
        &universe,
    )
    .expect("run");
    assert!(matches!(
        outcome,
        FactorJobOutcome::DeadlineExceeded {
            late_start: true,
            ..
        }
    ));
}

#[test]
fn rejects_early_start() {
    // The scheduled start is minute 450 (open 570 - 120). Invoked at 400 -> before the start
    // window, fail-closed.
    let universe = build_universe(8_000, 1);
    let error = run_factor_job(
        &schedule(100),
        &BusinessWeekCalendar,
        &full_universe_config(),
        &ValueMomentumFactor,
        &FixedClock(inst(100, 400)),
        &universe,
    )
    .expect_err("early start");
    assert!(matches!(
        error,
        FactorJobError::StartedBeforeScheduledStart { .. }
    ));
}

#[test]
fn stateful_model_output_is_order_independent() {
    // A model whose score depends on CALL ORDER (interior-mutable counter) still yields an
    // order-independent output, because the job scores in canonical key order (Codex R7).
    struct CallOrderFactor {
        next: std::cell::Cell<f64>,
    }
    impl FactorModel for CallOrderFactor {
        fn compute(
            &self,
            _market: &MarketFactorInput,
            _fundamental: &FundamentalFactorInput,
        ) -> Option<f64> {
            let v = self.next.get();
            self.next.set(v + 1.0);
            Some(v)
        }
    }
    let mut universe = build_universe(8_000, 1);
    let forward = run_factor_job(
        &schedule(100),
        &BusinessWeekCalendar,
        &full_universe_config(),
        &CallOrderFactor {
            next: std::cell::Cell::new(0.0),
        },
        &clock(),
        &universe,
    )
    .expect("forward");
    universe.reverse();
    let reversed = run_factor_job(
        &schedule(100),
        &BusinessWeekCalendar,
        &full_universe_config(),
        &CallOrderFactor {
            next: std::cell::Cell::new(0.0),
        },
        &clock(),
        &universe,
    )
    .expect("reversed");
    assert_eq!(forward, reversed);
}

#[test]
fn ranking_is_order_independent_with_skips() {
    // Determinism with skips (Codex finding): a universe containing skips yields the same outcome
    // (scores AND skipped) regardless of input order.
    let mut universe = build_universe(8_000, 1);
    universe[0].market = None;
    universe[1].fundamental = None;
    let forward = run_factor_job(
        &schedule(100),
        &BusinessWeekCalendar,
        &full_universe_config(),
        &ValueMomentumFactor,
        &clock(),
        &universe,
    )
    .expect("forward");
    universe.reverse();
    let reversed = run_factor_job(
        &schedule(100),
        &BusinessWeekCalendar,
        &full_universe_config(),
        &ValueMomentumFactor,
        &clock(),
        &universe,
    )
    .expect("reversed");
    assert_eq!(forward, reversed);
}

/// The deadline outcome is the ABSOLUTE resolved instant, not a function of universe size: the
/// SAME 8,000-security universe completes under a fast clock but is rejected under a slow one.
#[test]
fn deadline_outcome_depends_on_clock_not_universe_size() {
    let universe = build_universe(8_000, 1);
    let fast = run_factor_job(
        &schedule(100),
        &BusinessWeekCalendar,
        &full_universe_config(),
        &ValueMomentumFactor,
        &FixedClock(inst(100, 450)),
        &universe,
    )
    .expect("fast run");
    assert!(matches!(fast, FactorJobOutcome::WithinDeadline(_)));
    let slow = run_factor_job(
        &schedule(100),
        &BusinessWeekCalendar,
        &full_universe_config(),
        &ValueMomentumFactor,
        &StartFinishClock::new(inst(100, 560), inst(100, 580)),
        &universe,
    )
    .expect("slow run");
    assert!(matches!(
        slow,
        FactorJobOutcome::DeadlineExceeded {
            late_start: false,
            ..
        }
    ));
}

/// Coverage guard (Codex-flagged): a universe where every security abstains scores nothing, so the
/// run fails closed rather than emitting an empty ranking as a success.
#[test]
fn fails_closed_on_no_usable_coverage() {
    // Zero volatility makes ValueMomentumFactor abstain on every security.
    let universe: Vec<SecurityFactorInputs> = (0..8_000)
        .map(|i| SecurityFactorInputs {
            security: key(i),
            market: Some(MarketFactorInput {
                trailing_return: 0.1,
                realized_volatility: 0.0,
            }),
            fundamental: Some(FundamentalFactorInput {
                earnings_yield: 0.05,
                book_to_price: 0.5,
            }),
        })
        .collect();
    let error = run_factor_job(
        &schedule(100),
        &BusinessWeekCalendar,
        &full_universe_config(),
        &ValueMomentumFactor,
        &clock(),
        &universe,
    )
    .expect_err("no coverage");
    assert_eq!(
        error,
        FactorJobError::NoUsableCoverage {
            scored: 0,
            universe_size: 8_000,
            required: 4_000,
        }
    );
}

/// End-to-end producer bridge: a sequence of realized rebalance sessions (resolved through the
/// calendar) assembles into a REGULAR panel that the SRS-BT-006 tear-sheet consumes.
#[test]
fn produces_regular_panel_for_tear_sheet() {
    // Three consecutive trading sessions on the business-week calendar (100, 101, 102 are all
    // weekdays), a forward horizon of 1 session (non-overlapping), 2 quantiles.
    let observations = |base: f64| {
        vec![
            RealizedObservation {
                security: key(0),
                factor_value: 1.0 + base,
                forward_return: 0.01 + base,
            },
            RealizedObservation {
                security: key(1),
                factor_value: 2.0 + base,
                forward_return: 0.02 + base,
            },
            RealizedObservation {
                security: key(2),
                factor_value: 3.0 + base,
                forward_return: 0.03 + base,
            },
            RealizedObservation {
                security: key(3),
                factor_value: 4.0 + base,
                forward_return: 0.04 + base,
            },
        ]
    };
    let sessions = vec![
        RealizedFactorSession {
            session: 100,
            forward_window_end: 101,
            observations: observations(0.0),
        },
        RealizedFactorSession {
            session: 101,
            forward_window_end: 102,
            observations: observations(0.1),
        },
        RealizedFactorSession {
            session: 102,
            forward_window_end: 105,
            observations: observations(0.2),
        },
    ];
    let panel = assemble_regular_panel(&BusinessWeekCalendar, &sessions, 2, 1).expect("panel");
    // The produced panel must be consumable by the SRS-BT-006 tear-sheet and yield a coherent IC
    // (the factor perfectly ranks the forward return -> IC == 1).
    let tear_sheet = compute_tear_sheet(&panel).expect("tear sheet");
    for (_, ic) in &tear_sheet.ic.per_period {
        if let Some(value) = ic {
            assert!((value - 1.0).abs() < 1e-9, "perfect-ranking IC should be 1");
        }
    }
    // The mean spread is defined (the panel is regular -- consistent interval + horizon).
    assert!(tear_sheet.returns.mean_spread.is_some());
}

#[test]
fn rejects_irregular_panel_across_weekend_gap() {
    // Sessions 100, 101, 105. First gap 100->101 is 1 trading session. Second gap 101->105 is 2
    // trading sessions (101->102->105: ordinals 103/104 are the weekend, skipped). 1 != 2 -> the
    // rebalance interval is not constant, so the panel is irregular.
    let observations = vec![
        RealizedObservation {
            security: key(0),
            factor_value: 1.0,
            forward_return: 0.01,
        },
        RealizedObservation {
            security: key(1),
            factor_value: 2.0,
            forward_return: 0.02,
        },
    ];
    let sessions = vec![
        RealizedFactorSession {
            session: 100,
            forward_window_end: 101,
            observations: observations.clone(),
        },
        RealizedFactorSession {
            session: 101,
            forward_window_end: 102,
            observations: observations.clone(),
        },
        RealizedFactorSession {
            session: 105,
            forward_window_end: 106,
            observations,
        },
    ];
    let error =
        assemble_regular_panel(&BusinessWeekCalendar, &sessions, 2, 1).expect_err("irregular");
    assert!(matches!(
        error,
        FactorJobError::IrregularRebalanceInterval { .. }
    ));
}

#[test]
fn rejects_mislabeled_forward_window() {
    // The period claims horizon 1 but its forward window ends 2 sessions out (100 -> 102, skipping
    // session 101) -> the horizon is VERIFIED through the calendar and rejected (Codex F3).
    let observations = vec![RealizedObservation {
        security: key(0),
        factor_value: 1.0,
        forward_return: 0.01,
    }];
    let sessions = vec![RealizedFactorSession {
        session: 100,
        forward_window_end: 102, // gap 2, not the declared 1
        observations,
    }];
    let error =
        assemble_regular_panel(&BusinessWeekCalendar, &sessions, 2, 1).expect_err("mislabeled");
    assert!(matches!(
        error,
        FactorJobError::ForwardWindowMismatch {
            session: 100,
            declared_horizon: 1,
            actual_gap: Some(2),
        }
    ));
}

#[test]
fn fails_closed_on_thin_coverage() {
    // Only 3 of 8,000 securities score (the rest lack market data) -> below the 50% coverage floor,
    // so the run fails closed rather than reporting a near-empty ranking as a full-universe run.
    let mut universe = build_universe(8_000, 1);
    for row in universe.iter_mut().skip(3) {
        row.market = None;
    }
    let error = run_factor_job(
        &schedule(100),
        &BusinessWeekCalendar,
        &full_universe_config(),
        &ValueMomentumFactor,
        &clock(),
        &universe,
    )
    .expect_err("thin coverage");
    assert!(matches!(
        error,
        FactorJobError::NoUsableCoverage {
            scored: 3,
            required: 4_000,
            ..
        }
    ));
}

#[test]
fn rejects_invalid_coverage_ratio() {
    // A negative coverage ratio is not a valid policy -> fail closed (Codex F1).
    let universe = build_universe(8_000, 1);
    let bad = FactorJobConfig {
        min_scored_ratio: -0.5,
    };
    let error = run_factor_job(
        &schedule(100),
        &BusinessWeekCalendar,
        &bad,
        &ValueMomentumFactor,
        &clock(),
        &universe,
    )
    .expect_err("invalid ratio");
    assert!(matches!(error, FactorJobError::InvalidCoverageRatio { .. }));
}

#[test]
fn coverage_floor_cannot_collapse_below_platform_minimum() {
    // A config ratio of 0.0 must not drop the floor to one security -- the hard platform minimum
    // (0.5) still requires half the universe scored (Codex R8).
    let mut universe = build_universe(8_000, 1);
    for row in universe.iter_mut().skip(1) {
        row.market = None;
    }
    let zero = FactorJobConfig {
        min_scored_ratio: 0.0,
    };
    let error = run_factor_job(
        &schedule(100),
        &BusinessWeekCalendar,
        &zero,
        &ValueMomentumFactor,
        &clock(),
        &universe,
    )
    .expect_err("one scored");
    assert!(matches!(
        error,
        FactorJobError::NoUsableCoverage {
            scored: 1,
            required: 4_000,
            ..
        }
    ));
}

#[test]
fn fails_closed_on_backward_clock() {
    // The injected clock regresses between start and completion -> the deadline cannot be trusted,
    // so the run fails closed rather than reading a late completion as on-time (Codex F2).
    let universe = build_universe(8_000, 1);
    let error = run_factor_job(
        &schedule(100),
        &BusinessWeekCalendar,
        &full_universe_config(),
        &ValueMomentumFactor,
        &StartFinishClock::new(inst(100, 560), inst(100, 500)),
        &universe,
    )
    .expect_err("backward clock");
    assert!(matches!(error, FactorJobError::NonMonotonicClock { .. }));
}
