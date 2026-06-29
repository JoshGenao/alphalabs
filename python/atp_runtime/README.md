# `atp_runtime` — operator-interface runtime (HTTP + WebSocket + CLI)

This package is the **runtime** that binds the declarative operator contract —
[`atp_api`](../atp_api) (REST), [`atp_cli`](../atp_cli) (CLI), and
[`atp_ws`](../atp_ws) (WebSocket) — to handlers. It is the
`operator-interface-runtime` named in
`architecture/runtime_services.json#operator_workflow_surface_contract.deferred`,
traced to `SRS-API-001` (§5.8 / §7 API-2 / API-3 / API-4).

It is implemented in the **standard library only** (no web framework
dependency): `http.server` for REST, a minimal RFC 6455 codec for WebSocket,
and `argparse` (reused from `atp_cli`) for the CLI dispatcher. The contract
block is explicit that the runtime is framework-agnostic; this is the first
concrete binding.

## What is real vs deferred

The runtime serves the **full** documented surface from day one:

* **Runtime-owned (real responses):** the operations that describe the runtime
  itself and need no downstream feature —
  * `GET /api/v1/system/status` / `atp readiness check` — the runtime's own
    liveness + a per-workflow served/deferred map. A workflow is `fully_served`
    only when **every** one of its REST and CLI operations has a real handler,
    and `ready` is **false** while any operation of any required workflow is
    deferred — registering one operation of a multi-operation workflow does not
    flip it ready (domain readiness — IB / SSD / NAS / ingestion freshness — is
    owned by `SRS-MD-006`, not overstated here);
  * `atp admin version`, `atp admin config` (config schema, secrets redacted —
    `SRS-SEC-001`);
  * meta endpoints `GET /` (discovery), `GET /openapi.json` (the documented
    contract), `GET /healthz`.
* **Domain operations (deferred):** kill switch, live designation, lifecycle,
  Hot-Swap, ranking, backtests, watchlist, logs, alerts. Each is reachable and
  documented, but resolves to a structured `501 HANDLER_DEFERRED` envelope
  naming its owning feature and **performs no side effect**. Owners register
  real handlers on `HandlerRegistry` as they land — the route/command/channel
  shapes are frozen, so no renegotiation is needed.

## Interface invariants (independent of any domain feature)

| Invariant | Enforced by | Trace |
|---|---|---|
| Loopback / RFC 1918 bind only; a public bind fails closed | `assert_bind_allowed`, `LoopbackHTTPServer` | SRS-SEC-002 |
| Confirmation guard: handler never reached without a token | `Dispatcher` (REST 428), `CliDispatcher` (exit 3) | UI-4 / SRS-SAFE-001 |
| Structured interface errors (400/404/405/428/501) | `InterfaceError` | SRS-ERR-001 (shape) |
| Config view never emits a secret value | `ConfigHandler` redaction marker | SRS-SEC-001 |
| Surfaces never import each other / a vendor SDK / a core engine | top-layer-only imports | ARCH dependency direction |

## Usage

```python
from atp_runtime import OperatorInterfaceRuntime

runtime = OperatorInterfaceRuntime()

# In-process REST dispatch (no socket):
status, body = runtime.dispatch_rest("GET", "/api/v1/system/status")

# A live loopback HTTP + WebSocket server on an ephemeral port:
host, port = runtime.start(host="127.0.0.1", port=0)
# ... curl http://host:port/api/v1/system/status ; ws://host:port/ws/v1 ...
runtime.stop()

# CLI dispatch in-process:
exit_code = runtime.cli_dispatcher().dispatch(["admin", "version", "--json"])
```

The *working* operator CLI is `python -m atp_runtime <group> <command>` (it
dispatches through this runtime). `python -m atp_cli` is the declarative API-4
contract surface and intentionally returns `NOT_IMPLEMENTED`.

```bash
python -m atp_runtime admin version --json     # runtime metadata, exit 0
python -m atp_runtime readiness check --json    # status body, exit NOT_READY (5)
python -m atp_runtime kill-switch activate       # exit CONFIRMATION_REQUIRED (3)

# A downstream feature registers a real handler onto the frozen contract:
from atp_runtime import OperationKey, Surface
runtime.registry.register(OperationKey(Surface.REST, "POST /api/v1/kill-switch"), my_handler)
```

## Verification

* `tools/operator_interface_runtime_check.py` — runtime contract evidence
  (serves every documented operation, enforces every invariant, defers every
  domain operation to a named owner). Wired into `init.sh`, `ci.yml`, and
  `tools/run_ci_locally.sh`.
* `tests/boundary/test_operator_interface_runtime_wiring.py` — live loopback
  HTTP + WebSocket round-trips and CLI dispatch.
* `tests/domain/test_operator_interface_runtime.py` — safety invariants
  (bind policy, confirmation guard, deferred-handler inertness).
* `tests/test_operator_interface_runtime_contract.py` — L3 contract coverage.

## Status

`SRS-API-001` stays **`passes:false`**: this lands the operator-interface
runtime substrate, but the eight operator *workflows* are only end-to-end once
their domain handlers (`SRS-EXE-001`, `SRS-ORCH-004/005`, `SRS-RESV-002/003`,
`SRS-BT-001`, `SRS-DATA-002`, `SRS-LOG-001`, `SRS-NOTIF-001`) register real
behaviour on the registry. The deferred owners are listed in the contract block
and surfaced live at `GET /api/v1/system/status`.
