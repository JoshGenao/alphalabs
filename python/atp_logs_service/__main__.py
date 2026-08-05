"""``python -m atp_logs_service admin logs [...]`` — the composed log CLI.

``python -m atp_runtime`` constructs a BARE runtime, so every domain command
there returns the structured ``501`` naming its owner — that is deliberate, and
it is what ``kill-switch activate``, ``readiness wait``, and ``strategy
rollback`` all do too. A domain command only becomes real in a process that
COMPOSES it, and ``atp_runtime`` cannot do the composing itself without
importing its own consumers.

This entrypoint is that process for SRS-LOG-001: it builds a runtime, wires the
log surfaces from ``ATP_LOG_DIR``, and dispatches one invocation. It is the CLI
counterpart to ``python -m atp_dashboard``, which composes the same surfaces for
the REST route, the ``LOGS`` channel, and the dashboard pane.

``ATP_LOG_DIR`` is REQUIRED and there is no fallback directory: guessing where
an operator's audit trail lives and then reporting "no records" from the wrong
path would be worse than refusing. Unset, this exits ``USAGE_ERROR`` saying so.

Examples:
    ATP_LOG_DIR=/var/atp/logs python -m atp_logs_service admin logs --json
    ATP_LOG_DIR=/var/atp/logs python -m atp_logs_service admin logs \\
        --log-class strategy --severity WARN
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path

from atp_cli import ExitCode
from atp_runtime.runtime import OperatorInterfaceRuntime

from .wiring import wire_logs

#: Env knob naming the directory holding the separated stores. Mirrors the
#: dashboard's knob so one configuration serves every log surface.
LOG_DIR_ENV_KNOB = "ATP_LOG_DIR"

SYSTEM_STORE_FILENAME = "system.jsonl"
STRATEGY_STORE_FILENAME = "strategy.jsonl"


def main(argv: Sequence[str] | None = None) -> int:
    """Compose the log surfaces and dispatch one CLI invocation."""

    log_dir = os.environ.get(LOG_DIR_ENV_KNOB) or None
    if log_dir is None:
        print(  # noqa: T201 - operator-facing usage error
            f"{LOG_DIR_ENV_KNOB} is not set: point it at the directory holding "
            f"{SYSTEM_STORE_FILENAME} / {STRATEGY_STORE_FILENAME}. Refusing to guess "
            "an audit-trail location — reporting 'no records' from the wrong path "
            "would be worse than refusing.",
            file=sys.stderr,
        )
        return int(ExitCode.USAGE_ERROR)

    runtime = OperatorInterfaceRuntime()
    root = Path(log_dir)
    # The publisher is deliberately NOT started: a one-shot CLI invocation does
    # not publish a WebSocket channel, and claiming it would tell the runtime a
    # stream is running for the lifetime of one command.
    wire_logs(
        runtime,
        system_store_path=root / SYSTEM_STORE_FILENAME,
        strategy_store_path=root / STRATEGY_STORE_FILENAME,
    )
    return runtime.cli_dispatcher().dispatch(argv)


if __name__ == "__main__":
    raise SystemExit(main())
