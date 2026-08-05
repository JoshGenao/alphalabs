"""L1 unit tests for the SRS-LOG-001 log query handler's request parsing.

Pure request→response behaviour of :class:`atp_logs_service.LogsQueryHandler`
over a temp store: the parameter contract, the fail-closed rejections, the
response shape, and the bounded page. The audit-trail *safety* invariants
(separation across surfaces, unreadable-is-not-empty, publisher honesty) are
asserted at L7 in ``tests/domain/test_log_operator_surface.py``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_api import ROUTES  # noqa: E402
from atp_logging.persistence import build_separated_log_dispatcher  # noqa: E402
from atp_logging.records import LogClass, LogRecord, Severity, Source  # noqa: E402
from atp_logs_service import EVENT_FIELDS, LogsQueryHandler  # noqa: E402
from atp_runtime.errors import InterfaceError  # noqa: E402
from atp_runtime.registry import OperationKey, Request, Surface  # noqa: E402

pytestmark = pytest.mark.unit

_BASE_NS = 1_700_000_000_000_000_000
#: One second between fixture records. The declared start_time/end_time are
#: ISO-8601 strings, so the window resolves to microseconds at best — records
#: spaced by nanoseconds could not be separated by ANY bound.
_SPACING_NS = 1_000_000_000


def _record(index: int, severity: Severity, source: Source, event_type: str) -> LogRecord:
    return LogRecord(
        timestamp_ns=_BASE_NS + index * _SPACING_NS,
        severity=severity,
        source=source,
        event_type=event_type,
        message=f"{source.value}/{event_type}",
        correlation_id=f"corr-{index}",
        log_class=LogClass.SYSTEM,
        strategy_id=None,
    )


@pytest.fixture()
def store(tmp_path: Path) -> tuple[Path, Path]:
    dispatcher, _system, _strategy = build_separated_log_dispatcher(tmp_path)
    dispatcher.dispatch(_record(0, Severity.DEBUG, Source.MARKET_DATA, "SUBSCRIPTION_CHANGE"))
    dispatcher.dispatch(_record(1, Severity.WARN, Source.IB_GATEWAY, "HEARTBEAT_STALE"))
    dispatcher.dispatch(_record(2, Severity.CRITICAL, Source.KILL_SWITCH, "ACTIVATION"))
    dispatcher.dispatch(
        LogRecord(
            timestamp_ns=_BASE_NS + 3 * _SPACING_NS,
            severity=Severity.INFO,
            source=Source.STRATEGY,
            event_type="SIGNAL",
            message="strategy line",
            correlation_id="run-1",
            log_class=LogClass.STRATEGY,
            strategy_id="sma-1",
        )
    )
    return tmp_path / "system.jsonl", tmp_path / "strategy.jsonl"


def _handler(store: tuple[Path, Path], **kwargs: object) -> LogsQueryHandler:
    system, strategy = store
    return LogsQueryHandler(
        system_store_path=system,
        strategy_store_path=strategy,
        **kwargs,  # type: ignore[arg-type]
    )


def _rest(handler: LogsQueryHandler, **query: str) -> dict[str, object]:
    return dict(
        handler.handle(
            Request(
                surface=Surface.REST,
                operation=OperationKey(Surface.REST, "GET /api/v1/logs"),
                method="GET",
                path="/api/v1/logs",
                query=dict(query),
            )
        ).body
    )


def _cli(handler: LogsQueryHandler, **query: str) -> dict[str, object]:
    return dict(
        handler.handle(
            Request(
                surface=Surface.CLI,
                operation=OperationKey(Surface.CLI, "admin logs"),
                query=dict(query),
            )
        ).body
    )


def _messages(body: dict[str, object]) -> list[str]:
    events = body["events"]
    assert isinstance(events, list)
    return [str(event["message"]) for event in events]


# ----- response shape ------------------------------------------------------ #


def test_response_shape_matches_the_declared_contract_exactly(store: tuple[Path, Path]) -> None:
    """Every field the live route emits is declared, and every declared one is emitted.

    A superset would be undeclared drift a strict client cannot parse; a subset
    would be a promise the handler does not keep. Both directions are asserted.
    """

    route = next(r for r in ROUTES if r.path == "/api/v1/logs")
    declared = set(route.response_fields)

    body = _rest(_handler(store))
    events = body["events"]
    assert isinstance(events, list) and events

    # TOP-LEVEL: the envelope. Per-event fields must NOT appear here — they
    # belong under events[], and documenting them beside the array would send a
    # generated client looking in the wrong place.
    assert set(body) == declared, (
        f"live top-level response and atp_api declaration disagree: "
        f"only-live={sorted(set(body) - declared)}, only-declared={sorted(declared - set(body))}"
    )
    # ITEM-LEVEL: one events[] element, declared via response_item_fields.
    declared_item = {name for name, _type in dict(route.response_item_fields)["events"]}
    for event in events:
        assert set(event) == set(EVENT_FIELDS)
        assert set(event) == declared_item


def test_the_cli_surface_declares_no_follow_flag() -> None:
    """An uncovered capability gets no public surface — not even an erroring flag.

    The runtime answers a command with ONE result and cannot stream to stdout, so
    ``admin logs`` does not advertise ``--follow`` at all; the command summary
    points at the LOGS WebSocket channel instead.
    """

    from atp_cli import COMMANDS

    command = next(c for c in COMMANDS if c.group.value == "admin" and c.name == "logs")
    assert not any(arg.name == "--follow" for arg in command.arguments)
    assert "LOGS WebSocket channel" in command.summary
    assert "follow" not in {arg.name.lstrip("-").replace("-", "_") for arg in command.arguments}


def test_timestamp_renders_as_iso8601_utc(store: tuple[Path, Path]) -> None:
    body = _rest(_handler(store), log_class="strategy")
    events = body["events"]
    assert isinstance(events, list)
    stamp = str(events[0]["timestamp"])
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None
    assert parsed.astimezone(timezone.utc).timestamp() == pytest.approx(
        (_BASE_NS + 3 * _SPACING_NS) / 1_000_000_000, abs=0.001
    )


# ----- filters ------------------------------------------------------------- #


def test_severity_is_an_inclusive_minimum(store: tuple[Path, Path]) -> None:
    handler = _handler(store)
    assert len(_messages(_rest(handler))) == 3
    assert _messages(_rest(handler, severity="WARN")) == [
        "kill_switch/ACTIVATION",
        "ib_gateway/HEARTBEAT_STALE",
    ]
    assert _messages(_rest(handler, severity="CRITICAL")) == ["kill_switch/ACTIVATION"]


def test_exact_match_filters(store: tuple[Path, Path]) -> None:
    handler = _handler(store)
    assert _messages(_rest(handler, source="kill_switch")) == ["kill_switch/ACTIVATION"]
    assert _messages(_rest(handler, event_type="HEARTBEAT_STALE")) == ["ib_gateway/HEARTBEAT_STALE"]
    assert _messages(_rest(handler, correlation_id="corr-0")) == ["market_data/SUBSCRIPTION_CHANGE"]


def test_time_window_is_inclusive_and_accepts_z_suffix(store: tuple[Path, Path]) -> None:
    handler = _handler(store)
    start = datetime.fromtimestamp((_BASE_NS + 1 * _SPACING_NS) / 1_000_000_000, tz=timezone.utc)
    body = _rest(handler, start_time=start.isoformat().replace("+00:00", "Z"))
    assert set(_messages(body)) == {"ib_gateway/HEARTBEAT_STALE", "kill_switch/ACTIVATION"}


def test_exact_boundary_timestamps_are_included(store: tuple[Path, Path]) -> None:
    """An inclusive bound equal to a record's stamp must MATCH that record.

    The bound arrives as an ISO string and the record is stamped in nanoseconds.
    Converting through ``datetime.timestamp()`` floats loses ~240 ns of
    resolution at current epoch values, which can push the bound just past the
    record it was meant to include — the operator asks for "since 12:00:00" and
    the 12:00:00 entry silently disappears from the audit view.
    """

    handler = _handler(store)
    for index, expected_message in enumerate(
        [
            "market_data/SUBSCRIPTION_CHANGE",
            "ib_gateway/HEARTBEAT_STALE",
            "kill_switch/ACTIVATION",
        ]
    ):
        stamp_ns = _BASE_NS + index * _SPACING_NS
        moment = datetime.fromtimestamp(stamp_ns / 1_000_000_000, tz=timezone.utc)
        # start_time == the record's own stamp: inclusive, so it is returned.
        assert expected_message in _messages(_rest(handler, start_time=moment.isoformat()))
        # end_time == the record's own stamp: inclusive on the other side too.
        assert expected_message in _messages(_rest(handler, end_time=moment.isoformat()))
        # A one-record window over exactly that instant returns exactly it.
        assert _messages(
            _rest(handler, start_time=moment.isoformat(), end_time=moment.isoformat())
        ) == [expected_message]


def test_boundary_holds_at_a_stamp_float_math_would_miss(tmp_path: Path) -> None:
    """The drift is only visible at microsecond stamps floats cannot hold exactly.

    ``datetime.timestamp()`` for 2023-11-14T22:13:20.007919Z scales to
    ...007919104 ns rather than ...007919000 — 104 ns ABOVE the record. As an
    inclusive ``start_time`` that silently excludes the very entry the operator
    named. Round-second fixtures never expose this, so this case is explicit.
    """

    stamp_ns = 1_700_000_000_007_919_000
    dispatcher, _system, _strategy = build_separated_log_dispatcher(tmp_path)
    dispatcher.dispatch(
        LogRecord(
            timestamp_ns=stamp_ns,
            severity=Severity.CRITICAL,
            source=Source.KILL_SWITCH,
            event_type="ACTIVATION",
            message="boundary record",
            correlation_id="edge-1",
            log_class=LogClass.SYSTEM,
        )
    )
    handler = LogsQueryHandler(
        system_store_path=tmp_path / "system.jsonl",
        strategy_store_path=tmp_path / "strategy.jsonl",
    )
    exact = datetime.fromtimestamp(stamp_ns / 1_000_000_000, tz=timezone.utc).isoformat()
    # Float math would compute a bound 104 ns above the record and drop it.
    assert _messages(_rest(handler, start_time=exact)) == ["boundary record"]
    assert _messages(_rest(handler, end_time=exact)) == ["boundary record"]


def test_cli_since_maps_onto_the_same_window(store: tuple[Path, Path]) -> None:
    handler = _handler(store)
    start = datetime.fromtimestamp((_BASE_NS + 2 * _SPACING_NS) / 1_000_000_000, tz=timezone.utc)
    assert _messages(_cli(handler, since=start.isoformat())) == ["kill_switch/ACTIVATION"]


# ----- fail-closed parsing ------------------------------------------------- #


@pytest.mark.parametrize(
    ("query", "error_type"),
    [
        ({"log_class": "SYSTEM"}, "LOGS_BAD_LOG_CLASS"),  # case matters; enum is lowercase
        ({"log_class": "audit"}, "LOGS_BAD_LOG_CLASS"),
        ({"severity": "WARNING"}, "LOGS_BAD_SEVERITY"),
        ({"severity": "warn"}, "LOGS_BAD_SEVERITY"),
        ({"source": "orders"}, "LOGS_BAD_SOURCE"),
        ({"source": "strategy"}, "LOGS_SOURCE_CLASS_MISMATCH"),
        ({"event_type": "  "}, "LOGS_BLANK_FILTER"),
        ({"correlation_id": ""}, "LOGS_BLANK_FILTER"),
        ({"start_time": "yesterday"}, "LOGS_BAD_TIME_BOUND"),
        ({"end_time": "2026-13-45"}, "LOGS_BAD_TIME_BOUND"),
        ({"limit": "10"}, "LOGS_UNKNOWN_PARAMETER"),
        ({"follow": "true"}, "LOGS_UNKNOWN_PARAMETER"),  # REST declares no --follow
    ],
)
def test_bad_parameters_are_rejected(
    store: tuple[Path, Path], query: dict[str, str], error_type: str
) -> None:
    with pytest.raises(InterfaceError) as caught:
        _rest(_handler(store), **query)
    assert caught.value.status == 400
    assert caught.value.type == error_type
    # The offending value is echoed so the operator can see what was rejected.
    assert caught.value.detail


def test_inverted_window_is_rejected_rather_than_matching_nothing(
    store: tuple[Path, Path],
) -> None:
    later = datetime.fromtimestamp((_BASE_NS + 3 * _SPACING_NS) / 1_000_000_000, tz=timezone.utc)
    earlier = datetime.fromtimestamp(_BASE_NS / 1_000_000_000, tz=timezone.utc)
    with pytest.raises(InterfaceError) as caught:
        _rest(_handler(store), start_time=later.isoformat(), end_time=earlier.isoformat())
    assert caught.value.type == "LOGS_INVERTED_WINDOW"


def test_an_injected_follow_option_is_still_refused(store: tuple[Path, Path]) -> None:
    """argparse rejects the undeclared flag first; the handler refuses it too.

    Defence in depth: the option is not declared, so a real invocation never
    reaches here — but a caller constructing a Request by hand must not get a
    silently-ignored option either.
    """

    with pytest.raises(InterfaceError) as caught:
        _cli(_handler(store), follow="True")
    assert caught.value.status == 400
    assert caught.value.type == "LOGS_UNKNOWN_PARAMETER"


def test_constructor_rejects_degenerate_bounds(store: tuple[Path, Path]) -> None:
    for bad in (0, -1, True):
        with pytest.raises(ValueError, match="max_events"):
            _handler(store, max_events=bad)
    with pytest.raises(ValueError, match="max_files"):
        _handler(store, max_files=0)


# ----- bounded page -------------------------------------------------------- #


def test_page_is_newest_first_and_reports_truncation(store: tuple[Path, Path]) -> None:
    handler = _handler(store, max_events=2)
    body = _rest(handler)
    assert _messages(body) == ["kill_switch/ACTIVATION", "ib_gateway/HEARTBEAT_STALE"]
    assert body["returned"] == 2
    assert body["matched"] == 3
    assert body["truncated"] is True
    assert body["limit"] == 2


def test_untruncated_page_says_so(store: tuple[Path, Path]) -> None:
    body = _rest(_handler(store))
    assert body["truncated"] is False
    assert body["returned"] == body["matched"] == 3


def test_absent_store_fails_closed_rather_than_answering_empty(tmp_path: Path) -> None:
    """A CONFIGURED trail that is not there is a failure, not an empty log.

    Reading a missing file succeeds trivially and yields zero records — shaped
    exactly like a healthy quiet system. A deleted store, a mispointed
    directory, or a writer that never started would otherwise answer "200, no
    events": an all-clear over missing audit history.
    """

    handler = LogsQueryHandler(
        system_store_path=tmp_path / "system.jsonl",
        strategy_store_path=tmp_path / "strategy.jsonl",
    )
    with pytest.raises(InterfaceError) as caught:
        _rest(handler)
    assert caught.value.status == 500
    # Its own type: "missing" and "corrupt" are different operator problems.
    assert caught.value.type == "LOGS_STORE_MISSING"
    assert caught.value.detail["store"].endswith("system.jsonl")


def test_present_store_reports_store_present(store: tuple[Path, Path]) -> None:
    assert _rest(_handler(store))["store_present"] is True
