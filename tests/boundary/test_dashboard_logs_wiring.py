"""L4 boundary tests: the SRS-LOG-001 log pane mounted on a real runtime.

Proves the mount seam behaves like every other optional dashboard provider —
and, specifically, that a dashboard which cannot read a trail says so instead of
rendering an empty one:

* a bare ``SRS-UI-001`` mount registers NO log route (404), so the SPA renders
  its explicit not-mounted state;
* mounting the provider serves both classes from separate stores at
  ``GET /dashboard/api/logs``;
* the production entrypoint composes the pane and the REST/CLI/WS arm from one
  env knob, and composes NEITHER when it is unset;
* mounting the pane does not claim the ``LOGS`` WebSocket channel — only a
  running publisher does.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_dashboard.logs import LogPaneProvider  # noqa: E402
from atp_dashboard.provider import ReadinessBackedProvider  # noqa: E402
from atp_dashboard.server import (  # noqa: E402
    LOGS_SNAPSHOT_PATH,
    _mount_logs_arm,
    log_pane_provider,
    mount_dashboard,
    mount_default_dashboard,
)
from atp_logging.persistence import build_separated_log_dispatcher  # noqa: E402
from atp_logging.records import LogClass, LogRecord, Severity, Source  # noqa: E402
from atp_logs_service import LogEventPublisher  # noqa: E402
from atp_runtime.runtime import OperatorInterfaceRuntime  # noqa: E402

pytestmark = pytest.mark.boundary

_BASE_NS = 1_700_000_000_000_000_000


@pytest.fixture()
def log_dir(tmp_path: Path) -> Path:
    dispatcher, _system, _strategy = build_separated_log_dispatcher(tmp_path)
    dispatcher.dispatch(
        LogRecord(
            timestamp_ns=_BASE_NS,
            severity=Severity.WARN,
            source=Source.MARKET_DATA,
            event_type="HEARTBEAT_STALE",
            message="SPY line stale",
            correlation_id="feed-spy",
            log_class=LogClass.SYSTEM,
        )
    )
    dispatcher.dispatch(
        LogRecord(
            timestamp_ns=_BASE_NS + 1,
            severity=Severity.INFO,
            source=Source.STRATEGY,
            event_type="REBALANCE",
            message="rebalanced book",
            correlation_id="run-7",
            log_class=LogClass.STRATEGY,
            strategy_id="mr-2",
        )
    )
    return tmp_path


def _provider(log_dir: Path) -> LogPaneProvider:
    return LogPaneProvider(
        system_store_path=log_dir / "system.jsonl",
        strategy_store_path=log_dir / "strategy.jsonl",
    )


def test_bare_mount_registers_no_log_route() -> None:
    runtime = OperatorInterfaceRuntime()
    mount_dashboard(runtime, ReadinessBackedProvider({}))

    status, _body = runtime.dispatch_rest("GET", LOGS_SNAPSHOT_PATH, b"")
    assert status == 404, "an unmounted pane must 404, not serve an empty trail"


def test_mounted_pane_serves_both_classes_from_separate_stores(log_dir: Path) -> None:
    runtime = OperatorInterfaceRuntime()
    mount_dashboard(runtime, ReadinessBackedProvider({}), logs=_provider(log_dir))

    status, body = runtime.dispatch_rest("GET", LOGS_SNAPSHOT_PATH, b"")
    assert status == 200
    assert body["ok"] is True
    classes = body["classes"]
    assert set(classes) == {"system", "strategy"}
    assert classes["system"]["store"] == "system.jsonl"
    assert classes["strategy"]["store"] == "strategy.jsonl"
    assert [r["message"] for r in classes["system"]["records"]] == ["SPY line stale"]
    assert [r["message"] for r in classes["strategy"]["records"]] == ["rebalanced book"]
    assert body["source_coverage"] and body["coverage_note"]


def test_configured_but_missing_stores_fail_closed(tmp_path: Path) -> None:
    """``ATP_LOG_DIR`` pointing at a directory with no stores is NOT a healthy pane.

    This is the misconfiguration that matters most on an audit surface: the
    operator configured a trail, the files are not there, and a successful read
    of nothing looks exactly like a quiet system.
    """

    runtime = OperatorInterfaceRuntime()
    env = {"ATP_LOG_DIR": str(tmp_path)}  # empty directory: no system/strategy files
    mount_default_dashboard(runtime, env)

    status, body = runtime.dispatch_rest("GET", LOGS_SNAPSHOT_PATH, b"")
    assert status == 200, "the pane route must still answer — it reports the problem"
    assert body["ok"] is False, "a missing audit trail must not render as healthy"
    for cls in ("system", "strategy"):
        cell = body["classes"][cls]
        assert cell["ok"] is False
        assert cell["records"] is None, "a missing trail must never render as an empty list"
        assert cell["store_present"] is False
        assert "does not exist" in str(cell["error"])

    # The contract route fails closed too, with its own error type.
    publisher = _mount_logs_arm(runtime, env)
    assert publisher is not None
    status, body = runtime.dispatch_rest("GET", "/api/v1/logs", b"")
    assert status == 500
    assert body["error"]["type"] == "LOGS_STORE_MISSING"


def test_a_failed_bind_leaves_no_publisher_running(
    log_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup is all-or-nothing: a bind failure must not leave tickers polling.

    The publishers run on their own threads and are started BEFORE the bind. If
    binding fails — port in use, refused host — anything already running would
    keep polling the audit stores and publishing into a runtime that never came
    up: invisible work behind a process that looks dead.
    """

    import atp_dashboard.server as server_module

    started: list[object] = []
    real_start = server_module.DashboardPublisher.start
    real_logs_start = LogEventPublisher.start

    def tracking_start(self):  # type: ignore[no-untyped-def]
        started.append(self)
        return real_start(self)

    def tracking_logs_start(self):  # type: ignore[no-untyped-def]
        started.append(self)
        return real_logs_start(self)

    monkeypatch.setattr(server_module.DashboardPublisher, "start", tracking_start)
    monkeypatch.setattr(LogEventPublisher, "start", tracking_logs_start)
    monkeypatch.setenv("ATP_LOG_DIR", str(log_dir))

    def refuse_bind(self, host="127.0.0.1", port=0):  # type: ignore[no-untyped-def]
        raise OSError("address already in use")

    monkeypatch.setattr(OperatorInterfaceRuntime, "start", refuse_bind)

    with pytest.raises(OSError, match="address already in use"):
        server_module.serve(host="127.0.0.1", port=0)

    assert started, "the fixture never started a publisher"
    for publisher in started:
        running = publisher.health()["running"] if hasattr(publisher, "health") else None
        assert running is not True, (
            f"{type(publisher).__name__} was left running after a failed bind"
        )


def test_mounting_the_pane_does_not_claim_the_logs_channel(log_dir: Path) -> None:
    """A REST pane is not a stream: readiness must not read as published."""

    runtime = OperatorInterfaceRuntime()
    mount_dashboard(runtime, ReadinessBackedProvider({}), logs=_provider(log_dir))
    assert not runtime.is_publisher_registered("LOGS")


def test_default_composition_is_driven_by_one_env_knob(log_dir: Path) -> None:
    runtime = OperatorInterfaceRuntime()
    env = {"ATP_LOG_DIR": str(log_dir)}
    mount_default_dashboard(runtime, env)
    publisher = _mount_logs_arm(runtime, env)

    assert publisher is not None
    pane_status, _pane = runtime.dispatch_rest("GET", LOGS_SNAPSHOT_PATH, b"")
    api_status, api_body = runtime.dispatch_rest("GET", "/api/v1/logs", b"")
    assert pane_status == 200
    assert api_status == 200
    assert [event["message"] for event in api_body["events"]] == ["SPY line stale"]


def test_unset_knob_composes_neither_arm() -> None:
    runtime = OperatorInterfaceRuntime()
    mount_default_dashboard(runtime, {})

    assert log_pane_provider({}) is None
    assert _mount_logs_arm(runtime, {}) is None
    assert runtime.dispatch_rest("GET", LOGS_SNAPSHOT_PATH, b"")[0] == 404
    # The contract route keeps its honest 501 rather than serving a trail that
    # was never configured.
    status, body = runtime.dispatch_rest("GET", "/api/v1/logs", b"")
    assert status == 501
    assert body["error"]["detail"]["owner"] == "SRS-LOG-001"


def test_pane_asset_references_the_route_and_both_classes() -> None:
    """The SPA must actually read the route the mount registers."""

    app_js = (PYTHON_ROOT / "atp_dashboard" / "assets" / "app.js").read_text(encoding="utf-8")
    index = (PYTHON_ROOT / "atp_dashboard" / "assets" / "index.html").read_text(encoding="utf-8")

    assert LOGS_SNAPSHOT_PATH in app_js
    assert 'data-panel="logs"' in index

    # Both classes get their own markup — the pane cannot merge two trails it
    # renders into two independent tables.
    for cls in ("system", "strategy"):
        for suffix in ("rows", "empty", "count", "store", "table"):
            assert f"logs-{cls}-{suffix}" in index, f"logs-{cls}-{suffix} missing from the markup"
    for element in ("logs-coverage", "logs-summary", "logs-note", "logs-severity"):
        assert element in index, f"{element} missing from the pane markup"

    # The SPA addresses the per-class ids by construction, so assert the
    # builders (a literal-id search would pass while nothing rendered).
    assert 'const LOG_CLASSES = ["system", "strategy"]' in app_js
    for suffix in ("rows", "empty", "count", "store", "table"):
        assert f'"logs-" + cls + "-{suffix}"' in app_js, (
            f"the SPA never addresses the {suffix} node"
        )
    for element in ("logs-coverage", "logs-summary", "logs-note", "logs-severity"):
        assert f'$("{element}")' in app_js, f"{element} is never rendered by the SPA"
    # The live channel is routed into the pane, not dropped on the floor.
    assert 'channel === "LOGS"' in app_js


def test_every_handled_channel_is_actually_subscribed() -> None:
    """A handler for a channel the client never subscribes to is dead code.

    This is a general guard, not a LOGS-specific one: it caught the LOGS pane
    shipping with an ``onEvent`` branch and no subscription, where the REST poll
    quietly covered for the missing live path (and would have let the e2e pass
    for the wrong reason).
    """

    app_js = (PYTHON_ROOT / "atp_dashboard" / "assets" / "app.js").read_text(encoding="utf-8")

    subscribe = re.search(r'type:\s*"SUBSCRIBE",\s*channels:\s*\[(.*?)\]', app_js, re.S)
    assert subscribe is not None, "the SPA no longer sends a SUBSCRIBE frame"
    subscribed = set(re.findall(r'"([A-Z_]+)"', subscribe.group(1)))

    on_event = re.search(r"function onEvent\(channel, data\) \{(.*?)\n  \}", app_js, re.S)
    assert on_event is not None, "onEvent is no longer recognisable"
    handled = set(re.findall(r'channel === "([A-Z_]+)"', on_event.group(1)))

    assert "LOGS" in subscribed, "the SPA handles LOGS events but never subscribes to them"
    assert handled <= subscribed, (
        f"channels handled but never subscribed: {sorted(handled - subscribed)}"
    )
