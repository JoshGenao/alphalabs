"""Register the real kill-switch handlers on the operator runtime.

``backend`` is a REQUIRED keyword-only argument with no default and no
fixture fallback: a capability that cannot safely run must not be reachable
(uncovered capability → no public surface). A bare
:class:`~atp_runtime.runtime.OperatorInterfaceRuntime` — one no composer
wired — keeps serving the structured deferred ``501`` for every kill-switch
operation. The composer decides which backend is honest for its context:
:class:`~atp_safety.backend.RustCliKillSwitchBackend` (the mocked-IB fixture
CLI, per the feature's own verification Step 2) today; the live-IB backend
when SRS-EXE-006's transport lands.
"""

from __future__ import annotations

from pathlib import Path

from atp_logging.persistence import JsonlLogStore
from atp_runtime.registry import OperationKey, Surface
from atp_runtime.runtime import OperatorInterfaceRuntime

from .backend import KillSwitchBackend
from .handlers import KillSwitchActivateHandler, KillSwitchStatusHandler

#: The three SDK-pinned kill-switch operations this package makes real.
KILL_SWITCH_OPERATIONS: tuple[OperationKey, ...] = (
    OperationKey(Surface.REST, "POST /api/v1/kill-switch"),
    OperationKey(Surface.CLI, "kill-switch activate"),
    OperationKey(Surface.CLI, "kill-switch status"),
)


def wire_kill_switch(
    runtime: OperatorInterfaceRuntime,
    *,
    backend: KillSwitchBackend,
    system_log_store: JsonlLogStore,
    state_dir: Path,
) -> None:
    """Bind the kill-switch handlers to ``runtime``.

    Args:
        runtime: The operator-interface runtime to register on.
        backend: The activation executor — REQUIRED, no default. The caller
            owns the honesty of this choice.
        system_log_store: The durable SRS-LOG-001 SYSTEM store the
            ``ACTIVATION`` + ``HALTED`` records are written to.
        state_dir: Existing directory for the durable last-activation
            (replay-guard) record.
    """

    state_dir = Path(state_dir)
    if not state_dir.is_dir():
        raise FileNotFoundError(f"kill-switch state directory does not exist: {state_dir}")
    activate = KillSwitchActivateHandler(
        backend=backend, system_log_store=system_log_store, state_dir=state_dir
    )
    status = KillSwitchStatusHandler(state_dir=state_dir)
    registry = runtime.registry
    registry.register(OperationKey(Surface.REST, "POST /api/v1/kill-switch"), activate)
    registry.register(OperationKey(Surface.CLI, "kill-switch activate"), activate)
    registry.register(OperationKey(Surface.CLI, "kill-switch status"), status)
