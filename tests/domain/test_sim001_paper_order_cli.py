"""SRS-SIM-001 / SyRS SYS-82 + SYS-3 + SYS-4 -- the paper order-intake operator CLI is safe and honest.

L7 domain (safety) test, paired with the sim001_paper_order_cli operator surface. Paper order intake
is a trading-safety boundary: a paper strategy must execute its orders ENTIRELY inside the internal
simulation engine and create NO IB API order calls. If a paper order could reach a brokerage -- or if
a malformed order (an empty symbol, a non-positive quantity/limit/stop, a degenerate multi-leg shape)
could still be routed -- the platform would either place an unintended live order or fabricate a paper
execution it should have rejected. The safety core of SRS-SIM-001 is therefore: every SYS-3 order type
and every asset class routes to the internal simulation engine, a SYS-4 multi-leg order routes as one
atomic composite, NO accepted order reaches a brokerage, and any malformed order fails closed BEFORE
routing. The operator binary sim001_paper_order_cli makes that falsifiable at the workflow an operator
drives (types -> all-order-types-routed:true; assets -> both-asset-classes-routed:true; multileg ->
composite-routed:true; no-broker -> no-ib-order-calls:true). This test proves the invariant from three
angles:

  1. Behavioral -- it shells out to the Rust integration test
     ``crates/atp-simulation/tests/srs_sim_001_paper_order_cli.rs`` (which drives the
     sim001_paper_order_cli binary in fresh OS processes) and asserts every order type and asset class
     routes internally, a multi-leg order routes as one composite, the no-broker sweep proves no order
     reaches a brokerage, and EVERY injected fault makes intake fail closed with no proof line.

  2. Structural (non-vacuity) -- it asserts, via ``tools/sim_order_check.py``, that the CLI drives the
     REAL intake engine (not a hand-rolled stand-in that could agree with itself), prints every
     `:true` proof headline, and carries a fail-closed path -- each guard shown non-vacuous by a
     mutation that must be caught.

  3. Scope honesty -- it pins that the contract names the CLI surface as REALIZED, states the feature
     is now passes:true, and names the genuinely ADJACENT features (SRS-SIM-002 fills, SRS-SIM-003
     ledger, SRS-SIM-004 persistence, SRS-EXE-002 orchestrator, the Python runtime) as SEPARATE
     requirements NOT part of SRS-SIM-001's acceptance criterion -- so a later edit cannot silently
     re-inflate or deflate the scope.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPO_ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from sim_order_check import (  # noqa: E402
    SimOrderCheckError,
    check_paper_order_cli,
    cli_source,
    load_config,
)


def _run_cargo_test(test_name: str) -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust integration test")
    return subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-simulation",
            "--test",
            "srs_sim_001_paper_order_cli",
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_one_passed(result: subprocess.CompletedProcess[str], label: str) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"{label} failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined, f"unexpected cargo test output for {label}:\n{combined}"


# --------------------------------------------------------------------------- #
# Behavioral -- every order type / asset class / multi-leg routes internally; faults fail closed
# --------------------------------------------------------------------------- #


def test_every_order_type_routes_internally() -> None:
    # Each SYS-3 order type (market / limit / stop / stop-limit) routes to the internal simulation
    # engine as a single non-composite order -- all-order-types-routed:true.
    _assert_one_passed(
        _run_cargo_test("types_route_every_order_type_internally"),
        "SRS-SIM-001 order types route internally",
    )


def test_both_asset_classes_route_internally() -> None:
    # Both equity and option single orders route to the internal simulation engine --
    # both-asset-classes-routed:true.
    _assert_one_passed(
        _run_cargo_test("assets_route_both_classes_internally"),
        "SRS-SIM-001 asset classes route internally",
    )


def test_multi_leg_routes_as_one_composite() -> None:
    # A SYS-4 two-leg option spread routes as ONE atomic composite transaction --
    # composite-routed:true.
    _assert_one_passed(
        _run_cargo_test("multileg_routes_as_one_composite"),
        "SRS-SIM-001 multi-leg composite",
    )


def test_no_order_reaches_a_brokerage() -> None:
    # The safety core: a sweep of every accepted order shape proves EVERY routing is the internal
    # simulation engine and NONE reaches a brokerage -- no-ib-order-calls:true.
    _assert_one_passed(
        _run_cargo_test("no_broker_sweep_routes_everything_internally"),
        "SRS-SIM-001 no IB order calls",
    )


def test_every_malformed_order_fails_closed() -> None:
    # Every injected fault (empty symbol / non-positive quantity / non-positive limit / non-positive
    # stop / empty multi-leg / single-leg composite / non-option composite leg) makes intake fail
    # closed BEFORE any routing, with no proof line.
    _assert_one_passed(
        _run_cargo_test("every_fault_fails_closed_on_types"),
        "SRS-SIM-001 malformed orders fail closed",
    )


def test_no_subcommand_can_leak_a_proof_under_a_fault() -> None:
    # The fault handler is shared, so a fault must fail closed on every proof subcommand -- no
    # subcommand may print a `:true` headline under a malformed order.
    _assert_one_passed(
        _run_cargo_test("faults_fail_closed_on_every_subcommand"),
        "SRS-SIM-001 no proof under fault",
    )


# --------------------------------------------------------------------------- #
# Structural -- the CLI guards are real (non-vacuous)
# --------------------------------------------------------------------------- #


def test_cli_drives_the_real_intake_engine() -> None:
    config = load_config()
    # The operator binary must drive the REAL engine, so the routing proof runs over the real types,
    # not a hand-rolled echo that could agree with itself.
    check_paper_order_cli(config, cli_source(config))
    for token, replacement in (
        ("PaperSimulationEngine", "StubEngine"),
        ("accept_order", "fake_route"),
    ):
        mutated = cli_source(config).replace(token, replacement)
        with pytest.raises(SimOrderCheckError):
            check_paper_order_cli(config, mutated)


def test_cli_prints_every_proof_headline() -> None:
    config = load_config()
    # Dropping any `:true` proof headline would hide an unproven acceptance half; it must be caught.
    for proof in (
        "all-order-types-routed:",
        "both-asset-classes-routed:",
        "composite-routed:",
        "no-ib-order-calls:",
    ):
        mutated = cli_source(config).replace(proof, "renamed:")
        with pytest.raises(SimOrderCheckError):
            check_paper_order_cli(config, mutated)


def test_cli_fail_closed_path_is_real() -> None:
    config = load_config()
    # Removing the fail-closed path would let a malformed order produce a routing proof; it must be
    # caught.
    mutated = cli_source(config).replace("failed closed", "succeeded anyway")
    with pytest.raises(SimOrderCheckError):
        check_paper_order_cli(config, mutated)


# --------------------------------------------------------------------------- #
# Scope honesty -- the contract names the CLI realized and the adjacent features separate
# --------------------------------------------------------------------------- #


def test_scope_names_the_cli_surface_and_adjacent_separate_features() -> None:
    # An operator must read an HONEST scope: the CLI surface (sim001_paper_order_cli) closes the
    # operator-demonstrable half of the AC; the contract must (1) name that binary as realized, (2)
    # state the feature is now passes:true, and (3) name the genuinely ADJACENT features as SEPARATE
    # requirements NOT part of SRS-SIM-001's narrow acceptance criterion.
    config = load_config()
    block = config["sim_order_contract"]
    description = block["description"]
    assert "sim001_paper_order_cli" in description
    assert "passes:true" in description
    assert "NOT contexts inside SRS-SIM-001's acceptance criterion" in description
    assert "passes:false" not in description
    adjacent = " ".join(entry["feature"] + " " + entry["what"] for entry in block["deferred"])
    for owner in ("SRS-SIM-002", "SRS-SIM-003", "SRS-SIM-004", "SRS-EXE-002", "Python"):
        assert owner in adjacent, owner
