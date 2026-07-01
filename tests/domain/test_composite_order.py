"""SRS-EXE-004 / SyRS SYS-4 / SYS-40 / SYS-82 — multi-leg options orders as
composite transactions.

L7 domain (safety) test. A multi-leg options order must be submitted and filled
as ONE atomic composite — the legs fill together or not at all — and a malformed
composite (empty, single-leg, or any bad leg) must fail closed BEFORE it can
reach the live broker or the simulation engine, so no partial spread ever routes.
The shared substrate lives in ``atp-types`` (``OptionContractIdentity`` +
``CompositeOrderSubmission``); the live seam is the IB adapter's
``submit_composite_order`` (one composite -> one broker order id); the paper half
reuses the internal simulation engine's ``PaperOrderRequest::MultiLeg`` (SYS-4,
SRS-SIM-001).

The safety invariants pinned here:

  * an option contract is identified by underlying + expiration + strike + right
    (a value is well-formed by construction; blank underlying / non-positive
    strike / impossible calendar date fail closed) — so distinct contracts on one
    underlying are never conflated;
  * a composite is two or more legs (SYS-4); empty / single-leg composites fail
    closed;
  * one bad leg rejects the WHOLE composite (atomicity — nothing partial routes);
  * on the live path a well-formed four-leg composite reaches the gateway exactly
    ONCE (one combo order, not one per leg), and an invalid composite NEVER
    reaches the gateway;
  * on the paper path a four-leg options order simulates as one composite and (by
    the single-variant ``OrderRouting`` type) creates no IB API order call.

Each Rust integration/unit test pins one invariant; this Python test shells out
to ``cargo test`` and asserts the safety-relevant subset passes.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]


REPO_ROOT = Path(__file__).resolve().parents[2]


def _cargo() -> str:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust test")
    return cargo


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_cargo(), "test", *args, "--", "--exact"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_single_pass(result: subprocess.CompletedProcess[str]) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"SRS-EXE-004 Rust domain test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined, f"unexpected cargo test output:\n{combined}"


def _types_unit(test_path: str) -> subprocess.CompletedProcess[str]:
    return _run(["-p", "atp-types", "--lib", test_path])


def _adapter(test_name: str) -> subprocess.CompletedProcess[str]:
    return _run(["-p", "atp-adapters", "--test", "srs_exe_004_composite_order", test_name])


def _sim(test_name: str) -> subprocess.CompletedProcess[str]:
    return _run(["-p", "atp-simulation", "--test", "srs_exe_004_paper_composite", test_name])


def _exec_lib(test_path: str) -> subprocess.CompletedProcess[str]:
    return _run(["-p", "atp-execution", "--lib", test_path])


# --- option contract identity (no-conflation substrate) --------------------- #


def test_contract_identity_normalizes_and_fails_closed() -> None:
    _assert_single_pass(
        _types_unit("composite_order::tests::contract_identity_normalizes_and_fails_closed")
    )


def test_distinct_contracts_have_distinct_identities() -> None:
    # underlying + expiration + strike + right — distinct contracts on one
    # underlying never collide (the reason single-leg options fail closed today).
    _assert_single_pass(
        _types_unit("composite_order::tests::distinct_contracts_have_distinct_identities")
    )


def test_expiration_rejects_impossible_dates() -> None:
    _assert_single_pass(_types_unit("composite_order::tests::expiration_rejects_impossible_dates"))


# --- composite validation (SYS-4 atomicity, fail-closed) -------------------- #


def test_four_leg_composite_validates() -> None:
    _assert_single_pass(_types_unit("composite_order::tests::four_leg_composite_validates"))


def test_empty_and_single_leg_composites_fail_closed() -> None:
    _assert_single_pass(
        _types_unit("composite_order::tests::empty_and_single_leg_composites_fail_closed")
    )


def test_one_bad_leg_fails_the_whole_composite() -> None:
    _assert_single_pass(
        _types_unit("composite_order::tests::one_bad_leg_fails_the_whole_composite")
    )


# --- live adapter: one composite -> one broker order, fail-closed ----------- #


def test_live_four_leg_composite_submits_as_one_broker_order() -> None:
    _assert_single_pass(_adapter("srs_exe_004_four_leg_composite_submits_as_one_broker_order"))


def test_live_single_leg_composite_fails_closed_before_the_gateway() -> None:
    _assert_single_pass(
        _adapter("srs_exe_004_single_leg_composite_fails_closed_before_the_gateway")
    )


def test_live_bad_leg_fails_the_whole_composite_before_the_gateway() -> None:
    _assert_single_pass(
        _adapter("srs_exe_004_bad_leg_fails_the_whole_composite_before_the_gateway")
    )


def test_connectionless_adapter_never_fabricates_a_composite() -> None:
    _assert_single_pass(_adapter("srs_exe_004_connectionless_adapter_never_fabricates_a_composite"))


# --- paper engine: one composite, no broker route --------------------------- #


def test_paper_four_leg_options_order_simulates_as_one_composite() -> None:
    _assert_single_pass(_sim("srs_exe_004_four_leg_options_order_simulates_as_one_composite"))


def test_paper_non_option_composite_leg_fails_closed() -> None:
    _assert_single_pass(_sim("srs_exe_004_four_leg_composite_with_a_non_option_leg_fails_closed"))


# --- execution-engine live gate: composite ERR-1/2/3 safeguards ------------- #
# A composite routes as ONE combo order, so it MUST pass the SAME live safeguards
# as the single-leg path before touching the broker (the adapter seam does only
# shape validation). These pin non-live / connectivity / stale / malformed /
# authority blocking of composites at the execution layer.


def test_live_composite_routes_when_all_gates_pass() -> None:
    _assert_single_pass(_exec_lib("tests::live_composite_is_routed_to_the_broker"))


def test_paper_mode_composite_blocked_before_any_port() -> None:
    _assert_single_pass(_exec_lib("tests::paper_mode_composite_is_blocked_before_any_port"))


def test_live_composite_blocked_when_gateway_unreachable() -> None:
    _assert_single_pass(_exec_lib("tests::live_composite_blocked_when_gateway_unreachable"))


def test_live_composite_blocked_when_a_single_leg_contract_is_stale() -> None:
    # Contract-level freshness: legs sharing one underlying (SPY) are checked by
    # full option contract identity, so one stale strike blocks the whole combo.
    _assert_single_pass(
        _exec_lib("tests::live_composite_blocked_when_a_single_leg_contract_is_stale")
    )


def test_malformed_live_composite_fails_closed_before_the_broker() -> None:
    _assert_single_pass(
        _exec_lib("tests::malformed_live_composite_fails_closed_before_the_broker_port")
    )


def test_route_composite_order_rejects_non_designated_strategy() -> None:
    _assert_single_pass(_exec_lib("tests::route_composite_order_rejects_non_designated_strategy"))
