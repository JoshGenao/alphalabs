"""SRS-EXE-001 — the strategy side of the live execution host's order path.

A live strategy's ``StrategyContext.order`` reaches IB through exactly one door: the
Rust live execution host (``live_execution_host``), over the strategy's OWN Unix
socket ``<socket_dir>/<strategy_id>/order.sock``. The host decides, per order, whether
this strategy is the single designated live strategy; this module only speaks its
protocol (``ATP-LIVE-HOST/1``, mirrored from
``crates/atp-orchestrator/src/live_host/protocol.rs``) and turns each reply into the
SRS-SDK-004 ``OrderEvent`` the strategy's ``on_order_event`` receives.

Three pieces:

* :func:`encode_submit` / :func:`parse_reply` — the wire. The encoder refuses anything
  the host's parser would refuse (a control character, an empty value, a price that is
  not a whole number of minor units), so a strategy learns about a bad order HERE,
  with the field named, instead of from a ``refused`` reply.
* :class:`LiveHostClient` — one connection to one socket, one request and one reply at
  a time. It never retries a submit: once a frame has been written, a missing or
  unreadable reply means the order MAY exist at IB, which is
  :class:`LiveOrderOutcomeUnknown`, never "it failed".
* :class:`LiveOrderRouter` — the ``order()`` leg a live ``StrategyContext`` delegates
  to: warm-up and asset-class guards first (the same guards every context must run),
  then the host, then one queued ``ACK`` or ``REJECTED`` event that
  :meth:`LiveOrderRouter.deliver_pending` hands to ``on_order_event`` through the
  existing :func:`atp_strategy.dispatch.deliver_order_event`. Delivery is queued, not
  re-entrant: the API contract says events arrive asynchronously, and calling
  ``on_order_event`` from inside ``order()`` would let a callback that orders again
  recurse.

Latency (NFR-P1, MVP: host monotonic clock). Each delivered event yields one sample,
``perf_counter_ns`` at ``order()`` entry to the callback, which is exactly the span the
acceptance criterion names ("strategy API invocation to strategy acknowledgement
callback"). :attr:`LiveOrderRouter.latency_samples_ns` holds them for the caller.

Scope. This is the order leg, not a whole live ``StrategyContext``: subscriptions,
history, state, cancel, and the process that hosts a user strategy in its container are
other features' surfaces. The host has no cancel frame yet, so there is nothing here
to send one with.
"""

from __future__ import annotations

import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .api import (
    OrderEvent,
    OrderEventType,
    OrderHandle,
    OrderRequest,
    OrderType,
    StrategyAPIError,
    StrategyConfig,
    assert_asset_class,
)
from .dispatch import MINOR_UNITS_PER_UNIT, deliver_order_event
from .warmup import WarmupState, assert_warmup_complete

__all__ = [
    "MAX_FRAME_BYTES",
    "PROTOCOL_MAGIC",
    "SOCKET_FILE_NAME",
    "LiveAck",
    "LiveHostClient",
    "LiveHostProtocolError",
    "LiveOrderOutcomeUnknown",
    "LiveOrderRefused",
    "LiveOrderRouter",
    "LiveRefused",
    "LiveReject",
    "encode_submit",
    "parse_reply",
    "price_to_minor",
    "socket_path",
]

#: First field of every frame (``live_host/protocol.rs::PROTOCOL_MAGIC``).
PROTOCOL_MAGIC = "ATP-LIVE-HOST/1"

#: Longest frame either side accepts, excluding the newline
#: (``live_host/protocol.rs::MAX_FRAME_BYTES``).
MAX_FRAME_BYTES = 4096

#: The socket's file name inside the strategy's own directory
#: (``live_host/server.rs::SOCKET_FILE_NAME``).
SOCKET_FILE_NAME = "order.sock"

#: Default wait for one reply. The host holds the designation lock across the broker
#: call, whose IB transport deadline is 15 s, and may first wait up to 10 s for a
#: Hot-Swap to release that lock.
DEFAULT_REPLY_TIMEOUT_S = 30.0


class LiveHostProtocolError(StrategyAPIError):
    """The order could not be expressed on the wire, or the reply was not a reply.

    Raised BEFORE anything is sent when the request cannot be encoded, so no order
    exists. When raised for an unreadable reply, the caller sees
    :class:`LiveOrderOutcomeUnknown` instead, because by then a frame was sent.
    """


class LiveOrderRefused(StrategyAPIError):
    """The host answered ``refused``: no live order exists and none was attempted
    past the host's own checks (malformed frame, unreadable designation, outbox
    write-ahead failure, ...). ``error_type`` says which."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type
        self.message = message


class LiveOrderOutcomeUnknown(StrategyAPIError):
    """A submit frame was written but no valid reply came back.

    The order MAY be live at IB. Do not resubmit it: the host's outbox rejects the
    same correlation id, and a new id would risk a duplicate live order. Reconcile
    from broker state instead (SRS-EXE-009).
    """

    def __init__(self, correlation_id: str, detail: str) -> None:
        super().__init__(
            f"order {correlation_id}: the outcome is unknown ({detail}); it may be live "
            "at IB - reconcile before resubmitting"
        )
        self.correlation_id = correlation_id


# --------------------------------------------------------------------------- #
# Wire
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LiveAck:
    """The broker accepted the order; a live order exists."""

    correlation_id: str
    broker_order_id: str
    durable: bool
    tier: str


@dataclass(frozen=True, slots=True)
class LiveReject:
    """A structured order error (SRS-ERR-001 envelope); no live order exists."""

    correlation_id: str
    category: str
    error_type: str
    message: str
    durable: bool
    tier: str


@dataclass(frozen=True, slots=True)
class LiveRefused:
    """No live order exists and there is no structured order error to report."""

    correlation_id: str | None
    error_type: str
    message: str
    tier: str


LiveReply = LiveAck | LiveReject | LiveRefused


def socket_path(socket_dir: str | Path, strategy_id: str) -> Path:
    """``<socket_dir>/<strategy_id>/order.sock`` — the strategy's only door."""

    return Path(socket_dir) / strategy_id / SOCKET_FILE_NAME


def price_to_minor(field: str, price: float) -> int:
    """Convert a currency-unit price to integer minor units, EXACTLY.

    A price that is not a whole number of minor units (``190.005``) is refused
    rather than rounded: rounding would send IB a price the strategy did not write.
    Non-finite and non-positive prices are refused for the same reason the host's
    validator refuses them.
    """

    if isinstance(price, bool) or not isinstance(price, int | float):
        raise LiveHostProtocolError(f"{field} must be a number, got {type(price).__name__}")
    try:
        # str() gives the shortest repr that round-trips, so 190.1 is "190.1", not
        # its binary approximation.
        minor = Decimal(str(price)) * MINOR_UNITS_PER_UNIT
    except InvalidOperation as error:
        raise LiveHostProtocolError(f"{field} {price!r} is not a finite number") from error
    if not minor.is_finite():
        raise LiveHostProtocolError(f"{field} {price!r} is not a finite number")
    if minor != minor.to_integral_value():
        raise LiveHostProtocolError(
            f"{field} {price!r} is not a whole number of minor units "
            f"(1/{MINOR_UNITS_PER_UNIT}); refusing to round a live order's price"
        )
    if minor <= 0:
        raise LiveHostProtocolError(f"{field} {price!r} must be positive")
    return int(minor)


def _field_value(name: str, value: str) -> str:
    if not value:
        raise LiveHostProtocolError(f"{name} is empty")
    bad = next((c for c in value if ord(c) < 0x20 or ord(c) == 0x7F), None)
    if bad is not None:
        raise LiveHostProtocolError(
            f"{name} contains the control character U+{ord(bad):04X}, which would break the frame"
        )
    return value


def encode_submit(correlation_id: str, request: OrderRequest) -> str:
    """Encode one ``submit`` frame (without the trailing newline).

    The strategy id is NOT on the frame: the host knows who is speaking from the
    socket the connection arrived on.
    """

    if isinstance(request.quantity, bool) or not isinstance(request.quantity, int):
        raise LiveHostProtocolError(
            f"quantity must be an int, got {type(request.quantity).__name__}"
        )
    fields = [
        PROTOCOL_MAGIC,
        "submit",
        f"correlation_id={_field_value('correlation_id', correlation_id)}",
        f"symbol={_field_value('symbol', request.symbol)}",
        f"side={request.side.value}",
        f"quantity={request.quantity}",
        f"asset_class={request.asset_class.value}",
        f"order_type={request.order_type.value}",
    ]
    wants_limit = request.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT)
    wants_stop = request.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
    for name, price, wanted in (
        ("limit_price", request.limit_price, wants_limit),
        ("stop_price", request.stop_price, wants_stop),
    ):
        if wanted and price is None:
            raise LiveHostProtocolError(f"{request.order_type.value} order needs {name}")
        if not wanted and price is not None:
            raise LiveHostProtocolError(
                f"{request.order_type.value} order does not take {name}; refusing to drop it"
            )
        if wanted:
            fields.append(f"{name}_minor={price_to_minor(name, price)}")
    frame = "\t".join(fields)
    if len(frame.encode()) > MAX_FRAME_BYTES:
        raise LiveHostProtocolError(f"frame is longer than {MAX_FRAME_BYTES} bytes")
    return frame


_REPLY_FIELDS = {
    "ack": ("correlation_id", "broker_order_id", "durable", "tier"),
    "reject": ("correlation_id", "category", "error_type", "message", "durable", "tier"),
    "refused": ("error_type", "message", "tier"),
}
_OPTIONAL_REPLY_FIELDS = {"refused": ("correlation_id",)}
_TIERS = frozenset({"FIXTURE", "LIVE_IB"})


def _parse_bool(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise LiveHostProtocolError(f"durable={value!r} is not true or false")


def parse_reply(line: str) -> LiveReply:
    """Parse one reply frame (without its newline), failing closed.

    A reply with the wrong magic, an unknown outcome, a missing, repeated, or
    unknown field, or an unknown tier is not a reply.
    """

    parts = line.split("\t")
    if parts[0] != PROTOCOL_MAGIC:
        raise LiveHostProtocolError(f"reply does not start with {PROTOCOL_MAGIC}: {line!r}")
    if len(parts) < 2 or parts[1] not in _REPLY_FIELDS:
        raise LiveHostProtocolError(f"unknown reply outcome in {line!r}")
    outcome = parts[1]
    allowed = set(_REPLY_FIELDS[outcome]) | set(_OPTIONAL_REPLY_FIELDS.get(outcome, ()))
    values: dict[str, str] = {}
    for part in parts[2:]:
        key, sep, value = part.partition("=")
        if not sep or key not in allowed:
            raise LiveHostProtocolError(f"unexpected reply field {part!r}")
        if key in values:
            raise LiveHostProtocolError(f"reply field {key!r} appears twice")
        if not value:
            raise LiveHostProtocolError(f"reply field {key!r} is empty")
        values[key] = value
    missing = [key for key in _REPLY_FIELDS[outcome] if key not in values]
    if missing:
        raise LiveHostProtocolError(f"{outcome} reply is missing {missing}")
    if values["tier"] not in _TIERS:
        raise LiveHostProtocolError(f"unknown tier {values['tier']!r}")
    if outcome == "ack":
        return LiveAck(
            correlation_id=values["correlation_id"],
            broker_order_id=values["broker_order_id"],
            durable=_parse_bool(values["durable"]),
            tier=values["tier"],
        )
    if outcome == "reject":
        return LiveReject(
            correlation_id=values["correlation_id"],
            category=values["category"],
            error_type=values["error_type"],
            message=values["message"],
            durable=_parse_bool(values["durable"]),
            tier=values["tier"],
        )
    return LiveRefused(
        correlation_id=values.get("correlation_id"),
        error_type=values["error_type"],
        message=values["message"],
        tier=values["tier"],
    )


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class LiveHostClient:
    """One strategy's connection to its socket on the live execution host.

    Not thread-safe: a strategy submits one order at a time, and the host serves a
    connection's frames in order. A broken connection is re-opened on the NEXT
    submit; the submit that found it broken is never re-sent (see
    :class:`LiveOrderOutcomeUnknown`).
    """

    def __init__(
        self, socket_file: str | Path, *, reply_timeout_s: float = DEFAULT_REPLY_TIMEOUT_S
    ) -> None:
        if reply_timeout_s <= 0:
            raise ValueError("reply_timeout_s must be positive")
        self._socket_file = str(socket_file)
        self._timeout = reply_timeout_s
        self._conn: socket.socket | None = None
        self._buffer = b""

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self._buffer = b""

    def __enter__(self) -> LiveHostClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def submit(self, correlation_id: str, request: OrderRequest) -> LiveReply:
        """Send one order and return the host's reply.

        Raises :class:`LiveHostProtocolError` (nothing sent) if the order cannot be
        encoded or the socket cannot be opened, and :class:`LiveOrderOutcomeUnknown`
        if the frame may have been sent but no valid reply arrived.
        """

        frame = (encode_submit(correlation_id, request) + "\n").encode()
        if self._conn is None:
            try:
                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn.settimeout(self._timeout)
                conn.connect(self._socket_file)
            except OSError as error:
                raise LiveHostProtocolError(
                    f"cannot reach the live execution host at {self._socket_file}: {error}"
                ) from error
            self._conn = conn
        try:
            self._conn.sendall(frame)
            line = self._read_line()
        except (OSError, LiveHostProtocolError) as error:
            self.close()
            raise LiveOrderOutcomeUnknown(correlation_id, str(error)) from error
        try:
            reply = parse_reply(line)
        except LiveHostProtocolError as error:
            self.close()
            raise LiveOrderOutcomeUnknown(correlation_id, str(error)) from error
        if reply.correlation_id is not None and reply.correlation_id != correlation_id:
            self.close()
            raise LiveOrderOutcomeUnknown(
                correlation_id,
                f"the reply is for {reply.correlation_id!r}, not this order",
            )
        return reply

    def _read_line(self) -> str:
        assert self._conn is not None
        while b"\n" not in self._buffer:
            if len(self._buffer) > MAX_FRAME_BYTES:
                raise LiveHostProtocolError(f"reply exceeds {MAX_FRAME_BYTES} bytes")
            chunk = self._conn.recv(MAX_FRAME_BYTES + 1)
            if not chunk:
                raise LiveHostProtocolError("the host closed the connection without replying")
            self._buffer += chunk
        raw, _, self._buffer = self._buffer.partition(b"\n")
        if len(raw) > MAX_FRAME_BYTES:
            raise LiveHostProtocolError(f"reply exceeds {MAX_FRAME_BYTES} bytes")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise LiveHostProtocolError("reply is not UTF-8") from error


# --------------------------------------------------------------------------- #
# The order() leg of a live StrategyContext
# --------------------------------------------------------------------------- #


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class _Pending:
    event: OrderEvent
    submitted_at_ns: int


class LiveOrderRouter:
    """Route ``StrategyContext.order`` through the live execution host.

    ``order()`` runs the warm-up and asset-class guards, sends the order, and queues
    exactly one ``ACK`` (broker accepted) or ``REJECTED`` (structured order error)
    event. A ``refused`` reply raises :class:`LiveOrderRefused` from ``order()``
    itself: there is no order to report events about. :meth:`deliver_pending` hands
    queued events to ``strategy.on_order_event`` and records one latency sample each.
    """

    def __init__(
        self,
        *,
        client: LiveHostClient,
        strategy: object,
        context: object,
        config: StrategyConfig,
        warmup_state: Callable[[], WarmupState | None],
        clock: Callable[[], int] = time.perf_counter_ns,
        correlation_ids: Callable[[], str] = lambda: f"c-{uuid.uuid4().hex}",
        timestamps: Callable[[], str] = _utc_now_iso,
    ) -> None:
        self._client = client
        self._strategy = strategy
        self._context = context
        self._config = config
        self._warmup_state = warmup_state
        self._clock = clock
        self._correlation_ids = correlation_ids
        self._timestamps = timestamps
        self._pending: list[_Pending] = []
        self.latency_samples_ns: list[int] = []

    def order(self, request: OrderRequest) -> OrderHandle:
        submitted_at_ns = self._clock()
        assert_warmup_complete(self._warmup_state())
        assert_asset_class(self._config, request)
        correlation_id = request.client_order_id or self._correlation_ids()
        reply = self._client.submit(correlation_id, request)
        strategy_id = self._config.strategy_id
        if isinstance(reply, LiveRefused):
            raise LiveOrderRefused(reply.error_type, reply.message)
        if isinstance(reply, LiveAck):
            order_id = reply.broker_order_id
            event = OrderEvent(
                event_type=OrderEventType.ACK,
                order_id=order_id,
                client_order_id=correlation_id,
                strategy_id=strategy_id,
                symbol=request.symbol,
                fill_price=None,
                fill_quantity=None,
                cumulative_filled=0,
                remaining_quantity=request.quantity,
                commission=None,
                # A live order whose acknowledgement is not durable is still live; say
                # so rather than drop the fact (the host already logged it).
                reason=None if reply.durable else "acknowledgement not durably recorded",
                timestamp=self._timestamps(),
            )
        else:
            # No broker id exists for a rejected order; the correlation id is its only
            # identity, so it is both the handle and the event's order_id.
            order_id = correlation_id
            event = OrderEvent(
                event_type=OrderEventType.REJECTED,
                order_id=order_id,
                client_order_id=correlation_id,
                strategy_id=strategy_id,
                symbol=request.symbol,
                fill_price=0.0,
                fill_quantity=0,
                cumulative_filled=0,
                remaining_quantity=request.quantity,
                commission=0.0,
                reason=f"{reply.category}/{reply.error_type}: {reply.message}",
                timestamp=self._timestamps(),
            )
        self._pending.append(_Pending(event, submitted_at_ns))
        return OrderHandle(order_id=order_id, strategy_id=strategy_id)

    def deliver_pending(self) -> int:
        """Deliver every queued event in submission order; return how many."""

        delivered = 0
        while self._pending:
            pending = self._pending.pop(0)
            sample = deliver_order_event(
                self._strategy,
                self._context,
                pending.event,
                fill_at_ns=pending.submitted_at_ns,
                clock=self._clock,
            )
            self.latency_samples_ns.append(sample)
            delivered += 1
        return delivered
