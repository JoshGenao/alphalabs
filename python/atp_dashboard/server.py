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

from atp_logging import LogClass
from atp_logging.persistence import JsonlLogStore
from atp_logs_service import LogEventPublisher, wire_logs

# NOTE: keep the next line EXACTLY as written — one line, this order.
# tools/orchestrator_rollback_check.py greps for it as a literal to prove the dashboard
# imports the rollback surface from its owning package rather than reimplementing it, so
# reflowing or reordering it fails that gate. The trigger arm is imported from the SUBMODULE
# below precisely so isort has no second `from atp_orchestration import` to merge into it.
from atp_orchestration import REST_LIFECYCLE_OPERATION, mount_rollback, rollback_is_served
from atp_orchestration.hot_swap_triggers import mount_hot_swap_triggers
from atp_runtime import OperatorInterfaceRuntime

from .account import AccountStatusProvider
from .alerts import CriticalAlertsProvider
from .backtests import BacktestHistoryProvider, StoreCliBacktestHistorySource
from .heartbeat import (
    CliHeartbeatSource,
    HeartbeatFreshnessProvider,
    HeartbeatSource,
    SnapshotHeartbeatSource,
)
from .hotswap import (
    CliHotSwapCooldownSource,
    CliHotSwapDemotionSource,
    CliHotSwapPromotionSource,
    CliHotSwapTriggerSource,
    CompositeHotSwapStatusSource,
    HotSwapStatusProvider,
    HotSwapStatusSource,
)
from .inventory import RollbackSnapshotInventorySource, StrategyInventoryProvider
from .killswitch import DurableKillSwitchStatusSource, KillSwitchStatusProvider
from .logs import LogPaneProvider
from .navigation import PrimaryNavigationProvider
from .provider import DashboardMetricsProvider, ReadinessBackedProvider
from .publisher import DashboardPublisher
from .research import RESEARCH_PREFIX, UPSTREAM_ENV_KNOB, ResearchEnvironmentProvider
from .reservoir import ReservoirRankingProvider

_ASSET_DIR = Path(__file__).resolve().parent / "assets"

#: Route path -> (asset filename, content-type). Absolute paths are used inside
#: index.html so serving at ``/dashboard`` has no base-URL ambiguity.
_ASSET_SPEC: tuple[tuple[str, str, str], ...] = (
    ("/dashboard", "index.html", "text/html; charset=utf-8"),
    ("/dashboard/", "index.html", "text/html; charset=utf-8"),
    ("/dashboard/styles.css", "styles.css", "text/css; charset=utf-8"),
    ("/dashboard/freshness.js", "freshness.js", "application/javascript; charset=utf-8"),
    ("/dashboard/hotswap.js", "hotswap.js", "application/javascript; charset=utf-8"),
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

#: REST path the dashboard SPA polls for the SRS-RES-001 research-embed state
#: (served only when a research provider is mounted). The embed itself is the
#: same-origin ``/research/`` prefix the runtime reverse-proxies to the fixed
#: upstream — reachable from the dashboard without a separate service URL
#: (SYS-34a / IF-13).
RESEARCH_SNAPSHOT_PATH = "/dashboard/api/research"

#: REST path the dashboard SPA polls for the SRS-RES-003 primary-workflow
#: navigation model (served alongside the research embed — navigating to an
#: unmounted embed would be navigation to nothing). Probe-free: it reports
#: whether the same-origin research prefix is REGISTERED on this runtime, while
#: live reachability stays RESEARCH_SNAPSHOT_PATH's probe to answer (SYS-43).
NAVIGATION_SNAPSHOT_PATH = "/dashboard/api/navigation"

#: REST path the dashboard SPA polls for the SRS-MD-003 heartbeat-freshness
#: snapshot (served only when a heartbeat provider is mounted).
HEARTBEAT_SNAPSHOT_PATH = "/dashboard/api/heartbeat"

#: REST path the dashboard SPA polls for the UI-1 critical-alerts pane (served
#: only when an alerts provider is mounted). This is a dashboard-namespaced
#: first-paint poll, NOT the contract route ``GET /api/v1/alerts`` (owner
#: SRS-NOTIF-001), which stays a 501 deferred handler until the notifier lands.
ALERTS_SNAPSHOT_PATH = "/dashboard/api/alerts"

#: REST path the dashboard SPA polls for the UI-4 kill-switch status pane
#: (served only when a kill-switch status provider is mounted). READ-ONLY and
#: dashboard-namespaced: the *activation* control POSTs to the contract route
#: ``POST /api/v1/kill-switch`` (owner SRS-SAFE-001) on this same runtime —
#: there is deliberately no second kill path under ``/dashboard``.
KILL_SWITCH_SNAPSHOT_PATH = "/dashboard/api/kill-switch"

#: REST path the dashboard SPA polls for the UI-5 Hot-Swap status pane (served
#: only when a Hot-Swap status provider is mounted). READ-ONLY and
#: dashboard-namespaced: the *manual promotion* control POSTs to the contract
#: route ``POST /api/v1/hot-swap`` (owner SRS-RESV-003) on this same runtime —
#: there is deliberately no second swap path under ``/dashboard``.
HOT_SWAP_SNAPSHOT_PATH = "/dashboard/api/hot-swap"

#: REST path the dashboard SPA polls for the SRS-LOG-001 log pane (served only
#: when a log provider is mounted). Dashboard-namespaced first-paint poll of BOTH
#: log classes; the contract route ``GET /api/v1/logs`` on this same runtime is
#: the full query surface (same owner, same renderer), and the ``LOGS`` WebSocket
#: channel is the event stream. READ-ONLY: an audit trail has no dashboard-side
#: mutation affordance.
LOGS_SNAPSHOT_PATH = "/dashboard/api/logs"


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
    research: ResearchEnvironmentProvider | None = None,
    heartbeat: HeartbeatFreshnessProvider | None = None,
    alerts: CriticalAlertsProvider | None = None,
    kill_switch: KillSwitchStatusProvider | None = None,
    hot_swap: HotSwapStatusProvider | None = None,
    logs: LogPaneProvider | None = None,
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

    ``research`` (optional — the SRS-RES-001 research-embed provider) adds the
    ``GET /dashboard/api/research`` poll route and, when the provider carries a
    configured upstream, registers the same-origin ``/research/`` reverse-proxy
    on the runtime — the embedded Jupyter environment is then reachable from
    the dashboard without a separate service URL (SYS-34a / IF-13). It is
    REST-served (no WS channel) and adds no publisher work; without a
    configured upstream the panel renders the honest not-configured state and
    NO proxy route exists. It additionally adds the SRS-RES-003 (SyRS SYS-43)
    ``GET /dashboard/api/navigation`` route behind the topbar's primary
    Research entry — the operator's direct navigation to the embed from the
    primary workflow, carrying a same-origin path only (never a service URL)
    and probe-free, so the live-reachability answer stays the research
    snapshot's alone. A bare SRS-UI-001 mount serves no navigation route (there
    is no embed to navigate to) and the SPA renders the entry's not-mounted
    state.

    ``heartbeat`` (optional — the SRS-MD-003 heartbeat-freshness provider) adds
    the ``GET /dashboard/api/heartbeat`` poll route and moves the ``HEARTBEAT``
    channel onto its own isolated publisher ticker feeding REAL per-feed
    staleness rows (the provider shells the ``md003_heartbeat_cli`` monitor
    each second); without it the main ticker keeps publishing the metrics
    provider's honest deferred HEARTBEAT cells.
    ``alerts`` (optional — the UI-1 critical-alerts provider) adds the
    ``GET /dashboard/api/alerts`` poll route the alerts pane reads. It is
    REST-served (the event-driven ``ALERTS`` WS channel stays unpublished until
    its SRS-NOTIF-001 producer lands — deferred non-events would drift that
    contract), so it adds no publisher channel; without it the pane renders its
    explicit "not mounted" state. The feed is an honest deferred cell — the pane
    never renders "0 active alerts" while detection is unwired.

    ``kill_switch`` (optional — the UI-4 kill-switch status provider) adds the
    ``GET /dashboard/api/kill-switch`` poll route the Liquidate-Sequence panel
    reads. It is REST-served (there is no kill-switch WS channel to publish on),
    so it adds no publisher channel, and it is strictly a READ: the panel's
    activation control POSTs to the contract route ``POST /api/v1/kill-switch``
    (see app.js), whose live handler is SRS-SAFE-001's. Every leg is an honest
    deferred cell until an activation record exists — the pane never renders an
    all-clear for a sequence it cannot observe.

    ``hot_swap`` (optional — the UI-5 Hot-Swap status provider) adds the
    ``GET /dashboard/api/hot-swap`` poll route the Changeover Console panel
    reads. It is REST-served (there is no Hot-Swap WS channel to publish on), so
    it adds no publisher channel, and it is strictly a READ: the panel's manual
    promotion control POSTs to the contract route ``POST /api/v1/hot-swap`` (see
    app.js), whose live handler is SRS-RESV-003's. Every live fact (current live
    strategy, demotion-pending, cool-down expiry, per-trigger enabled-state) is
    an honest deferred cell until the SRS-RESV-002..006 producers land — the
    pane never fabricates a swap state, and with no promotion candidate the
    promote control is inert.

    ``logs`` (optional — the SRS-LOG-001 log-pane provider) adds the
    ``GET /dashboard/api/logs`` poll route the log pane reads: BOTH log classes,
    each read from its own store and returned in its own cell. It is REST-served
    here — the event-driven ``LOGS`` WS channel is claimed by
    ``atp_logs_service.wire_logs``'s publisher, not by this mount, so mounting a
    pane never overstates that a stream is running. Strictly a READ: an audit
    trail has no dashboard-side mutation affordance. Without it the pane renders
    its explicit "not mounted" state — never an empty log table.
    """

    runtime.register_asset_routes(load_assets())
    runtime.register_meta_route(SYSTEM_SNAPSHOT_PATH, provider.system_snapshot)
    if inventory is not None:
        # SRS-ORCH-005 capability probe. A retained previous version in the data
        # says nothing about whether THIS runtime serves rollback: an inventory
        # can be composed here on a runtime with no rollback handler. Ask the
        # OWNING package (rollback_is_served) rather than testing route
        # registration ourselves — the lifecycle route is SHARED with
        # SRS-ORCH-004's start/stop/restart, so registration on it proves
        # nothing about the rollback ACTION. Evaluated per request, so
        # composition ORDER cannot fake it.
        # Duck-typed: the inventory parameter is a structural provider (test
        # doubles supply their own payloads), so bind only where the hook
        # exists. A provider that cannot be bound simply keeps whatever
        # capability it reports — and the real provider's default is FALSE.
        if getattr(inventory, "bind_rollback_probe", None) is not None:
            inventory.bind_rollback_probe(lambda: rollback_is_served(runtime))
        runtime.register_meta_route(STRATEGIES_SNAPSHOT_PATH, inventory.inventory_snapshot)
    if backtests is not None:
        runtime.register_meta_route(BACKTESTS_SNAPSHOT_PATH, backtests.history_snapshot)
    if account is not None:
        runtime.register_meta_route(ACCOUNT_SNAPSHOT_PATH, account.account_snapshot)
    if reservoir is not None:
        runtime.register_meta_route(RESERVOIR_SNAPSHOT_PATH, reservoir.reservoir_snapshot)
    if research is not None:
        runtime.register_meta_route(RESEARCH_SNAPSHOT_PATH, research.research_snapshot)
        # SRS-RES-003: the primary-workflow navigation model rides with the
        # embed it navigates to. Registered BEFORE the proxy so the proxy's
        # meta-path shadow guard sees every dashboard route already claimed.
        runtime.register_meta_route(
            NAVIGATION_SNAPSHOT_PATH,
            PrimaryNavigationProvider.for_research(
                research, state_route=RESEARCH_SNAPSHOT_PATH
            ).navigation_snapshot,
        )
        if research.upstream is not None:
            runtime.register_proxy_route(RESEARCH_PREFIX, research.upstream)
    if heartbeat is not None:
        runtime.register_meta_route(HEARTBEAT_SNAPSHOT_PATH, heartbeat.heartbeat_snapshot)
    if alerts is not None:
        runtime.register_meta_route(ALERTS_SNAPSHOT_PATH, alerts.alerts_snapshot)
    if kill_switch is not None:
        runtime.register_meta_route(KILL_SWITCH_SNAPSHOT_PATH, kill_switch.kill_switch_snapshot)
    if hot_swap is not None:
        runtime.register_meta_route(HOT_SWAP_SNAPSHOT_PATH, hot_swap.hot_swap_snapshot)
    if logs is not None:
        runtime.register_meta_route(LOGS_SNAPSHOT_PATH, logs.logs_snapshot)
    return DashboardPublisher(
        runtime,
        provider,
        inventory=inventory,
        account=account,
        reservoir=reservoir,
        heartbeat=heartbeat,
    )


def mount_default_dashboard(
    runtime: OperatorInterfaceRuntime,
    env: Mapping[str, str],
    *,
    hot_swap_source: HotSwapStatusSource | None = None,
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

    # Drive the store location from the passed env AND hand that same mapping to the
    # source as the CLI subprocess's entire environment, so the composition is
    # deterministic w.r.t. `env` — a mapping that omits ATP_BACKTEST_RESULTS_DIR
    # cannot silently read an ambient store; the source fails closed to ok:false.
    results_dir = env.get("ATP_BACKTEST_RESULTS_DIR") or None
    backtests = BacktestHistoryProvider(
        StoreCliBacktestHistorySource(results_dir=results_dir, env=env)
    )
    # The SRS-MD-003 heartbeat-freshness provider is composed only when a
    # freshness producer is configured, and exactly ONE of the two may be:
    #
    #   ATP_MD003_SNAPSHOT     — the LIVE producer: the durable snapshot
    #       ``md003_live_feed_cli`` rewrites from real IB tick deliveries and
    #       genuine gateway round trips. This is the production wiring.
    #
    #       WHAT IT ATTESTS, PRECISELY: the daemon opens its OWN reqMktData
    #       lines from the operator's --symbol list, so a FRESH verdict here
    #       means "those lines are delivering", NOT "every market-data path a
    #       strategy consumes is healthy". Until SRS-MD-001's Market Data
    #       Subscription Manager owns these subscriptions, freshness can read
    #       healthy while a different consolidated path is wedged, and these
    #       lines are spent outside that manager's dedup/line-limit accounting.
    #       Deferred with its consequences and its owner in
    #       heartbeat_freshness_contract.live_feed.subscription_ownership.
    #       The broker line has the same shape of limit: the daemon's transport
    #       is IbAccountKind::Paper (the live account is gated on SRS-EXE-001
    #       and the transport refuses it), so the `ib_gateway` cell reports the
    #       PAPER gateway endpoint — not whichever gateway real execution uses.
    #
    #       That is why this is opt-in and unset by default: mounting it is an
    #       operator's explicit choice, made with the scope limits in view, and
    #       why SRS-MD-003 stays passes:false until MD-001 and EXE-001 close
    #       them.
    #   ATP_MD003_OBSERVATIONS — the FIXTURE producer: the directive script
    #       ``md003_heartbeat_cli`` replays. Demonstration and tests only.
    #
    # Setting BOTH is a configuration error, not a precedence puzzle: two
    # producers claiming one channel means the operator does not know which
    # one the health status reflects, and silently preferring either would let
    # a fixture's verdicts be read as live evidence (or bury a live feed behind
    # a stale script). Fail closed at boot and make them choose.
    #
    # Unset, the HEARTBEAT channel keeps its honest deferred cells. When
    # monitoring IS mounted, ATP_MD003_LOG_DIR is REQUIRED for either producer:
    # SRS-MD-003 makes the durable HEARTBEAT_STALE/RECOVERED audit trail a
    # first-class acceptance leg, so a composition that monitors-but-cannot-log
    # is a configuration error that must fail closed at boot, never a silent
    # no-audit mode (the full log-runtime wiring remains SRS-LOG-001's).
    heartbeat: HeartbeatFreshnessProvider | None = None
    observations = env.get("ATP_MD003_OBSERVATIONS") or None
    snapshot = env.get("ATP_MD003_SNAPSHOT") or None
    if observations is not None and snapshot is not None:
        raise ValueError(
            "ATP_MD003_SNAPSHOT and ATP_MD003_OBSERVATIONS are both set: the "
            "heartbeat channel has exactly one producer, and mounting the live "
            "feed snapshot alongside the fixture script leaves it ambiguous "
            "which one system health reflects (SRS-MD-003). Set exactly one."
        )
    source: HeartbeatSource | None = None
    if snapshot is not None:
        source = SnapshotHeartbeatSource(snapshot)
    elif observations is not None:
        source = CliHeartbeatSource(observations)
    if source is not None:
        log_dir = env.get("ATP_MD003_LOG_DIR") or None
        if log_dir is None:
            raise ValueError(
                "heartbeat monitoring is configured but ATP_MD003_LOG_DIR is not: "
                "heartbeat monitoring requires the durable transition-record "
                "sink (SRS-MD-003 'logged' acceptance leg)"
            )
        heartbeat = HeartbeatFreshnessProvider(
            source,
            log_store=JsonlLogStore(Path(log_dir) / "system.jsonl", log_class=LogClass.SYSTEM),
        )
    provider = ReadinessBackedProvider(env, heartbeat=heartbeat)
    # The SRS-UI-002 strategy inventory (UI-1's "live strategy status" leg) is
    # composed whenever the ORCH-005 deployment snapshot is configured:
    # ATP_DEPLOYMENT_STATE names the rollback state file orch005_rollback_cli
    # maintains, and the CLI stays the single snapshot-format owner. Unset, the
    # inventory route is not registered and the panel renders its explicit
    # "not mounted" state — never a fabricated inventory.
    inventory: StrategyInventoryProvider | None = None
    deployment_state = env.get("ATP_DEPLOYMENT_STATE") or None
    if deployment_state is not None:
        inventory = StrategyInventoryProvider(
            RollbackSnapshotInventorySource(state_path=deployment_state)
        )
        # SRS-ORCH-005 'via the dashboard' leg: the SAME snapshot that feeds the
        # inventory READ also backs the rollback WRITE, so the per-row ROLLBACK
        # control POSTs same-origin to a real handler instead of the bare
        # runtime's honest 501. Composing here (rather than leaving it to
        # SRS-API-001) is what makes rollback genuinely "available through the
        # dashboard" in the shipped `python -m atp_dashboard` entrypoint — one
        # env knob, one snapshot file, one runtime. The handler keeps every
        # guard it enforces on the CLI/REST surfaces: an unconfirmed rollback is
        # refused 428 before it can reach the binary, so mounting the control
        # never weakens the NFR-S2 confirmation parity.
        #
        # Unset ATP_DEPLOYMENT_STATE composes NEITHER arm: no inventory route and
        # no rollback handler, so the operation keeps its honest 501 rather than
        # a control that posts into a snapshot that does not exist.
        #
        # DEFENSIVE: the lifecycle route is SHARED and the registry rejects a
        # duplicate binding, so a composer that already registered a lifecycle
        # handler (SRS-ORCH-004's start/stop/restart, when it lands) would make
        # this raise and take the whole dashboard down at startup. Mounting the
        # dashboard must never do that. Skip instead — the capability probe then
        # reports the truth (that handler does not serve rollback), so the
        # control renders inert and no operator is offered an action the runtime
        # will refuse. Co-registering BOTH on the shared route needs a
        # per-action multiplexer on the frozen SRS-API-001 surface; that belongs
        # to SRS-ORCH-004 / SRS-API-001 and is recorded in
        # rollback_contract.deferred[].
        if not runtime.registry.is_registered(REST_LIFECYCLE_OPERATION):
            mount_rollback(runtime, state_path=deployment_state)
    # The SRS-UI-003 account + Reservoir + UI-1 alerts providers are pure builders
    # (no env, no subprocess), so they are ALWAYS composed here — the production
    # entrypoint actually serves /dashboard/api/{account,reservoir,alerts} and
    # publishes the ACCOUNT_STATUS / RESERVOIR_RANKING channels, rendering honest
    # deferred cells until their live producers (SRS-EXE-006 / SRS-RESV-002 /
    # SRS-NOTIF-001) land.
    # The SRS-RES-001 research provider is ALWAYS composed: the production
    # entrypoint serves /dashboard/api/research, rendering the honest
    # not-configured state until the operator sets ATP_RESEARCH_UPSTREAM —
    # only a CONFIGURED upstream registers the same-origin /research/ proxy.
    #
    # The UI-4 kill-switch status provider is ALWAYS composed too, but its
    # SOURCE is opt-in: ATP_KILL_SWITCH_STATE names the directory
    # atp_safety.persist_last_activation writes the last-activation record into,
    # and ATP_KILL_SWITCH_LOG_DIR the SRS-LOG-001 system log holding the SYS-44b
    # LIQUIDATION_TIMEOUT record. Unset, the provider carries NO source and the
    # pane renders every leg UNKNOWN with `activated: null` — an unconfigured
    # dashboard must never state that the kill switch has not been activated,
    # because it cannot know (SYS-44a status feedback fails closed).
    kill_switch_state = env.get("ATP_KILL_SWITCH_STATE") or None
    kill_switch = KillSwitchStatusProvider(
        DurableKillSwitchStatusSource(
            state_dir=kill_switch_state,
            log_dir=env.get("ATP_KILL_SWITCH_LOG_DIR") or None,
        )
        if kill_switch_state is not None
        else None
    )
    # The UI-5 Hot-Swap status provider is ALWAYS composed (the production
    # entrypoint serves /dashboard/api/hot-swap), but its SOURCE is opt-in:
    # ATP_HOT_SWAP_TRIGGER_STATE names the durable SRS-RESV-003 trigger
    # configuration that resv003_hot_swap_trigger_cli maintains, and the CLI
    # stays the single format owner. Unset, the provider carries NO source and
    # every live cell renders its honest deferred placeholder — an unconfigured
    # dashboard must never fabricate a swap state.
    #
    # The knob is named for the TRIGGER leg specifically because that is the
    # only one this source can answer. The pane's other cells (current live
    # strategy, demotion-pending, cool-down, promotion candidate) stay deferred
    # to SRS-RESV-002/004/005/006, which persist no queryable fact yet; the
    # source returns None for them rather than inventing one, and the three
    # protocol legs fail independently so an unreadable trigger configuration
    # cannot blank a live state that a later producer does resolve.
    # The REST arm (mount_hot_swap_triggers) builds this source and hands it here, so the
    # pane and the routes read ONE configuration through one client. When no source is
    # supplied — a bare dashboard, or a caller that mounted no trigger surface — the knob
    # still composes a read-only one, so the pane resolves its chips without the REST
    # handlers being registered.
    if hot_swap_source is None:
        hot_swap_trigger_state = env.get("ATP_HOT_SWAP_TRIGGER_STATE") or None
        hot_swap_source = (
            CliHotSwapTriggerSource(hot_swap_trigger_state)
            if hot_swap_trigger_state is not None
            else None
        )
    # SRS-RESV-004: ATP_HOT_SWAP_DEMOTION_STATE names the durable demotion-pending
    # lockout that resv004_hot_swap_demotion_cli maintains and the demotion gate
    # engages. Setting it resolves the pane's demotion_pending / demotion_detail
    # cells, which every prior session rendered as deferred:SRS-RESV-004.
    #
    # Composed as a SEPARATE leg rather than folded into the trigger source: they
    # answer different questions from different files, and a trigger configuration
    # that cannot be read must not blank a demotion state that can. Unset, the leg
    # is absent and those cells keep their honest deferred placeholder — an
    # unconfigured dashboard must never report "no demotion is pending", because it
    # does not know.
    # SRS-RESV-005: ATP_HOT_SWAP_DESIGNATION_STATE names the durable live-designation
    # snapshot resv005_hot_swap_promote_cli maintains — the record the promotion gate
    # itself writes. Setting it resolves the pane's current_live_strategy_id cell,
    # which every prior session rendered as deferred:SRS-RESV-005.
    #
    # SRS-RESV-006: ATP_HOT_SWAP_COOLDOWN_STATE names the durable SyRS SYS-49e window
    # that resv006_hot_swap_cooldown_cli maintains, the trigger gate classifies, and
    # execute_hot_swap both consults and writes. Setting it resolves the pane's
    # cooldown.in_effect / started_at / expires_at cells, which have rendered
    # deferred:SRS-RESV-006 since UI-5 shipped.
    #
    # FOUR separate legs, because each answers a different question from a different
    # file read by a different binary — so a lockout that is merely ABSENT does not
    # stop the designation resolving, and any one composes without the others.
    #
    # That does NOT buy failure independence, and saying so would be the drift this
    # pane exists to avoid: all three live-state sub-legs feed ONE protocol method
    # (`live_state`), so a leg that RAISES defers the whole live-state group, including
    # a live strategy or a cool-down window that was perfectly readable. That is the
    # fail-closed direction — the promote control goes inert rather than acting on part
    # of a safety picture — and tests/boundary/test_dashboard_designation_wiring.py
    # pins it as the real behaviour. Splitting it finer is UI-5's protocol to change,
    # not this call site's.
    #
    # Unset, a leg is absent and its cells keep their deferred placeholder — a
    # dashboard that cannot read the designation must never report which strategy is
    # live, and one that cannot read the window must never report "no cool-down is in
    # effect". The pane holds its promote control inert on exactly those unknowns.
    hot_swap_demotion_state = env.get("ATP_HOT_SWAP_DEMOTION_STATE") or None
    hot_swap_designation_state = env.get("ATP_HOT_SWAP_DESIGNATION_STATE") or None
    hot_swap_cooldown_state = env.get("ATP_HOT_SWAP_COOLDOWN_STATE") or None
    if (
        hot_swap_demotion_state is not None
        or hot_swap_designation_state is not None
        or hot_swap_cooldown_state is not None
    ):
        hot_swap_source = CompositeHotSwapStatusSource(
            triggers=hot_swap_source,
            demotion=(
                CliHotSwapDemotionSource(hot_swap_demotion_state)
                if hot_swap_demotion_state is not None
                else None
            ),
            promotion=(
                CliHotSwapPromotionSource(hot_swap_designation_state)
                if hot_swap_designation_state is not None
                else None
            ),
            cooldown=(
                CliHotSwapCooldownSource(hot_swap_cooldown_state)
                if hot_swap_cooldown_state is not None
                else None
            ),
        )
    # The SRS-LOG-001 log pane is opt-in on ATP_LOG_DIR, the directory holding
    # the separated `system.jsonl` / `strategy.jsonl` stores that
    # build_separated_log_dispatcher writes. Unset, NO route is registered and
    # the pane renders its explicit not-mounted state — a dashboard that cannot
    # read an audit trail must say so, never show an empty log table (which
    # reads as "nothing has happened").
    return mount_dashboard(
        runtime,
        provider,
        inventory=inventory,
        backtests=backtests,
        account=AccountStatusProvider(),
        reservoir=ReservoirRankingProvider(),
        research=ResearchEnvironmentProvider(env.get(UPSTREAM_ENV_KNOB) or None),
        heartbeat=heartbeat,
        alerts=CriticalAlertsProvider(),
        kill_switch=kill_switch,
        hot_swap=HotSwapStatusProvider(hot_swap_source),
        logs=log_pane_provider(env),
    )


#: Env knob naming the directory that holds the SRS-LOG-001 separated stores.
LOG_DIR_ENV_KNOB = "ATP_LOG_DIR"

#: Store filenames inside that directory — the defaults
#: ``atp_logging.persistence.build_separated_log_dispatcher`` writes.
SYSTEM_STORE_FILENAME = "system.jsonl"
STRATEGY_STORE_FILENAME = "strategy.jsonl"


def log_pane_provider(env: Mapping[str, str]) -> LogPaneProvider | None:
    """Build the SRS-LOG-001 pane provider when ``ATP_LOG_DIR`` is configured."""

    log_dir = env.get(LOG_DIR_ENV_KNOB) or None
    if log_dir is None:
        return None
    root = Path(log_dir)
    return LogPaneProvider(
        system_store_path=root / SYSTEM_STORE_FILENAME,
        strategy_store_path=root / STRATEGY_STORE_FILENAME,
    )


def _mount_logs_arm(
    runtime: OperatorInterfaceRuntime, env: Mapping[str, str]
) -> LogEventPublisher | None:
    """Register the SRS-LOG-001 REST/CLI/WS surfaces when ``ATP_LOG_DIR`` is set.

    Same opt-in shape as the Hot-Swap trigger arm: composing it here (rather
    than leaving it to SRS-API-001) is what makes ``GET /api/v1/logs``, the
    ``admin logs`` CLI, and the ``LOGS`` WebSocket channel real in the shipped
    ``python -m atp_dashboard`` entrypoint instead of answering the honest 501.
    Unset, every LOGS operation keeps that 501 and the channel stays unclaimed —
    a runtime with no configured trail must not report the workflow as served.

    The returned publisher is un-started: :func:`serve` starts it and stops it
    with the dashboard's own, so no ticker outlives a failed startup.
    """

    log_dir = env.get(LOG_DIR_ENV_KNOB) or None
    if log_dir is None:
        return None
    root = Path(log_dir)
    return wire_logs(
        runtime,
        system_store_path=root / SYSTEM_STORE_FILENAME,
        strategy_store_path=root / STRATEGY_STORE_FILENAME,
    )


def _mount_hot_swap_trigger_arm(
    runtime: OperatorInterfaceRuntime, env: Mapping[str, str]
) -> CliHotSwapTriggerSource | None:
    """Register the SRS-RESV-003 trigger REST routes when the operator has configured them.

    Opt-in on ``ATP_HOT_SWAP_TRIGGER_STATE``; unset, the routes keep the structured 501 the
    frozen contract gives every unbound operation and the pane renders its deferred cells.

    ``ATP_HOT_SWAP_TRIGGER_LOG`` is REQUIRED alongside it, and a missing one is a boot
    failure rather than a degraded mode: "all swap triggers are logged" is a first-class
    acceptance clause, so a surface that could fire a trigger it cannot record must not come
    up at all. Same rule the SRS-MD-003 heartbeat composition applies to its audit sink.

    ``ATP_HOT_SWAP_COOLDOWN_STATE`` is REQUIRED for the same reason (SRS-RESV-006 /
    SyRS SYS-49e). The binary already fails closed on a missing window — it reports UNKNOWN
    and refuses — so an unset knob would leave the REST arm mounted but structurally unable
    to fire ANY manual trigger, which reads to an operator as a broken surface rather than
    as a missing setting. Failing at boot names the actual cause once, instead of once per
    request.
    """

    state_path = env.get("ATP_HOT_SWAP_TRIGGER_STATE") or None
    if state_path is None:
        return None
    log_path = env.get("ATP_HOT_SWAP_TRIGGER_LOG") or None
    if log_path is None:
        raise ValueError(
            "ATP_HOT_SWAP_TRIGGER_STATE is set but ATP_HOT_SWAP_TRIGGER_LOG is not: the "
            "Hot-Swap trigger surface makes the durable audit record load-bearing "
            "(SRS-RESV-003 'all swap triggers are logged'), so it must not start without "
            "somewhere to write one"
        )
    cooldown_state_path = env.get("ATP_HOT_SWAP_COOLDOWN_STATE") or None
    if cooldown_state_path is None:
        raise ValueError(
            "ATP_HOT_SWAP_TRIGGER_STATE is set but ATP_HOT_SWAP_COOLDOWN_STATE is not: the "
            "Hot-Swap trigger surface enforces the SyRS SYS-49e cool-down "
            "(SRS-RESV-006), and with no window to read every manual trigger would be "
            "refused as UNKNOWN — a surface that can never fire must not come up claiming "
            "it can"
        )
    return mount_hot_swap_triggers(
        runtime,
        state_path=state_path,
        log_path=log_path,
        cooldown_state_path=cooldown_state_path,
    )


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:
    """Run the dashboard until interrupted (blocking; SIGINT/SIGTERM shut down)."""

    runtime = OperatorInterfaceRuntime()
    env = dict(os.environ)
    # THE process entrypoint is where the operator surfaces are composed together, so it is
    # the one place entitled to know both packages — and the only place that can make the
    # SRS-RESV-003 REST arm actually ship. Registering the handlers only in tests would mean
    # the documented routes answered 501 in production while the e2e proved otherwise.
    publisher = mount_default_dashboard(
        runtime, env, hot_swap_source=_mount_hot_swap_trigger_arm(runtime, env)
    )
    logs_publisher = _mount_logs_arm(runtime, env)

    # Startup is all-or-nothing. The publishers run on their own threads, so a
    # failure to BIND (port in use, a refused host) after they are running would
    # leave tickers polling the audit stores and publishing into a runtime that
    # never came up — invisible work behind a process that looks dead. Anything
    # already started is stopped before the failure is re-raised.
    started: list[object] = []
    try:
        publisher.start()
        started.append(publisher)
        if logs_publisher is not None:
            logs_publisher.start()
            started.append(logs_publisher)
        bound_host, bound_port = runtime.start(host=host, port=port)
    except BaseException:
        for running in reversed(started):
            running.stop()  # type: ignore[attr-defined]
        raise
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
        if logs_publisher is not None:
            logs_publisher.stop()
        runtime.stop()
