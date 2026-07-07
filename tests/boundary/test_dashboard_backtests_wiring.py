"""L4 boundary — the UI-3 backtest panel wired over real transports + the real store.

Boots :class:`atp_runtime.OperatorInterfaceRuntime` on an ephemeral loopback port,
seeds a REAL ``BacktestResultStore`` through the real ``bt009_store_cli persist``
(the same green SRS-BT-009 binary the dashboard shells), mounts the dashboard with
the backtest-history provider, and asserts:

* ``GET /dashboard/api/backtests`` returns the persisted runs with their real
  metrics + full equity curve over a real TCP socket;
* a bare mount (no backtest provider) does NOT register the route — the panel
  reports its explicit "not mounted" state rather than pretending a feed exists;
* the served assets carry the backtest panel + the contract launch route.

SRS trace: SRS-UI-004 (history view), SRS-BT-009 (the store), SRS-SEC-002 (bind).
"""

from __future__ import annotations

import http.client
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from atp_dashboard import (
    BacktestHistoryProvider,
    ReadinessBackedProvider,
    StoreCliBacktestHistorySource,
    mount_dashboard,
)
from atp_runtime import OperatorInterfaceRuntime

pytestmark = pytest.mark.boundary

_ROOT = Path(__file__).resolve().parents[2]
_BINARY = _ROOT / "target" / "debug" / "bt009_store_cli"


def _build_cli() -> Path:
    if not _BINARY.exists():
        build = subprocess.run(
            ["cargo", "build", "-q", "-p", "atp-simulation", "--bin", "bt009_store_cli"],
            cwd=_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if build.returncode != 0:
            pytest.skip(f"cannot build bt009_store_cli: {build.stderr}")
    return _BINARY


def _seed_store(results_dir: Path) -> None:
    subprocess.run(
        [str(_build_cli()), "persist", "--init", "--dir", str(results_dir)],
        check=True,
        capture_output=True,
    )


def _get(host: str, port: int, path: str) -> tuple[int, str, bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.getheader("Content-Type") or "", response.read()
    finally:
        conn.close()


@pytest.fixture()
def backtest_dashboard(tmp_path) -> Iterator[tuple[str, int]]:
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    _seed_store(results_dir)
    runtime = OperatorInterfaceRuntime()
    provider = BacktestHistoryProvider(
        StoreCliBacktestHistorySource(results_dir=results_dir, binary=_BINARY)
    )
    publisher = mount_dashboard(runtime, ReadinessBackedProvider({}), backtests=provider)
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield host, port
    finally:
        publisher.stop()
        runtime.stop()


def test_history_route_returns_real_persisted_backtests(backtest_dashboard) -> None:
    host, port = backtest_dashboard
    status, ctype, body = _get(host, port, "/dashboard/api/backtests")
    assert status == 200 and ctype.startswith("application/json")
    snap = json.loads(body)
    assert snap["ok"] is True and snap["count"] == 2
    assert snap["srs_ref"] == "SRS-UI-004"
    runs = {b["run_id"]: b for b in snap["backtests"]}
    assert set(runs) == {"run-momentum", "run-meanrev"}
    momentum = runs["run-momentum"]
    # The seven drill-down artifacts survived the real CLI -> parse round trip.
    assert momentum["strategy"] == "momentum"
    assert {p["key"] for p in momentum["parameters"]} == {"lookback", "threshold"}
    assert isinstance(momentum["metrics"]["sharpe"], float)  # a real computed metric
    assert momentum["comparison"]["benchmark_symbol"] == "SPY"
    assert len(momentum["trade_log"]) == 2
    assert len(momentum["equity_curve"]) == 5  # full curve for the drill-down chart
    assert momentum["code_version"] == "sha:deadbeef"


def test_bare_mount_does_not_register_the_history_route(tmp_path) -> None:
    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(runtime, ReadinessBackedProvider({}))  # no backtests provider
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        status, _, _ = _get(host, port, "/dashboard/api/backtests")
        assert status == 404  # the client renders "not mounted", never a fake feed
    finally:
        publisher.stop()
        runtime.stop()


def test_default_composition_serves_the_history_route(tmp_path) -> None:
    # The production entrypoint (python -m atp_dashboard -> mount_default_dashboard)
    # ALWAYS composes the backtest-history provider, so /dashboard/api/backtests is
    # served in the real app — configured => real persisted runs.
    from atp_dashboard import mount_default_dashboard

    results_dir = tmp_path / "results"
    results_dir.mkdir()
    _seed_store(results_dir)
    runtime = OperatorInterfaceRuntime()
    publisher = mount_default_dashboard(runtime, {"ATP_BACKTEST_RESULTS_DIR": str(results_dir)})
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        status, _, body = _get(host, port, "/dashboard/api/backtests")
        assert status == 200
        snap = json.loads(body)
        assert snap["ok"] is True and snap["count"] == 2
    finally:
        publisher.stop()
        runtime.stop()


def test_default_composition_is_deterministic_wrt_passed_env(monkeypatch, tmp_path) -> None:
    # Determinism: even with an AMBIENT ATP_BACKTEST_RESULTS_DIR set (pointing at a
    # real, populated store), a composition whose PASSED env omits the key must NOT
    # read that ambient store — the subprocess env is driven by the passed mapping,
    # so it fails closed to an explicit unavailable (ok:false), never a 404 nor the
    # ambient feed. This is the guard against leaking a store the caller did not ask
    # for.
    from atp_dashboard import mount_default_dashboard

    ambient = tmp_path / "ambient"
    ambient.mkdir()
    _seed_store(ambient)  # a real populated store on the ambient env var
    monkeypatch.setenv("ATP_BACKTEST_RESULTS_DIR", str(ambient))

    runtime = OperatorInterfaceRuntime()
    publisher = mount_default_dashboard(runtime, {})  # passed env omits the key
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        status, _, body = _get(host, port, "/dashboard/api/backtests")
        assert status == 200
        snap = json.loads(body)
        assert snap["ok"] is False and snap["backtests"] == []  # did NOT leak ambient
    finally:
        publisher.stop()
        runtime.stop()


def test_served_assets_carry_the_backtest_panel_and_contract_route(backtest_dashboard) -> None:
    host, port = backtest_dashboard
    _, _, index = _get(host, port, "/dashboard")
    assert b'data-panel="backtest"' in index
    assert b'id="backtest-form"' in index
    _, _, app = _get(host, port, "/dashboard/app.js")
    # The launch affordance targets the CONTRACT route + polls the history route.
    assert b'"/api/v1/backtests"' in app
    assert b'"/dashboard/api/backtests"' in app
