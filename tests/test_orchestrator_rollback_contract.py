"""SRS-ORCH-005 rollback — L3 contract test.

Drives ``tools/orchestrator_rollback_check.py`` (asserting the PASS banner +
evidence needles), then imports each static guard and injects a regression to
prove it is non-vacuous. The cargo suites are exercised by the script's own
smoke (and the domain test); the static guards here run cargo-free.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from orchestrator_rollback_check import (  # noqa: E402
    RollbackCheckError,
    _read,
    assert_rollback_static,
    check_confirmation_parity,
    check_handler_surface,
    check_retention_port,
    check_rollback_cli,
    check_rollback_gate_order,
    contract_block,
    load_config,
)


class ScriptRunTest(unittest.TestCase):
    def test_script_passes_with_evidence(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/orchestrator_rollback_check.py", "--skip-cargo"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ORCH-005 PASS", result.stdout)
        for needle in (
            "retention port",
            "gate order",
            "NFR-S2 parity",
            "operator bin",
            "surface wiring",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()

    def _src(self, key: str) -> str:
        return _read(self.config, key, ROOT)

    def _mutate(self, key: str, old: str, new: str) -> str:
        mutated = self._src(key).replace(old, new)
        self.assertNotEqual(mutated, self._src(key), f"mutation no-op for {key}: {old!r}")
        return mutated


class RetentionPortTest(_Fixture):
    def test_detached_supertrait_is_caught(self) -> None:
        # Detaching the subtrait from the frozen ORCH-004 port would let a rollback registry
        # skip the record/lookup contract entirely.
        mutated = self._mutate(
            "orchestrator_source",
            "pub trait RetainedDeployedVersionRegistry: DeployedVersionRegistry",
            "pub trait RetainedDeployedVersionRegistry",
        )
        with self.assertRaises(RollbackCheckError):
            check_retention_port(self.config, mutated)

    def test_dropped_same_hash_guard_is_caught(self) -> None:
        # Without the same-hash guard, a redeploy of the identical version would overwrite the
        # genuine previous version with a self-referential copy (a version its own target).
        mutated = self._mutate(
            "orchestrator_source",
            "existing.current.source_hash == version.source_hash",
            "false",
        )
        with self.assertRaises(RollbackCheckError):
            check_retention_port(self.config, mutated)


class GateOrderTest(_Fixture):
    def test_dropped_live_check_is_caught(self) -> None:
        mutated = self._mutate("orchestrator_source", "current_live()", "assumed_not_live()")
        with self.assertRaises(RollbackCheckError):
            check_rollback_gate_order(self.config, mutated)

    def test_fail_open_probe_is_caught(self) -> None:
        # Turning the probe failure into an assumed not-live would waive NFR-S2 exactly when
        # the safety data is degraded.
        mutated = self._mutate("orchestrator_source", "LiveStatusUnavailable", "AssumedNotLive")
        with self.assertRaises(RollbackCheckError):
            check_rollback_gate_order(self.config, mutated)

    def test_swallowed_registry_failure_is_caught(self) -> None:
        # A rollback whose write failed did not happen; swallowing it would lie to the operator.
        mutated = self._mutate(
            "orchestrator_source", ".map_err(RollbackError::RegistryFailed)?", ".ok();"
        )
        with self.assertRaises(RollbackCheckError):
            check_rollback_gate_order(self.config, mutated)


class ConfirmationParityTest(_Fixture):
    def test_dropped_empty_acknowledgement_rejection_is_caught(self) -> None:
        mutated = self._mutate("orchestrator_source", "trim().is_empty()", "is_char_boundary(0)")
        with self.assertRaises(RollbackCheckError):
            check_confirmation_parity(self.config, mutated, self._src("designation_source"))

    def test_parity_drift_in_designation_is_caught(self) -> None:
        # The parity check reads BOTH sources: if live promotion's control changed shape, the
        # mirror claim would silently rot without this.
        mutated = self._mutate("designation_source", "fn from_operator(", "fn from_anyone(")
        with self.assertRaises(RollbackCheckError):
            check_confirmation_parity(self.config, self._src("orchestrator_source"), mutated)


class RollbackCliTest(_Fixture):
    def test_dropped_snapshot_magic_is_caught(self) -> None:
        mutated = self._mutate("cli_bin_source", "STATE_MAGIC", "STATE_HEADER")
        with self.assertRaises(RollbackCheckError):
            check_rollback_cli(self.config, mutated)

    def test_dropped_atomic_publish_is_caught(self) -> None:
        mutated = self._mutate("cli_bin_source", "fs::rename", "fs::copy")
        with self.assertRaises(RollbackCheckError):
            check_rollback_cli(self.config, mutated)


class HandlerSurfaceTest(_Fixture):
    def test_dropped_confirmed_recheck_is_caught(self) -> None:
        mutated = self._mutate("handler_source", "if not request.confirmed:", "if False:")
        with self.assertRaises(RollbackCheckError):
            check_handler_surface(self.config, mutated)

    def test_hijacked_lifecycle_owner_is_caught(self) -> None:
        # Non-rollback lifecycle actions must keep their honest 501 naming SRS-ORCH-004 —
        # registering on the shared route must not over-claim start/stop/restart.
        mutated = self._mutate("handler_source", 'owner="SRS-ORCH-004"', 'owner="SRS-ORCH-005"')
        with self.assertRaises(RollbackCheckError):
            check_handler_surface(self.config, mutated)


class AggregateEvidenceTest(_Fixture):
    def test_static_check_count_is_pinned(self) -> None:
        # Five static guards (retention port, gate order, confirmation parity, operator bin,
        # surface wiring). A dropped or silently-added guard changes this count — pin it.
        self.assertEqual(len(assert_rollback_static(self.config, ROOT)), 5)

    def test_block_names_the_deferred_owners(self) -> None:
        block = contract_block(self.config)
        self.assertEqual(block["requirement"], "SRS-ORCH-005")
        deferred = " ".join(block["deferred"]).lower()
        for owner in ("srs-ui-001", "srs-exe-001", "srs-api-001"):
            self.assertIn(owner, deferred, f"deferred owners must name {owner!r}")


if __name__ == "__main__":
    unittest.main()
