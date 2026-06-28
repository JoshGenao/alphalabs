//! SRS-FAC-001 / SRS-DATA-007 — the scheduled factor job READS over the unified historical store.
//!
//! This exercises the factor pipeline's store READ: it ingests fixture market (`DailyEquityBar`) and
//! fundamental (`Fundamental`) records into a durable [`MarketDataStore`], then drives
//! [`run_scheduled_factor_job_over_store`], which sources BOTH the market and the fundamental inputs from
//! the store by symbol / date range / resolution — with NO provider named (the SRS-DATA-007 factor-job
//! READ consumer; point-in-time SAFE loaders) — assembles the cross-section, and runs the full-universe
//! factor job (SRS-FAC-001: the 8,000-floor, the calendar-resolved schedule, the injected-clock deadline).
//!
//! The run DERIVES its data as-of from the calendar (`TradingCalendar::session_as_of_ts(schedule.session)`,
//! here the BusinessWeekCalendar test mapping), NOT a caller-supplied timestamp — so a caller cannot pair
//! a session with a future as-of. The clock is a deterministic in-deadline clock so the run is
//! reproducible; the live wall-clock NFR-P7 performance harness over real provider data (Databento /
//! Sharadar, SRS-DATA-001/005) and the CONCRETE real-calendar `session_as_of_ts` mapping (test calendars
//! stand in) are deferred owners, so SRS-FAC-001 stays `passes:false`; fixture-sourced store data stands
//! in, exactly as the verification step permits.

use atp_data::store::{DatasetKind, MarketDataRecord, MarketDataStore, MarketField, NaturalKey};
use atp_factor_pipeline::factor_job::{
    Clock, FactorJobConfig, FactorJobError, FactorJobOutcome, FactorJobSchedule, FactorModel,
    FundamentalFactorInput, Instant, MarketFactorInput, MinutesOfDay, SessionOrdinal,
    TradingCalendar,
};
use atp_factor_pipeline::store_inputs::{
    assemble_factor_inputs, load_fundamental_input, run_scheduled_factor_job_over_store,
    FactorInputError, MarketInputBasis, StoreFactorJobError, FUNDAMENTAL_RATIOS_RESOLUTION,
};
use atp_types::{AssetClass, SecurityKey};

// ---- fixtures ------------------------------------------------------------------------------------

fn field(name: &str, value_minor: i64) -> MarketField {
    MarketField {
        name: name.to_string(),
        value_minor,
    }
}

fn close_bar(symbol: &str, event_ts: i64, close: i64) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::DailyEquityBar,
            symbol: symbol.to_string(),
            resolution: "1d".to_string(),
            event_ts,
            option_contract: None,
        },
        vec![field("close", close), field("volume", 1_000)],
    )
    .expect("well-formed daily bar")
}

/// A key-ratio fundamental snapshot whose availability (filing) instant is its period end (the simple
/// no-lookahead default: filed the day the period closed).
fn fundamental(
    symbol: &str,
    period_end_ts: i64,
    net_income_minor: i64,
    book_equity_minor: i64,
    market_value_minor: i64,
) -> MarketDataRecord {
    fundamental_with_availability(
        symbol,
        period_end_ts,
        period_end_ts,
        net_income_minor,
        book_equity_minor,
        market_value_minor,
    )
}

/// A key-ratio fundamental snapshot with an explicit AVAILABILITY (filing) instant distinct from its
/// fiscal `period_end_ts` -- the point-in-time provenance the loader gates on (no lookahead bias).
fn fundamental_with_availability(
    symbol: &str,
    period_end_ts: i64,
    available_ts: i64,
    net_income_minor: i64,
    book_equity_minor: i64,
    market_value_minor: i64,
) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::Fundamental,
            symbol: symbol.to_string(),
            resolution: FUNDAMENTAL_RATIOS_RESOLUTION.to_string(),
            event_ts: period_end_ts,
            option_contract: None,
        },
        vec![
            field("available_ts", available_ts),
            field("net_income_minor", net_income_minor),
            field("book_equity_minor", book_equity_minor),
            field("market_value_minor", market_value_minor),
        ],
    )
    .expect("well-formed fundamental record")
}

fn equity(symbol: &str) -> SecurityKey {
    SecurityKey::new(symbol, AssetClass::Equity).expect("equity key")
}

fn sym(i: usize) -> String {
    format!("SEC{i:05}")
}

/// Build a store of `n` securities, each with `bars_per_security` daily bars plus one fundamental
/// snapshot. Records are inserted in canonical natural-key order — all daily bars (by symbol, then ts)
/// before any fundamental — so every `upsert` appends (no O(n) shift), keeping even a large store cheap
/// to build. The close series is non-linear (consecutive returns differ → realized volatility > 0) and
/// the fundamental's net income varies with `i` (so earnings yield, hence the ranking, is non-degenerate).
fn build_store_sized(n: usize, bars_per_security: i64) -> MarketDataStore {
    let mut store = MarketDataStore::new();
    for i in 0..n {
        let s = sym(i);
        let slope = (i % 50 + 1) as i64 * 10;
        for t in 1..=bars_per_security {
            // The alternating +7 makes consecutive returns differ (realized volatility > 0); always > 0.
            let close = 10_000 + slope * t + if t % 2 == 0 { 7 } else { 0 };
            store.upsert(close_bar(&s, t, close)).expect("bar");
        }
    }
    for i in 0..n {
        let s = sym(i);
        store
            .upsert(fundamental(
                &s,
                3,
                100_000 + i as i64 * 7,
                500_000 + i as i64 * 3,
                2_000_000,
            ))
            .expect("fundamental");
    }
    store
}

fn build_store(n: usize) -> MarketDataStore {
    build_store_sized(n, 3)
}

// ---- calendar / clock / model -------------------------------------------------------------------

/// A business-day calendar (5-on / 2-off) over session ordinals — the same calendar contract strategy
/// scheduling resolves against (SYS-51).
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
    /// Test mapping: the session ordinal IS the as-of epoch instant (so session 100 -> as_of 100). A
    /// real calendar maps session -> civil-date/epoch; here the binding is just structural so the
    /// wrapper derives its as-of from (calendar, session), not a caller timestamp.
    fn session_as_of_ts(&self, session: SessionOrdinal) -> Option<i64> {
        Self::is_weekday(session).then_some(session as i64)
    }
}

/// A clock fixed within the schedule(100) window (start 450, deadline 565).
struct FixedClock(Instant);

impl Clock for FixedClock {
    fn now(&self) -> Instant {
        self.0
    }
}

fn clock() -> FixedClock {
    FixedClock(Instant {
        session: 100,
        minute: 500,
    })
}

/// A value+momentum factor over both sources; abstains only on a degenerate zero-volatility input.
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

fn schedule(session: SessionOrdinal) -> FactorJobSchedule {
    FactorJobSchedule {
        session,
        start_minutes_before_open: 120,
        deadline_minutes_before_open: 5,
    }
}

fn config() -> FactorJobConfig {
    FactorJobConfig {
        min_scored_ratio: 0.5,
    }
}

// ---- tests --------------------------------------------------------------------------------------

#[test]
fn assembles_market_and_fundamental_from_the_store_with_no_provider_named() {
    // net_income 250_000 / market_value 5_000_000 = 0.05; book_equity 1_000_000 / 5_000_000 = 0.2.
    // Closes 10_000, 12_000, 12_000: trailing (12000-10000)/10000 = 0.2; returns [0.2, 0.0], std 0.1.
    let mut store = MarketDataStore::new();
    store.upsert(close_bar("AAPL", 1, 10_000)).unwrap();
    store.upsert(close_bar("AAPL", 2, 12_000)).unwrap();
    store.upsert(close_bar("AAPL", 3, 12_000)).unwrap();
    store
        .upsert(fundamental("AAPL", 3, 250_000, 1_000_000, 5_000_000))
        .unwrap();

    let rows = assemble_factor_inputs(&store, &[equity("AAPL")], 0, 10, MarketInputBasis::Raw)
        .expect("assemble reads the store");
    assert_eq!(rows.len(), 1);
    let market = rows[0].market.expect("market input present");
    let fundamental = rows[0].fundamental.expect("fundamental input present");
    assert!((market.trailing_return - 0.2).abs() < 1e-12);
    assert!((market.realized_volatility - 0.1).abs() < 1e-12);
    assert!((fundamental.earnings_yield - 0.05).abs() < 1e-12);
    assert!((fundamental.book_to_price - 0.2).abs() < 1e-12);
}

#[test]
fn fundamental_loader_uses_the_latest_record_as_of_the_run_date() {
    // Two fundamental snapshots; the latest at/before the as-of date is the in-force statement.
    let mut store = MarketDataStore::new();
    store
        .upsert(fundamental("AAPL", 1, 100_000, 400_000, 2_000_000))
        .unwrap();
    store
        .upsert(fundamental("AAPL", 5, 300_000, 1_000_000, 2_000_000))
        .unwrap();
    let input = load_fundamental_input(&store, &equity("AAPL"), 10)
        .expect("loader reads the store")
        .expect("a record at/before the as-of date");
    // The ts=5 snapshot: 300_000 / 2_000_000 = 0.15.
    assert!((input.earnings_yield - 0.15).abs() < 1e-12);
    // As of ts=3, only the ts=1 snapshot is in force: 100_000 / 2_000_000 = 0.05.
    let earlier = load_fundamental_input(&store, &equity("AAPL"), 3)
        .expect("loader reads the store")
        .expect("the ts=1 snapshot is in force as of ts=3");
    assert!((earlier.earnings_yield - 0.05).abs() < 1e-12);
}

#[test]
fn fundamental_before_the_market_lookback_start_is_still_found() {
    // Codex regression: fundamentals are periodic, so the in-force statement can predate the market
    // return window's start. A market window [100, 200] with the latest fundamental at ts=50 must STILL
    // score the security -- not skip it as missing fundamental data because of the older statement.
    let mut store = MarketDataStore::new();
    store.upsert(close_bar("AAPL", 100, 10_000)).unwrap();
    store.upsert(close_bar("AAPL", 150, 11_000)).unwrap();
    store.upsert(close_bar("AAPL", 200, 12_000)).unwrap();
    // The latest fundamental is at ts=50 -- BEFORE the market window start (100).
    store
        .upsert(fundamental("AAPL", 50, 250_000, 1_000_000, 5_000_000))
        .unwrap();

    // Standalone loader: as of the run date (200) it finds the ts=50 statement.
    let fund = load_fundamental_input(&store, &equity("AAPL"), 200)
        .expect("loader reads the store")
        .expect("the older as-of fundamental is found, not None");
    assert!((fund.earnings_yield - 0.05).abs() < 1e-12);

    // Assembler over the market window [100, 200]: the security is scored (both inputs present), not
    // skipped for a stale-but-valid fundamental.
    let rows = assemble_factor_inputs(&store, &[equity("AAPL")], 100, 200, MarketInputBasis::Raw)
        .expect("assemble");
    assert!(rows[0].market.is_some(), "market input present");
    assert!(
        rows[0].fundamental.is_some(),
        "the pre-window-start fundamental must still be found, not skipped"
    );
}

#[test]
fn absent_fundamental_is_an_auditable_absence() {
    let store = MarketDataStore::new();
    let input = load_fundamental_input(&store, &equity("AAPL"), 10)
        .expect("no record at/before the as-of date is a value, not an error");
    assert_eq!(input, None, "no record -> skip, never a fabricated ratio");
    // A pre-epoch as-of date is also an auditable absence (no statement can exist that early).
    assert_eq!(
        load_fundamental_input(&store, &equity("AAPL"), -1).expect("value"),
        None
    );
}

fn fundamental_fields(symbol: &str, event_ts: i64, fields: Vec<MarketField>) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::Fundamental,
            symbol: symbol.to_string(),
            resolution: FUNDAMENTAL_RATIOS_RESOLUTION.to_string(),
            event_ts,
            option_contract: None,
        },
        fields,
    )
    .expect("well-formed record shell")
}

#[test]
fn fundamental_missing_a_required_field_fails_closed() {
    // A record with availability + net income but NO book equity: fail closed, do not fabricate.
    let mut store = MarketDataStore::new();
    store
        .upsert(fundamental_fields(
            "AAPL",
            3,
            vec![field("available_ts", 3), field("net_income_minor", 100_000)],
        ))
        .unwrap();
    let err = load_fundamental_input(&store, &equity("AAPL"), 10)
        .expect_err("a malformed fundamental must fail closed");
    assert_eq!(
        err,
        FactorInputError::MissingFundamentalField {
            symbol: "AAPL".to_string(),
            event_ts: 3,
            field: "book_equity_minor",
        }
    );
}

#[test]
fn fundamental_missing_availability_field_fails_closed() {
    // The availability (filing) stamp is REQUIRED for point-in-time selection; a record without it is
    // unusable (its lookahead-safety cannot be evaluated) and fails closed.
    let mut store = MarketDataStore::new();
    store
        .upsert(fundamental_fields(
            "AAPL",
            3,
            vec![
                field("net_income_minor", 100_000),
                field("book_equity_minor", 400_000),
                field("market_value_minor", 2_000_000),
            ],
        ))
        .unwrap();
    let err = load_fundamental_input(&store, &equity("AAPL"), 10)
        .expect_err("a fundamental with no availability stamp must fail closed");
    assert_eq!(
        err,
        FactorInputError::MissingFundamentalField {
            symbol: "AAPL".to_string(),
            event_ts: 3,
            field: "available_ts",
        }
    );
}

#[test]
fn fundamental_filed_after_the_run_date_is_excluded_no_lookahead() {
    // Point-in-time correctness: a statement whose fiscal PERIOD END is at/before the run date but
    // whose AVAILABILITY (filing) is AFTER it was not knowable then -- using it is lookahead bias. The
    // loader must select the earlier statement that WAS available, never the not-yet-filed one.
    let mut store = MarketDataStore::new();
    // Q3 statement: period end 90, filed (available) 100. EY = 200_000 / 4_000_000 = 0.05.
    store
        .upsert(fundamental_with_availability(
            "AAPL", 90, 100, 200_000, 800_000, 4_000_000,
        ))
        .unwrap();
    // Annual statement: period end 200 (<= run date 210) BUT filed 260 (AFTER the run date) -- lookahead.
    store
        .upsert(fundamental_with_availability(
            "AAPL", 200, 260, 999_999, 9_999_999, 4_000_000,
        ))
        .unwrap();

    let input = load_fundamental_input(&store, &equity("AAPL"), 210)
        .expect("loader reads the store")
        .expect("the available Q3 statement is in force");
    assert!(
        (input.earnings_yield - 0.05).abs() < 1e-12,
        "must use the available Q3 statement, NOT the not-yet-filed annual (lookahead)"
    );

    // Once the annual is filed (run date 300), it becomes the in-force statement.
    let later = load_fundamental_input(&store, &equity("AAPL"), 300)
        .expect("loader reads the store")
        .expect("the annual is now available");
    assert!(
        (later.earnings_yield - (999_999.0 / 4_000_000.0)).abs() < 1e-9,
        "once filed, the latest-period statement is in force"
    );
}

#[test]
fn fundamental_available_before_period_end_fails_closed() {
    // Corrupt provenance: a statement cannot be filed before its fiscal period ends. Fail closed
    // rather than trust availability metadata that could mask a lookahead.
    let mut store = MarketDataStore::new();
    store
        .upsert(fundamental_with_availability(
            "AAPL", 50, 40, 100_000, 400_000, 2_000_000,
        ))
        .unwrap();
    let err = load_fundamental_input(&store, &equity("AAPL"), 100)
        .expect_err("availability before period end must fail closed");
    assert_eq!(
        err,
        FactorInputError::AvailabilityBeforePeriodEnd {
            symbol: "AAPL".to_string(),
            event_ts: 50,
            available_ts: 40,
        }
    );
}

#[test]
fn assembler_fails_closed_on_inverted_market_window() {
    // An inverted market lookback (start > end) is a bad range construction; it fails closed via the
    // market loader rather than silently dropping the security.
    let store = MarketDataStore::new();
    let err = assemble_factor_inputs(&store, &[equity("AAPL")], 300, 100, MarketInputBasis::Raw)
        .expect_err("inverted window must fail closed");
    assert_eq!(
        err,
        FactorInputError::InvalidWindow {
            start_ts: 300,
            end_ts: 100,
        }
    );
}

#[test]
fn runs_full_universe_factor_job_over_the_store_within_deadline() {
    // 8,000+ securities, each with store-resident market + fundamental data; the job resolves session
    // 100 through the calendar (a weekday) and reads an in-deadline clock.
    let store = build_store(8_000);
    let securities: Vec<SecurityKey> = (0..8_000).map(|i| equity(&sym(i))).collect();

    let outcome = run_scheduled_factor_job_over_store(
        &store,
        &securities,
        100,
        MarketInputBasis::Raw,
        &schedule(100),
        &BusinessWeekCalendar,
        &config(),
        &ValueMomentumFactor,
        &clock(),
    )
    .expect("the store-backed job runs");

    let FactorJobOutcome::WithinDeadline(set) = outcome else {
        panic!("expected completion within the deadline");
    };
    assert_eq!(set.universe_size, 8_000);
    assert_eq!(
        set.scores.len(),
        8_000,
        "every security scored on both sources"
    );
    assert!(set.skipped.is_empty());
    // Ranks are a dense, non-increasing-by-factor 1..=n.
    for (i, score) in set.scores.iter().enumerate() {
        assert_eq!(score.rank as usize, i + 1);
        if i > 0 {
            assert!(set.scores[i - 1].factor_value >= score.factor_value);
        }
    }
}

// GATED performance test (run on demand: `cargo test -- --ignored`). It measures real wall-clock time,
// which is environment-sensitive, so it is NOT part of the normal contract smoke (a loaded host must not
// fail a correct build on timing). The DETERMINISTIC proof that the indexed read path is used lives in
// atp-data (`store::tests::records_for_returns_only_the_target_series_in_event_ts_order`, which asserts
// records_for isolates one series and that query_unified agrees with it); this test is the optional
// end-to-end wall-clock confirmation that a large store assembles fast rather than quadratically.
#[test]
#[ignore = "gated wall-clock performance test; environment-sensitive, run with --ignored"]
fn full_universe_store_read_scales_under_a_wall_clock_budget() {
    // Codex regression: assemble_factor_inputs does one store read per security. A full-store SCAN per
    // read would be O(universe * store_size) and blow the deadline at realistic volume. The store's
    // indexed read (MarketDataStore::records_for, used by query_unified for a kind-narrowed query)
    // keeps each read O(log n + matches), so a large store (8,000 securities x 40 daily bars +
    // fundamentals ~= 328k records) assembles + runs the full-universe job well within a generous
    // wall-clock budget; a per-read full scan over the same store would take minutes.
    let store = build_store_sized(8_000, 40);
    let securities: Vec<SecurityKey> = (0..8_000).map(|i| equity(&sym(i))).collect();

    let started = std::time::Instant::now();
    let outcome = run_scheduled_factor_job_over_store(
        &store,
        &securities,
        100,
        MarketInputBasis::Raw,
        &schedule(100),
        &BusinessWeekCalendar,
        &config(),
        &ValueMomentumFactor,
        &clock(),
    )
    .expect("the store-backed job runs");
    let elapsed = started.elapsed();

    let FactorJobOutcome::WithinDeadline(set) = outcome else {
        panic!("expected completion within the deadline");
    };
    assert_eq!(
        set.scores.len(),
        8_000,
        "every security scored over the large store"
    );
    // Generous budget: the indexed read is sub-second even in a debug build; a quadratic per-read scan
    // over a 328k-record store would be tens of seconds to minutes and blow this bound.
    assert!(
        elapsed < std::time::Duration::from_secs(30),
        "full-universe store-backed assembly took {elapsed:?}; a per-read full scan would be quadratic"
    );
}

#[test]
fn securities_missing_a_store_source_are_skipped_not_fabricated() {
    // A full universe plus three extra securities with no store data at all: each is an auditable skip
    // (MissingMarketData), never a fabricated score, and the run still completes over the rest.
    let store = build_store(8_000);
    let mut securities: Vec<SecurityKey> = (0..8_000).map(|i| equity(&sym(i))).collect();
    securities.push(equity("GHOST1"));
    securities.push(equity("GHOST2"));
    securities.push(equity("GHOST3"));

    let outcome = run_scheduled_factor_job_over_store(
        &store,
        &securities,
        100,
        MarketInputBasis::Raw,
        &schedule(100),
        &BusinessWeekCalendar,
        &config(),
        &ValueMomentumFactor,
        &clock(),
    )
    .expect("the store-backed job runs");

    let FactorJobOutcome::WithinDeadline(set) = outcome else {
        panic!("expected completion within the deadline");
    };
    assert_eq!(set.universe_size, 8_003);
    assert_eq!(set.scores.len(), 8_000);
    assert_eq!(
        set.skipped.len(),
        3,
        "the three ghost securities are skipped"
    );
}

#[test]
fn pre_start_store_backed_run_fails_before_assembly() {
    // The scheduled-execution boundary must be enforced for the WHOLE path: a run invoked before its
    // scheduled start must fail on the schedule gate WITHOUT doing assembly work. Proof: the store
    // holds a MALFORMED fundamental that would fail assembly closed (StoreFactorJobError::Input); a
    // pre-start run must instead return the schedule error (StoreFactorJobError::Job /
    // StartedBeforeScheduledStart), showing assembly never ran.
    let mut store = build_store(8_000);
    store
        .upsert(fundamental_fields(
            "SEC00000",
            9,
            vec![field("available_ts", 9), field("net_income_minor", 1)], // missing book_equity
        ))
        .unwrap();
    let securities: Vec<SecurityKey> = (0..8_000).map(|i| equity(&sym(i))).collect();

    // schedule(100)'s start window opens at minute 450 (open 570 - 120); a clock at 400 is pre-start.
    let early = FixedClock(Instant {
        session: 100,
        minute: 400,
    });
    let err = run_scheduled_factor_job_over_store(
        &store,
        &securities,
        100,
        MarketInputBasis::Raw,
        &schedule(100),
        &BusinessWeekCalendar,
        &config(),
        &ValueMomentumFactor,
        &early,
    )
    .expect_err("a pre-start run must fail closed");
    match err {
        StoreFactorJobError::Job(FactorJobError::StartedBeforeScheduledStart { .. }) => {}
        other => panic!(
            "expected the schedule gate to fire BEFORE assembly (no Input error), got {other:?}"
        ),
    }
}

/// A clock that returns `first` on its first read and `second` on every read after — models a wall
/// clock that JUMPS between the wrapper's preflight read and the scored core's completion read (e.g. a
/// backward step during input assembly).
struct JumpClock {
    first: Instant,
    second: Instant,
    reads: std::cell::Cell<u32>,
}

impl Clock for JumpClock {
    fn now(&self) -> Instant {
        let n = self.reads.get();
        self.reads.set(n + 1);
        if n == 0 {
            self.first
        } else {
            self.second
        }
    }
}

#[test]
fn store_backed_run_fails_closed_on_a_clock_regression_during_assembly() {
    // Timing integrity across assembly: the wrapper preflights (read #1 = the authoritative start),
    // assembles, then the scored core reads the clock for completion (read #2). If the wall clock
    // REGRESSED between those reads (a backward step during assembly), the run must fail closed
    // (NonMonotonicClock) -- the first observed start stays authoritative and a second independent start
    // read would have lost the regression.
    let store = build_store(8_000);
    let securities: Vec<SecurityKey> = (0..8_000).map(|i| equity(&sym(i))).collect();
    // First read (preflight start) at minute 500 (within the window 450..565); completion read jumps
    // BACKWARD to 460 -- a regression.
    let regressing = JumpClock {
        first: Instant {
            session: 100,
            minute: 500,
        },
        second: Instant {
            session: 100,
            minute: 460,
        },
        reads: std::cell::Cell::new(0),
    };
    let err = run_scheduled_factor_job_over_store(
        &store,
        &securities,
        100,
        MarketInputBasis::Raw,
        &schedule(100),
        &BusinessWeekCalendar,
        &config(),
        &ValueMomentumFactor,
        &regressing,
    )
    .expect_err("a clock regression during assembly must fail closed");
    match err {
        StoreFactorJobError::Job(FactorJobError::NonMonotonicClock { .. }) => {}
        other => {
            panic!("expected NonMonotonicClock from the authoritative-start gate, got {other:?}")
        }
    }
}

#[test]
fn store_assembly_failure_propagates_as_a_fail_closed_job_error() {
    // A fundamental record present but missing a field makes assembly fail closed; the store-backed job
    // surfaces it as StoreFactorJobError::Input rather than running on fabricated data.
    let mut store = build_store(8_000);
    // Add a later malformed snapshot for an existing universe symbol: available (so it becomes the
    // as-of record) but missing book equity, so derivation fails closed.
    store
        .upsert(fundamental_fields(
            "SEC00000",
            9, // a later period end than ts=3, so it becomes the as-of record
            vec![field("available_ts", 9), field("net_income_minor", 1)],
        ))
        .unwrap();
    let securities: Vec<SecurityKey> = (0..8_000).map(|i| equity(&sym(i))).collect();

    let err = run_scheduled_factor_job_over_store(
        &store,
        &securities,
        100,
        MarketInputBasis::Raw,
        &schedule(100),
        &BusinessWeekCalendar,
        &config(),
        &ValueMomentumFactor,
        &clock(),
    )
    .expect_err("a malformed store record must fail the job closed");
    match err {
        StoreFactorJobError::Input(FactorInputError::MissingFundamentalField {
            symbol,
            field,
            ..
        }) => {
            assert_eq!(symbol, "SEC00000");
            assert_eq!(field, "book_equity_minor");
        }
        other => panic!("expected a fail-closed input error, got {other:?}"),
    }
}
