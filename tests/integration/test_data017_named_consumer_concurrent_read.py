"""SRS-DATA-017 (support concurrent reads during ingestion writes) — L5 cross-process Load test
driven through the **named Python consumer** the acceptance criterion calls out.

Gated by ``ATP_RUN_INTEGRATION=1`` (see tests/conftest.py). The AC names "strategy containers,
backtests, factor jobs, and notebooks" as the readers. The sibling ``test_data017_concurrent_reads.py``
proves the property with the ``data007_query_cli`` / ``inspect`` operator-CLI reader PROCESSES — an
*analogy* for the in-process consumer path. This test closes that gap by using the ACTUAL named
consumer a strategy / backtest / factor job / notebook uses: :class:`StoreBackedHistoricalData`, the
``SRS-DATA-007`` ``HistoricalData`` binding (``python/atp_strategy/store_history.py``, which itself
names "the concurrent-read-DURING-write Load test for this *named Python consumer*" as the deferred
``SRS-DATA-017`` close). It reads via the consumer's deterministic, no-clock
:meth:`StoreBackedHistoricalData.get_bars_range` while a writer PROCESS genuinely holds the
single-writer ``StoreLock`` mid load-modify-save.

The lock-held window is REAL and KNOWN, not inferred from "the subprocess is alive": the
``data017_lock_holder`` example fixture acquires the lock, does the in-memory modify, signals a
ready-file, and only commits + releases when a release-file appears (bounded so a forgotten release
can never hang CI). While it holds the lock, the named consumer must (a) COMPLETE within its per-query
timeout — proving reads are non-blocking even though a writer holds the lock (a blocked read surfaces
:class:`StoreQueryError`, backed by ``subprocess.TimeoutExpired``, and FAILS the test rather than
hanging), (b) still see the previously-ingested "completed" seed (no corruption / no lost completed
data), and (c) NOT see the holder's uncommitted write (snapshot isolation). After release the
committed write becomes visible to the SAME consumer and the catalog is intact.

(The always-run Rust integration test ``srs_data_017_concurrent_reads`` carries the deterministic
in-process overlap, non-blocking, and two-writer no-loss proofs; this adds the AC-named consumer.)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_strategy import NormalizationMode  # noqa: E402
from atp_strategy.store_history import StoreBackedHistoricalData, StoreQueryError  # noqa: E402

pytestmark = pytest.mark.integration

SEED_TS = 1_700_000_000
# Matches ``HOLDER_EVENT_TS`` in crates/atp-data/examples/data017_lock_holder.rs — the holder's
# uncommitted batch lands at a distinct, later event_ts so it is new keys visible only after commit.
HOLDER_TS = 1_700_000_500
READ_TIMEOUT = 30.0  # a read that BLOCKS on the held lock must raise (never hang), and so FAIL

# A deterministic inclusive range that brackets BOTH the seed and the holder batch, so the same read
# returns just the seed while the write is held (1 AAPL daily bar) and seed + holder after release (2).
RANGE_START = datetime.fromtimestamp(SEED_TS - 10, tz=timezone.utc)
RANGE_END = datetime.fromtimestamp(HOLDER_TS + 10, tz=timezone.utc)


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


def _read_aapl(consumer: StoreBackedHistoricalData) -> list:
    """The named-consumer read: the deterministic, no-clock inclusive-range primitive a backtest /
    factor job calls. RAW so the values are served verbatim (the un-covered fixture seed would trip the
    coverage-gated adjusted modes)."""
    return consumer.get_bars_range(
        "AAPL",
        frequency="1d",
        start=RANGE_START,
        end=RANGE_END,
        normalization=NormalizationMode.RAW,
    )


def test_named_python_consumer_is_non_blocking_and_uncorrupted_during_a_held_write_lock() -> None:
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, query_bin, holder_bin = _build(cargo)

    with tempfile.TemporaryDirectory() as tmp:
        # Seed the previously-ingested ("completed") data the consumer must always observe.
        seed = _run(ingest_bin, "ingest", "--dir", tmp, "--kind", "daily-equity-bar", "--init")
        assert seed.returncode == 0, seed.stdout + seed.stderr

        # The actual named consumer a strategy container / backtest / factor job / notebook uses.
        consumer = StoreBackedHistoricalData(
            store_dir=tmp, query_binary=query_bin, timeout=READ_TIMEOUT
        )

        # Baseline: with no writer holding the lock, the named consumer reads the seed (1 AAPL bar).
        pre = _read_aapl(consumer)
        assert len(pre) == 1, f"seed not readable by the named consumer pre-write: {pre!r}"
        assert pre[0].symbol == "AAPL"

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

            # The lock is now genuinely HELD and a write is in progress (uncommitted). The named Python
            # consumer must (a) COMPLETE within its timeout — a block raises StoreQueryError (backed by
            # TimeoutExpired) and FAILS here, never hangs; (b) still see the seed (completed data,
            # uncorrupted); (c) NOT see the holder's uncommitted write (snapshot isolation: exactly the
            # 1 seed AAPL bar, never the holder's 2nd bar).
            for _ in range(6):
                assert not release_file.exists(), "released too early — window invariant broken"
                try:
                    bars = _read_aapl(consumer)
                except StoreQueryError as exc:
                    pytest.fail(f"the named consumer blocked/failed on a held writer lock: {exc}")
                assert len(bars) == 1, (
                    "the named consumer saw the holder's UNCOMMITTED write (snapshot isolation broken) "
                    f"or lost the completed seed: {[b.timestamp for b in bars]!r}"
                )
                assert bars[0].symbol == "AAPL"

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

        # After commit, the holder's batch is visible to the SAME named consumer (seed + holder = 2
        # AAPL daily bars) and the read is still uncorrupted.
        post = _read_aapl(consumer)
        assert len(post) == 2, (
            f"expected seed + committed holder bar after release; got {[b.timestamp for b in post]!r}"
        )
        assert all(b.symbol == "AAPL" for b in post)
