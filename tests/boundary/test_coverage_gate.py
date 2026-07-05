"""SRS-DATA-011 coverage gate — L4 boundary test (the CLI's adjusted output contract).

Pins the trust-boundary CONTRACT of ``data007_query_cli --normalization split-adjusted`` /
``fully-adjusted`` (the surface a Python consumer would parse): a covered query echoes the served
``normalization:<mode>``, a ``coverage_through:<D>`` line (so a consumer can validate the adjustment +
know the proven frontier), an ``adjusted_through:<ts>`` basis line, and an ``event_count:<n>`` line
(plus ``event.<i>.*`` structural corporate actions when the window spans one); an uncovered query
fails closed with a DISTINCT, parseable failure (exit != 0, stderr naming the have/need frontiers +
SRS-DATA-011) — never a silent fall-through to raw-as-adjusted. Builds the data CLIs on demand (skips
if cargo is unavailable, like the other cargo-driven boundary checks).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


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


def _parse_envelope(stdout: str) -> dict[str, str]:
    """Parse the source-neutral ``key:value`` header lines (ignore the per-record lines)."""
    env: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep and not key.startswith("record."):
            env[key] = value
    return env


def test_split_adjusted_output_contract() -> None:
    cargo = _cargo()
    if cargo is None:
        import pytest

        pytest.skip("cargo not on PATH")
    ingest_bin, coverage_bin, query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
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
        )
        _run(
            str(ingest_bin),
            "ingest",
            "--dir",
            tmp,
            "--kind",
            "corporate-action-split",
            "--event-ts",
            "200",
        )
        _run(
            str(coverage_bin),
            "assert-coverage",
            "--dir",
            tmp,
            "--symbol",
            "AAPL",
            "--through",
            "200",
        )

        def query(
            end: int, normalization: str = "split-adjusted"
        ) -> subprocess.CompletedProcess[str]:
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
                normalization,
            )

        # COVERED: the envelope echoes the served mode + the proven coverage frontier + the basis +
        # the (empty here) structural-event count.
        covered = query(100)
        assert covered.returncode == 0, covered.stderr
        env = _parse_envelope(covered.stdout)
        assert env.get("normalization") == "split-adjusted", env
        assert env.get("coverage_through") == "200", env
        assert env.get("adjusted_through") == "200", env
        assert env.get("event_count") == "0", env
        assert env.get("match_count") == "1", env

        # COVERED fully-adjusted: the same contract with its own mode echo (the dividend leg's math is
        # pinned by the crate unit tests + coverage_manifest_check round-trip; this is the envelope).
        fully = query(100, "fully-adjusted")
        assert fully.returncode == 0, fully.stderr
        env = _parse_envelope(fully.stdout)
        assert env.get("normalization") == "fully-adjusted", env
        assert env.get("coverage_through") == "200", env
        assert env.get("adjusted_through") == "200", env
        assert env.get("event_count") == "0", env

        # UNCOVERED: a DISTINCT, parseable fail-closed (no raw-as-adjusted), naming have/need + the
        # owner — identically for BOTH adjusted modes.
        for normalization in ("split-adjusted", "fully-adjusted"):
            uncovered = query(250, normalization)
            assert uncovered.returncode != 0
            assert not uncovered.stdout.strip(), "a refused query must not emit any record output"
            assert "SRS-DATA-011" in uncovered.stderr
            assert "200" in uncovered.stderr and "250" in uncovered.stderr


def test_raw_path_is_unchanged_by_the_gate() -> None:
    cargo = _cargo()
    if cargo is None:
        import pytest

        pytest.skip("cargo not on PATH")
    ingest_bin, _coverage_bin, query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
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
        )
        # RAW needs no coverage and carries no coverage_through line (the gate is split-adjusted-only).
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
        assert raw.returncode == 0, raw.stderr
        env = _parse_envelope(raw.stdout)
        assert env.get("normalization") == "raw", env
        assert "coverage_through" not in env, "raw output must not carry a coverage_through line"
        assert "adjusted_through" not in env, "raw output must not carry an adjusted_through line"
        assert "event_count" not in env, "raw output must not carry an event_count line"
