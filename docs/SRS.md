# Software Requirements Specification (SRS)

**Document ID:** SRS-001  
**Version:** 0.3  
**Status:** Review-response patch applied  
**Last updated:** 2026-05-02  
**Traces from:** StRS v0.7 and SyRS v0.7

---

## 1. Purpose

This Software Requirements Specification defines the software-level
requirements for the Algorithmic Trading Platform (ATP). It translates
stakeholder needs from `docs/StRS_v0.7.md` and system requirements from
`docs/SyRS_v0.7.md` into implementable, verifiable software requirements.

This document is the authoritative source for deriving `feature_list.json`,
software design tasks, verification cases, and implementation acceptance
criteria. No source code requirement is in scope unless it traces to this SRS
and, through this SRS, to the StRS and SyRS.

## 2. Standards Basis and Requirement Quality Rules

This SRS is guided by:

- ISO/IEC/IEEE 29148:2018 expectations for requirements specification,
  including identification, traceability, verifiability, consistency, and
  stakeholder alignment.
- INCOSE Guide for Writing Requirements quality characteristics: necessary,
  appropriate, unambiguous, complete, singular, feasible, verifiable, correct,
  conforming, and traceable.
- NASA Systems Engineering Handbook expectations for requirements flowdown,
  verification planning, interface definition, and risk-aware design.

Each software requirement in Section 5 uses the following conventions:

- **Shall** identifies a binding requirement.
- **Acceptance criteria** define observable pass conditions.
- **Verification method** is one or more of Inspection, Analysis, Test, or
  Demonstration.
- **Trace** links each requirement to SyRS requirements and StRS needs,
  constraints, or business goals.
- **Priority** follows SyRS priority where available: P1 is release-critical,
  P2 is required but not necessarily in the first executable slice, and P3 is
  deferred unless explicitly scheduled.

## 3. Scope and Constraints

ATP is a single-user algorithmic trading platform for research, backtesting,
internal paper simulation, and live trading through Interactive Brokers.

In scope:

- Python-authored user strategies.
- Rust core runtime services.
- Interactive Brokers live execution for exactly one live strategy at a time.
- Internal paper-trading simulation for Reservoir strategies.
- Databento market data ingestion, Sharadar fundamental ingestion, IB live
  market data, and IB option-chain capture.
- SSD-primary and NAS-archival storage.
- Backtesting, factor computation, Jupyter research, dashboard monitoring,
  notifications, kill switch, Hot-Swap, and deployment automation.

Out of scope:

- High-frequency trading infrastructure.
- Multi-user authentication, role-based access control, or multi-tenancy.
- Cryptocurrency, futures, alternative data integrations, or non-IB brokerages
  for the initial implementation.
- Institutional compliance reporting.
- Native mobile application.
- Cloud VPS deployment execution for the release baseline. Cloud deployment is a
  future target; Phase 1 verification is against the Proxmox Docker Compose
  deployment.
- Platform-enforced pre-trade risk controls such as per-strategy notional,
  exposure, fat-finger, or throttle limits beyond the safeguards explicitly
  required by the SyRS.
- Credential rotation without service restart for IB, SMTP, and SMS
  credentials. Encryption-at-rest is required (SRS-SEC-001); rotation without
  service restart is deferred to a future phase.

## 4. Software Architecture Modules

The software shall be organized so that implementation dependencies comply with
SyRS AC-11:

`Types/Models -> Data Layer -> Strategy Engine -> Execution Engine / Internal Simulation Engine -> Brokerage Adapter`

Dashboard and orchestration components may consume lower layers, but lower
layers shall not depend on the dashboard or orchestrator. Strategy containers
shall use only the Strategy API and shall not call the orchestrator directly.

| Module | Responsibility | Primary implementation | SyRS trace |
|---|---|---|---|
| Types and Domain Models | Shared identifiers, orders, positions, events, errors, metrics, schema versions, asset models, and serialization contracts | Rust crate plus generated Python bindings where needed | AC-11, SYS-64, SYS-66 |
| Data Layer | Ingestion, validation, storage catalog, SSD/NAS tiering, corporate actions, normalization, historical queries, schema evolution, and concurrent reads | Rust | SYS-22a through SYS-31, SYS-53, SYS-56, SYS-63, SYS-66 through SYS-69, SYS-77, SYS-78, AC-5, AC-8 |
| Strategy API and Python SDK | Python API for user strategies, scheduling, market data, order submission, callbacks, warm-up, indicators, logging, and state | Python package backed by runtime service clients | AC-1, AC-14, SYS-6 through SYS-8, SYS-12, SYS-35, SYS-64, NFR-U2 |
| Strategy Orchestrator | Strategy container lifecycle, resource profiles, health checks, live/paper designation, code versioning, rollback, and workload priority | Rust | SYS-9 through SYS-13, SYS-57, SYS-58, SYS-79, SYS-80, AC-12, AC-16 |
| Execution Engine | Live order routing, order state machine, live strategy enforcement, IB order events, kill-switch execution, stale-data blocking, and watchdog recovery | Rust | SYS-1 through SYS-7, SYS-39a, SYS-44a, SYS-44b, SYS-45, SYS-90, AC-15, AC-16 |
| Internal Simulation Engine | Local paper order execution, virtual ledgers, simulated callbacks, paper metrics, corporate actions, and paper state persistence | Rust | SYS-2b, SYS-47, SYS-82 through SYS-89, AC-10, AC-16 |
| Market Data Subscription Manager | Deduplicated IB subscriptions, line-limit enforcement, heartbeat monitoring, data fan-out, and stale-data state | Rust | SYS-39, SYS-39a, SYS-70, NFR-P5, AC-16 |
| Broker and Data Provider Adapters | IB Gateway, Databento, Sharadar, user Parquet, and future adapter boundaries | Rust adapter modules | SYS-25, SYS-26, SYS-52 through SYS-56, SYS-65, AC-2, AC-7, AC-8 |
| Backtesting and Optimization | Deterministic backtests, cost models, benchmark metrics, factor tearsheets, parameter sweeps, walk-forward analysis, and persisted results | Rust runtime with Python strategy execution boundary | SYS-14 through SYS-21, SYS-62, AC-4, AC-16 |
| Factor Pipeline Runtime | Scheduled full-universe factor computation and ranking | Rust | SYS-32, SYS-33, SYS-57, AC-16 |
| Dashboard and API | Web UI, REST API, WebSocket updates, CLI backing API, account view, Reservoir view, logs, backtests, Jupyter embedding, and operator controls | Dashboard/API may use another language; core runtime remains Rust | SYS-36 through SYS-43b, IF-6, IF-8, IF-9, AC-16 |
| Notification Dispatcher | Email, SMS, dashboard alerts, delivery status, and critical failure alerts | Rust | SYS-46, SYS-61, NFR-P6, NFR-S4, AC-16 |
| Observability and CI | Logs, metrics, traces, architecture checks, requirements trace checks, and automated verification | Rust/Python/CI scripts | SYS-61, NFR-U1, AC-7 through AC-16 |

## 5. Software Requirements

### 5.1 Architecture and Platform Requirements

| ID | Software requirement | Acceptance criteria | Verification method | Priority | Trace |
|---|---|---|---|---|---|
| SRS-ARCH-001 | The software shall implement core ATP runtime services in Rust and user-authored strategy interfaces in Python. | Build manifests show Rust crates for core runtime services; Python package exposes the strategy API; no core runtime service is implemented only in Python. | Inspection, architecture test | P1 | SyRS: AC-1, AC-16; StRS: C-1, C-12 |
| SRS-ARCH-002 | The software shall enforce the dependency direction `Types/Models -> Data Layer -> Strategy Engine -> Execution/Simulation -> Adapters`. | Automated dependency check fails if a lower layer imports the dashboard, orchestrator, or vendor-specific adapter modules outside permitted boundaries. | Inspection, architecture test | P1 | SyRS: AC-7, AC-8, AC-11; StRS: SN-3.01, SN-3.02, SN-3.03, BG-5 |
| SRS-ARCH-003 | The software shall isolate vendor integrations behind adapter interfaces. | IB, Databento, Sharadar, user Parquet, and future stub providers compile as adapter implementations; core strategy, backtest, and data-query code contains no vendor-specific imports; a structural integration test implements a fictional asset-class or alternative-data adapter using only public adapter interfaces and confirms no core module files are modified. | Inspection, structural test | P1 | SyRS: SYS-52, SYS-53, SYS-54, SYS-56, AC-7, AC-8; StRS: SN-3.02, SN-3.03, SN-3.04, BG-5 |
| SRS-ARCH-004 | The software shall deploy as a Docker Compose stack on the Phase 1 Proxmox Ubuntu VM target. | Compose configuration starts core services, dashboard/API, strategy containers, Jupyter, IB Gateway integration configuration, SSD paths, and NAS paths with environment-specific configuration; documentation states that cloud VPS deployment is a future target outside the release baseline and identifies portability constraints for future deployment. | Demonstration, inspection | P1 | SyRS: AC-12, AC-13, NFR-SC3; StRS: SN-2.07, C-5, BG-6 |
| SRS-ARCH-005 | The software shall provide a configuration system for credentials, storage paths, IB account settings, market-data line limits, resource limits, and notification channels. | Required configuration keys are documented, validated at startup, and reported as structured readiness failures when missing or invalid. | Test, inspection | P1 | SyRS: SYS-55, SYS-70, SYS-76, NFR-S1, NFR-S4; StRS: C-2, C-5, C-6, C-7, SN-1.12 |

### 5.2 Strategy Orchestration and Strategy API Requirements

| ID | Software requirement | Acceptance criteria | Verification method | Priority | Trace |
|---|---|---|---|---|---|
| SRS-ORCH-001 | The software shall run each strategy instance in an isolated Docker container managed only by the Strategy Orchestrator. | Starting, stopping, restarting, destroying, and health-checking a strategy instance occurs through orchestrator APIs; strategy containers cannot manage other containers; an unresponsive strategy container is restarted automatically, logged, and displayed on the dashboard; startup from orchestrator command to strategy ready, excluding warm-up, completes within 30 seconds. | Test, inspection, performance test | P1 | SyRS: SYS-10, SYS-13, AC-12, NFR-P9, NFR-R5, NFR-S5; StRS: SN-1.10, SN-2.03, SN-2.05 |
| SRS-ORCH-002 | The software shall enforce configurable resource profiles for live and paper strategy containers. | Default live profile is no more than 512 MB RAM and 0.25 CPU cores; default paper profile is no more than 300 MB RAM and 0.10 CPU cores; configuration overrides are validated. | Test, inspection | P1 | SyRS: SYS-11, SYS-57, SYS-58, NFR-SC1; StRS: SN-1.10, BG-6 |
| SRS-ORCH-003 | The software shall enforce workload priority when the configured host memory safety margin would be breached. | With available memory below the configured safety margin, new lower-priority workloads are refused; if a higher-priority workload requires resources, the lowest-priority active batch workload is terminated according to the SYS-57 hierarchy; dashboard and notification alerts are emitted; live strategy execution is not terminated for lower-priority work. | Test, fault injection | P1 | SyRS: SYS-57, SYS-58; StRS: SN-1.10, C-6, BG-1, BG-6 |
| SRS-ORCH-004 | The software shall record and expose the deployed code version for each strategy instance. | Each deployment stores a source hash and timestamp; dashboard, REST API, and backtest results display or return the same version identifier. | Test, inspection | P1 | SyRS: SYS-41, SYS-79, SYS-21; StRS: SN-1.01, SN-1.02, SN-1.10 |
| SRS-ORCH-005 | The software shall support rollback to the previous deployed strategy version. | Rollback is available through dashboard, CLI, and REST API; rollback of the live strategy requires the same confirmation control as live promotion. | Demonstration, test | P2 | SyRS: SYS-80, NFR-S2; StRS: SN-1.01, SN-1.10 |
| SRS-SDK-001 | The software shall provide a Python Strategy API that is identical for live IB execution and internal paper simulation, where internal paper simulation consumes live market data and produces simulated fills using fictional capital (per SYS-82 through SYS-87). | The same strategy source runs without execution-mode branches in live IB execution and internal paper simulation (per AC-14); order, data, logging, state, and callback APIs have identical signatures; the IB paper account integration test path (SYS-2e) executes the same strategy source. | Contract test, inspection | P1 | SyRS: IF-7, SYS-12, SYS-82, AC-1, AC-14; StRS: SN-1.01, SN-1.29, SN-3.01 |
| SRS-SDK-002 | The software shall expose trading-calendar-aware scheduling primitives through the Python Strategy API. | Strategies can schedule actions at market open, market close, every N minutes, and cron-like expressions; NYSE, NASDAQ, and CBOE holidays, early closes, pre-market and after-hours session boundaries, US Eastern time zone behavior, and daylight saving transitions resolve according to the trading calendar. | Test | P1 | SyRS: SYS-6, SYS-50, SYS-51; StRS: SN-1.09, SN-1.19, BG-1, BG-7 |
| SRS-SDK-003 | The software shall expose strategy subscriptions for equity and option market data while enforcing one tradable asset class per strategy instance. | A strategy configured for equities or options can subscribe to both asset classes for analysis; order submission is rejected when the strategy attempts to trade an unconfigured asset class. | Contract test | P1 | SyRS: SYS-5, SYS-64; StRS: SN-1.07, BG-1, BG-5 |
| SRS-SDK-004 | The software shall deliver order event callbacks to Python strategy code. | Fill, partial fill, cancellation, and rejection callbacks include fill price, fill quantity, commission, and order identifiers; live callback delivery is less than 1,000 ms p95 from broker fill acknowledgement; paper callback delivery is less than 100 ms p95 from simulated fill. | Contract test, performance test | P1 | SyRS: SYS-7, SYS-85, NFR-P4; StRS: SN-1.22, SN-1.29 |
| SRS-SDK-005 | The software shall provide a warm-up mechanism before live, paper, and backtest execution. | A strategy configured with a 200-bar warm-up receives historical bars before the first executable bar and begins with indicator buffers initialized. | Test | P1 | SyRS: SYS-8, NFR-R3; StRS: SN-1.23, BG-1, BG-2, BG-7 |
| SRS-SDK-006 | The software shall expose built-in technical indicators through wrappers around pandas-ta and TA-Lib. | SMA, EMA, RSI, MACD, Bollinger Bands, and ATR match pandas-ta or TA-Lib reference outputs and support incremental updates on each new bar. | Test, inspection | P1 | SyRS: SYS-35, AC-6; StRS: SN-1.20, C-9 |
| SRS-SDK-007 | The software shall expose time-based bar consolidation and resampling through the Strategy API. | Minute data can be consolidated into 5-minute, 15-minute, hourly, and daily bars without pre-processed datasets. | Test | P2 | SyRS: SYS-30a; StRS: SN-1.21 |
| SRS-SDK-008 | The software shall expose non-standard bar generation through the Strategy API. | Renko and range bars can be generated from tick or minute-resolution input data. | Test | P3 | SyRS: SYS-30b; StRS: SN-1.21 |
| SRS-SDK-009 | The software shall document the Python Strategy API for strategy authors. | Public Strategy API functions include Python docstrings and usage examples sufficient for a Python-proficient trader to author a new strategy without reading platform internals. | Inspection, documentation test | P1 | SyRS: NFR-U2; StRS: SN-1.01, C-1 |

### 5.3 Live Execution and Brokerage Requirements

| ID | Software requirement | Acceptance criteria | Verification method | Priority | Trace |
|---|---|---|---|---|---|
| SRS-EXE-001 | The software shall route orders to IB only for the designated live strategy. | Live designation requires explicit user confirmation; with one live strategy and at least 30 paper strategies running, only the live strategy can submit to IB; all other IB-bound attempts are rejected with a structured error; live order acknowledgement is less than 1,000 ms p95 from strategy API invocation to strategy acknowledgement callback under the reference baseline, excluding IB-to-exchange network round trip. | Integration test, structural test, performance test | P1 | SyRS: SYS-1, SYS-2a, SYS-2c, SYS-2d, AC-15, NFR-P1, NFR-S2; StRS: SN-1.01, SN-1.06, SN-1.11, C-11 |
| SRS-EXE-002 | The software shall route all non-live strategy orders to the internal simulation engine. | Paper strategy orders never create IB orders; the IB paper account is available only through operator-initiated adapter integration tests. | Test, inspection | P1 | SyRS: SYS-2b, SYS-2e, AC-10; StRS: SN-1.06, SN-1.29, C-11 |
| SRS-EXE-003 | The software shall support market, limit, stop, and stop-limit orders for equities and options in live and paper modes. | Each order type can be accepted, validated, state-tracked, and acknowledged in both live adapter test mode and internal simulation. | Contract test, integration test | P1 | SyRS: SYS-3, SYS-82; StRS: SN-1.08, BG-1 |
| SRS-EXE-004 | The software shall support multi-leg options orders as composite transactions. | A four-leg options order is submitted as one composite order in IB live test mode, simulated as one composite order in paper mode, and displayed as one composite dashboard position. | Integration test, demonstration | P1 | SyRS: SYS-4, SYS-40, SYS-82; StRS: SN-1.24 |
| SRS-EXE-005 | The software shall persist live strategy state needed for restart recovery and shall re-execute the warm-up mechanism on restart. | Pending submissions, awaiting acknowledgements, broker IDs, order statuses, fill events, correlation IDs, open positions, account equity snapshot, and the user-accessible JSON-serializable state dictionary survive execution engine restart without duplicate submissions; warm-up (SRS-SDK-005) is re-executed on restart to reconstruct indicator buffers and rolling windows from historical data; persisted live state is recoverable within 60 seconds of container restart, excluding warm-up duration. | Fault injection, test | P1 | SyRS: SYS-90, NFR-R3; StRS: SN-2.05, SN-1.01, BG-1 |
| SRS-EXE-006 | The software shall implement the initial brokerage adapter for headless IB Gateway. | The adapter passes automated IB paper-account tests for order submission, cancellation, market data subscription, and historical data retrieval without depending on the TWS GUI. | Integration test, inspection | P1 | SyRS: SYS-52, AC-2; StRS: C-2, SN-3.02 |
| SRS-EXE-007 | The software shall manage IB TWS API version compatibility for the brokerage adapter. | The adapter documents the supported IB TWS API version; API version upgrades are tested against the IB paper trading account before deployment to live trading. | Integration test, inspection | P2 | SyRS: SYS-65; StRS: C-2, SN-3.02 |
| SRS-EXE-008 | The software shall implement an order lifecycle state machine with documented states and transitions, and shall use a strategy-supplied client correlation ID as the idempotency key for live and paper order submissions. | Order states `{NEW, PENDING_SUBMIT, ACKED, PARTIALLY_FILLED, FILLED, CANCEL_PENDING, CANCELLED, REJECTED, EXPIRED}` are implemented with a documented transition graph; each order carries a client-assigned correlation ID stable across restarts; duplicate submissions for the same correlation ID are rejected idempotently with a structured error per SRS-ERR-001; cancel-replace operates as cancel-then-new with the original correlation ID retained for audit. | Test, inspection | P1 | SyRS: SYS-3, SYS-7, SYS-64, SYS-90, NFR-R3; StRS: SN-1.08, SN-1.22 |
| SRS-EXE-009 | The software shall durably commit live order intents before submission to IB and shall use the durable record to reconcile broker state on restart. | Order intents are written to a durable outbox before IB submission; on restart, the execution engine consults the outbox and treats acknowledged broker IDs as bound to their correlation IDs (SRS-EXE-008); replayed intents that already have an acknowledged broker ID are not resubmitted; outbox entries are retained until the corresponding terminal state (FILLED, CANCELLED, REJECTED, or EXPIRED) is observed. | Fault injection, test | P1 | SyRS: SYS-90, NFR-R3, NFR-R4; StRS: SN-2.05 |

### 5.4 Market Data Requirements

| ID | Software requirement | Acceptance criteria | Verification method | Priority | Trace |
|---|---|---|---|---|---|
| SRS-MD-001 | The software shall consolidate duplicate real-time market data subscriptions across active strategies. | Multiple strategies subscribing to the same security consume one IB subscription; each subscriber receives fan-out data with no more than 100 ms additional latency relative to the IB feed. | Integration test, performance test | P1 | SyRS: SYS-70; StRS: SN-1.10, SN-1.29, SC-25, A-13 |
| SRS-MD-002 | The software shall enforce the configured IB market-data line limit. | A subscription request that would exceed the configured account limit is rejected with `SUBSCRIPTION_LIMIT_REACHED` and displayed as an operator alert. | Test | P1 | SyRS: SYS-70, SYS-64; StRS: A-13, C-7 |
| SRS-MD-003 | The software shall monitor market data and broker heartbeat freshness continuously. | Market data and IB Gateway heartbeat staleness over 15 seconds is detected, logged, displayed, and reflected in system health status. | Test, demonstration | P1 | SyRS: SYS-39, NFR-P5; StRS: SN-2.03 |
| SRS-MD-004 | The software shall block live and simulated order submission when required market data is stale. | While subscribed market data is stale, live and paper order submissions return `MARKET_DATA_STALE`; submissions resume when fresh data is received. | Test | P1 | SyRS: SYS-39a, SYS-64, SYS-87; StRS: SN-2.04, SN-1.29 |
| SRS-MD-005 | The software shall handle the scheduled IB Gateway daily restart as planned maintenance. | During the configured restart window, order submission and market data requests are suspended beginning 60 seconds before the expected restart; normal connectivity notifications are suppressed for the configured window defaulting to 5 minutes; automatic reconnection is attempted after the window; if IB Gateway remains unavailable after the window, standard connectivity loss handling occurs. | Integration test, fault injection | P1 | SyRS: SYS-75, SYS-45, SYS-46, NFR-R2; StRS: C-2, SN-2.04, SN-2.05 |
| SRS-MD-006 | The software shall execute a startup readiness check before enabling live trading. | Live strategy startup is blocked until IB connectivity/authentication, IB account data, SSD data layer access, ingestion freshness within one trading day, system service health, and NAS reachability or degraded-mode alert pass; paper strategies may start only after the market data subscription manager and internal simulation engine are available; failures hold the system in pre-trade state unless manually overridden with an operator alert. | Test, demonstration | P1 | SyRS: SYS-76, NFR-R6; StRS: SN-2.04, SN-2.05, BG-6 |
| SRS-MD-007 | The market-data subscription manager shall detect sequence gaps in IB tick streams and reflect gap state in heartbeat/staleness. | Gap events are logged via SRS-LOG-001 with symbol, expected sequence, observed sequence, and timestamp; affected subscriptions enter the stale state until a recovery condition is satisfied (fresh tick with monotonic sequence or operator-acknowledged resync); stale-state transitions block live and paper order submission per SRS-MD-004 and are visible on the dashboard. | Test, fault injection | P1 | SyRS: SYS-39, SYS-39a, SYS-70, NFR-P5; StRS: SN-2.03, SN-2.04 |

### 5.5 Data Ingestion, Storage, and Query Requirements

| ID | Software requirement | Acceptance criteria | Verification method | Priority | Trace |
|---|---|---|---|---|---|
| SRS-DATA-001 | The software shall ingest daily OHLCV data for all US equities from Databento on a scheduled nightly basis. | A nightly job retrieves 8,000+ securities, writes validated data to SSD, syncs to NAS, and completes within the overnight window of 16:00 ET to 09:30 ET next trading day. | Integration test, performance test | P1 | SyRS: SYS-22a, SYS-31, NFR-P8a; StRS: SN-1.26, C-10, BG-3 |
| SRS-DATA-002 | The software shall ingest minute-bar OHLCV data from IB for a configurable watchlist. | The default watchlist includes securities with active strategies; the watchlist is editable by dashboard and API; projected request counts are validated against IB pacing limits of no more than 60 historical data requests per 10 minutes and no identical requests within 15 seconds; over-budget jobs are refused at scheduling time with an operator alert. | Integration test, test | P1 | SyRS: SYS-22b, SYS-31, SYS-55, NFR-P8b; StRS: SN-1.26, C-7 |
| SRS-DATA-003 | The software shall support initial bulk backfill from Databento. | Backfill retrieves the maximum available history offered by the bulk vendor at deployment, with floors of ≥ 15 years daily and ≥ 6 months minute, otherwise the deployment shall record a deviation note; daily and minute data are stored through the same validation and catalog path as incremental ingestion. | Integration test, inspection | P1 | SyRS: SYS-22c, SYS-56; StRS: SN-1.26, C-10 |
| SRS-DATA-004 | The software shall capture live option-chain snapshots from IB during the configured near-close window. | Snapshot records include underlying, expiration, strike, right, bid, ask, last, volume, open interest, and implied volatility; expired contracts remain queryable after expiration; projected capture request counts are validated against IB pacing limits and over-budget captures are refused at scheduling time with an operator alert. | Integration test | P1 | SyRS: SYS-23, SYS-24, SYS-31, SYS-55, NFR-P8c; StRS: SN-1.27, C-7 |
| SRS-DATA-005 | The software shall ingest Sharadar fundamental data on a scheduled basis. | Income statement, balance sheet, cash flow statement, and key ratio records for US equities are ingested, validated, cataloged, available to the factor pipeline, and completed within the overnight window of 16:00 ET to 09:30 ET next trading day. | Integration test | P1 | SyRS: SYS-26, NFR-P8d; StRS: SN-3.03, BG-3 |
| SRS-DATA-006 | The software shall import historical options data from Databento DBN and Parquet. | DBN and Parquet inputs are accepted, normalized into the system catalog, and queryable by symbol, contract, date range, and resolution. | Test, integration test | P1 | SyRS: SYS-25; StRS: SN-1.28, C-8 |
| SRS-DATA-007 | The software shall provide a unified historical data access interface. | Strategy code, backtests, factor jobs, and notebooks query by symbol, date range, and resolution without specifying the original source provider. | Contract test | P1 | SyRS: SYS-27, SYS-53; StRS: SN-1.28, SN-3.03, BG-5 |
| SRS-DATA-008 | The software shall implement SSD-primary and NAS-archival tiered storage. | All ingestion writes to SSD first; new data is synced to NAS; SSD retains at least 90 days of configured hot data; NAS is used for indefinite retention; storage growth estimates are documented in Section 12.1. | Test, inspection | P1 | SyRS: SYS-24, SYS-67, AC-5, NFR-SC2; StRS: C-5, SN-1.26, SN-1.27 |
| SRS-DATA-009 | The software shall transparently fall back to NAS for cold historical reads. | Requests outside SSD retention are served from NAS and cached on SSD without requiring consumer code changes; cold-read cache entries do not exceed the configurable SSD share defaulting to 20 percent and are evicted before hot runtime data. | Test | P1 | SyRS: SYS-68; StRS: SN-1.28, BG-5 |
| SRS-DATA-010 | The software shall evict SSD cache data according to the configured storage policy. | At the default 80 percent high-water mark, eviction prioritizes old inactive data; data for securities with the currently running live strategy is never evicted; data accessed within the configurable recency window defaulting to 24 hours by a running backtest or factor pipeline job is not evicted. | Test | P1 | SyRS: SYS-69; StRS: C-5, BG-6 |
| SRS-DATA-011 | The software shall adjust historical price data for corporate actions. | Splits, reverse splits, dividends, delistings, mergers, and symbol changes are reflected in historical records so that backtests spanning corporate-action dates produce correct P&L calculations under the selected normalization mode. | Test, scenario demonstration | P1 | SyRS: SYS-28a; StRS: SN-1.14 |
| SRS-DATA-012 | The software shall support raw, split-adjusted, fully adjusted, and total-return normalization modes per security subscription. | Historical and live subscription requests can select a normalization mode; options strategies can request raw prices; indicators can request adjusted series. | Test | P1 | SyRS: SYS-29; StRS: SN-1.15 |
| SRS-DATA-013 | The software shall validate ingested market and options records before writing to primary storage. | Records failing structural, range, duplicate, or required-field checks are quarantined and not written to primary tables; dashboard and notification alerts include counts and reasons. | Test | P1 | SyRS: SYS-77; StRS: SN-1.26, SN-1.27 |
| SRS-DATA-014 | The software shall detect configured anomalous data conditions during ingestion. | Price moves exceeding a configurable threshold defaulting to 50 percent without a known corporate action, volume exceeding a configurable multiple defaulting to 20 times the 20-day average volume, and missing records for securities that traded on the exchange calendar date produce non-blocking dashboard alerts. | Test, analysis | P2 | SyRS: SYS-78; StRS: SN-1.26, SN-1.14 |
| SRS-DATA-015 | The software shall support schema evolution for stored data entities. | Each persisted entity records a schema version; data written under older schema versions remains queryable after schema updates without bulk migration. | Test, inspection | P2 | SyRS: SYS-66; StRS: SN-1.26, SN-1.27, C-5 |
| SRS-DATA-016 | The software shall make ingestion jobs idempotent. | Re-running Databento, IB, option-chain, or Sharadar ingestion for an already ingested date creates no duplicate records and does not corrupt existing data. | Test | P1 | SyRS: NFR-R4; StRS: SN-1.26, SN-1.27 |
| SRS-DATA-017 | The software shall support concurrent reads during ingestion writes. | Strategy containers, backtests, factor jobs, and notebooks read previously ingested data while ingestion jobs write new data without corruption or blocking completed data. | Load test | P1 | SyRS: SYS-63; StRS: SN-1.26, SN-1.28 |
| SRS-DATA-018 | The software shall provide scheduled backup and validated recovery support for NAS-stored data. | Weekly default backups can export NAS data to an external target; backup completion validates integrity; documented RPO is no more than 7 days. | Demonstration, inspection | P2 | SyRS: SYS-59, SYS-60; StRS: C-5, BG-6 |
| SRS-DATA-019 | The software shall adjust or cancel live resting orders affected by corporate actions. | When a split or reverse split affects a security with resting live orders, order quantities and limit or stop prices are adjusted; when adjustment is not possible, including delisting cases, affected orders are canceled and the operator is notified through strategy callback and notification subsystem. | Scenario test | P1 | SyRS: SYS-28b; StRS: SN-1.14 |
| SRS-DATA-020 | The software shall adjust live positions affected by corporate actions. | Live position quantities and average cost basis are adjusted for splits, reverse splits, and dividends; mergers and symbol changes remap positions to successor securities; delistings mark positions as delisted and notify the operator. | Scenario test | P1 | SyRS: SYS-28c; StRS: SN-1.14 |
| SRS-DATA-021 | The software shall apply corporate actions to paper strategy virtual positions and orders. | Paper strategy virtual positions and average cost are adjusted for splits, dividends, and mergers; virtual orders for delisted securities are canceled using the same corporate-action data source as live trading and backtesting. | Scenario test | P1 | SyRS: SYS-88; StRS: SN-1.14, SN-1.29 |

### 5.6 Backtesting, Research, and Factor Requirements

| ID | Software requirement | Acceptance criteria | Verification method | Priority | Trace |
|---|---|---|---|---|---|
| SRS-BT-001 | The software shall backtest Python strategies against stored data and user-uploaded Parquet data over configurable date ranges. | A backtest can be launched with system data or uploaded Parquet data; start and end dates are selectable through API and dashboard. | Test, demonstration | P1 | SyRS: SYS-14, SYS-43a, AC-4; StRS: SN-1.02, SN-1.13, C-4 |
| SRS-BT-002 | The software shall apply configurable commission, slippage, and spread-impact models to backtests. | Defaults match SyRS values; a backtest run can override commission, slippage, and spread-impact models without changing strategy code. | Test | P1 | SyRS: SYS-15a, SYS-15b, SYS-15c, SYS-15d; StRS: SN-1.03 |
| SRS-BT-003 | The software shall use the same transaction-cost model family for internal simulation and backtesting unless configured otherwise. | A paper strategy and backtest using identical cost configuration compute fills and commissions from the same model family. | Test | P1 | SyRS: SYS-15e, SYS-83; StRS: SN-1.03, SN-1.29 |
| SRS-BT-004 | The software shall compute required backtest and paper/live performance metrics. | Sharpe ratio, Sortino ratio, alpha, beta, maximum drawdown, annualized return, annualized volatility, and win rate are produced for completed backtests, paper strategies, and live dashboard reporting. | Test | P1 | SyRS: SYS-16, SYS-86; StRS: SN-1.04, SN-1.05, SN-1.29 |
| SRS-BT-005 | The software shall compare strategy performance against a user-selected benchmark defaulting to SPY. | If no benchmark is selected, SPY is used; alpha and beta are computed against the selected benchmark; dashboard and backtest reports identify the benchmark. | Test, demonstration | P1 | SyRS: SYS-17, SYS-36, SYS-37; StRS: SN-1.04 |
| SRS-BT-006 | The software shall produce factor analysis and tear-sheet outputs. | Factor returns, information coefficient, and turnover analysis are available for completed factor-analysis runs. | Test | P1 | SyRS: SYS-18; StRS: SN-1.05 |
| SRS-BT-007 | The software shall support grid search and multidimensional parameter sweeps for backtests. | A parameter space definition produces ranked backtest results by the selected objective function. | Test | P2 | SyRS: SYS-19; StRS: SN-1.16 |
| SRS-BT-008 | The software shall support walk-forward analysis. | In-sample windows are optimized, out-of-sample windows are evaluated, and outputs preserve the parameter set and metrics per window. | Test | P2 | SyRS: SYS-20; StRS: SN-1.17 |
| SRS-BT-009 | The software shall persist completed backtest results. | Parameters, metrics, trade log, equity curve, benchmark comparison, strategy code version, and timestamp are queryable by strategy, date range, and parameter set. | Test | P1 | SyRS: SYS-21, SYS-79; StRS: SN-1.02, SN-1.04 |
| SRS-BT-010 | The software shall produce deterministic backtest results for identical inputs. | Repeated runs with identical strategy code, parameters, data, date range, seed, and cost model produce identical trade logs, equity curves, and metrics; platform parallelism, floating-point ordering, and platform-generated random values do not introduce nondeterminism. | Test | P1 | SyRS: SYS-62; StRS: SN-1.02 |
| SRS-FAC-001 | The software shall compute scheduled factors across the full US equity universe. | A factor job processes 8,000+ securities using market and Sharadar data, resolves its schedule through the same trading calendar used by strategy scheduling, and completes before the user-configured scheduled deadline. | Performance test | P1 | SyRS: SYS-32, SYS-33, SYS-51, NFR-P7; StRS: SN-2.06, BG-3 |
| SRS-RES-001 | The software shall embed the Jupyter research environment in the dashboard workflow. | Jupyter is reachable from the dashboard without navigating to a separate service URL and runs independently of live strategies, paper strategies, and backtests. | Demonstration | P1 | SyRS: SYS-34a, SYS-34c; StRS: SN-1.18, SN-2.01 |
| SRS-RES-002 | The software shall provide Jupyter access to historical data, indicators, and plotting. | Notebook code can query the unified data interface, compute pandas-ta or TA-Lib indicators, and render plots without access to live order submission. | Test, demonstration | P1 | SyRS: SYS-34b, NFR-S6; StRS: SN-1.18 |
| SRS-RES-003 | The software shall provide primary dashboard navigation to the embedded Jupyter research environment. | The operator can open the embedded Jupyter environment from the primary dashboard workflow without using a direct service URL. | Demonstration | P2 | SyRS: SYS-43; StRS: SN-1.18, SN-2.01 |

### 5.7 Internal Simulation, Reservoir, and Hot-Swap Requirements

| ID | Software requirement | Acceptance criteria | Verification method | Priority | Trace |
|---|---|---|---|---|---|
| SRS-SIM-001 | The software shall simulate paper strategy orders locally without routing to any brokerage. | Market, limit, stop, stop-limit, equity, option, and multi-leg orders are processed by the simulation engine and create no IB API order calls. | Test | P1 | SyRS: SYS-82; StRS: SN-1.29, SN-1.08, SN-1.24 |
| SRS-SIM-002 | The software shall simulate fills using live market data and configurable fill models. | Market, limit, stop, and stop-limit simulated fills follow SYS-83 defaults and per-strategy configuration; fill volume constraints are enforced. | Test | P1 | SyRS: SYS-83, SYS-87; StRS: SN-1.29, SN-1.03 |
| SRS-SIM-003 | The software shall maintain an independent virtual position ledger for each paper strategy. | Quantity, average cost, unrealized P&L, realized P&L, and commission paid are isolated per paper strategy and independent of IB account positions. | Test | P1 | SyRS: SYS-84; StRS: SN-1.29, SN-1.07 |
| SRS-SIM-004 | The software shall persist paper strategy simulation state. | Virtual positions, pending simulated orders, accumulated metrics, and user state are persisted every 60 seconds by default and restored within 30 seconds of container restart, excluding warm-up. | Fault injection, test | P1 | SyRS: SYS-89; StRS: SN-1.29, SN-2.05 |
| SRS-RESV-001 | The software shall maintain at least 30 concurrent paper strategies in the Strategy Reservoir for the release baseline. | One live strategy and 30 paper strategies run on the reference hardware baseline without violating order latency or dashboard refresh requirements. | System test, performance test | P1 | SyRS: SYS-9, SYS-47, NFR-SC1, NFR-P10; StRS: SN-1.10, SN-1.25 |
| SRS-RESV-002 | The software shall rank Reservoir strategies over a shared configurable evaluation window. | Evaluation windows of 1, 7, 15, 30, 60, and 90 calendar days are supported; default is 30; Sharpe, Sortino, and momentum score are exposed in dashboard and REST API. | Test, demonstration | P1 | SyRS: SYS-48; StRS: SN-1.25, SN-1.30 |
| SRS-RESV-003 | The software shall support manual and configurable automatic Hot-Swap triggers. | Manual promotion, drawdown-triggered demotion, top-ranked promotion, and highest-momentum promotion are configurable; automatic triggers default to disabled; all swap triggers are logged. | Test, demonstration | P1 | SyRS: SYS-49a; StRS: SN-1.25, SN-1.30 |
| SRS-RESV-004 | The software shall execute Hot-Swap demotion before promotion. | Current live strategy stops new signals, cancels resting IB orders, submits liquidation orders, waits for flat confirmation or the configured timeout defaulting to 60 seconds, and transitions to paper only after live positions are flat; on timeout, the swap enters demotion-pending state, dashboard/email/SMS notifications are sent, unfilled liquidation orders are canceled, and promotion is blocked until manual resolution. | Scenario test | P1 | SyRS: SYS-49b, SYS-49c; StRS: SN-1.25 |
| SRS-RESV-005 | The software shall promote a selected paper strategy to live execution only after successful demotion. | The promoted strategy starts live with no open IB positions, preserves prior paper performance history, and uses the same strategy code/API behavior. | Scenario test | P1 | SyRS: SYS-49d, AC-14; StRS: SN-1.25, SN-1.30 |
| SRS-RESV-006 | The software shall enforce Hot-Swap cool-down behavior. | After successful swap, automatic triggers are ignored for the configured cool-down period defaulting to 7 calendar days; manual swap during cool-down requires confirmation warning; the cool-down start time is the timestamp of the most recent successful swap completion. | Test | P1 | SyRS: SYS-49e; StRS: SN-1.25 |

### 5.8 Dashboard, API, Logging, and Notification Requirements

| ID | Software requirement | Acceptance criteria | Verification method | Priority | Trace |
|---|---|---|---|---|---|
| SRS-UI-001 | The software shall provide a web dashboard showing live performance, system health, latency, and benchmark-relative metrics. | Dashboard displays required metrics and refreshes within 5 seconds under release baseline load. | Demonstration, performance test | P1 | SyRS: SYS-36, SYS-37, NFR-P2; StRS: SN-2.01, SN-1.04 |
| SRS-UI-002 | The software shall display active strategy inventory in the dashboard. | Each active strategy shows name, mode, asset class, container status, deployed code version, P&L, and position count. | Demonstration | P1 | SyRS: SYS-41, SYS-79; StRS: SN-2.01, SN-1.10 |
| SRS-UI-003 | The software shall display account-level IB status and Reservoir overview in the dashboard. | Dashboard shows IB equity, daily and cumulative P&L, margin usage, buying power, paper strategy rankings, and momentum scores. | Demonstration | P1 | SyRS: SYS-43b, SYS-48; StRS: SN-2.08, SN-1.25 |
| SRS-UI-004 | The software shall display backtest result history and details in the dashboard. | Backtest history lists strategy, parameters, date range, metrics, and supports drill-down into trade log, equity curve, and benchmark comparison. | Demonstration | P1 | SyRS: SYS-42, SYS-43a; StRS: SN-1.02, SN-1.13 |
| SRS-LOG-001 | The software shall separate persistent system logs from user strategy logs. | System events and user strategy logs are stored with timestamp, severity, source, event type, message, and correlation ID; system logs include order routing outcomes, ingestion job lifecycle, container lifecycle, IB Gateway connection state changes, kill-switch activations, Hot-Swap events, resource threshold alerts, and market data subscription changes; both log classes are viewable from the dashboard. | Test, inspection | P1 | SyRS: SYS-38, SYS-61; StRS: SN-2.02, C-6 |
| SRS-API-001 | The software shall expose dashboard, CLI, and REST/WebSocket interfaces for operator workflows. | Live designation, strategy management, kill switch, Hot-Swap, Reservoir ranking, backtests, system status, and logs are available through documented API paths or CLI commands. | Contract test, demonstration | P1 | SyRS: IF-8, IF-9, SYS-2c, SYS-44a, SYS-48, SYS-49a; StRS: SN-1.11, SN-1.25 |
| SRS-ERR-001 | The software shall return structured errors for failed order submissions. | Errors include type, human-readable message, original order parameters, and one of the SyRS-defined error categories when applicable. | Contract test | P1 | SyRS: SYS-64; StRS: SN-1.08, SN-1.22, SN-1.29 |
| SRS-NOTIF-001 | The software shall notify the operator through email and SMS for IB connectivity loss and critical failures. | Notification dispatch begins within 60 seconds of detection and delivery status is stored as a notification event. | Fault injection, integration test | P1 | SyRS: SYS-46, NFR-P6; StRS: SN-1.12, SN-2.04 |

### 5.9 Safety, Reliability, and Security Requirements

| ID | Software requirement | Acceptance criteria | Verification method | Priority | Trace |
|---|---|---|---|---|---|
| SRS-SAFE-001 | The software shall provide a kill switch from dashboard, CLI, and REST API that follows the QuantConnect Liquidate sequence: cancel all resting IB orders for the live strategy, submit market liquidation orders to close every open live-strategy position in the opposite direction of the held quantity, halt all paper simulation engines, and disconnect from IB Gateway. | Measured under reference baseline (NFR-SC1): cancellation of all resting IB orders and submission of market liquidation orders for every open live-strategy position completes within 5 seconds (NFR-P3); paper simulation engines transition to the HALTED state with no further `on_fill` callbacks emitted; HALTED-state transition is observable through SRS-LOG-001 within 1 second of activation; IB Gateway is disconnected after liquidation orders are submitted. | Scenario test, performance test | P1 | SyRS: SYS-44a, NFR-P3, NFR-SC1; StRS: SN-1.11 |
| SRS-SAFE-002 | The software shall handle unfilled kill-switch liquidation orders according to the SyRS timeout behavior. | If a liquidation order remains unfilled after 30 seconds, details are logged, email and SMS are sent, the unfilled liquidation order is canceled, and IB is disconnected. | Scenario test | P1 | SyRS: SYS-44b; StRS: SN-1.11 |
| SRS-SAFE-003 | The software shall block live order submission when IB Gateway is unreachable. | During IB unreachable state, order submissions fail with `CONNECTIVITY_BLOCKED` until reconnection and readiness checks pass. | Fault injection | P1 | SyRS: SYS-45, SYS-64, NFR-R2; StRS: SN-2.04 |
| SRS-REL-001 | The software shall support the SyRS market-hours availability objective. | Health, restart, readiness, logging, and recovery mechanisms produce evidence for measuring at least 99.9 percent availability during US equity market hours, 09:30-16:00 ET Monday-Friday excluding market holidays, over a rolling 30-day period, with planned maintenance and scheduled IB Gateway restart excluded per SyRS NFR-R1. | Analysis, system test | P1 | SyRS: NFR-R1, NFR-R6, SYS-76; StRS: SN-2.05, BG-6 |
| SRS-REL-002 | The software shall restore a full system restart to trade-ready state within the SyRS recovery target. | Under reference deployment conditions, Proxmox VM, Docker daemon, ATP services, and readiness checks complete within 10 minutes. | System test | P1 | SyRS: NFR-R6, SYS-76; StRS: SN-2.05 |
| SRS-SEC-001 | The software shall encrypt brokerage and notification credentials at rest and prevent plaintext credential logging. | Credential files or records are encrypted; log redaction tests prove IB, SMTP, and SMS secrets are not emitted in plaintext. | Test, inspection | P1 | SyRS: NFR-S1, NFR-S4; StRS: C-3, SN-1.12 |
| SRS-SEC-002 | By default, the dashboard/API service shall bind only to RFC 1918 or loopback addresses. Binding to publicly routable interfaces shall require explicit operator configuration and documented external authentication. | Default Docker Compose port mappings expose dashboard/API only on loopback or RFC 1918 interfaces; an external-host connect test against a non-RFC 1918 interface fails with the default configuration; documentation states that external exposure requires operator-managed authentication. | Inspection, test | P1 | SyRS: NFR-S3; StRS: SN-2.01 |
| SRS-SEC-003 | The software shall run strategy containers with least-privilege permissions. | Strategy containers run without privileged mode, without host network access, and without access to other strategy filesystems. | Inspection, security test | P1 | SyRS: NFR-S5; StRS: SN-1.10, SN-3.01 |
| SRS-SEC-004 | The software shall isolate Jupyter from live trading credentials and execution APIs. | Jupyter has read-only access to market data and backtest results and cannot submit live orders or read brokerage credentials. | Security test | P1 | SyRS: NFR-S6, SYS-34c; StRS: SN-1.18 |

### 5.10 Performance Measurement Requirements

| ID | Software requirement | Acceptance criteria | Verification method | Priority | Trace |
|---|---|---|---|---|---|
| SRS-PERF-001 | The software shall measure latency-sensitive performance metrics against a Precision Time Protocol (PTP)–disciplined system clock with documented offset bounds and shall report p50, p95, p99, and p99.9 percentiles in verification artifacts. | Verification reports for NFR-P1, NFR-P4, NFR-P5, NFR-P6, NFR-P9, NFR-P10, and SRS-MD-001 fan-out latency include p50, p95, p99, and p99.9 percentiles measured against a PTP-disciplined host clock; the maximum observed clock offset for the measurement window is documented in the verification artifact; measurement boundaries match the SyRS measurement conditions for each NFR. | Test, inspection | P1 | SyRS: NFR-P1, NFR-P4, NFR-P5, NFR-P6, NFR-P9, NFR-P10; StRS: SN-1.01, SN-2.03 |

## 6. User Interface Requirements

| ID | Requirement | Acceptance criteria | Trace |
|---|---|---|---|
| UI-1 | The dashboard shall provide a primary operations view for live strategy, account health, Reservoir ranking, system health, and critical alerts. | User can inspect live strategy status, IB account equity, buying power, margin, Reservoir rankings, heartbeat state, and active critical alerts without SSH access. | SRS-UI-001, SRS-UI-002, SRS-UI-003 |
| UI-2 | The dashboard shall provide a strategy management view. | User can view active strategies, deployed code version, mode, asset class, container status, and key metrics; live designation requires explicit confirmation. | SRS-ORCH-004, SRS-EXE-001 |
| UI-3 | The dashboard shall provide backtest controls and result history. | User can initiate backtests with strategy, date range, parameter overrides, and cost model configuration; user can inspect completed backtest details. | SRS-BT-001, SRS-UI-004 |
| UI-4 | The dashboard shall provide a kill-switch control with confirmation and status feedback. | User can activate kill switch and see cancellation, liquidation submission, timeout, notification, and disconnect status. | SRS-SAFE-001, SRS-SAFE-002 |
| UI-5 | The dashboard shall provide Hot-Swap controls and status. | User can trigger manual promotion, inspect demotion-pending state, view cool-down expiry, and see automatic-trigger configuration. | SRS-RESV-003 through SRS-RESV-006 |
| UI-6 | The dashboard shall provide embedded Jupyter navigation. | User can access Jupyter from the dashboard workflow without direct service URL navigation. | SRS-RES-001, SRS-RES-003 |

## 7. API and Interface Requirements

The concrete OpenAPI, WebSocket event schema, CLI command reference, and Protobuf
schemas shall be derived from this section during software design.

| ID | Interface | Required capability | Auth/access | Trace |
|---|---|---|---|---|
| API-1 | Python Strategy API | Scheduling, subscriptions, historical data, order submission, cancellation, callbacks, indicators, warm-up, logging, state access, and author documentation | Strategy container identity | SRS-SDK-001 through SRS-SDK-009 |
| API-2 | REST API | Strategy lifecycle, live designation, kill switch, Hot-Swap, backtest launch, backtest query, Reservoir ranking, watchlist configuration, system status, logs, alerts | Local single-user deployment controls | SRS-API-001 |
| API-3 | WebSocket API | Live dashboard updates for P&L, metrics, account status, heartbeat, logs, alerts, Reservoir ranking, and strategy state | Local single-user deployment controls | SRS-UI-001 through SRS-UI-004 |
| API-4 | CLI | Kill switch, strategy management, live designation, Hot-Swap, readiness status, and basic administration | Local shell access | SRS-API-001, SRS-SAFE-001 |
| API-5 | Brokerage adapter interface | Order submission, cancellation, account status, positions, market data, historical data, and versioned adapter capability discovery | Internal service boundary | SRS-EXE-006, SRS-EXE-007 |
| API-6 | Data provider interface | Bulk equity download, historical backfill, incremental update, fundamentals ingestion, options import, and user Parquet import | Internal service boundary | SRS-DATA-001 through SRS-DATA-007 |
| API-7 | Unified historical data interface | Query by symbol, date range, resolution, asset class, normalization mode, and data source-neutral result shape | Strategy, backtest, factor, and research consumers | SRS-DATA-007, SRS-DATA-012 |

## 8. Error Handling Requirements

| ID | Error condition | Expected behavior | Trace |
|---|---|---|---|
| ERR-1 | Order submitted by a non-live strategy to live execution path | Reject synchronously with structured error and no IB order side effect. | SRS-EXE-001, SRS-ERR-001 |
| ERR-2 | IB Gateway unreachable | Reject live order submission with `CONNECTIVITY_BLOCKED`, log event, alert dashboard, and attempt reconnection. | SRS-SAFE-003, SRS-MD-005 |
| ERR-3 | Market data stale | Reject live and paper order submissions with `MARKET_DATA_STALE` until fresh data arrives. | SRS-MD-004 |
| ERR-4 | Market-data line limit exceeded | Reject subscription with `SUBSCRIPTION_LIMIT_REACHED` and alert operator. | SRS-MD-002 |
| ERR-5 | Ingestion record validation failure | Quarantine invalid record, omit it from primary storage, and alert with reason counts. | SRS-DATA-013 |
| ERR-6 | IB pacing budget exceeded by configured ingestion job | Refuse to start affected job and alert operator at scheduling time. | SRS-DATA-002, SRS-DATA-004 |
| ERR-7 | Hot-Swap demotion liquidation timeout | Enter demotion-pending state, notify operator, cancel unfilled order, and block promotion. | SRS-RESV-004 |
| ERR-8 | Kill-switch liquidation timeout | Log unfilled order details, notify by email and SMS, cancel unfilled liquidation order, and disconnect from IB. | SRS-SAFE-002 |
| ERR-9 | Missing or invalid startup configuration | Hold system in pre-trade state and expose readiness failure through logs, dashboard, and API. | SRS-ARCH-005, SRS-MD-006 |

## 9. Verification and Test Strategy

| Level | Required verification | Primary targets |
|---|---|---|
| Unit tests | Validate pure functions, state transitions, model validation, cost models, corporate action transforms, calendar calculations, metrics, normalization, and storage policies. | SRS-DATA-011 through SRS-DATA-021, SRS-BT-002 through SRS-BT-010, SRS-SIM-002 |
| Contract tests | Verify Strategy API parity, REST/WebSocket contracts, adapter interfaces, structured error schema, and data provider contracts. | SRS-SDK-001, SRS-ERR-001, SRS-API-001, SRS-ARCH-003 |
| Integration tests | Verify IB paper Gateway adapter, Databento ingestion, Sharadar ingestion, Parquet import, SSD/NAS sync, Jupyter data access, and notification services. | SRS-EXE-006, SRS-EXE-007, SRS-DATA-001 through SRS-DATA-007, SRS-RES-002, SRS-NOTIF-001 |
| System tests | Verify end-to-end workflows: live order path, paper strategy execution, backtest launch, dashboard updates, kill switch, Hot-Swap, Reservoir ranking, and startup readiness. | SRS-EXE-001, SRS-RESV-001 through SRS-RESV-006, SRS-SAFE-001, SRS-MD-006 |
| Performance tests | Measure p95 live order acknowledgement latency, paper callback latency, dashboard refresh latency, market-data fan-out latency, factor completion, ingestion windows, and restart-to-ready time. | NFR-P1 through NFR-P10, NFR-R6, SRS-FAC-001 |
| Fault-injection tests | Exercise IB disconnect, stale data, strategy crash, service restart, outbox replay or duplicate command submission, NAS unavailable degraded mode, and kill-switch timeout. | SRS-MD-003 through SRS-MD-006, SRS-EXE-005, SRS-SAFE-001 through SRS-SAFE-003 |
| Architecture tests | Enforce Rust core runtime, Python strategy boundary, adapter isolation, dependency direction, container isolation, and no vendor imports in core modules. | SRS-ARCH-001 through SRS-ARCH-004, SRS-SEC-003 |
| Documentation review | Confirm every requirement is necessary, singular, unambiguous, feasible, verifiable, and traced to StRS/SyRS. | Entire SRS |

### 9.1 Major Acceptance Scenarios

| Scenario | Pass condition | Related requirements |
|---|---|---|
| Live IB order path | A Python strategy submits a supported order, ATP routes it only from the live strategy to IB Gateway, and acknowledgement callback arrives within NFR-P1. | SRS-EXE-001, SRS-EXE-003, SRS-SDK-004 |
| Backtest reproducibility | Two identical backtest runs produce identical trade logs, equity curves, and metrics. | SRS-BT-001, SRS-BT-010 |
| Data ingestion | Databento daily data for 8,000+ equities ingests, validates, stores on SSD, syncs to NAS, and is queryable the next morning. | SRS-DATA-001, SRS-DATA-008 |
| Dashboard refresh | Dashboard shows live performance, heartbeat, account, logs, and Reservoir data with no more than 5 seconds refresh latency under release baseline load. | SRS-UI-001 through SRS-UI-003 |
| Kill switch | Activation cancels IB orders, submits liquidation orders within 5 seconds, halts paper simulation fills, and disconnects from IB. | SRS-SAFE-001 |
| Reservoir ranking | Thirty paper strategies maintain independent ledgers and produce ranked Sharpe, Sortino, and momentum scores over a configured evaluation window. | SRS-RESV-001, SRS-RESV-002 |
| Hot-Swap | Current live strategy is liquidated and demoted before selected paper strategy is promoted live with flat start and cool-down applied. | SRS-RESV-003 through SRS-RESV-006 |
| Paper simulation | Paper orders receive simulated fills, commissions, callbacks, P&L, and metrics without IB order side effects. | SRS-SIM-001 through SRS-SIM-004 |
| Stale-data blocking | Live and paper order submissions are rejected while required market data is stale and resume after fresh data returns. | SRS-MD-003, SRS-MD-004 |
| Market-data fan-out | Multiple strategies subscribing to the same security share one IB subscription and receive data with no more than 100 ms added fan-out latency. | SRS-MD-001 |

## 10. Traceability Matrix

### 10.1 SRS to SyRS/StRS Traceability

The trace links in Section 5 are binding. This matrix summarizes the primary
flowdown by SRS group.

| SRS group | Primary SyRS coverage | Primary StRS coverage |
|---|---|---|
| SRS-ARCH | AC-1 through AC-16, SYS-52 through SYS-56, SYS-70, SYS-76, NFR-S1, NFR-S4, NFR-SC3 | C-1 through C-12, SN-3.01 through SN-3.04, BG-5, BG-6 |
| SRS-ORCH and SRS-SDK | SYS-6 through SYS-13, SYS-35, SYS-50, SYS-51, SYS-57, SYS-58, SYS-79, SYS-80, NFR-P9, NFR-U2, NFR-S5, NFR-R5 | SN-1.01, SN-1.09, SN-1.10, SN-1.19, SN-1.20, SN-1.21, SN-1.23, SN-2.03, SN-2.05 |
| SRS-EXE and SRS-MD | SYS-1 through SYS-7, SYS-39, SYS-39a, SYS-44a, SYS-44b, SYS-45, SYS-46, SYS-65, SYS-70, SYS-75, SYS-76, SYS-90, NFR-P1, NFR-P4, NFR-P5, NFR-R2 | SN-1.01, SN-1.06, SN-1.07, SN-1.08, SN-1.22, SN-1.24, SN-2.03, SN-2.04 |
| SRS-DATA and SRS-FAC | SYS-22a through SYS-33, SYS-53, SYS-55, SYS-56, SYS-59, SYS-60, SYS-63, SYS-66 through SYS-69, SYS-77, SYS-78, SYS-88, NFR-P7, NFR-P8a through NFR-P8d, NFR-R4, NFR-SC2 | SN-1.14, SN-1.15, SN-1.26, SN-1.27, SN-1.28, SN-1.29, SN-2.06, SN-3.03, C-4, C-5, C-7, C-8, C-10 |
| SRS-BT and SRS-RES | SYS-14 through SYS-21, SYS-34a through SYS-35, SYS-43, SYS-43a, SYS-62, NFR-S6 | SN-1.02, SN-1.03, SN-1.04, SN-1.05, SN-1.13, SN-1.16, SN-1.17, SN-1.18, SN-1.20 |
| SRS-SIM and SRS-RESV | SYS-2b, SYS-47 through SYS-49e, SYS-82 through SYS-89, NFR-SC1 | SN-1.25, SN-1.29, SN-1.30 |
| SRS-UI, SRS-LOG, SRS-API, SRS-NOTIF | SYS-36 through SYS-43b, SYS-46, SYS-48, SYS-49a, SYS-61, IF-6, IF-8, IF-9, NFR-P2, NFR-P6, NFR-U1 | SN-1.11, SN-1.12, SN-2.01, SN-2.02, SN-2.08, BG-4 |
| SRS-SAFE, SRS-REL, SRS-SEC | SYS-44a, SYS-44b, SYS-45, SYS-46, SYS-64, SYS-76, NFR-R1 through NFR-R6, NFR-S1 through NFR-S6 | SN-1.11, SN-1.12, SN-2.04, SN-2.05, C-3, C-6 |

### 10.2 Reverse SyRS Coverage Check

Every SyRS functional requirement, non-functional requirement, and architectural
constraint has at least one SRS coverage target.

| SyRS ID | SRS coverage |
|---|---|
| SYS-1 | SRS-EXE-001 |
| SYS-2a | SRS-EXE-001 |
| SYS-2b | SRS-EXE-002, SRS-SIM-001 |
| SYS-2c | SRS-EXE-001, SRS-API-001 |
| SYS-2d | SRS-EXE-001 |
| SYS-2e | SRS-EXE-002 |
| SYS-3 | SRS-EXE-003, SRS-SIM-001, SRS-EXE-008 |
| SYS-4 | SRS-EXE-004, SRS-SIM-001 |
| SYS-5 | SRS-SDK-003 |
| SYS-6 | SRS-SDK-002 |
| SYS-7 | SRS-SDK-004, SRS-EXE-008 |
| SYS-8 | SRS-SDK-005 |
| SYS-9 | SRS-RESV-001 |
| SYS-10 | SRS-ORCH-001, SRS-ARCH-004 |
| SYS-11 | SRS-ORCH-002 |
| SYS-12 | SRS-SDK-001 |
| SYS-13 | SRS-ORCH-001 |
| SYS-14 | SRS-BT-001 |
| SYS-15a | SRS-BT-002 |
| SYS-15b | SRS-BT-002 |
| SYS-15c | SRS-BT-002 |
| SYS-15d | SRS-BT-002 |
| SYS-15e | SRS-BT-003 |
| SYS-15 | Parent reference covered by SYS-15a through SYS-15e via SRS-BT-002 and SRS-BT-003. |
| SYS-16 | SRS-BT-004 |
| SYS-17 | SRS-BT-005 |
| SYS-18 | SRS-BT-006 |
| SYS-19 | SRS-BT-007 |
| SYS-20 | SRS-BT-008 |
| SYS-21 | SRS-BT-009, SRS-ORCH-004 |
| SYS-22a | SRS-DATA-001 |
| SYS-22b | SRS-DATA-002 |
| SYS-22c | SRS-DATA-003 |
| SYS-23 | SRS-DATA-004 |
| SYS-24 | SRS-DATA-004, SRS-DATA-008 |
| SYS-25 | SRS-DATA-006 |
| SYS-26 | SRS-DATA-005 |
| SYS-27 | SRS-DATA-007 |
| SYS-28a | SRS-DATA-011 |
| SYS-28b | SRS-DATA-019 |
| SYS-28c | SRS-DATA-020 |
| SYS-29 | SRS-DATA-012 |
| SYS-30a | SRS-SDK-007 |
| SYS-30b | SRS-SDK-008 |
| SYS-31 | SRS-DATA-001, SRS-DATA-002, SRS-DATA-004 |
| SYS-32 | SRS-FAC-001 |
| SYS-33 | SRS-FAC-001 |
| SYS-34a | SRS-RES-001 |
| SYS-34b | SRS-RES-002 |
| SYS-34c | SRS-RES-001, SRS-SEC-004 |
| SYS-35 | SRS-SDK-006, SRS-RES-002 |
| SYS-36 | SRS-UI-001, SRS-BT-005 |
| SYS-37 | SRS-UI-001, SRS-BT-005 |
| SYS-38 | SRS-LOG-001 |
| SYS-39 | SRS-MD-003, SRS-MD-007 |
| SYS-39a | SRS-MD-004, SRS-MD-007 |
| SYS-40 | SRS-EXE-004 |
| SYS-41 | SRS-UI-002, SRS-ORCH-004 |
| SYS-42 | SRS-UI-004 |
| SYS-43 | SRS-RES-003 |
| SYS-43a | SRS-BT-001, SRS-UI-004 |
| SYS-43b | SRS-UI-003 |
| SYS-44a | SRS-SAFE-001, SRS-API-001 |
| SYS-44b | SRS-SAFE-002 |
| SYS-45 | SRS-SAFE-003, SRS-MD-005 |
| SYS-46 | SRS-NOTIF-001, SRS-MD-005 |
| SYS-47 | SRS-RESV-001 |
| SYS-48 | SRS-RESV-002, SRS-UI-003, SRS-API-001 |
| SYS-49a | SRS-RESV-003, SRS-API-001 |
| SYS-49b | SRS-RESV-004 |
| SYS-49c | SRS-RESV-004 |
| SYS-49d | SRS-RESV-005 |
| SYS-49e | SRS-RESV-006 |
| SYS-50 | SRS-SDK-002 |
| SYS-51 | SRS-SDK-002, SRS-FAC-001 |
| SYS-52 | SRS-ARCH-003, SRS-EXE-006 |
| SYS-53 | SRS-ARCH-003, SRS-DATA-007 |
| SYS-54 | SRS-ARCH-003 |
| SYS-55 | SRS-ARCH-005, SRS-DATA-002, SRS-DATA-004 |
| SYS-56 | SRS-ARCH-003, SRS-DATA-003 |
| SYS-57 | SRS-ORCH-002, SRS-ORCH-003 |
| SYS-58 | SRS-ORCH-003 |
| SYS-59 | SRS-DATA-018 |
| SYS-60 | SRS-DATA-018 |
| SYS-61 | SRS-LOG-001 |
| SYS-62 | SRS-BT-010 |
| SYS-63 | SRS-DATA-017 |
| SYS-64 | SRS-ERR-001, SRS-SDK-003, SRS-MD-002, SRS-MD-004, SRS-SAFE-003, SRS-EXE-008 |
| SYS-65 | SRS-EXE-007 |
| SYS-66 | SRS-DATA-015 |
| SYS-67 | SRS-DATA-008 |
| SYS-68 | SRS-DATA-009 |
| SYS-69 | SRS-DATA-010 |
| SYS-70 | SRS-MD-001, SRS-MD-002, SRS-MD-007 |
| SYS-75 | SRS-MD-005 |
| SYS-76 | SRS-MD-006, SRS-REL-001, SRS-REL-002 |
| SYS-77 | SRS-DATA-013 |
| SYS-78 | SRS-DATA-014 |
| SYS-79 | SRS-ORCH-004, SRS-BT-009 |
| SYS-80 | SRS-ORCH-005 |
| SYS-82 | SRS-SIM-001, SRS-EXE-003, SRS-EXE-004 |
| SYS-83 | SRS-SIM-002, SRS-BT-003 |
| SYS-84 | SRS-SIM-003 |
| SYS-85 | SRS-SDK-004 |
| SYS-86 | SRS-BT-004 |
| SYS-87 | SRS-SIM-002, SRS-MD-004 |
| SYS-88 | SRS-DATA-021 |
| SYS-89 | SRS-SIM-004 |
| SYS-90 | SRS-EXE-005, SRS-EXE-008, SRS-EXE-009 |
| NFR-P1 | SRS-EXE-001, SRS-PERF-001, major acceptance scenario: Live IB order path |
| NFR-P2 | SRS-UI-001 |
| NFR-P3 | SRS-SAFE-001 |
| NFR-P4 | SRS-SDK-004, SRS-PERF-001 |
| NFR-P5 | SRS-MD-003, SRS-PERF-001 |
| NFR-P6 | SRS-NOTIF-001, SRS-PERF-001 |
| NFR-P7 | SRS-FAC-001 |
| NFR-P8a | SRS-DATA-001 |
| NFR-P8b | SRS-DATA-002 |
| NFR-P8c | SRS-DATA-004 |
| NFR-P8d | SRS-DATA-005 |
| NFR-P9 | SRS-ORCH-001, SRS-PERF-001, performance tests |
| NFR-P10 | SRS-RESV-001, SRS-PERF-001 |
| NFR-S1 | SRS-SEC-001 |
| NFR-S2 | SRS-EXE-001, SRS-ORCH-005 |
| NFR-S3 | SRS-SEC-002 |
| NFR-S4 | SRS-SEC-001 |
| NFR-S5 | SRS-ORCH-001, SRS-SEC-003 |
| NFR-S6 | SRS-RES-002, SRS-SEC-004 |
| NFR-R1 | SRS-REL-001 |
| NFR-R2 | SRS-MD-005, SRS-SAFE-003 |
| NFR-R3 | SRS-SDK-005, SRS-EXE-005, SRS-EXE-008, SRS-EXE-009, SRS-SIM-004 |
| NFR-R4 | SRS-DATA-016, SRS-EXE-009 |
| NFR-R5 | SRS-ORCH-001 |
| NFR-R6 | SRS-MD-006, SRS-REL-002 |
| NFR-SC1 | SRS-RESV-001, SRS-ORCH-002 |
| NFR-SC2 | SRS-DATA-008 |
| NFR-SC3 | SRS-ARCH-004 |
| NFR-U1 | SRS-UI-001 through SRS-API-001 |
| NFR-U2 | SRS-SDK-009 |
| AC-1 | SRS-ARCH-001, SRS-SDK-001 |
| AC-2 | SRS-EXE-006 |
| AC-3 | SRS-SEC-002 |
| AC-4 | SRS-BT-001 |
| AC-5 | SRS-DATA-008 |
| AC-6 | SRS-SDK-006 |
| AC-7 | SRS-ARCH-002, SRS-ARCH-003 |
| AC-8 | SRS-ARCH-002, SRS-ARCH-003 |
| AC-9 | SRS-SAFE-001 through SRS-SAFE-003 |
| AC-10 | SRS-EXE-002, SRS-SIM-001 |
| AC-11 | SRS-ARCH-002 |
| AC-12 | SRS-ORCH-001, SRS-ARCH-004 |
| AC-13 | SRS-ARCH-004 |
| AC-14 | SRS-SDK-001, SRS-RESV-005 |
| AC-15 | SRS-EXE-001 |
| AC-16 | SRS-ARCH-001 |

### 10.3 StRS/SyRS Concurrency Deviation

StRS SN-1.10 expresses a stakeholder desire for one live strategy plus up to
59 paper strategies. SyRS v0.7 establishes the release verification baseline as
one live strategy plus at least 30 paper strategies in SYS-9, SYS-47, and
NFR-SC1. This SRS follows the SyRS release baseline. Expansion above 30 paper
strategies remains an intended capability only after target-platform performance
validation.

### 10.4 Cloud Deployment Scope

StRS SN-2.07 identifies cloud deployment as a target state. SyRS AC-13 makes the
Proxmox-hosted Ubuntu VM the binding Phase 1 deployment target and states that
cloud VPS deployment remains secondary. This SRS treats cloud VPS deployment as a
future target, not a release-baseline software requirement. SRS-ARCH-004 requires
the Phase 1 Docker Compose deployment to document portability constraints so that
future cloud deployment is not precluded.

## 11. Requirement Quality Checklist

Reviewers shall evaluate each SRS requirement against the following checklist:

| Quality attribute | Review question |
|---|---|
| Necessary | Does the requirement trace to an approved stakeholder need, system requirement, constraint, or derived safety need? |
| Singular | Does the requirement state one obligation instead of multiple independent obligations? |
| Unambiguous | Would two implementers interpret the requirement the same way? |
| Complete | Are acceptance criteria, priority, verification method, and trace links present? |
| Consistent | Does the requirement avoid conflict with StRS, SyRS, and other SRS requirements? |
| Feasible | Can the requirement be implemented within the stated architecture and deployment constraints? |
| Verifiable | Can the requirement be verified by inspection, analysis, test, or demonstration? |
| Traceable | Does the requirement map forward to tests and backward to SyRS/StRS? |
| Modifiable | Can the requirement be changed without rewriting unrelated requirements? |

## 12. Dependencies

Concrete package versions shall be pinned during implementation. This SRS
identifies required dependency categories only.

| Dependency category | Required use | Trace |
|---|---|---|
| Rust toolchain | Core ATP runtime services | AC-16, C-12 |
| Python 3.12 or later | User-authored strategies and Strategy SDK | AC-1, C-1 |
| Docker / Docker Compose | Strategy isolation and Phase 1 deployment | AC-12, AC-13 |
| IB TWS API / IB Gateway | Live trading, live market data, minute watchlist ingestion, and option-chain capture | AC-2, C-2, C-7 |
| Databento client and file parsers | Bulk equity data and historical options data | C-8, C-10 |
| Sharadar data access | Fundamental data ingestion | SYS-26 |
| Apache Parquet / Arrow tooling | User uploads, historical storage, analytics, and research access | C-4, SYS-25, SYS-27 |
| pandas-ta and TA-Lib | Technical indicator library | C-9, AC-6 |
| Trading calendar library or maintained calendar data | US exchange holidays, early closes, time zones, and sessions | SYS-50, SYS-51 |
| Email and SMS provider clients | Notification dispatch | SYS-46, NFR-S4 |
| JupyterLab or Jupyter Notebook | Embedded research environment | SYS-34a through SYS-34c |
| GitHub Actions | CI, architecture checks, and requirements trace checks | SyRS Section 10 closure record |

### 12.1 Storage Growth Estimates

The following estimates satisfy NFR-SC2 at SRS level. They are planning
estimates for capacity verification and shall be recalibrated during
implementation using observed provider schemas, compression ratios, and option
chain capture counts.

| Data class | Release-baseline driver | Planning estimate |
|---|---|---|
| Daily US equity bars | 8,000+ securities, about 252 trading days/year | Less than 1 GB/year compressed; about 20 GB reserve for 20-year history, indexes, and metadata |
| Initial minute-bar backfill | 8,000+ securities, about 1 year, about 390 regular-session bars/day | 50-150 GB compressed depending on schema and index layout |
| Ongoing IB minute watchlist | Configurable active-strategy watchlist; estimate per 100 symbols | 1-3 GB/year compressed per 100 symbols |
| Option-chain snapshots | One near-close snapshot per trading day; size depends on contracts captured | Estimate as `contracts_per_day * trading_days * compressed_bytes_per_contract`; reserve 25-100 GB/year until capture sampling refines the value |
| Sharadar fundamentals | US equity statements and ratios | Less than 10 GB/year including normalized tables and metadata |
| Backtest results, logs, and derived factors | Strategy count, retention policy, and run frequency dependent | Operator-configured retention; default capacity planning reserve is 100 GB on SSD/NAS for Phase 1 |
| SSD hot tier | 90 days of hot data plus cold-read cache capped at 20 percent SSD capacity | Must fit within 1 TB SSD with eviction policy in SRS-DATA-010 |
| NAS archival tier | Indefinite retention for ingested market and fundamental data | 20 TB NAS provides multi-year headroom under the release-baseline estimates; option snapshot growth shall be reviewed after the first month of capture |

## 13. Assumptions and Review Notes

- Only `docs/SRS.md` is updated by this SRS completion step.
- `feature_list.json` is not created by this step.
- No source code, tests, or deployment files are implemented by this step.
- The SRS is intentionally software-level. Detailed schemas, protocol files,
  OpenAPI documents, and UI component specifications shall be derived later.
- Where SyRS imposes a design constraint, this SRS preserves it even if another
  implementation might be possible.

### 13.1 TBD Register

There are no open TBDs at the v0.2 baseline. All defaults stated in Section 5
are binding for v0.2 verification. Any future TBD items shall be tracked in the
following table with an owner and target resolution date.

| TBD ID | Description | Owner | Target date | Status |
|---|---|---|---|---|
| _none_ | _No open TBDs at v0.2 baseline._ | — | — | — |

## 14. Change Log

| Version | Date | Author | Summary |
|---|---|---|---|
| 0.1 | Undated | Initial draft | Placeholder template created. |
| 0.2 draft | 2026-04-29 | ChatGPT / Codex | Replaced template with review-ready SRS derived from StRS v0.7 and SyRS v0.7, including software modules, requirements, acceptance criteria, verification methods, traceability, reverse coverage, verification strategy, dependencies, and requirement quality checklist. |
| 0.2 review patch | 2026-04-30 | Codex | Addressed SRS review findings: completed partial SyRS flowdown, split mixed-priority requirements, added Strategy API documentation requirement, added storage growth estimates, clarified cloud deployment as a future target, and kept platform pre-trade risk controls out of scope. |
| 0.3 | 2026-05-02 | Claude (review-response patch) | Incorporated stakeholder responses to v0.2 review findings. Clarified SRS-SDK-001 paper-mode terminology (live IB execution vs. internal paper simulation with live market data, QuantConnect-style fictional capital). Expanded SRS-EXE-005 to cover the NFR-R3 user-accessible state dictionary, account equity snapshot, position state, and warm-up re-execution within the 60-second recovery target. Added SRS-EXE-008 (order lifecycle state machine with documented states, transitions, and client correlation ID idempotency). Added SRS-EXE-009 (durable outbox commit before IB submission with restart reconciliation against acknowledged broker IDs). Added SRS-MD-007 (market-data sequence gap detection with stale-state propagation). Added SRS-PERF-001 (PTP-disciplined clock and p50/p95/p99/p99.9 reporting for latency NFRs). Rewrote SRS-SEC-002 to bind dashboard/API to RFC 1918 / loopback addresses by default. Updated SRS-SAFE-001 to align with the QuantConnect Liquidate sequence and added HALTED-state observability through SRS-LOG-001 within 1 second under NFR-SC1. Updated SRS-DATA-003 backfill targets to concrete floors (≥ 15 years daily, ≥ 6 months minute) with deviation-note fallback. Added a future-scope item for credential rotation without service restart. Added §13.1 TBD Register noting no open TBDs at v0.2 baseline. Updated reverse SyRS coverage in §10.2 to include the new SRS IDs. |
