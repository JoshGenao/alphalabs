"""SRS-DATA-017 (support concurrent reads during ingestion writes) — L5 cross-process Load test.

Gated by ``ATP_RUN_INTEGRATION=1`` (see tests/conftest.py). The AC names "strategy containers,
backtests, factor jobs, and notebooks" as the readers; those are separate OS processes, so the
faithful Load test runs reader PROCESSES (``data007_query_cli query`` + ``data016_ingest_cli inspect``
— the operator-demonstrable lock-free reader surfaces, which exercise the exact ``load_from_path`` read
path an in-process strategy/backtest/notebook binding would use) against the store WHILE a writer
process genuinely holds the single-writer lock mid load-modify-save.

The lock-held window is REAL and KNOWN, not inferred from "the subprocess is alive": the
``data017_lock_holder`` example fixture acquires the lock, does the in-memory modify, signals a
ready-file, and only commits + releases when a release-file appears. While it holds the lock, reader
processes must (a) COMPLETE within a timeout — proving reads are non-blocking even though a writer
holds the lock (a blocked read raises ``TimeoutExpired`` and FAILS the test, never counts as success),
(b) still see the previously-ingested "completed" seed (no corruption), and (c) NOT see the holder's
uncommitted write (snapshot isolation). After release the committed write becomes visible and the
catalog is intact. (The always-run Rust integration test ``srs_data_017_concurrent_reads`` carries the
deterministic in-process overlap, non-blocking, and two-writer no-loss proofs.)
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SEED_TS = 1_700_000_000
READ_TIMEOUT = 30.0  # a read that BLOCKS on the held lock must fail (TimeoutExpired), not hang

pytestmark = pytest.mark.integration


def _cargo() -> str | None:
    return shutil.which("cargo")


def _run(*args: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args), cwd=ROOT, check=False, capture_output=True, text=True, timeout=timeout
    )


def _build(cargo: str) -> tuple[str, str, str]:
    build = _run(
        cargo,
        "build",
        "-q",
        "-p",
        "atp-data",
        "--bin",
        "data016_ingest_cli",
        "--bin",
        "data007_query_cli",
        "--example",
        "data017_lock_holder",
    )
    assert build.returncode == 0, build.stdout + build.stderr
    debug = ROOT / "target" / "debug"
    return (
        str(debug / "data016_ingest_cli"),
        str(debug / "data007_query_cli"),
        str(debug / "examples" / "data017_lock_holder"),
    )


def test_reader_processes_are_non_blocking_and_uncorrupted_during_a_held_write_lock() -> None:
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, query_bin, holder_bin = _build(cargo)

    with tempfile.TemporaryDirectory() as tmp:
        # Seed the previously-ingested ("completed") data the readers must always observe.
        seed = _run(ingest_bin, "ingest", "--dir", tmp, "--kind", "daily-equity-bar", "--init")
        assert seed.returncode == 0, seed.stdout + seed.stderr

        ready_file = Path(tmp) / "holder.ready"
        release_file = Path(tmp) / "holder.release"

        # Start the writer process that genuinely HOLDS the lock mid load-modify-save.
        holder = subprocess.Popen(
            [
                holder_bin,
                "--dir",
                tmp,
                "--ready-file",
                str(ready_file),
                "--release-file",
                str(release_file),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # Wait until the holder has acquired the lock and is mid-write (bounded).
            deadline = time.monotonic() + 30.0
            while not ready_file.exists() and time.monotonic() < deadline:
                assert holder.poll() is None, "the lock holder exited before signalling ready"
                time.sleep(0.01)
            assert ready_file.exists(), "the lock holder never acquired the lock / signalled ready"

            # The lock is now genuinely HELD and a write is in progress (uncommitted). Reader PROCESSES
            # must complete within the timeout (non-blocking), see the seed (completed data, uncorrupted),
            # and NOT see the holder's uncommitted write (snapshot isolation: store_len stays at the seed).
            for _ in range(6):
                assert not release_file.exists(), "released too early — window invariant broken"
                q = _run(
                    query_bin,
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
                    "9999999999",
                    timeout=READ_TIMEOUT,
                )
                assert q.returncode == 0, q.stdout + q.stderr
                mc = [ln for ln in q.stdout.splitlines() if ln.startswith("match_count:")]
                assert mc and int(mc[0].split(":", 1)[1]) >= 1, (
                    f"seed unreadable during a held write: {q.stdout!r}"
                )

                insp = _run(ingest_bin, "inspect", "--dir", tmp, timeout=READ_TIMEOUT)
                assert insp.returncode == 0, insp.stdout + insp.stderr
                lens = [ln for ln in insp.stdout.splitlines() if ln.startswith("store_len:")]
                assert lens and int(lens[0].split(":", 1)[1]) == 2, (
                    f"a reader saw the holder's UNCOMMITTED write (snapshot isolation broken): {insp.stdout!r}"
                )

            # Release the holder: it commits and drops the lock.
            release_file.write_text("go\n")
            assert holder.wait(timeout=30) == 0, holder.communicate()
        finally:
            if holder.poll() is None:
                release_file.write_text("go\n")
                try:
                    holder.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    holder.kill()

        # After commit, the holder's batch is visible and the catalog is intact (seed + holder batch).
        final = _run(ingest_bin, "inspect", "--dir", tmp, timeout=READ_TIMEOUT)
        assert final.returncode == 0, final.stdout + final.stderr
        store_len = next(
            int(ln.split(":", 1)[1])
            for ln in final.stdout.splitlines()
            if ln.startswith("store_len:")
        )
        assert store_len == 4, f"expected seed (2) + committed holder batch (2); got {store_len}"


def test_many_reader_processes_read_a_static_store_without_a_lock() -> None:
    # Baseline: many concurrent reader processes against a published store all succeed (no reader needs
    # — or contends for — the single-writer lock), each within the read timeout.
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, query_bin, _holder = _build(cargo)

    with tempfile.TemporaryDirectory() as tmp:
        seed = _run(ingest_bin, "ingest", "--dir", tmp, "--kind", "daily-equity-bar", "--init")
        assert seed.returncode == 0, seed.stdout + seed.stderr

        errors: list[str] = []

        def reader() -> None:
            try:
                q = _run(
                    query_bin,
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
                    "9999999999",
                    timeout=READ_TIMEOUT,
                )
                if q.returncode != 0:
                    errors.append(q.stdout + q.stderr)
            except subprocess.TimeoutExpired:
                errors.append("a lock-free read blocked against a static store")

        threads = [threading.Thread(target=reader) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], "; ".join(errors)
