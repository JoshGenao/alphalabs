"""OpenAPI 3.1 generator for the ATP REST API contract.

The generator is intentionally pure-stdlib: it consumes the declarative
:data:`atp_api.routes.ROUTES` tuple and produces a deterministic OpenAPI 3.1
``dict``. The frozen snapshot at ``python/atp_api/openapi.json`` is byte-
compared against the regenerated dict in ``tools/rest_api_check.py``; the
``--update`` flag rewrites the snapshot.

SRS trace
---------
``SRS-API-001`` (REST/CLI/dashboard operator workflows). The generated
document is contract evidence only; runtime handlers are out of scope here.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any

from .routes import AUTH_MODEL, BIND_HOST, ROUTES, Method, Route

OPENAPI_TITLE = "ATP Operator REST API"
"""Document title surfaced under ``info.title`` in the OpenAPI snapshot."""

OPENAPI_VERSION = "0.1.0"
"""Document version surfaced under ``info.version`` in the OpenAPI snapshot."""

OPENAPI_SPEC = "3.1.0"
"""OpenAPI specification version emitted by :func:`build_openapi`."""

_DEFAULT_PORT = 8080
_PLACEHOLDER_DESCRIPTION = (
    "Contract only. Concrete request and response schemas land with the "
    "downstream feature that owns the handler (EXE-1, ORCH-1, RESV-1, "
    "BT-1, DATA-1, LOG-1, NOTIF-1)."
)


def _path_parameters(path: str) -> tuple[str, ...]:
    """Extract ``{name}`` segments from a route path in declaration order."""

    parts: list[str] = []
    cursor = 0
    while True:
        start = path.find("{", cursor)
        if start == -1:
            break
        end = path.find("}", start + 1)
        if end == -1:
            break
        parts.append(path[start + 1 : end])
        cursor = end + 1
    return tuple(parts)


def _string_schema() -> dict[str, Any]:
    return {"type": "string"}


def _placeholder_object_schema(
    field_names: Iterable[str],
    field_types: Mapping[str, str] | None = None,
    *,
    strict: bool = False,
    required: Iterable[str] = (),
    item_fields: Mapping[str, tuple[tuple[str, str], ...]] | None = None,
) -> dict[str, Any]:
    """An object schema over ``field_names``.

    Fields default to ``string`` — the offline contract's placeholder for a route whose
    handler has not landed. A route that HAS a handler declares real types via
    ``Route.field_types``, because a placeholder that contradicts a live handler is worse
    than no schema: a generated client would send a contract-valid payload the handler
    rejects, and misparse the response it returns.
    """

    types = field_types or {}
    properties: dict[str, Any] = {}
    for name in field_names:
        declared = types.get(name, "string")
        # `int|null` renders as OpenAPI 3.1's type union. A field the handler legitimately
        # returns as null — the drawdown threshold whenever that trigger is disabled, which
        # is the DEFAULT state — must say so, or the commonest valid response is documented
        # as schema-invalid.
        properties[name] = {"type": declared.split("|")} if "|" in declared else {"type": declared}
    # An array-of-objects field describes its ELEMENT here. Hoisting the item's
    # fields to the top level instead would document them beside the array rather
    # than inside it — a client generated from that looks in the wrong place.
    for array_name, item_spec in (item_fields or {}).items():
        properties[array_name] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    # Same `int|null` union rendering as the top level: an item
                    # field that is legitimately null (a system log line has no
                    # strategy_id) must say so, or the commonest valid element is
                    # documented as schema-invalid.
                    item_name: (
                        {"type": item_type.split("|")} if "|" in item_type else {"type": item_type}
                    )
                    for item_name, item_type in item_spec
                },
                "additionalProperties": False,
            },
        }
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        # An unimplemented route cannot say what it would reject, so its placeholder stays
        # permissive. A route whose handler enforces an exact allowlist must say so, or the
        # contract promises an acceptance the live surface refuses.
        "additionalProperties": not strict,
    }
    required_fields = [name for name in required if name in properties]
    if required_fields:
        schema["required"] = required_fields
    return schema


def _build_parameters(route: Route) -> list[dict[str, Any]]:
    parameters: list[dict[str, Any]] = []
    path_params = set(_path_parameters(route.path))

    for name in path_params:
        parameters.append(
            {
                "name": name,
                "in": "path",
                "required": True,
                "schema": _string_schema(),
            }
        )

    if route.method is Method.GET or route.method is Method.DELETE:
        for name in route.request_fields:
            if name in path_params:
                continue
            parameters.append(
                {
                    "name": name,
                    "in": "query",
                    "required": False,
                    "schema": _string_schema(),
                }
            )
    elif route.requires_confirmation and "confirm" in route.request_fields:
        parameters.append(
            {
                "name": "confirm",
                "in": "query",
                "required": True,
                "schema": _string_schema(),
                "description": "Confirmation token (UI-4 two-step modal).",
            }
        )
    elif route.confirmation_actions and "confirm" in route.request_fields:
        # A SHARED action route: the token is required only for the listed
        # actions, so it is documented as optional-at-the-route but mandatory
        # for those actions. Without this a generated client has no documented
        # way to satisfy the transport's action-level guard.
        actions = ", ".join(sorted(route.confirmation_actions))
        parameters.append(
            {
                "name": "confirm",
                "in": "query",
                "required": False,
                "schema": _string_schema(),
                "description": (
                    "Confirmation token (UI-4 two-step modal). REQUIRED for the "
                    f"irreversible action(s): {actions}. Requests carrying one of those "
                    "actions without it are refused 428 CONFIRMATION_REQUIRED. May also "
                    "be supplied as a boolean/string ``confirm`` field in the body."
                ),
            }
        )

    parameters.sort(key=lambda parameter: (parameter["in"], parameter["name"]))
    return parameters


def _build_request_body(route: Route) -> dict[str, Any] | None:
    if route.method in (Method.POST, Method.PUT):
        body_fields = tuple(
            name
            for name in route.request_fields
            if name not in _path_parameters(route.path) and name != "confirm"
        )
        if not body_fields and not (
            route.requires_confirmation and "confirm" in route.request_fields
        ):
            return None
        return {
            "required": True,
            "content": {
                "application/json": {
                    "schema": _placeholder_object_schema(
                        body_fields,
                        dict(route.field_types),
                        strict=route.strict_request_body,
                        required=route.required_request_fields,
                    )
                }
            },
        }
    return None


def _served_description(owner: str) -> str:
    """The implemented counterpart of :data:`_PLACEHOLDER_DESCRIPTION`.

    It states two things an integrator needs and one it must not be misled about:
    the schemas above are enforced, the owner is named, and a deployment that has
    not composed the handler still answers ``501`` — so "served" is never read as
    "served everywhere unconditionally".
    """

    return (
        f"Implemented by {owner}. The request and response schemas above are the "
        "ones the live handler enforces, not placeholders. A deployment that has "
        f"not composed {owner}'s handler returns 501 naming it as the owner."
    )


def _operation_description(route: Route) -> str:
    refs = ", ".join(route.srs_refs)
    served = _served_description(route.served_by) if route.served_by else _PLACEHOLDER_DESCRIPTION
    parts = [route.summary, f"SRS trace: {refs}.", served]
    if route.requires_confirmation:
        parts.append("Requires UI-4 confirmation; pass ``confirm`` query param.")
    elif route.confirmation_actions:
        actions = ", ".join(sorted(route.confirmation_actions))
        parts.append(
            f"Action-level confirmation: the action(s) {actions} require the ``confirm`` "
            "token (query param or body field) and are refused 428 CONFIRMATION_REQUIRED "
            "without it; other actions on this route do not."
        )
    return " ".join(parts)


def _build_operation(route: Route) -> dict[str, Any]:
    operation_id_segments = [
        segment.strip("{}") for segment in route.path.strip("/").split("/") if segment
    ]
    operation_id = f"{route.method.value.lower()}_" + "_".join(operation_id_segments)
    operation: dict[str, Any] = {
        "operationId": operation_id,
        "summary": route.summary,
        "description": _operation_description(route),
        "tags": [route.capability.value],
        "x-srs-refs": list(route.srs_refs),
        "x-capability": route.capability.value,
        "x-requires-confirmation": route.requires_confirmation,
        "x-confirmation-actions": list(route.confirmation_actions),
        "responses": {
            "200": {
                "description": "Success",
                "content": {
                    "application/json": {
                        "schema": _placeholder_object_schema(
                            route.response_fields,
                            dict(route.field_types),
                            item_fields=dict(route.response_item_fields),
                        )
                    }
                },
            }
        },
    }
    parameters = _build_parameters(route)
    if parameters:
        operation["parameters"] = parameters
    request_body = _build_request_body(route)
    if request_body is not None:
        operation["requestBody"] = request_body
    return operation


def build_openapi(routes: Iterable[Route] = ROUTES) -> dict[str, Any]:
    """Build a deterministic OpenAPI 3.1 document for the given routes.

    Output is stable across runs: paths are emitted in route declaration
    order; methods within a path are emitted in the order routes were
    declared; tags and parameters are alphabetised.

    Example:
        >>> doc = build_openapi()
        >>> doc["info"]["title"]
        'ATP Operator REST API'
        >>> "/api/v1/kill-switch" in doc["paths"]
        True
    """

    paths: MutableMapping[str, dict[str, Any]] = {}
    tags_seen: set[str] = set()

    for route in routes:
        bucket = paths.setdefault(route.path, {})
        bucket[route.method.value.lower()] = _build_operation(route)
        tags_seen.add(route.capability.value)

    document: dict[str, Any] = {
        "openapi": OPENAPI_SPEC,
        "info": {
            "title": OPENAPI_TITLE,
            "version": OPENAPI_VERSION,
            "description": (
                "Operator REST surface for the ATP single-user trading platform "
                "(API-2, traces SRS-API-001). Bound to loopback by default; no "
                "RBAC or bearer tokens — see SRS-SEC-002."
            ),
        },
        "servers": [
            {
                "url": f"http://{BIND_HOST}:{_DEFAULT_PORT}",
                "description": (
                    f"Loopback bind ({AUTH_MODEL}). RFC 1918 binds are also "
                    "permitted; see SRS-SEC-002."
                ),
            }
        ],
        "tags": [
            {"name": tag, "description": f"{tag} operator capability."} for tag in sorted(tags_seen)
        ],
        "paths": paths,
        "x-auth-model": AUTH_MODEL,
        "x-bind-host": BIND_HOST,
    }
    return document


def render_snapshot(routes: Iterable[Route] = ROUTES) -> str:
    """Render the OpenAPI document as the canonical snapshot string.

    The returned string is the byte-equal representation expected at
    ``python/atp_api/openapi.json`` (sorted keys, two-space indent, trailing
    newline).

    Example:
        >>> snapshot = render_snapshot()
        >>> snapshot.endswith("\\n")
        True
    """

    document = build_openapi(routes)
    return json.dumps(document, indent=2, sort_keys=True) + "\n"
