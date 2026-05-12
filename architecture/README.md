# Architecture Boundary

`SRS-ARCH-001` is enforced by keeping core ATP runtime services in Rust crates
under `crates/` and exposing user-authored strategy interfaces from the Python
package under `python/atp_strategy`.

The objective source of truth for the boundary is
`architecture/runtime_services.json`. The automated check in
`tools/architecture_check.py` verifies that every declared core service has a
Rust crate manifest and Rust source, that those service directories contain no
Python implementation files, that container configuration points core services
at the Rust runtime image, and that the Python package exposes the Strategy API.

`SRS-ARCH-002` is enforced by the same metadata file's
`dependency_direction` block. `tools/dependency_boundary_check.py` validates
the allowed internal Cargo dependency graph and scans lower-layer Rust crates
for forbidden dashboard, orchestrator, and vendor-adapter imports. The check
can be run directly:

```bash
python3 tools/dependency_boundary_check.py
```

The negative fixtures prove the check fails for the required boundary
violations:

```bash
python3 tools/dependency_boundary_check.py --fixture lower-layer-orchestrator-import
python3 tools/dependency_boundary_check.py --fixture lower-layer-vendor-adapter-import
python3 tools/dependency_boundary_check.py --fixture lower-layer-dashboard-import
```

`SRS-ARCH-003` is enforced by the same metadata file's `adapter_isolation`
block. `crates/atp-adapters` owns the public brokerage and data-provider
interfaces plus compile-only stubs for Interactive Brokers, Databento, Sharadar,
user Parquet, and a future provider. `tools/adapter_isolation_check.py` verifies
the interface surface, compiles the adapter crate, scans core crates for vendor
imports, and compiles a temporary fictional alternative-data adapter without
modifying core source files:

```bash
python3 tools/adapter_isolation_check.py
```

The negative fixtures prove core modules cannot import vendor SDKs directly:

```bash
python3 tools/adapter_isolation_check.py --fixture core-imports-ib
python3 tools/adapter_isolation_check.py --fixture core-imports-databento
python3 tools/adapter_isolation_check.py --fixture core-imports-sharadar
```

`SRS-ARCH-004` is enforced by the `deployment` block in
`architecture/runtime_services.json`. `tools/deployment_check.py` reads
`docker-compose.yml`, `.env.example`, and `docs/DEPLOYMENT.md` and
asserts that the Phase 1 stack declares the required services
(orchestrator, execution, strategy and simulation engines, market data,
data layer, factor pipeline, notifications, dashboard/API, Jupyter, IB
Gateway, strategy runtime), passes the required environment variables,
mounts the SSD primary tier and NAS archive tier, binds the dashboard
to loopback, ships every Phase 1 Dockerfile, and documents that cloud
VPS deployment is a future target with explicit portability constraints.

```bash
python3 tools/deployment_check.py
```

The negative fixtures prove the check fails when a required deployment
artefact regresses:

```bash
python3 tools/deployment_check.py --fixture missing-jupyter
python3 tools/deployment_check.py --fixture missing-ssd
python3 tools/deployment_check.py --fixture missing-portability-doc
```

`SRS-ARCH-005` is enforced by the top-level `configuration` block in
`architecture/runtime_services.json` and the `python/atp_config` package
that consumes it. The catalogue documents 16 required keys across six
categories — credentials, storage paths, IB account settings,
market-data line limits, resource limits, and notification channels —
each with type, validator, default, secret flag, and SRS trace.
`tools/config_check.py` runs `atp_config.load_and_validate` against the
process env layered over `.env.example` defaults, verifies that
`.env.example` documents every catalogued key, and emits structured
readiness failures (`{key, category, severity, reason, srs_trace}`) on
stderr when a key is missing or invalid. Placeholder secrets are
warnings in development and hard errors when `ATP_ENV` is `staging` or
`production`:

```bash
python3 tools/config_check.py
```

The negative fixtures prove that each failure mode produces a
structured readiness failure:

```bash
python3 tools/config_check.py --fixture missing-credential
python3 tools/config_check.py --fixture placeholder-secret-in-production
python3 tools/config_check.py --fixture invalid-line-limit
python3 tools/config_check.py --fixture missing-resource-limit
python3 tools/config_check.py --fixture invalid-storage-path
```

`API-3` (WebSocket API) is enforced by the `websocket_api` block in
`architecture/runtime_services.json` and the `python/atp_ws` package.
The catalogue declares 8 event channels (PNL, METRICS, ACCOUNT_STATUS,
HEARTBEAT, LOGS, ALERTS, RESERVOIR_RANKING, STRATEGY_STATE), the
SUBSCRIBE/UNSUBSCRIBE/HEARTBEAT control plane, and a frozen AsyncAPI 2.6
snapshot at `python/atp_ws/asyncapi.json`. `tools/websocket_api_check.py`
validates per-channel SRS traces and payload fields, the
`NFR-P2 ≤ 5 s` refresh budget, the AsyncAPI snapshot byte-equality, and
the `SRS-SEC-002` loopback / single-user policy:

```bash
python3 tools/websocket_api_check.py
python3 tools/websocket_api_check.py --update   # regenerate snapshot
```

The contract is parallel to API-2 (`atp_api`); concrete WebSocket
publishers land with downstream features (EXE-1, ORCH-1, MD-1, RESV-1,
LOG-1, NOTIF-1).

`API-4` (operator CLI) is enforced by the `cli` block in
`architecture/runtime_services.json` and the `python/atp_cli` package.
The catalogue declares 6 command groups (`kill-switch`, `strategy`,
`live`, `hot-swap`, `readiness`, `admin`), 18 commands, the
`local-shell` access model, the four irreversible commands that must
require `--confirm` (`kill-switch activate`, `strategy rollback`,
`live promote`, `hot-swap trigger`), and a frozen JSON manual snapshot
at `python/atp_cli/manual.json`. `tools/cli_check.py` validates the
per-group SRS traces and command coverage, the confirmation invariant,
the documented exit-code contract, and exercises
`python -m atp_cli` end-to-end (listing, confirmation gating, and
the `NOT_IMPLEMENTED` stub):

```bash
python3 tools/cli_check.py
python3 tools/cli_check.py --update   # regenerate manual snapshot
```

The contract is parallel to API-2 (`atp_api`) and API-3 (`atp_ws`);
concrete CLI handlers land with downstream features (EXE-1, ORCH-1,
RESV-1, LOG-1, NOTIF-1).

`API-5` (brokerage adapter interface) is enforced by the
`adapter_contract` block in `architecture/runtime_services.json` and
the public traits in `crates/atp-adapters/src/lib.rs`. The catalogue
declares the required methods on `BrokerageAdapter` (`submit_order`,
`cancel_order`, `account_status`, `positions`), `MarketDataAdapter`
(`subscribe_market_data`), and `HistoricalDataAdapter`
(`historical_data`), plus a versioned capability discovery surface:
`AdapterVersion { adapter_version, protocol_version, protocol_label }`,
exposed by a default `AdapterBoundary::version()` method and overridden
by `InteractiveBrokersAdapter` to document the supported IB TWS API
version (`INTERACTIVE_BROKERS_TWS_API_VERSION = "10.45"` — the latest
IB TWS API stable release per SRS-EXE-007 / SyRS SYS-65).
`tools/adapter_check.py` parses the Rust source for the required trait
methods and version metadata, asserts the IB protocol-version constant
matches the configuration block, and runs `cargo test -p atp-adapters
--lib` end-to-end:

```bash
python3 tools/adapter_check.py
```

To bump the documented IB TWS API version, change
`INTERACTIVE_BROKERS_TWS_API_VERSION` in `crates/atp-adapters/src/lib.rs`
and the matching `interactive_brokers.protocol_version` value in
`architecture/runtime_services.json`; the contract check refuses to pass
unless the two agree.

The contract is parallel to API-2/API-3/API-4; concrete brokerage
behaviour lands with downstream features (EXE-1 live order routing,
EXE-2 watchdog/outbox reconciliation, MD-1 market-data subscription
manager, IB-1 IB Gateway integration tests).

`API-6` (data provider interface) is enforced by the
`data_provider_contract` block in `architecture/runtime_services.json`
and the public traits in `crates/atp-adapters/src/lib.rs`. The catalogue
declares the required methods on `BulkEquityDataProvider`
(`download_full_universe_daily`, `initial_historical_backfill`,
`incremental_nightly_update`), `FundamentalDataProvider`
(`ingest_fundamentals`), `OptionsDataProvider` (`import_options`),
`UserParquetDataProvider` (`import_user_parquet`), and
`AlternativeDataProvider` (`fetch_alternative_data`). Every Phase 1
provider (`DatabentoAdapter`, `SharadarAdapter`, `UserParquetAdapter`,
`FutureStubProvider`) implements the shared `DataProviderAdapter` base
trait so the data layer can route through one trait family. The block's
`capability_traces` array binds the six API-6 description capabilities
(bulk equity download, historical backfill, incremental update,
fundamentals ingestion, options import, user Parquet import) to a
specific `(trait, method, srs_ref)` triple, and
`unified_historical_query` ties SRS-DATA-007 to the existing
`HistoricalDataAdapter::historical_data` method shared with API-5.

```bash
python3 tools/data_provider_check.py
```

`tools/data_provider_check.py` parses the Rust source for every
required trait method, verifies each declared `impl <Trait> for
<Provider>` block exists, asserts every API-6 capability resolves to a
real `(trait, method)` pair with an SRS reference inside
`data_provider_contract.srs_refs`, and runs `cargo test -p atp-adapters
--lib` end-to-end (which exercises the per-method
`NotConfigured`-return surface). The `architecture_check.py` path
short-circuits the cargo step via
`assert_data_provider_contract_static`, mirroring the API-5 split.

The contract is parallel to API-5; concrete data behaviour lands with
downstream features (DATA-1 Databento daily ingestion, DATA-2 IB
minute watchlist, DATA-3 bulk backfill, DATA-4 IB option-chain
captures, DATA-5 Sharadar fundamentals, DATA-6 options DBN/Parquet,
DATA-7 unified historical access).

`API-7` (unified historical data interface) is enforced by the
`unified_historical_data` block in
`architecture/runtime_services.json`, the public types in
`crates/atp-adapters/src/lib.rs`, and the `HistoricalData` Protocol in
`python/atp_strategy/api.py`. The catalogue extends the
`HistoricalDataAdapter::historical_data` query so a single call
expresses every API-7 description capability:

- `HistoricalDataRequest { symbol, start, end, resolution, asset_class,
  normalization_mode }` carries the six description fields.
- `AssetClass { Equity, Option, Future, Etf, Index }` types the
  Phase 1 SRS-DATA-007 universe.
- `NormalizationMode { Raw, SplitAdjusted, FullyAdjusted, TotalReturn }`
  enumerates the four SRS-DATA-012 normalization modes; options
  strategies request `Raw`, indicator pipelines request `SplitAdjusted`
  or `FullyAdjusted`, benchmarking workloads request `TotalReturn`.
- `HistoricalQueryResult { symbol, asset_class, normalization_mode,
  bars }` is the source-neutral envelope returned by every provider —
  the contract refuses any `provider`, `vendor`, `source`,
  `source_provider`, or `data_source` field on the envelope.
- The Python `HistoricalData.get_bars` Protocol accepts the same
  `asset_class` + `normalization` keyword arguments and `atp_strategy`
  re-exports `NormalizationMode` so strategies, backtests, factor jobs,
  and notebooks share one query surface.

```bash
python3 tools/historical_data_check.py
```

`tools/historical_data_check.py` parses the Rust source for the request
struct, the source-neutral envelope (asserting no forbidden vendor
field is present), the asset-class and normalization-mode enums, the
trait return type, and the Python Protocol parameters, then runs
`cargo test -p atp-adapters --lib` end-to-end. The
`architecture_check.py` path short-circuits the cargo step via
`assert_unified_historical_data_static`, mirroring the API-5 / API-6
split.

The contract is parallel to API-5 / API-6; concrete unified historical
behaviour (cold NAS reads, corporate-action adjustment, schema
evolution) lands with DATA-7 and DATA-8..DATA-21.

`ERR-1` (live-path rejection for non-live submissions, SRS-EXE-001 +
SRS-ERR-001 + SyRS SYS-1 / SYS-64 / AC-15) is enforced by the
`error_handling_contract` block in
`architecture/runtime_services.json`, the public types in
`crates/atp-types/src/lib.rs`, and `ExecutionEngine::submit_live_order`
in `crates/atp-execution/src/lib.rs`. The catalogue declares the
structured-error vocabulary every later ERR-* and SAFE-* feature
reuses:

- `StrategyMode { Live, Paper }` types the single-live-strategy
  designation. Exactly one strategy may be `Live`; everything else is
  `Paper` and must never route to IB (AC-15).
- `OrderErrorCategory` carries the seven SyRS SYS-64 categories —
  `InvalidSymbol`, `InsufficientBuyingPower`, `ConnectivityBlocked`,
  `RateLimited`, `MarketDataStale`, `SubscriptionLimitReached`, and
  `NonLiveStrategySubmission` — each mapped to its upper-snake wire
  string via `as_str()` so the form is identical across Rust, Python,
  REST, and WebSocket surfaces.
- `StructuredOrderError { category, error_type, message,
  original_order }` is the SRS-ERR-001 envelope — exactly four fields,
  with the check refusing any `broker`, `ib_order_id`, `vendor`, or
  `provider` leak.
- `ExecutionEngine::submit_live_order(mode, submission, broker)`
  routes to the brokerage port ONLY inside the `StrategyMode::Live`
  match arm. Paper submissions return
  `Err(StructuredOrderError { category: NonLiveStrategySubmission, .. })`
  synchronously, with zero broker invocations. The
  `crates/atp-execution/tests/err_1_no_ib_side_effect.rs` integration
  test pins this with a spy adapter that counts every `submit_order`
  call.

```bash
python3 tools/error_handling_check.py
```

`tools/error_handling_check.py` parses the Rust source for the
`StrategyMode` + `OrderErrorCategory` enums (and their SyRS wire
strings), the `StructuredOrderError` struct (rejecting forbidden
broker/vendor fields), the `submit_live_order` signature, and the
match-arm gating that keeps `broker.submit_order` exclusively on the
`Live` path, then runs `cargo test -p atp-execution --lib` plus the
`err_1_no_ib_side_effect` integration test end-to-end. The
`architecture_check.py` path short-circuits the cargo step via
`assert_error_handling_static`, mirroring the API-5 / API-6 / API-7
split.

The contract lands the rejection vocabulary; the live IB routing
pipeline, idempotency / correlation-ID handling, and per-category
adapter-error mapping arrive with later EXE-* and ERR-2..ERR-9
features.

`ERR-2` (live-path connectivity gate, SRS-SAFE-003 + SRS-MD-005 +
SyRS SYS-45 / SYS-46 / NFR-R2) is enforced by the
`connectivity_contract` block in
`architecture/runtime_services.json`, additional types in
`crates/atp-types/src/lib.rs`, and a nested match in
`ExecutionEngine::submit_live_order` inside
`crates/atp-execution/src/lib.rs`. The catalogue declares the
connectivity-safety vocabulary the live execution path consults on
every submission:

- `ConnectivityState { Connected, Unreachable, ScheduledRestartWindow }`
  types the IB-Gateway readiness state. `Unreachable` is the
  SRS-SAFE-003 connectivity-loss path; `ScheduledRestartWindow` is the
  SRS-MD-005 daily-restart suspension window.
- `ConnectivityEvent { state, strategy_id, symbol, scheduled_restart }`
  is the structured payload the engine publishes whenever it blocks a
  live submission. `scheduled_restart` is true iff the state is
  `ScheduledRestartWindow`, so notification dispatchers / dashboards
  can apply SRS-MD-005's suppression rule without re-inspecting the
  enum. The contract refuses any `broker`, `ib_session_id`, `vendor`,
  or `provider` leak on the event payload.
- `BrokerageConnectivity { state, request_reconnect }` is the port the
  execution engine consults at every live submission. The
  implementation (later: the IB adapter wired by the orchestrator)
  owns the actual TCP probe / readiness check / restart-window
  detection. Keeping the port at the execution layer preserves the
  SRS-ARCH-002 dependency direction.
- `ConnectivityEventSink { record }` is the publication channel for
  the structured event; concrete sinks route it to logs, the dashboard
  WebSocket (`ALERTS` / `ACCOUNT_STATUS` channels), and the
  notification dispatcher (SRS-NOTIF-001).
- Inside the `StrategyMode::Live` arm of `submit_live_order`, a nested
  match on `connectivity.state()` routes `Connected` to
  `broker.submit_order(...)` and routes `Unreachable` /
  `ScheduledRestartWindow` to a synchronous rejection that emits
  `OrderErrorCategory::ConnectivityBlocked` (wire string
  `CONNECTIVITY_BLOCKED`), records a `ConnectivityEvent`, and calls
  `connectivity.request_reconnect()` — all with zero broker
  invocations. The
  `crates/atp-execution/tests/err_2_connectivity_blocked.rs`
  integration test pins this with spy implementations of all three
  ports.

```bash
python3 tools/connectivity_check.py
```

`tools/connectivity_check.py` parses the Rust source for the
`ConnectivityState` enum and the `ConnectivityEvent` struct (rejecting
forbidden broker/session/vendor fields), the two port traits, and the
match-arm gating that keeps `broker.submit_order` exclusively on the
`Connected` sub-arm of the `Live` match. It then runs
`cargo test -p atp-execution --lib` plus the
`err_2_connectivity_blocked` integration test end-to-end. The
`architecture_check.py` path short-circuits the cargo step via
`assert_connectivity_static`, mirroring the API-5 / API-6 / API-7 /
ERR-1 split.

ERR-2 lands the connectivity gate at the execution layer; the
production IB-Gateway TCP probe / readiness check / daily-restart
detection lands with later EXE-* and IB-adapter features. The
notification dispatcher fan-out (email + SMS within 60 s,
SRS-NOTIF-001) and the dashboard WebSocket subscription that surfaces
the `ConnectivityEvent` to operators arrive with NOTIF-1 and UI-*
features respectively.

`ERR-3` (live-path market-data freshness gate, SRS-MD-004 + SyRS
SYS-39a / SYS-64 / SYS-87 / NFR-P5) is enforced by the
`freshness_contract` block in `architecture/runtime_services.json`,
additional types in `crates/atp-types/src/lib.rs`, and a nested match
inside the `Connected` sub-arm of `ExecutionEngine::submit_live_order`
in `crates/atp-execution/src/lib.rs`. The catalogue declares the
freshness-safety vocabulary the live execution path consults on every
submission once connectivity is confirmed:

- `MarketDataFreshness { Fresh, Stale }` types whether subscribed
  market data for the order's symbol is within the NFR-P5 15-second
  heartbeat staleness threshold. `Stale` is the SRS-MD-004 /
  SyRS SYS-39a path; live submissions must be rejected with
  `MARKET_DATA_STALE` until fresh data is observed.
- `StaleDataEvent { state, strategy_id, symbol, staleness_seconds }`
  is the structured payload the engine publishes whenever it blocks a
  submission for staleness. `staleness_seconds` carries the observed
  age so dashboards and the notification dispatcher can surface how
  stale the feed actually got before the gate fired. The contract
  refuses any `broker`, `ib_session_id`, `vendor`, `provider`, or
  `tick_id` leak on the event payload — staleness is a data-side
  condition, not a transport / vendor one.
- `MarketDataFreshnessProbe { freshness, staleness_seconds }` is the
  port the execution engine consults at every Live + Connected
  submission. The implementation (later: the market-data subscription
  manager) owns the heartbeat timestamps, sequence-gap tracking, and
  the configurable threshold. Keeping the port at the execution layer
  preserves the SRS-ARCH-002 dependency direction.
- `StaleDataEventSink { record }` is the publication channel for the
  structured event; concrete sinks route it to logs (SRS-LOG-001), the
  dashboard WebSocket, and the notification dispatcher.
- Inside the `StrategyMode::Live` arm of `submit_live_order`, the
  freshness match is **nested inside** the `ConnectivityState::Connected`
  sub-arm — meaning the ERR-2 connectivity gate short-circuits ahead of
  the ERR-3 freshness gate (you cannot meaningfully measure freshness
  if disconnected). The inner match routes `Fresh` to
  `broker.submit_order(...)` and routes `Stale` to a synchronous
  rejection that emits `OrderErrorCategory::MarketDataStale` (wire
  string `MARKET_DATA_STALE`) and records a `StaleDataEvent` via
  `stale_events.record(...)`. The `Stale` leaf does NOT call
  `connectivity.request_reconnect()` — staleness is a data-side
  condition, not a transport fault. The
  `crates/atp-execution/tests/err_3_stale_data_blocked.rs` integration
  test pins this with spy implementations of all five ports (broker,
  connectivity, connectivity-event sink, freshness probe, stale-event
  sink) and a `ForbiddenFreshness` panic stub on the Unreachable
  branch to prove the nested-match short-circuit.

```bash
python3 tools/freshness_check.py
```

`tools/freshness_check.py` parses the Rust source for the
`MarketDataFreshness` enum and the `StaleDataEvent` struct (rejecting
forbidden broker/session/vendor/tick fields), the two port traits, and
the match-arm gating that keeps `broker.submit_order` exclusively on
the `Fresh` leaf of the freshness match inside the `Connected`
sub-arm. It then runs `cargo test -p atp-execution --lib` plus the
`err_3_stale_data_blocked` integration test end-to-end. The
`architecture_check.py` path short-circuits the cargo step via
`assert_freshness_static`, mirroring the API-5 / API-6 / API-7 /
ERR-1 / ERR-2 split.

ERR-3 lands the live-path freshness gate at the execution layer.
**ERR-3b (deferred):** SRS-MD-004 also requires the internal simulation
engine to reject paper-on-sim submissions with `MARKET_DATA_STALE`. The
simulation engine in `crates/atp-simulation/src/lib.rs` currently has
no submission entry point; the next sub-task will introduce
`InternalSimulationEngine::submit_simulated_order` with the same
freshness port and event sink, completing the SRS-MD-004 paper leg.
The production market-data subscription manager (SRS-MD-007 sequence
gap detection, SRS-MD-003 continuous heartbeat monitoring) is a future
caller that will implement `MarketDataFreshnessProbe` and publish
through `StaleDataEventSink`. The dashboard WebSocket subscription
that surfaces `StaleDataEvent` to operators arrives with UI-*
features.

`ERR-4` (market-data subscription line-limit gate, SRS-MD-002 +
SyRS SYS-70 / SYS-64 + StRS A-13) is enforced by the
`subscription_limit_contract` block in
`architecture/runtime_services.json`, additional types in
`crates/atp-types/src/lib.rs`, and a match-on-state gate inside
`MarketDataSubscriptionManager::request_subscription` in
`crates/atp-market-data/src/lib.rs`. The catalogue declares the
subscription-limit vocabulary the market-data subscription manager
consults on every new subscription request:

- `SubscriptionRequest { strategy_id, symbol }` is the source-neutral
  request envelope the manager gates on — deliberately mirroring
  `OrderSubmission` minus the `quantity` field (a subscription has
  no order semantics).
- `SubscriptionLimitState { WithinLimit, ExceededLimit }` types
  whether admitting the request would push past the operator-configured
  IB market-data line ceiling. `ExceededLimit` is the SRS-MD-002 /
  SyRS SYS-70 path; the request must be rejected with
  `SUBSCRIPTION_LIMIT_REACHED` and an operator alert raised.
- `SubscriptionLimitEvent { state, strategy_id, symbol, current_lines,
  configured_limit }` is the structured payload the manager publishes
  whenever it rejects a request. Carrying BOTH `current_lines` and
  `configured_limit` closes a TOCTOU window: the configured value can
  be re-read between rejection and dashboard render, so the event
  must be self-describing. The contract refuses any `broker`,
  `ib_session_id`, `vendor`, `provider`, or `tick_id` leak on the
  event payload.
- `StructuredSubscriptionError { category, error_type, message,
  original_request }` is the rejection envelope. It reuses the
  pre-declared `OrderErrorCategory::SubscriptionLimitReached` variant
  (wire string `SUBSCRIPTION_LIMIT_REACHED`) as the single source of
  truth for the SyRS SYS-64 vocabulary. A category-pinned factory
  `StructuredSubscriptionError::limit_reached(...)` is the only
  blessed construction site.
- `SubscriptionLineCounter { lines_in_use, line_limit, try_acquire }`
  is the port the manager consults on every request. `try_acquire`
  is read-only with respect to the registry — admission happens
  after the manager observes `WithinLimit`. The eventual concrete
  implementation (deferred to SRS-MD-001 / SRS-MD-007) will own the
  consolidated subscription set and the operator-configured
  `ATP_MARKET_DATA_LINE_LIMIT` value (default 100, range 1–10000,
  catalogued under `market_data_limits` in the configuration block).
- `SubscriptionLimitEventSink { record }` is the publication channel
  for the structured event; concrete sinks route it to logs
  (SRS-LOG-001), the dashboard WebSocket alert pane (SyRS SYS-70's
  "alert the operator on the dashboard" clause), and the
  notification dispatcher.
- The match on `counter.try_acquire(&request)` routes `WithinLimit`
  to a `SubscriptionAccepted` envelope and routes `ExceededLimit`
  to a synchronous rejection that emits the rejection envelope AND
  records a `SubscriptionLimitEvent` via `events.record(...)`. The
  `ExceededLimit` leaf must NOT mutate the subscription registry —
  the `forbidden_mutations` field in the contract block enumerates
  the call sites the static gate refuses (`registry.insert`,
  `subscriptions.insert`, `request.acquire`, `counter.acquire`,
  `counter.commit`, `self.register`). The
  `crates/atp-market-data/tests/err_4_subscription_limit_blocked.rs`
  integration test pins this with `LineCounterSpy` /
  `EventSinkSpy` / `ForbiddenSink` stubs plus an explicit SyRS
  SYS-64 invariant test that proves the rejection envelope is
  byte-identical for live and paper subscribers (the manager API
  takes no `StrategyMode` parameter).

```bash
python3 tools/subscription_limit_check.py
```

`tools/subscription_limit_check.py` parses the Rust source for the
`SubscriptionLimitState` enum, the `SubscriptionLimitEvent` struct
(rejecting forbidden broker/session/vendor/tick fields), the
`SubscriptionRequest` request envelope, the two port traits, and the
match-arm gating that keeps `SubscriptionAccepted` exclusively on the
`WithinLimit` leaf and `OrderErrorCategory::SubscriptionLimitReached`
plus `events.record(...)` exclusively on the `ExceededLimit` leaf
(with the zero-registry-mutation invariant enforced via the
contract's `forbidden_mutations` list). It then runs
`cargo test -p atp-market-data --lib` plus the
`err_4_subscription_limit_blocked` integration test end-to-end. The
`architecture_check.py` path short-circuits the cargo step via
`assert_subscription_limit_static`, mirroring the ERR-1 / ERR-2 /
ERR-3 split.

ERR-4 lands the subscription-manager line-limit gate.
**ERR-4b (deferred):** when an order is placed against a symbol with
no existing subscription, `submit_live_order` should acquire a line
through the subscription manager and bubble `SUBSCRIPTION_LIMIT_REACHED`
up through the existing `StructuredOrderError` envelope (which is why
`OrderErrorCategory::SubscriptionLimitReached` is pre-declared in
`atp-types`). This adds another port to `submit_live_order`'s
signature and is tracked as a fresh sub-task. The concrete
`SubscriptionLineCounter` implementation backed by
`ATP_MARKET_DATA_LINE_LIMIT` and the live subscription set is owned
by SRS-MD-001 (subscription consolidation) and SRS-MD-007 (sequence-
gap detection). The dashboard WebSocket subscription that surfaces
`SubscriptionLimitEvent` to operators arrives with UI-* features.
