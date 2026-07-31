"""L6 e2e — ``SRS-RESV-003``: manual and configurable automatic Hot-Swap triggers.

The browser-automation + REST leg of the acceptance evidence (Step 2 / Step 3). The AC:

    Manual promotion, drawdown-triggered demotion, top-ranked promotion, and
    highest-momentum promotion are configurable; automatic triggers default to
    disabled; all swap triggers are logged.

Unlike the ``test_ui_5_*`` cases in ``test_dashboard_refresh.py``, which route a fake
payload to pin the pane's rendering before any producer existed, **nothing here is
faked**: a real ``resv003_hot_swap_trigger_cli`` writes a real durable configuration file,
the production ``mount_default_dashboard`` composition reads it, and a real headless
browser renders the result. The REST arm runs against the same runtime, so what the
operator configures over REST is what the browser shows.

Gated: ``pytest -m "not e2e"`` skips it; runs under ``ATP_RUN_E2E=1`` with Playwright
browsers installed (``playwright install chromium``) and cargo on PATH.
"""

from __future__ import annotations

import http.client
import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

# Guard collection: the import must not error when Playwright is absent — the
# collection-time skip in conftest runs *after* module import.
sync_api = pytest.importorskip("playwright.sync_api")

from atp_dashboard import mount_default_dashboard  # noqa: E402
from atp_dashboard.server import _mount_hot_swap_trigger_arm  # noqa: E402
from atp_runtime import OperatorInterfaceRuntime  # noqa: E402

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The three automatic triggers named by SYS-49a, and the chip each renders as.
_AUTOMATIC_KINDS = ("drawdown_demotion", "top_ranked_promotion", "highest_momentum_promotion")


@pytest.fixture(scope="module")
def trigger_binary() -> Path:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("cargo not on PATH; cannot build the trigger CLI")
    build = subprocess.run(
        [cargo, "build", "-q", "-p", "atp-orchestrator", "--bin", "resv003_hot_swap_trigger_cli"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, f"cargo build failed:\n{build.stdout}\n{build.stderr}"
    return REPO_ROOT / "target" / "debug" / "resv003_hot_swap_trigger_cli"


@pytest.fixture()
def live_dashboard(
    trigger_binary: Path, tmp_path: Path
) -> Iterator[tuple[str, tuple[str, int], Path, Path]]:
    """The PRODUCTION composition, reading a real durable trigger configuration.

    Yields ``(dashboard_url, (host, port), state_path, log_path)``.

    This goes through the SAME entrypoint helper ``serve()`` uses, rather than mounting the
    REST arm by hand. Mounting it manually is what let the routes pass an e2e while
    answering 501 in production: the fixture proved its own composition, not the shipped
    one. Here the env knobs are the only wiring, so the browser and the REST caller observe
    one configuration exactly as an operator's process would.
    """

    state = tmp_path / "triggers.json"
    log = tmp_path / "triggers.jsonl"
    env = {
        "ATP_HOT_SWAP_TRIGGER_STATE": str(state),
        "ATP_HOT_SWAP_TRIGGER_LOG": str(log),
    }
    runtime = OperatorInterfaceRuntime()
    publisher = mount_default_dashboard(
        runtime, env, hot_swap_source=_mount_hot_swap_trigger_arm(runtime, env)
    )
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield f"http://{host}:{port}/dashboard", (host, port), state, log
    finally:
        publisher.stop()
        runtime.stop()


def _rest(where: tuple[str, int], method: str, path: str, body: dict | None = None):
    host, port = where
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read() or b"{}"
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {}
        return response.status, parsed
    finally:
        conn.close()


def _chip_states(page) -> dict[str, str]:
    """Every trigger chip's rendered state, keyed by trigger kind."""

    page.wait_for_selector(".hs__chip", timeout=10_000)
    return page.eval_on_selector_all(
        ".hs__chip",
        "els => Object.fromEntries(els.map(e => [e.dataset.kind, e.dataset.state]))",
    )


def _open_pane(page, url: str) -> None:
    """Load the dashboard and wait for the Hot-Swap pane's FIRST poll to land.

    The chips exist from first paint, rendered deferred until a snapshot arrives — so
    reading them straight after `goto` samples the pre-fetch placeholder, not the state
    under test. Waiting on the response itself (rather than a sleep) keeps this
    deterministic, and `deferred` stays a legitimate observable outcome for the
    unreadable-configuration case.
    """

    with page.expect_response(
        lambda response: "/dashboard/api/hot-swap" in response.url, timeout=20_000
    ):
        page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector("#hs-triggers .hs__chip", timeout=15_000)
    page.wait_for_function(
        "document.querySelectorAll('#hs-triggers .hs__chip').length >= 4", timeout=10_000
    )


def _await_chip(page, kind: str, expected: str) -> None:
    """Wait for the pane's own poll to bring ``kind``'s chip to ``expected``.

    The pane refreshes itself within the NFR-P2 budget, so a configuration change made over
    REST reaches the browser without a reload — which is how an operator would actually see
    it, and avoids hammering the loopback server with a navigation per assertion.
    """

    page.wait_for_function(
        """([kind, expected]) => {
             const chip = document.querySelector(`.hs__chip[data-kind="${kind}"]`);
             return !!chip && chip.dataset.state === expected;
           }""",
        arg=[kind, expected],
        timeout=20_000,
    )


def test_resv_003_every_ac_clause_in_one_run(live_dashboard) -> None:
    """The whole acceptance criterion, end to end, with nothing faked."""

    url, rest, state, log = live_dashboard

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            # ---- CLAUSE 2: automatic triggers default to disabled ------------- #
            # Nothing has been configured, so this is the genuine default posture — and it
            # is asserted NON-VACUOUSLY: every automatic chip is present and reads off,
            # not merely absent from the page.
            _open_pane(page, url)
            chips = _chip_states(page)
            for kind in _AUTOMATIC_KINDS:
                assert chips[kind] == "off", (kind, chips)
            # ---- CLAUSE 1a: manual promotion is always available --------------- #
            assert chips["manual"] == "manual", chips

            status, body = _rest(rest, "GET", "/api/v1/hot-swap/triggers")
            assert status == 200, body
            assert body["config_source"] == "default", body
            assert body["any_automatic_enabled"] is False, body
            assert body["default_disabled"] is True, body
            assert body["manual_promotion_available"] is True, body

            # ---- CLAUSE 1b: each automatic trigger is configurable ------------- #
            # One at a time, over REST, checking the BROWSER reflects each change: the
            # operator's configuration reaching the pane is the thing under test.
            for kind, request_body in (
                (
                    "drawdown_demotion",
                    {
                        "drawdown_demotion_enabled": True,
                        "drawdown_demotion_threshold_bps": 250,
                    },
                ),
                ("top_ranked_promotion", {"top_ranked_promotion_enabled": True}),
                ("highest_momentum_promotion", {"highest_momentum_promotion_enabled": True}),
            ):
                status, body = _rest(
                    rest, "PUT", "/api/v1/hot-swap/triggers?confirm=yes", request_body
                )
                assert status == 200, (kind, body)
                _await_chip(page, kind, "on")

            # All three now armed, and the durable file says so independently of the UI.
            status, body = _rest(rest, "GET", "/api/v1/hot-swap/triggers")
            assert body["config_source"] == "persisted", body
            assert body["drawdown_demotion_threshold_bps"] == 250, body
            assert body["any_automatic_enabled"] is True, body
            assert "ATP-HOT-SWAP-TRIGGER-CONFIG" in state.read_text()

            # Disabling is reachable too, or "configurable" would be one-way.
            status, body = _rest(
                rest,
                "PUT",
                "/api/v1/hot-swap/triggers?confirm=yes",
                {"top_ranked_promotion_enabled": False},
            )
            assert status == 200, body
            _await_chip(page, "top_ranked_promotion", "off")
            assert _chip_states(page)["drawdown_demotion"] == "on", _chip_states(page)

            # ---- CLAUSE 3: all swap triggers are logged ------------------------ #
            status, body = _rest(
                rest,
                "POST",
                "/api/v1/hot-swap/triggers/manual?confirm=yes",
                {"demoting_strategy_id": "momentum-v3", "candidate_strategy_id": "meanrev-v7"},
            )
            assert status == 200, body
            assert body["trigger_kind"] == "MANUAL_PROMOTION", body
            assert body["logged"] is True, body
            # The trigger layer PROPOSES; it does not swap. The payload says so itself.
            assert body["execution"]["state"] == "DEFERRED", body
            assert body["execution"]["owner"] == "SRS-RESV-004", body
            # And the reported id addresses the durable record actually written.
            record = log.read_text().splitlines()[int(body["trigger_id"]) - 1]
            assert '"kind":"MANUAL_PROMOTION"' in record, record
            assert '"candidate_strategy_id":"meanrev-v7"' in record, record
        finally:
            browser.close()


def test_resv_003_manual_stays_available_with_every_automatic_trigger_off(
    live_dashboard,
) -> None:
    """SYS-49a(a): manual selection is not gated by the automatic configuration."""

    url, rest, _state, log = live_dashboard

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            _open_pane(page, url)
            chips = _chip_states(page)
            assert all(chips[kind] == "off" for kind in _AUTOMATIC_KINDS), chips
            assert chips["manual"] == "manual", chips

            status, body = _rest(
                rest,
                "POST",
                "/api/v1/hot-swap/triggers/manual?confirm=yes",
                {"demoting_strategy_id": "alpha", "candidate_strategy_id": "beta"},
            )
            assert status == 200, body
            assert body["logged"] is True, body
            assert log.read_text().count('"MANUAL_PROMOTION"') == 1
        finally:
            browser.close()


def test_resv_003_an_unreadable_config_never_renders_as_disabled(live_dashboard) -> None:
    """The false all-clear this pane must never show.

    An operator opens the Hot-Swap console to answer "can an automatic demotion fire right
    now?". If a corrupt configuration rendered as three tidy ``off`` chips, the answer they
    read would be a confident "no" that nobody actually knows.
    """

    url, rest, state, _log = live_dashboard

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            _rest(
                rest,
                "PUT",
                "/api/v1/hot-swap/triggers?confirm=yes",
                {"drawdown_demotion_enabled": True, "drawdown_demotion_threshold_bps": 250},
            )
            _open_pane(page, url)
            _await_chip(page, "drawdown_demotion", "on")

            # Tear the file the way a partial write or a bad edit would.
            state.write_text('{"magic":"ATP-HOT-SWAP-TRIGGER-CONFIG","schema_version":1,"draw')

            # No reload: the running pane must notice on its next poll.
            _await_chip(page, "drawdown_demotion", "deferred")
            chips = _chip_states(page)
            for kind in _AUTOMATIC_KINDS:
                assert chips[kind] == "deferred", (kind, chips)
            # Manual promotion is unaffected — it is never gated by this configuration.
            assert chips["manual"] == "manual", chips

            # The dashboard poll reports the failure rather than a clean snapshot...
            status, snapshot = _rest(rest, "GET", "/dashboard/api/hot-swap")
            assert status == 200, snapshot
            assert snapshot["ok"] is False, snapshot
            assert snapshot["auto_triggers_enabled"]["value"] is None, snapshot
            # ...and the REST read fails loudly instead of answering "disabled".
            status, body = _rest(rest, "GET", "/api/v1/hot-swap/triggers")
            assert status >= 500, body
            assert body["error"]["type"] == "TRIGGER_CONFIG_UNREADABLE", body
        finally:
            browser.close()
