"""ATP operator-interface runtime (SRS-API-001 / operator-interface-runtime).

This package is the HTTP server + WebSocket server + CLI dispatcher that bind
the declarative operator contract — :mod:`atp_api` (REST), :mod:`atp_cli`
(CLI), :mod:`atp_ws` (WebSocket) — to handlers. It is the ``operator-interface-
runtime`` named in
``architecture/runtime_services.json#operator_workflow_surface_contract.deferred``.

The runtime serves the *full* documented surface from day one: the handful of
operations that describe the runtime itself (system status, version, config
schema) return real data, and every domain operation (kill switch, lifecycle,
ranking, backtests, logs, alerts, ...) resolves to a structured ``501``
envelope naming its owning feature and performs no side effect. Those owners
register real handlers on :class:`HandlerRegistry` as they land, with no change
to the route/command/channel shapes the contract froze.

Interface invariants the runtime enforces (independent of any domain feature):

* loopback / RFC 1918 bind only (``SRS-SEC-002``); a public bind fails closed;
* confirmation guard on irreversible operations (``UI-4`` / ``SRS-SAFE-001``):
  the handler is never reached without a confirmation token;
* structured interface errors (404 / 405 / 428 / 501 / 400);
* secret redaction in the config view (``SRS-SEC-001``).

See ``python/atp_runtime/README.md`` for the operator-facing summary.
"""

from .cli_dispatch import CliDispatcher
from .contract import RUNTIME_OWNER, cli_owner, rest_owner, validate_owners
from .errors import BindPolicyError, ErrorCategory, InterfaceError, ProxyPolicyError
from .handlers import RUNTIME_VERSION, ConfigHandler, SystemStatusHandler, VersionHandler
from .registry import (
    DeferredHandler,
    Handler,
    HandlerRegistry,
    HandlerResult,
    OperationKey,
    Request,
    Surface,
)
from .rest_server import Dispatcher, RouteTable, assert_bind_allowed, is_allowed_bind_host
from .runtime import OperatorInterfaceRuntime
from .ws_frames import compute_accept_key, decode_frame, encode_text_frame
from .ws_protocol import VALID_CHANNELS, WsHub, WsSession

__all__ = [
    "BindPolicyError",
    "CliDispatcher",
    "ConfigHandler",
    "DeferredHandler",
    "Dispatcher",
    "ErrorCategory",
    "Handler",
    "HandlerRegistry",
    "HandlerResult",
    "InterfaceError",
    "OperationKey",
    "OperatorInterfaceRuntime",
    "ProxyPolicyError",
    "RUNTIME_OWNER",
    "RUNTIME_VERSION",
    "Request",
    "RouteTable",
    "Surface",
    "SystemStatusHandler",
    "VALID_CHANNELS",
    "VersionHandler",
    "WsHub",
    "WsSession",
    "assert_bind_allowed",
    "cli_owner",
    "compute_accept_key",
    "decode_frame",
    "encode_text_frame",
    "is_allowed_bind_host",
    "rest_owner",
    "validate_owners",
]
