"""Assemble the operator-interface runtime: registry + handlers + servers.

:class:`OperatorInterfaceRuntime` is the one object a future ``main`` (or the
dashboard process) constructs. It:

* builds a :class:`~atp_runtime.registry.HandlerRegistry` and registers the
  handful of runtime-owned handlers (status / version / config) — every other
  declared operation resolves to a structured deferred envelope;
* validates the in-code owner map against the contract's ``deferred`` list
  (fail-fast);
* exposes :meth:`cli_dispatcher` for ``python -m atp_runtime``-style dispatch,
  :meth:`dispatch_rest` for in-process REST testing, :meth:`publish` for
  WebSocket fan-out, and :meth:`serve_forever` / :meth:`start` / :meth:`stop`
  for a live loopback HTTP+WS server.

The runtime imports only peer interface packages (``atp_api`` / ``atp_cli`` /
``atp_ws`` / ``atp_config``); it never imports a core trading engine or a vendor
SDK, preserving the one-way dependency direction (it is the top layer).

SRS trace
---------
``SRS-API-001`` (the operator-interface-runtime named in the contract's
``deferred`` list), ``SRS-SEC-002`` (loopback bind / auth model),
``SRS-SEC-001`` (config redaction).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from pathlib import Path

from atp_api import ROUTES, Capability, build_openapi, routes_by_capability
from atp_cli import COMMANDS, Group, commands_by_group
from atp_config import REQUIRED_KEYS
from atp_ws import EVENT_CHANNELS, WS_PATH

from .cli_dispatch import CliDispatcher
from .contract import (
    SHARED_GROUP_WORKFLOW_COMMANDS,
    cli_owner,
    load_contract_block,
    rest_owner,
    validate_owners,
    ws_owner,
)
from .errors import ProxyPolicyError
from .handlers import (
    RUNTIME_VERSION,
    ConfigHandler,
    SystemStatusHandler,
    VersionHandler,
)
from .proxy import ProxyUpstream, compile_proxy_route
from .registry import HandlerRegistry, OperationKey, Surface
from .rest_server import (
    Dispatcher,
    LoopbackHTTPServer,
    assert_bind_allowed,
    is_allowed_bind_host,
    make_request_handler,
)
from .ws_protocol import VALID_CHANNELS, WsHub

ROOT = Path(__file__).resolve().parents[2]

# Runtime-owned operations: identifiers the runtime answers itself.
_REST_STATUS = "GET /api/v1/system/status"
_CLI_READINESS_CHECK = "readiness check"
_CLI_VERSION = "admin version"
_CLI_CONFIG = "admin config"


class OperatorInterfaceRuntime:
    """The operator-interface runtime (HTTP + WebSocket + CLI dispatch)."""

    def __init__(self, root: Path = ROOT) -> None:
        self._root = root
        self._contract = load_contract_block(root)
        validate_owners(self._contract, ROUTES, COMMANDS, EVENT_CHANNELS)

        self._registry = HandlerRegistry()
        self._ws_hub = WsHub()
        # Channels whose publisher a downstream feature has claimed. A channel
        # counts toward workflow readiness only once its publisher is registered.
        self._publishers: set[str] = set()
        self._register_runtime_handlers()

        discovery = self._discovery_index()
        meta_get = {
            "/": lambda: discovery,
            "/openapi.json": build_openapi,
            "/healthz": lambda: {"status": "ok", "service": "atp-operator-interface-runtime"},
        }
        self._dispatcher = Dispatcher(self._registry, meta_get)
        # Static assets (dashboard HTML/JS/CSS) a top-layer consumer mounts via
        # register_asset_routes; threaded into the server at start() time.
        self._asset_routes: dict[str, tuple[str, bytes]] = {}
        # Runtime-served meta GET paths (mirrors what register_meta_route adds)
        # — kept so proxy prefixes can be refused when they would shadow one.
        self._meta_paths: set[str] = set(meta_get)
        # Reverse-proxy routes (SRS-RES-001 / IF-13): prefix -> compiled fixed
        # upstream; threaded into the server at start() time.
        self._proxy_routes: dict[str, ProxyUpstream] = {}
        self._server: LoopbackHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ----- handler registration ----- #

    def _register_runtime_handlers(self) -> None:
        status = SystemStatusHandler(self.status_snapshot)
        self._registry.register(OperationKey(Surface.REST, _REST_STATUS), status)
        self._registry.register(OperationKey(Surface.CLI, _CLI_READINESS_CHECK), status)
        self._registry.register(
            OperationKey(Surface.CLI, _CLI_VERSION),
            VersionHandler(self._contract_revision()),
        )
        self._registry.register(
            OperationKey(Surface.CLI, _CLI_CONFIG),
            ConfigHandler(self._config_catalogue()),
        )

    # ----- introspection / snapshots ----- #

    def _contract_revision(self) -> str:
        return f"operator-workflow-surface@{RUNTIME_VERSION}"

    def _config_catalogue(self) -> list[dict]:
        catalogue: list[dict] = []
        for spec in REQUIRED_KEYS:
            catalogue.append(
                {
                    "name": spec.name,
                    "category": spec.category.value,
                    "type": spec.type.value,
                    "secret": spec.secret,
                    "value": ConfigHandler.REDACTION_MARKER if spec.secret else "<unset>",
                }
            )
        return catalogue

    def _workflow_status(self) -> list[dict]:
        """Per-AC-workflow implemented/deferred map for the status report.

        A workflow is ``fully_served`` only when **every** one of its REST and
        CLI operations has a real handler — registering a single operation does
        not make a multi-operation workflow ready. Each not-yet-served operation
        contributes its owning feature to ``deferred_owners`` so the report says
        exactly who must land for the workflow to complete.
        """

        rows: list[dict] = []
        for workflow in self._contract["ac_workflows"]:
            rest_caps = workflow.get("rest_capabilities", [])
            cli_groups = workflow.get("cli_groups", [])
            real = 0
            total = 0
            deferred_owners: set[str] = set()
            for cap in rest_caps:
                for route in routes_by_capability(Capability(cap)):
                    total += 1
                    key = OperationKey(Surface.REST, f"{route.method.value} {route.path}")
                    if self._registry.is_registered(key):
                        real += 1
                    else:
                        deferred_owners.add(rest_owner(cap))
            for group in cli_groups:
                # A shared group (e.g. `admin`) only contributes the commands
                # that belong to THIS workflow — so LOGS readiness never depends
                # on `admin alerts` / `admin config` / `admin version`.
                relevant = SHARED_GROUP_WORKFLOW_COMMANDS.get((workflow["id"], group))
                for command in commands_by_group(Group(group)):
                    if relevant is not None and command.name not in relevant:
                        continue
                    total += 1
                    key = OperationKey(Surface.CLI, command.invocation)
                    if self._registry.is_registered(key):
                        real += 1
                    else:
                        deferred_owners.add(cli_owner(group, command.name))
            for channel in workflow.get("websocket_channels", []):
                total += 1
                if channel in self._publishers:
                    real += 1
                else:
                    deferred_owners.add(ws_owner(channel))
            rows.append(
                {
                    "id": workflow["id"],
                    "label": workflow["label"],
                    "implemented_operations": real,
                    "total_operations": total,
                    # Fully served only when every operation is wired — a single
                    # registered handler must not flip a multi-op workflow ready.
                    "fully_served": total > 0 and real == total,
                    "deferred_owners": sorted(deferred_owners),
                }
            )
        return rows

    def status_snapshot(self) -> dict:
        """Build the runtime's own status report (never overstates readiness)."""

        workflows = self._workflow_status()
        required = set(self._contract["required_workflow_ids"])
        ready = all(w["fully_served"] for w in workflows if w["id"] in required)
        return {
            "ready": ready,
            "runtime": {
                "service": "atp-operator-interface-runtime",
                "status": "up",
                "version": RUNTIME_VERSION,
                "auth_model": self._contract["expected_auth_model"],
                "bind_host": self.bound_host(),
            },
            "surfaces": {
                "rest": {"routes": len(ROUTES), "capabilities": len(set(Capability))},
                "cli": {"commands": len(COMMANDS), "groups": len(set(Group))},
                "websocket": {"path": WS_PATH, "channels": len(EVENT_CHANNELS)},
            },
            "workflows": workflows,
            "note": (
                "ready=false while domain workflows are deferred; domain readiness "
                "(IB / SSD / NAS / ingestion freshness) is owned by SRS-MD-006."
            ),
            "srs_ref": "SRS-API-001",
        }

    def _discovery_index(self) -> dict:
        return {
            "service": "atp-operator-interface-runtime",
            "auth_model": self._contract["expected_auth_model"],
            "bind_policy": "loopback-or-rfc1918 (SRS-SEC-002)",
            "surfaces": {
                "rest": {"openapi": "/openapi.json", "base_path": "/api/v1", "routes": len(ROUTES)},
                "websocket": {
                    "path": WS_PATH,
                    "channels": sorted(c.name.value for c in EVENT_CHANNELS),
                },
                # The *working* entrypoint dispatches through this runtime;
                # `python -m atp_cli` is the declarative API-4 contract stub.
                "cli": {
                    "program": "python -m atp_runtime",
                    "contract_program": "python -m atp_cli",
                    "commands": len(COMMANDS),
                },
            },
            "status_path": "/api/v1/system/status",
            "srs_ref": "SRS-API-001",
        }

    # ----- dispatch entry points ----- #

    @property
    def registry(self) -> HandlerRegistry:
        """The handler registry (downstream features register real handlers here)."""

        return self._registry

    def dispatch_rest(
        self, method: str, raw_path: str, body_bytes: bytes = b""
    ) -> tuple[int, dict]:
        """In-process REST dispatch (used by tests and the live server alike)."""

        return self._dispatcher.dispatch_rest(method, raw_path, body_bytes)

    def cli_dispatcher(self) -> CliDispatcher:
        """Return a :class:`~atp_runtime.cli_dispatch.CliDispatcher` over this registry."""

        return CliDispatcher(self._registry)

    def register_meta_route(self, path: str, provider: Callable[[], dict[str, object]]) -> None:
        """Register a runtime-served GET path returning a JSON dict (generic seam).

        A top-layer consumer (e.g. a mounted dashboard) registers a discovery /
        snapshot endpoint outside the ``/api/v1`` contract. Register before
        :meth:`start`. The runtime itself imports no consumer package — the
        provider is an opaque ``Callable``.
        """

        self._meta_paths.add(path)
        self._dispatcher.register_meta_route(path, provider)

    def register_asset_routes(self, routes: Mapping[str, tuple[str, bytes]]) -> None:
        """Register static GET assets as an exact path -> ``(content_type, bytes)`` map.

        A top-layer consumer (e.g. a mounted dashboard) serves pre-materialised
        HTML/JS/CSS. Register before :meth:`start`; the map is passed to the
        server at bind time and served by exact-key lookup (no filesystem access,
        no path traversal).
        """

        self._asset_routes.update(routes)

    def register_proxy_route(self, prefix: str, upstream: str) -> None:
        """Reverse-proxy every request under ``prefix`` to the FIXED ``upstream``.

        Generic seam (``SRS-RES-001`` / IF-13) for a top-layer consumer — e.g.
        the dashboard's embedded Jupyter research environment at ``/research/``.
        The upstream is never derived from a request; it must be plain ``http``
        to a loopback/RFC-1918 address (a DNS upstream is re-validated on every
        connect, fail closed). A prefix that would shadow ``/api/``,
        ``/dashboard/``, the WebSocket path, or a registered meta/asset path is
        refused. Register before :meth:`start` so live handler threads never
        read the route map mid-mutation.

        Raises:
            ProxyPolicyError: On any policy violation, or if the runtime is
                already started.
        """

        if self._server is not None:
            raise ProxyPolicyError("register proxy routes before start()")
        reserved = [
            "/api/",
            "/dashboard/",
            WS_PATH,
            *self._meta_paths,
            *self._asset_routes,
            *self._proxy_routes,
        ]
        route = compile_proxy_route(
            prefix, upstream, reserved=reserved, allow_host=is_allowed_bind_host
        )
        self._proxy_routes[route.prefix] = route

    def register_publisher(self, channel: str) -> None:
        """Claim ownership of a WebSocket channel's publisher.

        A downstream feature (SRS-UI-001 / SRS-LOG-001 / SRS-NOTIF-001 / ...)
        calls this when it starts publishing a channel, so the channel counts
        toward its workflow's readiness. ``ready`` stays false while any channel
        of a required workflow has no registered publisher.
        """

        if channel not in VALID_CHANNELS:
            raise ValueError(f"unknown event channel {channel!r}")
        self._publishers.add(channel)

    def is_publisher_registered(self, channel: str) -> bool:
        """Whether a publisher has been claimed for ``channel``."""

        return channel in self._publishers

    def publish(self, channel: str, payload: object) -> int:
        """Fan a WebSocket EVENT out to subscribed sessions; return delivery count."""

        return self._ws_hub.publish(channel, payload)

    # ----- live server lifecycle ----- #

    def start(self, host: str = "127.0.0.1", port: int = 0) -> tuple[str, int]:
        """Bind a loopback HTTP+WS server in a background thread; return its address.

        Not re-entrant: starting an already-running runtime raises rather than
        leaking the existing listener (which ``stop()`` could no longer reach).
        Call :meth:`stop` before starting again.
        """

        if self._server is not None:
            raise RuntimeError("runtime already started; call stop() before starting again")
        assert_bind_allowed(host)
        self._server = LoopbackHTTPServer(
            (host, port),
            make_request_handler(),
            self._dispatcher,
            self._ws_hub,
            self._asset_routes,
            self._proxy_routes,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="atp-operator-interface", daemon=True
        )
        self._thread.start()
        return self.bound_address()

    def bound_address(self) -> tuple[str, int]:
        if self._server is None:
            raise RuntimeError("runtime is not started")
        address = self._server.server_address
        return str(address[0]), int(address[1])

    def bound_host(self) -> str:
        if self._server is None:
            return str(self._contract["loopback_bind_host"])
        return str(self._server.server_address[0])

    def stop(self) -> None:
        """Shut the live server down and join its thread."""

        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
