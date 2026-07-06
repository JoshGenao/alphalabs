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
