"""Structured interface-layer errors for the operator-interface runtime.

These are the *interface* errors the runtime returns when an operator request
cannot be served at the transport/contract layer — an unknown path, the wrong
method, a missing confirmation token, or a domain handler that has not yet
landed. They are deliberately distinct from the *order-submission* error
envelope owned by ``SRS-ERR-001`` (``crates/atp-execution`` /
``err001_error_envelope_cli``): that envelope carries the original order
parameters and a SyRS order-error category, which has no meaning for a generic
operator request.

Every error renders to a stable JSON body::

    {"error": {"type": ..., "category": ..., "message": ..., "detail": {...}}}

so the dashboard, CLI, and any future client can branch on ``category``
without parsing prose.

SRS trace
---------
``SRS-API-001`` (operator interface surface). The ``CONFIRMATION_REQUIRED``
category enforces the ``UI-4`` / ``SRS-SAFE-001`` two-step guard at the
transport layer; ``BIND_POLICY`` enforces ``SRS-SEC-002``.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCategory(StrEnum):
    """Interface-layer error categories returned by the runtime.

    Each member maps to a single HTTP status via :data:`STATUS_FOR_CATEGORY`
    and to a CLI exit code via :data:`atp_runtime.cli_dispatch`. The set is
    closed: a handler that needs a domain-specific category (e.g. an order
    rejection reason) returns it inside ``detail``, not here.

    Example:
        >>> ErrorCategory.NOT_IMPLEMENTED
        <ErrorCategory.NOT_IMPLEMENTED: 'NOT_IMPLEMENTED'>
    """

    BAD_REQUEST = "BAD_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    GATEWAY_TIMEOUT = "GATEWAY_TIMEOUT"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


#: HTTP status code emitted for each interface error category.
STATUS_FOR_CATEGORY: dict[ErrorCategory, int] = {
    ErrorCategory.BAD_REQUEST: 400,
    ErrorCategory.NOT_FOUND: 404,
    ErrorCategory.METHOD_NOT_ALLOWED: 405,
    # 428 Precondition Required: the operator must repeat the request with the
    # confirmation token (UI-4 two-step modal / SRS-SAFE-001).
    ErrorCategory.CONFIRMATION_REQUIRED: 428,
    ErrorCategory.PAYLOAD_TOO_LARGE: 413,
    # 500: an unexpected handler/dependency failure, surfaced as a structured
    # error rather than a silent connection close.
    ErrorCategory.INTERNAL_ERROR: 500,
    # 504: a handler/dependency *timeout* (e.g. an IB/IO call), kept distinct
    # from a generic 500 and from a readiness failure so callers can retry.
    ErrorCategory.GATEWAY_TIMEOUT: 504,
    ErrorCategory.NOT_IMPLEMENTED: 501,
}


class InterfaceError(Exception):
    """An operator request that the runtime cannot serve at the contract layer.

    Attributes:
        category: One of :class:`ErrorCategory`; selects the HTTP status.
        message: Human-readable, single-line explanation.
        type: Stable machine token (defaults to the category value); lets a
            handler distinguish two failures that share a category.
        detail: JSON-serialisable extra context (e.g. the deferred owner
            feature id, the allowed methods for a 405).

    Example:
        >>> err = InterfaceError(ErrorCategory.NOT_FOUND, "no such path")
        >>> err.status
        404
        >>> err.to_body()["error"]["category"]
        'NOT_FOUND'
    """

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        type: str | None = None,
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.type = type or category.value
        self.detail = dict(detail or {})

    @property
    def status(self) -> int:
        """HTTP status code for this error (see :data:`STATUS_FOR_CATEGORY`)."""

        return STATUS_FOR_CATEGORY[self.category]

    def to_body(self) -> dict:
        """Render the stable ``{"error": {...}}`` JSON body.

        Example:
            >>> InterfaceError(
            ...     ErrorCategory.NOT_IMPLEMENTED, "deferred",
            ...     detail={"owner": "SRS-EXE-001"},
            ... ).to_body()
            {'error': {'type': 'NOT_IMPLEMENTED', 'category': 'NOT_IMPLEMENTED', \
'message': 'deferred', 'detail': {'owner': 'SRS-EXE-001'}}}
        """

        return {
            "error": {
                "type": self.type,
                "category": self.category.value,
                "message": self.message,
                "detail": self.detail,
            }
        }


class BindPolicyError(Exception):
    """Raised when the runtime is asked to bind a non-RFC1918/non-loopback host.

    Per ``SRS-SEC-002`` the operator interface binds only to loopback or
    RFC 1918 addresses by default; binding a publicly routable interface
    requires explicit, documented operator configuration that this runtime
    intentionally does not provide. Attempting ``0.0.0.0`` / ``::`` / a public
    address fails closed here, before any socket is opened.
    """
