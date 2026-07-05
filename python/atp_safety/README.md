# atp_safety — SRS-SAFE-001 kill-switch operator surfaces

The Python half of the kill-switch activation runtime (SyRS SYS-44a; NFR-P3;
StRS SN-1.11). The Rust half — the `atp-execution` activation gate, the
`atp-simulation` `PaperEngineFleet` fan-out, and the orchestrator's
`safe001_kill_switch_cli` — is pinned by
`architecture/runtime_services.json#kill_switch_activation_contract` and
`tools/kill_switch_check.py`.

| Module | Role |
|---|---|
| `backend.py` | `KillSwitchBackend` protocol + `RustCliKillSwitchBackend`, the fail-closed subprocess bridge (repo convention: subprocess → cargo-built Rust binary) |
| `handlers.py` | `KillSwitchActivateHandler` (REST `POST /api/v1/kill-switch` + CLI `kill-switch activate`) and `KillSwitchStatusHandler` (`kill-switch status`) |
| `audit.py` | The SRS-LOG-001 `ACTIVATION` + `HALTED` SYSTEM records (correlated by activation id) |
| `state.py` | Durable last-activation record (scratch + fsync + atomic rename + dir fsync) — the replay guard |
| `wiring.py` | `wire_kill_switch(runtime, *, backend, system_log_store, state_dir)` |

## Wiring (the composer owns the honesty of the backend choice)

```python
from pathlib import Path

from atp_logging import LogClass
from atp_logging.persistence import JsonlLogStore
from atp_runtime import OperatorInterfaceRuntime
from atp_safety import RustCliKillSwitchBackend, wire_kill_switch

runtime = OperatorInterfaceRuntime()
wire_kill_switch(
    runtime,
    backend=RustCliKillSwitchBackend(),          # explicit — no default exists
    system_log_store=JsonlLogStore("data/logs/system.jsonl", log_class=LogClass.SYSTEM),
    state_dir=Path("data/state"),
)
```

- `backend` is a **required keyword-only argument**: a bare runtime nobody
  composed keeps serving the structured deferred `501` for every kill-switch
  operation (uncovered capability → no public surface).
- `RustCliKillSwitchBackend` shells `target/debug/safe001_kill_switch_cli`,
  which drives the REAL activation gate over a REAL `LiveExecutionState` +
  REAL paper-engine fleet with the **mocked-IB fixture transport** — exactly
  the verification vehicle SRS-SAFE-001's own Step 2 prescribes. The live IB
  transport behind the gate's brokerage port is the deferred SRS-EXE-006
  adapter.

## Semantics

- **Confirmation:** enforced at the transport (REST 428 / CLI exit 3) and
  re-checked in the handler (defense in depth).
- **Replay guard:** the durable last-activation record is consulted before
  the backend fires and armed before the audit writes — a repeat
  `kill-switch activate` replays the persisted response (same
  `activation_id`, no second backend call) instead of re-running the
  liquidate sequence. Corrupt state fails closed
  (`KILL_SWITCH_STATE_CORRUPT`), never "never activated".
- **Fail-closed backend:** missing binary / non-runnable exit / unparseable
  or incomplete report → `KILL_SWITCH_BACKEND_UNAVAILABLE` (500); a hung
  activation → `TimeoutError` → 504 → CLI exit `TIMEOUT`. Never
  success-shaped.
- **Observability (SRS-LOG-001):** every activation writes an `ACTIVATION`
  (CRITICAL) and a `HALTED` (WARN) SYSTEM record durably, correlated by
  activation id; the measured activation→durable-HALTED-write latency is
  recorded against the 1-second budget and surfaced by `kill-switch status`.
  A failed audit write is surfaced (`KILL_SWITCH_AUDIT_WRITE_FAILED`) — the
  sequence already ran and is replay-guarded, so a retry replays.
- **Response shape:** exactly the SDK-pinned `response_fields`
  (`activation_id`, `activated_at`, `cancelled_orders`, `liquidation_orders`,
  `paper_engines_halted`, `ib_gateway_disconnected`). The OpenAPI snapshot's
  placeholder value types are superseded by these concrete shapes per the
  route's own note ("concrete request and response schemas land with the
  downstream feature that owns the handler").

## Honest scope

SRS-SAFE-001 stays `passes:false` (serialized). What flips it is the live
path, enumerated in `kill_switch_activation_contract.deferred[]`: the real
SRS-EXE-006 IB transport, live SRS-EXE-001/005 execution-state producers,
SRS-EXE-002 hosting of real paper strategies on fleet-registered gates,
SRS-NOTIF-001 notifications, and the UI-4 rich dashboard control.
