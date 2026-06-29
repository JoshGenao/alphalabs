"""Bind the declarative operator-workflow contract to runtime owners.

The runtime serves the *exact* surface the three declarative packages
(:mod:`atp_api`, :mod:`atp_cli`, :mod:`atp_ws`) declare, cross-checked against
the ``operator_workflow_surface_contract`` block in
``architecture/runtime_services.json``. This module is the seam that answers
two questions for every documented operation:

* Which AC workflow does it belong to? (so the runtime can enforce the
  confirmation guard and label responses)
* Who owns the real behaviour? (so a deferred operation returns a ``501``
  naming the right downstream feature)

The owner maps live here as code constants rather than in the JSON contract so
the already-green ``operator_workflow_surface_check`` block shape is untouched;
:func:`validate_owners` re-couples them to the contract's ``deferred`` list so
an owner cannot silently drift to a feature nobody is tracking.

SRS trace
---------
``SRS-API-001``. Owner attributions mirror
``operator_workflow_surface_contract.deferred[]``.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

#: Sentinel owner for operations the operator-interface runtime serves itself
#: (no downstream feature required) — e.g. its own liveness/status report.
RUNTIME_OWNER = "runtime"

# Owner of each REST capability's real handler behaviour. ``RUNTIME_OWNER``
# marks capabilities the runtime answers itself; every other value is a feature
# id that must appear in the contract's ``deferred`` list (validated below).
REST_CAPABILITY_OWNERS: dict[str, str] = {
    "STRATEGY_LIFECYCLE": "SRS-ORCH-004",
    "LIVE_DESIGNATION": "SRS-EXE-001",
    "KILL_SWITCH": "SRS-EXE-001",
    "HOT_SWAP": "SRS-RESV-003",
    "BACKTEST_LAUNCH": "SRS-BT-001",
    "BACKTEST_QUERY": "SRS-BT-009",
    "RESERVOIR_RANKING": "SRS-RESV-002",
    "WATCHLIST_CONFIG": "SRS-DATA-002",
    "SYSTEM_STATUS": RUNTIME_OWNER,
    # The LOGS feature (sink + system/strategy separation + the query handler it
    # registers on this runtime's registry) is owned end-to-end by SRS-LOG-001,
    # the actionable blocker — consistent with every other workflow (the domain
    # feature owns and registers its handler). The runtime only provides the
    # registry/dispatch substrate it plugs into.
    "LOGS": "SRS-LOG-001",
    "ALERTS": "SRS-NOTIF-001",
}

# Owner of each CLI group's real handler behaviour, with per-command overrides
# where a single group spans two owners (e.g. ``admin version`` is runtime-owned
# while ``admin logs`` belongs to SRS-LOG-001).
CLI_GROUP_OWNERS: dict[str, str] = {
    "kill-switch": "SRS-EXE-001",
    "strategy": "SRS-ORCH-004",
    "live": "SRS-EXE-001",
    "hot-swap": "SRS-RESV-003",
    "readiness": RUNTIME_OWNER,
    "admin": "SRS-LOG-001",
}
CLI_COMMAND_OWNER_OVERRIDES: dict[tuple[str, str], str] = {
    ("strategy", "rollback"): "SRS-ORCH-005",
    ("readiness", "wait"): "SRS-MD-006",
    # The `admin` group is shared: `logs` is the LOGS workflow (SRS-LOG-001),
    # `alerts` is SRS-NOTIF-001, and `config`/`version` are runtime introspection.
    ("admin", "logs"): "SRS-LOG-001",
    ("admin", "alerts"): "SRS-NOTIF-001",
    ("admin", "config"): RUNTIME_OWNER,
    ("admin", "version"): RUNTIME_OWNER,
}

# The `admin` CLI group is shared across workflows + runtime introspection, so a
# workflow that declares it must only count *its* admin commands toward
# readiness — otherwise LOGS readiness would depend on `admin alerts`
# (SRS-NOTIF-001) or the runtime-meta `admin config`/`version`. Dedicated groups
# (kill-switch / strategy / live / hot-swap / readiness) are not listed here, so
# all of their commands count.
SHARED_GROUP_WORKFLOW_COMMANDS: dict[tuple[str, str], frozenset[str]] = {
    ("LOGS", "admin"): frozenset({"logs"}),
}

# Owner of each WebSocket channel's real *publisher* (who calls
# ``runtime.publish``). A channel counts toward a workflow's readiness only once
# its publisher is registered; until then it is deferred to this owner.
WS_CHANNEL_OWNERS: dict[str, str] = {
    "PNL": "SRS-UI-001",
    "METRICS": "SRS-UI-001",
    "ACCOUNT_STATUS": "SRS-UI-001",
    "HEARTBEAT": "SRS-UI-001",
    "STRATEGY_STATE": "SRS-ORCH-004",
    "RESERVOIR_RANKING": "SRS-RESV-002",
    "LOGS": "SRS-LOG-001",
    "ALERTS": "SRS-NOTIF-001",
}

CONTRACT_BLOCK = "operator_workflow_surface_contract"


def _python_root(root: Path) -> Path:
    return root / "python"


def load_contract_block(root: Path) -> dict[str, Any]:
    """Return the ``operator_workflow_surface_contract`` block from disk."""

    raw = json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))
    block = raw.get(CONTRACT_BLOCK)
    if not isinstance(block, dict):
        raise ValueError(f"runtime_services.json missing {CONTRACT_BLOCK!r} block")
    return block


def import_surface_modules(root: Path) -> tuple[Any, Any, Any]:
    """Import ``atp_api``, ``atp_cli``, ``atp_ws`` from ``<root>/python``.

    Returns the three modules in REST/CLI/WebSocket order.
    """

    python_root = str(_python_root(root))
    if python_root not in sys.path:
        sys.path.insert(0, python_root)
    return (
        importlib.import_module("atp_api"),
        importlib.import_module("atp_cli"),
        importlib.import_module("atp_ws"),
    )


def rest_owner(capability_value: str) -> str:
    """Owner feature for a REST capability (raises on an unknown capability)."""

    try:
        return REST_CAPABILITY_OWNERS[capability_value]
    except KeyError as exc:  # pragma: no cover - guarded by validate_owners
        raise KeyError(f"no owner mapping for REST capability {capability_value!r}") from exc


def cli_owner(group_value: str, command_name: str) -> str:
    """Owner feature for a CLI ``group``/``command`` (command override wins)."""

    override = CLI_COMMAND_OWNER_OVERRIDES.get((group_value, command_name))
    if override is not None:
        return override
    try:
        return CLI_GROUP_OWNERS[group_value]
    except KeyError as exc:  # pragma: no cover - guarded by validate_owners
        raise KeyError(f"no owner mapping for CLI group {group_value!r}") from exc


def ws_owner(channel_value: str) -> str:
    """Owner feature for a WebSocket channel's publisher (raises on unknown)."""

    try:
        return WS_CHANNEL_OWNERS[channel_value]
    except KeyError as exc:  # pragma: no cover - guarded by validate_owners
        raise KeyError(f"no owner mapping for WebSocket channel {channel_value!r}") from exc


def deferred_feature_ids(block: dict[str, Any]) -> frozenset[str]:
    """Return the set of feature ids named in the contract's ``deferred`` list."""

    return frozenset(
        entry["feature"]
        for entry in block.get("deferred", [])
        if isinstance(entry, dict) and "feature" in entry
    )


def validate_owners(
    block: dict[str, Any], rest_routes: Any, cli_commands: Any, ws_channels: Any = ()
) -> None:
    """Assert every owner is ``runtime`` or a feature named in ``deferred[]``.

    This re-couples the in-code owner maps to the contract so an owner cannot
    point at a feature nobody is tracking, and so a new REST capability / CLI
    command / WebSocket channel added to the declarative surface cannot land
    without an owner. Every owner of a *deferred operation* must be declared in
    ``deferred[]`` — even a ``passes:true`` feature whose own work is done but
    whose HTTP/CLI binding is still pending (the operation is what's deferred).

    Args:
        block: The contract block (for its ``deferred`` feature ids).
        rest_routes: The declared REST routes (``atp_api.ROUTES``).
        cli_commands: The declared CLI commands (``atp_cli.COMMANDS``).
        ws_channels: The declared WebSocket channels (``atp_ws.EVENT_CHANNELS``).
    """

    known = deferred_feature_ids(block) | {RUNTIME_OWNER}

    declared_caps = {route.capability.value for route in rest_routes}
    mapped_caps = set(REST_CAPABILITY_OWNERS)
    if declared_caps - mapped_caps:
        raise ValueError(
            f"REST capabilities without an owner mapping: {sorted(declared_caps - mapped_caps)}"
        )
    for capability, owner in REST_CAPABILITY_OWNERS.items():
        if owner not in known:
            raise ValueError(
                f"REST capability {capability!r} owner {owner!r} is not runtime "
                f"or a feature named in the contract deferred[]"
            )

    declared_groups = {command.group.value for command in cli_commands}
    mapped_groups = set(CLI_GROUP_OWNERS)
    if declared_groups - mapped_groups:
        raise ValueError(
            f"CLI groups without an owner mapping: {sorted(declared_groups - mapped_groups)}"
        )
    owners = set(CLI_GROUP_OWNERS.values()) | set(CLI_COMMAND_OWNER_OVERRIDES.values())
    for owner in owners:
        if owner not in known:
            raise ValueError(
                f"CLI owner {owner!r} is not runtime or a feature named in the deferred[]"
            )

    declared_channels = {channel.name.value for channel in ws_channels}
    mapped_channels = set(WS_CHANNEL_OWNERS)
    if declared_channels - mapped_channels:
        raise ValueError(
            f"WebSocket channels without an owner mapping: "
            f"{sorted(declared_channels - mapped_channels)}"
        )
    for channel, owner in WS_CHANNEL_OWNERS.items():
        if owner not in known:
            raise ValueError(
                f"WebSocket channel {channel!r} owner {owner!r} is not runtime "
                f"or a feature named in the contract deferred[]"
            )
