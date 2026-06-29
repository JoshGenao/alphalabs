"""HTTP transport for the operator REST surface, with a WebSocket upgrade.

Three layers, smallest-blast-radius first:

* :class:`RouteTable` — matches an incoming ``(method, path)`` against the
  declared :data:`atp_api.ROUTES`, capturing ``{param}`` segments and raising a
  structured ``404``/``405`` :class:`~atp_runtime.errors.InterfaceError`.
* :class:`Dispatcher` — transport-agnostic: turns ``(method, raw_path, body)``
  into a ``(status, body)`` pair, enforcing the confirmation guard and routing
  to the registry. Unit-testable without a socket.
* :class:`make_request_handler` / :class:`LoopbackHTTPServer` — bind the
  dispatcher to ``http.server`` and enforce the ``SRS-SEC-002`` loopback bind.

The WebSocket upgrade (``WS_PATH``) is handled in the request handler because it
hijacks the raw connection after the 101 response and runs a frame loop against
:mod:`atp_runtime.ws_protocol`.

SRS trace
---------
``SRS-API-001`` (REST + WebSocket operator surface), ``SRS-SEC-002`` (loopback
bind), ``SRS-ERR-001``-style structured interface errors, ``UI-4`` /
``SRS-SAFE-001`` (confirmation guard).
"""

from __future__ import annotations

import ipaddress
import json
import queue
import socket
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import parse_qs, urlsplit

from atp_api import ROUTES, Route
from atp_cli import Group, commands_by_group
from atp_ws import WS_PATH

from .contract import rest_owner
from .errors import BindPolicyError, ErrorCategory, InterfaceError
from .registry import (
    DeferredHandler,
    HandlerRegistry,
    OperationKey,
    Request,
    Surface,
    invoke_handler,
)
from .ws_frames import (
    FrameError,
    OpCode,
    compute_accept_key,
    decode_frame,
    encode_close_frame,
    encode_pong_frame,
)
from .ws_protocol import WsHub, WsSession

#: Query/body values that count as a *false* confirmation token.
_FALSEY_CONFIRM = frozenset({"", "0", "false", "no", "off"})

#: Hard ceiling on a REST request body (operator JSON is small). A larger
#: declared Content-Length is refused (413) before any body byte is read.
_MAX_BODY_BYTES = 1 << 20  # 1 MiB
#: Per-request socket read deadline: an incomplete/slow HTTP body fails closed
#: instead of pinning a handler thread forever.
_HTTP_READ_TIMEOUT = 15.0
#: WebSocket socket read/write deadline: a stuck client write raises rather than
#: blocking the connection's writer thread indefinitely.
_WS_SOCKET_TIMEOUT = 10.0
#: Bounded per-connection outbound frame buffer. When a slow consumer fills it,
#: the connection is closed rather than letting it block publisher fan-out.
_WS_OUTBOX_MAXSIZE = 256

# Lifecycle actions that require a confirmation token even though the shared
# ``POST /api/v1/strategies/{id}/lifecycle`` route is not itself marked
# requires_confirmation (it also serves non-irreversible start/stop/restart).
# Derived from the CLI ``strategy`` group so the REST guard and the CLI
# ``strategy rollback --confirm`` guard stay symmetric (UI-4 / SRS-SAFE-001 /
# SRS-ORCH-005): a new confirmation-required lifecycle command on either surface
# is enforced on both without drift.
_ACTION_CONFIRM_REQUIRED = frozenset(
    command.name for command in commands_by_group(Group.STRATEGY) if command.requires_confirmation
)

# The exact RFC 1918 private IPv4 ranges (SRS-SEC-002 names "RFC 1918 or
# loopback" specifically — NOT the broader ``is_private`` set, which also
# admits link-local 169.254/16, CGNAT 100.64/10, and IPv6 ULA).
_RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def is_allowed_bind_host(host: str) -> bool:
    """Return whether ``host`` is a loopback or RFC 1918 address (SRS-SEC-002).

    Only loopback (``127.0.0.0/8`` / ``::1``) and the three RFC 1918 IPv4 ranges
    are permitted. ``0.0.0.0`` / ``::`` / link-local / CGNAT / any publicly
    routable address returns ``False``.

    Example:
        >>> is_allowed_bind_host("127.0.0.1"), is_allowed_bind_host("0.0.0.0")
        (True, False)
        >>> is_allowed_bind_host("10.2.3.4"), is_allowed_bind_host("8.8.8.8")
        (True, False)
        >>> is_allowed_bind_host("169.254.1.1")  # link-local is not RFC 1918
        False
    """

    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:  # 127.0.0.0/8, ::1
        return True
    if address.version == 4:
        return any(address in network for network in _RFC1918_NETWORKS)
    return False  # IPv6: loopback only (RFC 1918 is IPv4-only)


def assert_bind_allowed(host: str) -> None:
    """Raise :class:`BindPolicyError` unless ``host`` is loopback/RFC 1918."""

    if not is_allowed_bind_host(host):
        raise BindPolicyError(
            f"refusing to bind operator interface to non-RFC1918/non-loopback host {host!r} "
            f"(SRS-SEC-002): external exposure requires explicit, documented operator config "
            f"this runtime does not provide"
        )


def _is_confirmed(value: str | None) -> bool:
    return value is not None and value.strip().lower() not in _FALSEY_CONFIRM


def _body_confirmed(body: Mapping[str, object]) -> bool:
    """Whether a parsed JSON body carries a truthy ``confirm`` token."""

    raw = body.get("confirm")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return _is_confirmed(raw)
    return False


@dataclass(frozen=True, slots=True)
class _CompiledRoute:
    method: str
    segments: tuple[str, ...]
    route: Route


class RouteTable:
    """Matches request paths against the declared REST routes."""

    def __init__(self, routes: tuple[Route, ...] = ROUTES) -> None:
        self._compiled = [
            _CompiledRoute(
                method=route.method.value,
                segments=tuple(seg for seg in route.path.strip("/").split("/")),
                route=route,
            )
            for route in routes
        ]

    @staticmethod
    def _path_segments(path: str) -> tuple[str, ...]:
        return tuple(seg for seg in path.strip("/").split("/") if seg != "")

    def match(self, method: str, path: str) -> tuple[Route, dict[str, str]]:
        """Return ``(route, path_params)`` or raise a 404/405 InterfaceError."""

        segments = self._path_segments(path)
        path_exists = False
        allowed: set[str] = set()
        for compiled in self._compiled:
            params = self._try_match(compiled.segments, segments)
            if params is None:
                continue
            path_exists = True
            allowed.add(compiled.method)
            if compiled.method == method:
                return compiled.route, params
        if path_exists:
            raise InterfaceError(
                ErrorCategory.METHOD_NOT_ALLOWED,
                f"{method} not allowed on {path}",
                detail={"allowed_methods": sorted(allowed)},
            )
        raise InterfaceError(
            ErrorCategory.NOT_FOUND,
            f"no operator route for {path}",
            detail={"path": path},
        )

    @staticmethod
    def _try_match(template: tuple[str, ...], actual: tuple[str, ...]) -> dict[str, str] | None:
        if len(template) != len(actual):
            return None
        params: dict[str, str] = {}
        for tmpl_seg, act_seg in zip(template, actual, strict=True):
            if tmpl_seg.startswith("{") and tmpl_seg.endswith("}"):
                params[tmpl_seg[1:-1]] = act_seg
            elif tmpl_seg != act_seg:
                return None
        return params


class Dispatcher:
    """Transport-agnostic REST dispatch + the meta (discovery/openapi) endpoints.

    Args:
        registry: Handler registry (real handlers; misses fall to deferral).
        meta_get: Map of exact GET paths the runtime serves itself outside the
            ``/api/v1`` contract (e.g. ``/`` discovery, ``/openapi.json``), each
            a zero-arg callable returning a JSON-serialisable dict.
    """

    def __init__(
        self, registry: HandlerRegistry, meta_get: Mapping[str, Callable[[], dict]]
    ) -> None:
        self._registry = registry
        self._routes = RouteTable()
        self._meta_get = meta_get

    def dispatch_rest(self, method: str, raw_path: str, body_bytes: bytes) -> tuple[int, dict]:
        """Dispatch one REST request to a ``(status, body)`` pair."""

        try:
            split = urlsplit(raw_path)
            path = split.path
            query = {k: v[-1] for k, v in parse_qs(split.query, keep_blank_values=True).items()}

            if method == "GET" and path in self._meta_get:
                return 200, self._meta_get[path]()

            route, path_params = self._routes.match(method, path)

            # Route-level confirmation guard (body-independent): kill switch,
            # live designation, Hot-Swap. Enforced before the body is even
            # parsed so a malformed body cannot mask the missing token.
            query_confirmed = _is_confirmed(query.get("confirm"))
            if route.requires_confirmation and not query_confirmed:
                raise InterfaceError(
                    ErrorCategory.CONFIRMATION_REQUIRED,
                    f"{route.method.value} {route.path} requires a confirmation token "
                    f"(UI-4 / SRS-SAFE-001)",
                    detail={"confirm_param": "confirm", "workflow": route.capability.value},
                )

            body = self._parse_body(method, body_bytes)
            confirmed = query_confirmed or _body_confirmed(body)

            # Action-level confirmation guard: the shared lifecycle route is not
            # route-level confirmation-gated, but a `rollback` action is
            # irreversible and must carry the same guard the CLI enforces
            # (SRS-ORCH-005 / SYS-80). Never dispatch such an action unconfirmed.
            action = ""
            if "action" in route.request_fields:
                action = str(body.get("action") or query.get("action") or "")
                if action in _ACTION_CONFIRM_REQUIRED and not confirmed:
                    raise InterfaceError(
                        ErrorCategory.CONFIRMATION_REQUIRED,
                        f"{route.method.value} {route.path} action={action!r} requires a "
                        f"confirmation token (UI-4 / SRS-SAFE-001 / SRS-ORCH-005)",
                        detail={
                            "confirm_param": "confirm",
                            "workflow": route.capability.value,
                            "action": action,
                        },
                    )

            identifier = f"{route.method.value} {route.path}"
            key = OperationKey(Surface.REST, identifier)
            owner = rest_owner(route.capability.value)
            handler = self._registry.resolve(
                key, deferred=DeferredHandler(owner=owner, summary=route.summary)
            )
            request = Request(
                surface=Surface.REST,
                operation=key,
                method=route.method.value,
                path=path,
                path_params=path_params,
                query=query,
                body=body,
                confirmed=confirmed,
                workflow_id=route.capability.value,
                srs_refs=tuple(route.srs_refs),
            )
            result = invoke_handler(handler, request)
            return result.status_code, dict(result.body)
        except InterfaceError as error:
            return error.status, error.to_body()

    @staticmethod
    def _parse_body(method: str, body_bytes: bytes) -> dict:
        if method not in ("POST", "PUT") or not body_bytes:
            return {}
        try:
            parsed = json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InterfaceError(ErrorCategory.BAD_REQUEST, f"malformed JSON body: {exc}") from exc
        if not isinstance(parsed, dict):
            raise InterfaceError(ErrorCategory.BAD_REQUEST, "JSON body must be an object")
        return parsed


class LoopbackHTTPServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` that refuses any non-loopback/non-RFC1918 bind."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        dispatcher: Dispatcher,
        ws_hub: WsHub,
    ) -> None:
        assert_bind_allowed(server_address[0])
        self.dispatcher = dispatcher
        self.ws_hub = ws_hub
        super().__init__(server_address, handler_class)

    def handle_error(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        """Swallow benign client-disconnect resets; re-raise anything else.

        A single-user operator client closing a keep-alive connection (or a
        WebSocket socket) yields ``ConnectionResetError`` / ``BrokenPipeError``
        from the stdlib read loop; that is normal and must not spam the log or
        look like a runtime fault. Other exceptions propagate to the default
        handler.
        """

        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def make_request_handler() -> type[BaseHTTPRequestHandler]:
    """Build a ``BaseHTTPRequestHandler`` subclass bound to the server dispatcher."""

    class _Handler(BaseHTTPRequestHandler):
        server_version = "atp-operator-interface/0.1"
        protocol_version = "HTTP/1.1"
        # Per-connection socket read deadline (stdlib reads use self.timeout):
        # an incomplete/slow request body fails closed instead of hanging.
        timeout = _HTTP_READ_TIMEOUT

        # Quiet by default; the runtime owns its own logging surface.
        def log_message(self, *args: object, **kwargs: object) -> None:  # noqa: D401
            return

        def handle_one_request(self) -> None:
            # Catch a *transport* read timeout (slow/incomplete client) at the
            # specific read site and close cleanly, rather than suppressing every
            # TimeoutError globally in handle_error — a handler/dependency timeout
            # is surfaced as a structured 500 by Dispatcher.dispatch_rest instead.
            try:
                super().handle_one_request()
            except TimeoutError:
                self.close_connection = True

        def _dispatch(self, method: str) -> None:
            if method == "GET" and self._is_ws_upgrade():
                self._serve_websocket()
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > _MAX_BODY_BYTES:
                # Refuse before reading a single body byte; close so an undrained
                # oversized body cannot desync a following keep-alive request.
                self.close_connection = True
                error = InterfaceError(
                    ErrorCategory.PAYLOAD_TOO_LARGE,
                    f"request body exceeds {_MAX_BODY_BYTES} byte ceiling",
                )
                self._write_json(error.status, error.to_body())
                return
            body_bytes = self.rfile.read(length) if length else b""
            server = cast(LoopbackHTTPServer, self.server)
            status, body = server.dispatcher.dispatch_rest(method, self.path, body_bytes)
            self._write_json(status, body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

        def _write_json(self, status: int, body: dict) -> None:
            payload = json.dumps(body, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        # ----- WebSocket upgrade (RFC 6455) ----- #

        def _is_ws_upgrade(self) -> bool:
            return (
                urlsplit(self.path).path == WS_PATH
                and self.headers.get("Upgrade", "").lower() == "websocket"
                and "upgrade" in self.headers.get("Connection", "").lower()
                and self.headers.get("Sec-WebSocket-Key") is not None
            )

        def _serve_websocket(self) -> None:
            accept = compute_accept_key(self.headers["Sec-WebSocket-Key"])
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self._run_ws_loop()

        def _run_ws_loop(self) -> None:
            # Backpressure lives in the transport, not the protocol: outbound
            # frames go to a bounded queue drained by one writer thread with a
            # socket write deadline. So `send` (called by the protocol under the
            # session lock, and by the publisher thread) only ever enqueues —
            # never blocks — and a slow consumer cannot stall fan-out to others.
            outbox: queue.Queue = queue.Queue(maxsize=_WS_OUTBOX_MAXSIZE)
            closed = threading.Event()

            def send(frame: bytes) -> None:
                if closed.is_set():
                    return
                try:
                    outbox.put_nowait(frame)
                except queue.Full:
                    closed.set()  # slow consumer: tear down rather than block

            def writer() -> None:
                # Deterministic shutdown without relying on a sentinel that a
                # full outbox could drop: poll with a timeout, drain whatever is
                # queued (so a clean-close frame is still flushed), and exit once
                # the queue is empty AND the connection is closed.
                while True:
                    try:
                        item = outbox.get(timeout=0.2)
                    except queue.Empty:
                        if closed.is_set():
                            return
                        continue
                    try:
                        self.wfile.write(item)
                        self.wfile.flush()
                    except OSError:  # incl. write-deadline TimeoutError
                        closed.set()
                        return

            self.connection.settimeout(_WS_SOCKET_TIMEOUT)
            writer_thread = threading.Thread(target=writer, name="atp-ws-writer", daemon=True)
            writer_thread.start()
            session = WsSession(send)
            hub: WsHub = cast(LoopbackHTTPServer, self.server).ws_hub
            hub.register(session)
            buffer = b""
            try:
                while not closed.is_set():
                    try:
                        chunk = (
                            self.rfile.read1(4096)
                            if hasattr(self.rfile, "read1")
                            else self.rfile.read(4096)
                        )
                    except TimeoutError:  # idle read deadline; keep the connection
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    buffer += chunk
                    done = False
                    while True:
                        try:
                            frame, buffer = decode_frame(buffer)
                        except FrameError:  # malformed/oversized frame: close
                            send(encode_close_frame(1002, "protocol error"))
                            done = True
                            break
                        if frame is None:
                            break
                        if frame.opcode == OpCode.CLOSE:
                            send(encode_close_frame())
                            done = True
                            break
                        if frame.opcode == OpCode.PING:
                            send(encode_pong_frame(frame.payload))
                            continue
                        if frame.opcode == OpCode.TEXT:
                            # The control protocol exchanges small, single-frame
                            # JSON; a fragmented (FIN=0) or continuation frame is
                            # a protocol violation rather than a partial message.
                            if not frame.fin:
                                send(encode_close_frame(1003, "fragmented frames unsupported"))
                                done = True
                                break
                            session.handle_text(frame.text)
                        elif frame.opcode == OpCode.CONTINUATION:
                            send(encode_close_frame(1003, "unexpected continuation frame"))
                            done = True
                            break
                    if done:
                        break
            finally:
                hub.unregister(session)
                closed.set()  # the writer polls `closed` and exits deterministically
                writer_thread.join(timeout=_WS_SOCKET_TIMEOUT + 1)

    return _Handler
