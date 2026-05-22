"""L7 domain test for the SRS-API-001 operator-workflow-surface contract.

The L7 layer drives a reference operator client stub through every AC
workflow named in SRS-API-001 (Live designation, strategy management,
kill switch, Hot-Swap, Reservoir ranking, backtests, system status,
logs). For each workflow the stub:

* picks the documented entry point (REST route or CLI command) from the
  contract block,
* looks the entry up in the snapshot file the per-surface check has
  already byte-synced (``python/atp_api/openapi.json``,
  ``python/atp_cli/manual.json``, ``python/atp_ws/asyncapi.json``), and
* JSON-round-trips one representative payload tuple so any
  non-serialisable field surfaces here rather than at the future
  handler-implementation session.

Marked ``safety`` + ``domain`` so the deterministic critic recognises
the file as the paired safety-path test for the SDK-surface diff.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_api import routes as rest_routes  # noqa: E402
from atp_cli import commands as cli_commands  # noqa: E402
from atp_ws import channels as ws_channels  # noqa: E402

pytestmark = [pytest.mark.safety, pytest.mark.domain]

_CONTRACT_BLOCK = "operator_workflow_surface_contract"
_RUNTIME_SERVICES = ROOT / "architecture" / "runtime_services.json"


def _load_contract() -> dict[str, Any]:
    return json.loads(_RUNTIME_SERVICES.read_text(encoding="utf-8"))[_CONTRACT_BLOCK]


# --------------------------------------------------------------------------- #
# Reference operator client
# --------------------------------------------------------------------------- #


class _RefOperatorClient:
    """Reference stub of the future dashboard/CLI/REST operator client.

    Each ``access_<workflow>`` method returns a list of ``(surface, identifier,
    srs_refs, requires_confirmation, sample_payload)`` tuples — one per
    documented entry the operator can use to reach the workflow.
    """

    def __init__(self) -> None:
        self.block = _load_contract()
        with (ROOT / "python" / "atp_api" / "openapi.json").open(encoding="utf-8") as fh:
            self.openapi = json.load(fh)
        with (ROOT / "python" / "atp_cli" / "manual.json").open(encoding="utf-8") as fh:
            self.cli_manual = json.load(fh)
        with (ROOT / "python" / "atp_ws" / "asyncapi.json").open(encoding="utf-8") as fh:
            self.asyncapi = json.load(fh)

    def _workflow(self, workflow_id: str) -> dict[str, Any]:
        for wf in self.block["ac_workflows"]:
            if wf["id"] == workflow_id:
                return wf
        raise KeyError(workflow_id)

    def access(
        self, workflow_id: str
    ) -> list[tuple[str, str, tuple[str, ...], bool, dict[str, Any]]]:
        wf = self._workflow(workflow_id)
        entries: list[tuple[str, str, tuple[str, ...], bool, dict[str, Any]]] = []
        for cap in wf["rest_capabilities"]:
            for route in rest_routes.ROUTES:
                if route.capability.value != cap:
                    continue
                self._assert_in_openapi(route.path)
                payload = {field: None for field in route.response_fields}
                # JSON round-trip — any non-serialisable field would raise here.
                json.loads(json.dumps(payload))
                entries.append(
                    (
                        "rest",
                        f"{route.method.value} {route.path}",
                        route.srs_refs,
                        route.requires_confirmation,
                        payload,
                    )
                )
        for grp in wf["cli_groups"]:
            for cmd in cli_commands.COMMANDS:
                if cmd.group.value != grp:
                    continue
                self._assert_in_cli_manual(cmd.invocation)
                payload = {arg.name.lstrip("-"): None for arg in cmd.arguments}
                json.loads(json.dumps(payload))
                entries.append(
                    (
                        "cli",
                        cmd.invocation,
                        cmd.srs_refs,
                        cmd.requires_confirmation,
                        payload,
                    )
                )
        for chan in wf["websocket_channels"]:
            for event in ws_channels.EVENT_CHANNELS:
                if event.name.value != chan:
                    continue
                self._assert_in_asyncapi(event.name.value)
                payload = {field: None for field in event.payload_fields}
                json.loads(json.dumps(payload))
                entries.append(
                    (
                        "websocket",
                        event.name.value,
                        event.srs_refs,
                        False,
                        payload,
                    )
                )
        return entries

    def _assert_in_openapi(self, route_path: str) -> None:
        paths = self.openapi.get("paths", {})
        if route_path not in paths:
            raise AssertionError(
                f"OpenAPI snapshot is missing route {route_path!r}; regenerate via "
                "python3 tools/rest_api_check.py --update"
            )

    def _assert_in_cli_manual(self, invocation: str) -> None:
        manual_invocations: list[str] = []
        for group in self.cli_manual.get("groups", []):
            for command in group.get("commands", []):
                manual_invocations.append(command["invocation"])
        if invocation not in manual_invocations:
            raise AssertionError(
                f"CLI manual snapshot is missing invocation {invocation!r}; regenerate "
                "via python3 tools/cli_check.py --update"
            )

    def _assert_in_asyncapi(self, channel_name: str) -> None:
        channels = self.asyncapi.get("channels", {})
        needle = channel_name.lower()
        if not any(needle in name.lower() for name in channels):
            raise AssertionError(
                f"AsyncAPI snapshot is missing channel {channel_name!r}; regenerate "
                "via python3 tools/websocket_api_check.py --update"
            )


# --------------------------------------------------------------------------- #
# Test classes
# --------------------------------------------------------------------------- #


class OperatorWorkflowAccessTest(unittest.TestCase):
    """For every AC workflow the operator stub must reach ≥1 entry."""

    def setUp(self) -> None:
        self.client = _RefOperatorClient()
        self.block = self.client.block

    def _assert_workflow_reachable(self, workflow_id: str) -> None:
        entries = self.client.access(workflow_id)
        self.assertGreaterEqual(
            len(entries),
            self.block["min_surface_entries_per_workflow"],
            msg=f"workflow {workflow_id} has fewer than minimum entries: {entries}",
        )
        for surface, identifier, srs_refs, _req, _payload in entries:
            self.assertIn(surface, ("rest", "cli", "websocket"))
            self.assertTrue(identifier, f"{workflow_id} entry has empty identifier")
            self.assertTrue(srs_refs, f"{workflow_id} entry {identifier} has empty srs_refs")

    def test_live_designation_reachable(self) -> None:
        self._assert_workflow_reachable("LIVE_DESIGNATION")

    def test_strategy_management_reachable(self) -> None:
        self._assert_workflow_reachable("STRATEGY_MANAGEMENT")

    def test_kill_switch_reachable(self) -> None:
        self._assert_workflow_reachable("KILL_SWITCH")

    def test_hot_swap_reachable(self) -> None:
        self._assert_workflow_reachable("HOT_SWAP")

    def test_reservoir_ranking_reachable(self) -> None:
        self._assert_workflow_reachable("RESERVOIR_RANKING")

    def test_backtests_reachable(self) -> None:
        self._assert_workflow_reachable("BACKTESTS")

    def test_system_status_reachable(self) -> None:
        self._assert_workflow_reachable("SYSTEM_STATUS")

    def test_logs_reachable(self) -> None:
        self._assert_workflow_reachable("LOGS")


class OperatorWorkflowConfirmationTest(unittest.TestCase):
    """State-mutating entries on confirmation-required workflows must guard."""

    def setUp(self) -> None:
        self.client = _RefOperatorClient()

    def _assert_state_mutating_entries_require_confirmation(self, workflow_id: str) -> None:
        entries = self.client.access(workflow_id)
        write_entries = [
            entry
            for entry in entries
            if (
                (entry[0] == "rest" and entry[1].split()[0] in {"POST", "PUT", "DELETE"})
                or (
                    entry[0] == "cli"
                    and any(
                        token in entry[1]
                        for token in ("activate", "promote", "rollback", "trigger")
                    )
                )
            )
        ]
        self.assertTrue(
            write_entries,
            msg=f"workflow {workflow_id} has no state-mutating entries to guard",
        )
        for surface, identifier, _srs, requires_confirmation, _payload in write_entries:
            self.assertTrue(
                requires_confirmation,
                msg=(
                    f"{surface} entry {identifier!r} on workflow {workflow_id} "
                    "is state-mutating but does not require operator confirmation"
                ),
            )

    def test_kill_switch_confirmation(self) -> None:
        self._assert_state_mutating_entries_require_confirmation("KILL_SWITCH")

    def test_live_designation_confirmation(self) -> None:
        self._assert_state_mutating_entries_require_confirmation("LIVE_DESIGNATION")

    def test_hot_swap_confirmation(self) -> None:
        self._assert_state_mutating_entries_require_confirmation("HOT_SWAP")


class OperatorWorkflowDeferredTest(unittest.TestCase):
    """The deferred list must name every downstream feature required for passes:true."""

    def setUp(self) -> None:
        self.block = _load_contract()
        self.declared = {entry["feature"] for entry in self.block["deferred"]}

    def test_deferred_names_exe_001(self) -> None:
        self.assertIn("SRS-EXE-001", self.declared)

    def test_deferred_names_exe_006(self) -> None:
        self.assertIn("SRS-EXE-006", self.declared)

    def test_deferred_names_orch_004(self) -> None:
        self.assertIn("SRS-ORCH-004", self.declared)

    def test_deferred_names_resv_003(self) -> None:
        self.assertIn("SRS-RESV-003", self.declared)

    def test_deferred_names_bt_001(self) -> None:
        self.assertIn("SRS-BT-001", self.declared)

    def test_deferred_names_log_001(self) -> None:
        self.assertIn("SRS-LOG-001", self.declared)

    def test_deferred_names_notif_001(self) -> None:
        self.assertIn("SRS-NOTIF-001", self.declared)

    def test_deferred_names_operator_interface_runtime(self) -> None:
        self.assertIn("operator-interface-runtime", self.declared)


class OperatorWorkflowPayloadSerialisableTest(unittest.TestCase):
    """Every payload tuple on every workflow JSON-round-trips."""

    def test_every_entry_payload_round_trips(self) -> None:
        client = _RefOperatorClient()
        for workflow_id in client.block["required_workflow_ids"]:
            for _surface, identifier, _srs, _req, payload in client.access(workflow_id):
                try:
                    json.loads(json.dumps(payload))
                except (TypeError, ValueError) as exc:
                    self.fail(
                        f"workflow {workflow_id} entry {identifier!r} payload "
                        f"does not JSON-round-trip: {exc}"
                    )


if __name__ == "__main__":
    unittest.main()
