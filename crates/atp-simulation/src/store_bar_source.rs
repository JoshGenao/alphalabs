//! SRS-DATA-007 backtest consumer — a real [`BarSource`] over the unified historical store.
//!
//! The acceptance criterion (docs/SRS.md SRS-DATA-007) names "strategy code, **backtests**, factor
//! jobs, and notebooks" as the consumers that "query by symbol, date range, and resolution **without
//! specifying the original source provider**." [`StoreBarSource`] is the backtest engine's wiring of
//! that interface: it is the concrete system-catalog reader the [`BacktestDataSource::SystemData`]
//! seam named, reading the durable [`MarketDataStore`] (the catalog SRS-DATA-016 persists) through the
//! source-neutral SRS-DATA-007 query path — [`MarketDataStore::query_unified`] for raw bars and the
//! coverage-gated [`MarketDataStore::query_split_adjusted`] for the split-comparable basis. It is
//! SHIPPED product code (`src/`), not a test stand-in, so the backtest engine is a real named consumer
//! of the unified interface.
//!
//! ## Source-neutral by construction
//!
//! [`StoreBarSource`] builds a [`UnifiedHistoricalQuery`] from only the three acceptance dimensions —
//! symbol, the inclusive `[start, end]` event-timestamp range, and resolution (carried by the equity-bar
//! [`DatasetKind`]). There is no provider / vendor / source parameter anywhere on the path, and a
//! [`MarketDataRecord`] carries no origin field, so a backtest is structurally unable to name or branch
//! on where a bar was ingested from.
//!
//! ## Fail-closed trust boundary
//!
//! Every conversion at the data boundary fails closed rather than coercing corrupt data:
//!
//! * The backtest window bounds are `u64`; the query takes signed epoch seconds. A bound above
//!   `i64::MAX` is unrepresentable and yields [`BacktestError::SourceUnavailable`] — never a wrap to a
//!   negative timestamp that would silently empty the query and masquerade as [`BacktestError::EmptyData`].
//! * A record's `event_ts` (`i64`) is converted to the bar `ts` (`u64`) fail-closed (store validation
//!   already rejects a negative `event_ts`, but the conversion stays honest against a hand-built record).
//! * A stored bar missing its `close` field fails closed rather than fabricating a price.
//! * [`Normalization::SplitAdjusted`] is served ONLY behind proven SRS-DATA-011 coverage: an uncovered
//!   query maps the [`CoverageError`](atp_data::CoverageError) to [`BacktestError::SourceUnavailable`]
//!   (the error's `Display` names SRS-DATA-011) — the engine never substitutes raw bars for a refused
//!   adjusted read.
//!
//! ## Bounded read
//!
//! Per the [`BarSource`] contract the source bounds its own read: it counts the matching records with an
//! allocation-free streaming pass over the in-memory store and fails closed with
//! [`BacktestError::TooManyBars`] BEFORE materializing any result -- so neither the borrowed
//! `query_unified` result nor the OWNED re-quoted `query_split_adjusted` record set is ever allocated for
//! an oversized window. The unified query is itself range-filtered, so the returned set is the exact
//! in-window set (no superset), and a kind-narrowed equity query cannot carry duplicate `event_ts` (equity
//! bars have no `option_contract` variation), so the engine's duplicate-bar guard never trips on
//! legitimate store contents.

use atp_data::query::UnifiedHistoricalQuery;
use atp_data::store::{DatasetKind, MarketDataRecord, MarketDataStore};

use crate::backtest::{BacktestBar, BacktestDataSource, BacktestError, BarSource, DateRange};

/// The normalized basis a backtest reads its bars on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Normalization {
    /// Stored values verbatim — e.g. an options strategy that must reason about raw prices.
    Raw,
    /// The split-comparable basis, served ONLY behind proven SRS-DATA-011 coverage
    /// ([`MarketDataStore::query_split_adjusted`]); an uncovered query fails closed.
    SplitAdjusted,
}

/// A backtest [`BarSource`] reading the platform's stored historical catalog through the source-neutral
/// SRS-DATA-007 unified query. See the module docs for the source-neutrality and fail-closed guarantees.
#[derive(Debug, Clone)]
pub struct StoreBarSource<'a> {
    store: &'a MarketDataStore,
    /// The equity-bar kind to query (the resolution's vendor-neutral taxonomy tag).
    kind: DatasetKind,
    /// The bar resolution to match (`1d` / `1m`).
    resolution: String,
    /// The normalized basis to read.
    normalization: Normalization,
}

impl<'a> StoreBarSource<'a> {
    /// A daily-equity-bar source (`1d` / [`DatasetKind::DailyEquityBar`]).
    pub fn daily(store: &'a MarketDataStore, normalization: Normalization) -> Self {
        Self {
            store,
            kind: DatasetKind::DailyEquityBar,
            resolution: "1d".to_string(),
            normalization,
        }
    }

    /// A minute-equity-bar source (`1m` / [`DatasetKind::MinuteEquityBar`]).
    pub fn minute(store: &'a MarketDataStore, normalization: Normalization) -> Self {
        Self {
            store,
            kind: DatasetKind::MinuteEquityBar,
            resolution: "1m".to_string(),
            normalization,
        }
    }

    /// Map a (pre-bounded) range of stored records onto [`BacktestBar`]s. The `max_bars` bound is
    /// enforced up front in [`Self::bars`] before any result is materialized, so `count` here is already
    /// `<= max_bars` and serves only as the allocation capacity hint. `symbol` is used only for
    /// fail-closed diagnostics.
    fn map_records<'r>(
        &self,
        symbol: &str,
        records: impl Iterator<Item = &'r MarketDataRecord>,
        count: usize,
    ) -> Result<Vec<BacktestBar>, BacktestError> {
        let mut bars = Vec::with_capacity(count);
        for record in records {
            let event_ts = record.key().event_ts;
            let ts = u64::try_from(event_ts).map_err(|_| BacktestError::SourceUnavailable {
                reason: format!(
                    "stored bar for {symbol} carries a negative event timestamp {event_ts}"
                ),
            })?;
            let close_minor = record
                .fields()
                .iter()
                .find(|field| field.name == "close")
                .map(|field| field.value_minor)
                .ok_or_else(|| BacktestError::SourceUnavailable {
                    reason: format!("stored bar for {symbol} at ts {ts} has no close field"),
                })?;
            bars.push(BacktestBar {
                symbol: record.key().symbol.clone(),
                ts,
                close_minor,
                // Daily / minute equity bars carry no observed bid-ask spread; the engine's default
                // spread-impact model falls back to a fixed fraction of notional when it is `None`.
                spread_minor: None,
            });
        }
        Ok(bars)
    }
}

impl BarSource for StoreBarSource<'_> {
    fn source(&self) -> BacktestDataSource {
        BacktestDataSource::SystemData
    }

    fn bars(
        &self,
        symbol: &str,
        range: &DateRange,
        max_bars: usize,
    ) -> Result<Vec<BacktestBar>, BacktestError> {
        // The unified query takes signed epoch seconds; the backtest window is u64. Convert fail-closed
        // — a bound above i64::MAX is unrepresentable, and wrapping to a negative ts would silently empty
        // the query and look like EmptyData.
        let start_ts = i64::try_from(range.start).map_err(|_| BacktestError::SourceUnavailable {
            reason: format!(
                "backtest window start {} exceeds the queryable timestamp range",
                range.start
            ),
        })?;
        let end_ts = i64::try_from(range.end).map_err(|_| BacktestError::SourceUnavailable {
            reason: format!(
                "backtest window end {} exceeds the queryable timestamp range",
                range.end
            ),
        })?;
        let query = UnifiedHistoricalQuery::new(symbol, self.resolution.clone(), start_ts, end_ts)
            .with_kind(self.kind);

        // Bound the read BEFORE materializing any result set (the BarSource contract). Count the matching
        // records by streaming the in-memory store with the query predicate -- an allocation-free pass --
        // and fail closed with TooManyBars when the window would exceed max_bars. Capping up front means
        // neither the borrowed query_unified result nor (for split-adjusted) the OWNED re-quoted
        // query_split_adjusted record set is ever allocated for an oversized window.
        let match_count = self
            .store
            .records()
            .iter()
            .filter(|record| query.matches(record))
            .count();
        if match_count > max_bars {
            return Err(BacktestError::TooManyBars {
                count: match_count,
                limit: max_bars,
            });
        }

        match self.normalization {
            Normalization::Raw => {
                let result = self.store.query_unified(&query);
                let count = result.len();
                self.map_records(symbol, result.records().iter().map(|r| &**r), count)
            }
            Normalization::SplitAdjusted => {
                // Split-adjusted is served ONLY behind proven SRS-DATA-011 coverage; an uncovered query
                // fails closed (SourceUnavailable, naming SRS-DATA-011) rather than ever returning raw
                // bars dressed up as adjusted.
                let result = self.store.query_split_adjusted(&query).map_err(|err| {
                    BacktestError::SourceUnavailable {
                        reason: err.to_string(),
                    }
                })?;
                let count = result.records.len();
                self.map_records(symbol, result.records.iter(), count)
            }
        }
    }
}
