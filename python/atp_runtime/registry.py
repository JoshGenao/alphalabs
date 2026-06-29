"""Handler registry and request/response value types for the runtime.

The operator-interface runtime is a *dispatcher*: it resolves an incoming
operator request (REST call, CLI invocation, or WebSocket control message) to
the declared contract operation, enforces the interface-level invariants
(confirmation, method, loopback bind), and then hands a normalised
:class:`Request` to a :class:`Handler`. Handlers are looked up in a
:class:`HandlerRegistry` keyed by ``(surface, identifier)``.

The registry is the single seam every downstream feature plugs into. Until a
feature lands, its operations resolve to a :class:`DeferredHandler` that
returns a structured ``501 NOT_IMPLEMENTED`` envelope naming the owning
feature and **performs no side effect** — the kill-switch route, for example,
is reachable and documented but inert until ``SRS-EXE-001`` registers a real
handler.

SRS trace
---------
``SRS-API-001`` (operator interface surface). The registry boundary is what
lets ``SRS-EXE-001`` / ``SRS-ORCH-004`` / ``SRS-RESV-002`` / ... wire real
behaviour onto the frozen contract without renegotiating route shapes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .errors import ErrorCategory, InterfaceError


class Surface(StrEnum):
    """The three operator surfaces the runtime serves.

    Example:
        >>> Surface.REST
        <Surface.REST: 'rest'>
    """

    REST = "rest"
    CLI = "cli"
    WEBSOCKET = "websocket"


@dataclass(frozen=True, slots=True)
class OperationKey:
    """Registry key uniquely identifying one contract operation.

    Attributes:
        surface: Which operator surface the operation belongs to.
        identifier: Surface-local identifier — ``"POST /api/v1/kill-switch"``
            for REST, ``"kill-switch activate"`` for CLI, ``"LOGS"`` for a
            WebSocket channel.

    Example:
        >>> OperationKey(Surface.REST, "POST /api/v1/kill-switch").identifier
        'POST /api/v1/kill-switch'
    """

    surface: Surface
    identifier: str


@dataclass(frozen=True, slots=True)
class Request:
    """Normalised operator request handed to a :class:`Handler`.

    The runtime builds this after it has matched the contract operation and
    enforced the interface invariants, so a handler can trust that
    ``confirmed`` is ``True`` whenever the operation requires confirmation.

    Attributes:
        surface: Originating surface.
        operation: The matched :class:`OperationKey`.
        method: HTTP method for REST operations; ``None`` for CLI/WS.
        path: REST path actually requested (with parameters substituted);
            ``None`` for CLI/WS.
        path_params: Values captured from ``{param}`` path segments.
        query: Query-string / option values as strings.
        body: Parsed JSON body (REST) or message payload (WS); ``{}`` if none.
        confirmed: Whether a confirmation token was supplied.
        workflow_id: The AC workflow this operation maps to, if any.
        srs_refs: SRS/SyRS traces declared on the contract operation.

    Example:
        >>> Request(Surface.CLI, OperationKey(Surface.CLI, "live show")).body
        {}
    """

    surface: Surface
    operation: OperationKey
    method: str | None = None
    path: str | None = None
    path_params: Mapping[str, str] = field(default_factory=dict)
    query: Mapping[str, str] = field(default_factory=dict)
    body: Mapping[str, object] = field(default_factory=dict)
    confirmed: bool = False
    workflow_id: str | None = None
    srs_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HandlerResult:
    """A handler's response: an HTTP-style status plus a JSON-serialisable body.

    Attributes:
        status_code: HTTP status (REST) / mapped exit-code source (CLI) /
            ``200`` for a delivered WS payload.
        body: JSON-serialisable response document.

    Example:
        >>> HandlerResult(200, {"ready": False}).status_code
        200
    """

    status_code: int
    body: Mapping[str, object]


@runtime_checkable
class Handler(Protocol):
    """Callable that turns a :class:`Request` into a :class:`HandlerResult`.

    A handler must be free of transport concerns (no socket, no argv) and must
    not raise for *expected* interface failures — it returns a
    :class:`HandlerResult` with the appropriate status, or raises
    :class:`atp_runtime.errors.InterfaceError` for a structured failure the
    runtime serialises.
    """

    def handle(self, request: Request) -> HandlerResult:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True, slots=True)
class DeferredHandler:
    """Inert handler returning a structured ``501`` naming the owning feature.

    This is the default for every contract operation whose behaviour belongs to
    a downstream feature that has not yet landed. It performs **no** side
    effect — the operation is reachable and documented (so the operator and the
    OpenAPI/manual/AsyncAPI snapshots agree it exists) but does nothing until
    its owner registers a real handler.

    Attributes:
        owner: Feature id that owns the real behaviour (e.g. ``"SRS-EXE-001"``).
        summary: One-line description carried in the response detail.

    Example:
        >>> h = DeferredHandler(owner="SRS-EXE-001", summary="kill switch")
        >>> h.handle(Request(Surface.REST,
        ...     OperationKey(Surface.REST, "POST /api/v1/kill-switch"))).status_code
        501
    """

    owner: str
    summary: str

    def handle(self, request: Request) -> HandlerResult:
        error = InterfaceError(
            ErrorCategory.NOT_IMPLEMENTED,
            f"{request.operation.identifier}: handler not yet wired; owned by {self.owner}.",
            type="HANDLER_DEFERRED",
            detail={
                "owner": self.owner,
                "summary": self.summary,
                "workflow_id": request.workflow_id,
                "srs_refs": list(request.srs_refs),
            },
        )
        return HandlerResult(error.status, error.to_body())


def invoke_handler(handler: Handler, request: Request) -> HandlerResult:
    """Run ``handler`` and serialise *any* failure to a structured result.

    Used by both the REST and CLI dispatchers so a handler failure is observable
    and distinguishable on every surface — never a traceback or a silent close:

    * a structured :class:`~atp_runtime.errors.InterfaceError` keeps its status;
    * a ``TimeoutError`` (e.g. an escaped IB/IO timeout) becomes a ``504``
      ``GATEWAY_TIMEOUT`` — distinct from a generic failure and from not-ready;
    * any other exception becomes a ``500`` ``INTERNAL_ERROR``.
    """

    try:
        return handler.handle(request)
    except InterfaceError as error:
        return HandlerResult(error.status, error.to_body())
    except TimeoutError:
        timeout = InterfaceError(
            ErrorCategory.GATEWAY_TIMEOUT,
            f"handler for {request.operation.identifier} timed out",
            detail={"exception": "TimeoutError"},
        )
        return HandlerResult(timeout.status, timeout.to_body())
    except Exception as exc:  # noqa: BLE001 - surface, never swallow
        internal = InterfaceError(
            ErrorCategory.INTERNAL_ERROR,
            f"handler for {request.operation.identifier} failed: {type(exc).__name__}",
            detail={"exception": type(exc).__name__},
        )
        return HandlerResult(internal.status, internal.to_body())


class HandlerRegistry:
    """Maps :class:`OperationKey` to :class:`Handler`; falls back to deferral.

    Resolution never raises for an unregistered-but-declared operation: it
    returns a :class:`DeferredHandler` built from the operation's owning
    feature, so the runtime can serve the full documented contract from day
    one. A lookup for an operation that is not in the contract at all is a
    programming error and raises ``KeyError`` via :meth:`require`.

    Example:
        >>> reg = HandlerRegistry()
        >>> key = OperationKey(Surface.CLI, "admin version")
        >>> reg.register(key, _Echo())            # doctest: +SKIP
        >>> reg.is_registered(key)                # doctest: +SKIP
        True
    """

    def __init__(self) -> None:
        self._handlers: dict[OperationKey, Handler] = {}

    def register(self, key: OperationKey, handler: Handler) -> None:
        """Bind a real handler to ``key`` (idempotent overwrite is rejected)."""

        if key in self._handlers:
            raise ValueError(f"handler already registered for {key}")
        self._handlers[key] = handler

    def is_registered(self, key: OperationKey) -> bool:
        """Return whether a real (non-deferred) handler is bound to ``key``."""

        return key in self._handlers

    def resolve(self, key: OperationKey, *, deferred: DeferredHandler) -> Handler:
        """Return the registered handler for ``key`` or ``deferred``.

        ``deferred`` is supplied by the caller (the runtime, which knows the
        owning feature for the operation) so the registry stays agnostic of
        the contract.
        """

        return self._handlers.get(key, deferred)

    def registered_keys(self) -> frozenset[OperationKey]:
        """Return the set of operations with a real handler bound."""

        return frozenset(self._handlers)
