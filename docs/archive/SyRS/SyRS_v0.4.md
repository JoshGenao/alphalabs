# System Requirements Specification (SyRS)

**Document ID:** SyRS-001  
**Version:** 0.4 (draft)  
**Status:** In progress  
**Last updated:** 2026-03-20  
**Traces from:** StRS-001

---

## 1. Purpose

This document defines what the **system** must do — the interfaces,
constraints, and non-functional requirements that bound the solution space.
It answers: **what must be true of the system as a whole?**

It is prepared in accordance with ISO/IEC/IEEE 29148:2018, the INCOSE
Guide for Writing Requirements, and the NASA Systems Engineering
Handbook (NASA/SP-2016-6105 Rev2). Every requirement herein is
traceable to one or more stakeholder needs or business goals in StRS-001.

Agents reading this document should use it to understand architectural
boundaries and to evaluate whether a proposed implementation is in scope.

---

## 2. System overview

The Algorithmic Trading Platform (ATP) is a single-user, software-intensive
system that enables a personal/retail trader to research, backtest, and
deploy Python-based algorithmic trading strategies against live US equity
and options markets through Interactive Brokers. The system is operated
and maintained by a single individual (no dedicated DevOps support) and
runs on Linux (Ubuntu) hosted on an on-premise Proxmox virtualization
server. It is designed for swing- and position-trading strategies (not
high-frequency trading) and targets an architecture that accommodates
future expansion to additional asset classes (crypto, futures),
brokerages, and data providers without major rearchitecture. The
reference hardware is a Proxmox host with an Intel i5-12400 (6C/12T),
32 GB RAM, and 1 TB SSD.

The system comprises eight major functional areas: (1) a Strategy Engine
that hosts and executes user-authored Python strategies; (2) an Execution
Engine that routes orders to Interactive Brokers; (3) a Data Layer that
ingests, stores, and serves historical and live market data (including
fundamental data from Sharadar) using tiered storage (local SSD primary,
NAS archival); (4) a Backtesting Engine that simulates strategy execution
against historical data; (5) a Research Environment (Jupyter) integrated
within the dashboard; (6) a Factor Pipeline for universe-wide screening
and ranking; (7) a Strategy Orchestrator that manages strategy lifecycle
using container-per-strategy isolation; and (8) a Dashboard and
Notification subsystem for observability and alerting.

---

## 3. System context

The following diagram shows the system boundary, external actors, and
primary data flows.

```
                          ┌──────────────────────────────────────────────────┐
                          │            System Boundary (ATP)                 │
                          │         Proxmox VM / Docker Compose              │
                          │                                                  │
  ┌───────────┐           │  ┌──────────────────────────────────────────┐   │
  │  Trader   │◄──────────┼──┤  Web Dashboard                          │   │
  │  (Josh)   │──────────►┼──┤  ├─ Performance & health monitoring     │   │
  └───────────┘  browser, │  │  ├─ Backtest result history              │   │
                 CLI,     │  │  ├─ Active strategy listing              │   │
                 API      │  │  ├─ Strategy logs / System logs          │   │
                          │  │  └─ Integrated Jupyter research env      │   │
                          │  └──────────────────────────────────────────┘   │
                          │                                                  │
                          │  ┌────────────────┐    ┌─────────────────────┐  │
                          │  │  Strategy       │    │  Execution          │  │
                          │  │  Orchestrator   │───►│  Engine             │  │
                          │  │  (Docker mgr +  │    └──────────┬──────────┘  │
                          │  │   resource      │               │            │
                          │  │   arbiter)      │               │            │
                          │  └───────┬────────┘               │            │
                          │          │                         │            │
                          │  ┌───────┴────────┐               │            │
                          │  │  Strategy       │               │            │
                          │  │  Containers     │               │            │
                          │  │  (1 per strat)  │               │            │
                          │  └────────────────┘               │            │
                          │                                    │            │
                          │  ┌────────────────┐    ┌──────────┴──────────┐  │
                          │  │  Factor         │    │  Data Layer         │  │
                          │  │  Pipeline       ├───►│  (ingest, store,    │  │
                          │  └────────────────┘    │   serve, unify)     │  │
                          │                         │  ┌───────────────┐  │  │
                          │  ┌────────────────┐    │  │ SSD (primary) │  │  │
                          │  │  Backtesting    │    │  │ NAS (archive) │  │  │
                          │  │  Engine         ├───►│  └───────────────┘  │  │
                          │  └────────────────┘    └──────────┬──────────┘  │
                          │                                    │            │
                          │  ┌────────────────┐    ┌──────────┴──────────┐  │
                          │  │  Notification   │    │  Brokerage Adapter  │  │
                          │  │  Dispatcher     │    │  (IB Gateway)       │  │
                          │  └───────┬────────┘    └──────────┬──────────┘  │
                          │          │                         │            │
                          └──────────┼─────────────────────────┼────────────┘
                                     │                         │
                       ┌─────────────┴──┐             ┌────────┴──────────┐
                       │  Email (SMTP)   │             │  Interactive      │
                       │  SMS Gateway    │             │  Brokers          │
                       └────────────────┘             │  (IB Gateway)     │
                                                       └────────┬──────────┘
                       ┌────────────────┐             ┌────────┴──────────┐
                       │  NAS (20 TB)   │             │  US Exchanges     │
                       │  (archive tier,│             │  (NYSE, NASDAQ,   │
                       │   on-premise)  │             │   CBOE, etc.)     │
                       └────────────────┘             └───────────────────┘

                       ┌────────────────┐             ┌────────────────────┐
                       │  Sharadar      │             │  Bulk Equity Data  │
                       │  (fundamental  │             │  Provider (TBD-10) │
                       │   data)        │             │  (daily OHLCV +    │
                       └────────────────┘             │   backfill)        │
                                                       └────────────────────┘
                       ┌────────────────┐
                       │  Databento     │
                       │  (historical   │
                       │   options)     │
                       └────────────────┘
```

### 3.1 External interfaces

| ID | Interface | Direction | Protocol / format | Traces to (StRS) | Notes |
|----|-----------|-----------|-------------------|-------------------|-------|
| IF-1 | IB Gateway API | Bidirectional | IB TWS API (socket-based, port 4001/4002) | C-2, C-7, SN-1.06 | Live and paper trading accounts; headless gateway mode; subject to IB rate limits and pacing rules |
| IF-2 | IB Historical Data API | Inbound | IB TWS API (reqHistoricalData) | C-7, SN-1.26, SN-1.27 | Minute-bar watchlist ingestion and option chain capture only (daily OHLCV sourced from IF-15); subject to IB pacing limits (A-10); pacing budget shared between SYS-22b and SYS-23 per SYS-55 |
| IF-3 | Databento Data Feed | Inbound | Databento API / file download (DBN, CSV, or Parquet) | C-8, SN-1.28 | Historical options data for backtesting; used for deep history predating system deployment |
| IF-4 | Sharadar Fundamental Data | Inbound | Sharadar API (REST/CSV) or bulk download | SN-3.03, BG-3 | Fundamental data for factor pipeline; accessed via vendor-agnostic data provider interface |
| IF-5 | User Parquet Upload | Inbound | Apache Parquet file | C-4, SN-1.02 | User-supplied historical data for backtesting; ingested via API or file system drop |
| IF-6 | Web Dashboard | Outbound (to user) | HTTPS (WebSocket for live updates) | SN-2.01, SN-2.02, SN-2.03 | Web-based UI; technology-agnostic at SyRS level; includes integrated Jupyter environment |
| IF-7 | Strategy API (Python) | Internal/Bidirectional | Python function calls and callbacks | C-1, SN-1.01, SN-1.09, SN-1.22 | The programmatic interface exposed to user-authored strategies within each strategy container |
| IF-8 | CLI Interface | Bidirectional | Command-line (stdin/stdout) | SN-1.11 | Kill switch, strategy management, system administration |
| IF-9 | REST/WebSocket API | Bidirectional | HTTPS / WSS | SN-1.11 | Programmatic access to kill switch, strategy control, system status |
| IF-10 | Email Notification | Outbound | SMTP or third-party email API | SN-1.12 | Connectivity loss, critical failure alerts |
| IF-11 | SMS Notification | Outbound | Third-party SMS gateway API | SN-1.12 | Connectivity loss, critical failure alerts |
| IF-12 | NAS Storage | Bidirectional | NFS v3 (direct-attached, 10.0.0.20) | C-5, SN-1.26, SN-1.27 | 20 TB network-attached storage; archival tier for persistent market data; TrueNAS, HDD-based, 4 GB ZFS ARC |
| IF-13 | Jupyter Notebook Server | Internal (embedded in dashboard) | Jupyter protocol (proxied through dashboard HTTPS) | SN-1.18 | Integrated within dashboard; not a standalone external endpoint |
| IF-14 | Docker Engine API | Internal | Unix socket / Docker API | SN-1.10 | Strategy Orchestrator manages strategy container lifecycle |
| IF-15 | Bulk Equity Data Provider | Inbound | Vendor-specific (REST API, file download, or streaming — abstracted by SYS-56 interface) | SN-1.26, SN-3.03, BG-3 | Phase 1 vendor TBD (TBD-10). Provides daily OHLCV for full US equity universe and minute-bar historical backfill. Accessed via vendor-agnostic data provider interface (SYS-53, SYS-56). |

---

## 4. Functional requirements (system-level)

These are system-level capabilities, not software implementation details.
Each requirement traces to one or more stakeholder needs or business goals
in StRS-001. Requirements are written per INCOSE guidelines: each uses
"shall," is singular, is testable, and avoids implementation prescription
where possible.

### 4.1 Strategy execution

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-1 | The system shall execute user-authored Python trading strategies against live markets through the IB Gateway API, with order acknowledgement received within 1 second (p95) of signal generation under normal network conditions. | BG-1, SN-1.01, SN-1.06, C-1, C-2 | P1 |
| SYS-2 | The system shall allow the user to select, per strategy deployment, whether execution targets the IB live trading account or the IB paper trading account. The selection shall require explicit user confirmation before live account activation. | BG-1, SN-1.06 | P1 |
| SYS-3 | The system shall support submission of market, limit, stop, and stop-limit order types for both equities and options. | BG-1, SN-1.08 | P1 |
| SYS-4 | The system shall support multi-leg options orders (iron condors, straddles, strangles, vertical spreads, butterflies, calendar spreads) submitted as a single composite transaction through the IB API. | BG-1, SN-1.24 | P1 |
| SYS-5 | The system shall enforce that each strategy instance trades a single asset class (equities or options) while permitting the strategy to subscribe to market data for both asset classes simultaneously. | BG-1, BG-5, SN-1.07 | P1 |
| SYS-6 | The system shall provide scheduling primitives within the strategy API (e.g., on market open, on market close, every N minutes, cron-like expressions) that resolve correctly against the trading calendar, including exchange holidays, early closes, and daylight saving time transitions. | BG-1, BG-7, SN-1.09, SN-1.19 | P1 |
| SYS-7 | The system shall deliver order event callbacks (fill, partial fill, cancellation, rejection) to user strategy code, including fill price, fill quantity, and commission, within 1 second (p95) of fill acknowledgement from the broker. | BG-1, SN-1.22 | P1 |
| SYS-8 | The system shall provide a warm-up mechanism that feeds historical data into a strategy before live or backtest execution begins, ensuring all indicators, rolling windows, and internal state are fully initialized before the first trading signal is generated. | BG-1, BG-2, BG-7, SN-1.23 | P1 |
| SYS-9 | The system shall run a minimum of 30 live strategy containers and 30 paper-trading strategy containers concurrently (60 total) without degradation of order execution latency (SYS-1) or dashboard refresh latency (SYS-36). This concurrency target is sized for the reference hardware (Intel i5-12400, 32 GB RAM). | BG-1, BG-6, SN-1.10 | P1 |

### 4.2 Strategy orchestration

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-10 | The system shall provide a Strategy Orchestrator that manages the lifecycle (create, start, stop, restart, destroy) of strategy instances, where each strategy instance executes within its own isolated Docker container. | BG-1, SN-1.10, C-6 | P1 |
| SYS-11 | The Strategy Orchestrator shall allocate and enforce per-container resource limits (CPU, memory) to prevent a single strategy from degrading system-wide performance. Default limits: ≤ 400 MB RAM and ≤ 0.15 CPU cores per container (tunable in orchestrator configuration). | SN-1.10, BG-6 | P1 |
| SYS-12 | The Strategy Orchestrator shall provide a shared communication channel (e.g., message bus, shared volume, or network API) through which strategy containers access the Data Layer, Execution Engine, and logging infrastructure without direct inter-container coupling. | SN-3.01, BG-5 | P1 |
| SYS-13 | The Strategy Orchestrator shall support health checks for each strategy container and shall automatically restart a container that becomes unresponsive, logging the event and notifying the user via the dashboard. | SN-2.03, SN-2.04, C-6 | P1 |

### 4.3 Backtesting

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-14 | The system shall backtest user-authored Python strategies against historical data stored in the system or uploaded by the user as Apache Parquet files, over user-configurable date ranges. | BG-2, SN-1.02, SN-1.13, C-4 | P1 |
| SYS-15 | The system shall model transaction costs in backtests using configurable models for: (a) commissions, defaulting to IB's published tiered commission schedule; (b) slippage, defaulting to a fixed-percentage model of 0.05% per trade; (c) bid-ask spread impact, defaulting to half the median spread observed in stored historical data for the security (or 0.10% if no spread data is available). All defaults shall be user-overridable per backtest run. | BG-2, SN-1.03 | P1 |
| SYS-16 | The system shall compute and report standard performance metrics for completed backtests, including at minimum: Sharpe ratio, Sortino ratio, alpha, beta, maximum drawdown, annualized return, annualized volatility, and win rate. | BG-2, SN-1.04, SN-1.05 | P1 |
| SYS-17 | The system shall compare strategy backtest performance against a user-selected benchmark, defaulting to SPY if no benchmark is specified. | BG-2, BG-4, SN-1.04 | P2 |
| SYS-18 | The system shall provide factor analysis and tear-sheet reporting capabilities, including at minimum: factor returns, information coefficient, and turnover analysis. | BG-2, BG-3, SN-1.05 | P1 |
| SYS-19 | The system shall provide a parameter optimization framework supporting grid search and multi-dimensional parameter sweeps across user-defined parameter ranges, evaluating each combination against a user-specified objective function (e.g., maximize Sharpe ratio, minimize maximum drawdown). | BG-2, BG-7, SN-1.16 | P2 |
| SYS-20 | The system shall provide walk-forward analysis capability that partitions historical data into rolling in-sample and out-of-sample windows, optimizes parameters on each in-sample window, and validates on the corresponding out-of-sample window. | BG-2, BG-7, SN-1.17 | P2 |
| SYS-21 | The system shall persist all backtest results (parameters, metrics, trade log, benchmark comparison) and make them queryable by strategy name, date range, and parameter set. | BG-2, SN-1.02, SN-1.04 | P1 |

### 4.4 Data ingestion and storage

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-22a | The system shall perform nightly scheduled batch ingestion of daily OHLCV data for all US equities (8,000+ securities) from the designated bulk equity data provider, storing results on the local SSD (primary tier) and syncing to the NAS (archival tier). | BG-2, BG-3, BG-7, SN-1.26, C-5 | P1 |
| SYS-22b | The system shall perform nightly scheduled batch ingestion of minute-bar OHLCV data from IB for a user-configurable watchlist of securities, storing results on the local SSD (primary tier) and syncing to the NAS (archival tier). The default watchlist shall include all securities with active live or paper strategies. The watchlist shall be editable via the dashboard and the strategy API. | BG-2, BG-7, SN-1.26, C-5, C-7 | P1 |
| SYS-22c | For initial deployment, the system shall support bulk backfill of both daily and minute-bar historical data from the designated bulk equity data provider, retrieving the maximum available history (target: approximately 20 years daily, approximately 1 year minute). | BG-2, BG-7, SN-1.26 | P1 |
| SYS-23 | The system shall perform nightly scheduled batch ingestion of option chain data from IB for all currently live (non-expired) contracts, capturing at minimum: underlying, expiration, strike, right (call/put), bid, ask, last, volume, open interest, and implied volatility. | BG-2, BG-7, SN-1.27, C-7 | P1 |
| SYS-24 | The system shall retain all ingested equity and options data indefinitely on the NAS (archival tier). Data for expired option contracts shall remain queryable after expiration. | SN-1.26, SN-1.27, C-5 | P1 |
| SYS-25 | The system shall support import of historical options data from Databento in Databento's native format (DBN) and Parquet. | SN-1.28, C-8 | P1 |
| SYS-26 | The system shall ingest fundamental data from Sharadar (the Phase 1 fundamental data provider) on a scheduled basis, including at minimum: income statement, balance sheet, cash flow statement, and key ratios for US equities. | BG-3, BG-7, SN-3.03 | P1 |
| SYS-27 | The system shall provide a unified historical data access interface that allows strategies and research notebooks to query by symbol, date range, and resolution without distinguishing between the underlying data source (bulk equity vendor, IB-ingested minute bars, Databento-imported historical options, Sharadar fundamental data, or user-uploaded Parquet). | BG-2, BG-5, BG-7, SN-1.28, SN-3.03 | P1 |
| SYS-28a | The system shall automatically adjust historical price data for corporate actions — including stock splits, reverse splits, dividends, delistings, mergers, and ticker/symbol changes — such that backtests spanning corporate action dates produce correct P&L calculations. | BG-2, BG-7, SN-1.14 | P1 |
| SYS-28b | The system shall automatically adjust open order quantities and limit prices when a corporate action (stock split, reverse split) affects a security with resting orders during live trading. If adjustment is not possible (e.g., delisting), the system shall cancel the affected orders and notify the user via the strategy API callback and the notification subsystem. | BG-1, BG-7, SN-1.14 | P1 |
| SYS-28c | The system shall automatically adjust portfolio position quantities and average cost basis when a corporate action (stock split, reverse split, dividend) affects a held position during live trading. For mergers and symbol changes, the system shall remap positions to the successor security. For delistings, the system shall mark the position as delisted and notify the user. | BG-1, BG-7, SN-1.14 | P1 |
| SYS-29 | The system shall support configurable data normalization modes: raw (unadjusted), split-adjusted, fully adjusted (splits and dividends), and total return, selectable per security subscription. | BG-2, BG-7, SN-1.15 | P1 |
| SYS-30a | The system shall provide data consolidation capabilities that aggregate minute-resolution data into user-defined time-based bar periods (e.g., 5-minute, 15-minute, hourly, daily) within the strategy API. | BG-1, BG-2, SN-1.21 | P2 |
| SYS-30b | The system shall provide non-standard bar type generation (Renko bars, range bars) within the strategy API, operating on tick or minute-resolution input data. | BG-1, BG-2, SN-1.21 | P3 |
| SYS-31 | Nightly IB batch ingestion (SYS-22b minute-bar watchlist, SYS-23 option chains) shall complete within the overnight window (16:00 ET to 09:30 ET next trading day) while respecting IB's pacing limits (no more than 60 historical data requests per 10-minute period, no identical requests within 15 seconds). Nightly bulk vendor ingestion (SYS-22a daily bars) shall complete within the same overnight window. | BG-3, SN-1.26, A-10 | P1 |

### 4.5 Factor pipeline

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-32 | The system shall screen, rank, and compute user-defined factors across the full US equity universe (8,000+ securities) on a user-defined schedule (e.g., daily before market open), using both market data and fundamental data from Sharadar. | BG-3, SN-2.06 | P1 |
| SYS-33 | The factor pipeline shall complete execution within its scheduled window (e.g., before 09:30 ET for a pre-open schedule) for 8,000+ securities. | BG-3, SN-2.06 | P1 |

### 4.6 Research environment

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-34 | The system shall provide an interactive Jupyter notebook research environment integrated within the web-based dashboard, with access to the system's historical data (equity, options, fundamental), indicator library, and plotting capabilities, operable independently of the backtest engine or any running live strategy. | BG-7, SN-1.18, SN-2.01 | P1 |

### 4.7 Indicator library

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-35 | The system shall provide a built-in technical indicator library integrating pandas-ta and TA-Lib, exposing at minimum SMA, EMA, RSI, MACD, Bollinger Bands, and ATR through the strategy API, with support for incremental (streaming) computation on each new bar in live trading. | BG-1, BG-7, SN-1.20, C-9 | P1 |

### 4.8 Dashboard and observability

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-36 | The system shall provide a web-based dashboard that displays live strategy performance metrics (at minimum: Sharpe ratio, Sortino ratio, alpha, beta, maximum drawdown, P&L), system latency, and system health, with a refresh latency of 5 seconds or less. | BG-4, SN-2.01 | P1 |
| SYS-37 | The dashboard shall display benchmark comparison (default SPY) for each live strategy. | BG-4, SN-1.04 | P2 |
| SYS-38 | The dashboard shall display logs generated by user strategy code via a logging API exposed within the strategy API. | BG-4, SN-2.02 | P1 |
| SYS-39 | The system shall perform continuous heartbeat monitoring of market data feeds and broker API connections, with a maximum staleness threshold of 15 seconds, displayed on the dashboard. | BG-4, SN-2.03, SN-1.12 | P1 |
| SYS-40 | The dashboard shall display multi-leg options positions as single composite positions (e.g., an iron condor displayed as one position, not four independent legs). | BG-4, SN-1.24 | P1 |
| SYS-41 | The dashboard shall display a listing of all currently running (active) strategies, including each strategy's name, mode (live/paper), asset class, container status, and key real-time metrics (P&L, position count). | BG-4, SN-2.01, SN-1.10 | P1 |
| SYS-42 | The dashboard shall provide a backtest result history view that lists all completed backtests for each strategy, including parameters, date range, key performance metrics, and the ability to drill into individual backtest details (trade log, equity curve, benchmark comparison). | BG-2, BG-4, SN-1.02, SN-1.04 | P1 |
| SYS-43 | The dashboard shall integrate the Jupyter research environment (SYS-34) as an embedded view accessible without navigating to a separate URL or service. | BG-7, SN-1.18, SN-2.01 | P1 |

### 4.9 Safety and fail-safes

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-44a | Upon kill switch activation, the system shall cancel all resting orders and submit market liquidation orders for all open positions within 5 seconds. The kill switch shall be accessible from the dashboard, CLI, and REST API. | BG-1, BG-4, SN-1.11 | P1 |
| SYS-44b | If any liquidation order submitted by the kill switch has not received a fill confirmation within 30 seconds, the system shall log the unfilled order details, notify the operator via email and SMS, cancel the unfilled liquidation order, and disconnect from the exchange. The operator shall resolve remaining positions manually. | BG-1, BG-4, SN-1.11 | P1 |
| SYS-45 | The system shall automatically detect when the IB Gateway API becomes unreachable and shall prevent all order submission until connectivity is restored. | BG-1, BG-4, SN-2.04 | P1 |
| SYS-46 | The system shall notify the user through email and SMS within 60 seconds of detecting connectivity loss to IB Gateway or any critical system failure. | BG-4, SN-1.12 | P1 |

### 4.10 Strategy Reservoir and Hot-Swap

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-47 | The system shall maintain a Strategy Reservoir capable of hosting at least 30 concurrent paper-trading strategies, each running in its own container managed by the Strategy Orchestrator (SYS-10). | BG-1, BG-2, SN-1.25 | P1 |
| SYS-48 | The system shall produce a monthly ranking of Strategy Reservoir strategies based on risk-adjusted returns (at minimum: Sharpe ratio, Sortino ratio). | BG-1, BG-2, BG-4, SN-1.25 | P1 |
| SYS-49a | Upon demotion trigger (drawdown breach of user-configured threshold), the system shall submit market liquidation orders for all open positions held by the demoted strategy and shall wait for fill confirmation or a configurable timeout (default: 60 seconds) before transitioning the strategy container to paper-trading mode. | BG-1, SN-1.25 | P1 |
| SYS-49b | If any demotion liquidation order is not filled within the configured timeout, the system shall notify the operator, cancel the unfilled order, and hold the strategy in a "demotion-pending" state. The strategy shall cease generating new trading signals but shall remain live until the operator manually resolves the unfilled position. | BG-1, SN-1.25 | P1 |
| SYS-49c | Upon promotion trigger, the promoted strategy shall begin live execution with no open positions (flat start). The strategy's first live trading signals shall establish its initial positions. | BG-1, SN-1.25 | P1 |
| SYS-49d | The system shall enforce a configurable cool-down period (default: 7 calendar days) between successive Hot-Swap state transitions for the same strategy, to prevent promotion/demotion oscillation. | BG-1, SN-1.25 | P1 |

### 4.11 Trading calendar

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-50 | The system shall maintain a trading calendar for all supported US exchanges (NYSE, NASDAQ, CBOE) that includes exchange holidays, early closes, pre-market and after-hours session boundaries, and time zone data (US Eastern). | BG-1, BG-7, SN-1.19 | P1 |
| SYS-51 | All scheduling primitives (SYS-6) and the factor pipeline schedule (SYS-32) shall resolve against the trading calendar (SYS-50). | SN-1.19, SN-1.09 | P1 |

### 4.12 Extensibility

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-52 | The system shall define a brokerage adapter interface such that a new brokerage can be integrated by implementing the interface without modifying the strategy engine, data layer, or execution engine. | BG-5, SN-3.01, SN-3.02 | P2 |
| SYS-53 | The system shall define a data provider interface such that a new data source (market data, fundamental data, or alternative data) can be integrated by implementing the interface without modifying the strategy engine or backtesting engine. | BG-5, SN-3.01, SN-3.03 | P1 |
| SYS-54 | The system architecture shall support the future addition of new asset classes (cryptocurrency, futures) and alternative data sources (prediction markets, sentiment analytics) by implementing the existing modular interfaces (SYS-52 brokerage adapter, SYS-53 data provider) without modification to the strategy engine, execution engine, data layer, or backtesting engine core modules. | BG-5, SN-3.04 | P2 |

### 4.13 Data architecture (tiered storage)

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-55 | The system shall validate that the combined IB request count for nightly minute-bar watchlist ingestion (SYS-22b) and option chain capture (SYS-23) does not exceed the IB pacing ceiling (approximately 6,300 requests per overnight window). If the projected request count exceeds the ceiling, the system shall alert the operator at ingestion scheduling time and shall refuse to start the ingestion job until the watchlist or option chain scope is reduced. | BG-3, SN-1.26, A-10 | P1 |
| SYS-56 | The system shall define a bulk equity data provider interface such that the Phase 1 vendor can be selected at deployment time by implementing the interface, without modification to the data layer, strategy engine, or backtesting engine. The interface shall support: (a) full-universe daily OHLCV download, (b) initial historical backfill, and (c) incremental nightly updates. | BG-5, SN-3.03, SN-1.26 | P1 |
| SYS-67 | The data layer shall implement a tiered storage architecture with the local SSD as the primary runtime storage tier and the NAS as the archival tier. All data ingestion shall write to the SSD first. A post-ingestion sync job shall copy newly ingested data to the NAS at lowest workload priority (SYS-57). The SSD shall retain at minimum the most recent 90 days of bar data (configurable) for all securities. Data older than the SSD retention window shall remain available on the NAS for cold reads. | C-5, BG-6, SN-1.26 | P1 |
| SYS-68 | The unified data access interface (SYS-27) shall transparently serve read requests from the SSD when the requested data is within the retention window, falling back to the NAS for historical data outside the retention window, without requiring consumers (strategy containers, factor pipeline, backtesting engine, research environment) to be aware of the storage tier. Cold-read results from NAS shall be cached on the SSD. | SN-1.28, BG-5 | P1 |
| SYS-69 | The data layer shall implement a storage eviction policy for the SSD. When SSD usage exceeds a configurable high-water mark (default: 80% of SSD capacity), the storage manager shall evict data by age, prioritizing removal of data for securities not on the active strategy list or minute-bar watchlist. The storage manager shall never evict data for securities with currently running live strategy containers. | C-5, BG-6 | P1 |

### 4.14 Resource management

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-57 | The system shall enforce a workload priority hierarchy for compute and memory resource allocation: (1) live strategy containers (highest), (2) paper strategy containers, (3) nightly data ingestion, (4) factor pipeline, (5) backtesting engine, (6) research environment / Jupyter (lowest). The system shall refuse to start a lower-priority workload if doing so would reduce available host memory below a configurable safety margin (default: 2 GB). | BG-1, BG-6, SN-1.10, C-6 | P1 |
| SYS-58 | The system shall monitor host CPU and memory utilization. If available memory falls below the configured safety margin (SYS-57), the system shall: (a) refuse to deploy new strategy containers, (b) terminate the lowest-priority active batch workload (per the hierarchy in SYS-57) if a higher-priority workload requires resources, and (c) alert the operator via the dashboard and the notification subsystem. The system shall never terminate a live-trading strategy container to free resources for a lower-priority workload. | BG-1, BG-6, SN-1.10, C-6 | P1 |

### 4.15 Backup and recovery

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-59 | The system shall provide a scheduled backup mechanism for all data stored on the NAS (ingested equity data, option chain snapshots, fundamental data, backtest results). Backup shall support at minimum: export to an external storage target (e.g., USB drive, secondary NAS, or cloud archival bucket) on a user-configurable schedule (default: weekly). The backup job shall run at the lowest priority in the workload hierarchy (SYS-57). | BG-6, C-5, SN-1.26, SN-1.27 | P2 |
| SYS-60 | The system shall define a recovery point objective (RPO) of 7 days for NAS-stored market data. If the NAS fails, data loss shall be limited to at most 7 days of ingestion (recoverable by re-running ingestion for the missing dates). The system shall validate backup integrity on completion. | BG-6, C-5 | P2 |

### 4.16 System operational logging

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-61 | The system shall log all system-level operational events to a persistent, queryable log store with timestamps and severity levels (DEBUG, INFO, WARN, ERROR, CRITICAL). Logged events shall include at minimum: order routing decisions and outcomes, data ingestion job start/completion/failure, container lifecycle events (start, stop, restart, OOM kill), IB Gateway connection state changes (connect, disconnect, reconnect), kill switch activations, Hot-Swap promotion/demotion events, and resource threshold alerts (SYS-58). System logs shall be separate from user strategy logs (SYS-38) and shall be viewable from the dashboard. | BG-4, C-6 | P1 |

### 4.17 Backtest reproducibility

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-62 | The backtesting engine shall produce deterministic, reproducible results: given identical strategy code, parameters, date range, input data, and transaction cost model, repeated backtest runs shall produce identical trade logs, equity curves, and performance metrics. The system shall not introduce non-determinism through parallelism, floating-point ordering, or random number generation unless the user's strategy code explicitly uses randomness (in which case the strategy API shall expose a seed parameter). | BG-2, SN-1.02 | P1 |

### 4.18 Concurrent data access

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-63 | The data layer shall support concurrent read access from multiple consumers (strategy containers, factor pipeline, backtesting engine, research environment) without blocking or data corruption. Write operations (nightly data ingestion, backfill) shall not block read access to previously ingested data. | BG-1, BG-2, SN-1.26, SN-1.28 | P1 |

### 4.19 Strategy API error contract

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-64 | The strategy API shall report order submission failures synchronously to user strategy code, returning a structured error object containing: error type (e.g., INVALID_SYMBOL, INSUFFICIENT_BUYING_POWER, CONNECTIVITY_BLOCKED, RATE_LIMITED), human-readable message, and the original order parameters. The system shall not silently drop failed order submissions. | BG-1, SN-1.08, SN-1.22 | P1 |

### 4.20 IB API version management

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-65 | The brokerage adapter (IB Gateway) shall document the supported IB TWS API version and shall include automated integration tests that validate order submission, market data subscription, and historical data retrieval against the supported version. API version upgrades shall be tested against the IB paper trading account before deployment to live trading. | C-2, SN-3.02 | P2 |

### 4.21 Data schema evolution

| ID | Requirement | Traces to (StRS) | Priority |
|----|------------|-------------------|---------|
| SYS-66 | The data layer shall support schema evolution such that data ingested under a prior schema version remains queryable after schema updates, without requiring bulk migration of historical records. Schema version shall be tracked per data entity. | BG-2, SN-1.26, SN-1.27, C-5 | P2 |

---

## 5. Non-functional requirements

### 5.1 Performance

| ID | Requirement | Metric | Condition | Traces to (StRS) |
|----|------------|--------|-----------|-------------------|
| NFR-P1 | Order signal-to-acknowledgement latency | < 1,000 ms p95 | Measured from the strategy container's invocation of the order submission API to the strategy container's receipt of the order acknowledgement callback, including all internal system latency (communication channel, execution engine processing, IB Gateway submission) but excluding IB-to-exchange network round-trip time. Under 60 concurrent strategy containers (30 live + 30 paper), normal network conditions. | BG-1, SN-1.01 |
| NFR-P2 | Dashboard refresh latency | ≤ 5,000 ms | Under 60 concurrent strategy containers (30 live + 30 paper) | BG-4, SN-2.01 |
| NFR-P3 | Kill switch order cancellation and liquidation submission time | ≤ 5,000 ms | Measured from kill switch activation to confirmation that all resting orders are cancelled and all liquidation orders are submitted. Liquidation fill timeout governed by SYS-44b (default 30 seconds). | SN-1.11 |
| NFR-P4 | Order event callback delivery latency | < 1,000 ms p95 | From broker fill acknowledgement to user strategy code callback | SN-1.22 |
| NFR-P5 | Heartbeat staleness detection threshold | ≤ 15,000 ms | Continuous during market hours | SN-2.03 |
| NFR-P6 | Connectivity loss notification delivery | ≤ 60,000 ms | From detection to notification dispatch via email and SMS | SN-1.12, SN-2.04 |
| NFR-P7 | Factor pipeline completion time | Before scheduled deadline (e.g., 09:30 ET) | Processing 8,000+ securities | BG-3, SN-2.06 |
| NFR-P8 | Nightly data ingestion completion | All ingestion jobs (SYS-22a bulk vendor daily bars, SYS-22b IB minute-bar watchlist, SYS-23 IB option chains, SYS-26 Sharadar fundamentals) shall complete within the overnight window (16:00–09:30 ET). IB-sourced ingestion shall remain within IB pacing limits. | 8,000+ securities daily bars; watchlist minute bars; option chains | SN-1.26, SN-1.27, A-10 |
| NFR-P9 | Strategy container startup time | ≤ 30,000 ms | From orchestrator start command to strategy ready (warm-up excluded) | SN-1.10 |
| NFR-P10 | Peak-load validation | The system shall be validated under peak-load conditions (all 60 strategy containers actively processing simultaneous market data events, such as at market open) to confirm that NFR-P1 and NFR-P2 are met. | All 60 containers active simultaneously | BG-1, SN-1.10 |

### 5.2 Security

| ID | Requirement | Traces to (StRS) | Standard / reference |
|----|------------|-------------------|---------------------|
| NFR-S1 | The system shall store all brokerage API credentials (IB Gateway connection parameters, account identifiers) encrypted at rest and shall not log credentials in plaintext. | C-3 (personal/retail context; proportionate security) | OWASP credential storage guidelines |
| NFR-S2 | The system shall require explicit user confirmation before switching any strategy from paper trading to live trading mode. | SN-1.06 | Operational safety practice |
| NFR-S3 | The dashboard shall be accessible only from the local network or via an authenticated reverse proxy when exposed externally. *(Derived requirement.)* | SN-2.01 | OWASP authentication guidelines |
| NFR-S4 | Notification channel credentials (SMTP, SMS gateway API keys) shall be stored encrypted at rest and shall not appear in logs. | SN-1.12 | OWASP credential storage guidelines |
| NFR-S5 | Strategy containers shall run with least-privilege permissions: no host network access, no privileged mode, no access to other strategy containers' filesystems. *(Derived requirement.)* | SN-1.10, SN-3.01 | Docker security best practices (CIS Docker Benchmark) |
| NFR-S6 | The embedded Jupyter research environment shall execute in an isolated container or restricted process with no write access to brokerage credentials (NFR-S1), no direct access to the execution engine, and no ability to submit live orders. Jupyter shall have read-only access to market data and backtest results via the data layer. | SN-1.18, BG-7 | Container isolation best practices |

### 5.3 Reliability

| ID | Requirement | Target | Traces to (StRS) |
|----|------------|--------|-------------------|
| NFR-R1 | System availability during US equity market hours (09:30–16:00 ET, Mon–Fri, excluding market holidays) shall be ≥ 99.9% measured over a rolling 30-day period (≤ 1.17 minutes downtime per trading day on average). Planned maintenance (host reboots, Docker updates, OS patches) shall be scheduled outside market hours and is excluded from the availability measurement. Unplanned host-level outages (hardware failure, kernel panic) are included in the measurement. | ≥ 99.9% | BG-6, SN-2.05 |
| NFR-R2 | The system shall automatically reconnect to IB Gateway after a transient connection drop without manual intervention. | Reconnection attempt within 15 seconds of detection; order submission blocked until reconnection succeeds (SYS-45) | SN-2.04 |
| NFR-R3 | The system shall persist strategy state comprising: open positions, pending order status, account equity snapshot, and a user-accessible state dictionary (JSON-serializable key-value pairs written by user strategy code via the strategy API). Upon container restart, the system shall restore persisted state and re-execute the warm-up mechanism (SYS-8) to reconstruct indicator buffers and rolling windows from historical data. State shall be recoverable within 60 seconds of container restart (excluding warm-up duration, which depends on the strategy's configured warm-up period). User-authored strategy code is responsible for serializing any additional internal state to the state dictionary before shutdown. | State recoverable within 60 seconds of container restart | SN-2.05, SN-1.01 |
| NFR-R4 | Nightly data ingestion (SYS-22a, SYS-22b, SYS-23, SYS-26) shall be idempotent: re-running ingestion for a date that has already been ingested shall not produce duplicate records or corrupt existing data. | Zero duplicate records after re-run | SN-1.26, SN-1.27 |
| NFR-R5 | The Strategy Orchestrator shall survive the crash of any individual strategy container without affecting other running strategies or system-level services (dashboard, data layer, execution engine). | Zero cross-container impact on crash | SN-1.10, SN-2.05 |

### 5.4 Scalability

| ID | Requirement | Traces to (StRS) | Notes |
|----|------------|-------------------|-------|
| NFR-SC1 | The system shall support 60 concurrent strategy containers (30 live + 30 paper) without degradation of NFR-P1 or NFR-P2. | SN-1.10, BG-6 | Sized for reference hardware: Intel i5-12400 (6C/12T), 32 GB RAM, 1 TB SSD. Per-container budget: ≤ 400 MB RAM, ≤ 0.15 CPU cores (default). Higher concurrency requires hardware upgrade. |
| NFR-SC2 | The data layer shall accommodate the full US equity universe (8,000+ securities) at daily and minute-bar resolution, plus option chain snapshots and Sharadar fundamental data, using tiered storage: a local 1 TB SSD as the primary runtime storage tier and the 20 TB NAS as the archival tier. Storage growth estimates shall be documented in SRS. | SN-2.06, SN-1.26, SN-1.27, C-5 | SSD retention window: 90 days (configurable). NAS I/O validated by benchmark (TBD-4 resolved): sequential read 112 MB/s (adequate for cold reads), concurrent random I/O insufficient for runtime use (513 ms latency at 60 workers). |
| NFR-SC3 | The system architecture shall not preclude migration to a multi-node deployment if the Proxmox host's resource limits are reached. | BG-5, BG-6, A-3 | Not required for initial phase; container-per-strategy model facilitates future distribution |

### 5.5 Usability

| ID | Requirement | Traces to (StRS) | Standard |
|----|------------|-------------------|---------|
| NFR-U1 | The system shall be operable and maintainable by a single individual without dedicated DevOps expertise; routine operations (deploy strategy, run backtest, monitor dashboard, trigger kill switch) shall not require SSH access to the Proxmox host. | C-6, SN-2.01, SN-2.02 | NASA-HDBK-8739.22 (software maintainability) — proportionate to single-operator context |
| NFR-U2 | The strategy API shall be documented with Python docstrings and usage examples sufficient for a Python-proficient trader to author a new strategy without reading source code of the platform internals. | SN-1.01, C-1 | IEEE 1063 (software user documentation) |

---

## 6. Architectural constraints

These are **hard rules** that all agents must follow. Violations shall be
identified during code review and, where feasible, enforced by automated
checks in CI.

| ID | Constraint | Source (StRS) | Enforcement |
|----|-----------|---------------|------------|
| AC-1 | User-authored strategies shall be written in Python. The strategy API shall be a Python API. | C-1 | Code review; strategy loader rejects non-Python files |
| AC-2 | The initial brokerage integration shall target IB Gateway (headless mode) via the IB TWS API. The system shall not depend on the TWS GUI being running. | C-2, stakeholder decision | Integration test against IB Gateway |
| AC-3 | The system shall operate in a single-user, single-tenant mode. No multi-user authentication or role-based access control is required. | C-3, OS-2 | Code review |
| AC-4 | User-uploaded backtest data shall be accepted in Apache Parquet format. | C-4 | Data ingestion module input validation |
| AC-5 | The NAS (20 TB) shall be the archival storage tier for all ingested market data and fundamental data. The local SSD (1 TB) shall be the primary runtime storage tier. | C-5, TBD-4 resolution | Deployment configuration; data path audit |
| AC-6 | The built-in indicator library shall wrap pandas-ta and TA-Lib; custom reimplementations of indicators available in these libraries are prohibited. | C-9 | Code review; dependency manifest |
| AC-7 | The brokerage integration layer shall be abstracted behind a defined adapter interface. Core strategy engine, data layer, and backtesting engine code shall not contain IB-specific logic. | SN-3.01, SN-3.02, BG-5 | Structural test; import dependency analysis |
| AC-8 | The data provider layer shall be abstracted behind a vendor-agnostic interface. Core strategy engine and backtesting engine code shall not contain IB-specific, Databento-specific, Sharadar-specific, or bulk-vendor-specific data access logic. | SN-3.01, SN-3.03, BG-5 | Structural test; import dependency analysis |
| AC-9 | Risk management logic (per-strategy, per-account, per-position limits) is the responsibility of user-authored strategy code. The system provides a risk management API framework but does not enforce risk limits. | OS-5 | Code review; documentation |
| AC-10 | The system shall not implement an internal paper-trading simulation engine. Paper trading shall use IB's built-in paper trading account. | OS-6 | Code review |
| AC-11 | Dependencies shall flow in one direction: Types/Models → Data Layer → Strategy Engine → Execution Engine → Brokerage Adapter. The Dashboard and Orchestrator are consumers of all layers but no layer depends on them. Strategy containers depend on Data Layer and Execution Engine services at runtime; the Orchestrator manages container lifecycle but containers do not call the Orchestrator. | SN-3.01, BG-5 | Import dependency analysis; structural test |
| AC-12 | Each strategy instance shall execute in its own Docker container. The Strategy Orchestrator shall be the sole component that manages container lifecycle. No strategy shall directly manage its own container or other containers. | SN-1.10, stakeholder decision | Docker Compose configuration; orchestrator integration test |
| AC-13 | The system shall be deployable on a Proxmox-hosted Ubuntu VM. Infrastructure cost shall be near-zero recurring by leveraging on-premise compute and storage. Cloud VPS deployment remains a secondary target. | C-5, BG-6, stakeholder decision | Deployment documentation; cost audit |

---

## 7. Data requirements

### 7.1 Data entities (high level)

| Entity | Description | Owner (module) | Traces to (StRS) |
|--------|------------|----------------|-------------------|
| Equity Bar (Daily) | OHLCV + adjusted close for a single equity on a single date | Data Layer | SN-1.26 |
| Equity Bar (Minute) | OHLCV for a single equity for a single minute | Data Layer | SN-1.26 |
| Option Chain Snapshot | Bid, ask, last, volume, OI, IV, Greeks per contract per capture date | Data Layer | SN-1.27 |
| Fundamental Record | Income statement, balance sheet, cash flow, key ratios per security per period (from Sharadar) | Data Layer | SN-3.03, BG-3 |
| Corporate Action | Split ratio, dividend amount, merger mapping, delisting flag, symbol change, effective date | Data Layer | SN-1.14 |
| Strategy Instance | Configuration, state, positions, orders, performance metrics, container ID for a deployed strategy | Strategy Engine / Orchestrator | SN-1.01, SN-1.10 |
| Order | Order type, symbol, quantity, price, status, fill details, commission | Execution Engine | SN-1.08, SN-1.22 |
| Position | Symbol, quantity, average cost, unrealized P&L, asset class | Strategy Engine | SN-1.07 |
| Backtest Result | Parameter set, performance metrics, trade log, equity curve, benchmark comparison, timestamp | Backtesting Engine | SN-1.02, SN-1.04 |
| Factor Score | Security, factor name, factor value, rank, computation date | Factor Pipeline | BG-3, SN-2.06 |
| Trading Calendar | Exchange, date, session type (full, early close, holiday), open/close times | Data Layer | SN-1.19 |
| Strategy Reservoir Entry | Strategy ID, paper performance history, ranking score, promotion/demotion status, demotion-pending flag, cool-down expiry, container ID | Strategy Engine / Orchestrator | SN-1.25 |
| Notification Event | Event type, timestamp, channels dispatched (email, SMS), delivery status | Notification subsystem | SN-1.12 |
| System Log Entry | Timestamp, severity, source component, event type, message, correlation ID | System logging | C-6 |
| Minute-Bar Watchlist | List of securities for IB minute-bar ingestion, editable via dashboard and API | Data Layer | SN-1.26 |

### 7.2 Data retention and privacy

- **Retention:** All ingested market data (equities, options) and fundamental data (Sharadar) shall be retained indefinitely on the NAS archival tier (SYS-24). The SSD primary tier retains the most recent 90 days (configurable) for runtime access (SYS-67). Backtest results and strategy logs shall be retained until explicitly deleted by the user.
- **PII handling:** The system operates in a single-user personal context (C-3). No PII of third parties is stored. The user's brokerage account credentials are the only sensitive personal data and shall be encrypted at rest (NFR-S1).
- **Encryption:** Brokerage and notification credentials encrypted at rest (NFR-S1, NFR-S4). Data at rest on NAS and SSD is not encrypted (proportionate to personal/retail context and both being on a private local network). If either storage device is exposed to an untrusted network, encryption at rest should be reassessed.

---

## 8. Deployment environment

| Property | Value | Traces to (StRS) |
|----------|-------|-------------------|
| Strategy runtime | Python ≥ 3.12 | C-1 |
| Target OS | Ubuntu Linux (latest LTS) | Stakeholder decision |
| Virtualization host | Proxmox VE (on-premise) | Stakeholder decision, BG-6 |
| Host hardware | Intel i5-12400 (6C/12T), 32 GB RAM, 1 TB SSD | Stakeholder-provided |
| Container runtime | Docker / Docker Compose | Stakeholder decision (container-per-strategy) |
| IB connectivity | IB Gateway (headless), ports 4001 (live) / 4002 (paper) | C-2, stakeholder decision |
| Primary storage (SSD) | Local 1 TB SSD on Proxmox host, ext4 or XFS. Runtime storage for all active data: recent bars, strategy state, backtest working sets, factor pipeline inputs, logs. | C-5, TBD-4 resolution |
| Archival storage (NAS) | TrueNAS, 20 TB, HDD-based, 4 GB ZFS ARC, NFS v3, direct-attached (10.0.0.20). Archival tier for full historical data. Sequential read: 112 MB/s. Concurrent random I/O inadequate for runtime use (validated by benchmark). | C-5, TBD-4 resolution |
| Storage architecture | Tiered: SSD primary (runtime read/write) + NAS archival (sync + cold reads). Ingestion writes to SSD first, syncs to NAS post-ingestion. | TBD-4 resolution |
| Fundamental data | Sharadar (Phase 1) | Stakeholder decision |
| Bulk equity data | Phase 1 vendor TBD (TBD-10). Candidates: Databento, Polygon. | Option D architecture decision |
| Deployment — Phase 1 | Single Proxmox VM running Docker Compose stack | SN-2.07, BG-6 |
| Deployment — Phase 2 (optional) | Cloud VPS with equivalent Docker Compose stack | SN-2.07 |
| CI/CD | TBD (TBD-3, to be selected before SRS phase) | — |
| Indicator dependencies | pandas-ta (Python), TA-Lib (C library + Python wrapper) | C-9 |
| Jupyter | JupyterLab or Jupyter Notebook, embedded within dashboard, isolated per NFR-S6 | SN-1.18 |
| Recurring infrastructure cost target | Near-zero (on-premise compute and storage; external costs limited to IB subscriptions, Sharadar subscription, bulk equity vendor subscription, email/SMS service) | BG-6 |
| Concurrency target | 30 live + 30 paper strategy containers (60 total) on reference hardware | SN-1.10 |

---

## 9. Traceability matrix (SyRS → StRS)

The following matrix maps every system requirement to its originating stakeholder need(s) and business goal(s).

| SyRS ID | StRS Need(s) | StRS Business Goal(s) |
|---------|-------------|----------------------|
| SYS-1 | SN-1.01, SN-1.06 | BG-1, BG-5 |
| SYS-2 | SN-1.06 | BG-1 |
| SYS-3 | SN-1.08 | BG-1 |
| SYS-4 | SN-1.24 | BG-1 |
| SYS-5 | SN-1.07 | BG-1, BG-5 |
| SYS-6 | SN-1.09, SN-1.19 | BG-1, BG-7 |
| SYS-7 | SN-1.22 | BG-1 |
| SYS-8 | SN-1.23 | BG-1, BG-2, BG-7 |
| SYS-9 | SN-1.10 | BG-1, BG-6 |
| SYS-10 | SN-1.10 | BG-1 |
| SYS-11 | SN-1.10 | BG-6 |
| SYS-12 | SN-3.01 | BG-5 |
| SYS-13 | SN-2.03, SN-2.04 | BG-4 |
| SYS-14 | SN-1.02, SN-1.13 | BG-2 |
| SYS-15 | SN-1.03 | BG-2 |
| SYS-16 | SN-1.04, SN-1.05 | BG-2 |
| SYS-17 | SN-1.04 | BG-2, BG-4 |
| SYS-18 | SN-1.05 | BG-2, BG-3 |
| SYS-19 | SN-1.16 | BG-2, BG-7 |
| SYS-20 | SN-1.17 | BG-2, BG-7 |
| SYS-21 | SN-1.02, SN-1.04 | BG-2 |
| SYS-22a | SN-1.26 | BG-2, BG-3, BG-7 |
| SYS-22b | SN-1.26 | BG-2, BG-7 |
| SYS-22c | SN-1.26 | BG-2, BG-7 |
| SYS-23 | SN-1.27 | BG-2, BG-7 |
| SYS-24 | SN-1.26, SN-1.27 | BG-2, BG-7 |
| SYS-25 | SN-1.28 | BG-2, BG-5, BG-7 |
| SYS-26 | SN-3.03 | BG-3, BG-7 |
| SYS-27 | SN-1.28, SN-3.03 | BG-2, BG-5, BG-7 |
| SYS-28a | SN-1.14 | BG-2, BG-7 |
| SYS-28b | SN-1.14 | BG-1, BG-7 |
| SYS-28c | SN-1.14 | BG-1, BG-7 |
| SYS-29 | SN-1.15 | BG-2, BG-7 |
| SYS-30a | SN-1.21 | BG-1, BG-2 |
| SYS-30b | SN-1.21 | BG-1, BG-2 |
| SYS-31 | SN-1.26, A-10 | BG-3 |
| SYS-32 | SN-2.06 | BG-3 |
| SYS-33 | SN-2.06 | BG-3 |
| SYS-34 | SN-1.18, SN-2.01 | BG-7 |
| SYS-35 | SN-1.20 | BG-1, BG-7 |
| SYS-36 | SN-2.01 | BG-4 |
| SYS-37 | SN-1.04 | BG-4 |
| SYS-38 | SN-2.02 | BG-4 |
| SYS-39 | SN-2.03, SN-1.12 | BG-4 |
| SYS-40 | SN-1.24 | BG-4 |
| SYS-41 | SN-2.01, SN-1.10 | BG-4 |
| SYS-42 | SN-1.02, SN-1.04 | BG-2, BG-4 |
| SYS-43 | SN-1.18, SN-2.01 | BG-7 |
| SYS-44a | SN-1.11 | BG-1, BG-4 |
| SYS-44b | SN-1.11 | BG-1, BG-4 |
| SYS-45 | SN-2.04 | BG-1, BG-4 |
| SYS-46 | SN-1.12 | BG-4 |
| SYS-47 | SN-1.25 | BG-1, BG-2 |
| SYS-48 | SN-1.25 | BG-1, BG-2, BG-4 |
| SYS-49a | SN-1.25 | BG-1, BG-2 |
| SYS-49b | SN-1.25 | BG-1, BG-2 |
| SYS-49c | SN-1.25 | BG-1 |
| SYS-49d | SN-1.25 | BG-1 |
| SYS-50 | SN-1.19 | BG-1, BG-7 |
| SYS-51 | SN-1.19, SN-1.09 | BG-1, BG-7 |
| SYS-52 | SN-3.01, SN-3.02 | BG-5 |
| SYS-53 | SN-3.01, SN-3.03 | BG-5 |
| SYS-54 | SN-3.04 | BG-5 |
| SYS-55 | SN-1.26, A-10 | BG-3 |
| SYS-56 | SN-1.26, SN-3.03 | BG-3, BG-5 |
| SYS-57 | SN-1.10, C-6 | BG-1, BG-6 |
| SYS-58 | SN-1.10, C-6 | BG-1, BG-6 |
| SYS-59 | SN-1.26, SN-1.27, C-5 | BG-6 |
| SYS-60 | C-5 | BG-6 |
| SYS-61 | SN-2.01, C-6 | BG-4 |
| SYS-62 | SN-1.02 | BG-2 |
| SYS-63 | SN-1.26, SN-1.28 | BG-1, BG-2 |
| SYS-64 | SN-1.08, SN-1.22 | BG-1 |
| SYS-65 | C-2, SN-3.02 | BG-5 |
| SYS-66 | SN-1.26, SN-1.27, C-5 | BG-2 |
| SYS-67 | SN-1.26, C-5 | BG-6 |
| SYS-68 | SN-1.28 | BG-2, BG-5 |
| SYS-69 | C-5 | BG-6 |
| NFR-P1 | SN-1.01 | BG-1 |
| NFR-P2 | SN-2.01 | BG-4 |
| NFR-P3 | SN-1.11 | BG-1 |
| NFR-P4 | SN-1.22 | BG-1 |
| NFR-P5 | SN-2.03 | BG-4 |
| NFR-P6 | SN-1.12, SN-2.04 | BG-4 |
| NFR-P7 | SN-2.06 | BG-3 |
| NFR-P8 | SN-1.26, SN-1.27 | BG-3 |
| NFR-P9 | SN-1.10 | BG-1 |
| NFR-P10 | SN-1.10 | BG-1 |
| NFR-S1 | — | BG-1 (operational safety) |
| NFR-S2 | SN-1.06 | BG-1 |
| NFR-S3 *(derived)* | SN-2.01 | BG-4 |
| NFR-S4 | SN-1.12 | BG-4 |
| NFR-S5 *(derived)* | SN-1.10, SN-3.01 | BG-1 |
| NFR-S6 | SN-1.18 | BG-7 |
| NFR-R1 | SN-2.05 | BG-6 |
| NFR-R2 | SN-2.04 | BG-1, BG-4 |
| NFR-R3 | SN-2.05, SN-1.01 | BG-1, BG-6 |
| NFR-R4 | SN-1.26, SN-1.27 | BG-2 |
| NFR-R5 | SN-1.10, SN-2.05 | BG-1, BG-6 |
| NFR-SC1 | SN-1.10 | BG-1, BG-6 |
| NFR-SC2 | SN-2.06, SN-1.26, SN-1.27 | BG-3, BG-6 |
| NFR-SC3 | — | BG-5, BG-6 |
| NFR-U1 | — | BG-6 (C-6) |
| NFR-U2 | SN-1.01 | BG-1 |
| AC-1 | — | C-1 |
| AC-2 | — | C-2 |
| AC-3 | — | C-3, OS-2 |
| AC-4 | — | C-4 |
| AC-5 | — | C-5 |
| AC-6 | — | C-9 |
| AC-7 | SN-3.01, SN-3.02 | BG-5 |
| AC-8 | SN-3.01, SN-3.03 | BG-5 |
| AC-9 | — | OS-5 |
| AC-10 | — | OS-6 |
| AC-11 | SN-3.01 | BG-5 |
| AC-12 | SN-1.10 | BG-1 |
| AC-13 | — | BG-6, C-5 |

---

## 10. Open questions and TBDs

The following items require stakeholder or engineering decisions before
this document advances to version 1.0:

| # | Question | Impact | Owner | Status |
|---|----------|--------|-------|--------|
| TBD-1 | ~~What is the monthly infrastructure cost threshold referenced in BG-6?~~ | — | Stakeholder | **Resolved v0.2:** Near-zero recurring cost; on-premise Proxmox host. External costs limited to IB subscriptions, Sharadar subscription, email/SMS service. |
| TBD-2 | ~~Which specific fundamental data provider will be used?~~ | — | Stakeholder | **Resolved v0.2:** Sharadar (Phase 1). Vendor-agnostic interface (SYS-53) accommodates future migration. |
| TBD-3 | What CI/CD platform will be adopted? | Affects enforcement strategy for architectural constraints (Section 6). | Engineering | Open |
| TBD-4 | ~~Is NAS I/O throughput sufficient for backtesting workloads, or is a local SSD hot-data cache needed?~~ | — | Engineering | **Resolved v0.4:** NAS insufficient for runtime use. Benchmark results: sequential read 112 MB/s (pass), concurrent random I/O 116 IOPS / 513 ms latency at 60 workers (fail), mixed read/write 3.8 MB/s read (fail). Architecture decision: SSD-primary runtime storage + NAS archival tier. See TBD-4 Resolution document for full benchmark data and architectural details. SYS-67, SYS-68, SYS-69, AC-5, NFR-SC2 updated accordingly. |
| TBD-5 | What is the acceptable overnight window for option chain ingestion (SYS-23), given that IB may require real-time snapshots near close rather than EOD batch? (Relates to A-11.) Under Option D, IB pacing budget is shared between SYS-22b (minute-bar watchlist) and SYS-23 (option chains). Option chain ingestion scope and timing directly affect available pacing budget for minute bars. SYS-55 enforces the combined budget constraint. | May shift option capture from nightly batch to near-close real-time, affecting system scheduling and pacing budget allocation. | Engineering | Open |
| TBD-6 | ~~Should the notification subsystem be extensible to additional channels?~~ | — | Stakeholder | **Resolved v0.2:** Email and SMS sufficient for Phase 1. Extensibility to additional channels deferred to future phase. |
| TBD-7 | What is the target recovery time objective (RTO) for a full system restart during market hours? NFR-R3 specifies 60-second state recovery per container, but total RTO including Proxmox VM boot, Docker daemon startup, and orchestrator initialization is not yet defined. | Affects deployment architecture (e.g., Proxmox auto-start, Docker restart policies, watchdog configuration). Must be compatible with NFR-R1 availability target. | Engineering | Open |
| TBD-8 | ~~What are the CPU and memory resource limits per strategy container on the Proxmox host?~~ | — | Engineering | **Resolved v0.3:** Default per-container limits set at ≤ 400 MB RAM, ≤ 0.15 CPU cores. Concurrency target reduced to 30 live + 30 paper (60 total) to fit reference hardware. Resource budget: ~7 GB fixed overhead + (60 × 400 MB) = ~31 GB total RAM. Tunable in orchestrator config. SYS-57/58 add workload priority hierarchy and 2 GB safety margin. |
| TBD-9 | ~~What Proxmox host hardware is available (CPU cores, RAM, local SSD)?~~ | — | Stakeholder | **Resolved v0.3:** Intel i5-12400 (6C/12T), 32 GB RAM, 1 TB SSD. Documented in Section 8 (Deployment environment). |
| TBD-10 | Which bulk equity data provider will be used for daily OHLCV and minute-bar backfill (IF-15, SYS-22a, SYS-22c, SYS-56)? Candidates: Databento (already integrated for historical options), Polygon (~$199/mo flat-rate). Decision affects IF-15 protocol, ingestion scheduling, and recurring cost. Vendor-agnostic interface (SYS-56) accommodates deferred selection. | Affects IF-15 protocol, recurring cost, and data layer adapter implementation. | Stakeholder | Open |

---

## 10.1 StRS deviations

The following SyRS requirements deviate from StRS-001 values. All
deviations have been approved by the stakeholder.

| SyRS ID | StRS Reference | StRS Value | SyRS Value | Rationale |
|---------|---------------|------------|------------|-----------|
| SYS-9, SYS-47, NFR-SC1 | SN-1.10 (50 objective, 100 threshold), SN-1.25 (50+ reservoir) | 50–100 concurrent strategies | 60 total (30 live + 30 paper) | Reference hardware (i5-12400, 32 GB RAM) cannot sustain 50–100 Docker containers with acceptable per-container resource budgets. 60 containers at ≤ 400 MB RAM each consume ~31 GB including fixed overhead, which is near the 32 GB ceiling. Stakeholder accepted reduced target over hardware upgrade. StRS SN-1.10 and SN-1.25 should be updated to reflect this revision in the next StRS version. |
| SYS-22a, SYS-22b | SN-1.26 (ingest from IB) | All equity data (daily + minute) from IB | Daily OHLCV from bulk vendor; minute bars from IB (watchlist only) | IB pacing limit of 60 requests per 10 minutes yields ~6,300 requests per overnight window vs. ~16,000+ required for full-universe daily + minute ingestion — a 2.5x shortfall. Option D architecture sources daily bars from a bulk vendor (no pacing constraint) and reserves IB pacing budget for minute-bar watchlist and option chain capture. StRS SN-1.26 should be updated in the next StRS version. |

---

## 10.2 Recommended StRS-001 updates

The following updates to StRS-001 are recommended for StRS v0.5:

1. **SN-1.10:** Update from "50 strategies (objective), 100 (threshold)" to "60 total (30 live + 30 paper)" per deviation approved in §10.1.
2. **SN-1.25:** Update from "at least 50 paper-trading strategies" to "at least 30 paper-trading strategies" per same deviation.
3. **SN-1.12:** Scope Phase 1 channels to "email, SMS, and dashboard alert" per TBD-6 resolution. Note future extensibility to Telegram, Discord, push notification.
4. **SN-1.11:** Remove "mobile notification action" or clarify mechanism. SyRS SYS-44a provides dashboard (mobile-accessible via browser), CLI, and REST API.
5. **SN-1.26:** Note that daily OHLCV sourcing has been moved from IB to a bulk equity data provider (vendor TBD), per Option D data architecture decision. IB remains the source for minute-bar data on a watchlist basis.
6. **Add A-12:** "The designated bulk equity data provider delivers complete, timely daily OHLCV data for the full US equity universe with sufficient reliability for nightly ingestion. If the provider experiences extended outages, the system may fall back to IB for daily bar ingestion on a staggered rotation basis, subject to pacing limits."

---

## 11. Change log

| Version | Date | Author | Summary |
|---------|------|--------|---------|
| 0.1 | 2026-03-18 | Systems Engineer | Initial draft: 45 functional requirements, 8 performance NFRs, 4 security NFRs, 4 reliability NFRs, 3 scalability NFRs, 2 usability NFRs, 11 architectural constraints; full traceability matrix to StRS-001 v0.4; 7 open TBDs identified |
| 0.2 | 2026-03-19 | Systems Engineer | Incorporated stakeholder feedback: added Proxmox as deployment target (AC-13); added Sharadar as Phase 1 fundamental data provider (SYS-26, IF-4); added container-per-strategy orchestration (SYS-10 through SYS-13, AC-12, IF-14); added dashboard requirements for active strategy listing (SYS-41), backtest result history (SYS-42), and embedded Jupyter (SYS-43); added backtest result persistence (SYS-21); added NFR-P9 (container startup time), NFR-S5 (container security), NFR-R5 (fault isolation); resolved TBD-1, TBD-2, TBD-6; added TBD-8, TBD-9 for Proxmox resource planning. Total: 54 functional requirements, 9 performance NFRs, 5 security NFRs, 5 reliability NFRs, 3 scalability NFRs, 2 usability NFRs, 13 architectural constraints. |
| 0.3 | 2026-03-20 | Systems Engineer | Hardware-constrained resource planning: documented reference hardware (i5-12400, 32 GB RAM, 1 TB SSD); revised concurrency target from 50/100 to 30 live + 30 paper (60 total) based on resource analysis; set default per-container limits (≤ 400 MB RAM, ≤ 0.15 CPU cores); updated SYS-9, SYS-11, SYS-47, NFR-P1, NFR-P2, NFR-SC1 accordingly; deferred SSD hot-cache decision pending NAS benchmark (TBD-4); resolved TBD-8, TBD-9. Remaining open TBDs: 3 (TBD-3, TBD-5, TBD-7). |
| 0.4 | 2026-03-20 | Systems Engineer | Peer review incorporation and architectural decisions. **Option D data architecture:** split SYS-22 into SYS-22a/22b/22c (bulk vendor for daily OHLCV, IB for minute-bar watchlist, bulk vendor for backfill); added SYS-55 (pacing budget validator), SYS-56 (bulk equity provider interface), IF-15; revised SYS-31 and NFR-P8. **Tiered storage (TBD-4 resolved):** NAS benchmark confirmed insufficient concurrent I/O (116 IOPS, 513 ms latency at 60 workers); added SYS-67/68/69 (SSD-primary + NAS archival), updated AC-5, NFR-SC2. **Resource management:** added SYS-57/58 (workload priority hierarchy, resource monitoring, 2 GB safety margin). **Hot-Swap safety:** decomposed SYS-49 into SYS-49a–d (liquidate-before-demotion, demotion-pending state, flat-start promotion, 7-day cool-down). **Kill switch:** decomposed SYS-44 into SYS-44a/44b (submission vs. fill timeout). **Requirement quality:** decomposed SYS-28 (corporate actions singularity), SYS-30 (time-based vs. non-standard bars); revised NFR-P1 (measurement boundary), NFR-P3 (aligned to SYS-44a/b), NFR-R1 (maintenance exclusion), NFR-R3 (state recovery scope), SYS-54 (testability), SYS-15 (default values). **Missing requirements:** SYS-59/60 (backup/recovery), SYS-61 (system logging), SYS-62 (backtest reproducibility), SYS-63 (concurrent data access), SYS-64 (API error contract), SYS-65 (IB API version), SYS-66 (schema evolution), NFR-P10 (peak-load validation), NFR-S6 (Jupyter isolation). **Traceability:** added SYS-39→SN-1.12 trace; marked NFR-S3, NFR-S5 as derived; added SYS-51 business goals; added §10.2 recommended StRS updates. Added TBD-10 (bulk equity vendor selection), updated TBD-5 context. Resolved TBD-4. Remaining open TBDs: 4 (TBD-3, TBD-5, TBD-7, TBD-10). Total: 69 functional requirements, 10 performance NFRs, 6 security NFRs, 5 reliability NFRs, 3 scalability NFRs, 2 usability NFRs, 13 architectural constraints. |
