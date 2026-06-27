//! SRS-DATA-005 fundamental-statement record builder — vendor-neutral DTO → canonical store records.
//!
//! SRS-DATA-005 (SyRS SYS-26, NFR-P8d; StRS SN-3.03 / BG-3) ingests "income statement, balance
//! sheet, cash flow statement, and key ratio records for US equities … available to the factor
//! pipeline." This module is the data-layer half of that path: it turns a vendor-neutral
//! [`FundamentalStatements`] bundle (the boundary DTO a provider adapter — e.g. the Sharadar adapter
//! in `atp-adapters` — produces by mapping its vendor columns onto integer minor-unit fields) into
//! the four canonical [`MarketDataRecord`]s the unified store catalogs, one per statement type:
//!
//!   * [`FUNDAMENTAL_INCOME_RESOLUTION`]   `fundamental:income`   — revenue, net income
//!   * [`FUNDAMENTAL_BALANCE_RESOLUTION`]  `fundamental:balance`  — total assets / liabilities, book equity
//!   * [`FUNDAMENTAL_CASHFLOW_RESOLUTION`] `fundamental:cashflow` — operating / investing / financing CF
//!   * [`FUNDAMENTAL_RATIOS_RESOLUTION`]   `fundamental:ratios`   — the key-ratio inputs the factor reads
//!
//! All four records share the natural key `(DatasetKind::Fundamental, symbol, event_ts =
//! period_end_ts)` and differ only by resolution, so they are four distinct catalog rows (never a
//! false duplicate). Each carries `available_ts` (the filing instant) so a point-in-time consumer of
//! ANY statement type can gate on availability — not just the ratios reader.
//!
//! ## Known limitation — one authoritative filing per period (restatements deferred)
//!
//! Because the natural key carries `period_end_ts` but NOT a filing-version discriminator (the
//! `available_ts` is a value field, not part of the key), this models exactly ONE authoritative
//! filing per `(symbol, resolution, fiscal period)`. A later RESTATEMENT / amended filing for the
//! same period (a different filing date and revised numbers) is a same-key / different-content write,
//! which the SRS-DATA-016 idempotency core fails CLOSED on (`StoreError::ConflictingContent`) — so a
//! restatement is rejected (no corruption, no lookahead), but not yet cataloged as point-in-time
//! history. Representing multiple filings per period (a filing-version dimension + a loader that
//! selects the latest filing available as-of the run date) is a STORAGE-SCHEMA change deferred with
//! the real Sharadar restatement feed; see the `fundamental_ingestion_contract` deferred owners and
//! the `srs_data_005_restatement_currently_fails_closed_pending_filing_version_keying` regression pin.
//!
//! ## The factor-pipeline contract (why the ratios record is shaped exactly so)
//!
//! `atp-factor-pipeline`'s `load_fundamental_input` reads the `fundamental:ratios` record at
//! `DatasetKind::Fundamental` and requires EXACTLY the fields `available_ts`, `net_income_minor`,
//! `book_equity_minor`, `market_value_minor`; it derives `earnings_yield = net_income / market_value`
//! and `book_to_price = book_equity / market_value`, requires a POSITIVE `market_value_minor`, and
//! gates point-in-time on `available_ts`. So this builder emits a ratios record with precisely those
//! four fields. The resolution literal is duplicated here (the core data layer must NOT depend on the
//! factor-pipeline crate — that would invert the SRS-ARCH-002 dependency direction); the SRS-DATA-005
//! integration test reads a built ratios record back THROUGH the real loader, so any drift between the
//! two literals fails the test. This module names NO vendor (SRS-ARCH-003).
//!
//! The [`FundamentalStatements`] constructor already guarantees a non-empty symbol, a non-negative
//! period end, `available_ts >= period_end_ts` (no impossible provenance), and a positive market value
//! — so the records this builder produces are always well-formed and the ratios record is always
//! loader-readable. [`build_fundamental_records`] still returns a [`StoreError`] because
//! [`MarketDataRecord::new`] is fallible; a valid bundle never triggers it.

use atp_types::FundamentalStatements;

use crate::store::{DatasetKind, MarketDataRecord, MarketField, NaturalKey, StoreError};

/// The income-statement resolution label (`revenue_minor`, `net_income_minor`).
pub const FUNDAMENTAL_INCOME_RESOLUTION: &str = "fundamental:income";
/// The balance-sheet resolution label (`total_assets_minor`, `total_liabilities_minor`,
/// `book_equity_minor`).
pub const FUNDAMENTAL_BALANCE_RESOLUTION: &str = "fundamental:balance";
/// The cash-flow-statement resolution label (`operating_/investing_/financing_cash_flow_minor`).
pub const FUNDAMENTAL_CASHFLOW_RESOLUTION: &str = "fundamental:cashflow";
/// The key-ratio resolution label. MUST equal `atp_factor_pipeline::store_inputs`'s
/// `FUNDAMENTAL_RATIOS_RESOLUTION` (the data layer cannot depend on that crate to share the const);
/// the SRS-DATA-005 integration test reads a built record back through the real loader, so any drift
/// fails closed.
pub const FUNDAMENTAL_RATIOS_RESOLUTION: &str = "fundamental:ratios";

fn field(name: &str, value_minor: i64) -> MarketField {
    MarketField {
        name: name.to_string(),
        value_minor,
    }
}

fn fundamental_key(symbol: &str, resolution: &str, period_end_ts: i64) -> NaturalKey {
    NaturalKey {
        kind: DatasetKind::Fundamental,
        symbol: symbol.to_string(),
        resolution: resolution.to_string(),
        event_ts: period_end_ts,
        option_contract: None,
    }
}

/// Build the four canonical `Fundamental` store records for one fiscal period from a validated
/// [`FundamentalStatements`] bundle. Returns them in resolution order (income, balance, cashflow,
/// ratios); the store re-orders into canonical natural-key order on ingest, so the order is not
/// load-bearing. Each record carries `available_ts` for point-in-time consumers, and the ratios
/// record carries exactly the four fields the factor loader reads.
pub fn build_fundamental_records(
    statements: &FundamentalStatements,
) -> Result<Vec<MarketDataRecord>, StoreError> {
    let symbol = statements.symbol();
    let period_end_ts = statements.period_end_ts();
    let available_ts = statements.available_ts();

    let income = MarketDataRecord::new(
        fundamental_key(symbol, FUNDAMENTAL_INCOME_RESOLUTION, period_end_ts),
        [
            field("available_ts", available_ts),
            field("revenue_minor", statements.revenue_minor()),
            field("net_income_minor", statements.net_income_minor()),
        ],
    )?;
    let balance = MarketDataRecord::new(
        fundamental_key(symbol, FUNDAMENTAL_BALANCE_RESOLUTION, period_end_ts),
        [
            field("available_ts", available_ts),
            field("total_assets_minor", statements.total_assets_minor()),
            field("total_liabilities_minor", statements.total_liabilities_minor()),
            field("book_equity_minor", statements.book_equity_minor()),
        ],
    )?;
    let cashflow = MarketDataRecord::new(
        fundamental_key(symbol, FUNDAMENTAL_CASHFLOW_RESOLUTION, period_end_ts),
        [
            field("available_ts", available_ts),
            field("operating_cash_flow_minor", statements.operating_cash_flow_minor()),
            field("investing_cash_flow_minor", statements.investing_cash_flow_minor()),
            field("financing_cash_flow_minor", statements.financing_cash_flow_minor()),
        ],
    )?;
    // The ratios record carries EXACTLY the factor loader's required fields (available_ts,
    // net_income_minor, book_equity_minor, market_value_minor) so a built bundle is always readable
    // by load_fundamental_input.
    let ratios = MarketDataRecord::new(
        fundamental_key(symbol, FUNDAMENTAL_RATIOS_RESOLUTION, period_end_ts),
        [
            field("available_ts", available_ts),
            field("net_income_minor", statements.net_income_minor()),
            field("book_equity_minor", statements.book_equity_minor()),
            field("market_value_minor", statements.market_value_minor()),
        ],
    )?;

    Ok(vec![income, balance, cashflow, ratios])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> FundamentalStatements {
        FundamentalStatements::new(
            "AAPL",
            1_700_000_000,
            1_702_000_000,
            5_000_000,
            -250_000,
            8_000_000,
            3_000_000,
            5_000_000,
            900_000,
            -400_000,
            -100_000,
            20_000_000,
        )
        .expect("sample statements are well-formed")
    }

    fn record<'a>(records: &'a [MarketDataRecord], resolution: &str) -> &'a MarketDataRecord {
        records
            .iter()
            .find(|r| r.key().resolution == resolution)
            .unwrap_or_else(|| panic!("missing {resolution} record"))
    }

    fn field_value(record: &MarketDataRecord, name: &str) -> i64 {
        record
            .fields()
            .iter()
            .find(|f| f.name == name)
            .unwrap_or_else(|| panic!("missing field {name}"))
            .value_minor
    }

    #[test]
    fn builds_four_statement_records() {
        let records = build_fundamental_records(&sample()).expect("build succeeds");
        assert_eq!(records.len(), 4);
        // All four share the natural key except resolution.
        for r in &records {
            assert_eq!(r.key().kind, DatasetKind::Fundamental);
            assert_eq!(r.key().symbol, "AAPL");
            assert_eq!(r.key().event_ts, 1_700_000_000);
            assert_eq!(r.key().option_contract, None);
            // Every statement carries the availability instant for point-in-time consumers.
            assert_eq!(field_value(r, "available_ts"), 1_702_000_000);
        }
    }

    #[test]
    fn ratios_record_matches_loader_contract() {
        let records = build_fundamental_records(&sample()).expect("build succeeds");
        let ratios = record(&records, FUNDAMENTAL_RATIOS_RESOLUTION);
        // Exactly the four fields the factor loader reads, no more.
        let mut names: Vec<&str> = ratios.fields().iter().map(|f| f.name.as_str()).collect();
        names.sort_unstable();
        assert_eq!(
            names,
            ["available_ts", "book_equity_minor", "market_value_minor", "net_income_minor"]
        );
        assert_eq!(field_value(ratios, "net_income_minor"), -250_000);
        assert_eq!(field_value(ratios, "book_equity_minor"), 5_000_000);
        assert_eq!(field_value(ratios, "market_value_minor"), 20_000_000);
    }

    #[test]
    fn income_balance_cashflow_carry_their_line_items() {
        let records = build_fundamental_records(&sample()).expect("build succeeds");
        assert_eq!(
            field_value(record(&records, FUNDAMENTAL_INCOME_RESOLUTION), "revenue_minor"),
            5_000_000
        );
        assert_eq!(
            field_value(record(&records, FUNDAMENTAL_BALANCE_RESOLUTION), "total_assets_minor"),
            8_000_000
        );
        assert_eq!(
            field_value(
                record(&records, FUNDAMENTAL_CASHFLOW_RESOLUTION),
                "operating_cash_flow_minor"
            ),
            900_000
        );
    }
}
