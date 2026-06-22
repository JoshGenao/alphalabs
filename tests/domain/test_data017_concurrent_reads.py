"""SRS-DATA-017 / SyRS SYS-63 / StRS SN-1.26, SN-1.28 -- strategy containers, backtests, factor jobs,
and notebooks read previously ingested data while ingestion jobs write new data WITHOUT corruption or
blocking completed data.

L7 domain (data-integrity) test. The acceptance criterion's safety core is read integrity under
concurrent writes: a backtest or running strategy that reads the historical store WHILE a nightly
ingestion job is writing new dates must never observe a half-written / torn store, and must never lose
the already-ingested ("completed") data it depends on. A torn read would feed a strategy garbage bars
(a mis-fill, a mis-ranked Reservoir strategy); a blocked read would stall a trading decision. The
substrate guarantees this by snapshot isolation -- lock-free readers over an atomically-published file
that fails closed on corruption, while writers serialize behind a single-writer lock. This test proves
the invariant from two angles:

  1. Behavioral -- it runs the Rust L5 Load test
     ``crates/atp-data/tests/srs_data_017_concurrent_reads.rs`` (a lock-held writer thread doing many
     load-modify-save ingests concurrently with lock-free reader threads, asserting no torn read, no
     lost seed, monotonic snapshots, and that a read never blocks on a held writer lock), and exercises
     the real operator CLIs concurrently over a temp directory (a ``data016_ingest_cli ingest`` writer
     PROCESS churning new dates while ``inspect`` + ``data007_query_cli query`` reader PROCESSES read),
     asserting every concurrent read succeeds and still sees the seed.

  2. Structural -- it asserts, via ``tools/concurrent_read_check.py``, the three integrity-critical
     guards (the query reader takes no lock, the write is atomically published, and a read fails closed
     on a checksum mismatch) and checks each for non-vacuity (an injected reader lock, a non-atomic
     publish, and a dropped checksum guard are each shown to be caught).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.domain

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPO_ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from concurrent_read_check import (  # noqa: E402
    ConcurrentReadCheckError,
    check_atomic_publish,
    check_fail_closed_read,
    check_read_cli_lock_free,
    load_config,
    read_cli_source,
    store_source,
)

SEED_TS = 1_700_000_000


# --------------------------------------------------------------------------- #
# Behavioral: the Rust L5 Load test.
# --------------------------------------------------------------------------- #


def _cargo() -> str:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run the Rust concurrent-read Load test")
    return cargo


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), cwd=REPO_ROOT, check=False, capture_output=True, text=True)


def _run_cargo_test(test_name: str) -> subprocess.CompletedProcess[str]:
    return _run(
        _cargo(), "test", "-p", "atp-data", "--test", "srs_data_017_concurrent_reads",
        test_name, "--", "--exact",
    )


def test_concurrent_readers_never_see_a_torn_or_lost_store() -> None:
    result = _run_cargo_test("srs_data_017_concurrent_readers_never_see_a_torn_or_lost_store")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "1 passed" in combined, combined


def test_a_read_never_blocks_on_a_held_writer_lock() -> None:
    result = _run_cargo_test("srs_data_017_a_read_never_blocks_on_a_held_writer_lock")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "1 passed" in combined, combined


def test_completed_data_survives_a_concurrent_ingestion_across_processes() -> None:
    cargo = _cargo()
    build = _run(
        cargo, "build", "-q", "-p", "atp-data",
        "--bin", "data016_ingest_cli", "--bin", "data007_query_cli",
    )
    assert build.returncode == 0, build.stdout + build.stderr
    ingest_bin = str(REPO_ROOT / "target" / "debug" / "data016_ingest_cli")
    query_bin = str(REPO_ROOT / "target" / "debug" / "data007_query_cli")

    with tempfile.TemporaryDirectory() as tmp:
        seed = _run(ingest_bin, "ingest", "--dir", tmp, "--kind", "daily-equity-bar", "--init")
        assert seed.returncode == 0, seed.stdout + seed.stderr

        errors: list[str] = []
        done = threading.Event()

        def writer() -> None:
            try:
                for i in range(1, 11):
                    res = _run(
                        ingest_bin, "ingest", "--dir", tmp,
                        "--kind", "daily-equity-bar", "--event-ts", str(SEED_TS + i),
                    )
                    if res.returncode != 0:
                        errors.append(f"writer ingest {i}: {res.stdout}{res.stderr}")
            finally:
                done.set()

        def reader() -> None:
            while True:
                q = _run(
                    query_bin, "query", "--dir", tmp,
                    "--symbol", "AAPL", "--resolution", "1d", "--start", "0", "--end", "9999999999",
                )
                if q.returncode != 0:
                    errors.append(f"reader query failed (torn/blocked read): {q.stdout}{q.stderr}")
                else:
                    mc = [ln for ln in q.stdout.splitlines() if ln.startswith("match_count:")]
                    if not mc or int(mc[0].split(":", 1)[1]) < 1:
                        errors.append(f"completed seed lost mid-ingestion: {q.stdout!r}")
                if done.is_set():
                    break

        threads = [threading.Thread(target=writer)] + [
            threading.Thread(target=reader) for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], "; ".join(errors)


# --------------------------------------------------------------------------- #
# Structural: the integrity-critical guards, each shown non-vacuous.
# --------------------------------------------------------------------------- #


def test_query_reader_is_lock_free_and_guard_is_non_vacuous() -> None:
    config = load_config()
    src = read_cli_source(config)
    assert "lock-free reader" in check_read_cli_lock_free(config, src)
    mutated = src.replace(
        "let store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;",
        "let _l = StoreLock::acquire(&dir);\n    let store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;",
        1,
    )
    with pytest.raises(ConcurrentReadCheckError):
        check_read_cli_lock_free(config, mutated)


def test_write_is_atomically_published_and_guard_is_non_vacuous() -> None:
    config = load_config()
    src = store_source(config)
    assert "publishes atomically" in check_atomic_publish(config, src)
    mutated = src.replace("fs::rename(&tmp_path, &final_path)", "fs::copy(&tmp_path, &final_path)", 1)
    with pytest.raises(ConcurrentReadCheckError):
        check_atomic_publish(config, mutated)


def test_read_fails_closed_on_corruption_and_guard_is_non_vacuous() -> None:
    config = load_config()
    src = store_source(config)
    assert "reads fail-closed" in check_fail_closed_read(config, src)
    mutated = src.replace("if checksum(body) != stored_checksum {", "if false {", 1)
    with pytest.raises(ConcurrentReadCheckError):
        check_fail_closed_read(config, mutated)
