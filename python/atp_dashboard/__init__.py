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

from .account import (
    ACCOUNT_CHANNEL,
    ACCOUNT_FIELD_OWNERS,
    AccountStatusProvider,
)
from .backtests import (
    BacktestHistoryProvider,
    BacktestHistorySource,
    BacktestHistoryUnavailable,
    StoreCliBacktestHistorySource,
)
from .inventory import (
    INVENTORY_CHANNEL,
    RollbackSnapshotInventorySource,
    StrategyInventoryProvider,
    StrategyInventorySource,
)
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
from .reservoir import (
    ALLOWED_EVAL_WINDOWS,
    DEFAULT_EVAL_WINDOW,
    RESERVOIR_CHANNEL,
    RESERVOIR_FIELD_OWNERS,
    ReservoirRankingProvider,
)
from .server import (
    ACCOUNT_SNAPSHOT_PATH,
    BACKTESTS_SNAPSHOT_PATH,
    RESERVOIR_SNAPSHOT_PATH,
    STRATEGIES_SNAPSHOT_PATH,
    SYSTEM_SNAPSHOT_PATH,
    load_assets,
    mount_dashboard,
    mount_default_dashboard,
    serve,
)

__all__ = [
    "ACCOUNT_CHANNEL",
    "ACCOUNT_FIELD_OWNERS",
    "ACCOUNT_SNAPSHOT_PATH",
    "ALLOWED_EVAL_WINDOWS",
    "BACKTESTS_SNAPSHOT_PATH",
    "DEFAULT_EVAL_WINDOW",
    "DEFERRED",
    "INVENTORY_CHANNEL",
    "LIVE",
    "OWNED_CHANNELS",
    "REFRESH_BUDGET_MS",
    "RESERVOIR_CHANNEL",
    "RESERVOIR_FIELD_OWNERS",
    "RESERVOIR_SNAPSHOT_PATH",
    "STRATEGIES_SNAPSHOT_PATH",
    "SYSTEM_SNAPSHOT_PATH",
    "AccountStatusProvider",
    "BacktestHistoryProvider",
    "BacktestHistorySource",
    "BacktestHistoryUnavailable",
    "DashboardMetricsProvider",
    "DashboardPublisher",
    "ReadinessBackedProvider",
    "ReservoirRankingProvider",
    "RollbackSnapshotInventorySource",
    "StoreCliBacktestHistorySource",
    "StrategyInventoryProvider",
    "StrategyInventorySource",
    "cadence_for",
    "deferred_field",
    "load_assets",
    "mount_dashboard",
    "mount_default_dashboard",
    "serve",
]
