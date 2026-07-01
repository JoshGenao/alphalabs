//! Scheduled full-universe factor job (SRS-FAC-001 / SyRS SYS-32, SYS-33, SYS-51, NFR-P7;
//! StRS SN-2.06, BG-3). The deterministic, dependency-free core that *produces* the factor
//! cross-section the [`crate::factor_analysis`] tear-sheet (SRS-BT-006) consumes -- the
//! upstream half the factor-analysis module explicitly defers to "the deferred SRS-FAC-001
//! horizon-aware producer (reading via the now-complete SRS-DATA-007 interface)".
//!
//! SRS-FAC-001's acceptance criterion bundles four facets, each made falsifiable here over
//! immutable inputs:
//!
//!   1. **Full universe (SYS-32/33).** [`run_factor_job`] screens, ranks, and computes a
//!      user-defined [`FactorModel`] across the full US-equity universe and enforces the HARD
//!      [`FULL_UNIVERSE_MIN`] (8,000) floor ([`FactorJobError::UniverseBelowMinimum`]) -- the
//!      floor is the platform constant, NOT a caller config, so coverage cannot be weakened
//!      from outside; every security must be an EQUITY ([`FactorJobError::NonEquitySecurity`];
//!      the factor universe is US equities); and a run that SCORES fewer than the GREATER of
//!      [`FactorJobConfig::min_scored_ratio`] and the hard [`MIN_SCORED_COVERAGE_RATIO`] of the
//!      universe fails closed ([`FactorJobError::NoUsableCoverage`]) -- so a config of 0.0 cannot
//!      collapse the floor to one security and a near-empty result is never a "success".
//!      Binding the universe to a trusted, session-versioned US-equity MANIFEST (so an arbitrary
//!      equity set cannot be certified as "the" universe) is the deferred SRS-DATA-001 catalog.
//!   2. **Market + fundamental data (SYS-32).** Each [`SecurityFactorInputs`] carries BOTH a
//!      market summary and a fundamental summary (the Phase 1 fundamental provider's data);
//!      a security missing EITHER -- or for which the factor abstains -- is recorded as a
//!      [`SkippedSecurity`] with a reason, never given a fabricated score.
//!   3. **Schedule resolves through the trading calendar (SYS-51).** The start and deadline
//!      offsets are resolved against the calendar's [`TradingCalendar::session_open`] -- the
//!      same calendar contract strategy scheduling (SRS-SDK-002 / SyRS SYS-50) resolves
//!      against -- into concrete intraday instants, not an ad-hoc wall clock; a session with
//!      no resolvable open fails closed ([`FactorJobError::NotASession`]) and a lead that
//!      precedes the day start fails closed ([`FactorJobError::ScheduleBeforeDayStart`]).
//!   4. **Deadline INSTANT, absolute + session-aware (NFR-P7).** The schedule resolves to a
//!      session-aware deadline [`Instant`] (the scheduled session + open minus the deadline lead).
//!      The run reads an injected [`Clock`] at the START -- a run invoked BEFORE its scheduled
//!      start fails closed ([`FactorJobError::StartedBeforeScheduledStart`]), and one invoked
//!      AFTER the deadline (even on a LATER session, which a bare minute-of-day would miss) fails
//!      closed as a late start -- and again AFTER scoring, ranking, and output construction (a run
//!      whose work crossed the deadline fails closed) -- so completion is gated against the
//!      ABSOLUTE deadline instant, never assumed on-time
//!      ([`FactorJobOutcome::DeadlineExceeded`]). The deadline minute is EXCLUSIVE (end-of-minute
//!      semantics): a run still executing during the deadline minute is late. This gate is
//!      OBSERVATIONAL: it catches every overrun it can OBSERVE -- a late start, and a run whose
//!      scoring/ranking/finalization COMPLETED past the deadline -- but it cannot PREEMPT a hung or
//!      pathologically-slow synchronous [`FactorModel`] mid-call (a deterministic, dependency-free
//!      core has no threads, timers, or cancellation). Supervised HARD-deadline termination of a
//!      hung model is the deferred runtime's job (the strategy orchestrator runs each job in its
//!      own resource-limited container, SYS-57; the real NFR-P7 wall-clock performance test
//!      verifies it). The clock is the run's injected timing authority: production supplies a
//!      wall-clock-backed clock, tests a deterministic one. Both the ranked scores AND the skipped
//!      list are sorted by key, so the whole outcome is a pure function of the input SET,
//!      independent of input order.
//!
//! [`assemble_regular_panel`] is the producer bridge to SRS-BT-006: it turns a sequence of
//! realized per-rebalance sessions into a *regular* [`FactorPanel`] -- one whose rebalance
//! interval is a constant number of trading sessions (resolved through the calendar) and whose
//! forward-return windows share one non-overlapping horizon, checked for LABEL CONSISTENCY by
//! matching each period's declared `forward_window_end` against the declared horizon through the
//! calendar ([`FactorJobError::ForwardWindowMismatch`]) -- so a mixed/mislabeled-horizon period is
//! rejected, the regularity the tear-sheet's `mean_spread` / `mean_top` aggregates assume but
//! cannot themselves validate (a [`FactorPeriod`] carries a start timestamp only). Proving the
//! realized returns were actually computed over that window is the deferred (SRS-DATA-007 interface complete; real data = SRS-DATA-005 / SRS-FAC-001) data
//! layer's trust boundary, not this offline producer's.
//!
//! DETERMINISM (the SRS-BT-010 criterion the whole crate honors): fixed left-to-right folds, the
//! [`FactorModel`] invoked in canonical [`SecurityKey`] order, cross-sections ranked by the total
//! order `(factor_value desc, SecurityKey asc)`, the deadline checked against the injected clock
//! (no wall clock of its own), and no parallelism / RNG -- so for a pure model, identical inputs
//! yield identical output regardless of input order. Factor scores are dimensionless `f64` (the
//! factor domain, not a money leak). [`run_factor_job`] takes a caller-supplied
//! [`SecurityFactorInputs`] slice; the store-backed READ path
//! ([`crate::store_inputs::run_scheduled_factor_job_over_store`], SRS-DATA-007) sources BOTH the market
//! ([`crate::store_inputs::load_daily_market_input`]) and the fundamental
//! ([`crate::store_inputs::load_fundamental_input`]) inputs from the unified historical store by symbol /
//! date range / resolution, assembles the cross-section, and feeds it here -- so the named SRS-DATA-007
//! factor-job consumer READS the store with no provider named, DERIVING its data as-of from the calendar
//! ([`TradingCalendar::session_as_of_ts`]) for the scheduled session — NOT a caller-supplied timestamp —
//! so a caller cannot pair a session with a future as-of. The CONCRETE US-equity calendar that provides
//! the real [`SessionOrdinal`] ↔ epoch mapping (test calendars stand in), the REAL provider network
//! adapters (Databento / Sharadar, SRS-DATA-001/005), the live wall-clock NFR-P7 performance
//! verification over real securities, and the SYS-57 workload-priority admission remain their own
//! deferred owners, so SRS-FAC-001 stays `passes:false`.

use std::collections::HashSet;

use atp_types::{AssetClass, SecurityKey};

use crate::factor_analysis::{FactorAnalysisError, FactorObservation, FactorPanel, FactorPeriod};

/// The full US-equity universe floor SYS-32/33 names: a scheduled factor run must screen at
/// least this many securities to attest full-universe coverage. [`run_factor_job`] enforces it
/// as a HARD constant (not a caller config), so coverage cannot be weakened from outside.
pub const FULL_UNIVERSE_MIN: usize = 8_000;

/// The HARD platform floor on SCORED coverage, as a fraction of the screened universe. A run must
/// score at least this fraction to be a successful full-universe computation; the operator's
/// [`FactorJobConfig::min_scored_ratio`] can RAISE it but never lower it below this, so a config of
/// 0.0 cannot collapse the floor to a single security.
pub const MIN_SCORED_COVERAGE_RATIO: f64 = 0.5;

/// A trading-session ordinal (e.g. days since an epoch). The job resolves its schedule against
/// the trading calendar by session ordinal, NOT a wall clock -- so this module reads no system
/// or monotonic clock at all. The concrete ordinal <-> civil-date mapping is the caller's
/// (owned by the trading-calendar service).
pub type SessionOrdinal = u64;

/// An intraday instant as minutes from the start of the trading day (e.g. `570` = 09:30).
pub type MinutesOfDay = u32;

/// Read-only trading-calendar authority the factor job resolves its schedule against
/// (SyRS SYS-51: "the factor pipeline schedule shall resolve against the trading calendar").
///
/// Mirrors the VALUE-returning method surface of the Python `TradingCalendar` protocol that
/// strategy scheduling (SRS-SDK-002 / SyRS SYS-50) resolves against (`is_session -> bool`,
/// `session_open -> Option`, ...), so the factor pipeline and strategy scheduling share ONE
/// calendar contract rather than each inventing a clock (SYS-51). It is a read-only timing
/// *authority*: the job consults it, never mutates it, and holds no clock of its own. A `None`
/// from `session_open`/`session_close` means "this ordinal is not a trading session" -- it is NOT
/// a dependency-health signal: distinguishing a DEGRADED or STALE calendar (an unavailable
/// calendar service) from a legitimate non-session is the readiness / connectivity gates'
/// responsibility (SRS-ARCH-005 startup readiness, ERR-2 connectivity blocking), surfaced by the
/// concrete US-equity calendar SERVICE (NYSE/NASDAQ/CBOE sessions, early closes, DST) -- the
/// deferred owner this port is the seam for. This offline producer cannot tell a stale calendar
/// from a holiday, so it does not try; it resolves against whatever calendar it is given.
pub trait TradingCalendar {
    /// Whether `session` is a trading session (not a weekend or holiday).
    fn is_session(&self, session: SessionOrdinal) -> bool;

    /// The session-open instant for `session`, in minutes-of-day; `None` when `session` is
    /// not a trading session.
    fn session_open(&self, session: SessionOrdinal) -> Option<MinutesOfDay>;

    /// The session-close instant for `session`, in minutes-of-day; `None` when `session` is
    /// not a trading session.
    fn session_close(&self, session: SessionOrdinal) -> Option<MinutesOfDay>;

    /// Whether `session` closes early (e.g. a half day). Informational for the producer; it
    /// does not change schedule resolution but is part of the mirrored calendar contract.
    fn is_early_close(&self, session: SessionOrdinal) -> bool;

    /// The next trading session strictly after `session`; `None` when no session is found
    /// within the calendar's modeled horizon. Used to resolve the rebalance interval (so a
    /// daily run steps over weekends/holidays to the next session).
    fn next_session(&self, session: SessionOrdinal) -> Option<SessionOrdinal>;

    /// The POINT-IN-TIME as-of instant (epoch seconds) for `session`'s scheduled run — the
    /// `SessionOrdinal` ↔ epoch-second binding. `None` when `session` is not a trading session OR when
    /// this calendar does not provide the mapping (the default).
    ///
    /// This is the seam that lets a store-backed scheduled run DERIVE its data as-of from the calendar +
    /// session (so a caller cannot pair a session with an arbitrary future as-of):
    /// [`crate::store_inputs::run_scheduled_factor_job_over_store`] reads the data window's upper bound
    /// from here, NOT from a caller-supplied timestamp. The concrete US-equity calendar SERVICE owns the
    /// real session→civil-date/epoch mapping (the deferred owner, the same boundary as the rest of this
    /// port); the default returns `None`, so a calendar that does not implement it makes the store-backed
    /// run fail closed rather than run on an unbound as-of.
    fn session_as_of_ts(&self, session: SessionOrdinal) -> Option<i64> {
        let _ = session;
        None
    }
}

/// One security's market-data summary feeding the factor (SyRS SYS-32 "using market data").
/// Dimensionless `f64` features -- the factor domain, not money.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MarketFactorInput {
    /// Trailing total return over the factor's lookback window (a fraction, e.g. `0.12`).
    pub trailing_return: f64,
    /// Realized volatility over the lookback window (a non-negative fraction).
    pub realized_volatility: f64,
}

impl MarketFactorInput {
    fn is_finite(&self) -> bool {
        self.trailing_return.is_finite() && self.realized_volatility.is_finite()
    }
}

/// One security's fundamental-data summary feeding the factor (SyRS SYS-32 "and fundamental
/// data from the Phase 1 fundamental provider"). Dimensionless `f64` ratios -- the factor
/// domain, not money.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct FundamentalFactorInput {
    /// Earnings yield (earnings / price), a dimensionless ratio.
    pub earnings_yield: f64,
    /// Book-to-price, a dimensionless ratio.
    pub book_to_price: f64,
}

impl FundamentalFactorInput {
    fn is_finite(&self) -> bool {
        self.earnings_yield.is_finite() && self.book_to_price.is_finite()
    }
}

/// One security's inputs for a scheduled factor run: its identity plus BOTH a market summary
/// and a fundamental summary, each `Option` because a real universe always has securities
/// missing one source on a given session. A security missing EITHER is skipped (it cannot be
/// scored on both sources), never fabricated.
///
/// These values are CALLER-SUPPLIED. This offline producer validates what it can structurally
/// check (equity asset class, uniqueness, finite inputs, full-universe count) but it does NOT and
/// CANNOT attest that the universe is the TRUSTED session-versioned US-equity manifest or that the
/// market/fundamental values came from the real providers -- binding a run to a trusted,
/// session-versioned universe manifest and market/Sharadar source-provenance manifest is the
/// deferred SRS-DATA-001 (universe catalog) data layer's trust boundary -- the records are read
/// via the now-complete SRS-DATA-007 unified historical interface. A successful [`FactorScoreSet`] therefore certifies a correct
/// COMPUTATION over the inputs given, not the trustworthiness of those inputs (which the data
/// layer owns).
#[derive(Debug, Clone, PartialEq)]
pub struct SecurityFactorInputs {
    /// The security this row is for.
    pub security: SecurityKey,
    /// The market-data summary; `None` when the security has no usable market data.
    pub market: Option<MarketFactorInput>,
    /// The fundamental-data summary; `None` when the security has no usable fundamental data.
    pub fundamental: Option<FundamentalFactorInput>,
}

/// A user-defined factor (SyRS SYS-32 "user-defined factors"): given a security's market +
/// fundamental inputs it returns the factor score, or ABSTAINS (`None`) for a security it cannot
/// score -- which the job records as a skip, not a fabricated zero. The job only calls `compute`
/// when BOTH inputs are present, so a factor always sees both sources (the SYS-32 requirement).
///
/// `compute` takes `&self`, which Rust permits to carry interior mutability, so the trait cannot
/// *force* purity. The job calls `compute` in CANONICAL [`SecurityKey`] order (never caller-input
/// order), so a model that depends on call order still produces an output that is a pure function
/// of the input SET -- order-independent within a run. The crate's determinism guarantee
/// (identical inputs -> identical output ACROSS runs) is therefore CONDITIONAL on the model being
/// pure (no state that varies between `run_factor_job` calls); that purity is the caller's
/// contract, the same way a non-deterministic factor would make any downstream ranking
/// non-reproducible.
pub trait FactorModel {
    /// Compute the factor score from both sources, or abstain (`None`). Should be a pure function
    /// of its arguments (see the trait docs); the job invokes it in canonical security order.
    fn compute(
        &self,
        market: &MarketFactorInput,
        fundamental: &FundamentalFactorInput,
    ) -> Option<f64>;
}

/// A user-configured factor-job schedule, expressed RELATIVE to the session open so it must be
/// resolved against the trading calendar (SyRS SYS-51) rather than a wall clock.
///
/// SYS-32's example is "daily before market open": the run starts `start_minutes_before_open`
/// before the open and must complete by `deadline_minutes_before_open` before the open (the
/// user-configured deadline, NFR-P7). [`run_factor_job`] resolves these offsets against the
/// calendar's [`TradingCalendar::session_open`] for the session into concrete intraday
/// instants -- so a session with no resolvable open, or a lead that precedes the start of the
/// trading day, fails closed. The available compute window is
/// `start_minutes_before_open - deadline_minutes_before_open` minutes, which must be positive.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FactorJobSchedule {
    /// The trading session this run is scheduled for.
    pub session: SessionOrdinal,
    /// Minutes before the session open at which the run starts.
    pub start_minutes_before_open: MinutesOfDay,
    /// Minutes before the session open by which the run MUST complete (the deadline). Must be
    /// strictly less than `start_minutes_before_open` so the window is non-empty.
    pub deadline_minutes_before_open: MinutesOfDay,
}

/// A session-aware absolute instant: the trading session it falls on plus the minute-of-day
/// within it. Ordered session-major (then minute), so an instant on a LATER session is always
/// greater -- a run cannot be mistaken for on-time merely because its minute-of-day is small on a
/// later day. The fields are public so a caller can construct the run's actual time.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct Instant {
    /// The trading session this instant falls on.
    pub session: SessionOrdinal,
    /// The minute-of-day within `session`.
    pub minute: MinutesOfDay,
}

/// The ABSOLUTE time authority a run checks itself against, INJECTED so the deterministic core
/// reads no real wall clock of its own. [`run_factor_job`] reads it at the start (to reject a run
/// invoked before its scheduled start, or AFTER its deadline) and again after ranking and output
/// construction (to reject a run whose actual processing -- scoring, sorting, finalization --
/// pushed COMPLETION past the deadline), comparing the session-aware reading against the
/// calendar-resolved deadline [`Instant`] of the SCHEDULED session, not a relative budget. So a
/// late start (even on a later day), an early start, or a slow finalization is caught against the
/// real deadline, not assumed on-time.
///
/// Production injects a wall-clock-backed clock -- which lives in the runtime, OUTSIDE this
/// dependency-free core, so the real NFR-P7 wall-clock performance test is the deferred owner --
/// while tests inject a deterministic clock, so the run stays reproducible. The clock is the run's
/// timing AUTHORITY (like [`TradingCalendar`] is its scheduling authority).
pub trait Clock {
    /// The current session-aware instant. Must be non-decreasing across reads within a single run.
    fn now(&self) -> Instant;
}

/// The scored-coverage policy for a scheduled run. A factor run that scores too few securities is
/// not a successful full-universe computation (every security may be missing data or abstain), so
/// the run fails closed below this floor. The full-universe INPUT floor is NOT configurable -- it
/// is the hard platform constant [`FULL_UNIVERSE_MIN`] -- but the minimum SCORED coverage is an
/// explicit operator policy expressed as a FRACTION of the universe, so it scales with the
/// universe and a one-scored "success" cannot slip through.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct FactorJobConfig {
    /// The minimum FRACTION of the screened universe that must be SCORED (not skipped) for the run
    /// to be a successful full-universe computation. Must be a finite value in `[0, 1]` -- a
    /// non-finite or out-of-range ratio FAILS CLOSED ([`FactorJobError::InvalidCoverageRatio`]),
    /// never silently treated as permissive. The effective floor is the GREATER of this and the
    /// hard [`MIN_SCORED_COVERAGE_RATIO`], times the universe size (always at least 1) -- so a
    /// config of 0.0 cannot collapse the floor and a near-empty result is rejected rather than
    /// reported as a misleading full-universe ranking.
    pub min_scored_ratio: f64,
}

/// One security's computed factor score and its rank within the cross-section. Rank is
/// 1-based over the total order `(factor_value desc, SecurityKey asc)`: rank 1 is the highest
/// factor exposure, and the `SecurityKey` tiebreak keeps ranks deterministic on ties (SYS-32
/// "screen, rank, and compute").
#[derive(Debug, Clone, PartialEq)]
pub struct FactorScore {
    /// The security scored.
    pub security: SecurityKey,
    /// The dimensionless factor score.
    pub factor_value: f64,
    /// 1-based rank (1 = highest factor value).
    pub rank: u32,
}

/// Why a security in the universe was not scored. Recorded so a skipped security is an
/// auditable absence, never a fabricated score.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FactorSkipReason {
    /// The security had no market-data summary for the session.
    MissingMarketData,
    /// The security had a market summary but no fundamental summary.
    MissingFundamentalData,
    /// Both inputs were present but the factor abstained (`compute` returned `None`).
    FactorAbstained,
}

/// A security the run did not score, with the reason.
#[derive(Debug, Clone, PartialEq)]
pub struct SkippedSecurity {
    /// The security skipped.
    pub security: SecurityKey,
    /// Why it was skipped.
    pub reason: FactorSkipReason,
}

/// The ranked output of a completed scheduled factor run: the per-security scores (ranked) and
/// the skipped securities, stamped with the session.
#[derive(Debug, Clone, PartialEq)]
pub struct FactorScoreSet {
    /// The session this run computed factors for.
    pub session: SessionOrdinal,
    /// The scored securities, in ascending rank order (rank 1 first).
    pub scores: Vec<FactorScore>,
    /// The total size of the screened universe (scored + skipped).
    pub universe_size: usize,
    /// The securities skipped (with reasons); `universe_size = scores.len() + skipped.len()`.
    pub skipped: Vec<SkippedSecurity>,
}

/// The outcome of a scheduled factor run: its actual time (read from the injected [`Clock`])
/// either stayed within the calendar-resolved deadline INSTANT, or it crossed it (fail-closed --
/// no ranked set is produced for a run that started late or finished late).
///
/// The deadline is enforced against the ABSOLUTE resolved deadline instant, not a relative
/// budget: a run invoked after the deadline, or whose scoring + ranking + finalization completed
/// after it, is reported as exceeded rather than presented on-time. The gate is OBSERVATIONAL
/// (start + completion) -- it cannot preempt a hung synchronous model mid-call; supervised
/// termination of a hung model is the deferred runtime (NFR-P7 performance test + the orchestrator
/// container, SYS-57).
#[derive(Debug, Clone, PartialEq)]
pub enum FactorJobOutcome {
    /// The run started and completed at or before the deadline instant; carries the ranked scores.
    WithinDeadline(FactorScoreSet),
    /// The run started after, or completed after, the calendar-resolved deadline instant
    /// (NFR-P7). No ranked set is emitted -- the job refuses to present a late run as on-time.
    DeadlineExceeded {
        /// The session whose run crossed the deadline.
        session: SessionOrdinal,
        /// The calendar-resolved deadline instant (session + minute): session open minus the
        /// configured deadline lead, on the scheduled session.
        deadline: Instant,
        /// The session-aware clock reading that was found past the deadline.
        observed: Instant,
        /// Whether the deadline was already past at INVOCATION (a late start, including a run
        /// fired on a later session) versus crossed during processing/finalization.
        late_start: bool,
    },
}

/// One realized rebalance session feeding [`assemble_regular_panel`]: the session ordinal, the
/// session at which this period's forward-return window ENDS (its provenance), and the
/// cross-section of `(security, factor value, realized forward return)`. Unlike a live
/// [`FactorScoreSet`] (which has no forward return yet), this is a HISTORICAL session whose
/// forward window has elapsed, so the realized return is known.
///
/// `forward_window_end` is the period's declared forward window, which the assembler checks for
/// LABEL CONSISTENCY: it must be exactly the declared horizon of trading sessions out (resolved
/// through the calendar), so all periods share one calendar-valid horizon and a mislabeled or
/// mixed-horizon period is rejected. It does NOT prove the realized returns were computed over that
/// window -- that return provenance is the deferred (SRS-DATA-007 interface complete; real data = SRS-DATA-005 / SRS-FAC-001) data layer's trust boundary.
#[derive(Debug, Clone, PartialEq)]
pub struct RealizedFactorSession {
    /// The rebalance session ordinal (becomes the [`FactorPeriod`] timestamp).
    pub session: SessionOrdinal,
    /// The trading session at which this period's forward-return window ends. Must be exactly
    /// `forward_horizon_sessions` trading sessions after `session` (verified through the calendar).
    pub forward_window_end: SessionOrdinal,
    /// The cross-section of realized observations.
    pub observations: Vec<RealizedObservation>,
}

/// One security's realized observation for a [`RealizedFactorSession`].
#[derive(Debug, Clone, PartialEq)]
pub struct RealizedObservation {
    /// The security.
    pub security: SecurityKey,
    /// The factor score measured at the start of the period.
    pub factor_value: f64,
    /// The return realized over the period's forward window.
    pub forward_return: f64,
}

/// Why a scheduled factor run, or a panel assembly, failed closed. Every variant is localized
/// so the operator sees a precise reason rather than a generic failure.
#[derive(Debug, Clone, PartialEq)]
pub enum FactorJobError {
    /// The universe was empty -- nothing to screen.
    EmptyUniverse,
    /// The universe was smaller than the hard full-universe floor [`FULL_UNIVERSE_MIN`]
    /// (SYS-32/33) -- the run cannot attest full-universe coverage.
    UniverseBelowMinimum {
        /// The universe size supplied.
        actual: usize,
        /// The required floor ([`FULL_UNIVERSE_MIN`]).
        required: usize,
    },
    /// The configured [`FactorJobConfig::min_scored_ratio`] was outside `[0, 1]` (or non-finite),
    /// which would make the coverage floor meaningless; the run fails closed rather than
    /// silently treating an invalid policy as permissive.
    InvalidCoverageRatio {
        /// The offending ratio.
        ratio: f64,
    },
    /// The run scored fewer securities than the configured coverage floor (and always fails when
    /// none were scored): every security was missing data or the factor abstained, so the run
    /// computed too few factors to be a successful full-universe run.
    NoUsableCoverage {
        /// The number of securities actually scored.
        scored: usize,
        /// The total universe size screened.
        universe_size: usize,
        /// The minimum scored coverage required.
        required: usize,
    },
    /// The scheduled target day is not a trading session per the calendar, i.e. the calendar
    /// has no resolvable [`TradingCalendar::session_open`] for it (SyRS SYS-51).
    NotASession {
        /// The offending session ordinal.
        session: SessionOrdinal,
    },
    /// The schedule's lead offset precedes the start of the trading day: the session open minus
    /// `start_minutes_before_open` underflows minute 0, so the schedule cannot be resolved to a
    /// valid intraday start instant.
    ScheduleBeforeDayStart {
        /// The session being scheduled.
        session: SessionOrdinal,
        /// The session-open instant (minutes-of-day) resolved from the calendar.
        session_open: MinutesOfDay,
        /// The lead `start_minutes_before_open` that underflowed the day.
        lead: MinutesOfDay,
    },
    /// The schedule's compute window was empty or inverted (deadline not strictly after the
    /// start).
    EmptyScheduleWindow {
        /// `start_minutes_before_open`.
        start: MinutesOfDay,
        /// `deadline_minutes_before_open`.
        deadline: MinutesOfDay,
    },
    /// The run was invoked BEFORE its scheduled start instant -- the orchestrator fired it too
    /// early. It fails closed rather than running ahead of schedule.
    StartedBeforeScheduledStart {
        /// The scheduled start instant (session + minute).
        scheduled_start: Instant,
        /// The session-aware clock reading at invocation.
        observed: Instant,
    },
    /// The injected clock went BACKWARD between the start and completion reads (a regressing wall
    /// clock). The deadline cannot be trusted, so the run fails closed rather than letting an
    /// actually-late completion appear on-time.
    NonMonotonicClock {
        /// The clock reading at the start of the run.
        started: Instant,
        /// The earlier-or-equal reading observed at completion.
        observed: Instant,
    },
    /// A security in the universe was not an equity. The factor pipeline is a US-EQUITY universe
    /// (SyRS SYS-32); a non-equity key cannot be certified as part of a full-US-equity run.
    NonEquitySecurity {
        /// The offending symbol.
        symbol: String,
    },
    /// A security appeared more than once in the universe (or a panel period), which would
    /// double-count it in the ranking (or a quantile).
    DuplicateSecurity {
        /// The offending symbol.
        symbol: String,
    },
    /// A present market/fundamental input field was non-finite (NaN/inf).
    NonFiniteInput {
        /// The offending symbol.
        symbol: String,
    },
    /// The factor returned a non-finite score for a security, which must fail closed rather
    /// than poison a ranking.
    NonFiniteFactor {
        /// The offending symbol.
        symbol: String,
    },
    /// A panel assembly was handed no sessions.
    EmptyPanelInput,
    /// The declared forward-return horizon was zero sessions.
    InvalidHorizon,
    /// Two consecutive rebalance sessions were not separated by a constant number of trading
    /// sessions (resolved through the calendar) -- the panel is not REGULAR, so the
    /// tear-sheet's interval-dependent means would mix incomparable magnitudes.
    IrregularRebalanceInterval {
        /// The trading-session gap that was expected (from the first consecutive pair).
        expected: u32,
        /// The trading-session gap actually found.
        found: u32,
        /// The session at which the irregular gap began.
        at_session: SessionOrdinal,
    },
    /// The declared forward horizon exceeds the rebalance interval, so consecutive forward
    /// windows OVERLAP -- a compounded cumulative spread over the panel would double-count
    /// return. The producer must emit non-overlapping windows.
    OverlappingForwardWindows {
        /// The declared forward-return horizon, in trading sessions.
        horizon: u32,
        /// The rebalance interval, in trading sessions.
        interval: u32,
    },
    /// A period's DECLARED forward window is inconsistent with the panel's horizon: its
    /// `forward_window_end` is not exactly `forward_horizon_sessions` trading sessions after the
    /// period (resolved through the calendar). This is a LABEL-consistency check; verifying the
    /// returns were actually computed over that window is the deferred (SRS-DATA-007 interface complete; real data = SRS-DATA-005 / SRS-FAC-001) data layer.
    ForwardWindowMismatch {
        /// The period whose window was mislabeled.
        session: SessionOrdinal,
        /// The declared common horizon, in trading sessions.
        declared_horizon: u32,
        /// The actual trading-session gap from the period to its `forward_window_end`, or `None`
        /// when the end is not a reachable forward session.
        actual_gap: Option<u32>,
    },
    /// The assembled panel failed the [`FactorPanel`] trust boundary.
    Panel(FactorAnalysisError),
}

impl From<FactorAnalysisError> for FactorJobError {
    fn from(error: FactorAnalysisError) -> Self {
        Self::Panel(error)
    }
}

impl std::fmt::Display for FactorJobError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::EmptyUniverse => {
                write!(formatter, "SRS-FAC-001: factor-job universe was empty")
            }
            Self::UniverseBelowMinimum { actual, required } => write!(
                formatter,
                "SRS-FAC-001: universe of {actual} securities is below the full-universe floor \
                 of {required}"
            ),
            Self::InvalidCoverageRatio { ratio } => write!(
                formatter,
                "SRS-FAC-001: min_scored_ratio {ratio} is outside the valid [0, 1] range"
            ),
            Self::NoUsableCoverage {
                scored,
                universe_size,
                required,
            } => write!(
                formatter,
                "SRS-FAC-001: scored only {scored} of {universe_size} securities, below the \
                 required minimum coverage of {required}"
            ),
            Self::NotASession { session } => write!(
                formatter,
                "SRS-FAC-001: scheduled session {session} has no resolvable trading-session open \
                 (SyRS SYS-51)"
            ),
            Self::ScheduleBeforeDayStart {
                session,
                session_open,
                lead,
            } => write!(
                formatter,
                "SRS-FAC-001: session {session} lead of {lead} min precedes the day start (open \
                 at minute {session_open})"
            ),
            Self::NonMonotonicClock { started, observed } => write!(
                formatter,
                "SRS-FAC-001: clock went backward during the run (start {}/{}, completion {}/{})",
                started.session, started.minute, observed.session, observed.minute
            ),
            Self::EmptyScheduleWindow { start, deadline } => write!(
                formatter,
                "SRS-FAC-001: schedule window is empty (start {start} min before open is not \
                 after deadline {deadline} min before open)"
            ),
            Self::StartedBeforeScheduledStart {
                scheduled_start,
                observed,
            } => write!(
                formatter,
                "SRS-FAC-001: run invoked at session {}/minute {} is before the scheduled start \
                 session {}/minute {}",
                observed.session, observed.minute, scheduled_start.session, scheduled_start.minute
            ),
            Self::NonEquitySecurity { symbol } => write!(
                formatter,
                "SRS-FAC-001: security {symbol} is not an equity (the factor universe is US equities)"
            ),
            Self::DuplicateSecurity { symbol } => write!(
                formatter,
                "SRS-FAC-001: security {symbol} appeared more than once"
            ),
            Self::NonFiniteInput { symbol } => write!(
                formatter,
                "SRS-FAC-001: security {symbol} had a non-finite market/fundamental input"
            ),
            Self::NonFiniteFactor { symbol } => write!(
                formatter,
                "SRS-FAC-001: factor produced a non-finite score for security {symbol}"
            ),
            Self::EmptyPanelInput => {
                write!(formatter, "SRS-FAC-001: panel assembly had no sessions")
            }
            Self::InvalidHorizon => write!(
                formatter,
                "SRS-FAC-001: forward-return horizon must be at least one session"
            ),
            Self::IrregularRebalanceInterval {
                expected,
                found,
                at_session,
            } => write!(
                formatter,
                "SRS-FAC-001: irregular rebalance interval at session {at_session}: expected a \
                 {expected}-session gap, found {found}"
            ),
            Self::OverlappingForwardWindows { horizon, interval } => write!(
                formatter,
                "SRS-FAC-001: forward horizon {horizon} sessions exceeds the {interval}-session \
                 rebalance interval, so forward windows overlap"
            ),
            Self::ForwardWindowMismatch {
                session,
                declared_horizon,
                actual_gap,
            } => write!(
                formatter,
                "SRS-FAC-001: period {session} forward window spans {actual_gap:?} trading \
                 sessions, not the declared horizon of {declared_horizon}"
            ),
            Self::Panel(error) => {
                write!(formatter, "SRS-FAC-001: assembled panel invalid: {error}")
            }
        }
    }
}

impl std::error::Error for FactorJobError {}

/// The outcome of gating a run's START window against the injected clock, BEFORE any per-security work.
/// Returned by [`preflight_schedule`] so a caller that reads large inputs can fail fast.
///
/// CRATE-INTERNAL: this and [`preflight_schedule`] / [`run_factor_job_gated`] are the coordination
/// primitives that let the in-crate store wrapper preflight, assemble, then score with the SAME gate.
/// They are deliberately `pub(crate)`, NOT `pub` — exposing the scored core publicly would let an
/// external caller forge a `session` / `started` / `deadline` and BYPASS the schedule, deadline, and
/// coverage-ratio validation. External callers use [`run_factor_job`] or
/// [`crate::store_inputs::run_scheduled_factor_job_over_store`], which always preflight.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum StartGate {
    /// The run may proceed: the observed start instant and the calendar-resolved deadline instant it
    /// must complete before.
    Proceed {
        /// The start instant observed from the clock.
        started: Instant,
        /// The calendar-resolved deadline instant the run must complete before.
        deadline: Instant,
    },
    /// The run was invoked at/after its deadline (a late start, even on a later session): NO work
    /// should be done. The caller returns this [`FactorJobOutcome::DeadlineExceeded`].
    LateStart(FactorJobOutcome),
}

/// Validate the coverage policy, resolve the schedule against the trading calendar, and gate the START
/// window against the injected [`Clock`] — the preconditions that must hold BEFORE a run does any
/// per-security work (NFR-P7 / SYS-51). A caller that reads large inputs (the store-backed path) calls
/// this FIRST so a pre-start, non-session, or past-deadline run fails fast WITHOUT spending that work;
/// [`run_factor_job`] calls it too, so the start gate is identical whether the universe is
/// caller-supplied or store-assembled (no separate, drift-prone copy of the gate).
///
/// Returns [`StartGate::Proceed`] when the run may proceed, [`StartGate::LateStart`] (carrying the
/// fail-closed [`FactorJobOutcome::DeadlineExceeded`]) when invoked at/after the deadline, or a
/// fail-closed [`FactorJobError`] for an invalid coverage ratio, a non-session day, a lead before the
/// day start, an empty schedule window, or a run invoked before its scheduled start.
pub(crate) fn preflight_schedule<C, K>(
    schedule: &FactorJobSchedule,
    calendar: &C,
    config: &FactorJobConfig,
    clock: &K,
) -> Result<StartGate, FactorJobError>
where
    C: TradingCalendar,
    K: Clock,
{
    // The coverage policy must be a valid fraction. A non-finite or out-of-[0,1] ratio fails
    // closed -- it is never silently treated as a permissive (near-zero) floor.
    if !config.min_scored_ratio.is_finite() || !(0.0..=1.0).contains(&config.min_scored_ratio) {
        return Err(FactorJobError::InvalidCoverageRatio {
            ratio: config.min_scored_ratio,
        });
    }

    // Resolve the schedule against the trading calendar (SyRS SYS-51). session_open is the
    // resolution authority: it returns None for a non-session day, AND it gives the concrete
    // intraday open the before-open offsets are measured from -- so the schedule is genuinely
    // calendar-resolved, not merely "is it a session?".
    let session_open =
        calendar
            .session_open(schedule.session)
            .ok_or(FactorJobError::NotASession {
                session: schedule.session,
            })?;
    // Resolve the start instant: open minus the lead. A lead that precedes the day start cannot
    // resolve to a valid instant and fails closed.
    let start_at = session_open
        .checked_sub(schedule.start_minutes_before_open)
        .ok_or(FactorJobError::ScheduleBeforeDayStart {
            session: schedule.session,
            session_open,
            lead: schedule.start_minutes_before_open,
        })?;
    // The deadline instant is open minus the (smaller) deadline lead; it must be strictly after
    // the start instant so the run has a non-empty window. Both are session-aware instants on the
    // SCHEDULED session, so the clock comparisons cannot be fooled by a later day's minute-of-day.
    let deadline_minute = session_open.saturating_sub(schedule.deadline_minutes_before_open);
    if deadline_minute <= start_at {
        return Err(FactorJobError::EmptyScheduleWindow {
            start: schedule.start_minutes_before_open,
            deadline: schedule.deadline_minutes_before_open,
        });
    }
    let start_instant = Instant {
        session: schedule.session,
        minute: start_at,
    };
    let deadline_instant = Instant {
        session: schedule.session,
        minute: deadline_minute,
    };

    // Start-window gates (NFR-P7), against the SESSION-AWARE clock. The deadline minute is
    // EXCLUSIVE (end-of-minute semantics): the run must start at/after the start instant and reach
    // the deadline check at a minute STRICTLY before the deadline minute -- so a run still
    // executing during the deadline minute (which a minute-resolution clock would read AT the
    // deadline) is late, not on-time. A run invoked before its scheduled start fails closed; one
    // invoked at/after the deadline -- including on a later session -- fails closed.
    let started = clock.now();
    if started < start_instant {
        return Err(FactorJobError::StartedBeforeScheduledStart {
            scheduled_start: start_instant,
            observed: started,
        });
    }
    if started >= deadline_instant {
        return Ok(StartGate::LateStart(FactorJobOutcome::DeadlineExceeded {
            session: schedule.session,
            deadline: deadline_instant,
            observed: started,
            late_start: true,
        }));
    }
    Ok(StartGate::Proceed {
        started,
        deadline: deadline_instant,
    })
}

/// Run a scheduled full-universe factor job for one session: resolve the schedule against the
/// trading calendar (SYS-51), enforce the hard full-universe floor (SYS-32/33), compute the
/// user-defined factor over each security's market + fundamental inputs, rank the scored
/// cross-section, and gate on the calendar-resolved deadline INSTANT read from the injected
/// [`Clock`] (NFR-P7).
///
/// Returns [`FactorJobOutcome::WithinDeadline`] with the ranked set when the run both started and
/// completed at or before the resolved deadline instant, or [`FactorJobOutcome::DeadlineExceeded`]
/// (fail-closed) when it was invoked after the deadline (a late start) or its scoring + ranking +
/// finalization crossed it -- so a late run is caught against the ABSOLUTE deadline, not assumed
/// on-time. A run that scores fewer than [`FactorJobConfig::min_scored`] securities fails closed
/// ([`FactorJobError::NoUsableCoverage`]). Every precondition violation fails closed with a
/// localized [`FactorJobError`].
pub fn run_factor_job<C, M, K>(
    schedule: &FactorJobSchedule,
    calendar: &C,
    config: &FactorJobConfig,
    model: &M,
    clock: &K,
    universe: &[SecurityFactorInputs],
) -> Result<FactorJobOutcome, FactorJobError>
where
    C: TradingCalendar,
    M: FactorModel,
    K: Clock,
{
    // Validate the coverage policy, resolve the schedule, and gate the START window against the
    // injected clock BEFORE any per-security work (NFR-P7 / SYS-51) -- the same gate the store-backed
    // wrapper runs before reading the store, so a pre-start / non-session / past-deadline run never
    // does work. A late start returns DeadlineExceeded (fail-closed, no ranked set).
    let (started, deadline_instant) = match preflight_schedule(schedule, calendar, config, clock)? {
        StartGate::Proceed { started, deadline } => (started, deadline),
        StartGate::LateStart(outcome) => return Ok(outcome),
    };
    // The first observed `started` (and resolved deadline) is AUTHORITATIVE through scoring (and, for
    // the store-backed wrapper, through input assembly): run_factor_job_gated gates completion against
    // it, so a clock regression after the start read is caught, not lost by a fresh second start read.
    run_factor_job_gated(
        schedule.session,
        started,
        deadline_instant,
        config,
        model,
        clock,
        universe,
    )
}

/// The SCORED CORE of a factor run, run AFTER the start gate has already passed: it takes the
/// AUTHORITATIVE `started` and `deadline_instant` from a single [`preflight_schedule`] (NOT a fresh
/// clock read), enforces the full-universe floor (SYS-32/33), scores + ranks the cross-section, and
/// gates COMPLETION against that SAME `started` / `deadline_instant` (NFR-P7).
///
/// Threading the first observation through is what lets a caller do WORK between the start gate and the
/// scored core without losing timing integrity: the store-backed wrapper
/// ([`crate::store_inputs::run_scheduled_factor_job_over_store`]) preflights, ASSEMBLES the universe from
/// the store, then calls this core with the FIRST `started` — so a clock regression DURING assembly is
/// caught by the monotonic-clock guard (`completed < started`), which a second independent start read (a
/// fresh `clock.now()` after assembly) would silently lose. The coverage ratio is assumed already
/// validated by the preflight; an out-of-range / non-finite ratio still fails closed here (it can only
/// RAISE the required-coverage floor, never collapse it), so this core never fabricates a success.
#[allow(clippy::too_many_arguments)]
pub(crate) fn run_factor_job_gated<M, K>(
    session: SessionOrdinal,
    started: Instant,
    deadline_instant: Instant,
    config: &FactorJobConfig,
    model: &M,
    clock: &K,
    universe: &[SecurityFactorInputs],
) -> Result<FactorJobOutcome, FactorJobError>
where
    M: FactorModel,
    K: Clock,
{
    // Hard full-universe floor (SYS-32/33) -- the constant FULL_UNIVERSE_MIN, NOT a caller
    // config, so coverage cannot be weakened from outside.
    if universe.is_empty() {
        return Err(FactorJobError::EmptyUniverse);
    }
    if universe.len() < FULL_UNIVERSE_MIN {
        return Err(FactorJobError::UniverseBelowMinimum {
            actual: universe.len(),
            required: FULL_UNIVERSE_MIN,
        });
    }

    // Every security must be an EQUITY (the factor universe is US equities, SyRS SYS-32) and may
    // appear only once (a duplicate would double-count in the ranking). Detected in input order;
    // the final ranking is order-independent (it is re-sorted by the total order). NOTE: binding
    // the universe to a trusted, session-versioned US-equity MANIFEST (so an arbitrary set of
    // equities cannot be certified as "the" full universe) is the deferred SRS-DATA-001 catalog's
    // job; this gate enforces the asset class and uniqueness the surface can verify.
    let mut seen: HashSet<&SecurityKey> = HashSet::with_capacity(universe.len());
    for row in universe {
        if row.security.asset_class() != AssetClass::Equity {
            return Err(FactorJobError::NonEquitySecurity {
                symbol: row.security.symbol().to_string(),
            });
        }
        if !seen.insert(&row.security) {
            return Err(FactorJobError::DuplicateSecurity {
                symbol: row.security.symbol().to_string(),
            });
        }
    }

    // Score each security over BOTH sources (SYS-32). A missing source or an abstaining factor
    // is an auditable skip; a non-finite input or score fails closed. The model is invoked in
    // CANONICAL SecurityKey order (not input order), so even a FactorModel with interior mutability
    // sees the same sequence on every run -- the per-run output is a pure function of the input
    // SET, independent of input order. (Determinism ACROSS runs additionally requires the model to
    // be pure -- a model whose state spans run_factor_job calls is the caller's contract to honor.)
    let mut scan: Vec<&SecurityFactorInputs> = universe.iter().collect();
    scan.sort_by(|a, b| a.security.cmp(&b.security));
    let mut scored: Vec<(SecurityKey, f64)> = Vec::new();
    let mut skipped: Vec<SkippedSecurity> = Vec::new();
    for row in scan {
        if let Some(market) = &row.market {
            if !market.is_finite() {
                return Err(FactorJobError::NonFiniteInput {
                    symbol: row.security.symbol().to_string(),
                });
            }
        }
        if let Some(fundamental) = &row.fundamental {
            if !fundamental.is_finite() {
                return Err(FactorJobError::NonFiniteInput {
                    symbol: row.security.symbol().to_string(),
                });
            }
        }

        match (&row.market, &row.fundamental) {
            (None, _) => skipped.push(SkippedSecurity {
                security: row.security.clone(),
                reason: FactorSkipReason::MissingMarketData,
            }),
            (Some(_), None) => skipped.push(SkippedSecurity {
                security: row.security.clone(),
                reason: FactorSkipReason::MissingFundamentalData,
            }),
            (Some(market), Some(fundamental)) => match model.compute(market, fundamental) {
                None => skipped.push(SkippedSecurity {
                    security: row.security.clone(),
                    reason: FactorSkipReason::FactorAbstained,
                }),
                Some(value) if !value.is_finite() => {
                    return Err(FactorJobError::NonFiniteFactor {
                        symbol: row.security.symbol().to_string(),
                    });
                }
                Some(value) => scored.push((row.security.clone(), value)),
            },
        }
    }

    // Coverage guard (Codex-flagged): a run that scored too few securities is not a successful
    // full-universe computation, so it fails closed rather than emitting a thin ranking as a
    // success. The floor is a FRACTION of the universe (so it scales), and the effective fraction
    // is the GREATER of the operator's policy and the HARD platform minimum -- the operator can
    // require MORE coverage but a config of 0.0 cannot collapse the floor to a single security.
    let effective_ratio = config.min_scored_ratio.max(MIN_SCORED_COVERAGE_RATIO);
    let required_coverage = ((effective_ratio * universe.len() as f64).ceil() as usize).max(1);
    if scored.len() < required_coverage {
        return Err(FactorJobError::NoUsableCoverage {
            scored: scored.len(),
            universe_size: universe.len(),
            required: required_coverage,
        });
    }

    // Rank by the total order (factor_value desc, SecurityKey asc): every factor_value is
    // finite (guarded above), so the comparison is total.
    scored.sort_by(|(a_key, a_value), (b_key, b_value)| {
        b_value
            .partial_cmp(a_value)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a_key.cmp(b_key))
    });
    let scores = scored
        .into_iter()
        .enumerate()
        .map(|(index, (security, factor_value))| FactorScore {
            security,
            factor_value,
            rank: (index as u32) + 1,
        })
        .collect();

    // Sort the skipped securities by their key too, so the WHOLE output (scores AND skipped) is a
    // pure function of the input SET, independent of input order -- otherwise reversing an input
    // that contains skips would change the outcome, breaking the determinism claim.
    skipped.sort_by(|a, b| a.security.cmp(&b.security));

    let result = FactorScoreSet {
        session,
        scores,
        universe_size: universe.len(),
        skipped,
    };

    // COMPLETION gate (NFR-P7): re-read the clock AFTER scoring, ranking, AND output
    // construction. If the run crossed the deadline while doing that work, it fails closed --
    // finalization is inside the deadline, not excluded from it.
    let completed = clock.now();
    // The clock must be monotonic non-decreasing within a run. If it regressed (a backward wall
    // clock), the deadline check cannot be trusted, so the run fails closed rather than letting an
    // actually-late completion read as on-time.
    if completed < started {
        return Err(FactorJobError::NonMonotonicClock {
            started,
            observed: completed,
        });
    }
    if completed >= deadline_instant {
        return Ok(FactorJobOutcome::DeadlineExceeded {
            session,
            deadline: deadline_instant,
            observed: completed,
            late_start: false,
        });
    }

    Ok(FactorJobOutcome::WithinDeadline(result))
}

/// Count the trading sessions from `from` (exclusive) to `to` (inclusive) by stepping
/// [`TradingCalendar::next_session`]; `None` when `to` is not reachable as a forward session
/// within `cap` steps. Used to measure the rebalance interval in trading sessions.
fn trading_session_gap<C: TradingCalendar>(
    calendar: &C,
    from: SessionOrdinal,
    to: SessionOrdinal,
    cap: u32,
) -> Option<u32> {
    if to <= from {
        return None;
    }
    let mut current = from;
    for step in 1..=cap {
        match calendar.next_session(current) {
            Some(next) if next == to => return Some(step),
            Some(next) if next > to => return None,
            Some(next) => current = next,
            None => return None,
        }
    }
    None
}

/// The maximum number of trading-session steps [`assemble_regular_panel`] will walk to resolve
/// a single rebalance gap before giving up. A regular panel's interval is far below this; the
/// cap just bounds the calendar walk for a pathological input.
const MAX_REBALANCE_GAP_STEPS: u32 = 1_024;

/// Assemble a *regular* [`FactorPanel`] from realized per-rebalance sessions -- the SRS-BT-006
/// producer contract the tear-sheet defers to SRS-FAC-001.
///
/// The panel is regular iff: it has at least one session; the forward horizon is at least one
/// session; every session is a trading session; consecutive rebalance sessions are separated
/// by a CONSTANT number of trading sessions (resolved through `calendar` -- the consistent
/// rebalance interval the `mean_top`/`mean_bottom` aggregates require); and that interval is at
/// least the forward horizon, so consecutive forward windows do not overlap (the precondition a
/// compounded cumulative spread would need). The built panel is then validated at the
/// [`FactorPanel`] trust boundary, so a duplicate security, an empty or degenerate period, or a
/// non-finite observation also fails closed.
pub fn assemble_regular_panel<C: TradingCalendar>(
    calendar: &C,
    sessions: &[RealizedFactorSession],
    quantiles: usize,
    forward_horizon_sessions: u32,
) -> Result<FactorPanel, FactorJobError> {
    if sessions.is_empty() {
        return Err(FactorJobError::EmptyPanelInput);
    }
    if forward_horizon_sessions == 0 {
        return Err(FactorJobError::InvalidHorizon);
    }
    for session in sessions {
        if !calendar.is_session(session.session) {
            return Err(FactorJobError::NotASession {
                session: session.session,
            });
        }
        // Check forward-window LABEL CONSISTENCY: each period's DECLARED `forward_window_end` must
        // be exactly `forward_horizon_sessions` trading sessions out (resolved through the
        // calendar), so the panel's periods all declare the same, calendar-valid horizon and a
        // mislabeled or mixed-horizon period is rejected rather than silently averaged. This checks
        // the LABEL, not that the realized returns were actually computed over that window --
        // binding each return to a trusted query-window manifest is the deferred (SRS-DATA-007 interface complete; real data = SRS-DATA-005 / SRS-FAC-001) data
        // layer's trust boundary (the producer here consumes returns the data layer computed).
        let actual_gap = trading_session_gap(
            calendar,
            session.session,
            session.forward_window_end,
            MAX_REBALANCE_GAP_STEPS,
        );
        if actual_gap != Some(forward_horizon_sessions) {
            return Err(FactorJobError::ForwardWindowMismatch {
                session: session.session,
                declared_horizon: forward_horizon_sessions,
                actual_gap,
            });
        }
    }

    // The rebalance interval is the trading-session gap of the FIRST consecutive pair; every
    // later pair must match it (a constant interval), and it must be >= the forward horizon
    // (non-overlapping windows).
    let mut expected_gap: Option<u32> = None;
    for pair in sessions.windows(2) {
        let from = pair[0].session;
        let to = pair[1].session;
        let gap = trading_session_gap(calendar, from, to, MAX_REBALANCE_GAP_STEPS).ok_or(
            FactorJobError::IrregularRebalanceInterval {
                expected: expected_gap.unwrap_or(0),
                found: 0,
                at_session: to,
            },
        )?;
        match expected_gap {
            None => {
                if forward_horizon_sessions > gap {
                    return Err(FactorJobError::OverlappingForwardWindows {
                        horizon: forward_horizon_sessions,
                        interval: gap,
                    });
                }
                expected_gap = Some(gap);
            }
            Some(expected) if expected != gap => {
                return Err(FactorJobError::IrregularRebalanceInterval {
                    expected,
                    found: gap,
                    at_session: to,
                });
            }
            Some(_) => {}
        }
    }

    let periods = sessions
        .iter()
        .map(|session| {
            FactorPeriod::new(
                session.session,
                session
                    .observations
                    .iter()
                    .map(|observation| {
                        FactorObservation::new(
                            observation.security.clone(),
                            observation.factor_value,
                            observation.forward_return,
                        )
                    })
                    .collect(),
            )
        })
        .collect();
    let panel = FactorPanel::new(periods, quantiles);
    panel.validate()?;
    Ok(panel)
}

#[cfg(test)]
mod tests {
    use super::*;
    use atp_types::AssetClass;

    fn key(symbol: &str) -> SecurityKey {
        SecurityKey::new(symbol, AssetClass::Equity).expect("equity key")
    }

    /// A simple, deterministic calendar: every ordinal is a trading session, the open is 570
    /// (09:30) and close 960 (16:00), no early closes, and the next session is the next
    /// ordinal. Lets the unit tests resolve schedules without the deferred concrete calendar.
    struct DenseCalendar;
    impl TradingCalendar for DenseCalendar {
        fn is_session(&self, _session: SessionOrdinal) -> bool {
            true
        }
        fn session_open(&self, _session: SessionOrdinal) -> Option<MinutesOfDay> {
            Some(570)
        }
        fn session_close(&self, _session: SessionOrdinal) -> Option<MinutesOfDay> {
            Some(960)
        }
        fn is_early_close(&self, _session: SessionOrdinal) -> bool {
            false
        }
        fn next_session(&self, session: SessionOrdinal) -> Option<SessionOrdinal> {
            Some(session + 1)
        }
    }

    /// A calendar where only EVEN ordinals are sessions (odd ordinals model weekends/holidays),
    /// so `next_session` steps by two. Lets the tests exercise calendar-resolved gaps.
    struct EvenCalendar;
    impl TradingCalendar for EvenCalendar {
        fn is_session(&self, session: SessionOrdinal) -> bool {
            session % 2 == 0
        }
        fn session_open(&self, session: SessionOrdinal) -> Option<MinutesOfDay> {
            (session % 2 == 0).then_some(570)
        }
        fn session_close(&self, session: SessionOrdinal) -> Option<MinutesOfDay> {
            (session % 2 == 0).then_some(960)
        }
        fn is_early_close(&self, _session: SessionOrdinal) -> bool {
            false
        }
        fn next_session(&self, session: SessionOrdinal) -> Option<SessionOrdinal> {
            Some(if session % 2 == 0 {
                session + 2
            } else {
                session + 1
            })
        }
    }

    /// A momentum-minus-value factor: trailing return plus earnings yield, abstaining when
    /// volatility is exactly zero (a degenerate input the factor declines to score).
    struct DemoFactor;
    impl FactorModel for DemoFactor {
        fn compute(
            &self,
            market: &MarketFactorInput,
            fundamental: &FundamentalFactorInput,
        ) -> Option<f64> {
            if market.realized_volatility == 0.0 {
                return None;
            }
            Some(market.trailing_return + fundamental.earnings_yield)
        }
    }

    fn inputs(symbol: &str, ret: f64, vol: f64, ey: f64, bp: f64) -> SecurityFactorInputs {
        SecurityFactorInputs {
            security: key(symbol),
            market: Some(MarketFactorInput {
                trailing_return: ret,
                realized_volatility: vol,
            }),
            fundamental: Some(FundamentalFactorInput {
                earnings_yield: ey,
                book_to_price: bp,
            }),
        }
    }

    fn schedule(session: SessionOrdinal) -> FactorJobSchedule {
        FactorJobSchedule {
            session,
            start_minutes_before_open: 60,
            deadline_minutes_before_open: 5,
        }
    }

    fn cfg() -> FactorJobConfig {
        // Require at least half the universe scored (the coverage policy).
        FactorJobConfig {
            min_scored_ratio: 0.5,
        }
    }

    fn inst(session: SessionOrdinal, minute: MinutesOfDay) -> Instant {
        Instant { session, minute }
    }

    /// A deterministic clock fixed at a single session-aware instant. The default tests use a
    /// minute within the schedule(100) window (start 510, deadline 565).
    struct FixedClock(Instant);

    impl Clock for FixedClock {
        fn now(&self) -> Instant {
            self.0
        }
    }

    /// A clock that returns `start` on the first read and `finish` after -- models a run that
    /// begins at `start` and completes at `finish`, so the start/completion checks can be exercised
    /// independently. Deterministic (interior `Cell`, fixed sequence).
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

    /// A clock fixed on the scheduled session, within its window (start 510, deadline 565) -- the
    /// default for tests that do not exercise the deadline.
    fn clock() -> FixedClock {
        FixedClock(inst(100, 520))
    }

    fn build_universe(n: usize) -> Vec<SecurityFactorInputs> {
        (0..n)
            .map(|i| inputs(&format!("S{i:05}"), i as f64 * 0.001, 0.2, 0.05, 0.5))
            .collect()
    }

    #[test]
    fn ranks_full_universe_within_deadline() {
        let universe = build_universe(8_000);
        let outcome = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
            &DemoFactor,
            &clock(),
            &universe,
        )
        .expect("run");
        let FactorJobOutcome::WithinDeadline(set) = outcome else {
            panic!("expected completion");
        };
        assert_eq!(set.universe_size, 8_000);
        assert_eq!(set.scores.len(), 8_000);
        assert!(set.skipped.is_empty());
        // Highest trailing return (last symbol) ranks 1; ranks are a dense 1..=n.
        assert_eq!(set.scores[0].rank, 1);
        assert_eq!(set.scores[0].security.symbol(), "S07999");
        assert_eq!(set.scores.last().unwrap().rank, 8_000);
    }

    #[test]
    fn ranking_is_independent_of_input_order() {
        let mut forward = build_universe(8_000);
        let outcome_a = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
            &DemoFactor,
            &clock(),
            &forward,
        )
        .expect("run a");
        forward.reverse();
        let outcome_b = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
            &DemoFactor,
            &clock(),
            &forward,
        )
        .expect("run b");
        assert_eq!(outcome_a, outcome_b);
    }

    #[test]
    fn skips_missing_source_and_abstention_without_fabricating() {
        let mut universe = build_universe(8_000);
        universe[0].market = None; // missing market data
        universe[1].fundamental = None; // missing fundamental data
        universe[2].market = Some(MarketFactorInput {
            trailing_return: 0.1,
            realized_volatility: 0.0, // factor abstains
        });
        let FactorJobOutcome::WithinDeadline(set) = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
            &DemoFactor,
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
    fn rejects_universe_below_full_floor() {
        let universe = build_universe(7_999);
        let error = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
            &DemoFactor,
            &clock(),
            &universe,
        )
        .expect_err("below floor");
        assert_eq!(
            error,
            FactorJobError::UniverseBelowMinimum {
                actual: 7_999,
                required: 8_000,
            }
        );
    }

    #[test]
    fn rejects_empty_universe() {
        let error = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
            &DemoFactor,
            &clock(),
            &[],
        )
        .expect_err("empty");
        assert_eq!(error, FactorJobError::EmptyUniverse);
    }

    #[test]
    fn rejects_non_session_day() {
        let universe = build_universe(10);
        let error = run_factor_job(
            &schedule(101), // odd -> not a session on EvenCalendar
            &EvenCalendar,
            &cfg(),
            &DemoFactor,
            &clock(),
            &universe,
        )
        .expect_err("non-session");
        assert_eq!(error, FactorJobError::NotASession { session: 101 });
    }

    #[test]
    fn rejects_inverted_schedule_window() {
        let universe = build_universe(10);
        let bad = FactorJobSchedule {
            session: 100,
            start_minutes_before_open: 5,
            deadline_minutes_before_open: 60,
        };
        let error = run_factor_job(
            &bad,
            &DenseCalendar,
            &cfg(),
            &DemoFactor,
            &clock(),
            &universe,
        )
        .expect_err("inverted window");
        assert_eq!(
            error,
            FactorJobError::EmptyScheduleWindow {
                start: 5,
                deadline: 60,
            }
        );
    }

    #[test]
    fn rejects_late_start() {
        // Schedule deadline at minute 565 (open 570 - 5). Invoked at minute 600 on the scheduled
        // session, after the deadline -> fail-closed before any work, flagged as a late start.
        let universe = build_universe(8_000);
        let outcome = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
            &DemoFactor,
            &FixedClock(inst(100, 600)),
            &universe,
        )
        .expect("run");
        assert_eq!(
            outcome,
            FactorJobOutcome::DeadlineExceeded {
                session: 100,
                deadline: inst(100, 565),
                observed: inst(100, 600),
                late_start: true,
            }
        );
    }

    #[test]
    fn rejects_run_fired_on_a_later_session() {
        // A run for session 100 invoked on a LATER session (101), even at an early minute (520,
        // within session 100's window), is past the deadline -- the session-aware instant catches
        // it where a bare minute-of-day would not.
        let universe = build_universe(8_000);
        let outcome = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
            &DemoFactor,
            &FixedClock(inst(101, 520)),
            &universe,
        )
        .expect("run");
        assert!(matches!(
            outcome,
            FactorJobOutcome::DeadlineExceeded {
                late_start: true,
                observed: Instant {
                    session: 101,
                    minute: 520
                },
                ..
            }
        ));
    }

    #[test]
    fn rejects_early_start() {
        // The scheduled start is minute 510 (open 570 - 60). Invoked at 480 -> before the start
        // window, fail-closed (the orchestrator fired it early).
        let universe = build_universe(8_000);
        let error = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
            &DemoFactor,
            &FixedClock(inst(100, 480)),
            &universe,
        )
        .expect_err("early start");
        assert_eq!(
            error,
            FactorJobError::StartedBeforeScheduledStart {
                scheduled_start: inst(100, 510),
                observed: inst(100, 480),
            }
        );
    }

    #[test]
    fn reports_deadline_exceeded_on_late_finalization() {
        // The run starts at 560 (within the 565 deadline) but finalization completes at 570 --
        // ranking/output construction pushed it past the deadline, fail-closed.
        let universe = build_universe(8_000);
        let outcome = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
            &DemoFactor,
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
    fn deadline_outcome_depends_on_clock_not_universe_size() {
        // The SAME 8,000 universe yields opposite outcomes purely from the injected clock -- the
        // deadline is the ABSOLUTE resolved instant, not a function of the universe size.
        let universe = build_universe(8_000);
        let within = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
            &DemoFactor,
            &FixedClock(inst(100, 520)),
            &universe,
        )
        .expect("fast run");
        assert!(matches!(within, FactorJobOutcome::WithinDeadline(_)));
        let exceeded = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
            &DemoFactor,
            &StartFinishClock::new(inst(100, 560), inst(100, 580)),
            &universe,
        )
        .expect("slow run");
        assert!(matches!(
            exceeded,
            FactorJobOutcome::DeadlineExceeded {
                late_start: false,
                ..
            }
        ));
    }

    #[test]
    fn stateful_model_output_is_order_independent() {
        // A model with interior mutability whose score depends on CALL ORDER (a running counter)
        // still yields an order-independent output, because the job scores in canonical key order.
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
        let mut universe = build_universe(8_000);
        let forward = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
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
            &DenseCalendar,
            &cfg(),
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
        // Determinism with skips (Codex finding): a universe containing skipped securities yields
        // the SAME outcome (scores AND skipped) regardless of input order, because both are sorted.
        let mut universe = build_universe(8_000);
        universe[0].market = None;
        universe[1].fundamental = None;
        universe[2].market = Some(MarketFactorInput {
            trailing_return: 0.1,
            realized_volatility: 0.0,
        });
        let forward = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
            &DemoFactor,
            &clock(),
            &universe,
        )
        .expect("forward");
        universe.reverse();
        let reversed = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
            &DemoFactor,
            &clock(),
            &universe,
        )
        .expect("reversed");
        assert_eq!(forward, reversed);
    }

    #[test]
    fn fails_closed_on_no_usable_coverage() {
        // Every security has zero volatility, so DemoFactor abstains on all of them -- nothing is
        // scored, so the run fails closed rather than emitting an empty "successful" ranking.
        let universe: Vec<SecurityFactorInputs> = (0..8_000)
            .map(|i| inputs(&format!("S{i:05}"), 0.1, 0.0, 0.05, 0.5))
            .collect();
        let error = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
            &DemoFactor,
            &clock(),
            &universe,
        )
        .expect_err("no coverage");
        assert_eq!(
            error,
            FactorJobError::NoUsableCoverage {
                scored: 0,
                universe_size: 8_000,
                required: 4_000, // ceil(0.5 * 8_000)
            }
        );
    }

    #[test]
    fn fails_closed_on_thin_coverage() {
        // Only a handful scored (below the 50% floor) -> not a successful full-universe run.
        let mut universe = build_universe(8_000);
        for row in universe.iter_mut().skip(3) {
            row.market = None; // all but the first 3 are skipped
        }
        let error = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
            &DemoFactor,
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
        // A negative ratio is not a valid coverage policy -> fail closed (not silently permissive).
        let universe = build_universe(8_000);
        let bad = FactorJobConfig {
            min_scored_ratio: -0.5,
        };
        let error = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &bad,
            &DemoFactor,
            &clock(),
            &universe,
        )
        .expect_err("invalid ratio");
        assert_eq!(error, FactorJobError::InvalidCoverageRatio { ratio: -0.5 });
        // ...and a non-finite ratio likewise.
        let nan = FactorJobConfig {
            min_scored_ratio: f64::NAN,
        };
        assert!(matches!(
            run_factor_job(
                &schedule(100),
                &DenseCalendar,
                &nan,
                &DemoFactor,
                &clock(),
                &universe
            )
            .expect_err("nan ratio"),
            FactorJobError::InvalidCoverageRatio { .. }
        ));
    }

    #[test]
    fn coverage_floor_cannot_collapse_below_platform_minimum() {
        // A config ratio of 0.0 must NOT drop the floor to one security -- the hard platform
        // minimum (0.5) still requires 4,000 of 8,000 scored.
        let mut universe = build_universe(8_000);
        for row in universe.iter_mut().skip(1) {
            row.market = None; // only the first security scores
        }
        let zero = FactorJobConfig {
            min_scored_ratio: 0.0,
        };
        let error = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &zero,
            &DemoFactor,
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
        // The clock starts at 560 (within window) but regresses to 500 at completion -> the
        // deadline cannot be trusted, fail closed (a backward wall clock).
        let universe = build_universe(8_000);
        let error = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
            &DemoFactor,
            &StartFinishClock::new(inst(100, 560), inst(100, 500)),
            &universe,
        )
        .expect_err("backward clock");
        assert_eq!(
            error,
            FactorJobError::NonMonotonicClock {
                started: inst(100, 560),
                observed: inst(100, 500),
            }
        );
    }

    #[test]
    fn rejects_duplicate_security() {
        let mut universe = build_universe(8_000);
        universe[5].security = universe[4].security.clone();
        let error = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
            &DemoFactor,
            &clock(),
            &universe,
        )
        .expect_err("dup");
        assert!(matches!(error, FactorJobError::DuplicateSecurity { .. }));
    }

    #[test]
    fn fails_closed_on_non_finite_input() {
        let mut universe = build_universe(8_000);
        universe[3].market = Some(MarketFactorInput {
            trailing_return: f64::NAN,
            realized_volatility: 0.2,
        });
        let error = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
            &DemoFactor,
            &clock(),
            &universe,
        )
        .expect_err("nan input");
        assert!(matches!(error, FactorJobError::NonFiniteInput { .. }));
    }

    #[test]
    fn fails_closed_on_non_finite_factor() {
        struct InfFactor;
        impl FactorModel for InfFactor {
            fn compute(
                &self,
                _market: &MarketFactorInput,
                _fundamental: &FundamentalFactorInput,
            ) -> Option<f64> {
                Some(f64::INFINITY)
            }
        }
        let universe = build_universe(8_000);
        let error = run_factor_job(
            &schedule(100),
            &DenseCalendar,
            &cfg(),
            &InfFactor,
            &clock(),
            &universe,
        )
        .expect_err("inf factor");
        assert!(matches!(error, FactorJobError::NonFiniteFactor { .. }));
    }

    fn realized(symbol: &str, factor: f64, fwd: f64) -> RealizedObservation {
        RealizedObservation {
            security: key(symbol),
            factor_value: factor,
            forward_return: fwd,
        }
    }

    /// `forward_window_end` is the session the period's forward window ends on; pass it explicitly
    /// so each test states its horizon provenance.
    fn realized_session(
        session: SessionOrdinal,
        forward_window_end: SessionOrdinal,
    ) -> RealizedFactorSession {
        RealizedFactorSession {
            session,
            forward_window_end,
            observations: vec![
                realized("AAA", 1.0, 0.01),
                realized("BBB", 2.0, 0.02),
                realized("CCC", 3.0, 0.03),
                realized("DDD", 4.0, 0.04),
            ],
        }
    }

    #[test]
    fn assembles_regular_panel_for_tear_sheet() {
        // DenseCalendar horizon 1 -> each forward window ends on the next session.
        let sessions = vec![
            realized_session(100, 101),
            realized_session(101, 102),
            realized_session(102, 103),
        ];
        let panel = assemble_regular_panel(&DenseCalendar, &sessions, 2, 1).expect("panel");
        assert_eq!(panel.periods.len(), 3);
        assert_eq!(panel.quantiles, 2);
        assert_eq!(panel.periods[0].ts, 100);
        panel.validate().expect("regular panel validates");
    }

    #[test]
    fn assembles_regular_panel_over_calendar_gaps() {
        // EvenCalendar: 100, 102, 104 are consecutive sessions (gap 1); horizon 1 ends on the next
        // trading session (102, 104, 106).
        let sessions = vec![
            realized_session(100, 102),
            realized_session(102, 104),
            realized_session(104, 106),
        ];
        let panel = assemble_regular_panel(&EvenCalendar, &sessions, 2, 1).expect("panel");
        assert_eq!(panel.periods.len(), 3);
    }

    #[test]
    fn rejects_irregular_rebalance_interval() {
        // Gaps 1 then 2 trading sessions -> not a constant interval (forward windows all valid).
        let sessions = vec![
            realized_session(100, 101),
            realized_session(101, 102),
            realized_session(103, 104),
        ];
        let error = assemble_regular_panel(&DenseCalendar, &sessions, 2, 1).expect_err("irregular");
        assert!(matches!(
            error,
            FactorJobError::IrregularRebalanceInterval { .. }
        ));
    }

    #[test]
    fn rejects_overlapping_forward_windows() {
        // Rebalance interval 1 session, horizon 3 sessions -> windows overlap.
        let sessions = vec![realized_session(100, 103), realized_session(101, 104)];
        let error = assemble_regular_panel(&DenseCalendar, &sessions, 2, 3).expect_err("overlap");
        assert_eq!(
            error,
            FactorJobError::OverlappingForwardWindows {
                horizon: 3,
                interval: 1,
            }
        );
    }

    #[test]
    fn rejects_forward_window_mismatch() {
        // The period claims horizon 1 but its forward window ends 3 sessions out -> mislabeled.
        let sessions = vec![realized_session(100, 103), realized_session(101, 102)];
        let error =
            assemble_regular_panel(&DenseCalendar, &sessions, 2, 1).expect_err("window mismatch");
        assert_eq!(
            error,
            FactorJobError::ForwardWindowMismatch {
                session: 100,
                declared_horizon: 1,
                actual_gap: Some(3),
            }
        );
    }

    #[test]
    fn rejects_empty_panel_input_and_zero_horizon() {
        assert_eq!(
            assemble_regular_panel(&DenseCalendar, &[], 2, 1).expect_err("empty"),
            FactorJobError::EmptyPanelInput
        );
        let sessions = vec![realized_session(100, 101)];
        assert_eq!(
            assemble_regular_panel(&DenseCalendar, &sessions, 2, 0).expect_err("horizon"),
            FactorJobError::InvalidHorizon
        );
    }

    #[test]
    fn propagates_panel_trust_boundary_failure() {
        // A period with a duplicate security must fail closed via FactorPanel::validate.
        let mut bad = realized_session(100, 101);
        bad.observations.push(realized("AAA", 9.0, 0.09));
        let sessions = vec![bad];
        let error =
            assemble_regular_panel(&DenseCalendar, &sessions, 2, 1).expect_err("dup in period");
        assert!(matches!(error, FactorJobError::Panel(_)));
    }

    #[test]
    fn rejects_non_session_in_panel() {
        let sessions = vec![realized_session(101, 102)]; // odd -> not a session on EvenCalendar
        let error =
            assemble_regular_panel(&EvenCalendar, &sessions, 2, 1).expect_err("non-session");
        assert_eq!(error, FactorJobError::NotASession { session: 101 });
    }
}
