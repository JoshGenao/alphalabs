"""L6 e2e — the dashboard top bar keeps its parts apart at every width.

The brand column was allowed to shrink below its own title (``min-width: 0`` on a
``white-space: nowrap`` title), so at common laptop widths "ATP · MISSION CONTROL"
slid underneath the Research navigation key and the subtitle was cut to
"SRS-UI-00…". Below ~1100 px the single row could not hold the status chips at
all, and the Readiness chip overran the Research key instead. No offline test
could see either: both are geometry.

Gated: runs under ``ATP_RUN_E2E=1`` with Playwright browsers installed.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from atp_dashboard import mount_default_dashboard  # noqa: E402
from atp_runtime import OperatorInterfaceRuntime  # noqa: E402

pytestmark = pytest.mark.e2e

#: The parts of the top bar that must never overlap one another.
PARTS = (".brand__title", "#nav-research", "#conn", "#readiness", "#killswitch-btn", ".pulse")


@pytest.fixture(scope="module")
def url() -> Iterator[str]:
    runtime = OperatorInterfaceRuntime()
    publisher = mount_default_dashboard(runtime, {})
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield f"http://{host}:{port}/dashboard/"
    finally:
        publisher.stop()
        runtime.stop()


def _overlap(a: dict, b: dict) -> bool:
    return (
        a["x"] < b["x"] + b["width"]
        and b["x"] < a["x"] + a["width"]
        and a["y"] < b["y"] + b["height"]
        and b["y"] < a["y"] + a["height"]
    )


@pytest.mark.parametrize("width", [1440, 1280, 1100, 1000, 800, 600])
def test_the_top_bar_parts_never_overlap(url: str, width: int) -> None:
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector("#nav-research")
        boxes = {sel: page.locator(sel).first.bounding_box() for sel in PARTS}
        title_clipped = page.eval_on_selector(
            ".brand__title", "e => e.scrollWidth > e.clientWidth + 1"
        )
        page_scrolls = page.evaluate(
            "() => document.documentElement.scrollWidth > document.documentElement.clientWidth"
        )
        browser.close()

    assert all(boxes.values()), f"a part is not rendered: {boxes}"
    collisions = [
        (a, b) for i, a in enumerate(PARTS) for b in PARTS[i + 1 :] if _overlap(boxes[a], boxes[b])
    ]
    assert collisions == [], f"at {width}px these overlap: {collisions}"
    assert not title_clipped, f"at {width}px the brand title is cut off"
    assert not page_scrolls, f"at {width}px the page scrolls sideways"
