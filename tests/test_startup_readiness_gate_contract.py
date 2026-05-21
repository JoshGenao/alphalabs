"""L3 contract tests for the ERR-9 SDK-surface startup readiness gate.

Two layers of evidence:

* :class:`StartupReadinessGateScriptTest` runs ``tools/startup_readiness_gate_check.py``
  as a subprocess so the positive-evidence path stays under CI coverage.
* :class:`StartupReadinessGateMutationTest` rebuilds ``python/atp_readiness``
  in a temporary copy of the repo, mutates one rule, and re-runs the check
  to ensure each rule has an L3 negative-case anchor — preventing silent
  rule regressions.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_readiness import (  # noqa: E402  (path manipulation must come first)
    GateState,
    GateTransitionError,
    OperatorOverride,
    OverrideAuditError,
    PreTradeHoldError,
    ReadinessGate,
)

# --------------------------------------------------------------------------- #
# Subprocess positive-evidence
# --------------------------------------------------------------------------- #


class StartupReadinessGateScriptTest(unittest.TestCase):
    def test_script_returns_zero_and_prints_sdk_surface_pass(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/startup_readiness_gate_check.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("ERR-9 SDK-SURFACE PASS", result.stdout)
        # The PASS line must name each downstream owner so contract drift is loud.
        for owner in ("SRS-LOG-001", "SRS-UI-001", "SRS-API-001", "SRS-MD-006"):
            self.assertIn(owner, result.stdout, f"PASS line should mention {owner}")


# --------------------------------------------------------------------------- #
# Mutation rig
# --------------------------------------------------------------------------- #


class _MutationRig:
    """Copy the repo into a tempdir, apply a mutation, and re-run the check."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="atp-readiness-mutation-"))
        for entry in ("architecture", "python", "tools"):
            shutil.copytree(ROOT / entry, self.tmp / entry)
        # Original sys.path is restored on cleanup; the mutated repo inserts
        # its own python/ first so the subprocess picks up the mutated copy.
        self.env = {
            "PYTHONPATH": str(self.tmp / "python"),
        }

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel: str, content: str) -> None:
        target = self.tmp / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def replace_in(self, rel: str, old: str, new: str) -> None:
        path = self.tmp / rel
        text = path.read_text(encoding="utf-8")
        if old not in text:
            raise AssertionError(f"mutation source {old!r} not found in {rel}")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def run_check(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.tmp / "tools" / "startup_readiness_gate_check.py")],
            cwd=self.tmp,
            capture_output=True,
            text=True,
            env={**self.env, "PATH": __import__("os").environ.get("PATH", "")},
            check=False,
        )


class StartupReadinessGateMutationTest(unittest.TestCase):
    """One L3 negative-case anchor per rule in the contract block.

    Each mutation breaks exactly one contract clause and re-runs the check.
    The check MUST return non-zero (``ERR-9 SDK-SURFACE FAIL``) with a stderr
    message naming the broken clause.
    """

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
        self.assertNotEqual(result.returncode, 0, msg=f"expected FAIL; got OK\n{result.stdout}")
        haystack = result.stderr + result.stdout
        self.assertIn(needle, haystack, msg=f"expected {needle!r} in output:\n{haystack}")

    # ---- exports / hierarchy ---- #

    def test_mutation_drops_required_export(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_readiness/__init__.py",
                '    "OperatorOverride",\n',
                "",
            )

        self._assert_fail(self._run_mutation(mutate), "required_exports")

    def test_mutation_breaks_error_hierarchy(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_readiness/errors.py",
                "class PreTradeHoldError(ReadinessGateError):",
                "class PreTradeHoldError(Exception):",
            )

        self._assert_fail(self._run_mutation(mutate), "subclass ReadinessGateError")

    # ---- state machine ---- #

    def test_mutation_renames_state_variant(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            # Rename the OVERRIDDEN string value so the enum still loads but
            # the variant list no longer matches the contract block.
            rig.replace_in(
                "python/atp_readiness/gate.py",
                'OVERRIDDEN = "overridden"',
                'OVERRIDDEN = "overrode"',
            )

        self._assert_fail(self._run_mutation(mutate), "gate_state_variants")

    def test_mutation_allows_forbidden_transition(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            # Add (READY, OVERRIDDEN) to allowed transitions even though
            # the contract forbids it.
            rig.replace_in(
                "python/atp_readiness/gate.py",
                "(GateState.OVERRIDDEN, GateState.READY),",
                "(GateState.OVERRIDDEN, GateState.READY),\n        "
                "(GateState.READY, GateState.OVERRIDDEN),",
            )

        self._assert_fail(self._run_mutation(mutate), "forbidden transition")

    def test_mutation_drops_allowed_transition(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_readiness/gate.py",
                "(GateState.PRE_TRADE_BLOCKED, GateState.READY),\n",
                "",
            )

        self._assert_fail(self._run_mutation(mutate), "allowed transition")

    # ---- override audit fields ---- #

    def test_mutation_drops_audit_field(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_readiness/override.py",
                "    audit_trail_id: str\n",
                "",
            )

        self._assert_fail(self._run_mutation(mutate), "audit")

    def test_mutation_accepts_empty_actor(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            # Make the empty-string guard a no-op for `actor` only.
            rig.replace_in(
                "python/atp_readiness/gate.py",
                'for field_name in ("actor", "reason", "audit_trail_id"):',
                'for field_name in ("reason", "audit_trail_id"):',
            )

        self._assert_fail(self._run_mutation(mutate), "empty string")

    def test_mutation_accepts_negative_timestamp(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_readiness/gate.py",
                "if timestamp_ns < 0 or not math.isfinite(timestamp_ns):",
                "if False:",
            )

        self._assert_fail(self._run_mutation(mutate), "OverrideAuditError")

    def test_mutation_accepts_bool_timestamp(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_readiness/gate.py",
                "if isinstance(timestamp_ns, bool) or not isinstance(timestamp_ns, int):",
                "if not isinstance(timestamp_ns, int):",
            )

        self._assert_fail(self._run_mutation(mutate), "OverrideAuditError")

    # ---- payload shapes ---- #

    def test_mutation_drops_dashboard_payload_field(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_readiness/gate.py",
                '"overrides": [o.as_dict() for o in self._overrides],',
                "",
            )

        self._assert_fail(self._run_mutation(mutate), "dashboard payload")

    def test_mutation_drops_log_record_field(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_readiness/gate.py",
                '"srs_trace": list(failure.srs_trace),',
                "# field removed",
            )

        self._assert_fail(self._run_mutation(mutate), "log record")

    # ---- runtime-probe leakage ---- #

    def test_mutation_leaks_runtime_probe_token(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_readiness/gate.py",
                "from atp_config import (",
                "# ingestion_freshness probe placeholder (must not land here)\nfrom atp_config import (",
            )

        self._assert_fail(self._run_mutation(mutate), "deferred SRS-MD-006")

    # ---- env type guard ---- #

    def test_mutation_drops_env_mapping_guard(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            # Neutralise the Mapping check so non-Mapping envs slip through.
            rig.replace_in(
                "python/atp_readiness/gate.py",
                "if not isinstance(env, Mapping):",
                "if False:",
            )

        self._assert_fail(self._run_mutation(mutate), "non-Mapping")

    # ---- dependency direction ---- #

    def test_mutation_imports_upstream_atp_strategy(self) -> None:
        def mutate(rig: _MutationRig) -> None:
            rig.replace_in(
                "python/atp_readiness/gate.py",
                "from atp_config import (",
                "from atp_strategy.api import Strategy  # noqa\nfrom atp_config import (",
            )

        self._assert_fail(self._run_mutation(mutate), "upstream")


# --------------------------------------------------------------------------- #
# In-process behavioural assertions (no subprocess)
# --------------------------------------------------------------------------- #


def _defaults() -> dict[str, str]:
    from atp_config import REQUIRED_KEYS

    return {spec.name: spec.default for spec in REQUIRED_KEYS if spec.default is not None}


class ContractBlockParityTest(unittest.TestCase):
    """The Python code and the architecture metadata block must stay in parity."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.block = json.loads(
            (ROOT / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
        )["startup_readiness_gate_contract"]

    def test_block_lists_python_module_paths(self) -> None:
        for path in self.block["module_paths"]:
            self.assertTrue((ROOT / path).exists(), f"missing module path {path}")

    def test_block_variants_match_enum(self) -> None:
        self.assertEqual(
            self.block["gate_state_variants"],
            [state.value for state in GateState],
        )

    def test_block_exports_match_package(self) -> None:
        import atp_readiness

        self.assertEqual(
            sorted(self.block["required_exports"]),
            sorted(atp_readiness.__all__),
        )

    def test_block_documents_at_least_five_deferred_owners(self) -> None:
        # SRS-LOG-001, SRS-UI-001, SRS-API-001, SRS-MD-006, SRS-NOTIF-001 at minimum.
        deferred = " ".join(self.block["deferred"])
        for owner in ("SRS-LOG-001", "SRS-UI-001", "SRS-API-001", "SRS-MD-006", "SRS-NOTIF-001"):
            self.assertIn(owner, deferred, f"deferred[] should name {owner}")


class TransitionGateBehaviourTest(unittest.TestCase):
    def test_initializing_is_starting_state(self) -> None:
        self.assertIs(ReadinessGate().state, GateState.INITIALIZING)

    def test_from_env_with_clean_defaults_reaches_ready(self) -> None:
        gate = ReadinessGate.from_env(_defaults())
        self.assertIs(gate.state, GateState.READY)
        gate.assert_ready_or_hold()

    def test_from_env_with_missing_key_holds_pre_trade(self) -> None:
        env = _defaults()
        env.pop("DATABENTO_API_KEY")
        gate = ReadinessGate.from_env(env)
        self.assertIs(gate.state, GateState.PRE_TRADE_BLOCKED)
        with self.assertRaises(PreTradeHoldError):
            gate.assert_ready_or_hold()

    def test_assert_ready_or_hold_includes_report(self) -> None:
        env = _defaults()
        env.pop("DATABENTO_API_KEY")
        gate = ReadinessGate.from_env(env)
        with self.assertRaises(PreTradeHoldError) as ctx:
            gate.assert_ready_or_hold()
        self.assertEqual(len(ctx.exception.report.errors), 1)
        self.assertEqual(ctx.exception.report.errors[0].key, "DATABENTO_API_KEY")

    def test_initializing_gate_refuses_assert_ready(self) -> None:
        gate = ReadinessGate()
        with self.assertRaises(GateTransitionError):
            gate.assert_ready_or_hold()

    def test_reevaluate_after_fix_transitions_to_ready(self) -> None:
        env = _defaults()
        env.pop("DATABENTO_API_KEY")
        gate = ReadinessGate.from_env(env)
        self.assertIs(gate.state, GateState.PRE_TRADE_BLOCKED)
        gate.reevaluate(_defaults())
        self.assertIs(gate.state, GateState.READY)

    def test_override_only_from_pre_trade(self) -> None:
        ov = OperatorOverride(
            actor="alice@example.com",
            reason="r",
            audit_trail_id="a-1",
            timestamp_ns=time.time_ns(),
        )
        ready = ReadinessGate.from_env(_defaults())
        with self.assertRaises(GateTransitionError):
            ready.operator_override(ov)
        env = _defaults()
        env.pop("DATABENTO_API_KEY")
        held = ReadinessGate.from_env(env)
        held.operator_override(ov)
        self.assertIs(held.state, GateState.OVERRIDDEN)
        self.assertEqual(len(held.overrides), 1)

    def test_override_persists_through_reevaluate_to_ready(self) -> None:
        env = _defaults()
        env.pop("DATABENTO_API_KEY")
        gate = ReadinessGate.from_env(env)
        gate.operator_override(
            OperatorOverride(
                actor="alice@example.com",
                reason="r",
                audit_trail_id="a-2",
                timestamp_ns=time.time_ns(),
            )
        )
        gate.reevaluate(_defaults())
        self.assertIs(gate.state, GateState.READY)
        self.assertEqual(len(gate.overrides), 1)


class OperatorOverrideAuditTest(unittest.TestCase):
    def _seeded_gate(self) -> ReadinessGate:
        env = _defaults()
        env.pop("DATABENTO_API_KEY")
        return ReadinessGate.from_env(env)

    def test_rejects_empty_actor(self) -> None:
        gate = self._seeded_gate()
        with self.assertRaises(OverrideAuditError):
            gate.operator_override(
                OperatorOverride(actor="", reason="r", audit_trail_id="a", timestamp_ns=0)
            )

    def test_rejects_whitespace_only_reason(self) -> None:
        gate = self._seeded_gate()
        with self.assertRaises(OverrideAuditError):
            gate.operator_override(
                OperatorOverride(actor="a", reason="   ", audit_trail_id="a", timestamp_ns=0)
            )

    def test_rejects_negative_timestamp(self) -> None:
        gate = self._seeded_gate()
        with self.assertRaises(OverrideAuditError):
            gate.operator_override(
                OperatorOverride(actor="a", reason="r", audit_trail_id="a", timestamp_ns=-1)
            )

    def test_rejects_bool_timestamp(self) -> None:
        gate = self._seeded_gate()
        with self.assertRaises(OverrideAuditError):
            gate.operator_override(
                OperatorOverride(actor="a", reason="r", audit_trail_id="a", timestamp_ns=True)  # type: ignore[arg-type]
            )

    def test_rejects_non_override_payload(self) -> None:
        gate = self._seeded_gate()
        with self.assertRaises(OverrideAuditError):
            gate.operator_override({"actor": "a"})  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
