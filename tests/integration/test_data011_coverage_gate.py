"""SRS-DATA-011 corporate-action coverage gate — L5 integration test (end-to-end value correctness).

Gated by ``ATP_RUN_INTEGRATION=1`` (see tests/conftest.py). Builds the real data CLIs and exercises the
coverage gate end to end over a real ingested store: ingest a daily bar (AAPL@100, close 10000) + a
4-for-1 split @200, assert coverage AAPL through 200, then prove

  * COVERED  -> ``data007_query_cli --normalization split-adjusted`` over [0,100] returns the ADJUSTED
    bar (close 2500 = 10000/4, volume 400000 = 100000*4) and echoes ``coverage_through:200``;
  * UNCOVERED -> the same query past the frontier ([0,250]) FAILS closed naming SRS-DATA-011.

SRS-DATA-011 STAYS passes:false (foundational: only splits / reverse-splits have math + coverage).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.integration


def _cargo() -> str | None:
    return shutil.which("cargo")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), cwd=ROOT, check=False, capture_output=True, text=True)


def _build(cargo: str) -> tuple[Path, Path, Path]:
    build = _run(
        cargo, "build", "-q", "-p", "atp-data",
        "--bin", "data016_ingest_cli", "--bin", "data011_coverage_cli", "--bin", "data007_query_cli",
    )
    assert build.returncode == 0, build.stdout + build.stderr
    debug = ROOT / "target" / "debug"
    return debug / "data016_ingest_cli", debug / "data011_coverage_cli", debug / "data007_query_cli"


def _fields(stdout: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in stdout.splitlines():
        if line.startswith("record.0.field."):
            name, value = line[len("record.0.field."):].split(":", 1)
            out[name] = int(value)
    return out


def test_coverage_gate_end_to_end() -> None:
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, coverage_bin, query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        # Ingest a daily bar (AAPL@100, close 10000, vol 100000) and a 4-for-1 split @200.
        assert _run(str(ingest_bin), "ingest", "--dir", tmp, "--kind", "daily-equity-bar",
                    "--event-ts", "100", "--init").returncode == 0
        assert _run(str(ingest_bin), "ingest", "--dir", tmp, "--kind", "corporate-action-split",
                    "--event-ts", "200").returncode == 0
        # Assert coverage AAPL complete through 200.
        asserted = _run(str(coverage_bin), "assert-coverage", "--dir", tmp,
                        "--symbol", "AAPL", "--through", "200")
        assert asserted.returncode == 0, asserted.stderr
        assert "frontier:200" in asserted.stdout
        assert "outcome:inserted" in asserted.stdout

        def query(end: int) -> subprocess.CompletedProcess[str]:
            return _run(
                str(query_bin), "query", "--dir", tmp, "--symbol", "AAPL", "--resolution", "1d",
                "--start", "0", "--end", str(end), "--kind", "daily-equity-bar",
                "--normalization", "split-adjusted",
            )

        # COVERED [0,100]: the bar@100 is pre-split (200 > 100) -> adjusted onto the as-of-200 basis.
        covered = query(100)
        assert covered.returncode == 0, covered.stderr
        assert "normalization:split-adjusted" in covered.stdout
        assert "coverage_through:200" in covered.stdout
        fields = _fields(covered.stdout)
        assert fields["close"] == 2500, fields  # 10000 / 4
        assert fields["volume"] == 400000, fields  # 100000 * 4

        # UNCOVERED [0,250]: end 250 > frontier 200 -> fail closed naming the coverage owner.
        uncovered = query(250)
        assert uncovered.returncode != 0
        assert "SRS-DATA-011" in uncovered.stderr
        assert "200" in uncovered.stderr and "250" in uncovered.stderr  # have / need


def test_data016_refuses_to_ingest_coverage() -> None:
    # The coverage frontier is a trust assertion with a SINGLE write surface (data011_coverage_cli).
    # The generic market-data ingest CLI must refuse --kind corporate-action-coverage, so there is no
    # second, fixture-shaped route to grant split-adjusted coverage.
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, _coverage_bin, _query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        refused = _run(str(ingest_bin), "ingest", "--dir", tmp, "--kind",
                       "corporate-action-coverage", "--event-ts", "500", "--init")
        assert refused.returncode != 0
        assert "data011_coverage_cli" in refused.stderr
        # The other kinds still ingest fine through data016.
        assert _run(str(ingest_bin), "ingest", "--dir", tmp, "--kind", "daily-equity-bar",
                    "--event-ts", "100").returncode == 0


def test_coverage_advances_monotonically() -> None:
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, coverage_bin, query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        assert _run(str(ingest_bin), "ingest", "--dir", tmp, "--kind", "daily-equity-bar",
                    "--event-ts", "100", "--init").returncode == 0
        # First assert through 150 (Inserted), then advance to 300 (Inserted), then re-assert 300
        # (unchanged). A lower re-assert (100) never regresses the frontier.
        first = _run(str(coverage_bin), "assert-coverage", "--dir", tmp, "--symbol", "AAPL", "--through", "150")
        assert "outcome:inserted" in first.stdout and "frontier:150" in first.stdout
        advanced = _run(str(coverage_bin), "assert-coverage", "--dir", tmp, "--symbol", "AAPL", "--through", "300")
        assert "outcome:inserted" in advanced.stdout and "frontier:300" in advanced.stdout
        again = _run(str(coverage_bin), "assert-coverage", "--dir", tmp, "--symbol", "AAPL", "--through", "300")
        assert "outcome:unchanged" in again.stdout and "frontier:300" in again.stdout
        lower = _run(str(coverage_bin), "assert-coverage", "--dir", tmp, "--symbol", "AAPL", "--through", "100")
        assert "outcome:inserted" in lower.stdout, "a new lower through is a distinct (Inserted) record"
        assert "frontier:300" in lower.stdout, "the effective frontier never regresses (max)"
