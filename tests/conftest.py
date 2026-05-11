"""Shared fixtures for the layered test suite.

Layers (see plan):
    L1 unit / L2 property / L3 contract / L4 boundary
    L5 integration (gated by ATP_RUN_INTEGRATION=1)
    L6 e2e        (gated by ATP_RUN_E2E=1)
    L7 domain     — trading-system-specific safety/invariant tests
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-skip integration and e2e markers unless their gate env var is set."""
    skip_integration = pytest.mark.skip(reason="set ATP_RUN_INTEGRATION=1 to run")
    skip_e2e = pytest.mark.skip(reason="set ATP_RUN_E2E=1 to run")

    run_integration = os.environ.get("ATP_RUN_INTEGRATION") == "1"
    run_e2e = os.environ.get("ATP_RUN_E2E") == "1"

    for item in items:
        if "integration" in item.keywords and not run_integration:
            item.add_marker(skip_integration)
        if "e2e" in item.keywords and not run_e2e:
            item.add_marker(skip_e2e)


@pytest.fixture(scope="session")
def project_root() -> Path:
    """Absolute path to the repo root."""
    return ROOT


@pytest.fixture()
def staged_diff() -> str:
    """Unified diff of currently-staged changes, as the critic sees them."""
    result = subprocess.run(
        ["git", "diff", "--cached", "-U0"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


@pytest.fixture()
def fake_brokerage_adapter() -> Iterator[object]:
    """In-memory stub of the brokerage adapter contract.

    Records calls and returns deterministic acks. Use for L4 boundary tests
    where you want real Strategy/Execution wiring without an IB connection.
    """

    class _FakeAdapter:
        def __init__(self) -> None:
            self.submitted: list[dict] = []
            self.cancelled: list[str] = []
            self.connected: bool = True

        def submit_order(self, order: dict) -> dict:
            if not self.connected:
                raise RuntimeError("CONNECTIVITY_BLOCKED")
            order_id = f"ord-{len(self.submitted) + 1}"
            self.submitted.append({**order, "id": order_id})
            return {"id": order_id, "status": "ACCEPTED"}

        def cancel_order(self, order_id: str) -> dict:
            self.cancelled.append(order_id)
            return {"id": order_id, "status": "CANCELLED"}

        def positions(self) -> list[dict]:
            return []

        def account_status(self) -> dict:
            return {"equity": 100_000.0, "buying_power": 100_000.0, "margin": 0.0}

    yield _FakeAdapter()


# Make tools/ importable for tests that drive existing check scripts.
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
