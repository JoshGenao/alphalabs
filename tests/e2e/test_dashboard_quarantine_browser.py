"""L6 e2e — ``SRS-DATA-013`` Step 3: quarantined records, alerted with counts and reasons.

The acceptance criterion:

    Records failing structural, range, duplicate, or required-field checks are
    quarantined and not written to primary tables; dashboard and notification
    alerts include counts and reasons.

**What is real here.** The run is written by the real
``data013_ingestion_validation_cli ingest`` (the SYS-77 mixed fixture: ten
records, one failing each of the six rules). The primary table is read back by
the real ``inspect`` command. The operator alert is sent by the real
``data013_quarantine_alert_cli notify`` — real dispatcher, real audit store —
and the pane is the real dashboard reading both stores through the real CLIs.

**What stands in, and why.** The two transports are loopback stand-ins
(``tests/fixtures/operator_transports.py``) that speak the relay's SMTP and
ntfy's publish protocol, so this test can read back exactly what each channel
was handed. The real Brevo relay and the operator's ntfy are exercised by the
live run recorded in the session note, not here.

Gated: ``pytest -m "not e2e"`` skips it. Runs under ``ATP_RUN_E2E=1`` with
Playwright browsers installed and cargo on PATH. ``ATP_CAPTURE_EVIDENCE=1``
attaches the screenshots the step's record requires.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from atp_dashboard import QUARANTINE_REASONS, mount_default_dashboard  # noqa: E402
from atp_runtime import OperatorInterfaceRuntime  # noqa: E402

from tests.fixtures.operator_transports import operator_transports  # noqa: E402

from .capture import evidence_browser  # noqa: E402

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
INGEST_CLI = "data013_ingestion_validation_cli"
ALERT_CLI = "data013_quarantine_alert_cli"
FEATURE = "SRS-DATA-013"
#: The acceptance-criterion step this evidence belongs to.
STEP = 3

#: The six records the mixed fixture quarantines, by key, and the four it admits.
QUARANTINED_KEYS = {
    "record:daily_equity_bar:TSLA",
    "record:daily_equity_bar:NVDA",
    "record:daily_equity_bar:AMZN",
    "record:daily_equity_bar:META",
}


@pytest.fixture(scope="module")
def binaries() -> dict[str, Path]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("cargo not on PATH")
    build = subprocess.run(
        [
            cargo,
            "build",
            "-p",
            "atp-data",
            "--bin",
            INGEST_CLI,
            "-p",
            "atp-orchestrator",
            "--bin",
            ALERT_CLI,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    return {name: ROOT / "target" / "debug" / name for name in (INGEST_CLI, ALERT_CLI)}


@pytest.fixture()
def stores(tmp_path: Path) -> tuple[Path, Path, Path]:
    ssd, nas, alerts = tmp_path / "ssd", tmp_path / "nas", tmp_path / "alerts"
    nas.mkdir()
    alerts.mkdir()
    return ssd, nas, alerts


def _dashboard(ssd: Path, alerts: Path) -> Iterator[str]:
    """The SHIPPED composition (`python -m atp_dashboard`), configured only through
    the two env keys an operator sets."""
    runtime = OperatorInterfaceRuntime()
    publisher = mount_default_dashboard(
        runtime, {"ATP_SSD_DATA_DIR": str(ssd), "ATP_NOTIFICATION_STORE_DIR": str(alerts)}
    )
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield f"http://{host}:{port}/dashboard/"
    finally:
        publisher.stop()
        runtime.stop()


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


def _open_pane(page, url: str) -> None:
    with page.expect_response(lambda r: "/dashboard/api/quarantine" in r.url, timeout=20_000):
        page.goto(url, wait_until="domcontentloaded")
    page.wait_for_function(
        "() => document.getElementById('qgate').dataset.state !== 'loading'", timeout=15_000
    )
    page.locator('[data-panel="quarantine"]').scroll_into_view_if_needed()


def _reason_counts(page) -> dict[str, str]:
    return page.eval_on_selector_all(
        "#qreasons .qreason",
        "els => Object.fromEntries(els.map(e => [e.dataset.reason, e.querySelector('.qreason__n').textContent]))",
    )


def test_quarantined_records_are_held_and_the_dashboard_and_alert_show_counts_and_reasons(
    binaries, stores
) -> None:
    ssd, nas, alerts = stores

    # 1. A real ingest of ten records, one failing each SYS-77 rule.
    ingest = _run([str(binaries[INGEST_CLI]), "ingest", "--ssd", str(ssd), "--nas", str(nas)])
    assert ingest.returncode == 0, ingest.stderr
    assert "quarantined_total:6" in ingest.stdout.splitlines()

    # 2. Not written to primary tables: the primary store holds only the four valid records.
    inspect = _run([str(binaries[INGEST_CLI]), "inspect", "--ssd", str(ssd)])
    assert inspect.returncode == 0, inspect.stderr
    present = set(inspect.stdout.splitlines())
    assert "store_len:4" in present
    assert not (QUARANTINED_KEYS & present), f"a quarantined record reached primary: {present}"

    # 3. The dashboard, before any alert went out, and then after one did.
    dashboard = _dashboard(ssd, alerts)
    url = next(dashboard)
    try:
        with evidence_browser(sync_api, FEATURE, step=STEP) as cap:
            page = cap.page()
            page.set_viewport_size({"width": 1320, "height": 900})
            _open_pane(page, url)

            assert page.eval_on_selector("#qgate", "e => e.dataset.state") == "quarantined"
            title = page.text_content("#qgate-title")
            assert "6 of 10 records held at the gate" in title
            assert "4 admitted to the SSD primary" in title
            assert page.text_content("#qflow-in") == "10"
            assert page.text_content("#qflow-admitted-n") == "4"
            assert page.text_content("#qflow-held-n") == "6"
            assert _reason_counts(page) == dict.fromkeys(QUARANTINE_REASONS, "1")
            # Nothing has been sent yet, and the pane says so rather than showing a quiet strip.
            assert page.eval_on_selector("#qalert", "e => e.dataset.state") == "bad"
            assert "NOT YET DELIVERED" in page.text_content("#qalert-text")
            cap.shot(
                page,
                "quarantine pane before the alert: 6 of 10 held, one per SYS-77 rule, alert NOT YET DELIVERED",
                element='[data-panel="quarantine"]',
            )

            # 4. The real notify pass, through the two transports.
            with operator_transports() as (relay, ntfy, env):
                sent = _run(
                    [str(binaries[ALERT_CLI]), "notify", "--ssd", str(ssd), "--store", str(alerts)],
                    env={**env, "PATH": os.environ.get("PATH", "")},
                )
                assert sent.returncode == 0, sent.stdout + sent.stderr
                emailed, pushed = relay.messages, ntfy.published

            # The notification alert carries the count and every reason, on both channels.
            assert len(emailed) == 1 and len(pushed) == 1
            for body in (emailed[0], pushed[0]):
                assert "6 of 10 records quarantined" in body
                for reason in QUARANTINE_REASONS:
                    assert f"{reason}=1" in body, f"{reason} missing from {body!r}"

            # 5. The pane picks the delivery up on its next poll, with each channel's outcome.
            page.wait_for_function(
                "() => document.getElementById('qalert').dataset.state === 'ok'", timeout=15_000
            )
            assert page.text_content("#qalert-text") == "operator alerted"
            chips = page.eval_on_selector_all(
                "#qalert-chans .qchan", "els => els.map(e => e.textContent)"
            )
            assert chips == ["EMAIL · QUEUED", "PUSH · DELIVERED"]
            ledger_alert = page.text_content("#qledger-rows tr:first-child .qalertchip")
            assert ledger_alert == "alerted"
            cap.shot(
                page,
                "quarantine pane after the real notify pass: alert out on email (queued at the relay) and push (delivered)",
                element='[data-panel="quarantine"]',
            )
    finally:
        next(dashboard, None)


def test_an_unreadable_store_shows_no_count(binaries, tmp_path: Path) -> None:
    """The non-vacuity partner: when nothing was read, the pane shows nothing it did not read."""

    dashboard = _dashboard(tmp_path / "not-mounted", tmp_path)
    url = next(dashboard)
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            _open_pane(page, url)
            assert page.eval_on_selector("#qgate", "e => e.dataset.state") == "unreadable"
            assert "SSD primary directory is missing" in page.text_content("#qgate-title")
            assert page.text_content("#qflow-held-n") == "—"
            assert set(_reason_counts(page).values()) == {"—"}
            assert page.is_hidden("#qledger-table")
            browser.close()
    finally:
        next(dashboard, None)
