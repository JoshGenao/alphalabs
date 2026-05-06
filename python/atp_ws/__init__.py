"""ATP WebSocket API contract surface (API-3 / SRS-UI-001..004).

This package exposes the declarative WebSocket API contract used to
verify ``API-3`` in ``feature_list.json``. It contains no WebSocket
runtime; concrete publishers and an asyncio runtime arrive with
downstream features.

See ``python/atp_ws/README.md`` for the operator-facing summary.
"""

from .asyncapi import (
    ASYNCAPI_SPEC,
    ASYNCAPI_TITLE,
    ASYNCAPI_VERSION,
    build_asyncapi,
    render_snapshot,
)
from .channels import (
    AUTH_MODEL,
    BIND_HOST,
    CLIENT_COMMANDS,
    EVENT_CHANNELS,
    MAX_REFRESH_SECONDS,
    WS_PATH,
    Channel,
    ClientCommand,
    Direction,
    EventChannel,
    MessageType,
    channels_by_name,
    commands_by_type,
)


__all__ = [
    "ASYNCAPI_SPEC",
    "ASYNCAPI_TITLE",
    "ASYNCAPI_VERSION",
    "AUTH_MODEL",
    "BIND_HOST",
    "CLIENT_COMMANDS",
    "Channel",
    "ClientCommand",
    "Direction",
    "EVENT_CHANNELS",
    "EventChannel",
    "MAX_REFRESH_SECONDS",
    "MessageType",
    "WS_PATH",
    "build_asyncapi",
    "channels_by_name",
    "commands_by_type",
    "render_snapshot",
]
