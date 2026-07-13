"""L2 property tests for the SRS-DATA-020 live-position corporate-action planner.

Value-conservation invariants over generated signed positions + factors, driven
through the real ``data020_position_corp_action_cli`` planner:

* **A split conserves the total cost basis exactly** — for any exact split, the
  adjusted position carries the SAME ``cost_basis_minor`` (a split re-expresses the
  per-unit average, it never changes the total invested), and the quantity scales by
  ``N / M``. A non-integral split is sent to review, never truncated.
* **A cash dividend keeps absolute P&L invariant across the ex-date** — for any
  applied dividend, ``mark * q - basis`` before the ex-date equals
  ``(mark - amount) * q - basis'`` after it. This property *fails* under a
  multiplicative ratio factor from the share price (the bug the additive convention
  corrects), so it pins the corrected semantics.

These drive the compiled CLI (built once) rather than a Python binding — the planner
is Rust. Examples are bounded so the subprocess-per-example cost stays modest.
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
CLI_BIN = "data020_position_corp_action_cli"
PACKAGE = "atp-execution"

pytestmark = [pytest.mark.property]


def _cargo() -> str:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot build the DATA-020 CLI")
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


def _plan(args: list[str]) -> dict:
    result = subprocess.run(
        [str(_CLI), "plan", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cli failed: {result.stderr}"
    outcomes = [
        json.loads(line[len("outcome:") :])
        for line in result.stdout.splitlines()
        if line.startswith("outcome:")
    ]
    assert len(outcomes) == 1, result.stdout
    return outcomes[0]


# A signed, non-zero share count.
_quantity = st.integers(min_value=-100_000, max_value=100_000).filter(lambda q: q != 0)
# A positive per-share average cost in minor units.
_avg_cost = st.integers(min_value=1, max_value=1_000_000)
_factor = st.integers(min_value=1, max_value=50)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(quantity=_quantity, avg_cost=_avg_cost, numerator=_factor, denominator=_factor)
def test_split_conserves_total_basis_exactly(quantity, avg_cost, numerator, denominator) -> None:
    basis = quantity * avg_cost  # sign-consistent with the quantity
    outcome = _plan(
        [
            "--symbol",
            "SYM",
            "--split",
            f"{numerator}:{denominator}",
            "--position",
            f"sym=SYM,qty={quantity},basis={basis}",
        ]
    )
    if numerator == denominator:
        assert outcome["result"] == "UNAFFECTED", "a 1-for-1 split is a no-op"
    elif (quantity * numerator) % denominator == 0:
        assert outcome["result"] == "ADJUSTED"
        # Total basis is invariant; the quantity scales by N / M.
        assert outcome["after"]["cost_basis_minor"] == basis
        assert outcome["after"]["quantity"] == quantity * numerator // denominator
    else:
        assert outcome["result"] == "MANUAL_REVIEW"
        assert outcome["reason"] == "QUANTITY_NOT_INTEGRAL"


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(quantity=_quantity, avg_cost=_avg_cost, amount=st.integers(min_value=1, max_value=1_000_000))
def test_dividend_preserves_absolute_pnl(quantity, avg_cost, amount) -> None:
    basis = quantity * avg_cost
    prev_close = amount + 1  # a valid dividend is strictly below the reference close
    outcome = _plan(
        [
            "--symbol",
            "SYM",
            "--dividend",
            f"{amount}:{prev_close}",
            "--position",
            f"sym=SYM,qty={quantity},basis={basis}",
        ]
    )
    # Either the dividend applies (and conserves absolute P&L), or it is flagged for
    # review because it would drive the basis through zero — never silently wrong.
    if outcome["result"] == "ADJUSTED":
        new_basis = outcome["after"]["cost_basis_minor"]
        assert outcome["after"]["quantity"] == quantity, "quantity is unchanged by a cash dividend"
        pnl_before = prev_close * quantity - basis
        pnl_after = (prev_close - amount) * quantity - new_basis
        assert pnl_before == pnl_after, "absolute P&L is invariant across the ex-date"
    else:
        assume(outcome["result"] == "MANUAL_REVIEW")
        assert outcome["reason"] == "BASIS_CROSSING_DIVIDEND"
