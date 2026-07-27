"""L3 contract test — SRS-EXE-007 IB TWS API version agreement across the tree.

Clause 1 of SRS-EXE-007 ("the adapter documents the supported IB TWS API version")
is stated in four places that can drift independently: the Rust constant, the
architecture metadata, the version-support record, and the paper-account evidence
artifact. This test pins them together on the REAL tree, so drift fails here (with a
precise message) and not only inside the CI check script.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import ib_api_version_check as gate  # noqa: E402

pytestmark = pytest.mark.contract


def _load(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def test_gate_passes_against_the_real_tree() -> None:
    assert gate.run(root=ROOT) == 0


def test_declared_version_matches_the_support_record() -> None:
    support = _load(gate.SUPPORT_REL)
    assert gate.declared_tws_api_version(ROOT) == support["supported_tws_api_version"]


def test_runtime_metadata_mirrors_the_declared_version() -> None:
    support = _load(gate.SUPPORT_REL)
    services = _load(gate.RUNTIME_REL)
    ib_meta = services["adapter_contract"]["interactive_brokers"]
    assert ib_meta["protocol_version"] == support["supported_tws_api_version"]
    assert ib_meta["protocol_version_constant"] == "INTERACTIVE_BROKERS_TWS_API_VERSION"


def test_wire_pin_agrees_between_source_metadata_record_and_evidence() -> None:
    support = _load(gate.SUPPORT_REL)
    services = _load(gate.RUNTIME_REL)
    runtime = services["adapter_contract"]["ib_brokerage_runtime"]
    evidence = _load(gate.EVIDENCE_REL)
    pinned = gate.pinned_server_version(runtime, ROOT)
    assert pinned == runtime["pinned_server_version"]
    assert pinned == support["negotiated_server_version"]
    assert pinned == evidence["pinned_server_version"]


def test_support_record_cites_the_current_paper_account_evidence() -> None:
    validation = _load(gate.SUPPORT_REL)["validated_against_paper_account"]
    evidence = _load(gate.EVIDENCE_REL)
    assert validation["evidence_ref"] == gate.EVIDENCE_REL
    assert validation["evidence_code_digest"] == evidence["code_digest"]
    assert validation["evidence_generated_at"] == evidence["generated_at"]
    assert evidence["returncode"] == 0


def test_the_recorded_run_exercised_the_declared_api_version() -> None:
    """The clause-2 binding: the paper-account artifact names the IB TWS API version
    it ran against, and that is the version the adapter declares."""
    evidence = _load(gate.EVIDENCE_REL)
    assert evidence["tws_api_version"] == gate.declared_tws_api_version(ROOT)


def test_the_documented_version_is_a_real_version_string() -> None:
    assert re.match(r"^\d+\.\d+\.\d+$", gate.declared_tws_api_version(ROOT))


def test_the_upgrade_procedure_is_documented_for_operators() -> None:
    """SYS-65 requires the supported version be documented; the procedure that keeps
    it honest must be discoverable next to it."""
    readme = (ROOT / "architecture" / "README.md").read_text(encoding="utf-8")
    assert "ib_api_version_check.py" in readme
    assert "ATP_RUN_INTEGRATION=1 python3 tools/ib_adapter_check.py" in readme
    assert "--sync" in readme


def test_the_gate_runs_in_both_ci_paths() -> None:
    """A check that is not wired into CI is documentation, not enforcement."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    mirror = (ROOT / "tools" / "run_ci_locally.sh").read_text(encoding="utf-8")
    assert "ib_api_version" in workflow
    assert "ib_api_version_check" in mirror
