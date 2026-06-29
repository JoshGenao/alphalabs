"""WebSocket control-plane protocol over the declared event channels.

The runtime multiplexes every event channel declared in :mod:`atp_ws` over a
single socket (``WS_PATH``). This module implements the control plane that
:mod:`atp_runtime.ws_frames` carries:

* a client sends ``SUBSCRIBE`` / ``UNSUBSCRIBE`` / ``HEARTBEAT_PING`` messages,
* the server replies ``ACK`` / ``HEARTBEAT_PONG`` / ``ERROR``,
* downstream publishers fan an ``EVENT`` out to every subscribed session via
  :class:`WsHub`.

The protocol is transport-agnostic: a :class:`WsSession` is constructed with a
``send`` callback (``bytes -> None``) so it can be unit-tested by appending to a
list, and driven by a real socket in production. Channel *publishers* (P&L,
metrics, logs, ...) are owned by downstream features; until they call
:meth:`WsHub.publish`, the channels are subscribable but silent.

SRS trace
---------
``SRS-API-001`` (API-3 WebSocket surface), ``SRS-UI-001`` (dashboard
subscriber), ``NFR-P2`` (the declared refresh ceiling lives on each channel).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable

from atp_ws import EVENT_CHANNELS, MessageType

from .ws_frames import encode_text_frame

SendFn = Callable[[bytes], None]

#: Valid channel names a client may SUBSCRIBE to (the declared event channels).
VALID_CHANNELS: frozenset[str] = frozenset(channel.name.value for channel in EVENT_CHANNELS)


class WsSession:
    """One client connection's subscription state and control-message handler.

    Thread-safety: :meth:`handle_text` (connection-read thread) and
    :meth:`deliver` (publisher thread) both touch ``_channels`` and call
    ``send``; a lock serialises them so a publish interleaving a subscribe is
    consistent and frames are never written half-overlapped.

    Example:
        >>> sent = []
        >>> s = WsSession(sent.append)
        >>> s.handle_text('{"type": "SUBSCRIBE", "channels": ["PNL"]}')
        >>> s.is_subscribed("PNL")
        True
    """

    def __init__(self, send: SendFn) -> None:
        self._send = send
        self._channels: set[str] = set()
        self._lock = threading.Lock()

    def is_subscribed(self, channel: str) -> bool:
        """Return whether this session is subscribed to ``channel``."""

        with self._lock:
            return channel in self._channels

    def subscriptions(self) -> frozenset[str]:
        """Return a snapshot of this session's current subscriptions."""

        with self._lock:
            return frozenset(self._channels)

    def handle_text(self, text: str) -> None:
        """Process one inbound control message and push any reply frames."""

        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            self._emit({"type": MessageType.ERROR.value, "error": "malformed JSON"})
            return
        if not isinstance(message, dict):
            self._emit({"type": MessageType.ERROR.value, "error": "message must be an object"})
            return

        msg_type = message.get("type")
        if msg_type == MessageType.SUBSCRIBE.value:
            self._handle_subscribe(message.get("channels", []))
        elif msg_type == MessageType.UNSUBSCRIBE.value:
            self._handle_unsubscribe(message.get("channels", []))
        elif msg_type == MessageType.HEARTBEAT_PING.value:
            self._emit({"type": MessageType.HEARTBEAT_PONG.value})
        else:
            self._emit(
                {
                    "type": MessageType.ERROR.value,
                    "error": f"unsupported message type {msg_type!r}",
                }
            )

    def deliver(self, channel: str, data: object) -> bool:
        """Push an ``EVENT`` frame if subscribed to ``channel``; return delivered.

        The frame matches the declared AsyncAPI EVENT envelope
        ``{type, channel, data}`` (API-3) so a client generated from the
        published contract reads the event body from ``data``.

        The subscription check and the outbound frame are emitted under one
        lock hold, so a concurrent UNSUBSCRIBE cannot interleave between the
        check and the send and leak a stale event after the unsubscribe takes
        effect (publisher thread vs. connection-read thread).
        """

        with self._lock:
            if channel not in self._channels:
                return False
            self._emit({"type": MessageType.EVENT.value, "channel": channel, "data": data})
            return True

    def _handle_subscribe(self, channels: object) -> None:
        names = self._coerce_channel_list(channels)
        if names is None:
            return
        accepted, rejected = [], []
        for name in names:
            if name in VALID_CHANNELS:
                accepted.append(name)
            else:
                rejected.append(name)
        with self._lock:
            self._channels.update(accepted)
        self._emit(
            {
                "type": MessageType.ACK.value,
                "action": MessageType.SUBSCRIBE.value,
                "subscribed": sorted(accepted),
                "rejected": sorted(rejected),
            }
        )

    def _handle_unsubscribe(self, channels: object) -> None:
        names = self._coerce_channel_list(channels)
        if names is None:
            return
        with self._lock:
            self._channels.difference_update(names)
        self._emit(
            {
                "type": MessageType.ACK.value,
                "action": MessageType.UNSUBSCRIBE.value,
                "unsubscribed": sorted(set(names)),
            }
        )

    def _coerce_channel_list(self, channels: object) -> list[str] | None:
        if not isinstance(channels, list) or not all(isinstance(c, str) for c in channels):
            self._emit(
                {"type": MessageType.ERROR.value, "error": "channels must be a list of strings"}
            )
            return None
        return channels

    def _emit(self, message: dict) -> None:
        self._send(encode_text_frame(json.dumps(message, sort_keys=True)))


class WsHub:
    """Thread-safe registry of live sessions for channel fan-out.

    Downstream publishers call :meth:`publish`; the hub delivers an ``EVENT`` to
    every session currently subscribed to that channel and returns the delivery
    count. Registration/unregistration happen on connection open/close.

    Example:
        >>> hub = WsHub()
        >>> sent = []
        >>> s = WsSession(sent.append)
        >>> hub.register(s)
        >>> s.handle_text('{"type": "SUBSCRIBE", "channels": ["LOGS"]}')
        >>> hub.publish("LOGS", {"message": "hello"})
        1
    """

    def __init__(self) -> None:
        self._sessions: set[WsSession] = set()
        self._lock = threading.Lock()

    def register(self, session: WsSession) -> None:
        with self._lock:
            self._sessions.add(session)

    def unregister(self, session: WsSession) -> None:
        with self._lock:
            self._sessions.discard(session)

    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def publish(self, channel: str, payload: object) -> int:
        """Deliver an EVENT to every subscribed session; return delivery count.

        Raises ``ValueError`` if ``channel`` is not a declared event channel, so
        a publisher cannot silently fan out to a misspelled channel.
        """

        if channel not in VALID_CHANNELS:
            raise ValueError(f"unknown event channel {channel!r}")
        with self._lock:
            targets: Iterable[WsSession] = tuple(self._sessions)
        return sum(1 for session in targets if session.deliver(channel, payload))
