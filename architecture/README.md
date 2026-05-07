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
