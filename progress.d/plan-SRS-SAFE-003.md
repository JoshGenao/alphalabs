# SRS-SAFE-003 — Block live order submission when IB Gateway is unreachable

## Context

**Feature:** SRS-SAFE-003 (P1, safety) — "block live order submission when IB Gateway is
unreachable." AC: *During IB unreachable state, order submissions fail with
`CONNECTIVITY_BLOCKED` until reconnection and readiness checks pass.* Verification method:
**Fault injection**. SyRS: SYS-45, SYS-64, NFR-R2; StRS SN-2.04. Maps to error-handling row
**ERR-2** (`docs/SRS.md:291`).

**Why this change:** The connectivity *gate* itself was already built and flipped `passes:true`
by feature **ERR-2** — `ExecutionEngine::submit_live_order` (`crates/atp-execution/src/lib.rs:562`,
block arm at 638-659) rejects Live submissions with `CONNECTIVITY_BLOCKED` on
`ConnectivityState::{Unreachable,ScheduledRestartWindow}`, emits a `ConnectivityEvent`, requests a
reconnect, and never calls the broker. A comprehensive L7 domain test exists
(`crates/atp-execution/tests/err_2_connectivity_blocked.rs`, 4 tests). **But SRS-SAFE-003 was never
formally addressed as its own feature** (`feature_list.json` still `passes:false`, no session note),
and two gaps remain that this session closes:

1. **No proof through the production authority boundary.** ERR-2's test drives `submit_live_order`
   *directly* with a caller-supplied `StrategyMode::Live`, bypassing the `route_order` designation
   authority. The memory rule *"prove via route_order, not submit_live_order — designate a live
   strategy, count wire attempts, add a non-designated path"* is unmet for the connectivity block.
2. **No operator / fault-injection surface.** Nothing drives `CONNECTIVITY_BLOCKED` as an operator
   workflow. The only wiring scenario (`run_routing_scenario`,
   `crates/atp-orchestrator/src/order_routing_wiring.rs:630`) always wires `HealthyConnectivityFixture`
   (always `Connected`). SAFE-003 Step 2 explicitly asks for a "fault-injection workflow using mocked
   IB … CLI/API calls, and logs."

**Intended outcome:** land a fault-injection operator surface that proves the block end-to-end
through the real `dispatch_order → route_order → submit_live_order` authority chain (with a
wire-attempt witness), matching the established house pattern (ERR-001/EXE-002/SAFE-002 CLIs), and
integrate **`--mode serialized`** (passes stays false).

## Completeness classification: **serialized** (confirmed with operator)

Passes stays `false`. The end-to-end AC ("…until reconnection **and readiness checks pass**" against
a *real* IB unreachable signal) cannot be verified solo because:

- **No real `ConnectivityState` producer exists.** Every `impl BrokerageConnectivity` is a
  fixture/stub. The IB adapter only classifies per-operation errors as `ConnectivityBlocked`; nothing
  maps an IB disconnect (1100/2110) → `ConnectivityState::Unreachable`. That producer is the deferred
  SRS-EXE-006 / SRS-MD-005 / SRS-EXE-001 connectivity-runtime leg.
- **The "readiness checks pass" half is unenforced** — collapsed into the `Connected` state's prose
  doc and delegated to the unimplemented `BrokerageConnectivity` port. The real readiness machinery
  (SYS-76 `SubCheck` / `python/atp_reliability/restart.py`, `atp_readiness`) is a separate startup
  subsystem, not wired to the order gate (`progress.txt:230`: that enforcement point "DOES NOT EXIST
  in the repo yet").
- A live IB fault-injection e2e (kill gateway → `CONNECTIVITY_BLOCKED` → reconnect+readiness → resume)
  needs real IB and can't run alongside siblings.

This session proves the **gate** end-to-end through the production authority chain under fixture
fault-injection; the operator finishes the live/producer verification later.

## What I'll build

### 1. Wiring — `crates/atp-orchestrator/src/order_routing_wiring.rs` (additive only)
- `InjectableConnectivity { state, reconnects: Cell<u32> }` impl `BrokerageConnectivity` — returns a
  configured `ConnectivityState`, counts `request_reconnect()`. Copies the shape of the existing
  `HealthyConnectivityFixture` (line 481). Doc-comment states it stands in for the deferred real
  producer.
- `CollectingConnectivitySink::events(&self) -> Vec<ConnectivityEvent>` — additive accessor next to
  `recorded()` (line 516), so the scenario can read the event's `scheduled_restart`/`strategy_id`.
- `ConnectivityBlockEvidence` + `LiveConnectivityOutcome { Blocked{category,error_type,message} |
  RoutedThrough{broker_order_id} }` evidence structs. Evidence captures: injected state, designated
  strategy, live outcome, `ib_orders_created` (from `RecordingIbGateway::orders_created()` — the
  wire-attempt witness), `reconnects`, `events_recorded`, `event_scheduled_restart`,
  `event_strategy`, and the non-designated paper contrast's sim receipt.
- `run_connectivity_block_scenario(state: ConnectivityState) -> Result<ConnectivityBlockEvidence, String>`
  — modeled on `run_routing_scenario` (630-763). Designates one live strategy
  (`LiveDesignationConfirmation::from_operator` + `engine.designate`), routes the designated-live
  order through the **real** `engine.dispatch_order(...)` with `InjectableConnectivity` +
  `IbBrokerageBridge::new(RecordingIbGateway::new())` + `CollectingConnectivitySink` +
  `FreshMarketDataFixture` + `CollectingStaleDataSink`; then routes a **non-designated paper**
  contrast through the same fixtures (must return `Simulated`, proving a paper order never touches IB
  regardless of connectivity). Fail-loud on any authority drift.

### 2. Operator CLI — `crates/atp-orchestrator/src/bin/safe003_connectivity_block_cli.rs` (new)
Template: `crates/atp-orchestrator/src/bin/err001_broker_envelope_cli.rs` (subcommand dispatch,
`ExitCode` main, `--inject` non-vacuity device, labeled proof lines, terminal boolean claim + no
proof line / non-zero exit on any failure, fail-closed arg parsing).
- `prove-block [--state unreachable|scheduled-restart] [--inject connected]` — runs the block
  scenario; asserts `category==CONNECTIVITY_BLOCKED`, `error_type==IbGatewayUnreachable`,
  message traces the strategy + `SRS-SAFE-003`, `ib-orders-created==0`, `reconnects==1`, `events==1`,
  `scheduled_restart==(state==ScheduledRestartWindow)`, `event-strategy==designated`, contrast
  `sim-receipt` starts `paper-`. `--inject connected` overrides to `Connected` → block underivable →
  **fail closed** (non-vacuity, mirrors err001 `--inject accepted`).
- `routes-when-connected [--inject unreachable|scheduled-restart]` — affirmative positive control:
  `Connected` routes the same live order through (`ib-orders-created==1`, 0 reconnects, 0 events).
  `--inject <blocked>` → routes-through underivable → fail closed.
- Output: labeled `key:value` lines only (**no JSON**, so no C0-escape surface); every path prints
  `transports:FIXTURE` self-labeling; terminal `connectivity-block-proven:true` /
  `connectivity-routes-when-connected:true` printed only when every check holds.
- Fail-closed parsing: unknown subcommand/flag, missing flag value, `--state connected` on
  `prove-block`, wrong-class `--inject` → non-zero exit, no proof line.

### 3. `crates/atp-orchestrator/Cargo.toml` — `[[bin]]` registration for `safe003_connectivity_block_cli`
(after the err001 entry at lines 68-70), with a doc-comment stating FIXTURE transports + the deferred
real-producer caveat. No `[dependencies]` change.

## Tests

- **L4/L5 Rust wiring test** — `crates/atp-orchestrator/tests/srs_safe_003_connectivity_block_wiring.rs`
  (auto-discovered; model on `srs_exe_002_routing_wiring.rs`). Asserts: Unreachable → blocked,
  `ib_orders_created==0`, `reconnects==1`, `events_recorded==1`, `scheduled_restart==Some(false)`,
  `event_strategy==Some(live-alpha)`, sim receipt `paper-`; ScheduledRestartWindow → same but
  `scheduled_restart==Some(true)`; Connected → `RoutedThrough` + `ib_orders_created==1`, 0 reconnects,
  0 events; non-designated paper never touches IB in any state.
- **L6-style Rust CLI-process test** — `crates/atp-orchestrator/tests/srs_safe_003_connectivity_block_cli.rs`
  (model on `srs_err_001_broker_envelope_cli.rs`, via `env!("CARGO_BIN_EXE_safe003_connectivity_block_cli")`).
  Happy paths (prove-block unreachable + scheduled-restart, routes-when-connected), determinism
  (identical output across two processes), fail-closed non-vacuity (`--inject` opposite class), and
  fail-closed parsing (missing/unknown subcommand, unknown flag, missing/invalid values).
- **L7 mandatory domain test** — `tests/domain/test_safe003_connectivity_block_cli.py`
  (`pytest.mark.domain`/`safety`; model on `tests/domain/test_err001_broker_envelope.py` +
  `test_connectivity_blocked.py`). Shells `cargo test -p atp-orchestrator --test
  srs_safe_003_connectivity_block_cli <name> -- --exact` per behavioral case, plus a self-contained
  source scan asserting the CLI drives `dispatch_order` / `run_connectivity_block_scenario` /
  `transports:FIXTURE` and does **not** call `submit_live_order` directly.
  **Required** because the new CLI path matches the critic's `SAFETY_PATH_RE` (`connectivity`/`safe`)
  and the deterministic critic blocks a safety-path diff without a paired `tests/domain/*.py` diff.

## Constraints / do-not-touch
- **Do NOT edit the EXE-006 digest-pinned files**: `crates/atp-adapters/src/interactive_brokers.rs`,
  `crates/atp-adapters/src/interactive_brokers/wire.rs`,
  `crates/atp-adapters/tests/srs_exe_006_ib_adapter.rs` (SHA-256 pinned by
  `tools/ib_adapter_check.py`; editing flips closed-green SRS-EXE-006 red).
- No `feature_list.json` / `progress.txt` hand-edits (only the locked `integrate` mutates them).
- No new dependencies; all reuse in-crate fixtures. Do NOT extend `tools/error_handling_check.py`.
- Additive changes only to `order_routing_wiring.rs` (no behavior change to `run_routing_scenario`).

## Verification (end-to-end, per feature `steps[]`)
1. `./init.sh` → wait for "✓ Environment ready".
2. `cargo build -p atp-orchestrator --bin safe003_connectivity_block_cli`, then exercise the
   fault-injection workflow and capture per-command output for the session note:
   - `cargo run -q -p atp-orchestrator --bin safe003_connectivity_block_cli -- prove-block --state unreachable`
     → `connectivity-block-proven:true`, `ib-orders-created:0`, `reconnects:1`, `events:1`,
     `scheduled_restart:false`, `category:CONNECTIVITY_BLOCKED`.
   - `… prove-block --state scheduled-restart` → `scheduled_restart:true`.
   - `… routes-when-connected` → `connectivity-routes-when-connected:true`, `ib-orders-created:1`.
   - `… prove-block --inject connected` → non-zero exit, no proof line (non-vacuity).
   - fail-closed parsing cases → non-zero exit.
3. `cargo test -p atp-orchestrator --test srs_safe_003_connectivity_block_wiring` and
   `--test srs_safe_003_connectivity_block_cli` → all pass.
4. `pytest tests/domain/test_safe003_connectivity_block_cli.py -q` → passes (or skips if cargo absent).
5. Full gate before integrate: `cargo test --workspace`, `pytest -m "not integration and not e2e"`,
   `tools/run_ci_locally.sh`, `python3 tools/critic_check.py --staged`, and
   `python3 tools/adversarial_review.py origin/main` — both critic passes must APPROVE.
6. `python3 tools/agent_pool.py integrate "$ATP_FEATURE_ID" --mode serialized`.

## Resume pointer (for the session note)
Serialized. Remaining to flip `passes:true`: (a) the real IB-disconnect→`ConnectivityState::Unreachable`
producer (deferred SRS-EXE-006/SRS-MD-005/SRS-EXE-001 connectivity-runtime); (b) readiness-gate wiring
into the order path (SRS-MD-006/SRS-ARCH-005/ERR-9 `atp_readiness`); (c) a live IB fault-injection e2e.
