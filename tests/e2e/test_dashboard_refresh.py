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
        finally:
            browser.close()
