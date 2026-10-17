"""L1 — the strategy side of the SRS-EXE-001 live order path (atp_strategy.live_client).

The wire is checked for exactness (a live order's price is never rounded; anything the
host would refuse is refused here first), the reply parser for strictness, and the
router for ordering: guards before the host, one queued event per order, and a
latency sample per delivered callback. A fake one-shot socket server covers every way
a reply can be missing or wrong, all of which must surface as "outcome unknown", never
as "it failed".
"""

from __future__ import annotations

import socket
import tempfile
import threading
from pathlib import Path

import pytest
from atp_strategy.api import (
    AssetClass,
    AssetClassViolation,
    OrderEventType,
    OrderRequest,
    OrderSide,
    OrderType,
    StrategyConfig,
    WarmupNotComplete,
)
from atp_strategy.live_client import (
    PROTOCOL_MAGIC,
    LiveAck,
    LiveHostClient,
    LiveHostProtocolError,
    LiveOrderOutcomeUnknown,
    LiveOrderRefused,
    LiveOrderRouter,
    LiveRefused,
    LiveReject,
    encode_submit,
    parse_reply,
    price_to_minor,
    socket_path,
)
from atp_strategy.warmup import WarmupState

pytestmark = pytest.mark.unit

LIMIT = OrderRequest("AAPL", 2, OrderSide.BUY, OrderType.LIMIT, limit_price=190.1)


# --------------------------------------------------------------------------- #
# Wire
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("price", "minor"), [(190.1, 19010), (0.01, 1), (1, 100), (1234.56, 123456)]
)
def test_prices_convert_to_minor_units_exactly(price, minor) -> None:
    assert price_to_minor("limit_price", price) == minor


@pytest.mark.parametrize("price", [190.005, 0.001, 0, -1.0, float("nan"), float("inf"), True])
def test_prices_that_would_need_rounding_or_are_invalid_are_refused(price) -> None:
    with pytest.raises(LiveHostProtocolError):
        price_to_minor("limit_price", price)


def test_a_limit_order_encodes_to_the_host_frame() -> None:
    assert encode_submit("c-1", LIMIT) == (
        f"{PROTOCOL_MAGIC}\tsubmit\tcorrelation_id=c-1\tsymbol=AAPL\tside=BUY\tquantity=2"
        "\tasset_class=EQUITY\torder_type=LIMIT\tlimit_price_minor=19010"
    )


@pytest.mark.parametrize(
    "request_",
    [
        OrderRequest("AAPL", 1, OrderSide.BUY, OrderType.LIMIT),  # no limit price
        OrderRequest("AAPL", 1, OrderSide.BUY, OrderType.MARKET, limit_price=1.0),  # stray
        OrderRequest("AA\tPL", 1, OrderSide.BUY, OrderType.MARKET),  # breaks the frame
        OrderRequest("", 1, OrderSide.BUY, OrderType.MARKET),
        OrderRequest("AAPL", True, OrderSide.BUY, OrderType.MARKET),  # bool is not an int
        OrderRequest("AAPL", 0, OrderSide.BUY, OrderType.MARKET),
        OrderRequest("AAPL", -5, OrderSide.SELL, OrderType.MARKET),  # direction is `side`
        OrderRequest("   ", 1, OrderSide.BUY, OrderType.MARKET),
    ],
)
def test_orders_the_host_would_refuse_are_refused_before_sending(request_) -> None:
    with pytest.raises(LiveHostProtocolError):
        encode_submit("c-1", request_)


def test_replies_parse_into_their_typed_outcomes() -> None:
    m = PROTOCOL_MAGIC
    assert parse_reply(
        f"{m}\tack\tcorrelation_id=c-1\tbroker_order_id=IB-7\tdurable=true\ttier=FIXTURE"
    ) == LiveAck("c-1", "IB-7", True, "FIXTURE")
    rejected = parse_reply(
        f"{m}\treject\tcorrelation_id=c-1\tcategory=NON_LIVE_STRATEGY_SUBMISSION"
        "\terror_type=NotDesignatedLiveStrategy\tmessage=not live\tdurable=true\ttier=FIXTURE"
    )
    assert isinstance(rejected, LiveReject)
    assert rejected.category == "NON_LIVE_STRATEGY_SUBMISSION"
    refused = parse_reply(f"{m}\trefused\terror_type=ProtocolError\tmessage=x\ttier=FIXTURE")
    assert refused == LiveRefused(None, "ProtocolError", "x", "FIXTURE")


@pytest.mark.parametrize(
    "line",
    [
        "ATP-LIVE-HOST/2\tack\tcorrelation_id=c\tbroker_order_id=b\tdurable=true\ttier=FIXTURE",
        f"{PROTOCOL_MAGIC}\tfilled\tcorrelation_id=c",
        f"{PROTOCOL_MAGIC}\tack\tcorrelation_id=c\tdurable=true\ttier=FIXTURE",  # no broker id
        f"{PROTOCOL_MAGIC}\tack\tcorrelation_id=c\tbroker_order_id=b\tdurable=yes\ttier=FIXTURE",
        f"{PROTOCOL_MAGIC}\tack\tcorrelation_id=c\tbroker_order_id=b\tdurable=true\ttier=PAPER",
        f"{PROTOCOL_MAGIC}\tack\tcorrelation_id=c\tbroker_order_id=b\tbroker_order_id=d"
        "\tdurable=true\ttier=FIXTURE",
        f"{PROTOCOL_MAGIC}\tack\tcorrelation_id=c\tbroker_order_id=b\tdurable=true"
        "\ttier=FIXTURE\textra=1",
    ],
)
def test_anything_that_is_not_exactly_a_reply_is_refused(line) -> None:
    with pytest.raises(LiveHostProtocolError):
        parse_reply(line)


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #


class _ScriptedClient:
    def __init__(self, reply) -> None:
        self.reply = reply
        self.sent: list[tuple[str, OrderRequest]] = []

    def submit(self, correlation_id, request):
        self.sent.append((correlation_id, request))
        return self.reply


class _Strategy:
    def __init__(self) -> None:
        self.events = []

    def on_order_event(self, context, event) -> None:
        self.events.append(event)


def _router(reply, *, warmup=WarmupState.COMPLETE, asset=AssetClass.EQUITY):
    client = _ScriptedClient(reply)
    strategy = _Strategy()
    router = LiveOrderRouter(
        client=client,
        strategy=strategy,
        context=None,
        config=StrategyConfig("live-a", asset),
        warmup_state=lambda: warmup,
        correlation_ids=lambda: "c-gen",
    )
    return router, client, strategy


def test_an_ack_queues_one_ack_event_and_records_one_latency_sample() -> None:
    router, client, strategy = _router(LiveAck("c-gen", "IB-9", True, "FIXTURE"))
    handle = router.order(LIMIT)
    assert handle.order_id == "IB-9" and handle.strategy_id == "live-a"
    assert strategy.events == [], "delivery is queued, never re-entrant inside order()"
    assert router.deliver_pending() == 1
    (event,) = strategy.events
    assert event.event_type is OrderEventType.ACK
    assert (event.order_id, event.client_order_id) == ("IB-9", "c-gen")
    assert event.remaining_quantity == 2 and event.reason is None
    assert len(router.latency_samples_ns) == 1 and router.latency_samples_ns[0] >= 0
    assert router.deliver_pending() == 0


def test_a_non_durable_ack_is_still_an_ack_and_says_so() -> None:
    router, _, strategy = _router(LiveAck("c-gen", "IB-9", False, "FIXTURE"))
    router.order(LIMIT)
    router.deliver_pending()
    assert strategy.events[0].event_type is OrderEventType.ACK
    assert "not durably recorded" in strategy.events[0].reason


def test_a_reject_becomes_a_rejected_event_with_the_structured_reason() -> None:
    reply = LiveReject(
        "c-gen",
        "NON_LIVE_STRATEGY_SUBMISSION",
        "NotDesignatedLiveStrategy",
        "nope",
        True,
        "FIXTURE",
    )
    router, _, strategy = _router(reply)
    handle = router.order(LIMIT)
    assert handle.order_id == "c-gen"
    router.deliver_pending()
    (event,) = strategy.events
    assert event.event_type is OrderEventType.REJECTED
    assert event.reason.startswith("NON_LIVE_STRATEGY_SUBMISSION/NotDesignatedLiveStrategy")
    assert (event.fill_price, event.fill_quantity, event.commission) == (0.0, 0, 0.0)


def test_a_refusal_raises_from_order_and_queues_nothing() -> None:
    router, _, strategy = _router(LiveRefused(None, "DesignationUnreadable", "x", "FIXTURE"))
    with pytest.raises(LiveOrderRefused) as caught:
        router.order(LIMIT)
    assert caught.value.error_type == "DesignationUnreadable"
    assert router.deliver_pending() == 0 and strategy.events == []


def test_the_client_order_id_is_used_as_the_correlation_id_when_given() -> None:
    router, client, _ = _router(LiveAck("mine-1", "IB-1", True, "FIXTURE"))
    router.order(OrderRequest("AAPL", 1, OrderSide.BUY, OrderType.MARKET, client_order_id="mine-1"))
    assert client.sent[0][0] == "mine-1"


def test_a_malformed_quantity_raises_from_order_before_anything_is_sent() -> None:
    # Sent anyway, the host would reject it and the REJECTED event would carry a negative
    # remaining_quantity that the SDK's own payload guard refuses to deliver. The REAL
    # client encodes before it connects, so a socket that does not exist proves the
    # refusal happened first (the message is the quantity's, not the connection's).
    missing = Path(tempfile.mkdtemp(prefix="lc-")) / "order.sock"
    strategy = _Strategy()
    router = LiveOrderRouter(
        client=LiveHostClient(missing, reply_timeout_s=1),
        strategy=strategy,
        context=None,
        config=StrategyConfig("live-a", AssetClass.EQUITY),
        warmup_state=lambda: WarmupState.COMPLETE,
    )
    with pytest.raises(LiveHostProtocolError, match="quantity must be positive"):
        router.order(OrderRequest("AAPL", -1, OrderSide.SELL, OrderType.MARKET))
    assert router.deliver_pending() == 0 and strategy.events == []


def test_warmup_and_asset_class_guards_run_before_anything_is_sent() -> None:
    router, client, _ = _router(None, warmup=WarmupState.IN_PROGRESS)
    with pytest.raises(WarmupNotComplete):
        router.order(LIMIT)
    router, client2, _ = _router(None, asset=AssetClass.OPTION)
    with pytest.raises(AssetClassViolation):
        router.order(LIMIT)
    assert client.sent == [] and client2.sent == []


# --------------------------------------------------------------------------- #
# Client over a real Unix socket
# --------------------------------------------------------------------------- #


def _one_shot_server(reply: bytes | None):
    """Serve ONE connection: read a line, then send ``reply`` (None = just close)."""

    directory = Path(tempfile.mkdtemp(prefix="lc-"))
    path = directory / "order.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def serve() -> None:
        conn, _ = listener.accept()
        with conn:
            conn.makefile("rb").readline()
            if reply is not None:
                conn.sendall(reply)
        listener.close()

    threading.Thread(target=serve, daemon=True).start()
    return path


def test_a_correct_reply_round_trips_over_the_socket() -> None:
    path = _one_shot_server(
        f"{PROTOCOL_MAGIC}\tack\tcorrelation_id=c-1\tbroker_order_id=IB-1\tdurable=true"
        "\ttier=FIXTURE\n".encode()
    )
    with LiveHostClient(path, reply_timeout_s=5) as client:
        assert client.submit("c-1", LIMIT) == LiveAck("c-1", "IB-1", True, "FIXTURE")


@pytest.mark.parametrize(
    "reply",
    [
        None,  # the host closed without replying
        b"garbage\n",
        f"{PROTOCOL_MAGIC}\tack\tcorrelation_id=OTHER\tbroker_order_id=IB-1\tdurable=true"
        "\ttier=FIXTURE\n".encode(),  # someone else's reply
        b"A" * 9000,  # an unterminated oversize reply
        b"\xff\xfe\n",  # not UTF-8
    ],
)
def test_a_sent_order_without_a_valid_reply_has_an_unknown_outcome(reply) -> None:
    path = _one_shot_server(reply)
    with LiveHostClient(path, reply_timeout_s=5) as client:
        with pytest.raises(LiveOrderOutcomeUnknown):
            client.submit("c-1", LIMIT)


def test_an_unreachable_host_is_a_protocol_error_because_nothing_was_sent() -> None:
    missing = Path(tempfile.mkdtemp(prefix="lc-")) / "order.sock"
    with pytest.raises(LiveHostProtocolError) as caught:
        LiveHostClient(missing, reply_timeout_s=1).submit("c-1", LIMIT)
    assert not isinstance(caught.value, LiveOrderOutcomeUnknown)


@pytest.mark.parametrize(
    "strategy_id",
    ["paper-1/../live-a", "../live-a", "/abs", ".hidden", "", "a b", "x" * 65, "a\x00b"],
)
def test_a_strategy_id_that_could_name_another_socket_is_refused(strategy_id) -> None:
    # The socket is the identity: a traversal id would reach another strategy's socket.
    with pytest.raises(LiveHostProtocolError):
        socket_path("/run/atp/live", strategy_id)


def test_a_valid_strategy_id_maps_to_its_own_directory() -> None:
    assert socket_path("/run/atp/live", "paper-1") == Path("/run/atp/live/paper-1/order.sock")
