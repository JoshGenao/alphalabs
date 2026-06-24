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
* the binding serves ``RAW`` only (``--normalization raw``); the normalization default
  (SPLIT_ADJUSTED) and every other adjusted mode (and an ``OPTION`` asset class) raise
  ``NotImplementedError`` -- split-adjusted is deferred as a strategy-facing default pending
  corporate-action coverage (SRS-DATA-011);
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
    (as the real CLI does)."""
    lines = [
        f"symbol:{symbol}",
        f"resolution:{resolution}",
        f"start:{start}",
        f"end:{end}",
        "kind:any",
        f"normalization:{normalization}",
        f"match_count:{len(records)}",
    ]
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
    # Only the daily ('1d') and minute ('1m') equity-bar datasets exist; richer resolutions need bar
    # consolidation (SRS-SDK-007, deferred) and must fail closed rather than query an unknown kind.
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    with pytest.raises(NotImplementedError):
        _binding(runner).get_bars("AAPL", lookback=1, frequency="5m", normalization=RAW)


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
    assert len(_binding(runner).get_bars("AAPL", lookback=99, frequency="1d", normalization=RAW)) == 2


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


def test_default_normalization_fails_closed() -> None:
    # Omitting normalization uses the Protocol default (SPLIT_ADJUSTED); the binding must FAIL CLOSED
    # rather than silently serve raw bars dressed up as adjusted. Split-adjusted is not a trustworthy
    # strategy-facing default until SRS-DATA-011 corporate-action coverage exists.
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    with pytest.raises(NotImplementedError):
        _binding(runner).get_bars("AAPL", lookback=1, frequency="1d")
    assert runner.calls == []  # refused before any query


@pytest.mark.parametrize(
    "mode",
    [NormalizationMode.SPLIT_ADJUSTED, NormalizationMode.FULLY_ADJUSTED, NormalizationMode.TOTAL_RETURN],
)
def test_adjusted_normalization_modes_raise_not_implemented(mode: NormalizationMode) -> None:
    # The binding serves RAW only. Every adjusted mode fails closed before any query: SPLIT_ADJUSTED
    # pending corporate-action coverage (SRS-DATA-011); FULLY_ADJUSTED / TOTAL_RETURN pending dividends.
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    with pytest.raises(NotImplementedError):
        _binding(runner).get_bars("AAPL", lookback=1, frequency="1d", normalization=mode)
    assert runner.calls == []  # refused before any query


def test_raw_normalization_passes() -> None:
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    bars = _binding(runner).get_bars("AAPL", lookback=1, frequency="1d", normalization=RAW)
    assert len(bars) == 1
    assert _arg(runner.calls[0], "--normalization") == "raw"


def test_option_asset_class_raises_not_implemented() -> None:
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    with pytest.raises(NotImplementedError):
        _binding(runner).get_bars(
            "AAPL", lookback=1, frequency="1d", asset_class=AssetClass.OPTION, normalization=RAW
        )


def test_get_bars_range_is_inclusive_and_pure() -> None:
    records = [(1_700_000_000, _OHLCV), (1_700_086_400, _OHLCV)]
    runner = _FakeRunner(records=records)
    start = datetime(2023, 11, 14, tzinfo=timezone.utc)
    end = datetime(2023, 11, 16, tzinfo=timezone.utc)
    bars = _binding(runner).get_bars_range("AAPL", frequency="1d", start=start, end=end, normalization=RAW)
    (argv,) = runner.calls
    assert _arg(argv, "--start") == str(int(start.timestamp()))
    assert _arg(argv, "--end") == str(int(end.timestamp()))  # inclusive: no -1
    assert len(bars) == 2


def test_get_bars_range_default_normalization_fails_closed() -> None:
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV)])
    start = datetime(2023, 11, 14, tzinfo=timezone.utc)
    end = datetime(2023, 11, 16, tzinfo=timezone.utc)
    with pytest.raises(NotImplementedError):
        _binding(runner).get_bars_range("AAPL", frequency="1d", start=start, end=end)


# --------------------------------------------------------------------------- #
# Truncated / drifted / mislabelled / out-of-range output must fail closed.
# A deterministic clock pins the requested end_ts so the echoed start/end pass, isolating the guard
# under test; bare-record bodies use _rec() so they sit inside the requested [0, _NEG_END] range.
# --------------------------------------------------------------------------- #

_NEG_CLOCK = datetime(2024, 1, 1, tzinfo=timezone.utc)
_NEG_END = int(_NEG_CLOCK.timestamp())  # the end_ts get_bars(default end) will request with _NEG_CLOCK
_T0 = 1_700_000_000  # 2023-11-14, inside [0, _NEG_END]
_T1 = 1_700_086_400  # 2023-11-15, inside [0, _NEG_END]


def _rec(index: int, event_ts: int) -> str:
    return (
        f"record.{index}.event_ts:{event_ts}\nrecord.{index}.field.open:9950\n"
        f"record.{index}.field.high:10075\nrecord.{index}.field.low:9910\n"
        f"record.{index}.field.close:10000\nrecord.{index}.field.volume:100000\n"
    )


def _envelope(
    match_count: int, body: str = "", *, symbol: str = "AAPL", resolution: str = "1d",
    start: int = 0, end: int = _NEG_END, normalization: str = "raw",
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
