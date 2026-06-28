"""SRS-DATA-011 corporate-action coverage — L7 domain test (the keystone safety invariant).

The acceptance: corporate actions are reflected in historical records so that "backtests spanning
corporate-action dates produce correct P&L calculations under the selected normalization mode." The
keystone safety property this session ships is: **split-adjusted history is served ONLY behind proven
coverage** — a backtest / strategy can never silently consume raw-as-adjusted bars. This domain test
drives the real gate (``data007_query_cli --normalization split-adjusted`` over a real ingested store,
standing in for a backtest reading split-adjusted history) and asserts:

  * COVERED — when the symbol's coverage frontier reaches the query end, the pre-split bar is re-quoted
    onto the split-comparable basis (a 10000 close under a 4-for-1 split reads 2500), so a backtest sees
    a continuous (split-adjusted) series — correct P&L;
  * UNCOVERED — when coverage is absent OR does not reach the query end, the read FAILS CLOSED (a
    structured error naming SRS-DATA-011), so a backtest is never handed raw bars dressed as adjusted.

Plus a structural assertion that the gate is registered in the architecture metadata as foundational
(passes:false) substrate. Builds the data CLIs on demand (skips if cargo is unavailable, like the other
cargo-driven domain tests). This is the paired ``tests/domain/`` diff for the safety-critical coverage
paths (``crates/atp-data/src/coverage.rs``, ``data011_coverage_cli``).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = ROOT / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

pytestmark = pytest.mark.domain


def _cargo() -> str | None:
    return shutil.which("cargo")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), cwd=ROOT, check=False, capture_output=True, text=True)


def _build(cargo: str) -> tuple[Path, Path, Path]:
    build = _run(
        cargo,
        "build",
        "-q",
        "-p",
        "atp-data",
        "--bin",
        "data016_ingest_cli",
        "--bin",
        "data011_coverage_cli",
        "--bin",
        "data007_query_cli",
    )
    assert build.returncode == 0, build.stdout + build.stderr
    debug = ROOT / "target" / "debug"
    return debug / "data016_ingest_cli", debug / "data011_coverage_cli", debug / "data007_query_cli"


def _close(stdout: str) -> int | None:
    for line in stdout.splitlines():
        if line.startswith("record.0.field.close:"):
            return int(line.split(":", 1)[1])
    return None


def test_backtest_gets_adjusted_only_when_covered_else_fails_closed() -> None:
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, coverage_bin, query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        # A pre-split daily bar (AAPL@100, close 10000) and a 4-for-1 split @200.
        assert (
            _run(
                str(ingest_bin),
                "ingest",
                "--dir",
                tmp,
                "--kind",
                "daily-equity-bar",
                "--event-ts",
                "100",
                "--init",
            ).returncode
            == 0
        )
        assert (
            _run(
                str(ingest_bin),
                "ingest",
                "--dir",
                tmp,
                "--kind",
                "corporate-action-split",
                "--event-ts",
                "200",
            ).returncode
            == 0
        )

        def split_adjusted(end: int) -> subprocess.CompletedProcess[str]:
            return _run(
                str(query_bin),
                "query",
                "--dir",
                tmp,
                "--symbol",
                "AAPL",
                "--resolution",
                "1d",
                "--start",
                "0",
                "--end",
                str(end),
                "--kind",
                "daily-equity-bar",
                "--normalization",
                "split-adjusted",
            )

        # (1) NO coverage yet -> a backtest reading split-adjusted history FAILS CLOSED (never raw bars
        # dressed as adjusted). This is the keystone safety property.
        no_coverage = split_adjusted(100)
        assert no_coverage.returncode != 0
        assert "SRS-DATA-011" in no_coverage.stderr

        # (2) Assert coverage through 200, then the COVERED read re-quotes the pre-split bar onto the
        # split-comparable basis: 10000 / 4 = 2500. A backtest sees the adjusted series -> correct P&L.
        assert (
            _run(
                str(coverage_bin),
                "assert-coverage",
                "--dir",
                tmp,
                "--symbol",
                "AAPL",
                "--through",
                "200",
            ).returncode
            == 0
        )
        covered = split_adjusted(100)
        assert covered.returncode == 0, covered.stderr
        assert _close(covered.stdout) == 2500
        assert "coverage_through:200" in covered.stdout

        # (3) A query PAST the frontier (end 250 > 200) still FAILS CLOSED: partial coverage is not
        # enough -- a split could exist in the uncovered tail (200, 250].
        past_frontier = split_adjusted(250)
        assert past_frontier.returncode != 0
        assert "SRS-DATA-011" in past_frontier.stderr

        # (4) The RAW path is always available without coverage (the gate is split-adjusted-only): a
        # backtest can still read unadjusted bars explicitly.
        raw = _run(
            str(query_bin),
            "query",
            "--dir",
            tmp,
            "--symbol",
            "AAPL",
            "--resolution",
            "1d",
            "--start",
            "0",
            "--end",
            "100",
            "--kind",
            "daily-equity-bar",
            "--normalization",
            "raw",
        )
        assert raw.returncode == 0
        assert _close(raw.stdout) == 10000


def test_coverage_gate_is_registered_foundational() -> None:
    # Structural: the gate is registered in the architecture metadata as foundational (passes:false)
    # substrate with the six static guards pinned, so the contract cannot silently drift.
    from coverage_manifest_check import (
        assert_coverage_manifest_static,
        contract_block,
        load_config,
    )

    config = load_config()
    block = contract_block(config)
    assert block["passes"] is False
    assert block["requirement"] == "SRS-DATA-011"
    assert len(assert_coverage_manifest_static(config, ROOT)) == 8
