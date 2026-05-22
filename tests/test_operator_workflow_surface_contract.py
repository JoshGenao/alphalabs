"""L3 contract tests for the SRS-API-001 operator-workflow-surface contract.

Three layers of evidence:

* :class:`OperatorWorkflowSurfaceScriptTest` runs
  ``tools/operator_workflow_surface_check.py`` as a subprocess so the
  positive-evidence path stays under CI coverage.
* :class:`OperatorWorkflowSurfaceMutationTest` rebuilds the relevant
  packages in a temporary copy of the repo, mutates one rule at a time,
  and re-runs the check to ensure each contract clause has a negative
  anchor — preventing silent rule regressions.
* :class:`ContractBlockParityTest` is a pure-Python parity pass over the
  block keys + workflow IDs that runs without a subprocess so contract
  block typos surface fast.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_api import routes as rest_routes  # noqa: E402
from atp_cli import commands as cli_commands  # noqa: E402
from atp_ws import channels as ws_channels  # noqa: E402

_CONTRACT_BLOCK = "operator_workflow_surface_contract"
_RUNTIME_SERVICES = ROOT / "architecture" / "runtime_services.json"


def _load_contract() -> dict:
    return json.loads(_RUNTIME_SERVICES.read_text(encoding="utf-8"))[_CONTRACT_BLOCK]


# --------------------------------------------------------------------------- #
# Subprocess positive-evidence
# --------------------------------------------------------------------------- #


class OperatorWorkflowSurfaceScriptTest(unittest.TestCase):
    def test_script_returns_zero_and_prints_sdk_surface_pass(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/operator_workflow_surface_check.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("SRS-API-001 SDK-SURFACE PASS", result.stdout)
        # The PASS line must name each downstream owner so contract drift
        # is loud in CI logs.
        for owner in (
            "SRS-EXE-001",
            "SRS-EXE-006",
            "SRS-ORCH-004",
            "SRS-RESV-002..006",
            "SRS-BT-001",
            "SRS-BT-009",
            "SRS-DATA-002",
            "SRS-LOG-001",
            "SRS-NOTIF-001",
            "operator-interface-runtime",
        ):
            self.assertIn(owner, result.stdout, f"PASS line should mention {owner}")


# --------------------------------------------------------------------------- #
# Mutation rig
# --------------------------------------------------------------------------- #


class _MutationRig:
    """Copy the repo into a tempdir, apply a mutation, and re-run the check."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="atp-operator-workflow-mutation-"))
        for entry in ("architecture", "python", "tools"):
            shutil.copytree(ROOT / entry, self.tmp / entry)

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def replace_in(self, rel: str, old: str, new: str) -> None:
        path = self.tmp / rel
        text = path.read_text(encoding="utf-8")
        if old not in text:
            raise AssertionError(f"mutation source {old!r} not found in {rel}")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def delete(self, rel: str) -> None:
        target = self.tmp / rel
        if target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        else:
            raise AssertionError(f"cannot delete missing path {rel}")

    def patch_contract(self, mutator: Callable[[dict], None]) -> None:
        path = self.tmp / "architecture" / "runtime_services.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        block = data[_CONTRACT_BLOCK]
        mutator(block)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def run_check(self) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.tmp / "python")
        return subprocess.run(
            [
                sys.executable,
                str(self.tmp / "tools" / "operator_workflow_surface_check.py"),
                "--root",
                str(self.tmp),
            ],
            cwd=self.tmp,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )


class OperatorWorkflowSurfaceMutationTest(unittest.TestCase):
    """One L3 negative-case anchor per contract rule."""

    def _run_mutation(
        self, apply: Callable[[_MutationRig], None]
    ) -> subprocess.CompletedProcess[str]:
        rig = _MutationRig()
        try:
            apply(rig)
            return rig.run_check()
        finally:
            rig.cleanup()

    def _assert_fail(self, result: subprocess.CompletedProcess[str], needle: str) -> None:
        self.assertNotEqual(
            result.returncode,
            0,
            msg=f"expected FAIL; got OK\nSTDOUT:{result.stdout}\nSTDERR:{result.stderr}",
        )
        haystack = result.stderr + result.stdout
        self.assertIn(needle, haystack, msg=f"expected {needle!r} in output:\n{haystack}")

    # ---- contract block shape ---- #

    def test_mutation_drops_required_workflow_id(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.patch_contract(lambda b: b["required_workflow_ids"].remove("LIVE_DESIGNATION"))

        self._assert_fail(self._run_mutation(mutate), "required_workflow_ids")

    def test_mutation_breaks_required_keys(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.patch_contract(lambda b: b.pop("snapshot_paths"))

        self._assert_fail(self._run_mutation(mutate), "missing required keys")

    def test_mutation_zero_min_surface_entries_per_workflow(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.patch_contract(lambda b: b.__setitem__("min_surface_entries_per_workflow", 0))

        self._assert_fail(self._run_mutation(mutate), "min_surface_entries_per_workflow")

    def test_mutation_min_surface_entries_per_workflow_is_bool(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.patch_contract(lambda b: b.__setitem__("min_surface_entries_per_workflow", True))

        self._assert_fail(self._run_mutation(mutate), "min_surface_entries_per_workflow")

    # ---- bucket coverage ---- #

    def test_mutation_workflow_references_unknown_capability(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            def patch(block: dict) -> None:
                block["ac_workflows"][0]["rest_capabilities"] = ["UNKNOWN_BUCKET"]

            rig.patch_contract(patch)

        self._assert_fail(self._run_mutation(mutate), "unknown buckets")

    def test_mutation_workflow_has_no_surface_entries(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            def patch(block: dict) -> None:
                # Wipe both REST + CLI from LIVE_DESIGNATION so the workflow
                # is documented but unreachable; AC requires at least one.
                wf = block["ac_workflows"][0]
                wf["rest_capabilities"] = []
                wf["cli_groups"] = []
                wf["websocket_channels"] = []

            rig.patch_contract(patch)

        self._assert_fail(self._run_mutation(mutate), "no REST capabilities AND no CLI groups")

    def test_mutation_capability_orphan(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            def patch(block: dict) -> None:
                # Drop WATCHLIST_CONFIG from orphan_buckets_allowed.rest so
                # the existing watchlist REST routes become an orphan.
                block["orphan_buckets_allowed"]["rest"] = []

            rig.patch_contract(patch)

        self._assert_fail(self._run_mutation(mutate), "WATCHLIST_CONFIG")

    # ---- confirmation guard ---- #

    def test_mutation_kill_switch_drops_confirmation(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_api/routes.py",
                'summary="Activate the QuantConnect Liquidate sequence '
                '(cancel, liquidate, halt, disconnect).",\n        srs_refs=("SRS-SAFE-001", "SYS-44a", '
                '"SYS-44b", "NFR-P3"),\n        request_fields=("confirm",),\n        response_fields=(\n'
                '            "activation_id",\n            "activated_at",\n            '
                '"cancelled_orders",\n            "liquidation_orders",\n            '
                '"paper_engines_halted",\n            "ib_gateway_disconnected",\n        ),\n        '
                "requires_confirmation=True,",
                'summary="Activate the QuantConnect Liquidate sequence '
                '(cancel, liquidate, halt, disconnect).",\n        srs_refs=("SRS-SAFE-001", "SYS-44a", '
                '"SYS-44b", "NFR-P3"),\n        request_fields=("confirm",),\n        response_fields=(\n'
                '            "activation_id",\n            "activated_at",\n            '
                '"cancelled_orders",\n            "liquidation_orders",\n            '
                '"paper_engines_halted",\n            "ib_gateway_disconnected",\n        ),\n        '
                "requires_confirmation=False,",
            )

        self._assert_fail(self._run_mutation(mutate), "requires_confirmation")

    def test_mutation_live_promote_drops_confirmation(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            # Find the `live promote` CLI Command and flip requires_confirmation.
            rig.replace_in(
                "python/atp_cli/commands.py",
                "summary=(\n            "
                '"Designate a strategy as the single live IB strategy. Enforces "\n            '
                '"the one-live-strategy invariant; requires --confirm."\n        ),\n        '
                'srs_refs=("SRS-API-001", "SYS-2c", "SYS-2d"),\n        arguments=(\n            '
                'Argument(\n                name="strategy_id",\n                '
                'summary="Strategy to promote to the live IB account.",\n                '
                "required=True,\n            ),\n            _CONFIRM,\n        ),\n        "
                "exit_codes=(\n            ExitCode.OK,\n            ExitCode.NOT_FOUND,\n            "
                "ExitCode.CONFIRMATION_REQUIRED,\n        ),\n        requires_confirmation=True,",
                "summary=(\n            "
                '"Designate a strategy as the single live IB strategy. Enforces "\n            '
                '"the one-live-strategy invariant; requires --confirm."\n        ),\n        '
                'srs_refs=("SRS-API-001", "SYS-2c", "SYS-2d"),\n        arguments=(\n            '
                'Argument(\n                name="strategy_id",\n                '
                'summary="Strategy to promote to the live IB account.",\n                '
                "required=True,\n            ),\n            _CONFIRM,\n        ),\n        "
                "exit_codes=(\n            ExitCode.OK,\n            ExitCode.NOT_FOUND,\n            "
                "ExitCode.CONFIRMATION_REQUIRED,\n        ),\n        requires_confirmation=False,",
            )

        self._assert_fail(self._run_mutation(mutate), "requires_confirmation")

    # ---- bind / auth policy ---- #

    def test_mutation_rest_remote_bind(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_api/routes.py",
                'BIND_HOST: str = "127.0.0.1"',
                'BIND_HOST: str = "0.0.0.0"',
            )

        self._assert_fail(self._run_mutation(mutate), "SRS-SEC-002")

    def test_mutation_ws_remote_bind(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_ws/channels.py",
                'BIND_HOST: str = "127.0.0.1"',
                'BIND_HOST: str = "0.0.0.0"',
            )

        self._assert_fail(self._run_mutation(mutate), "SRS-SEC-002")

    def test_mutation_cli_auth_model_drift(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_cli/commands.py",
                'AUTH_MODEL: str = "local-single-user"',
                'AUTH_MODEL: str = "oauth2"',
            )

        self._assert_fail(self._run_mutation(mutate), "expected_auth_model")

    # ---- snapshots ---- #

    def test_mutation_missing_openapi_snapshot(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.delete("python/atp_api/openapi.json")

        self._assert_fail(self._run_mutation(mutate), "openapi.json")

    # ---- deferred audit ---- #

    def test_mutation_empty_deferred_list(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.patch_contract(lambda b: b.__setitem__("deferred", []))

        self._assert_fail(self._run_mutation(mutate), "deferred")

    def test_mutation_drops_required_downstream_feature(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            def patch(block: dict) -> None:
                block["deferred"] = [
                    entry for entry in block["deferred"] if entry["feature"] != "SRS-LOG-001"
                ]

            rig.patch_contract(patch)

        self._assert_fail(self._run_mutation(mutate), "SRS-LOG-001")

    # ---- dependency direction / vendor isolation ---- #

    def test_mutation_cross_surface_import(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_api/routes.py",
                "from __future__ import annotations\n",
                "from __future__ import annotations\nfrom atp_cli.commands import COMMANDS  # noqa: F401\n",
            )

        self._assert_fail(self._run_mutation(mutate), "atp_cli")

    def test_mutation_vendor_token_leakage(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_api/__init__.py",
                '"""ATP REST API contract surface (API-2 / SRS-API-001).',
                '"""ATP REST API contract surface (API-2 / SRS-API-001). ibapi compat note.',
            )

        self._assert_fail(self._run_mutation(mutate), "ibapi")

    # ---- AC phrase fidelity ---- #

    def test_mutation_ac_phrase_drift(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.patch_contract(
                lambda b: b.__setitem__(
                    "ac_phrase_canonical",
                    "Live designation, kill switch, and logs",
                )
            )

        self._assert_fail(self._run_mutation(mutate), "ac_phrase_canonical")

    def test_mutation_duplicate_ac_phrase(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            def patch(block: dict) -> None:
                block["ac_workflows"][0]["ac_phrase"] = block["ac_workflows"][1]["ac_phrase"]

            rig.patch_contract(patch)

        self._assert_fail(self._run_mutation(mutate), "unique")


# --------------------------------------------------------------------------- #
# Pure-Python contract block parity
# --------------------------------------------------------------------------- #


class ContractBlockParityTest(unittest.TestCase):
    """Fast in-process parity assertions; do not spawn a subprocess."""

    def setUp(self) -> None:
        self.block = _load_contract()

    def test_required_workflow_ids_matches_ac_workflows_order(self) -> None:
        self.assertEqual(
            self.block["required_workflow_ids"],
            [wf["id"] for wf in self.block["ac_workflows"]],
        )
        self.assertEqual(len(self.block["required_workflow_ids"]), 8)

    def test_surface_packages_modules_exist_on_disk(self) -> None:
        for spec in self.block["surface_packages"]:
            module_path = ROOT / spec["module"]
            snap_path = ROOT / spec["snapshot"]
            self.assertTrue(module_path.is_file(), f"{spec['module']} missing")
            self.assertTrue(snap_path.is_file(), f"{spec['snapshot']} missing")

    def test_confirmation_workflows_are_a_subset_of_required(self) -> None:
        self.assertTrue(
            set(self.block["confirmation_required_workflows"])
            <= set(self.block["required_workflow_ids"])
        )

    def test_every_deferred_entry_has_feature_and_what(self) -> None:
        for entry in self.block["deferred"]:
            self.assertIsInstance(entry.get("feature"), str)
            self.assertTrue(entry["feature"].strip())
            self.assertIsInstance(entry.get("what"), str)
            self.assertTrue(entry["what"].strip())

    def test_ac_phrase_canonical_matches_srs_ac_verbatim(self) -> None:
        # The 8 labels in order, joined with Oxford-style comma + "and ",
        # must equal the AC phrase verbatim.
        expected = (
            "Live designation, strategy management, kill switch, Hot-Swap, "
            "Reservoir ranking, backtests, system status, and logs"
        )
        self.assertEqual(self.block["ac_phrase_canonical"], expected)

    def test_workflow_bucket_references_resolve(self) -> None:
        rest_buckets = {b.value for b in rest_routes.Capability}
        cli_buckets = {g.value for g in cli_commands.Group}
        ws_buckets = {c.value for c in ws_channels.Channel}
        for wf in self.block["ac_workflows"]:
            for cap in wf["rest_capabilities"]:
                self.assertIn(cap, rest_buckets, f"unknown REST capability {cap}")
            for grp in wf["cli_groups"]:
                self.assertIn(grp, cli_buckets, f"unknown CLI group {grp}")
            for chan in wf["websocket_channels"]:
                self.assertIn(chan, ws_buckets, f"unknown WS channel {chan}")


if __name__ == "__main__":
    unittest.main()
