//! Market-data storage substrate + **idempotent ingestion** for **SRS-DATA-016**
//! ("make ingestion jobs idempotent"; SyRS NFR-R4; StRS SN-1.26 / SN-1.27).
//!
//! # What SRS-DATA-016 asks for
//!
//! The acceptance criterion: *"Re-running Databento, IB, option-chain, or Sharadar ingestion for
//! an already ingested date creates no duplicate records and does not corrupt existing data."*
//! This module owns the **canonical market-data record**, a local **[`MarketDataStore`]** keyed by
//! a natural key, an **idempotent validating write path** (the `upsert` that makes a re-ingest a
//! no-op), and a fail-closed serialize/restore + durable file codec that round-trips the whole
//! store byte-for-byte — so re-running an ingestion for an already-ingested date neither duplicates
//! a record nor mutates the bytes on disk.
//!
//! # The substrate this lays down (foundational, kind-agnostic)
//!
//! There is no canonical market-data record, store, or catalog anywhere in the platform yet — the
//! data layer ships only the read-only validation ([`crate::DataLayer::ingest_record`], ERR-5) and
//! pacing ([`crate::DataLayer::schedule_ingestion_job`], ERR-6) gates, and the `atp-adapters`
//! provider traits return record *counts*, not records. This module is the storage spine the rest
//! of the SRS-DATA family composes:
//!
//!   * SRS-DATA-007 (unified historical query) reads [`MarketDataStore::records`] / [`get`];
//!   * SRS-DATA-013 (validation → quarantine) keeps composing the unchanged ERR-5 gate;
//!   * SRS-DATA-017 (concurrent reads during writes) builds on the atomic whole-file publish
//!     ([`save_to_path`]), which already gives a reader a consistent snapshot;
//!   * SRS-DATA-008/009/010 (SSD-primary / NAS-archival tiering, eviction, cold-read failover) wrap
//!     the directory this store persists to.
//!
//! [`get`]: MarketDataStore::get
//! [`save_to_path`]: MarketDataStore::save_to_path
//!
//! # Idempotency core (the headline invariant)
//!
//! Every record carries a [`NaturalKey`] = (provider/dataset **kind**, symbol, resolution, event
//! timestamp, optional option contract). The key is the dedup identity; the record's
//! canonically-ordered **fields** (integer minor units) are its value.
//! [`MarketDataStore::upsert`] is the idempotency core:
//!
//!   * key absent → **insert** (in canonical order);
//!   * key present **and the fields are identical** → **no-op** ([`UpsertOutcome::UnchangedDuplicate`]):
//!     a re-ingest of an already-ingested datum creates no duplicate row and leaves the store
//!     byte-identical;
//!   * key present **and the fields differ** → **fail closed** ([`StoreError::ConflictingContent`]),
//!     leaving the existing record exactly as found — re-ingesting a *different* value for an
//!     already-ingested date is the "corrupts existing data" case the acceptance forbids.
//!
//! The four ingestion kinds the acceptance names ([`DatasetKind`]) differ only in how a source
//! materializes records; the store core is kind-agnostic, so one idempotent path covers all four.
//! Records are held in a single canonical order, so the serialized form is **byte-identical** for
//! the same record set regardless of ingest order — which makes "no duplicate / no corruption"
//! inspectable directly on disk.
//!
//! # What is real here vs deferred (honest scope — SRS-DATA-016 closes)
//!
//! This module is a genuinely runnable store + idempotent write path, exercised by fixture sources
//! ([`fixture_batch`]) that stand in for the four provider adapters — exactly as the SRS-DATA-016
//! verification step permits ("fixture market data, provider mocks, file reads, and persisted
//! output inspection"). The pieces that remain deferred (named owners): the **real Databento / IB /
//! Sharadar / option-chain network adapters** are SRS-DATA-001/003/005/006 (the `atp-adapters`
//! provider traits stay stubs); **unified query consumers** (strategy code, notebooks) are
//! SRS-DATA-007; **SSD/NAS tiering, eviction, and failover** of the store directory are
//! SRS-DATA-008/009/010; the **validator rule logic + quarantine alert surface** are
//! SRS-DATA-013 / SRS-NOTIF-001. None of those is load-bearing for the idempotency property.
//!
//! # Money math + determinism
//!
//! Every value field stays in **integer minor units** (`i64`, with `i128` codec intermediates) —
//! no `f64` anywhere (the `f64` in the `atp-adapters` `HistoricalBar` stub is deliberately NOT
//! reused). The work is deterministic: fixed left-to-right folds, no parallelism / RNG / wall-clock
//! read, so a re-ingest is reproducible and the serialized form is byte-identical. No `serde` /
//! external dependency — the same zero-dependency discipline as `crates/atp-simulation`'s
//! `backtest_store`, whose durable-write pattern this mirrors.

use std::collections::HashSet;
use std::fmt;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use atp_types::IngestionRecordSubmission;

/// The current record schema version. Bumped on a layout change that an older reader cannot safely
/// parse. **Version history:** v1 = the original four dataset kinds (daily / minute equity bar,
/// option-chain, fundamental); v2 added [`DatasetKind::CorporateActionSplit`] (codec tag 4); v3 added
/// [`DatasetKind::CorporateActionCoverage`] (codec tag 5, the SRS-DATA-011 completeness-through-date
/// frontier); v4 added the remaining four SRS-DATA-011 corporate-action FACT kinds —
/// [`DatasetKind::CorporateActionDividend`] (tag 6), [`DatasetKind::CorporateActionDelisting`] (tag 7),
/// [`DatasetKind::CorporateActionMerger`] (tag 8), and [`DatasetKind::CorporateActionSymbolChange`]
/// (tag 9). A store is serialized at the MINIMUM version that can represent its contained kinds (see
/// [`serialize`](MarketDataStore::serialize)), so a store that contains a coverage record is written as
/// v3, one that carries a dividend/delisting/merger/symbol-change record as v4, and an OLDER reader
/// rejects it cleanly at the version gate ([`StoreError::UnknownSchemaVersion`]) instead of hitting the
/// unknown tag mid-restore. [`MarketDataStore::restore`] reads ALL versions in
/// `[MIN_SUPPORTED_SCHEMA_VERSION, SCHEMA_VERSION]` (a legacy v1/v2/v3 store still loads), but a store
/// may NOT carry a kind introduced in a later version than the one it declares — that is rejected as
/// inconsistent.
pub const SCHEMA_VERSION: i64 = 4;

/// The oldest serialized schema version [`MarketDataStore::restore`] still accepts (read backward
/// compatibility). A blob at any version outside `[MIN_SUPPORTED_SCHEMA_VERSION, SCHEMA_VERSION]` is
/// rejected loudly with [`StoreError::UnknownSchemaVersion`].
pub const MIN_SUPPORTED_SCHEMA_VERSION: i64 = 1;

/// The magic header line that prefixes every serialized store, so a foreign or truncated blob is
/// rejected before any field is parsed.
pub const MAGIC: &str = "ATP-MARKET-DATA-STORE";

/// File name of the durable store within its configured directory
/// ([`MarketDataStore::save_to_path`] / [`MarketDataStore::load_from_path`]).
pub const STORE_FILENAME: &str = "market_data.store";

/// Base name of the scratch file an atomic save writes (and fsyncs) before renaming it onto
/// [`STORE_FILENAME`]. The actual scratch file appends a per-process, per-call suffix
/// (`<base>.<pid>.<seq>`) so two writers persisting to the same directory cannot rename over each
/// other's scratch file. The suffix is a pid + a process-local counter, NOT a clock / RNG read, so
/// the persisted *content* stays byte-deterministic.
pub const STORE_TMP_FILENAME: &str = "market_data.store.tmp";

/// The exclusive single-writer lock file ([`StoreLock`]). Held across a whole load-modify-save so
/// two ingestion jobs against the same directory cannot each load the old catalog and have the
/// later save erase the earlier job's records.
pub const LOCK_FILENAME: &str = "market_data.lock";

/// Process-local monotonic counter that disambiguates concurrent scratch files within one process
/// (combined with the pid for cross-process uniqueness). Affects only the scratch file name, never
/// the persisted bytes.
static SCRATCH_SEQ: AtomicU64 = AtomicU64::new(0);

/// The data kind a record holds — the **vendor-neutral** taxonomy the four ingestion sources
/// SRS-DATA-016 names map onto (the core never names a vendor; the adapter layer maps provider →
/// kind, per SRS-ARCH-003). The kind is part of the [`NaturalKey`], so the same symbol+date
/// ingested as a daily bar and as a minute bar are distinct records (never a false duplicate). The
/// variant set is closed; a new data kind adds a variant (and a tag) when its ingestion lands.
///
/// The SRS-DATA-016 source mapping (the four sources the acceptance names):
///   * [`DailyEquityBar`](Self::DailyEquityBar) ⇐ Databento daily OHLCV (SRS-DATA-001);
///   * [`MinuteEquityBar`](Self::MinuteEquityBar) ⇐ IB minute OHLCV (SRS-DATA-002);
///   * [`OptionChainSnapshot`](Self::OptionChainSnapshot) ⇐ IB option-chain capture (SRS-DATA-004);
///   * [`Fundamental`](Self::Fundamental) ⇐ Sharadar fundamentals (SRS-DATA-005).
///
/// [`CorporateActionSplit`](Self::CorporateActionSplit) is a corporate-action FACT (a split ratio
/// effective on a date), not a price bar — the input the SRS-DATA-012 split-adjusted normalization
/// reads to adjust historical equity bars. It is vendor-neutral like every other kind.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum DatasetKind {
    /// Daily OHLCV equity bars (the Databento daily source — SRS-DATA-001).
    DailyEquityBar,
    /// Minute OHLCV equity bars (the IB minute source — SRS-DATA-002).
    MinuteEquityBar,
    /// Option-chain snapshots (the IB option-chain source — SRS-DATA-004).
    OptionChainSnapshot,
    /// Fundamental records (the Sharadar source — SRS-DATA-005).
    Fundamental,
    /// A stock-split corporate action keyed by `(symbol, effective_ts)` with `numerator`/
    /// `denominator` ratio fields — the input the SRS-DATA-012 split-adjusted read applies.
    CorporateActionSplit,
    /// A corporate-action **coverage** assertion (SRS-DATA-011): "all corporate actions for `symbol`
    /// effective on or before this record's `event_ts` are known." Keyed by `(symbol, event_ts = D)`
    /// — the `event_ts` IS the completeness-through instant `D`, so advancing the frontier is a NEW
    /// record (a higher `D`) and re-asserting the same `D` is an idempotent no-op. The
    /// `complete_through = D` value field carries `D` for self-description. The coverage-enforcing gate
    /// ([`MarketDataStore::query_split_adjusted`](crate::coverage)) serves split-adjusted output only
    /// when a symbol's frontier `D >= query.end_ts`.
    CorporateActionCoverage,
    /// A cash-dividend corporate action (SRS-DATA-011): keyed by `(symbol, event_ts = ex-date)` with
    /// an `amount_minor` field (cash per share, integer minor units, validated `> 0`). `event_ts` is
    /// the EX-DIVIDEND instant — the first session the shares trade WITHOUT the dividend — so the
    /// fully-adjusted math's strict `ex_ts > t` boundary mirrors the split boundary. The input the
    /// SRS-DATA-012 fully-adjusted (splits AND dividends) read applies.
    CorporateActionDividend,
    /// A delisting corporate action (SRS-DATA-011): keyed by `(symbol, event_ts = the delisting
    /// instant)` with a self-describing `last_trading_ts = event_ts` field (the coverage-record
    /// pattern). The series simply ends; the coverage-gated reads SURFACE the event so a backtest
    /// spanning the date can mark the position final rather than silently seeing data stop.
    CorporateActionDelisting,
    /// A merger corporate action (SRS-DATA-011): the record `symbol` is the ACQUIRED instrument,
    /// the successor (acquirer) symbol rides in the resolution label `merger:<SUCCESSOR>` (a
    /// [`MarketField`] value is an `i64`, so the label is the record's one string slot for a
    /// counterparty symbol — the same subtype-label idiom as `fundamental:income`). Value fields:
    /// `numerator`/`denominator` (shares of the successor per `denominator` shares of the acquired)
    /// and `cash_per_share_minor` (the cash leg, integer minor units, `>= 0`). `event_ts` is the
    /// effective instant: the acquired series terminates there and the coverage-gated reads surface
    /// the conversion terms. A merger does NOT splice the acquired history into the successor's.
    CorporateActionMerger,
    /// A symbol-change (ticker rename) corporate action (SRS-DATA-011): the record `symbol` is the
    /// OLD symbol, the successor rides in the resolution label `symbol-change:<SUCCESSOR>`, and a
    /// self-describing `effective_ts = event_ts` field pins the rename instant. The coverage-gated
    /// reads resolve the LINEAGE: querying the current symbol returns the predecessor's bars
    /// (relabeled) for instants before the change, so a backtest spanning the rename sees one
    /// continuous series.
    CorporateActionSymbolChange,
}

/// The resolution-label prefix a [`DatasetKind::CorporateActionMerger`] record carries; the successor
/// (acquirer) symbol follows the prefix (`merger:<SUCCESSOR>`). See [`successor_symbol`].
pub const MERGER_RESOLUTION_PREFIX: &str = "merger:";
/// The resolution-label prefix a [`DatasetKind::CorporateActionSymbolChange`] record carries; the
/// successor (new) symbol follows the prefix (`symbol-change:<SUCCESSOR>`). See [`successor_symbol`].
pub const SYMBOL_CHANGE_RESOLUTION_PREFIX: &str = "symbol-change:";

/// The successor symbol a merger / symbol-change record names in its resolution label
/// (`merger:<SUCCESSOR>` / `symbol-change:<SUCCESSOR>`), or `None` for every other kind. Store
/// validation guarantees the successor is non-empty and differs from the record's own symbol (so a
/// self-referential rename/merger can never enter the store), making this a total, trustworthy read
/// for any stored record of the two kinds.
pub fn successor_symbol(key: &NaturalKey) -> Option<&str> {
    let prefix = match key.kind {
        DatasetKind::CorporateActionMerger => MERGER_RESOLUTION_PREFIX,
        DatasetKind::CorporateActionSymbolChange => SYMBOL_CHANGE_RESOLUTION_PREFIX,
        _ => return None,
    };
    key.resolution.strip_prefix(prefix)
}

impl DatasetKind {
    /// A stable, lowercase-hyphenated tag for logs / operator surfaces / the CLI.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::DailyEquityBar => "daily-equity-bar",
            Self::MinuteEquityBar => "minute-equity-bar",
            Self::OptionChainSnapshot => "option-chain",
            Self::Fundamental => "fundamental",
            Self::CorporateActionSplit => "corporate-action-split",
            Self::CorporateActionCoverage => "corporate-action-coverage",
            Self::CorporateActionDividend => "corporate-action-dividend",
            Self::CorporateActionDelisting => "corporate-action-delisting",
            Self::CorporateActionMerger => "corporate-action-merger",
            Self::CorporateActionSymbolChange => "corporate-action-symbol-change",
        }
    }

    /// The earliest store [`SCHEMA_VERSION`] in which this kind's codec tag is defined. A serialized
    /// store must declare a schema version at least this high to legitimately carry the kind, so a
    /// legacy v1 store cannot smuggle a kind introduced in a later version (it is rejected on restore).
    fn min_schema_version(&self) -> i64 {
        match self {
            Self::DailyEquityBar
            | Self::MinuteEquityBar
            | Self::OptionChainSnapshot
            | Self::Fundamental => 1,
            Self::CorporateActionSplit => 2,
            Self::CorporateActionCoverage => 3,
            Self::CorporateActionDividend
            | Self::CorporateActionDelisting
            | Self::CorporateActionMerger
            | Self::CorporateActionSymbolChange => 4,
        }
    }

    /// The codec tag (a small stable integer). Kept distinct from `as_str` so a rename of the
    /// human label never changes the on-disk encoding.
    fn tag(&self) -> i128 {
        match self {
            Self::DailyEquityBar => 0,
            Self::MinuteEquityBar => 1,
            Self::OptionChainSnapshot => 2,
            Self::Fundamental => 3,
            Self::CorporateActionSplit => 4,
            Self::CorporateActionCoverage => 5,
            Self::CorporateActionDividend => 6,
            Self::CorporateActionDelisting => 7,
            Self::CorporateActionMerger => 8,
            Self::CorporateActionSymbolChange => 9,
        }
    }

    fn from_tag(tag: i64) -> Result<Self, StoreError> {
        match tag {
            0 => Ok(Self::DailyEquityBar),
            1 => Ok(Self::MinuteEquityBar),
            2 => Ok(Self::OptionChainSnapshot),
            3 => Ok(Self::Fundamental),
            4 => Ok(Self::CorporateActionSplit),
            5 => Ok(Self::CorporateActionCoverage),
            6 => Ok(Self::CorporateActionDividend),
            7 => Ok(Self::CorporateActionDelisting),
            8 => Ok(Self::CorporateActionMerger),
            9 => Ok(Self::CorporateActionSymbolChange),
            _ => Err(StoreError::CorruptRecord {
                context: "unknown dataset kind tag",
            }),
        }
    }

    /// Resolve a kind from its lowercase-hyphenated label (the CLI `--kind` value).
    pub fn from_label(label: &str) -> Option<Self> {
        match label {
            "daily-equity-bar" => Some(Self::DailyEquityBar),
            "minute-equity-bar" => Some(Self::MinuteEquityBar),
            "option-chain" => Some(Self::OptionChainSnapshot),
            "fundamental" => Some(Self::Fundamental),
            "corporate-action-split" => Some(Self::CorporateActionSplit),
            "corporate-action-coverage" => Some(Self::CorporateActionCoverage),
            "corporate-action-dividend" => Some(Self::CorporateActionDividend),
            "corporate-action-delisting" => Some(Self::CorporateActionDelisting),
            "corporate-action-merger" => Some(Self::CorporateActionMerger),
            "corporate-action-symbol-change" => Some(Self::CorporateActionSymbolChange),
            _ => None,
        }
    }

    /// All kinds, in canonical order — the full set the CLI inspect counts iterate.
    pub fn all() -> [DatasetKind; 10] {
        [
            Self::DailyEquityBar,
            Self::MinuteEquityBar,
            Self::OptionChainSnapshot,
            Self::Fundamental,
            Self::CorporateActionSplit,
            Self::CorporateActionCoverage,
            Self::CorporateActionDividend,
            Self::CorporateActionDelisting,
            Self::CorporateActionMerger,
            Self::CorporateActionSymbolChange,
        ]
    }

    /// The kinds the **provider** fixture/ingestion path handles — the four market-data sources plus
    /// the five corporate-action FACT kinds (split, dividend, delisting, merger, symbol change), all
    /// of which originate from a provider adapter (Databento / IB / Sharadar). This DELIBERATELY
    /// excludes [`CorporateActionCoverage`](Self::CorporateActionCoverage): a coverage frontier is an
    /// OPERATOR trust assertion (asserted only via `data011_coverage_cli` / [`coverage_record`]),
    /// never provider market data, so [`fixture_batch`] emits none for it and
    /// [`DataLayer::ingest_market_record`](crate::DataLayer::ingest_market_record) refuses it. A generic
    /// ingestion flow iterates THIS set, not [`all`](Self::all), so it can never mint a trusted frontier.
    pub fn provider_ingestion_kinds() -> [DatasetKind; 9] {
        [
            Self::DailyEquityBar,
            Self::MinuteEquityBar,
            Self::OptionChainSnapshot,
            Self::Fundamental,
            Self::CorporateActionSplit,
            Self::CorporateActionDividend,
            Self::CorporateActionDelisting,
            Self::CorporateActionMerger,
            Self::CorporateActionSymbolChange,
        ]
    }
}

/// The dedup identity of a market-data record (SRS-DATA-016). Two records with the same natural key
/// are the **same logical datum**; re-ingesting one is a no-op (when the value is unchanged) or a
/// fail-closed conflict (when the value differs). `Ord`/`Hash` so the store can hold records in a
/// single canonical order and detect duplicate keys in `O(n)` on restore.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct NaturalKey {
    /// The ingestion source kind.
    pub kind: DatasetKind,
    /// The instrument symbol (the underlying, for an option contract).
    pub symbol: String,
    /// The bar/snapshot resolution (e.g. `1d`, `1m`, `chain`, `fundamental:income`).
    pub resolution: String,
    /// The record's event instant in epoch seconds — the bar/snapshot timestamp or the
    /// fundamental period end. The "date" the acceptance keys idempotency on.
    pub event_ts: i64,
    /// The OCC option contract symbol for an option-chain record; `None` for non-option kinds.
    pub option_contract: Option<String>,
}

/// One value field of a record: a name and an integer-minor value. Kept generic (a name + an `i64`
/// minor value) so the one canonical record models every kind — OHLCV bars (`open`/`high`/`low`/
/// `close`/`volume`), option-chain snapshots (`bid`/`ask`/`last`/`open_interest`/`implied_vol_micros`),
/// and Sharadar fundamentals (`revenue`/`net_income`/…) — without a per-kind struct or any `f64`.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct MarketField {
    /// The field name (e.g. `close`, `bid`, `revenue`). Non-empty, unique within a record.
    pub name: String,
    /// The field value in integer minor units (price minor, integer count, or fixed-point micros).
    pub value_minor: i64,
}

/// A canonical market-data record: a [`NaturalKey`] plus a canonically-ordered set of value
/// [`MarketField`]s. Built fail-closed via [`MarketDataRecord::new`] (empty/duplicate field names,
/// an empty symbol/resolution, or a mis-placed option contract are rejected), and held in the store
/// in a single canonical order so the serialized form is byte-identical for the same record set.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MarketDataRecord {
    key: NaturalKey,
    fields: Vec<MarketField>,
}

impl MarketDataRecord {
    /// Build a validated record, canonicalizing the fields (sorted by name) and failing closed on
    /// an empty symbol/resolution, an empty or duplicate field name, a non-positive event
    /// timestamp, or an option contract that does not match the kind.
    pub fn new(
        key: NaturalKey,
        fields: impl IntoIterator<Item = MarketField>,
    ) -> Result<Self, StoreError> {
        let mut fields: Vec<MarketField> = fields.into_iter().collect();
        fields.sort_by(|a, b| a.name.cmp(&b.name));
        let record = Self { key, fields };
        validate_record(&record)?;
        Ok(record)
    }

    /// The record's dedup identity.
    pub fn key(&self) -> &NaturalKey {
        &self.key
    }

    /// The record's canonical value fields (sorted by name).
    pub fn fields(&self) -> &[MarketField] {
        &self.fields
    }

    /// The canonical **normalized record bytes** — the deterministic full-record encoding (kind,
    /// symbol, resolution, event_ts, option_contract, and the canonically-ordered value fields),
    /// the input the ERR-5 `record_hash` SHA-256 is taken over. Identical to the per-record codec
    /// encoding, so the hash is stable across producers and covers the WHOLE record (key + value).
    pub fn normalized_bytes(&self) -> Vec<u8> {
        let mut encoded = String::new();
        encode_record(&mut encoded, self);
        encoded.into_bytes()
    }

    /// The ERR-5 ingestion envelope **derived from this record**. `source` is the dataset-kind tag;
    /// `record_hash` is the **canonical SHA-256 of the normalized record bytes** — the format the
    /// `IngestionRecordSubmission` type contract requires (the SHA-256 of the *whole* record, key
    /// included, so two distinct records can never share a `record_hash`). Binding the validated
    /// envelope to the record (rather than letting a caller supply an independent one) is what
    /// guarantees the ERR-5 gate is applied to *exactly* the record that will be persisted: there is
    /// no separate payload to forge, so a caller can never validate one record and store another.
    pub fn ingestion_submission(&self) -> IngestionRecordSubmission {
        IngestionRecordSubmission {
            source: self.key.kind.as_str().to_string(),
            record_hash: sha256::hex(&self.normalized_bytes()),
        }
    }
}

/// The total order the store and codec canonicalize on: by the whole natural key. Keys are unique
/// within a store, so this is a strict total order.
fn order_key(record: &MarketDataRecord) -> &NaturalKey {
    &record.key
}

/// A canonical string form of a natural key, for `O(n)` duplicate detection on restore and for
/// fail-closed conflict messages. Length-prefixed so two distinct keys can never collide.
fn key_identity(key: &NaturalKey) -> String {
    let mut out = String::new();
    push_i128(&mut out, key.kind.tag());
    push_str(&mut out, &key.symbol);
    push_str(&mut out, &key.resolution);
    push_i128(&mut out, i128::from(key.event_ts));
    match &key.option_contract {
        None => push_line(&mut out, "N"),
        Some(contract) => push_str(&mut out, contract),
    }
    out
}

/// Fail-closed coherence validation shared by [`MarketDataRecord::new`] and
/// [`MarketDataStore::restore`] — the single place a record's invariants are checked.
fn validate_record(record: &MarketDataRecord) -> Result<(), StoreError> {
    if record.key.symbol.trim().is_empty() {
        return Err(StoreError::InconsistentField {
            context: "empty record symbol",
        });
    }
    if record.key.resolution.trim().is_empty() {
        return Err(StoreError::InconsistentField {
            context: "empty record resolution",
        });
    }
    if record.key.event_ts < 0 {
        return Err(StoreError::InconsistentField {
            context: "negative event timestamp",
        });
    }
    // An option-chain record must name its contract; a non-option kind must not carry one (so the
    // natural key cannot smuggle an option contract onto an equity bar).
    match record.key.kind {
        DatasetKind::OptionChainSnapshot => {
            let contract_ok = record
                .key
                .option_contract
                .as_ref()
                .is_some_and(|c| !c.trim().is_empty());
            if !contract_ok {
                return Err(StoreError::InconsistentField {
                    context: "option-chain record missing its option contract",
                });
            }
        }
        _ => {
            if record.key.option_contract.is_some() {
                return Err(StoreError::InconsistentField {
                    context: "non-option record carries an option contract",
                });
            }
        }
    }
    if record.fields.is_empty() {
        return Err(StoreError::InconsistentField {
            context: "record has no value fields",
        });
    }
    // Fields must be canonical: sorted by name, with non-empty, unique names — so two equal records
    // serialize identically and a re-ingest comparison is exact.
    let mut prev: Option<&str> = None;
    for field in &record.fields {
        if field.name.trim().is_empty() {
            return Err(StoreError::InconsistentField {
                context: "empty field name",
            });
        }
        if let Some(previous) = prev {
            if field.name.as_str() <= previous {
                return Err(StoreError::InconsistentField {
                    context: "field names not strictly sorted/unique",
                });
            }
        }
        prev = Some(field.name.as_str());
    }
    // A corporate-action COVERAGE record asserts a trust decision (the SRS-DATA-011 frontier the
    // split-adjusted gate checks), and `MarketDataRecord::new` is public, so its self-consistency
    // CANNOT be left to the `coverage_record` constructor. Require EXACTLY one value field named
    // `complete_through` whose value equals the key `event_ts` (the frontier identity). This is checked
    // by BOTH the in-memory write path (`upsert`) and the on-disk restore path, so a forged or buggy
    // coverage record — a mismatched `complete_through`, a wrong/missing field name, or extra fields —
    // fails closed and can never grant the split-adjusted gate a frontier its key does not carry.
    if record.key.kind == DatasetKind::CorporateActionCoverage {
        let consistent = matches!(
            record.fields.as_slice(),
            [field] if field.name == "complete_through" && field.value_minor == record.key.event_ts
        );
        if !consistent {
            return Err(StoreError::InconsistentField {
                context: "coverage record must carry exactly one 'complete_through' field equal to its event_ts",
            });
        }
    }
    // The remaining corporate-action FACT kinds feed the coverage-gated adjustment/lineage reads, and
    // `MarketDataRecord::new` is public — so their self-consistency is enforced HERE (upsert AND
    // restore, the same discipline as the coverage record), not left to the fixture constructors.
    if record.key.kind == DatasetKind::CorporateActionDividend {
        // Exactly one positive cash amount: a zero/negative dividend would corrupt the fully-adjusted
        // factor (prev_close - amount)/prev_close (identity or a price INCREASE) — fail closed at write.
        let consistent = matches!(
            record.fields.as_slice(),
            [field] if field.name == "amount_minor" && field.value_minor > 0
        );
        if !consistent {
            return Err(StoreError::InconsistentField {
                context: "dividend record must carry exactly one positive 'amount_minor' field",
            });
        }
    }
    if record.key.kind == DatasetKind::CorporateActionDelisting {
        // Self-describing terminal marker: exactly one last_trading_ts equal to the key event_ts (the
        // coverage-record pattern), so a serialized delisting is readable without re-deriving the key.
        let consistent = matches!(
            record.fields.as_slice(),
            [field] if field.name == "last_trading_ts" && field.value_minor == record.key.event_ts
        );
        if !consistent {
            return Err(StoreError::InconsistentField {
                context: "delisting record must carry exactly one 'last_trading_ts' field equal to its event_ts",
            });
        }
    }
    if record.key.kind == DatasetKind::CorporateActionSymbolChange {
        let consistent = matches!(
            record.fields.as_slice(),
            [field] if field.name == "effective_ts" && field.value_minor == record.key.event_ts
        );
        if !consistent {
            return Err(StoreError::InconsistentField {
                context: "symbol-change record must carry exactly one 'effective_ts' field equal to its event_ts",
            });
        }
    }
    if record.key.kind == DatasetKind::CorporateActionMerger {
        // The gated reads SURFACE a merger's conversion terms to P&L consumers, so the terms are
        // validated at write: exactly the three term fields, a positive share-ratio denominator, a
        // non-negative numerator and cash leg, and at least one non-zero consideration (an all-zero
        // merger converts a position into nothing — that is a delisting, not a merger).
        let terms = |name: &str| {
            record
                .fields
                .iter()
                .find(|field| field.name == name)
                .map(|field| field.value_minor)
        };
        let consistent = record.fields.len() == 3
            && matches!(terms("cash_per_share_minor"), Some(cash) if cash >= 0)
            && matches!(terms("denominator"), Some(den) if den > 0)
            && matches!(terms("numerator"), Some(num) if num >= 0)
            && (terms("numerator") != Some(0) || terms("cash_per_share_minor") != Some(0));
        if !consistent {
            return Err(StoreError::InconsistentField {
                context:
                    "merger record must carry exactly 'cash_per_share_minor' (>= 0), \
                          'denominator' (> 0), and 'numerator' (>= 0) with a non-zero consideration",
            });
        }
    }
    // A merger / symbol-change record names its successor in the resolution label
    // (`merger:<SUCCESSOR>` / `symbol-change:<SUCCESSOR>`). The successor must be present, non-empty,
    // and DIFFERENT from the record's own symbol — a self-referential successor is the trivial lineage
    // cycle, blocked at the record level so it can never enter the store (upsert or restore).
    if matches!(
        record.key.kind,
        DatasetKind::CorporateActionMerger | DatasetKind::CorporateActionSymbolChange
    ) {
        match successor_symbol(&record.key) {
            Some(successor) if !successor.trim().is_empty() && successor != record.key.symbol => {}
            _ => {
                return Err(StoreError::InconsistentField {
                    context: "merger/symbol-change record must name a non-empty successor symbol \
                              (resolution 'merger:<SUCCESSOR>' / 'symbol-change:<SUCCESSOR>') that \
                              differs from its own symbol",
                });
            }
        }
    }
    Ok(())
}

/// The outcome of a [`MarketDataStore::upsert`] — the idempotency signal.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UpsertOutcome {
    /// The key was absent: a fresh record was inserted.
    Inserted,
    /// The key was already present with **identical** content: a no-op (the idempotent re-ingest).
    /// The store is unchanged — no duplicate row, byte-identical on disk.
    UnchangedDuplicate,
}

/// A queryable, persistable collection of market-data records (SRS-DATA-016).
///
/// Records are held in a single canonical natural-key order, so the serialized form is
/// byte-identical for the same record set and [`upsert`](Self::upsert) is idempotent. The store is
/// the storage spine the rest of the SRS-DATA family composes.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct MarketDataStore {
    records: Vec<MarketDataRecord>,
}

impl MarketDataStore {
    /// An empty store.
    pub fn new() -> Self {
        Self::default()
    }

    /// The number of persisted records.
    pub fn len(&self) -> usize {
        self.records.len()
    }

    /// Whether the store holds no records.
    pub fn is_empty(&self) -> bool {
        self.records.is_empty()
    }

    /// All records in canonical natural-key order (the SRS-DATA-007 read groundwork).
    pub fn records(&self) -> &[MarketDataRecord] {
        &self.records
    }

    /// The record for `key`, or `None` if absent (the SRS-DATA-007 point-lookup groundwork).
    pub fn get(&self, key: &NaturalKey) -> Option<&MarketDataRecord> {
        let position = self
            .records
            .binary_search_by(|existing| existing.key.cmp(key))
            .ok()?;
        Some(&self.records[position])
    }

    /// The number of records for a given [`DatasetKind`] — the per-kind count the CLI inspects.
    pub fn count_for_kind(&self, kind: DatasetKind) -> usize {
        self.records.iter().filter(|r| r.key.kind == kind).count()
    }

    /// The contiguous slice of records for one `(kind, symbol, resolution)` series, found by binary
    /// search over the canonical natural-key order (which sorts by `kind`, then `symbol`, then
    /// `resolution`, then `event_ts`). So a single series read is `O(log n + matches)` rather than a
    /// full `O(n)` scan — the indexed read path that keeps a full-universe assembly (one read per
    /// security) from being `O(universe * store_size)`. The returned slice is `event_ts`-ascending (the
    /// next key field), exactly the order [`query_unified`](Self::query_unified) returns for a
    /// kind-narrowed query.
    pub fn records_for(
        &self,
        kind: DatasetKind,
        symbol: &str,
        resolution: &str,
    ) -> &[MarketDataRecord] {
        let target = (kind, symbol, resolution);
        let lower = self.records.partition_point(|r| {
            (r.key.kind, r.key.symbol.as_str(), r.key.resolution.as_str()) < target
        });
        let upper = self.records.partition_point(|r| {
            (r.key.kind, r.key.symbol.as_str(), r.key.resolution.as_str()) <= target
        });
        &self.records[lower..upper]
    }

    /// **The idempotency core (SRS-DATA-016).** Insert `record` if its key is absent; if the key is
    /// already present, a no-op when the content is identical ([`UpsertOutcome::UnchangedDuplicate`])
    /// or a fail-closed [`StoreError::ConflictingContent`] when the content differs — leaving the
    /// existing record exactly as found. So re-running an ingestion for an already-ingested date
    /// creates no duplicate record and does not corrupt existing data.
    pub fn upsert(&mut self, record: MarketDataRecord) -> Result<UpsertOutcome, StoreError> {
        validate_record(&record)?;
        match self
            .records
            .binary_search_by(|existing| order_key(existing).cmp(order_key(&record)))
        {
            Ok(position) => {
                // Key already present. Identical content is the idempotent no-op; differing content
                // is a corruption attempt that fails closed WITHOUT mutating the stored record.
                if self.records[position].fields == record.fields {
                    Ok(UpsertOutcome::UnchangedDuplicate)
                } else {
                    Err(StoreError::ConflictingContent {
                        key: key_identity(&record.key),
                    })
                }
            }
            Err(position) => {
                self.records.insert(position, record);
                Ok(UpsertOutcome::Inserted)
            }
        }
    }

    /// Serialize the whole store to the deterministic, dependency-free text form.
    ///
    /// Records are emitted in canonical order, variable-length strings are length-prefixed, and
    /// integers are decimal lines, so the output is byte-identical for the same record set and
    /// round-trips losslessly. A [`MAGIC`] header and an integrity checksum over the body let
    /// [`restore`](Self::restore) detect any later byte change.
    pub fn serialize(&self) -> String {
        // Write the MINIMUM schema version that can represent the contained kinds, not the current
        // maximum: a store holding only the original v1 kinds (daily / minute bar, option-chain,
        // fundamental) stays v1 and remains readable by an older v1-only tool; only a store that
        // actually contains a kind introduced later (CorporateActionSplit -> v2) is written at the
        // higher version, where an older reader rejects it cleanly at the version gate. An empty store
        // is the lowest supported version. So adding the split kind does NOT make every ordinary store
        // unreadable by v1 tools -- the format bump is scoped to stores that use the new kind.
        let version = self
            .records
            .iter()
            .map(|record| record.key.kind.min_schema_version())
            .max()
            .unwrap_or(MIN_SUPPORTED_SCHEMA_VERSION);
        let mut body = String::new();
        push_i128(&mut body, i128::from(version));
        push_count(&mut body, self.records.len());
        for record in &self.records {
            encode_record(&mut body, record);
        }

        let mut out = String::with_capacity(body.len() + MAGIC.len() + 32);
        push_line(&mut out, MAGIC);
        push_i128(&mut out, i128::from(checksum(body.as_bytes())));
        out.push_str(&body);
        out
    }

    /// Restore a store produced by [`serialize`](Self::serialize), failing closed on any
    /// malformation and building the whole store in a local before returning — so a corrupt or
    /// truncated blob (any change that does not recompute the FNV-1a checksum) returns an [`Err`]
    /// and yields no partially-restored store. The checksum is non-cryptographic, so it catches
    /// **accidental** corruption, not a deliberate checksum-recomputing tamperer (that needs a
    /// keyed MAC, out of scope for the single-user, local-only baseline).
    pub fn restore(serialized: &str) -> Result<Self, StoreError> {
        let mut cursor = Cursor::new(serialized);

        let magic = cursor.read_line("magic header")?;
        if magic != MAGIC {
            return Err(StoreError::CorruptRecord {
                context: "magic header",
            });
        }
        // Integrity check FIRST: the checksum covers the entire body that follows.
        let stored_checksum = cursor.read_u64("checksum")?;
        let body = cursor.remaining();
        if checksum(body) != stored_checksum {
            return Err(StoreError::ChecksumMismatch);
        }

        let schema_version = cursor.read_i64("schema version")?;
        if !(MIN_SUPPORTED_SCHEMA_VERSION..=SCHEMA_VERSION).contains(&schema_version) {
            return Err(StoreError::UnknownSchemaVersion {
                found: schema_version,
            });
        }

        // Decode into a temporary Vec — validating each record and detecting duplicate keys via the
        // HashSet in O(n) — then sort ONCE by the natural key and construct the store directly.
        // (Calling `upsert` per record would re-scan + shift the Vec on every record, making restore
        // of a large catalog O(n^2).) The store is built in a local before returning, so any
        // malformation yields no partial store.
        let record_count = cursor.read_count("record count")?;
        let mut records: Vec<MarketDataRecord> = Vec::new();
        let mut seen: HashSet<String> = HashSet::new();
        for _ in 0..record_count {
            let record = decode_record(&mut cursor)?;
            validate_record(&record)?;
            // Forward-compat guard: a kind may only appear in a store whose declared schema version is
            // at least the version that introduced it. So a legacy v1 blob carrying the v2-introduced
            // CorporateActionSplit kind is rejected as inconsistent rather than silently accepted.
            if record.key.kind.min_schema_version() > schema_version {
                return Err(StoreError::CorruptRecord {
                    context: "dataset kind newer than the store's declared schema version",
                });
            }
            if !seen.insert(key_identity(&record.key)) {
                return Err(StoreError::DuplicateKey {
                    key: key_identity(&record.key),
                });
            }
            records.push(record);
        }
        cursor.expect_end()?;
        // Canonicalize once. The serialized form is already in key order, so this is typically a
        // no-op, but sorting defends against a reordered (checksum-recomputed) blob and guarantees
        // the store invariant regardless of byte order.
        records.sort_by(|a, b| order_key(a).cmp(order_key(b)));
        Ok(Self { records })
    }

    /// Durably persist the whole store to [`STORE_FILENAME`] under `dir`, creating `dir` if absent.
    ///
    /// The write is **crash-durable and atomically published**: it writes the blob to a
    /// per-call-unique scratch file, `fsync`s the scratch file so its bytes reach disk, then
    /// `rename`s it onto the live store (an atomic replace — a reader never sees a half-written
    /// blob), and finally `fsync`s the parent directory so the rename itself survives a crash. The
    /// scratch name carries a `<pid>.<seq>` suffix so two writers persisting to the same directory
    /// cannot rename over each other's scratch file. Every `std::io` failure surfaces as a
    /// fail-closed [`StoreError::Io`].
    ///
    /// Guarantee scope: a single `save_to_path` is atomic (unique scratch + atomic rename), but a
    /// load-modify-save sequence done by two writers concurrently would be last-publish-wins — the
    /// later save could erase the earlier writer's newly ingested records. That is prevented by
    /// holding a [`StoreLock`] across the whole load-modify-save (the operator CLI and ingestion
    /// jobs do): a second concurrent writer is **refused** ([`StoreError::Locked`]) rather than
    /// silently losing records. The SSD-primary / NAS-archival *tiering* of this directory remains
    /// the deferred SRS-DATA-008 owner.
    pub fn save_to_path(&self, dir: &Path) -> Result<(), StoreError> {
        fs::create_dir_all(dir).map_err(|err| io_error("create store directory", &err))?;
        let seq = SCRATCH_SEQ.fetch_add(1, Ordering::Relaxed);
        let tmp_path = dir.join(format!("{STORE_TMP_FILENAME}.{}.{seq}", std::process::id()));
        let final_path = dir.join(STORE_FILENAME);

        // Write the blob to the scratch file and fsync it, so its bytes are durably on disk BEFORE
        // we publish it — otherwise a crash could leave the renamed file referencing unwritten data.
        let mut scratch = fs::File::create(&tmp_path)
            .map_err(|err| io_error("create store scratch file", &err))?;
        if let Err(err) = scratch
            .write_all(self.serialize().as_bytes())
            .and_then(|()| scratch.sync_all())
        {
            let _ = fs::remove_file(&tmp_path);
            return Err(io_error("write store scratch file", &err));
        }
        drop(scratch);

        // Atomic publish: rename replaces the live store in one step, so a reader never sees a
        // partially written blob.
        fs::rename(&tmp_path, &final_path).map_err(|err| {
            let _ = fs::remove_file(&tmp_path);
            io_error("publish store file", &err)
        })?;

        // fsync the directory so the rename (a directory-entry change) is itself durable.
        let dir_handle =
            fs::File::open(dir).map_err(|err| io_error("open store directory", &err))?;
        dir_handle
            .sync_all()
            .map_err(|err| io_error("sync store directory", &err))?;
        Ok(())
    }

    /// Load a store previously written by [`save_to_path`](Self::save_to_path) from `dir`.
    ///
    /// Fail-closed taxonomy (a persisted catalog must never be silently lost):
    /// - The configured `dir` is **absent or not a directory** → [`StoreError::Io`]. An unmounted /
    ///   deleted / misconfigured store path is a configuration failure, NOT an empty catalog —
    ///   restoring empty here would silently erase previously persisted records.
    /// - `dir` exists but holds **no store file** → an empty store. The one legitimate "fresh
    ///   install has never ingested" case (the provisioned directory is there).
    /// - A **present** file is decoded through the fail-closed [`restore`](Self::restore) codec, so
    ///   a corrupt / truncated / checksum-mismatching blob returns an [`Err`] (never a partial
    ///   store). Any other I/O failure surfaces as [`StoreError::Io`].
    pub fn load_from_path(dir: &Path) -> Result<Self, StoreError> {
        if !dir.is_dir() {
            return Err(StoreError::Io {
                context: "store directory is missing or not a directory",
            });
        }
        let final_path = dir.join(STORE_FILENAME);
        match fs::read_to_string(&final_path) {
            Ok(contents) => Self::restore(&contents),
            Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(Self::new()),
            Err(err) => Err(io_error("read store file", &err)),
        }
    }
}

/// An exclusive **single-writer lock** over a store directory (SRS-DATA-016 no-corruption under
/// concurrent ingestion jobs).
///
/// Held across a whole **load-modify-save** so two ingestion jobs against the same
/// `ATP_DATA_STORE_DIR` cannot each load the old catalog and have the later save erase the earlier
/// job's records. Acquisition is an atomic exclusive file create (`create_new`, i.e. `O_EXCL`): a
/// second writer that finds the lock present is **refused** with [`StoreError::Locked`] rather than
/// proceeding to a last-publish-wins overwrite. The lock is released on [`Drop`] (the file is
/// removed), so a normal scope exit frees it.
///
/// Scope (honest bound): this serializes writers on **one host/filesystem** — the realistic case
/// for the single-user, local-only baseline (manual operator runs + the scheduled ingestion jobs
/// the orchestrator serializes). A crashed holder leaves a stale lock file that an operator removes
/// before retrying; richer liveness detection (pid-liveness, lease expiry) and cross-host
/// coordination are out of scope for the baseline.
#[derive(Debug)]
pub struct StoreLock {
    path: PathBuf,
}

impl StoreLock {
    /// Acquire the exclusive single-writer lock for `dir`. The directory must already exist (a
    /// missing directory fails closed as [`StoreError::Io`], symmetric with
    /// [`MarketDataStore::load_from_path`], so an unmounted/mistyped path is never silently
    /// created and forked). If another writer already holds the lock, returns
    /// [`StoreError::Locked`].
    pub fn acquire(dir: &Path) -> Result<StoreLock, StoreError> {
        if !dir.is_dir() {
            return Err(StoreError::Io {
                context: "store directory is missing or not a directory",
            });
        }
        let path = dir.join(LOCK_FILENAME);
        match fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
        {
            Ok(mut file) => {
                // Best-effort holder pid for operator debugging (advisory only — not used for
                // liveness); a write failure does not invalidate the acquired lock.
                let _ = writeln!(file, "{}", std::process::id());
                Ok(StoreLock { path })
            }
            Err(err) if err.kind() == io::ErrorKind::AlreadyExists => Err(StoreError::Locked),
            Err(err) => Err(io_error("acquire store lock", &err)),
        }
    }
}

impl Drop for StoreLock {
    fn drop(&mut self) {
        // Release the lock on scope exit. Best-effort: a failed removal leaves a stale lock the
        // operator clears before retrying (documented), never a corrupted store.
        let _ = fs::remove_file(&self.path);
    }
}

/// Map a `std::io::Error` to the fail-closed [`StoreError::Io`]. `StoreError` derives
/// `Clone`/`PartialEq`/`Eq` (which `io::Error` does not), so the variant carries a `'static`
/// context label naming the operation rather than the source error.
fn io_error(context: &'static str, _err: &io::Error) -> StoreError {
    StoreError::Io { context }
}

// --------------------------------------------------------------------------- //
// Record encode / decode
// --------------------------------------------------------------------------- //

fn encode_record(body: &mut String, record: &MarketDataRecord) {
    push_i128(body, record.key.kind.tag());
    push_str(body, &record.key.symbol);
    push_str(body, &record.key.resolution);
    push_i128(body, i128::from(record.key.event_ts));
    encode_opt_str(body, record.key.option_contract.as_deref());
    push_count(body, record.fields.len());
    for field in &record.fields {
        push_str(body, &field.name);
        push_i128(body, i128::from(field.value_minor));
    }
}

fn decode_record(cursor: &mut Cursor<'_>) -> Result<MarketDataRecord, StoreError> {
    let kind = DatasetKind::from_tag(cursor.read_i64("dataset kind tag")?)?;
    let symbol = cursor.read_str("symbol")?;
    let resolution = cursor.read_str("resolution")?;
    let event_ts = cursor.read_i64("event_ts")?;
    let option_contract = decode_opt_str(cursor, "option contract")?;
    // Counts are read from the blob and are NOT trusted: a tampered (checksum-recomputed) or
    // accidentally-corrupted count must never drive an eager allocation, so the vector grows
    // incrementally (never pre-sized from the count) and a count larger than the remaining data
    // simply exhausts the cursor and fails closed — never an out-of-memory abort.
    let field_count = cursor.read_count("field count")?;
    let mut fields = Vec::new();
    for _ in 0..field_count {
        let name = cursor.read_str("field name")?;
        let value_minor = cursor.read_i64("field value_minor")?;
        fields.push(MarketField { name, value_minor });
    }
    // Build the raw record directly (not via `new`, which re-sorts) — restore preserves the
    // canonical serialized order and validates fail-closed via the caller's validate_record.
    Ok(MarketDataRecord {
        key: NaturalKey {
            kind,
            symbol,
            resolution,
            event_ts,
            option_contract,
        },
        fields,
    })
}

// --------------------------------------------------------------------------- //
// Deterministic, dependency-free text codec
// --------------------------------------------------------------------------- //

/// Append `value` as its own line.
fn push_line(out: &mut String, value: &str) {
    out.push_str(value);
    out.push('\n');
}

/// Append a decimal integer as its own line.
fn push_i128(out: &mut String, value: i128) {
    out.push_str(&value.to_string());
    out.push('\n');
}

/// Append a non-negative count as its own line.
fn push_count(out: &mut String, value: usize) {
    out.push_str(&value.to_string());
    out.push('\n');
}

/// Append a length-prefixed string: the byte length on one line, then the bytes followed by a
/// newline — so any byte (spaces, an OCC option symbol, etc.) round-trips without escaping.
fn push_str(out: &mut String, value: &str) {
    out.push_str(&value.len().to_string());
    out.push('\n');
    out.push_str(value);
    out.push('\n');
}

/// Append an optional string: `N` for `None`, else the length-prefixed bytes (preceded by a `S`
/// marker line so a `None` and an empty string never collide).
fn encode_opt_str(out: &mut String, value: Option<&str>) {
    match value {
        None => push_line(out, "N"),
        Some(text) => {
            push_line(out, "S");
            push_str(out, text);
        }
    }
}

fn decode_opt_str(
    cursor: &mut Cursor<'_>,
    context: &'static str,
) -> Result<Option<String>, StoreError> {
    match cursor.read_line(context)? {
        "N" => Ok(None),
        "S" => Ok(Some(cursor.read_str(context)?)),
        _ => Err(StoreError::CorruptRecord { context }),
    }
}

/// A 64-bit FNV-1a integrity checksum over the serialized body.
///
/// A NON-cryptographic checksum: it detects *accidental* corruption (bit flips, truncation, a value
/// changed to another structurally-valid value) so a damaged blob fails closed instead of restoring
/// fabricated records. It is NOT a security MAC — defending against a deliberate tamperer who
/// recomputes the checksum needs a keyed MAC and key management, out of scope for the single-user,
/// local-only release baseline. Deterministic, dependency-free, integer-only.
fn checksum(bytes: &[u8]) -> u64 {
    const OFFSET_BASIS: u64 = 0xcbf29ce484222325;
    const PRIME: u64 = 0x0000_0100_0000_01b3;
    let mut hash = OFFSET_BASIS;
    for &byte in bytes {
        hash ^= u64::from(byte);
        hash = hash.wrapping_mul(PRIME);
    }
    hash
}

/// A dependency-free, deterministic **SHA-256** (FIPS 180-4) used for the ERR-5 `record_hash`.
///
/// The `IngestionRecordSubmission` type contract specifies `record_hash` as the canonical SHA-256
/// of the normalized record bytes. atp-data carries no external crate (the same zero-dependency
/// discipline as the FNV-1a checksum), so SHA-256 is implemented here in safe `u32` arithmetic and
/// pinned by the FIPS known-answer test vectors. Unlike the FNV-1a integrity checksum (which only
/// catches accidental corruption), this is the cryptographic record identity.
mod sha256 {
    const H0: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];

    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];

    /// The lowercase 64-hex SHA-256 digest of `bytes`.
    pub fn hex(bytes: &[u8]) -> String {
        let mut h = H0;

        // Pre-processing: append 0x80, pad with zeros to 56 mod 64, then the 64-bit big-endian
        // bit length.
        let mut msg = bytes.to_vec();
        let bit_len = (bytes.len() as u64).wrapping_mul(8);
        msg.push(0x80);
        while msg.len() % 64 != 56 {
            msg.push(0);
        }
        msg.extend_from_slice(&bit_len.to_be_bytes());

        for chunk in msg.chunks_exact(64) {
            let mut w = [0u32; 64];
            for (i, word) in w.iter_mut().enumerate().take(16) {
                let base = i * 4;
                *word = u32::from_be_bytes([
                    chunk[base],
                    chunk[base + 1],
                    chunk[base + 2],
                    chunk[base + 3],
                ]);
            }
            for i in 16..64 {
                let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
                let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
                w[i] = w[i - 16]
                    .wrapping_add(s0)
                    .wrapping_add(w[i - 7])
                    .wrapping_add(s1);
            }

            let mut a = h[0];
            let mut b = h[1];
            let mut c = h[2];
            let mut d = h[3];
            let mut e = h[4];
            let mut f = h[5];
            let mut g = h[6];
            let mut hh = h[7];

            for i in 0..64 {
                let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
                let ch = (e & f) ^ ((!e) & g);
                let temp1 = hh
                    .wrapping_add(s1)
                    .wrapping_add(ch)
                    .wrapping_add(K[i])
                    .wrapping_add(w[i]);
                let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
                let maj = (a & b) ^ (a & c) ^ (b & c);
                let temp2 = s0.wrapping_add(maj);
                hh = g;
                g = f;
                f = e;
                e = d.wrapping_add(temp1);
                d = c;
                c = b;
                b = a;
                a = temp1.wrapping_add(temp2);
            }

            h[0] = h[0].wrapping_add(a);
            h[1] = h[1].wrapping_add(b);
            h[2] = h[2].wrapping_add(c);
            h[3] = h[3].wrapping_add(d);
            h[4] = h[4].wrapping_add(e);
            h[5] = h[5].wrapping_add(f);
            h[6] = h[6].wrapping_add(g);
            h[7] = h[7].wrapping_add(hh);
        }

        let mut out = String::with_capacity(64);
        for word in h {
            out.push_str(&format!("{word:08x}"));
        }
        out
    }

    #[cfg(test)]
    mod tests {
        use super::hex;

        #[test]
        fn fips_known_answer_vectors() {
            // FIPS 180-4 / NIST test vectors — pin the implementation to the standard.
            assert_eq!(
                hex(b""),
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            );
            assert_eq!(
                hex(b"abc"),
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
            );
            assert_eq!(
                hex(b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"),
                "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"
            );
        }
    }
}

/// A forward-only cursor over a serialized store's bytes. Reads are exact and fail closed: a missing
/// newline, a malformed integer, a truncated length-prefixed string, or trailing garbage all
/// surface as [`StoreError::CorruptRecord`].
struct Cursor<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Cursor<'a> {
    fn new(serialized: &'a str) -> Self {
        Self {
            bytes: serialized.as_bytes(),
            pos: 0,
        }
    }

    /// The not-yet-consumed bytes (used to checksum the body after the header).
    fn remaining(&self) -> &'a [u8] {
        &self.bytes[self.pos..]
    }

    /// Read up to (and consuming) the next `\n`, returning the line without it.
    fn read_line(&mut self, context: &'static str) -> Result<&'a str, StoreError> {
        let start = self.pos;
        while self.pos < self.bytes.len() && self.bytes[self.pos] != b'\n' {
            self.pos += 1;
        }
        if self.pos >= self.bytes.len() {
            return Err(StoreError::CorruptRecord { context });
        }
        let line = &self.bytes[start..self.pos];
        self.pos += 1; // consume the '\n'
        std::str::from_utf8(line).map_err(|_| StoreError::CorruptRecord { context })
    }

    fn read_i64(&mut self, context: &'static str) -> Result<i64, StoreError> {
        self.read_line(context)?
            .parse::<i64>()
            .map_err(|_| StoreError::CorruptRecord { context })
    }

    fn read_u64(&mut self, context: &'static str) -> Result<u64, StoreError> {
        self.read_line(context)?
            .parse::<u64>()
            .map_err(|_| StoreError::CorruptRecord { context })
    }

    /// Read a non-negative count line (a `usize`).
    fn read_count(&mut self, context: &'static str) -> Result<usize, StoreError> {
        self.read_line(context)?
            .parse::<usize>()
            .map_err(|_| StoreError::CorruptRecord { context })
    }

    /// Read a length-prefixed string: a byte-length line, then exactly that many bytes, then a
    /// terminating `\n`.
    fn read_str(&mut self, context: &'static str) -> Result<String, StoreError> {
        let len = self.read_count(context)?;
        let end = self
            .pos
            .checked_add(len)
            .ok_or(StoreError::CorruptRecord { context })?;
        if end >= self.bytes.len() || self.bytes[end] != b'\n' {
            return Err(StoreError::CorruptRecord { context });
        }
        let value = std::str::from_utf8(&self.bytes[self.pos..end])
            .map_err(|_| StoreError::CorruptRecord { context })?
            .to_string();
        self.pos = end + 1; // consume the trailing '\n'
        Ok(value)
    }

    /// Confirm the cursor is exhausted; trailing bytes mean the blob is corrupt.
    fn expect_end(&self) -> Result<(), StoreError> {
        if self.pos == self.bytes.len() {
            Ok(())
        } else {
            Err(StoreError::CorruptRecord {
                context: "trailing data",
            })
        }
    }
}

/// Fail-closed errors from market-data store persistence + ingestion. Carries no broker/vendor
/// identifiers.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StoreError {
    /// The serialized blob was malformed: a bad magic header, a missing newline, a non-integer
    /// where an integer was expected, a truncated length-prefixed string, an unknown enum tag, or
    /// trailing data. `context` names where parsing failed.
    CorruptRecord { context: &'static str },
    /// The blob's schema version is not one this reader understands. Rejected loudly rather than
    /// mis-read.
    UnknownSchemaVersion { found: i64 },
    /// A field violated a record invariant (an empty symbol/resolution, an empty/duplicate field
    /// name, a negative event timestamp, or a mis-placed option contract). `context` names the
    /// violation.
    InconsistentField { context: &'static str },
    /// Two records shared a natural key in a restored blob, so a datum's identity would be
    /// ambiguous.
    DuplicateKey { key: String },
    /// A re-ingest carried **different content** for an already-ingested natural key — the
    /// "corrupts existing data" case SRS-DATA-016 forbids. The existing record is left untouched.
    ConflictingContent { key: String },
    /// Another writer already holds the [`StoreLock`] for the store directory. The caller is
    /// **refused** rather than proceeding to a last-publish-wins overwrite that could lose the
    /// other writer's records; the operator retries once the holder releases the lock.
    Locked,
    /// The blob's integrity checksum did not match the body, so the bytes were corrupted or tampered
    /// after serialization. Rejected before any state is built.
    ChecksumMismatch,
    /// A filesystem operation behind [`MarketDataStore::save_to_path`] /
    /// [`load_from_path`](MarketDataStore::load_from_path) failed. `context` names the operation. A
    /// *missing* store file is NOT this error — it restores an empty store; this variant is a real
    /// I/O failure that must fail closed rather than be mistaken for "fresh install".
    Io { context: &'static str },
}

impl fmt::Display for StoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::CorruptRecord { context } => write!(f, "corrupt market-data record: {context}"),
            Self::UnknownSchemaVersion { found } => {
                write!(f, "unknown market-data store schema version: {found}")
            }
            Self::InconsistentField { context } => {
                write!(f, "inconsistent market-data record field: {context}")
            }
            Self::DuplicateKey { key } => write!(f, "duplicate market-data natural key: {key}"),
            Self::ConflictingContent { key } => write!(
                f,
                "conflicting re-ingest for an already-ingested key (existing record left intact): {key}"
            ),
            Self::Locked => write!(
                f,
                "another writer holds the market-data store lock (retry once it is released)"
            ),
            Self::ChecksumMismatch => write!(f, "market-data store checksum mismatch"),
            Self::Io { context } => write!(f, "market-data store I/O failure: {context}"),
        }
    }
}

impl std::error::Error for StoreError {}

// --------------------------------------------------------------------------- //
// Fixture ingestion sources (stand-ins for the four provider adapters)
// --------------------------------------------------------------------------- //

/// A deterministic fixture batch of records for `kind` on `event_ts` — the operator-demonstrable
/// stand-in for the real Databento / IB / option-chain / Sharadar provider adapters (deferred to
/// SRS-DATA-001/003/005/006), exactly as the SRS-DATA-016 verification step permits ("fixture
/// market data, provider mocks"). Deterministic: the same `(kind, event_ts)` always yields the same
/// records, so re-ingesting it is a genuine idempotency test.
pub fn fixture_batch(kind: DatasetKind, event_ts: i64) -> Vec<MarketDataRecord> {
    match kind {
        DatasetKind::DailyEquityBar => ["AAPL", "MSFT"]
            .iter()
            .enumerate()
            .map(|(i, symbol)| ohlcv_record(kind, symbol, "1d", event_ts, 100 + i as i64))
            .collect(),
        DatasetKind::MinuteEquityBar => ["AAPL", "MSFT"]
            .iter()
            .enumerate()
            .map(|(i, symbol)| ohlcv_record(kind, symbol, "1m", event_ts, 200 + i as i64))
            .collect(),
        DatasetKind::OptionChainSnapshot => ["AAPL  240119C00150000", "AAPL  240119P00150000"]
            .iter()
            .enumerate()
            .map(|(i, contract)| option_record(event_ts, contract, 50 + i as i64))
            .collect(),
        DatasetKind::Fundamental => ["AAPL", "MSFT"]
            .iter()
            .map(|symbol| fundamental_record(event_ts, symbol))
            .collect(),
        // A deterministic 4-for-1 AAPL split effective on `event_ts` — the SRS-DATA-012 input the
        // split-adjusted read applies to AAPL daily/minute bars dated strictly BEFORE `event_ts`.
        DatasetKind::CorporateActionSplit => vec![split_record(event_ts, "AAPL", 4, 1)],
        // Corporate-action COVERAGE is NOT provider fixture data: it is an OPERATOR trust assertion (the
        // SRS-DATA-011 frontier the split-adjusted gate reads), asserted ONLY via data011_coverage_cli
        // (store::coverage_record). The provider-fixture generator deliberately emits NONE, so a generic
        // ingestion flow iterating dataset kinds over fixture_batch can never mint a trusted frontier
        // (and DataLayer::ingest_market_record refuses the kind besides). See provider_ingestion_kinds.
        DatasetKind::CorporateActionCoverage => Vec::new(),
        // A deterministic $1.00 AAPL cash dividend ex on `event_ts` — against the fixture AAPL daily
        // close of 10000 minor the fully-adjusted factor is (10000-100)/10000 = 99/100.
        DatasetKind::CorporateActionDividend => vec![dividend_record(event_ts, "AAPL", 100)],
        // A deterministic MSFT delisting at `event_ts` (the terminal marker the gated reads surface).
        DatasetKind::CorporateActionDelisting => vec![delisting_record(event_ts, "MSFT")],
        // A deterministic MSFT->AAPL merger at `event_ts`: 1 AAPL share per 2 MSFT plus 500 minor cash
        // per MSFT share (the conversion terms the gated reads surface).
        DatasetKind::CorporateActionMerger => {
            vec![merger_record(event_ts, "MSFT", "AAPL", 1, 2, 500)]
        }
        // A deterministic AAPL->AAPLN ticker rename at `event_ts` (the lineage hop the gated reads
        // resolve: querying AAPLN returns the pre-change AAPL bars relabeled).
        DatasetKind::CorporateActionSymbolChange => {
            vec![symbol_change_record(event_ts, "AAPL", "AAPLN")]
        }
    }
}

fn ohlcv_record(
    kind: DatasetKind,
    symbol: &str,
    resolution: &str,
    event_ts: i64,
    seed: i64,
) -> MarketDataRecord {
    let close = seed * 100;
    MarketDataRecord::new(
        NaturalKey {
            kind,
            symbol: symbol.to_string(),
            resolution: resolution.to_string(),
            event_ts,
            option_contract: None,
        },
        [
            field("open", close - 50),
            field("high", close + 75),
            field("low", close - 90),
            field("close", close),
            field("volume", seed * 1_000),
        ],
    )
    .expect("fixture OHLCV record is well-formed")
}

fn option_record(event_ts: i64, contract: &str, seed: i64) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::OptionChainSnapshot,
            symbol: "AAPL".to_string(),
            resolution: "chain".to_string(),
            event_ts,
            option_contract: Some(contract.to_string()),
        },
        [
            field("bid", seed * 100),
            field("ask", seed * 100 + 25),
            field("last", seed * 100 + 10),
            field("volume", seed * 5),
            field("open_interest", seed * 40),
            field("implied_vol_micros", 250_000 + seed),
        ],
    )
    .expect("fixture option record is well-formed")
}

fn fundamental_record(event_ts: i64, symbol: &str) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::Fundamental,
            symbol: symbol.to_string(),
            resolution: "fundamental:income".to_string(),
            event_ts,
            option_contract: None,
        },
        [
            field("revenue_minor", 1_000_000),
            field("net_income_minor", 250_000),
            field("total_assets_minor", 5_000_000),
        ],
    )
    .expect("fixture fundamental record is well-formed")
}

/// A stock-split corporate-action record: keyed by `(symbol, effective_ts)` with the split ratio as
/// two integer fields. `numerator`/`denominator` express an `N`-for-`M` split — a 4-for-1 forward
/// split is `(4, 1)`, a 1-for-10 reverse split is `(1, 10)`. `event_ts` is the EFFECTIVE instant: a
/// bar dated strictly before it is on the pre-split basis and gets adjusted; a bar on/after it is
/// already on the new basis. The resolution label is the vendor-neutral `split`.
fn split_record(event_ts: i64, symbol: &str, numerator: i64, denominator: i64) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::CorporateActionSplit,
            symbol: symbol.to_string(),
            resolution: "split".to_string(),
            event_ts,
            option_contract: None,
        },
        [
            field("denominator", denominator),
            field("numerator", numerator),
        ],
    )
    .expect("fixture split record is well-formed")
}

/// A corporate-action **coverage** record (SRS-DATA-011): the assertion "all corporate actions for
/// `symbol` effective on or before `through` are known." The single constructor for the coverage
/// record shape (the `data011_coverage_cli` operator surface and the fixture batch both build through
/// here). `event_ts` IS `through` — the completeness-through instant `D` — so the natural key encodes
/// the frontier and advancing it is a NEW record while re-asserting the same `through` is an idempotent
/// no-op; the `complete_through = through` value field carries `D` so a serialized record is
/// self-describing and the frontier is readable without re-deriving it from the key. The vendor-neutral
/// resolution label is `coverage`. `through` must be non-negative (it is an event timestamp): the
/// `data011_coverage_cli` operator surface rejects a negative `--through` at parse time and the fixture
/// passes a non-negative `event_ts`, so this constructor is only ever handed a valid record.
pub fn coverage_record(through: i64, symbol: &str) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::CorporateActionCoverage,
            symbol: symbol.to_string(),
            resolution: "coverage".to_string(),
            event_ts: through,
            option_contract: None,
        },
        [field("complete_through", through)],
    )
    .expect("fixture coverage record is well-formed")
}

/// A cash-dividend corporate-action record (SRS-DATA-011): keyed by `(symbol, ex_ts)` with the cash
/// amount per share as the single `amount_minor` field (integer minor units; store validation requires
/// it strictly positive). `ex_ts` is the EX-DIVIDEND instant — the first session trading WITHOUT the
/// dividend — mirroring the split record's "first session on the new basis" semantic, so the
/// fully-adjusted math's strict `ex_ts > t` boundary matches the split boundary. The vendor-neutral
/// resolution label is `dividend`.
pub fn dividend_record(ex_ts: i64, symbol: &str, amount_minor: i64) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::CorporateActionDividend,
            symbol: symbol.to_string(),
            resolution: "dividend".to_string(),
            event_ts: ex_ts,
            option_contract: None,
        },
        [field("amount_minor", amount_minor)],
    )
    .expect("fixture dividend record is well-formed")
}

/// A delisting corporate-action record (SRS-DATA-011): keyed by `(symbol, last_ts)` with a
/// self-describing `last_trading_ts = last_ts` field (the coverage-record pattern — store validation
/// requires the field to equal the key `event_ts`). `last_ts` is the delisting instant: the symbol's
/// series ends there, and the coverage-gated reads surface the event so a backtest spanning the date
/// marks the position final rather than silently seeing the data stop. The vendor-neutral resolution
/// label is `delisting`.
pub fn delisting_record(last_ts: i64, symbol: &str) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::CorporateActionDelisting,
            symbol: symbol.to_string(),
            resolution: "delisting".to_string(),
            event_ts: last_ts,
            option_contract: None,
        },
        [field("last_trading_ts", last_ts)],
    )
    .expect("fixture delisting record is well-formed")
}

/// A merger corporate-action record (SRS-DATA-011): the record symbol is the ACQUIRED instrument and
/// the successor (acquirer) rides in the resolution label `merger:<SUCCESSOR>` (a value field is an
/// `i64`, so the label is the record's string slot for the counterparty symbol; store validation
/// requires a non-empty successor differing from the acquired symbol). The conversion terms are the
/// `numerator`/`denominator` share ratio (`numerator` successor shares per `denominator` acquired
/// shares) and `cash_per_share_minor` (the cash leg per acquired share, integer minor units).
/// `effective_ts` is the effective instant: the acquired series terminates there.
pub fn merger_record(
    effective_ts: i64,
    acquired: &str,
    successor: &str,
    numerator: i64,
    denominator: i64,
    cash_per_share_minor: i64,
) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::CorporateActionMerger,
            symbol: acquired.to_string(),
            resolution: format!("{MERGER_RESOLUTION_PREFIX}{successor}"),
            event_ts: effective_ts,
            option_contract: None,
        },
        [
            field("cash_per_share_minor", cash_per_share_minor),
            field("denominator", denominator),
            field("numerator", numerator),
        ],
    )
    .expect("fixture merger record is well-formed")
}

/// A symbol-change (ticker rename) corporate-action record (SRS-DATA-011): the record symbol is the
/// OLD symbol, the successor rides in the resolution label `symbol-change:<SUCCESSOR>` (store
/// validation requires it non-empty and different from the old symbol), and a self-describing
/// `effective_ts = event_ts` field pins the rename instant (the coverage-record pattern). Bars of the
/// old symbol dated strictly BEFORE `effective_ts` belong to the successor's lineage; the
/// coverage-gated reads resolve that lineage so a query for the current symbol spans the rename.
pub fn symbol_change_record(
    effective_ts: i64,
    old_symbol: &str,
    successor: &str,
) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::CorporateActionSymbolChange,
            symbol: old_symbol.to_string(),
            resolution: format!("{SYMBOL_CHANGE_RESOLUTION_PREFIX}{successor}"),
            event_ts: effective_ts,
            option_contract: None,
        },
        [field("effective_ts", effective_ts)],
    )
    .expect("fixture symbol-change record is well-formed")
}

fn field(name: &str, value_minor: i64) -> MarketField {
    MarketField {
        name: name.to_string(),
        value_minor,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn daily_key(symbol: &str, event_ts: i64) -> NaturalKey {
        NaturalKey {
            kind: DatasetKind::DailyEquityBar,
            symbol: symbol.to_string(),
            resolution: "1d".to_string(),
            event_ts,
            option_contract: None,
        }
    }

    fn record(symbol: &str, event_ts: i64, close: i64) -> MarketDataRecord {
        MarketDataRecord::new(
            daily_key(symbol, event_ts),
            [field("close", close), field("open", close - 10)],
        )
        .unwrap()
    }

    #[test]
    fn fields_are_canonicalized_on_construction() {
        // Fields supplied out of order are sorted by name, so two equal records compare and
        // serialize identically regardless of input order.
        let a = MarketDataRecord::new(
            daily_key("AAPL", 1),
            [field("open", 90), field("close", 100)],
        )
        .unwrap();
        let b = MarketDataRecord::new(
            daily_key("AAPL", 1),
            [field("close", 100), field("open", 90)],
        )
        .unwrap();
        assert_eq!(a, b);
        assert_eq!(a.fields()[0].name, "close");
        assert_eq!(a.fields()[1].name, "open");
    }

    #[test]
    fn new_rejects_incoherent_records() {
        assert!(MarketDataRecord::new(daily_key("", 1), [field("close", 1)]).is_err());
        assert!(MarketDataRecord::new(daily_key("AAPL", -1), [field("close", 1)]).is_err());
        // Empty field set.
        assert!(MarketDataRecord::new(daily_key("AAPL", 1), []).is_err());
        // Duplicate field name.
        assert!(MarketDataRecord::new(
            daily_key("AAPL", 1),
            [field("close", 1), field("close", 2)]
        )
        .is_err());
        // A non-option kind must not carry an option contract.
        assert!(MarketDataRecord::new(
            NaturalKey {
                option_contract: Some("X".to_string()),
                ..daily_key("AAPL", 1)
            },
            [field("close", 1)]
        )
        .is_err());
        // An option-chain kind MUST carry a contract.
        assert!(MarketDataRecord::new(
            NaturalKey {
                kind: DatasetKind::OptionChainSnapshot,
                resolution: "chain".to_string(),
                ..daily_key("AAPL", 1)
            },
            [field("last", 1)]
        )
        .is_err());
    }

    #[test]
    fn upsert_inserts_a_fresh_key() {
        let mut store = MarketDataStore::new();
        assert_eq!(
            store.upsert(record("AAPL", 1, 100)).unwrap(),
            UpsertOutcome::Inserted
        );
        assert_eq!(store.len(), 1);
        assert!(store.get(&daily_key("AAPL", 1)).is_some());
    }

    #[test]
    fn upsert_identical_content_is_an_idempotent_no_op() {
        // SRS-DATA-016 core: re-ingesting the same datum creates no duplicate and changes nothing.
        let mut store = MarketDataStore::new();
        store.upsert(record("AAPL", 1, 100)).unwrap();
        let before = store.serialize();
        assert_eq!(
            store.upsert(record("AAPL", 1, 100)).unwrap(),
            UpsertOutcome::UnchangedDuplicate
        );
        assert_eq!(store.len(), 1, "no duplicate row");
        assert_eq!(store.serialize(), before, "store is byte-identical");
    }

    #[test]
    fn upsert_conflicting_content_fails_closed_without_mutating() {
        // Re-ingesting a DIFFERENT value for an already-ingested key is the "corrupts existing
        // data" case: it must fail closed and leave the stored record exactly as found.
        let mut store = MarketDataStore::new();
        store.upsert(record("AAPL", 1, 100)).unwrap();
        let before = store.serialize();
        let err = store.upsert(record("AAPL", 1, 999)).unwrap_err();
        assert!(matches!(err, StoreError::ConflictingContent { .. }));
        assert_eq!(store.len(), 1);
        assert_eq!(store.serialize(), before, "existing record left intact");
    }

    #[test]
    fn store_stays_in_canonical_order_regardless_of_insert_order() {
        let mut forward = MarketDataStore::new();
        forward.upsert(record("AAPL", 1, 100)).unwrap();
        forward.upsert(record("MSFT", 1, 100)).unwrap();
        forward.upsert(record("AAPL", 2, 100)).unwrap();

        let mut reverse = MarketDataStore::new();
        reverse.upsert(record("AAPL", 2, 100)).unwrap();
        reverse.upsert(record("MSFT", 1, 100)).unwrap();
        reverse.upsert(record("AAPL", 1, 100)).unwrap();

        assert_eq!(forward, reverse);
        assert_eq!(forward.serialize(), reverse.serialize());
    }

    #[test]
    fn records_for_returns_only_the_target_series_in_event_ts_order() {
        // The indexed read must isolate ONE (kind, symbol, resolution) series -- no foreign symbol,
        // kind, or resolution leaks in -- and return it event_ts-ascending, even with interleaved
        // neighbors in the canonical order.
        let mut store = MarketDataStore::new();
        store.upsert(record("AAA", 2, 100)).unwrap();
        store.upsert(record("AAA", 1, 100)).unwrap();
        store.upsert(record("AAA", 3, 100)).unwrap();
        store.upsert(record("AAB", 1, 100)).unwrap(); // adjacent symbol must not leak
        store.upsert(record("AA", 1, 100)).unwrap(); // prefix symbol must not leak
        store.upsert(split_record(2, "AAA", 4, 1)).unwrap(); // different kind, same symbol

        let series = store.records_for(DatasetKind::DailyEquityBar, "AAA", "1d");
        let got: Vec<(&str, i64)> = series
            .iter()
            .map(|r| (r.key.symbol.as_str(), r.key.event_ts))
            .collect();
        assert_eq!(got, vec![("AAA", 1), ("AAA", 2), ("AAA", 3)]);

        // A non-existent series is an empty slice, never a foreign match.
        assert!(store
            .records_for(DatasetKind::DailyEquityBar, "ZZZ", "1d")
            .is_empty());
        assert!(store
            .records_for(DatasetKind::MinuteEquityBar, "AAA", "1m")
            .is_empty());

        // The indexed read agrees with the full-scan unified query (same records, same order).
        let via_query = store.query_unified(
            &crate::query::UnifiedHistoricalQuery::new("AAA", "1d", 0, 100)
                .with_kind(DatasetKind::DailyEquityBar),
        );
        let query_ts: Vec<i64> = via_query
            .records()
            .iter()
            .map(|r| r.key().event_ts)
            .collect();
        assert_eq!(query_ts, vec![1, 2, 3]);

        // A kind-narrowed INVERTED range (start > end) over an EXISTING series returns the empty
        // result, never a panic from inverted slice bounds (the indexed path's regression guard).
        let inverted = store.query_unified(
            &crate::query::UnifiedHistoricalQuery::new("AAA", "1d", 3, 1)
                .with_kind(DatasetKind::DailyEquityBar),
        );
        assert!(
            inverted.records().is_empty(),
            "inverted kind-narrowed range is empty, not a crash"
        );
    }

    /// Build a serialized blob at an arbitrary declared schema `version` (mirrors `serialize`, which
    /// always writes the CURRENT `SCHEMA_VERSION`) so the version-gate tests can forge legacy / future
    /// / inconsistent blobs with a correct checksum.
    fn versioned_blob(version: i64, records: &[MarketDataRecord]) -> String {
        let mut body = String::new();
        push_i128(&mut body, i128::from(version));
        push_count(&mut body, records.len());
        for record in records {
            encode_record(&mut body, record);
        }
        let mut out = String::new();
        push_line(&mut out, MAGIC);
        push_i128(&mut out, i128::from(checksum(body.as_bytes())));
        out.push_str(&body);
        out
    }

    /// The schema version a serialized blob declares (line 3: MAGIC, checksum, version, count, ...).
    fn declared_version(blob: &str) -> i64 {
        blob.lines()
            .nth(2)
            .expect("version line")
            .parse()
            .expect("version is an integer")
    }

    #[test]
    fn current_schema_version_is_four_after_adding_the_corporate_action_kinds() {
        assert_eq!(SCHEMA_VERSION, 4);
        assert_eq!(MIN_SUPPORTED_SCHEMA_VERSION, 1);
        // A store carrying a split record still serializes at v2 (the split kind's version)...
        let mut with_split = MarketDataStore::new();
        with_split.upsert(split_record(200, "AAPL", 4, 1)).unwrap();
        assert_eq!(declared_version(&with_split.serialize()), 2);
        assert_eq!(
            MarketDataStore::restore(&with_split.serialize()).unwrap(),
            with_split
        );
        // ...a store carrying a coverage record still serializes at v3 (the coverage kind's version)...
        let mut with_coverage = MarketDataStore::new();
        with_coverage.upsert(coverage_record(200, "AAPL")).unwrap();
        assert_eq!(declared_version(&with_coverage.serialize()), 3);
        let restored = MarketDataStore::restore(&with_coverage.serialize()).unwrap();
        assert_eq!(restored, with_coverage);
        // ...and a store carrying any of the four v4 corporate-action kinds serializes at the current
        // (v4) version, so an older v1/v2/v3 reader rejects it at the version gate rather than
        // mid-restore on the unknown tag.
        for v4_record in [
            dividend_record(200, "AAPL", 100),
            delisting_record(200, "MSFT"),
            merger_record(200, "MSFT", "AAPL", 1, 2, 500),
            symbol_change_record(200, "AAPL", "AAPLN"),
        ] {
            let mut with_v4 = MarketDataStore::new();
            with_v4.upsert(v4_record).unwrap();
            assert_eq!(declared_version(&with_v4.serialize()), 4);
            assert_eq!(
                MarketDataStore::restore(&with_v4.serialize()).unwrap(),
                with_v4
            );
        }
    }

    #[test]
    fn serialize_writes_the_minimum_schema_version_for_the_contained_kinds() {
        // A store holding only the original v1 kinds stays v1 -- adding later kinds does NOT make an
        // ordinary daily-bar store unreadable by an older v1-only tool.
        let mut v1_only = MarketDataStore::new();
        v1_only.upsert(record("AAPL", 1, 100)).unwrap();
        v1_only.upsert(record("MSFT", 1, 100)).unwrap();
        assert_eq!(declared_version(&v1_only.serialize()), 1);

        // A store whose newest kind is a split record is written at v2 (a coverage record is absent).
        let mut with_split = MarketDataStore::new();
        with_split.upsert(record("AAPL", 1, 100)).unwrap();
        with_split.upsert(split_record(200, "AAPL", 4, 1)).unwrap();
        assert_eq!(declared_version(&with_split.serialize()), 2);

        // A store that actually contains a coverage record is written at v3 (so an older v1/v2 reader
        // rejects it cleanly at the version gate). The declared version is the MAX over contained kinds,
        // so a v1 bar + a v2 split + a v3 coverage record together still declare v3.
        let mut with_coverage = MarketDataStore::new();
        with_coverage.upsert(record("AAPL", 1, 100)).unwrap();
        with_coverage
            .upsert(split_record(200, "AAPL", 4, 1))
            .unwrap();
        with_coverage.upsert(coverage_record(200, "AAPL")).unwrap();
        assert_eq!(declared_version(&with_coverage.serialize()), 3);

        // Only a store carrying one of the v4 corporate-action kinds is written at v4 — a coverage-only
        // store stays v3-readable by an older v3 tool (the format bump is scoped to stores using the
        // new kinds).
        let mut with_dividend = MarketDataStore::new();
        with_dividend.upsert(record("AAPL", 1, 100)).unwrap();
        with_dividend
            .upsert(dividend_record(200, "AAPL", 100))
            .unwrap();
        assert_eq!(declared_version(&with_dividend.serialize()), 4);

        // An empty store is the lowest supported version.
        assert_eq!(
            declared_version(&MarketDataStore::new().serialize()),
            MIN_SUPPORTED_SCHEMA_VERSION
        );
    }

    #[test]
    fn restore_accepts_a_legacy_v1_store_without_later_kinds() {
        // Backward compatibility: a v1 store (only the original four kinds) still loads under the v3
        // reader.
        let blob = versioned_blob(1, &[record("AAPL", 1, 100), record("MSFT", 1, 100)]);
        let restored = MarketDataStore::restore(&blob).unwrap();
        assert_eq!(restored.len(), 2);
    }

    #[test]
    fn restore_accepts_a_legacy_v2_store_with_split_but_no_coverage() {
        // Backward compatibility: a v2 store (original kinds + split, no coverage) still loads under
        // the v3 reader.
        let blob = versioned_blob(
            2,
            &[record("AAPL", 1, 100), split_record(200, "AAPL", 4, 1)],
        );
        let restored = MarketDataStore::restore(&blob).unwrap();
        assert_eq!(restored.len(), 2);
    }

    #[test]
    fn restore_rejects_a_v1_store_carrying_a_v2_only_kind() {
        // A legacy v1 store may NOT carry the CorporateActionSplit kind (introduced in v2): an
        // inconsistent blob is rejected rather than silently accepted.
        let blob = versioned_blob(1, &[split_record(200, "AAPL", 4, 1)]);
        assert!(matches!(
            MarketDataStore::restore(&blob),
            Err(StoreError::CorruptRecord { .. })
        ));
    }

    #[test]
    fn coverage_record_field_must_be_consistent_with_its_event_ts() {
        // A coverage record asserts the SRS-DATA-011 frontier the split-adjusted gate trusts, and
        // MarketDataRecord::new is public — so store validation must reject any coverage record whose
        // complete_through field does not exactly match its key event_ts (a forged frontier), or that
        // carries the wrong / missing field or extra fields. The honest constructor is accepted.
        let coverage_key = |event_ts: i64| NaturalKey {
            kind: DatasetKind::CorporateActionCoverage,
            symbol: "AAPL".to_string(),
            resolution: "coverage".to_string(),
            event_ts,
            option_contract: None,
        };
        let f = |name: &str, value: i64| MarketField {
            name: name.to_string(),
            value_minor: value,
        };

        // Accepted: complete_through == event_ts (what coverage_record builds).
        assert!(MarketDataRecord::new(coverage_key(200), [f("complete_through", 200)]).is_ok());

        // Rejected: the field disagrees with the key (a forged frontier of 999 dressed as 200).
        assert!(matches!(
            MarketDataRecord::new(coverage_key(200), [f("complete_through", 999)]),
            Err(StoreError::InconsistentField { .. })
        ));
        // Rejected: wrong field name.
        assert!(matches!(
            MarketDataRecord::new(coverage_key(200), [f("through", 200)]),
            Err(StoreError::InconsistentField { .. })
        ));
        // Rejected: extra fields (a coverage record is exactly one complete_through).
        assert!(matches!(
            MarketDataRecord::new(
                coverage_key(200),
                [f("complete_through", 200), f("extra", 1)]
            ),
            Err(StoreError::InconsistentField { .. })
        ));

        // The same guard runs on RESTORE: an honest coverage blob restores, but a forged on-disk blob
        // carrying a mismatched coverage record (built bypassing new()) is rejected — validate_record is
        // shared by new() and restore(), so the gate's frontier is trustworthy from disk too.
        assert!(
            MarketDataStore::restore(&versioned_blob(3, &[coverage_record(200, "AAPL")])).is_ok()
        );
        let forged_record = MarketDataRecord {
            key: coverage_key(200),
            fields: vec![f("complete_through", 999)],
        };
        assert!(matches!(
            MarketDataStore::restore(&versioned_blob(3, &[forged_record])),
            Err(StoreError::InconsistentField { .. })
        ));
    }

    #[test]
    fn restore_rejects_a_v1_or_v2_store_carrying_a_v3_only_kind() {
        // Neither a legacy v1 nor a v2 store may carry the CorporateActionCoverage kind (introduced in
        // v3): a forged lower-version blob smuggling the coverage kind is rejected as inconsistent.
        for version in [1, 2] {
            let blob = versioned_blob(version, &[coverage_record(200, "AAPL")]);
            assert!(
                matches!(
                    MarketDataStore::restore(&blob),
                    Err(StoreError::CorruptRecord { .. })
                ),
                "v{version} blob carrying the v3-only coverage kind must be rejected"
            );
        }
    }

    #[test]
    fn corporate_action_fact_records_are_validated_at_new_and_restore() {
        // The four v4 corporate-action FACT kinds feed the coverage-gated adjustment/lineage reads,
        // and MarketDataRecord::new is public — so validate_record (shared by new() and restore())
        // must reject a malformed record of each kind, the same discipline as the coverage record.
        let key = |kind: DatasetKind, symbol: &str, resolution: &str, event_ts: i64| NaturalKey {
            kind,
            symbol: symbol.to_string(),
            resolution: resolution.to_string(),
            event_ts,
            option_contract: None,
        };
        let f = |name: &str, value: i64| MarketField {
            name: name.to_string(),
            value_minor: value,
        };

        // DIVIDEND: exactly one positive amount_minor.
        let div_key = || {
            key(
                DatasetKind::CorporateActionDividend,
                "AAPL",
                "dividend",
                200,
            )
        };
        assert!(MarketDataRecord::new(div_key(), [f("amount_minor", 100)]).is_ok());
        for bad in [
            MarketDataRecord::new(div_key(), [f("amount_minor", 0)]), // non-positive amount
            MarketDataRecord::new(div_key(), [f("amount_minor", -5)]),
            MarketDataRecord::new(div_key(), [f("amount", 100)]), // wrong field name
            MarketDataRecord::new(div_key(), [f("amount_minor", 100), f("extra", 1)]),
        ] {
            assert!(matches!(bad, Err(StoreError::InconsistentField { .. })));
        }

        // DELISTING: exactly one last_trading_ts equal to the key event_ts (self-describing).
        let del_key = || {
            key(
                DatasetKind::CorporateActionDelisting,
                "MSFT",
                "delisting",
                300,
            )
        };
        assert!(MarketDataRecord::new(del_key(), [f("last_trading_ts", 300)]).is_ok());
        assert!(matches!(
            MarketDataRecord::new(del_key(), [f("last_trading_ts", 999)]), // forged instant
            Err(StoreError::InconsistentField { .. })
        ));

        // SYMBOL CHANGE: effective_ts == event_ts AND a non-empty successor differing from the symbol.
        assert!(MarketDataRecord::new(
            key(
                DatasetKind::CorporateActionSymbolChange,
                "AAPL",
                "symbol-change:AAPLN",
                400
            ),
            [f("effective_ts", 400)],
        )
        .is_ok());
        for (resolution, field_value) in [
            ("symbol-change:AAPLN", 999), // forged instant
            ("symbol-change:", 400),      // empty successor
            ("symbol-change:AAPL", 400),  // self successor (the trivial lineage cycle)
            ("rename:AAPLN", 400),        // missing prefix
        ] {
            assert!(
                matches!(
                    MarketDataRecord::new(
                        key(
                            DatasetKind::CorporateActionSymbolChange,
                            "AAPL",
                            resolution,
                            400
                        ),
                        [f("effective_ts", field_value)],
                    ),
                    Err(StoreError::InconsistentField { .. })
                ),
                "symbol-change '{resolution}' / effective_ts {field_value} must be rejected"
            );
        }

        // MERGER: a non-empty successor differing from the acquired symbol in the resolution label.
        assert!(MarketDataRecord::new(
            key(
                DatasetKind::CorporateActionMerger,
                "MSFT",
                "merger:AAPL",
                500
            ),
            [
                f("cash_per_share_minor", 500),
                f("denominator", 2),
                f("numerator", 1)
            ],
        )
        .is_ok());
        for resolution in ["merger:", "merger:MSFT", "acquisition:AAPL"] {
            assert!(
                matches!(
                    MarketDataRecord::new(
                        key(DatasetKind::CorporateActionMerger, "MSFT", resolution, 500),
                        [
                            f("cash_per_share_minor", 500),
                            f("denominator", 2),
                            f("numerator", 1)
                        ],
                    ),
                    Err(StoreError::InconsistentField { .. })
                ),
                "merger resolution '{resolution}' must be rejected"
            );
        }
        // Merger TERMS are validated too (the gated reads surface them to P&L consumers): a
        // non-positive denominator, a negative numerator or cash leg, an all-zero consideration, or
        // missing/extra fields all fail closed. A cash-only merger (numerator 0, cash > 0) is valid.
        assert!(MarketDataRecord::new(
            key(
                DatasetKind::CorporateActionMerger,
                "MSFT",
                "merger:AAPL",
                500
            ),
            [
                f("cash_per_share_minor", 500),
                f("denominator", 1),
                f("numerator", 0)
            ],
        )
        .is_ok());
        for fields in [
            vec![
                f("cash_per_share_minor", 500),
                f("denominator", 0),
                f("numerator", 1),
            ], // den 0
            vec![
                f("cash_per_share_minor", -1),
                f("denominator", 2),
                f("numerator", 1),
            ], // cash < 0
            vec![
                f("cash_per_share_minor", 500),
                f("denominator", 2),
                f("numerator", -1),
            ], // num < 0
            vec![
                f("cash_per_share_minor", 0),
                f("denominator", 2),
                f("numerator", 0),
            ], // no consideration
            vec![f("denominator", 2), f("numerator", 1)], // missing cash
        ] {
            assert!(
                matches!(
                    MarketDataRecord::new(
                        key(
                            DatasetKind::CorporateActionMerger,
                            "MSFT",
                            "merger:AAPL",
                            500
                        ),
                        fields.clone(),
                    ),
                    Err(StoreError::InconsistentField { .. })
                ),
                "merger terms {fields:?} must be rejected"
            );
        }

        // The same guard runs on RESTORE: a forged on-disk blob (built bypassing new()) is rejected.
        let forged = MarketDataRecord {
            key: key(
                DatasetKind::CorporateActionDividend,
                "AAPL",
                "dividend",
                200,
            ),
            fields: vec![f("amount_minor", 0)],
        };
        assert!(matches!(
            MarketDataStore::restore(&versioned_blob(4, &[forged])),
            Err(StoreError::InconsistentField { .. })
        ));
        assert!(
            MarketDataStore::restore(&versioned_blob(4, &[dividend_record(200, "AAPL", 100)]))
                .is_ok()
        );
    }

    #[test]
    fn successor_symbol_reads_the_resolution_label_for_the_two_lineage_kinds() {
        let merger = merger_record(500, "MSFT", "AAPL", 1, 2, 500);
        assert_eq!(successor_symbol(merger.key()), Some("AAPL"));
        let rename = symbol_change_record(400, "AAPL", "AAPLN");
        assert_eq!(successor_symbol(rename.key()), Some("AAPLN"));
        // Every other kind — even one whose resolution happens to carry the prefix text — reads None.
        let bar = record("AAPL", 1, 100);
        assert_eq!(successor_symbol(bar.key()), None);
        assert_eq!(
            successor_symbol(dividend_record(200, "AAPL", 100).key()),
            None
        );
    }

    #[test]
    fn restore_rejects_a_lower_version_store_carrying_a_v4_only_kind() {
        // A v1/v2/v3 blob may not carry any of the four v4-introduced corporate-action kinds: a forged
        // lower-version blob smuggling one is rejected as inconsistent (the forward-compat guard).
        for version in [1, 2, 3] {
            for v4_record in [
                dividend_record(200, "AAPL", 100),
                delisting_record(200, "MSFT"),
                merger_record(200, "MSFT", "AAPL", 1, 2, 500),
                symbol_change_record(200, "AAPL", "AAPLN"),
            ] {
                let kind = v4_record.key().kind;
                let blob = versioned_blob(version, &[v4_record]);
                assert!(
                    matches!(
                        MarketDataStore::restore(&blob),
                        Err(StoreError::CorruptRecord { .. })
                    ),
                    "v{version} blob carrying the v4-only {kind:?} kind must be rejected"
                );
            }
        }
    }

    #[test]
    fn restore_rejects_an_unknown_future_schema_version() {
        let blob = versioned_blob(99, &[record("AAPL", 1, 100)]);
        assert!(matches!(
            MarketDataStore::restore(&blob),
            Err(StoreError::UnknownSchemaVersion { found: 99 })
        ));
    }

    #[test]
    fn serialize_restore_round_trips() {
        let mut store = MarketDataStore::new();
        for kind in DatasetKind::provider_ingestion_kinds() {
            for record in fixture_batch(kind, 1_700_000_000) {
                store.upsert(record).unwrap();
            }
        }
        // Coverage is not provider fixture data; add one explicitly (the only legitimate path) so the
        // round-trip still exercises a store carrying every kind.
        store
            .upsert(coverage_record(1_700_000_000, "AAPL"))
            .unwrap();
        assert_eq!(declared_version(&store.serialize()), 4);
        let restored = MarketDataStore::restore(&store.serialize()).unwrap();
        assert_eq!(restored, store);
        assert_eq!(restored.serialize(), store.serialize());
    }

    #[test]
    fn restore_fails_closed_on_a_flipped_byte() {
        let mut store = MarketDataStore::new();
        store.upsert(record("AAPL", 1, 100)).unwrap();
        let mut bytes = store.serialize().into_bytes();
        // Flip a body byte while staying ASCII (< 0x80) so we exercise restore()'s checksum guard
        // (a checksum-recomputing tamperer is out of scope) rather than String::from_utf8.
        let last = bytes.len() - 1;
        bytes[last] = bytes[last].wrapping_add(1) & 0x7F;
        let corrupted = String::from_utf8(bytes).expect("mutation stays valid UTF-8");
        assert!(MarketDataStore::restore(&corrupted).is_err());
    }

    #[test]
    fn restore_rejects_a_foreign_blob() {
        assert!(MarketDataStore::restore("not a store blob").is_err());
    }

    /// A unique scratch directory under the OS temp dir. The suffix is a fixed per-test label, not a
    /// clock/RNG read, so the persistence layer itself stays deterministic; each test owns a
    /// distinct label so parallel test runs do not collide.
    fn temp_store_dir(label: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("atp_data016_store_{label}"));
        let _ = fs::remove_dir_all(&dir);
        dir
    }

    #[test]
    fn save_then_load_round_trips_through_disk() {
        let dir = temp_store_dir("round_trip");
        let mut store = MarketDataStore::new();
        store.upsert(record("AAPL", 1, 100)).unwrap();
        store.upsert(record("MSFT", 1, 100)).unwrap();
        store.save_to_path(&dir).unwrap();

        // The atomic publish left exactly the final store file (no scratch behind).
        let names: Vec<String> = fs::read_dir(&dir)
            .unwrap()
            .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        assert_eq!(names, vec![STORE_FILENAME.to_string()]);

        let loaded = MarketDataStore::load_from_path(&dir).unwrap();
        assert_eq!(loaded, store);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn reingest_through_disk_is_byte_identical() {
        // The headline SRS-DATA-016 disk property: re-ingesting an already-ingested batch and
        // re-saving leaves the persisted file byte-for-byte identical (no duplicate, no corruption).
        let dir = temp_store_dir("reingest_identical");
        let mut store = MarketDataStore::new();
        for record in fixture_batch(DatasetKind::DailyEquityBar, 1_700_000_000) {
            store.upsert(record).unwrap();
        }
        store.save_to_path(&dir).unwrap();
        let first = fs::read(dir.join(STORE_FILENAME)).unwrap();

        let mut reloaded = MarketDataStore::load_from_path(&dir).unwrap();
        for record in fixture_batch(DatasetKind::DailyEquityBar, 1_700_000_000) {
            assert_eq!(
                reloaded.upsert(record).unwrap(),
                UpsertOutcome::UnchangedDuplicate
            );
        }
        reloaded.save_to_path(&dir).unwrap();
        let second = fs::read(dir.join(STORE_FILENAME)).unwrap();
        assert_eq!(first, second, "re-ingest left the persisted file unchanged");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn load_missing_directory_fails_closed() {
        let dir = temp_store_dir("missing_dir");
        assert!(matches!(
            MarketDataStore::load_from_path(&dir),
            Err(StoreError::Io { .. })
        ));
    }

    #[test]
    fn load_missing_file_in_present_dir_is_empty() {
        let dir = temp_store_dir("missing_file");
        fs::create_dir_all(&dir).unwrap();
        assert!(MarketDataStore::load_from_path(&dir).unwrap().is_empty());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn store_lock_is_exclusive_and_released_on_drop() {
        // The single-writer guard: while one writer holds the lock, a second acquire is refused;
        // after the holder drops, the lock can be re-acquired.
        let dir = temp_store_dir("lock_exclusive");
        fs::create_dir_all(&dir).unwrap();
        {
            let _held = StoreLock::acquire(&dir).unwrap();
            assert!(matches!(StoreLock::acquire(&dir), Err(StoreError::Locked)));
        }
        assert!(StoreLock::acquire(&dir).is_ok());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn store_lock_missing_directory_fails_closed() {
        let dir = temp_store_dir("lock_missing").join("never-provisioned");
        assert!(matches!(
            StoreLock::acquire(&dir),
            Err(StoreError::Io { .. })
        ));
    }

    #[test]
    fn fixture_batches_are_deterministic_and_distinct_per_kind() {
        for kind in DatasetKind::provider_ingestion_kinds() {
            assert_eq!(fixture_batch(kind, 7), fixture_batch(kind, 7));
            assert!(!fixture_batch(kind, 7).is_empty());
        }
        // Coverage is NOT a provider fixture kind: the generator emits NONE for it (so a generic
        // ingestion flow cannot mint a trusted coverage frontier).
        assert!(fixture_batch(DatasetKind::CorporateActionCoverage, 7).is_empty());

        // The same symbol+date under two kinds is NOT a duplicate (kind is part of the key).
        let mut store = MarketDataStore::new();
        for kind in DatasetKind::provider_ingestion_kinds() {
            for record in fixture_batch(kind, 7) {
                assert_eq!(store.upsert(record).unwrap(), UpsertOutcome::Inserted);
            }
        }
        for kind in DatasetKind::provider_ingestion_kinds() {
            assert!(store.count_for_kind(kind) > 0);
        }
    }

    #[test]
    fn ingestion_submission_is_a_full_record_sha256() {
        let a = record("AAPL", 1, 100);
        let b = record("AAPL", 2, 100); // SAME value fields, DIFFERENT key (event_ts)
        let c = record("AAPL", 1, 999); // same key, different value
        let ha = a.ingestion_submission().record_hash;
        // record_hash is the canonical 64-hex SHA-256 the IngestionRecordSubmission contract requires.
        assert_eq!(ha.len(), 64);
        assert!(ha.chars().all(|ch| ch.is_ascii_hexdigit()));
        // The hash covers the WHOLE record (key + value), so two distinct records never collide --
        // not even two records with identical value fields under different keys.
        assert_ne!(
            ha,
            b.ingestion_submission().record_hash,
            "a different key must not collide"
        );
        assert_ne!(
            ha,
            c.ingestion_submission().record_hash,
            "a different value must not collide"
        );
        // source is the vendor-neutral dataset-kind tag.
        assert_eq!(a.ingestion_submission().source, "daily-equity-bar");
    }
}
