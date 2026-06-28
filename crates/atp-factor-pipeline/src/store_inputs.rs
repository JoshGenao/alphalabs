//! SRS-DATA-007 / SRS-FAC-001 factor-job store loader — source a factor's inputs from the unified store.
//!
//! The SRS-DATA-007 acceptance criterion names "strategy code, backtests, **factor jobs**, and notebooks"
//! as the consumers that "query by symbol, date range, and resolution **without specifying the original
//! source provider**." This module is the factor pipeline's reader over that interface: given a security
//! and a window it reads the security's series from the durable [`MarketDataStore`] through the
//! source-neutral SRS-DATA-007 query path ([`MarketDataStore::query_unified`] raw /
//! [`MarketDataStore::query_split_adjusted`] gated) and derives the dimensionless factor inputs that
//! [`crate::factor_job::run_factor_job`] scores — the market half ([`load_daily_market_input`] →
//! [`MarketFactorInput`]: `trailing_return`, `realized_volatility`) and the fundamental half
//! ([`load_fundamental_input`] → [`crate::factor_job::FundamentalFactorInput`]: `earnings_yield`,
//! `book_to_price`).
//!
//! ## Scope — the factor-job READ over the unified store (a primitive, not a schedule-bound close)
//!
//! [`load_daily_market_input`] and [`load_fundamental_input`] read a security's inputs through the
//! source-neutral SRS-DATA-007 path, and [`assemble_factor_inputs`] combines them into the
//! [`crate::factor_job::SecurityFactorInputs`] cross-section — so the named SRS-DATA-007 *factor-job*
//! consumer queries by symbol / date range / resolution with NO provider named (the read surface for
//! factor jobs). These loaders are SAFE point-in-time primitives: given a correct `as_of_ts` they never
//! consume data dated/filed after it.
//!
//! [`run_scheduled_factor_job_over_store`] composes that read with the schedule gate. Its data as-of is
//! DERIVED from the calendar — [`crate::factor_job::TradingCalendar::session_as_of_ts`]`(schedule.session)`
//! — NOT a caller-supplied timestamp, so a caller CANNOT pair a session with an arbitrary future as-of:
//! the whole data window is bound to the scheduled session, and the point-in-time loaders consume no
//! data after it. What remains deferred is the CONCRETE US-equity calendar that provides the real
//! `SessionOrdinal` ↔ epoch `session_as_of_ts` mapping (the same boundary as the rest of the calendar
//! port — test calendars stand in), plus the REAL provider network adapters (Databento / Sharadar,
//! SRS-DATA-001/005) and the live wall-clock NFR-P7 performance harness — so SRS-FAC-001 stays
//! `passes:false`; fixture-sourced store data stands in, exactly as the verification step permits.
//!
//! ## Domain & fail-closed boundary
//!
//! `trailing_return` and `realized_volatility` are dimensionless `f64` factor features (the factor
//! domain, not money), consistent with the rest of the crate. Every value is finite by construction:
//! the loader fails closed ([`FactorInputError::NonPositiveClose`]) on any non-positive close before a
//! ratio is formed, so each per-bar return divides by a positive prior close and the features cannot be
//! `NaN`/`inf`. A window with fewer than two bars is an *auditable absence* (`Ok(None)`) — the factor
//! job records it as a [`crate::factor_job::FactorSkipReason::MissingMarketData`] skip, never a
//! fabricated score. The split-adjusted basis is served ONLY behind proven SRS-DATA-011 coverage; an
//! uncovered query fails closed ([`FactorInputError::CoverageNotProven`]) rather than deriving a factor
//! from a raw series mislabeled as adjusted (which would corrupt a return spanning a corporate action).
//!
//! `earnings_yield` and `book_to_price` are the same kind of dimensionless `f64` ratio, derived at read
//! time as the integer quotient of two stored minor-unit fields (so the store holds NO `f64` and there
//! is no lossy ratio encoding — exactly the market loader's discipline). The denominator
//! (`market_value_minor`) must be positive or the loader fails closed
//! ([`FactorInputError::NonPositiveMarketValue`]); the numerators (`net_income_minor`,
//! `book_equity_minor`) may legitimately be negative (a loss-making or negative-book-value security),
//! so once the denominator is positive both ratios are finite by construction. A security with NO
//! AVAILABLE fundamental record as of the run date is an *auditable absence* (`Ok(None)` →
//! [`crate::factor_job::FactorSkipReason::MissingFundamentalData`]); a record PRESENT but missing a
//! required field is malformed and fails closed ([`FactorInputError::MissingFundamentalField`]).
//!
//! The fundamental read is also **point-in-time correct** (no lookahead bias): a statement is selected
//! by its `available_ts` (filing instant), NOT its fiscal `event_ts` (period end), so a run never
//! consumes a statement that was not yet filed on the run date — see [`load_fundamental_input`].

use atp_data::query::UnifiedHistoricalQuery;
use atp_data::store::{DatasetKind, MarketDataRecord, MarketDataStore};
use atp_types::SecurityKey;

use crate::factor_job::{
    preflight_schedule, run_factor_job_gated, Clock, FactorJobConfig, FactorJobError,
    FactorJobOutcome, FactorJobSchedule, FactorModel, FundamentalFactorInput, MarketFactorInput,
    SecurityFactorInputs, StartGate, TradingCalendar,
};

/// The vendor-neutral resolution label for the key-ratio fundamental snapshot a factor reads. The
/// factor-input fields (`net_income_minor`, `book_equity_minor`, `market_value_minor`) are the raw line
/// items SRS-DATA-005's "key ratio records" carry, plus an `available_ts` field — the AVAILABILITY
/// (filing) instant the record became knowable, distinct from the natural-key `event_ts` (the fiscal
/// period end). Point-in-time selection gates on `available_ts` to avoid lookahead bias (see
/// [`load_fundamental_input`]). Naming a resolution is NOT naming Sharadar (the adapter layer maps
/// provider → kind, SRS-ARCH-003).
pub const FUNDAMENTAL_RATIOS_RESOLUTION: &str = "fundamental:ratios";

/// The normalized basis a factor sources its market inputs on. Returns spanning a corporate action are
/// only correct on the split-comparable basis, so [`MarketInputBasis::SplitAdjusted`] is the honest
/// default for a factor; [`MarketInputBasis::Raw`] is available for completeness.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MarketInputBasis {
    /// Stored closes verbatim.
    Raw,
    /// The POINT-IN-TIME split-comparable basis: served ONLY behind proven SRS-DATA-011 coverage (an
    /// uncovered query fails closed), and adjusted for splits effective at/before the window's as-of
    /// date ONLY — a split effective AFTER the run date is not applied, so a future corporate action
    /// cannot bias a historical factor input (via [`atp_data::store::MarketDataStore::query_split_adjusted_as_of`]).
    SplitAdjusted,
}

/// A fail-closed error sourcing a factor's market input from the unified store. Every condition fails
/// closed rather than fabricating a factor feature.
#[derive(Debug, Clone, PartialEq)]
pub enum FactorInputError {
    /// The requested window was inverted (`start_ts > end_ts`). A bad lookback / range construction
    /// must fail closed rather than fall through to an empty query result that a caller would misread as
    /// "no market data" — silently dropping the security from the factor run and masking the
    /// scheduling / range-construction bug.
    InvalidWindow {
        /// The requested inclusive lower bound (epoch seconds).
        start_ts: i64,
        /// The requested inclusive upper bound (epoch seconds) — found to be `< start_ts`.
        end_ts: i64,
    },
    /// A queried close was non-positive, so a return ratio is undefined — fail closed rather than
    /// produce a `NaN`/`inf` factor feature.
    NonPositiveClose {
        /// The security symbol.
        symbol: String,
        /// The offending bar's event timestamp.
        event_ts: i64,
        /// The non-positive close (integer minor units).
        close_minor: i64,
    },
    /// A queried daily-equity bar was missing its `close` field, so no price could be read.
    MissingClose {
        /// The security symbol.
        symbol: String,
        /// The bar's event timestamp.
        event_ts: i64,
    },
    /// The split-adjusted basis was refused because corporate-action coverage is not proven through the
    /// window end (SRS-DATA-011); the loader never falls back to a raw series dressed up as adjusted.
    /// `reason` carries the underlying coverage diagnostic (it names SRS-DATA-011).
    CoverageNotProven {
        /// The security symbol.
        symbol: String,
        /// The underlying coverage diagnostic.
        reason: String,
    },
    /// A fundamental record was present in the window but missing a field the factor input requires, so
    /// no ratio could be derived — fail closed rather than fabricate a fundamental feature.
    MissingFundamentalField {
        /// The security symbol.
        symbol: String,
        /// The fundamental record's event timestamp.
        event_ts: i64,
        /// The required field name that was absent.
        field: &'static str,
    },
    /// The fundamental ratio denominator (`market_value_minor`) was non-positive, so the
    /// `earnings_yield` / `book_to_price` ratios are undefined — fail closed rather than produce a
    /// `NaN`/`inf` factor feature. (The numerators may legitimately be negative; only the denominator
    /// must be positive.)
    NonPositiveMarketValue {
        /// The security symbol.
        symbol: String,
        /// The fundamental record's event timestamp.
        event_ts: i64,
        /// The non-positive market value (integer minor units).
        market_value_minor: i64,
    },
    /// A fundamental record's availability (filing) instant precedes its fiscal `event_ts` (period
    /// end), which is impossible for real provenance — a statement cannot be filed before its period
    /// ends. Fail closed rather than trust corrupt availability metadata that could mask a lookahead.
    AvailabilityBeforePeriodEnd {
        /// The security symbol.
        symbol: String,
        /// The fundamental record's fiscal period-end timestamp.
        event_ts: i64,
        /// The (corrupt) availability/filing timestamp found to be `< event_ts`.
        available_ts: i64,
    },
}

impl std::fmt::Display for FactorInputError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidWindow { start_ts, end_ts } => write!(
                f,
                "SRS-DATA-007: factor market-input window is inverted (start_ts {start_ts} > end_ts \
                 {end_ts})"
            ),
            Self::NonPositiveClose {
                symbol,
                event_ts,
                close_minor,
            } => write!(
                f,
                "SRS-DATA-007: factor market input for {symbol} has a non-positive close \
                 {close_minor} at ts {event_ts}"
            ),
            Self::MissingClose { symbol, event_ts } => write!(
                f,
                "SRS-DATA-007: factor market input for {symbol} bar at ts {event_ts} has no close field"
            ),
            Self::CoverageNotProven { symbol, reason } => write!(
                f,
                "SRS-DATA-007: factor market input for {symbol} could not use the split-adjusted basis: \
                 {reason}"
            ),
            Self::MissingFundamentalField {
                symbol,
                event_ts,
                field,
            } => write!(
                f,
                "SRS-DATA-007: factor fundamental input for {symbol} record at ts {event_ts} has no \
                 {field} field"
            ),
            Self::NonPositiveMarketValue {
                symbol,
                event_ts,
                market_value_minor,
            } => write!(
                f,
                "SRS-DATA-007: factor fundamental input for {symbol} has a non-positive market value \
                 {market_value_minor} at ts {event_ts}"
            ),
            Self::AvailabilityBeforePeriodEnd {
                symbol,
                event_ts,
                available_ts,
            } => write!(
                f,
                "SRS-DATA-007: factor fundamental input for {symbol} has availability {available_ts} \
                 before its fiscal period end {event_ts} (corrupt provenance)"
            ),
        }
    }
}

impl std::error::Error for FactorInputError {}

/// Source a security's daily [`MarketFactorInput`] from the unified store over the inclusive
/// `[start_ts, end_ts]` event-timestamp window, on `basis`. Queries by symbol / date range / resolution
/// with NO provider named (`DailyEquityBar`, resolution `1d`).
///
/// Returns `Ok(None)` when the window holds fewer than two closes (insufficient history to form a
/// return — an auditable skip), `Ok(Some(_))` with the derived dimensionless features otherwise, or a
/// fail-closed [`FactorInputError`].
pub fn load_daily_market_input(
    store: &MarketDataStore,
    security: &SecurityKey,
    start_ts: i64,
    end_ts: i64,
    basis: MarketInputBasis,
) -> Result<Option<MarketFactorInput>, FactorInputError> {
    // Fail closed on an inverted window BEFORE querying: the unified query treats start_ts > end_ts as a
    // (valid) empty result, which would fall through to Ok(None) and be misread as "no market data" --
    // silently dropping the security and masking a bad lookback / range-construction bug.
    if start_ts > end_ts {
        return Err(FactorInputError::InvalidWindow { start_ts, end_ts });
    }
    let symbol = security.symbol();
    let query = UnifiedHistoricalQuery::new(symbol, "1d", start_ts, end_ts)
        .with_kind(DatasetKind::DailyEquityBar);

    let closes: Vec<(i64, i64)> = match basis {
        MarketInputBasis::Raw => {
            let result = store.query_unified(&query);
            collect_closes(symbol, result.records().iter().map(|r| &**r))?
        }
        MarketInputBasis::SplitAdjusted => {
            // POINT-IN-TIME split adjustment: query_split_adjusted_as_of applies only splits effective
            // at/before the window end (the as-of date), NOT through the coverage frontier -- so a split
            // effective AFTER the run date cannot re-base the historical window (no lookahead bias).
            let result = store.query_split_adjusted_as_of(&query).map_err(|err| {
                FactorInputError::CoverageNotProven {
                    symbol: symbol.to_string(),
                    reason: err.to_string(),
                }
            })?;
            collect_closes(symbol, result.records.iter())?
        }
    };

    derive_market_input(symbol, &closes)
}

/// Extract the `(event_ts, close_minor)` series (already `event_ts`-ascending from the unified query),
/// failing closed on a bar missing its `close` field.
fn collect_closes<'r>(
    symbol: &str,
    records: impl Iterator<Item = &'r MarketDataRecord>,
) -> Result<Vec<(i64, i64)>, FactorInputError> {
    let mut closes = Vec::new();
    for record in records {
        let event_ts = record.key().event_ts;
        let close_minor = record
            .fields()
            .iter()
            .find(|field| field.name == "close")
            .map(|field| field.value_minor)
            .ok_or_else(|| FactorInputError::MissingClose {
                symbol: symbol.to_string(),
                event_ts,
            })?;
        closes.push((event_ts, close_minor));
    }
    Ok(closes)
}

/// Derive the dimensionless market features from an `event_ts`-ascending close series.
///
/// `trailing_return = (last - first) / first`; `realized_volatility` is the population standard
/// deviation of the per-bar simple returns `(c_i - c_{i-1}) / c_{i-1}`. Fewer than two closes is an
/// auditable absence (`Ok(None)`). Every close must be positive (else the ratio is undefined) — once
/// that holds, both features are finite by construction.
fn derive_market_input(
    symbol: &str,
    closes: &[(i64, i64)],
) -> Result<Option<MarketFactorInput>, FactorInputError> {
    if closes.len() < 2 {
        return Ok(None);
    }
    for &(event_ts, close_minor) in closes {
        if close_minor <= 0 {
            return Err(FactorInputError::NonPositiveClose {
                symbol: symbol.to_string(),
                event_ts,
                close_minor,
            });
        }
    }

    let first = closes.first().expect("len >= 2").1 as f64;
    let last = closes.last().expect("len >= 2").1 as f64;
    let trailing_return = (last - first) / first;

    let returns: Vec<f64> = closes
        .windows(2)
        .map(|pair| {
            let prev = pair[0].1 as f64;
            let cur = pair[1].1 as f64;
            (cur - prev) / prev
        })
        .collect();
    let n = returns.len() as f64;
    let mean = returns.iter().sum::<f64>() / n;
    let variance = returns.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / n;
    let realized_volatility = variance.sqrt();

    Ok(Some(MarketFactorInput {
        trailing_return,
        realized_volatility,
    }))
}

/// Source a security's [`FundamentalFactorInput`] from the unified store **point-in-time** as of
/// `as_of_ts`: the statement for the latest fiscal period that was actually AVAILABLE (filed) by the run
/// date. Queries by symbol / resolution with NO provider named (`Fundamental` kind, resolution
/// [`FUNDAMENTAL_RATIOS_RESOLUTION`]).
///
/// ## No lookahead bias (point-in-time correctness)
///
/// A fundamental record's natural-key `event_ts` is its fiscal PERIOD END (per the store), NOT the date
/// it became knowable — a Dec-31 statement is often filed weeks later. Selecting by period end alone
/// would let a January run consume a Dec-31 statement filed in February, contaminating the ranking /
/// backtest with **lookahead bias**. So each record carries a separate `available_ts` field (the
/// filing / availability instant), and this loader selects the latest-period statement whose
/// `available_ts <= as_of_ts` — a statement is never used before it was knowable. Stamping the real
/// availability date (e.g. Sharadar SF1 `datekey`) is the ingestion layer's job (deferred SRS-DATA-005);
/// this loader enforces the point-in-time GATE.
///
/// Because availability is bounded above by `as_of_ts` and (validated) `available_ts >= event_ts`, every
/// usable statement has `event_ts <= as_of_ts`, so the query is bounded to `[0, as_of_ts]` by period end.
/// The lookup spans ALL history below `as_of_ts` — it is NOT bounded by any market lookback start, since
/// the in-force statement may predate a price window (a 10-K before a 1-year return window).
///
/// Returns `Ok(None)` when no AVAILABLE fundamental record exists at/before `as_of_ts` (an auditable
/// absence — the factor job records it as a
/// [`crate::factor_job::FactorSkipReason::MissingFundamentalData`] skip), `Ok(Some(_))` with the derived
/// dimensionless ratios otherwise, or a fail-closed [`FactorInputError`] (missing field, non-positive
/// denominator, or corrupt `available_ts < event_ts` provenance).
pub fn load_fundamental_input(
    store: &MarketDataStore,
    security: &SecurityKey,
    as_of_ts: i64,
) -> Result<Option<FundamentalFactorInput>, FactorInputError> {
    // event_ts is always >= 0 (store-validated), so no statement can exist at/before a pre-epoch as-of
    // date -- an auditable absence, never an inverted-query artifact.
    if as_of_ts < 0 {
        return Ok(None);
    }
    let symbol = security.symbol();
    // Lower bound 0 (the minimum valid event_ts) so the query spans all history up to as_of_ts. Any
    // statement AVAILABLE by as_of_ts has available_ts >= event_ts (validated below), so its period end
    // is <= as_of_ts and it is inside this range -- no available statement is missed.
    let query = UnifiedHistoricalQuery::new(symbol, FUNDAMENTAL_RATIOS_RESOLUTION, 0, as_of_ts)
        .with_kind(DatasetKind::Fundamental);
    let result = store.query_unified(&query);
    // Records are event_ts-ascending. The point-in-time in-force statement is the one for the latest
    // fiscal period (largest event_ts) whose availability is at/before the run date -- so iterate in
    // order and keep the last record that passes the availability gate.
    let mut chosen: Option<&MarketDataRecord> = None;
    for record in result.records() {
        let record: &MarketDataRecord = record;
        let event_ts = record.key().event_ts;
        let available_ts = read_fundamental_field(record, symbol, "available_ts")?;
        // A statement cannot be filed before its fiscal period ends -- corrupt provenance fails closed.
        if available_ts < event_ts {
            return Err(FactorInputError::AvailabilityBeforePeriodEnd {
                symbol: symbol.to_string(),
                event_ts,
                available_ts,
            });
        }
        // Lookahead gate: a statement not yet available at the run date was not knowable then -- skip it.
        if available_ts <= as_of_ts {
            chosen = Some(record);
        }
    }
    match chosen {
        None => Ok(None),
        Some(record) => derive_fundamental_input(symbol, record).map(Some),
    }
}

/// Read a required integer minor-unit field from a fundamental record, failing closed if it is absent.
fn read_fundamental_field(
    record: &MarketDataRecord,
    symbol: &str,
    field: &'static str,
) -> Result<i64, FactorInputError> {
    record
        .fields()
        .iter()
        .find(|f| f.name == field)
        .map(|f| f.value_minor)
        .ok_or_else(|| FactorInputError::MissingFundamentalField {
            symbol: symbol.to_string(),
            event_ts: record.key().event_ts,
            field,
        })
}

/// Derive the dimensionless fundamental ratios from one as-of fundamental record.
///
/// `earnings_yield = net_income_minor / market_value_minor` and
/// `book_to_price = book_equity_minor / market_value_minor` — each the integer quotient of two stored
/// minor-unit fields formed at read time (no `f64` in storage, no lossy ratio encoding). The denominator
/// `market_value_minor` must be positive (else the ratios are undefined) and fails closed otherwise; the
/// numerators may legitimately be negative. Once the denominator is positive both ratios are finite.
fn derive_fundamental_input(
    symbol: &str,
    record: &MarketDataRecord,
) -> Result<FundamentalFactorInput, FactorInputError> {
    let net_income_minor = read_fundamental_field(record, symbol, "net_income_minor")?;
    let book_equity_minor = read_fundamental_field(record, symbol, "book_equity_minor")?;
    let market_value_minor = read_fundamental_field(record, symbol, "market_value_minor")?;
    fundamental_ratios(
        symbol,
        record.key().event_ts,
        net_income_minor,
        book_equity_minor,
        market_value_minor,
    )
}

/// The pure ratio math: form the two dimensionless ratios from integer minor-unit line items, failing
/// closed on a non-positive denominator. Split out so the numeric contract is unit-testable without a
/// store record.
fn fundamental_ratios(
    symbol: &str,
    event_ts: i64,
    net_income_minor: i64,
    book_equity_minor: i64,
    market_value_minor: i64,
) -> Result<FundamentalFactorInput, FactorInputError> {
    if market_value_minor <= 0 {
        return Err(FactorInputError::NonPositiveMarketValue {
            symbol: symbol.to_string(),
            event_ts,
            market_value_minor,
        });
    }
    let denominator = market_value_minor as f64;
    Ok(FundamentalFactorInput {
        earnings_yield: net_income_minor as f64 / denominator,
        book_to_price: book_equity_minor as f64 / denominator,
    })
}

/// Assemble the [`SecurityFactorInputs`] cross-section a scheduled factor run scores, by reading BOTH
/// the market ([`load_daily_market_input`]) and fundamental ([`load_fundamental_input`]) halves for each
/// security over the inclusive `[start_ts, end_ts]` window from the unified store — by symbol / date
/// range / resolution, with NO provider named (the SRS-DATA-007 factor-job consumer).
///
/// The market half is read over the inclusive `[start_ts, end_ts]` lookback (for the trailing return /
/// volatility); the fundamental half is the as-of statement at/before `end_ts` (the run date), which may
/// predate `start_ts` because fundamentals are periodic. A `None` market or fundamental input is a
/// legitimate auditable absence carried through to [`run_factor_job`] (which records the corresponding
/// skip); a structural problem (inverted market window, missing field, non-positive close/denominator,
/// uncovered split-adjusted basis) fails closed. The securities are returned in input order; the job
/// re-sorts into canonical order, so the outcome is order-independent.
///
/// Each per-security read goes through the store's INDEXED path
/// ([`MarketDataStore::query_unified`] with a named kind, backed by
/// [`atp_data::store::MarketDataStore::records_for`]'s binary search), so a full-universe assembly of
/// `N` securities over a store of `M` records is `O(N * (log M + per-series matches))`, NOT
/// `O(N * M)` — a single linear scan per read would not scale to a realistic multi-year store.
pub fn assemble_factor_inputs(
    store: &MarketDataStore,
    securities: &[SecurityKey],
    start_ts: i64,
    end_ts: i64,
    basis: MarketInputBasis,
) -> Result<Vec<SecurityFactorInputs>, FactorInputError> {
    let mut rows = Vec::with_capacity(securities.len());
    for security in securities {
        let market = load_daily_market_input(store, security, start_ts, end_ts, basis)?;
        // Fundamental as-of the run date (end_ts), spanning all history below it -- not bounded by the
        // market lookback start, since the latest statement may predate the price window.
        let fundamental = load_fundamental_input(store, security, end_ts)?;
        rows.push(SecurityFactorInputs {
            security: security.clone(),
            market,
            fundamental,
        });
    }
    Ok(rows)
}

/// A fail-closed error running a scheduled factor job over the unified store: either assembling the
/// inputs failed ([`FactorInputError`]) or the job itself failed ([`FactorJobError`]).
#[derive(Debug, Clone, PartialEq)]
pub enum StoreFactorJobError {
    /// Assembling the cross-section from the store failed closed.
    Input(FactorInputError),
    /// The scheduled factor job failed closed.
    Job(FactorJobError),
}

impl std::fmt::Display for StoreFactorJobError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Input(error) => write!(f, "SRS-FAC-001: factor-input assembly failed: {error}"),
            Self::Job(error) => write!(f, "SRS-FAC-001: scheduled factor job failed: {error}"),
        }
    }
}

impl std::error::Error for StoreFactorJobError {}

/// Run a scheduled full-universe factor job whose inputs are sourced from the unified store: assemble
/// the [`SecurityFactorInputs`] cross-section for `securities` — market features over the inclusive
/// `[as_of_ts - market_lookback_seconds, as_of_ts]` window and the fundamental statement in force *as
/// of* `as_of_ts` ([`assemble_factor_inputs`]) — then run the scored core over it. This is the concrete
/// SRS-DATA-007 *factor-job* consumer (reads by symbol / date range / resolution, no provider named) and
/// the SRS-FAC-001 store-backed execution path (full-universe floor, calendar-resolved schedule,
/// injected-clock deadline).
///
/// ## Point-in-time as-of — DERIVED from the calendar + session (not caller-forgeable)
///
/// The run's point-in-time instant `as_of_ts` is **derived** from the trading calendar via
/// [`TradingCalendar::session_as_of_ts`]`(schedule.session)`, NOT taken as a caller-supplied timestamp —
/// so a caller **cannot** pair `schedule.session` with an arbitrary future as-of. NO market or
/// fundamental data after `as_of_ts` is consumed: the market window ends at `as_of_ts` and the
/// fundamental availability gate ([`load_fundamental_input`]) excludes any statement filed after it.
/// `market_lookback_seconds` (clamped `>= 0`) is a RELATIVE lookback length, so the whole window is
/// bound to the scheduled session.
///
/// The caller supplies only the relative lookback and the calendar; the `SessionOrdinal` ↔ epoch-second
/// mapping that yields `as_of_ts` lives in the calendar port. A calendar that does not implement
/// `session_as_of_ts` (the default `None`) makes the run fail closed (`NotASession`) rather than run on
/// an unbound as-of — the concrete US-equity calendar SERVICE that provides the real mapping is the
/// deferred owner (the same boundary as the rest of the [`TradingCalendar`] port).
#[allow(clippy::too_many_arguments)]
pub fn run_scheduled_factor_job_over_store<C, M, K>(
    store: &MarketDataStore,
    securities: &[SecurityKey],
    market_lookback_seconds: i64,
    basis: MarketInputBasis,
    schedule: &FactorJobSchedule,
    calendar: &C,
    config: &FactorJobConfig,
    model: &M,
    clock: &K,
) -> Result<FactorJobOutcome, StoreFactorJobError>
where
    C: TradingCalendar,
    M: FactorModel,
    K: Clock,
{
    // Gate the schedule / START window against the clock BEFORE reading the store, so a pre-start,
    // non-session, or past-deadline run fails fast WITHOUT spending the (potentially large) assembly
    // work -- the scheduled-execution boundary is enforced for the whole path, not just after assembly.
    let (started, deadline_instant) = match preflight_schedule(schedule, calendar, config, clock)
        .map_err(StoreFactorJobError::Job)?
    {
        StartGate::Proceed { started, deadline } => (started, deadline),
        StartGate::LateStart(outcome) => return Ok(outcome),
    };
    // DERIVE the point-in-time as-of instant from the calendar + scheduled session (NOT a caller
    // timestamp), so the data window is bound to the schedule and a future as-of cannot be forged. A
    // calendar without the session->epoch mapping fails closed rather than running on an unbound as-of.
    let as_of_ts = calendar
        .session_as_of_ts(schedule.session)
        .ok_or(StoreFactorJobError::Job(FactorJobError::NotASession {
            session: schedule.session,
        }))?;
    // The data window ends at `as_of_ts`: the market lookback is [as_of_ts - lookback, as_of_ts] and the
    // fundamental is the statement available as of `as_of_ts` -- so no record dated/filed after the run's
    // as-of instant is consumed (no lookahead). The lookback is clamped >= 0 (a negative lookback would
    // extend the window into the future); a zero/short window yields too-few-bars skips, never a fabrication.
    let market_lookback_start_ts = as_of_ts.saturating_sub(market_lookback_seconds.max(0));
    let universe =
        assemble_factor_inputs(store, securities, market_lookback_start_ts, as_of_ts, basis)
            .map_err(StoreFactorJobError::Input)?;
    // Feed the SCORED CORE with the FIRST observed `started`/`deadline` (NOT a fresh start read after
    // assembly), so the monotonic-clock guard (`completed < started`) catches a clock regression that
    // happened DURING assembly -- a second independent start gate would lose it.
    run_factor_job_gated(
        schedule.session,
        started,
        deadline_instant,
        config,
        model,
        clock,
        &universe,
    )
    .map_err(StoreFactorJobError::Job)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fewer_than_two_closes_is_an_auditable_absence() {
        assert_eq!(derive_market_input("AAPL", &[]).unwrap(), None);
        assert_eq!(derive_market_input("AAPL", &[(1, 10_000)]).unwrap(), None);
    }

    #[test]
    fn derives_finite_dimensionless_features() {
        // Closes 100, 120, 120 ($1.00, $1.20, $1.20 in minor units): trailing (12000-10000)/10000 = 0.2;
        // per-bar returns [0.2, 0.0], population std = 0.1.
        let input = derive_market_input("AAPL", &[(1, 10_000), (2, 12_000), (3, 12_000)])
            .unwrap()
            .expect("two+ closes");
        assert!((input.trailing_return - 0.2).abs() < 1e-12);
        assert!((input.realized_volatility - 0.1).abs() < 1e-12);
    }

    #[test]
    fn non_positive_close_fails_closed() {
        let err = derive_market_input("AAPL", &[(1, 10_000), (2, 0)]).unwrap_err();
        assert_eq!(
            err,
            FactorInputError::NonPositiveClose {
                symbol: "AAPL".to_string(),
                event_ts: 2,
                close_minor: 0,
            }
        );
    }

    #[test]
    fn fundamental_ratios_are_finite_dimensionless_quotients() {
        // net_income 250_000 / market_value 5_000_000 = 0.05; book_equity 1_000_000 / 5_000_000 = 0.2.
        let input = fundamental_ratios("AAPL", 2, 250_000, 1_000_000, 5_000_000)
            .expect("positive denominator yields ratios");
        assert!((input.earnings_yield - 0.05).abs() < 1e-12);
        assert!((input.book_to_price - 0.2).abs() < 1e-12);
    }

    #[test]
    fn negative_numerators_are_legitimate() {
        // A loss-making, negative-book-value security: only the denominator must be positive.
        let input = fundamental_ratios("AAPL", 2, -250_000, -1_000_000, 5_000_000)
            .expect("negative numerators are valid");
        assert!((input.earnings_yield - (-0.05)).abs() < 1e-12);
        assert!((input.book_to_price - (-0.2)).abs() < 1e-12);
    }

    #[test]
    fn non_positive_market_value_fails_closed() {
        let err = fundamental_ratios("AAPL", 2, 250_000, 1_000_000, 0).unwrap_err();
        assert_eq!(
            err,
            FactorInputError::NonPositiveMarketValue {
                symbol: "AAPL".to_string(),
                event_ts: 2,
                market_value_minor: 0,
            }
        );
    }
}
