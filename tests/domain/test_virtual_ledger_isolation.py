"""SRS-SIM-003 / SyRS SYS-84 — virtual ledgers are isolated and broker-independent.

L7 domain (safety) test. The acceptance criterion's safety core is that each paper
strategy's virtual position ledger (quantity, average cost, realized/unrealized
P&L, commission paid) is *isolated per strategy* and *independent of the IB
account's actual positions*. A leak in either direction is a trading-safety bug:
one paper strategy reading or moving another's positions, or a virtual position
being confused with a real IB position. This test proves the invariant from three
angles:

  1. Behavioral — it shells out to the Rust integration test
     ``crates/atp-simulation/tests/srs_sim_003_virtual_ledger.rs`` and asserts that
     two strategies holding the same symbol keep fully independent positions
     (mutating one leaves the other byte-for-byte unchanged), that average-cost
     accounting realizes the right P&L over longs, shorts, and flips while
     commission accumulates separately, and that corrupt input fails closed (a
     rejected fill never silently mutates a ledger).

  2. Structural (broker independence) — it asserts, via
     ``tools/sim_ledger_check.py``, that the ``atp-simulation`` crate declares no
     dependency on the live/broker path (``atp-execution`` / ``atp-adapters``) and
     that the ledger module leaks no vendor SDK token. Because there is no broker
     crate to reach, a virtual position cannot be reconciled against or routed to
     the IB account.

  3. Structural (per-strategy isolation) — it asserts the ledger book keys each
     strategy's ledger by ``StrategyId`` and routes a fill to only the named
     strategy's entry, so one strategy's fills cannot touch another's positions.

Each structural guard is checked for non-vacuity: an injected broker dependency, a
leaked vendor token, and a removed per-strategy route are each shown to be caught.
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
    cargo_source,
    check_ledger_book_isolation,
    check_no_broker_dependency,
    check_symbol_normalization,
    check_vendor_isolation,
    ledger_source,
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
            "srs_sim_003_virtual_ledger",
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


def test_strategies_holding_the_same_symbol_stay_independent() -> None:
    # The safety core: two strategies holding the same symbol keep independent
    # quantities and average cost, and mutating one never touches the other.
    _assert_one_passed(
        _run_cargo_test("srs_sim_003_isolates_strategies_holding_the_same_symbol"),
        "SRS-SIM-003 per-strategy isolation",
    )


def test_average_cost_accounting_realizes_the_right_pnl() -> None:
    # A long round trip realizes P&L from the price move while commission
    # accumulates separately (SYS-84 keeps the two distinct).
    _assert_one_passed(
        _run_cargo_test("srs_sim_003_long_round_trip_realizes_pnl_and_accumulates_commission"),
        "SRS-SIM-003 realized P&L + commission",
    )


def test_short_cover_and_flip_through_zero_behaviorally() -> None:
    # Shorts and a flip through zero realize only the closed portion and reopen at
    # the fill price, with mark-to-market on the new position.
    _assert_one_passed(
        _run_cargo_test("srs_sim_003_short_cover_and_flip_through_zero"),
        "SRS-SIM-003 short / flip accounting",
    )


def test_corrupt_input_fails_closed() -> None:
    # Negative control: corrupt input (non-positive mark, empty symbol) is rejected,
    # never silently mutating a ledger.
    _assert_one_passed(
        _run_cargo_test("srs_sim_003_fails_closed_on_corrupt_input"),
        "SRS-SIM-003 fail-closed ledger",
    )


def test_rejected_first_fill_leaves_no_phantom_strategy() -> None:
    # A rejected first fill for a brand-new strategy must not register the strategy
    # at all -- a phantom strategy would pollute later metrics / persistence /
    # orchestrator accounting with a strategy that never had a valid fill.
    _assert_one_passed(
        _run_cargo_test("srs_sim_003_rejected_first_fill_leaves_no_phantom_strategy"),
        "SRS-SIM-003 no phantom strategy on rejected fill",
    )


def test_aliased_symbols_share_one_position() -> None:
    # The same security under different casing/whitespace must keep ONE position --
    # splitting it across aliases would corrupt per-symbol quantity and P&L.
    _assert_one_passed(
        _run_cargo_test("srs_sim_003_aliased_symbols_share_one_position"),
        "SRS-SIM-003 symbol alias equivalence",
    )


def test_ledger_reconciles_with_simulated_cash() -> None:
    # Money-correctness invariant: the ledger's net P&L (gross realized minus the
    # FULL transaction cost) must reconcile exactly with the simulator's cash, so
    # no charged cost (commission, slippage, spread) silently disappears.
    _assert_one_passed(
        _run_cargo_test("srs_sim_003_ledger_reconciles_with_simulated_cash"),
        "SRS-SIM-003 cash reconciliation",
    )


def test_inconsistent_cash_delta_fails_closed() -> None:
    # A tampered fill whose cash_delta disagrees with -(notional) - total cost is
    # rejected before any mutation, so it cannot silently break reconciliation.
    _assert_one_passed(
        _run_cargo_test("srs_sim_003_rejects_inconsistent_cash_delta"),
        "SRS-SIM-003 cash-delta integrity",
    )


def test_symbol_keyed_marking_selects_the_named_position() -> None:
    # Mark-to-market selects the position BY symbol, so a quote is never applied to
    # a different instrument's position.
    _assert_one_passed(
        _run_cargo_test("srs_sim_003_symbol_keyed_marking_selects_the_named_position"),
        "SRS-SIM-003 symbol-keyed marking",
    )


def test_simulation_crate_has_no_broker_dependency() -> None:
    config = load_config()
    # The real Cargo.toml must declare no live/broker-path dependency, so a virtual
    # position is independent of the IB account at the crate boundary.
    check_no_broker_dependency(config, cargo_source(config))
    # ...and the guard must not be vacuous: an injected broker dep is caught.
    mutated = cargo_source(config) + '\natp-execution = { path = "../atp-execution" }\n'
    with pytest.raises(SimLedgerCheckError):
        check_no_broker_dependency(config, mutated)


def test_ledger_module_leaks_no_vendor_token() -> None:
    config = load_config()
    # The real ledger module must carry no vendor SDK token.
    check_vendor_isolation(config, ledger_source(config))
    # ...and the guard must not be vacuous: a leaked token is caught.
    mutated = ledger_source(config) + "\n// positions reconciled against ib_insync under the hood\n"
    with pytest.raises(SimLedgerCheckError):
        check_vendor_isolation(config, mutated)


def test_ledger_book_is_structurally_isolated_per_strategy() -> None:
    config = load_config()
    # The real ledger book keys each strategy by StrategyId and routes a fill to
    # only the named strategy's ledger, inserting a new ledger only on success.
    check_ledger_book_isolation(config, ledger_source(config))
    # ...and the guard must not be vacuous: inserting a new ledger under a shared
    # key instead of strategy.clone() (which would merge strategies) is caught.
    mutated = ledger_source(config).replace(
        "self.ledgers.insert(strategy.clone()",
        'self.ledgers.insert(StrategyId::new("shared")',
        1,
    )
    with pytest.raises(SimLedgerCheckError):
        check_ledger_book_isolation(config, mutated)
    # ...and dropping the `?` that propagates a rejected fresh-ledger fill before
    # the insert (which would leave a phantom strategy) is caught too.
    phantom = ledger_source(config).replace(
        "ledger.apply_fill(fill)?;", "let _ = ledger.apply_fill(fill);", 1
    )
    with pytest.raises(SimLedgerCheckError):
        check_ledger_book_isolation(config, phantom)


def test_symbol_canonicalization_keeps_one_security_in_one_position() -> None:
    config = load_config()
    # The real ledger canonicalizes symbols (trim + upper-case) on key and lookup,
    # so aliases of one security cannot split into separate positions.
    check_symbol_normalization(config, ledger_source(config))
    # ...and the guard must not be vacuous: dropping the upper-case (keeping only
    # trim) -- which would let AAPL and aapl split -- is caught.
    mutated = ledger_source(config).replace(
        "symbol.trim().to_uppercase()", "symbol.trim().to_string()", 1
    )
    with pytest.raises(SimLedgerCheckError):
        check_symbol_normalization(config, mutated)
