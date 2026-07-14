"""L2 property tests for the SRS-DATA-021 paper corporate-action application.

Value-conservation invariants over generated signed paper positions + factors,
driven through the real ``data021_paper_corp_action_cli`` (the position is built
through the ledger's own fill path, so every example also passes the ledger's
intake invariants):

* **A split conserves the total cost basis exactly** — for any exact split the
  adjusted position carries the SAME ``cost_basis_after_minor`` (a split
  re-expresses the per-unit average, never the total invested) and the quantity
  scales by ``N / M``; a non-integral split goes to MANUAL_REVIEW with the
  position untouched, never truncated.
* **A cash dividend keeps absolute P&L invariant across the ex-date** — for any
  applied dividend, ``mark * q - basis`` before the ex-date equals
  ``(mark - amount) * q - basis'`` after it. This property *fails* under a
  multiplicative price-ratio factor (the bug the additive convention corrects),
  so it pins the corrected semantics — byte-consistent with the SRS-DATA-020
  live planner.

These drive the compiled CLI (built once); examples are bounded so the
subprocess-per-example cost stays modest.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[2]
CLI_BIN = "data021_paper_corp_action_cli"
PACKAGE = "atp-simulation"

pytestmark = [pytest.mark.property]


def _cargo() -> str:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot build the DATA-021 CLI")
    return cargo


def _build_cli() -> Path:
    build = subprocess.run(
        [_cargo(), "build", "-p", PACKAGE, "--bin", CLI_BIN],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, f"cli build failed:\n{build.stderr}"
    path = ROOT / "target" / "debug" / CLI_BIN
    assert path.exists(), f"built CLI not found at {path}"
    return path


# Build once at import; every generated example reuses the binary.
_CLI = _build_cli()


def _apply(args: list[str]) -> tuple[list[dict], dict]:
    result = subprocess.run(
        [str(_CLI), "apply", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cli failed: {result.stderr}"
    outcomes = [
        json.loads(line[len("position-outcome:") :])
        for line in result.stdout.splitlines()
        if line.startswith("position-outcome:")
    ]
    summaries = [
        json.loads(line[len("summary:") :])
        for line in result.stdout.splitlines()
        if line.startswith("summary:")
    ]
    assert len(summaries) == 1, result.stdout
    return outcomes, summaries[0]


# A signed, non-zero share count (the CLI opens the position through a real fill).
_quantity = st.integers(min_value=-100_000, max_value=100_000).filter(lambda q: q != 0)
# A positive per-share fill price in minor units (basis = qty * price, signed).
_price = st.integers(min_value=1, max_value=1_000_000)
_factor = st.integers(min_value=1, max_value=50)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(quantity=_quantity, price=_price, numerator=_factor, denominator=_factor)
def test_split_conserves_total_basis_exactly(quantity, price, numerator, denominator) -> None:
    basis = quantity * price
    outcomes, summary = _apply(
        [
            "--symbol",
            "SYM",
            "--split",
            f"{numerator}:{denominator}",
            "--position",
            f"strat=alpha,sym=SYM,qty={quantity},price={price}",
        ]
    )
    if numerator == denominator:
        assert outcomes == [], "a 1-for-1 split transforms nothing"
        assert summary["adjusted"] == 0
    elif (quantity * numerator) % denominator == 0:
        assert outcomes[0]["kind"] == "ADJUSTED"
        # Total basis is invariant; the quantity scales by N / M (sign preserved).
        assert outcomes[0]["cost_basis_after_minor"] == basis
        assert outcomes[0]["quantity_after"] == quantity * numerator // denominator
    else:
        assert outcomes[0]["kind"] == "MANUAL_REVIEW"
        assert outcomes[0]["reason"] == "QUANTITY_NOT_INTEGRAL"
        assert summary["adjusted"] == 0, "the position is untouched on review"


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    quantity=_quantity,
    price=_price,
    amount=st.integers(min_value=1, max_value=1_000_000),
)
def test_dividend_preserves_absolute_pnl(quantity, price, amount) -> None:
    basis = quantity * price
    prev_close = amount + 1  # a valid dividend is strictly below the reference close
    outcomes, _summary = _apply(
        [
            "--symbol",
            "SYM",
            "--dividend",
            f"{amount}:{prev_close}",
            "--position",
            f"strat=alpha,sym=SYM,qty={quantity},price={price}",
        ]
    )
    # Either the dividend applies (and conserves absolute P&L), or it is flagged
    # for review because it would drive the basis through zero — never silently
    # wrong.
    assert outcomes, "a non-flat position on the symbol always yields an outcome"
    if outcomes[0]["kind"] == "ADJUSTED":
        new_basis = outcomes[0]["cost_basis_after_minor"]
        assert outcomes[0]["quantity_after"] == quantity, "quantity unchanged by a dividend"
        pnl_before = prev_close * quantity - basis
        pnl_after = (prev_close - amount) * quantity - new_basis
        assert pnl_before == pnl_after, "absolute P&L is invariant across the ex-date"
    else:
        assume(outcomes[0]["kind"] == "MANUAL_REVIEW")
        assert outcomes[0]["reason"] == "BASIS_CROSSING_DIVIDEND"
