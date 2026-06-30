"""SRS-EXE-003 / SyRS SYS-3 / SYS-82 — source-neutral order-type vocabulary +
price-validation authority.

L7 domain (safety) test. The order-type authority decides which orders are
well-formed enough to be accepted, validated, and (downstream) routed to the
live IB broker or the internal simulation engine. It lives in ``atp-types`` as
the single shared definition — the paper path consumes it via re-export and (as of SRS-EXE-003) the live
path does too — ``atp_types::OrderSubmission`` carries the order type and the IB
adapter validates it; pinned by ``tools/order_type_check.py``. The safety invariants:

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
import sys
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


def _run_adapter_test(test_name: str) -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust integration test")
    return subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-adapters",
            "--test",
            "srs_exe_003_order_types",
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_live_adapter_validates_each_order_type_before_acknowledgement() -> None:
    # SRS-EXE-003 live-adapter-test-mode: each of the four EQUITY order types is
    # accepted + acknowledged over the deterministic gateway double.
    _assert_single_pass(
        _run_adapter_test("srs_exe_003_each_equity_order_type_accepted_and_acknowledged")
    )


def test_option_orders_fail_closed_pending_contract_identity() -> None:
    # SRS-EXE-003 honest scope: an option order (just an underlying, no contract
    # identity) must fail closed — never treated as broker-ready — so distinct
    # option contracts are not conflated (the SecurityKey-fails-closed-on-Option
    # pattern). Live option submission lands with SRS-EXE-004 / SRS-DATA-004.
    _assert_single_pass(
        _run_adapter_test("srs_exe_003_option_orders_fail_closed_pending_contract_identity")
    )


def test_live_adapter_invalid_order_fails_closed_before_the_broker() -> None:
    # The core live-path safety invariant: an order with a non-positive price is
    # rejected by the adapter (AdapterError::InvalidOrder) and is NEVER forwarded
    # to the broker gateway — a malformed order can never create a live order.
    _assert_single_pass(
        _run_adapter_test("srs_exe_003_non_positive_priced_orders_fail_closed_before_the_gateway")
    )


def test_live_adapter_blank_symbol_or_bad_quantity_fails_closed() -> None:
    # Live/paper validation parity: a blank symbol or non-positive quantity is
    # rejected before the gateway too (not only bad prices) — the same
    # well-formedness the paper intake enforces.
    _assert_single_pass(
        _run_adapter_test("srs_exe_003_blank_symbol_or_bad_quantity_fail_closed_before_the_gateway")
    )


def _run_execution_lib_test(test_path: str) -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust test")
    return subprocess.run(
        [cargo, "test", "-p", "atp-execution", "--lib", test_path, "--", "--exact"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_dispatch_validates_before_any_routing_port() -> None:
    # The shared-entry safety invariant (SRS-EXE-003 / EXE-002): ExecutionEngine
    # ::dispatch_order validates the order BEFORE routing, so a malformed order
    # reaches neither the broker nor the simulation port (parity is enforced at
    # the dispatch boundary, not left to the downstream port impl).
    _assert_single_pass(
        _run_execution_lib_test(
            "order_routing::tests::order_routing_dispatch_rejects_malformed_order_before_any_port"
        )
    )


def test_live_route_validates_before_the_broker_port() -> None:
    # The live-path safety invariant: submit_live_order (the public ERR-1/2/3
    # live entry) validates before the broker port, so a malformed order fails
    # closed and the broker is never called — not relying on the adapter alone.
    _assert_single_pass(
        _run_execution_lib_test("tests::malformed_live_order_fails_closed_before_the_broker_port")
    )


def test_contract_evidence_reflects_both_paths_consume_the_order_type() -> None:
    # SRS-EXE-003 contract coherence: the order-type check's evidence must report
    # that BOTH paths consume the vocabulary (live via OrderSubmission + adapter/
    # dispatch validation), not the stale "live consumption deferred" claim — so
    # feature-closure reasoning is not misled by contradictory public evidence.
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "order_type_check.py")],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "BOTH paths consume" in out, out
    assert "live consumption deferred" not in out, out
