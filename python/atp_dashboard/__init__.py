"""SRS-UI-001 web dashboard: live performance, system health, latency, benchmark.

Top-layer package built on the :mod:`atp_runtime` operator-interface runtime
(``SRS-API-001``). It serves a self-contained web dashboard and drives the live
``PNL`` / ``METRICS`` / ``HEARTBEAT`` WebSocket channels it owns, refreshing
within the ``NFR-P2`` 5-second budget. It imports only downward
(``atp_runtime`` / ``atp_readiness`` / ``atp_ws``) — never a core trading engine.

Metric values produced by not-yet-built features (``SRS-BT-004`` / ``SRS-BT-005``
/ ``SRS-PERF-001`` / ``SRS-MD-006`` / ``SRS-MD-007``) are surfaced as honest
deferred placeholders, never fabricated.
"""

from __future__ import annotations

from .provider import (
    DEFERRED,
    LIVE,
    OWNED_CHANNELS,
    REFRESH_BUDGET_MS,
    DashboardMetricsProvider,
    ReadinessBackedProvider,
    deferred_field,
)
from .publisher import DashboardPublisher, cadence_for
from .server import SYSTEM_SNAPSHOT_PATH, load_assets, mount_dashboard, serve

__all__ = [
    "DEFERRED",
    "LIVE",
    "OWNED_CHANNELS",
    "REFRESH_BUDGET_MS",
    "SYSTEM_SNAPSHOT_PATH",
    "DashboardMetricsProvider",
    "DashboardPublisher",
    "ReadinessBackedProvider",
    "cadence_for",
    "deferred_field",
    "load_assets",
    "mount_dashboard",
    "serve",
]
