"""L7 domain safety test for the SRS-LOG-001 OPERATOR surfaces.

``tests/domain/test_log_persistence.py`` drives the storage layer: separation,
durability, torn-tail tolerance, fail-closed corruption. This file drives the
three surfaces an operator actually looks at — the ``GET /api/v1/logs`` REST
handler, the ``admin logs`` CLI, the ``LOGS`` WebSocket publisher, and the
dashboard pane provider — and asserts the invariants that make an audit trail
trustworthy *as displayed*:

* **Separation survives the read.** A query for one class can never return a
  record of the other, on any surface. The storage layer keeping two files
  apart is worthless if the read surface merges them.
* **Unreadable is never rendered as empty.** A corrupt store fails closed on
  every surface with an explicit error. ``{"events": []}`` for a store that
  could not be read is indistinguishable from "nothing happened" — the exact
  misreading an audit log exists to prevent.
* **The publisher never fabricates and never silently drops.** Every published
  event corresponds to a record read back from a store; a read failure or an
  eviction that costs the publisher its place is surfaced on its health, not
  swallowed into silence.
* **The pane never overstates coverage.** Five of the eight SyRS SYS-61 system
  sources have no producer anywhere in the tree, so the pane must say so; a
  pane that showed three kinds of event without that context would read as
  "the system emitted three kinds of event".

Scope: these tests exercise the operator READ path over an in-process store.
The core-runtime event FORWARDING path (how Rust-owned system events reach this
operator store) stays deferred — see ``log_persistence_contract.deferred``.

Marked ``safety`` + ``domain``: the surfaces under test read kill-switch
activations, IB Gateway connectivity transitions, and the rest of the
safety-relevant system trail.
"""

from __future__ import annotations

import inspect
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_dashboard.logs import SOURCE_COVERAGE, LogPaneProvider  # noqa: E402
from atp_logging.persistence import (  # noqa: E402
    JsonlLogStore,
    build_separated_log_dispatcher,
    iter_records,
    read_records,
    read_records_bounded,
)
from atp_logging.records import (  # noqa: E402
    EVENT_TYPES_BY_SOURCE,
    SYSTEM_SOURCES,
    LogClass,
    LogRecord,
    Severity,
    Source,
)
from atp_logs_service import LogEventPublisher, LogsQueryHandler  # noqa: E402
from atp_logs_service import publisher as publisher_module  # noqa: E402
from atp_runtime.errors import InterfaceError  # noqa: E402
from atp_runtime.registry import OperationKey, Request, Surface  # noqa: E402

pytestmark = [pytest.mark.safety, pytest.mark.domain]

_BASE_NS = 1_700_000_000_000_000_000


def _system_record(**overrides: object) -> LogRecord:
    fields: dict[str, object] = {
        "timestamp_ns": _BASE_NS,
        "severity": Severity.CRITICAL,
        "source": Source.KILL_SWITCH,
        "event_type": "ACTIVATION",
        "message": "operator triggered kill switch",
        "correlation_id": "ks-001",
        "log_class": LogClass.SYSTEM,
        "strategy_id": None,
    }
    fields.update(overrides)
    return LogRecord(**fields)  # type: ignore[arg-type]


def _strategy_record(**overrides: object) -> LogRecord:
    fields: dict[str, object] = {
        "timestamp_ns": _BASE_NS + 1,
        "severity": Severity.INFO,
        "source": Source.STRATEGY,
        "event_type": "SIGNAL",
        "message": "long AAPL",
        "correlation_id": "run-9",
        "log_class": LogClass.STRATEGY,
        "strategy_id": "sma-crossover",
    }
    fields.update(overrides)
    return LogRecord(**fields)  # type: ignore[arg-type]


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    """Write one record of each class through the separated dispatcher."""

    dispatcher, _system, _strategy = build_separated_log_dispatcher(tmp_path)
    dispatcher.dispatch(_system_record())
    dispatcher.dispatch(_strategy_record())
    return tmp_path / "system.jsonl", tmp_path / "strategy.jsonl"


def _handler(system: Path, strategy: Path, **kwargs: object) -> LogsQueryHandler:
    return LogsQueryHandler(
        system_store_path=system,
        strategy_store_path=strategy,
        **kwargs,  # type: ignore[arg-type]
    )


def _rest(handler: LogsQueryHandler, **query: str) -> tuple[int, dict[str, object]]:
    request = Request(
        surface=Surface.REST,
        operation=OperationKey(Surface.REST, "GET /api/v1/logs"),
        method="GET",
        path="/api/v1/logs",
        query=dict(query),
    )
    result = handler.handle(request)
    return result.status_code, dict(result.body)


def _cli(handler: LogsQueryHandler, **query: str) -> tuple[int, dict[str, object]]:
    request = Request(
        surface=Surface.CLI,
        operation=OperationKey(Surface.CLI, "admin logs"),
        query=dict(query),
    )
    result = handler.handle(request)
    return result.status_code, dict(result.body)


# --------------------------------------------------------------------------- #
# Separation survives the read
# --------------------------------------------------------------------------- #


def test_query_never_crosses_the_class_boundary(tmp_path: Path) -> None:
    """Each class query returns ONLY that class, on both REST and CLI."""

    system, strategy = _seed(tmp_path)
    handler = _handler(system, strategy)

    for query, expected in (
        ({}, LogClass.SYSTEM),  # default is the CLI's declared default
        ({"log_class": "system"}, LogClass.SYSTEM),
        ({"log_class": "strategy"}, LogClass.STRATEGY),
    ):
        status, body = _rest(handler, **query)
        assert status == 200
        events = body["events"]
        assert isinstance(events, list) and events, f"{query} returned nothing"
        assert {event["log_class"] for event in events} == {expected.value}

    status, body = _cli(handler, log_class="strategy")
    assert status == 200
    assert {event["log_class"] for event in body["events"]} == {LogClass.STRATEGY.value}


def test_source_from_the_other_class_is_refused_not_empty(tmp_path: Path) -> None:
    """A contradictory filter is an error — an empty list would read as "none"."""

    system, strategy = _seed(tmp_path)
    handler = _handler(system, strategy)

    with pytest.raises(InterfaceError) as caught:
        _rest(handler, log_class="strategy", source="kill_switch")
    assert caught.value.status == 400
    assert caught.value.type == "LOGS_SOURCE_CLASS_MISMATCH"


def test_aliased_store_paths_are_refused_at_construction(tmp_path: Path) -> None:
    """Wiring both classes at one file would defeat separation — fail closed."""

    system, _strategy = _seed(tmp_path)
    for alias in (system, Path(str(system))):
        with pytest.raises(ValueError, match="separate persistent sinks"):
            _handler(system, alias)
        with pytest.raises(ValueError, match="separate persistent sinks"):
            LogPaneProvider(system_store_path=system, strategy_store_path=alias)


def test_strategy_lines_stay_attributable_on_every_surface(tmp_path: Path) -> None:
    """Two strategies, two lines — each must remain traceable to its emitter.

    ``source`` is the literal ``strategy`` on every strategy-class record, so
    the id is the ONLY thing that distinguishes one of the 30 Reservoir
    strategies from another. Dropping it anywhere between the store and the
    operator turns the strategy trail into an unattributable stream.
    """

    dispatcher, system_store, strategy_store = build_separated_log_dispatcher(tmp_path)
    for index, strategy_id in enumerate(("sma-crossover", "mean-reversion")):
        dispatcher.dispatch(
            _strategy_record(
                timestamp_ns=_BASE_NS + index,
                message=f"signal from {strategy_id}",
                correlation_id=f"run-{index}",
                strategy_id=strategy_id,
            )
        )
    dispatcher.dispatch(_system_record())
    system_store.close()
    strategy_store.close()

    system = tmp_path / "system.jsonl"
    strategy = tmp_path / "strategy.jsonl"

    def attribution(events: list[dict[str, object]]) -> set[tuple[object, object]]:
        return {(event["message"], event["strategy_id"]) for event in events}

    expected = {
        ("signal from sma-crossover", "sma-crossover"),
        ("signal from mean-reversion", "mean-reversion"),
    }

    handler = _handler(system, strategy)
    # REST
    _status, body = _rest(handler, log_class="strategy")
    assert attribution(body["events"]) == expected
    # CLI (same handler, CLI surface)
    _status, body = _cli(handler, log_class="strategy")
    assert attribution(body["events"]) == expected
    # Dashboard pane
    cell = LogPaneProvider(system_store_path=system, strategy_store_path=strategy).logs_snapshot()[
        "classes"
    ]["strategy"]
    assert attribution(cell["records"]) == expected
    # LOGS WebSocket channel
    fanout = _Fanout()
    publisher = _publisher(fanout, system, strategy)
    publisher.poll_once()
    published = [
        payload for _channel, payload in fanout.events if payload["log_class"] == "strategy"
    ]
    assert attribution(published) == expected

    # A SYSTEM record carries no strategy id — the record schema forbids one, so
    # the surfaces must report null rather than inventing an attribution.
    _status, body = _rest(handler, log_class="system")
    assert body["events"][0]["strategy_id"] is None


def test_a_contaminated_store_fails_closed_on_every_surface(tmp_path: Path) -> None:
    """A wrong-class record in a store is a broken invariant, not a filter case.

    The store refuses to WRITE one, so a file holding a strategy record in
    ``system.jsonl`` means separation was broken by something outside this API —
    legacy data, a hand-edited file, a bad recovery. Quietly filtering it out
    would hide that; publishing it would carry the breakage to every subscriber.
    Both are refused: the read fails closed, like corruption.
    """

    system, strategy = _seed(tmp_path)

    # Bypass the store's own write guard exactly as an external hand would.
    contaminant = _strategy_record(message="wrong trail", correlation_id="x-1")
    import json as _json

    from atp_logging.persistence import SCHEMA_VERSION_KEY, SEGMENT_SCHEMA_VERSION

    payload = dict(contaminant.as_dict())
    payload[SCHEMA_VERSION_KEY] = SEGMENT_SCHEMA_VERSION
    with system.open("a", encoding="utf-8") as handle:
        handle.write(_json.dumps(payload) + "\n")

    # REST: refuses, naming the breakage.
    with pytest.raises(InterfaceError) as caught:
        _rest(_handler(system, strategy), log_class="system")
    assert caught.value.status == 500
    assert caught.value.type == "LOGS_STORE_CLASS_MISMATCH"

    # Dashboard pane: an explicit unavailable cell, never a quietly filtered list.
    cell = LogPaneProvider(system_store_path=system, strategy_store_path=strategy).logs_snapshot()[
        "classes"
    ]["system"]
    assert cell["ok"] is False
    assert cell["records"] is None
    assert "separation is broken" in str(cell["error"])

    # LOGS channel: nothing from the contaminated trail is published, and the
    # failure is surfaced rather than the record being fanned out.
    fanout = _Fanout()
    publisher = _publisher(fanout, system, strategy)
    publisher.poll_once()
    published = {payload["log_class"] for _channel, payload in fanout.events}
    assert "wrong trail" not in {payload["message"] for _channel, payload in fanout.events}
    assert published == {"strategy"}, "the contaminated system trail published anyway"
    assert publisher.health()["ok"] is False
    assert publisher.health()["read_failures"] >= 1


# --------------------------------------------------------------------------- #
# Unreadable is never empty
# --------------------------------------------------------------------------- #


def _corrupt(path: Path) -> None:
    """Append a COMPLETE but unparseable line (not a torn tail)."""

    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json at all}\n")


def test_rest_fails_closed_on_a_corrupt_store(tmp_path: Path) -> None:
    system, strategy = _seed(tmp_path)
    _corrupt(system)
    handler = _handler(system, strategy)

    with pytest.raises(InterfaceError) as caught:
        _rest(handler, log_class="system")
    assert caught.value.status == 500
    assert caught.value.type == "LOGS_STORE_CORRUPT"
    # The still-readable class must remain readable: one corrupt trail does not
    # blank the other.
    status, body = _rest(handler, log_class="strategy")
    assert status == 200 and body["events"]


def test_cli_fails_closed_on_a_corrupt_store(tmp_path: Path) -> None:
    system, strategy = _seed(tmp_path)
    _corrupt(strategy)
    handler = _handler(system, strategy)

    with pytest.raises(InterfaceError) as caught:
        _cli(handler, log_class="strategy")
    assert caught.value.status == 500


def test_pane_renders_unreadable_as_null_never_empty_list(tmp_path: Path) -> None:
    """``records`` is None for an unreadable trail — never ``[]``."""

    system, strategy = _seed(tmp_path)
    _corrupt(system)
    snapshot = LogPaneProvider(
        system_store_path=system, strategy_store_path=strategy
    ).logs_snapshot()

    classes = snapshot["classes"]
    assert isinstance(classes, dict)
    system_cell = classes["system"]
    assert system_cell["ok"] is False
    assert system_cell["records"] is None, "an unreadable trail must not render as empty"
    assert system_cell["matched"] is None
    assert system_cell["error"]
    # The pane as a whole is unhealthy while EITHER trail is unreadable.
    assert snapshot["ok"] is False
    # ...and the healthy class still carries its real records.
    assert classes["strategy"]["ok"] is True
    assert [record["message"] for record in classes["strategy"]["records"]] == ["long AAPL"]


def test_pane_distinguishes_empty_trail_from_unreadable_one(tmp_path: Path) -> None:
    """A readable-but-empty store is ``[]`` with ok=True — a different state."""

    system, strategy = _seed(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    dispatcher, _s, _t = build_separated_log_dispatcher(empty)
    del dispatcher, system, strategy

    snapshot = LogPaneProvider(
        system_store_path=empty / "system.jsonl",
        strategy_store_path=empty / "strategy.jsonl",
    ).logs_snapshot()
    classes = snapshot["classes"]
    assert isinstance(classes, dict)
    assert snapshot["ok"] is True
    assert classes["system"]["records"] == []
    # The pane TAIL-reads (it polls), so it reports no total — inventing one
    # would be its own fabrication. `page_only` says which kind of answer it is;
    # the operator's full query reports the exact figure.
    assert classes["system"]["matched"] is None
    assert classes["system"]["page_only"] is True
    assert classes["system"]["error"] is None


# --------------------------------------------------------------------------- #
# The publisher: no fabrication, no silent gap
# --------------------------------------------------------------------------- #


class _Fanout:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []
        self.claims = 0
        self.releases = 0

    def publish(self, channel: str, payload: object) -> int:
        assert isinstance(payload, dict)
        self.events.append((channel, payload))
        return 1

    def claim(self) -> None:
        self.claims += 1

    def release(self) -> None:
        self.releases += 1


def _publisher(fanout: _Fanout, system: Path, strategy: Path) -> LogEventPublisher:
    return LogEventPublisher(
        publish=fanout.publish,
        claim_channel=fanout.claim,
        release_channel=fanout.release,
        system_store_path=system,
        strategy_store_path=strategy,
    )


def test_a_new_event_is_not_queued_behind_the_existing_history(tmp_path: Path) -> None:
    """A live channel must carry what happens NOW, not drain the archive first.

    The cursor starts at "nothing published", which on an append-only trail means
    "everything retained is new to me". A deployment with real audit history
    would then fan out old records ``max_events_per_poll`` at a time while a
    kill-switch activation written a second after startup waited behind all of
    them — and every health signal would stay green throughout: no read failure,
    no gap, events flowing. Delivering an alert far too late while reporting
    itself fine is the worst shape this surface can take, so ``start()`` anchors
    at the tail and the history stays where it can be paged: GET /api/v1/logs.
    """

    dispatcher, system_store, strategy_store = build_separated_log_dispatcher(tmp_path)
    system, strategy = tmp_path / "system.jsonl", tmp_path / "strategy.jsonl"
    try:
        # A trail that already holds well over one poll's worth of history.
        for index in range(500):
            dispatcher.dispatch(
                _system_record(
                    timestamp_ns=_BASE_NS + index,
                    message=f"history {index:03d}",
                    correlation_id=f"h-{index:03d}",
                )
            )

        fanout = _Fanout()
        publisher = _publisher(fanout, system, strategy)
        # start() seeds the cursor and runs one poll; the ticker is stopped
        # immediately so the polls under test are the ones driven here.
        publisher.start()
        publisher.stop()
        assert fanout.events == [], "startup replayed the archive onto a live channel"

        # The event an operator is actually waiting on, written after start.
        dispatcher.dispatch(
            _system_record(
                timestamp_ns=_BASE_NS + 10_000,
                message="kill switch engaged",
                correlation_id="ks-live",
            )
        )
        assert publisher.poll_once() == 1
        assert [payload["message"] for _channel, payload in fanout.events] == [
            "kill switch engaged"
        ], "the current event did not arrive on the very next poll"

        # Skipping history is a real trade, so it is DECLARED, not inferred —
        # and it is not a fault: nothing was missed that this channel promised.
        health = publisher.health()
        assert health["history_not_replayed"] == ["system"]
        assert health["ok"] is True
        # The history is still there for the surface built to page it.
        status, body = _rest(_handler(system, strategy), log_class="system")
        assert status == 200
        assert body["matched"] == 501
    finally:
        system_store.close()
        strategy_store.close()


def test_a_failed_seed_never_degrades_into_replaying_the_archive(tmp_path: Path) -> None:
    """The error path must not reach the failure the happy path exists to avoid.

    Establishing the cursor at the tail is what keeps the channel live. If that
    read fails transiently — a rotation window where the active segment has been
    renamed but not yet recreated, EMFILE, EINTR — and the failure were simply
    swallowed, the cursor would stay unset, and an unset cursor means "publish
    everything retained". The very next poll would read the store perfectly well
    and replay the whole archive, with no counter raised and ``ok`` still true:
    the exact failure the seed exists to prevent, reached through its error path.
    """

    dispatcher, system_store, strategy_store = build_separated_log_dispatcher(tmp_path)
    system, strategy = tmp_path / "system.jsonl", tmp_path / "strategy.jsonl"
    try:
        for index in range(20):
            dispatcher.dispatch(
                _system_record(
                    timestamp_ns=_BASE_NS + index,
                    message=f"history {index:02d}",
                    correlation_id=f"h-{index:02d}",
                )
            )

        fanout = _Fanout()
        publisher = _publisher(fanout, system, strategy)
        failures = {"left": 1}
        real_read_tail = publisher_module.read_tail

        def flaky_read_tail(*args, **kwargs):  # type: ignore[no-untyped-def]
            if failures["left"]:
                failures["left"] -= 1
                raise OSError("too many open files")
            return real_read_tail(*args, **kwargs)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(publisher_module, "read_tail", flaky_read_tail)
            publisher.start()
            publisher.stop()

            # The seed failed, so nothing was published and the failure is on the
            # record — NOT silently absorbed.
            assert fanout.events == []
            assert publisher.health()["read_failures"] >= 1
            assert publisher.health()["ok"] is False
            assert fanout.claims == 0, "a channel that never established its cursor was claimed"

            # The next poll reads fine. It must seed and go live, NOT replay.
            dispatcher.dispatch(
                _system_record(
                    timestamp_ns=_BASE_NS + 10_000,
                    message="kill switch engaged",
                    correlation_id="ks-live",
                )
            )
            assert publisher.poll_once() == 1
        assert [payload["message"] for _channel, payload in fanout.events] == [
            "kill switch engaged"
        ], "the retried seed replayed the archive instead of going live"
    finally:
        system_store.close()
        strategy_store.close()


def test_a_poll_costs_the_page_not_the_whole_history(tmp_path: Path) -> None:
    """Steady-state polling must not re-read the trail it already published.

    The anchor tells the publisher WHERE it stopped, but finding that place by
    scanning from the beginning means every tick re-reads, re-parses and
    re-validates the entire retained trail — and rotation is opt-in, so that
    trail grows without bound. The cost climbs until the 1s ticker's real cadence
    drifts past its interval and ``stop()`` starts timing out, with ``health()``
    still reporting ``ok``. Measured in BYTES READ rather than asserted in prose:
    a docstring promising a bounded read is exactly what regressed here before.
    """

    dispatcher, system_store, strategy_store = build_separated_log_dispatcher(tmp_path)
    system, strategy = tmp_path / "system.jsonl", tmp_path / "strategy.jsonl"
    try:
        for index in range(400):
            dispatcher.dispatch(
                _system_record(
                    timestamp_ns=_BASE_NS + index,
                    message=f"history {index:03d}",
                    correlation_id=f"h-{index:03d}",
                )
            )
        trail_bytes = system.stat().st_size
        assert trail_bytes > 64 * 1024, "fixture too small to tell a page from a full scan"

        fanout = _Fanout()
        publisher = _publisher(fanout, system, strategy)
        publisher.start()
        publisher.stop()

        # Count the bytes the next poll actually reads off the system trail.
        read_bytes = 0
        real_open = Path.open

        def counting_open(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            handle = real_open(self, *args, **kwargs)
            if self != system:
                return handle
            real_read, real_readline = handle.read, handle.readline

            def read(*a, **k):  # type: ignore[no-untyped-def]
                nonlocal read_bytes
                chunk = real_read(*a, **k)
                read_bytes += len(chunk)
                return chunk

            def readline(*a, **k):  # type: ignore[no-untyped-def]
                nonlocal read_bytes
                line = real_readline(*a, **k)
                read_bytes += len(line)
                return line

            handle.read, handle.readline = read, readline  # type: ignore[method-assign]
            return handle

        dispatcher.dispatch(
            _system_record(
                timestamp_ns=_BASE_NS + 10_000,
                message="kill switch engaged",
                correlation_id="ks-live",
            )
        )
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(Path, "open", counting_open)
            assert publisher.poll_once() == 1

        assert read_bytes < trail_bytes // 4, (
            f"the poll read {read_bytes} bytes of a {trail_bytes}-byte trail — it is still "
            "walking the history to find its place"
        )
    finally:
        system_store.close()
        strategy_store.close()


def test_a_reused_slot_is_not_mistaken_for_the_resume_point(tmp_path: Path) -> None:
    """Resuming by offset alone would skip records; the CONTENT is checked too.

    A rotated-away segment's inode can be reused and a later line can land at the
    same byte offset. A cursor that trusted the position alone would then seek
    past records nobody ever published — a silent drop, which is the one failure
    this whole cursor exists to prevent. So the record at the slot must match the
    record that was published there, and when it does not, the publisher falls
    back to the honest full scan rather than assuming.
    """

    system = tmp_path / "system.jsonl"
    strategy = tmp_path / "strategy.jsonl"
    strategy.write_text("", encoding="utf-8")
    store = JsonlLogStore(system, log_class=LogClass.SYSTEM)
    try:
        store.write(_system_record(message="aaaa", correlation_id="c-0"))
        store.write(_system_record(message="bbbb", correlation_id="c-1"))
    finally:
        store.close()

    fanout = _Fanout()
    publisher = _publisher(fanout, system, strategy)
    assert publisher.poll_once() == 2
    anchor_slot = fanout.events[-1][1]["record_id"]

    # Overwrite the anchor's line IN PLACE with a different record of the same
    # length: same file, same inode, same byte offsets, different content — the
    # shape a reused inode at a reused offset presents to the cursor.
    original = system.read_bytes()
    system.write_bytes(original.replace(b'"bbbb"', b'"zzzz"'))
    assert len(system.read_bytes()) == len(original), "the fixture moved the offsets"

    store = JsonlLogStore(system, log_class=LogClass.SYSTEM)
    try:
        store.write(_system_record(message="written after", correlation_id="c-2"))
    finally:
        store.close()

    # The slot no longer holds what was published there, so the publisher must
    # NOT seek past it. It falls back to the full scan, does not find its anchor,
    # and republishes the retained trail rather than skipping the record after —
    # a re-delivery is recoverable, a silent drop from an audit channel is not.
    published = publisher.poll_once()
    messages = [payload["message"] for _channel, payload in fanout.events]
    assert "written after" in messages[-published:], "the record after the reused slot was skipped"
    # And the loss of the anchor is reported, not papered over.
    health = publisher.health()
    assert health["eviction_gaps"] == 1
    assert health["ok"] is False
    assert anchor_slot  # the slot really was the one that got overwritten


def test_publisher_emits_each_record_once_and_only_real_records(tmp_path: Path) -> None:
    system, strategy = _seed(tmp_path)
    fanout = _Fanout()
    publisher = _publisher(fanout, system, strategy)

    assert publisher.poll_once() == 2
    assert publisher.poll_once() == 0, "a second poll must not re-broadcast"
    published = [payload["message"] for _channel, payload in fanout.events]
    assert published == ["operator triggered kill switch", "long AAPL"]
    assert all(channel == "LOGS" for channel, _payload in fanout.events)
    # Every published event carries its own class, so a subscriber can never
    # attribute a strategy log to the system trail.
    assert [payload["log_class"] for _channel, payload in fanout.events] == [
        "system",
        "strategy",
    ]
    assert publisher.health()["ok"] is True


def test_publisher_surfaces_a_read_failure_instead_of_going_quiet(tmp_path: Path) -> None:
    system, strategy = _seed(tmp_path)
    fanout = _Fanout()
    publisher = _publisher(fanout, system, strategy)
    _corrupt(system)

    published = publisher.poll_once()

    # The strategy trail still publishes; the corrupt system trail publishes
    # nothing but is REPORTED — silence alone would look like an idle system.
    assert published == 1
    health = publisher.health()
    assert health["ok"] is False
    assert health["read_failures"] == 1
    assert "system store unreadable" in str(health["last_error"])


def test_publisher_retries_the_window_it_could_not_read(tmp_path: Path) -> None:
    """A failed poll must not advance the cursor past unpublished records."""

    system, strategy = _seed(tmp_path)
    fanout = _Fanout()
    publisher = _publisher(fanout, system, strategy)

    original = system.read_bytes()
    system.write_bytes(b"{not json}\n")
    assert publisher.poll_once() == 1  # strategy only; system failed
    system.write_bytes(original)

    assert publisher.poll_once() == 1  # the system record, not skipped
    assert [payload["log_class"] for _channel, payload in fanout.events] == [
        "strategy",
        "system",
    ]


def test_a_failing_fanout_retries_the_record_and_never_kills_the_ticker(
    tmp_path: Path,
) -> None:
    """A publish fault must not skip a record OR silently stop the stream.

    ``start()`` has already claimed the LOGS channel, so a ticker that died on a
    fan-out error would leave the runtime reporting the workflow served while
    nothing is published — the worst failure mode this channel has.
    """

    system, strategy = _seed(tmp_path)
    fanout = _Fanout()
    publisher = _publisher(fanout, system, strategy)

    calls = {"n": 0}
    real_publish = fanout.publish

    def flaky(channel: str, payload: object) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("websocket hub wedged")
        return real_publish(channel, payload)

    publisher._publish = flaky  # type: ignore[attr-defined]

    # First poll: the system record's publish raises. Nothing is recorded as
    # published for that class, and the failure is surfaced.
    assert publisher.poll_once() == 1  # the strategy record still went out
    health = publisher.health()
    assert health["ok"] is False
    assert health["publish_failures"] == 1
    assert "retried on the next poll" in str(health["last_error"])

    # Second poll: the record that failed is RETRIED, not skipped.
    assert publisher.poll_once() == 1
    assert [payload["message"] for _channel, payload in fanout.events] == [
        "long AAPL",
        "operator triggered kill switch",
    ]
    # ...and the health stays unhealthy: a subscriber did miss an event window.
    assert publisher.health()["ok"] is False


def test_the_ticker_survives_an_unexpected_poll_error(tmp_path: Path) -> None:
    """Belt-and-braces: even an unforeseen fault leaves the thread running."""

    system, strategy = _seed(tmp_path)
    fanout = _Fanout()
    publisher = _publisher(fanout, system, strategy)

    def explode() -> int:
        raise RuntimeError("unforeseen")

    publisher.poll_once = explode  # type: ignore[method-assign]
    publisher.start()
    try:
        deadline = time.monotonic() + 5.0
        while publisher.publish_failures == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert publisher.publish_failures >= 1, "the failure was never recorded"
        assert publisher.health()["running"] is True, "the ticker died on an error"
    finally:
        publisher.stop()


def test_rotation_that_evicts_while_records_arrive_does_not_skip_them(
    tmp_path: Path,
) -> None:
    """The failure a COUNT cursor hides: evict as many as arrive, length unchanged.

    With ``records[published_count:]`` the retained list shifts left underneath
    the index, the length looks the same, and the new records are skipped with a
    perfectly healthy-looking poll. The anchor cursor re-locates the last record
    actually published, so the new ones still go out.
    """

    system_store = JsonlLogStore(
        tmp_path / "system.jsonl", log_class=LogClass.SYSTEM, max_bytes=700, max_files=3
    )
    strategy_store = JsonlLogStore(tmp_path / "strategy.jsonl", log_class=LogClass.STRATEGY)
    fanout = _Fanout()
    publisher = _publisher(fanout, tmp_path / "system.jsonl", tmp_path / "strategy.jsonl")

    def write(index: int) -> None:
        system_store.write(
            _system_record(
                timestamp_ns=_BASE_NS + index,
                message=f"system event {index:03d}",
                correlation_id=f"ks-{index:03d}",
            )
        )

    try:
        for index in range(6):
            write(index)
        assert publisher.poll_once() == 6
        # Keep writing until rotation has actually evicted history.
        for index in range(6, 30):
            write(index)
        published = publisher.poll_once()
    finally:
        system_store.close()
        strategy_store.close()

    seen = [payload["message"] for _channel, payload in fanout.events]
    retained = {record.message for record in read_records(tmp_path / "system.jsonl", max_files=3)}

    # Every record still RETAINED in the trail that was written after the first
    # poll must have been published — none skipped by a shifted index.
    late_and_retained = {f"system event {i:03d}" for i in range(6, 30)} & retained
    assert late_and_retained, "the fixture did not retain any post-poll records"
    assert late_and_retained <= set(seen), (
        f"records were silently skipped: {sorted(late_and_retained - set(seen))}"
    )
    assert published >= len(late_and_retained)
    # No record is published twice.
    assert len(seen) == len(set(seen)), "a record was re-broadcast"


def test_an_evicted_anchor_publishes_the_retained_history_and_reports_the_gap(
    tmp_path: Path,
) -> None:
    """The anchor itself rotated out — the retained trail must STILL be published.

    Rotation drops a prefix, so everything still retained was written after the
    evicted anchor and has never gone out. Re-syncing to the end here would skip
    exactly the records an operator can still see in the store. The window
    between the anchor and the retained head is genuinely unrecoverable, so it is
    reported as a gap rather than papered over.
    """

    system_store = JsonlLogStore(
        tmp_path / "system.jsonl", log_class=LogClass.SYSTEM, max_bytes=400, max_files=1
    )
    (tmp_path / "strategy.jsonl").touch()
    fanout = _Fanout()
    publisher = _publisher(fanout, tmp_path / "system.jsonl", tmp_path / "strategy.jsonl")

    try:
        system_store.write(_system_record(message="first", correlation_id="c-0"))
        assert publisher.poll_once() == 1
        # Write enough that the anchor rotates out of the retained window.
        for index in range(1, 20):
            system_store.write(
                _system_record(
                    timestamp_ns=_BASE_NS + index,
                    message=f"later {index:03d}",
                    correlation_id=f"c-{index:03d}",
                )
            )
        retained = read_records(tmp_path / "system.jsonl", max_files=1)
        assert all(record.message != "first" for record in retained), (
            "the fixture did not evict the anchor"
        )
        publisher.poll_once()
    finally:
        system_store.close()

    seen = [payload["message"] for _channel, payload in fanout.events]
    # Everything still in the trail went out — the retained history is NOT
    # skipped just because the publisher lost its place.
    assert {record.message for record in retained} <= set(seen), (
        f"retained records were skipped: {sorted({r.message for r in retained} - set(seen))}"
    )
    assert len(seen) == len(set(seen)), "a record was re-broadcast"

    health = publisher.health()
    assert health["ok"] is False, "an evicted anchor must not report a healthy channel"
    assert health["eviction_gaps"] == 1
    assert "may have" in str(health["last_error"])


def test_identical_records_get_distinct_record_ids_on_every_surface(tmp_path: Path) -> None:
    """Two records that look alike must still be two records to a consumer.

    A retried operation legitimately writes the same message with the same
    correlation id, and the rendered timestamp is only milliseconds — so the
    dashboard, which merges a REST poll with the live channel, cannot dedupe on
    values without collapsing two real audit events into one. ``record_id`` is
    the identity that makes them distinguishable.
    """

    dispatcher, system_store, _strategy = build_separated_log_dispatcher(tmp_path)
    duplicate = _system_record(message="retry me", correlation_id="dup-1")
    dispatcher.dispatch(duplicate)
    dispatcher.dispatch(duplicate)  # byte-identical, same nanosecond stamp
    system_store.close()

    system = tmp_path / "system.jsonl"
    strategy = tmp_path / "strategy.jsonl"

    _status, body = _rest(_handler(system, strategy))
    ids = [event["record_id"] for event in body["events"]]
    assert len(ids) == 2, "the two identical records did not both survive the read"
    assert len(set(ids)) == 2, f"identical records share a record_id: {ids}"
    # Everything else about them IS identical — that is the point.
    assert body["events"][0]["message"] == body["events"][1]["message"]
    assert body["events"][0]["timestamp"] == body["events"][1]["timestamp"]

    pane = LogPaneProvider(system_store_path=system, strategy_store_path=strategy).logs_snapshot()[
        "classes"
    ]["system"]
    assert len({record["record_id"] for record in pane["records"]}) == 2

    fanout = _Fanout()
    publisher = _publisher(fanout, system, strategy)
    publisher.poll_once()
    published = [payload["record_id"] for _channel, payload in fanout.events]
    assert len(set(published)) == 2
    # The same record has the SAME id on every surface, so a consumer can match
    # a published event against a polled one.
    assert set(published) == set(ids)


def test_byte_identical_records_are_each_published_exactly_once(tmp_path: Path) -> None:
    """Two identical audit lines are two events, not one.

    A retried operation can legitimately persist the same message, with the same
    correlation id, stamped in the same nanosecond. A cursor keyed on the
    record's VALUE would see the second line as "the one I already sent" and drop
    it silently — so the cursor is keyed on the line's physical position instead.
    """

    dispatcher, system_store, _strategy = build_separated_log_dispatcher(tmp_path)
    duplicate = _system_record(message="retry me", correlation_id="dup-1")
    dispatcher.dispatch(duplicate)
    system_store.close()

    fanout = _Fanout()
    publisher = _publisher(fanout, tmp_path / "system.jsonl", tmp_path / "strategy.jsonl")
    assert publisher.poll_once() == 1

    # The SAME record, byte for byte, appended again.
    reopened = JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM)
    try:
        reopened.write(duplicate)
    finally:
        reopened.close()

    assert publisher.poll_once() == 1, "the duplicate line was mistaken for the anchor"
    messages = [payload["message"] for _channel, payload in fanout.events]
    assert messages == ["retry me", "retry me"], (
        "both persisted lines must reach the channel exactly once each"
    )
    assert publisher.poll_once() == 0, "a third poll must not re-broadcast"
    assert publisher.health()["ok"] is True


def test_rotation_during_a_scan_loses_no_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rotation landing MID-SCAN must not be acted on.

    The scan enumerates segment PATHS and then opens them, so a rotation in that
    window makes it incoherent: the old active segment can be renamed past a path
    already visited, and its post-anchor records never appear. Acting on such a
    scan would drop them. The publisher detects the active segment's identity
    changing across the scan, discards that read, and re-reads — so the race
    costs a retry, never an event.
    """

    import atp_logs_service.publisher as publisher_module

    system_store = JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM)
    (tmp_path / "strategy.jsonl").touch()
    fanout = _Fanout()
    publisher = _publisher(fanout, tmp_path / "system.jsonl", tmp_path / "strategy.jsonl")

    try:
        system_store.write(_system_record(message="before", correlation_id="c-0"))
        assert publisher.poll_once() == 1
        system_store.write(_system_record(message="after", correlation_id="c-1"))
        system_store.close()

        # Rotate the active segment away in the middle of the next scan.
        real_iter = publisher_module.iter_records_with_positions
        raced = {"done": False}

        def racing_iter(path, **kwargs):  # type: ignore[no-untyped-def]
            for index, entry in enumerate(real_iter(path, **kwargs)):
                if index == 0 and not raced["done"] and Path(path).name == "system.jsonl":
                    raced["done"] = True
                    # Exactly what a rotation does: rename the active segment
                    # away AND put a fresh active file in its place (leaving no
                    # active file would be a vanished store, not a rotation —
                    # a different condition with its own handling).
                    Path(path).rename(Path(path).with_name("system.jsonl.1"))
                    Path(path).touch()
                yield entry

        monkeypatch.setattr(publisher_module, "iter_records_with_positions", racing_iter)
        published = publisher.poll_once()
    finally:
        system_store.close()

    assert raced["done"], "the fixture never triggered the race"
    # The retry re-read a coherent trail, so the pending record still went out —
    # exactly once, and no earlier record was re-broadcast.
    assert published == 1
    assert [payload["message"] for _channel, payload in fanout.events] == ["before", "after"]


def test_a_store_rotating_through_every_attempt_publishes_nothing_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When every read races, the honest answer is to publish nothing and report it.

    Publishing from an incoherent scan could drop records; publishing nothing
    cannot, because the trail is append-only and the next poll sees a superset.
    The condition is counted rather than hidden — a store rotating faster than it
    can be read is a real operator problem.
    """

    import atp_logs_service.publisher as publisher_module

    system_store = JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM)
    (tmp_path / "strategy.jsonl").touch()
    fanout = _Fanout()
    publisher = _publisher(fanout, tmp_path / "system.jsonl", tmp_path / "strategy.jsonl")

    try:
        system_store.write(_system_record(message="pending", correlation_id="c-9"))
        system_store.close()

        real_iter = publisher_module.iter_records_with_positions
        rotations = {"count": 0}

        def always_racing_iter(path, **kwargs):  # type: ignore[no-untyped-def]
            entries = list(real_iter(path, **kwargs))
            active = Path(path)
            if active.name == "system.jsonl" and active.exists():
                # Give the active segment a NEW inode mid-scan, exactly as a
                # rotation would, while keeping its contents: the point is that
                # the file the scan started on is no longer the file at that
                # path, not that data moved.
                rotations["count"] += 1
                swap = active.with_name("system.jsonl.swap")
                shutil.copy2(active, swap)
                swap.replace(active)
            yield from entries

        monkeypatch.setattr(publisher_module, "iter_records_with_positions", always_racing_iter)
        published = publisher.poll_once()
    finally:
        system_store.close()

    assert rotations["count"] >= 1
    assert published == 0, "a permanently-raced scan must not publish"
    health = publisher.health()
    assert health["rotation_races"] >= 1
    assert "rotated during every read attempt" in str(health["last_error"])
    # ``ok`` means "no event may have been MISSED". A raced scan is a delay, not
    # a loss — the records are still on disk and the next poll carries them — so
    # it is reported through rotation_races/last_error rather than by flipping a
    # flag whose meaning is data loss.
    assert health["ok"] is True

    # Nothing was skipped: with the race over, the record still goes out once.
    monkeypatch.undo()
    assert publisher.poll_once() == 1
    assert [payload["message"] for _channel, payload in fanout.events] == ["pending"]


def test_missing_stores_are_a_read_failure_not_a_healthy_empty_stream(
    tmp_path: Path,
) -> None:
    """A configured trail that is not there must never look like a quiet channel.

    Reading a missing file yields an empty scan with no error, so without this
    the publisher would sit there emitting nothing and reporting ``ok: True`` —
    the same misreading the REST handler and the pane already refuse.
    """

    fanout = _Fanout()
    publisher = _publisher(fanout, tmp_path / "system.jsonl", tmp_path / "strategy.jsonl")

    assert publisher.poll_once() == 0
    health = publisher.health()
    assert health["ok"] is False, "a missing audit trail reported a healthy stream"
    assert health["read_failures"] == 2, "both configured stores should be reported"
    assert "does not exist" in str(health["last_error"])
    assert not fanout.events


def test_the_channel_is_not_claimed_while_a_store_is_missing(tmp_path: Path) -> None:
    """Readiness must not report LOGS served over a trail nobody can read.

    The claim is what makes the runtime count the channel toward the LOGS
    workflow, so it waits for a poll that read BOTH trails cleanly — and picks
    the channel up automatically once the trail appears.
    """

    fanout = _Fanout()
    publisher = _publisher(fanout, tmp_path / "system.jsonl", tmp_path / "strategy.jsonl")

    publisher.start()
    try:
        assert fanout.claims == 0, "the channel was claimed over a missing trail"
        assert publisher.health()["ok"] is False

        # The writer boots and creates the trail: the channel is picked up.
        dispatcher, system_store, _strategy = build_separated_log_dispatcher(tmp_path)
        dispatcher.dispatch(_system_record(message="now it exists"))
        system_store.close()
        publisher.poll_once()

        assert fanout.claims == 1, "the channel was never claimed once readable"
        assert [payload["message"] for _channel, payload in fanout.events] == ["now it exists"]
        # One claim only, however many further polls run.
        publisher.poll_once()
        assert fanout.claims == 1
    finally:
        publisher.stop()


def test_a_stop_that_times_out_never_leaves_two_tickers(tmp_path: Path) -> None:
    """Two loops sharing one cursor would duplicate and skip audit events.

    ``stop()`` joins with a budget. If the ticker is wedged in a read or a
    publish it can outlive that budget — and dropping the reference there would
    let the next ``start()`` clear the stop flag and launch a SECOND ticker
    beside the first. The reference is kept instead, the timeout is surfaced, and
    ``start()`` refuses until the old thread has actually exited.
    """

    dispatcher, system_store, _strategy = build_separated_log_dispatcher(tmp_path)
    dispatcher.dispatch(_system_record(message="first", correlation_id="c-0"))
    fanout = _Fanout()
    publisher = LogEventPublisher(
        publish=fanout.publish,
        claim_channel=fanout.claim,
        release_channel=fanout.release,
        system_store_path=tmp_path / "system.jsonl",
        strategy_store_path=tmp_path / "strategy.jsonl",
        poll_interval_s=0.05,
    )

    wedged = threading.Event()
    release = threading.Event()

    def wedging_publish(channel: str, payload: object) -> int:
        wedged.set()
        release.wait(timeout=30)
        return fanout.publish(channel, payload)

    # The first poll (synchronous, inside start) succeeds; the TICKER thread is
    # what wedges, on a record written after it is already running.
    publisher.start()
    try:
        publisher._publish = wedging_publish  # type: ignore[attr-defined]
        system_store.write(_system_record(message="wedges the ticker", correlation_id="c-1"))
        assert wedged.wait(timeout=10), "the ticker never reached the wedged publish"

        publisher.stop(timeout=0.2)  # cannot finish: the publish is still blocked

        assert publisher.stop_timeouts == 1
        assert "did not exit" in str(publisher.health()["last_error"])
        with pytest.raises(RuntimeError, match="has not exited"):
            publisher.start()
    finally:
        release.set()
        publisher.stop(timeout=10)
        system_store.close()

    # Once it really has exited, the slot is reusable — one ticker, never two.
    assert publisher.health()["running"] is False
    publisher.start()
    publisher.stop()


def test_the_channel_is_not_claimed_when_the_first_fanout_fails(tmp_path: Path) -> None:
    """Readiness must not count a channel whose very first publish raised."""

    _seed(tmp_path)
    fanout = _Fanout()
    publisher = _publisher(fanout, tmp_path / "system.jsonl", tmp_path / "strategy.jsonl")

    def always_failing(channel: str, payload: object) -> int:
        raise RuntimeError("hub wedged")

    publisher._publish = always_failing  # type: ignore[attr-defined]
    publisher.poll_once()

    assert fanout.claims == 0, "the channel was claimed while fan-out was already failing"
    assert publisher.health()["ok"] is False

    # Once the hub recovers and a poll is clean end-to-end, the claim happens.
    publisher._publish = fanout.publish  # type: ignore[attr-defined]
    publisher.poll_once()
    assert fanout.claims == 1


def test_a_reused_position_with_different_content_is_not_the_anchor(
    tmp_path: Path,
) -> None:
    """An evicted segment's inode can be reused — that must not fake the anchor.

    Rotation UNLINKS the oldest segment, and the filesystem is free to hand that
    inode to a later segment of the same store; a record can then land at the
    same byte offset. A cursor keyed on position alone would read that
    brand-new line as "the one I already published" and skip the retained
    history before it, with no gap reported. Matching the record too makes a
    false match need both coincidences at once.
    """

    import atp_logs_service.publisher as publisher_module

    dispatcher, system_store, _strategy = build_separated_log_dispatcher(tmp_path)
    dispatcher.dispatch(_system_record(message="published", correlation_id="c-0"))
    system_store.close()

    fanout = _Fanout()
    publisher = _publisher(fanout, tmp_path / "system.jsonl", tmp_path / "strategy.jsonl")
    assert publisher.poll_once() == 1

    # Now every entry reports the ANCHOR'S position — the inode-reuse collision —
    # while carrying different records. None of them is the anchor.
    anchor_position = publisher._anchor[LogClass.SYSTEM][0]  # type: ignore[index]
    real_iter = publisher_module.iter_records_with_positions

    def colliding_iter(path, **kwargs):  # type: ignore[no-untyped-def]
        for _position, record in real_iter(path, **kwargs):
            yield anchor_position, record

    # The anchor's OWN line is gone (its segment was evicted) and the trail now
    # holds only later records — every one of them reporting the anchor's
    # position, which is what inode reuse looks like from here.
    (tmp_path / "system.jsonl").unlink()
    reopened = JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM)
    try:
        reopened.write(_system_record(message="after the reuse", correlation_id="c-1"))
        reopened.write(_system_record(message="also retained", correlation_id="c-2"))
    finally:
        reopened.close()

    publisher_module.iter_records_with_positions = colliding_iter  # type: ignore[assignment]
    try:
        publisher.poll_once()
    finally:
        publisher_module.iter_records_with_positions = real_iter  # type: ignore[assignment]

    published = [payload["message"] for _channel, payload in fanout.events]
    assert published == ["published", "after the reuse", "also retained"], (
        "a reused position was mistaken for the anchor, so retained records were "
        f"skipped: {published}"
    )
    # The anchor really is gone, so this is reported as an eviction gap rather
    # than passed off as a clean poll.
    assert publisher.health()["eviction_gaps"] >= 1
    assert publisher.health()["ok"] is False


def test_a_trail_that_vanishes_mid_read_is_reported_on_every_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The missing-store rule must be ATOMIC, not a pre-check that can be raced.

    ``exists()`` followed by a scan is a TOCTOU: a deletion landing in that
    window leaves the scan yielding nothing, which is exactly what a healthy
    empty trail looks like. The absence is raised by the READ instead, so the
    race cannot turn a vanished audit trail into a clean poll.
    """

    import atp_logging.persistence as persistence
    import atp_logs_service.publisher as publisher_module

    system, strategy = _seed(tmp_path)
    real_iter = persistence.iter_records_with_positions

    def deleting_iter(path, **kwargs):  # type: ignore[no-untyped-def]
        # Delete the active segment AFTER any pre-check has passed but BEFORE
        # the scan opens it — the exact window the pre-check cannot cover.
        target = Path(path)
        if target.name == "system.jsonl" and target.exists():
            target.unlink()
        yield from real_iter(path, **kwargs)

    monkeypatch.setattr(persistence, "iter_records_with_positions", deleting_iter)
    monkeypatch.setattr(publisher_module, "iter_records_with_positions", deleting_iter)

    # The pane reads the TAIL — a different code path with the same rule, and
    # the deletion is injected INSIDE it (after read_tail's own pre-check, right
    # before the segment open) so the window under test is the real one.
    real_tail_once = persistence._read_tail_once

    def deleting_tail_once(base, **kwargs):  # type: ignore[no-untyped-def]
        target = Path(base)
        if target.name == "system.jsonl" and target.exists():
            target.unlink()
        return real_tail_once(base, **kwargs)

    monkeypatch.setattr(persistence, "_read_tail_once", deleting_tail_once)

    # REST: fails closed rather than answering "no events".
    with pytest.raises(InterfaceError) as caught:
        _rest(_handler(system, strategy), log_class="system")
    assert caught.value.type == "LOGS_STORE_MISSING"

    # Dashboard pane: records is None, not [].
    _seed(tmp_path)
    cell = LogPaneProvider(system_store_path=system, strategy_store_path=strategy).logs_snapshot()[
        "classes"
    ]["system"]
    assert cell["ok"] is False
    assert cell["records"] is None

    # Publisher: a read failure for the vanished class, never a clean poll — and
    # no claim taken. The intact class keeps publishing; one broken trail must
    # not silence the other.
    _seed(tmp_path)
    fanout = _Fanout()
    publisher = _publisher(fanout, system, strategy)
    publisher.poll_once()
    published_classes = {payload["log_class"] for _channel, payload in fanout.events}
    assert published_classes == {"strategy"}, (
        f"the vanished system trail still published: {published_classes}"
    )
    assert publisher.health()["read_failures"] >= 1
    assert publisher.health()["ok"] is False
    assert fanout.claims == 0, "readiness was claimed over a trail that vanished mid-read"


def test_a_query_racing_rotation_fails_closed_instead_of_returning_a_short_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rotation mid-scan must not yield a successful-looking but short answer.

    The scan enumerates segment paths and then opens them; a rotation in that
    window can move a segment past a path already visited. Returning the page
    anyway would be a false negative on an audit query — records the store still
    holds, absent from a 200 response.
    """

    import atp_logging.persistence as persistence

    system, strategy = _seed(tmp_path)
    real_iter = persistence.iter_records_with_positions

    def always_rotating_iter(path, **kwargs):  # type: ignore[no-untyped-def]
        entries = list(real_iter(path, **kwargs))
        active = Path(path)
        if active.name == "system.jsonl" and active.exists():
            # Same content, new inode: what a rotation looks like to the check.
            swap = active.with_name("system.jsonl.swap")
            shutil.copy2(active, swap)
            swap.replace(active)
        yield from entries

    monkeypatch.setattr(persistence, "iter_records_with_positions", always_rotating_iter)

    real_tail_once = persistence._read_tail_once

    def rotating_tail_once(base, **kwargs):  # type: ignore[no-untyped-def]
        page = real_tail_once(base, **kwargs)
        if Path(base).name == "system.jsonl" and Path(base).exists():
            swap = Path(base).with_name("system.jsonl.swap")
            shutil.copy2(base, swap)
            swap.replace(base)
        return page

    monkeypatch.setattr(persistence, "_read_tail_once", rotating_tail_once)

    with pytest.raises(InterfaceError) as caught:
        _rest(_handler(system, strategy), log_class="system")
    assert caught.value.status == 500
    assert caught.value.type == "LOGS_ROTATION_RACE"

    # The dashboard pane degrades to its explicit unavailable cell — never a
    # short list rendered as if it were the trail.
    cell = LogPaneProvider(system_store_path=system, strategy_store_path=strategy).logs_snapshot()[
        "classes"
    ]["system"]
    assert cell["ok"] is False
    assert cell["records"] is None
    assert "rotat" in str(cell["error"]).lower()


def test_a_stop_interleaved_with_a_claim_leaves_the_channel_released(
    tmp_path: Path,
) -> None:
    """stop() must win the race against a poll that is mid-claim.

    Deciding to claim under one lock and then calling out unlocked left a
    window: stop() could release the claim and clear the flag, and the pending
    poll would re-register the channel — leaving the runtime reporting LOGS
    served by a ticker that had already stopped.
    """

    _seed(tmp_path)
    fanout = _Fanout()
    publisher = _publisher(fanout, tmp_path / "system.jsonl", tmp_path / "strategy.jsonl")

    stopper: list[threading.Thread] = []

    def claim_then_stop() -> None:
        # Runs INSIDE the publisher's claim path. A stop starting exactly here
        # is the interleaving under test. It BLOCKS until this callback returns
        # (that serialization is the fix), so the thread is started and left to
        # queue — joining it here would deadlock by design.
        fanout.claim()
        thread = threading.Thread(target=publisher.stop, kwargs={"timeout": 5})
        thread.start()
        stopper.append(thread)
        time.sleep(0.1)  # let the stop reach the lock and queue behind us

    publisher._claim_channel = claim_then_stop  # type: ignore[attr-defined]
    publisher.poll_once()

    assert stopper, "the interleaved stop never started"
    stopper[0].join(timeout=10)
    assert not stopper[0].is_alive(), "stop() never completed after the claim finished"

    # Exactly one claim and one release, in that order: the channel ends
    # RELEASED. Without the shared lock the release would have run first and the
    # queued claim would have re-registered a stopped publisher.
    assert fanout.claims == 1
    assert fanout.releases == 1


def test_publisher_claims_the_channel_only_on_start(tmp_path: Path) -> None:
    """Readiness is claimed when publishing actually begins, not at wiring."""

    system, strategy = _seed(tmp_path)
    fanout = _Fanout()
    publisher = _publisher(fanout, system, strategy)

    assert fanout.claims == 0
    publisher.start()
    try:
        assert fanout.claims == 1
    finally:
        publisher.stop()


# --------------------------------------------------------------------------- #
# The read is bounded in MEMORY, not merely in output
# --------------------------------------------------------------------------- #


def test_query_never_materialises_the_whole_trail(tmp_path: Path) -> None:
    """A big audit trail must not be loaded just to return one page.

    The store is append-only and rotation is OPT-IN, so a long-lived operator
    log has no natural size bound. Slicing the last page off a fully-materialised
    list would let the trail's size decide whether the runtime survives the
    request. The read streams instead: this asserts the reader never holds more
    than the page, while the reported total stays exact.
    """

    dispatcher, system_store, _strategy = build_separated_log_dispatcher(tmp_path)
    total = 500
    for index in range(total):
        dispatcher.dispatch(
            _system_record(
                timestamp_ns=_BASE_NS + index,
                message=f"event {index:04d}",
                correlation_id=f"c-{index:04d}",
            )
        )
    system_store.close()

    # The read path is lazy: iter_records is a generator, so the bounded reader
    # never has the whole trail in hand — it keeps only the page-sized window.
    stream = iter_records(tmp_path / "system.jsonl")
    assert inspect.isgenerator(stream), "the reader must stream, not materialise"
    assert isinstance(next(stream), LogRecord)
    stream.close()

    page, matched = read_records_bounded(
        tmp_path / "system.jsonl", limit=10, log_class=LogClass.SYSTEM
    )

    assert matched == total, "the reported total must stay exact"
    assert len(page) == 10
    assert [record.message for _position, record in page] == [
        f"event {index:04d}" for index in range(total - 1, total - 11, -1)
    ], "the page must be the NEWEST records, newest first"

    handler = _handler(tmp_path / "system.jsonl", tmp_path / "strategy.jsonl", max_events=10)
    _status, body = _rest(handler)
    assert body["returned"] == 10
    assert body["matched"] == total
    assert body["truncated"] is True


def test_the_polled_pane_reads_the_page_not_the_trail(tmp_path: Path) -> None:
    """The dashboard polls forever — its cost must not grow with the log.

    Counting the whole trail on every refresh would make an append-only store a
    self-inflicted load that rises without bound. The pane reads BACKWARDS and
    stops when its page is full, so the work it does is proportional to what it
    shows. This measures the bytes actually read, which is the property that
    matters; a row-count assertion would pass even on a full scan.
    """

    dispatcher, system_store, _strategy = build_separated_log_dispatcher(tmp_path)
    for index in range(2_000):
        dispatcher.dispatch(
            _system_record(
                timestamp_ns=_BASE_NS + index,
                message=f"event {index:05d}",
                correlation_id=f"c-{index:05d}",
            )
        )
    system_store.close()

    system = tmp_path / "system.jsonl"
    trail_bytes = system.stat().st_size
    assert trail_bytes > 200_000, "the fixture is too small to distinguish the two reads"

    read_bytes = {"n": 0}
    real_open = Path.open

    def counting_open(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        handle = real_open(self, *args, **kwargs)
        if self.name.startswith("system.jsonl"):
            real_read = handle.read

            def counting_read(*read_args, **read_kwargs):  # type: ignore[no-untyped-def]
                chunk = real_read(*read_args, **read_kwargs)
                read_bytes["n"] += len(chunk)
                return chunk

            handle.read = counting_read  # type: ignore[method-assign]
        return handle

    provider = LogPaneProvider(
        system_store_path=system,
        strategy_store_path=tmp_path / "strategy.jsonl",
        max_records=20,
    )
    original = Path.open
    Path.open = counting_open  # type: ignore[method-assign]
    try:
        cell = provider.logs_snapshot()["classes"]["system"]
    finally:
        Path.open = original  # type: ignore[method-assign]

    assert len(cell["records"]) == 20
    assert [record["message"] for record in cell["records"]][0] == "event 01999"
    # A full scan would read the whole file (twice over, with the rotation-
    # stability re-read). The tail read touches a small multiple of the page.
    assert read_bytes["n"] < trail_bytes // 2, (
        f"the pane read {read_bytes['n']} bytes of a {trail_bytes}-byte trail — "
        "it is scanning the history, not the page"
    )


def test_the_public_contracts_do_not_call_the_live_logs_surface_a_placeholder() -> None:
    """What the generated docs say must match what the code answers.

    Every unimplemented entry in ``openapi.json`` / ``manual.json`` /
    ``asyncapi.json`` carries a "Contract only — concrete behaviour lands with
    the downstream feature" sentence. That is the truth right up until a handler
    lands, and its exact opposite afterwards: an integrator reading it is told
    not to build against an endpoint that returns audit records, and an operator
    is told the log trail is unavailable when ``admin logs`` works. This is the
    same drift class as a stale ``field_types`` — a public document contradicting
    the running system — so the declaration that drives the generator is asserted
    alongside the generated artefact, and the generator is exercised rather than
    trusted.
    """

    import json

    from atp_api import ROUTES
    from atp_api.openapi import build_openapi
    from atp_cli import COMMANDS
    from atp_cli.manual import build_manual
    from atp_ws import EVENT_CHANNELS
    from atp_ws.asyncapi import build_asyncapi

    route = next(r for r in ROUTES if r.path == "/api/v1/logs")
    command = next(c for c in COMMANDS if c.invocation == "admin logs")
    channel = next(c for c in EVENT_CHANNELS if c.name.value == "LOGS")
    assert (route.served_by, command.served_by, channel.served_by) == (
        "SRS-LOG-001",
        "SRS-LOG-001",
        "SRS-LOG-001",
    ), "a surface with a live handler must declare who serves it"

    rest_description = build_openapi()["paths"]["/api/v1/logs"]["get"]["description"]
    cli_description = next(
        entry["description"]
        for group in build_manual()["groups"]
        for entry in group["commands"]
        if entry["invocation"] == "admin logs"
    )
    ws_description = build_asyncapi()["channels"]["/ws/v1/logs"]["description"]

    for surface, description in (
        ("REST", rest_description),
        ("CLI", cli_description),
        ("WS", ws_description),
    ):
        assert "Contract only" not in description, (
            f"{surface} doc still calls a live surface a stub"
        )
        assert "SRS-LOG-001" in description, f"{surface} doc does not name the owner"
    # Named as served, but never as unconditionally served: an unwired deployment
    # still 501s, and a doc that hid that would send an operator hunting a bug in
    # their client instead of composing the handler.
    assert "501" in rest_description
    assert "has not composed" in cli_description and "has not composed" in ws_description

    # The frozen snapshots are what consumers actually read, so they must carry
    # the same text the generator produces.
    frozen_rest = json.loads((PYTHON_ROOT / "atp_api" / "openapi.json").read_text(encoding="utf-8"))
    assert frozen_rest["paths"]["/api/v1/logs"]["get"]["description"] == rest_description

    # A channel with NO live publisher keeps the placeholder — otherwise this
    # would pass by making every description implemented-shaped.
    unserved = [c for c in EVENT_CHANNELS if not c.served_by]
    assert unserved, "fixture assumption: some channels are still contract-only"
    unserved_doc = build_asyncapi()["channels"][f"/ws/v1/{unserved[0].name.value.lower()}"]
    assert "Contract only" in unserved_doc["description"]


def test_the_pane_never_claims_health_it_did_not_verify(tmp_path: Path) -> None:
    """Corruption OUTSIDE the page must not be reported as a sound trail.

    A tail read stops at the page, so a corrupt line further back is neither
    detected nor denied. The pane may still render its page — that is its job —
    but it must not let ``ok`` be read as "this audit history is clean". It
    declares the SCOPE of what it checked, and the full-trail verification the
    operator can actually rely on lives on GET /api/v1/logs, which fails closed.
    """

    dispatcher, system_store, _strategy = build_separated_log_dispatcher(tmp_path)
    system, strategy = tmp_path / "system.jsonl", tmp_path / "strategy.jsonl"

    # A corrupt COMPLETE line, then well past a page of good records after it.
    dispatcher.dispatch(_system_record(message="oldest good", correlation_id="c-0"))
    system_store.close()
    _corrupt(system)
    reopened = JsonlLogStore(system, log_class=LogClass.SYSTEM)
    try:
        for index in range(30):
            reopened.write(
                _system_record(
                    timestamp_ns=_BASE_NS + index,
                    message=f"recent {index:02d}",
                    correlation_id=f"r-{index:02d}",
                )
            )
    finally:
        reopened.close()

    cell = LogPaneProvider(
        system_store_path=system, strategy_store_path=strategy, max_records=5
    ).logs_snapshot()["classes"]["system"]

    # It renders the page it read...
    assert [record["message"] for record in cell["records"]][0] == "recent 29"
    # ...and states that this is ALL it verified, so `ok` cannot be mistaken for
    # a clean bill of health on the trail.
    assert cell["integrity_scope"] == "page"
    assert cell["matched"] is None

    # The verification an operator can rely on is on the full query, and it
    # DOES catch the older corruption.
    with pytest.raises(InterfaceError) as caught:
        _rest(_handler(system, strategy), log_class="system")
    assert caught.value.type == "LOGS_STORE_CORRUPT"


def test_publisher_buffers_at_most_one_page_per_poll(tmp_path: Path) -> None:
    """The publisher streams too — a backlog is drained over polls, not slurped."""

    dispatcher, system_store, _strategy = build_separated_log_dispatcher(tmp_path)
    for index in range(50):
        dispatcher.dispatch(
            _system_record(
                timestamp_ns=_BASE_NS + index,
                message=f"backlog {index:03d}",
                correlation_id=f"b-{index:03d}",
            )
        )
    system_store.close()

    fanout = _Fanout()
    publisher = LogEventPublisher(
        publish=fanout.publish,
        claim_channel=fanout.claim,
        release_channel=fanout.release,
        system_store_path=tmp_path / "system.jsonl",
        strategy_store_path=tmp_path / "strategy.jsonl",
        max_events_per_poll=10,
    )

    assert publisher.poll_once() == 10, "one poll must emit at most one page"
    assert publisher.poll_once() == 10
    # The backlog drains in order, without repeats.
    seen = [payload["message"] for _channel, payload in fanout.events]
    assert seen == [f"backlog {index:03d}" for index in range(20)]
    while publisher.poll_once():
        pass
    assert len({payload["message"] for _channel, payload in fanout.events}) == 50
    assert publisher.health()["ok"] is True


# --------------------------------------------------------------------------- #
# Coverage is stated, not implied
# --------------------------------------------------------------------------- #


def test_pane_states_producer_coverage_for_every_system_source(tmp_path: Path) -> None:
    system, strategy = _seed(tmp_path)
    snapshot = LogPaneProvider(
        system_store_path=system, strategy_store_path=strategy
    ).logs_snapshot()

    coverage = snapshot["source_coverage"]
    assert isinstance(coverage, list)
    covered = {entry["source"] for entry in coverage}
    assert covered == {source.value for source in SYSTEM_SOURCES}, (
        "every AC-named system source must carry an explicit coverage verdict"
    )
    for entry in coverage:
        assert entry["state"] in {"produced", "partial", "deferred"}
        # A source without a full producer must NAME who owns the gap; an
        # unattributed gap is not actionable.
        if entry["state"] != "produced":
            assert entry["owners"], f"{entry['source']} has no named owner"
        assert entry["note"]
    assert snapshot["coverage_note"]


def _record_emitters() -> dict[str, list[str]]:
    """Map each system source to the modules that BUILD records for it.

    A module counts as a producer only when it constructs a ``LogRecord`` AND
    names the source: referencing ``Source.X`` alone is what a *reader* does
    (``atp_dashboard.killswitch`` filters ``read_records`` by
    ``source=Source.KILL_SWITCH``) and what this coverage map itself does.
    Counting those would report a query surface as a producer.
    """

    emitters: dict[str, list[str]] = {}
    for path in PYTHON_ROOT.rglob("*.py"):
        if path.parts[len(PYTHON_ROOT.parts)] in {"atp_logging", "atp_logs_service"}:
            continue  # the SDK and the read surfaces are not producers
        text = path.read_text(encoding="utf-8")
        if "LogRecord(" not in text:
            continue
        for source in SYSTEM_SOURCES:
            if f"Source.{source.name}" in text:
                emitters.setdefault(source.value, []).append(path.name)
    return emitters


def test_coverage_claims_produced_only_where_a_producer_exists() -> None:
    """Guard against a future edit quietly upgrading — or stranding — a gap.

    The tree is what proves the claim: a ``produced``/``partial`` verdict
    asserts some module builds records for that source. When a producer lands,
    this fails and whoever added it updates ``SOURCE_COVERAGE`` deliberately,
    rather than leaving the dashboard understating its own coverage forever.
    """

    emitters = _record_emitters()
    for source in SYSTEM_SOURCES:
        state = SOURCE_COVERAGE[source].state_for(source)
        found = emitters.get(source.value, [])
        if state == "deferred":
            assert not found, (
                f"{source.value} is marked deferred but {found} build records for it — "
                "a producer landed; update SOURCE_COVERAGE"
            )
        else:
            assert found, f"{source.value} is marked {state!r} but no module builds records for it"


def test_coverage_is_stated_per_event_type_not_only_per_source() -> None:
    """ "Partial" names what is missing, or it is not a coverage statement.

    A source marked ``partial`` tells an operator that something under it has no
    producer without saying what — and the gap that actually hid there was
    ``SEQUENCE_GAP``: a declared ``market_data`` event type with no producer
    anywhere, behind a note that accounted only for the other three. On a strip
    whose entire job is "coverage is stated, not implied", under-reporting is the
    failure it exists to prevent. So every declared event type is accounted for,
    and every unaccounted one names an owner.
    """

    for source in SYSTEM_SOURCES:
        cell = SOURCE_COVERAGE[source].as_dict(source)
        declared = set(EVENT_TYPES_BY_SOURCE[source])
        assert set(cell["produced_event_types"]) | set(cell["unproduced_event_types"]) == declared
        assert not (set(cell["produced_event_types"]) & set(cell["unproduced_event_types"]))
        if cell["unproduced_event_types"]:
            assert cell["owners"], f"{source.value} leaves event types unowned"

    market_data = SOURCE_COVERAGE[Source.MARKET_DATA].as_dict(Source.MARKET_DATA)
    assert "SEQUENCE_GAP" in market_data["unproduced_event_types"]
    # And it is not merely unbuilt: IB exposes no tick sequence, so the owning
    # seam has nothing to detect a gap with. An operator reading the strip must
    # get that, not an implied "coming soon".
    assert "SRS-MD-007" in market_data["owners"]
    assert "no tick sequence" in market_data["note"]

    # The tri-state must be DERIVED, not a second copy that can drift: flipping
    # the produced set has to move the verdict with it.
    from dataclasses import replace

    coverage = SOURCE_COVERAGE[Source.KILL_SWITCH]
    assert coverage.state_for(Source.KILL_SWITCH) == "produced"
    assert replace(coverage, produced=()).state_for(Source.KILL_SWITCH) == "deferred"
    assert replace(coverage, produced=("ACTIVATION",)).state_for(Source.KILL_SWITCH) == "partial"


def test_five_of_the_eight_system_sources_still_have_no_producer() -> None:
    """Pin the honest state this session ships with (AC coverage is partial).

    SRS-LOG-001 stays ``passes:false`` precisely because of this gap. If a
    later session wires a producer, this number changes and the session note
    plus the contract's deferred list must change with it.
    """

    deferred = {
        source.value
        for source in SYSTEM_SOURCES
        if SOURCE_COVERAGE[source].state_for(source) == "deferred"
    }
    assert deferred == {
        "order_routing",
        "ingestion",
        "container_lifecycle",
        "hot_swap",
        "resource_monitor",
    }
