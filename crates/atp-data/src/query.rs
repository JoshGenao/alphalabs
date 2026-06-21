//! SRS-DATA-007 — the unified historical data access interface.
//!
//! The acceptance criterion (docs/SRS.md SRS-DATA-007): *"Strategy code, backtests, factor jobs, and
//! notebooks query by symbol, date range, and resolution **without specifying the original source
//! provider**."* This module is the runnable read path over the [`MarketDataStore`] storage substrate
//! the SRS-DATA-016 session built: a consumer asks for `(symbol, resolution, [start, end])` and gets
//! back the matching records — and there is structurally **no way** to name, pass, or read the
//! provider a record was originally ingested from.
//!
//! ## Why this is source-neutral by construction
//!
//! 1. [`UnifiedHistoricalQuery`] carries only the three acceptance dimensions (symbol, resolution, an
//!    inclusive `event_ts` range) plus an optional [`DatasetKind`] disambiguator. There is no
//!    provider / vendor / source / feed parameter — a consumer literally cannot pass one. `DatasetKind`
//!    is the **vendor-neutral** taxonomy (a daily bar vs a minute bar vs an option-chain snapshot vs a
//!    fundamental record); naming a kind is *not* naming Databento or IB (the adapter layer maps
//!    provider → kind, SRS-ARCH-003).
//! 2. [`UnifiedHistoricalResult`] echoes back the queried symbol + resolution and the matched records.
//!    It has no provider / vendor / source field, so a strategy / backtest / factor job / notebook is
//!    structurally unable to branch on where a record came from.
//! 3. [`MarketDataStore::query_unified`] filters only on the vendor-neutral [`NaturalKey`] dimensions.
//!    There is no provider column anywhere in a [`MarketDataRecord`] to filter on or return. The SAME
//!    one method serves every kind — a `DailyEquityBar` (⇐ Databento daily), a `MinuteEquityBar`
//!    (⇐ IB minute), an `OptionChainSnapshot` (⇐ IB option-chain), and a `Fundamental` (⇐ Sharadar) are
//!    queried identically — so a multi-provider catalog has exactly one read path.
//!
//! ## Determinism
//!
//! [`MarketDataStore::records`] is held in canonical natural-key order
//! `(kind, symbol, resolution, event_ts, option_contract)` — which sorts by `kind` BEFORE `event_ts`.
//! So a kind-AGNOSTIC match that spans two kinds sharing a symbol + resolution would NOT be
//! `event_ts`-ascending if returned in store order (it would group by kind first). The query therefore
//! sorts the matched records explicitly by `event_ts` (the date-range contract), with the full natural
//! key as a deterministic total-order tiebreaker — no clock read, no RNG. Running the same query twice
//! (or over a store reloaded from disk) yields an identical `event_ts`-ascending sequence. An empty
//! result is a normal value, never an error.
//!
//! ## Read-path scope
//!
//! `query_unified` is a pure read over an in-memory [`MarketDataStore`]; the operator CLI loads the
//! atomically-published on-disk snapshot ([`MarketDataStore::load_from_path`]) and never takes the
//! single-writer `StoreLock` — a read does not need it. Coordinating concurrent reads *during* an
//! active ingestion write is the separate deferred owner SRS-DATA-017; the SSD/NAS tiering of the
//! queried directory is SRS-DATA-008/009/010; the real provider network adapters are
//! SRS-DATA-001/003/005/006 (fixture sources stand in, as the verification step permits); the
//! in-process Python / backtest / factor bindings over this engine are downstream consumers (the Rust
//! CLI is the operator-demonstrable surface).
//!
//! [`NaturalKey`]: crate::store::NaturalKey

use crate::store::{DatasetKind, MarketDataRecord, MarketDataStore};

/// A source-neutral unified historical query (SRS-DATA-007).
///
/// Carries ONLY the three acceptance dimensions — `symbol`, an inclusive `event_ts` range, and
/// `resolution` — plus an optional vendor-neutral [`DatasetKind`] disambiguator. There is deliberately
/// NO provider / vendor / source / feed field: *"query ... without specifying the original source
/// provider"* is the entire point of the requirement. `DatasetKind` is the vendor-NEUTRAL taxonomy
/// (daily bar / minute bar / option-chain / fundamental), never a provider name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnifiedHistoricalQuery {
    /// The exact instrument symbol to match (the underlying, for an option-chain record).
    pub symbol: String,
    /// The exact bar/snapshot resolution to match (e.g. `1d`, `1m`, `chain`, `fundamental:income`).
    pub resolution: String,
    /// The inclusive lower bound on the record event timestamp (epoch seconds).
    pub start_ts: i64,
    /// The inclusive upper bound on the record event timestamp (epoch seconds).
    pub end_ts: i64,
    /// An optional dataset-kind narrowing. `None` matches a record of ANY kind that shares the
    /// symbol + resolution + range (the maximally neutral query); `Some(kind)` restricts to one
    /// [`DatasetKind`].
    pub kind: Option<DatasetKind>,
}

impl UnifiedHistoricalQuery {
    /// Build a kind-agnostic query over the three acceptance dimensions (matches any [`DatasetKind`]).
    pub fn new(
        symbol: impl Into<String>,
        resolution: impl Into<String>,
        start_ts: i64,
        end_ts: i64,
    ) -> Self {
        Self {
            symbol: symbol.into(),
            resolution: resolution.into(),
            start_ts,
            end_ts,
            kind: None,
        }
    }

    /// Narrow the query to a single vendor-neutral [`DatasetKind`] (still names no provider).
    pub fn with_kind(mut self, kind: DatasetKind) -> Self {
        self.kind = Some(kind);
        self
    }

    /// Whether `record` satisfies every dimension of this query: exact symbol, exact resolution, an
    /// inclusive `event_ts` range, and the optional kind. The single predicate the query filters on —
    /// it reads only vendor-neutral [`NaturalKey`] fields, so there is no provider dimension to match.
    ///
    /// [`NaturalKey`]: crate::store::NaturalKey
    pub fn matches(&self, record: &MarketDataRecord) -> bool {
        let key = record.key();
        key.symbol == self.symbol
            && key.resolution == self.resolution
            && key.event_ts >= self.start_ts
            && key.event_ts <= self.end_ts
            && self.kind.map_or(true, |kind| kind == key.kind)
    }
}

/// The source-neutral result of a [`UnifiedHistoricalQuery`] (SRS-DATA-007).
///
/// Echoes back the queried `symbol` + `resolution` and carries the matched records, borrowed from the
/// store in deterministic canonical (`event_ts`-ascending) order. There is NO provider / vendor /
/// source field — the envelope cannot name where a record came from, so a consumer is structurally
/// unable to branch on the origin. An empty `records` is a valid result, never an error.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnifiedHistoricalResult<'a> {
    /// The queried symbol (echoed for a self-describing result).
    pub symbol: String,
    /// The queried resolution (echoed for a self-describing result).
    pub resolution: String,
    /// The matched records, borrowed from the store in canonical `event_ts`-ascending order.
    pub records: Vec<&'a MarketDataRecord>,
}

impl<'a> UnifiedHistoricalResult<'a> {
    /// The number of matched records.
    pub fn len(&self) -> usize {
        self.records.len()
    }

    /// Whether the query matched no records (a valid empty result, not an error).
    pub fn is_empty(&self) -> bool {
        self.records.is_empty()
    }

    /// The matched records, in canonical `event_ts`-ascending order.
    pub fn records(&self) -> &[&'a MarketDataRecord] {
        &self.records
    }
}

impl MarketDataStore {
    /// SRS-DATA-007 unified historical query.
    ///
    /// Returns every record matching the query's symbol + resolution + inclusive `event_ts` range
    /// (and optional [`DatasetKind`]), sorted into deterministic `event_ts`-ascending order (the full
    /// natural key breaks ties). Pure and deterministic: no I/O, no clock, no RNG. An empty result is
    /// RETURNED (a value), never an error.
    ///
    /// The SAME path serves records regardless of the provider they were originally ingested from: the
    /// filter reads only the vendor-neutral [`NaturalKey`] dimensions (symbol, resolution, event_ts,
    /// kind), so a `DailyEquityBar` (⇐ Databento), a `MinuteEquityBar` (⇐ IB), an `OptionChainSnapshot`
    /// (⇐ IB), and a `Fundamental` (⇐ Sharadar) are queried identically — there is no provider field to
    /// filter on and none to return.
    ///
    /// [`NaturalKey`]: crate::store::NaturalKey
    pub fn query_unified<'a>(
        &'a self,
        query: &UnifiedHistoricalQuery,
    ) -> UnifiedHistoricalResult<'a> {
        let mut records: Vec<&'a MarketDataRecord> = self
            .records()
            .iter()
            .filter(|&record| query.matches(record))
            .collect();
        // The store's canonical order is the WHOLE natural key, which sorts by `kind` BEFORE
        // `event_ts`. So a kind-AGNOSTIC match that spans two kinds sharing this symbol + resolution
        // is NOT event_ts-ascending by construction (it would group by kind first). Sort explicitly by
        // `event_ts` (the date-range query contract), with the full natural key as a deterministic
        // total-order tiebreaker so the result is reproducible for any matching set.
        records.sort_by(|a, b| {
            let (ka, kb) = (a.key(), b.key());
            ka.event_ts.cmp(&kb.event_ts).then_with(|| ka.cmp(kb))
        });
        UnifiedHistoricalResult {
            symbol: query.symbol.clone(),
            resolution: query.resolution.clone(),
            records,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::{MarketField, NaturalKey};

    fn field(name: &str, value_minor: i64) -> MarketField {
        MarketField {
            name: name.to_string(),
            value_minor,
        }
    }

    fn daily(symbol: &str, event_ts: i64, close: i64) -> MarketDataRecord {
        MarketDataRecord::new(
            NaturalKey {
                kind: DatasetKind::DailyEquityBar,
                symbol: symbol.to_string(),
                resolution: "1d".to_string(),
                event_ts,
                option_contract: None,
            },
            [field("close", close), field("open", close - 10)],
        )
        .expect("well-formed daily fixture")
    }

    fn store_of(records: impl IntoIterator<Item = MarketDataRecord>) -> MarketDataStore {
        let mut store = MarketDataStore::new();
        for record in records {
            store.upsert(record).expect("fixture upsert");
        }
        store
    }

    #[test]
    fn filters_symbol_resolution_and_inclusive_range() {
        let store = store_of([
            daily("AAPL", 100, 1),
            daily("AAPL", 200, 2),
            daily("AAPL", 300, 3),
        ]);
        let result = store.query_unified(&UnifiedHistoricalQuery::new("AAPL", "1d", 100, 200));
        let ts: Vec<i64> = result.records().iter().map(|r| r.key().event_ts).collect();
        assert_eq!(ts, vec![100, 200], "inclusive range excludes 300, ascending order");
    }

    #[test]
    fn single_point_range_is_inclusive_both_ends() {
        let store = store_of([daily("AAPL", 100, 1), daily("AAPL", 200, 2)]);
        assert_eq!(
            store.query_unified(&UnifiedHistoricalQuery::new("AAPL", "1d", 200, 200)).len(),
            1
        );
        assert_eq!(
            store.query_unified(&UnifiedHistoricalQuery::new("AAPL", "1d", 100, 100)).len(),
            1
        );
    }

    #[test]
    fn symbol_and_resolution_are_exact() {
        let store = store_of([
            daily("AAPL", 100, 1),
            daily("MSFT", 100, 9),
            MarketDataRecord::new(
                NaturalKey {
                    kind: DatasetKind::MinuteEquityBar,
                    symbol: "AAPL".to_string(),
                    resolution: "1m".to_string(),
                    event_ts: 100,
                    option_contract: None,
                },
                [field("close", 5)],
            )
            .unwrap(),
        ]);
        let result = store.query_unified(&UnifiedHistoricalQuery::new("AAPL", "1d", 0, 1_000));
        assert_eq!(result.len(), 1, "only AAPL 1d matches (not MSFT, not AAPL 1m)");
        assert_eq!(result.records()[0].key().symbol, "AAPL");
        assert_eq!(result.records()[0].key().resolution, "1d");
    }

    #[test]
    fn empty_result_is_a_value_not_an_error() {
        let store = store_of([daily("AAPL", 100, 1)]);
        let result = store.query_unified(&UnifiedHistoricalQuery::new("NOSUCH", "1d", 0, 1_000));
        assert!(result.is_empty());
        assert_eq!(result.len(), 0);
        // Inverted range also yields an empty value, not a panic/error.
        let inverted = store.query_unified(&UnifiedHistoricalQuery::new("AAPL", "1d", 1_000, 0));
        assert!(inverted.is_empty());
    }

    #[test]
    fn kind_disambiguator_narrows_a_shared_symbol_resolution() {
        // Two kinds at the same symbol+resolution (constructed directly): kind:None returns both,
        // kind:Some narrows to one. Proves the optional disambiguator is honored.
        let shared_key = |kind: DatasetKind, contract: Option<String>| NaturalKey {
            kind,
            symbol: "AAPL".to_string(),
            resolution: "blend".to_string(),
            event_ts: 100,
            option_contract: contract,
        };
        let store = store_of([
            MarketDataRecord::new(shared_key(DatasetKind::DailyEquityBar, None), [field("close", 1)])
                .unwrap(),
            MarketDataRecord::new(shared_key(DatasetKind::MinuteEquityBar, None), [field("close", 2)])
                .unwrap(),
        ]);
        assert_eq!(
            store.query_unified(&UnifiedHistoricalQuery::new("AAPL", "blend", 0, 1_000)).len(),
            2
        );
        let narrowed = store.query_unified(
            &UnifiedHistoricalQuery::new("AAPL", "blend", 0, 1_000)
                .with_kind(DatasetKind::MinuteEquityBar),
        );
        assert_eq!(narrowed.len(), 1);
        assert_eq!(narrowed.records()[0].key().kind, DatasetKind::MinuteEquityBar);
    }

    #[test]
    fn cross_kind_results_are_event_ts_ascending() {
        // Regression for the store-order ordering bug: the store sorts by `kind` BEFORE `event_ts`,
        // so a kind-agnostic match spanning two kinds that share symbol+resolution must still come
        // back in event_ts-ascending order. Daily (kind tag 0) at ts 300, minute (kind tag 1) at ts
        // 100: store order would be [300, 100]; the query must return [100, 300].
        let shared = |kind: DatasetKind, event_ts: i64, close: i64| {
            MarketDataRecord::new(
                NaturalKey {
                    kind,
                    symbol: "AAPL".to_string(),
                    resolution: "blend".to_string(),
                    event_ts,
                    option_contract: None,
                },
                [field("close", close)],
            )
            .unwrap()
        };
        let store = store_of([
            shared(DatasetKind::DailyEquityBar, 300, 1),
            shared(DatasetKind::MinuteEquityBar, 100, 2),
        ]);
        let result = store.query_unified(&UnifiedHistoricalQuery::new("AAPL", "blend", 0, 1_000));
        let ts: Vec<i64> = result.records().iter().map(|r| r.key().event_ts).collect();
        assert_eq!(ts, vec![100, 300], "event_ts-ascending across kinds, not store (kind-first) order");
    }

    #[test]
    fn repeated_query_is_deterministic() {
        let store = store_of([daily("AAPL", 300, 3), daily("AAPL", 100, 1), daily("AAPL", 200, 2)]);
        let q = UnifiedHistoricalQuery::new("AAPL", "1d", 0, 1_000);
        let first: Vec<i64> = store.query_unified(&q).records().iter().map(|r| r.key().event_ts).collect();
        let second: Vec<i64> = store.query_unified(&q).records().iter().map(|r| r.key().event_ts).collect();
        assert_eq!(first, second);
        assert_eq!(first, vec![100, 200, 300], "ascending regardless of insert order");
    }
}
