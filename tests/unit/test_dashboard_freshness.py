"""L1 unit — the SRS-UI-001 freshness classifier ships the 5s contract boundary.

Exercises the exact browser classifier (``python/atp_dashboard/assets/freshness.js``)
via node, so a required channel that is OVER its refresh budget is never
classified ``fresh`` (regression guard: SRS-UI-001 / NFR-P2 must fail at the
contract boundary, not budget + jitter grace). Skips where node is unavailable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

FRESHNESS_JS = (
    Path(__file__).resolve().parents[2] / "python" / "atp_dashboard" / "assets" / "freshness.js"
)
_NODE = shutil.which("node")


def _classify(staleness: object, budget: int, grace: int = 1500) -> str:
    arg = "null" if staleness is None else str(staleness)
    script = (
        f"const {{freshnessState}} = require({json.dumps(str(FRESHNESS_JS))});"
        f"process.stdout.write(String(freshnessState({arg}, {budget}, {grace})));"
    )
    result = subprocess.run([str(_NODE), "-e", script], capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.mark.skipif(_NODE is None, reason="node unavailable to exercise the browser classifier")
@pytest.mark.parametrize(
    "staleness,budget,expected",
    [
        (0, 5000, "fresh"),
        (4999, 5000, "fresh"),
        (5000, 5000, "fresh"),  # inclusive at the budget
        (5001, 5000, "warn"),  # <-- over the 5s contract => NOT fresh (the regression)
        (6000, 5000, "warn"),  # METRICS arriving after 5s is not fresh
        (6500, 5000, "warn"),
        (6501, 5000, "stale"),  # past the jitter grace => stale
        (900, 1000, "fresh"),
        (2600, 1000, "stale"),  # a 1s channel silent for 2.6s is stale
        (None, 5000, "wait"),
    ],
)
def test_over_budget_channel_is_never_fresh(staleness: object, budget: int, expected: str) -> None:
    assert _classify(staleness, budget) == expected


@pytest.mark.skipif(_NODE is None, reason="node unavailable to exercise the browser classifier")
def test_metrics_after_5s_is_not_fresh() -> None:
    # Direct SRS-UI-001 regression: the 5s METRICS/benchmark panel refreshing
    # after its 5s budget must never be reported healthy to the operator.
    assert _classify(6000, 5000) != "fresh"
