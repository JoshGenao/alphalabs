"""AsyncAPI 2.6 generator for the ATP WebSocket API contract.

The generator is intentionally pure-stdlib: it consumes the declarative
:data:`atp_ws.channels.EVENT_CHANNELS` and
:data:`atp_ws.channels.CLIENT_COMMANDS` tuples and produces a
deterministic AsyncAPI 2.6 ``dict``. The frozen snapshot at
``python/atp_ws/asyncapi.json`` is byte-compared against the regenerated
dict in ``tools/websocket_api_check.py``; the ``--update`` flag rewrites
the snapshot.

SRS trace
---------
``SRS-UI-001`` through ``SRS-UI-004`` (dashboard requirements that
consume the WebSocket fan-out). The generated document is contract
evidence only; runtime publishers are out of scope here.
"""

from __future__ import annotations

import json
from typing import Iterable, MutableMapping, Tuple

from .channels import (
    AUTH_MODEL,
    BIND_HOST,
    CLIENT_COMMANDS,
    EVENT_CHANNELS,
    WS_PATH,
    Channel,
    ClientCommand,
    Direction,
    EventChannel,
    MessageType,
)


ASYNCAPI_TITLE = "ATP Operator WebSocket API"
"""Document title surfaced under ``info.title`` in the AsyncAPI snapshot."""

ASYNCAPI_VERSION = "0.1.0"
"""Document version surfaced under ``info.version`` in the AsyncAPI snapshot."""

ASYNCAPI_SPEC = "2.6.0"
"""AsyncAPI specification version emitted by :func:`build_asyncapi`."""


_DEFAULT_PORT = 8080
_PLACEHOLDER_DESCRIPTION = (
    "Contract only. Concrete payload schemas land with the downstream "
    "feature that owns the publisher (EXE-1, ORCH-1, MD-1, RESV-1, "
    "LOG-1, NOTIF-1)."
)


def _placeholder_object_schema(field_names: Iterable[str]) -> dict:
    properties = {name: {"type": "string"} for name in field_names}
    return {
        "type": "object",
        "properties": properties,
        "additionalProperties": True,
    }


def _channel_path(name: Channel) -> str:
    return f"{WS_PATH}/{name.value.lower()}"


def _event_message(channel: EventChannel) -> dict:
    return {
        "name": f"{channel.name.value}_event",
        "title": channel.summary,
        "summary": channel.summary,
        "contentType": "application/json",
        "tags": [{"name": channel.name.value}],
        "x-srs-refs": list(channel.srs_refs),
        "x-refresh-seconds": channel.refresh_seconds,
        "x-direction": Direction.SERVER_TO_CLIENT.value,
        "payload": {
            "type": "object",
            "properties": {
                "type": {"const": MessageType.EVENT.value},
                "channel": {"const": channel.name.value},
                "data": _placeholder_object_schema(channel.payload_fields),
            },
            "required": ["type", "channel", "data"],
            "additionalProperties": True,
        },
    }


def _command_message(command: ClientCommand) -> dict:
    return {
        "name": f"{command.type.value}_command",
        "title": command.summary,
        "summary": command.summary,
        "contentType": "application/json",
        "tags": [{"name": command.type.value}],
        "x-srs-refs": list(command.srs_refs),
        "x-direction": Direction.CLIENT_TO_SERVER.value,
        "x-response-message": command.response_message.value,
        "payload": {
            "type": "object",
            "properties": {
                "type": {"const": command.type.value},
                **{
                    field_name: {"type": "string"}
                    for field_name in command.request_fields
                },
            },
            "required": ["type", *command.request_fields],
            "additionalProperties": True,
        },
    }


def _build_event_channel(channel: EventChannel) -> dict:
    description_parts = [
        channel.summary,
        f"SRS trace: {', '.join(channel.srs_refs)}.",
        _PLACEHOLDER_DESCRIPTION,
    ]
    if channel.refresh_seconds == 0:
        description_parts.append("Event-driven; no fixed refresh cadence.")
    else:
        description_parts.append(
            f"Periodic publish; refresh ≤ {channel.refresh_seconds}s "
            "(NFR-P2 ≤ 5s ceiling)."
        )

    return {
        "description": " ".join(description_parts),
        "x-srs-refs": list(channel.srs_refs),
        "x-refresh-seconds": channel.refresh_seconds,
        "x-requires-subscription": channel.requires_subscription,
        "subscribe": {
            "operationId": f"on_{channel.name.value.lower()}",
            "summary": channel.summary,
            "tags": [{"name": channel.name.value}],
            "message": _event_message(channel),
        },
    }


def _build_control_channel(commands: Tuple[ClientCommand, ...]) -> dict:
    return {
        "description": (
            "Multiplexed client-to-server control plane: SUBSCRIBE, "
            "UNSUBSCRIBE, and HEARTBEAT_PING. Server replies with ACK / "
            "HEARTBEAT_PONG / ERROR over the same socket."
        ),
        "x-direction": Direction.CLIENT_TO_SERVER.value,
        "publish": {
            "operationId": "client_command",
            "summary": "Submit a control-plane command.",
            "message": {
                "oneOf": [_command_message(command) for command in commands],
            },
        },
    }


def build_asyncapi(
    channels: Iterable[EventChannel] = EVENT_CHANNELS,
    commands: Iterable[ClientCommand] = CLIENT_COMMANDS,
) -> dict:
    """Build a deterministic AsyncAPI 2.6 document for the WebSocket API.

    Output is stable across runs: channels are emitted in declaration
    order; messages within a channel preserve declaration order; tags
    and document keys are sorted in :func:`render_snapshot`.

    Example:
        >>> doc = build_asyncapi()
        >>> doc["info"]["title"]
        'ATP Operator WebSocket API'
        >>> "/ws/v1/pnl" in doc["channels"]
        True
    """

    channels_tuple = tuple(channels)
    commands_tuple = tuple(commands)

    channel_map: MutableMapping[str, dict] = {}
    tags_seen: set[str] = set()

    for channel in channels_tuple:
        channel_map[_channel_path(channel.name)] = _build_event_channel(channel)
        tags_seen.add(channel.name.value)

    channel_map[f"{WS_PATH}/_control"] = _build_control_channel(commands_tuple)
    for command in commands_tuple:
        tags_seen.add(command.type.value)

    document: dict = {
        "asyncapi": ASYNCAPI_SPEC,
        "info": {
            "title": ASYNCAPI_TITLE,
            "version": ASYNCAPI_VERSION,
            "description": (
                "Operator WebSocket surface for the ATP single-user "
                "trading platform (API-3, traces SRS-UI-001 through "
                "SRS-UI-004). Bound to loopback by default; no RBAC or "
                "bearer tokens — see SRS-SEC-002."
            ),
        },
        "servers": {
            "loopback": {
                "url": f"ws://{BIND_HOST}:{_DEFAULT_PORT}{WS_PATH}",
                "protocol": "ws",
                "description": (
                    f"Loopback bind ({AUTH_MODEL}). RFC 1918 binds are "
                    "also permitted; see SRS-SEC-002."
                ),
            }
        },
        "tags": [
            {"name": tag, "description": f"{tag} channel."}
            for tag in sorted(tags_seen)
        ],
        "channels": channel_map,
        "x-auth-model": AUTH_MODEL,
        "x-bind-host": BIND_HOST,
        "x-ws-path": WS_PATH,
    }
    return document


def render_snapshot(
    channels: Iterable[EventChannel] = EVENT_CHANNELS,
    commands: Iterable[ClientCommand] = CLIENT_COMMANDS,
) -> str:
    """Render the AsyncAPI document as the canonical snapshot string.

    The returned string is the byte-equal representation expected at
    ``python/atp_ws/asyncapi.json`` (sorted keys, two-space indent,
    trailing newline).

    Example:
        >>> snapshot = render_snapshot()
        >>> snapshot.endswith("\\n")
        True
    """

    document = build_asyncapi(channels, commands)
    return json.dumps(document, indent=2, sort_keys=True) + "\n"
