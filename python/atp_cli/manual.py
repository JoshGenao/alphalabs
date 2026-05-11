"""Deterministic JSON manual generator for the ATP CLI contract.

The generator is intentionally pure-stdlib: it consumes the
declarative :data:`atp_cli.commands.COMMANDS` tuple and produces a
deterministic JSON manual ``dict``. The frozen snapshot at
``python/atp_cli/manual.json`` is byte-compared against the regenerated
dict in ``tools/cli_check.py``; the ``--update`` flag rewrites the
snapshot.

SRS trace
---------
``SRS-API-001`` (REST/CLI/dashboard operator workflows) and
``SRS-SAFE-001`` (kill switch via dashboard, CLI, REST API). The
generated manual is contract evidence only; runtime handlers are out of
scope here.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, MutableMapping

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
)

MANUAL_TITLE = "ATP Operator CLI"
"""Document title surfaced under ``info.title`` in the JSON manual."""

MANUAL_VERSION = "0.1.0"
"""Document version surfaced under ``info.version`` in the JSON manual."""

MANUAL_SPEC = "atp.cli.manual/0.1"
"""Manual schema version emitted by :func:`build_manual`."""


_PLACEHOLDER_DESCRIPTION = (
    "Contract only. Concrete handler behaviour lands with the "
    "downstream feature that owns the workflow (EXE-1, ORCH-1, "
    "RESV-1, LOG-1, NOTIF-1)."
)


def _argument_dict(argument: Argument) -> dict:
    payload: dict = {
        "name": argument.name,
        "summary": argument.summary,
        "required": argument.required,
        "is_flag": argument.is_flag,
    }
    if argument.default is not None:
        payload["default"] = argument.default
    return payload


def _command_dict(command: Command) -> dict:
    description_parts = [
        command.summary,
        f"SRS trace: {', '.join(command.srs_refs)}.",
        _PLACEHOLDER_DESCRIPTION,
    ]
    if command.requires_confirmation:
        description_parts.append("Requires --confirm (UI-4 / SRS-SAFE-001 two-step modal).")
    return {
        "group": command.group.value,
        "name": command.name,
        "invocation": command.invocation,
        "summary": command.summary,
        "description": " ".join(description_parts),
        "x-srs-refs": list(command.srs_refs),
        "x-requires-confirmation": command.requires_confirmation,
        "arguments": [_argument_dict(argument) for argument in command.arguments],
        "exit_codes": [{"code": int(code), "name": code.name} for code in command.exit_codes],
    }


def _group_dict(group: Group, commands: Iterable[Command]) -> dict:
    command_list = [_command_dict(command) for command in commands]
    return {
        "name": group.value,
        "summary": _GROUP_SUMMARIES[group],
        "x-srs-refs": list(_GROUP_TRACES[group]),
        "commands": command_list,
    }


_GROUP_SUMMARIES: dict[Group, str] = {
    Group.KILL_SWITCH: ("Operator kill switch (QuantConnect Liquidate sequence)."),
    Group.STRATEGY: ("Deployed-strategy lifecycle: list, show, start, stop, restart, rollback."),
    Group.LIVE: "Live IB designation: promote a strategy and inspect the live slot.",
    Group.HOT_SWAP: "Reservoir Hot-Swap: manual trigger and status inspection.",
    Group.READINESS: "Startup readiness: state inspection and blocking wait.",
    Group.ADMIN: ("Basic administration: logs, alerts, configuration, and version."),
}

_GROUP_TRACES: dict[Group, tuple[str, ...]] = {
    Group.KILL_SWITCH: ("SRS-SAFE-001", "SRS-API-001"),
    Group.STRATEGY: ("SRS-API-001", "SRS-ORCH-004", "SRS-ORCH-005"),
    Group.LIVE: ("SRS-API-001", "SYS-2c", "SYS-2d"),
    Group.HOT_SWAP: ("SRS-RESV-003", "SRS-RESV-004", "SRS-RESV-005", "SRS-RESV-006"),
    Group.READINESS: ("SYS-76", "SRS-ARCH-005"),
    Group.ADMIN: ("SRS-LOG-001", "SRS-NOTIF-001", "SRS-ARCH-005"),
}


def build_manual(commands: Iterable[Command] = COMMANDS) -> dict:
    """Build a deterministic JSON manual document for the CLI.

    Output is stable across runs: groups are emitted in :class:`Group`
    declaration order; commands within a group preserve their
    declaration order in :data:`COMMANDS`.

    Example:
        >>> doc = build_manual()
        >>> doc["info"]["title"]
        'ATP Operator CLI'
        >>> "kill-switch" in {g["name"] for g in doc["groups"]}
        True
    """

    by_group: MutableMapping[Group, list[Command]] = {group: [] for group in Group}
    for command in commands:
        by_group[command.group].append(command)

    document: dict = {
        "manual": MANUAL_SPEC,
        "info": {
            "title": MANUAL_TITLE,
            "version": MANUAL_VERSION,
            "description": (
                "Operator CLI surface for the ATP single-user trading "
                "platform (API-4, traces SRS-API-001 and SRS-SAFE-001). "
                "Runs only under local shell access; no remote-CLI mode "
                "and no auth tokens — see SRS-SEC-002."
            ),
        },
        "entry_point": CLI_ENTRY_POINT,
        "program": CLI_PROGRAM,
        "groups": [_group_dict(group, by_group[group]) for group in Group if by_group[group]],
        "exit_codes": [{"code": int(code), "name": code.name} for code in ExitCode],
        "x-access-model": ACCESS_MODEL,
        "x-auth-model": AUTH_MODEL,
    }
    return document


def render_snapshot(commands: Iterable[Command] = COMMANDS) -> str:
    """Render the manual document as the canonical snapshot string.

    The returned string is the byte-equal representation expected at
    ``python/atp_cli/manual.json`` (sorted keys, two-space indent,
    trailing newline).

    Example:
        >>> snapshot = render_snapshot()
        >>> snapshot.endswith("\\n")
        True
    """

    document = build_manual(commands)
    return json.dumps(document, indent=2, sort_keys=True) + "\n"
