"""Active-strategy inventory provider (``SRS-UI-002`` / SyRS SYS-41 + SYS-79).

Feeds the dashboard's strategy-inventory panel and the ``STRATEGY_STATE``
WebSocket channel: one row per recorded strategy carrying the fields the AC
names — name, mode, asset class, container status, deployed code version, P&L,
and position count.

Honesty (no fabrication — the SRS-UI-001 convention)
----------------------------------------------------
The one REAL per-strategy signal available today is the **deployed code
version** (SyRS SYS-79 / SRS-ORCH-004): the inventory source shells the
cargo-built ``orch005_rollback_cli list`` operator binary (the repo's
subprocess → Rust CLI → parse-stdout boundary) over the SRS-ORCH-005 deployment
state snapshot, so the panel renders the same ``version_identifier`` the
orchestrator recorded — the exact SYS-41 rendering leg
``deployment_version_contract.deferred[]`` assigns to SRS-UI-002. ``name`` is
the strategy id (no separate human-name field exists anywhere in the system).
Every other AC field's live producer is a not-yet-built feature and is carried
as an explicit ``{"value": None, "data_source": "deferred:<owner>"}`` cell —
never a fabricated number:

* ``mode`` / ``lifecycle_state`` — the ORCH-004 lifecycle dispatcher / live
  designation state (``SRS-ORCH-004`` / ``SRS-EXE-001``);
* ``asset_class`` — a per-security property today (``AssetClass`` lives on
  orders/ticks, not the deployment record); a strategy-level asset class needs
  the strategy registry (``SRS-ORCH-004``);
* ``container_status`` — the concrete container runtime / WorkloadRegistry
  (``SRS-ORCH-001`` / ``SRS-ARCH-004``);
* ``pnl`` — the SYS-70-fed metrics accumulator (``SRS-BT-004``; P&L rides the
  per-strategy ``PNL`` channel per the atp_ws contract — ``STRATEGY_STATE``
  deliberately carries no pnl field);
* ``position_count`` — the live virtual-ledger wiring (``SRS-SIM-003`` /
  ``SRS-EXE-005`` runtime feed).

A missing or unreadable snapshot is reported as an explicit unavailable
inventory (``ok: false`` + the reason) — a monitoring surface must not crash,
and an absent snapshot must never masquerade as "no strategies deployed".

SRS trace
---------
``SRS-UI-002`` (inventory panel), SyRS ``SYS-41`` (strategy management view) /
``SYS-79`` (deployed version rendered on the dashboard), ``NFR-P2`` (the
STRATEGY_STATE channel's ≤5 s cadence), consuming ``SRS-ORCH-005``'s state
snapshot via its operator CLI.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from atp_ws import Channel

from .provider import deferred_field_named

__all__ = [
    "INVENTORY_CHANNEL",
    "INVENTORY_FIELD_OWNERS",
    "InventoryCliRunner",
    "InventoryUnavailable",
    "RollbackSnapshotInventorySource",
    "StrategyInventoryProvider",
    "StrategyInventorySource",
]

# Default location of the cargo-built operator binary, relative to the repo root
# (python/atp_dashboard/inventory.py -> parents[2] == repo root). Build it with
# ``cargo build -p atp-orchestrator --bin orch005_rollback_cli``.
_DEFAULT_BINARY = Path(__file__).resolve().parents[2] / "target" / "debug" / "orch005_rollback_cli"

# Per-invocation subprocess budget (seconds) — a wedged binary surfaces as an
# unavailable inventory, never a hung dashboard tick.
_DEFAULT_TIMEOUT_S = 10.0

#: The feature that owns each still-deferred inventory field's live producer.
INVENTORY_FIELD_OWNERS: dict[str, str] = {
    "mode": "SRS-ORCH-004",
    "asset_class": "SRS-ORCH-004",
    "container_status": "SRS-ORCH-001",
    "lifecycle_state": "SRS-ORCH-004",
    "position_count": "SRS-SIM-003",
    "pnl": "SRS-BT-004",
}


class InventoryUnavailable(Exception):
    """The inventory source cannot be read right now (reported, never fabricated)."""


@runtime_checkable
class InventoryCliRunner(Protocol):
    """The subprocess surface the snapshot source depends on (injectable for tests)."""

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]: ...


def _default_runner(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    if not Path(argv[0]).exists():
        raise FileNotFoundError(
            f"inventory binary not found at {argv[0]}; build it with "
            "`cargo build -p atp-orchestrator --bin orch005_rollback_cli`"
        )
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)


@runtime_checkable
class StrategyInventorySource(Protocol):
    """Source of the recorded strategy rows (id + deployed/previous versions)."""

    def rows(self) -> list[dict[str, str]]:
        """One ``{"id", "current", "previous"}`` row per recorded strategy,
        strategy-id-sorted. Raises :class:`InventoryUnavailable` when the
        underlying record cannot be read (never an empty masquerade)."""
        ...


class RollbackSnapshotInventorySource:
    """Reads the strategy inventory from the SRS-ORCH-005 deployment snapshot via
    ``orch005_rollback_cli list`` (single format owner — the dashboard never
    parses the snapshot file itself)."""

    def __init__(
        self,
        *,
        state_path: str | Path,
        binary: str | Path | None = None,
        runner: InventoryCliRunner | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._state_path = str(state_path)
        self._binary = Path(binary) if binary is not None else _DEFAULT_BINARY
        self._runner = runner if runner is not None else _default_runner
        self._timeout = float(timeout)

    def rows(self) -> list[dict[str, str]]:
        argv = [str(self._binary), "list", "--state", self._state_path]
        try:
            completed = self._runner(argv, timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise InventoryUnavailable(f"inventory CLI unavailable: {error}") from error
        if completed.returncode != 0:
            raise InventoryUnavailable(
                f"inventory CLI refused: {completed.stderr.strip() or 'nonzero exit'}"
            )
        return _parse_rows(completed.stdout)


def _parse_rows(stdout: str) -> list[dict[str, str]]:
    """Parse the ``strategy_count`` / ``strategy.<i>.*`` proof lines fail-closed:
    a count/row mismatch or a row missing its id/current fields is CLI drift,
    reported as unavailable rather than a partial inventory."""

    count: int | None = None
    rows: dict[int, dict[str, str]] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        if key == "strategy_count":
            count = int(value)
        elif key.startswith("strategy."):
            parts = key.split(".", 2)
            if len(parts) == 3:
                rows.setdefault(int(parts[1]), {})[parts[2]] = value
    if count is None:
        raise InventoryUnavailable("inventory CLI output missing strategy_count")
    ordered = [rows[index] for index in sorted(rows)]
    if len(ordered) != count or sorted(rows) != list(range(count)):
        raise InventoryUnavailable(
            f"inventory CLI output inconsistent: strategy_count={count} but rows={sorted(rows)}"
        )
    for row in ordered:
        if not row.get("id") or not row.get("current"):
            raise InventoryUnavailable(f"inventory CLI row missing id/current: {row}")
    return ordered


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class StrategyInventoryProvider:
    """Assembles the SRS-UI-002 inventory payloads from a
    :class:`StrategyInventorySource` (fail-safe: an unreadable source becomes an
    explicit unavailable inventory, never a crash or an empty masquerade)."""

    def __init__(self, source: StrategyInventorySource) -> None:
        self._source = source

    def inventory_snapshot(self) -> dict[str, object]:
        """The REST poll body served at ``GET /dashboard/api/strategies``."""

        try:
            rows = self._source.rows()
        except InventoryUnavailable as unavailable:
            return {
                "generated_at": _utc_iso(),
                "ok": False,
                "error": str(unavailable),
                "strategies": [],
                "srs_ref": "SRS-UI-002",
            }
        return {
            "generated_at": _utc_iso(),
            "ok": True,
            "strategies": [self._strategy_payload(row) for row in rows],
            "srs_ref": "SRS-UI-002",
        }

    def strategy_state_events(self) -> list[dict[str, object]]:
        """The STRATEGY_STATE events for one publish tick: a summary event (so a
        subscriber's freshness signal ticks even with zero strategies or an
        unavailable source) followed by one event per strategy, fields exactly
        as the atp_ws contract declares."""

        summary: dict[str, object] = {
            "strategy_id": None,
            "as_of": _utc_iso(),
            "event": "inventory-summary",
        }
        try:
            rows = self._source.rows()
        except InventoryUnavailable as unavailable:
            summary["ok"] = False
            summary["error"] = str(unavailable)
            summary["strategy_count"] = None
            return [summary]
        summary["ok"] = True
        summary["strategy_count"] = len(rows)
        return [summary] + [self._strategy_payload(row) for row in rows]

    @staticmethod
    def _strategy_payload(row: dict[str, str]) -> dict[str, object]:
        """One strategy's payload: keys exactly the STRATEGY_STATE contract's
        ``payload_fields`` (plus the canonical ``version_identifier`` and the
        AC's pnl cell, allowed by the contract's open schema)."""

        version_identifier = row["current"]
        # version_identifier is `<hash>@<deployed_at>`; the declared field
        # carries the hash half (matching the REST route's field), while the
        # full identifier is the ORCH-004 canonical rendered string.
        deployment_version_hash = version_identifier.split("@", 1)[0]
        payload: dict[str, object] = {
            "strategy_id": row["id"],
            # No separate human-name field exists anywhere in the system; the
            # id IS the name (a real value, not a placeholder).
            "name": row["id"],
            "as_of": _utc_iso(),
            "deployment_version_hash": {
                "value": deployment_version_hash,
                "data_source": "live:orch005_rollback_cli",
            },
            "version_identifier": {
                "value": version_identifier,
                "data_source": "live:orch005_rollback_cli",
            },
            "previous_version_identifier": {
                "value": None if row.get("previous", "-") == "-" else row["previous"],
                "data_source": "live:orch005_rollback_cli",
            },
        }
        for field, owner in INVENTORY_FIELD_OWNERS.items():
            payload[field] = deferred_field_named(owner)
        return payload


#: The channel this provider publishes (kept next to the provider so the
#: publisher and the safety test share one authority).
INVENTORY_CHANNEL: str = Channel.STRATEGY_STATE
