use atp_types::{
    FundamentalStatements, FundamentalStatementsError, OrderErrorCategory, RuntimeService,
};
use std::fmt;

pub use atp_types::{OrderReceipt, OrderSubmission};

/// SRS-EXE-006 — the headless IB Gateway brokerage adapter: the IB-error → SyRS
/// SYS-64 classification, the TWS transport seam, the four AC operations exposed
/// through the canonical [`BrokerageAdapter`] / [`MarketDataAdapter`] /
/// [`HistoricalDataAdapter`] traits, and the operator-gated live transport. See the
/// module docs for what ships solo vs. what the operator-initiated paper-account
/// integration test completes.
pub mod interactive_brokers;
pub use interactive_brokers::{
    classify_ib_order_error, IbAccountKind, IbApiError, IbConnectionConfig,
    IbConnectionConfigError, IbGatewayConnection, InteractiveBrokersBrokerage,
};
/// The live IB socket transport is behind the non-default `ib-live-transport`
/// feature (operator-gated scaffold; see [`interactive_brokers`]).
#[cfg(feature = "ib-live-transport")]
pub use interactive_brokers::{TcpIbGateway, IB_CONNECT_TIMEOUT};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum AdapterCapability {
    Brokerage,
    MarketData,
    HistoricalData,
    BulkEquityData,
    FundamentalData,
    OptionsData,
    UserParquetImport,
    AlternativeData,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AdapterError {
    NotConfigured {
        adapter: &'static str,
        capability: &'static str,
    },
    /// A provider payload row could not be mapped onto the vendor-neutral domain model -- a
    /// malformed / out-of-contract vendor record (e.g. a fundamental statement filed before its
    /// period ended, or a non-positive market cap). Surfaced through the common adapter error
    /// taxonomy so a caller handles a mapping failure the same way as any other adapter fault.
    InvalidProviderData {
        adapter: &'static str,
        detail: String,
    },
    /// A brokerage adapter operation failed (SRS-EXE-006). Carries the SyRS SYS-64
    /// [`OrderErrorCategory`] classification when the underlying vendor error maps
    /// onto one — so a failed order submission surfaces `INVALID_SYMBOL` /
    /// `INSUFFICIENT_BUYING_POWER` / `RATE_LIMITED` / `CONNECTIVITY_BLOCKED` through
    /// this common taxonomy (the SRS-ERR-001 broker categories) — plus the raw
    /// vendor `code` + `message`. `category: None` is a recognised-but-unmapped
    /// failure that is still surfaced, never dropped (SYS-64).
    Brokerage {
        adapter: &'static str,
        category: Option<OrderErrorCategory>,
        code: i32,
        message: String,
    },
}

impl fmt::Display for AdapterError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotConfigured {
                adapter,
                capability,
            } => write!(
                formatter,
                "{adapter} adapter capability {capability} is not configured"
            ),
            Self::InvalidProviderData { adapter, detail } => write!(
                formatter,
                "{adapter} adapter received invalid provider data: {detail}"
            ),
            Self::Brokerage {
                adapter,
                category,
                code,
                message,
            } => match category {
                Some(category) => write!(
                    formatter,
                    "{adapter} brokerage operation failed [{}]: vendor error {code} — {message}",
                    category.as_str()
                ),
                None => write!(
                    formatter,
                    "{adapter} brokerage operation failed: vendor error {code} — {message}"
                ),
            },
        }
    }
}

impl std::error::Error for AdapterError {}

pub type AdapterResult<T> = Result<T, AdapterError>;

fn not_configured<T>(adapter: &'static str, capability: &'static str) -> AdapterResult<T> {
    Err(AdapterError::NotConfigured {
        adapter,
        capability,
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct AdapterVersion {
    pub adapter_version: &'static str,
    pub protocol_version: &'static str,
    pub protocol_label: &'static str,
}

pub const ADAPTER_VERSION_NOT_APPLICABLE: AdapterVersion = AdapterVersion {
    adapter_version: "0.1.0",
    protocol_version: "not-applicable",
    protocol_label: "not-applicable",
};

pub const INTERACTIVE_BROKERS_TWS_API_VERSION: &str = "10.45";
pub const INTERACTIVE_BROKERS_ADAPTER_VERSION: &str = "0.1.0";
pub const INTERACTIVE_BROKERS_PROTOCOL_LABEL: &str = "IB TWS API";

pub trait AdapterBoundary {
    fn provider_name(&self) -> &'static str;

    fn capabilities(&self) -> &'static [AdapterCapability] {
        &[]
    }

    fn version(&self) -> AdapterVersion {
        ADAPTER_VERSION_NOT_APPLICABLE
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum MarketDataChannel {
    Trades,
    Quotes,
    Bars,
    OptionChain,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MarketDataSubscription {
    pub symbol: String,
    pub channel: MarketDataChannel,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubscriptionReceipt {
    pub subscription_id: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum AssetClass {
    Equity,
    Option,
    Future,
    Etf,
    Index,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum NormalizationMode {
    Raw,
    SplitAdjusted,
    FullyAdjusted,
    TotalReturn,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HistoricalDataRequest {
    pub symbol: String,
    pub start: String,
    pub end: String,
    pub resolution: String,
    pub asset_class: AssetClass,
    pub normalization_mode: NormalizationMode,
}

#[derive(Debug, Clone, PartialEq)]
pub struct HistoricalBar {
    pub symbol: String,
    pub close: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct HistoricalQueryResult {
    pub symbol: String,
    pub asset_class: AssetClass,
    pub normalization_mode: NormalizationMode,
    pub bars: Vec<HistoricalBar>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UniverseDownloadRequest {
    pub dataset: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BackfillRequest {
    pub dataset: String,
    pub years: u16,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IncrementalUpdateRequest {
    pub dataset: String,
    pub date: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DataBatch {
    pub records: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FundamentalsRequest {
    pub symbol: String,
    pub statement: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FundamentalDataSet {
    pub records: usize,
}

/// One row of a Sharadar SF1 fundamentals payload, named with the real vendor columns — the input
/// the Sharadar adapter maps onto the vendor-neutral [`FundamentalStatements`] boundary DTO
/// (SRS-DATA-005 / SRS-ARCH-003: the adapter layer maps provider → kind so the core stays
/// vendor-neutral). Monetary columns are pre-scaled to integer minor units (cents) — parsing the
/// vendor wire format (CSV/JSON, dollar floats) into minor units is part of the deferred live fetch;
/// this struct is the post-parse, pre-normalization shape so the *column → field* mapping is unit
/// testable without a network call.
///
/// `reportperiod` is the fiscal PERIOD END; `datekey` is the FILING (availability) instant — Sharadar
/// publishes both precisely so a point-in-time consumer never reads a statement before it was filed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SharadarFundamentalRow {
    /// `ticker` — the security symbol.
    pub ticker: String,
    /// `dimension` — the SF1 statement dimension (e.g. `ARQ` as-reported quarterly, `MRQ`
    /// most-recent-reported quarterly, `ARY`/`MRY` annual, `ART`/`MRT` trailing-twelve-month).
    /// Sharadar emits MULTIPLE rows per `(ticker, reportperiod)` distinguished by this dimension, so
    /// the adapter MUST disambiguate on it or two rows would collapse onto the same vendor-neutral
    /// identity. This substrate supports exactly one dimension ([`SUPPORTED_SHARADAR_DIMENSION`]) and
    /// rejects the rest fail-closed; multi-dimension keying is deferred with the restatement /
    /// filing-version storage-schema change (see `fundamental_ingestion_contract`).
    pub dimension: String,
    /// `reportperiod` — the fiscal period end, epoch seconds → `period_end_ts`.
    pub reportperiod: i64,
    /// `datekey` — the filing / availability instant, epoch seconds → `available_ts`.
    pub datekey: i64,
    /// `revenue` (minor units) → income statement.
    pub revenue_minor: i64,
    /// `netinc` (minor units) → income statement (may be negative).
    pub netinc_minor: i64,
    /// `assets` (minor units) → balance sheet.
    pub assets_minor: i64,
    /// `liabilities` (minor units) → balance sheet.
    pub liabilities_minor: i64,
    /// `equity` (minor units) → balance sheet book value (may be negative).
    pub equity_minor: i64,
    /// `ncfo` — net cash flow from operations (minor units).
    pub ncfo_minor: i64,
    /// `ncfi` — net cash flow from investing (minor units).
    pub ncfi_minor: i64,
    /// `ncff` — net cash flow from financing (minor units).
    pub ncff_minor: i64,
    /// `marketcap` (minor units) → the key-ratio denominator (must be positive).
    pub marketcap_minor: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OptionsImportRequest {
    pub underlying: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OptionsDataSet {
    pub contracts: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UserParquetImportRequest {
    pub path: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AlternativeDataRequest {
    pub dataset: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AlternativeDataSet {
    pub dataset: String,
    pub rows: usize,
}

pub trait BrokerageAdapter: AdapterBoundary {
    fn submit_order(&self, _request: OrderSubmission) -> AdapterResult<OrderReceipt> {
        not_configured(self.provider_name(), "submit_order")
    }

    fn cancel_order(&self, _broker_order_id: &str) -> AdapterResult<()> {
        not_configured(self.provider_name(), "cancel_order")
    }

    fn account_status(&self) -> AdapterResult<DataBatch> {
        not_configured(self.provider_name(), "account_status")
    }

    fn positions(&self) -> AdapterResult<DataBatch> {
        not_configured(self.provider_name(), "positions")
    }
}

pub trait MarketDataAdapter: AdapterBoundary {
    fn subscribe_market_data(
        &self,
        _request: MarketDataSubscription,
    ) -> AdapterResult<SubscriptionReceipt> {
        not_configured(self.provider_name(), "subscribe_market_data")
    }
}

pub trait HistoricalDataAdapter: AdapterBoundary {
    fn historical_data(
        &self,
        _request: HistoricalDataRequest,
    ) -> AdapterResult<HistoricalQueryResult> {
        not_configured(self.provider_name(), "historical_data")
    }
}

pub trait DataProviderAdapter: AdapterBoundary {
    fn provider_family(&self) -> &'static str {
        "data-provider"
    }
}

pub trait BulkEquityDataProvider: DataProviderAdapter {
    fn download_full_universe_daily(
        &self,
        _request: UniverseDownloadRequest,
    ) -> AdapterResult<DataBatch> {
        not_configured(self.provider_name(), "download_full_universe_daily")
    }

    fn initial_historical_backfill(&self, _request: BackfillRequest) -> AdapterResult<DataBatch> {
        not_configured(self.provider_name(), "initial_historical_backfill")
    }

    fn incremental_nightly_update(
        &self,
        _request: IncrementalUpdateRequest,
    ) -> AdapterResult<DataBatch> {
        not_configured(self.provider_name(), "incremental_nightly_update")
    }
}

pub trait FundamentalDataProvider: DataProviderAdapter {
    fn ingest_fundamentals(
        &self,
        _request: FundamentalsRequest,
    ) -> AdapterResult<FundamentalDataSet> {
        not_configured(self.provider_name(), "ingest_fundamentals")
    }
}

pub trait OptionsDataProvider: DataProviderAdapter {
    fn import_options(&self, _request: OptionsImportRequest) -> AdapterResult<OptionsDataSet> {
        not_configured(self.provider_name(), "import_options")
    }
}

pub trait UserParquetDataProvider: DataProviderAdapter {
    fn import_user_parquet(&self, _request: UserParquetImportRequest) -> AdapterResult<DataBatch> {
        not_configured(self.provider_name(), "import_user_parquet")
    }
}

pub trait AlternativeDataProvider: DataProviderAdapter {
    fn fetch_alternative_data(
        &self,
        _request: AlternativeDataRequest,
    ) -> AdapterResult<AlternativeDataSet> {
        not_configured(self.provider_name(), "fetch_alternative_data")
    }
}

pub(crate) const INTERACTIVE_BROKERS_CAPABILITIES: &[AdapterCapability] = &[
    AdapterCapability::Brokerage,
    AdapterCapability::MarketData,
    AdapterCapability::HistoricalData,
];

const DATABENTO_CAPABILITIES: &[AdapterCapability] = &[
    AdapterCapability::BulkEquityData,
    AdapterCapability::HistoricalData,
    AdapterCapability::OptionsData,
];

const SHARADAR_CAPABILITIES: &[AdapterCapability] = &[AdapterCapability::FundamentalData];

const USER_PARQUET_CAPABILITIES: &[AdapterCapability] = &[
    AdapterCapability::HistoricalData,
    AdapterCapability::UserParquetImport,
];

const FUTURE_STUB_CAPABILITIES: &[AdapterCapability] = &[AdapterCapability::AlternativeData];

#[derive(Debug, Default, Clone, Copy)]
pub struct AdapterRegistry;

impl AdapterRegistry {
    pub fn service(&self) -> RuntimeService {
        RuntimeService::BrokerAndDataProviderAdapters
    }
}

#[derive(Debug, Default, Clone, Copy)]
pub struct InteractiveBrokersAdapter;

impl AdapterBoundary for InteractiveBrokersAdapter {
    fn provider_name(&self) -> &'static str {
        "interactive_brokers"
    }

    fn capabilities(&self) -> &'static [AdapterCapability] {
        INTERACTIVE_BROKERS_CAPABILITIES
    }

    fn version(&self) -> AdapterVersion {
        AdapterVersion {
            adapter_version: INTERACTIVE_BROKERS_ADAPTER_VERSION,
            protocol_version: INTERACTIVE_BROKERS_TWS_API_VERSION,
            protocol_label: INTERACTIVE_BROKERS_PROTOCOL_LABEL,
        }
    }
}

impl BrokerageAdapter for InteractiveBrokersAdapter {}
impl MarketDataAdapter for InteractiveBrokersAdapter {}
impl HistoricalDataAdapter for InteractiveBrokersAdapter {}

#[derive(Debug, Default, Clone, Copy)]
pub struct DatabentoAdapter;

impl AdapterBoundary for DatabentoAdapter {
    fn provider_name(&self) -> &'static str {
        "databento"
    }

    fn capabilities(&self) -> &'static [AdapterCapability] {
        DATABENTO_CAPABILITIES
    }
}

impl DataProviderAdapter for DatabentoAdapter {}
impl BulkEquityDataProvider for DatabentoAdapter {}
impl OptionsDataProvider for DatabentoAdapter {}
impl HistoricalDataAdapter for DatabentoAdapter {}

#[derive(Debug, Default, Clone, Copy)]
pub struct SharadarAdapter;

impl AdapterBoundary for SharadarAdapter {
    fn provider_name(&self) -> &'static str {
        "sharadar"
    }

    fn capabilities(&self) -> &'static [AdapterCapability] {
        SHARADAR_CAPABILITIES
    }
}

impl DataProviderAdapter for SharadarAdapter {}
impl FundamentalDataProvider for SharadarAdapter {}

/// The single SF1 statement dimension this substrate ingests: `ARQ` — As-Reported Quarterly.
///
/// Sharadar SF1 emits multiple rows per `(ticker, reportperiod)` distinguished by `dimension`. The
/// AS-REPORTED (`AR*`) family carries the numbers AS THEY WERE ORIGINALLY FILED (point-in-time
/// honest); the MOST-RECENT-REPORTED (`MR*`) family back-fills later restatements onto historical
/// periods, which would inject LOOKAHEAD bias into a factor run. This substrate therefore ingests
/// exactly `ARQ` and rejects every other dimension fail-closed, so two rows for the same ticker +
/// period that differ only by dimension cannot silently collapse onto one vendor-neutral identity.
/// Supporting additional dimensions needs the same filing-version store key as restatements (a
/// storage-schema change, deferred — see `fundamental_ingestion_contract`).
pub const SUPPORTED_SHARADAR_DIMENSION: &str = "ARQ";

impl SharadarAdapter {
    /// Map Sharadar SF1 rows onto the vendor-neutral [`FundamentalStatements`] boundary DTO
    /// (SRS-DATA-005). This is the load-bearing *provider → vendor-neutral* mapping (SRS-ARCH-003):
    /// the vendor columns `ticker` / `reportperiod` (period end) / `datekey` (filing instant) and the
    /// minor-unit line items become the DTO's fields, which the data layer then turns into the
    /// canonical `Fundamental` store records (`atp_data::fundamentals::build_fundamental_records`).
    ///
    /// **Dimension policy (explicit, fail-closed):** only the [`SUPPORTED_SHARADAR_DIMENSION`]
    /// (`ARQ`, as-reported quarterly) is accepted; any other SF1 dimension (`MRQ`, `ARY`, `MRY`,
    /// `ART`, `MRT`, …) is REJECTED, so two rows for the same `(ticker, reportperiod)` that differ
    /// only by dimension can never collapse onto the same vendor-neutral identity (and the
    /// lookahead-prone most-recent-reported family is never ingested).
    ///
    /// Fail-closed: a row carrying an unsupported dimension, or whose `datekey < reportperiod`
    /// (impossible provenance — filed before the period ended), whose market cap is non-positive,
    /// whose nonnegative-domain line items (revenue / assets / liabilities) are negative, or whose
    /// ticker is empty, is surfaced through the common adapter taxonomy as
    /// [`AdapterError::InvalidProviderData`] (so a caller handles a mapping failure the same way as
    /// any other adapter fault — the real adapter routes it to quarantine / SRS-DATA-013).
    ///
    /// The LIVE network fetch ([`FundamentalDataProvider::ingest_fundamentals`]) stays a
    /// `not_configured` stub until SRS-DATA-005's real Sharadar client lands; this deterministic
    /// mapping is the half that does not need the network, and is exercised by the SRS-DATA-005 tests.
    pub fn map_fundamentals(
        &self,
        rows: &[SharadarFundamentalRow],
    ) -> AdapterResult<Vec<FundamentalStatements>> {
        rows.iter()
            .map(|row| {
                // Disambiguate on the SF1 dimension BEFORE collapsing to the vendor-neutral identity:
                // reject any dimension this substrate does not ingest (the multi-dimension key is
                // deferred with the restatement filing-version schema change).
                if row.dimension != SUPPORTED_SHARADAR_DIMENSION {
                    return Err(AdapterError::InvalidProviderData {
                        adapter: self.provider_name(),
                        detail: format!(
                            "unsupported SF1 dimension '{}' for {} (only '{}' as-reported quarterly \
                             is ingested; other dimensions are deferred with multi-filing keying)",
                            row.dimension, row.ticker, SUPPORTED_SHARADAR_DIMENSION
                        ),
                    });
                }
                FundamentalStatements::new(
                    &row.ticker,
                    row.reportperiod,
                    row.datekey,
                    row.revenue_minor,
                    row.netinc_minor,
                    row.assets_minor,
                    row.liabilities_minor,
                    row.equity_minor,
                    row.ncfo_minor,
                    row.ncfi_minor,
                    row.ncff_minor,
                    row.marketcap_minor,
                )
                .map_err(|err: FundamentalStatementsError| AdapterError::InvalidProviderData {
                    adapter: self.provider_name(),
                    detail: err.to_string(),
                })
            })
            .collect()
    }
}

#[derive(Debug, Default, Clone, Copy)]
pub struct UserParquetAdapter;

impl AdapterBoundary for UserParquetAdapter {
    fn provider_name(&self) -> &'static str {
        "user_parquet"
    }

    fn capabilities(&self) -> &'static [AdapterCapability] {
        USER_PARQUET_CAPABILITIES
    }
}

impl DataProviderAdapter for UserParquetAdapter {}
impl UserParquetDataProvider for UserParquetAdapter {}
impl HistoricalDataAdapter for UserParquetAdapter {}

#[derive(Debug, Default, Clone, Copy)]
pub struct FutureStubProvider;

impl AdapterBoundary for FutureStubProvider {
    fn provider_name(&self) -> &'static str {
        "future_stub"
    }

    fn capabilities(&self) -> &'static [AdapterCapability] {
        FUTURE_STUB_CAPABILITIES
    }
}

impl DataProviderAdapter for FutureStubProvider {}
impl AlternativeDataProvider for FutureStubProvider {}

#[cfg(test)]
mod tests {
    use super::*;
    use atp_types::StrategyId;

    fn brokerage_name<T: BrokerageAdapter>(adapter: &T) -> &'static str {
        adapter.provider_name()
    }

    fn bulk_provider_name<T: BulkEquityDataProvider>(adapter: &T) -> &'static str {
        adapter.provider_name()
    }

    fn fundamental_provider_name<T: FundamentalDataProvider>(adapter: &T) -> &'static str {
        adapter.provider_name()
    }

    fn parquet_provider_name<T: UserParquetDataProvider>(adapter: &T) -> &'static str {
        adapter.provider_name()
    }

    fn alternative_provider_name<T: AlternativeDataProvider>(adapter: &T) -> &'static str {
        adapter.provider_name()
    }

    #[test]
    fn identifies_adapter_runtime_boundary() {
        let registry = AdapterRegistry;
        assert_eq!(
            registry.service(),
            RuntimeService::BrokerAndDataProviderAdapters
        );
    }

    #[test]
    fn phase_one_stubs_compile_as_public_adapter_implementations() {
        assert_eq!(
            brokerage_name(&InteractiveBrokersAdapter),
            "interactive_brokers"
        );
        assert_eq!(bulk_provider_name(&DatabentoAdapter), "databento");
        assert_eq!(fundamental_provider_name(&SharadarAdapter), "sharadar");
        assert_eq!(parquet_provider_name(&UserParquetAdapter), "user_parquet");
        assert_eq!(
            alternative_provider_name(&FutureStubProvider),
            "future_stub"
        );
    }

    #[test]
    fn stub_operations_return_not_configured() {
        let adapter = InteractiveBrokersAdapter;
        let request = OrderSubmission {
            strategy_id: StrategyId::new("live-1"),
            symbol: "AAPL".to_string(),
            quantity: 10,
        };
        assert_eq!(
            adapter.submit_order(request).unwrap_err(),
            AdapterError::NotConfigured {
                adapter: "interactive_brokers",
                capability: "submit_order",
            }
        );

        let data_provider = DatabentoAdapter;
        assert_eq!(
            data_provider
                .download_full_universe_daily(UniverseDownloadRequest {
                    dataset: "equities-daily".to_string(),
                })
                .unwrap_err(),
            AdapterError::NotConfigured {
                adapter: "databento",
                capability: "download_full_universe_daily",
            }
        );
    }

    #[test]
    fn brokerage_adapter_exposes_required_surface() {
        let adapter = InteractiveBrokersAdapter;

        assert!(matches!(
            adapter.cancel_order("order-1"),
            Err(AdapterError::NotConfigured {
                capability: "cancel_order",
                ..
            })
        ));
        assert!(matches!(
            adapter.account_status(),
            Err(AdapterError::NotConfigured {
                capability: "account_status",
                ..
            })
        ));
        assert!(matches!(
            adapter.positions(),
            Err(AdapterError::NotConfigured {
                capability: "positions",
                ..
            })
        ));
        assert!(matches!(
            adapter.subscribe_market_data(MarketDataSubscription {
                symbol: "AAPL".to_string(),
                channel: MarketDataChannel::Quotes,
            }),
            Err(AdapterError::NotConfigured {
                capability: "subscribe_market_data",
                ..
            })
        ));
        assert!(matches!(
            adapter.historical_data(HistoricalDataRequest {
                symbol: "AAPL".to_string(),
                start: "2026-01-01".to_string(),
                end: "2026-02-01".to_string(),
                resolution: "1m".to_string(),
                asset_class: AssetClass::Equity,
                normalization_mode: NormalizationMode::SplitAdjusted,
            }),
            Err(AdapterError::NotConfigured {
                capability: "historical_data",
                ..
            })
        ));
    }

    #[test]
    fn interactive_brokers_documents_tws_api_version() {
        let adapter = InteractiveBrokersAdapter;
        let version = adapter.version();
        assert_eq!(version.protocol_label, "IB TWS API");
        assert_eq!(
            version.protocol_version,
            INTERACTIVE_BROKERS_TWS_API_VERSION
        );
        assert_eq!(version.protocol_version, "10.45");
        assert!(!version.adapter_version.is_empty());
    }

    #[test]
    fn default_adapter_version_is_not_applicable() {
        let adapter = SharadarAdapter;
        let version = adapter.version();
        assert_eq!(version, ADAPTER_VERSION_NOT_APPLICABLE);
        assert_eq!(version.protocol_label, "not-applicable");
    }

    fn sample_sharadar_row() -> SharadarFundamentalRow {
        SharadarFundamentalRow {
            ticker: "aapl".to_string(), // lower-case -> normalized to AAPL by the DTO
            dimension: SUPPORTED_SHARADAR_DIMENSION.to_string(),
            reportperiod: 1_700_000_000, // fiscal period end
            datekey: 1_702_000_000,      // filed later
            revenue_minor: 5_000_000,
            netinc_minor: -250_000, // a loss is allowed
            assets_minor: 8_000_000,
            liabilities_minor: 3_000_000,
            equity_minor: 5_000_000,
            ncfo_minor: 900_000,
            ncfi_minor: -400_000,
            ncff_minor: -100_000,
            marketcap_minor: 20_000_000,
        }
    }

    #[test]
    fn sharadar_rows_map_to_vendor_neutral_statements() {
        let adapter = SharadarAdapter;
        let mapped = adapter
            .map_fundamentals(&[sample_sharadar_row()])
            .expect("a well-formed Sharadar row maps cleanly");
        assert_eq!(mapped.len(), 1);
        let s = &mapped[0];
        // Column semantics: reportperiod -> period_end_ts, datekey -> available_ts; ticker normalized.
        assert_eq!(s.symbol(), "AAPL");
        assert_eq!(s.period_end_ts(), 1_700_000_000);
        assert_eq!(s.available_ts(), 1_702_000_000);
        assert_eq!(s.net_income_minor(), -250_000);
        assert_eq!(s.book_equity_minor(), 5_000_000);
        assert_eq!(s.operating_cash_flow_minor(), 900_000);
        assert_eq!(s.market_value_minor(), 20_000_000);
    }

    #[test]
    fn sharadar_mapping_fails_closed_on_impossible_provenance() {
        // datekey (filing) strictly before reportperiod (period end) is impossible -> fail closed
        // through the common adapter taxonomy, so a malformed vendor row never yields a record.
        let adapter = SharadarAdapter;
        let mut row = sample_sharadar_row();
        row.datekey = row.reportperiod - 1;
        let err = adapter
            .map_fundamentals(&[row])
            .expect_err("a filed-before-period-end row must fail closed");
        match err {
            AdapterError::InvalidProviderData { adapter, detail } => {
                assert_eq!(adapter, "sharadar");
                assert!(
                    detail.contains("available_ts"),
                    "detail names the cause: {detail}"
                );
            }
            other => panic!("expected InvalidProviderData, got {other:?}"),
        }
    }

    #[test]
    fn sharadar_mapping_fails_closed_on_non_positive_market_cap() {
        let adapter = SharadarAdapter;
        let mut row = sample_sharadar_row();
        row.marketcap_minor = 0;
        let err = adapter
            .map_fundamentals(&[row])
            .expect_err("a non-positive market cap must fail closed");
        match err {
            AdapterError::InvalidProviderData { adapter, detail } => {
                assert_eq!(adapter, "sharadar");
                assert!(
                    detail.contains("market_value_minor"),
                    "detail names the cause: {detail}"
                );
            }
            other => panic!("expected InvalidProviderData, got {other:?}"),
        }
    }

    #[test]
    fn sharadar_mapping_fails_closed_on_negative_revenue() {
        // A nonnegative-domain line item (revenue) that is negative is a corrupt row -> fail closed.
        let adapter = SharadarAdapter;
        let mut row = sample_sharadar_row();
        row.revenue_minor = -1;
        let err = adapter
            .map_fundamentals(&[row])
            .expect_err("negative revenue must fail closed");
        assert!(matches!(err, AdapterError::InvalidProviderData { .. }));
    }

    #[test]
    fn sharadar_mapping_rejects_unsupported_dimension() {
        // The most-recent-reported quarterly dimension back-fills restatements (lookahead-prone) and
        // is not the ingested dimension -> rejected fail-closed.
        let adapter = SharadarAdapter;
        let mut row = sample_sharadar_row();
        row.dimension = "MRQ".to_string();
        let err = adapter
            .map_fundamentals(&[row])
            .expect_err("an unsupported SF1 dimension must fail closed");
        match err {
            AdapterError::InvalidProviderData { adapter, detail } => {
                assert_eq!(adapter, "sharadar");
                assert!(
                    detail.contains("MRQ"),
                    "detail names the rejected dimension: {detail}"
                );
            }
            other => panic!("expected InvalidProviderData, got {other:?}"),
        }
    }

    #[test]
    fn sharadar_mapping_does_not_collapse_same_period_different_dimension() {
        // Two rows for the SAME ticker + reportperiod that differ ONLY by dimension must NOT silently
        // collapse onto one identity: the unsupported one is rejected, so the whole batch fails closed
        // rather than ingesting an ambiguous basis.
        let adapter = SharadarAdapter;
        let arq = sample_sharadar_row(); // ARQ (supported)
        let mut mrq = sample_sharadar_row();
        mrq.dimension = "MRQ".to_string();
        mrq.netinc_minor = 999_999; // a different (restated) basis for the same period
        let result = adapter.map_fundamentals(&[arq, mrq]);
        assert!(
            matches!(result, Err(AdapterError::InvalidProviderData { .. })),
            "a same-period different-dimension batch must fail closed, not collapse: {result:?}"
        );
    }

    #[test]
    fn data_provider_traits_expose_required_surface() {
        // Bulk equity download / historical backfill / incremental update
        let bulk = DatabentoAdapter;
        assert!(matches!(
            bulk.download_full_universe_daily(UniverseDownloadRequest {
                dataset: "equities-daily".to_string(),
            }),
            Err(AdapterError::NotConfigured {
                capability: "download_full_universe_daily",
                ..
            })
        ));
        assert!(matches!(
            bulk.initial_historical_backfill(BackfillRequest {
                dataset: "equities-daily".to_string(),
                years: 15,
            }),
            Err(AdapterError::NotConfigured {
                capability: "initial_historical_backfill",
                ..
            })
        ));
        assert!(matches!(
            bulk.incremental_nightly_update(IncrementalUpdateRequest {
                dataset: "equities-daily".to_string(),
                date: "2026-05-06".to_string(),
            }),
            Err(AdapterError::NotConfigured {
                capability: "incremental_nightly_update",
                ..
            })
        ));

        // Fundamentals ingestion
        let fundamental = SharadarAdapter;
        assert!(matches!(
            fundamental.ingest_fundamentals(FundamentalsRequest {
                symbol: "AAPL".to_string(),
                statement: "income".to_string(),
            }),
            Err(AdapterError::NotConfigured {
                capability: "ingest_fundamentals",
                ..
            })
        ));

        // Options import
        let options = DatabentoAdapter;
        assert!(matches!(
            options.import_options(OptionsImportRequest {
                underlying: "AAPL".to_string(),
            }),
            Err(AdapterError::NotConfigured {
                capability: "import_options",
                ..
            })
        ));

        // User Parquet import
        let parquet = UserParquetAdapter;
        assert!(matches!(
            parquet.import_user_parquet(UserParquetImportRequest {
                path: "/tmp/data.parquet".to_string(),
            }),
            Err(AdapterError::NotConfigured {
                capability: "import_user_parquet",
                ..
            })
        ));

        // Alternative data
        let alt = FutureStubProvider;
        assert!(matches!(
            alt.fetch_alternative_data(AlternativeDataRequest {
                dataset: "altdata".to_string(),
            }),
            Err(AdapterError::NotConfigured {
                capability: "fetch_alternative_data",
                ..
            })
        ));
    }

    #[test]
    fn data_providers_share_data_provider_adapter_base() {
        fn data_family<T: DataProviderAdapter>(adapter: &T) -> &'static str {
            adapter.provider_family()
        }
        assert_eq!(data_family(&DatabentoAdapter), "data-provider");
        assert_eq!(data_family(&SharadarAdapter), "data-provider");
        assert_eq!(data_family(&UserParquetAdapter), "data-provider");
        assert_eq!(data_family(&FutureStubProvider), "data-provider");
    }

    #[test]
    fn unified_historical_data_interface_routes_through_historical_adapter() {
        fn historical_name<T: HistoricalDataAdapter>(adapter: &T) -> &'static str {
            adapter.provider_name()
        }
        // SRS-DATA-007: strategies/backtests/factor jobs query through a
        // single trait without binding to a specific source provider.
        assert_eq!(historical_name(&DatabentoAdapter), "databento");
        assert_eq!(historical_name(&UserParquetAdapter), "user_parquet");
        assert_eq!(
            historical_name(&InteractiveBrokersAdapter),
            "interactive_brokers"
        );

        let request = HistoricalDataRequest {
            symbol: "AAPL".to_string(),
            start: "2026-01-01".to_string(),
            end: "2026-02-01".to_string(),
            resolution: "1d".to_string(),
            asset_class: AssetClass::Equity,
            normalization_mode: NormalizationMode::SplitAdjusted,
        };
        assert!(matches!(
            DatabentoAdapter.historical_data(request.clone()),
            Err(AdapterError::NotConfigured {
                capability: "historical_data",
                ..
            })
        ));
        assert!(matches!(
            UserParquetAdapter.historical_data(request),
            Err(AdapterError::NotConfigured {
                capability: "historical_data",
                ..
            })
        ));
    }

    #[test]
    fn unified_historical_query_carries_asset_class_and_normalization() {
        // API-7: every (asset_class, normalization_mode) pair must round-trip
        // through the unified request shape so strategies, backtests, factor
        // jobs, and research notebooks can express the SRS-DATA-007 +
        // SRS-DATA-012 query knobs without leaking vendor specifics.
        let asset_classes = [
            AssetClass::Equity,
            AssetClass::Option,
            AssetClass::Future,
            AssetClass::Etf,
            AssetClass::Index,
        ];
        let modes = [
            NormalizationMode::Raw,
            NormalizationMode::SplitAdjusted,
            NormalizationMode::FullyAdjusted,
            NormalizationMode::TotalReturn,
        ];
        for asset_class in asset_classes {
            for normalization_mode in modes {
                let request = HistoricalDataRequest {
                    symbol: "AAPL".to_string(),
                    start: "2026-01-01".to_string(),
                    end: "2026-02-01".to_string(),
                    resolution: "1d".to_string(),
                    asset_class,
                    normalization_mode,
                };
                assert_eq!(request.asset_class, asset_class);
                assert_eq!(request.normalization_mode, normalization_mode);
                assert!(matches!(
                    DatabentoAdapter.historical_data(request),
                    Err(AdapterError::NotConfigured {
                        capability: "historical_data",
                        ..
                    })
                ));
            }
        }
    }

    #[test]
    fn historical_query_result_envelope_is_source_neutral() {
        // The envelope must carry symbol + asset_class + normalization_mode
        // + bars and nothing that exposes a vendor source. Constructing the
        // struct exhaustively would not compile if any forbidden field were
        // added; the explicit assertion below guards the field set.
        let result = HistoricalQueryResult {
            symbol: "AAPL".to_string(),
            asset_class: AssetClass::Equity,
            normalization_mode: NormalizationMode::FullyAdjusted,
            bars: vec![HistoricalBar {
                symbol: "AAPL".to_string(),
                close: 100.0,
            }],
        };
        assert_eq!(result.symbol, "AAPL");
        assert_eq!(result.asset_class, AssetClass::Equity);
        assert_eq!(result.normalization_mode, NormalizationMode::FullyAdjusted);
        assert_eq!(result.bars.len(), 1);
        // The exhaustive destructure proves there are no other public fields.
        let HistoricalQueryResult {
            symbol: _,
            asset_class: _,
            normalization_mode: _,
            bars: _,
        } = result;
    }

    #[test]
    fn unified_historical_data_interface_supports_phase1_normalizations() {
        // SRS-DATA-012 requires raw, split-adjusted, fully adjusted, and
        // total-return normalization modes per security subscription.
        fn run_through<T: HistoricalDataAdapter>(
            adapter: &T,
            mode: NormalizationMode,
        ) -> AdapterResult<HistoricalQueryResult> {
            adapter.historical_data(HistoricalDataRequest {
                symbol: "AAPL".to_string(),
                start: "2026-01-01".to_string(),
                end: "2026-02-01".to_string(),
                resolution: "1d".to_string(),
                asset_class: AssetClass::Equity,
                normalization_mode: mode,
            })
        }
        for mode in [
            NormalizationMode::Raw,
            NormalizationMode::SplitAdjusted,
            NormalizationMode::FullyAdjusted,
            NormalizationMode::TotalReturn,
        ] {
            assert!(matches!(
                run_through(&DatabentoAdapter, mode),
                Err(AdapterError::NotConfigured {
                    capability: "historical_data",
                    ..
                })
            ));
            assert!(matches!(
                run_through(&UserParquetAdapter, mode),
                Err(AdapterError::NotConfigured {
                    capability: "historical_data",
                    ..
                })
            ));
            assert!(matches!(
                run_through(&InteractiveBrokersAdapter, mode),
                Err(AdapterError::NotConfigured {
                    capability: "historical_data",
                    ..
                })
            ));
        }
    }
}
