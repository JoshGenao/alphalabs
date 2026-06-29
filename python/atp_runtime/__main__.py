"""Real operator CLI entrypoint: ``python -m atp_runtime <group> <command>``.

This is the *working* CLI. ``python -m atp_cli`` is the declarative API-4
contract surface — it introspects the command set and returns
``NOT_IMPLEMENTED`` — and it cannot dispatch through this runtime without a
circular import (``atp_runtime`` imports ``atp_cli``). So the runtime exposes
its own module entrypoint, which constructs an
:class:`~atp_runtime.runtime.OperatorInterfaceRuntime` and routes the invocation
through its handler registry: runtime-owned commands (``admin version``,
``readiness check``, ``admin config``) return real data and exit codes, and
domain commands return the structured deferred envelope mapped to an exit code.

Examples:
    python -m atp_runtime --list
    python -m atp_runtime admin version --json
    python -m atp_runtime readiness check --json     # exits NOT_READY while not ready
    python -m atp_runtime kill-switch activate        # exits CONFIRMATION_REQUIRED
"""

from __future__ import annotations

from collections.abc import Sequence

from .runtime import OperatorInterfaceRuntime


def main(argv: Sequence[str] | None = None) -> int:
    """Construct the runtime and dispatch one CLI invocation; return the exit code."""

    return OperatorInterfaceRuntime().cli_dispatcher().dispatch(argv)


if __name__ == "__main__":
    raise SystemExit(main())
