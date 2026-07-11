"""SRS-DATA-017 / SyRS SYS-63 / StRS SN-1.26, SN-1.28 — the NAMED Python consumer reads the durable
store via the lock-free path (L7 domain, solo-runnable de-risk companion).

The AC names "strategy containers, backtests, factor jobs, and notebooks" as the readers. The faithful
read-DURING-a-held-write Load test through that named consumer requires a genuinely-held cross-process
lock window, so it lives in the ``ATP_RUN_INTEGRATION``-gated
``tests/integration/test_data017_named_consumer_concurrent_read.py``. This domain test exercises the
EXACT reader path that gated test drives — :class:`StoreBackedHistoricalData` (the ``SRS-DATA-007``
``HistoricalData`` binding) over the lock-free ``data007_query_cli`` read path — but against a
statically-published store (no held-writer window needed), so it runs in the default suite and proves
the named consumer's read path is sound:

  1. the consumer reads the previously-ingested seed (a strategy/backtest/notebook sees completed
     data), returning a real :class:`Bar` (never a fabricated one), and
  2. after a subsequent ingest COMMITS, the same consumer reflects the new record — i.e. it reads the
     durable published store, not a stale snapshot.

Because the reader here is byte-for-byte the reader the gated held-writer Load test uses, a green run
here means the only thing the operator's ``ATP_RUN_INTEGRATION=1`` run adds is the held-lock
concurrency window, not the consumer wiring.

Layer note (L7, not L5): like the sibling ``tests/domain/test_data017_concurrent_reads.py``, this
domain test shells the cargo-built store CLIs against a HERMETIC per-test temp dir -- no held-writer
window, no shared/fixed resource, no container, no fixed port -- so it is deterministic and safe for
the default parallel suite. The ``ATP_RUN_INTEGRATION`` gate is reserved for the cross-process
HELD-writer Load test (the AC's actual concurrency proof), which stays ``pytest.mark.integration`` in
``tests/integration/test_data017_named_consumer_concurrent_read.py`` -- so the evidence/gating boundary
is explicit: this file de-risks only the consumer READ path, never claims the held-writer proof.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = REPO_ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_strategy import Bar, NormalizationMode  # noqa: E402
from atp_strategy.store_history import StoreBackedHistoricalData  # noqa: E402

pytestmark = pytest.mark.domain

SEED_TS = 1_700_000_000
# A distinct, later event_ts so the second ingest is a NEW committed record.
LATER_TS = 1_700_000_500
READ_TIMEOUT = 30.0

# Deterministic inclusive range (no clock read) bracketing both event_ts values.
RANGE_START = datetime.fromtimestamp(SEED_TS - 10, tz=timezone.utc)
RANGE_END = datetime.fromtimestamp(LATER_TS + 10, tz=timezone.utc)


def _cargo() -> str:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot build the store CLIs for the consumer read")
    return cargo


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), cwd=REPO_ROOT, check=False, capture_output=True, text=True)


def _build() -> tuple[str, str]:
    build = _run(
        _cargo(),
        "build",
        "-q",
        "-p",
        "atp-data",
        "--bin",
        "data016_ingest_cli",
        "--bin",
        "data007_query_cli",
    )
    assert build.returncode == 0, build.stdout + build.stderr
    debug = REPO_ROOT / "target" / "debug"
    return str(debug / "data016_ingest_cli"), str(debug / "data007_query_cli")


def _read_aapl(consumer: StoreBackedHistoricalData) -> list[Bar]:
    return consumer.get_bars_range(
        "AAPL",
        frequency="1d",
        start=RANGE_START,
        end=RANGE_END,
        normalization=NormalizationMode.RAW,
    )


def test_named_consumer_reads_committed_data_and_reflects_a_new_commit() -> None:
    ingest_bin, query_bin = _build()

    with tempfile.TemporaryDirectory() as tmp:
        seed = _run(ingest_bin, "ingest", "--dir", tmp, "--kind", "daily-equity-bar", "--init")
        assert seed.returncode == 0, seed.stdout + seed.stderr

        # The named consumer the AC calls out (SRS-DATA-007 HistoricalData binding).
        consumer = StoreBackedHistoricalData(
            store_dir=tmp, query_binary=query_bin, timeout=READ_TIMEOUT
        )

        # (1) It reads the previously-ingested seed as a real Bar (never fabricated).
        seeded = _read_aapl(consumer)
        assert len(seeded) == 1, f"consumer did not read the committed seed: {seeded!r}"
        bar = seeded[0]
        assert isinstance(bar, Bar)
        assert bar.symbol == "AAPL"
        assert bar.timestamp, "the consumer returned a Bar with an empty timestamp"
        assert bar.open > 0 and bar.high > 0 and bar.low > 0 and bar.close > 0, (
            f"non-positive OHLC from the consumer (fabricated/mis-parsed bar?): {bar!r}"
        )
        assert bar.high >= bar.low, f"high < low from the consumer: {bar!r}"

        # (2) After a subsequent ingest COMMITS, the same consumer reflects the durable published store
        #     (reads the committed catalog, not a stale snapshot).
        more = _run(
            ingest_bin,
            "ingest",
            "--dir",
            tmp,
            "--kind",
            "daily-equity-bar",
            "--event-ts",
            str(LATER_TS),
        )
        assert more.returncode == 0, more.stdout + more.stderr

        after = _read_aapl(consumer)
        assert len(after) == 2, f"consumer did not reflect the newly committed record: {after!r}"
        assert all(b.symbol == "AAPL" for b in after)
        assert [b.timestamp for b in after] == sorted(b.timestamp for b in after), (
            f"consumer returned bars out of chronological order: {[b.timestamp for b in after]!r}"
        )


def test_gated_named_consumer_load_test_is_present_and_non_vacuous() -> None:
    """Guard the OPERATOR-GATED held-writer Load test from silent gutting.

    ``tests/integration/test_data017_named_consumer_concurrent_read.py`` is the load-bearing
    ``SRS-DATA-017`` close, but it is ``ATP_RUN_INTEGRATION``-gated and does NOT run in the default
    suite — so nothing else in CI would catch it being reduced to a vacuous pass. This solo test
    asserts it still drives the named consumer (:class:`StoreBackedHistoricalData`) through a genuinely
    HELD writer window (the ``data017_lock_holder`` ready/release fixture), enforces non-blocking via
    the structured :class:`StoreQueryError`, and asserts snapshot isolation — the exact invariants that
    make it a real Load test rather than a smoke test.
    """
    gated = REPO_ROOT / "tests" / "integration" / "test_data017_named_consumer_concurrent_read.py"
    assert gated.exists(), "the named-consumer SRS-DATA-017 Load test is missing"
    src = gated.read_text(encoding="utf-8")
    required = {
        "StoreBackedHistoricalData": "the named consumer must be the reader",
        "get_bars_range": "reads via the deterministic named-consumer primitive",
        "data017_lock_holder": "drives a genuinely-held cross-process writer window",
        "ready_file": "waits for the KNOWN lock-held window (not 'the subprocess is alive')",
        "release_file": "the window is bounded/known, released deterministically",
        "StoreQueryError": "a blocked read surfaces the structured error and FAILS (never hangs)",
        "snapshot isolation": "asserts the uncommitted write is invisible",
        "pytest.mark.integration": "stays ATP_RUN_INTEGRATION-gated (operator-run)",
    }
    missing = [f"{tok} ({why})" for tok, why in required.items() if tok not in src]
    assert not missing, "the named-consumer Load test was gutted; missing: " + "; ".join(missing)
