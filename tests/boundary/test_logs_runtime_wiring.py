"""L4 boundary tests: the SRS-LOG-001 surfaces on a REAL operator runtime.

Drives ``atp_logs_service.wire_logs`` against an actual
:class:`~atp_runtime.runtime.OperatorInterfaceRuntime` — the registry, the REST
dispatcher, the CLI dispatcher, and the publisher claim — rather than calling
the handler directly. What this layer proves that L1/L7 cannot:

* an UNWIRED runtime keeps the structured ``501`` naming ``SRS-LOG-001``, so a
  composition that forgot to wire logs cannot look served;
* the wired handler is reachable through the declared paths and command
  invocations, with the dispatcher's own status→exit-code mapping;
* the ``LOGS`` workflow becomes ``fully_served`` only once the publisher is
  actually running — readiness is never claimed by wiring alone;
* the composed CLI entrypoint (``python -m atp_logs_service``) serves a real
  query, and refuses to guess a trail location when its knob is unset.
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_logging.persistence import build_separated_log_dispatcher  # noqa: E402
from atp_logging.records import LogClass, LogRecord, Severity, Source  # noqa: E402
from atp_logs_service import LOGS_OPERATIONS, wire_logs  # noqa: E402
from atp_runtime.runtime import OperatorInterfaceRuntime  # noqa: E402

pytestmark = pytest.mark.boundary

_BASE_NS = 1_700_000_000_000_000_000


@pytest.fixture()
def log_dir(tmp_path: Path) -> Path:
    dispatcher, _system, _strategy = build_separated_log_dispatcher(tmp_path)
    dispatcher.dispatch(
        LogRecord(
            timestamp_ns=_BASE_NS,
            severity=Severity.CRITICAL,
            source=Source.KILL_SWITCH,
            event_type="ACTIVATION",
            message="kill switch fired",
            correlation_id="ks-1",
            log_class=LogClass.SYSTEM,
        )
    )
    dispatcher.dispatch(
        LogRecord(
            timestamp_ns=_BASE_NS + 1,
            severity=Severity.INFO,
            source=Source.STRATEGY,
            event_type="SIGNAL",
            message="strategy line",
            correlation_id="run-1",
            log_class=LogClass.STRATEGY,
            strategy_id="sma-1",
        )
    )
    return tmp_path


def _wire(runtime: OperatorInterfaceRuntime, log_dir: Path, **kwargs: object):
    return wire_logs(
        runtime,
        system_store_path=log_dir / "system.jsonl",
        strategy_store_path=log_dir / "strategy.jsonl",
        **kwargs,  # type: ignore[arg-type]
    )


def test_bare_runtime_defers_every_logs_operation_to_this_feature() -> None:
    runtime = OperatorInterfaceRuntime()

    status, body = runtime.dispatch_rest("GET", "/api/v1/logs", b"")
    assert status == 501
    assert body["error"]["detail"]["owner"] == "SRS-LOG-001"

    out = io.StringIO()
    exit_code = runtime.cli_dispatcher().dispatch(["admin", "logs", "--json"], stdout=out)
    assert exit_code != 0
    assert "SRS-LOG-001" in out.getvalue()

    assert not runtime.is_publisher_registered("LOGS")
    for operation in LOGS_OPERATIONS:
        assert not runtime.registry.is_registered(operation)


def test_wired_rest_route_serves_the_persisted_trail(log_dir: Path) -> None:
    runtime = OperatorInterfaceRuntime()
    _wire(runtime, log_dir)

    status, body = runtime.dispatch_rest("GET", "/api/v1/logs", b"")
    assert status == 200
    assert [event["message"] for event in body["events"]] == ["kill switch fired"]

    status, body = runtime.dispatch_rest("GET", "/api/v1/logs?log_class=strategy", b"")
    assert status == 200
    assert [event["message"] for event in body["events"]] == ["strategy line"]


def test_wired_cli_command_serves_and_exits_zero(log_dir: Path) -> None:
    runtime = OperatorInterfaceRuntime()
    _wire(runtime, log_dir)

    out = io.StringIO()
    exit_code = runtime.cli_dispatcher().dispatch(
        ["admin", "logs", "--log-class", "strategy", "--json"], stdout=out
    )
    assert exit_code == 0
    assert "strategy line" in out.getvalue()
    assert "kill switch fired" not in out.getvalue(), "CLI leaked a system record"


def test_cli_rejects_an_undeclared_follow_flag(log_dir: Path) -> None:
    """``--follow`` is not part of the surface, so the parser refuses it.

    The runtime cannot stream a command result to stdout, so the capability is
    not advertised at all rather than advertised-and-erroring.
    """

    runtime = OperatorInterfaceRuntime()
    _wire(runtime, log_dir)

    with pytest.raises(SystemExit) as caught:
        runtime.cli_dispatcher().dispatch(["admin", "logs", "--follow"], stdout=io.StringIO())
    assert caught.value.code != 0


def test_a_bad_query_is_a_400_through_the_real_dispatcher(log_dir: Path) -> None:
    runtime = OperatorInterfaceRuntime()
    _wire(runtime, log_dir)

    status, body = runtime.dispatch_rest("GET", "/api/v1/logs?severity=nonsense", b"")
    assert status == 400
    assert body["error"]["type"] == "LOGS_BAD_SEVERITY"


def test_logs_workflow_is_served_only_once_the_publisher_runs(log_dir: Path) -> None:
    runtime = OperatorInterfaceRuntime()

    def logs_workflow() -> dict[str, object]:
        return next(w for w in runtime.status_snapshot()["workflows"] if w["id"] == "LOGS")

    assert logs_workflow()["fully_served"] is False
    publisher = _wire(runtime, log_dir)
    # Handlers registered, but nothing is publishing yet: still not served.
    assert logs_workflow()["fully_served"] is False
    assert logs_workflow()["implemented_operations"] == 2

    publisher.start()
    try:
        served = logs_workflow()
        assert served["fully_served"] is True
        assert served["deferred_owners"] == []
        assert runtime.is_publisher_registered("LOGS")
    finally:
        publisher.stop()


def test_logs_workflow_is_not_served_over_a_missing_audit_trail(tmp_path: Path) -> None:
    """Runtime readiness must not report LOGS served when the trail is absent.

    The handlers register regardless (they answer with a structured error), but
    the channel claim — the thing that completes the workflow — waits for a poll
    that could actually read both trails.
    """

    runtime = OperatorInterfaceRuntime()
    publisher = wire_logs(
        runtime,
        system_store_path=tmp_path / "system.jsonl",  # never created
        strategy_store_path=tmp_path / "strategy.jsonl",
    )
    publisher.start()
    try:
        logs = next(w for w in runtime.status_snapshot()["workflows"] if w["id"] == "LOGS")
        assert logs["fully_served"] is False, "LOGS reported served over a missing trail"
        assert not runtime.is_publisher_registered("LOGS")
        assert publisher.health()["ok"] is False
        # And the query surface says so rather than answering "no events".
        status, body = runtime.dispatch_rest("GET", "/api/v1/logs", b"")
        assert status == 500
        assert body["error"]["type"] == "LOGS_STORE_MISSING"
    finally:
        publisher.stop()


def test_logs_readiness_is_revoked_when_the_trail_is_lost(log_dir: Path) -> None:
    """Readiness that cannot be revoked eventually lies.

    A claim answers "is this channel being published?" — and that stops being
    true when the audit store disappears. Latching it would keep
    ``GET /api/v1/system/status`` reporting the LOGS workflow fully served by a
    stream that has stopped delivering.
    """

    runtime = OperatorInterfaceRuntime()
    publisher = _wire(runtime, log_dir)

    def logs_workflow() -> dict[str, object]:
        return next(w for w in runtime.status_snapshot()["workflows"] if w["id"] == "LOGS")

    publisher.start()
    try:
        assert logs_workflow()["fully_served"] is True
        assert runtime.is_publisher_registered("LOGS")

        # The audit trail goes away underneath a live, already-claimed channel.
        (log_dir / "system.jsonl").unlink()
        publisher.poll_once()

        assert not runtime.is_publisher_registered("LOGS"), "the claim was latched"
        assert logs_workflow()["fully_served"] is False
        assert publisher.health()["ok"] is False

        # It comes back when the trail does — readiness tracks reality both ways.
        build_separated_log_dispatcher(log_dir)
        publisher.poll_once()
        assert runtime.is_publisher_registered("LOGS")
        assert logs_workflow()["fully_served"] is True
    finally:
        publisher.stop()


def test_stopping_the_publisher_drops_logs_readiness(log_dir: Path) -> None:
    """A stopped publisher is not publishing — readiness must say so.

    The runtime outlives the publisher (a dashboard can stop its log arm and
    keep serving), so a claim left behind would report the LOGS workflow fully
    served by a ticker that no longer exists.
    """

    runtime = OperatorInterfaceRuntime()
    publisher = _wire(runtime, log_dir)

    def logs_workflow() -> dict[str, object]:
        return next(w for w in runtime.status_snapshot()["workflows"] if w["id"] == "LOGS")

    publisher.start()
    assert logs_workflow()["fully_served"] is True

    publisher.stop()

    assert not runtime.is_publisher_registered("LOGS"), "a stopped publisher kept its claim"
    assert logs_workflow()["fully_served"] is False
    # The REST/CLI handlers stay registered — they still answer; only the
    # channel's publisher is gone.
    assert runtime.registry.is_registered(LOGS_OPERATIONS[0])

    # Starting again re-claims it.
    publisher.start()
    try:
        assert logs_workflow()["fully_served"] is True
    finally:
        publisher.stop()


def test_logs_is_not_served_while_rotation_blocks_every_read(
    log_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store rotating through every attempt delivers nothing — so it is not served.

    A rotation race means the poll published nothing from that trail. Unlike an
    eviction gap (a fact about lost history, which persists and must not pin
    readiness off), a race is transient: readiness comes back on the next clean
    poll. Claiming through it would report LOGS served by a stream delivering
    nothing.
    """

    import shutil

    import atp_logs_service.publisher as publisher_module

    runtime = OperatorInterfaceRuntime()
    publisher = _wire(runtime, log_dir)
    real_iter = publisher_module.iter_records_with_positions

    def always_racing_iter(path, **kwargs):  # type: ignore[no-untyped-def]
        entries = list(real_iter(path, **kwargs))
        active = Path(path)
        if active.name == "system.jsonl" and active.exists():
            swap = active.with_name("system.jsonl.swap")
            shutil.copy2(active, swap)
            swap.replace(active)  # same content, new inode: a rotation
        yield from entries

    monkeypatch.setattr(publisher_module, "iter_records_with_positions", always_racing_iter)
    publisher.start()
    try:
        assert not runtime.is_publisher_registered("LOGS"), (
            "LOGS was claimed while every read raced rotation"
        )
        logs = next(w for w in runtime.status_snapshot()["workflows"] if w["id"] == "LOGS")
        assert logs["fully_served"] is False
        assert publisher.rotation_races >= 1

        # Transient, not latched: once the rotation stops, the next poll claims.
        monkeypatch.undo()
        publisher.poll_once()
        assert runtime.is_publisher_registered("LOGS")
    finally:
        publisher.stop()


def test_status_stays_self_consistent_while_logs_claims_and_releases(log_dir: Path) -> None:
    """A status report must describe a moment that actually happened.

    The publisher now claims and RELEASES its channel from its own thread as the
    store comes and goes, while status requests read the same registry from
    server threads. A report that inspected the set once per channel could
    straddle a transition; the snapshot is taken once, under a lock.
    """

    import threading

    runtime = OperatorInterfaceRuntime()
    publisher = _wire(runtime, log_dir)
    publisher.start()

    stop = threading.Event()
    problems: list[str] = []

    def flap() -> None:
        # Hammer the registry the way a store flapping in and out would.
        while not stop.is_set():
            runtime.register_publisher("LOGS")
            runtime.unregister_publisher("LOGS")

    def poll_status() -> None:
        while not stop.is_set():
            try:
                snapshot = runtime.status_snapshot()
            except Exception as error:  # noqa: BLE001 - the point of the test
                problems.append(f"status raised: {error!r}")
                return
            for workflow in snapshot["workflows"]:
                served = workflow["fully_served"]
                owners = workflow["deferred_owners"]
                # The two must never disagree inside ONE report.
                if served and owners:
                    problems.append(f"{workflow['id']}: fully_served with deferred {owners}")
                    return
                if workflow["implemented_operations"] > workflow["total_operations"]:
                    problems.append(f"{workflow['id']}: implemented exceeds total")
                    return

    threads = [threading.Thread(target=flap), threading.Thread(target=poll_status)]
    for thread in threads:
        thread.start()
    time.sleep(0.5)
    stop.set()
    for thread in threads:
        thread.join(timeout=5)
    publisher.stop()

    assert not problems, problems[0]


def test_an_unforeseen_poll_failure_gives_the_logs_claim_back(log_dir: Path) -> None:
    """Even a bug nobody predicted must not leave readiness green.

    The ticker survives an unexpected exception on purpose (a monitoring surface
    must not die), but surviving is not delivering: if the claim stayed, the
    runtime would report LOGS served by a loop erroring out on every poll.
    """

    runtime = OperatorInterfaceRuntime()
    publisher = _wire(runtime, log_dir, poll_interval_s=0.05)

    publisher.start()
    try:
        assert runtime.is_publisher_registered("LOGS"), "the healthy start never claimed"

        # Something unforeseen breaks mid-flight.
        def explode() -> int:
            raise RuntimeError("unforeseen")

        publisher.poll_once = explode  # type: ignore[method-assign]

        deadline = time.monotonic() + 5
        while runtime.is_publisher_registered("LOGS") and time.monotonic() < deadline:
            time.sleep(0.02)

        assert not runtime.is_publisher_registered("LOGS"), (
            "a ticker failing every poll kept the LOGS claim"
        )
        logs = next(w for w in runtime.status_snapshot()["workflows"] if w["id"] == "LOGS")
        assert logs["fully_served"] is False
        assert publisher.health()["running"] is True, "the ticker should still be alive"
    finally:
        publisher.stop()


def test_publisher_fans_new_records_out_over_the_runtime_hub(log_dir: Path) -> None:
    runtime = OperatorInterfaceRuntime()
    publisher = _wire(runtime, log_dir)

    published: list[tuple[str, object]] = []
    original = runtime.publish

    def _record_publish(channel: str, payload: object) -> int:
        published.append((channel, payload))
        return original(channel, payload)

    publisher._publish = _record_publish  # type: ignore[attr-defined]
    publisher.poll_once()

    assert [channel for channel, _payload in published] == ["LOGS", "LOGS"]
    assert {payload["log_class"] for _channel, payload in published} == {  # type: ignore[index]
        "system",
        "strategy",
    }


def test_composed_cli_entrypoint_serves_a_real_query(
    log_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``python -m atp_logs_service`` composes what the bare runtime defers."""

    from atp_logs_service.__main__ import main

    monkeypatch.setenv("ATP_LOG_DIR", str(log_dir))
    captured = io.StringIO()
    monkeypatch.setattr("sys.stdout", captured)
    exit_code = main(["admin", "logs", "--log-class", "strategy", "--json"])

    assert exit_code == 0
    assert "strategy line" in captured.getvalue()
    assert "kill switch fired" not in captured.getvalue()


def test_composed_cli_refuses_to_guess_the_trail_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fallback directory: a wrong-path 'no records' is worse than refusing."""

    from atp_cli import ExitCode
    from atp_logs_service.__main__ import main

    monkeypatch.delenv("ATP_LOG_DIR", raising=False)
    assert main(["admin", "logs"]) == int(ExitCode.USAGE_ERROR)


def test_wiring_twice_is_refused_rather_than_silently_rebinding(log_dir: Path) -> None:
    """The registry rejects a duplicate binding — two trails on one route would
    make which store answers a query depend on composition order."""

    runtime = OperatorInterfaceRuntime()
    _wire(runtime, log_dir)
    with pytest.raises(ValueError, match="already registered"):
        _wire(runtime, log_dir)
