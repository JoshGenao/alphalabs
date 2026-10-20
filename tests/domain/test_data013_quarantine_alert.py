"""SRS-DATA-013 / SyRS SYS-77 — the operator is alerted to quarantined records with their count
and nature, through the SRS-NOTIF-001 email + push channels.

L7 domain (safety) test. The safety core: a quarantine the operator never hears about is a silent
data-quality failure, so every run that quarantined a record must be alerted, an alert no channel
carried must be retried rather than marked done, and an operator binary that cannot reach its
transports must refuse before sending anything rather than report a clean pass.

  1. Behavioral (Rust) — the integration tests in
     ``crates/atp-orchestrator/tests/srs_data_013_quarantine_alert.rs`` drive a real ingest, the
     real dispatcher (fake channels mirroring the real adapters), and the real audit store.
  2. Behavioral (operator CLI) — ``data013_quarantine_alert_cli`` refuses unusable configuration
     with exit 2 and sends nothing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = "data013_quarantine_alert_cli"


def _cargo() -> str:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH")
    return cargo


@pytest.mark.parametrize(
    "rust_test",
    [
        "a_quarantine_run_is_alerted_once_with_its_count_and_nature",
        "a_clean_run_is_not_alerted",
        "an_alert_that_reached_no_channel_is_retried",
        "an_unreadable_run_is_alerted_as_unreadable",
        "an_unreadable_store_is_an_error_not_nothing_due",
        # Review L2 r1-r2: no run falls out of view — however many runs follow it, or however
        # late it is published.
        "an_old_quarantine_run_behind_200_clean_runs_is_still_alerted",
        "a_failed_alert_is_retried_however_many_runs_follow_it",
        "a_run_published_after_newer_runs_settled_is_still_alerted",
        "a_damaged_marker_cannot_hide_a_run",
    ],
)
def test_rust_quarantine_alert_behaviour(rust_test: str) -> None:
    result = subprocess.run(
        [
            _cargo(),
            "test",
            "-p",
            "atp-orchestrator",
            "--test",
            "srs_data_013_quarantine_alert",
            rust_test,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "1 passed" in combined, f"the named Rust test did not run:\n{combined}"


def _binary() -> Path:
    build = subprocess.run(
        [_cargo(), "build", "-q", "-p", "atp-orchestrator", "--bin", CLI],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    return REPO_ROOT / "target" / "debug" / CLI


def _clean_env(**extra: str) -> dict[str, str]:
    """The process environment without any notification transport configuration."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ATP_SMTP_", "ATP_PUSH_"))}
    env.pop("ATP_OPERATOR_EMAIL", None)
    env.pop("ATP_ENV", None)
    env.update(extra)
    return env


def test_unconfigured_transports_are_refused_before_any_send() -> None:
    binary = _binary()
    with tempfile.TemporaryDirectory(prefix="atp_data013_alert_l7_") as tmp:
        store = Path(tmp) / "alerts"
        store.mkdir()
        result = subprocess.run(
            [str(binary), "notify", "--ssd", str(Path(tmp) / "ssd"), "--store", str(store)],
            env=_clean_env(),
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2, result.stdout + result.stderr
        assert "transport configuration is unusable" in result.stderr
        assert not any(store.iterdir()), "nothing was stored: nothing was sent"


def test_placeholder_credentials_are_refused_in_production() -> None:
    binary = _binary()
    placeholder = "placeholder-set-in-environment"
    with tempfile.TemporaryDirectory(prefix="atp_data013_alert_l7_") as tmp:
        store = Path(tmp) / "alerts"
        store.mkdir()
        result = subprocess.run(
            [str(binary), "notify", "--ssd", str(Path(tmp) / "ssd"), "--store", str(store)],
            env=_clean_env(
                ATP_ENV="production",
                ATP_SMTP_API_KEY=placeholder,
                ATP_PUSH_TOPIC=placeholder,
                ATP_PUSH_TOKEN=placeholder,
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2, result.stdout + result.stderr
        assert "placeholder" in result.stderr


def test_a_missing_notification_store_is_refused() -> None:
    binary = _binary()
    with tempfile.TemporaryDirectory(prefix="atp_data013_alert_l7_") as tmp:
        result = subprocess.run(
            [
                str(binary),
                "notify",
                "--ssd",
                str(Path(tmp) / "ssd"),
                "--store",
                str(Path(tmp) / "no-such-store"),
            ],
            env=_clean_env(),
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "does not exist" in result.stderr
