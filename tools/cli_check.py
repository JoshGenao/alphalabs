#!/usr/bin/env python3
"""Contract evidence script for feature API-4.

Introspects the declarative ``atp_cli`` package and confirms that the
operator CLI contract exposes every command group and irreversible-
action safeguard required by API-4's description, tracing each to
``SRS-API-001`` and ``SRS-SAFE-001`` plus the supporting clauses
listed in ``docs/SRS.md`` §7.

Mirrors the PASS/FAIL output style of ``tools/rest_api_check.py`` and
``tools/websocket_api_check.py``.

Invoke:
    python3 tools/cli_check.py            # check (exit 0 on PASS)
    python3 tools/cli_check.py --update   # rewrite frozen JSON manual snapshot
"""

from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
SNAPSHOT_PATH = ROOT / "python" / "atp_cli" / "manual.json"


class CLIContractError(AssertionError):
    pass


def fail(message: str) -> None:
    raise CLIContractError(message)


def _load() -> object:
    if str(PYTHON_ROOT) not in sys.path:
        sys.path.insert(0, str(PYTHON_ROOT))
    return importlib.import_module("atp_cli")


def _commands_for(module, group_name: str):
    group = getattr(module.Group, group_name)
    matches = [c for c in module.COMMANDS if c.group is group]
    if not matches:
        fail(f"No CLI commands declared for group {group_name}")
    return matches


def _expect_srs_refs(commands, required: Iterable[str]) -> None:
    refs: set[str] = set()
    for command in commands:
        if not command.srs_refs:
            fail(f"Command {command.invocation!r} has empty srs_refs")
        refs.update(command.srs_refs)
    missing = sorted(set(required) - refs)
    if missing:
        fail(
            "Group is missing required SRS traces: "
            f"{', '.join(missing)}"
        )


def _expect_command_names(commands, required: Iterable[str]) -> None:
    declared = {c.name for c in commands}
    missing = sorted(set(required) - declared)
    if missing:
        fail("Group is missing commands: " + ", ".join(missing))


def _expect_confirmation(commands, names: Iterable[str]) -> None:
    by_name = {c.name: c for c in commands}
    for name in names:
        command = by_name.get(name)
        if command is None:
            fail(f"Confirmation-required command missing: {name}")
        if not command.requires_confirmation:
            fail(
                f"Command {command.invocation!r} must require --confirm "
                "(SRS-SAFE-001 / UI-4)"
            )
        if not any(arg.name == "--confirm" for arg in command.arguments):
            fail(
                f"Command {command.invocation!r} declares "
                "requires_confirmation=True but lacks a --confirm argument"
            )


def _summary(label: str, commands) -> str:
    items = ", ".join(c.invocation for c in commands)
    return f"{label}: {items}"


# --------------------------------------------------------------------------- #
# Per-group checks (one per API-4 bucket)
# --------------------------------------------------------------------------- #


def check_api_4_001_kill_switch(module) -> str:
    commands = _commands_for(module, "KILL_SWITCH")
    _expect_command_names(commands, ("activate", "status"))
    _expect_srs_refs(commands, ("SRS-SAFE-001", "SYS-44a", "SYS-44b", "NFR-P3"))
    _expect_confirmation(commands, ("activate",))
    return _summary("kill-switch (SRS-SAFE-001, SYS-44a, NFR-P3)", commands)


def check_api_4_002_strategy(module) -> str:
    commands = _commands_for(module, "STRATEGY")
    _expect_command_names(
        commands,
        ("list", "show", "start", "stop", "restart", "rollback"),
    )
    _expect_srs_refs(
        commands, ("SRS-API-001", "SRS-ORCH-004", "SRS-ORCH-005", "SYS-79", "SYS-80")
    )
    _expect_confirmation(commands, ("rollback",))
    return _summary("strategy (SRS-ORCH-004, SRS-ORCH-005)", commands)


def check_api_4_003_live(module) -> str:
    commands = _commands_for(module, "LIVE")
    _expect_command_names(commands, ("promote", "show"))
    _expect_srs_refs(commands, ("SRS-API-001", "SYS-2c", "SYS-2d"))
    _expect_confirmation(commands, ("promote",))
    return _summary("live (SRS-API-001, SYS-2c, SYS-2d)", commands)


def check_api_4_004_hot_swap(module) -> str:
    commands = _commands_for(module, "HOT_SWAP")
    _expect_command_names(commands, ("trigger", "status"))
    _expect_srs_refs(
        commands,
        (
            "SRS-RESV-003",
            "SRS-RESV-004",
            "SRS-RESV-005",
            "SRS-RESV-006",
            "SYS-49a",
        ),
    )
    _expect_confirmation(commands, ("trigger",))
    return _summary("hot-swap (SRS-RESV-003..006, SYS-49a)", commands)


def check_api_4_005_readiness(module) -> str:
    commands = _commands_for(module, "READINESS")
    _expect_command_names(commands, ("check", "wait"))
    _expect_srs_refs(commands, ("SYS-76",))
    return _summary("readiness (SYS-76, SRS-ARCH-005)", commands)


def check_api_4_006_admin(module) -> str:
    commands = _commands_for(module, "ADMIN")
    _expect_command_names(commands, ("logs", "alerts", "config", "version"))
    _expect_srs_refs(
        commands, ("SRS-LOG-001", "SRS-NOTIF-001", "SRS-ARCH-005")
    )
    return _summary("admin (SRS-LOG-001, SRS-NOTIF-001, SRS-ARCH-005)", commands)


# --------------------------------------------------------------------------- #
# Cross-cutting checks
# --------------------------------------------------------------------------- #


def check_api_4_007_confirmation_invariant(module) -> str:
    """Every requires_confirmation=True command must declare --confirm."""

    irreversible = [c for c in module.COMMANDS if c.requires_confirmation]
    if not irreversible:
        fail("No requires_confirmation commands declared (SRS-SAFE-001)")
    for command in irreversible:
        flag = next(
            (arg for arg in command.arguments if arg.name == "--confirm"),
            None,
        )
        if flag is None:
            fail(
                f"Command {command.invocation!r} requires confirmation "
                "but has no --confirm argument."
            )
        if not flag.required:
            fail(
                f"Command {command.invocation!r} --confirm flag must be "
                "required=True."
            )
        if int(module.ExitCode.CONFIRMATION_REQUIRED) not in [
            int(code) for code in command.exit_codes
        ]:
            fail(
                f"Command {command.invocation!r} must document "
                "ExitCode.CONFIRMATION_REQUIRED in exit_codes."
            )
    return (
        "Confirmation invariant: "
        f"{len(irreversible)} irreversible commands enforce --confirm"
    )


def check_api_4_008_local_shell_policy(module) -> str:
    """Access model and entry point encode SRS-SEC-002 / API-4 constraint."""

    if module.ACCESS_MODEL != "local-shell":
        fail(
            "ACCESS_MODEL must be 'local-shell' (SRS-SEC-002 / API-4 "
            f"local-shell-access constraint); got {module.ACCESS_MODEL!r}"
        )
    if module.AUTH_MODEL != "local-single-user":
        fail(
            "AUTH_MODEL must be 'local-single-user' (SRS-SEC-002); "
            f"got {module.AUTH_MODEL!r}"
        )
    document = module.build_manual()
    for key, expected in (
        ("x-access-model", module.ACCESS_MODEL),
        ("x-auth-model", module.AUTH_MODEL),
    ):
        if document.get(key) != expected:
            fail(
                f"Manual document missing/incorrect {key}: "
                f"expected {expected!r}, got {document.get(key)!r}"
            )
    return "Local shell access (local-shell + local-single-user, SRS-SEC-002)"


def check_api_4_009_exit_code_contract(module) -> str:
    """Every command's exit_codes must be documented values that include OK."""

    documented = {int(code) for code in module.ExitCode}
    for command in module.COMMANDS:
        if not command.exit_codes:
            fail(f"Command {command.invocation!r} declares no exit codes")
        if int(module.ExitCode.OK) not in [int(c) for c in command.exit_codes]:
            fail(
                f"Command {command.invocation!r} must document ExitCode.OK"
            )
        unknown = [
            int(c) for c in command.exit_codes if int(c) not in documented
        ]
        if unknown:
            fail(
                f"Command {command.invocation!r} declares undocumented "
                f"exit codes: {unknown}"
            )
    return f"Exit-code contract enforced across {len(module.COMMANDS)} commands"


def check_api_4_010_manual_snapshot(module) -> str:
    """Snapshot of the JSON manual must be byte-equal to the committed file."""

    if not SNAPSHOT_PATH.exists():
        fail(
            "JSON manual snapshot is missing; "
            "run: python3 tools/cli_check.py --update"
        )
    actual = SNAPSHOT_PATH.read_text(encoding="utf-8")
    expected = module.render_snapshot()
    if actual != expected:
        fail(
            "JSON manual snapshot is stale; "
            "regenerate via: python3 tools/cli_check.py --update"
        )
    return f"JSON manual snapshot in sync ({SNAPSHOT_PATH.relative_to(ROOT)})"


def check_api_4_011_runner_smoke(module) -> str:
    """Argparse runner exposes --list and rejects unconfirmed kill-switch."""

    listing = subprocess.run(
        [sys.executable, "-m", "atp_cli", "--list"],
        cwd=ROOT,
        env={**_module_env(), "PYTHONPATH": str(PYTHON_ROOT)},
        check=False,
        capture_output=True,
        text=True,
    )
    if listing.returncode != 0:
        fail(
            "`python -m atp_cli --list` failed: "
            f"{listing.stderr or listing.stdout}"
        )
    if "kill-switch activate" not in listing.stdout:
        fail("`python -m atp_cli --list` did not list kill-switch activate")

    blocked = subprocess.run(
        [sys.executable, "-m", "atp_cli", "kill-switch", "activate"],
        cwd=ROOT,
        env={**_module_env(), "PYTHONPATH": str(PYTHON_ROOT)},
        check=False,
        capture_output=True,
        text=True,
    )
    if blocked.returncode != int(module.ExitCode.CONFIRMATION_REQUIRED):
        fail(
            "`kill-switch activate` without --confirm must exit "
            f"{int(module.ExitCode.CONFIRMATION_REQUIRED)}; "
            f"got {blocked.returncode}"
        )

    confirmed = subprocess.run(
        [
            sys.executable,
            "-m",
            "atp_cli",
            "kill-switch",
            "activate",
            "--confirm",
        ],
        cwd=ROOT,
        env={**_module_env(), "PYTHONPATH": str(PYTHON_ROOT)},
        check=False,
        capture_output=True,
        text=True,
    )
    if confirmed.returncode != int(module.ExitCode.NOT_IMPLEMENTED):
        fail(
            "`kill-switch activate --confirm` must exit "
            f"{int(module.ExitCode.NOT_IMPLEMENTED)} until handler is "
            f"wired; got {confirmed.returncode}"
        )
    return (
        "Runner: --list, kill-switch confirm gating, NOT_IMPLEMENTED "
        "stub all behave as contracted"
    )


def _module_env() -> dict:
    import os

    return {k: v for k, v in os.environ.items()}


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


def _group_coverage(module) -> None:
    declared = {c.group for c in module.COMMANDS}
    expected = set(module.Group)
    missing = sorted(g.value for g in (expected - declared))
    if missing:
        fail(f"COMMANDS missing groups: {', '.join(missing)}")


def run_checks() -> list[str]:
    module = _load()
    _group_coverage(module)

    evidence: list[str] = []
    for check in (
        check_api_4_001_kill_switch,
        check_api_4_002_strategy,
        check_api_4_003_live,
        check_api_4_004_hot_swap,
        check_api_4_005_readiness,
        check_api_4_006_admin,
        check_api_4_007_confirmation_invariant,
        check_api_4_008_local_shell_policy,
        check_api_4_009_exit_code_contract,
        check_api_4_010_manual_snapshot,
        check_api_4_011_runner_smoke,
    ):
        evidence.append(check(module))
    return evidence


def update_snapshot() -> str:
    module = _load()
    SNAPSHOT_PATH.write_text(module.render_snapshot(), encoding="utf-8")
    return f"Wrote {SNAPSHOT_PATH.relative_to(ROOT)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="API-4 contract evidence")
    parser.add_argument(
        "--update",
        action="store_true",
        help="Regenerate the frozen JSON manual snapshot from atp_cli.COMMANDS.",
    )
    args = parser.parse_args(argv)

    if args.update:
        try:
            message = update_snapshot()
        except Exception as error:  # noqa: BLE001 - surfacing all import/IO errors
            print(f"API-4 UPDATE FAIL: {error}", file=sys.stderr)
            return 1
        print(message)
        return 0

    try:
        evidence = run_checks()
    except CLIContractError as error:
        print(f"API-4 FAIL: {error}", file=sys.stderr)
        return 1

    print("API-4 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
