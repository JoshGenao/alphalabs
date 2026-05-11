from __future__ import annotations

import inspect
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
SNAPSHOT_PATH = ROOT / "python" / "atp_cli" / "manual.json"

if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

import atp_cli  # noqa: E402
from atp_cli import (  # noqa: E402
    ACCESS_MODEL,
    AUTH_MODEL,
    CLI_ENTRY_POINT,
    COMMANDS,
    Argument,
    Command,
    ExitCode,
    Group,
    build_manual,
    build_parser,
    commands_by_group,
    find_command,
    render_snapshot,
)


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "atp_cli", *args],
        cwd=ROOT,
        env={**_env(), "PYTHONPATH": str(PYTHON_ROOT)},
        check=False,
        capture_output=True,
        text=True,
    )


def _env() -> dict:
    import os

    return {k: v for k, v in os.environ.items()}


class CLIContractScriptTest(unittest.TestCase):
    def test_api_4_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/cli_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("API-4 PASS", result.stdout)


class CommandShapeTest(unittest.TestCase):
    def test_every_command_is_well_formed(self) -> None:
        self.assertGreater(len(COMMANDS), 0)
        for command in COMMANDS:
            with self.subTest(command=command.invocation):
                self.assertIsInstance(command, Command)
                self.assertIsInstance(command.group, Group)
                self.assertTrue(command.summary)
                self.assertTrue(command.srs_refs)
                for ref in command.srs_refs:
                    self.assertIsInstance(ref, str)
                    self.assertTrue(ref)
                self.assertIn(ExitCode.OK, command.exit_codes)
                for argument in command.arguments:
                    self.assertIsInstance(argument, Argument)
                    self.assertTrue(argument.name)
                    self.assertTrue(argument.summary)


class GroupCoverageTest(unittest.TestCase):
    def test_every_group_has_at_least_one_command(self) -> None:
        declared = {command.group for command in COMMANDS}
        self.assertEqual(declared, set(Group))

    def test_six_groups_present(self) -> None:
        self.assertEqual(len(Group), 6)

    def test_eighteen_commands_present(self) -> None:
        self.assertEqual(len(COMMANDS), 18)


class ConfirmationInvariantTest(unittest.TestCase):
    REQUIRED = (
        ("KILL_SWITCH", "activate"),
        ("STRATEGY", "rollback"),
        ("LIVE", "promote"),
        ("HOT_SWAP", "trigger"),
    )

    def test_irreversible_commands_require_confirm(self) -> None:
        for group_name, command_name in self.REQUIRED:
            with self.subTest(group=group_name, command=command_name):
                command = find_command(getattr(Group, group_name), command_name)
                self.assertIsNotNone(command, f"{group_name}.{command_name} missing")
                self.assertTrue(command.requires_confirmation)
                flag = next(
                    (a for a in command.arguments if a.name == "--confirm"),
                    None,
                )
                self.assertIsNotNone(flag)
                self.assertTrue(flag.required)
                self.assertIn(ExitCode.CONFIRMATION_REQUIRED, command.exit_codes)


class ManualSnapshotInSyncTest(unittest.TestCase):
    def test_snapshot_byte_equal_to_render(self) -> None:
        self.assertTrue(SNAPSHOT_PATH.exists(), SNAPSHOT_PATH)
        on_disk = SNAPSHOT_PATH.read_text(encoding="utf-8")
        regenerated = render_snapshot()
        if on_disk != regenerated:
            self.fail("manual.json drift; regenerate via `python3 tools/cli_check.py --update`")

    def test_snapshot_is_deterministic(self) -> None:
        first = render_snapshot()
        second = render_snapshot()
        self.assertEqual(first, second)

    def test_snapshot_parses_with_expected_groups(self) -> None:
        document = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(document["info"]["title"], "ATP Operator CLI")
        names = {group["name"] for group in document["groups"]}
        self.assertEqual(names, {group.value for group in Group})


class LocalShellPolicyTest(unittest.TestCase):
    def test_access_model_is_local_shell(self) -> None:
        self.assertEqual(ACCESS_MODEL, "local-shell")

    def test_auth_model_is_local_single_user(self) -> None:
        self.assertEqual(AUTH_MODEL, "local-single-user")

    def test_entry_point_is_atp(self) -> None:
        self.assertEqual(CLI_ENTRY_POINT, "atp")

    def test_manual_carries_policy_extensions(self) -> None:
        document = build_manual()
        self.assertEqual(document["x-access-model"], "local-shell")
        self.assertEqual(document["x-auth-model"], "local-single-user")
        self.assertEqual(document["entry_point"], "atp")


class ExitCodeContractTest(unittest.TestCase):
    def test_documented_exit_codes_only(self) -> None:
        documented = {int(code) for code in ExitCode}
        for command in COMMANDS:
            with self.subTest(command=command.invocation):
                for code in command.exit_codes:
                    self.assertIn(int(code), documented)


class RunnerSubprocessTest(unittest.TestCase):
    def test_list_prints_every_command(self) -> None:
        result = _run(["--list"])
        self.assertEqual(result.returncode, int(ExitCode.OK), result.stderr)
        for command in COMMANDS:
            self.assertIn(command.invocation, result.stdout)

    def test_kill_switch_requires_confirm(self) -> None:
        result = _run(["kill-switch", "activate"])
        self.assertEqual(result.returncode, int(ExitCode.CONFIRMATION_REQUIRED), result.stderr)
        self.assertIn("--confirm", result.stderr)

    def test_kill_switch_with_confirm_returns_not_implemented(self) -> None:
        result = _run(["kill-switch", "activate", "--confirm"])
        self.assertEqual(result.returncode, int(ExitCode.NOT_IMPLEMENTED), result.stderr)
        self.assertIn("API-4 contract surface", result.stdout)

    def test_readiness_check_returns_not_implemented(self) -> None:
        result = _run(["readiness", "check"])
        self.assertEqual(result.returncode, int(ExitCode.NOT_IMPLEMENTED))


class ParserBuildTest(unittest.TestCase):
    def test_build_parser_exposes_every_group(self) -> None:
        parser = build_parser()
        result = parser.parse_args(["--list"])
        self.assertTrue(result.list)


class CommandsByGroupTest(unittest.TestCase):
    def test_kill_switch_group(self) -> None:
        commands = commands_by_group(Group.KILL_SWITCH)
        names = {c.name for c in commands}
        self.assertEqual(names, {"activate", "status"})


class PublicDocstringsTest(unittest.TestCase):
    def test_every_export_has_docstring(self) -> None:
        for name in atp_cli.__all__:
            with self.subTest(name=name):
                obj = getattr(atp_cli, name)
                if inspect.isclass(obj) or inspect.isfunction(obj):
                    docstring = inspect.getdoc(obj) or ""
                    self.assertTrue(
                        docstring.strip(),
                        f"{name} is missing a docstring",
                    )


if __name__ == "__main__":
    unittest.main()
