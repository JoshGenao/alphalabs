"""Mount + serve the SRS-UI-001 dashboard on an operator-interface runtime.

:func:`mount_dashboard` wires a dashboard onto an existing
:class:`atp_runtime.OperatorInterfaceRuntime`: it materialises the static assets
once into an exact ``path -> (content_type, bytes)`` map (no per-request disk I/O,
no request-derived path → no traversal surface), registers them plus the JSON
system-snapshot endpoint through the runtime's generic seams, and returns an
un-started :class:`DashboardPublisher`.

:func:`serve` is the ``python -m atp_dashboard`` process entrypoint: it builds a
runtime, mounts the dashboard, starts publishing, binds the loopback server, and
**blocks** until interrupted (``start()`` runs the server on a daemon thread, so
the process must not return), tearing both down cleanly on SIGINT/SIGTERM.

SRS trace
---------
``SRS-UI-001`` (dashboard), ``SRS-SEC-002`` (loopback/RFC-1918 bind via
``runtime.start``), ``NFR-P2`` (≤5 s refresh via the publisher).
"""

from __future__ import annotations

import os
import signal
import threading
from collections.abc import Mapping
from pathlib import Path
from types import FrameType

from atp_runtime import OperatorInterfaceRuntime

from .account import AccountStatusProvider
from .backtests import BacktestHistoryProvider, StoreCliBacktestHistorySource
from .inventory import StrategyInventoryProvider
from .provider import DashboardMetricsProvider, ReadinessBackedProvider
from .publisher import DashboardPublisher
from .reservoir import ReservoirRankingProvider

_ASSET_DIR = Path(__file__).resolve().parent / "assets"

#: Route path -> (asset filename, content-type). Absolute paths are used inside
#: index.html so serving at ``/dashboard`` has no base-URL ambiguity.
_ASSET_SPEC: tuple[tuple[str, str, str], ...] = (
    ("/dashboard", "index.html", "text/html; charset=utf-8"),
    ("/dashboard/", "index.html", "text/html; charset=utf-8"),
    ("/dashboard/styles.css", "styles.css", "text/css; charset=utf-8"),
    ("/dashboard/freshness.js", "freshness.js", "application/javascript; charset=utf-8"),
    ("/dashboard/app.js", "app.js", "application/javascript; charset=utf-8"),
)

#: REST path the dashboard SPA polls for the health + latency snapshot.
SYSTEM_SNAPSHOT_PATH = "/dashboard/api/system"

#: REST path the dashboard SPA polls for the SRS-UI-002 strategy inventory
#: (served only when an inventory provider is mounted).
STRATEGIES_SNAPSHOT_PATH = "/dashboard/api/strategies"

#: REST path the dashboard SPA polls for the SRS-UI-004 backtest result history
#: (served only when a backtest-history provider is mounted).
BACKTESTS_SNAPSHOT_PATH = "/dashboard/api/backtests"

#: REST path the dashboard SPA polls for the SRS-UI-003 account-level IB status
#: (served only when an account provider is mounted).
ACCOUNT_SNAPSHOT_PATH = "/dashboard/api/account"

#: REST path the dashboard SPA polls for the SRS-UI-003 Reservoir ranking overview
#: (served only when a Reservoir provider is mounted). This is a dashboard-namespaced
#: first-paint poll, NOT the SYS-48 contract route ``GET /api/v1/reservoir/ranking``
#: (owner SRS-RESV-002), which stays a 501 deferred handler until the ranking engine lands.
RESERVOIR_SNAPSHOT_PATH = "/dashboard/api/reservoir"


def load_assets() -> dict[str, tuple[str, bytes]]:
    """Read the dashboard's static assets once into an immutable route map."""

    routes: dict[str, tuple[str, bytes]] = {}
    for route_path, filename, content_type in _ASSET_SPEC:
        body = (_ASSET_DIR / filename).read_bytes()
        routes[route_path] = (content_type, body)
    return routes


def mount_dashboard(
    runtime: OperatorInterfaceRuntime,
    provider: DashboardMetricsProvider,
    *,
    inventory: StrategyInventoryProvider | None = None,
    backtests: BacktestHistoryProvider | None = None,
    account: AccountStatusProvider | None = None,
    reservoir: ReservoirRankingProvider | None = None,
) -> DashboardPublisher:
    """Register the dashboard's routes on ``runtime`` and return its publisher.

    Call before :meth:`OperatorInterfaceRuntime.start`. Returns an un-started
    :class:`DashboardPublisher`; the caller starts it (and the runtime).

    ``inventory`` (optional — the SRS-UI-002 strategy-inventory provider) adds
    the ``GET /dashboard/api/strategies`` poll route and puts the
    ``STRATEGY_STATE`` channel on the publisher's schedule; without it the
    dashboard is exactly the SRS-UI-001 surface (the inventory panel renders
    its explicit unavailable state).

    ``backtests`` (optional — the SRS-UI-004 / UI-3 backtest-history provider)
    adds the ``GET /dashboard/api/backtests`` poll route the backtest panel's
    history + drill-down reads. It is REST-served (there is no BACKTEST WS
    channel), so it adds no publisher channel; without it the backtest panel
    renders its explicit "not mounted" state. The panel's *launch* affordance is
    independent of this provider — it POSTs to the contract route
    ``POST /api/v1/backtests`` (see app.js), whose live handler is SRS-API-001's.

    ``account`` / ``reservoir`` (optional — the SRS-UI-003 account-status and
    Reservoir-ranking providers) each add a ``GET /dashboard/api/{account,reservoir}``
    poll route and put the ``ACCOUNT_STATUS`` / ``RESERVOIR_RANKING`` channel on
    the publisher's schedule; without them a bare SRS-UI-001 mount claims neither
    channel and serves neither route (the panels render their explicit unavailable
    state). Their values are honest deferred cells until SRS-EXE-006 (live IB) and
    SRS-RESV-002 (ranking engine) land — the panels never fabricate a number.
    """

    runtime.register_asset_routes(load_assets())
    runtime.register_meta_route(SYSTEM_SNAPSHOT_PATH, provider.system_snapshot)
    if inventory is not None:
        runtime.register_meta_route(STRATEGIES_SNAPSHOT_PATH, inventory.inventory_snapshot)
    if backtests is not None:
        runtime.register_meta_route(BACKTESTS_SNAPSHOT_PATH, backtests.history_snapshot)
    if account is not None:
        runtime.register_meta_route(ACCOUNT_SNAPSHOT_PATH, account.account_snapshot)
    if reservoir is not None:
        runtime.register_meta_route(RESERVOIR_SNAPSHOT_PATH, reservoir.reservoir_snapshot)
    return DashboardPublisher(
        runtime, provider, inventory=inventory, account=account, reservoir=reservoir
    )


def mount_default_dashboard(
    runtime: OperatorInterfaceRuntime, env: Mapping[str, str]
) -> DashboardPublisher:
    """The default composition used by ``python -m atp_dashboard``: the SRS-UI-001
    metrics surface plus the SRS-UI-004 backtest history.

    The backtest-history provider is ALWAYS composed here (so the production
    entrypoint actually serves ``/dashboard/api/backtests``, not just the tests);
    it reads the configured ``ATP_BACKTEST_RESULTS_DIR`` store via the SRS-BT-009
    CLI and reports an explicit unavailable history when that directory is unset or
    unreadable — never a 404 "not mounted" nor a fabricated feed. Extracted from
    :func:`serve` as a testable seam.
    """

    provider = ReadinessBackedProvider(env)
    # Drive the store location from the passed env AND hand that same mapping to the
    # source as the CLI subprocess's entire environment, so the composition is
    # deterministic w.r.t. `env` — a mapping that omits ATP_BACKTEST_RESULTS_DIR
    # cannot silently read an ambient store; the source fails closed to ok:false.
    results_dir = env.get("ATP_BACKTEST_RESULTS_DIR") or None
    backtests = BacktestHistoryProvider(
        StoreCliBacktestHistorySource(results_dir=results_dir, env=env)
    )
    # The SRS-UI-003 account + Reservoir providers are pure builders (no env, no
    # subprocess), so they are ALWAYS composed here — the production entrypoint
    # actually serves /dashboard/api/account and /dashboard/api/reservoir and
    # publishes the ACCOUNT_STATUS / RESERVOIR_RANKING channels, rendering honest
    # deferred cells until their live producers (SRS-EXE-006 / SRS-RESV-002) land.
    return mount_dashboard(
        runtime,
        provider,
        backtests=backtests,
        account=AccountStatusProvider(),
        reservoir=ReservoirRankingProvider(),
    )


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:
    """Run the dashboard until interrupted (blocking; SIGINT/SIGTERM shut down)."""

    runtime = OperatorInterfaceRuntime()
    publisher = mount_default_dashboard(runtime, dict(os.environ))
    publisher.start()
    bound_host, bound_port = runtime.start(host=host, port=port)
    print(  # noqa: T201 - operator-facing startup line
        f"atp-dashboard serving on http://{bound_host}:{bound_port}/dashboard "
        f"(ws://{bound_host}:{bound_port}/ws/v1)"
    )

    stopped = threading.Event()

    def _shutdown(_signum: int, _frame: FrameType | None) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    try:
        stopped.wait()
    finally:
        publisher.stop()
        runtime.stop()
