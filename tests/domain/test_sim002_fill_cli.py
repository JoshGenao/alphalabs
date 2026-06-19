"""SRS-SIM-002 / SyRS SYS-83 + SYS-87b -- the configurable fill-model operator CLI is safe and honest.

L7 domain (safety) test, paired with the sim002_fill_cli operator surface. A simulated fill is money
math: it decides whether a paper order executes, at what price, and for how many shares. If a fill
mispriced an order (used the wrong side of the book, ignored the limit, or filled past the bar's
observed volume), an operator could promote a strategy on fills it could never have gotten in the
live market -- a trading-safety bug. If corrupt market data (a non-positive quote, a crossed book, a
negative volume) or a malformed order (a non-positive limit/stop, a zero quantity) could still
produce a fill, the simulation would fabricate executions. The safety core of SRS-SIM-002 is
therefore: each SYS-83 fill rule prices the fill correctly, the per-strategy fill model is a genuine
choice, the SYS-87b volume cap holds per-order AND in aggregate, and any corrupt input fails closed
BEFORE any fill decision. The operator binary sim002_fill_cli makes that falsifiable at the workflow
an operator drives (rules -> sys83-rules-correct:true; config -> config-divergent:true; volume ->
volume-capped:true). This test proves the invariant from three angles:

  1. Behavioral -- it shells out to the Rust integration test
     ``crates/atp-simulation/tests/srs_sim_002_fill_cli.rs`` (which drives the sim002_fill_cli binary
     in fresh OS processes) and asserts each SYS-83 reference price holds, the two per-strategy fill
     models diverge, the SYS-87b cap is enforced single + aggregate, and EVERY injected fault makes
     the fill model fail closed with no proof line -- plus the non-vacuity guards that reject a
     request within the bar volume and a degenerate bar.

  2. Structural (non-vacuity) -- it asserts, via ``tools/sim_fill_check.py``, that the CLI drives the
     REAL fill-model engine (not a hand-rolled stand-in that could agree with itself), prints every
     `:true` proof headline, and carries a fail-closed path -- each guard shown non-vacuous by a
     mutation that must be caught.

  3. Scope honesty -- it pins that the contract names the CLI surface as REALIZED, states the feature
     is now passes:true, and names the genuinely ADJACENT features (SYS-87a market hours, SYS-87c
     stale data, SYS-70 live feed, SYS-83b stochastic model, SRS-SIM-004 persistence, SRS-EXE-002
     orchestrator) as SEPARATE requirements NOT part of SRS-SIM-002's acceptance criterion -- so a
     later edit cannot silently re-inflate or deflate the scope.
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

from sim_fill_check import (  # noqa: E402
    SimFillCheckError,
    check_fill_cli,
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
            "srs_sim_002_fill_cli",
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
# Behavioral -- the SYS-83 rules, per-strategy config, and SYS-87b cap hold; faults fail closed
# --------------------------------------------------------------------------- #


def test_sys83_rules_price_every_order_type() -> None:
    # Each SYS-83 fill rule fills at its expected reference price (market@ask/bid, crossed limit@limit,
    # triggered stop@market, triggered stop-limit@limit) -- sys83-rules-correct:true.
    _assert_one_passed(
        _run_cargo_test("rules_prove_every_sys83_reference_price"),
        "SRS-SIM-002 SYS-83 rules",
    )


def test_per_strategy_fill_model_is_behavior_changing() -> None:
    # The per-strategy configuration is a genuine choice: the two limit models disagree on the same
    # touch snapshot (config-divergent:true) -- not a vacuous "configurable" claim.
    _assert_one_passed(
        _run_cargo_test("config_proves_the_two_models_diverge"),
        "SRS-SIM-002 per-strategy config divergence",
    )


def test_volume_cap_holds_single_and_aggregate() -> None:
    # SYS-87b: a single order is capped at the bar volume AND the aggregate of fills across orders
    # never exceeds the observed volume (volume-capped:true).
    _assert_one_passed(
        _run_cargo_test("volume_caps_single_and_aggregate"),
        "SRS-SIM-002 SYS-87b volume cap",
    )


def test_every_corrupt_input_fails_closed() -> None:
    # The money-safety core: every injected fault (corrupt quote / crossed book / negative volume /
    # zero quantity / non-positive limit / non-positive stop / a budget bound to another bar) makes
    # the fill model fail closed BEFORE any fill decision, with no proof line.
    _assert_one_passed(
        _run_cargo_test("every_fault_fails_closed_on_rules"),
        "SRS-SIM-002 corrupt input fails closed",
    )


def test_no_subcommand_can_leak_a_proof_under_a_fault() -> None:
    # The fault handler is shared, so a fault must fail closed on every proof subcommand -- no
    # subcommand may print a `:true` headline under corrupt input.
    _assert_one_passed(
        _run_cargo_test("faults_fail_closed_on_config_and_volume"),
        "SRS-SIM-002 no proof under fault",
    )


def test_request_within_bar_cannot_fake_a_cap() -> None:
    # Evidence integrity: a request within the bar volume fills in full, so it cannot demonstrate the
    # single-order cap; the CLI rejects it rather than print a vacuous volume-capped:true.
    _assert_one_passed(
        _run_cargo_test("volume_qty_not_exceeding_bar_is_rejected"),
        "SRS-SIM-002 no vacuous volume cap",
    )


def test_degenerate_bar_is_rejected() -> None:
    # A one-share bar cannot exercise a genuine aggregate cap with a zero-volume tail; it is rejected.
    _assert_one_passed(
        _run_cargo_test("volume_degenerate_bar_is_rejected"),
        "SRS-SIM-002 degenerate bar rejected",
    )


# --------------------------------------------------------------------------- #
# Structural -- the CLI guards are real (non-vacuous)
# --------------------------------------------------------------------------- #


def test_cli_drives_the_real_fill_model_engine() -> None:
    config = load_config()
    # The operator binary must drive the REAL engine, so the SYS-83 / SYS-87b proofs run over the real
    # types, not a hand-rolled echo that could agree with itself.
    check_fill_cli(config, cli_source(config))
    for token, replacement in (
        ("PaperSimulationEngine", "StubEngine"),
        ("evaluate_fill_against_budget", "fake_eval"),
    ):
        mutated = cli_source(config).replace(token, replacement)
        with pytest.raises(SimFillCheckError):
            check_fill_cli(config, mutated)


def test_cli_prints_every_proof_headline() -> None:
    config = load_config()
    # Dropping any `:true` proof headline would hide an unproven acceptance half; it must be caught.
    for proof in ("sys83-rules-correct:", "config-divergent:", "volume-capped:"):
        mutated = cli_source(config).replace(proof, "renamed:")
        with pytest.raises(SimFillCheckError):
            check_fill_cli(config, mutated)


def test_cli_fail_closed_path_is_real() -> None:
    config = load_config()
    # Removing the fail-closed path would let corrupt market data produce a fill proof; it must be
    # caught.
    mutated = cli_source(config).replace("failed closed", "succeeded anyway")
    with pytest.raises(SimFillCheckError):
        check_fill_cli(config, mutated)


# --------------------------------------------------------------------------- #
# Scope honesty -- the contract names the CLI realized and the adjacent features separate
# --------------------------------------------------------------------------- #


def test_scope_names_the_cli_surface_and_adjacent_separate_features() -> None:
    # An operator must read an HONEST scope: the CLI surface (sim002_fill_cli) closes the
    # operator-demonstrable half of the AC; the contract must (1) name that binary as realized, (2)
    # state the feature is now passes:true, and (3) name the genuinely ADJACENT features as SEPARATE
    # requirements NOT part of SRS-SIM-002's narrow acceptance criterion.
    config = load_config()
    block = config["sim_fill_contract"]
    description = block["description"]
    assert "sim002_fill_cli" in description
    assert "passes:true" in description
    assert "NOT contexts inside SRS-SIM-002's acceptance criterion" in description
    assert "passes:false" not in description
    adjacent = " ".join(entry["feature"] + " " + entry["what"] for entry in block["deferred"])
    for owner in ("SYS-87a", "SYS-87c", "SYS-70", "SYS-83b", "SRS-SIM-004", "SRS-EXE-002"):
        assert owner in adjacent, owner
