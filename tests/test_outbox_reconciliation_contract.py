"""Contract tests for SRS-EXE-009 (SyRS SYS-90 / NFR-R3 / NFR-R4; StRS SN-2.05).

Mirrors ``tests/test_live_designation_contract.py``: shells out to
``tools/outbox_reconciliation_check.py``, then exercises each per-check function
in-process, including negative spot-checks that verify the contract actually
catches regressions (a renamed write-ahead/reconcile method, a removed duplicate
rejection, a dropped durable MAGIC, an out-of-order fsync/rename sequence, a
missing coverage/conflict variant or plan field, a dropped is_terminal retention
authority, a missing binary/test artifact, and a deferred[] that drops a real-IB
owner).
"""

from __future__ import annotations

import copy
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from outbox_reconciliation_check import (  # noqa: E402
    OutboxReconciliationCheckError,
    assert_outbox_reconciliation_static,
    check_coverage_artifacts,
    check_deferred,
    check_durable_codec,
    check_durable_submit,
    check_metadata,
    check_outbox_api,
    check_reconciliation,
    check_terminal_states,
    load_config,
    outbox_source,
    run_checks,
)


class OutboxReconciliationScriptTest(unittest.TestCase):
    def test_srs_exe_009_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/outbox_reconciliation_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-EXE-009 PASS", result.stdout)
        for needle in (
            "pins SRS-EXE-009",
            "commit_intent",
            "DuplicateClientCorrelationId",
            "sync_all -> rename -> sync_all",
            "reconcile over BrokerOpenOrderSource",
            "skip_bound, adopt_ack, resubmit, mark_terminal, unresolved",
            "OpenOnly, OpenAndRecentlyCompleted",
            "route_order_durably",
            "self.designation.authority_for",
            "FILLED, CANCELLED, REJECTED, EXPIRED",
            "exe009_outbox_reconcile_cli",
            "SRS-EXE-006 / SRS-EXE-001 / SRS-EXE-008",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class StaticCheckBase(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.source = outbox_source(self.config)


class MetadataTest(StaticCheckBase):
    def test_metadata_ok(self) -> None:
        self.assertIn("SRS-EXE-009", check_metadata(self.config, self.source))

    def test_wrong_requirement_is_caught(self) -> None:
        mutated = copy.deepcopy(self.config)
        mutated["outbox_reconciliation_contract"]["requirement"] = "SRS-EXE-999"
        with self.assertRaises(OutboxReconciliationCheckError):
            check_metadata(mutated, self.source)

    def test_missing_syrs_ref_is_caught(self) -> None:
        mutated = copy.deepcopy(self.config)
        mutated["outbox_reconciliation_contract"]["syrs_refs"] = ["SYS-90", "NFR-R3"]
        with self.assertRaises(OutboxReconciliationCheckError) as ctx:
            check_metadata(mutated, self.source)
        self.assertIn("NFR-R4", str(ctx.exception))


class OutboxApiTest(StaticCheckBase):
    def test_api_ok(self) -> None:
        evidence = check_outbox_api(self.config, self.source)
        self.assertIn("commit_intent", evidence)

    def test_renamed_write_ahead_method_is_caught(self) -> None:
        mutated = self.source.replace("pub fn commit_intent", "pub fn commit_intentX")
        with self.assertRaises(OutboxReconciliationCheckError) as ctx:
            check_outbox_api(self.config, mutated)
        self.assertIn("commit_intent", str(ctx.exception))

    def test_removed_duplicate_rejection_is_caught(self) -> None:
        mutated = self.source.replace(
            "OrderErrorCategory::DuplicateClientCorrelationId", "OrderErrorCategory::Whatever"
        )
        with self.assertRaises(OutboxReconciliationCheckError) as ctx:
            check_outbox_api(self.config, mutated)
        self.assertIn("DuplicateClientCorrelationId", str(ctx.exception))


class DurableCodecTest(StaticCheckBase):
    def test_codec_ok(self) -> None:
        self.assertIn(
            "sync_all -> rename -> sync_all", check_durable_codec(self.config, self.source)
        )

    def test_dropped_magic_is_caught(self) -> None:
        mutated = self.source.replace('"ATP-ORDER-OUTBOX-V1"', '"WRONG-MAGIC"')
        with self.assertRaises(OutboxReconciliationCheckError):
            check_durable_codec(self.config, mutated)

    def test_out_of_order_durability_sequence_is_caught(self) -> None:
        # Force the parent-dir fsync to precede the rename by removing the file
        # sync_all so the first `sync_all` found sits AFTER `rename`.
        mutated = self.source.replace(
            ".and_then(|()| scratch.sync_all())", ".and_then(|()| Ok(()))"
        )
        with self.assertRaises(OutboxReconciliationCheckError) as ctx:
            check_durable_codec(self.config, mutated)
        self.assertIn("order", str(ctx.exception).lower())


class ReconciliationTest(StaticCheckBase):
    def test_reconciliation_ok(self) -> None:
        self.assertIn("ReconciliationPlan", check_reconciliation(self.config, self.source))

    def test_renamed_reconcile_entry_is_caught(self) -> None:
        mutated = self.source.replace("pub fn reconcile", "pub fn reconcileX")
        with self.assertRaises(OutboxReconciliationCheckError):
            check_reconciliation(self.config, mutated)

    def test_missing_coverage_variant_is_caught(self) -> None:
        mutated = self.source.replace("OpenAndRecentlyCompleted", "SomethingElse")
        with self.assertRaises(OutboxReconciliationCheckError) as ctx:
            check_reconciliation(self.config, mutated)
        self.assertIn("OpenAndRecentlyCompleted", str(ctx.exception))

    def test_missing_plan_field_is_caught(self) -> None:
        mutated = self.source.replace("pub resubmit:", "pub notresubmit:")
        with self.assertRaises(OutboxReconciliationCheckError) as ctx:
            check_reconciliation(self.config, mutated)
        self.assertIn("resubmit", str(ctx.exception))


class TerminalStatesTest(StaticCheckBase):
    def test_terminal_states_ok(self) -> None:
        self.assertIn("is_terminal", check_terminal_states(self.config, self.source))

    def test_missing_is_terminal_is_caught(self) -> None:
        mutated = self.source.replace("is_terminal()", "always_false()")
        with self.assertRaises(OutboxReconciliationCheckError) as ctx:
            check_terminal_states(self.config, mutated)
        self.assertIn("is_terminal", str(ctx.exception))


class CoverageArtifactsTest(StaticCheckBase):
    def test_artifacts_present(self) -> None:
        self.assertIn(
            "exe009_outbox_reconcile_cli", check_coverage_artifacts(self.config, self.source)
        )

    def test_missing_binary_is_caught(self) -> None:
        mutated = copy.deepcopy(self.config)
        mutated["outbox_reconciliation_contract"]["cli_binary"] = "no_such_binary"
        with self.assertRaises(OutboxReconciliationCheckError) as ctx:
            check_coverage_artifacts(mutated, self.source)
        self.assertIn("no_such_binary", str(ctx.exception))

    def test_missing_domain_test_is_caught(self) -> None:
        mutated = copy.deepcopy(self.config)
        mutated["outbox_reconciliation_contract"]["domain_test"] = "tests/domain/test_absent.py"
        with self.assertRaises(OutboxReconciliationCheckError):
            check_coverage_artifacts(mutated, self.source)


class DeferredTest(StaticCheckBase):
    def test_deferred_ok(self) -> None:
        self.assertIn("SRS-EXE-006", check_deferred(self.config, self.source))

    def test_dropped_owner_is_caught(self) -> None:
        mutated = copy.deepcopy(self.config)
        mutated["outbox_reconciliation_contract"]["deferred"] = [
            "only names SRS-EXE-001 and SRS-EXE-008, passes:false"
        ]
        with self.assertRaises(OutboxReconciliationCheckError) as ctx:
            check_deferred(mutated, self.source)
        self.assertIn("SRS-EXE-006", str(ctx.exception))


class DurableSubmitTest(StaticCheckBase):
    def test_durable_submit_ok(self) -> None:
        self.assertIn("submit_live_order_durably", check_durable_submit(self.config, self.source))

    def test_renamed_durable_submit_method_is_caught(self) -> None:
        mutated = copy.deepcopy(self.config)
        mutated["outbox_reconciliation_contract"]["durable_submit"]["method"] = "no_such_method"
        with self.assertRaises(OutboxReconciliationCheckError) as ctx:
            check_durable_submit(mutated, self.source)
        self.assertIn("no_such_method", str(ctx.exception))

    def test_missing_seam_test_is_caught(self) -> None:
        mutated = copy.deepcopy(self.config)
        mutated["outbox_reconciliation_contract"]["durable_submit"]["seam_test"] = (
            "srs_exe_009_absent"
        )
        with self.assertRaises(OutboxReconciliationCheckError) as ctx:
            check_durable_submit(mutated, self.source)
        self.assertIn("srs_exe_009_absent", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_nine_evidence_items(self) -> None:
        # 8 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(run_checks()), 9)

    def test_static_emits_eight_evidence_items(self) -> None:
        self.assertEqual(len(assert_outbox_reconciliation_static(load_config(), ROOT)), 8)


if __name__ == "__main__":
    unittest.main()
