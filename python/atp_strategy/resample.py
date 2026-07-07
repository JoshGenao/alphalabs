"""Time-based bar consolidation / resampling for the Python Strategy API (``SRS-SDK-007``).

Consolidates minute-resolution OHLCV bars into 5-minute, 15-minute, hourly, and
daily bars *without pre-processed datasets* — the ``SRS-SDK-007`` / SyRS ``SYS-30a`` /
StRS ``SN-1.21`` acceptance criterion (success criterion ``SC-16``).

Two consumption surfaces, ONE bucketing core
---------------------------------------------
* **Batch** — :func:`consolidate_bars` takes an ascending minute series and returns
  the consolidated series. Used by the historical binding
  (:class:`atp_strategy.store_history.StoreBackedHistoricalData` serves ``5m``/``15m``/
  ``1h`` by consolidating stored ``1m``) and by research notebooks.
* **Streaming** — :class:`TimeBarConsolidator` consumes one bar per :meth:`~TimeBarConsolidator.update`
  call and emits a completed higher-period :class:`~atp_strategy.api.Bar` only when a bar
  crosses into a new bucket (the ``SRS-SDK-008`` ``RenkoBuilder`` / ``RangeBarBuilder``
  ``update()`` convention), plus :meth:`~TimeBarConsolidator.flush` for the trailing
  partial bucket. It is the concrete implementation behind the
  :class:`atp_strategy.api.BarConsolidator` Protocol and ``StrategyContext.consolidate``.

Because both surfaces delegate to the same pure ``_bucket_key`` / ``_finalize`` core,
incremental (live) and batch (backtest / warm-up) consolidation produce **byte-identical**
bars — the paper/live parity invariant (``AC-14``), pinned by
``tests/domain/test_consolidation_parity.py``. Consolidation is a pure function of its
input bars: no wall-clock read (the determinism rule from ``scheduler.py``), no
gap-filling, no fabrication.

Bucketing
---------
* **Intraday (``5m`` / ``15m`` / ``1h``):** the bar instant's epoch-seconds floored to
  the period width. US-Eastern's UTC offset is a whole number of hours, so epoch-floored
  buckets align to ET ``:00``/``:05``/``:15``/hour boundaries and match
  ``pandas.DataFrame.resample("5min"/"15min"/"1h")`` on a UTC index. The bucket is
  labelled at its period-start instant in **UTC ISO-8601** (the same idiom
  :func:`atp_strategy.store_history.StoreBackedHistoricalData._build_bar` uses).
* **Daily (``1d``):** grouped by the US-Eastern *calendar date* of the bar instant. A US
  equity regular/extended session lies within one ET date, so this equals session-grouping
  without needing the trading calendar. The bucket is labelled at that ET date, ``00:00`` ET.

Aggregation is the standard OHLCV resample: ``open`` = first, ``high`` = ``max(high)``,
``low`` = ``min(low)``, ``close`` = last, ``volume`` = ``sum``. Only ``volume`` is summed
(integer, exact); OHLC are selected extrema / endpoints (no float summation → no precision
drift). Split adjustment commutes with this (monotonic positive scaling + a sum), so a
consolidated bar built from split-adjusted minutes equals the split-adjusted consolidation.

Fail closed
-----------
An unknown ``period``, a timezone-naive bar timestamp, bars for more than one symbol in a
single call, or a non-monotonic (out-of-order) input timestamp raises :class:`ValueError` —
the consolidator never fabricates or reorders bars.
"""

from __future__ import annotations

import zoneinfo
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timezone

from .api import Bar

__all__ = ["SUPPORTED_PERIODS", "TimeBarConsolidator", "consolidate_bars", "period_seconds"]

# Identical to ``atp_strategy.calendar.EASTERN`` — daily buckets group by the ET session
# date (DST-aware). Not imported from ``calendar`` to keep this module free of the
# ``exchange_calendars`` dependency: daily grouping needs only the timezone, not the
# holiday schedule ("without pre-processed datasets").
_EASTERN = zoneinfo.ZoneInfo("America/New_York")

# Intraday period label -> bucket width in seconds (epoch-floored).
_INTRADAY_PERIOD_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}
_DAILY_PERIOD = "1d"

#: The consolidation periods this module produces (``SRS-SDK-007`` AC).
SUPPORTED_PERIODS = frozenset({*_INTRADAY_PERIOD_SECONDS, _DAILY_PERIOD})


def _require_period(period: str) -> None:
    if period not in SUPPORTED_PERIODS:
        raise ValueError(
            f"unsupported consolidation period {period!r}; "
            f"supported periods are {sorted(SUPPORTED_PERIODS)}"
        )


def period_seconds(period: str) -> int:
    """The fixed bucket width in seconds for an intraday period (``5m``/``15m``/``1h``).

    Raises :class:`ValueError` for ``"1d"``: a daily bucket spans a US-Eastern calendar
    date, whose length in seconds varies across DST transitions, so it has no fixed width.
    Used by range-bounded consumers (the store binding) to decide whether a bucket's whole
    period lies inside a requested ``[start, end]`` window.
    """
    try:
        return _INTRADAY_PERIOD_SECONDS[period]
    except KeyError:
        raise ValueError(
            f"period {period!r} has no fixed second-width; fixed-width intraday periods are "
            f"{sorted(_INTRADAY_PERIOD_SECONDS)} (daily buckets span a variable-length calendar day)"
        ) from None


def _instant(bar: Bar) -> datetime:
    """Parse a bar's ISO-8601 timestamp, failing closed on a timezone-naive value.

    A naive timestamp has no unambiguous instant (no offset), so bucketing it would be a
    silent guess. The consolidator refuses rather than assume a timezone.
    """
    parsed = datetime.fromisoformat(bar.timestamp)
    if parsed.tzinfo is None:
        raise ValueError(
            f"consolidation requires timezone-aware bar timestamps; got naive "
            f"{bar.timestamp!r} for symbol {bar.symbol!r}"
        )
    return parsed


def _bucket_key(period: str, instant: datetime) -> int | date:
    """The bucket a bar instant falls in: an epoch-second period-start (intraday) or an ET date (daily)."""
    if period == _DAILY_PERIOD:
        return instant.astimezone(_EASTERN).date()
    width = _INTRADAY_PERIOD_SECONDS[period]
    epoch = int(instant.timestamp())
    return (epoch // width) * width


def _bucket_label(period: str, key: int | date) -> str:
    """The consolidated bar's ISO-8601 timestamp: the bucket's left edge."""
    if period == _DAILY_PERIOD:
        assert isinstance(key, date)
        return datetime(key.year, key.month, key.day, tzinfo=_EASTERN).isoformat()
    assert isinstance(key, int)
    return datetime.fromtimestamp(key, tz=timezone.utc).isoformat()


@dataclass
class _Bucket:
    """A single open consolidation bucket accumulating one period's minute bars."""

    key: int | date
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int

    def add(self, bar: Bar) -> None:
        """Fold a later in-bucket bar in: high=max, low=min, close=last, volume=sum (open is first)."""
        if bar.high > self.high:
            self.high = bar.high
        if bar.low < self.low:
            self.low = bar.low
        self.close = bar.close
        self.volume += bar.volume


def _open_bucket(key: int | date, bar: Bar) -> _Bucket:
    return _Bucket(key, bar.symbol, bar.open, bar.high, bar.low, bar.close, bar.volume)


def _finalize(period: str, bucket: _Bucket) -> Bar:
    return Bar(
        bucket.symbol,
        _bucket_label(period, bucket.key),
        bucket.open,
        bucket.high,
        bucket.low,
        bucket.close,
        bucket.volume,
    )


def consolidate_bars(bars: Iterable[Bar], period: str) -> list[Bar]:
    """Consolidate an ascending single-symbol minute series into ``period`` bars.

    Args:
        bars: Minute (or finer) OHLCV bars for ONE symbol, ascending by timestamp.
        period: One of :data:`SUPPORTED_PERIODS` (``"5m"``, ``"15m"``, ``"1h"``, ``"1d"``).

    Returns:
        The consolidated bars, ascending. One bar per non-empty bucket; a bucket reflects
        exactly the input bars present in it (no gap-filling). An empty input yields ``[]``.

    Raises:
        ValueError: unknown ``period``; a timezone-naive timestamp; bars for more than one
            symbol; or a non-monotonic (decreasing) timestamp.

    Example:
        >>> from atp_strategy import Bar
        >>> minute = [
        ...     Bar("AAPL", "2026-05-04T13:30:00+00:00", 10.0, 11.0, 9.5, 10.5, 100),
        ...     Bar("AAPL", "2026-05-04T13:31:00+00:00", 10.5, 12.0, 10.0, 11.5, 200),
        ... ]
        >>> [(b.timestamp, b.open, b.high, b.low, b.close, b.volume) for b in consolidate_bars(minute, "5m")]
        [('2026-05-04T13:30:00+00:00', 10.0, 12.0, 9.5, 11.5, 300)]
    """
    _require_period(period)
    out: list[Bar] = []
    current: _Bucket | None = None
    symbol: str | None = None
    prev_epoch: int | None = None
    for bar in bars:
        instant = _instant(bar)
        epoch = int(instant.timestamp())
        if symbol is None:
            symbol = bar.symbol
        elif bar.symbol != symbol:
            raise ValueError(
                f"consolidate_bars operates on a single symbol; got {symbol!r} then {bar.symbol!r}"
            )
        if prev_epoch is not None and epoch < prev_epoch:
            raise ValueError(
                f"consolidate_bars requires ascending timestamps; {bar.timestamp!r} precedes the prior bar"
            )
        prev_epoch = epoch
        key = _bucket_key(period, instant)
        if current is None:
            current = _open_bucket(key, bar)
        elif key == current.key:
            current.add(bar)
        else:
            out.append(_finalize(period, current))
            current = _open_bucket(key, bar)
    if current is not None:
        out.append(_finalize(period, current))
    return out


class TimeBarConsolidator:
    """Concrete time-based bar consolidator (``SRS-SDK-007``).

    Implements the :class:`atp_strategy.api.BarConsolidator` Protocol (the pull-style
    :meth:`consolidate`) **and** an incremental streaming surface
    (:meth:`update` / :meth:`flush`) that mirrors the ``RenkoBuilder`` /
    ``RangeBarBuilder`` ``update()`` convention. A strategy drives live consolidation with
    :meth:`update` inside ``on_bar``; backtests / warm-up use the batch
    :func:`consolidate_bars`. Both share the same bucketing core, so the streamed and
    batched bars are identical (``AC-14`` paper/live parity).

    Lifecycle — the FINAL bucket. :meth:`update` emits a bucket only when a LATER bar opens
    the next one (proof the bucket has closed). The last bucket of a session has no following
    bar, so it is emitted by :meth:`flush`. To match a backtest bar-for-bar, a live consumer
    must flush at the session close (e.g. from a ``ctx.schedule.at_market_close`` callback);
    the runtime-managed ``ctx.consolidate`` handle is flushed at session boundaries by the
    runtime (``SRS-SDK-001``). :func:`consolidate_bars` includes the final bucket implicitly —
    it is exactly ``update``-all + ``flush``.

    Args:
        period: One of :data:`SUPPORTED_PERIODS`.
        source: Optional ascending bar iterable the Protocol :meth:`consolidate` reads
            from. The live runtime feeds bars via :meth:`update` instead; ``source`` makes
            the pull surface usable standalone (tests, notebooks, backtests).

    Example:
        >>> from atp_strategy import Bar
        >>> c = TimeBarConsolidator("5m")
        >>> c.update(Bar("AAPL", "2026-05-04T13:30:00+00:00", 10.0, 11.0, 9.5, 10.5, 100))  # in-bucket
        >>> c.update(Bar("AAPL", "2026-05-04T13:35:00+00:00", 11.5, 11.5, 11.0, 11.2, 50)).close  # new bucket -> emits
        10.5
        >>> c.flush().close  # the trailing partial bucket
        11.2
    """

    def __init__(self, period: str, *, source: Iterable[Bar] | None = None) -> None:
        _require_period(period)
        self._period = period
        self._source = source
        self._current: _Bucket | None = None
        self._symbol: str | None = None
        self._prev_epoch: int | None = None

    @property
    def period(self) -> str:
        """The consolidation period this instance is bound to."""
        return self._period

    def update(self, bar: Bar) -> Bar | None:
        """Consume one bar; return the just-completed consolidated bar, or ``None`` mid-bucket.

        Returns a :class:`~atp_strategy.api.Bar` only when ``bar`` opens a *new* bucket
        (so the previous bucket is now complete); returns ``None`` while the current bucket
        is still filling. Call :meth:`flush` after the last input bar to emit the final
        (partial) bucket. Fails closed identically to :func:`consolidate_bars`.
        """
        instant = _instant(bar)
        epoch = int(instant.timestamp())
        if self._symbol is None:
            self._symbol = bar.symbol
        elif bar.symbol != self._symbol:
            raise ValueError(
                f"TimeBarConsolidator is bound to symbol {self._symbol!r}; got {bar.symbol!r}"
            )
        if self._prev_epoch is not None and epoch < self._prev_epoch:
            raise ValueError(
                f"TimeBarConsolidator requires ascending timestamps; {bar.timestamp!r} "
                "precedes the prior bar"
            )
        self._prev_epoch = epoch
        key = _bucket_key(self._period, instant)
        completed: Bar | None = None
        if self._current is None:
            self._current = _open_bucket(key, bar)
        elif key == self._current.key:
            self._current.add(bar)
        else:
            completed = _finalize(self._period, self._current)
            self._current = _open_bucket(key, bar)
        return completed

    def flush(self) -> Bar | None:
        """Emit the currently-open (partial) bucket and reset it, or ``None`` if none is open.

        Idempotent: a second call without an intervening :meth:`update` returns ``None``.
        """
        if self._current is None:
            return None
        bar = _finalize(self._period, self._current)
        self._current = None
        return bar

    def consolidate(self, source_symbol: str, *, period: str) -> Iterator[Bar]:
        """Yield consolidated bars for ``source_symbol`` (the ``BarConsolidator`` Protocol method).

        Reads the bound ``source`` (filtered to ``source_symbol``) and yields each completed
        consolidated bar. ``period`` must equal the period this consolidator is bound to (a
        consolidator produces one period); it is part of the declared Protocol signature.

        Raises:
            ValueError: ``period`` differs from the bound period, or no ``source`` was bound.
        """
        # Validate eagerly (not lazily on first iteration): a returned iterator over the
        # pre-computed batch, so a period mismatch / missing source raises at the call site.
        if period != self._period:
            raise ValueError(
                f"this consolidator is bound to period {self._period!r}, not {period!r}"
            )
        if self._source is None:
            raise ValueError(
                "consolidate() requires a bound bar source: construct "
                "TimeBarConsolidator(period, source=<ascending bars>). The live runtime "
                "streams bars via update() instead."
            )
        selected = [bar for bar in self._source if bar.symbol == source_symbol]
        return iter(consolidate_bars(selected, self._period))
