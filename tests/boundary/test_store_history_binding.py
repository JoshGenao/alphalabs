"""SRS-DATA-007 close — L4 boundary test for the Python store-history binding.

Exercises ``atp_strategy.store_history.StoreBackedHistoricalData`` over a FAKE subprocess runner
(no cargo needed) so the CLI-arg construction, lookback↔range mapping, money-units conversion,
normalization honesty, timeout handling, envelope validation, and source-neutral parsing are all
pinned at the boundary:

* an empty match (``match_count:0``) is a returned ``[]``, never an error;
* a non-zero CLI exit raises ``StoreQueryError`` carrying stderr;
* the CLI is invoked with a LIST argv (no shell string) under a bounded timeout;
* a wedged CLI (``subprocess.TimeoutExpired``) is mapped to ``StoreQueryError``, never a hang;
* ``lookback`` returns the LAST N of the ascending result; ``lookback=0`` returns ``[]`` with no call;
* ``end`` is treated as EXCLUSIVE (``--end == int(end) - 1``); ``end=None`` uses the injected clock;
* OHLC minor units are scaled by 100 (cents); ``volume`` is a raw, UNSCALED int count;
* ``event_ts`` becomes a UTC ISO-8601 timestamp;
* the binding serves ``RAW`` (``--normalization raw``) and the gated ``SPLIT_ADJUSTED`` (the Protocol
  default, ``--normalization split-adjusted``), ``FULLY_ADJUSTED``, and ``TOTAL_RETURN`` (SRS-DATA-012):
  an adjusted response must carry the ``coverage_through`` frontier (gate-integrity) and an uncovered
  query maps to ``CoverageNotProvenError`` (naming SRS-DATA-011); an ``OPTION`` asset class (or an
  unmapped mode) raises ``NotImplementedError``;
* the CLI-echoed ``normalization`` mode must match the request, else fail closed (a stale binary that
  ignores the flag cannot return raw values labelled as adjusted);
* a truncated / drifted output (``match_count`` mismatch, missing index, malformed integer) fails closed;
* the CLI-echoed ``symbol`` / ``resolution`` must match the request, else fail closed (no relabelling);
* a missing cargo binary fails closed with ``FileNotFoundError`` through the default runner.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_strategy import AssetClass, Bar, NormalizationMode  # noqa: E402
from atp_strategy.store_history import (  # noqa: E402
    CoverageNotProvenError,
    StoreBackedHistoricalData,
    StoreQueryError,
)

pytestmark = pytest.mark.boundary

RAW = NormalizationMode.RAW
_OHLCV = {"open": 9950, "high": 10075, "low": 9910, "close": 10000, "volume": 100000}


def _render(
    symbol: str,
    resolution: str,
    start: str,
    end: str,
    records: list[tuple[int, dict[str, int]]],
    normalization: str = "raw",
) -> str:
    """Render data007_query_cli output, echoing the queried symbol/resolution/start/end/normalization
    (as the real CLI does). For the gate-served adjusted modes (split-adjusted / fully-adjusted /
    total-return), the real CLI also echoes the proven coverage frontier ``coverage_through`` (always
    >= end); the fake sets it to ``end`` (D == end, the inclusive boundary the gate guarantees) so a
    fake adjusted response is gate-valid by construction."""
    lines = [
        f"symbol:{symbol}",
        f"resolution:{resolution}",
        f"start:{start}",
        f"end:{end}",
        "kind:any",
        f"normalization:{normalization}",
    ]
    if normalization in ("split-adjusted", "fully-adjusted", "total-return"):
        lines.append(f"coverage_through:{end}")
    lines.append(f"match_count:{len(records)}")
    for i, (event_ts, fields) in enumerate(records):
        lines.append(f"record.{i}.event_ts:{event_ts}")
        lines.append(f"record.{i}.option_contract:-")
        for name, value in fields.items():
            lines.append(f"record.{i}.field.{name}:{value}")
    return "\n".join(lines) + "\n"


class _FakeRunner:
    """A fake data007_query_cli. In records-mode it echoes the requested symbol/resolution (like the
    real CLI); ``stdout=`` overrides with a verbatim response for drift/corruption tests."""

    def __init__(
        self,
        *,
        records: list[tuple[int, dict[str, int]]] | None = None,
        stdout: str | None = None,
        returncode: int = 0,
        stderr: str = "",
    ) -> None:
        self._records = records
        self._override = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))  # a copy, so the test can assert it is a genuine list
        self.timeouts.append(timeout)
        if self._override is not None:
            out = self._override
        elif self.returncode != 0:
            out = ""
        else:
            symbol = argv[argv.index("--symbol") + 1]
            resolution = argv[argv.index("--resolution") + 1]
            start = argv[argv.index("--start") + 1]
            end = argv[argv.index("--end") + 1]
            normalization = argv[argv.index("--normalization") + 1]
            out = _render(symbol, resolution, start, end, self._records or [], normalization)
        return subprocess.CompletedProcess(argv, self.returncode, out, self.stderr)


def _binding(runner: _FakeRunner, *, clock: datetime | None = None, timeout: float = 30.0):
    return StoreBackedHistoricalData(
        store_dir="/tmp/does-not-matter",
        query_binary="/tmp/fake-data007_query_cli",
        runner=runner,
        clock=(lambda: clock) if clock is not None else None,
        timeout=timeout,
    )


def _arg(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_empty_match_returns_empty_list_not_error() -> None:
    runner = _FakeRunner(records=[])
    assert _binding(runner).get_bars("AAPL", lookback=5, frequency="1d", normalization=RAW) == []


def test_nonzero_exit_raises_store_query_error() -> None:
    runner = _FakeRunner(returncode=1, stderr="store directory missing")
    with pytest.raises(StoreQueryError) as exc:
        _binding(runner).get_bars("AAPL", lookback=5, frequency="1d", normalization=RAW)
    assert "store directory missing" in str(exc.value)


def test_argv_is_a_list_with_no_provider_flag_and_a_timeout() -> None:
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    _binding(runner, timeout=12.0).get_bars("AAPL", lookback=1, frequency="1d", normalization=RAW)
    (argv,) = runner.calls
    assert isinstance(argv, list)
    assert argv[1] == "query"
    assert _arg(argv, "--symbol") == "AAPL"
    assert _arg(argv, "--resolution") == "1d"
    assert not any(a in ("--provider", "--source", "--vendor", "--feed") for a in argv)
    assert runner.timeouts == [12.0]  # the per-query budget is handed to the runner


def test_equity_query_narrows_to_the_equity_bar_kind() -> None:
    # The query passes the vendor-neutral --kind so a non-bar record cannot poison an OHLCV read.
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    _binding(runner).get_bars("AAPL", lookback=1, frequency="1d", normalization=RAW)
    assert _arg(runner.calls[0], "--kind") == "daily-equity-bar"
    runner2 = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    _binding(runner2).get_bars("AAPL", lookback=1, frequency="1m", normalization=RAW)
    assert _arg(runner2.calls[0], "--kind") == "minute-equity-bar"


def test_unsupported_resolution_raises_not_implemented() -> None:
    # '1d'/'1m' are native and 5m/15m/1h are served by consolidating 1m (SRS-SDK-007); any OTHER
    # resolution must still fail closed rather than query an unknown kind. (5m/15m/1h consolidation
    # is pinned in tests/boundary/test_store_history_consolidation.py.)
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    with pytest.raises(NotImplementedError):
        _binding(runner).get_bars("AAPL", lookback=1, frequency="30m", normalization=RAW)


def test_symbol_is_passed_verbatim_as_one_list_element() -> None:
    # Even an injection-shaped symbol is a single argv element (shell=False) — it cannot inject.
    runner = _FakeRunner(records=[])
    _binding(runner).get_bars("AAPL; rm -rf /", lookback=1, frequency="1d", normalization=RAW)
    (argv,) = runner.calls
    assert _arg(argv, "--symbol") == "AAPL; rm -rf /"


def test_timeout_is_mapped_to_store_query_error() -> None:
    def wedged(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, timeout)

    binding = StoreBackedHistoricalData(
        store_dir="/tmp/x", query_binary="/tmp/fake", runner=wedged, timeout=0.5
    )
    with pytest.raises(StoreQueryError) as exc:
        binding.get_bars("AAPL", lookback=1, frequency="1d", normalization=RAW)
    assert "timed out" in str(exc.value)


def test_non_positive_timeout_is_rejected() -> None:
    runner = _FakeRunner(records=[])
    with pytest.raises(ValueError):
        _binding(runner, timeout=0)


def test_lookback_returns_last_n_ascending() -> None:
    records = [(1_700_000_000 + i * 86_400, {**_OHLCV, "close": (i + 1) * 100}) for i in range(5)]
    runner = _FakeRunner(records=records)
    bars = _binding(runner).get_bars("AAPL", lookback=2, frequency="1d", normalization=RAW)
    assert [b.close for b in bars] == [4.0, 5.0]  # last two: close=400, 500 minor -> 4.0, 5.0


def test_lookback_larger_than_available_returns_all() -> None:
    records = [(1_700_000_000, _OHLCV), (1_700_086_400, _OHLCV)]
    runner = _FakeRunner(records=records)
    assert (
        len(_binding(runner).get_bars("AAPL", lookback=99, frequency="1d", normalization=RAW)) == 2
    )


def test_lookback_zero_returns_empty_without_calling_cli() -> None:
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    assert _binding(runner).get_bars("AAPL", lookback=0, frequency="1d", normalization=RAW) == []
    assert runner.calls == []


def test_negative_lookback_raises() -> None:
    runner = _FakeRunner(records=[])
    with pytest.raises(ValueError):
        _binding(runner).get_bars("AAPL", lookback=-1, frequency="1d", normalization=RAW)


def test_end_is_treated_as_exclusive() -> None:
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    end = datetime(2023, 11, 15, 0, 0, 0, tzinfo=timezone.utc)
    _binding(runner).get_bars("AAPL", lookback=1, frequency="1d", end=end, normalization=RAW)
    (argv,) = runner.calls
    assert _arg(argv, "--end") == str(int(end.timestamp()) - 1)
    assert _arg(argv, "--start") == "0"


def test_end_none_uses_injected_clock() -> None:
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    _binding(runner, clock=now).get_bars("AAPL", lookback=1, frequency="1d", normalization=RAW)
    (argv,) = runner.calls
    assert _arg(argv, "--end") == str(int(now.timestamp()))


def test_fractional_second_end_keeps_the_boundary_second() -> None:
    # end=...:00.5 (exclusive) -> a record at ...:00 is strictly before it and must be kept; the
    # inclusive upper bound is ceil(end)-1 (truncation would drop the latest valid second).
    import math

    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    end = datetime(2023, 11, 15, 12, 0, 0, 500_000, tzinfo=timezone.utc)
    _binding(runner).get_bars("AAPL", lookback=1, frequency="1d", end=end, normalization=RAW)
    (argv,) = runner.calls
    assert _arg(argv, "--end") == str(math.ceil(end.timestamp()) - 1)
    # The exact-second case stays exclusive (ceil(ts)-1 == ts-1).
    runner2 = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    exact = datetime(2023, 11, 15, 12, 0, 0, tzinfo=timezone.utc)
    _binding(runner2).get_bars("AAPL", lookback=1, frequency="1d", end=exact, normalization=RAW)
    (argv2,) = runner2.calls
    assert _arg(argv2, "--end") == str(int(exact.timestamp()) - 1)


def test_money_scale_ohlc_scaled_volume_unscaled() -> None:
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    (bar,) = _binding(runner).get_bars("AAPL", lookback=1, frequency="1d", normalization=RAW)
    assert bar.open == 99.5 and bar.high == 100.75 and bar.low == 99.1 and bar.close == 100.0
    assert bar.volume == 100000 and isinstance(bar.volume, int)


def test_event_ts_becomes_utc_iso_timestamp() -> None:
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    (bar,) = _binding(runner).get_bars("AAPL", lookback=1, frequency="1d", normalization=RAW)
    assert bar.timestamp == "2023-11-14T22:13:20+00:00"
    assert bar.symbol == "AAPL"
    assert isinstance(bar, Bar)


def test_missing_required_field_raises_store_query_error() -> None:
    runner = _FakeRunner(records=[(1_700_000_000, {"open": 1, "high": 2, "low": 3})])
    with pytest.raises(StoreQueryError):
        _binding(runner).get_bars("AAPL", lookback=1, frequency="1d", normalization=RAW)


def test_default_normalization_serves_split_adjusted_through_the_gate() -> None:
    # Omitting normalization uses the Protocol default (SPLIT_ADJUSTED); the binding now ISSUES the query
    # with --normalization split-adjusted (routed through the coverage gate), validates the echoed
    # coverage_through frontier, and returns adjusted bars. It is no longer a NotImplementedError.
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    bars = _binding(runner).get_bars("AAPL", lookback=1, frequency="1d")
    assert len(bars) == 1
    assert _arg(runner.calls[0], "--normalization") == "split-adjusted"


def test_total_return_passes_and_routes_through_the_gate() -> None:
    # TOTAL_RETURN (splits AND reinvested dividends, SRS-DATA-012) is served through the SAME coverage
    # gate as SPLIT_ADJUSTED / FULLY_ADJUSTED: the binding issues --normalization total-return and the
    # gate-integrity validation (the echoed coverage_through) applies to it identically. Only the LIVE
    # per-subscription selection remains deferred (SRS-DATA-012 remainder, SRS-MD-001).
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    bars = _binding(runner).get_bars(
        "AAPL", lookback=1, frequency="1d", normalization=NormalizationMode.TOTAL_RETURN
    )
    assert len(bars) == 1
    assert _arg(runner.calls[0], "--normalization") == "total-return"


def test_fully_adjusted_passes_and_routes_through_the_gate() -> None:
    # FULLY_ADJUSTED (splits AND dividends, SYS-29) is served through the same coverage gate as
    # SPLIT_ADJUSTED: the binding issues --normalization fully-adjusted and the gate-integrity
    # validation (the echoed coverage_through) applies to it identically.
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    bars = _binding(runner).get_bars(
        "AAPL", lookback=1, frequency="1d", normalization=NormalizationMode.FULLY_ADJUSTED
    )
    assert len(bars) == 1
    assert _arg(runner.calls[0], "--normalization") == "fully-adjusted"


def test_fully_adjusted_without_coverage_through_fails_closed() -> None:
    # Gate-integrity applies to fully-adjusted exactly like split-adjusted: a fully-adjusted response
    # that omits the coverage_through frontier is un-gated and must fail closed.
    no_frontier = _render(
        "AAPL", "1d", "0", "1700000000", [(1_700_000_000, _OHLCV)], "fully-adjusted"
    )
    no_frontier = no_frontier.replace("coverage_through:1700000000\n", "")
    runner = _FakeRunner(stdout=no_frontier)
    with pytest.raises(StoreQueryError) as exc:
        _binding(runner).get_bars_range(
            "AAPL",
            frequency="1d",
            start=datetime(1970, 1, 1, tzinfo=timezone.utc),
            end=datetime.fromtimestamp(1_700_000_000, tz=timezone.utc),
            normalization=NormalizationMode.FULLY_ADJUSTED,
        )
    assert "coverage_through" in str(exc.value)


def test_raw_normalization_passes() -> None:
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    bars = _binding(runner).get_bars("AAPL", lookback=1, frequency="1d", normalization=RAW)
    assert len(bars) == 1
    assert _arg(runner.calls[0], "--normalization") == "raw"


def test_split_adjusted_passes_and_routes_through_the_gate() -> None:
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    bars = _binding(runner).get_bars(
        "AAPL", lookback=1, frequency="1d", normalization=NormalizationMode.SPLIT_ADJUSTED
    )
    assert len(bars) == 1
    assert _arg(runner.calls[0], "--normalization") == "split-adjusted"


def test_split_adjusted_without_coverage_through_fails_closed() -> None:
    # Gate-integrity: a split-adjusted response that omits the coverage_through frontier is un-gated and
    # must fail closed (a stale/forged CLI that labels output split-adjusted without passing the gate).
    no_frontier = _render(
        "AAPL", "1d", "0", "1700000000", [(1_700_000_000, _OHLCV)], "split-adjusted"
    )
    no_frontier = no_frontier.replace("coverage_through:1700000000\n", "")
    runner = _FakeRunner(stdout=no_frontier)
    with pytest.raises(StoreQueryError) as exc:
        _binding(runner).get_bars_range(
            "AAPL",
            frequency="1d",
            start=datetime(1970, 1, 1, tzinfo=timezone.utc),
            end=datetime.fromtimestamp(1_700_000_000, tz=timezone.utc),
            normalization=NormalizationMode.SPLIT_ADJUSTED,
        )
    assert "coverage_through" in str(exc.value)


def test_split_adjusted_with_short_coverage_through_fails_closed() -> None:
    # Gate-integrity: a coverage_through frontier BELOW the requested end is not proven complete through
    # the query end — fail closed rather than return adjusted output past the advertised frontier.
    end_ts = 1_700_086_400
    short = _render("AAPL", "1d", "0", str(end_ts), [(1_700_000_000, _OHLCV)], "split-adjusted")
    short = short.replace(f"coverage_through:{end_ts}\n", "coverage_through:1700000000\n")
    runner = _FakeRunner(stdout=short)
    with pytest.raises(StoreQueryError) as exc:
        _binding(runner).get_bars_range(
            "AAPL",
            frequency="1d",
            start=datetime(1970, 1, 1, tzinfo=timezone.utc),
            end=datetime.fromtimestamp(end_ts, tz=timezone.utc),
            normalization=NormalizationMode.SPLIT_ADJUSTED,
        )
    assert "coverage_through" in str(exc.value)


def test_raw_with_unexpected_coverage_through_fails_closed() -> None:
    # Gate-integrity (the other direction): a RAW response must NOT carry a coverage_through line.
    raw_with_frontier = _render("AAPL", "1d", "0", "1700000000", [(1_700_000_000, _OHLCV)], "raw")
    raw_with_frontier = raw_with_frontier.replace(
        "normalization:raw\n", "normalization:raw\ncoverage_through:1700000000\n"
    )
    runner = _FakeRunner(stdout=raw_with_frontier)
    with pytest.raises(StoreQueryError) as exc:
        _binding(runner).get_bars_range(
            "AAPL",
            frequency="1d",
            start=datetime(1970, 1, 1, tzinfo=timezone.utc),
            end=datetime.fromtimestamp(1_700_000_000, tz=timezone.utc),
            normalization=RAW,
        )
    assert "coverage_through" in str(exc.value)


def test_uncovered_split_adjusted_exit_maps_to_coverage_not_proven_error() -> None:
    # The gate fails closed (exit non-zero, stderr names SRS-DATA-011) when the symbol is not covered
    # through --end; the binding maps that to CoverageNotProvenError (a StoreQueryError), never raw.
    runner = _FakeRunner(
        returncode=1,
        stderr="data007_query_cli: split-adjusted refused for AAPL: ... (SRS-DATA-011); ...",
    )
    with pytest.raises(CoverageNotProvenError) as exc:
        _binding(runner).get_bars(
            "AAPL", lookback=1, frequency="1d", normalization=NormalizationMode.SPLIT_ADJUSTED
        )
    assert "SRS-DATA-011" in str(exc.value)
    assert isinstance(exc.value, StoreQueryError)


def test_option_asset_class_raises_not_implemented() -> None:
    # This EQUITY binding defers option-chain bar access (owner: real option ingestion SRS-DATA-006 +
    # the binding's equity scope), even for RAW -- so the strategy-facing "options can request raw
    # prices" path is NOT closed by SRS-DATA-012 (a passes:false contributor). Its normalization-MODE
    # half IS done at the data layer (the operator CLI serves raw option-chain records verbatim, and the
    # coverage gate refuses an adjusted read on a non-equity kind so an options query resolves to raw),
    # but an in-process strategy cannot yet get raw option DATA through ctx.history.
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    with pytest.raises(NotImplementedError):
        _binding(runner).get_bars(
            "AAPL", lookback=1, frequency="1d", asset_class=AssetClass.OPTION, normalization=RAW
        )
    assert runner.calls == []  # refused before any query (deferred, never a fabricated option bar)


def test_get_bars_range_is_inclusive_and_pure() -> None:
    records = [(1_700_000_000, _OHLCV), (1_700_086_400, _OHLCV)]
    runner = _FakeRunner(records=records)
    start = datetime(2023, 11, 14, tzinfo=timezone.utc)
    end = datetime(2023, 11, 16, tzinfo=timezone.utc)
    bars = _binding(runner).get_bars_range(
        "AAPL", frequency="1d", start=start, end=end, normalization=RAW
    )
    (argv,) = runner.calls
    assert _arg(argv, "--start") == str(int(start.timestamp()))
    assert _arg(argv, "--end") == str(int(end.timestamp()))  # inclusive: no -1
    assert len(bars) == 2


def test_get_bars_range_default_serves_split_adjusted() -> None:
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    start = datetime(2023, 11, 14, tzinfo=timezone.utc)
    end = datetime(2023, 11, 16, tzinfo=timezone.utc)
    bars = _binding(runner).get_bars_range("AAPL", frequency="1d", start=start, end=end)
    assert len(bars) == 1
    assert _arg(runner.calls[0], "--normalization") == "split-adjusted"


def test_get_bars_range_serves_fully_adjusted_and_total_return_through_the_gate() -> None:
    # Both dividend-aware modes are served through the coverage gate on the range primitive too.
    start = datetime(2023, 11, 14, tzinfo=timezone.utc)
    end = datetime(2023, 11, 16, tzinfo=timezone.utc)
    for mode, label in (
        (NormalizationMode.FULLY_ADJUSTED, "fully-adjusted"),
        (NormalizationMode.TOTAL_RETURN, "total-return"),
    ):
        runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
        bars = _binding(runner).get_bars_range(
            "AAPL",
            frequency="1d",
            start=start,
            end=end,
            normalization=mode,
        )
        assert len(bars) == 1
        assert _arg(runner.calls[0], "--normalization") == label


# --------------------------------------------------------------------------- #
# Truncated / drifted / mislabelled / out-of-range output must fail closed.
# A deterministic clock pins the requested end_ts so the echoed start/end pass, isolating the guard
# under test; bare-record bodies use _rec() so they sit inside the requested [0, _NEG_END] range.
# --------------------------------------------------------------------------- #

_NEG_CLOCK = datetime(2024, 1, 1, tzinfo=timezone.utc)
_NEG_END = int(
    _NEG_CLOCK.timestamp()
)  # the end_ts get_bars(default end) will request with _NEG_CLOCK
_T0 = 1_700_000_000  # 2023-11-14, inside [0, _NEG_END]
_T1 = 1_700_086_400  # 2023-11-15, inside [0, _NEG_END]


def _rec(index: int, event_ts: int) -> str:
    return (
        f"record.{index}.event_ts:{event_ts}\nrecord.{index}.field.open:9950\n"
        f"record.{index}.field.high:10075\nrecord.{index}.field.low:9910\n"
        f"record.{index}.field.close:10000\nrecord.{index}.field.volume:100000\n"
    )


def _envelope(
    match_count: int,
    body: str = "",
    *,
    symbol: str = "AAPL",
    resolution: str = "1d",
    start: int = 0,
    end: int = _NEG_END,
    normalization: str = "raw",
) -> str:
    return (
        f"symbol:{symbol}\nresolution:{resolution}\nstart:{start}\nend:{end}\nkind:any\n"
        f"normalization:{normalization}\nmatch_count:{match_count}\n" + body
    )


def _neg(stdout: str):
    return _binding(_FakeRunner(stdout=stdout), clock=_NEG_CLOCK).get_bars(
        "AAPL", lookback=5, frequency="1d", normalization=RAW
    )


def test_match_count_exceeds_parsed_records_raises() -> None:
    # match_count:3 but only two record groups -> truncated output must fail closed.
    with pytest.raises(StoreQueryError) as exc:
        _neg(_envelope(3, _rec(0, _T0) + _rec(1, _T1)))
    assert "partial history" in str(exc.value)


def test_missing_record_index_raises() -> None:
    # match_count:2 but indexes {0, 5} (a gap) -> does not cover [0, 2) -> fail closed.
    with pytest.raises(StoreQueryError):
        _neg(_envelope(2, _rec(0, _T0) + _rec(5, _T1)))


def test_malformed_integer_value_raises() -> None:
    with pytest.raises(StoreQueryError):
        _neg(_envelope(1, "record.0.event_ts:1700000000\nrecord.0.field.close:not-a-number\n"))


def test_match_count_zero_with_records_raises() -> None:
    # match_count:0 WITH record lines is inconsistent drift, not an empty result -> fail closed.
    with pytest.raises(StoreQueryError):
        _neg(_envelope(0, _rec(0, _T0)))


def test_negative_match_count_raises() -> None:
    with pytest.raises(StoreQueryError):
        _neg(_envelope(-1))


def test_echoed_symbol_mismatch_raises() -> None:
    # The CLI echoes a DIFFERENT symbol than requested (wrong/stale binary) -> must not relabel.
    with pytest.raises(StoreQueryError) as exc:
        _neg(_envelope(1, _rec(0, _T0), symbol="MSFT"))
    assert "relabel" in str(exc.value)


def test_echoed_resolution_mismatch_raises() -> None:
    with pytest.raises(StoreQueryError):
        _neg(_envelope(1, _rec(0, _T0), resolution="1m"))


def test_echoed_range_mismatch_raises() -> None:
    # The CLI echoes a different end than requested (drift) -> fail closed.
    with pytest.raises(StoreQueryError):
        _neg(_envelope(1, _rec(0, _T0), end=_NEG_END + 12345))


def test_missing_echo_header_raises() -> None:
    # No symbol header at all (schema drift) -> echoed symbol is None != request -> fail closed.
    stdout = f"resolution:1d\nstart:0\nend:{_NEG_END}\nkind:any\nmatch_count:1\n" + _rec(0, _T0)
    with pytest.raises(StoreQueryError):
        _neg(stdout)


def test_event_ts_outside_requested_range_raises() -> None:
    # A record dated AFTER the requested end (future data) must fail closed, not leak through.
    with pytest.raises(StoreQueryError) as exc:
        _neg(_envelope(1, _rec(0, _NEG_END + 86_400)))
    assert "out-of-range" in str(exc.value) or "outside" in str(exc.value)


def test_non_ascending_event_ts_raises() -> None:
    # Records returned out of event_ts order (drift) must fail closed -- get_bars takes the last N.
    with pytest.raises(StoreQueryError):
        _neg(_envelope(2, _rec(0, _T1) + _rec(1, _T0)))  # index 0 is LATER than index 1


def test_missing_store_dir_fails_closed() -> None:
    runner = _FakeRunner(records=[])
    with pytest.raises(ValueError):
        StoreBackedHistoricalData(store_dir="   ", runner=runner)


def test_missing_binary_raises_store_query_error_via_default_runner() -> None:
    # No runner injected -> the default runner hits the absent binary; the binding wraps the launch
    # failure (FileNotFoundError) in its one structured StoreQueryError boundary.
    binding = StoreBackedHistoricalData(
        store_dir="/tmp/x", query_binary="/tmp/definitely-not-built-data007_query_cli"
    )
    with pytest.raises(StoreQueryError) as exc:
        binding.get_bars("AAPL", lookback=1, frequency="1d", normalization=RAW)
    assert "could not be launched" in str(exc.value)
    assert isinstance(exc.value, StoreQueryError)
