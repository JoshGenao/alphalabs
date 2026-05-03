# Stakeholder Requirements Specification (StRS)

**Document ID:** StRS
**Version:** 0.6 (draft)  
**Status:** In progress  
**Last updated:** 2026-03-22

---

## 1. Purpose

This document captures the needs, goals, and success criteria of all
stakeholders for the Algorithmic Trading System project. It is prepared
in accordance with ISO/IEC/IEEE 29148:2018 and the INCOSE Guide for
Writing Requirements. It answers the question: **why are we building this?**

The system under development is an algorithmic trading platform capable
of trading stocks and options through Interactive Brokers, with an
architecture designed for future expansion to additional securities
(e.g., cryptocurrency, futures) and additional brokerages. The system
shall support strategy types including (but not limited to) momentum,
volatility, fundamental, arbitrage, and mean-reversion.

Agents reading this document should use it to understand priority and
intent when requirements in the SRS appear ambiguous or conflicting.

---

## 2. Stakeholders

| Role | Name / Group | Primary concern |
|------|-------------|----------------|
| Product Owner / End User | Josh (Stakeholder) | System enables profitable, reliable algorithmic trading of stocks and options with minimal operational overhead |
| Operator / Maintainer | Josh (Stakeholder) | System is maintainable, observable, and recoverable by a single person without dedicated DevOps support |
| Engineering | Development Team | Requirements are unambiguous, testable, and architecturally feasible within stated constraints |
| Broker / Exchange | Interactive Brokers (External Dependency) | API usage complies with IB terms of service, rate limits, and market data subscription agreements |

---

## 3. Business goals

List the top-level goals in priority order. Each goal must be specific
enough that an agent can evaluate whether a proposed feature serves it.

| ID | Goal | Priority | Success metric |
|----|------|----------|---------------|
| BG-1 | Execute algorithmic trading strategies (momentum, volatility, fundamental, arbitrage, mean-reversion) against live markets through Interactive Brokers with reliable, low-latency order routing | High | At least one strategy executes live trades end-to-end with order acknowledgement within 1 second of signal generation |
| BG-2 | Provide a robust backtesting environment that produces realistic, bias-aware performance results using stored or user-supplied data | High | Backtest results include configurable transaction cost, slippage, and commission models; strategy performance metrics match industry-standard calculations |
| BG-3 | Screen, rank, and compute factors across the full US equity universe based on scheduling primitives to support quantitative strategy development | High | Pipeline processes 8,000+ securities and completes factor computation according to the user-defined schedule (e.g., before market open at 09:30 ET) |
| BG-4 | Deliver real-time observability into strategy performance, system health, and connectivity status through a web-based dashboard | Medium | Dashboard renders live P&L, core performance metrics, heartbeat status, and strategy logs with less than 5-second refresh latency |
| BG-5 | Maintain a modular, extensible architecture that enables future addition of new asset classes (crypto, futures), brokerages, and data providers without major rearchitecture | Medium | New brokerage adapter or data provider can be integrated without modifying core strategy or execution engine code |
| BG-6 | Minimize infrastructure cost while maintaining 99.9% availability during US equity market hours | Medium | Monthly infrastructure cost remains below a stakeholder-defined threshold; measured uptime meets or exceeds 99.9% during regular trading hours (09:30–16:00 ET, Mon–Fri) |
| BG-7 | Provide a comprehensive research and strategy development toolchain that accelerates the hypothesis-to-backtest-to-live workflow, including interactive research, built-in indicators, parameter optimization, and correct handling of corporate actions | High | Strategies can be prototyped in a notebook environment, backtested with automatically adjusted corporate action data, optimized via parameter sweeps, and deployed live — all within the same platform |

---

## 4. Stakeholder needs

For each stakeholder group, describe what they need the system to do and
why. These are not features — they are needs that features must satisfy.

### 4.1 Trader / Product Owner (Josh)

**Context:** Josh is a personal/retail trader who develops Python-based
algorithmic trading strategies. He operates as a solo trader and sole
system operator. He requires a platform that allows him to research,
backtest, and deploy strategies to live markets through Interactive
Brokers with confidence in execution quality and system reliability.

**Needs:**

| ID | Need | Rationale | Priority |
|----|------|-----------|----------|
| SN-1.01 | The trader needs to write algorithmic strategies in Python and have them executed by the system against live or simulated markets. | The trader's expertise is in Python-based quantitative development; the system must provide an API that exposes scheduling, market data, and order management to user-written Python code. | High |
| SN-1.02 | The trader needs to backtest strategies against historical data stored in the system or uploaded by the user (Parquet file format) with configurable date ranges. | Validating strategy performance before risking capital is essential; both system-stored and user-provided datasets must be supported to enable flexible research. | High |
| SN-1.03 | The trader needs the backtesting engine to model realistic transaction costs including Interactive Brokers commission schedules, bid-ask spread impact, and slippage, with defaults that the user can override. | Backtests that ignore friction costs produce misleading results; IB-based defaults reduce configuration burden while retaining flexibility. | High |
| SN-1.04 | The trader needs to compare strategy performance against a user-selected benchmark (defaulting to SPY if no benchmark is selected) in both backtesting reports and on the live trading dashboard. | Benchmark comparison is fundamental to evaluating risk-adjusted returns and determining whether a strategy adds value over passive investment. | Medium |
| SN-1.05 | The trader needs factor analysis and tear-sheet capabilities (e.g., factor returns, information coefficient, turnover analysis) to evaluate quantitative strategies. | Factor-level diagnostics reveal whether alpha signals are robust or driven by unintended exposures; the capability shall be vendor-agnostic to accommodate the best available libraries. | High |
| SN-1.06 | The trader needs to designate exactly one strategy at a time as the live strategy, executing against the IB live trading account. All other running strategies execute against the internal simulation engine. The IB paper trading account remains available as a manual integration testing tool. | Only one strategy trades real capital at a time, eliminating multi-strategy position attribution complexity on IB's flat account model. Paper simulation is handled internally for the Strategy Reservoir. IB paper trading is retained for validating IB adapter behavior before live deployment. | High |
| SN-1.07 | The trader needs to trade stocks and options as distinct asset classes per strategy, while allowing a single strategy to receive both stock and option market data for analysis. | Multi-asset data access supports complex strategies (e.g., hedging equity positions with options) without conflating execution across asset classes. | High |
| SN-1.08 | The trader needs the system to support market, limit, stop, and stop-limit order types. | These four order types cover the execution needs of swing and position trading strategies. | High |
| SN-1.09 | The trader needs the strategy API to expose scheduling primitives (e.g., run at market open, run every N minutes, run at market close) rather than requiring the user to manage event loops. | Built-in scheduling reduces boilerplate, prevents timing errors, and enables the system to coordinate multiple concurrent strategies. | High |
| SN-1.10 | The trader needs to run one strategy live on the IB account and up to 59 strategies concurrently in internal paper-trading simulation, all sharing live market data from IB, sized for the reference hardware (Intel i5-12400, 32 GB RAM). The total concurrent strategy count (1 live + N paper) is bounded by reference hardware resource constraints. | The trader's research pipeline produces many candidate strategies that must run in parallel for evaluation. Only one strategy executes against the IB account at any time; all others run in internal simulation to build performance track records for the Strategy Reservoir. The single-live-strategy model eliminates the complexity of multi-strategy position attribution on a flat brokerage account. | Medium |
| SN-1.11 | The trader needs a kill switch that immediately cancels all resting orders on IB, liquidates all open IB positions (held by the single live strategy), halts all paper strategy simulations, and disconnects from the exchange, accessible from the dashboard, CLI, and REST API. | In an emergency (runaway strategy, flash crash, connectivity degradation), the trader must be able to halt all activity instantly from any available interface. The dashboard is accessible via mobile browser; the REST API is callable from any HTTP client. | High |
| SN-1.12 | The trader needs to be notified through email, SMS, and dashboard alert when the system loses internet connectivity or critical failures occur. Future phases may extend notification to additional channels (push notification, Telegram, Discord). | As a solo operator, the trader cannot monitor the system continuously; multi-channel notification ensures awareness of problems regardless of the trader's current context. Email and SMS provide sufficient coverage for Phase 1. | High |
| SN-1.13 | The trader needs to select backtest start and end dates from the user interface. | Manual date selection from the UI streamlines the research workflow and avoids requiring code changes for each backtest run. | Medium |
| SN-1.14 | The trader needs the system to automatically handle corporate actions — including stock splits, reverse splits, dividends, delistings, mergers, and ticker/symbol changes — by adjusting historical price data, open order quantities and prices, and portfolio positions correctly in both backtesting, live trading, and internal paper-trading simulation. | Failure to account for corporate actions produces incorrect backtest results (e.g., phantom price drops on split dates) and can cause live strategies to submit orders with stale quantities or prices. Both Zipline and QuantConnect handle these automatically; the system must do the same. | High |
| SN-1.15 | The trader needs configurable data normalization modes (e.g., raw, split-adjusted, fully adjusted for splits and dividends, total return) so that strategies and indicators can operate on the appropriate price series for their use case. | Options strategies require raw (unadjusted) prices, while technical indicators on equities typically require split-adjusted or fully adjusted prices. The system must support multiple normalization modes, selectable per security subscription. | High |
| SN-1.16 | The trader needs a parameter optimization framework that supports grid search and multi-dimensional parameter sweeps across user-defined parameter ranges, with the ability to evaluate backtest results for each parameter combination against an objective function (e.g., maximize Sharpe ratio, minimize drawdown). | Systematically searching for optimal strategy parameters (e.g., lookback windows, thresholds) is a core quant workflow. Manual iteration is slow and error-prone; an optimization framework accelerates research and ensures reproducibility. | Medium |
| SN-1.17 | The trader needs walk-forward analysis capability that divides historical data into rolling in-sample and out-of-sample windows, optimizes strategy parameters on each in-sample window, and validates on the corresponding out-of-sample window to assess strategy robustness and detect overfitting. | Strategies optimized on a single historical period frequently overfit and fail in live trading. Walk-forward analysis is the industry-standard technique for evaluating whether optimized parameters generalize to unseen data. | Medium |
| SN-1.18 | The trader needs an interactive research environment (Jupyter notebook integration) that provides access to the system's historical data, indicator library, and plotting capabilities, separate from the backtest engine, for exploratory data analysis and strategy prototyping. | A notebook environment enables rapid hypothesis testing, statistical analysis, and data visualization before committing to a full backtest. Both QuantConnect (QuantBook) and Quantopian provided this capability as a core part of the research workflow. | High |
| SN-1.19 | The trader needs the system to include a trading calendar that is aware of exchange holidays, early closes, pre-market and after-hours sessions, and time zones for all supported exchanges, so that scheduling primitives (e.g., "run at market open") resolve correctly on all trading days including half-days and around holidays. | Scheduling errors on holidays or early-close days can cause strategies to submit orders when markets are closed, miss scheduled executions, or misalign data windows. Both Zipline and QuantConnect maintain comprehensive trading calendars for this reason. | High |
| SN-1.20 | The trader needs a built-in technical indicator library that integrates existing open-source libraries (pandas-ta, TA-Lib) and exposes common indicators (SMA, EMA, RSI, MACD, Bollinger Bands, ATR, and others) through the strategy API, with support for incremental (streaming) computation compatible with the event-driven execution model. | Writing indicator calculations from scratch for every strategy is repetitive and error-prone. Leveraging pandas-ta and TA-Lib provides battle-tested, performant implementations. Incremental computation is necessary for live trading where indicators must update on each new bar without recomputing the full history. | High |
| SN-1.21 | The trader needs data consolidation and resampling capabilities that allow aggregation of tick or minute-resolution data into custom-period bars (e.g., 5-minute, 15-minute, hourly, daily) and non-standard bar types (e.g., Renko, range bars) within the strategy API. | Many trading strategies operate on timeframes that do not match the native resolution of the data feed. The system must allow the user to define custom bar periods without requiring pre-processed datasets for each timeframe. | Medium |
| SN-1.22 | The trader needs order event callbacks in the strategy API that notify user code when orders are filled, partially filled, cancelled, or rejected, including fill price, fill quantity, and commission details, so that strategies can react to execution events programmatically. | Strategies that manage position sizing, implement scaling logic, or track execution quality require real-time feedback on order status. Without order event callbacks, users must poll for order status, which introduces latency and complexity. | High |
| SN-1.23 | The trader needs a warm-up period mechanism that feeds historical data into the strategy before live or backtest execution begins, so that indicators, rolling windows, and internal state are fully initialized before the first trading signal is generated. | Without warm-up, indicators produce undefined or garbage values for the first N bars (e.g., a 200-day moving average has no value for the first 199 bars), leading to erroneous signals at strategy start. Both Zipline and QuantConnect provide warm-up capabilities for this reason. | High |
| SN-1.24 | The trader needs the system to support multi-leg options orders submitted as a single transaction, enabling execution of defined-risk strategies such as iron condors, straddles, strangles, vertical spreads, butterflies, and calendar spreads. | Multi-leg options strategies are fundamental to volatility and income-oriented trading. Submitting legs as a single transaction ensures simultaneous execution, avoids leg risk (one leg filling while the other does not), and receives a single net fill price from the exchange. | High |
| SN-1.25 | The platform shall maintain a Strategy Reservoir of at least 30 internally-simulated paper-trading strategies, each tracking positions and P&L independently using the internal simulation engine with shared live market data. The system shall produce a ranking based on risk-adjusted returns (e.g., Sharpe ratio, Sortino ratio) over a configurable evaluation window. The system must support a Hot-Swap capability to promote a paper strategy to the single live IB slot and simultaneously demote the current live strategy to paper simulation (swap model). Promotion may be triggered by: (a) manual user selection, (b) automatic promotion of the top-ranked strategy, or (c) automatic promotion of the strategy with the highest performance momentum over the evaluation window. Demotion shall liquidate all open IB positions before transitioning the strategy to paper simulation mode with a flat start. Promotion shall begin with no open IB positions (flat start). A configurable cool-down period (default: 7 days) shall prevent promotion/demotion oscillation. | A reservoir of internally-simulated paper strategies creates a continuous pipeline for strategy evaluation and selection under real market conditions without capital risk. Internal simulation (rather than IB paper trading) enables independent position tracking per strategy without the complexity of multi-strategy position attribution on IB's flat account model. Automated swap based on risk-adjusted performance or momentum reduces the lag between strategy validation and deployment, while drawdown-based demotion provides a systematic risk control mechanism that does not rely on manual intervention by the solo operator. The 30-strategy reservoir target reflects hardware resource constraints. | High |
| SN-1.26 | The system shall automatically ingest and persistently store US equity market data on a scheduled nightly batch basis: (a) daily OHLCV bars for all US equities (8,000+) from a designated bulk equity data provider (vendor TBD); (b) minute-bar OHLCV data from Interactive Brokers for a user-configurable watchlist of securities (default: securities with active strategies). The initial backfill shall retrieve the maximum available history from the bulk vendor (target: approximately 20 years daily, approximately 1 year minute). All stored data shall be retained indefinitely on the NAS (archival tier), with recent data cached on the local SSD (primary runtime tier). | A local historical data store is essential for backtesting, factor pipeline computation, indicator warm-up, and research notebook access. IB's pacing limits (60 requests per 10 minutes, yielding approximately 6,300 requests per overnight window) are insufficient for full-universe daily + minute ingestion (~16,000+ requests required). Sourcing daily bars from a bulk vendor eliminates the pacing constraint for the factor pipeline's primary input, while reserving IB's pacing budget for minute-bar watchlist and option chain capture. Indefinite retention builds a growing proprietary dataset. | High |
| SN-1.27 | The system shall automatically ingest and persistently store option chain data from Interactive Brokers for currently live (non-expired) contracts on a scheduled nightly basis, capturing at minimum: underlying, expiration, strike, right (call/put), bid, ask, last, volume, open interest, and implied volatility. Because IB does not provide historical data for expired option contracts, the system must capture and store this data before expiration to build a forward-growing historical options dataset. All stored options data shall be retained indefinitely on the NAS (archival tier), with recent data on the local SSD (primary runtime tier). | IB explicitly does not provide historical data for expired options — once a contract expires, its data is permanently unavailable from IB. A forward-capture model ensures that options data accumulated by the system over time becomes a proprietary historical dataset usable for backtesting and research. For deep historical options data predating system deployment, Databento (C-8) serves as the source. | High |
| SN-1.28 | The system shall provide the strategy API and research environment with unified access to stored historical data (equities and options), allowing strategies and notebooks to query by symbol, date range, and resolution without distinguishing between the original data source (bulk equity vendor, IB-ingested minute bars, Databento-imported historical options, Sharadar fundamental data, or user-uploaded Parquet). | A unified data access layer simplifies strategy code and research workflows by abstracting the underlying storage and source differences. Strategies should not need to know whether data came from the bulk vendor, IB minute-bar ingestion, a Databento import, Sharadar, or a user upload. | High |
| SN-1.29 | The trader needs an internal paper-trading simulation engine that enables Reservoir strategies to generate trading signals, receive simulated fills with configurable slippage and commission models, track virtual positions, and compute performance metrics using live market data — without submitting orders to or receiving fills from Interactive Brokers. The simulation engine must produce sufficiently realistic results that Reservoir rankings meaningfully predict live strategy performance. | Running 30+ strategies on IB's paper account introduces multi-strategy position attribution complexity on a flat account model. Internal simulation isolates each strategy's position tracking while consuming the same market data, enabling fair comparison across the Reservoir. | High |
| SN-1.30 | The trader needs the ability to promote a paper strategy to the live slot using one of three modes: (a) manual selection via the dashboard or API, (b) automatic promotion of the top-ranked strategy by risk-adjusted return, or (c) automatic promotion of the strategy with the highest performance momentum (defined as the rate of change of risk-adjusted return over the evaluation window). The trader needs to configure which mode is active and to override automatic promotion decisions. | Different market conditions may favor different selection criteria. Manual override ensures the trader retains ultimate control. Momentum-based selection captures strategies that are improving, not just strategies that have historically performed well. | High |

**Pain points they have today:**
- No unified platform to develop, backtest, and deploy strategies end-to-end
- Difficulty screening the full US equity universe without dedicated pipeline infrastructure
- Risk of deploying strategies without realistic cost modeling leading to live performance degradation
- Lack of real-time observability into system health and strategy performance during live trading
- No automated handling of corporate actions (splits, dividends, delistings) leading to corrupted backtests and live execution errors
- No interactive research environment for exploratory data analysis before committing to full backtests
- Manual re-implementation of common technical indicators in every strategy
- No systematic way to optimize strategy parameters or validate robustness against overfitting
- No way to run multiple candidate strategies simultaneously and select the best one for live trading

### 4.2 System Operator (Josh)

**Context:** Josh is also the sole operator and maintainer of the system.
The system must be operable by a single individual without dedicated
DevOps resources.

**Needs:**

| ID | Need | Rationale | Priority |
|----|------|-----------|----------|
| SN-2.01 | The operator needs a modern web-based dashboard to monitor strategy performance metrics (Sharpe ratio, Sortino ratio, alpha, beta, maximum drawdown, and others), system latency, and system health in real time. | A modern web-based interface allows monitoring from any device; centralized observability is critical for a solo operator. | High |
| SN-2.02 | The operator needs the dashboard to display logs generated by user strategy code, with a logging API that the user invokes from within their Python strategies. | Debug-level visibility into strategy behavior during live trading enables rapid diagnosis without SSH access to the host. | High |
| SN-2.03 | The operator needs continuous heartbeat monitoring (maximum 15-second staleness threshold) of market data feeds and broker API connections, displayed on the dashboard, to prevent trading on stale or missing data. | Trading decisions based on stale data can produce significant financial losses; heartbeat monitoring provides early warning. | High |
| SN-2.04 | The operator needs connectivity fail-safes that automatically detect when the IB API is unreachable or when market data is stale, and prevent order submission until connectivity is restored and data is fresh. | Submitting orders during an API outage or on stale data can result in unacknowledged or duplicate orders, or orders based on incorrect prices. | High |
| SN-2.05 | The operator needs the system to achieve 99.9% availability during US equity market hours (09:30–16:00 ET, Monday through Friday, excluding market holidays). | Downtime during trading hours directly translates to missed trading opportunities or unmanaged positions. | High |
| SN-2.06 | The operator needs the system to handle increased data loads (full US equity universe of 8,000+ securities, options chains, fundamental data) and operate without interruptions. | The strategy pipeline must scale to the entire investable universe without degrading execution or monitoring performance. | High |
| SN-2.07 | The operator needs the system to be deployable locally (initial phase) and via Docker container in a cloud environment (target state) with minimal reconfiguration. | Local deployment enables rapid development; containerized deployment enables reliability and remote access. | Medium |
| SN-2.08 | The operator needs the dashboard to display an account-level view showing the IB account's actual equity, P&L, margin usage, and buying power (reflecting the single live strategy), alongside an aggregate overview of all paper strategy performance from the Reservoir. | With one live strategy and 30+ paper strategies, the operator needs both a real account health view and a Reservoir overview to make informed promotion/demotion decisions. | High |

**Pain points they have today:**
- No centralized monitoring — must check multiple terminals and logs
- No automated detection of stale data or dropped connections
- Manual deployment processes with no containerization

### 4.3 Development Team

**Context:** The development team is responsible for implementing the
system. They require clear, testable requirements and an architecture
that supports modularity and future extensibility.

**Needs:**

| ID | Need | Rationale | Priority |
|----|------|-----------|----------|
| SN-3.01 | The development team needs a modular architecture with clearly defined interfaces between the strategy engine, data layer, execution layer, and brokerage adapters. | Modularity enables independent development, testing, and future replacement of components (e.g., adding a new brokerage). | High |
| SN-3.02 | The development team needs the brokerage integration layer to be abstracted such that adding a new brokerage requires implementing a defined adapter interface without modifying core engine code. | The stakeholder has stated that additional brokerages shall be supported in the future; the architecture must anticipate this. | Medium |
| SN-3.03 | The development team needs the data provider layer to support pluggable data sources with a vendor-agnostic interface for market data, historical data, and fundamental data. | The stakeholder requires IB for live equity/options data, Databento for historical options backtesting, and an undetermined fundamental data provider; a pluggable architecture accommodates all three and future additions. | High |
| SN-3.04 | The system architecture must support the addition of new asset classes (cryptocurrency, futures) and alternative data sources (prediction markets, sentiment analytics) through existing modular interfaces without requiring major rearchitecture. | The stakeholder has stated that crypto, futures, and alternative data support are future requirements; the initial architecture must not preclude these additions and must treat alternative data sources as first-class pluggable components. | Medium |

---

## 5. Constraints and assumptions

**Constraints** (hard limits that cannot be negotiated):

| ID | Constraint | Source | Impact on Design |
|----|-----------|--------|------------------|
| C-1 | The system shall use Python as the language for user-authored algorithmic trading strategies. | Stakeholder | Strategy engine must provide a Python API; execution environment must support Python runtime |
| C-2 | The initial brokerage integration shall be Interactive Brokers TWS API (https://www.interactivebrokers.com/campus/ibkr-api-page/twsapi-doc/). | Stakeholder | Execution layer must implement IB TWS API protocol; paper trading uses IB's built-in paper account for integration testing only |
| C-3 | The system shall operate within a personal/retail trading context; no institutional regulatory or compliance requirements apply. | Stakeholder | No audit trail, multi-user access control, or regulatory reporting modules are required |
| C-4 | User-uploaded backtest data shall be in Apache Parquet file format. | Stakeholder | Data ingestion layer must include a Parquet reader |
| C-5 | Infrastructure costs shall be minimized. A network-attached storage (NAS) device with 20 TB of available space is provided for archival data storage. A 1 TB local SSD on the Proxmox host is available for primary runtime storage. | Stakeholder + SyRS benchmark | Architecture shall use tiered storage: local SSD as primary runtime tier (ingestion writes, strategy reads, backtesting), NAS as archival tier (indefinite retention, cold reads). NAS I/O is insufficient for concurrent runtime use (validated by benchmark: 513 ms latency at 60 concurrent readers). |
| C-6 | The system shall be operable and maintainable by a single person (no dedicated DevOps team). | Stakeholder | System complexity, deployment procedures, and monitoring must be manageable by one individual |
| C-7 | Live data for equities and options shall be sourced from Interactive Brokers. | Stakeholder | Live data pipeline depends on IB market data subscriptions and is subject to IB pacing/rate limits |
| C-8 | Historical options data for backtesting shall be sourced from Databento. | Stakeholder | Backtest data pipeline must support Databento's data format and schema |
| C-9 | The built-in technical indicator library shall utilize existing open-source libraries (pandas-ta and TA-Lib) rather than custom implementations. | Stakeholder | Strategy engine must integrate pandas-ta and TA-Lib; deployment environment must include TA-Lib C library dependency |
| C-10 | Daily OHLCV equity data for the full US universe shall be sourced from a designated bulk equity data provider (vendor TBD; candidates: Databento, Polygon). Minute-bar data for the watchlist and live option chain data shall continue to be sourced from IB. | Stakeholder / SyRS analysis | Data layer must implement vendor-agnostic bulk equity provider interface; IB pacing budget reserved for minute bars and option chains |
| C-11 | Exactly one strategy shall execute against the IB live trading account at any given time. All other concurrent strategies shall execute against the internal simulation engine. | Stakeholder | Execution engine routes orders for the designated live strategy to IB; all other strategies' orders are handled by the internal simulation engine. Eliminates multi-strategy position attribution on IB's flat account model. |

**Assumptions** (things believed to be true but not yet validated):

| ID | Assumption | Risk if wrong |
|----|-----------|---------------|
| A-1 | Interactive Brokers TWS API provides sufficient market data bandwidth and historical data access to support live streaming data for active strategies and minute-bar watchlist ingestion within pacing limits. *(Note: IB is no longer the source for full-universe daily bars — this is handled by the bulk equity vendor per C-10.)* | If IB pacing limits constrain minute-bar watchlist size, the watchlist must be reduced or a supplemental vendor used for minute-bar data. |
| A-2 | The IB paper trading account provides a sufficiently realistic simulation environment for validating IB adapter integration logic before going live. *(Note: IB paper trading is used for integration testing only; Strategy Reservoir paper-trading uses the internal simulation engine per SN-1.29.)* | If paper trading behavior diverges significantly from live (e.g., unrealistic fills), integration testing coverage may be insufficient. |
| A-3 | A single Proxmox VM running a Docker Compose stack on the reference hardware (i5-12400, 32 GB RAM) can support 1 live + up to 59 paper strategy containers with acceptable performance. Paper containers are expected to consume fewer resources than live containers (~300 MB RAM vs. ~512 MB). Estimated total: ~16.5 GB RAM, well within the 32 GB ceiling. | If resource requirements exceed the reference hardware, either the container target must be reduced or a hardware upgrade / multi-node deployment is needed. |
| A-4 | Storing full options chain snapshots (Greeks, IV surface, all expirations) accelerates algorithm execution enough to justify the storage cost. | If pre-computed snapshots do not materially improve performance, storage requirements can be reduced by computing Greeks on demand. |
| A-5 | ~~The NAS is accessible with sufficient I/O throughput from the trading system host to support backtesting and factor pipeline workloads.~~ **Validated v0.5: NAS I/O insufficient for concurrent runtime use.** Benchmark results: 112 MB/s sequential read (adequate), 116 IOPS / 513 ms latency at 60 concurrent readers (inadequate). Architecture decision: SSD-primary runtime storage + NAS archival tier. | N/A — resolved. Tiered storage architecture adopted. |
| A-6 | The stakeholder has or will obtain the necessary IB market data subscriptions (US equities, US options) to receive live streaming data. | Without active subscriptions, the system cannot receive live data; strategy execution and heartbeat monitoring will not function. |
| A-7 | A vendor-agnostic fundamental data interface can be defined before the specific provider is selected. | If the chosen fundamental data provider has a highly idiosyncratic schema or delivery mechanism, the adapter may require rework. |
| A-8 | An open-source trading calendar library (e.g., exchange_calendars) provides sufficiently accurate and up-to-date holiday and early-close schedules for US exchanges. | If the calendar library is inaccurate or delayed in updating holiday schedules, the system may misfire scheduled events on holidays or early-close days; a manual override mechanism would be needed. |
| A-9 | Corporate action data (splits, dividends, delistings, mergers, symbol changes) is available through IB's API or a supplemental data source with sufficient history and timeliness for both backtesting, live trading, and internal paper-trading simulation. | If corporate action data is incomplete or delayed, backtests will contain price discontinuities and live strategies may trade on unadjusted data, producing incorrect position sizing. |
| A-10 | ~~IB's pacing limits (no more than 60 historical data requests per 10-minute period, no identical requests within 15 seconds) allow nightly batch ingestion of daily and minute bars for the full US equity universe within an acceptable overnight window.~~ **Validated v0.5: Assumption disproven.** IB pacing yields ~6,300 requests per overnight window vs. ~16,000+ required for full-universe daily + minute ingestion (2.5x shortfall). Resolved via Option D architecture: daily bars sourced from bulk vendor (C-10), IB pacing budget reserved for minute-bar watchlist and option chains. | N/A — resolved. IB pacing limits remain applicable to minute-bar and option chain ingestion only. |
| A-11 | IB provides sufficient intraday or end-of-session option chain data for currently live contracts to support the forward-capture model described in SN-1.27. | IB's documentation states that EOD data for options is unavailable; if real-time snapshots near close are also unreliable, a supplemental options data vendor may be needed for live chain capture. |
| A-12 | The designated bulk equity data provider (C-10) delivers complete, timely daily OHLCV data for the full US equity universe with sufficient reliability for nightly ingestion. | If the provider experiences extended outages, the system may fall back to IB for daily bar ingestion on a staggered rotation basis, subject to pacing limits — which would result in multi-day data staleness for some securities. |
| A-13 | The IB account's concurrent market data line limit (approximately 100 lines for the stakeholder's account tier) is sufficient for the aggregate universe of securities subscribed by all active strategies (1 live + N paper). | If the aggregate universe exceeds the line limit, the system must implement subscription rotation or priority queuing, which may introduce data latency for lower-priority paper strategies. |

---

## 6. Success criteria

The project is complete when all of the following are true:

- [ ] **SC-1:** A user-written Python strategy can be deployed to live trading through IB, executing market, limit, stop, and stop-limit orders, with order acknowledgement received within 1 second of signal generation. *(Traces to: SN-1.01, SN-1.06, SN-1.08)*
- [ ] **SC-2:** The backtesting engine produces performance reports (including Sharpe, Sortino, alpha, beta, max drawdown) using both system-stored data and user-uploaded Parquet files, with configurable date ranges, benchmark comparison (default SPY), and realistic IB-default cost/slippage modeling. *(Traces to: SN-1.02, SN-1.03, SN-1.04, SN-1.05, SN-1.13)*
- [ ] **SC-3:** The factor pipeline screens and computes factors for 8,000+ US equities according to user-defined scheduling primitives and completes within the scheduled window. *(Traces to: SN-2.06, BG-3)*
- [ ] **SC-4:** The web-based dashboard displays live performance metrics (including benchmark-relative), heartbeat status, strategy logs, system health, and an account-level view (equity, margin, buying power) with ≤5-second refresh latency. *(Traces to: SN-2.01, SN-2.02, SN-2.03, SN-2.08)*
- [ ] **SC-5:** The kill switch cancels all resting IB orders, submits liquidation orders for the live strategy's positions within 5 seconds of activation, halts all paper strategy simulations, and disconnects from the exchange (with a 30-second timeout for unfilled liquidation orders). The kill switch is accessible from dashboard, CLI, and REST API. *(Traces to: SN-1.11)*
- [ ] **SC-6:** The system achieves ≥99.9% measured availability during US equity market hours over a rolling 30-day period. *(Traces to: SN-2.05)*
- [ ] **SC-7:** 1 live strategy + 30 paper strategies run concurrently without degradation of order execution latency or dashboard responsiveness. *(Traces to: SN-1.10)*
- [ ] **SC-8:** A new brokerage adapter can be implemented and integrated without modifying core strategy engine, data layer, or execution engine code. *(Traces to: SN-3.01, SN-3.02, SN-3.04)*
- [ ] **SC-9:** Connectivity loss triggers user notification through at least two configured channels within 60 seconds of detection. *(Traces to: SN-1.12, SN-2.04)*
- [ ] **SC-10:** The system deploys and runs successfully both locally and as a Docker container in a cloud environment. *(Traces to: SN-2.07)*
- [ ] **SC-11:** The system automatically adjusts historical prices, open order quantities, and portfolio positions for stock splits, reverse splits, and dividends in live trading, backtesting, and internal paper simulation; backtests spanning corporate action dates produce correct P&L calculations. *(Traces to: SN-1.14, SN-1.15)*
- [ ] **SC-12:** The parameter optimization framework completes a grid search across a user-defined parameter space and returns ranked results by objective function within a reasonable time for the dataset size. *(Traces to: SN-1.16, SN-1.17)*
- [ ] **SC-13:** The Jupyter research environment can access the system's historical data, compute indicators via pandas-ta and TA-Lib, and render plots without requiring a running backtest or live strategy. *(Traces to: SN-1.18)*
- [ ] **SC-14:** Scheduling primitives correctly resolve market open, market close, and intraday intervals on exchange holidays, half-days, and across daylight saving time transitions. *(Traces to: SN-1.19)*
- [ ] **SC-15:** Built-in indicators (SMA, EMA, RSI, MACD, Bollinger Bands, ATR at minimum) produce values matching pandas-ta and TA-Lib reference outputs, and update incrementally on each new bar in live trading. *(Traces to: SN-1.20)*
- [ ] **SC-16:** A strategy can subscribe to minute-resolution data and receive consolidated 5-minute, 15-minute, and hourly bars via the strategy API without pre-processed datasets. *(Traces to: SN-1.21)*
- [ ] **SC-17:** Order event callbacks deliver fill price, fill quantity, and commission to user strategy code within 1 second of fill acknowledgement from the broker (live) or within 100 ms of simulated fill (paper). *(Traces to: SN-1.22, SN-1.29)*
- [ ] **SC-18:** A strategy with a 200-bar warm-up period begins generating trading signals on the first live/backtest bar with all indicators fully initialized. *(Traces to: SN-1.23)*
- [ ] **SC-19:** A multi-leg options order (e.g., iron condor with 4 legs) is submitted as a single transaction through the IB API, receives a single combined fill confirmation, and is reflected as a single composite position in the dashboard. *(Traces to: SN-1.24)*
- [ ] **SC-20:** The Strategy Reservoir maintains at least 30 concurrent internally-simulated paper strategies. The Reservoir produces a ranking over a configurable evaluation window. A Hot-Swap successfully promotes a selected paper strategy to the single live IB slot: the current live strategy's IB positions are liquidated, the strategy transitions to internal paper simulation, the promoted strategy begins live trading with a flat start, and a 7-day cool-down prevents re-swap. *(Traces to: SN-1.25, SN-1.29, SN-1.30)*
- [ ] **SC-21:** The system completes nightly batch ingestion of daily OHLCV bars for 8,000+ US equities from the bulk equity data provider and stores them on the local SSD (primary tier) with sync to NAS (archival tier); stored data is queryable by the strategy API and research environment the following morning. *(Traces to: SN-1.26, SN-1.28)*
- [ ] **SC-22:** The system captures and stores option chain data (bid, ask, last, volume, OI, IV at minimum) for live contracts nightly; data for a contract that subsequently expires remains available in the historical store indefinitely (NAS archival tier). *(Traces to: SN-1.27)*
- [ ] **SC-23:** A strategy or research notebook can query historical data by symbol, date range, and resolution and receives a unified result regardless of whether the underlying data was ingested from the bulk equity vendor, IB minute-bar ingestion, imported from Databento, sourced from Sharadar, or uploaded as Parquet. *(Traces to: SN-1.28)*
- [ ] **SC-24:** The internal simulation engine tracks virtual positions and P&L for 30 paper strategies consuming shared live IB market data. Simulated fill prices include configurable slippage and commission. After one month of operation, the Reservoir ranking produces a consistent ordering. A strategy's API behavior is identical whether running in live or paper mode — the same strategy code runs without modification in both modes. *(Traces to: SN-1.29, SN-1.30)*
- [ ] **SC-25:** The market data subscription manager consolidates subscriptions from all active strategies into a single deduplicated set of IB subscriptions, respecting the IB line limit. Multiple strategies subscribing to the same security each receive data with ≤100 ms additional latency relative to the IB feed. *(Traces to: SN-1.10, SN-1.29, A-13)*

---

## 7. Out of scope

The following are explicitly **not** part of this project:

- **OS-1:** High-frequency trading (HFT) infrastructure (sub-millisecond latency, co-location, FPGA-based execution). The system targets swing and position trading strategies.
- **OS-2:** Multi-user authentication, role-based access control, or multi-tenancy. The system is for a single personal/retail user.
- **OS-3:** Cryptocurrency and futures trading; integration of alternative data sources (prediction markets, sentiment analytics). Architecture shall accommodate future addition, but implementation of these asset classes and data sources is deferred.
- **OS-4:** Integration with brokerages other than Interactive Brokers. Architecture shall support future brokerage adapters, but only IB is implemented in this phase.
- **OS-5:** System-enforced risk limits (per-strategy, per-account, or per-position hard stops). Risk management logic is the responsibility of user-authored strategy code. The system provides a risk management API framework similar to QuantConnect's model, but enforcement is user-driven.
- **OS-6:** ~~Internal paper-trading simulation engine.~~ **Revised v0.6:** An internal paper-trading simulation engine is **in scope** for Strategy Reservoir strategies. IB's built-in paper trading account is retained for integration testing of the IB adapter only and is not used for Reservoir strategy evaluation.
- **OS-7:** Institutional regulatory compliance, audit logging, or reporting. The system operates in a personal/retail context.
- **OS-8:** Mobile application. The web-based dashboard and notification channels provide sufficient mobile access.

---

## 8. Traceability matrix

| Need ID | Stakeholder Need (Summary) | Business Goal(s) |
|---------|---------------------------|-------------------|
| SN-1.01 | Execute user-written Python strategies | BG-1, BG-5 |
| SN-1.02 | Backtest with stored or user-uploaded data | BG-2 |
| SN-1.03 | Realistic cost/slippage modeling in backtests | BG-2 |
| SN-1.04 | Benchmark comparison (default SPY) | BG-2, BG-4 |
| SN-1.05 | Factor analysis and tear-sheet capabilities | BG-2, BG-3 |
| SN-1.06 | Single live strategy on IB; all others on internal simulation; IB paper for integration testing | BG-1 |
| SN-1.07 | Single asset class per strategy; multi-asset data access | BG-1, BG-5 |
| SN-1.08 | Market, limit, stop, stop-limit order types | BG-1 |
| SN-1.09 | Scheduling primitives in strategy API | BG-1 |
| SN-1.10 | 1 live + up to 59 paper concurrent strategies (internal simulation) | BG-1, BG-6 |
| SN-1.11 | Kill switch — liquidate live IB positions, halt paper simulations, disconnect | BG-1, BG-4 |
| SN-1.12 | Connectivity notifications (email, SMS, dashboard alert — Phase 1) | BG-4 |
| SN-1.13 | UI-based backtest date selection | BG-2 |
| SN-1.14 | Automatic corporate actions handling (splits, dividends, delistings, mergers, symbol changes) | BG-2, BG-7 |
| SN-1.15 | Configurable data normalization modes (raw, split-adjusted, fully adjusted, total return) | BG-2, BG-7 |
| SN-1.16 | Parameter optimization framework (grid search, multi-dimensional sweeps) | BG-2, BG-7 |
| SN-1.17 | Walk-forward analysis for overfitting detection | BG-2, BG-7 |
| SN-1.18 | Interactive Jupyter research environment | BG-7 |
| SN-1.19 | Trading calendar with holiday, early-close, and time zone awareness | BG-1, BG-7 |
| SN-1.20 | Built-in indicator library (pandas-ta, TA-Lib) with incremental computation | BG-1, BG-7 |
| SN-1.21 | Data consolidation and resampling (custom bar periods, Renko, range bars) | BG-1, BG-2 |
| SN-1.22 | Order event callbacks (fill, partial fill, cancel, reject) | BG-1 |
| SN-1.23 | Warm-up period for indicator and state initialization | BG-1, BG-2, BG-7 |
| SN-1.24 | Multi-leg options orders as single transaction (iron condors, straddles, spreads, etc.) | BG-1 |
| SN-1.25 | Strategy Reservoir (30 internal-sim paper strategies, configurable ranking window, Hot-Swap swap model with 3 promotion modes) | BG-1, BG-2, BG-4 |
| SN-1.26 | Nightly batch ingestion of US equity data: daily bars from bulk vendor (full universe), minute bars from IB (watchlist); tiered SSD+NAS storage | BG-2, BG-3, BG-7 |
| SN-1.27 | Forward-capture of live option chain data from IB before expiration; indefinite retention | BG-2, BG-7 |
| SN-1.28 | Unified historical data access layer across bulk vendor, IB, Databento, Sharadar, and user-uploaded sources | BG-2, BG-5, BG-7 |
| SN-1.29 | Internal paper-trading simulation engine for Reservoir strategies | BG-1, BG-2 |
| SN-1.30 | Three promotion modes (manual, top-ranked, momentum) with operator override | BG-1, BG-2, BG-4 |
| SN-2.01 | Modern web-based performance dashboard | BG-4 |
| SN-2.02 | Strategy log display | BG-4 |
| SN-2.03 | Heartbeat monitoring (15-second threshold) | BG-4 |
| SN-2.04 | Connectivity fail-safes (IB unreachable and stale data) | BG-1, BG-4 |
| SN-2.05 | 99.9% market-hours availability | BG-6 |
| SN-2.06 | Scale to full US equity universe | BG-3 |
| SN-2.07 | Local + Docker cloud deployment | BG-6 |
| SN-2.08 | Account-level IB view + Reservoir aggregate overview on dashboard | BG-4 |
| SN-3.01 | Modular architecture | BG-5 |
| SN-3.02 | Pluggable brokerage adapters | BG-5 |
| SN-3.03 | Vendor-agnostic data provider interface | BG-5 |
| SN-3.04 | Extensible to new asset classes and alternative data sources (prediction markets, sentiment) | BG-5 |

---

## 9. Change log

| Version | Date | Author | Summary |
|---------|------|--------|---------|
| 0.1 | 2026-03-14 | Systems Engineer | Initial draft based on stakeholder elicitation sessions |
| 0.2 | 2026-03-14 | Systems Engineer | Added SN-1.14 through SN-1.23 (corporate actions, data normalization, parameter optimization, walk-forward analysis, research environment, trading calendar, built-in indicators via pandas-ta/TA-Lib, data consolidation, order event callbacks, warm-up period); added BG-7, C-9, A-8, A-9; added SC-11 through SC-18 |
| 0.3 | 2026-03-16 | Systems Engineer | Updated BG-1 latency to 1s, BG-3 to scheduling-based pipeline, BG-6 priority to Medium; updated SN-1.04 priority to Medium, SN-1.05 priority to High, SN-2.01 to "modern web-based", SN-3.04 to include alternative data sources; added SN-1.24 (multi-leg options), SN-1.25 (Strategy Reservoir with Hot-Swap); added SC-19, SC-20 |
| 0.4 | 2026-03-17 | Systems Engineer | Added SN-1.26 (equity data ingestion and storage), SN-1.27 (options chain forward-capture with IB expired-options limitation documented), SN-1.28 (unified historical data access layer); added A-10, A-11 for IB pacing and options data risks; added SC-21 through SC-23 |
| 0.5 | 2026-03-20 | Systems Engineer | Incorporated SyRS v0.4 peer review findings and architectural decisions. **Concurrency:** revised SN-1.10 from 50/100 to 60 (30 live + 30 paper) per hardware constraint analysis; revised SN-1.25 from 50 to 30 paper strategies. **Data architecture (Option D):** revised SN-1.26 to source daily OHLCV from bulk equity vendor (full universe) and minute bars from IB (watchlist only), resolving IB pacing infeasibility (A-10 validated: 2.5x shortfall); added C-10 (bulk equity data provider), A-12 (vendor reliability); updated SN-1.28 source list to include bulk vendor and Sharadar. **Storage:** updated C-5 to reflect tiered architecture (SSD primary + NAS archival) per NAS benchmark results (A-5 validated: 513 ms concurrent latency). **Kill switch:** revised SN-1.11 to remove "mobile notification action" (OS-8 excludes mobile app; dashboard accessible via mobile browser). **Notifications:** scoped SN-1.12 Phase 1 channels to email, SMS, and dashboard alert. **Hot-Swap:** added liquidate-before-demotion, flat-start promotion, and 7-day cool-down to SN-1.25. Updated SC-5, SC-7, SC-20, SC-21, SC-23 and traceability matrix accordingly. |
| 0.6 | 2026-03-22 | Systems Engineer | **Single-live-strategy architecture pivot.** Revised SN-1.06 from live/paper account selection to single-live-strategy model (exactly one strategy on IB at a time; all others on internal simulation). Revised SN-1.10 from 30 live + 30 paper to 1 live + up to 59 paper; revised A-3 resource estimate from ~31 GB to ~16.5 GB. Revised SN-1.25: Reservoir uses internal simulation (not IB paper), swap promotion model, three promotion modes (manual, top-ranked, momentum), configurable evaluation window. Revised SN-1.11 kill switch scope to single live strategy + paper simulation halt. Revised SN-1.14 to include internal simulation. Revised OS-6 to bring internal simulation engine in scope. Revised SN-2.04 to include stale data blocking. Added SN-1.29 (internal simulation engine), SN-1.30 (promotion criteria), SN-2.08 (account-level + Reservoir dashboard view), C-11 (single live strategy constraint), A-13 (IB market data line limit). Revised A-2 (IB paper = integration testing only). Added SC-24, SC-25; revised SC-4, SC-5, SC-7, SC-11, SC-17, SC-20. Updated traceability matrix. Added pain point for multi-strategy evaluation. |
