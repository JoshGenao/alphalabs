//! SRS-DATA-007 factor-job market-input loader — source a factor's market inputs from the unified store.
//!
//! The acceptance criterion names "strategy code, backtests, **factor jobs**, and notebooks" as the
//! consumers that "query by symbol, date range, and resolution **without specifying the original source
//! provider**." [`load_daily_market_input`] is the factor pipeline's market-input PRIMITIVE over that
//! interface: given a security and a window it reads the security's daily close series from the durable
//! [`MarketDataStore`] through the source-neutral SRS-DATA-007 query path
//! ([`MarketDataStore::query_unified`] raw / [`MarketDataStore::query_split_adjusted`] gated) and derives
//! the dimensionless [`MarketFactorInput`] (`trailing_return`, `realized_volatility`) that
//! [`crate::factor_job::run_factor_job`] scores.
//!
//! ## Scope (honest) — substrate, not the close
//!
//! This is SHIPPED product code (`src/`), but it is the market-input *primitive*, **not yet invoked by**
//! [`crate::factor_job::run_factor_job`] (which still takes a caller-supplied
//! [`crate::factor_job::SecurityFactorInputs`] slice). Wiring this loader into the factor-job EXECUTION
//! path is deferred — and even then a complete scored run needs the **fundamental** half, which stays the
//! deferred Sharadar provider (SRS-DATA-005). So this proves a factor job *can query its market inputs*
//! by symbol / date range / resolution with no provider named, NOT that the named factor-job consumer is
//! wired end to end: SRS-DATA-007 (its factor-job consumer) and SRS-FAC-001 both stay `passes:false`.
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

use atp_data::query::UnifiedHistoricalQuery;
use atp_data::store::{DatasetKind, MarketDataRecord, MarketDataStore};
use atp_types::SecurityKey;

use crate::factor_job::MarketFactorInput;

/// The normalized basis a factor sources its market inputs on. Returns spanning a corporate action are
/// only correct on the split-comparable basis, so [`MarketInputBasis::SplitAdjusted`] is the honest
/// default for a factor; [`MarketInputBasis::Raw`] is available for completeness.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MarketInputBasis {
    /// Stored closes verbatim.
    Raw,
    /// The split-comparable basis, served ONLY behind proven SRS-DATA-011 coverage; an uncovered query
    /// fails closed.
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
            let result =
                store
                    .query_split_adjusted(&query)
                    .map_err(|err| FactorInputError::CoverageNotProven {
                        symbol: symbol.to_string(),
                        reason: err.to_string(),
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
}
