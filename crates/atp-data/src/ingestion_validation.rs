//! SRS-DATA-013 / SyRS SYS-77 / ERR-5 — the concrete ingestion-validation rule logic.
//!
//! The ERR-5 gate ([`DataLayer::ingest_record`](crate::DataLayer::ingest_record)) and its ports
//! ([`RecordValidator`](crate::RecordValidator) / [`IngestionValidationEventSink`](crate::IngestionValidationEventSink))
//! define the *mechanism* — probe a record, and on `Quarantined` emit one structured event and refuse
//! the primary write. This module supplies the three pieces DATA-013 owns:
//!
//!   1. [`Sys77RecordValidator`] — the real, read-only, kind-aware classifier that applies SyRS SYS-77
//!      rules (a)..(f) to a canonical [`MarketDataRecord`] (equity OHLCV bars *and* option-chain
//!      snapshots), returning exactly one [`QuarantineReason`] per rejected record.
//!   2. [`QuarantineSummarySink`] — an [`IngestionValidationEventSink`](crate::IngestionValidationEventSink)
//!      that aggregates the per-record events into the "count and nature of quarantined records" the
//!      SyRS SYS-77 alert clause requires (counts-by-reason + total).
//!   3. [`DataLayer::ingest_market_records_quarantining`] — the **quarantine-and-continue** batch write
//!      path: quarantined records are dropped (never written to the primary tables) while the valid
//!      subset is written SSD-first + NAS-synced through the SRS-DATA-008 tier, and the summary reports
//!      how many were quarantined and why.
//!
//! Out of scope (deferred, per `architecture/runtime_services.json` → `ingestion_validation_contract`):
//! the durable *quarantine store* that persists rejected payloads (SRS-DATA-014 / DATA-015) and the
//! dashboard/notification *display* fan-out (SRS-UI-001 / SRS-NOTIF-001). This module produces the
//! structured counts-and-reasons those surfaces will consume; it does not render them.

use std::cell::RefCell;
use std::collections::{BTreeSet, HashMap};

use atp_types::{IngestionValidationEvent, QuarantineReason, RecordValidationOutcome};

use crate::store::{DatasetKind, MarketDataRecord, MarketField, NaturalKey};
use crate::tiering::{TierIngestOutcome, TieredStore};
use crate::{DataLayer, IngestionValidationEventSink, MarketIngestError, RecordValidator};

/// The six SyRS SYS-77 rule categories in a fixed canonical order — the order the CLI prints counts
/// and the order [`QuarantineSummary::per_reason`] returns, so operator output is deterministic.
pub const ALL_QUARANTINE_REASONS: [QuarantineReason; 6] = [
    QuarantineReason::RangeViolation,
    QuarantineReason::OhlcOutOfBand,
    QuarantineReason::NegativeVolume,
    QuarantineReason::NullRequiredField,
    QuarantineReason::DuplicateRecord,
    QuarantineReason::OptionFieldMissing,
];

// --------------------------------------------------------------------------- //
// The SYS-77 validator
// --------------------------------------------------------------------------- //

/// The concrete SRS-DATA-013 / SyRS SYS-77 record validator.
///
/// Read-only and **kind-aware**: OHLCV equity bars ([`DatasetKind::DailyEquityBar`] /
/// [`DatasetKind::MinuteEquityBar`]) are checked against rules (a)..(d); option-chain snapshots
/// ([`DatasetKind::OptionChainSnapshot`]) against rule (f) (SYS-23 required fields) plus the
/// non-negativity range checks. Fundamentals and corporate-action facts are outside SYS-77's
/// OHLCV/option rule set (their own validation is SRS-DATA-005 / 011 / 012) so they are subject only
/// to the cross-record duplicate check (rule e).
///
/// **Exactly one reason per record**, evaluated in a fixed deterministic order so the classification
/// is reproducible:
///   1. rule (d) required fields present (a range check needs the field to exist first),
///   2. rules (a)/(b)/(c) OHLC range / band / non-negative volume, or rule (f) option field presence +
///      non-negativity,
///   3. rule (e) duplicate natural key — checked **last and only against previously-admitted (valid)
///      records**, so a record already quarantined for a field/range violation never anchors a later
///      duplicate (no double jeopardy).
///
/// **Batch-scoped and stateful.** Rule (e) needs cross-record state, held here as an interior-mutable
/// set of the natural keys already admitted within the batch. The ERR-5 gate probes `validate` exactly
/// once per record in batch order, so the set accumulates correctly. Construct a **fresh validator per
/// batch** ([`Sys77RecordValidator::new`]) — reusing one across batches would carry keys forward and
/// mis-flag a legitimate record as a duplicate.
///
/// Cross-run idempotency (re-running an ingestion job) remains [`MarketDataStore::upsert`]'s
/// responsibility (SRS-DATA-016): the valid subset still flows through the idempotent tier write, which
/// collapses an identical re-ingest to a no-op. Rule (e) here is the *within-batch* duplicate guard.
#[derive(Debug, Default)]
pub struct Sys77RecordValidator {
    /// Natural keys already admitted as `Valid` within this batch. A repeat is a `DuplicateRecord`.
    seen_valid_keys: RefCell<BTreeSet<NaturalKey>>,
}

impl Sys77RecordValidator {
    /// A fresh validator for one ingestion batch (empty seen-key set).
    pub fn new() -> Self {
        Self::default()
    }
}

impl RecordValidator for Sys77RecordValidator {
    fn validate(&self, record: &MarketDataRecord) -> RecordValidationOutcome {
        // Rules (a)..(d)/(f): field presence + range, per kind. A violation short-circuits with its
        // reason, so a malformed record is never admitted to the seen-key set below.
        if let Some(reason) = classify_fields(record) {
            return RecordValidationOutcome::Quarantined(reason);
        }
        // Rule (e): within-batch duplicate natural key — only otherwise-valid records participate.
        let key = record.key().clone();
        let mut seen = self.seen_valid_keys.borrow_mut();
        if seen.contains(&key) {
            return RecordValidationOutcome::Quarantined(QuarantineReason::DuplicateRecord);
        }
        seen.insert(key);
        RecordValidationOutcome::Valid
    }
}

/// The field/range/option rules (everything except the cross-record duplicate check). Returns the
/// violated rule's reason, or `None` if the record's fields are all well-formed for its kind.
fn classify_fields(record: &MarketDataRecord) -> Option<QuarantineReason> {
    match record.key().kind {
        DatasetKind::DailyEquityBar | DatasetKind::MinuteEquityBar => classify_ohlcv(record),
        DatasetKind::OptionChainSnapshot => classify_option(record),
        // Not in SYS-77's OHLCV/option rule set — only the duplicate check (rule e) applies.
        DatasetKind::Fundamental
        | DatasetKind::CorporateActionSplit
        | DatasetKind::CorporateActionCoverage => None,
    }
}

/// SYS-77 (a)..(d) for an OHLCV equity bar. `symbol` + `date` (the natural key) are structurally
/// guaranteed present by [`MarketDataRecord::new`], so rule (d) reduces to the five value fields.
fn classify_ohlcv(record: &MarketDataRecord) -> Option<QuarantineReason> {
    // (d) required OHLCV value fields present.
    let (open, high, low, close, volume) = match (
        field_value(record, "open"),
        field_value(record, "high"),
        field_value(record, "low"),
        field_value(record, "close"),
        field_value(record, "volume"),
    ) {
        (Some(o), Some(h), Some(l), Some(c), Some(v)) => (o, h, l, c, v),
        _ => return Some(QuarantineReason::NullRequiredField),
    };
    // (a) High >= Low >= 0.
    if low < 0 || high < low {
        return Some(QuarantineReason::RangeViolation);
    }
    // (b) Open and Close within [Low, High].
    if open < low || open > high || close < low || close > high {
        return Some(QuarantineReason::OhlcOutOfBand);
    }
    // (c) Volume >= 0.
    if volume < 0 {
        return Some(QuarantineReason::NegativeVolume);
    }
    None
}

/// SYS-77 (f) for an option-chain snapshot: the SYS-23 required fields must be present, plus the
/// non-negativity range checks. The OCC contract identity lives on the natural key.
fn classify_option(record: &MarketDataRecord) -> Option<QuarantineReason> {
    // (f) the OCC option contract must be present and non-empty (also enforced structurally at
    // construction; re-checked here so the SYS-77 reason is reported for a malformed identity).
    let contract_present = record
        .key()
        .option_contract
        .as_ref()
        .is_some_and(|c| !c.trim().is_empty());
    if !contract_present {
        return Some(QuarantineReason::OptionFieldMissing);
    }
    // (f) SYS-23 required value fields present.
    let (bid, ask, last, volume, open_interest, implied_vol) = match (
        field_value(record, "bid"),
        field_value(record, "ask"),
        field_value(record, "last"),
        field_value(record, "volume"),
        field_value(record, "open_interest"),
        field_value(record, "implied_vol_micros"),
    ) {
        (Some(b), Some(a), Some(l), Some(v), Some(oi), Some(iv)) => (b, a, l, v, oi, iv),
        _ => return Some(QuarantineReason::OptionFieldMissing),
    };
    // (c) Volume >= 0.
    if volume < 0 {
        return Some(QuarantineReason::NegativeVolume);
    }
    // Range: quote prices, open interest, and implied volatility are non-negative quantities.
    if bid < 0 || ask < 0 || last < 0 || open_interest < 0 || implied_vol < 0 {
        return Some(QuarantineReason::RangeViolation);
    }
    None
}

/// The integer-minor value of the named field, or `None` if the record does not carry it.
fn field_value(record: &MarketDataRecord, name: &str) -> Option<i64> {
    record
        .fields()
        .iter()
        .find(|f| f.name == name)
        .map(|f| f.value_minor)
}

// --------------------------------------------------------------------------- //
// Counts-and-reasons summary sink
// --------------------------------------------------------------------------- //

/// The "count and nature of quarantined records" (SyRS SYS-77 alert clause): a total plus a breakdown
/// by [`QuarantineReason`]. Built by [`QuarantineSummarySink`] from the per-record events the gate
/// emits; consumed by the operator CLI and (eventually) the deferred dashboard/notification surfaces.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct QuarantineSummary {
    /// Total records quarantined across the batch (== sum of the per-reason counts).
    pub quarantined_total: u64,
    /// Per-reason counts. Private so the only read paths are [`count`](Self::count) /
    /// [`per_reason`](Self::per_reason), which impose the canonical reason order.
    counts: HashMap<QuarantineReason, u64>,
}

impl QuarantineSummary {
    /// The number of records quarantined for `reason` (0 if none).
    pub fn count(&self, reason: QuarantineReason) -> u64 {
        self.counts.get(&reason).copied().unwrap_or(0)
    }

    /// Every reason with its count, in the fixed [`ALL_QUARANTINE_REASONS`] order (deterministic
    /// operator output; zero-count reasons included).
    pub fn per_reason(&self) -> Vec<(QuarantineReason, u64)> {
        ALL_QUARANTINE_REASONS
            .iter()
            .map(|&reason| (reason, self.count(reason)))
            .collect()
    }
}

/// An [`IngestionValidationEventSink`](crate::IngestionValidationEventSink) that aggregates the ERR-5
/// per-record events into a [`QuarantineSummary`]. Interior-mutable so the gate can `record` into a
/// shared `&self` while the batch loop runs (single-threaded ingestion).
#[derive(Debug, Default)]
pub struct QuarantineSummarySink {
    inner: RefCell<QuarantineSummary>,
}

impl QuarantineSummarySink {
    /// A fresh sink (empty summary).
    pub fn new() -> Self {
        Self::default()
    }

    /// A snapshot of the aggregated counts-and-reasons so far.
    pub fn summary(&self) -> QuarantineSummary {
        self.inner.borrow().clone()
    }
}

impl IngestionValidationEventSink for QuarantineSummarySink {
    fn record(&self, event: IngestionValidationEvent) {
        let mut summary = self.inner.borrow_mut();
        summary.quarantined_total += 1;
        *summary.counts.entry(event.reason).or_insert(0) += 1;
    }
}

// --------------------------------------------------------------------------- //
// Quarantine-and-continue batch write path
// --------------------------------------------------------------------------- //

/// The outcome of [`DataLayer::ingest_market_records_quarantining`]: how many records were written to
/// the primary tier, how many were quarantined, and the tier's SSD-first + NAS-sync result for the
/// valid subset.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QuarantiningIngestionOutcome {
    /// Records that passed SYS-77 validation and were handed to the tier for the primary write.
    pub written: usize,
    /// Records quarantined (never written to primary). Equals the paired sink's `quarantined_total`.
    pub quarantined: usize,
    /// The SSD-first durable write + NAS archival sync result for the valid subset.
    pub tier: TierIngestOutcome,
}

impl DataLayer {
    /// SRS-DATA-013 / SyRS SYS-77 — the **quarantine-and-continue** market-data ingestion path.
    ///
    /// Unlike [`ingest_market_records_tiered`](Self::ingest_market_records_tiered) (SRS-DATA-008, which
    /// fails the whole batch closed on the first invalid record), this path implements the SYS-77
    /// disposition: each record is validated read-only through the unchanged ERR-5 gate; a **quarantined
    /// record is dropped** (its structured event is emitted through `events` for the count-and-nature
    /// summary and it is **never written to the primary tables**), while the batch **continues**; the
    /// **valid subset** is then written through the SRS-DATA-008 tier (SSD-first durable write + NAS
    /// sync), so no valid record is lost and no invalid record reaches primary storage.
    ///
    /// Routing the valid subset through [`TieredStore::ingest`] keeps SRS-DATA-008's "all ingestion is
    /// SSD-first + NAS-synced" invariant intact. The same trust boundary as the sibling paths applies:
    /// corporate-action COVERAGE ([`DatasetKind::CorporateActionCoverage`]) is refused
    /// ([`MarketIngestError::UnsupportedKind`]) — it is an operator trust assertion, not provider
    /// market data.
    ///
    /// Pass a **fresh** [`Sys77RecordValidator`] per call so its within-batch duplicate detection
    /// (rule e) starts clean, and a [`QuarantineSummarySink`] to collect the counts and reasons.
    pub fn ingest_market_records_quarantining<V, S>(
        &self,
        tier: &TieredStore,
        records: impl IntoIterator<Item = MarketDataRecord>,
        validator: &V,
        events: &S,
        observed_at_seconds: u64,
    ) -> Result<QuarantiningIngestionOutcome, MarketIngestError>
    where
        V: RecordValidator,
        S: IngestionValidationEventSink,
    {
        let mut valid: Vec<MarketDataRecord> = Vec::new();
        let mut quarantined = 0usize;
        for record in records {
            // Same trust boundary as ingest_market_record / ingest_market_records_tiered: the tier is
            // not a second path to a trusted coverage frontier, so refuse COVERAGE here too.
            if record.key().kind == DatasetKind::CorporateActionCoverage {
                return Err(MarketIngestError::UnsupportedKind {
                    kind: record.key().kind.as_str(),
                });
            }
            match self.ingest_record(&record, validator, events, observed_at_seconds) {
                // Valid → collect for the primary write.
                Ok(_) => valid.push(record),
                // Quarantined → the gate already emitted the structured IngestionValidationEvent
                // through `events` (the counts-and-reasons aggregation). Drop the record (no primary
                // write) and CONTINUE the batch. This is the SYS-77 quarantine disposition.
                Err(_) => quarantined += 1,
            }
        }
        let written = valid.len();
        // Write ONLY the valid subset — SSD-first durable, then NAS sync — through the SRS-DATA-008
        // tier. Quarantined records are absent from `valid`, so they never reach the primary tables.
        let tier_outcome = tier.ingest(valid)?;
        Ok(QuarantiningIngestionOutcome {
            written,
            quarantined,
            tier: tier_outcome,
        })
    }
}

// --------------------------------------------------------------------------- //
// Deterministic mixed fixture (valid + one malformed record per SYS-77 rule)
// --------------------------------------------------------------------------- //

/// A deterministic mixed batch for the operator CLI and the integration test: the well-formed daily
/// and option fixtures plus **one deliberately-malformed record per SYS-77 rule**, so a single ingest
/// exercises quarantine-and-continue with every [`QuarantineReason`]. Order is fixed (valid records
/// first, then the malformed ones, duplicate last) so re-running is reproducible.
///
/// Composition: 2 valid daily bars (AAPL, MSFT) + 2 valid option snapshots, then malformed:
/// `NullRequiredField` (bar missing `close`), `RangeViolation` (bar `high < low`),
/// `OhlcOutOfBand` (bar `open > high`), `NegativeVolume` (bar `volume < 0`),
/// `OptionFieldMissing` (option missing `implied_vol_micros`), and `DuplicateRecord`
/// (a second AAPL daily bar sharing the first's natural key).
pub fn mixed_validation_fixture(event_ts: i64) -> Vec<MarketDataRecord> {
    // --- Valid records (seed the primary tables + the duplicate anchor). --------------------- //
    let mut batch = vec![
        ohlcv("AAPL", "1d", event_ts, 150),
        ohlcv("MSFT", "1d", event_ts, 320),
        option("AAPL  240119C00150000", event_ts, 50),
        option("AAPL  240119P00150000", event_ts, 40),
    ];

    // --- Malformed: one record per SYS-77 rule. ---------------------------------------------- //
    // (d) NullRequiredField — a daily bar missing `close`.
    batch.push(make_record(
        DatasetKind::DailyEquityBar,
        "TSLA",
        "1d",
        event_ts,
        None,
        &[("open", 100), ("high", 120), ("low", 90), ("volume", 1_000)],
    ));
    // (a) RangeViolation — `high < low`.
    batch.push(make_record(
        DatasetKind::DailyEquityBar,
        "NVDA",
        "1d",
        event_ts,
        None,
        &[
            ("open", 100),
            ("high", 50),
            ("low", 90),
            ("close", 70),
            ("volume", 1_000),
        ],
    ));
    // (b) OhlcOutOfBand — `open` above `high`.
    batch.push(make_record(
        DatasetKind::DailyEquityBar,
        "AMZN",
        "1d",
        event_ts,
        None,
        &[
            ("open", 500),
            ("high", 120),
            ("low", 90),
            ("close", 110),
            ("volume", 1_000),
        ],
    ));
    // (c) NegativeVolume — `volume < 0`.
    batch.push(make_record(
        DatasetKind::DailyEquityBar,
        "META",
        "1d",
        event_ts,
        None,
        &[
            ("open", 100),
            ("high", 120),
            ("low", 90),
            ("close", 110),
            ("volume", -5),
        ],
    ));
    // (f) OptionFieldMissing — option snapshot missing `implied_vol_micros`.
    batch.push(make_record(
        DatasetKind::OptionChainSnapshot,
        "AAPL",
        "chain",
        event_ts,
        Some("AAPL  240119C00160000"),
        &[
            ("bid", 100),
            ("ask", 120),
            ("last", 110),
            ("volume", 25),
            ("open_interest", 300),
        ],
    ));
    // (e) DuplicateRecord — a second AAPL daily bar sharing the first valid bar's natural key.
    batch.push(ohlcv("AAPL", "1d", event_ts, 151));

    batch
}

/// A well-formed OHLCV daily/minute bar fixture (low < open,close < high; positive volume).
fn ohlcv(symbol: &str, resolution: &str, event_ts: i64, base: i64) -> MarketDataRecord {
    make_record(
        DatasetKind::DailyEquityBar,
        symbol,
        resolution,
        event_ts,
        None,
        &[
            ("open", base),
            ("high", base + 10),
            ("low", base - 10),
            ("close", base + 2),
            ("volume", base * 100),
        ],
    )
}

/// A well-formed option-chain snapshot fixture (all SYS-23 fields present, non-negative).
fn option(contract: &str, event_ts: i64, seed: i64) -> MarketDataRecord {
    make_record(
        DatasetKind::OptionChainSnapshot,
        "AAPL",
        "chain",
        event_ts,
        Some(contract),
        &[
            ("bid", seed * 100),
            ("ask", seed * 100 + 25),
            ("last", seed * 100 + 10),
            ("volume", seed * 5),
            ("open_interest", seed * 40),
            ("implied_vol_micros", 250_000 + seed),
        ],
    )
}

/// Build a [`MarketDataRecord`] from raw parts. `MarketDataRecord::new` enforces only structural
/// coherence (non-empty symbol/resolution, canonical field names, option-contract/kind agreement) —
/// NOT the SYS-77 range/required-OHLCV rules — so a record with a missing OHLCV field or an out-of-band
/// value is constructible here and is exactly what the validator must catch.
fn make_record(
    kind: DatasetKind,
    symbol: &str,
    resolution: &str,
    event_ts: i64,
    option_contract: Option<&str>,
    fields: &[(&str, i64)],
) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind,
            symbol: symbol.to_string(),
            resolution: resolution.to_string(),
            event_ts,
            option_contract: option_contract.map(|c| c.to_string()),
        },
        fields.iter().map(|(name, value)| MarketField {
            name: name.to_string(),
            value_minor: *value,
        }),
    )
    .expect("mixed_validation_fixture builds structurally-coherent records")
}

#[cfg(test)]
mod tests {
    use super::*;

    const TS: i64 = 1_700_000_000;

    fn validate(record: &MarketDataRecord) -> RecordValidationOutcome {
        Sys77RecordValidator::new().validate(record)
    }

    fn quarantined(record: &MarketDataRecord) -> QuarantineReason {
        match validate(record) {
            RecordValidationOutcome::Quarantined(reason) => reason,
            RecordValidationOutcome::Valid => panic!("expected Quarantined, got Valid"),
        }
    }

    #[test]
    fn valid_ohlcv_bar_passes() {
        assert_eq!(
            validate(&ohlcv("AAPL", "1d", TS, 150)),
            RecordValidationOutcome::Valid
        );
    }

    #[test]
    fn valid_option_snapshot_passes() {
        assert_eq!(
            validate(&option("AAPL  240119C00150000", TS, 50)),
            RecordValidationOutcome::Valid
        );
    }

    #[test]
    fn boundary_values_are_valid() {
        // High == Low == Open == Close == 0 (rule a: High >= Low >= 0 holds with equality; rule b:
        // Open/Close within [Low, High] holds at the endpoints), Volume == 0 (rule c allows zero).
        let rec = make_record(
            DatasetKind::DailyEquityBar,
            "FLAT",
            "1d",
            TS,
            None,
            &[
                ("open", 0),
                ("high", 0),
                ("low", 0),
                ("close", 0),
                ("volume", 0),
            ],
        );
        assert_eq!(validate(&rec), RecordValidationOutcome::Valid);
    }

    #[test]
    fn missing_required_ohlcv_field_is_null_required_field() {
        let rec = make_record(
            DatasetKind::DailyEquityBar,
            "TSLA",
            "1d",
            TS,
            None,
            &[("open", 100), ("high", 120), ("low", 90), ("volume", 1_000)],
        );
        assert_eq!(quarantined(&rec), QuarantineReason::NullRequiredField);
    }

    #[test]
    fn high_below_low_is_range_violation() {
        let rec = make_record(
            DatasetKind::DailyEquityBar,
            "NVDA",
            "1d",
            TS,
            None,
            &[
                ("open", 70),
                ("high", 50),
                ("low", 90),
                ("close", 60),
                ("volume", 1),
            ],
        );
        assert_eq!(quarantined(&rec), QuarantineReason::RangeViolation);
    }

    #[test]
    fn negative_low_is_range_violation() {
        let rec = make_record(
            DatasetKind::DailyEquityBar,
            "NEG",
            "1d",
            TS,
            None,
            &[
                ("open", 5),
                ("high", 10),
                ("low", -1),
                ("close", 5),
                ("volume", 1),
            ],
        );
        assert_eq!(quarantined(&rec), QuarantineReason::RangeViolation);
    }

    #[test]
    fn open_above_high_is_ohlc_out_of_band() {
        let rec = make_record(
            DatasetKind::DailyEquityBar,
            "AMZN",
            "1d",
            TS,
            None,
            &[
                ("open", 500),
                ("high", 120),
                ("low", 90),
                ("close", 110),
                ("volume", 1),
            ],
        );
        assert_eq!(quarantined(&rec), QuarantineReason::OhlcOutOfBand);
    }

    #[test]
    fn close_below_low_is_ohlc_out_of_band() {
        let rec = make_record(
            DatasetKind::DailyEquityBar,
            "AMZN",
            "1d",
            TS,
            None,
            &[
                ("open", 100),
                ("high", 120),
                ("low", 90),
                ("close", 10),
                ("volume", 1),
            ],
        );
        assert_eq!(quarantined(&rec), QuarantineReason::OhlcOutOfBand);
    }

    #[test]
    fn negative_volume_is_negative_volume() {
        let rec = make_record(
            DatasetKind::MinuteEquityBar,
            "META",
            "1m",
            TS,
            None,
            &[
                ("open", 100),
                ("high", 120),
                ("low", 90),
                ("close", 110),
                ("volume", -5),
            ],
        );
        assert_eq!(quarantined(&rec), QuarantineReason::NegativeVolume);
    }

    #[test]
    fn option_missing_required_field_is_option_field_missing() {
        let rec = make_record(
            DatasetKind::OptionChainSnapshot,
            "AAPL",
            "chain",
            TS,
            Some("AAPL  240119C00160000"),
            &[
                ("bid", 100),
                ("ask", 120),
                ("last", 110),
                ("volume", 25),
                ("open_interest", 300),
            ],
        );
        assert_eq!(quarantined(&rec), QuarantineReason::OptionFieldMissing);
    }

    #[test]
    fn option_negative_volume_is_negative_volume() {
        let rec = make_record(
            DatasetKind::OptionChainSnapshot,
            "AAPL",
            "chain",
            TS,
            Some("AAPL  240119C00160000"),
            &[
                ("bid", 100),
                ("ask", 120),
                ("last", 110),
                ("volume", -1),
                ("open_interest", 300),
                ("implied_vol_micros", 250_000),
            ],
        );
        assert_eq!(quarantined(&rec), QuarantineReason::NegativeVolume);
    }

    #[test]
    fn option_negative_price_is_range_violation() {
        let rec = make_record(
            DatasetKind::OptionChainSnapshot,
            "AAPL",
            "chain",
            TS,
            Some("AAPL  240119C00160000"),
            &[
                ("bid", -1),
                ("ask", 120),
                ("last", 110),
                ("volume", 25),
                ("open_interest", 300),
                ("implied_vol_micros", 250_000),
            ],
        );
        assert_eq!(quarantined(&rec), QuarantineReason::RangeViolation);
    }

    #[test]
    fn ohlcv_evaluation_order_reports_required_before_range() {
        // A record that is BOTH missing `close` AND has high < low must report the FIRST rule in the
        // deterministic order (required-field), never the later range rule.
        let rec = make_record(
            DatasetKind::DailyEquityBar,
            "BOTH",
            "1d",
            TS,
            None,
            &[("open", 70), ("high", 50), ("low", 90), ("volume", 1)],
        );
        assert_eq!(quarantined(&rec), QuarantineReason::NullRequiredField);
    }

    #[test]
    fn within_batch_duplicate_key_is_quarantined_second() {
        let validator = Sys77RecordValidator::new();
        let first = ohlcv("AAPL", "1d", TS, 150);
        let second = ohlcv("AAPL", "1d", TS, 151); // same natural key, different values
        assert_eq!(validator.validate(&first), RecordValidationOutcome::Valid);
        assert_eq!(
            validator.validate(&second),
            RecordValidationOutcome::Quarantined(QuarantineReason::DuplicateRecord)
        );
    }

    #[test]
    fn distinct_keys_are_not_duplicates() {
        let validator = Sys77RecordValidator::new();
        assert_eq!(
            validator.validate(&ohlcv("AAPL", "1d", TS, 150)),
            RecordValidationOutcome::Valid
        );
        assert_eq!(
            validator.validate(&ohlcv("MSFT", "1d", TS, 150)),
            RecordValidationOutcome::Valid
        );
    }

    #[test]
    fn quarantined_record_does_not_anchor_a_later_duplicate() {
        // No double jeopardy: a bad-range AAPL bar is quarantined for RangeViolation and does NOT seed
        // the seen-key set, so a subsequent well-formed AAPL bar with the same key is Valid, not a dup.
        let validator = Sys77RecordValidator::new();
        let bad = make_record(
            DatasetKind::DailyEquityBar,
            "AAPL",
            "1d",
            TS,
            None,
            &[
                ("open", 70),
                ("high", 50),
                ("low", 90),
                ("close", 60),
                ("volume", 1),
            ],
        );
        let good = ohlcv("AAPL", "1d", TS, 150);
        assert_eq!(
            validator.validate(&bad),
            RecordValidationOutcome::Quarantined(QuarantineReason::RangeViolation)
        );
        assert_eq!(validator.validate(&good), RecordValidationOutcome::Valid);
    }

    #[test]
    fn summary_sink_aggregates_counts_by_reason() {
        let sink = QuarantineSummarySink::new();
        let event = |reason| IngestionValidationEvent {
            state: RecordValidationOutcome::Quarantined(reason),
            reason,
            source: "daily-equity-bar".to_string(),
            record_hash: "hash".to_string(),
            observed_at_seconds: TS as u64,
        };
        sink.record(event(QuarantineReason::RangeViolation));
        sink.record(event(QuarantineReason::RangeViolation));
        sink.record(event(QuarantineReason::NegativeVolume));

        let summary = sink.summary();
        assert_eq!(summary.quarantined_total, 3);
        assert_eq!(summary.count(QuarantineReason::RangeViolation), 2);
        assert_eq!(summary.count(QuarantineReason::NegativeVolume), 1);
        assert_eq!(summary.count(QuarantineReason::DuplicateRecord), 0);
        // per_reason is in canonical order and includes zero-count reasons.
        let per = summary.per_reason();
        assert_eq!(per.len(), 6);
        assert_eq!(per[0], (QuarantineReason::RangeViolation, 2));
        assert_eq!(
            per.iter().map(|(_, c)| c).sum::<u64>(),
            summary.quarantined_total
        );
    }

    #[test]
    fn mixed_fixture_quarantines_exactly_one_per_reason() {
        // Drive the whole mixed fixture through the validator + summary sink (no tier / no I/O) and
        // confirm exactly one record per reason is quarantined and 4 remain valid.
        let validator = Sys77RecordValidator::new();
        let sink = QuarantineSummarySink::new();
        let mut valid = 0u64;
        for record in mixed_validation_fixture(TS) {
            match validator.validate(&record) {
                RecordValidationOutcome::Valid => valid += 1,
                RecordValidationOutcome::Quarantined(reason) => {
                    sink.record(IngestionValidationEvent {
                        state: RecordValidationOutcome::Quarantined(reason),
                        reason,
                        source: record.key().kind.as_str().to_string(),
                        record_hash: "hash".to_string(),
                        observed_at_seconds: TS as u64,
                    })
                }
            }
        }
        let summary = sink.summary();
        assert_eq!(valid, 4, "the four well-formed fixtures are admitted");
        assert_eq!(summary.quarantined_total, 6, "one record per SYS-77 rule");
        for reason in ALL_QUARANTINE_REASONS {
            assert_eq!(
                summary.count(reason),
                1,
                "exactly one {reason:?} in the fixture"
            );
        }
    }
}
