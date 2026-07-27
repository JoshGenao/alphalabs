"""L1 unit tests for the SRS-EXE-007 IB TWS API version-upgrade gate.

``tools/ib_api_version_check.py`` binds the DECLARED IB TWS API version to a
paper-account validation record, so that SRS-EXE-007 clause 2 — "API version
upgrades are tested against the IB paper trading account before deployment to live
trading" — is enforced rather than merely documented.

Every case here drives the gate against a hermetic copy of the artifact tree
(``root=`` parameter), so nothing touches the real repository. The emphasis is on
the REFUSAL branches: a gate whose failure paths are untested is a gate that can
silently fail open.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import ib_api_version_check as gate  # noqa: E402

pytestmark = pytest.mark.unit

#: Everything the gate reads: the three architecture artifacts plus every source
#: file that feeds the evidence code digest.
TREE_FILES = (
    gate.SUPPORT_REL,
    gate.EVIDENCE_REL,
    gate.RUNTIME_REL,
    gate.ADAPTERS_LIB_REL,
    "crates/atp-adapters/src/interactive_brokers.rs",
    "crates/atp-adapters/src/interactive_brokers/wire.rs",
    "crates/atp-adapters/tests/srs_exe_006_ib_adapter.rs",
)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A hermetic copy of the artifacts + sources the gate inspects."""
    for rel in TREE_FILES:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)
    return tmp_path


def read_support(tree: Path) -> dict:
    return json.loads((tree / gate.SUPPORT_REL).read_text(encoding="utf-8"))


def write_support(tree: Path, payload: dict) -> None:
    (tree / gate.SUPPORT_REL).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_evidence(tree: Path) -> dict:
    return json.loads((tree / gate.EVIDENCE_REL).read_text(encoding="utf-8"))


def write_evidence(tree: Path, payload: dict) -> None:
    (tree / gate.EVIDENCE_REL).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _bump_declared_version(tree: Path, to: str = "10.30.1") -> None:
    lib = tree / gate.ADAPTERS_LIB_REL
    lib.write_text(
        lib.read_text(encoding="utf-8").replace(
            'INTERACTIVE_BROKERS_TWS_API_VERSION: &str = "10.19.4";',
            f'INTERACTIVE_BROKERS_TWS_API_VERSION: &str = "{to}";',
        ),
        encoding="utf-8",
    )


def _bump_everything_but_the_evidence(tree: Path, to: str = "10.30.1") -> None:
    """Declare `to` everywhere paperwork can declare it — adapter, runtime metadata
    and support record — while the recorded paper run still names the old version."""
    _bump_declared_version(tree, to)
    services = json.loads((tree / gate.RUNTIME_REL).read_text(encoding="utf-8"))
    services["adapter_contract"]["interactive_brokers"]["protocol_version"] = to
    (tree / gate.RUNTIME_REL).write_text(json.dumps(services, indent=2) + "\n", encoding="utf-8")
    support = read_support(tree)
    support["supported_tws_api_version"] = to
    write_support(tree, support)


def test_unmodified_tree_passes(tree: Path) -> None:
    assert gate.run(root=tree) == 0


# --------------------------------------------------------------------------
# Clause 2 — the upgrade gate itself
# --------------------------------------------------------------------------


def test_version_bump_without_a_new_paper_run_is_refused(tree: Path) -> None:
    """THE point of the feature: bumping the declared version while the support
    record still cites the old validation must fail before live deployment."""
    _bump_declared_version(tree)
    with pytest.raises(gate.IbApiVersionError, match="upgrade is NOT validated"):
        gate.run(root=tree)


def test_bumping_the_support_record_alone_is_refused(tree: Path) -> None:
    """The mirror image: claiming a new version in the record without touching the
    adapter (or running paper) must not pass either."""
    support = read_support(tree)
    support["supported_tws_api_version"] = "10.30.1"
    write_support(tree, support)
    with pytest.raises(gate.IbApiVersionError, match="upgrade is NOT validated"):
        gate.run(root=tree)


def test_forged_evidence_citation_is_refused(tree: Path) -> None:
    support = read_support(tree)
    support["validated_against_paper_account"]["evidence_code_digest"] = "0" * 64
    write_support(tree, support)
    with pytest.raises(gate.IbApiVersionError, match="hand-edited citation is not validation"):
        gate.run(root=tree)


def test_evidence_timestamp_drift_is_refused(tree: Path) -> None:
    support = read_support(tree)
    support["validated_against_paper_account"]["evidence_generated_at"] = "2020-01-01T00:00:00Z"
    write_support(tree, support)
    with pytest.raises(gate.IbApiVersionError, match="evidence_generated_at"):
        gate.run(root=tree)


def test_stale_evidence_after_a_wire_change_is_refused(tree: Path) -> None:
    """The evidence is bound to the wire code that produced it: edit the wire and the
    recorded validation no longer describes this tree."""
    wire = tree / "crates/atp-adapters/src/interactive_brokers/wire.rs"
    wire.write_text(wire.read_text(encoding="utf-8") + "\n// drift\n", encoding="utf-8")
    with pytest.raises(gate.IbApiVersionError, match="evidence is stale"):
        gate.run(root=tree)


def test_failed_paper_run_validates_nothing(tree: Path) -> None:
    evidence = read_evidence(tree)
    evidence["returncode"] = 101
    write_evidence(tree, evidence)
    with pytest.raises(gate.IbApiVersionError, match="did not pass"):
        gate.run(root=tree)


def test_evidence_for_a_different_wire_version_is_refused(tree: Path) -> None:
    evidence = read_evidence(tree)
    evidence["pinned_server_version"] = 999
    write_evidence(tree, evidence)
    with pytest.raises(gate.IbApiVersionError, match="different wire"):
        gate.run(root=tree)


def test_missing_evidence_is_refused(tree: Path) -> None:
    (tree / gate.EVIDENCE_REL).unlink()
    with pytest.raises(gate.IbApiVersionError, match="missing"):
        gate.run(root=tree)


def test_bumping_every_artifact_except_the_evidence_is_refused(tree: Path) -> None:
    """The strongest forgery attempt: lib.rs, the runtime metadata AND the support
    record all claim the new version, but the recorded paper run exercised the old
    one. The run — not the paperwork — is what validates a version."""
    _bump_everything_but_the_evidence(tree)
    with pytest.raises(gate.IbApiVersionError, match="exercised '10.19.4'"):
        gate.run(root=tree)


def test_evidence_without_a_recorded_api_version_validates_nothing(tree: Path) -> None:
    """A legacy artifact that does not say which API version it ran against cannot
    stand in for one — fail closed rather than assume it matches."""
    evidence = read_evidence(tree)
    del evidence["tws_api_version"]
    write_evidence(tree, evidence)
    with pytest.raises(gate.IbApiVersionError, match="does not say which IB TWS API version"):
        gate.run(root=tree)


@pytest.mark.parametrize("bogus", ["", "latest", "10.19", 10.194, None])
def test_malformed_recorded_api_version_is_refused(tree: Path, bogus: object) -> None:
    evidence = read_evidence(tree)
    evidence["tws_api_version"] = bogus
    write_evidence(tree, evidence)
    with pytest.raises(gate.IbApiVersionError, match="missing or malformed"):
        gate.run(root=tree)


# --------------------------------------------------------------------------
# Clause 1 — the documented version agrees everywhere it is stated
# --------------------------------------------------------------------------


def test_runtime_metadata_drift_is_refused(tree: Path) -> None:
    services = json.loads((tree / gate.RUNTIME_REL).read_text(encoding="utf-8"))
    services["adapter_contract"]["interactive_brokers"]["protocol_version"] = "9.9.9"
    (tree / gate.RUNTIME_REL).write_text(json.dumps(services, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(gate.IbApiVersionError, match="disagrees with the validated version"):
        gate.run(root=tree)


def test_wire_pin_drift_between_record_and_source_is_refused(tree: Path) -> None:
    support = read_support(tree)
    support["negotiated_server_version"] = 175
    write_support(tree, support)
    with pytest.raises(gate.IbApiVersionError, match="IB_PINNED_SERVER_VERSION"):
        gate.run(root=tree)


def test_missing_version_constant_is_refused(tree: Path) -> None:
    lib = tree / gate.ADAPTERS_LIB_REL
    lib.write_text(
        lib.read_text(encoding="utf-8").replace(
            "pub const INTERACTIVE_BROKERS_TWS_API_VERSION", "pub const RENAMED_AWAY"
        ),
        encoding="utf-8",
    )
    with pytest.raises(gate.IbApiVersionError, match="could not find"):
        gate.run(root=tree)


# --------------------------------------------------------------------------
# Fail-closed record parsing
# --------------------------------------------------------------------------


def test_missing_support_record_is_refused(tree: Path) -> None:
    (tree / gate.SUPPORT_REL).unlink()
    with pytest.raises(gate.IbApiVersionError, match="missing"):
        gate.run(root=tree)


def test_unparseable_support_record_is_refused(tree: Path) -> None:
    (tree / gate.SUPPORT_REL).write_text("{not json", encoding="utf-8")
    with pytest.raises(gate.IbApiVersionError, match="not valid JSON"):
        gate.run(root=tree)


def test_duplicate_keys_are_refused(tree: Path) -> None:
    """Last-one-wins on a safety artifact is silent ambiguity — refuse it."""
    (tree / gate.SUPPORT_REL).write_text(
        '{"schema_version": 1, "schema_version": 1}', encoding="utf-8"
    )
    with pytest.raises(gate.IbApiVersionError, match="duplicate key"):
        gate.run(root=tree)


def test_schema_drift_is_refused(tree: Path) -> None:
    support = read_support(tree)
    support["schema_version"] = 2
    write_support(tree, support)
    with pytest.raises(gate.IbApiVersionError, match="schema_version"):
        gate.run(root=tree)


@pytest.mark.parametrize("dropped", sorted(gate.SUPPORT_KEYS - {"schema_version"}))
def test_missing_top_level_key_is_refused(tree: Path, dropped: str) -> None:
    support = read_support(tree)
    del support[dropped]
    write_support(tree, support)
    with pytest.raises(gate.IbApiVersionError, match="key set must be exactly"):
        gate.run(root=tree)


def test_unknown_top_level_key_is_refused(tree: Path) -> None:
    support = read_support(tree)
    support["supported_tws_api_verison"] = "10.19.4"  # typo'd alias
    write_support(tree, support)
    with pytest.raises(gate.IbApiVersionError, match="unknown="):
        gate.run(root=tree)


@pytest.mark.parametrize("dropped", sorted(gate.VALIDATION_KEYS))
def test_missing_validation_key_is_refused(tree: Path, dropped: str) -> None:
    support = read_support(tree)
    del support["validated_against_paper_account"][dropped]
    write_support(tree, support)
    with pytest.raises(gate.IbApiVersionError, match="key set must be exactly"):
        gate.run(root=tree)


@pytest.mark.parametrize("bogus", ["", "latest", "10.19", " 10.19.4", "10.19.4 ", 10.194, None])
def test_degenerate_version_values_are_refused(tree: Path, bogus: object) -> None:
    support = read_support(tree)
    support["supported_tws_api_version"] = bogus
    write_support(tree, support)
    with pytest.raises(gate.IbApiVersionError, match="MAJOR.MINOR.PATCH"):
        gate.run(root=tree)


@pytest.mark.parametrize("bogus", [0, -176, True, "176", None])
def test_degenerate_server_versions_are_refused(tree: Path, bogus: object) -> None:
    support = read_support(tree)
    support["negotiated_server_version"] = bogus
    write_support(tree, support)
    with pytest.raises(gate.IbApiVersionError, match="positive integer"):
        gate.run(root=tree)


def test_blank_provenance_is_refused(tree: Path) -> None:
    support = read_support(tree)
    support["validated_against_paper_account"]["provenance"] = "   "
    write_support(tree, support)
    with pytest.raises(gate.IbApiVersionError, match="non-empty string"):
        gate.run(root=tree)


def test_evidence_ref_must_point_at_the_operator_artifact(tree: Path) -> None:
    support = read_support(tree)
    support["validated_against_paper_account"]["evidence_ref"] = "architecture/other.json"
    write_support(tree, support)
    with pytest.raises(gate.IbApiVersionError, match="evidence_ref must be"):
        gate.run(root=tree)


# --------------------------------------------------------------------------
# The operator --sync path must not launder an unvalidated upgrade
# --------------------------------------------------------------------------


def test_sync_refuses_when_no_new_paper_run_exists(tree: Path) -> None:
    with pytest.raises(gate.IbApiVersionError, match="nothing to sync"):
        gate.sync(root=tree)


def test_sync_refuses_a_bump_with_the_same_old_run(tree: Path) -> None:
    """Bump the constant, then try to bless it by syncing: the evidence is unchanged,
    so there is nothing new to record and the upgrade stays unvalidated."""
    _bump_declared_version(tree)
    with pytest.raises(gate.IbApiVersionError, match="nothing to sync"):
        gate.sync(root=tree)
    with pytest.raises(gate.IbApiVersionError, match="upgrade is NOT validated"):
        gate.run(root=tree)


def test_sync_refuses_a_new_run_that_exercised_the_old_version(tree: Path) -> None:
    """A fresh paper run is necessary but not sufficient: if the operator re-ran the
    round trip while the OLD version was declared, it validates the old version only."""
    _bump_declared_version(tree)
    evidence = read_evidence(tree)
    evidence["generated_at"] = "2026-08-01T12:00:00Z"  # a genuinely new run...
    write_evidence(tree, evidence)  # ...but tws_api_version is still 10.19.4
    with pytest.raises(gate.IbApiVersionError, match="refusing to record an unvalidated upgrade"):
        gate.sync(root=tree)


def test_sync_records_a_genuinely_new_paper_run(tree: Path) -> None:
    """After a real re-run at the new version (the operator path writes both a new
    generated_at and the declared tws_api_version), --sync records it and the gate
    goes green."""
    _bump_declared_version(tree)
    services = json.loads((tree / gate.RUNTIME_REL).read_text(encoding="utf-8"))
    services["adapter_contract"]["interactive_brokers"]["protocol_version"] = "10.30.1"
    (tree / gate.RUNTIME_REL).write_text(json.dumps(services, indent=2) + "\n", encoding="utf-8")

    evidence = read_evidence(tree)
    evidence["generated_at"] = "2026-08-01T12:00:00Z"
    evidence["tws_api_version"] = "10.30.1"
    write_evidence(tree, evidence)

    assert gate.sync(root=tree) == 0
    assert gate.run(root=tree) == 0
    support = read_support(tree)
    assert support["supported_tws_api_version"] == "10.30.1"
    assert support["validated_against_paper_account"]["evidence_generated_at"] == (
        "2026-08-01T12:00:00Z"
    )


@pytest.mark.parametrize(
    ("field", "bogus"),
    [
        ("schema_version", 99),
        ("test", "some_other_test"),
        ("gate_env", "ATP_RUN_ANYTHING"),
        ("paper_port", 4001),
        ("result_line", "test result: FAILED. 0 passed; 1 failed"),
        ("result_line", ""),
    ],
)
def test_evidence_that_is_not_shaped_like_an_operator_run_is_refused(
    tree: Path, field: str, bogus: object
) -> None:
    """`run` and `--sync` demand the same operator-run shape, so an artifact that was
    assembled rather than produced by the ATP_RUN_INTEGRATION path is refused by both."""
    evidence = read_evidence(tree)
    evidence[field] = bogus
    write_evidence(tree, evidence)
    with pytest.raises(gate.IbApiVersionError):
        gate.run(root=tree)

    evidence["generated_at"] = "2026-08-01T12:00:00Z"  # pretend it is a new run too
    write_evidence(tree, evidence)
    with pytest.raises(gate.IbApiVersionError):
        gate.sync(root=tree)


@pytest.mark.parametrize(
    "dropped", ["schema_version", "test", "gate_env", "paper_port", "result_line"]
)
def test_evidence_missing_an_operator_run_field_is_refused(tree: Path, dropped: str) -> None:
    evidence = read_evidence(tree)
    del evidence[dropped]
    write_evidence(tree, evidence)
    with pytest.raises(gate.IbApiVersionError):
        gate.run(root=tree)


def test_sync_refuses_a_failed_run(tree: Path) -> None:
    evidence = read_evidence(tree)
    evidence["generated_at"] = "2026-08-01T12:00:00Z"
    evidence["returncode"] = 7
    write_evidence(tree, evidence)
    with pytest.raises(gate.IbApiVersionError, match="failed paper-account run"):
        gate.sync(root=tree)


def test_sync_refuses_evidence_that_does_not_match_this_wire(tree: Path) -> None:
    evidence = read_evidence(tree)
    evidence["generated_at"] = "2026-08-01T12:00:00Z"
    evidence["code_digest"] = "1" * 64
    write_evidence(tree, evidence)
    with pytest.raises(gate.IbApiVersionError, match="re-run the paper-account round trip"):
        gate.sync(root=tree)
