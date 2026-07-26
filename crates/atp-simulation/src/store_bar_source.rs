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
use atp_data::{AccessRecorder, JobRef};

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
        let start_ts =
            i64::try_from(range.start).map_err(|_| BacktestError::SourceUnavailable {
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

/// **SRS-DATA-010 access-recording wrapper** over a [`StoreBarSource`]: a [`BarSource`] that delegates
/// every read to an inner [`StoreBarSource`] **unchanged** and records the accessed symbol into an
/// injected [`AccessRecorder`], so the SRS-DATA-010 eviction policy can protect data a running backtest
/// is using (the SYS-69 recency window).
///
/// It is a pure decorator: `source()` and `bars()` return exactly what the inner source returns — the
/// recording is a side effect that never changes the bars, never fails the read (recording fails open),
/// and adds no provider awareness. So wrapping a backtest source in this changes no observable read
/// behaviour; a backtest run WITHOUT the wrapper (or with a [`atp_data::NoopRecorder`]) is byte-identical.
///
/// `access_ts` is the instant the job accessed the data (its run time), **injected** so the reader
/// stays wall-clock-free and deterministic — the same discipline as the rest of the data layer.
#[derive(Debug)]
pub struct RecordingBarSource<'a, 'r, R: AccessRecorder> {
    inner: StoreBarSource<'a>,
    recorder: &'r R,
    job: JobRef,
    access_ts: i64,
}

impl<'a, 'r, R: AccessRecorder> RecordingBarSource<'a, 'r, R> {
    /// Wrap an inner [`StoreBarSource`], attributing each recorded access to `job` at `access_ts`.
    pub fn new(inner: StoreBarSource<'a>, recorder: &'r R, job: JobRef, access_ts: i64) -> Self {
        Self {
            inner,
            recorder,
            job,
            access_ts,
        }
    }
}

impl<R: AccessRecorder> BarSource for RecordingBarSource<'_, '_, R> {
    fn source(&self) -> BacktestDataSource {
        self.inner.source()
    }

    fn bars(
        &self,
        symbol: &str,
        range: &DateRange,
        max_bars: usize,
    ) -> Result<Vec<BacktestBar>, BacktestError> {
        // Record the access BEFORE delegating: recording fails open (never surfaces an error), so it
        // cannot break the read, and recording first captures the access even if the read then fails
        // closed (e.g. an oversized window or an uncovered adjusted basis). The symbol is what the
        // eviction policy protects; the inner read result is returned verbatim.
        self.recorder.record(&self.job, symbol, self.access_ts);
        self.inner.bars(symbol, range, max_bars)
    }
}

#[cfg(test)]
mod recording_tests {
    use super::*;
    use atp_data::store::{MarketDataRecord as Rec, MarketField, NaturalKey};
    use atp_data::{AccessJournal, JobId, JobKind, NoopRecorder};

    fn daily_record(symbol: &str, event_ts: i64, close: i64) -> Rec {
        Rec::new(
            NaturalKey {
                kind: DatasetKind::DailyEquityBar,
                symbol: symbol.to_string(),
                resolution: "1d".to_string(),
                event_ts,
                option_contract: None,
            },
            [MarketField {
                name: "close".to_string(),
                value_minor: close,
            }],
        )
        .unwrap()
    }

    fn store_with(symbol: &str) -> MarketDataStore {
        let mut store = MarketDataStore::new();
        store.upsert(daily_record(symbol, 1_000, 100)).unwrap();
        store.upsert(daily_record(symbol, 2_000, 110)).unwrap();
        store
    }

    #[test]
    fn recording_wrapper_returns_identical_bars_to_the_bare_source() {
        let store = store_with("AAPL");
        let bare = StoreBarSource::daily(&store, Normalization::Raw);
        let bare_bars = bare
            .bars(
                "AAPL",
                &DateRange {
                    start: 0,
                    end: 9_999,
                },
                100,
            )
            .unwrap();

        let store2 = store_with("AAPL");
        let inner = StoreBarSource::daily(&store2, Normalization::Raw);
        let noop = NoopRecorder;
        let wrapped = RecordingBarSource::new(
            inner,
            &noop,
            JobRef::new(JobKind::Backtest, JobId::new("bt-1").unwrap()),
            5_000,
        );
        let wrapped_bars = wrapped
            .bars(
                "AAPL",
                &DateRange {
                    start: 0,
                    end: 9_999,
                },
                100,
            )
            .unwrap();

        assert_eq!(bare_bars, wrapped_bars, "wrapper must not change the bars");
    }

    #[test]
    fn recording_wrapper_writes_the_accessed_symbol_to_the_journal() {
        let tmp =
            std::env::temp_dir().join(format!("atp-recbar-{}-{}", std::process::id(), line!()));
        let _ = std::fs::remove_dir_all(&tmp);
        std::fs::create_dir_all(&tmp).unwrap();

        let store = store_with("AAPL");
        let inner = StoreBarSource::daily(&store, Normalization::Raw);
        let journal = AccessJournal::under_ssd(&tmp);
        let wrapped = RecordingBarSource::new(
            inner,
            &journal,
            JobRef::new(JobKind::Backtest, JobId::new("bt-42").unwrap()),
            5_000,
        );
        wrapped
            .bars(
                "aapl",
                &DateRange {
                    start: 0,
                    end: 9_999,
                },
                100,
            )
            .unwrap();

        // The eviction policy would see AAPL protected at access_ts 5_000 (within a wide window).
        let recent = journal.recent(10_000, 6_000, None).unwrap();
        assert_eq!(recent.get("AAPL"), Some(&5_000));
    }

    #[test]
    fn recording_is_fail_open_even_when_the_read_fails() {
        // An oversized window fails closed with TooManyBars; the access is still recorded (recorded
        // before the delegate call) and the read error is surfaced unchanged.
        let tmp = std::env::temp_dir().join(format!(
            "atp-recbar-failopen-{}-{}",
            std::process::id(),
            line!()
        ));
        let _ = std::fs::remove_dir_all(&tmp);
        std::fs::create_dir_all(&tmp).unwrap();

        let store = store_with("MSFT");
        let inner = StoreBarSource::daily(&store, Normalization::Raw);
        let journal = AccessJournal::under_ssd(&tmp);
        let wrapped = RecordingBarSource::new(
            inner,
            &journal,
            JobRef::new(JobKind::Backtest, JobId::new("bt-x").unwrap()),
            5_000,
        );
        let err = wrapped
            .bars(
                "MSFT",
                &DateRange {
                    start: 0,
                    end: 9_999,
                },
                1,
            )
            .unwrap_err();
        assert!(matches!(err, BacktestError::TooManyBars { .. }));
        let recent = journal.recent(10_000, 6_000, None).unwrap();
        assert_eq!(recent.get("MSFT"), Some(&5_000));
    }
}
