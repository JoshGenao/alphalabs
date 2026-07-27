"""L6 e2e — SRS-RES-003: reaching Jupyter from the PRIMARY dashboard workflow.

The browser-automation leg of the ``SRS-RES-003`` acceptance evidence (Step 2 /
Step 3). ``SRS-RES-001``'s e2e already proves the embed renders once the
operator finds the Research panel; this proves the part ``SyRS SYS-43`` adds —
that the operator *gets there from the primary workflow*, with no scrolling
hunt through the panel deck and, above all, **no direct service URL**:

* the persistent topbar navigation entry arms itself only after the live probe
  reports the environment reachable, and one click on it renders REAL
  JupyterLab in the same-origin iframe;
* the browser never leaves the dashboard origin — ``page.url`` stays on the
  dashboard, the iframe ``src`` is a root-relative path, and the upstream's
  authority appears nowhere in the served DOM;
* ``/dashboard#research`` is an addressable deep link that lands and opens;
* with the environment DOWN the entry renders its explicit unreachable state,
  carries no target, and clicking it fabricates no embed.

Gated: ``pytest -m "not e2e"`` skips it; runs under ``ATP_RUN_E2E=1`` with
Playwright browsers installed AND ``jupyterlab`` importable. JupyterLab is
installed ad hoc for this demonstration — it is a container-image dependency
(``docker/jupyter.Dockerfile``), deliberately NOT pinned in ``requirements*.txt``.
"""

from __future__ import annotations

import http.client
import socket
import subprocess
import sys
import time
from collections.abc import Iterator

import pytest

# Guard collection: imports must not error when the optional tools are absent —
# the collection-time skip in conftest runs *after* module import.
sync_api = pytest.importorskip("playwright.sync_api")
pytest.importorskip("jupyterlab")

from atp_dashboard import (  # noqa: E402
    ReadinessBackedProvider,
    ResearchEnvironmentProvider,
    mount_dashboard,
)
from atp_runtime import OperatorInterfaceRuntime  # noqa: E402

pytestmark = pytest.mark.e2e

_READY_DEADLINE = 60.0
_ARM_TIMEOUT_MS = 20_000
_LAB_TIMEOUT_MS = 45_000


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def jupyter_lab(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    """A real local JupyterLab under base_url=/research/ on an ephemeral port."""

    root = tmp_path_factory.mktemp("jupyterlab-nav")
    for sub in ("runtime", "data", "config", "nb"):
        (root / sub).mkdir()
    port = _free_port()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(root),
        "JUPYTER_RUNTIME_DIR": str(root / "runtime"),
        "JUPYTER_DATA_DIR": str(root / "data"),
        "JUPYTER_CONFIG_DIR": str(root / "config"),
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "jupyter",
            "lab",
            "--ServerApp.ip=127.0.0.1",
            f"--ServerApp.port={port}",
            "--ServerApp.base_url=/research/",
            "--IdentityProvider.token=",
            "--ServerApp.password=",
            "--no-browser",
            f"--notebook-dir={root / 'nb'}",
        ],
        env=env,
        stdout=(root / "jupyterlab.log").open("wb"),
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + _READY_DEADLINE
        while time.monotonic() < deadline:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                conn.request("GET", "/research/api/status")
                status = conn.getresponse().status
                conn.close()
                if status == 200:
                    break
            except OSError:
                time.sleep(0.5)
        else:
            log_tail = (root / "jupyterlab.log").read_text(errors="replace")[-2000:]
            pytest.fail(f"JupyterLab never became ready on :{port}\n{log_tail}")
        yield port
    finally:
        process.terminate()
        process.wait(timeout=15)


def _dashboard_over(upstream_port: int | None) -> Iterator[tuple[str, int]]:
    """A dashboard runtime with ZERO strategy/backtest handlers (SYS-34c)."""

    upstream = f"http://127.0.0.1:{upstream_port}" if upstream_port else None
    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(
        runtime, ReadinessBackedProvider({}), research=ResearchEnvironmentProvider(upstream)
    )
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield host, port
    finally:
        publisher.stop()
        runtime.stop()


@pytest.fixture()
def embedded_dashboard(jupyter_lab: int) -> Iterator[tuple[str, int]]:
    yield from _dashboard_over(jupyter_lab)


@pytest.fixture()
def dead_dashboard() -> Iterator[tuple[str, int]]:
    """The same runtime, but the research environment is not listening."""

    yield from _dashboard_over(_free_port())


_NAV_ARMED = "() => document.getElementById('nav-research').dataset.state === 'ready'"
_LAB_IN_FRAME = (
    "() => {"
    "  const f = document.getElementById('research-frame');"
    "  try { return f && f.contentDocument"
    "      && /JupyterLab/i.test(f.contentDocument.title); }"
    "  catch (e) { return false; }"
    "}"
)


def test_topbar_navigation_opens_jupyter_without_a_service_url(
    embedded_dashboard, jupyter_lab: int
) -> None:
    """The acceptance criterion, demonstrated end to end in a real browser."""

    host, port = embedded_dashboard
    dashboard_url = f"http://{host}:{port}/dashboard"
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(dashboard_url)

            # The entry is part of the primary workflow chrome — present in the
            # topbar on arrival, without scrolling to find a panel.
            nav = page.locator("#nav-research")
            nav.wait_for(state="visible", timeout=_ARM_TIMEOUT_MS)
            assert nav.get_attribute("href") == "#research"

            # It arms only after the live probe proves the environment
            # reachable — never pre-armed on composition alone.
            page.wait_for_function(_NAV_ARMED, timeout=_ARM_TIMEOUT_MS)
            assert "open" in page.locator("#nav-research-state").inner_text().lower()

            nav.click()

            frame_element = page.locator("#research-frame")
            frame_element.wait_for(state="visible", timeout=_ARM_TIMEOUT_MS)
            # SYS-43: the destination is a ROOT-RELATIVE path on this origin.
            src = frame_element.get_attribute("src")
            assert src is not None and src.startswith("/research/")
            assert "://" not in src

            # The embedded document is REAL JupyterLab.
            page.wait_for_function(_LAB_IN_FRAME, timeout=_LAB_TIMEOUT_MS)

            # The browser never left the dashboard origin, and the operator was
            # never shown the service URL they must not need.
            assert page.url.startswith(f"http://{host}:{port}/dashboard")
            assert f"127.0.0.1:{jupyter_lab}" not in page.content()
        finally:
            browser.close()


def test_hash_deep_link_lands_and_opens_the_environment(
    embedded_dashboard,
) -> None:
    """``/dashboard#research`` is addressable — no direct service URL needed."""

    host, port = embedded_dashboard
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(f"http://{host}:{port}/dashboard#research")
            page.wait_for_function(_NAV_ARMED, timeout=_ARM_TIMEOUT_MS)
            # The one-shot intent is consumed by the first completed poll that
            # proves reachability — the embed opens with no further click.
            page.locator("#research-frame").wait_for(state="visible", timeout=_ARM_TIMEOUT_MS)
            page.wait_for_function(_LAB_IN_FRAME, timeout=_LAB_TIMEOUT_MS)
        finally:
            browser.close()


def test_navigation_never_fabricates_an_open_when_the_environment_is_down(
    dead_dashboard,
) -> None:
    """A registered-but-dead environment must not leave an actionable promise.

    The entry is reachable-gated, so it renders the explicit down state, holds
    no target for a click to find, and clicking it opens nothing — the operator
    lands on the panel and reads the probe's reason instead.
    """

    host, port = dead_dashboard
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(f"http://{host}:{port}/dashboard")
            nav = page.locator("#nav-research")
            nav.wait_for(state="visible", timeout=_ARM_TIMEOUT_MS)
            page.wait_for_function(
                "() => document.getElementById('nav-research').dataset.state === 'down'",
                timeout=_ARM_TIMEOUT_MS,
            )
            assert nav.get_attribute("data-embed-path") is None

            nav.click()
            page.wait_for_timeout(1_000)

            frame = page.locator("#research-frame")
            assert frame.get_attribute("src") in (None, "")
            assert frame.is_hidden()
            # The panel — now in view — carries the honest reason.
            assert "unreachable" in page.locator("#research-status").inner_text().lower()
        finally:
            browser.close()
