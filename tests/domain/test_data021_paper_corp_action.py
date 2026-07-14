"""SRS-DATA-021 — apply corporate actions to paper virtual positions and orders.

L7 domain (safety) scenario test. Anchors the safety post-conditions of the paper
corporate-action application in the domain-test layer and walks the AC end to end
over fixtures:

* The Rust suites ``srs_data_021_corp_action_facts`` (atp-data: the coverage-gated
  corporate-action FACT read — the "same data source" seam — fails closed
  uncovered, is point-in-time bounded, and surfaces split/dividend terms with the
  resolved reference close) and ``srs_data_021_paper_corp_action``
  (atp-simulation: the split / dividend / merger / symbol-change / delisting
  application over the real ledger fill path and order intake, the fallible alert
  sink, and the store-facts-drive-the-application binding) pass.
* The ``data021_paper_corp_action_cli`` scenario CLI adjusts a position's quantity
  and average cost for a split (basis invariant), rebases resting-order prices
  half-to-even, and — the AC's cancel clause — cancels virtual orders on a
  delisted security while leaving other symbols resting; ``apply-from-store``
  proves the SAME-data-source clause: corporate-action RECORDS ingested into the
  SRS-DATA-011 store surface through the coverage-gated fact read and drive the
  paper application in event order, and an UNCOVERED store refuses the read
  (exit 2) rather than acting on a window that could hide an action.

The AC clauses proven here over fixtures (the SRS names "Scenario test" as this
requirement's verification method):
  1. paper virtual positions and average cost adjust for splits, dividends, and
     mergers (exact integer math, fail-closed to manual review, position
     untouched on review);
  2. virtual orders for delisted securities are canceled (terminally, with an
     operator-notification intent);
  3. the corporate-action data source is the SAME SRS-DATA-011 store (and the
     same coverage gate) live trading's planners and backtesting's adjusted bar
     reads consume.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = [pytest.mark.domain, pytest.mark.safety]

CLI_BIN = "data021_paper_corp_action_cli"


# --------------------------------------------------------------------------- #
# Rust suites (shelled so the safety post-conditions are anchored here)
# --------------------------------------------------------------------------- #


def _cargo() -> str:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run the Rust DATA-021 suites")
    return cargo


def _run_cargo_test(package: str, suite: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_cargo(), "test", "-p", package, "--test", suite],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_all_passed(result: subprocess.CompletedProcess[str], label: str) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"{label} Rust suite failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "test result: ok." in combined and "0 failed" in combined, (
        f"unexpected cargo test output for {label}:\n{combined}"
    )


def test_rust_fact_read_suite_passes() -> None:
    _assert_all_passed(
        _run_cargo_test("atp-data", "srs_data_021_corp_action_facts"), "fact-read"
    )


def test_rust_paper_application_suite_passes() -> None:
    _assert_all_passed(
        _run_cargo_test("atp-simulation", "srs_data_021_paper_corp_action"),
        "paper-application",
    )


# --------------------------------------------------------------------------- #
# Scenario CLI
# --------------------------------------------------------------------------- #


def _cli_path() -> Path:
    cargo = _cargo()
    build = subprocess.run(
        [cargo, "build", "-p", "atp-simulation", "--bin", CLI_BIN],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, f"cli build failed:\n{build.stderr}"
    path = ROOT / "target" / "debug" / CLI_BIN
    assert path.exists(), f"built CLI not found at {path}"
    return path


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_cli_path()), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _lines(stdout: str, prefix: str) -> list[dict]:
    return [
        json.loads(line[len(prefix) :])
        for line in stdout.splitlines()
        if line.startswith(prefix)
    ]


def test_split_adjusts_position_and_rebases_resting_order() -> None:
    result = _run_cli(
        [
            "apply",
            "--symbol",
            "AAPL",
            "--split",
            "4:1",
            "--position",
            "strat=alpha,sym=AAPL,qty=100,price=5000",
            "--order",
            "strat=alpha,sym=AAPL,side=buy,qty=100,type=limit:4000",
        ]
    )
    assert result.returncode == 0, result.stderr
    positions = _lines(result.stdout, "position-outcome:")
    assert positions == [
        {
            "strategy": "alpha",
            "symbol": "AAPL",
            "kind": "ADJUSTED",
            "quantity_before": 100,
            "quantity_after": 400,
            "cost_basis_before_minor": 500000,
            "cost_basis_after_minor": 500000,
        }
    ], "quantity x4, total basis invariant (average cost re-derives to 1250)"
    orders = _lines(result.stdout, "order-outcome:")
    assert orders[0]["kind"] == "ADJUSTED"
    assert orders[0]["quantity_after"] == 400
    assert orders[0]["order_type_after"] == "limit:1000"


def test_dividend_adjusts_basis_and_review_leaves_position_untouched() -> None:
    dividend = _run_cli(
        [
            "apply",
            "--symbol",
            "AAPL",
            "--dividend",
            "100:4000",
            "--position",
            "strat=alpha,sym=AAPL,qty=100,price=5000",
        ]
    )
    assert dividend.returncode == 0, dividend.stderr
    outcome = _lines(dividend.stdout, "position-outcome:")[0]
    assert outcome["kind"] == "ADJUSTED"
    assert outcome["cost_basis_after_minor"] == 490000, "additive: basis - 100*100"

    # An odd lot under a 1-for-2 reverse split is cash-in-lieu -> MANUAL_REVIEW
    # with a notification intent; the summary confirms nothing was adjusted.
    review = _run_cli(
        [
            "apply",
            "--symbol",
            "AAPL",
            "--split",
            "1:2",
            "--position",
            "strat=alpha,sym=AAPL,qty=101,price=5000",
        ]
    )
    assert review.returncode == 0, review.stderr
    outcome = _lines(review.stdout, "position-outcome:")[0]
    assert outcome["kind"] == "MANUAL_REVIEW"
    assert outcome["reason"] == "QUANTITY_NOT_INTEGRAL"
    alerts = _lines(review.stdout, "alert:")
    assert alerts and alerts[0]["kind"] == "MANUAL_REVIEW"
    summary = _lines(review.stdout, "summary:")[0]
    assert summary["adjusted"] == 0 and summary["review"] == 1


def test_merger_remaps_position_and_cancels_acquired_orders() -> None:
    result = _run_cli(
        [
            "apply",
            "--symbol",
            "OLD",
            "--merger",
            "NEW:3:2:0",
            "--position",
            "strat=alpha,sym=OLD,qty=200,price=4000",
            "--order",
            "strat=alpha,sym=OLD,side=sell,qty=10,type=market",
        ]
    )
    assert result.returncode == 0, result.stderr
    position = _lines(result.stdout, "position-outcome:")[0]
    assert position["kind"] == "REMAPPED"
    assert position["successor"] == "NEW"
    assert position["quantity_after"] == 300
    assert position["cost_basis_after_minor"] == 800000, "basis carried intact"
    order = _lines(result.stdout, "order-outcome:")[0]
    assert order["kind"] == "CANCELLED"
    assert order["reason"] == "MERGER_TERMINATION"

    # A MIXED stock-and-cash merger also adjusts: the cash leg reduces the
    # basis additively on the pre-conversion count (500000 - 250*100), and a
    # pure-cash acquisition (numerator 0) fails closed to review instead.
    mixed = _run_cli(
        [
            "apply",
            "--symbol",
            "OLD",
            "--merger",
            "NEW:1:1:250",
            "--position",
            "strat=alpha,sym=OLD,qty=100,price=5000",
        ]
    )
    assert mixed.returncode == 0, mixed.stderr
    outcome = _lines(mixed.stdout, "position-outcome:")[0]
    assert outcome["kind"] == "REMAPPED"
    assert outcome["cost_basis_after_minor"] == 475000
    pure_cash = _run_cli(
        [
            "apply",
            "--symbol",
            "OLD",
            "--merger",
            "NEW:0:1:5500",
            "--position",
            "strat=alpha,sym=OLD,qty=100,price=5000",
        ]
    )
    assert pure_cash.returncode == 0, pure_cash.stderr
    outcome = _lines(pure_cash.stdout, "position-outcome:")[0]
    assert outcome["kind"] == "MANUAL_REVIEW"
    assert outcome["reason"] == "CASH_CONSIDERATION_NOT_SUPPORTED"


def test_delisting_cancels_only_the_delisted_symbols_orders() -> None:
    result = _run_cli(
        [
            "apply",
            "--symbol",
            "DEAD",
            "--delisting",
            "--position",
            "strat=alpha,sym=DEAD,qty=100,price=5000",
            "--order",
            "strat=alpha,sym=DEAD,side=buy,qty=10,type=market",
            "--order",
            "strat=beta,sym=DEAD,side=sell,qty=20,type=limit:4900",
            "--order",
            "strat=alpha,sym=LIVE,side=buy,qty=10,type=market",
        ]
    )
    assert result.returncode == 0, result.stderr
    orders = _lines(result.stdout, "order-outcome:")
    assert len(orders) == 2, "only DEAD orders are touched"
    assert all(
        o["symbol"] == "DEAD" and o["kind"] == "CANCELLED" and o["reason"] == "DELISTING"
        for o in orders
    )
    position = _lines(result.stdout, "position-outcome:")[0]
    assert position["kind"] == "DELISTED_HOLD"
    alerts = _lines(result.stdout, "alert:")
    assert {a["kind"] for a in alerts} == {"DELISTED_HOLD", "ORDER_CANCELLED"}
    summary = _lines(result.stdout, "summary:")[0]
    assert summary["orders_cancelled"] == 2 and summary["delisted_hold"] == 1


def test_store_facts_drive_the_application_end_to_end() -> None:
    """The same-data-source clause: SRS-DATA-011 records -> gated fact read ->
    paper application, in event order (split first, then the dividend on the
    post-split share count)."""
    result = _run_cli(
        [
            "apply-from-store",
            "--facts-symbol",
            "AAPL",
            "--facts-symbol",
            "DEAD",
            "--facts-window",
            "0:500",
            "--bar",
            "AAPL:100:4000",
            "--split-record",
            "AAPL:200:4:1",
            "--dividend-record",
            "AAPL:300:100",
            "--delisting-record",
            "DEAD:400",
            "--coverage",
            "AAPL:500",
            "--coverage",
            "DEAD:500",
            "--position",
            "strat=alpha,sym=AAPL,qty=100,price=5000",
            "--position",
            "strat=alpha,sym=DEAD,qty=10,price=1000",
            "--order",
            "strat=alpha,sym=DEAD,side=buy,qty=5,type=market",
        ]
    )
    assert result.returncode == 0, result.stderr
    facts = _lines(result.stdout, "fact:")
    assert [f["kind"] for f in facts] == ["SPLIT", "DIVIDEND", "DELISTING"]
    assert facts[1]["prev_close_minor"] == 4000, "reference close resolved from the store"
    positions = _lines(result.stdout, "position-outcome:")
    # Split: 100 -> 400 shares, basis invariant. Dividend: applied to the
    # POST-SPLIT 400 shares, basis 500000 - 100*400 = 460000.
    assert positions[0]["kind"] == "ADJUSTED" and positions[0]["quantity_after"] == 400
    assert positions[1]["cost_basis_after_minor"] == 460000
    assert positions[2]["kind"] == "DELISTED_HOLD"
    orders = _lines(result.stdout, "order-outcome:")
    assert orders[0]["kind"] == "CANCELLED" and orders[0]["reason"] == "DELISTING"


def test_uncovered_store_refuses_the_fact_read() -> None:
    """The application inherits the SRS-DATA-011 coverage gate: a store with the
    split record but NO coverage record refuses the read (exit 2) — the paper
    books are never adjusted from a window that could hide an action."""
    result = _run_cli(
        [
            "apply-from-store",
            "--facts-symbol",
            "AAPL",
            "--facts-window",
            "0:500",
            "--split-record",
            "AAPL:200:4:1",
            "--position",
            "strat=alpha,sym=AAPL,qty=100,price=5000",
        ]
    )
    assert result.returncode == 2
    assert "fact read refused" in result.stderr
    assert "position-outcome:" not in result.stdout, "no adjustment happened"


def test_bad_input_fails_closed() -> None:
    # Unknown flag.
    assert _run_cli(["apply", "--symbol", "A", "--delisting", "--bogus", "x"]).returncode == 2
    # Two action flags.
    assert (
        _run_cli(
            ["apply", "--symbol", "A", "--delisting", "--split", "2:1"]
        ).returncode
        == 2
    )
    # Malformed position spec (ledger rejects a zero-quantity fill).
    assert (
        _run_cli(
            [
                "apply",
                "--symbol",
                "A",
                "--delisting",
                "--position",
                "strat=alpha,sym=A,qty=0,price=100",
            ]
        ).returncode
        == 2
    )
    # Order intake rejects a non-positive limit price.
    assert (
        _run_cli(
            [
                "apply",
                "--symbol",
                "A",
                "--delisting",
                "--order",
                "strat=alpha,sym=A,side=buy,qty=1,type=limit:0",
            ]
        ).returncode
        == 2
    )
    # apply-from-store rejects action flags (its actions come from the store).
    assert (
        _run_cli(
            [
                "apply-from-store",
                "--facts-symbol",
                "A",
                "--facts-window",
                "0:10",
                "--delisting",
            ]
        ).returncode
        == 2
    )
