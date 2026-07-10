"""SRS-DATA-011 corporate actions — L5 integration test (the acceptance's scenario demonstration).

Gated by ``ATP_RUN_INTEGRATION=1`` (see tests/conftest.py). The acceptance: "Splits, reverse splits,
dividends, delistings, mergers, and symbol changes are reflected in historical records so that
backtests spanning corporate-action dates produce correct P&L calculations under the selected
normalization mode." Each test below walks ONE action type end to end with the real operator CLIs
(``data016_ingest_cli`` fixture ingestion → ``data011_coverage_cli assert-coverage`` →
``data007_query_cli``) over a fresh store, then computes a tiny buy-before / evaluate-after P&L from
the PARSED output and asserts the exact integers — the "backtest spanning the corporate-action date"
the acceptance names, driven purely through the public surfaces.

Reverse splits share the forward-split code path (a 1-for-N ``SplitEvent``; the deterministic fixture
batch ships a 4-for-1, and the reverse direction is pinned by the crate unit + 5000-case property
tests over ``normalization.rs``), so the CLI walk here demonstrates the split leg once and the
in-crate tests carry the reverse-ratio evidence.

Fixture values (crates/atp-data/src/store.rs): daily bars AAPL close 10000 / volume 100000 and MSFT
close 10100 / volume 101000 per event_ts; split = AAPL 4-for-1; dividend = AAPL 100 minor ($1.00);
delisting = MSFT; merger = MSFT -> AAPL at 1-for-2 plus 500 minor cash per share; symbol change =
AAPL -> AAPLN.
"""

from __future__ import annotations

import shutil
import subprocess
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


def _ingest(ingest_bin: Path, tmp: str, kind: str, event_ts: int, *, init: bool = False) -> None:
    args = [str(ingest_bin), "ingest", "--dir", tmp, "--kind", kind, "--event-ts", str(event_ts)]
    if init:
        args.append("--init")
    done = _run(*args)
    assert done.returncode == 0, f"ingest {kind}@{event_ts}: {done.stdout}{done.stderr}"


def _assert_coverage(coverage_bin: Path, tmp: str, symbol: str, through: int) -> None:
    done = _run(
        str(coverage_bin),
        "assert-coverage",
        "--dir",
        tmp,
        "--symbol",
        symbol,
        "--through",
        str(through),
    )
    assert done.returncode == 0, done.stdout + done.stderr


def _query(
    query_bin: Path, tmp: str, symbol: str, start: int, end: int, normalization: str
) -> subprocess.CompletedProcess[str]:
    return _run(
        str(query_bin),
        "query",
        "--dir",
        tmp,
        "--symbol",
        symbol,
        "--resolution",
        "1d",
        "--start",
        str(start),
        "--end",
        str(end),
        "--kind",
        "daily-equity-bar",
        "--normalization",
        normalization,
    )


def _parse(stdout: str) -> tuple[dict[str, str], list[dict[str, int]], list[dict[str, str]]]:
    """Parse the CLI output into (envelope, records[{event_ts,+fields}], events[{kind,...}])."""
    envelope: dict[str, str] = {}
    records: dict[int, dict[str, int]] = {}
    events: dict[int, dict[str, str]] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        if key.startswith("record."):
            parts = key.split(".")
            index = int(parts[1])
            record = records.setdefault(index, {})
            if parts[2] == "event_ts":
                record["event_ts"] = int(value)
            elif parts[2] == "field":
                record[parts[3]] = int(value)
        elif key.startswith("event."):
            parts = key.split(".", 2)
            events.setdefault(int(parts[1]), {})[parts[2]] = value
        else:
            envelope[key] = value
    ordered_records = [records[i] for i in sorted(records)]
    ordered_events = [events[i] for i in sorted(events)]
    return envelope, ordered_records, ordered_events


def test_split_backtest_sees_a_continuous_adjusted_series() -> None:
    # A backtest holding AAPL across the 4-for-1 split @200: the pre-split bar is re-quoted onto the
    # post-split basis, so buy@100 -> hold through the split -> evaluate@300 computes P&L on ONE
    # comparable basis (no phantom 4x loss on the split date).
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, coverage_bin, query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        _ingest(ingest_bin, tmp, "daily-equity-bar", 100, init=True)
        _ingest(ingest_bin, tmp, "corporate-action-split", 200)
        _ingest(ingest_bin, tmp, "daily-equity-bar", 300)
        _assert_coverage(coverage_bin, tmp, "AAPL", 300)
        result = _query(query_bin, tmp, "AAPL", 0, 300, "split-adjusted")
        assert result.returncode == 0, result.stderr
        envelope, records, _events = _parse(result.stdout)
        assert envelope["normalization"] == "split-adjusted"
        assert [r["event_ts"] for r in records] == [100, 300]
        # Pre-split bar re-quoted: close 10000/4 = 2500, volume 100000*4 = 400000; the post-split bar
        # (already on the new basis) is verbatim.
        assert records[0]["close"] == 2500
        assert records[0]["volume"] == 400000
        assert records[1]["close"] == 10000
        # P&L on the comparable basis: buy 4 (post-split-equivalent) shares at the adjusted 2500,
        # evaluate at 10000 -> +7500 per share; on the RAW series the same trade would look flat
        # across the split despite the 4x share multiplication.
        pnl_per_share_minor = records[1]["close"] - records[0]["close"]
        assert pnl_per_share_minor == 7500


def test_dividend_backtest_pnl_under_the_fully_adjusted_mode() -> None:
    # A $1.00 dividend ex @150 then a 4-for-1 split @200. Under the SYS-29 fully-adjusted mode the
    # pre-ex bar is back-adjusted by (10000-100)/10000 AND the split: 10000·(1·9900)/(4·10000) = 2475.
    # Under split-adjusted the dividend is (by mode semantics) NOT in the prices: 2500. The mode
    # difference (25 minor per adjusted share) IS the dividend leg a total-return-aware backtest sees.
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, coverage_bin, query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        _ingest(ingest_bin, tmp, "daily-equity-bar", 100, init=True)
        _ingest(ingest_bin, tmp, "corporate-action-dividend", 150)
        _ingest(ingest_bin, tmp, "corporate-action-split", 200)
        _assert_coverage(coverage_bin, tmp, "AAPL", 200)
        fully = _query(query_bin, tmp, "AAPL", 0, 100, "fully-adjusted")
        assert fully.returncode == 0, fully.stderr
        envelope, records, _events = _parse(fully.stdout)
        assert envelope["normalization"] == "fully-adjusted"
        assert envelope["coverage_through"] == "200"
        assert records[0]["close"] == 2475
        # Volume takes the SPLIT factor only: a dividend never changes a share count.
        assert records[0]["volume"] == 400000
        split_only = _query(query_bin, tmp, "AAPL", 0, 100, "split-adjusted")
        assert split_only.returncode == 0, split_only.stderr
        _, split_records, _ = _parse(split_only.stdout)
        assert split_records[0]["close"] == 2500
        # The per-share dividend leg on the split-adjusted basis: 100 minor / 4 = 25.
        assert split_records[0]["close"] - records[0]["close"] == 25


def test_total_return_backtest_reinvests_the_dividend_forward() -> None:
    # SRS-DATA-012 total-return: the SAME $1.00 dividend ex @150 the fully-adjusted test uses, but the
    # total-return mode REINVESTS it forward (the growth-of-one-share index) instead of back-adjusting.
    # A price-return (raw) backtest buying AAPL @100 (close 10000) and evaluating @300 (close 10000)
    # sees FLAT P&L across the dividend; the total-return series captures the reinvested dividend: the
    # post-ex bar is grossed UP by reference/(reference-amount) = 10000/9900, so @300 reads 10101.
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, coverage_bin, query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        _ingest(ingest_bin, tmp, "daily-equity-bar", 100, init=True)
        _ingest(ingest_bin, tmp, "corporate-action-dividend", 150)
        _ingest(ingest_bin, tmp, "daily-equity-bar", 300)
        _assert_coverage(coverage_bin, tmp, "AAPL", 300)

        total = _query(query_bin, tmp, "AAPL", 0, 300, "total-return")
        assert total.returncode == 0, total.stderr
        envelope, tr_records, _events = _parse(total.stdout)
        assert envelope["normalization"] == "total-return"
        assert envelope["coverage_through"] == "300"
        assert [r["event_ts"] for r in tr_records] == [100, 300]
        # Pre-ex bar @100 stays raw (the dividend is not yet ex); post-ex bar @300 reinvested UP.
        assert tr_records[0]["close"] == 10000
        assert tr_records[1]["close"] == 10101  # 10000 * 10000/9900, round-half-even
        # Volume is never dividend-scaled (no split here either): verbatim.
        assert tr_records[1]["volume"] == 100000
        # The total-return P&L across the dividend is POSITIVE (the reinvested dividend), whereas the
        # raw price-return series is flat over the same window -- the mode difference IS the dividend.
        tr_pnl = tr_records[1]["close"] - tr_records[0]["close"]
        assert tr_pnl == 101
        raw = _query(query_bin, tmp, "AAPL", 0, 300, "raw")
        _, raw_records, _ = _parse(raw.stdout)
        assert raw_records[1]["close"] - raw_records[0]["close"] == 0

        # Total-return is DISTINCT from fully-adjusted (which anchors the latest bar at raw and
        # back-adjusts the pre-ex bar DOWN to 9900): different levels, same reinvestment content.
        fully = _query(query_bin, tmp, "AAPL", 0, 300, "fully-adjusted")
        _, fa_records, _ = _parse(fully.stdout)
        assert fa_records[0]["close"] == 9900
        assert fa_records[1]["close"] == 10000
        assert tr_records[1]["close"] != fa_records[1]["close"]


def test_delisting_backtest_marks_the_position_final() -> None:
    # MSFT delists @150. A backtest holding MSFT through [0, 300] gets the full (terminated) series
    # plus the structural delisting event: it marks the position final at the last served close
    # instead of treating the silence after 150 as missing data.
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, coverage_bin, query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        _ingest(ingest_bin, tmp, "daily-equity-bar", 100, init=True)
        _ingest(ingest_bin, tmp, "corporate-action-delisting", 150)
        _assert_coverage(coverage_bin, tmp, "MSFT", 300)
        result = _query(query_bin, tmp, "MSFT", 0, 300, "split-adjusted")
        assert result.returncode == 0, result.stderr
        envelope, records, events = _parse(result.stdout)
        assert envelope["event_count"] == "1"
        assert events == [
            {"kind": "delisting", "symbol": "MSFT", "successor": "-", "effective_ts": "150"}
        ]
        # The position is marked final at the last close before the delisting instant.
        final_marks = [r["close"] for r in records if r["event_ts"] <= 150]
        assert final_marks[-1] == 10100
        # P&L for 10 shares bought at that close and marked final at it: 0 (the honest terminal mark);
        # the delisting event — not a missing-data guess — is what authorizes closing the position.
        assert 10 * (final_marks[-1] - records[0]["close"]) == 0


def test_merger_backtest_converts_at_the_surfaced_terms() -> None:
    # MSFT merges into AAPL @180 at 1-for-2 plus 500 minor cash per MSFT share. The gated read
    # surfaces the exact terms; a backtest holding 10 MSFT converts to 5 AAPL + 5000 minor cash and
    # computes P&L across the date from the surfaced numbers.
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, coverage_bin, query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        _ingest(ingest_bin, tmp, "daily-equity-bar", 100, init=True)
        _ingest(ingest_bin, tmp, "corporate-action-merger", 180)
        _assert_coverage(coverage_bin, tmp, "MSFT", 300)
        _assert_coverage(coverage_bin, tmp, "AAPL", 300)
        result = _query(query_bin, tmp, "MSFT", 0, 300, "split-adjusted")
        assert result.returncode == 0, result.stderr
        _envelope, records, events = _parse(result.stdout)
        assert events == [
            {
                "kind": "merger",
                "symbol": "MSFT",
                "successor": "AAPL",
                "effective_ts": "180",
                "numerator": "1",
                "denominator": "2",
                "cash_per_share_minor": "500",
            }
        ]
        # Convert 10 MSFT (bought at close 10100) at the surfaced terms: 10·1/2 = 5 AAPL + 10·500 cash.
        msft_shares = 10
        cost_minor = msft_shares * records[0]["close"]
        aapl_shares = msft_shares * int(events[0]["numerator"]) // int(events[0]["denominator"])
        cash_minor = msft_shares * int(events[0]["cash_per_share_minor"])
        assert (aapl_shares, cash_minor) == (5, 5000)
        # Mark the received AAPL at ITS gated close (the successor's series is queried separately).
        aapl = _query(query_bin, tmp, "AAPL", 0, 300, "split-adjusted")
        assert aapl.returncode == 0, aapl.stderr
        _, aapl_records, _ = _parse(aapl.stdout)
        proceeds_minor = aapl_shares * aapl_records[0]["close"] + cash_minor
        # 5·10000 + 5000 - 10·10100 = -46000: an exact, reproducible P&L across the merger date —
        # computable ONLY because the terms are surfaced (the series alone just stops at the merger).
        assert proceeds_minor - cost_minor == -46000


def test_symbol_change_backtest_spans_the_rename() -> None:
    # AAPL renamed to AAPLN @300. A backtest querying the CURRENT symbol (AAPLN) sees the pre-rename
    # AAPL bar relabeled into one continuous series plus the symbol-change event; the same query as of
    # a date BEFORE the rename (point-in-time discipline) is owned by the crate's _as_of tests.
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, coverage_bin, query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        _ingest(ingest_bin, tmp, "daily-equity-bar", 100, init=True)
        _ingest(ingest_bin, tmp, "corporate-action-symbol-change", 300)
        _assert_coverage(coverage_bin, tmp, "AAPLN", 400)
        result = _query(query_bin, tmp, "AAPLN", 0, 400, "split-adjusted")
        assert result.returncode == 0, result.stderr
        envelope, records, events = _parse(result.stdout)
        assert envelope["symbol"] == "AAPLN"
        # The predecessor's bar is served under the queried symbol, values verbatim — a P&L series
        # with no artificial break at the rename.
        assert [r["event_ts"] for r in records] == [100]
        assert records[0]["close"] == 10000
        assert events == [
            {
                "kind": "symbol-change",
                "symbol": "AAPL",
                "successor": "AAPLN",
                "effective_ts": "300",
            }
        ]
        # Coverage is keyed to the QUERIED symbol: the same read without AAPLN coverage fails closed.
        bare = _query(query_bin, tmp, "AAPL", 0, 400, "split-adjusted")
        assert bare.returncode != 0
        assert "SRS-DATA-011" in bare.stderr
