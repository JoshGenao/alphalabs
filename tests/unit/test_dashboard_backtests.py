"""L1 unit — UI-3 / SRS-UI-004 backtest history provider parse + fail-closed.

The dashboard reads completed backtests by shelling ``bt009_store_cli --format kv``
and parsing its indexed proof lines. These tests pin that a well-formed machine
output parses into the seven drill-down artifacts, and that ANY drift (a wedged /
refusing CLI, a missing field, a count/index mismatch, a non-numeric value) fails
CLOSED to an explicit unavailable history — never a partial, forged, or fabricated
one. A metric the engine computed but that is mathematically undefined (``n/a``)
stays ``None`` (rendered as "—"), never a fabricated ``0``.

SRS trace: SRS-UI-004 (history view) consuming SRS-BT-009's store via its CLI.
"""

from __future__ import annotations

import subprocess

import pytest
from atp_dashboard.backtests import (
    BacktestHistoryProvider,
    BacktestHistoryUnavailable,
    StoreCliBacktestHistorySource,
)

pytestmark = pytest.mark.unit

_METRICS = (
    "sharpe",
    "sortino",
    "alpha",
    "beta",
    "max_drawdown",
    "annualized_return",
    "annualized_volatility",
    "win_rate",
)
_CMP = ("alpha", "beta", "strategy_total_return", "benchmark_total_return", "excess_return")


def _kv_doc(records: list[dict]) -> str:
    """Emit a valid ``--format kv`` document from simple record descriptors."""

    lines = [f"record_count:{len(records)}"]
    for i, r in enumerate(records):
        p = f"record.{i}"
        lines += [
            f"{p}.run_id:{r.get('run_id', 'run-' + str(i))}",
            f"{p}.strategy:{r.get('strategy', 'momentum')}",
            f"{p}.symbol:{r.get('symbol', 'AAPL')}",
            f"{p}.source:{r.get('source', 'system_data')}",
            f"{p}.run_window_start:{r.get('start', 0)}",
            f"{p}.run_window_end:{r.get('end', 100)}",
            f"{p}.starting_cash_minor:{r.get('cash', 1000000)}",
            f"{p}.completed_at:{r.get('completed_at', 100 + i)}",
            f"{p}.code_version:{r.get('code_version', 'sha:deadbeef')}",
            f"{p}.benchmark_symbol:{r.get('benchmark', 'SPY')}",
        ]
        metrics = r.get("metrics", {})
        for m in _METRICS:
            lines.append(f"{p}.metric.{m}:{metrics.get(m, '1.5')}")
        lines.append(f"{p}.comparison.benchmark_symbol:SPY")
        lines.append(f"{p}.comparison.is_default:true")
        for c in _CMP:
            lines.append(f"{p}.comparison.{c}:0.1")
        params = r.get("params", [("lookback", "20")])
        lines.append(f"{p}.param_count:{len(params)}")
        for j, (k, v) in enumerate(params):
            lines.append(f"{p}.param.{j}.key:{k}")
            lines.append(f"{p}.param.{j}.value:{v}")
        trades = r.get("trades", [])
        lines.append(f"{p}.trade_count:{len(trades)}")
        equity = r.get("equity", [])
        lines.append(f"{p}.equity_count:{len(equity)}")
        for j, t in enumerate(trades):
            lines += [
                f"{p}.trade.{j}.ts:{t['ts']}",
                f"{p}.trade.{j}.symbol:{t.get('symbol', 'AAPL')}",
                f"{p}.trade.{j}.quantity:{t['qty']}",
                f"{p}.trade.{j}.price_minor:{t['price']}",
                f"{p}.trade.{j}.commission_minor:{t.get('comm', 0)}",
                f"{p}.trade.{j}.slippage_minor:{t.get('slip', 0)}",
                f"{p}.trade.{j}.spread_impact_minor:{t.get('spread', 0)}",
            ]
        for j, e in enumerate(equity):
            lines += [f"{p}.equity.{j}.ts:{e['ts']}", f"{p}.equity.{j}.equity_minor:{e['eq']}"]
    return "\n".join(lines) + "\n"


def _runner(stdout: str, *, returncode: int = 0, stderr: str = ""):
    def run(argv, *, timeout):  # noqa: ANN001, ARG001
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

    return run


def _source(runner, *, results_dir=None):
    return StoreCliBacktestHistorySource(runner=runner, binary=__file__, results_dir=results_dir)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_well_formed_kv_parses_into_the_drilldown_artifacts() -> None:
    doc = _kv_doc(
        [
            {
                "run_id": "run-momentum",
                "strategy": "momentum",
                "params": [("lookback", "20"), ("threshold", "0.5")],
                "metrics": {"sharpe": "2.5", "win_rate": "1"},
                "trades": [{"ts": 1, "qty": 10, "price": 100}, {"ts": 5, "qty": -10, "price": 125}],
                "equity": [{"ts": 1, "eq": 999988}, {"ts": 5, "eq": 1000223}],
            }
        ]
    )
    records = _source(_runner(doc)).records()
    assert len(records) == 1
    r = records[0]
    assert r["run_id"] == "run-momentum" and r["strategy"] == "momentum"
    assert r["run_window"] == {"start": 0, "end": 100}
    assert r["parameters"] == [
        {"key": "lookback", "value": "20"},
        {"key": "threshold", "value": "0.5"},
    ]
    assert r["metrics"]["sharpe"] == 2.5 and r["metrics"]["win_rate"] == 1.0
    assert r["comparison"]["benchmark_symbol"] == "SPY" and r["comparison"]["is_default"] is True
    assert len(r["trade_log"]) == 2 and r["trade_log"][1]["quantity"] == -10
    assert len(r["equity_curve"]) == 2 and r["equity_curve"][0]["equity_minor"] == 999988
    # A colon-bearing value (the code version) survives the first-colon split.
    assert r["code_version"] == "sha:deadbeef"


def test_undefined_metric_is_none_never_fabricated() -> None:
    doc = _kv_doc([{"metrics": {"sharpe": "n/a", "sortino": "n/a"}}])
    r = _source(_runner(doc)).records()[0]
    assert r["metrics"]["sharpe"] is None and r["metrics"]["sortino"] is None
    # A defined metric still parses to a real float.
    assert isinstance(r["metrics"]["alpha"], float)


def test_history_snapshot_orders_newest_first() -> None:
    doc = _kv_doc(
        [
            {"run_id": "older", "completed_at": 100},
            {"run_id": "newer", "completed_at": 500},
        ]
    )
    snap = BacktestHistoryProvider(_source(_runner(doc))).history_snapshot()
    assert snap["ok"] is True and snap["count"] == 2
    assert [b["run_id"] for b in snap["backtests"]] == ["newer", "older"]
    assert snap["srs_ref"] == "SRS-UI-004"


def test_runner_argv_requests_the_machine_format_and_full_detail() -> None:
    seen: dict[str, list[str]] = {}

    def run(argv, *, timeout):  # noqa: ANN001, ARG001
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout=_kv_doc([{}]), stderr="")

    _source(run, results_dir="/data/results").records()
    assert seen["argv"][1:] == ["query", "--format", "kv", "--full", "--dir", "/data/results"]

    # With no results_dir the CLI resolves ATP_BACKTEST_RESULTS_DIR itself (no --dir).
    _source(run, results_dir=None).records()
    assert "--dir" not in seen["argv"]


# --------------------------------------------------------------------------- #
# Fail-closed
# --------------------------------------------------------------------------- #


def test_nonzero_exit_is_unavailable_snapshot() -> None:
    src = _source(_runner("", returncode=1, stderr="missing directory"))
    snap = BacktestHistoryProvider(src).history_snapshot()
    assert snap["ok"] is False and snap["backtests"] == [] and snap["count"] == 0
    assert "missing directory" in snap["error"]


def test_subprocess_error_is_unavailable() -> None:
    def boom(argv, *, timeout):  # noqa: ANN001, ARG001
        raise OSError("binary not found")

    snap = BacktestHistoryProvider(_source(boom)).history_snapshot()
    assert snap["ok"] is False and "binary not found" in snap["error"]


def test_timeout_is_unavailable() -> None:
    def hang(argv, *, timeout):  # noqa: ANN001, ARG001
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    snap = BacktestHistoryProvider(_source(hang)).history_snapshot()
    assert snap["ok"] is False


@pytest.mark.parametrize(
    "doc",
    [
        "record.0.run_id:x\n",  # missing record_count
        "record_count:2\nrecord.0.run_id:only-one\nrecord.0.param_count:0\n",  # count/index mismatch
        "record_count:-1\n",  # impossible count
        "record_count:1\nrecord.0.run_id:x\n",  # record present but missing required fields
    ],
)
def test_drifted_output_fails_closed(doc: str) -> None:
    with pytest.raises(BacktestHistoryUnavailable):
        _source(_runner(doc)).records()


def test_non_integer_field_fails_closed() -> None:
    bad = _kv_doc([{}]).replace("record.0.completed_at:100", "record.0.completed_at:not-a-number")
    with pytest.raises(BacktestHistoryUnavailable):
        _source(_runner(bad)).records()


def test_non_float_metric_fails_closed() -> None:
    bad = _kv_doc([{"metrics": {"sharpe": "garbage"}}])
    with pytest.raises(BacktestHistoryUnavailable):
        _source(_runner(bad)).records()


def test_newline_forged_line_fails_closed() -> None:
    # A newline-bearing parameter value splits (on "\n" ONLY — never
    # str.splitlines()) into a forged `record.0.run_id:` line that would overwrite
    # the real scalar. The real run_id is emitted first, so the forged second
    # assignment is a DUPLICATE field — the parser fails closed rather than let the
    # last writer win (the SRS-UI-002 injection lesson, hardened for newlines).
    doc = _kv_doc([{"run_id": "real", "params": [("evil", "v\nrecord.0.run_id:HACKED")]}])
    with pytest.raises(BacktestHistoryUnavailable):
        _source(_runner(doc)).records()


def test_control_char_in_value_fails_closed() -> None:
    # A residual control character (\r/\t/\v/\f) in a value is CLI drift or a
    # forgery attempt, not data we render — rejected, not kept as opaque content.
    doc = _kv_doc([{"params": [("evil", "x\rrecord.0.run_id:HACKED")]}])
    with pytest.raises(BacktestHistoryUnavailable):
        _source(_runner(doc)).records()


def test_duplicate_field_fails_closed() -> None:
    # A scalar field emitted twice for one record (drift, or a forged line) fails
    # closed rather than silently overwriting the first value.
    doc = _kv_doc([{"run_id": "real"}]) + "record.0.run_id:second\n"
    with pytest.raises(BacktestHistoryUnavailable):
        _source(_runner(doc)).records()
