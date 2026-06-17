"""SRS-BT-002 / SyRS SYS-15a-d -- apply configurable commission, slippage, and spread-impact
models to backtests, with defaults that match the SyRS values and per-run overrides that need no
strategy code change.

L7 domain (safety) test. Transaction cost is money math: every model SUBTRACTS from cash, so a
cost that could go negative -- or a misconfigured parameter that slipped past validation -- would
*fabricate cash* and inflate a backtest an operator might promote a strategy on. The safety core
of SRS-BT-002 is therefore: a cost is always non-negative, the engine always subtracts it, the
default family is provably the published SyRS baseline, and a misconfigured cost fails closed
BEFORE any fill. The operator override surface (the bt002_cost_cli binary) makes that guarantee
falsifiable at the workflow an operator actually drives. This test proves the invariant from two
angles:

  1. Behavioral -- it shells out to the Rust integration test
     ``crates/atp-simulation/tests/srs_bt_002_cost_cli.rs`` (which drives the bt002_cost_cli binary
     in fresh OS processes) and asserts the SyRS defaults are applied exactly, an override changes
     the realized cost while the SAME strategy produces the SAME fills, the frictionless override
     zeroes every cost and recovers the starting cash, and a negative override parameter fails
     closed with no fill or equity printed (no cash fabricated).

  2. Structural -- it asserts, via ``tools/backtest_cost_check.py``, that the cost path is
     integer-only (no f64 in the money path), that the engine actually SUBTRACTS the cost total
     from cash, that ``CostConfig::validate`` rejects a negative parameter, and that the
     bt002_cost_cli operator binary is registered -- each guard shown non-vacuous by a mutation
     that must be caught. Finally it pins the scope honesty: the contract names the CLI override
     surface as REALIZED and the genuinely remaining deferred owners (the REST/dashboard half, the
     Python strategy host), so a later edit cannot silently re-inflate or deflate the scope.
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

from backtest_cost_check import (  # noqa: E402
    BacktestCostCheckError,
    backtest_source,
    cargo_source,
    check_cost_cli,
    check_engine_application,
    check_money_invariant,
    check_validate_fail_closed,
    cost_source,
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
            "srs_bt_002_cost_cli",
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
# Behavioral -- the operator CLI applies the SyRS defaults and per-run overrides
# --------------------------------------------------------------------------- #


def test_defaults_match_the_syrs_values() -> None:
    # "Defaults match the SyRS values" half of the AC: the CLI prints the published constants and
    # proves CostConfig::default() == CostConfig::syrs_defaults().
    _assert_one_passed(
        _run_cargo_test("defaults_match_the_syrs_values"),
        "SRS-BT-002 defaults match SyRS",
    )


def test_default_run_applies_the_syrs_cost_family() -> None:
    # The SyRS default family is applied to the fills exactly (commission 35, slippage 500,
    # observed-spread 2000 vs fallback 1000), every figure an exact integer minor unit.
    _assert_one_passed(
        _run_cargo_test("default_run_applies_the_syrs_cost_family"),
        "SRS-BT-002 default cost family",
    )


def test_override_changes_costs_without_changing_the_strategy() -> None:
    # The safety-relevant half of the AC: an override changes the realized cost while the SAME
    # fixture strategy produces the SAME fills -- the override lives on the request, not the
    # strategy (SYS-15d), so an operator cannot accidentally alter trading by tuning costs.
    _assert_one_passed(
        _run_cargo_test("override_changes_costs_without_changing_the_strategy"),
        "SRS-BT-002 override without strategy change",
    )


def test_frictionless_override_recovers_the_starting_cash() -> None:
    # With every cost overridden to None, the flat-price round trip recovers EXACTLY the starting
    # cash -- the costs are the only thing that moved equity, so a stray cost cannot hide.
    _assert_one_passed(
        _run_cargo_test("frictionless_override_zeroes_every_cost"),
        "SRS-BT-002 frictionless recovers cash",
    )


def test_negative_parameter_fails_closed_with_no_cash_fabricated() -> None:
    # The money-safety core: a negative override parameter is rejected by CostConfig::validate
    # inside the engine BEFORE any fill -- the run exits non-zero and prints no fill or equity, so
    # a misconfigured cost can never fabricate cash.
    _assert_one_passed(
        _run_cargo_test("negative_override_parameter_fails_closed"),
        "SRS-BT-002 negative parameter fails closed",
    )


def test_identical_inputs_are_byte_identical_across_processes() -> None:
    # Cost math is integer-only with no platform randomness, so two fresh processes over identical
    # flags are byte-identical (the SRS-BT-010 determinism property the cost path must honor).
    _assert_one_passed(
        _run_cargo_test("identical_inputs_are_byte_identical_across_processes"),
        "SRS-BT-002 cross-process determinism",
    )


# --------------------------------------------------------------------------- #
# Structural -- each money-safety guard is real (non-vacuous)
# --------------------------------------------------------------------------- #


def test_cost_math_is_integer_only() -> None:
    config = load_config()
    # The real cost path is integer minor units -- no f64 that could round money nondeterministically.
    check_money_invariant(config, cost_source(config))
    # ...and the guard must not be vacuous: a float leaked into the cost decomposition is caught.
    mutated = cost_source(config).replace("pub commission_minor: i64,", "pub commission_minor: f64,", 1)
    with pytest.raises(BacktestCostCheckError):
        check_money_invariant(config, mutated)


def test_engine_subtracts_the_cost_from_cash() -> None:
    config = load_config()
    # The real engine SUBTRACTS the per-fill cost total from cash (a cost can never add cash).
    check_engine_application(config, backtest_source(config))
    # ...and the guard must not be vacuous: dropping the cost deduction is caught -- the exact
    # regression where a backtest would silently ignore costs and overstate returns.
    mutated = backtest_source(config).replace(
        "checked_sub(total_cost_minor)", "checked_sub(zero_cost_minor)", 1
    )
    with pytest.raises(BacktestCostCheckError):
        check_engine_application(config, mutated)


def test_validate_rejects_a_negative_parameter() -> None:
    config = load_config()
    # The real CostConfig::validate fails closed on a negative configured parameter before any fill.
    check_validate_fail_closed(config, cost_source(config))
    # ...and the guard must not be vacuous: dropping the per-share rate from the guard is caught.
    mutated = cost_source(config).replace("rate_centiminor_per_share", "rate_x")
    with pytest.raises(BacktestCostCheckError):
        check_validate_fail_closed(config, mutated)


def test_cost_override_cli_is_registered() -> None:
    config = load_config()
    # The bt002_cost_cli binary is the operator surface that makes the override AC demonstrable;
    # without a registered bin the operator override surface is gone.
    check_cost_cli(config, cargo_source(config))
    # ...and the guard must not be vacuous: an unregistered bin is caught.
    mutated = cargo_source(config).replace('name = "bt002_cost_cli"', 'name = "x"', 1)
    with pytest.raises(BacktestCostCheckError):
        check_cost_cli(config, mutated)


def test_scope_names_the_cli_surface_and_remaining_deferred_owners() -> None:
    # Safety: an operator must read an HONEST scope. The CLI override surface (bt002_cost_cli) is
    # what closes the operator-override half of the AC the engine could not demonstrate on its own,
    # so the contract must (1) name that binary as realized, (2) state the feature is now passes:true,
    # and (3) name the genuinely remaining deferred owners (the REST/dashboard half, the Python
    # strategy host) without over-claiming. This pins the scope so a later edit cannot silently
    # re-inflate or deflate it.
    config = load_config()
    block = config["backtest_cost_contract"]
    description = block["description"]
    assert "bt002_cost_cli" in description
    assert "passes:true" in description
    # The genuinely remaining deferred owners are named honestly.
    deferred = " ".join(entry["feature"] + " " + entry["what"] for entry in block["deferred"])
    assert "SRS-API-001" in deferred
    assert "SRS-BT-001-runtime" in deferred
    # The CLI half is described as REALIZED in the deferred REST/UI entry, not still-deferred.
    rest_ui = next(entry for entry in block["deferred"] if entry["feature"].startswith("SRS-API-001"))
    assert "REALIZED" in rest_ui["what"]
    assert "REST" in rest_ui["what"]
