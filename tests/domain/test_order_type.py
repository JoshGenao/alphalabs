"""SRS-EXE-003 / SyRS SYS-3 / SYS-82 — source-neutral order-type vocabulary +
price-validation authority.

L7 domain (safety) test. The order-type authority decides which orders are
well-formed enough to be accepted, validated, and (downstream) routed to the
live IB broker or the internal simulation engine. It lives in ``atp-types`` as
the single shared definition — the paper path consumes it via re-export today;
the live path will consume the same definition (deferred); pinned by
``tools/order_type_check.py``. The safety invariants:

  * the four supported order types encode their trigger/limit prices in the
    variants, so a CONTRADICTORY price set (a market order with a stray price, a
    limit order with no limit price) can never even be represented — this is the
    by-construction guarantee;
  * the price-requirement matrix is total over all four types;
  * the price-positivity rule rejects a zero or negative limit/stop price,
    identical for equity and option legs (an intake applies it before an order
    is accepted — see the module docs; positivity is an intake-boundary check,
    not construction-enforced, because the variants are public).

The Rust integration test ``crates/atp-types/tests/srs_exe_003_order_type.rs``
pins each invariant; this Python test shells out to ``cargo test`` and asserts
the safety-relevant subset.
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
            "atp-types",
            "--test",
            "srs_exe_003_order_type",
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_single_pass(result: subprocess.CompletedProcess[str]) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"SRS-EXE-003 Rust domain test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_no_contradictory_price_set_is_representable() -> None:
    _assert_single_pass(_run_cargo_test("srs_exe_003_no_contradictory_price_set_is_representable"))


def test_price_requirement_matrix_is_total_and_correct() -> None:
    _assert_single_pass(
        _run_cargo_test("srs_exe_003_price_requirement_matrix_is_total_and_correct")
    )


def test_validate_prices_fails_closed_on_non_positive_prices() -> None:
    _assert_single_pass(
        _run_cargo_test("srs_exe_003_validate_prices_fails_closed_on_non_positive_prices")
    )


def test_validation_identical_across_equity_and_option() -> None:
    _assert_single_pass(
        _run_cargo_test("srs_exe_003_validation_identical_across_equity_and_option")
    )


def test_order_type_wire_strings_are_stable() -> None:
    _assert_single_pass(
        _run_cargo_test("srs_exe_003_all_four_order_types_have_stable_wire_strings")
    )
