//! SRS-DATA-021 (L5 integration) — the coverage-gated corporate-action FACT read.
//!
//! `MarketDataStore::query_corporate_action_facts` is the "same corporate-action
//! data source" seam the paper application (SYS-88) consumes and the live
//! SRS-DATA-019/020 composition roots will: split and dividend TERMS plus the
//! structural delisting / merger / symbol-change events, surfaced from the SAME
//! store and behind the SAME coverage gate as the adjusted price reads. These
//! tests pin the gate (fail closed uncovered), the point-in-time window bounds
//! (no lookahead), the term extraction (including the dividend's resolved
//! reference close), the lineage retag, and the fail-closed malformed-record
//! path.

use atp_data::store::{
    coverage_record, delisting_record, dividend_record, merger_record, symbol_change_record,
    DatasetKind, MarketDataRecord, MarketDataStore, MarketField, NaturalKey,
};
use atp_data::{CorporateActionFact, CoverageError, UnifiedHistoricalQuery};

fn field(name: &str, value_minor: i64) -> MarketField {
    MarketField {
        name: name.to_string(),
        value_minor,
    }
}

fn daily_bar(symbol: &str, event_ts: i64, close: i64) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::DailyEquityBar,
            symbol: symbol.to_string(),
            resolution: "1d".to_string(),
            event_ts,
            option_contract: None,
        },
        [field("close", close), field("volume", 1_000)],
    )
    .expect("well-formed daily bar")
}

fn split_record(
    symbol: &str,
    effective_ts: i64,
    numerator: i64,
    denominator: i64,
) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::CorporateActionSplit,
            symbol: symbol.to_string(),
            resolution: "split".to_string(),
            event_ts: effective_ts,
            option_contract: None,
        },
        [
            field("denominator", denominator),
            field("numerator", numerator),
        ],
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

#[test]
fn srs_data_021_covered_read_surfaces_split_and_dividend_terms() {
    // A bar (the dividend's reference close), a 4-for-1 split, a 100-minor
    // dividend, coverage through 400.
    let store = store_of([
        daily_bar("AAPL", 100, 4_000),
        split_record("AAPL", 200, 4, 1),
        dividend_record(300, "AAPL", 100),
        coverage_record(400, "AAPL"),
    ]);
    let facts = store
        .query_corporate_action_facts(&daily_query("AAPL", 0, 400))
        .expect("covered fact read");
    assert_eq!(
        facts,
        vec![
            CorporateActionFact::Split {
                symbol: "AAPL".to_string(),
                effective_ts: 200,
                numerator: 4,
                denominator: 1,
            },
            CorporateActionFact::Dividend {
                symbol: "AAPL".to_string(),
                ex_ts: 300,
                amount_minor: 100,
                // The reference close is RESOLVED from the raw series (the bar
                // @100), not caller-supplied — the sanity term the applier needs.
                prev_close_minor: 4_000,
            },
        ],
        "facts carry the terms in effective_ts order"
    );
}

#[test]
fn srs_data_021_uncovered_read_fails_closed() {
    // Same records, no coverage record: an application consumer must never act
    // on a window whose tail could hide an action.
    let bare = store_of([
        daily_bar("AAPL", 100, 4_000),
        split_record("AAPL", 200, 4, 1),
    ]);
    let err = bare
        .query_corporate_action_facts(&daily_query("AAPL", 0, 400))
        .unwrap_err();
    assert_eq!(
        err,
        CoverageError::NotCovered {
            symbol: "AAPL".to_string(),
            have_through: None,
            need_through: 400,
        }
    );

    // Coverage one short of the window end also fails closed.
    let short = store_of([
        daily_bar("AAPL", 100, 4_000),
        split_record("AAPL", 200, 4, 1),
        coverage_record(399, "AAPL"),
    ]);
    assert!(matches!(
        short
            .query_corporate_action_facts(&daily_query("AAPL", 0, 400))
            .unwrap_err(),
        CoverageError::NotCovered {
            have_through: Some(399),
            ..
        }
    ));
}

#[test]
fn srs_data_021_kind_guard_fails_closed() {
    let store = store_of([coverage_record(400, "AAPL")]);
    let mut query = daily_query("AAPL", 0, 400);
    query.kind = None;
    assert!(matches!(
        store.query_corporate_action_facts(&query).unwrap_err(),
        CoverageError::UnsupportedQueryKind {
            kind: "unspecified"
        }
    ));
}

#[test]
fn srs_data_021_window_bounds_are_point_in_time() {
    // Actions @200 (split) and @300 (dividend) with coverage through 1000.
    let store = store_of([
        daily_bar("AAPL", 100, 4_000),
        split_record("AAPL", 200, 4, 1),
        dividend_record(300, "AAPL", 100),
        delisting_record(500, "AAPL"),
        coverage_record(1_000, "AAPL"),
    ]);
    // A window ending BEFORE the dividend surfaces only the split — a fact after
    // the as-of end has not happened yet (no lookahead), even inside coverage.
    let facts = store
        .query_corporate_action_facts(&daily_query("AAPL", 0, 250))
        .expect("covered");
    assert_eq!(facts.len(), 1);
    assert!(matches!(facts[0], CorporateActionFact::Split { .. }));

    // A window starting AFTER the split excludes it (facts are window-bounded
    // for the applier: it asks "what happened in [start, end]").
    let facts = store
        .query_corporate_action_facts(&daily_query("AAPL", 250, 600))
        .expect("covered");
    assert_eq!(
        facts.iter().map(fact_kind).collect::<Vec<_>>(),
        vec!["dividend", "delisting"],
    );
}

#[test]
fn srs_data_021_structural_facts_carry_merger_terms() {
    let store = store_of([
        merger_record(200, "OLD", "NEW", 3, 2, 0),
        coverage_record(400, "OLD"),
    ]);
    let facts = store
        .query_corporate_action_facts(&daily_query("OLD", 0, 400))
        .expect("covered");
    assert_eq!(
        facts,
        vec![CorporateActionFact::Merger {
            symbol: "OLD".to_string(),
            successor: "NEW".to_string(),
            numerator: 3,
            denominator: 2,
            cash_per_share_minor: 0,
            effective_ts: 200,
        }]
    );
}

#[test]
fn srs_data_021_lineage_facts_keep_the_as_held_symbol() {
    // OLD renamed to NEW @300; OLD's split @200 must surface under OLD — the
    // symbol a holder actually held at that instant — NOT retagged to the
    // queried symbol (the price reads' relabeling). An applier walking the
    // facts in effective_ts order then hits the held OLD position with the
    // split FIRST and is carried onto NEW by the rename fact itself; a
    // NEW-retagged split would silently miss the book (the fail-open the
    // adversarial review caught).
    let store = store_of([
        split_record("OLD", 200, 2, 1),
        symbol_change_record(300, "OLD", "NEW"),
        coverage_record(400, "NEW"),
    ]);
    let facts = store
        .query_corporate_action_facts(&daily_query("NEW", 0, 400))
        .expect("covered");
    assert_eq!(
        facts,
        vec![
            CorporateActionFact::Split {
                symbol: "OLD".to_string(),
                effective_ts: 200,
                numerator: 2,
                denominator: 1,
            },
            CorporateActionFact::SymbolChange {
                predecessor: "OLD".to_string(),
                successor: "NEW".to_string(),
                effective_ts: 300,
            },
        ]
    );

    // The PRICE reads are unchanged by the fact semantics: the same store's
    // split-adjusted read still relabels the series to the queried symbol.
    let priced = store_of([
        daily_bar("OLD", 100, 10_000),
        split_record("OLD", 200, 2, 1),
        symbol_change_record(300, "OLD", "NEW"),
        coverage_record(400, "NEW"),
    ]);
    let result = priced
        .query_split_adjusted(&daily_query("NEW", 0, 400))
        .expect("covered price read");
    assert_eq!(result.records[0].key().symbol, "NEW", "bars relabeled");
}

#[test]
fn srs_data_021_structural_facts_outside_lineage_validity_fail_closed() {
    // A predecessor's delisting dated AFTER its rename is structurally
    // impossible rename data. Surfacing it would hand the paper applier an
    // OLD-keyed event its book (already carried onto NEW by the earlier rename
    // fact) can only no-op against — silently skipping a required
    // cancel/freeze. The read fails closed instead (the same discipline the
    // split/dividend collectors already apply).
    let store = store_of([
        symbol_change_record(300, "OLD", "NEW"),
        delisting_record(350, "OLD"),
        coverage_record(400, "NEW"),
    ]);
    assert!(matches!(
        store
            .query_corporate_action_facts(&daily_query("NEW", 0, 400))
            .unwrap_err(),
        CoverageError::AmbiguousLineage { .. }
    ));
    // The PRICE reads inherit the same fail-closed guard (one collector).
    assert!(matches!(
        store
            .query_split_adjusted(&daily_query("NEW", 0, 400))
            .unwrap_err(),
        CoverageError::AmbiguousLineage { .. }
    ));

    // A successor's merger dated BEFORE its validity began (NEW exists only
    // from the rename @300; the merger record says NEW converted @250) is
    // equally impossible — fail closed.
    let store = store_of([
        symbol_change_record(300, "OLD", "NEW"),
        merger_record(250, "NEW", "ACQ", 1, 1, 0),
        coverage_record(400, "NEW"),
    ]);
    assert!(matches!(
        store
            .query_corporate_action_facts(&daily_query("NEW", 0, 400))
            .unwrap_err(),
        CoverageError::AmbiguousLineage { .. }
    ));

    // The rename record ITSELF legitimately sits ON its segment's closing
    // boundary and still surfaces (a strict window check would reject every
    // legitimate rename) — pinned by the boundary case: a delisting of the
    // SUCCESSOR after the rename is consistent and surfaces alongside it.
    let store = store_of([
        symbol_change_record(300, "OLD", "NEW"),
        delisting_record(350, "NEW"),
        coverage_record(400, "NEW"),
    ]);
    let facts = store
        .query_corporate_action_facts(&daily_query("NEW", 0, 400))
        .expect("consistent lineage");
    assert_eq!(
        facts.iter().map(fact_kind).collect::<Vec<_>>(),
        vec!["symbol-change", "delisting"],
    );
}

#[test]
fn srs_data_021_querying_a_retired_predecessor_bounds_its_own_segment() {
    // The r4 finding: a consumer can hold (and query) the PREDECESSOR symbol.
    // OLD's own outgoing rename retires it @300, so a stale OLD action dated
    // after the retirement must FAIL the read closed — not surface as a fact an
    // applier would trust.
    let store = store_of([
        symbol_change_record(300, "OLD", "NEW"),
        delisting_record(350, "OLD"),
        coverage_record(400, "OLD"),
    ]);
    assert!(matches!(
        store
            .query_corporate_action_facts(&daily_query("OLD", 0, 400))
            .unwrap_err(),
        CoverageError::AmbiguousLineage { .. }
    ));

    // The legitimate held-predecessor journey still works: querying OLD
    // surfaces its pre-rename split AND the rename itself (the record sits on
    // the segment's closing boundary), in event order — exactly what a book
    // holding OLD needs to first adjust, then be carried onto NEW.
    let store = store_of([
        split_record("OLD", 200, 2, 1),
        symbol_change_record(300, "OLD", "NEW"),
        coverage_record(400, "OLD"),
    ]);
    let facts = store
        .query_corporate_action_facts(&daily_query("OLD", 0, 400))
        .expect("consistent predecessor query");
    assert_eq!(
        facts,
        vec![
            CorporateActionFact::Split {
                symbol: "OLD".to_string(),
                effective_ts: 200,
                numerator: 2,
                denominator: 1,
            },
            CorporateActionFact::SymbolChange {
                predecessor: "OLD".to_string(),
                successor: "NEW".to_string(),
                effective_ts: 300,
            },
        ]
    );

    // As-of semantics: a rename AFTER the query window's end has not happened
    // yet at the as-of instant, so it neither bounds the segment nor surfaces —
    // OLD's in-window actions still serve.
    let facts = store
        .query_corporate_action_facts(&daily_query("OLD", 0, 250))
        .expect("covered pre-rename window");
    assert_eq!(
        facts.len(),
        1,
        "the split only; the future rename is unseen"
    );
}

#[test]
fn srs_data_021_dividend_without_reference_close_fails_closed() {
    // A dividend with NO raw bar before its ex-date has no resolvable reference
    // close — the read fails closed (never a silently dropped or defaulted term).
    let store = store_of([
        dividend_record(300, "AAPL", 100),
        coverage_record(400, "AAPL"),
    ]);
    assert!(matches!(
        store
            .query_corporate_action_facts(&daily_query("AAPL", 0, 400))
            .unwrap_err(),
        CoverageError::Normalization(_)
    ));
}

#[test]
fn srs_data_021_empty_window_is_a_valid_covered_result() {
    let store = store_of([daily_bar("AAPL", 100, 4_000), coverage_record(400, "AAPL")]);
    let facts = store
        .query_corporate_action_facts(&daily_query("AAPL", 0, 400))
        .expect("covered");
    assert!(facts.is_empty(), "no actions is a result, not an error");
}

fn fact_kind(fact: &CorporateActionFact) -> &'static str {
    match fact {
        CorporateActionFact::Split { .. } => "split",
        CorporateActionFact::Dividend { .. } => "dividend",
        CorporateActionFact::Delisting { .. } => "delisting",
        CorporateActionFact::Merger { .. } => "merger",
        CorporateActionFact::SymbolChange { .. } => "symbol-change",
    }
}
