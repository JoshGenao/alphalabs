"""Register the real SRS-MD-006 readiness handlers on the operator runtime.

Mirrors ``atp_safety.wiring``: every collaborator is a REQUIRED keyword-only
argument with no default and no fixture fallback — the composer owns the
honesty of each choice (fixture probes for the feature's own verification
context today; live probe sources as their producers land). A bare
runtime — one no composer wired — keeps serving the structured deferred
``501`` for ``readiness wait``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from atp_reliability.restart import SubCheckResult
from atp_runtime.registry import OperationKey, Surface
from atp_runtime.runtime import OperatorInterfaceRuntime

from .gate import GateState, ReadinessGate
from .handlers import ReadinessWaitHandler
from .runtime import AlertSink, evaluate_runtime_readiness

__all__ = ["READINESS_OPERATIONS", "wire_readiness"]

#: The SDK-pinned operation this package makes real.
READINESS_OPERATIONS: tuple[OperationKey, ...] = (OperationKey(Surface.CLI, "readiness wait"),)


def wire_readiness(
    runtime: OperatorInterfaceRuntime,
    *,
    gate: ReadinessGate,
    env: Mapping[str, str],
    collect_results: Callable[[], Sequence[SubCheckResult]],
    alert_sink: AlertSink,
    now_ns: Callable[[], int],
    atp_env: str | None = None,
    poll_interval_s: float = 1.0,
) -> None:
    """Bind the ``readiness wait`` handler to ``runtime``.

    Args:
        runtime: The operator-interface runtime to register on.
        gate: The seeded pre-trade gate (``ReadinessGate.from_env``).
        env: The environment the static half re-validates on every poll.
        collect_results: Produces the five SYS-76 sub-check results for one
            evaluation — the composed probe set (fixture or live). REQUIRED,
            no default.
        alert_sink: The operator-alert dispatch sink. REQUIRED, no default —
            SYS-76 alerts must have somewhere real to go.
        now_ns: The composition's clock (stamped onto failures/alerts).
    """

    def _evaluate() -> GateState:
        return evaluate_runtime_readiness(
            gate,
            env,
            collect_results(),
            alert_sink=alert_sink,
            timestamp_ns=now_ns(),
            atp_env=atp_env,
        )

    handler = ReadinessWaitHandler(gate=gate, evaluate=_evaluate, poll_interval_s=poll_interval_s)
    runtime.registry.register(OperationKey(Surface.CLI, "readiness wait"), handler)
