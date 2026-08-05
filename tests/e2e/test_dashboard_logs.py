"""L6 e2e — the SRS-LOG-001 log pane in a real browser (deferred verification).

This is the browser-automation leg of the SRS-LOG-001 acceptance evidence
(Step 2): it opens the live dashboard, and asserts the AC's third clause — "both
log classes are viewable from the dashboard" — actually renders, from two
separate stores, with the SyRS SYS-61 fields visible per record.

It also pins the honesty branches that matter more than the happy path:

* a record published on the ``LOGS`` WebSocket channel appears in its own
  class's table without a re-poll;
* a corrupt store renders an explicit error, never an empty table;
* the producer-coverage strip states, per source, whether anything can write it.

It is **gated** off the parallel suite: ``pytest -m "not e2e"`` skips it, and it
only runs under ``ATP_RUN_E2E=1`` with Playwright browsers installed
(``playwright install chromium``). It binds the dashboard, so it must not run
while sibling agents hold the shared ports — the operator runs it in a
verification window. Passing it is one of the legs required to flip
SRS-LOG-001; the AC's system-log COVERAGE clause additionally needs the five
unproduced sources' producers (see ``log_persistence_contract.deferred``).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

# Guard collection: the import must not error when Playwright is absent — the
# collection-time skip in conftest runs *after* module import.
sync_api = pytest.importorskip("playwright.sync_api")

from atp_dashboard import ReadinessBackedProvider, mount_dashboard  # noqa: E402
from atp_dashboard.logs import LogPaneProvider  # noqa: E402
from atp_logging.persistence import build_separated_log_dispatcher  # noqa: E402
from atp_logging.records import LogClass, LogRecord, Severity, Source  # noqa: E402
from atp_logs_service import wire_logs  # noqa: E402
from atp_runtime import OperatorInterfaceRuntime  # noqa: E402

pytestmark = pytest.mark.e2e

_BASE_NS = 1_700_000_000_000_000_000


def _seed(log_dir: Path) -> None:
    dispatcher, _system, _strategy = build_separated_log_dispatcher(log_dir)
    dispatcher.dispatch(
        LogRecord(
            timestamp_ns=_BASE_NS,
            severity=Severity.CRITICAL,
            source=Source.KILL_SWITCH,
            event_type="ACTIVATION",
            message="operator triggered kill switch",
            correlation_id="ks-e2e-1",
            log_class=LogClass.SYSTEM,
        )
    )
    dispatcher.dispatch(
        LogRecord(
            timestamp_ns=_BASE_NS + 1_000_000_000,
            severity=Severity.INFO,
            source=Source.STRATEGY,
            event_type="SIGNAL",
            message="strategy opened AAPL",
            correlation_id="run-e2e-1",
            log_class=LogClass.STRATEGY,
            strategy_id="sma-crossover",
        )
    )


@pytest.fixture()
def dashboard(tmp_path: Path) -> Iterator[tuple[str, Path, OperatorInterfaceRuntime]]:
    _seed(tmp_path)
    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(
        runtime,
        ReadinessBackedProvider({}),
        logs=LogPaneProvider(
            system_store_path=tmp_path / "system.jsonl",
            strategy_store_path=tmp_path / "strategy.jsonl",
        ),
    )
    logs_publisher = wire_logs(
        runtime,
        system_store_path=tmp_path / "system.jsonl",
        strategy_store_path=tmp_path / "strategy.jsonl",
        poll_interval_s=0.25,
    )
    publisher.start()
    logs_publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield f"http://{host}:{port}/dashboard", tmp_path, runtime
    finally:
        logs_publisher.stop()
        publisher.stop()
        runtime.stop()


def test_both_log_classes_are_viewable_from_the_dashboard(
    dashboard: tuple[str, Path, OperatorInterfaceRuntime],
) -> None:
    """The AC's third clause, proven in a browser."""

    url, _log_dir, _runtime = dashboard
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded")

            assert page.locator('[data-panel="logs"]').count() == 1

            # Each class renders in its OWN table, fed by its own store.
            page.wait_for_selector("#logs-system-rows tr", timeout=5_000)
            page.wait_for_selector("#logs-strategy-rows tr", timeout=5_000)
            system_text = page.locator("#logs-system-rows").inner_text()
            strategy_text = page.locator("#logs-strategy-rows").inner_text()

            assert "operator triggered kill switch" in system_text
            assert "strategy opened AAPL" in strategy_text
            # Neither table shows the other class's record.
            assert "strategy opened AAPL" not in system_text
            assert "operator triggered kill switch" not in strategy_text

            # The six SYS-61 fields are visible on the system record's row.
            for cell in ("CRITICAL", "kill_switch", "ACTIVATION", "ks-e2e-1"):
                assert cell in system_text

            # The store each table read is named, so the operator can see the
            # two trails are two files.
            assert "system.jsonl" in page.locator("#logs-system-store").inner_text()
            assert "strategy.jsonl" in page.locator("#logs-strategy-store").inner_text()

            # Producer coverage is stated, not implied.
            coverage = page.locator("#logs-coverage").inner_text()
            for source in ("order_routing", "ingestion", "container_lifecycle", "hot_swap"):
                assert source in coverage
            assert page.locator('#logs-coverage [data-state="deferred"]').count() >= 5
        finally:
            browser.close()


def test_a_published_log_event_arrives_over_the_logs_channel(
    dashboard: tuple[str, Path, OperatorInterfaceRuntime],
) -> None:
    """The LOGS channel drives the pane live, routed by the record's own class.

    The REST poll would eventually paint the same record, so this asserts the
    WebSocket FRAMES directly: the client must subscribe to ``LOGS`` and must
    receive the record as a ``LOGS`` EVENT. Waiting only on the rendered text
    would pass even with the subscription missing — that is exactly how the
    channel shipped dead in review.
    """

    url, log_dir, _runtime = dashboard
    frames: list[str] = []
    sent: list[str] = []

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.on(
                "websocket",
                lambda ws: (
                    ws.on("framesent", lambda payload: sent.append(str(payload))),
                    ws.on("framereceived", lambda payload: frames.append(str(payload))),
                ),
            )
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_function(
                "document.getElementById('conn').dataset.state === 'open'", timeout=5_000
            )
            page.wait_for_selector("#logs-strategy-rows tr", timeout=5_000)

            # The SUBSCRIBE frame must actually claim the channel.
            subscribe = [frame for frame in sent if "SUBSCRIBE" in frame]
            assert subscribe, "the client sent no SUBSCRIBE frame"
            assert any("LOGS" in frame for frame in subscribe), (
                "the client never subscribed to LOGS — the pane would be poll-only"
            )

            dispatcher, _s, _t = build_separated_log_dispatcher(log_dir)
            dispatcher.dispatch(
                LogRecord(
                    timestamp_ns=_BASE_NS + 2_000_000_000,
                    severity=Severity.WARN,
                    source=Source.STRATEGY,
                    event_type="RISK_LIMIT",
                    message="position limit reached",
                    correlation_id="run-e2e-2",
                    log_class=LogClass.STRATEGY,
                    strategy_id="sma-crossover",
                )
            )

            # The record arrives as a LOGS EVENT frame, not merely as pixels a
            # poll could have produced.
            page.wait_for_function(
                "document.getElementById('logs-strategy-rows').innerText"
                ".includes('position limit reached')",
                timeout=10_000,
            )
            log_events = [
                frame
                for frame in frames
                if '"channel": "LOGS"' in frame.replace('"channel":"LOGS"', '"channel": "LOGS"')
            ]
            assert log_events, "no LOGS EVENT frame was delivered to the client"
            assert any("position limit reached" in frame for frame in log_events), (
                "the new record never arrived over the LOGS channel"
            )
            # The live event landed in the STRATEGY table only.
            assert "position limit reached" not in page.locator("#logs-system-rows").inner_text()

            # ...and it SURVIVES the next REST poll. The poll replaces the class
            # buffer from a snapshot, so an event that arrived after that poll
            # started must be merged rather than overwritten: an audit event that
            # appears and then vanishes is worse than one that arrives late.
            # (The same interleaving is driven deterministically under node in
            # tests/boundary/test_dashboard_logs_interleaving.py.)
            page.wait_for_timeout(5_000)  # > POLL_MS, so at least one poll landed
            assert "position limit reached" in page.locator("#logs-strategy-rows").inner_text()
        finally:
            browser.close()


def test_an_unreadable_store_renders_an_error_not_an_empty_table(tmp_path: Path) -> None:
    """The failure that matters: a corrupt trail must never look quiet."""

    _seed(tmp_path)
    with (tmp_path / "system.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{not json at all}\n")

    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(
        runtime,
        ReadinessBackedProvider({}),
        logs=LogPaneProvider(
            system_store_path=tmp_path / "system.jsonl",
            strategy_store_path=tmp_path / "strategy.jsonl",
        ),
    )
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(f"http://{host}:{port}/dashboard", wait_until="domcontentloaded")
                page.wait_for_function(
                    "document.getElementById('logs-system-empty').innerText"
                    ".includes('unavailable')",
                    timeout=5_000,
                )
                empty = page.locator("#logs-system-empty")
                assert empty.get_attribute("data-tone") == "error"
                assert "NOT an empty trail" in empty.inner_text()
                assert page.locator("#logs-system-table").is_hidden()
                assert "unreadable" in page.locator("#logs-system-count").inner_text()
                # The readable class is unaffected — one bad trail does not
                # blank the other.
                assert "strategy opened AAPL" in page.locator("#logs-strategy-rows").inner_text()
            finally:
                browser.close()
    finally:
        publisher.stop()
        runtime.stop()
