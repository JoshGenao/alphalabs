"""L4 boundary tests: the SRS-DATA-013 quarantine pane on a real runtime.

Real binaries, real stores on disk, the real route. The quarantine store is
written by a real ingest; the alert is sent by the real
``data013_quarantine_alert_cli notify`` through loopback stand-ins for the two
operator transports (``tests/fixtures/operator_transports.py``), so the alert
delivery the pane shows was produced by the same dispatcher and audit store the
deployment uses.

* a bare ``SRS-UI-001`` mount serves NO quarantine route (404);
* the production entrypoint always composes the pane, and with nothing
  configured it answers ``not_configured`` with no count;
* configured over real stores, it serves the run's real counts and reasons and
  the alert state the next notify pass would act on.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "python"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from atp_dashboard.provider import ReadinessBackedProvider  # noqa: E402
from atp_dashboard.quarantine import QUARANTINE_REASONS  # noqa: E402
from atp_dashboard.server import (  # noqa: E402
    QUARANTINE_SNAPSHOT_PATH,
    mount_dashboard,
    mount_default_dashboard,
)
from atp_runtime.runtime import OperatorInterfaceRuntime  # noqa: E402

from tests.fixtures.operator_transports import operator_transports  # noqa: E402

pytestmark = pytest.mark.boundary

INGEST_CLI = "data013_ingestion_validation_cli"
ALERT_CLI = "data013_quarantine_alert_cli"


@pytest.fixture(scope="module")
def binaries() -> dict[str, Path]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("cargo not on PATH")
    build = subprocess.run(
        [
            cargo,
            "build",
            "-q",
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


@pytest.fixture(autouse=True)
def every_request_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests read the same runtime before and after a store changes."""
    from atp_dashboard import quarantine

    monkeypatch.setattr(quarantine, "MIN_READ_INTERVAL_S", 0.0)


@pytest.fixture()
def stores(tmp_path: Path) -> tuple[Path, Path]:
    ssd, nas, alerts = tmp_path / "ssd", tmp_path / "nas", tmp_path / "alerts"
    nas.mkdir()
    alerts.mkdir()
    return ssd, alerts


def _ingest_mixed(binaries: dict[str, Path], ssd: Path) -> None:
    """One real run: the SYS-77 mixed fixture, 10 records, one failing each rule."""
    nas = ssd.parent / "nas"
    result = subprocess.run(
        [str(binaries[INGEST_CLI]), "ingest", "--ssd", str(ssd), "--nas", str(nas)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _env(ssd: Path, alerts: Path) -> dict[str, str]:
    return {"ATP_SSD_DATA_DIR": str(ssd), "ATP_NOTIFICATION_STORE_DIR": str(alerts)}


def _get(runtime: OperatorInterfaceRuntime) -> dict:
    status, body = runtime.dispatch_rest("GET", QUARANTINE_SNAPSHOT_PATH, b"")
    assert status == 200, body
    return body


def test_a_bare_mount_serves_no_quarantine_route() -> None:
    runtime = OperatorInterfaceRuntime()
    mount_dashboard(runtime, ReadinessBackedProvider({}))
    status, _body = runtime.dispatch_rest("GET", QUARANTINE_SNAPSHOT_PATH, b"")
    assert status == 404


def test_the_entrypoint_composes_the_pane_and_says_when_nothing_is_configured() -> None:
    runtime = OperatorInterfaceRuntime()
    mount_default_dashboard(runtime, {})
    body = _get(runtime)
    assert body["state"] == "not_configured"
    assert body["runs"] is None, "unconfigured is not 'no quarantined records'"


def test_a_configured_ssd_that_is_not_there_is_unreadable(tmp_path: Path, binaries) -> None:
    runtime = OperatorInterfaceRuntime()
    mount_default_dashboard(runtime, _env(tmp_path / "not-mounted", tmp_path))
    body = _get(runtime)
    assert body["state"] == "unreadable"
    assert body["runs"] is None
    assert "SSD primary directory is missing" in body["error"]


def test_a_real_run_is_served_with_its_counts_reasons_and_alert(binaries, stores) -> None:
    ssd, alerts = stores
    _ingest_mixed(binaries, ssd)
    runtime = OperatorInterfaceRuntime()
    mount_default_dashboard(runtime, _env(ssd, alerts))

    before = _get(runtime)
    assert before["state"] == "quarantined"
    run = before["runs"][0]
    assert (run["records_in"], run["written"], run["quarantined_total"]) == (10, 4, 6)
    assert run["primary"] == "WRITTEN"
    assert run["counts"] == dict.fromkeys(QUARANTINE_REASONS, 1)
    assert run["alert"]["state"] == "DUE", "not yet sent"
    assert before["alerts"]["due"] == 1

    with operator_transports() as (relay, ntfy, env):
        sent = subprocess.run(
            [str(binaries[ALERT_CLI]), "notify", "--ssd", str(ssd), "--store", str(alerts)],
            env={**env, "PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
        )
        assert sent.returncode == 0, sent.stdout + sent.stderr
        assert len(relay.messages) == 1 and len(ntfy.published) == 1
        # The alert carries the count and the nature (every reason), as SYS-77 requires.
        assert "6 of 10 records quarantined" in ntfy.published[0]
        for reason in QUARANTINE_REASONS:
            assert f"{reason}=1" in ntfy.published[0]

    after = _get(runtime)
    alert = after["runs"][0]["alert"]
    assert alert["state"] == "ALERTED"
    assert alert["settled"] is True
    assert alert["channels"] == {"EMAIL": "QUEUED", "PUSH": "DELIVERED"}
    assert after["alerts"]["due"] == 0


def test_the_status_read_sends_nothing(binaries, stores) -> None:
    """Polling the pane must never page the operator: `status` takes no transport."""
    ssd, alerts = stores
    _ingest_mixed(binaries, ssd)
    runtime = OperatorInterfaceRuntime()
    mount_default_dashboard(runtime, _env(ssd, alerts))
    with operator_transports() as (relay, ntfy, _env_unused):
        for _ in range(3):
            assert _get(runtime)["runs"][0]["alert"]["state"] == "DUE"
        assert relay.messages == [] and ntfy.published == []
    assert not (alerts / "notification_events.store").exists(), "nothing was stored either"
