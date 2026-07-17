"""The SRS-MD-006 ``readiness wait`` operator handler.

SYS-76's launch clause — "the live strategy container shall not be started
until the readiness check passes" — needs a consumer of the runtime gate.
This handler makes the SDK-pinned CLI command ``readiness wait``
(``("readiness", "wait") → SRS-MD-006`` in the runtime contract) real: it
re-evaluates BOTH readiness halves (static config + the SYS-76 runtime
probes) on every poll until the gate is ``READY`` / ``OVERRIDDEN`` or the
``--timeout`` budget elapses. This is the ADVISORY blocking surface: a
launch orchestration composed as ``atp readiness wait && atp strategy
start --live ...`` cannot start live work against a held gate, but a
launch path that skips it is not yet blocked — full in-process enforcement
inside the strategy-container/live-designation path is the runtime
contract's named deferred SRS-ORCH-004/SRS-EXE-001 consultation.

Timeout returns HTTP 504, which the CLI dispatcher maps onto the command's
documented ``TIMEOUT`` exit code; a held gate at the deadline is reported
with the full failure payload — never a bare error.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from atp_runtime.registry import HandlerResult, Request

from .errors import PreTradeHoldError
from .gate import GateState, ReadinessGate

__all__ = ["ReadinessWaitHandler"]

#: How long the poll loop sleeps between evaluations (seconds). Injectable
#: so tests never sleep for real.
_DEFAULT_POLL_INTERVAL_S = 1.0


class ReadinessWaitHandler:
    """Registered for ``readiness wait`` (CLI).

    ``evaluate`` is the composition's full readiness evaluation — typically
    a closure over :func:`atp_readiness.runtime.evaluate_runtime_readiness`
    with the composed gate, probes, alert sink, and clock. It must RAISE
    ``PreTradeHoldError`` when the gate holds and return the gate state when
    ready; this handler owns only the wait/timeout loop.
    """

    def __init__(
        self,
        *,
        gate: ReadinessGate,
        evaluate: Callable[[], GateState],
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._gate = gate
        self._evaluate = evaluate
        self._poll_interval_s = poll_interval_s
        self._monotonic = monotonic
        self._sleep = sleep

    def handle(self, request: Request) -> HandlerResult:
        raw_timeout = request.query.get("timeout", "60")
        try:
            timeout_s = float(raw_timeout)
        except (TypeError, ValueError):
            return HandlerResult(400, {"error": f"unparseable --timeout {raw_timeout!r}"})
        if timeout_s < 0:
            return HandlerResult(400, {"error": f"--timeout must be >= 0; got {raw_timeout!r}"})

        deadline = self._monotonic() + timeout_s
        last_payload: dict[str, object] | None = None
        while True:
            try:
                state = self._evaluate()
                return HandlerResult(
                    200,
                    {
                        "ready": True,
                        "state": str(state),
                        "readiness": self._gate.as_dashboard_payload(),
                        "srs_ref": "SRS-MD-006",
                    },
                )
            except PreTradeHoldError:
                # The evaluation already dispatched the per-failure operator
                # alerts; capture the full payload for the timeout report.
                last_payload = self._gate.as_dashboard_payload()
            if self._monotonic() >= deadline:
                return HandlerResult(
                    504,
                    {
                        "ready": False,
                        "timeout_seconds": timeout_s,
                        "readiness": last_payload,
                        "srs_ref": "SRS-MD-006",
                    },
                )
            self._sleep(self._poll_interval_s)
