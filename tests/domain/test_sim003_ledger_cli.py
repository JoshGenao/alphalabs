"""SRS-SIM-003 / SyRS SYS-84 -- the per-strategy virtual-ledger operator CLI is safe and honest.

L7 domain (safety) test, paired with the sim003_ledger_cli operator surface. The virtual ledger is
money math (quantity, average cost, realized / unrealized P&L, commission) tracked per paper strategy.
If one paper strategy's fills could read or mutate another's positions -- or if a virtual position
could be confused with the IB account's real position -- an operator could mis-promote a strategy on
P&L it never actually held, a trading-safety bug. The safety core of SRS-SIM-003 is therefore: each
strategy's ledger is isolated, a virtual position is independent of the IB account, and a corrupt fill
fails closed BEFORE any mutation (never silently moving a position or fabricating cash). The operator
binary sim003_ledger_cli makes that isolation falsifiable at the workflow an operator drives
(`isolate` -> ledger-isolation:true / account-independent:true). This test proves the invariant from
three angles:

  1. Behavioral -- it shells out to the Rust integration test
     ``crates/atp-simulation/tests/srs_sim_003_ledger_cli.rs`` (which drives the sim003_ledger_cli
     binary in fresh OS processes) and asserts two strategies on the same symbol stay isolated and
     account-independent, that the five quantities print, that the round-trip reconciliation holds,
     and that an injected fault (a non-positive price, a zero-quantity fill) makes the ledger fail
     closed with no isolation line, and a non-positive lot cannot fabricate a vacuous isolation proof.

  2. Structural (non-vacuity) -- it asserts, via ``tools/sim_ledger_check.py``, that the CLI drives
     the REAL engine AND the REAL ledger (not a hand-rolled stand-in that could agree with itself),
     prints all five SYS-84 quantities, prints the isolation headline, and carries a fail-closed path
     -- each guard shown non-vacuous by a mutation that must be caught.

  3. Scope honesty -- it pins that the contract names the CLI surface as REALIZED, states the feature
     is now passes:true, and names the genuinely ADJACENT features (SYS-70 live feed, SYS-88 corporate
     actions, SRS-SIM-004 persistence, SYS-85 metrics, SRS-EXE-002 orchestrator, the Python host) as
     SEPARATE requirements NOT part of SRS-SIM-003's acceptance criterion -- so a later edit cannot
     silently re-inflate or deflate the scope.
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

from sim_ledger_check import (  # noqa: E402
    SimLedgerCheckError,
    check_ledger_cli,
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
            "srs_sim_003_ledger_cli",
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
# Behavioral -- per-strategy ledgers are isolated, and a corrupt fill fails closed
# --------------------------------------------------------------------------- #


def test_isolate_proves_per_strategy_isolation() -> None:
    # The headline acceptance criterion: two strategies on the same symbol are isolated per strategy
    # (ledger-isolation:true) and account-independent.
    _assert_one_passed(
        _run_cargo_test("isolate_proves_per_strategy_isolation"),
        "SRS-SIM-003 per-strategy isolation",
    )


def test_alpha_and_beta_hold_independent_positions() -> None:
    # Non-vacuity: the same symbol resolves to DIFFERENT positions per strategy -- a per-strategy
    # virtual ledger, not one shared (IB) account position.
    _assert_one_passed(
        _run_cargo_test("alpha_and_beta_hold_independent_positions"),
        "SRS-SIM-003 account independence",
    )


def test_all_five_quantities_print_per_strategy() -> None:
    # The five SYS-84 quantities (quantity, average cost, unrealized / realized P&L, commission) are
    # each present and populated per strategy.
    _assert_one_passed(
        _run_cargo_test("isolate_prints_all_five_quantities_per_strategy"),
        "SRS-SIM-003 five quantities",
    )


def test_round_trip_reconciles_with_simulated_cash() -> None:
    # The economics agree on ALPHA's actual ledger: gross realized P&L minus the FULL transaction cost
    # equals the sum of every alpha fill's cash delta, so no charged cost disappears.
    _assert_one_passed(
        _run_cargo_test("full_reconciles_alphas_actual_ledger_with_simulated_cash"),
        "SRS-SIM-003 reconciliation",
    )


def test_nonpositive_price_fails_closed() -> None:
    # The money-safety core: a non-positive price is rejected before any mutation (no isolation line).
    _assert_one_passed(
        _run_cargo_test("inject_nonpositive_price_fails_closed"),
        "SRS-SIM-003 non-positive price fails closed",
    )


def test_zero_quantity_fails_closed() -> None:
    # A zero-quantity fill is rejected by the ledger before any mutation (no phantom strategy).
    _assert_one_passed(
        _run_cargo_test("inject_zero_quantity_fails_closed"),
        "SRS-SIM-003 zero-quantity fails closed",
    )


def test_nonpositive_lot_cannot_produce_a_vacuous_isolation_proof() -> None:
    # Evidence integrity: a non-positive lot leaves nothing to isolate, so ledger-isolation:true must
    # NOT be printed -- the proof can never be vacuous.
    _assert_one_passed(
        _run_cargo_test("nonpositive_lot_a_fails_closed_with_no_isolation_claim"),
        "SRS-SIM-003 no vacuous isolation",
    )


# --------------------------------------------------------------------------- #
# Structural -- the CLI guards are real (non-vacuous)
# --------------------------------------------------------------------------- #


def test_cli_drives_the_real_engine_and_ledger() -> None:
    config = load_config()
    # The operator binary must drive the REAL engine AND the REAL ledger, so ledger-isolation is a
    # genuine proof over the real types, not a hand-rolled echo.
    check_ledger_cli(config, cli_source(config))
    for token, replacement in (
        ("PaperSimulationEngine", "StubEngine"),
        ("VirtualLedgerBook", "FakeBook"),
    ):
        mutated = cli_source(config).replace(token, replacement)
        with pytest.raises(SimLedgerCheckError):
            check_ledger_cli(config, mutated)


def test_cli_prints_all_five_quantities() -> None:
    config = load_config()
    # Dropping any of the five SYS-84 quantity prints would hide an uncomputed field; it must be
    # caught.
    mutated = cli_source(config).replace("commission-paid-minor:", "comm:")
    with pytest.raises(SimLedgerCheckError):
        check_ledger_cli(config, mutated)


def test_cli_fail_closed_path_is_real() -> None:
    config = load_config()
    # Removing the fail-closed path would let a corrupt fill produce a proof; it must be caught.
    mutated = cli_source(config).replace("failed closed", "succeeded anyway")
    with pytest.raises(SimLedgerCheckError):
        check_ledger_cli(config, mutated)


# --------------------------------------------------------------------------- #
# Scope honesty -- the contract names the CLI realized and the adjacent features separate
# --------------------------------------------------------------------------- #


def test_scope_names_the_cli_surface_and_adjacent_separate_features() -> None:
    # An operator must read an HONEST scope: the CLI surface (sim003_ledger_cli) closes the
    # operator-demonstrable half of the AC; the contract must (1) name that binary as realized, (2)
    # state the feature is now passes:true, and (3) name the genuinely ADJACENT features as SEPARATE
    # requirements NOT part of SRS-SIM-003's narrow acceptance criterion.
    config = load_config()
    block = config["virtual_ledger_contract"]
    description = block["description"]
    assert "sim003_ledger_cli" in description
    assert "passes:true" in description
    assert "NOT contexts inside SRS-SIM-003" in description
    adjacent = " ".join(entry["feature"] + " " + entry["what"] for entry in block["deferred"])
    for owner in ("SYS-70", "SYS-88", "SRS-SIM-004", "SYS-85", "SRS-EXE-002"):
        assert owner in adjacent, owner
