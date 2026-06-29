"""Dispatch the declared CLI commands through the runtime handler registry.

:mod:`atp_cli` owns the *declarative* command surface (API-4) and a runner that
prints a ``NOT_IMPLEMENTED`` contract note — it is intentionally inert so the
API-4 snapshot stays stable. This module is the runtime's CLI *dispatcher*: it
reuses ``atp_cli``'s parser and command table but routes a parsed invocation to
the same :class:`~atp_runtime.registry.HandlerRegistry` the REST/WS surfaces
use, so ``atp readiness check`` and ``atp admin version`` return real data while
domain commands return the structured deferred envelope mapped to an exit code.

It does **not** modify ``atp_cli`` (keeping API-4 green); it composes with it.

SRS trace
---------
``SRS-API-001`` (CLI operator surface, API-4), ``SRS-SAFE-001`` (the
``--confirm`` guard is enforced before any handler runs).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from typing import IO

from atp_cli import COMMANDS, ExitCode, Group, build_parser, find_command
from atp_cli.runner import _print_command_listing

from .contract import cli_owner
from .registry import (
    DeferredHandler,
    HandlerRegistry,
    OperationKey,
    Request,
    Surface,
    invoke_handler,
)

# Interface-error HTTP status -> CLI exit code. Mirrors the documented
# ExitCode semantics in atp_cli.commands.
_STATUS_TO_EXIT: dict[int, ExitCode] = {
    200: ExitCode.OK,
    400: ExitCode.USAGE_ERROR,
    404: ExitCode.NOT_FOUND,
    405: ExitCode.USAGE_ERROR,
    413: ExitCode.USAGE_ERROR,
    428: ExitCode.CONFIRMATION_REQUIRED,
    501: ExitCode.NOT_IMPLEMENTED,
    504: ExitCode.TIMEOUT,
}
# A 500/unmapped internal failure gets a distinct non-zero (sysexits EX_SOFTWARE)
# — never NOT_READY, so an internal/dependency error is not read as "not ready".
_INTERNAL_FAILURE_EXIT = 70
_RESERVED_OPTIONS = frozenset({"group", "command", "confirm", "json", "list"})


class CliDispatcher:
    """Parse argv and dispatch the matched command through ``registry``."""

    def __init__(self, registry: HandlerRegistry) -> None:
        self._registry = registry
        self._parser = build_parser()

    def dispatch(self, argv: Sequence[str] | None = None, *, stdout: IO[str] | None = None) -> int:
        """Run one CLI invocation; return the process exit code."""

        out = stdout if stdout is not None else sys.stdout
        args = self._parser.parse_args(argv)

        if getattr(args, "list", False):
            _print_command_listing()
            return int(ExitCode.OK)

        group_name = getattr(args, "group", None)
        if not group_name:
            self._parser.print_help(out)
            return int(ExitCode.OK)

        command_name = getattr(args, "command", None) or ""
        command = find_command(Group(group_name), command_name)
        if command is None:
            self._parser.error(f"unknown command: {group_name} {command_name}".strip())
            return int(ExitCode.USAGE_ERROR)  # pragma: no cover - argparse exits

        # Confirmation guard: never dispatch an irreversible command without it.
        if command.requires_confirmation and not getattr(args, "confirm", False):
            print(
                f"{command.invocation}: --confirm is required (SRS-SAFE-001 / UI-4).",
                file=sys.stderr,
            )
            return int(ExitCode.CONFIRMATION_REQUIRED)

        key = OperationKey(Surface.CLI, command.invocation)
        owner = cli_owner(command.group.value, command.name)
        handler = self._registry.resolve(
            key, deferred=DeferredHandler(owner=owner, summary=command.summary)
        )
        request = Request(
            surface=Surface.CLI,
            operation=key,
            query=self._option_values(args),
            confirmed=bool(getattr(args, "confirm", False)),
            workflow_id=command.group.value,
            srs_refs=tuple(command.srs_refs),
        )
        result = invoke_handler(handler, request)
        self._emit(out, result.body, json_mode=bool(getattr(args, "json", False)))

        mapped = _STATUS_TO_EXIT.get(result.status_code)
        # A readiness-style command whose handler returns a healthy HTTP status
        # but reports `ready: false` must still exit NOT_READY, so automation
        # cannot read a not-ready trading runtime as ready (e.g. `readiness
        # check`). Only applies to commands that document NOT_READY.
        if (
            mapped is ExitCode.OK
            and ExitCode.NOT_READY in command.exit_codes
            and result.body.get("ready") is False
        ):
            return int(ExitCode.NOT_READY)
        if mapped is not None:
            return int(mapped)
        return _INTERNAL_FAILURE_EXIT

    @staticmethod
    def _option_values(args: argparse.Namespace) -> dict[str, str]:
        return {
            name: str(value)
            for name, value in vars(args).items()
            if name not in _RESERVED_OPTIONS and value is not None
        }

    @staticmethod
    def _emit(out: IO[str], body: Mapping[str, object], *, json_mode: bool) -> None:
        if json_mode:
            print(json.dumps(body, sort_keys=True), file=out)
            return
        if "error" in body and isinstance(body["error"], dict):
            error = body["error"]
            print(f"{error.get('category')}: {error.get('message')}", file=out)
            return
        print(json.dumps(body, indent=2, sort_keys=True), file=out)


# Expose the declared command count so callers/tests can assert full coverage.
COMMAND_COUNT = len(COMMANDS)
