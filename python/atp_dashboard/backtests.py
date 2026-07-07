"""Backtest result-history provider (``SRS-UI-004`` / SyRS SYS-42; UI-3 read leg).

Feeds the dashboard's backtest history + drill-down view. The one REAL source of
completed backtests today is the durable ``BacktestResultStore`` that the green
``SRS-BT-009`` feature persists; this module shells its operator binary
``bt009_store_cli query --format kv --full`` (the repo's subprocess → Rust CLI →
parse-stdout boundary, exactly as :mod:`atp_dashboard.inventory` does for the
strategy inventory) and renders the seven persisted artifacts the AC drill-down
names — strategy, parameters, date range, the eight performance metrics,
benchmark comparison, the full trade log, and the full equity curve.

Honesty (no fabrication — the SRS-UI-001 convention)
----------------------------------------------------
The dashboard never parses the store file or the human CLI rendering; it consumes
only the CLI's ``--format kv`` machine grammar (a single format owner). A metric
that the engine computed but that is *mathematically undefined* (e.g. a Sharpe
ratio with zero dispersion) arrives as ``n/a`` and is carried as ``None`` — the UI
renders an explicit "—", never a fabricated ``0``. A missing / unmounted results
directory, a wedged or refusing CLI, or ANY drift in the machine format is
reported as an explicit unavailable history (``ok: false`` + the reason) — a
monitoring surface must not crash, and an absent store must never masquerade as
"no completed backtests".

Scope (why UI-3 stays serialized / ``passes:false``)
----------------------------------------------------
This is the READ leg (SRS-UI-004). Populating the store from an *orchestrated*
Python-strategy run, and the write leg that *initiates* a backtest, are deferred:
the dashboard's launch affordance POSTs to the declared contract route
``POST /api/v1/backtests`` whose live handler is ``SRS-API-001``'s (see app.js).

SRS trace
---------
``SRS-UI-004`` (backtest history + drill-down), SyRS ``SYS-42`` (history view) /
``SYS-43a`` (backtest controls), consuming ``SRS-BT-009``'s durable store via its
operator CLI.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = [
    "BacktestCliRunner",
    "BacktestHistoryProvider",
    "BacktestHistorySource",
    "BacktestHistoryUnavailable",
    "StoreCliBacktestHistorySource",
]

# Default location of the cargo-built operator binary, relative to the repo root
# (python/atp_dashboard/backtests.py -> parents[2] == repo root). Build it with
# ``cargo build -p atp-simulation --bin bt009_store_cli``.
_DEFAULT_BINARY = Path(__file__).resolve().parents[2] / "target" / "debug" / "bt009_store_cli"

# Per-invocation subprocess budget (seconds) — a wedged binary surfaces as an
# unavailable history rather than hanging the poll.
_DEFAULT_TIMEOUT_S = 10.0

#: The eight performance metrics the store persists (SRS-BT-004 family).
_METRIC_NAMES: tuple[str, ...] = (
    "sharpe",
    "sortino",
    "alpha",
    "beta",
    "max_drawdown",
    "annualized_return",
    "annualized_volatility",
    "win_rate",
)

#: The benchmark-comparison float fields (SRS-BT-005 identity leg).
_COMPARISON_FLOATS: tuple[str, ...] = (
    "alpha",
    "beta",
    "strategy_total_return",
    "benchmark_total_return",
    "excess_return",
)


class BacktestHistoryUnavailable(Exception):
    """The backtest history cannot be read right now (reported, never fabricated)."""


@runtime_checkable
class BacktestCliRunner(Protocol):
    """The subprocess surface the history source depends on (injectable for tests)."""

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]: ...


def _default_runner(
    argv: list[str], *, timeout: float, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    if not Path(argv[0]).exists():
        raise FileNotFoundError(
            f"backtest history binary not found at {argv[0]}; build it with "
            "`cargo build -p atp-simulation --bin bt009_store_cli`"
        )
    # When env is supplied the subprocess sees EXACTLY it (no ambient inheritance),
    # so the CLI's own ATP_BACKTEST_RESULTS_DIR resolution is driven by the caller's
    # mapping — a composition whose env omits the key cannot silently read an ambient
    # store. env=None keeps the ambient inheritance (used only with an explicit --dir).
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=(dict(env) if env is not None else None),
    )


@runtime_checkable
class BacktestHistorySource(Protocol):
    """Source of the completed-backtest records (one structured record per run)."""

    def records(self) -> list[dict[str, object]]:
        """Every completed backtest as a structured record. Raises
        :class:`BacktestHistoryUnavailable` when the underlying store cannot be
        read (never an empty masquerade)."""
        ...


class StoreCliBacktestHistorySource:
    """Reads completed backtests from the SRS-BT-009 durable store via
    ``bt009_store_cli query --format kv --full`` (single format owner — the
    dashboard never parses the store file or the human rendering itself)."""

    def __init__(
        self,
        *,
        results_dir: str | Path | None = None,
        binary: str | Path | None = None,
        runner: BacktestCliRunner | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        env: Mapping[str, str] | None = None,
    ) -> None:
        # results_dir is optional: when None the CLI resolves ATP_BACKTEST_RESULTS_DIR
        # itself and fails closed if it is unset (symmetric with the CLI's own contract).
        self._results_dir = None if results_dir is None else str(results_dir)
        self._binary = Path(binary) if binary is not None else _DEFAULT_BINARY
        # A None runner uses the env-aware default runner; env (when set) becomes the
        # subprocess's ENTIRE environment, so the CLI's results-dir resolution is
        # deterministic w.r.t. the passed mapping rather than the ambient process env.
        self._runner = runner
        self._timeout = float(timeout)
        self._env = dict(env) if env is not None else None

    def records(self) -> list[dict[str, object]]:
        argv = [str(self._binary), "query", "--format", "kv", "--full"]
        if self._results_dir is not None:
            argv += ["--dir", self._results_dir]
        try:
            if self._runner is not None:
                completed = self._runner(argv, timeout=self._timeout)
            else:
                completed = _default_runner(argv, timeout=self._timeout, env=self._env)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BacktestHistoryUnavailable(
                f"backtest history CLI unavailable: {error}"
            ) from error
        if completed.returncode != 0:
            raise BacktestHistoryUnavailable(
                f"backtest history CLI refused: {completed.stderr.strip() or 'nonzero exit'}"
            )
        return _parse_records(completed.stdout)


def _to_int(field: str, value: str) -> int:
    try:
        return int(value)
    except ValueError as error:
        raise BacktestHistoryUnavailable(
            f"backtest history CLI output malformed ({field} not an integer: {value!r})"
        ) from error


def _to_opt_float(field: str, value: str) -> float | None:
    # 'n/a' means the metric is mathematically undefined (never a fabricated 0).
    if value == "n/a":
        return None
    try:
        return float(value)
    except ValueError as error:
        raise BacktestHistoryUnavailable(
            f"backtest history CLI output malformed ({field} not a float: {value!r})"
        ) from error


def _parse_records(stdout: str) -> list[dict[str, object]]:
    """Parse the ``record_count`` / ``record.<i>.<field>`` kv proof lines fail-closed.

    ANY drift — a non-integer count/index, non-contiguous indices, a missing
    required field, or a count that disagrees with the emitted entries — is CLI
    drift, reported as :class:`BacktestHistoryUnavailable` rather than a partial
    or forged history. Lines are split on the record separator (``"\\n"``) ONLY,
    never :meth:`str.splitlines` (whose extra separators would let a hostile
    value forge whole proof lines); the value is taken after the FIRST ``:`` so a
    colon-bearing value like a ``sha:...`` code version survives intact.
    """

    count: int | None = None
    flat: dict[int, dict[str, str]] = {}
    for line in stdout.split("\n"):
        key, sep, value = line.partition(":")
        if not sep:
            continue
        # Reject any control character in a value: a stored string (a parameter
        # value, a code version) that smuggled one is CLI drift or a forgery
        # attempt, not data we render. (A newline would already have split into a
        # forged line — caught by the duplicate-field guard below; this rejects
        # the residual \r/\t/\v/\f/NUL vectors too.)
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            raise BacktestHistoryUnavailable(
                f"backtest history CLI output has a control char in {key!r}"
            )
        if key == "record_count":
            count = _to_int("record_count", value)
        elif key.startswith("record."):
            index_str, dot, field = key[len("record.") :].partition(".")
            if not dot:
                continue
            try:
                index = int(index_str)
            except ValueError as error:
                raise BacktestHistoryUnavailable(
                    f"backtest history CLI output malformed (non-integer index in {key!r})"
                ) from error
            record = flat.setdefault(index, {})
            # A field emitted twice for one record is drift OR a forged line (a
            # newline-bearing value that split into a second `record.<i>.<field>:`
            # line, overwriting a real scalar). Fail closed rather than let the
            # last writer win.
            if field in record:
                raise BacktestHistoryUnavailable(
                    f"backtest history CLI output has a duplicate field {key!r} (possible forged line)"
                )
            record[field] = value

    if count is None:
        raise BacktestHistoryUnavailable("backtest history CLI output missing record_count")
    if count < 0:
        raise BacktestHistoryUnavailable(f"backtest history CLI output impossible: count={count}")
    if sorted(flat) != list(range(count)):
        raise BacktestHistoryUnavailable(
            f"backtest history CLI output inconsistent: record_count={count} but "
            f"records={sorted(flat)}"
        )
    return [_build_record(flat[index]) for index in range(count)]


def _build_record(fields: dict[str, str]) -> dict[str, object]:
    """Assemble one structured record from its flat ``<field> -> value`` map,
    failing closed on any missing required field or count mismatch."""

    def req(name: str) -> str:
        if name not in fields:
            raise BacktestHistoryUnavailable(f"backtest history CLI record missing {name!r}")
        return fields[name]

    param_count = _to_int("param_count", req("param_count"))
    parameters: list[dict[str, str]] = []
    for j in range(param_count):
        key = fields.get(f"param.{j}.key")
        value = fields.get(f"param.{j}.value")
        if key is None or value is None:
            raise BacktestHistoryUnavailable(f"backtest history CLI record missing param {j}")
        parameters.append({"key": key, "value": value})

    trade_count = _to_int("trade_count", req("trade_count"))
    trade_log: list[dict[str, object]] = []
    for j in range(trade_count):
        trade_log.append(
            {
                "ts": _to_int(f"trade.{j}.ts", req(f"trade.{j}.ts")),
                "symbol": req(f"trade.{j}.symbol"),
                "quantity": _to_int(f"trade.{j}.quantity", req(f"trade.{j}.quantity")),
                "price_minor": _to_int(f"trade.{j}.price_minor", req(f"trade.{j}.price_minor")),
                "commission_minor": _to_int(
                    f"trade.{j}.commission_minor", req(f"trade.{j}.commission_minor")
                ),
                "slippage_minor": _to_int(
                    f"trade.{j}.slippage_minor", req(f"trade.{j}.slippage_minor")
                ),
                "spread_impact_minor": _to_int(
                    f"trade.{j}.spread_impact_minor", req(f"trade.{j}.spread_impact_minor")
                ),
            }
        )

    equity_count = _to_int("equity_count", req("equity_count"))
    equity_curve: list[dict[str, int]] = []
    for j in range(equity_count):
        equity_curve.append(
            {
                "ts": _to_int(f"equity.{j}.ts", req(f"equity.{j}.ts")),
                "equity_minor": _to_int(
                    f"equity.{j}.equity_minor", req(f"equity.{j}.equity_minor")
                ),
            }
        )

    metrics = {
        name: _to_opt_float(f"metric.{name}", req(f"metric.{name}")) for name in _METRIC_NAMES
    }
    comparison: dict[str, object] = {
        "benchmark_symbol": req("comparison.benchmark_symbol"),
        "is_default": req("comparison.is_default") == "true",
    }
    for name in _COMPARISON_FLOATS:
        comparison[name] = _to_opt_float(f"comparison.{name}", req(f"comparison.{name}"))

    return {
        "run_id": req("run_id"),
        "strategy": req("strategy"),
        "symbol": req("symbol"),
        "source": req("source"),
        "run_window": {
            "start": _to_int("run_window_start", req("run_window_start")),
            "end": _to_int("run_window_end", req("run_window_end")),
        },
        "starting_cash_minor": _to_int("starting_cash_minor", req("starting_cash_minor")),
        "completed_at": _to_int("completed_at", req("completed_at")),
        "code_version": req("code_version"),
        "benchmark_symbol": req("benchmark_symbol"),
        "metrics": metrics,
        "comparison": comparison,
        "parameters": parameters,
        "trade_log": trade_log,
        "equity_curve": equity_curve,
    }


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class BacktestHistoryProvider:
    """Assembles the SRS-UI-004 history payload from a
    :class:`BacktestHistorySource` (fail-safe: an unreadable store becomes an
    explicit unavailable history, never a crash or an empty masquerade)."""

    def __init__(self, source: BacktestHistorySource) -> None:
        self._source = source

    def history_snapshot(self) -> dict[str, object]:
        """The REST poll body served at ``GET /dashboard/api/backtests``."""

        try:
            records = self._source.records()
        except BacktestHistoryUnavailable as unavailable:
            return {
                "generated_at": _utc_iso(),
                "ok": False,
                "error": str(unavailable),
                "backtests": [],
                "count": 0,
                "srs_ref": "SRS-UI-004",
            }
        # Newest completed backtest first — the natural order for a history view.
        ordered = sorted(
            records,
            key=lambda record: (record["completed_at"], record["run_id"]),
            reverse=True,
        )
        return {
            "generated_at": _utc_iso(),
            "ok": True,
            "backtests": ordered,
            "count": len(ordered),
            "srs_ref": "SRS-UI-004",
        }
