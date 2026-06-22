"""Store-backed ``HistoricalData`` binding for the Python Strategy API (``SRS-DATA-007``).

This module ships the **first real Python↔atp-data consumer binding**: a concrete
implementation of the :class:`atp_strategy.api.HistoricalData` Protocol that answers a
strategy / backtest / factor-job / notebook query **by symbol, date range, and
resolution — without ever naming the original source provider** — by driving the
lock-free, source-neutral Rust operator CLI ``data007_query_cli`` over the durable
``MarketDataStore`` the SRS-DATA-016 ingestion path persists.

Why a subprocess CLI and not an in-process FFI binding
------------------------------------------------------
The repo's only cross-language boundary pattern is *subprocess → cargo-built Rust binary
→ parse stdout* (see ``tests/domain/test_ingestion_idempotency.py``). There is no
PyO3/maturin toolchain in scope. ``data007_query_cli`` already calls
``MarketDataStore::load_from_path`` + ``query_unified`` directly, takes **no** single-writer
``StoreLock`` (so a read never blocks on, nor is blocked by, an ingestion write — the
SRS-DATA-017 read property), and is contract-tested to emit **source-neutral** output (no
provider/vendor/source line, no ``--provider`` flag). The binding inherits all of those
guarantees. Because ``HistoricalData`` is a Protocol, this transport can be swapped for an
in-process binding later without changing any caller.

Scope / honesty
---------------
* **Source-neutral.** There is NO provider/vendor/source/feed parameter, and no origin field
  is read off the result — the core ``SRS-DATA-007`` invariant.
* **Normalization.** The store applies NO corporate-action adjustment yet (the deferred
  ``SRS-DATA-012``). The query methods keep the :class:`HistoricalData` Protocol default
  (``SPLIT_ADJUSTED``) and raise :class:`NotImplementedError` for ``SPLIT_ADJUSTED`` /
  ``FULLY_ADJUSTED`` / ``TOTAL_RETURN``, so a caller that omits ``normalization`` fails closed
  rather than silently receiving raw bars dressed up as adjusted. A caller passes
  ``NormalizationMode.RAW`` to read the stored values **verbatim**.
* **No hang.** Every CLI invocation is bounded by a per-query ``timeout`` (default
  :data:`_DEFAULT_QUERY_TIMEOUT_S`); a wedged CLI surfaces a :class:`StoreQueryError`, never an
  indefinite block of a strategy container.
* **Money units.** Stored OHLC values are integer *minor* units; this binding assumes the
  cents scale (:data:`_PRICE_MINOR_SCALE` ``= 100``) the fixture convention uses for equity
  OHLCV (``close = seed*100`` in ``crates/atp-data/src/store.rs``). ``volume`` is a raw integer
  count and is **not** scaled. The authoritative SDK↔core money-unit boundary is deferred
  (``atp-types`` ``order_type.rs``); :data:`_PRICE_MINOR_SCALE` is the single place to change
  once it is pinned.
* **Asset class.** Only ``EQUITY`` OHLCV bars are in scope; ``OPTION`` chain access raises
  (deferred). Real provider network adapters (Databento/IB/Sharadar/option-chain) are deferred
  (``SRS-DATA-001/003/005/006``); fixture data stands in. The concurrent-read-DURING-write
  Load test for this *named Python consumer* is the deferred ``SRS-DATA-017`` close.
"""

from __future__ import annotations

import math
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from .api import AssetClass, Bar, NormalizationMode, StrategyAPIError

__all__ = [
    "StoreBackedHistoricalData",
    "StoreQueryError",
]

# Price minor-unit scale for equity OHLC fields. The fixtures encode prices in cents
# (``close = seed*100`` in crates/atp-data/src/store.rs), so $100.00 is stored as 10000.
# ``volume`` is a raw integer count and is NEVER divided by this scale. This is an explicit
# assumption pending the authoritative SDK↔core money-unit boundary (deferred; see
# atp-types/src/order_type.rs) — change it in this one place when that boundary is pinned.
_PRICE_MINOR_SCALE = 100

# Default location of the cargo-built operator binary, relative to the repo root
# (python/atp_strategy/store_history.py -> parents[2] == repo root). Build it with
# ``cargo build -p atp-data --bin data007_query_cli``.
_DEFAULT_QUERY_BINARY = Path(__file__).resolve().parents[2] / "target" / "debug" / "data007_query_cli"

# OHLC price field names (scaled) vs the volume count field (unscaled). Sourced from the
# vendor-neutral fixture records in crates/atp-data/src/store.rs (ohlcv_record).
_OHLC_FIELDS = ("open", "high", "low", "close")
_VOLUME_FIELD = "volume"

# Equity-bar resolution -> vendor-neutral DatasetKind label (NOT a provider; the data007_query_cli
# --kind disambiguator). Narrowing an equity query to its bar kind stops a fundamental / option-chain
# record that happens to share the same symbol + resolution from poisoning an OHLCV-bar read.
_EQUITY_BAR_KIND_BY_RESOLUTION = {"1d": "daily-equity-bar", "1m": "minute-equity-bar"}

# Default per-query subprocess budget (seconds). A local store read is sub-second; this bound exists
# so a wedged CLI surfaces a TimeoutExpired -> StoreQueryError rather than hanging a strategy container.
_DEFAULT_QUERY_TIMEOUT_S = 30.0


class StoreQueryError(StrategyAPIError):
    """Raised when the store query binding fails to produce a result.

    Surfaced as a :class:`atp_strategy.api.StrategyAPIError` (SyRS ``SYS-64`` structured
    error) so strategy code sees ONE structured failure type rather than a raw subprocess /
    OS error. Covers a launch failure (missing or un-executable binary), a timeout, a non-zero
    CLI exit, a missing / unparseable / drifted / mislabelled / out-of-range / misordered
    response, or a record missing a required OHLCV field — the binding never fabricates a
    :class:`Bar`.
    """


class _QueryRunner(Protocol):
    """The subprocess surface the binding depends on (injectable for tests)."""

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]: ...


def _default_runner(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run the query CLI with ``argv`` as a list (``shell=False``; no shell injection).

    Fails closed with a clear :class:`FileNotFoundError` (rather than a bare ``OSError``) when the
    cargo-built binary is absent, so a consumer is told to build it. The ``timeout`` bounds the
    wait so a wedged CLI surfaces a ``subprocess.TimeoutExpired`` (mapped to ``StoreQueryError`` by
    the caller) instead of hanging a strategy container indefinitely.
    """
    if not Path(argv[0]).exists():
        raise FileNotFoundError(
            f"query binary not found at {argv[0]}; build it with "
            "`cargo build -p atp-data --bin data007_query_cli`"
        )
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)


class StoreBackedHistoricalData:
    """Concrete :class:`atp_strategy.api.HistoricalData` over the durable market-data store.

    Drives the lock-free, source-neutral ``data007_query_cli`` to answer queries by
    ``(symbol, resolution, date range)`` with no provider named, then converts the
    integer-minor stored fields into :class:`Bar` objects.

    Args:
        store_dir: Directory holding the persisted store. Falls back to the
            ``ATP_DATA_STORE_DIR`` config key (read as an environment variable), else
            fails closed with :class:`ValueError` — mirrors the Rust ``resolve_dir`` rather
            than masquerading as an empty catalog.
        query_binary: Path to the cargo-built ``data007_query_cli``; defaults to
            ``target/debug/data007_query_cli`` under the repo root.
        clock: Injectable ``() -> datetime`` used only to resolve ``end=None`` ("now"); the
            single non-pure input, isolated so range queries stay deterministic.
        runner: Injectable subprocess runner (defaults to :func:`_default_runner`); tests
            substitute a fake to exercise parsing without building cargo.
        timeout: Per-query subprocess wall-clock budget in seconds (default
            :data:`_DEFAULT_QUERY_TIMEOUT_S`). A wedged CLI raises
            :class:`subprocess.TimeoutExpired`, which the binding maps to :class:`StoreQueryError`
            — a read never hangs a strategy container indefinitely.

    Example:
        >>> from atp_strategy import HistoricalData
        >>> from atp_strategy.store_history import StoreBackedHistoricalData
        >>> isinstance(StoreBackedHistoricalData(store_dir="/tmp/x"), HistoricalData)
        True
    """

    def __init__(
        self,
        *,
        store_dir: str | os.PathLike[str] | None = None,
        query_binary: str | os.PathLike[str] | None = None,
        clock: Callable[[], datetime] | None = None,
        runner: _QueryRunner | None = None,
        timeout: float = _DEFAULT_QUERY_TIMEOUT_S,
    ) -> None:
        resolved_dir = store_dir if store_dir is not None else os.environ.get("ATP_DATA_STORE_DIR")
        if resolved_dir is None or not str(resolved_dir).strip():
            raise ValueError(
                "no store directory: pass store_dir=... or set ATP_DATA_STORE_DIR "
                "(the binding fails closed rather than reading an empty catalog)"
            )
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError(f"timeout must be a positive number of seconds (got {timeout!r})")
        self._store_dir = str(resolved_dir)
        self._query_binary = Path(query_binary) if query_binary is not None else _DEFAULT_QUERY_BINARY
        self._clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))
        self._runner = runner if runner is not None else _default_runner
        self._timeout = float(timeout)

    # ------------------------------------------------------------------ #
    # HistoricalData Protocol surface
    # ------------------------------------------------------------------ #

    def get_bars(
        self,
        symbol: str,
        *,
        lookback: int,
        frequency: str = "1m",
        end: datetime | None = None,
        asset_class: AssetClass = AssetClass.EQUITY,
        normalization: NormalizationMode = NormalizationMode.SPLIT_ADJUSTED,
    ) -> list[Bar]:
        """Return the last ``lookback`` bars at ``frequency`` ending at ``end`` (default: now).

        ``frequency`` maps 1:1 to the store ``resolution``. ``end`` is treated as **exclusive**
        per the Protocol contract (the store range is inclusive, so the upper bound is
        ``end - 1s``). The query spans the full ``[0, end]`` range and the last ``lookback``
        records are returned — exact regardless of bar spacing/gaps, and deterministic given
        the store contents and ``end``.

        ``normalization`` defaults to ``SPLIT_ADJUSTED`` to MATCH the :class:`HistoricalData`
        Protocol default: because the store applies no corporate-action adjustment yet (deferred
        SRS-DATA-012), a caller that omits ``normalization`` gets a fail-closed
        :class:`NotImplementedError` rather than silently receiving raw bars dressed up as
        adjusted. Pass ``normalization=NormalizationMode.RAW`` to read the stored values verbatim.
        """
        if not isinstance(lookback, int) or isinstance(lookback, bool):
            raise ValueError(f"lookback must be a non-negative int (got {lookback!r})")
        if lookback < 0:
            raise ValueError(f"lookback must be a non-negative int (got {lookback})")
        self._reject_unsupported(asset_class, normalization)
        if lookback == 0:
            return []
        end_ts = self._exclusive_end_ts(end)
        if end_ts < 0:
            return []
        bars = self._query(symbol=symbol, resolution=frequency, start_ts=0, end_ts=end_ts)
        return bars[-lookback:]

    def get_bars_range(
        self,
        symbol: str,
        *,
        frequency: str,
        start: datetime,
        end: datetime,
        asset_class: AssetClass = AssetClass.EQUITY,
        normalization: NormalizationMode = NormalizationMode.SPLIT_ADJUSTED,
    ) -> list[Bar]:
        """Return every bar in the **inclusive** ``[start, end]`` range at ``frequency``.

        The fully deterministic range primitive (no clock read) that backtests and factor
        jobs call for reproducibility; :meth:`get_bars` is the lookback-shaped wrapper. Like
        :meth:`get_bars`, ``normalization`` defaults to ``SPLIT_ADJUSTED`` and a non-RAW request
        fails closed (deferred SRS-DATA-012); pass ``NormalizationMode.RAW`` for verbatim values.
        """
        self._reject_unsupported(asset_class, normalization)
        start_ts = self._epoch_seconds(start)
        end_ts = self._epoch_seconds(end)
        if start_ts < 0:
            raise ValueError(f"start must not predate the epoch (got {start_ts})")
        if end_ts < start_ts:
            raise ValueError(f"end ({end_ts}) must not precede start ({start_ts})")
        return self._query(symbol=symbol, resolution=frequency, start_ts=start_ts, end_ts=end_ts)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    @staticmethod
    def _reject_unsupported(asset_class: AssetClass, normalization: NormalizationMode) -> None:
        """Fail closed for out-of-scope asset class / normalization rather than mis-answering."""
        if normalization != NormalizationMode.RAW:
            raise NotImplementedError(
                f"StoreBackedHistoricalData returns stored values verbatim "
                f"(NormalizationMode.RAW); {normalization} (split / fully-adjusted / "
                "total-return) normalization is deferred to SRS-DATA-012"
            )
        if asset_class != AssetClass.EQUITY:
            raise NotImplementedError(
                f"StoreBackedHistoricalData serves EQUITY OHLCV bars; {asset_class} "
                "(option-chain) bar access is out of scope (deferred)"
            )

    @staticmethod
    def _epoch_seconds(value: datetime) -> int:
        """Convert a datetime to epoch seconds, treating a naive datetime as UTC."""
        if not isinstance(value, datetime):
            raise TypeError(f"expected datetime, got {type(value).__name__}")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())

    def _exclusive_end_ts(self, end: datetime | None) -> int:
        """Resolve the inclusive (second-granularity) upper bound for an *exclusive* ``end`` (or "now").

        The store keys on integer epoch-seconds. For an exclusive ``end`` the largest second STRICTLY
        before it is ``ceil(end) - 1``: an exact-second ``end`` (12:00:00.0) excludes the 12:00:00
        record (``ceil``-1 = 11:59:59), while a fractional ``end`` (12:00:00.5) still includes the
        12:00:00 record (``ceil``-1 = 12:00:00) instead of dropping it via plain truncation.
        """
        if end is None:
            return int(self._clock().timestamp())
        if not isinstance(end, datetime):
            raise TypeError(f"end must be a datetime, got {type(end).__name__}")
        normalized = end if end.tzinfo is not None else end.replace(tzinfo=timezone.utc)
        return math.ceil(normalized.timestamp()) - 1

    @classmethod
    def _equity_bar_kind(cls, resolution: str) -> str:
        """Map an equity-bar resolution to its vendor-neutral DatasetKind label (else fail closed)."""
        kind = _EQUITY_BAR_KIND_BY_RESOLUTION.get(resolution)
        if kind is None:
            raise NotImplementedError(
                f"StoreBackedHistoricalData serves the daily ('1d') and minute ('1m') equity-bar "
                f"datasets; resolution {resolution!r} is out of scope (richer resolutions need bar "
                "consolidation, SRS-SDK-007, deferred)"
            )
        return kind

    def _query(self, *, symbol: str, resolution: str, start_ts: int, end_ts: int) -> list[Bar]:
        """Run the source-neutral query CLI and parse its stdout into ascending bars."""
        # Narrow to the vendor-neutral equity-bar DatasetKind so a fundamental / option-chain record
        # sharing this symbol + resolution cannot poison the OHLCV-bar read (DatasetKind is a dataset
        # type, NOT a provider — the query stays source-neutral).
        kind = self._equity_bar_kind(resolution)
        # argv is a LIST (shell=False) — never a shell string — so a symbol can never inject.
        argv = [
            str(self._query_binary),
            "query",
            "--dir",
            self._store_dir,
            "--symbol",
            symbol,
            "--resolution",
            resolution,
            "--start",
            str(start_ts),
            "--end",
            str(end_ts),
            "--kind",
            kind,
        ]
        try:
            completed = self._runner(argv, timeout=self._timeout)
        except subprocess.TimeoutExpired as expired:
            raise StoreQueryError(
                f"data007_query_cli timed out after {self._timeout}s for symbol={symbol!r} "
                f"resolution={resolution!r} — refusing to block the consumer indefinitely"
            ) from expired
        except OSError as launch_error:
            # A missing/un-executable binary (FileNotFoundError, PermissionError, ...) is a launch
            # failure — surface it as the binding's one structured StoreQueryError (StrategyAPIError)
            # rather than leaking a raw OS exception past the documented boundary.
            raise StoreQueryError(
                f"data007_query_cli could not be launched for symbol={symbol!r} "
                f"(is it built? `cargo build -p atp-data --bin data007_query_cli`): {launch_error}"
            ) from launch_error
        if completed.returncode != 0:
            raise StoreQueryError(
                f"data007_query_cli failed (exit {completed.returncode}) for "
                f"symbol={symbol!r} resolution={resolution!r}: {completed.stderr.strip()}"
            )
        return self._parse(
            completed.stdout, symbol=symbol, resolution=resolution, start_ts=start_ts, end_ts=end_ts
        )

    def _parse(
        self, stdout: str, *, symbol: str, resolution: str, start_ts: int, end_ts: int
    ) -> list[Bar]:
        """Parse the source-neutral ``key:value`` lines into :class:`Bar` objects.

        Validates the full echoed envelope before building any :class:`Bar` — the echoed ``symbol`` /
        ``resolution`` / ``start`` / ``end`` MUST match the request, every record's ``event_ts`` MUST
        fall inside the inclusive ``[start_ts, end_ts]`` range, and the records MUST be ``event_ts``
        ascending. A wrong/stale ``data007_query_cli`` (CLI/schema drift) therefore fails closed
        rather than leaking out-of-range (future/stale) or misordered bars into a strategy / backtest /
        factor consumer at the SRS-DATA-007 trust boundary. ``match_count:0`` (with no records) is a
        valid empty result. No provider/source/vendor line exists to read.
        """
        match_count: int | None = None
        echoed_symbol: str | None = None
        echoed_resolution: str | None = None
        echoed_start: int | None = None
        echoed_end: int | None = None
        records: dict[int, dict[str, object]] = {}
        try:
            for line in stdout.splitlines():
                key, sep, value = line.partition(":")
                if not sep:
                    continue
                if key == "symbol":
                    echoed_symbol = value
                elif key == "resolution":
                    echoed_resolution = value
                elif key == "start":
                    echoed_start = int(value)
                elif key == "end":
                    echoed_end = int(value)
                elif key == "match_count":
                    match_count = int(value)
                elif key.startswith("record."):
                    parts = key.split(".")
                    index = int(parts[1])
                    record = records.setdefault(index, {"fields": {}})
                    if parts[2] == "event_ts":
                        record["event_ts"] = int(value)
                    elif parts[2] == "field":
                        fields = record["fields"]
                        assert isinstance(fields, dict)
                        fields[parts[3]] = int(value)
                    # record.{i}.option_contract is ignored: equity bars carry no contract, and it
                    # is not a source/provider field.
        except (ValueError, IndexError) as malformed:
            # A malformed index / non-integer value is corruption, not an empty result — fail closed.
            raise StoreQueryError(
                f"malformed data007_query_cli output for symbol={symbol!r}: {malformed}"
            ) from malformed
        # Validate the echoed envelope BEFORE building any Bar: the CLI echoes the symbol + resolution
        # it actually queried, so a mismatch (CLI/schema drift, or a wrong/stale query binary) must
        # fail closed rather than relabel one symbol's records as another at the trust boundary.
        if echoed_symbol != symbol or echoed_resolution != resolution:
            raise StoreQueryError(
                f"data007_query_cli echoed symbol={echoed_symbol!r} resolution={echoed_resolution!r} "
                f"but the request was symbol={symbol!r} resolution={resolution!r}; refusing to relabel "
                "records (CLI/schema drift or a wrong/stale query binary)"
            )
        if echoed_start != start_ts or echoed_end != end_ts:
            raise StoreQueryError(
                f"data007_query_cli echoed start={echoed_start!r} end={echoed_end!r} but the request "
                f"was start={start_ts} end={end_ts} for symbol={symbol!r}; refusing a mismatched range "
                "(CLI/schema drift or a wrong/stale query binary)"
            )
        if match_count is None:
            raise StoreQueryError(
                f"data007_query_cli produced no match_count for symbol={symbol!r}; "
                f"unparseable output:\n{stdout}"
            )
        if match_count < 0:
            raise StoreQueryError(
                f"data007_query_cli reported a negative match_count={match_count} for "
                f"symbol={symbol!r}; refusing impossible output (CLI/schema drift)"
            )
        if match_count == 0:
            # An empty match is a value, but match_count:0 WITH record lines is inconsistent drift.
            if records:
                raise StoreQueryError(
                    f"data007_query_cli reported match_count=0 but parsed {len(records)} record "
                    f"group(s) {sorted(records)} for symbol={symbol!r}; refusing inconsistent output"
                )
            return []
        # The parsed record indexes must cover EXACTLY [0, match_count) — a truncated or drifted CLI
        # output (e.g. match_count:3 with only two record groups, or a gap) must fail closed rather
        # than silently feed partial history to a strategy / backtest / factor job.
        expected = set(range(match_count))
        if set(records) != expected:
            raise StoreQueryError(
                f"data007_query_cli reported match_count={match_count} but parsed record indexes "
                f"{sorted(records)} for symbol={symbol!r} (missing={sorted(expected - set(records))}, "
                f"unexpected={sorted(set(records) - expected)}); refusing to return partial history"
            )
        # Every record's event_ts must fall inside the requested inclusive range and the records must
        # be event_ts-ascending (the CLI guarantees both). A wrong/stale binary returning out-of-range
        # (future/stale) or misordered rows must fail closed — get_bars takes the LAST `lookback`,
        # which is only correct for ascending input.
        ordered = [records[index] for index in range(match_count)]
        previous_ts: int | None = None
        for record in ordered:
            event_ts = record.get("event_ts")
            if not isinstance(event_ts, int):
                raise StoreQueryError(f"record for symbol={symbol!r} is missing event_ts")
            if event_ts < start_ts or event_ts > end_ts:
                raise StoreQueryError(
                    f"data007_query_cli returned event_ts={event_ts} outside the requested range "
                    f"[{start_ts}, {end_ts}] for symbol={symbol!r}; refusing out-of-range "
                    "(future/stale) data (CLI/schema drift or a wrong/stale query binary)"
                )
            if previous_ts is not None and event_ts < previous_ts:
                raise StoreQueryError(
                    f"data007_query_cli returned non-ascending event_ts ({previous_ts} then "
                    f"{event_ts}) for symbol={symbol!r}; refusing misordered data"
                )
            previous_ts = event_ts
        return [self._build_bar(record, symbol=symbol) for record in ordered]

    def _build_bar(self, record: dict[str, object], *, symbol: str) -> Bar:
        """Convert one parsed record into a :class:`Bar` (raises on a missing OHLCV field)."""
        event_ts = record.get("event_ts")
        fields = record.get("fields")
        if not isinstance(event_ts, int) or not isinstance(fields, dict):
            raise StoreQueryError(f"record for symbol={symbol!r} is missing event_ts/fields")
        timestamp = datetime.fromtimestamp(event_ts, tz=timezone.utc).isoformat()
        try:
            # OHLC prices are scaled minor units -> major units; volume is a raw count (UNSCALED).
            open_ = fields["open"] / _PRICE_MINOR_SCALE
            high = fields["high"] / _PRICE_MINOR_SCALE
            low = fields["low"] / _PRICE_MINOR_SCALE
            close = fields["close"] / _PRICE_MINOR_SCALE
            volume = int(fields[_VOLUME_FIELD])
        except KeyError as missing:
            raise StoreQueryError(
                f"record for symbol={symbol!r} at event_ts={event_ts} is missing "
                f"required OHLCV field {missing}; the binding never fabricates a Bar"
            ) from missing
        return Bar(symbol, timestamp, open_, high, low, close, volume)
