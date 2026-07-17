"""SRS-MD-006 live leg — the REAL IB Gateway round-trip for SYS-76(a)+(b).

Gated by ``ATP_RUN_INTEGRATION=1`` (auto-skipped otherwise) because it binds
the fixed paper port 4002 and requires a live gateway session — forbidden in
parallel agent runs (single-live invariant). This is the deferred[] leg that
keeps SRS-MD-006 serialized: an operator (or an exclusive live session) runs
it to demonstrate the two IB sub-checks from a genuine gateway round-trip,
by re-running the SRS-EXE-006 paper-account diagnostic and feeding its
outcome through the same readiness fold every solo test exercises.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from atp_readiness.runtime import build_runtime_report
from atp_reliability.restart import (
    REQUIRED_SUBCHECKS,
    SubCheck,
    SubCheckResult,
    SubCheckStatus,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


class RecordingSink:
    def __init__(self) -> None:
        self.alerts = []

    def dispatch(self, alert) -> None:
        self.alerts.append(alert)


def test_live_ib_round_trip_feeds_the_readiness_fold() -> None:
    """Run the real paper-account diagnostic; PASS both IB sub-checks from it."""

    result = subprocess.run(
        [
            "cargo",
            "test",
            "-p",
            "atp-adapters",
            "--test",
            "srs_exe_006_ib_diagnostic",
            "paper_account_per_operation_diagnostic",
            "--",
            "--exact",
            "--ignored",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    combined = result.stdout + result.stderr
    live_ok = result.returncode == 0 and "1 passed" in combined
    status = SubCheckStatus.PASS if live_ok else SubCheckStatus.FAIL

    results = [
        SubCheckResult(check=check, status=SubCheckStatus.PASS)
        for check in REQUIRED_SUBCHECKS
        if check not in (SubCheck.IB_CONNECTIVITY, SubCheck.IB_ACCOUNT)
    ]
    results += [
        SubCheckResult(check=SubCheck.IB_CONNECTIVITY, status=status),
        SubCheckResult(check=SubCheck.IB_ACCOUNT, status=status),
    ]
    sink = RecordingSink()
    report = build_runtime_report(results, alert_sink=sink, timestamp_ns=1)
    assert live_ok, (
        "live IB Gateway round-trip failed — the readiness fold correctly "
        f"holds the gate (alerts: {[a.key for a in sink.alerts]}):\n{combined[-2000:]}"
    )
    assert report.ok
