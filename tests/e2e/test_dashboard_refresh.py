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
            # The alerts pane's freshness dot reaches fresh (the poll is live).
            page.wait_for_function(
                "document.getElementById('fresh-alerts').dataset.state === 'fresh'",
                timeout=7_000,
            )

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

            # Heal the endpoint: the pane recovers to the honest awaiting state.
            page.unroute("**/dashboard/api/alerts")
            page.wait_for_function(
                "document.getElementById('alerts-summary').dataset.tone === 'warn'",
                timeout=7_000,
            )
        finally:
            browser.close()
