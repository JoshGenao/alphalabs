"""L7 domain safety test for SRS-EXE-007 — no live deployment on an unvalidated
IB TWS API version.

The trading-domain invariant: the brokerage adapter speaks a specific IB TWS API
generation, and SyRS SYS-65 / SRS-EXE-007 require that *"API version upgrades are
tested against the IB paper trading account before deployment to live trading"*.
Deploying a live strategy against an API version whose wire behaviour was never
exercised on the paper account risks silent order-encoding drift on the real
account — the highest-consequence failure this system has.

Before this feature the requirement was unenforced: the declared version constant
was bound to nothing an operator paper run produces, so a bump shipped green. This
test pins the enforcement in the same three layers the SRS-EXE-006 domain test uses:

1. **Behavioral** — the real gate passes against the real tree.
2. **Structural non-vacuity** — each refusal is fed a mutated tree and MUST fail, so
   the gate cannot rot into a no-op that reports PASS on an unvalidated upgrade.
3. **Scope honesty** — the paper-account leg the gate depends on stays declared as
   operator-gated evidence; the gate never fabricates it.

Paired with the safety-path change in ``tools/ib_api_version_check.py`` +
``architecture/ib_api_version_support.json`` (see ``progress.d/session-SRS-EXE-007.md``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import ib_api_version_check as gate  # noqa: E402

pytestmark = pytest.mark.domain

TREE_FILES = (
    gate.SUPPORT_REL,
    gate.EVIDENCE_REL,
    gate.RUNTIME_REL,
    gate.ADAPTERS_LIB_REL,
    "crates/atp-adapters/src/interactive_brokers.rs",
    "crates/atp-adapters/src/interactive_brokers/wire.rs",
    "crates/atp-adapters/tests/srs_exe_006_ib_adapter.rs",
)

VERSION_LINE = 'INTERACTIVE_BROKERS_TWS_API_VERSION: &str = "10.19.4";'


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for rel in TREE_FILES:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)
    return tmp_path


def _bump_declared_version(tree: Path, to: str = "10.30.1") -> None:
    lib = tree / gate.ADAPTERS_LIB_REL
    text = lib.read_text(encoding="utf-8")
    assert VERSION_LINE in text, "the version constant moved — this test must be updated"
    lib.write_text(
        text.replace(VERSION_LINE, f'INTERACTIVE_BROKERS_TWS_API_VERSION: &str = "{to}";'),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# 1. Behavioral — the shipped gate passes on the shipped tree
# --------------------------------------------------------------------------


def test_gate_passes_on_the_real_tree() -> None:
    proc = subprocess.run(
        [sys.executable, str(TOOLS / "ib_api_version_check.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc.returncode == 0, f"gate failed on the real tree:\n{proc.stdout}\n{proc.stderr}"
    assert "SRS-EXE-007 IB TWS API VERSION GATE PASS" in proc.stdout


# --------------------------------------------------------------------------
# 2. Structural non-vacuity — the safety invariant, from every angle
# --------------------------------------------------------------------------


def test_unvalidated_version_upgrade_cannot_pass(tree: Path) -> None:
    """THE invariant. A newly declared API version with no fresh paper-account run
    must be refused before it can reach live trading."""
    _bump_declared_version(tree)
    with pytest.raises(gate.IbApiVersionError) as excinfo:
        gate.run(root=tree)
    message = str(excinfo.value)
    assert "NOT validated" in message
    # The refusal must tell the operator how to validate it, not just that it failed.
    assert "paper" in message.lower()
    assert "ATP_RUN_INTEGRATION=1" in message


def _bump_everything_but_the_evidence(tree: Path, to: str = "10.30.1") -> None:
    _bump_declared_version(tree, to)
    support = json.loads((tree / gate.SUPPORT_REL).read_text(encoding="utf-8"))
    support["supported_tws_api_version"] = to
    (tree / gate.SUPPORT_REL).write_text(json.dumps(support, indent=2) + "\n", encoding="utf-8")
    services = json.loads((tree / gate.RUNTIME_REL).read_text(encoding="utf-8"))
    services["adapter_contract"]["interactive_brokers"]["protocol_version"] = to
    (tree / gate.RUNTIME_REL).write_text(json.dumps(services, indent=2) + "\n", encoding="utf-8")


def test_upgrade_cannot_be_blessed_by_editing_the_paperwork(tree: Path) -> None:
    """Declaring the new version in EVERY document — adapter, runtime metadata and
    support record — must still fail. Only the recorded run validates a version, and
    the recorded run says it exercised 10.19.4."""
    _bump_everything_but_the_evidence(tree)
    with pytest.raises(gate.IbApiVersionError, match="exercised '10.19.4'"):
        gate.run(root=tree)
    # ...and the operator path refuses to launder it too.
    with pytest.raises(gate.IbApiVersionError, match="nothing to sync"):
        gate.sync(root=tree)


def test_a_fresh_run_at_the_old_version_does_not_validate_the_new_one(tree: Path) -> None:
    """A new paper run is necessary but not sufficient — it must have been run WITH the
    new version declared. Re-running at the old version cannot bless an upgrade."""
    _bump_everything_but_the_evidence(tree)
    evidence = json.loads((tree / gate.EVIDENCE_REL).read_text(encoding="utf-8"))
    evidence["generated_at"] = "2026-08-01T12:00:00Z"  # genuinely new run...
    (tree / gate.EVIDENCE_REL).write_text(  # ...but it exercised 10.19.4
        json.dumps(evidence, indent=2) + "\n", encoding="utf-8"
    )
    with pytest.raises(gate.IbApiVersionError, match="exercised '10.19.4'"):
        gate.run(root=tree)
    with pytest.raises(gate.IbApiVersionError, match="refusing to record an unvalidated upgrade"):
        gate.sync(root=tree)


def test_evidence_that_names_no_api_version_validates_nothing(tree: Path) -> None:
    """Fail closed on a legacy artifact: "it did not say" is never "it matches"."""
    evidence = json.loads((tree / gate.EVIDENCE_REL).read_text(encoding="utf-8"))
    del evidence["tws_api_version"]
    (tree / gate.EVIDENCE_REL).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(gate.IbApiVersionError, match="does not say which IB TWS API version"):
        gate.run(root=tree)


def test_a_hand_assembled_record_cannot_pass_either_path(tree: Path) -> None:
    """The lowest-effort forgery — write a plausible-looking record by hand — must fail:
    every field the operator writer stamps (schema, test name, gate env, paper port,
    passing cargo line) is required, by BOTH run() and sync(), through one function.

    This raises the cost of an accidental or careless bless. It is NOT a cryptographic
    attestation that the run happened — see the session note's named follow-up on the
    repo-wide operator-evidence trust boundary.
    """
    hand_written = {
        "schema_version": 1,
        "test": "paper_account_round_trip",
        "gate_env": "ATP_RUN_INTEGRATION",
        "paper_port": 4002,
        "tws_api_version": "10.30.1",
        "returncode": 0,
        "result_line": "looks fine to me",  # not the cargo success line
        "code_digest": "0" * 64,
        "generated_at": "2026-08-01T12:00:00Z",
    }
    (tree / gate.EVIDENCE_REL).write_text(
        json.dumps(hand_written, indent=2) + "\n", encoding="utf-8"
    )
    _bump_everything_but_the_evidence(tree)
    with pytest.raises(gate.IbApiVersionError, match="result_line"):
        gate.run(root=tree)
    with pytest.raises(gate.IbApiVersionError, match="result_line"):
        gate.sync(root=tree)


def test_sync_is_never_weaker_than_the_gate() -> None:
    """`--sync` writes the record the gate then trusts, so it must demand the same
    operator-run shape. Both call the one shared checker — structurally, not by
    convention — so the two cannot drift apart."""
    src = (TOOLS / "ib_api_version_check.py").read_text(encoding="utf-8")
    assert src.count("check_operator_run_shape(evidence, runtime)") == 2


def test_operator_run_records_the_version_it_exercised() -> None:
    """The binding only holds if the operator path WRITES the version it ran at, so
    the evidence writer must read it from the declared constant — never a fixed
    literal that could drift from what actually ran."""
    src = (TOOLS / "ib_adapter_check.py").read_text(encoding="utf-8")
    assert '"tws_api_version": _declared_tws_api_version()' in src
    assert "INTERACTIVE_BROKERS_TWS_API_VERSION" in src


def test_gate_never_fabricates_paper_evidence(tree: Path) -> None:
    """With the evidence artifact absent, the answer is "cannot verify" — never a
    pass. A safety gate that treats missing evidence as success is worse than none."""
    (tree / gate.EVIDENCE_REL).unlink()
    with pytest.raises(gate.IbApiVersionError):
        gate.run(root=tree)


def test_failed_paper_run_is_not_a_validation(tree: Path) -> None:
    evidence = json.loads((tree / gate.EVIDENCE_REL).read_text(encoding="utf-8"))
    evidence["returncode"] = 1
    (tree / gate.EVIDENCE_REL).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(gate.IbApiVersionError, match="did not pass"):
        gate.run(root=tree)


def test_wire_change_invalidates_the_recorded_validation(tree: Path) -> None:
    """The paper run validates a specific wire codec. Change the codec and the
    validation no longer describes the code that would trade live."""
    wire = tree / "crates/atp-adapters/src/interactive_brokers/wire.rs"
    wire.write_text(wire.read_text(encoding="utf-8") + "\n// silent encoding change\n", "utf-8")
    with pytest.raises(gate.IbApiVersionError, match="stale"):
        gate.run(root=tree)


def test_gate_reports_failure_through_the_process_exit_code(tree: Path) -> None:
    """CI trusts the exit code: a refusal must be non-zero and must not print the
    success sentinel anywhere."""
    _bump_declared_version(tree)
    script = tree / "tools" / "ib_api_version_check.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TOOLS / "ib_api_version_check.py", script)
    shutil.copy2(TOOLS / "ib_adapter_check.py", script.parent / "ib_adapter_check.py")
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, cwd=tree)
    assert proc.returncode != 0
    assert "GATE PASS" not in proc.stdout
    assert "GATE PASS" not in proc.stderr
    assert "FAIL" in proc.stderr


# --------------------------------------------------------------------------
# 3. Scope honesty — the operator leg stays operator-gated
# --------------------------------------------------------------------------


def test_paper_validation_remains_operator_gated_evidence() -> None:
    """The gate proves the *policy* solo; the round trip itself is the operator's
    ATP_RUN_INTEGRATION run against port 4002. That split must stay declared, so a
    green gate is never mistaken for "the new version was tested here"."""
    services = json.loads((ROOT / gate.RUNTIME_REL).read_text(encoding="utf-8"))
    integration = services["adapter_contract"]["ib_brokerage_runtime"]["integration_test"]
    assert integration["gate_env"] == "ATP_RUN_INTEGRATION"
    assert integration["paper_port"] == 4002

    support = json.loads((ROOT / gate.SUPPORT_REL).read_text(encoding="utf-8"))
    provenance = support["validated_against_paper_account"]["provenance"]
    assert "paper" in provenance.lower()
    # The record must name where the validation came from — never an unsourced claim.
    assert provenance.strip()

    source = (TOOLS / "ib_api_version_check.py").read_text(encoding="utf-8")
    assert "no network" in source
    # The gate must not run the live round trip itself — it inspects recorded
    # evidence only, so it can never manufacture the validation it checks for.
    assert "import subprocess" not in source
    assert "import socket" not in source
