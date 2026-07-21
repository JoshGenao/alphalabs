"""L6 e2e — SRS-UI-001 dashboard in a real browser (deferred verification).

This is the browser-automation leg of the SRS-UI-001 acceptance evidence
(Step 2 / Step 3): it opens the live dashboard in a headless browser, confirms
the four metric panels render, and asserts the self-measured refresh-latency
readout updates within the NFR-P2 5-second budget.

It is **gated** off the parallel suite: ``pytest -m "not e2e"`` skips it, and it
only runs under ``ATP_RUN_E2E=1`` with Playwright browsers installed
(``playwright install chromium``). The 30-paper release-baseline-load performance
run (NFR-P2 under NFR-SC1) is the operator's separate step; passing this plus that
load test is what flips SRS-UI-001 to ``passes:true`` via the ``verified-e2e`` label.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator

import pytest

# Guard collection: the import must not error when Playwright is absent — the
# collection-time skip in conftest runs *after* module import.
sync_api = pytest.importorskip("playwright.sync_api")

from atp_dashboard import ReadinessBackedProvider, mount_dashboard  # noqa: E402
from atp_runtime import OperatorInterfaceRuntime  # noqa: E402

pytestmark = pytest.mark.e2e


@pytest.fixture()
def dashboard_url() -> Iterator[str]:
    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(runtime, ReadinessBackedProvider({}))
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield f"http://{host}:{port}/dashboard"
    finally:
        publisher.stop()
        runtime.stop()


def test_dashboard_renders_panels_and_refreshes_within_5s(dashboard_url: str) -> None:
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(dashboard_url, wait_until="domcontentloaded")

            # The four metric panels render.
            for panel in ("pnl", "metrics", "health", "latency"):
                assert page.locator(f'[data-panel="{panel}"]').count() == 1

            # The WebSocket link goes live.
            page.wait_for_function(
                "document.getElementById('conn').dataset.state === 'open'", timeout=5_000
            )

            # The self-measured refresh-latency readout updates from "—" to a
            # number within the 5-second budget (proves live ≤5s refresh).
            page.wait_for_function(
                "() => { const t = document.getElementById('pulse-value').textContent.trim();"
                " return t && t !== '—' && !Number.isNaN(Number(t.replace(/,/g,''))); }",
                timeout=5_000,
            )

            # EACH required metric panel refreshes within budget — its freshness
            # dot must reach "fresh" (not "stale"/"wait"). A fast channel must not
            # mask a stalled METRICS/benchmark panel.
            for panel in ("pnl", "metrics", "health"):
                page.wait_for_function(
                    f"document.getElementById('fresh-{panel}').dataset.state === 'fresh'",
                    timeout=5_000,
                )

            # The freshness contract fails at the budget boundary: a channel over
            # its 5s budget is NOT 'fresh' (regression for the grace-window bug).
            assert page.evaluate("window.freshnessState(6000, 5000, 1500)") != "fresh"
            assert page.evaluate("window.freshnessState(4000, 5000, 1500)") == "fresh"
            # The self-measured worst-case refresh stays within the NFR-P2 budget.
            observed = page.evaluate(
                "Number(document.getElementById('pulse-value').textContent.replace(/,/g, ''))"
            )
            assert observed <= 5000
        finally:
            browser.close()


@pytest.fixture()
def inventory_dashboard_url(tmp_path) -> Iterator[str]:
    """SRS-UI-002: a dashboard with the strategy-inventory provider mounted over
    a REAL seeded deployment snapshot (record two versions for one strategy plus
    a second strategy through the real orch005_rollback_cli)."""

    import subprocess
    from pathlib import Path

    from atp_dashboard import RollbackSnapshotInventorySource, StrategyInventoryProvider

    root = Path(__file__).resolve().parents[2]
    binary = root / "target" / "debug" / "orch005_rollback_cli"
    if not binary.exists():
        build = subprocess.run(
            ["cargo", "build", "-q", "-p", "atp-orchestrator", "--bin", "orch005_rollback_cli"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if build.returncode != 0:
            pytest.skip(f"cannot build orch005_rollback_cli: {build.stderr}")
    state = tmp_path / "deploy.state"
    for sid, digit, ts in (
        ("alpha-1", "1", "100"),
        ("alpha-1", "2", "200"),
        ("beta-9", "3", "300"),
    ):
        subprocess.run(
            [
                str(binary),
                "record",
                "--state",
                str(state),
                "--strategy",
                sid,
                "--hash",
                "sha256:" + digit * 64,
                "--observed-at",
                ts,
            ],
            check=True,
            capture_output=True,
        )

    runtime = OperatorInterfaceRuntime()
    inventory = StrategyInventoryProvider(
        RollbackSnapshotInventorySource(state_path=state, binary=binary)
    )
    publisher = mount_dashboard(runtime, ReadinessBackedProvider({}), inventory=inventory)
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield f"http://{host}:{port}/dashboard"
    finally:
        publisher.stop()
        runtime.stop()


@pytest.fixture()
def backtest_dashboard_url(tmp_path) -> Iterator[str]:
    """UI-3 / SRS-UI-004: a dashboard with the backtest-history provider mounted
    over a REAL store seeded through the real bt009_store_cli persist."""

    import subprocess
    from pathlib import Path

    from atp_dashboard import BacktestHistoryProvider, StoreCliBacktestHistorySource

    root = Path(__file__).resolve().parents[2]
    binary = root / "target" / "debug" / "bt009_store_cli"
    if not binary.exists():
        build = subprocess.run(
            ["cargo", "build", "-q", "-p", "atp-simulation", "--bin", "bt009_store_cli"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if build.returncode != 0:
            pytest.skip(f"cannot build bt009_store_cli: {build.stderr}")
    results = tmp_path / "results"
    results.mkdir()
    subprocess.run(
        [str(binary), "persist", "--init", "--dir", str(results)],
        check=True,
        capture_output=True,
    )

    runtime = OperatorInterfaceRuntime()
    provider = BacktestHistoryProvider(
        StoreCliBacktestHistorySource(results_dir=results, binary=binary)
    )
    publisher = mount_dashboard(runtime, ReadinessBackedProvider({}), backtests=provider)
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield f"http://{host}:{port}/dashboard"
    finally:
        publisher.stop()
        runtime.stop()


def test_backtest_panel_renders_real_history_and_honest_deferred_launch(
    backtest_dashboard_url: str,
) -> None:
    """UI-3 / SyRS SYS-42 + SYS-43a: the backtest panel lists the REAL persisted
    runs (strategy, params, metrics), drills a row down into the inline
    equity-curve chart + trade log, and the launch control POSTs to the contract
    route — rendering the runtime's honest 501 (deferred), never a fake success."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(backtest_dashboard_url, wait_until="domcontentloaded")

            assert page.locator('[data-panel="backtest"]').count() == 1
            # The controls form is present (SYS-43a).
            for field in ("bt-strategy", "bt-start", "bt-end", "bt-cost", "bt-params"):
                assert page.locator(f"#{field}").count() == 1

            # The REAL persisted runs render, newest first.
            page.wait_for_function(
                "document.querySelectorAll('#bthistory-rows tr').length === 2", timeout=5_000
            )
            row = page.locator('#bthistory-rows tr[data-run="run-momentum"]')
            assert row.count() == 1
            assert "momentum" in row.inner_text()
            # Newest-first ordering: run-meanrev (completed_at 1700000500) sorts
            # above run-momentum (1700000000) — the reorder pass keeps it on top.
            assert (
                page.locator("#bthistory-rows tr").first.get_attribute("data-run") == "run-meanrev"
            )

            # Drill down: the equity-curve chart + trade log render from real data.
            row.click()
            page.wait_for_function(
                "!document.getElementById('backtest-detail').hidden", timeout=5_000
            )
            assert page.locator("#backtest-detail svg.eqchart path.eqchart__line").count() == 1
            assert page.locator("#backtest-detail .bttrades table tbody tr").count() == 2

            # The launch control POSTs to the CONTRACT route and renders the honest
            # 501 deferred outcome — never dressed as a success.
            page.fill("#bt-strategy", "momentum")
            page.click("#bt-run")
            page.wait_for_function(
                "document.getElementById('bt-run-status').dataset.tone === 'deferred'",
                timeout=5_000,
            )
            assert "not yet wired" in page.locator("#bt-run-status").inner_text()
        finally:
            browser.close()


def test_srs_ui_004_backtest_history_lists_ac_fields_and_drills_down(
    backtest_dashboard_url: str,
) -> None:
    """SRS-UI-004 acceptance criteria, clause by clause: the backtest history
    lists strategy, parameters, date range, and metrics for each REAL persisted
    run, and drill-down opens the trade log, equity curve, and benchmark
    comparison — every value traced to the seeded store, never a placeholder."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(backtest_dashboard_url, wait_until="domcontentloaded")

            page.wait_for_function(
                "document.querySelectorAll('#bthistory-rows tr').length === 2", timeout=5_000
            )

            # Before any row is selected the drill-down container is genuinely
            # invisible — the panel's display:grid must not override [hidden]
            # (regression pin for the stray-empty-box bug).
            assert (
                page.eval_on_selector("#backtest-detail", "e => getComputedStyle(e).display")
                == "none"
            )

            # --- AC: "history lists strategy, parameters, date range, metrics" ---
            row = page.locator('#bthistory-rows tr[data-run="run-momentum"]')
            cells = row.locator("td")
            assert cells.nth(0).inner_text() == "run-momentum"
            assert cells.nth(1).inner_text() == "momentum"  # strategy
            params = cells.nth(2).inner_text()  # parameters
            assert "lookback=20" in params and "threshold=0.5" in params
            assert cells.nth(3).inner_text() == "0–100"  # date range (run_window)
            # metrics: Sharpe (ratio), max drawdown + annualized return (pct) —
            # real numbers from the persisted record, never the "—" undefined cell.
            sharpe = cells.nth(4).inner_text()
            assert sharpe != "—"
            assert math.isfinite(float(sharpe.replace("−", "-")))
            for pct_cell in (cells.nth(5), cells.nth(6), cells.nth(7)):
                text = pct_cell.inner_text()
                assert text.endswith("%") and text != "—"

            # --- AC: "drill-down into trade log, equity curve, benchmark comparison" ---
            row.click()
            page.wait_for_function(
                "!document.getElementById('backtest-detail').hidden", timeout=5_000
            )
            # The detail header names the selected run and its provenance.
            assert page.locator("#backtest-detail .btd__title").inner_text() == "run-momentum"
            sub = page.locator("#backtest-detail .btd__sub").inner_text()
            assert "momentum" in sub and "AAPL" in sub and "window 0–100" in sub

            # Equity curve: a real inline-SVG polyline over the 5 persisted points.
            line = page.locator("#backtest-detail svg.eqchart path.eqchart__line")
            assert line.count() == 1
            assert len(line.get_attribute("d") or "") > 0
            assert "5 marks" in page.locator("#backtest-detail .btd__readout").inner_text()

            # Benchmark comparison: the two comparison tiles render real values.
            tiles = page.eval_on_selector_all(
                "#backtest-detail .btstat",
                "els => Object.fromEntries(els.map(e => ["
                "e.querySelector('.btstat__k').textContent,"
                "e.querySelector('.btstat__v').textContent]))",
            )
            assert tiles["Excess vs SPY"].endswith("%") and tiles["Excess vs SPY"] != "—"
            assert tiles["Beta vs benchmark"] != "—"
            float(tiles["Beta vs benchmark"].replace("−", "-"))  # parses as a number

            # Trade log: both persisted fills render with real money columns.
            assert "Trade log (2 fills)" in (
                page.locator("#backtest-detail .bttrades .btd__section-label").text_content() or ""
            )
            trade_rows = page.locator("#backtest-detail .bttrades table tbody tr")
            assert trade_rows.count() == 2
            for i in range(2):
                price = trade_rows.nth(i).locator("td").nth(3).inner_text()
                assert price.startswith("$")
        finally:
            browser.close()


def test_strategy_inventory_panel_renders_real_versions_and_honest_deferred_cells(
    inventory_dashboard_url: str,
) -> None:
    """SRS-UI-002 / SyRS SYS-41: the inventory panel lists each recorded strategy
    with its REAL deployed version identifier (SYS-79) while the AC fields whose
    producers are unbuilt render as explicit deferred cells — and the panel's
    freshness dot reaches "fresh" within the NFR-P2 budget."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(inventory_dashboard_url, wait_until="domcontentloaded")

            assert page.locator('[data-panel="strategies"]').count() == 1
            page.wait_for_function(
                "document.getElementById('conn').dataset.state === 'open'", timeout=5_000
            )
            # Both strategies render with their REAL version identifiers.
            page.wait_for_function(
                "document.querySelectorAll('#inventory-rows tr').length === 2", timeout=5_000
            )
            row = page.locator('#inventory-rows tr[data-strategy="alpha-1"]')
            assert row.count() == 1
            row_text = row.inner_text()
            assert "sha256:" + "2" * 64 + "@200" in row_text
            # The deferred AC cells render as explicit placeholders, not numbers.
            assert "—" in row_text
            # The summary names the honest state.
            summary = page.locator("#inventory-summary").inner_text()
            assert "2 strategies" in summary
            # The inventory panel's own freshness dot reaches fresh (≤5s cadence).
            page.wait_for_function(
                "document.getElementById('fresh-strategies').dataset.state === 'fresh'",
                timeout=7_000,
            )
        finally:
            browser.close()


@pytest.fixture()
def account_reservoir_dashboard_url() -> Iterator[str]:
    from atp_dashboard import AccountStatusProvider, ReservoirRankingProvider

    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(
        runtime,
        ReadinessBackedProvider({}),
        account=AccountStatusProvider(),
        reservoir=ReservoirRankingProvider(),
    )
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield f"http://{host}:{port}/dashboard"
    finally:
        publisher.stop()
        runtime.stop()


def test_account_and_reservoir_panels_render_honest_deferred(
    account_reservoir_dashboard_url: str,
) -> None:
    """SRS-UI-003 / SyRS SYS-43b + SYS-48: the account + Reservoir panels render.
    Every account/ranking value is an explicit deferred cell (no fabrication),
    the SYS-48 evaluation-window selector is a REAL control (1/7/15/30/60/90,
    default 30), and each panel's freshness dot reaches "fresh" within the
    NFR-P2 budget while staying OFF the pulse gauge."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(account_reservoir_dashboard_url, wait_until="domcontentloaded")

            assert page.locator('[data-panel="account"]').count() == 1
            assert page.locator('[data-panel="reservoir"]').count() == 1
            page.wait_for_function(
                "document.getElementById('conn').dataset.state === 'open'", timeout=5_000
            )

            # The account hero equity + cells render explicit deferred placeholders.
            page.wait_for_function(
                "() => document.querySelector('.account__hero .metric__value').textContent.trim() === '—'",
                timeout=5_000,
            )
            # The IB connection pill honestly reports the deferred producer.
            page.wait_for_function(
                "() => document.getElementById('account-conn-pill').dataset.state === 'deferred'",
                timeout=5_000,
            )
            account_text = page.locator('[data-panel="account"]').inner_text()
            assert "SRS-EXE-006" in account_text  # names the deferred owner
            assert "—" in account_text

            # The SYS-48 evaluation-window selector is a REAL control.
            options = page.eval_on_selector_all(
                "#resv-window option", "els => els.map(e => e.value)"
            )
            assert options == ["1", "7", "15", "30", "60", "90"]
            assert page.eval_on_selector("#resv-window", "e => e.value") == "30"
            # The ranking is honestly deferred (not an empty "0 strategies" table).
            page.wait_for_function(
                "() => document.getElementById('reservoir-table').hidden === true", timeout=5_000
            )
            summary = page.locator("#reservoir-summary").inner_text()
            assert "SRS-RESV-002" in summary

            # Both panels' freshness dots reach fresh (≤5s ticks arrive) — and they
            # are OFF the NFR-P2 pulse gauge (a bare mount must not read as a breach).
            for panel in ("account", "reservoir"):
                page.wait_for_function(
                    f"document.getElementById('fresh-{panel}').dataset.state === 'fresh'",
                    timeout=7_000,
                )
        finally:
            browser.close()


@pytest.fixture()
def operations_view_url(tmp_path) -> Iterator[str]:
    """UI-1: the FULL primary operations view through the PRODUCTION
    composition — ``mount_default_dashboard`` (what ``python -m atp_dashboard``
    runs) over a REAL seeded deployment snapshot (ATP_DEPLOYMENT_STATE) and a
    REAL seeded backtest store (ATP_BACKTEST_RESULTS_DIR); account / Reservoir /
    alerts are the honest-deferred providers the entrypoint always composes."""

    import subprocess
    from pathlib import Path

    from atp_dashboard import mount_default_dashboard

    root = Path(__file__).resolve().parents[2]

    def _built(package: str, name: str) -> Path:
        binary = root / "target" / "debug" / name
        if not binary.exists():
            build = subprocess.run(
                ["cargo", "build", "-q", "-p", package, "--bin", name],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
            )
            if build.returncode != 0:
                pytest.skip(f"cannot build {name}: {build.stderr}")
        return binary

    rollback = _built("atp-orchestrator", "orch005_rollback_cli")
    state = tmp_path / "deploy.state"
    subprocess.run(
        [
            str(rollback),
            "record",
            "--state",
            str(state),
            "--strategy",
            "alpha-1",
            "--hash",
            "sha256:" + "1" * 64,
            "--observed-at",
            "100",
        ],
        check=True,
        capture_output=True,
    )

    bt009 = _built("atp-simulation", "bt009_store_cli")
    results = tmp_path / "results"
    results.mkdir()
    subprocess.run(
        [str(bt009), "persist", "--init", "--dir", str(results)],
        check=True,
        capture_output=True,
    )

    runtime = OperatorInterfaceRuntime()
    publisher = mount_default_dashboard(
        runtime,
        {
            "ATP_DEPLOYMENT_STATE": str(state),
            "ATP_BACKTEST_RESULTS_DIR": str(results),
        },
    )
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield f"http://{host}:{port}/dashboard"
    finally:
        publisher.stop()
        runtime.stop()


def test_ui_1_primary_operations_view_covers_every_ac_surface(
    operations_view_url: str,
) -> None:
    """UI-1 acceptance criteria, surface by surface, in ONE browser view over
    HTTP only (no SSH): live strategy status, IB account equity / buying power /
    margin, Reservoir rankings, heartbeat state, and active critical alerts.
    Producer-deferred surfaces render explicit awaiting states naming their
    owner features — never a fabricated value, never "0 active alerts"."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(operations_view_url, wait_until="domcontentloaded")

            # One primary view: all ten panels present.
            for panel in (
                "pnl",
                "metrics",
                "health",
                "latency",
                "strategies",
                "backtest",
                "account",
                "reservoir",
                "research",
                "alerts",
            ):
                assert page.locator(f'[data-panel="{panel}"]').count() == 1

            # System health: the WS link and heartbeat state go live.
            page.wait_for_function(
                "document.getElementById('conn').dataset.state === 'open'", timeout=5_000
            )
            page.wait_for_function(
                "document.getElementById('fresh-health').dataset.state === 'fresh'",
                timeout=7_000,
            )
            # The readiness findings are operator-READABLE (ERR-9: the failure
            # is inspectable from the dashboard) — structured records render as
            # "key — reason", never String(object).
            page.wait_for_function(
                "document.querySelectorAll('#health-notes li').length > 0", timeout=5_000
            )
            notes_text = page.locator("#health-notes").inner_text()
            assert "[object Object]" not in notes_text
            assert "ATP_ENV" in notes_text  # the real finding, readable

            # Live strategy status: the REAL recorded deployment renders.
            page.wait_for_function(
                "document.querySelectorAll('#inventory-rows tr').length === 1", timeout=5_000
            )
            assert (
                "sha256:" + "1" * 64
                in page.locator('#inventory-rows tr[data-strategy="alpha-1"]').inner_text()
            )

            # IB account equity / buying power / margin: cells present, honest
            # deferred (value "—", producer SRS-EXE-006 named), never a number.
            account_text = page.locator('[data-panel="account"]').inner_text()
            assert "SRS-EXE-006" in account_text and "—" in account_text
            for field in ("equity", "buying_power", "margin_usage"):
                assert page.locator(f'[data-panel="account"] [data-field="{field}"]').count() == 1

            # Reservoir rankings: honest awaiting state naming SRS-RESV-002,
            # with the REAL SYS-48 evaluation-window control.
            page.wait_for_function(
                "document.getElementById('reservoir-table').hidden === true", timeout=5_000
            )
            assert "SRS-RESV-002" in page.locator("#reservoir-summary").inner_text()
            assert page.eval_on_selector("#resv-window", "e => e.value") == "30"

            # Active critical alerts: the pane reaches its explicit awaiting
            # state naming SRS-NOTIF-001 — and never claims "0 active alerts"
            # while the detection feed is unwired.
            page.wait_for_function(
                "document.getElementById('alerts-summary').dataset.tone === 'warn'",
                timeout=5_000,
            )
            alerts_summary = page.locator("#alerts-summary").inner_text()
            assert "SRS-NOTIF-001" in alerts_summary
            assert "active critical alert" not in alerts_summary
            assert page.eval_on_selector("#alerts-table", "e => e.hidden") is True
            assert page.eval_on_selector("#alerts-beacon", "e => e.dataset.state") == "deferred"
            # The alerts dot must NOT read "fresh" while the producer is
            # deferred — placeholder-poll health is not alert-monitoring
            # health. It holds the honest awaiting state naming the owner.
            assert page.eval_on_selector("#fresh-alerts", "e => e.dataset.state") == "wait"
            assert "SRS-NOTIF-001" in (page.eval_on_selector("#fresh-alerts", "e => e.title") or "")

            # The stylesheet actually APPLIES (a malformed rule earlier in the
            # sheet would silently drop these): the deferred beacon renders its
            # dashed frame, and the 64-hex deployed-version hash wraps instead
            # of overflowing the strategy-inventory panel sideways.
            assert (
                page.eval_on_selector("#alerts-beacon", "e => getComputedStyle(e).borderTopStyle")
                == "dashed"
            )
            assert page.eval_on_selector(
                '[data-panel="strategies"]', "e => e.scrollWidth <= e.clientWidth"
            )
        finally:
            browser.close()


def test_ui_1_alerts_pane_reports_endpoint_failure_never_stale_state(
    operations_view_url: str,
) -> None:
    """UI-1 degraded path: when the alerts endpoint fails (5xx), the pane must
    render its explicit unavailable state — never leave a stale summary/beacon
    on a safety-critical pane — and recover once the endpoint is healthy."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(operations_view_url, wait_until="domcontentloaded")
            # Healthy first: the deferred awaiting state renders.
            page.wait_for_function(
                "document.getElementById('alerts-summary').dataset.tone === 'warn'",
                timeout=5_000,
            )

            # Break the endpoint: every poll now returns 503.
            page.route(
                "**/dashboard/api/alerts",
                lambda route: route.fulfill(status=503, body="upstream down"),
            )
            page.wait_for_function(
                "document.getElementById('alerts-summary').dataset.tone === 'error'",
                timeout=7_000,
            )
            summary = page.locator("#alerts-summary").inner_text()
            assert "unavailable" in summary and "503" in summary
            assert page.eval_on_selector("#alerts-beacon", "e => e.dataset.state") == "error"
            assert page.eval_on_selector("#alerts-table", "e => e.hidden") is True
            # The dot flags the failing endpoint too — never a healthy read.
            assert page.eval_on_selector("#fresh-alerts", "e => e.dataset.state") == "stale"

            # Route DISAPPEARANCE fails closed too: a 404 (provider no longer
            # composed) clears the table/beacon/dot instead of leaving stale
            # safety state under a "not mounted" caption.
            page.unroute("**/dashboard/api/alerts")
            page.route(
                "**/dashboard/api/alerts",
                lambda route: route.fulfill(status=404, body="not found"),
            )
            page.wait_for_function(
                "document.getElementById('alerts-summary').textContent.includes('not mounted')",
                timeout=7_000,
            )
            assert page.eval_on_selector("#alerts-table", "e => e.hidden") is True
            assert page.eval_on_selector("#alerts-beacon", "e => e.dataset.state") == "deferred"
            assert page.eval_on_selector("#fresh-alerts", "e => e.dataset.state") == "wait"

            # Heal the endpoint: the pane recovers to the honest awaiting state.
            page.unroute("**/dashboard/api/alerts")
            page.wait_for_function(
                "document.getElementById('alerts-summary').dataset.tone === 'warn'"
                " && document.getElementById('alerts-summary').textContent.includes('SRS-NOTIF-001')",
                timeout=7_000,
            )
        finally:
            browser.close()


def test_ui_1_alerts_real_feed_counts_string_false_ack_as_active(
    operations_view_url: str,
) -> None:
    """UI-1 real-feed semantics (pinned ahead of the SRS-NOTIF-001 provider
    swap): the contract types alert fields as strings, so acknowledgement must
    be parsed FAIL-CLOSED — ``"false"`` (and any unknown shape) counts as an
    ACTIVE alert; only an explicit true acknowledges. A truthiness check would
    render a false all-clear over an unacknowledged CRITICAL alert."""

    feed_body = (
        '{"generated_at": "2026-07-16T00:00:00Z", "ok": true, "srs_ref": "UI-1",'
        ' "feed": {"value": "live", "data_source": "live"},'
        ' "alerts": ['
        '{"alert_id": "alert-1", "raised_at": "2026-07-16T00:00:01Z",'
        ' "severity": "CRITICAL", "channel": "EMAIL",'
        ' "delivery_status": "SENT", "acknowledged": "false"},'
        '{"alert_id": "alert-2", "raised_at": "2026-07-16T00:00:02Z",'
        ' "severity": "ERROR", "channel": "SMS",'
        ' "delivery_status": "SENT", "acknowledged": "true"}'
        "]}"
    )

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.route(
                "**/dashboard/api/alerts",
                lambda route: route.fulfill(
                    status=200, content_type="application/json", body=feed_body
                ),
            )
            page.goto(operations_view_url, wait_until="domcontentloaded")

            page.wait_for_function(
                "document.querySelectorAll('#alerts-rows tr').length === 2", timeout=7_000
            )
            summary = page.locator("#alerts-summary").inner_text()
            # "false" (string) is NOT acknowledged: exactly one ACTIVE alert.
            assert "1 active critical alert" in summary
            assert page.eval_on_selector("#alerts-beacon", "e => e.dataset.state") == "alarm"
            # The rows render the ack column fail-closed: no / YES.
            acks = page.eval_on_selector_all(
                "#alerts-rows .alert-ack", "els => els.map(e => e.textContent)"
            )
            assert acks == ["no", "YES"]
            # With a LIVE feed the dot may honestly read fresh.
            assert page.eval_on_selector("#fresh-alerts", "e => e.dataset.state") == "fresh"
        finally:
            browser.close()


def test_ui_1_alerts_malformed_live_feed_fails_closed(operations_view_url: str) -> None:
    """UI-1: a live feed cell whose alert list is missing/malformed (version
    skew, partial rollout) must render the explicit unavailable state — never
    a coerced "0 active critical alerts" all-clear."""

    malformed = (
        '{"generated_at": "2026-07-16T00:00:00Z", "ok": true, "srs_ref": "UI-1",'
        ' "feed": {"value": "live", "data_source": "live"}, "alerts": "oops"}'
    )
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.route(
                "**/dashboard/api/alerts",
                lambda route: route.fulfill(
                    status=200, content_type="application/json", body=malformed
                ),
            )
            page.goto(operations_view_url, wait_until="domcontentloaded")
            page.wait_for_function(
                "document.getElementById('alerts-summary').dataset.tone === 'error'",
                timeout=7_000,
            )
            summary = page.locator("#alerts-summary").inner_text()
            assert "unavailable" in summary and "malformed" in summary
            assert "active critical alert" not in summary
            assert page.eval_on_selector("#alerts-beacon", "e => e.dataset.state") == "error"
            assert page.eval_on_selector("#alerts-table", "e => e.hidden") is True
        finally:
            browser.close()


def test_ui_2_strategy_management_view_covers_every_ac_surface(
    operations_view_url: str,
) -> None:
    """UI-2 acceptance criteria over the PRODUCTION composition: the strategy
    management view lists the recorded strategy with its REAL deployed code
    version; mode, asset class, container status, and the key metrics render as
    explicit deferred cells naming their owner features; the per-row PROMOTE
    LIVE control is present; and the designation readout holds its honest
    deferred state — never an all-clear-shaped "no live strategy"."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(operations_view_url, wait_until="domcontentloaded")

            # (inner_text reflects the CSS-uppercased rendering.)
            assert (
                "strategy management"
                in page.locator('[data-panel="strategies"] .panel__title').inner_text().lower()
            )

            page.wait_for_function(
                "document.querySelectorAll('#inventory-rows tr').length === 1", timeout=5_000
            )
            row = page.locator('#inventory-rows tr[data-strategy="alpha-1"]')
            row_text = row.inner_text()
            # Deployed code version: REAL.
            assert "sha256:" + "1" * 64 in row_text
            # Mode / asset / container / P&L / positions: explicit deferred
            # cells, each srctag naming an owner whose work is still deferred.
            owners = page.eval_on_selector_all(
                '#inventory-rows tr[data-strategy="alpha-1"] .srctag',
                "els => els.map(e => e.textContent)",
            )
            for owner in (
                "SRS-EXE-001",
                "SRS-API-001",
                "SRS-ORCH-002",
                "SRS-BT-004",
                "SRS-SIM-004",
            ):
                assert owner in owners, (owner, owners)
            assert "—" in row_text

            # The management affordance: one PROMOTE LIVE control on the row.
            btn = row.locator(".manage__btn")
            assert btn.count() == 1
            assert btn.inner_text().strip().upper() == "PROMOTE LIVE"
            assert (
                page.eval_on_selector("#inventory-rows .manage__btn", "e => e.dataset.armed")
                == "false"
            )

            # The designation readout: honest deferred copy naming the owner,
            # dashed awaiting-producer frame, and never "no live strategy".
            designation = page.locator("#designation-status").inner_text()
            assert "SRS-EXE-001" in designation
            assert "no live strategy" not in designation.lower()
            assert page.eval_on_selector("#designation-state", "e => e.dataset.state") == "deferred"
            assert (
                page.eval_on_selector(
                    "#designation-state", "e => getComputedStyle(e).borderTopStyle"
                )
                == "dashed"
            )
            # The management additions keep the panel inside its box.
            assert page.eval_on_selector(
                '[data-panel="strategies"]', "e => e.scrollWidth <= e.clientWidth"
            )
        finally:
            browser.close()


def _arm_promote(page, btn) -> None:
    """Click PROMOTE LIVE until the armed state sticks. A 5 s STRATEGY_STATE
    tick may rebuild the row between click and assertion (disarm-on-upsert is
    deliberate UI-2 behavior), so arming retries instead of flaking."""
    for _ in range(4):
        btn.click()
        try:
            # One-armed-at-a-time is an invariant, so "any armed button" is
            # exactly the one just clicked — and this stays correct when the
            # clicked row is not the table's first.
            page.wait_for_function(
                "document.querySelector('.manage__btn[data-armed=\"true\"]') !== null",
                timeout=1_000,
            )
            return
        except Exception:  # noqa: BLE001 — a tick disarmed us; try again
            continue
    raise AssertionError("PROMOTE LIVE could not be armed")


def _confirm_promote(page) -> None:
    """Arm-then-confirm, retrying if an inventory tick voids the staged arm
    between the two clicks (in which case no POST fires, by design)."""
    btn = page.locator('#inventory-rows tr[data-strategy="alpha-1"] .manage__btn')
    for _ in range(4):
        _arm_promote(page, btn)
        btn.click()
        try:
            page.wait_for_function(
                "document.querySelector('#inventory-rows .manage__btn').dataset.armed === 'false'",
                timeout=2_000,
            )
        except Exception:  # noqa: BLE001
            continue
        if page.eval_on_selector("#designation-state", "e => e.dataset.state") != "deferred":
            return  # the confirm click fired (pending/error/live — not resting)
    raise AssertionError("PROMOTE LIVE confirm click never fired")


def test_ui_2_promote_live_requires_explicit_confirmation(
    operations_view_url: str,
) -> None:
    """UI-2 / NFR-S2 / SYS-2c: live designation is a two-step arm-then-confirm
    flow. A single click stages the candidate (naming the exact strategy id)
    and fires NO network request; the arm window auto-disarms; the confirmed
    click POSTs once to the CONTRACT route with the confirmation token, and the
    un-wired runtime's 501 HANDLER_DEFERRED (owner SRS-EXE-001) renders as an
    explicit refusal — the row is never marked live."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            posts: list[str] = []
            page.on(
                "request",
                lambda req: posts.append(req.url) if req.method == "POST" else None,
            )
            page.goto(operations_view_url, wait_until="domcontentloaded")
            page.wait_for_function(
                "document.querySelectorAll('#inventory-rows tr').length === 1", timeout=5_000
            )

            # Click 1: arm — no POST leaves the page. (One atomic read of the
            # armed facts so a 5 s inventory tick can't race the assertions.)
            btn = page.locator('#inventory-rows tr[data-strategy="alpha-1"] .manage__btn')
            _arm_promote(page, btn)
            armed = page.evaluate(
                "() => {"
                " const b = document.querySelector('#inventory-rows .manage__btn');"
                " return {"
                "  armed: b.dataset.armed,"
                "  label: b.textContent,"
                "  state: document.getElementById('designation-state').dataset.state,"
                "  status: document.getElementById('designation-status').textContent,"
                "  rowStaged: b.closest('tr').classList.contains('manage-armed'),"
                " };"
                "}"
            )
            assert armed["armed"] == "true"
            assert "CONFIRM LIVE: alpha-1?" in armed["label"]
            assert "alpha-1" in armed["status"] and "confirm" in armed["status"]
            assert armed["state"] == "armed"
            # The staged row locks focus (armed choreography applied).
            assert armed["rowStaged"] is True
            assert not [u for u in posts if "promote-live" in u]

            # The arm window auto-disarms back to the honest deferred state.
            page.wait_for_function(
                "document.querySelector('#inventory-rows .manage__btn').dataset.armed === 'false'",
                timeout=7_000,
            )
            assert page.eval_on_selector("#designation-state", "e => e.dataset.state") == "deferred"
            assert not [u for u in posts if "promote-live" in u]

            # Arm-then-confirm within the window: exactly ONE POST to the
            # contract route with the confirmation token — answered 501 by
            # THIS runtime.
            _confirm_promote(page)
            page.wait_for_function(
                "document.getElementById('designation-state').dataset.state === 'error'",
                timeout=7_000,
            )
            promote_posts = [u for u in posts if "promote-live" in u]
            assert len(promote_posts) == 1
            assert promote_posts[0].endswith("/api/v1/strategies/alpha-1/promote-live?confirm=true")
            refusal = page.locator("#designation-status").inner_text()
            assert "REFUSED 501" in refusal and "HANDLER_DEFERRED" in refusal
            assert "SRS-EXE-001" in refusal
            # The refusal is never dressed as success: no live marking anywhere.
            row_text = page.locator('#inventory-rows tr[data-strategy="alpha-1"]').inner_text()
            assert "live" not in row_text.split("sha256:")[0].lower()
            assert (
                page.eval_on_selector("#inventory-rows .manage__btn", "e => e.dataset.armed")
                == "false"
            )
        finally:
            browser.close()


def test_ui_2_promote_live_renders_refusals_and_success_honestly(
    operations_view_url: str,
) -> None:
    """UI-2 response semantics (pinned ahead of the SRS-EXE-001 handler): a 428
    renders as a refusal; a 200 renders ONLY the runtime's own fields and only
    an explicit ``is_live: true`` reads as designated (a 200 without it renders
    as NOT designated — fail-closed); a transport failure renders FAILED with
    the outcome marked unknown; and no branch ever flips the deferred Mode
    cell — the POST outcome is not the mode producer."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(operations_view_url, wait_until="domcontentloaded")
            page.wait_for_function(
                "document.querySelectorAll('#inventory-rows tr').length === 1", timeout=5_000
            )

            # 428 (transport confirmation guard) renders as a refusal.
            page.route(
                "**/api/v1/strategies/*/promote-live*",
                lambda route: route.fulfill(
                    status=428,
                    content_type="application/json",
                    body='{"error": {"category": "CONFIRMATION_REQUIRED",'
                    ' "type": "CONFIRMATION_REQUIRED"}}',
                ),
            )
            _confirm_promote(page)
            page.wait_for_function(
                "document.getElementById('designation-state').dataset.state === 'error'",
                timeout=7_000,
            )
            assert "REFUSED 428" in page.locator("#designation-status").inner_text()

            # A 200 WITHOUT an explicit is_live=true is NOT a designation.
            page.unroute("**/api/v1/strategies/*/promote-live*")
            page.route(
                "**/api/v1/strategies/*/promote-live*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"strategy_id": "alpha-1", "is_live": "true",'
                    ' "promoted_at": "2026-07-17T00:00:00Z"}',
                ),
            )
            _confirm_promote(page)
            page.wait_for_function(
                "document.getElementById('designation-state').dataset.state === 'error'",
                timeout=7_000,
            )
            not_designated = page.locator("#designation-status").inner_text()
            assert "NOT designated" in not_designated and "is_live" in not_designated

            # A 200 naming a DIFFERENT strategy_id is NOT a designation either —
            # the NFR-S2 confirmation is bound to one exact strategy, so a
            # misrouted/stale success for another strategy renders as an error.
            page.unroute("**/api/v1/strategies/*/promote-live*")
            page.route(
                "**/api/v1/strategies/*/promote-live*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"strategy_id": "other-9", "is_live": true,'
                    ' "promoted_at": "2026-07-17T00:00:00Z"}',
                ),
            )
            _confirm_promote(page)
            page.wait_for_function(
                "document.getElementById('designation-state').dataset.state === 'error'",
                timeout=7_000,
            )
            mismatched = page.locator("#designation-status").inner_text()
            assert "NOT designated" in mismatched
            assert "other-9" in mismatched and "alpha-1" in mismatched

            # A real 200 with boolean is_live renders the runtime's own fields.
            page.unroute("**/api/v1/strategies/*/promote-live*")
            page.route(
                "**/api/v1/strategies/*/promote-live*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"strategy_id": "alpha-1", "is_live": true,'
                    ' "promoted_at": "2026-07-17T00:00:00Z"}',
                ),
            )
            _confirm_promote(page)
            page.wait_for_function(
                "document.getElementById('designation-state').dataset.state === 'live'",
                timeout=7_000,
            )
            confirmed = page.locator("#designation-status").inner_text()
            assert "alpha-1" in confirmed and "2026-07-17T00:00:00Z" in confirmed
            # Even a confirmed response never flips the deferred Mode cell —
            # its producer is the durable designation state, not this control.
            mode_owner = page.eval_on_selector(
                '#inventory-rows tr[data-strategy="alpha-1"] td:nth-child(2) .srctag',
                "e => e.textContent",
            )
            assert mode_owner == "SRS-EXE-001"
            assert (
                page.eval_on_selector(
                    '#inventory-rows tr[data-strategy="alpha-1"] td:nth-child(2) .metric__value',
                    "e => e.textContent",
                ).strip()
                == "—"
            )

            # Transport failure: FAILED with the outcome marked unknown, and
            # the control recovers (no stale armed state, button re-enabled).
            page.unroute("**/api/v1/strategies/*/promote-live*")
            page.route(
                "**/api/v1/strategies/*/promote-live*",
                lambda route: route.abort("connectionrefused"),
            )
            _confirm_promote(page)
            page.wait_for_function(
                "document.getElementById('designation-state').dataset.state === 'error'",
                timeout=7_000,
            )
            failed = page.locator("#designation-status").inner_text()
            assert "FAILED" in failed and "unknown" in failed
            assert page.eval_on_selector("#inventory-rows .manage__btn", "e => e.disabled") is False
            assert (
                page.eval_on_selector("#inventory-rows .manage__btn", "e => e.dataset.armed")
                == "false"
            )
        finally:
            browser.close()


@pytest.fixture()
def shrinking_inventory_dashboard(tmp_path) -> Iterator[tuple]:
    """UI-2 row lifecycle: a dashboard over a REAL deployment snapshot whose
    state file the test can rewrite mid-run, so the next real 5 s tick shrinks
    (or breaks) the inventory the panel renders."""

    import subprocess
    from pathlib import Path

    from atp_dashboard import RollbackSnapshotInventorySource, StrategyInventoryProvider

    root = Path(__file__).resolve().parents[2]
    binary = root / "target" / "debug" / "orch005_rollback_cli"
    if not binary.exists():
        build = subprocess.run(
            ["cargo", "build", "-q", "-p", "atp-orchestrator", "--bin", "orch005_rollback_cli"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if build.returncode != 0:
            pytest.skip(f"cannot build orch005_rollback_cli: {build.stderr}")

    def _seed(path, strategies) -> None:
        for sid, digit, ts in strategies:
            subprocess.run(
                [
                    str(binary),
                    "record",
                    "--state",
                    str(path),
                    "--strategy",
                    sid,
                    "--hash",
                    "sha256:" + digit * 64,
                    "--observed-at",
                    ts,
                ],
                check=True,
                capture_output=True,
            )

    state = tmp_path / "deploy.state"
    _seed(state, (("alpha-1", "1", "100"), ("beta-9", "3", "300")))
    shrunk = tmp_path / "shrunk.state"
    _seed(shrunk, (("alpha-1", "1", "100"),))

    runtime = OperatorInterfaceRuntime()
    inventory = StrategyInventoryProvider(
        RollbackSnapshotInventorySource(state_path=state, binary=binary)
    )
    publisher = mount_dashboard(runtime, ReadinessBackedProvider({}), inventory=inventory)
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield f"http://{host}:{port}/dashboard", state, shrunk
    finally:
        publisher.stop()
        runtime.stop()


def test_ui_2_removed_strategy_loses_its_promote_control(
    shrinking_inventory_dashboard,
) -> None:
    """UI-2 row lifecycle (fail-closed): a strategy that leaves the ACTIVE
    inventory loses its row — an armed PROMOTE LIVE control must not survive on
    a strategy the current inventory no longer contains — and an UNREADABLE
    inventory clears every actionable row rather than keeping stale ones under
    an error caption."""

    url, state, shrunk = shrinking_inventory_dashboard
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_function(
                "document.querySelectorAll('#inventory-rows tr').length === 2", timeout=7_000
            )

            # Arm the strategy that is about to disappear.
            _arm_promote(
                page, page.locator('#inventory-rows tr[data-strategy="beta-9"] .manage__btn')
            )

            # The REAL snapshot shrinks: the next 5 s tick drops beta-9.
            state.write_bytes(shrunk.read_bytes())
            page.wait_for_function(
                "document.querySelectorAll('#inventory-rows tr').length === 1", timeout=12_000
            )
            assert page.locator('#inventory-rows tr[data-strategy="beta-9"]').count() == 0
            assert page.locator('#inventory-rows tr[data-strategy="alpha-1"]').count() == 1
            # No armed control survives anywhere; the readout is back to resting.
            assert page.locator('.manage__btn[data-armed="true"]').count() == 0
            assert page.eval_on_selector("#designation-state", "e => e.dataset.state") == "deferred"
            summary = page.locator("#inventory-summary").inner_text()
            assert "1 strategy" in summary

            # The snapshot becomes UNREADABLE: rows clear, table hides — never
            # stale actionable rows under an error caption.
            state.write_text("corrupted\n", encoding="utf-8")
            page.wait_for_function(
                "document.getElementById('inventory-summary').dataset.tone === 'error'",
                timeout=12_000,
            )
            assert page.locator("#inventory-rows tr").count() == 0
            assert page.eval_on_selector("#inventory-table", "e => e.hidden") is True
            assert "unavailable" in page.locator("#inventory-summary").inner_text()
        finally:
            browser.close()


@pytest.fixture()
def poll_only_inventory_dashboard(tmp_path) -> Iterator[str]:
    """UI-2 degraded-path harness: the inventory dashboard with the WS
    publisher deliberately NOT started, so the REST poll is the only inventory
    transport and endpoint failures are deterministic to observe."""

    import subprocess
    from pathlib import Path

    from atp_dashboard import RollbackSnapshotInventorySource, StrategyInventoryProvider

    root = Path(__file__).resolve().parents[2]
    binary = root / "target" / "debug" / "orch005_rollback_cli"
    if not binary.exists():
        build = subprocess.run(
            ["cargo", "build", "-q", "-p", "atp-orchestrator", "--bin", "orch005_rollback_cli"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if build.returncode != 0:
            pytest.skip(f"cannot build orch005_rollback_cli: {build.stderr}")
    state = tmp_path / "deploy.state"
    for sid, digit, ts in (("alpha-1", "1", "100"), ("beta-9", "3", "300")):
        subprocess.run(
            [
                str(binary),
                "record",
                "--state",
                str(state),
                "--strategy",
                sid,
                "--hash",
                "sha256:" + digit * 64,
                "--observed-at",
                ts,
            ],
            check=True,
            capture_output=True,
        )

    runtime = OperatorInterfaceRuntime()
    inventory = StrategyInventoryProvider(
        RollbackSnapshotInventorySource(state_path=state, binary=binary)
    )
    publisher = mount_dashboard(runtime, ReadinessBackedProvider({}), inventory=inventory)
    # publisher deliberately NOT started — REST poll only.
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield f"http://{host}:{port}/dashboard"
    finally:
        publisher.stop()
        runtime.stop()


def test_ui_2_inventory_endpoint_failure_clears_promote_controls(
    poll_only_inventory_dashboard: str,
) -> None:
    """UI-2 degraded paths fail closed: after rows (and an armed PROMOTE LIVE)
    render, a 404 (provider no longer composed) clears every actionable row —
    not just the caption; an unreachable endpoint clears them too with an
    explicit error; and a healthy endpoint repopulates the view."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(poll_only_inventory_dashboard, wait_until="domcontentloaded")
            page.wait_for_function(
                "document.querySelectorAll('#inventory-rows tr').length === 2", timeout=7_000
            )
            _arm_promote(
                page, page.locator('#inventory-rows tr[data-strategy="beta-9"] .manage__btn')
            )

            # Route disappearance: rows AND the armed control go, honestly.
            page.route(
                "**/dashboard/api/strategies",
                lambda route: route.fulfill(status=404, body="not found"),
            )
            page.wait_for_function(
                "document.querySelectorAll('#inventory-rows tr').length === 0", timeout=20_000
            )
            assert page.locator(".manage__btn").count() == 0
            assert page.eval_on_selector("#inventory-table", "e => e.hidden") is True
            assert "not mounted" in page.locator("#inventory-summary").inner_text()
            assert page.eval_on_selector("#designation-state", "e => e.dataset.state") == "deferred"

            # Recovery: a healthy endpoint repopulates the management view.
            page.unroute("**/dashboard/api/strategies")
            page.wait_for_function(
                "document.querySelectorAll('#inventory-rows tr').length === 2", timeout=20_000
            )

            # Unreachable endpoint: cleared again with an explicit error.
            page.route("**/dashboard/api/strategies", lambda route: route.abort())
            page.wait_for_function(
                "document.querySelectorAll('#inventory-rows tr').length === 0", timeout=20_000
            )
            summary = page.locator("#inventory-summary").inner_text()
            assert "unavailable" in summary and "unreachable" in summary
            assert page.eval_on_selector("#inventory-summary", "e => e.dataset.tone") == "error"
        finally:
            browser.close()


def test_ui_2_malformed_ws_summary_clears_promote_controls(
    poll_only_inventory_dashboard: str,
) -> None:
    """UI-2 fail-closed WS semantics (pinned ahead of producer evolution): a
    version-skewed STRATEGY_STATE summary — ``ok`` present but not exactly
    true, or a ``strategy_count`` that is not a non-negative integer — is
    unknown truth: rows and their PROMOTE LIVE controls clear immediately
    under an explicit unavailable caption, never a preserved stale table."""

    import json as _json

    mock_ws = []

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            # Fully mock the WS transport: the page connects, we hold the
            # server side and inject frames (the publisher is un-started, so
            # nothing else speaks STRATEGY_STATE).
            page.route_web_socket("**/ws/v1", lambda ws: mock_ws.append(ws))
            page.goto(poll_only_inventory_dashboard, wait_until="domcontentloaded")
            page.wait_for_function(
                "document.querySelectorAll('#inventory-rows tr').length === 2", timeout=7_000
            )
            _arm_promote(
                page, page.locator('#inventory-rows tr[data-strategy="alpha-1"] .manage__btn')
            )

            # Freeze the REST poll (404 from here on) so the only thing that
            # can change the table inside the assertion window is the injected
            # WS frame — and the clearing below is attributable to it (the
            # next poll tick is seconds away).
            page.route(
                "**/dashboard/api/strategies",
                lambda route: route.fulfill(status=404, body="gone"),
            )
            assert mock_ws, "the dashboard never opened its WebSocket"
            mock_ws[0].send(
                _json.dumps(
                    {
                        "type": "EVENT",
                        "channel": "STRATEGY_STATE",
                        "data": {"event": "inventory-summary", "ok": True},
                    }
                )
            )
            page.wait_for_function(
                "document.querySelectorAll('#inventory-rows tr').length === 0", timeout=2_500
            )
            assert page.locator(".manage__btn").count() == 0
            assert page.eval_on_selector("#inventory-table", "e => e.hidden") is True
            summary = page.locator("#inventory-summary").inner_text()
            assert "unavailable" in summary and "malformed" in summary
            assert page.eval_on_selector("#inventory-summary", "e => e.dataset.tone") == "error"
            assert page.eval_on_selector("#designation-state", "e => e.dataset.state") == "deferred"
        finally:
            browser.close()


def test_ui_2_promote_live_requests_are_serialized(
    poll_only_inventory_dashboard: str,
) -> None:
    """UI-2 / AC-15: one designation request at a time. While a confirmed
    promote-live POST is pending, every other PROMOTE LIVE control is inert —
    arming a second strategy is ignored, no competing POST leaves the page —
    and once the pending request settles the controls come back."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            posts: list[str] = []
            page.on(
                "request",
                lambda req: posts.append(req.url) if req.method == "POST" else None,
            )
            held_routes: list = []
            # Hold the promote route open: the fetch stays pending until the
            # test settles it, pinning the in-flight window deterministically.
            page.route(
                "**/api/v1/strategies/*/promote-live*",
                lambda route: held_routes.append(route),
            )
            page.goto(poll_only_inventory_dashboard, wait_until="domcontentloaded")
            page.wait_for_function(
                "document.querySelectorAll('#inventory-rows tr').length === 2", timeout=7_000
            )

            # Confirm alpha-1 → its POST is now pending (held by the route).
            _confirm_promote(page)
            page.wait_for_function(
                "document.getElementById('designation-state').dataset.state === 'pending'",
                timeout=7_000,
            )
            assert len([u for u in posts if "promote-live" in u]) == 1

            # While pending, the OTHER strategy's control is inert: clicking it
            # neither arms nor fires.
            beta = page.locator('#inventory-rows tr[data-strategy="beta-9"] .manage__btn')
            beta.click()
            page.wait_for_timeout(400)
            assert page.locator('.manage__btn[data-armed="true"]').count() == 0
            assert page.eval_on_selector("#designation-state", "e => e.dataset.state") == "pending"
            assert len([u for u in posts if "promote-live" in u]) == 1

            # Settle the pending request (501 from the deferred handler): the
            # refusal renders and the controls come back to life.
            assert held_routes, "the promote POST never reached the route"
            held_routes[0].fulfill(
                status=501,
                content_type="application/json",
                body='{"error": {"type": "HANDLER_DEFERRED", "detail": {"owner": "SRS-EXE-001"}}}',
            )
            page.wait_for_function(
                "document.getElementById('designation-state').dataset.state === 'error'",
                timeout=7_000,
            )
            assert "REFUSED 501" in page.locator("#designation-status").inner_text()

            # Guard released: arming works again.
            _arm_promote(page, beta)
            assert page.locator('.manage__btn[data-armed="true"]').count() == 1
        finally:
            browser.close()


def test_ui_2_cross_source_interleaving_never_clears_healthy_rows(
    poll_only_inventory_dashboard: str,
) -> None:
    """UI-2 burst-source independence: the WS feed and the REST poll are
    separate burst sources over the same truth. A WS summary, a full REST
    snapshot, and then a DELAYED WS row interleaved across them must leave the
    healthy rows (and their PROMOTE LIVE controls) intact — a frame from one
    source must never read as corruption of the other's burst."""

    import json as _json

    mock_ws = []

    def _ws_frame(data: dict) -> str:
        return _json.dumps({"type": "EVENT", "channel": "STRATEGY_STATE", "data": data})

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.route_web_socket("**/ws/v1", lambda ws: mock_ws.append(ws))
            page.goto(poll_only_inventory_dashboard, wait_until="domcontentloaded")
            # REST poll paints the truth (its own burst source).
            page.wait_for_function(
                "document.querySelectorAll('#inventory-rows tr').length === 2", timeout=7_000
            )
            assert mock_ws, "the dashboard never opened its WebSocket"

            # WS opens its own burst: summary, then ONE of its two rows...
            mock_ws[0].send(
                _ws_frame({"event": "inventory-summary", "ok": True, "strategy_count": 2})
            )
            mock_ws[0].send(
                _ws_frame(
                    {
                        "strategy_id": "alpha-1",
                        "name": "alpha-1",
                        "version_identifier": "sha256:" + "1" * 64 + "@100",
                    }
                )
            )
            # ...a full REST poll cycle lands in between (natural, ≤4 s)...
            page.wait_for_timeout(4_500)
            # ...and only then the DELAYED second WS row arrives.
            mock_ws[0].send(
                _ws_frame(
                    {
                        "strategy_id": "beta-9",
                        "name": "beta-9",
                        "version_identifier": "sha256:" + "3" * 64 + "@300",
                    }
                )
            )
            page.wait_for_timeout(600)

            # The healthy dashboard is untouched: both rows and both controls
            # remain, and the summary never flipped to unavailable.
            assert page.locator("#inventory-rows tr").count() == 2
            assert page.locator(".manage__btn").count() == 2
            assert page.eval_on_selector("#inventory-summary", "e => e.dataset.tone") == "ok"
            assert "unavailable" not in page.locator("#inventory-summary").inner_text()

            # And it stays healthy across the next full poll cycle too.
            page.wait_for_timeout(4_500)
            assert page.locator("#inventory-rows tr").count() == 2
            assert page.eval_on_selector("#inventory-summary", "e => e.dataset.tone") == "ok"
        finally:
            browser.close()


# --------------------------------------------------------------------------- #
# UI-4 — kill-switch control + Liquidate-Sequence status feedback
# (SyRS SYS-44a / SYS-44b; traces SRS-SAFE-001 + SRS-SAFE-002)
#
# The AC is "user can activate kill switch and see cancellation, liquidation
# submission, timeout, notification, and disconnect status". These exercise both
# halves in a real browser: the two-step control against the CONTRACT route, and
# the status rail — including every way the rail must FAIL CLOSED. A pane that
# leaves one stale green leg on screen after its feed dies tells an operator that
# live positions are closed when they may not be.
# --------------------------------------------------------------------------- #


def _seed_kill_switch_state(tmp_path):
    """A durable last-activation record + a SYS-44b timeout record, written
    through the SAME writers the runtime uses (never hand-rolled JSON), so the
    pane is exercised against real artefacts."""

    from atp_logging import LogClass
    from atp_logging.persistence import JsonlLogStore
    from atp_safety.audit import build_liquidation_timeout_record
    from atp_safety.state import persist_last_activation

    state_dir = tmp_path / "ks-state"
    state_dir.mkdir()
    report = {
        "activation_id": "act-e2e-01",
        "live_strategy_id": "alpha-1",
        "activated_at_epoch_ms": 1_700_000_000_000,
        "paper_halt": {"status": "SUCCEEDED"},
        "paper_halt_summary": {"engines_total": 4, "transitioned": 3, "already_halted": 1},
        "resting_order_cancels": [
            {
                "order_id": "o-1",
                "symbol": "SPY",
                "broker_order_id": "b-1",
                "outcome": {"status": "SUCCEEDED"},
            },
            {
                "order_id": "o-2",
                "symbol": "QQQ",
                "broker_order_id": "b-2",
                "outcome": {"status": "SUCCEEDED"},
            },
        ],
        "liquidations": [
            {"symbol": "SPY", "side": "SELL", "quantity": 120, "outcome": {"status": "SUCCEEDED"}},
            {
                "symbol": "QQQ",
                "side": "BUY",
                "quantity": 40,
                "outcome": {"status": "FAILED", "reason": "no route"},
            },
        ],
        "ib_disconnect": {"status": "SUCCEEDED"},
        "timings": {
            "halt_completed_ms": 12,
            "cancels_completed_ms": 210,
            "liquidations_submitted_ms": 1842,
            "disconnect_completed_ms": 1900,
        },
        "fully_clean": False,
        "within_nfr_p3": True,
        "all_engines_halted": True,
        "events_recorded": 2,
    }
    persist_last_activation(
        state_dir,
        {
            "activation_id": "act-e2e-01",
            "response": {
                "activation_id": "act-e2e-01",
                "activated_at": "2023-11-14T22:13:20.000+00:00",
                "cancelled_orders": report["resting_order_cancels"],
                "liquidation_orders": report["liquidations"],
                "paper_engines_halted": 4,
                "ib_gateway_disconnected": True,
            },
            "report": report,
            "ran_clean": False,
            "audit_recorded": True,
            "halted_log_latency_ms": 412.0,
            "persisted_at_ns": 1,
        },
    )
    JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM).write(
        build_liquidation_timeout_record(
            {
                "disposition": "TIMED_OUT_UNFILLED",
                "transports": "FIXTURE",
                "unfilled_order": {
                    "order_id": "o-2",
                    "symbol": "QQQ",
                    "side": "BUY",
                    "quantity": 40,
                },
                "cleanup": {
                    "operator_alert": {"status": "SUCCEEDED"},
                    "liquidation_cancel": {"status": "SUCCEEDED"},
                    "ib_disconnect": {"status": "SUCCEEDED"},
                },
                "manual_resolution_required": False,
            }
        )
    )
    return state_dir


@pytest.fixture()
def kill_switch_dashboard(tmp_path) -> Iterator[str]:
    """The PRODUCTION composition over a seeded kill-switch state directory."""

    from atp_dashboard import mount_default_dashboard

    state_dir = _seed_kill_switch_state(tmp_path)
    runtime = OperatorInterfaceRuntime()
    publisher = mount_default_dashboard(
        runtime,
        {
            "ATP_KILL_SWITCH_STATE": str(state_dir),
            "ATP_KILL_SWITCH_LOG_DIR": str(tmp_path),
        },
    )
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield f"http://{host}:{port}/dashboard"
    finally:
        publisher.stop()
        runtime.stop()


@pytest.fixture()
def bare_kill_switch_dashboard() -> Iterator[str]:
    """The production composition with NO kill-switch state configured — the
    pane must render UNKNOWN rather than an all-clear."""

    from atp_dashboard import mount_default_dashboard

    runtime = OperatorInterfaceRuntime()
    publisher = mount_default_dashboard(runtime, {})
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield f"http://{host}:{port}/dashboard"
    finally:
        publisher.stop()
        runtime.stop()


def _rung_status(page, phase: str) -> str:
    return page.eval_on_selector(f'.ks__rung[data-phase="{phase}"]', "e => e.dataset.status")


def _every_rung_unknown(page) -> bool:
    return page.eval_on_selector_all(
        ".ks__rung", "els => els.length === 6 && els.every(e => e.dataset.status === 'UNKNOWN')"
    )


def _arm_kill_switch(page) -> None:
    page.click("#ks-btn")
    page.wait_for_function(
        "document.getElementById('ks-btn').dataset.armed === 'true'", timeout=2_000
    )


def test_ui_4_kill_switch_control_covers_every_ac_surface(kill_switch_dashboard: str) -> None:
    """Every leg the AC names, rendered from real durable artefacts in one
    browser view: cancellation, liquidation submission, timeout, notification
    and disconnect — plus the confirmation-guarded control itself."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(kill_switch_dashboard, wait_until="domcontentloaded")
            page.wait_for_function(
                "document.querySelectorAll('.ks__rung').length === 6", timeout=8_000
            )
            page.wait_for_function(
                "document.querySelector('.ks__rung[data-phase=\"cancellation\"]')"
                ".dataset.status === 'SUCCEEDED'",
                timeout=8_000,
            )

            # --- the five AC legs, each with an observed status ------------ #
            assert _rung_status(page, "cancellation") == "SUCCEEDED"
            # One of two liquidations FAILED — the phase must say so, loudly.
            assert _rung_status(page, "liquidation") == "FAILED"
            assert _rung_status(page, "disconnect") == "SUCCEEDED"
            assert _rung_status(page, "halt") == "SUCCEEDED"
            # The SYS-44b legs show the record's CONTENT but stay UNKNOWN: the
            # timeout record is order-correlated, so it cannot be proven to
            # belong to this activation.
            assert _rung_status(page, "timeout") == "UNKNOWN"
            assert _rung_status(page, "notification") == "UNKNOWN"

            rail = page.locator("#ks-rail").inner_text()
            assert "1842 ms / 5000 ms" in rail  # NFR-P3 evidence
            assert "4 / 4 engines HALTED" in rail
            assert "o-2" in rail  # the unfilled order id
            assert "TIMED_OUT_UNFILLED" in rail  # the timeout evidence
            assert "operator page SUCCEEDED" in rail  # the notification evidence
            assert "NOT correlated" in rail  # ...honestly labelled

            # --- the receipt ---------------------------------------------- #
            assert page.locator("#ks-activation-id").inner_text() == "act-e2e-01"
            assert page.locator("#ks-nfr").inner_text() == "WITHIN 5s"
            assert page.locator("#ks-ran-clean").inner_text() == "WITH FAILURES"
            assert "412 ms / 1000 ms" in page.locator("#ks-halt-latency").inner_text()

            # --- fixture-drill evidence is labelled as such ---------------- #
            assert page.eval_on_selector("#ks-tier", "e => e.dataset.tier") == "FIXTURE"
            assert "FIXTURE DRILL" in page.locator("#ks-tier").inner_text()

            # --- the per-order table --------------------------------------- #
            assert page.locator("#ks-orders tr").count() == 4
            orders = page.locator("#ks-orders").inner_text()
            assert "CANCEL" in orders and "LIQUIDATION" in orders

            # --- the control is present and confirmation-guarded ----------- #
            assert page.eval_on_selector("#ks-btn", "e => e.dataset.armed") == "false"
            _arm_kill_switch(page)
            assert page.eval_on_selector("#ks", "e => e.dataset.state") == "armed"
        finally:
            browser.close()


def test_ui_4_unconfigured_pane_never_reads_as_all_clear(
    bare_kill_switch_dashboard: str,
) -> None:
    """With no state configured, every leg is UNKNOWN and the pane does NOT
    claim the kill switch was never activated — it cannot know."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(bare_kill_switch_dashboard, wait_until="domcontentloaded")
            page.wait_for_function(
                "document.querySelectorAll('.ks__rung').length === 6", timeout=8_000
            )
            assert _every_rung_unknown(page)
            assert page.locator("#ks-activation-id").inner_text() == "UNKNOWN"
            assert page.locator("#ks-orders-table").is_hidden()
            assert page.eval_on_selector("#ks-tier", "e => e.dataset.tier") == "unknown"
            note = page.locator("#ks-note").inner_text()
            assert "UNKNOWN" in note or "not configured" in note or "ATP_KILL_SWITCH_STATE" in note
        finally:
            browser.close()


def test_ui_4_activation_requires_explicit_confirmation(kill_switch_dashboard: str) -> None:
    """SYS-44a: no POST leaves the browser on the first click. Only the second,
    inside the arm window, fires — and it goes to the CONTRACT route."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        posts: list[str] = []
        page.on("request", lambda req: posts.append(req.url) if req.method == "POST" else None)
        try:
            page.goto(kill_switch_dashboard, wait_until="domcontentloaded")
            page.wait_for_selector("#ks-btn")

            page.click("#ks-btn")  # arm only
            page.wait_for_timeout(600)
            assert posts == [], "arming must not fire the liquidate sequence"
            assert page.eval_on_selector("#ks-btn", "e => e.dataset.armed") == "true"
            assert "ARMED" in page.locator("#ks-status").inner_text()

            page.click("#ks-btn")  # confirm
            page.wait_for_timeout(1_200)
            assert len(posts) == 1
            assert posts[0].endswith("/api/v1/kill-switch?confirm=true")
        finally:
            browser.close()


def test_ui_4_arm_window_expires_back_to_the_resting_caption(
    kill_switch_dashboard: str,
) -> None:
    """A staged confirmation that is not confirmed must disarm AND restore the
    resting caption — a leftover 'ARMED' readout with nothing staged is stale
    state an operator would act on."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        posts: list[str] = []
        page.on("request", lambda req: posts.append(req.url) if req.method == "POST" else None)
        try:
            page.goto(kill_switch_dashboard, wait_until="domcontentloaded")
            page.wait_for_selector("#ks-btn")
            _arm_kill_switch(page)

            page.wait_for_function(
                "document.getElementById('ks-btn').dataset.armed === 'false'", timeout=9_000
            )
            assert posts == []
            assert "ARMED" not in page.locator("#ks-status").inner_text()
            assert page.eval_on_selector("#ks", "e => e.dataset.state") != "armed"
        finally:
            browser.close()


def test_ui_4_renders_refusals_honestly(kill_switch_dashboard: str) -> None:
    """A refusal is rendered as its error type and owner — never dressed as a
    completed liquidation."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            page.route(
                "**/api/v1/kill-switch*",
                lambda route: route.fulfill(
                    status=501,
                    content_type="application/json",
                    body='{"error":{"type":"HANDLER_DEFERRED","category":"NOT_IMPLEMENTED",'
                    '"detail":{"owner":"SRS-SAFE-001"}}}',
                ),
            )
            page.goto(kill_switch_dashboard, wait_until="domcontentloaded")
            page.wait_for_selector("#ks-btn")
            _arm_kill_switch(page)
            page.click("#ks-btn")

            page.wait_for_function(
                "document.getElementById('ks-status').dataset.tone === 'error'", timeout=5_000
            )
            status = page.locator("#ks-status").inner_text()
            assert "REFUSED 501" in status
            assert "HANDLER_DEFERRED" in status
            assert "SRS-SAFE-001" in status
            # The topbar affordance tells the same story — one control, one truth.
            assert "REFUSED 501" in page.locator("#killswitch-status").inner_text()
        finally:
            browser.close()


def test_ui_4_partial_failure_is_never_dressed_as_success(
    kill_switch_dashboard: str,
) -> None:
    """A 200 means the sequence RAN, not that every phase succeeded."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            page.route(
                "**/api/v1/kill-switch*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "activation_id": "act-partial",
                            "activated_at": "2026-07-21T00:00:00Z",
                            "cancelled_orders": [
                                {
                                    "order_id": "o-1",
                                    "symbol": "SPY",
                                    "outcome": {"status": "FAILED", "reason": "rejected"},
                                }
                            ],
                            "liquidation_orders": [],
                            "paper_engines_halted": 2,
                            "ib_gateway_disconnected": False,
                        }
                    ),
                ),
            )
            page.goto(kill_switch_dashboard, wait_until="domcontentloaded")
            page.wait_for_selector("#ks-btn")
            _arm_kill_switch(page)
            page.click("#ks-btn")

            page.wait_for_function(
                "document.getElementById('ks-status').dataset.tone === 'error'", timeout=5_000
            )
            status = page.locator("#ks-status").inner_text()
            assert "WITH FAILURES" in status
            assert "IB NOT disconnected" in status
        finally:
            browser.close()


def test_ui_4_a_success_without_an_activation_id_is_refused(
    kill_switch_dashboard: str,
) -> None:
    """Identity binding: a success-shaped body that names no activation proves
    nothing about what ran, so it is never rendered as an activation."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            page.route(
                "**/api/v1/kill-switch*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"paper_engines_halted":3,"ib_gateway_disconnected":true}',
                ),
            )
            page.goto(kill_switch_dashboard, wait_until="domcontentloaded")
            page.wait_for_selector("#ks-btn")
            _arm_kill_switch(page)
            page.click("#ks-btn")

            page.wait_for_function(
                "document.getElementById('ks-status').dataset.tone === 'error'", timeout=5_000
            )
            status = page.locator("#ks-status").inner_text()
            assert "no activation_id" in status
            assert "activated" not in status.split("REFUSED")[0]
        finally:
            browser.close()


def test_ui_4_degraded_status_route_clears_every_leg(kill_switch_dashboard: str) -> None:
    """The false-all-clear class: once the status feed dies, the previously
    green sequence must NOT stay on screen."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(kill_switch_dashboard, wait_until="domcontentloaded")
            page.wait_for_function(
                "document.querySelector('.ks__rung[data-phase=\"disconnect\"]')"
                ".dataset.status === 'SUCCEEDED'",
                timeout=8_000,
            )

            # Kill the feed mid-flight.
            page.route("**/dashboard/api/kill-switch", lambda route: route.abort())
            page.wait_for_function(
                "Array.from(document.querySelectorAll('.ks__rung'))"
                ".every(e => e.dataset.status === 'UNKNOWN')",
                timeout=9_000,
            )

            assert _every_rung_unknown(page)
            assert page.locator("#ks-orders-table").is_hidden()
            assert page.locator("#ks-activation-id").inner_text() == "UNKNOWN"
            assert page.eval_on_selector("#ks-tier", "e => e.dataset.tier") == "unknown"
            assert page.eval_on_selector("#ks-note", "e => e.dataset.tone") == "error"
        finally:
            browser.close()


def test_ui_4_a_route_that_disappears_clears_every_leg(kill_switch_dashboard: str) -> None:
    """404 fails closed exactly like every other degraded branch."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(kill_switch_dashboard, wait_until="domcontentloaded")
            page.wait_for_function(
                "document.querySelector('.ks__rung[data-phase=\"disconnect\"]')"
                ".dataset.status === 'SUCCEEDED'",
                timeout=8_000,
            )
            page.route(
                "**/dashboard/api/kill-switch",
                lambda route: route.fulfill(status=404, content_type="application/json", body="{}"),
            )
            page.wait_for_function(
                "Array.from(document.querySelectorAll('.ks__rung'))"
                ".every(e => e.dataset.status === 'UNKNOWN')",
                timeout=9_000,
            )
            assert "not mounted" in page.locator("#ks-note").inner_text()
        finally:
            browser.close()


def test_ui_4_a_shape_drifted_payload_is_refused_wholesale(
    kill_switch_dashboard: str,
) -> None:
    """A payload claiming green legs in an unknown shape must not render: the
    client refuses the WHOLE sequence rather than draw a partial one."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(kill_switch_dashboard, wait_until="domcontentloaded")
            page.wait_for_function(
                "document.querySelector('.ks__rung[data-phase=\"disconnect\"]')"
                ".dataset.status === 'SUCCEEDED'",
                timeout=8_000,
            )
            # Green statuses, wrong contract: two legs instead of six.
            page.route(
                "**/dashboard/api/kill-switch",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "ok": True,
                            "activated": True,
                            "activation_id": "act-lies",
                            "sequence": [
                                {
                                    "phase": "cancellation",
                                    "status": "SUCCEEDED",
                                    "value": "SUCCEEDED",
                                    "detail": "all good",
                                },
                                {
                                    "phase": "disconnect",
                                    "status": "SUCCEEDED",
                                    "value": "SUCCEEDED",
                                    "detail": "all good",
                                },
                            ],
                            "orders": [],
                            "tier": "LIVE",
                        }
                    ),
                ),
            )
            page.wait_for_function(
                "Array.from(document.querySelectorAll('.ks__rung'))"
                ".every(e => e.dataset.status === 'UNKNOWN')",
                timeout=9_000,
            )
            assert _every_rung_unknown(page)
            assert page.eval_on_selector("#ks-tier", "e => e.dataset.tier") == "unknown"
        finally:
            browser.close()


def test_ui_4_a_deferred_cell_never_draws_a_resolved_rung(
    kill_switch_dashboard: str,
) -> None:
    """A leg whose value is null renders UNKNOWN even when its status claims
    otherwise — the server cannot talk the client into a green rung."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(kill_switch_dashboard, wait_until="domcontentloaded")
            page.wait_for_selector(".ks__rung")
            phases = [
                "halt",
                "cancellation",
                "liquidation",
                "timeout",
                "notification",
                "disconnect",
            ]
            page.route(
                "**/dashboard/api/kill-switch",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "ok": True,
                            "activated": True,
                            "activation_id": "act-x",
                            "sequence": [
                                {
                                    "phase": ph,
                                    "label": ph.upper(),
                                    "branch": False,
                                    "order": i + 1,
                                    "owner": "SRS-SAFE-001",
                                    "status": "SUCCEEDED",
                                    "detail": "claimed clean",
                                    "value": None,
                                    "data_source": "deferred:SRS-SAFE-001",
                                }
                                for i, ph in enumerate(phases)
                            ],
                            "orders": None,
                            "tier": None,
                        }
                    ),
                ),
            )
            page.wait_for_function(
                "Array.from(document.querySelectorAll('.ks__rung'))"
                ".every(e => e.dataset.status === 'UNKNOWN')",
                timeout=9_000,
            )
            assert _every_rung_unknown(page)
        finally:
            browser.close()


def test_ui_4_activations_are_serialized(kill_switch_dashboard: str) -> None:
    """One liquidate sequence in flight at a time: while a POST is settling,
    BOTH triggers are inert — no second arm, no second POST."""

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        posts: list[str] = []
        held: list = []
        page.on("request", lambda req: posts.append(req.url) if req.method == "POST" else None)
        try:
            page.route("**/api/v1/kill-switch*", lambda route: held.append(route))
            page.goto(kill_switch_dashboard, wait_until="domcontentloaded")
            page.wait_for_selector("#ks-btn")

            _arm_kill_switch(page)
            page.click("#ks-btn")  # fires; the route is HELD open
            page.wait_for_function(
                "document.getElementById('ks-btn').disabled === true", timeout=5_000
            )

            # Hammer BOTH triggers while the first request is in flight.
            for _ in range(3):
                page.click("#ks-btn", force=True)
                page.click("#killswitch-btn", force=True)
                page.wait_for_timeout(120)
            assert len(posts) == 1, f"activation was not serialized: {posts}"
            assert page.eval_on_selector("#ks-btn", "e => e.dataset.armed") == "false"

            # Release: the control returns to service, still exactly one POST.
            held[0].fulfill(
                status=501,
                content_type="application/json",
                body='{"error":{"type":"HANDLER_DEFERRED","detail":{"owner":"SRS-SAFE-001"}}}',
            )
            page.wait_for_function(
                "document.getElementById('ks-btn').disabled === false", timeout=5_000
            )
            assert len(posts) == 1
        finally:
            browser.close()
