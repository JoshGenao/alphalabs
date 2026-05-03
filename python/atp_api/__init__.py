"""ATP REST API contract surface (API-2 / SRS-API-001).

This package exposes the declarative REST API contract used to verify
``API-2`` in ``feature_list.json``. It contains no HTTP handlers; concrete
handlers and an HTTP runtime arrive with downstream features.

See ``python/atp_api/README.md`` for the operator-facing summary.
"""

from .openapi import (
    OPENAPI_SPEC,
    OPENAPI_TITLE,
    OPENAPI_VERSION,
    build_openapi,
    render_snapshot,
)
from .routes import (
    AUTH_MODEL,
    BIND_HOST,
    ROUTES,
    Capability,
    Method,
    Route,
    routes_by_capability,
)


__all__ = [
    "AUTH_MODEL",
    "BIND_HOST",
    "Capability",
    "Method",
    "OPENAPI_SPEC",
    "OPENAPI_TITLE",
    "OPENAPI_VERSION",
    "ROUTES",
    "Route",
    "build_openapi",
    "render_snapshot",
    "routes_by_capability",
]
