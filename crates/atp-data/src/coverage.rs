//! SRS-DATA-011 — corporate-action **coverage** gate (the keystone that makes a split-adjusted label
//! honest).
//!
//! The acceptance criterion (docs/SRS.md SRS-DATA-011): *"Splits, reverse splits, dividends,
//! delistings, mergers, and symbol changes are reflected in historical records so that backtests
//! spanning corporate-action dates produce correct P&L calculations under the selected normalization
//! mode."* This module ships the **coverage** half — the keystone that lets the (crate-internal)
//! SRS-DATA-012 split-adjustment math finally be served on a public surface, but **only** when coverage
//! proves every relevant split is known. Only SPLITS / reverse-splits have adjustment math + coverage
//! here; dividends, delistings, mergers, and symbol changes are deferred (SRS-DATA-011 remainder), so
//! SRS-DATA-011 stays `passes:false`.
//!
//! # The gate (the crux)
//!
//! The split math re-quotes a bar at `event_ts = t` by every split with `effective_ts > t` (STRICT).
//! Every in-window bar has `t <= end_ts`, so the deepest requirement for an internally-consistent
//! split-adjusted `[start_ts, end_ts]` window is that **all splits with `effective_ts <= end_ts` are
//! known**. Define the **coverage frontier** `D = max(complete_through)` over the symbol's
//! [`CorporateActionCoverage`](crate::store::DatasetKind::CorporateActionCoverage) records (`None` if
//! the symbol has no coverage record). [`MarketDataStore::query_split_adjusted`] serves split-adjusted
//! output **iff a coverage record exists AND `D >= end_ts`**; otherwise it FAILS CLOSED with
//! [`CoverageError::NotCovered`].
//!
//! `D >= end_ts` (not `==`) is correct, and these are the edges an adversarial reviewer probes:
//!
//! * A known split in `(end_ts, D]` has `effective_ts > end_ts >=` every in-window bar, so it applies
//!   *uniformly* to all of them — the series stays internally consistent (this is why the bound is
//!   `>=`, not `==`).
//! * A split exactly at `end_ts` (`effective_ts == end_ts`) leaves the `end_ts` bar unadjusted (the
//!   strict `effective_ts > t` boundary) while adjusting every earlier in-window bar — and it has
//!   `effective_ts = end_ts <= D`, so it is known and handled. The strict math boundary and the `>=`
//!   gate boundary are mutually coherent; there is no off-by-one.
//! * `D < end_ts` (or no record) could leave an unknown split in `(D, end_ts]` that adjusts some
//!   in-window bars — that bar would be silently under-adjusted (a phantom split-drop = wrong P&L), so
//!   it must fail closed.
//!
//! The result is an honest **"as-of-`D` split-adjusted"** series with no phantom split-drops inside the
//! window — the SRS-DATA-011 *"correct P&L for backtests spanning corporate-action dates"* property. To
//! keep the basis exactly as-of-`D`, the adjustment applies ONLY splits with `effective_ts <= D` (so a
//! bar at `t` is adjusted for splits in `(t, D]`): a split with `effective_ts > D` is EXCLUDED even if
//! its record is already in the store, because coverage guarantees completeness only through `D` — the
//! `(D, ...]` range may hide unknown splits, so applying a known later split would silently adjust the
//! series PAST the advertised `coverage_through:D`. Over-claiming a "current-adjusted" series would be
//! the fail-open; `D >= end_ts` (applying splits up to `D`) is the strongest honest claim available
//! without future corporate-action data.
//!
//! # The single gated public entry point (no uncovered capability on ANY surface)
//!
//! The split-adjustment math (`crate::normalization`) stays **crate-internal** — `split_adjust_records`
//! / `SplitEvent` are not re-exported. This module is a sibling in the same crate, so it can call those
//! crate-internal functions while no external caller can. [`MarketDataStore::query_split_adjusted`] is
//! the **only** public path to split-adjusted output, and it cannot return adjusted records without the
//! coverage check passing — so there is no public path to raw-as-adjusted. The query kind is also
//! required to be an equity bar (`DailyEquityBar` / `MinuteEquityBar`) so the math's `UnsupportedKind`
//! path is unreachable at runtime and a split-adjusted *series* is equity-only by construction.

use crate::normalization::{self, NormalizationError};
use crate::query::UnifiedHistoricalQuery;
use crate::store::{DatasetKind, MarketDataRecord, MarketDataStore};

/// The result of a covered [`MarketDataStore::query_split_adjusted`]: the split-adjusted records (owned,
/// in `event_ts`-ascending order) plus the coverage frontier `D` the adjustment was computed against
/// (the "as-of" instant), so a consumer knows the basis the series is quoted on.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SplitAdjustedResult {
    /// The split-adjusted records, owned, in `event_ts`-ascending order (the query result re-quoted).
    pub records: Vec<MarketDataRecord>,
    /// The coverage frontier `D` (the completeness-through instant) the adjustment was computed
    /// against — the "as-of" date the series is quoted on. Always `>= query.end_ts`.
    pub coverage_through: i64,
}

/// A fail-closed split-adjusted-serving error. Split-adjusted output is served only behind proven
/// coverage; every other condition fails closed rather than emitting raw-as-adjusted output.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CoverageError {
    /// Corporate-action coverage for the symbol does not extend through the query end, so a split in
    /// the uncovered tail could be missing — serving split-adjusted would risk raw-as-adjusted output.
    NotCovered {
        /// The symbol whose coverage was insufficient.
        symbol: String,
        /// The coverage frontier `D` the symbol DOES have (`None` if it has no coverage record at all).
        have_through: Option<i64>,
        /// The frontier the query needs (`query.end_ts`): coverage must extend at least this far.
        need_through: i64,
    },
    /// A split-adjusted query named no kind, or a non-equity-bar kind. Split adjustment is an
    /// equity-bar operation; the gate requires an explicit `DailyEquityBar` / `MinuteEquityBar` kind so
    /// the series is equity-only by construction (a kind-agnostic query could otherwise sweep in an
    /// option-chain / fundamental record sharing the symbol + resolution).
    UnsupportedQueryKind {
        /// The (vendor-neutral) kind label the query named, or `"unspecified"` for a kind-agnostic query.
        kind: &'static str,
    },
    /// The split-adjustment math fails closed (a malformed split record, a non-positive factor, an
    /// overflow). Passed through verbatim so the caller sees the precise money-math reason.
    Normalization(NormalizationError),
}

impl std::fmt::Display for CoverageError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotCovered {
                symbol,
                have_through,
                need_through,
            } => {
                let have = match have_through {
                    Some(d) => format!("complete through {d}"),
                    None => "absent (no coverage record)".to_string(),
                };
                write!(
                    f,
                    "split-adjusted refused for {symbol}: corporate-action coverage is {have} but the \
                     query needs coverage through >= {need_through} (SRS-DATA-011); ingest coverage \
                     with data011_coverage_cli"
                )
            }
            Self::UnsupportedQueryKind { kind } => write!(
                f,
                "split-adjusted normalization requires an explicit equity-bar kind \
                 (daily-equity-bar | minute-equity-bar); got '{kind}'"
            ),
            Self::Normalization(err) => write!(f, "{err}"),
        }
    }
}

impl std::error::Error for CoverageError {}

impl From<NormalizationError> for CoverageError {
    fn from(err: NormalizationError) -> Self {
        Self::Normalization(err)
    }
}

impl MarketDataStore {
    /// The corporate-action coverage frontier `D` for `symbol`: the maximum completeness-through
    /// instant over the symbol's [`CorporateActionCoverage`](DatasetKind::CorporateActionCoverage)
    /// records, or `None` if it has none. The frontier is read from each coverage record's
    /// `event_ts` — the natural-key dedup identity that IS the through-date `D` (see
    /// [`coverage_record`](crate::store::coverage_record)), so advancing the frontier is a new record
    /// and the effective frontier is simply their maximum. Reading `event_ts` is safe even though
    /// `MarketDataRecord::new` is public: store validation requires every coverage record to carry a
    /// single `complete_through` field equal to its `event_ts`, so a forged record asserting a frontier
    /// its key does not carry fails closed before it can enter the store (`upsert` / `restore`).
    pub fn coverage_frontier(&self, symbol: &str) -> Option<i64> {
        self.records()
            .iter()
            .filter(|record| {
                let key = record.key();
                key.kind == DatasetKind::CorporateActionCoverage && key.symbol == symbol
            })
            .map(|record| record.key().event_ts)
            .max()
    }

    /// **The coverage-enforcing split-adjusted read (SRS-DATA-011 / SRS-DATA-012).** Returns the
    /// query's equity bars re-quoted onto a split-comparable basis — but ONLY when the symbol's
    /// corporate-action coverage frontier extends through the query end.
    ///
    /// This is the SINGLE public path to split-adjusted output. It fails closed:
    /// * [`CoverageError::UnsupportedQueryKind`] unless `query.kind` is an explicit equity-bar kind, so
    ///   the split-adjustment math's `UnsupportedKind` path is unreachable at runtime;
    /// * [`CoverageError::NotCovered`] unless a coverage record exists and `frontier >= query.end_ts`
    ///   (see the module docs for why `>=` is the precise, honest condition);
    /// * [`CoverageError::Normalization`] if a split record is malformed (passed through verbatim).
    ///
    /// On success the result carries the adjusted records and the `coverage_through` frontier the
    /// adjustment was computed against (the "as-of" instant). An empty in-range result is a valid
    /// covered result (`records` empty), never an error.
    pub fn query_split_adjusted(
        &self,
        query: &UnifiedHistoricalQuery,
    ) -> Result<SplitAdjustedResult, CoverageError> {
        // (1) Equity-bar kind guard. Split adjustment is an equity-bar operation; require an explicit
        // DailyEquityBar / MinuteEquityBar kind so a kind-agnostic query cannot sweep in an
        // option-chain / fundamental record (which the math would reject mid-stream as UnsupportedKind)
        // and so the served series is equity-only by construction.
        match query.kind {
            Some(DatasetKind::DailyEquityBar) | Some(DatasetKind::MinuteEquityBar) => {}
            Some(other) => {
                return Err(CoverageError::UnsupportedQueryKind { kind: other.as_str() })
            }
            None => return Err(CoverageError::UnsupportedQueryKind { kind: "unspecified" }),
        }

        // (2) Coverage gate. The frontier must exist AND reach at least the query end, else the
        // uncovered tail could hide a split that adjusts an in-window bar -> fail closed.
        let frontier = self.coverage_frontier(&query.symbol);
        let coverage_through = match frontier {
            Some(d) if d >= query.end_ts => d,
            have => {
                return Err(CoverageError::NotCovered {
                    symbol: query.symbol.clone(),
                    have_through: have,
                    need_through: query.end_ts,
                })
            }
        };

        // (3) Collect the symbol's split records up to the coverage frontier D (the as-of date) -- NOT
        // just the query window, and NOT every split store-wide. A split with effective_ts in
        // (end_ts, D] legitimately re-bases the in-window bars onto the as-of-D basis, so the set is not
        // bounded by the query range. But a split with effective_ts > D is EXCLUDED: it is beyond the
        // proven-complete frontier (coverage only guarantees completeness through D, so the (D, ...]
        // range may hide unknown splits), and applying it would silently adjust the series PAST the
        // advertised coverage_through:D. So the served series is consistently adjusted for every split
        // effective on or before D and no further -- an honest as-of-D basis. (A malformed split beyond
        // D is likewise out of scope and cannot fail an as-of-D query.) `split_events_for` re-filters by
        // kind + symbol and fails closed on a malformed split within the frontier.
        let split_refs: Vec<&MarketDataRecord> = self
            .records()
            .iter()
            .filter(|record| {
                let key = record.key();
                key.kind == DatasetKind::CorporateActionSplit
                    && key.symbol == query.symbol
                    && key.event_ts <= coverage_through
            })
            .collect();
        let splits = normalization::split_events_for(&query.symbol, &split_refs)?;

        // (4) The equity bars in range (kind-narrowed, event_ts-ascending), then (5) apply the
        // crate-internal split math. Every record is the guarded equity-bar kind, so UnsupportedKind is
        // unreachable; an empty match yields an empty (still covered) result.
        let matched = self.query_unified(query);
        let adjusted = normalization::split_adjust_records(matched.records(), &splits)?;
        Ok(SplitAdjustedResult {
            records: adjusted,
            coverage_through,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::{coverage_record, MarketField, NaturalKey};

    fn field(name: &str, value_minor: i64) -> MarketField {
        MarketField {
            name: name.to_string(),
            value_minor,
        }
    }

    fn daily_bar(symbol: &str, event_ts: i64, close: i64, volume: i64) -> MarketDataRecord {
        MarketDataRecord::new(
            NaturalKey {
                kind: DatasetKind::DailyEquityBar,
                symbol: symbol.to_string(),
                resolution: "1d".to_string(),
                event_ts,
                option_contract: None,
            },
            [field("close", close), field("volume", volume)],
        )
        .expect("well-formed daily bar")
    }

    fn split(symbol: &str, effective_ts: i64, numerator: i64, denominator: i64) -> MarketDataRecord {
        MarketDataRecord::new(
            NaturalKey {
                kind: DatasetKind::CorporateActionSplit,
                symbol: symbol.to_string(),
                resolution: "split".to_string(),
                event_ts: effective_ts,
                option_contract: None,
            },
            [field("denominator", denominator), field("numerator", numerator)],
        )
        .expect("well-formed split record")
    }

    fn store_of(records: impl IntoIterator<Item = MarketDataRecord>) -> MarketDataStore {
        let mut store = MarketDataStore::new();
        for record in records {
            store.upsert(record).expect("fixture upsert");
        }
        store
    }

    fn daily_query(symbol: &str, start: i64, end: i64) -> UnifiedHistoricalQuery {
        UnifiedHistoricalQuery::new(symbol, "1d", start, end).with_kind(DatasetKind::DailyEquityBar)
    }

    fn close_of(record: &MarketDataRecord, name: &str) -> i64 {
        record
            .fields()
            .iter()
            .find(|f| f.name == name)
            .expect("field present")
            .value_minor
    }

    #[test]
    fn covered_query_returns_split_adjusted_bars() {
        // AAPL bar @100 (close 10000, vol 100000), a 4-for-1 split @200, coverage through 200.
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            split("AAPL", 200, 4, 1),
            coverage_record(200, "AAPL"),
        ]);
        // Query [0,100]: frontier 200 >= 100 -> served; bar@100 is pre-split (200 > 100) -> adjusted.
        let result = store.query_split_adjusted(&daily_query("AAPL", 0, 100)).unwrap();
        assert_eq!(result.coverage_through, 200);
        assert_eq!(result.records.len(), 1);
        assert_eq!(close_of(&result.records[0], "close"), 2_500); // 10000 / 4
        assert_eq!(close_of(&result.records[0], "volume"), 400_000); // 100000 * 4
    }

    #[test]
    fn gate_passes_at_frontier_equal_to_end_and_above() {
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            coverage_record(150, "AAPL"),
        ]);
        // D == end_ts (150 == 150) passes (boundary).
        assert!(store.query_split_adjusted(&daily_query("AAPL", 0, 150)).is_ok());
        // D > end_ts (150 > 100) passes.
        assert!(store.query_split_adjusted(&daily_query("AAPL", 0, 100)).is_ok());
    }

    #[test]
    fn gate_fails_closed_one_short_of_the_end() {
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            coverage_record(149, "AAPL"),
        ]);
        // D == end_ts - 1 (149 < 150) fails closed.
        let err = store.query_split_adjusted(&daily_query("AAPL", 0, 150)).unwrap_err();
        assert_eq!(
            err,
            CoverageError::NotCovered {
                symbol: "AAPL".to_string(),
                have_through: Some(149),
                need_through: 150,
            }
        );
    }

    #[test]
    fn no_coverage_record_fails_closed() {
        let store = store_of([daily_bar("AAPL", 100, 10_000, 100_000), split("AAPL", 200, 4, 1)]);
        let err = store.query_split_adjusted(&daily_query("AAPL", 0, 100)).unwrap_err();
        assert_eq!(
            err,
            CoverageError::NotCovered {
                symbol: "AAPL".to_string(),
                have_through: None,
                need_through: 100,
            }
        );
    }

    #[test]
    fn split_in_the_covered_tail_above_end_still_adjusts_in_window_bars() {
        // Coverage through 300; query [0,100]; a split @200 is in (end=100, D=300]. It must still
        // adjust the in-window bar@100 (200 > 100), which is exactly why the gate is `D >= end`, not
        // a window-bounded split scan.
        let store = store_of([
            daily_bar("AAPL", 100, 8_000, 50_000),
            split("AAPL", 200, 2, 1),
            coverage_record(300, "AAPL"),
        ]);
        let result = store.query_split_adjusted(&daily_query("AAPL", 0, 100)).unwrap();
        assert_eq!(close_of(&result.records[0], "close"), 4_000); // 8000 / 2
        assert_eq!(close_of(&result.records[0], "volume"), 100_000); // 50000 * 2
    }

    #[test]
    fn split_exactly_at_end_leaves_the_end_bar_unadjusted_but_adjusts_earlier_bars() {
        // Split effective exactly at end_ts (200). The bar AT 200 is already post-split (strict
        // boundary) -> unadjusted; the bar @100 is pre-split -> adjusted. The split has
        // effective_ts == end_ts <= D, so it is known and handled by a `D >= end_ts` gate.
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            daily_bar("AAPL", 200, 4_000, 40_000),
            split("AAPL", 200, 4, 1),
            coverage_record(200, "AAPL"),
        ]);
        let result = store.query_split_adjusted(&daily_query("AAPL", 0, 200)).unwrap();
        assert_eq!(result.records.len(), 2);
        // bar@100 adjusted (10000/4 = 2500), bar@200 unadjusted (4000).
        assert_eq!(close_of(&result.records[0], "close"), 2_500);
        assert_eq!(close_of(&result.records[1], "close"), 4_000);
    }

    #[test]
    fn split_beyond_the_frontier_is_not_applied_as_of_d() {
        // as-of-D: a split with effective_ts > D must NOT adjust the result, even though its record is
        // present in the store. Coverage through 150, query [0,100], a split @200 (> 150): the bar@100
        // must come back UNADJUSTED (close 10000), labeled coverage_through:150 -- not silently
        // re-based past the advertised frontier.
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            split("AAPL", 200, 4, 1),
            coverage_record(150, "AAPL"),
        ]);
        let result = store.query_split_adjusted(&daily_query("AAPL", 0, 100)).unwrap();
        assert_eq!(result.coverage_through, 150);
        assert_eq!(close_of(&result.records[0], "close"), 10_000, "split@200 > D=150 must not apply");
        assert_eq!(close_of(&result.records[0], "volume"), 100_000);
    }

    #[test]
    fn split_at_exactly_the_frontier_is_applied() {
        // Boundary: a split with effective_ts == D is within the proven-complete frontier (coverage
        // through D is inclusive), so it IS applied to earlier in-window bars.
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            split("AAPL", 150, 4, 1),
            coverage_record(150, "AAPL"),
        ]);
        let result = store.query_split_adjusted(&daily_query("AAPL", 0, 100)).unwrap();
        assert_eq!(result.coverage_through, 150);
        assert_eq!(close_of(&result.records[0], "close"), 2_500, "split@150 == D=150 applies");
    }

    #[test]
    fn malformed_split_beyond_the_frontier_does_not_fail_an_as_of_d_query() {
        // A malformed (non-positive) split beyond the frontier is out of the as-of-D scope, so it must
        // NOT fail closed an otherwise-covered query (only malformed splits WITHIN the frontier do).
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            split("AAPL", 300, 0, 1), // malformed, but effective_ts 300 > D=200 -> excluded
            coverage_record(200, "AAPL"),
        ]);
        let result = store.query_split_adjusted(&daily_query("AAPL", 0, 100)).unwrap();
        assert_eq!(close_of(&result.records[0], "close"), 10_000);
    }

    #[test]
    fn frontier_is_the_max_over_multiple_coverage_records() {
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            coverage_record(100, "AAPL"),
            coverage_record(300, "AAPL"),
            coverage_record(200, "AAPL"),
        ]);
        assert_eq!(store.coverage_frontier("AAPL"), Some(300));
        // A query needing through 250 passes (max frontier 300 >= 250).
        assert!(store.query_split_adjusted(&daily_query("AAPL", 0, 250)).is_ok());
    }

    #[test]
    fn advancing_the_frontier_never_conflicts() {
        // Append-only, idempotent: advancing is an Insert, re-asserting the same D is a no-op, and a
        // ConflictingContent is structurally impossible (event_ts = D, so a different D is a new key).
        use crate::store::UpsertOutcome;
        let mut store = MarketDataStore::new();
        assert_eq!(store.upsert(coverage_record(100, "AAPL")).unwrap(), UpsertOutcome::Inserted);
        assert_eq!(store.upsert(coverage_record(200, "AAPL")).unwrap(), UpsertOutcome::Inserted);
        assert_eq!(
            store.upsert(coverage_record(200, "AAPL")).unwrap(),
            UpsertOutcome::UnchangedDuplicate
        );
        assert_eq!(store.coverage_frontier("AAPL"), Some(200));
    }

    #[test]
    fn coverage_is_per_symbol() {
        // MSFT coverage does not cover an AAPL query.
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            coverage_record(500, "MSFT"),
        ]);
        assert_eq!(store.coverage_frontier("AAPL"), None);
        let err = store.query_split_adjusted(&daily_query("AAPL", 0, 100)).unwrap_err();
        assert!(matches!(err, CoverageError::NotCovered { have_through: None, .. }));
    }

    #[test]
    fn unspecified_or_non_equity_query_kind_fails_closed() {
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            coverage_record(200, "AAPL"),
        ]);
        // Kind-agnostic query (no kind) is rejected before any coverage/math.
        let agnostic = UnifiedHistoricalQuery::new("AAPL", "1d", 0, 100);
        assert_eq!(
            store.query_split_adjusted(&agnostic).unwrap_err(),
            CoverageError::UnsupportedQueryKind { kind: "unspecified" }
        );
        // A non-equity kind (fundamental) is rejected.
        let fundamental = UnifiedHistoricalQuery::new("AAPL", "1d", 0, 100)
            .with_kind(DatasetKind::Fundamental);
        assert_eq!(
            store.query_split_adjusted(&fundamental).unwrap_err(),
            CoverageError::UnsupportedQueryKind { kind: "fundamental" }
        );
    }

    #[test]
    fn covered_but_empty_in_range_is_a_value_not_an_error() {
        // Coverage exists and reaches the end, but no bar falls in the range -> a covered empty result.
        let store = store_of([
            daily_bar("AAPL", 500, 10_000, 100_000),
            coverage_record(100, "AAPL"),
        ]);
        let result = store.query_split_adjusted(&daily_query("AAPL", 0, 100)).unwrap();
        assert!(result.records.is_empty());
        assert_eq!(result.coverage_through, 100);
    }

    #[test]
    fn a_forged_coverage_frontier_cannot_enter_the_store_so_the_gate_holds() {
        // The gate trusts the coverage record's event_ts as the frontier. MarketDataRecord::new is
        // public, so a producer could TRY to forge a coverage record whose key event_ts (999) asserts a
        // frontier its complete_through field (200) does not carry. Store validation rejects it, so it
        // can never enter the store -> the gate sees no coverage -> NotCovered (it cannot be fooled).
        let forged = MarketDataRecord::new(
            NaturalKey {
                kind: DatasetKind::CorporateActionCoverage,
                symbol: "AAPL".to_string(),
                resolution: "coverage".to_string(),
                event_ts: 999,
                option_contract: None,
            },
            [field("complete_through", 200)],
        );
        assert!(forged.is_err(), "a coverage record with a forged frontier must fail validation");

        // A store built only from the honest constructor has a trustworthy frontier; a bare-bones store
        // with no coverage record fails the gate closed.
        let store = store_of([daily_bar("AAPL", 100, 10_000, 100_000)]);
        assert_eq!(store.coverage_frontier("AAPL"), None);
        assert!(matches!(
            store.query_split_adjusted(&daily_query("AAPL", 0, 100)),
            Err(CoverageError::NotCovered { have_through: None, .. })
        ));
    }

    #[test]
    fn malformed_split_fails_closed_through_the_gate() {
        // A non-positive split factor for the symbol surfaces as a Normalization error (not a panic).
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            split("AAPL", 200, 0, 1), // zero numerator
            coverage_record(200, "AAPL"),
        ]);
        let err = store.query_split_adjusted(&daily_query("AAPL", 0, 100)).unwrap_err();
        assert!(matches!(err, CoverageError::Normalization(_)));
    }

    #[test]
    fn unadjusted_when_no_split_but_covered() {
        // Covered, but no split for the symbol -> the bars come back verbatim (identity), correctly
        // labeled split-adjusted as-of-D (not a masquerade: there genuinely is no split to apply).
        let store = store_of([
            daily_bar("AAPL", 100, 10_003, 100_001),
            coverage_record(200, "AAPL"),
        ]);
        let result = store.query_split_adjusted(&daily_query("AAPL", 0, 100)).unwrap();
        assert_eq!(close_of(&result.records[0], "close"), 10_003);
        assert_eq!(close_of(&result.records[0], "volume"), 100_001);
    }
}
