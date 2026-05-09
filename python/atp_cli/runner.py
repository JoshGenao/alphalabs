"""Argparse-backed CLI runner for the declarative ATP CLI contract.

The runner builds an ``argparse`` parser from :data:`atp_cli.commands.COMMANDS`
so that operators can introspect the command surface (``--help``,
``--list``) and so that contract evidence can exercise every command
end-to-end without booting the runtime services. Concrete handlers are
deliberately stubbed: every command exits with
:attr:`atp_cli.commands.ExitCode.NOT_IMPLEMENTED` and references the
downstream feature that will own the real behaviour.

The runner enforces the API-4 ``--confirm`` invariant for irreversible
commands: if a command has ``requires_confirmation=True`` and the
operator omits the flag, the runner exits with
:attr:`atp_cli.commands.ExitCode.CONFIRMATION_REQUIRED` and does **not**
call the (stub) handler.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .commands import (
    ACCESS_MODEL,
    AUTH_MODEL,
    CLI_ENTRY_POINT,
    COMMANDS,
    Argument,
    Command,
    ExitCode,
    Group,
    find_command,
)

_CONTRACT_NOTE = (
    "API-4 contract surface: handler not yet wired. Real behaviour "
    "lands with the downstream feature that owns this workflow."
)


def _add_argument(parser: argparse.ArgumentParser, argument: Argument) -> None:
    """Translate an :class:`Argument` declaration into an argparse entry."""

    if argument.is_flag:
        parser.add_argument(
            argument.name,
            action="store_true",
            help=argument.summary,
        )
        return

    kwargs: dict = {"help": argument.summary}
    if argument.default is not None:
        kwargs["default"] = argument.default

    if argument.name.startswith("--"):
        kwargs["required"] = argument.required
        parser.add_argument(argument.name, **kwargs)
    else:
        if not argument.required:
            kwargs["nargs"] = "?"
        parser.add_argument(argument.name, **kwargs)


def _command_help(command: Command) -> str:
    refs = ", ".join(command.srs_refs)
    parts = [command.summary, f"SRS trace: {refs}."]
    if command.requires_confirmation:
        parts.append("Requires --confirm.")
    return " ".join(parts)


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser for the CLI contract surface.

    Example:
        >>> parser = build_parser()
        >>> parser.prog == 'atp'
        True
    """

    parser = argparse.ArgumentParser(
        prog=CLI_ENTRY_POINT,
        description=(
            "ATP operator CLI (API-4). Local shell access only "
            f"({ACCESS_MODEL}, {AUTH_MODEL}). See SRS-SEC-002."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List every command in the contract surface and exit.",
    )

    group_subparsers = parser.add_subparsers(
        dest="group",
        title="groups",
        metavar="GROUP",
    )

    for group in Group:
        group_commands = [c for c in COMMANDS if c.group is group]
        if not group_commands:
            continue
        group_parser = group_subparsers.add_parser(
            group.value,
            help=f"{group.value} commands",
        )
        command_subparsers = group_parser.add_subparsers(
            dest="command",
            title="commands",
            metavar="COMMAND",
        )
        for command in group_commands:
            command_parser = command_subparsers.add_parser(
                command.name,
                help=_command_help(command),
                description=_command_help(command),
            )
            for argument in command.arguments:
                _add_argument(command_parser, argument)

    return parser


def _print_command_listing() -> None:
    print(f"{CLI_ENTRY_POINT} — operator CLI ({ACCESS_MODEL})")
    for group in Group:
        group_commands = [c for c in COMMANDS if c.group is group]
        if not group_commands:
            continue
        print(f"\n[{group.value}]")
        for command in group_commands:
            print(f"  {command.invocation:<28} {command.summary}")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m atp_cli``.

    Returns one of the codes documented on the matched command's
    ``exit_codes`` field; if the command is not yet implemented, returns
    :attr:`atp_cli.commands.ExitCode.NOT_IMPLEMENTED`.
    """

    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "list", False):
        _print_command_listing()
        return int(ExitCode.OK)

    group_name = getattr(args, "group", None)
    if not group_name:
        parser.print_help()
        return int(ExitCode.OK)

    command_name = getattr(args, "command", None) or ""
    command = find_command(Group(group_name), command_name)
    if command is None:
        parser.error(f"unknown command: {group_name} {command_name}".strip())
        return int(ExitCode.USAGE_ERROR)  # pragma: no cover - argparse exits

    if command.requires_confirmation and not getattr(args, "confirm", False):
        print(
            f"{command.invocation}: --confirm is required "
            "(SRS-SAFE-001 / UI-4).",
            file=sys.stderr,
        )
        return int(ExitCode.CONFIRMATION_REQUIRED)

    print(
        f"{command.invocation}: {_CONTRACT_NOTE} "
        f"(SRS trace: {', '.join(command.srs_refs)})"
    )
    return int(ExitCode.NOT_IMPLEMENTED)
