"""ATP CLI contract surface (API-4 / SRS-API-001 + SRS-SAFE-001).

This package exposes the declarative CLI command contract used to
verify ``API-4`` in ``feature_list.json``. It contains an argparse
runner that lets operators introspect the surface (``python -m atp_cli
--list`` and ``python -m atp_cli <group> <command> --help``); concrete
handlers arrive with downstream features.

See ``python/atp_cli/README.md`` for the operator-facing summary.
"""

from .commands import (
    ACCESS_MODEL,
    AUTH_MODEL,
    CLI_ENTRY_POINT,
    CLI_PROGRAM,
    COMMANDS,
    Argument,
    Command,
    ExitCode,
    Group,
    commands_by_group,
    find_command,
)
from .manual import (
    MANUAL_SPEC,
    MANUAL_TITLE,
    MANUAL_VERSION,
    build_manual,
    render_snapshot,
)
from .runner import build_parser, main


__all__ = [
    "ACCESS_MODEL",
    "AUTH_MODEL",
    "Argument",
    "CLI_ENTRY_POINT",
    "CLI_PROGRAM",
    "COMMANDS",
    "Command",
    "ExitCode",
    "Group",
    "MANUAL_SPEC",
    "MANUAL_TITLE",
    "MANUAL_VERSION",
    "build_manual",
    "build_parser",
    "commands_by_group",
    "find_command",
    "main",
    "render_snapshot",
]
