"""L3 contract tests for SRS-MD-006 (startup readiness runtime probes).

Pins the ``startup_readiness_runtime_contract`` block against the shipped
code and anchors the check tool's PASS discipline plus the invariants a
future edit must not weaken (single SubCheck vocabulary, mandatory alert
sink, exact freshness boundary, ERR-9 leakage rule after the gate
extension)."""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((REPO_ROOT / "architecture" / "runtime_services.json").read_text())
BLOCK = CONTRACT["startup_readiness_runtime_contract"]


class RuntimeCheckScriptTest(unittest.TestCase):
    def test_check_tool_passes_and_names_the_deferred_legs(self) -> None:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "startup_readiness_runtime_check.py")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-MD-006 RUNTIME-PROBES PASS", result.stdout)
        self.assertIn("integration-gated", result.stdout)
        self.assertIn("deferred", result.stdout)

    def test_sdk_gate_check_still_passes_after_the_extension(self) -> None:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "startup_readiness_gate_check.py")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ERR-9 SDK-SURFACE PASS", result.stdout)


class ContractParityTest(unittest.TestCase):
    def test_module_paths_exist(self) -> None:
        for rel in BLOCK["module_paths"]:
            self.assertTrue((REPO_ROOT / rel).is_file(), rel)

    def test_subcheck_vocabulary_is_single_sourced(self) -> None:
        from atp_reliability.restart import SubCheck

        self.assertEqual(BLOCK["subcheck_source"], "atp_reliability.restart")
        self.assertEqual(sorted(BLOCK["subchecks"]), sorted(c.value for c in SubCheck))

    def test_service_and_paper_vocabularies_match_enums(self) -> None:
        from atp_readiness.runtime import PaperPrerequisite, ReadinessService

        self.assertEqual(
            sorted(BLOCK["required_services"]), sorted(s.value for s in ReadinessService)
        )
        self.assertEqual(
            sorted(BLOCK["paper_prerequisites"]),
            sorted(p.value for p in PaperPrerequisite),
        )

    def test_subcheck_category_map_matches_code(self) -> None:
        from atp_readiness.runtime import _SUBCHECK_CATEGORY

        code_map = {check.value: category.value for check, category in _SUBCHECK_CATEGORY.items()}
        self.assertEqual(BLOCK["subcheck_category_map"], code_map)

    def test_alert_sink_is_mandatory_keyword_only(self) -> None:
        from atp_readiness import runtime as rt

        for fn_name in (
            "build_runtime_report",
            "assert_paper_ready_or_hold",
            "release_hold_with_override",
        ):
            parameter = inspect.signature(getattr(rt, fn_name)).parameters["alert_sink"]
            self.assertIs(parameter.default, inspect.Parameter.empty, fn_name)
            self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY, fn_name)

    def test_freshness_boundary_wording_stays_strict(self) -> None:
        boundary = BLOCK["freshness_boundary"]
        self.assertIn("most recent COMPLETED trading session", boundary)
        self.assertIn("one nanosecond earlier is stale", boundary)
        self.assertIn("None frontier", boundary)

    def test_gate_module_still_leak_free(self) -> None:
        gate_src = (REPO_ROOT / "python" / "atp_readiness" / "gate.py").read_text()
        for token in ("ib_gateway", "ingestion_freshness", "nas_reach", "service_health"):
            self.assertNotIn(token, gate_src)

    def test_deferred_names_the_remaining_owners(self) -> None:
        joined = json.dumps(BLOCK["deferred"])
        for owner in ("SRS-EXE-006", "SRS-ORCH-004", "SRS-NOTIF-001", "SRS-LOG-001"):
            self.assertIn(owner, joined)


if __name__ == "__main__":
    unittest.main()
