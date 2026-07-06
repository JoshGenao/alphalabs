"""SRS-UI-002 strategy-inventory provider — L1 unit tests (fake CLI runner, no cargo).

Pins the honesty contract of the inventory feed: the deployed code version is
the one REAL per-strategy value (parsed from the ``orch005_rollback_cli list``
proof lines), every other AC field is an explicit deferred cell naming its
producer feature, an unreadable source is an explicit unavailable inventory
(never a crash or an empty masquerade), and the per-strategy event keys cover
the ``STRATEGY_STATE`` contract's declared ``payload_fields`` so the WS
contract and the rendered panel never drift.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_dashboard.inventory import (  # noqa: E402
    INVENTORY_FIELD_OWNERS,
    RollbackSnapshotInventorySource,
    StrategyInventoryProvider,
)
from atp_ws import EVENT_CHANNELS, Channel  # noqa: E402

HASH_V1 = "sha256:" + "1" * 64
HASH_V2 = "sha256:" + "2" * 64

LIST_STDOUT = (
    "strategy_count:2\n"
    f"strategy.0.id:alpha-1\n"
    f"strategy.0.current:{HASH_V2}@200\n"
    f"strategy.0.previous:{HASH_V1}@100\n"
    f"strategy.1.id:beta-9\n"
    f"strategy.1.current:{HASH_V1}@300\n"
    f"strategy.1.previous:-\n"
)


class _FakeRunner:
    def __init__(self, *, stdout: str = LIST_STDOUT, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


def _provider(runner: _FakeRunner) -> StrategyInventoryProvider:
    return StrategyInventoryProvider(
        RollbackSnapshotInventorySource(
            state_path="/tmp/does-not-matter.state",
            binary="/tmp/fake-orch005_rollback_cli",
            runner=runner,
        )
    )


def test_snapshot_parses_rows_with_the_real_deployed_version() -> None:
    snapshot = _provider(_FakeRunner()).inventory_snapshot()
    assert snapshot["ok"] is True
    strategies = snapshot["strategies"]
    assert [row["strategy_id"] for row in strategies] == ["alpha-1", "beta-9"]
    first = strategies[0]
    # The deployed version is REAL — the canonical identifier plus the hash half.
    assert first["version_identifier"] == {
        "value": f"{HASH_V2}@200",
        "data_source": "live:orch005_rollback_cli",
    }
    assert first["deployment_version_hash"]["value"] == HASH_V2
    assert first["previous_version_identifier"]["value"] == f"{HASH_V1}@100"
    # No retained previous -> an honest None, not a dash-string leak.
    assert strategies[1]["previous_version_identifier"]["value"] is None


def test_every_unbuilt_ac_field_is_an_explicit_deferred_cell() -> None:
    (row,) = _provider(
        _FakeRunner(
            stdout=f"strategy_count:1\nstrategy.0.id:a\nstrategy.0.current:{HASH_V1}@1\nstrategy.0.previous:-\n"
        )
    ).inventory_snapshot()["strategies"]
    for field, owner in INVENTORY_FIELD_OWNERS.items():
        assert row[field] == {"value": None, "data_source": f"deferred:{owner}"}, field


def test_events_cover_the_strategy_state_contract_fields() -> None:
    events = _provider(_FakeRunner()).strategy_state_events()
    # Summary first (freshness ticks even with zero strategies), then one per strategy.
    assert events[0]["event"] == "inventory-summary"
    assert events[0]["ok"] is True and events[0]["strategy_count"] == 2
    declared = next(
        spec.payload_fields for spec in EVENT_CHANNELS if spec.name == Channel.STRATEGY_STATE
    )
    for event in events[1:]:
        missing = [field for field in declared if field not in event]
        assert not missing, f"event missing declared payload fields: {missing}"


def test_unreadable_source_is_an_explicit_unavailable_inventory() -> None:
    provider = _provider(_FakeRunner(returncode=1, stderr="state file missing"))
    snapshot = provider.inventory_snapshot()
    assert snapshot["ok"] is False
    assert "state file missing" in str(snapshot["error"])
    assert snapshot["strategies"] == []
    (summary,) = provider.strategy_state_events()
    assert summary["ok"] is False and summary["strategy_count"] is None


@pytest.mark.parametrize(
    "stdout",
    [
        "",  # no count line at all
        "strategy_count:2\nstrategy.0.id:a\nstrategy.0.current:x@1\n",  # count/rows mismatch
        f"strategy_count:1\nstrategy.0.current:{HASH_V1}@1\n",  # row missing its id
    ],
)
def test_drifted_cli_output_is_unavailable_not_a_partial_inventory(stdout: str) -> None:
    snapshot = _provider(_FakeRunner(stdout=stdout)).inventory_snapshot()
    assert snapshot["ok"] is False, stdout
    assert snapshot["strategies"] == []
