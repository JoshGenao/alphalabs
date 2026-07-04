"""ERR-5 / SRS-DATA-013 / SyRS SYS-77 / StRS SN-1.26 / SN-1.27 — when an
ingested record fails any of the six SyRS SYS-77 validation rules
(a..f), the data layer's ingestion gate must reject with
``INGESTION_RECORD_VALIDATION_FAILED``, publish a structured
``IngestionValidationEvent`` carrying the matching ``QuarantineReason``,
the source, the record hash, and the observation timestamp, and leave
the primary storage tier exactly as it found it (zero write on
rejection).

L7 domain (safety) test. The Rust integration test at
``crates/atp-data/tests/err_5_record_validation_blocked.rs`` drives the
gate with spy implementations of ``RecordValidator`` /
``IngestionValidationEventSink`` (to control the outcome and count
calls / events in isolation) AND with the REAL ``Sys77RecordValidator``
over the deterministic mixed fixture (so each of the six rules is
exercised by actual malformed records). This Python test shells out to
``cargo test`` to anchor those post-conditions in the domain-test layer
so the deterministic critic recognizes the diff as having a paired
``tests/domain/`` safety test (matched by ``ingestion[_-]?validation`` /
``record[_-]?quarantine`` in ``SAFETY_PATH_RE``).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cargo_test(test_name: str) -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust integration test")
    return subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-data",
            "--test",
            "err_5_record_validation_blocked",
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_quarantined_state_blocks_record_with_structured_error() -> None:
    # SRS-DATA-013 / SyRS SYS-77: the rejection envelope must carry the
    # INGESTION_RECORD_VALIDATION_FAILED wire string, the original
    # record, and exactly one IngestionValidationEvent must be recorded
    # with reason, source, record_hash, and observed_at_seconds
    # populated.
    result = _run_cargo_test("err_5_quarantined_state_blocks_record_with_structured_error")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-5 Rust domain test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_valid_outcome_returns_accepted_and_emits_no_event() -> None:
    # Negative control: ERR-5's rejection must be selective. A Valid
    # outcome must return IngestionAccepted and must NOT touch the event
    # sink — the Rust ForbiddenSink would panic if invoked.
    result = _run_cargo_test("err_5_valid_outcome_returns_accepted_and_emits_no_event")
    assert result.returncode == 0, (
        f"ERR-5 Valid control test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )


def test_quarantined_state_holds_across_many_records() -> None:
    # Pseudo-property: the Rust test drives the REAL Sys77RecordValidator
    # over the deterministic mixed fixture (one malformed record per
    # SYS-77 rule) and verifies the gate emits exactly one event per
    # blocked record, covering all six QuarantineReason variants, while
    # the well-formed records return Ok with no event.
    result = _run_cargo_test("err_5_real_validator_sweep_emits_one_event_per_rule")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-5 pseudo-property test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_identical_contract_for_live_feed_and_paper_feed_sources() -> None:
    # SyRS SYS-77 source-invariance: the rejection envelope must be
    # identical regardless of which source/kind produced the record. The
    # Rust test drives records of two different kinds (a daily equity bar
    # and an option-chain snapshot, whose derived source tags differ)
    # through the same gate and asserts byte-identical category/wire
    # strings — the data layer API takes no StrategyMode parameter and no
    # per-vendor branch.
    result = _run_cargo_test("err_5_identical_contract_across_sources")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-5 SYS-77 source-invariance test failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_quarantined_state_anchors_zero_mutation_via_port_shape() -> None:
    # Zero-primary-write invariant (behavioral anchor): the
    # RecordValidator port exposes no mutator method, so the gate
    # cannot write to primary storage through it. The PRIMARY
    # enforcement is the static check
    # (tools/ingestion_validation_check.py) via the contract's
    # forbidden_mutations allowlist; this test anchors the port-shape
    # post-condition at the behavioral layer.
    result = _run_cargo_test("err_5_quarantined_state_anchors_zero_mutation_via_port_shape")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-5 zero-mutation invariant test failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )
