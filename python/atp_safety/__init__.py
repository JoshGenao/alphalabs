"""SRS-SAFE-001 kill-switch operator surfaces (SyRS SYS-44a; NFR-P3; SN-1.11).

The Python half of the kill-switch activation runtime: a fail-closed
subprocess bridge to the Rust activation CLI
(``safe001_kill_switch_cli`` — the REAL execution-gate + paper-engine-fleet
sequence over the mocked-IB fixture transport the feature's own verification
Step 2 prescribes), the REST/CLI handlers that make the SDK-pinned
kill-switch operations real on the SRS-API-001 operator runtime, the durable
SRS-LOG-001 ``ACTIVATION`` + ``HALTED`` audit writes (1-second observability
budget, measured), and the durable last-activation record that makes a
repeat activation an idempotent replay.

Honest scope (``kill_switch_activation_contract``): the LIVE path — a real
SRS-EXE-006 IB transport behind the brokerage port, live SRS-EXE-001/005
state producers, SRS-EXE-002 hosting of real paper strategies — is deferred,
so SRS-SAFE-001 stays ``passes:false`` (serialized). ``wire_kill_switch``
therefore takes its backend EXPLICITLY: there is no default and no implicit
fixture fallback, and a bare :class:`atp_runtime.OperatorInterfaceRuntime`
keeps serving the structured deferred ``501`` (uncovered capability → no
public surface).
"""

from .backend import (
    ActivationOutcome,
    KillSwitchBackend,
    KillSwitchBackendError,
    RustCliKillSwitchBackend,
)
from .handlers import KillSwitchActivateHandler, KillSwitchStatusHandler
from .state import load_last_activation, persist_last_activation
from .timeout import (
    LiquidationTimeoutAuditError,
    LiquidationTimeoutBackend,
    LiquidationTimeoutBackendError,
    LiquidationTimeoutOutcome,
    RustCliLiquidationTimeoutBackend,
    resolve_liquidation_timeout,
)
from .wiring import wire_kill_switch

__all__ = [
    "ActivationOutcome",
    "KillSwitchBackend",
    "KillSwitchBackendError",
    "KillSwitchActivateHandler",
    "KillSwitchStatusHandler",
    "LiquidationTimeoutAuditError",
    "LiquidationTimeoutBackend",
    "LiquidationTimeoutBackendError",
    "LiquidationTimeoutOutcome",
    "RustCliKillSwitchBackend",
    "RustCliLiquidationTimeoutBackend",
    "resolve_liquidation_timeout",
    "load_last_activation",
    "persist_last_activation",
    "wire_kill_switch",
]
