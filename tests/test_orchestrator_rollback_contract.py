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
    check_dashboard_arm,
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
            "dashboard arm",
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


class DashboardArmTest(_Fixture):
    """SYS-80's third surface. Each mutation is a way the dashboard arm could
    regress to the read-only surface it used to be, or to an actionable control
    that overstates what the runtime actually confirmed."""

    def _check(self, server: str | None = None, app: str | None = None) -> str:
        return check_dashboard_arm(
            self.config,
            server if server is not None else self._src("dashboard_server_source"),
            app if app is not None else self._src("dashboard_app_source"),
        )

    def test_dropped_composition_is_caught(self) -> None:
        # Without the composition the control POSTs into the bare runtime's 501:
        # a dashboard that presents a rollback it cannot perform.
        mutated = self._mutate(
            "dashboard_server_source",
            "mount_rollback(runtime, state_path=deployment_state)",
            "pass  # composition dropped",
        )
        with self.assertRaises(RollbackCheckError):
            self._check(server=mutated)

    def test_dropped_confirm_token_is_caught(self) -> None:
        # Dropping the confirm token would make every dashboard rollback 428 —
        # and, worse, invites "fixing" it by weakening the guard instead.
        mutated = self._mutate("dashboard_app_source", '"/lifecycle?confirm=true"', '"/lifecycle"')
        with self.assertRaises(RollbackCheckError):
            self._check(app=mutated)

    def test_unevidenced_success_rendering_is_caught(self) -> None:
        # A bare 2xx must never read as a rollback that happened.
        mutated = self._mutate(
            "dashboard_app_source", 'body.lifecycle_state === "rolled-back"', "true"
        )
        with self.assertRaises(RollbackCheckError):
            self._check(app=mutated)

    def test_dropped_mutual_exclusion_is_caught(self) -> None:
        # Two staged live-state mutations at once leaves the operator unable to
        # say which control a response belongs to.
        mutated = self._mutate("dashboard_app_source", "controlsBusy()", "promoteInFlight")
        with self.assertRaises(RollbackCheckError):
            self._check(app=mutated)

    def test_dropped_capability_probe_is_caught(self) -> None:
        # Without the probe only mount_default_dashboard would report the truth;
        # the public mount_dashboard(..., inventory=...) path could serve rows
        # with retained previous versions and no handler, rendering an
        # actionable control that posts into the bare runtime's 501.
        mutated = self._mutate(
            "dashboard_server_source", "inventory.bind_rollback_probe(", "_unused = ("
        )
        with self.assertRaises(RollbackCheckError):
            self._check(server=mutated)

    def test_capability_inferred_from_row_data_is_caught(self) -> None:
        # A retained previous version says nothing about whether the route is
        # served; inferring the capability from the data is the fail-open.
        mutated = self._mutate(
            "dashboard_app_source",
            'rollbackAvailable === true && targetHash !== ""',
            'targetHash !== ""',
        )
        with self.assertRaises(RollbackCheckError):
            self._check(app=mutated)

    def test_dropped_inert_state_is_caught(self) -> None:
        # SYS-80 is inert before a second deployment: a strategy with no
        # retained previous version must not present an actionable rollback.
        mutated = self._mutate("dashboard_app_source", "rb.disabled = true", "rb.disabled = false")
        with self.assertRaises(RollbackCheckError):
            self._check(app=mutated)


class AggregateEvidenceTest(_Fixture):
    def test_static_check_count_is_pinned(self) -> None:
        # Six static guards (retention port, gate order, confirmation parity, operator bin,
        # surface wiring, dashboard arm). A dropped or silently-added guard changes this
        # count — pin it.
        self.assertEqual(len(assert_rollback_static(self.config, ROOT)), 6)

    def test_block_names_the_deferred_owners(self) -> None:
        block = contract_block(self.config)
        self.assertEqual(block["requirement"], "SRS-ORCH-005")
        deferred = " ".join(block["deferred"]).lower()
        for owner in ("srs-ui-001", "srs-exe-001", "srs-api-001"):
            self.assertIn(owner, deferred, f"deferred owners must name {owner!r}")


if __name__ == "__main__":
    unittest.main()
