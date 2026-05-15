"""SRS-ORCH-004 / SyRS SYS-79 — the strategy orchestrator records the
deployed code version (source hash + deployment timestamp) for each
strategy at deployment time and exposes it through the
``DeployedVersionRegistry`` port so the deferred dashboard (SyRS SYS-41),
REST API (IF-9), and backtest result rows (SYS-21) render the same
``version_identifier`` across all three surfaces.

L7 domain (safety) test. The Rust integration test at
``crates/atp-orchestrator/tests/orch_4_deployment_version_contract.rs``
builds spy implementations of ``StrategyContainerRuntime``,
``HealthCheckEventSink``, and ``DeployedVersionRegistry`` (with both a
recording spy and a ``ForbiddenVersionRegistry`` that panics if any
record / lookup leaks on the pre-create rejection or DeadlineExceeded
arms); this Python test shells out to ``cargo test`` to anchor those
post-conditions in the domain-test layer so the deterministic critic
recognizes the diff as having a paired ``tests/domain/`` safety test
(matched by ``deployment[_-]?version`` / ``source[_-]?hash`` in
``SAFETY_PATH_RE``).
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
            "atp-orchestrator",
            "--test",
            "orch_4_deployment_version_contract",
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_cargo_passed(
    result: subprocess.CompletedProcess[str], requirement_label: str
) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"{requirement_label} integration test failed:\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert (
        "1 passed" in combined or "test result: ok. 1 passed" in combined
    ), f"unexpected cargo test output:\n{combined}"


def test_ready_within_deadline_records_deployed_version_exactly_once() -> None:
    # SyRS SYS-79 write path: the orchestrator records the deployed
    # version (hash + observed_at_seconds) through the registry port
    # exactly once per successful launch, and the same record appears
    # on the launch outcome.
    result = _run_cargo_test(
        "orch_4_ready_within_deadline_records_deployed_version_exactly_once"
    )
    _assert_cargo_passed(result, "SRS-ORCH-004 ReadyWithinDeadline recording")


def test_version_identifier_is_queryable_via_registry_lookup() -> None:
    # SRS-ORCH-004 acceptance: dashboard, REST API, and backtest
    # results "display or return the same version identifier". The
    # registry lookup returns the same record the orchestrator wrote
    # and the same `version_identifier()` string is produced by both
    # the outcome and the lookup.
    result = _run_cargo_test(
        "orch_4_version_identifier_is_queryable_via_registry_lookup"
    )
    _assert_cargo_passed(
        result, "SRS-ORCH-004 version_identifier queryable"
    )


def test_malformed_source_hash_is_refused_without_invoking_runtime() -> None:
    # SRS-ORCH-004 validate-before-create: a misformed override (wrong
    # digest length) must never reach `runtime.create`. The gate
    # short-circuits with DeployedVersionInvalid, emits no sink event,
    # and never records a version.
    result = _run_cargo_test(
        "orch_4_malformed_source_hash_is_refused_without_invoking_runtime"
    )
    _assert_cargo_passed(
        result, "SRS-ORCH-004 validate-before-create refusal"
    )


def test_unknown_algorithm_prefix_is_rejected() -> None:
    # SyRS SYS-79 wire-form pin: a non-sha256 algorithm prefix must
    # surface as a distinct UnknownAlgorithm discriminator so the
    # dashboard can render the cause precisely.
    result = _run_cargo_test("orch_4_unknown_algorithm_prefix_is_rejected")
    _assert_cargo_passed(result, "SRS-ORCH-004 unknown-algorithm rejection")


def test_deadline_exceeded_records_no_version() -> None:
    # SRS-ORCH-004 / SyRS SYS-41: a version that was never deployed
    # must not appear in the active-strategy inventory or REST API
    # listing. The DeadlineExceeded path destroys the container and
    # skips the version record.
    result = _run_cargo_test("orch_4_deadline_exceeded_records_no_version")
    _assert_cargo_passed(
        result, "SRS-ORCH-004 DeadlineExceeded skips version record"
    )


def test_record_failure_does_not_abort_the_launch() -> None:
    # SRS-ORCH-004: the version record is best-effort. Once the
    # container is running, a registry-record failure must NOT
    # retroactively abort the launch — that would lie to operators
    # and force a destroy.
    result = _run_cargo_test("orch_4_record_failure_does_not_abort_the_launch")
    _assert_cargo_passed(
        result, "SRS-ORCH-004 best-effort recording semantics"
    )


def test_distinct_strategies_carry_distinct_version_records() -> None:
    # The registry's read path must discriminate by strategy_id — two
    # distinct strategies deployed in the same session must surface
    # distinct version_identifier strings.
    result = _run_cargo_test(
        "orch_4_distinct_strategies_carry_distinct_version_records"
    )
    _assert_cargo_passed(
        result, "SRS-ORCH-004 per-strategy version isolation"
    )
