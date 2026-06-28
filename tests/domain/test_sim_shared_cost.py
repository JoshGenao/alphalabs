"""SRS-BT-003 / SyRS SYS-15e / SYS-83d -- use the SAME transaction-cost model family for internal
simulation and backtesting unless configured otherwise. Acceptance: a paper strategy and backtest
using identical cost configuration compute fills and commissions from the same model family.

L7 domain (safety) test. The shared cost family is money math applied in TWO engines: if the
internal simulation engine and the backtest engine ever computed a fill's commission / slippage /
spread-impact differently for the same config and inputs, an operator could promote a strategy on
backtest economics it would never realize in paper (or vice versa) -- a silent, money-losing
divergence. The safety core of SRS-BT-003 is therefore: both engines call the IDENTICAL cost entry
point, both ALWAYS subtract the cost (never fabricate cash), a misconfigured / corrupt input fails
closed in BOTH before any fill, and the per-fill decomposition is provably EQUAL between them. The
operator binary bt003_shared_cost_cli makes that equality falsifiable at the workflow an operator
drives (`compare` -> cost-family-match:true). This test proves the invariant from two angles:

  1. Behavioral -- it shells out to the Rust integration test
     ``crates/atp-simulation/tests/srs_bt_003_cost_cli.rs`` (which drives the bt003_shared_cost_cli
     binary in fresh OS processes) and asserts the two engines agree fill-for-fill under the default
     family and under an operator override, that the economics (final equity vs ledger cash) agree,
     and that an injected fault (a negative cost parameter, a non-positive price) is rejected by
     BOTH engines before any fill (no comparison line, no cash fabricated).

  2. Structural -- it asserts, via ``tools/sim_cost_check.py``, that the simulation cost path is
     integer-only (no f64 in the money path), that simulate_fill SUBTRACTS the cost total, and that
     the bt003_shared_cost_cli operator binary actually drives BOTH engines (not a single-engine
     echo) -- each guard shown non-vacuous by a mutation that must be caught. Finally it pins the
     scope honesty: the contract names the CLI surface as REALIZED, states the feature is now
     passes:true, and names the genuinely ADJACENT features (SIM-002/003/004, the REST/UI override
     surface, the Python host) as SEPARATE requirements NOT part of SRS-BT-003's acceptance
     criterion -- so a later edit cannot silently re-inflate or deflate the scope.
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

from sim_cost_check import (  # noqa: E402
    SimCostCheckError,
    check_money_invariant,
    check_shared_cost_cli,
    check_shared_entry_point,
    cli_source,
    load_config,
    sim_source,
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
            "srs_bt_003_cost_cli",
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
# Behavioral -- the two engines compute the SAME fills/commissions, and fail closed together
# --------------------------------------------------------------------------- #


def test_default_compare_matches_fill_for_fill() -> None:
    # The headline acceptance criterion: identical default config -> identical per-fill commission /
    # slippage / spread-impact in both engines (cost-family-match:true).
    _assert_one_passed(
        _run_cargo_test("default_compare_matches_fill_for_fill"),
        "SRS-BT-003 default compare matches",
    )


def test_engines_agree_with_nonzero_costs() -> None:
    # Non-vacuity: the agreement is over a family that actually charges a cost, not 0 == 0.
    _assert_one_passed(
        _run_cargo_test("each_fill_shows_both_engines_agree_with_nonzero_costs"),
        "SRS-BT-003 engines agree (non-vacuous)",
    )


def test_override_is_shared_by_both_engines() -> None:
    # A single operator override is applied to BOTH engines and they still agree fill-for-fill.
    _assert_one_passed(
        _run_cargo_test("override_is_shared_by_both_engines"),
        "SRS-BT-003 override shared",
    )


def test_full_reports_equal_equity_and_cash() -> None:
    # The economics agree: the backtest final equity equals the paper ledger cash for the round trip,
    # because both engines moved cash by exactly the same total cost.
    _assert_one_passed(
        _run_cargo_test("full_reports_equal_equity_and_cash_for_the_round_trip"),
        "SRS-BT-003 economics agree",
    )


def test_negative_commission_fails_closed_in_both_engines() -> None:
    # The money-safety core: a negative cost parameter is rejected by BOTH engines before any fill
    # (no comparison line, no cash fabricated).
    _assert_one_passed(
        _run_cargo_test("inject_negative_commission_fails_closed_in_both_engines"),
        "SRS-BT-003 negative commission fails closed",
    )


def test_nonpositive_price_fails_closed_in_both_engines() -> None:
    # A non-positive price would let a buy fabricate cash when the cost is subtracted -- rejected by
    # BOTH engines before any fill.
    _assert_one_passed(
        _run_cargo_test("inject_nonpositive_price_fails_closed_in_both_engines"),
        "SRS-BT-003 non-positive price fails closed",
    )


def test_zero_lot_cannot_produce_a_vacuous_match() -> None:
    # Evidence integrity: a zero lot trades nothing, so the cost-family-match headline must NOT be
    # printed over zero comparisons -- the proof can never be vacuous.
    _assert_one_passed(
        _run_cargo_test("zero_lot_fails_closed_with_no_vacuous_match"),
        "SRS-BT-003 no vacuous match",
    )


# --------------------------------------------------------------------------- #
# Structural -- each money-safety guard is real (non-vacuous)
# --------------------------------------------------------------------------- #


def test_sim_cost_math_is_integer_only() -> None:
    config = load_config()
    # The real simulation cost path is integer minor units -- no f64 that could round money.
    check_money_invariant(config, sim_source(config))
    mutated = sim_source(config).replace(
        "pub cash_delta_minor: i64,", "pub cash_delta_minor: f64,", 1
    )
    with pytest.raises(SimCostCheckError):
        check_money_invariant(config, mutated)


def test_simulation_subtracts_the_cost_from_cash() -> None:
    config = load_config()
    # simulate_fill SUBTRACTS the per-fill cost total (a cost can never add cash).
    check_shared_entry_point(config, sim_source(config))
    mutated = sim_source(config).replace(
        ".checked_sub(total_cost_minor)", ".checked_sub(zero_minor)", 1
    )
    with pytest.raises(SimCostCheckError):
        check_shared_entry_point(config, mutated)


def test_compare_cli_drives_both_engines() -> None:
    config = load_config()
    # The operator binary must drive BOTH engines, so cost-family-match is a real cross-engine proof.
    check_shared_cost_cli(config, cli_source(config))
    # ...non-vacuous: collapsing the paper engine to the backtest engine is caught.
    mutated = cli_source(config).replace("PaperSimulationEngine", "BacktestEngine")
    with pytest.raises(SimCostCheckError):
        check_shared_cost_cli(config, mutated)


def test_scope_names_the_cli_surface_and_adjacent_separate_features() -> None:
    # Safety: an operator must read an HONEST scope. The CLI surface (bt003_shared_cost_cli) closes
    # the operator-demonstrable half of the AC; the contract must (1) name that binary as realized,
    # (2) state the feature is now passes:true, and (3) name the genuinely ADJACENT features as
    # SEPARATE requirements not part of SRS-BT-003's narrow acceptance criterion -- so a later edit
    # cannot silently re-inflate or deflate the scope.
    config = load_config()
    block = config["sim_cost_contract"]
    description = block["description"]
    assert "bt003_shared_cost_cli" in description
    assert "passes:true" in description
    assert "NOT part of SRS-BT-003" in description
    deferred = " ".join(entry["feature"] + " " + entry["what"] for entry in block["deferred"])
    for owner in ("SRS-SIM-002", "SRS-SIM-003", "SRS-SIM-004", "SRS-API-001", "SRS-BT-001-runtime"):
        assert owner in deferred, owner
    # The CLI half of the override surface is described as REALIZED (not still-deferred).
    rest_ui = next(
        entry for entry in block["deferred"] if entry["feature"].startswith("SRS-API-001")
    )
    assert "REALIZED" in rest_ui["what"]
    assert "REST" in rest_ui["what"]
