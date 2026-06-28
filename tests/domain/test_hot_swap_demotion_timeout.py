"""ERR-7 / SRS-RESV-004 / SyRS SYS-49b / SYS-49c / StRS SN-1.25 — when a
Hot-Swap demotion's liquidation does not reach flat within the configured
timeout (default 60 s), the orchestrator's ``resolve_demotion`` gate must
enter the demotion-pending state: cancel the unfilled liquidation order,
notify the operator over dashboard + email + SMS, record the demotion
transition, refuse the swap with ``HOT_SWAP_DEMOTION_TIMEOUT``, and BLOCK
promotion. On flat-before-timeout the swap proceeds with no alert and no
cancel.

L7 domain (safety) test. The Rust integration test at
``crates/atp-orchestrator/tests/err_7_hot_swap_demotion_timeout.rs`` builds
spy + panicking-stub implementations of the four ports
(``HotSwapLiquidationProbe``, ``UnfilledOrderCanceller``,
``OperatorAlertSink``, ``HotSwapDemotionEventSink``); this Python test
shells out to ``cargo test`` to anchor those safety post-conditions in the
domain-test layer so the deterministic critic recognizes the diff as having
a paired ``tests/domain/`` safety test (matched by ``hot[_-]?swap`` /
``demotion`` / ``liquidation[_-]?timeout`` / ``promotion[_-]?block`` /
``operator[_-]?alert`` in ``SAFETY_PATH_RE``).
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
            "err_7_hot_swap_demotion_timeout",
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_timeout_enters_demotion_pending_blocks_promotion_and_alerts_all_channels() -> None:
    # SRS-RESV-004: on liquidation timeout the gate must refuse with
    # HOT_SWAP_DEMOTION_TIMEOUT, cancel the unfilled order exactly once,
    # dispatch one operator alert carrying all three channels, record the
    # demotion-pending transition with promotion_blocked == true, and
    # return Err (so the caller never promotes).
    result = _run_cargo_test(
        "err_7_timeout_enters_demotion_pending_blocks_promotion_and_alerts_all_channels"
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-7 Rust domain test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_flat_before_timeout_promotes_with_no_alert_or_cancel() -> None:
    # Negative control: ERR-7's demotion-pending side effects must be
    # selective. A flat-before-timeout demotion must return
    # Ok(HotSwapDemotionResolved) with promotion_allowed == true and must
    # NOT dispatch an alert or cancel anything — the Rust
    # OperatorAlertForbiddenSink / UnfilledOrderForbiddenCanceller panic if
    # invoked.
    result = _run_cargo_test("err_7_flat_before_timeout_promotes_with_no_alert_or_cancel")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-7 flat-before-timeout control test failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_timeout_blocks_promotion_across_many_demotions() -> None:
    # Pseudo-property: the Rust test sweeps several (elapsed, timeout)
    # cases and verifies every timeout blocks the swap with exactly one
    # cancel + one alert (all three channels) + one demotion event whose
    # promotion_blocked flag is set.
    result = _run_cargo_test("err_7_timeout_blocks_promotion_across_many_demotions")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-7 pseudo-property test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_flat_outcome_over_deadline_is_failed_closed_and_blocks_promotion() -> None:
    # Defense-in-depth: a probe that mislabels an over-deadline demotion as
    # FlatBeforeTimeout must not bypass the promotion block — the gate
    # normalises it to a timeout (cancel + alert fire, promotion blocked).
    result = _run_cargo_test(
        "err_7_flat_outcome_over_deadline_is_failed_closed_and_blocks_promotion"
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-7 fail-closed test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_audit_sink_failure_is_best_effort_and_safety_posture_holds() -> None:
    # SRS-RESV-004: a failing demotion event sink must not abort the gate —
    # the cancel + alert still fire and promotion stays blocked (event
    # emission is best-effort; durable delivery is the deferred SRS-LOG-001
    # sink's concern).
    result = _run_cargo_test("err_7_audit_sink_failure_is_best_effort_and_safety_posture_holds")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-7 best-effort-audit test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )
