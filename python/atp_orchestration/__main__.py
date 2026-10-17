"""``python -m atp_orchestration live <promote|show>`` — the composed live-designation CLI.

``python -m atp_runtime`` builds a BARE runtime, so ``live promote`` and ``live show``
there return the structured ``501`` naming SRS-EXE-001; ``atp_runtime`` cannot compose
its own consumers. This entrypoint is the process that composes them, as
``python -m atp_logs_service`` does for the log commands and ``python -m atp_dashboard``
does for the REST routes.

``ATP_HOT_SWAP_DESIGNATION_STATE`` is REQUIRED and has no fallback: it is the one
durable snapshot the live execution host re-reads on every order and the Hot-Swap
moves. Designating a strategy into a guessed file would report success for a strategy
the host never sees. Unset, this exits ``USAGE_ERROR`` saying so.
``ATP_LIVE_DESIGNATION_BINARY`` overrides where ``exe001_live_designation_cli`` lives.

Examples:
    ATP_HOT_SWAP_DESIGNATION_STATE=/var/atp/designation python -m atp_orchestration \\
        live show --json
    ATP_HOT_SWAP_DESIGNATION_STATE=/var/atp/designation python -m atp_orchestration \\
        live promote live-a --confirm
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from atp_cli import ExitCode
from atp_runtime import OperatorInterfaceRuntime

from .live_designation import mount_live_designation

#: The shared designation snapshot (the same knob the dashboard and Hot-Swap read).
DESIGNATION_STATE_ENV_KNOB = "ATP_HOT_SWAP_DESIGNATION_STATE"


def main(argv: Sequence[str] | None = None) -> int:
    """Compose the live-designation commands and dispatch one CLI invocation."""

    state = os.environ.get(DESIGNATION_STATE_ENV_KNOB) or None
    if state is None:
        print(  # noqa: T201 - operator-facing usage error
            f"{DESIGNATION_STATE_ENV_KNOB} is not set: point it at the durable live-designation "
            "snapshot the live execution host and the Hot-Swap read. Refusing to guess: a "
            "designation written anywhere else would never reach the host.",
            file=sys.stderr,
        )
        return int(ExitCode.USAGE_ERROR)
    runtime = OperatorInterfaceRuntime()
    mount_live_designation(runtime, state_path=state)
    return runtime.cli_dispatcher().dispatch(argv)


if __name__ == "__main__":
    raise SystemExit(main())
