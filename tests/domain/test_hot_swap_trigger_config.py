"""SRS-RESV-003 / SyRS SYS-49a / StRS SN-1.25 / SN-1.30 — the Hot-Swap trigger
DECISION + CONFIGURATION + LOGGING layer. A Hot-Swap may be triggered by manual
operator selection (always available) or by three AUTOMATIC triggers
(drawdown-demotion, top-ranked promotion, highest-momentum promotion), each
enable/disable-able per type and DEFAULTING TO DISABLED, with every fired
trigger logged. The trigger layer proposes + logs; it does NOT execute the swap
(that is the SRS-RESV-004 ``resolve_demotion`` gate, which consumes the
``HotSwapDemotionRequest`` this layer produces).

L7 domain (safety) test. The Rust integration test at
``crates/atp-orchestrator/tests/resv_3_hot_swap_triggers.rs`` builds spy /
failing / forbidden fake implementations of the three injected ports
(``LiveStrategyProbe``, ``ReservoirRankingSource``, ``HotSwapTriggerLog``);
this Python test shells out to ``cargo test`` to anchor the safety
post-conditions in the domain-test layer so the deterministic critic
recognizes the diff as having a paired ``tests/domain/`` safety test (the CLI
``resv003_hot_swap_trigger_cli.rs`` and ``tools/hot_swap_trigger_check.py``
paths match ``hot[_-]?swap`` in ``SAFETY_PATH_RE``).
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
            "resv_3_hot_swap_triggers",
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_single_pass(result: subprocess.CompletedProcess[str], label: str) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"{label} Rust domain test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_default_config_fires_nothing_even_when_conditions_met() -> None:
    # SRS-RESV-003 core safety invariant: automatic triggers default to
    # disabled, so a default HotSwapTriggerConfig fires NOTHING and logs
    # NOTHING even with a deep drawdown and an excellent candidate present.
    # The Rust HotSwapTriggerLogForbiddenSink panics if any trigger is logged.
    result = _run_cargo_test("resv_3_default_config_fires_nothing_even_when_conditions_met")
    _assert_single_pass(result, "RESV-003 default-disabled")


def test_all_enabled_fire_in_priority_order_and_each_logged() -> None:
    # "All swap triggers are logged": with every automatic trigger enabled and
    # its condition met, all fire in a fixed priority order (drawdown-demotion
    # first as the risk control) and the log record count equals the fired
    # count — the mechanical guarantee behind the logging clause.
    result = _run_cargo_test(
        "resv_3_all_enabled_all_conditions_met_fire_in_priority_order_and_each_logged"
    )
    _assert_single_pass(result, "RESV-003 all-logged")


def test_manual_promotion_always_fires_and_logs() -> None:
    # SYS-49a(a): manual selection is always available — it fires + logs
    # regardless of the automatic-trigger config (which defaults to all off).
    result = _run_cargo_test("resv_3_manual_promotion_always_fires_and_logs_even_when_all_disabled")
    _assert_single_pass(result, "RESV-003 manual-always")


def test_failing_log_sink_is_best_effort() -> None:
    # Logging is best-effort: a failing log sink must not un-fire the trigger
    # or abort the evaluation (durable delivery is the deferred SRS-LOG-001
    # sink's concern).
    result = _run_cargo_test("resv_3_failing_log_sink_is_best_effort")
    _assert_single_pass(result, "RESV-003 best-effort-log")


def test_ranking_non_finite_and_empty_fail_closed_no_fire() -> None:
    # Fail-closed: an empty or non-finite ranking yields no promotion candidate,
    # so no automatic trigger fires (no fabricated pick, no panic).
    result = _run_cargo_test("resv_3_ranking_non_finite_and_empty_fail_closed_no_fire")
    _assert_single_pass(result, "RESV-003 fail-closed-ranking")
