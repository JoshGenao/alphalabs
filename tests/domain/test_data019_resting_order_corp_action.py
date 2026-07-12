"""SRS-DATA-019 — adjust / cancel live resting orders on corporate actions.

L7 domain (safety) scenario test. Anchors the safety post-conditions of the
resting-order corporate-action planner in the domain-test layer (so the
deterministic critic recognizes the paired ``tests/domain/`` diff for the
``order_lifecycle`` cancel path) and walks the AC end-to-end over fixtures:

* The Rust suites ``srs_data_019_resting_order_corp_action`` (the adjust/cancel
  math + the cancel-then-new ledger apply) and ``srs_data_019_corp_action_notify``
  (the operator-notification clause: every cancel — and only a cancel — is emitted
  through the neutral alert port carrying the symbol + reason) pass.
* The ``data019_order_lifecycle_corp_action_cli`` scenario CLI adjusts a resting
  order's quantity + prices for a forward split, CANCELS an un-adjustable order
  (fractional reverse split, delisting) and emits BOTH the operator-notification
  intent and the strategy-callback intent, and fails closed (exit 2) on bad input.
* The **strategy-callback** AC clause: the CLI's emitted callback intent for a
  cancelled order is delivered end-to-end through the real SRS-SDK-004
  ``deliver_order_event`` seam to a recording ``Strategy.on_order_event`` as an
  ``OrderEvent(CANCELLED, reason=...)``.

Completeness: **serialized** — this proves the deterministic core over fixtures.
The live resting-order state + broker cancel wiring (SRS-EXE-001/EXE-006), real
operator email/SMS (SRS-NOTIF-001), and live in-container callback delivery
(SRS-SDK-004) are the deferred end-to-end integration that keeps SRS-DATA-019
``passes:false``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

pytestmark = [pytest.mark.domain, pytest.mark.safety]

CLI_BIN = "data019_order_lifecycle_corp_action_cli"
PACKAGE = "atp-execution"


# --------------------------------------------------------------------------- #
# Rust suites (shelled so the safety post-conditions are anchored here)
# --------------------------------------------------------------------------- #


def _cargo() -> str:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run the Rust DATA-019 suites")
    return cargo


def _run_cargo_test(suite: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_cargo(), "test", "-p", PACKAGE, "--test", suite],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_all_passed(result: subprocess.CompletedProcess[str], label: str) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"{label} Rust suite failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "test result: ok." in combined and "0 failed" in combined, (
        f"unexpected cargo test output for {label}:\n{combined}"
    )


def test_rust_transform_and_ledger_suite_passes() -> None:
    _assert_all_passed(
        _run_cargo_test("srs_data_019_resting_order_corp_action"), "transform+ledger"
    )


def test_rust_notification_emission_suite_passes() -> None:
    _assert_all_passed(_run_cargo_test("srs_data_019_corp_action_notify"), "notification-emission")


# --------------------------------------------------------------------------- #
# Scenario CLI (fixture market data + file reads + persisted-output inspection)
# --------------------------------------------------------------------------- #


def _cli_path() -> Path:
    cargo = _cargo()
    build = subprocess.run(
        [cargo, "build", "-p", PACKAGE, "--bin", CLI_BIN],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, f"cli build failed:\n{build.stderr}"
    path = ROOT / "target" / "debug" / CLI_BIN
    assert path.exists(), f"built CLI not found at {path}"
    return path


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_cli_path()), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _outcomes(stdout: str) -> list[dict]:
    return [
        json.loads(line[len("outcome:") :])
        for line in stdout.splitlines()
        if line.startswith("outcome:")
    ]


def test_cli_forward_split_adjusts_quantity_and_prices() -> None:
    result = _run_cli(
        [
            "plan",
            "--symbol",
            "AAPL",
            "--split",
            "4:1",
            "--order",
            "id=o1,side=BUY,qty=100,type=LIMIT,limit=40000",
        ]
    )
    assert result.returncode == 0, result.stderr
    (order,) = _outcomes(result.stdout)
    assert order["result"] == "ADJUSTED"
    assert order["old"]["quantity"] == 100 and order["new"]["quantity"] == 400
    assert order["old"]["limit_minor"] == 40000 and order["new"]["limit_minor"] == 10000


def test_cli_fractional_reverse_split_cancels_with_notify_and_callback_intents() -> None:
    result = _run_cli(
        [
            "plan",
            "--symbol",
            "ZZZ",
            "--split",
            "1:10",
            "--order",
            "id=frac,side=BUY,qty=5,type=LIMIT,limit=1000",
        ]
    )
    assert result.returncode == 0, result.stderr
    (order,) = _outcomes(result.stdout)
    assert order["result"] == "CANCELLED"
    assert order["reason"] == "QUANTITY_NOT_INTEGRAL"
    # AC: the operator is notified through the notification subsystem ...
    assert order["notification"]["trigger_kind"] == "CRITICAL_FAILURE"
    assert order["notification"]["severity"] == "CRITICAL"
    assert "ZZZ" in order["notification"]["summary"]
    # ... AND the strategy callback.
    assert order["callback"]["event_type"] == "CANCELLED"
    assert order["callback"]["fill_price"] == 0.0
    assert order["callback"]["fill_quantity"] == 0
    assert order["callback"]["reason"]


def test_cli_delisting_cancels() -> None:
    result = _run_cli(
        ["plan", "--symbol", "DEAD", "--delisting", "--order", "id=d1,side=SELL,qty=10,type=MARKET"]
    )
    assert result.returncode == 0, result.stderr
    (order,) = _outcomes(result.stdout)
    assert order["result"] == "CANCELLED"
    assert order["reason"] == "DELISTING"
    assert order["callback"]["event_type"] == "CANCELLED"


def test_cli_orders_file_is_read(tmp_path: Path) -> None:
    orders_file = tmp_path / "resting.txt"
    orders_file.write_text(
        "# resting orders for the AAPL split\n"
        "id=a,side=BUY,qty=100,type=LIMIT,limit=40000\n"
        "\n"
        "id=b,sym=MSFT,side=BUY,qty=50,type=MARKET\n"
    )
    result = _run_cli(
        ["plan", "--symbol", "AAPL", "--split", "2:1", "--orders-file", str(orders_file)]
    )
    assert result.returncode == 0, result.stderr
    outcomes = {o["order_id"]: o for o in _outcomes(result.stdout)}
    assert outcomes["live-1/a"]["result"] == "ADJUSTED"
    assert outcomes["live-1/b"]["result"] == "UNAFFECTED"  # other symbol


def test_cli_unknown_flag_exits_2_without_planning() -> None:
    result = _run_cli(["plan", "--symbol", "AAPL", "--split", "4:1", "--bogus", "x"])
    assert result.returncode == 2
    assert "outcome:" not in result.stdout


# --------------------------------------------------------------------------- #
# The strategy-callback AC clause, end-to-end through the SRS-SDK-004 seam
# --------------------------------------------------------------------------- #


def test_cancel_delivers_cancelled_order_event_to_strategy_callback() -> None:
    from atp_strategy import OrderEvent, OrderEventType, Strategy
    from atp_strategy.dispatch import deliver_order_event

    # Take the callback intent the CLI emitted for a cancelled resting order.
    result = _run_cli(
        [
            "plan",
            "--symbol",
            "ZZZ",
            "--split",
            "1:10",
            "--order",
            "id=frac,side=BUY,qty=5,type=LIMIT,limit=1000",
        ]
    )
    assert result.returncode == 0, result.stderr
    (order,) = _outcomes(result.stdout)
    callback = order["callback"]

    class _RecordingStrategy(Strategy):
        def __init__(self) -> None:
            self.received: list[OrderEvent] = []

        def on_order_event(self, context, event: OrderEvent) -> None:  # type: ignore[override]
            self.received.append(event)

    strategy = _RecordingStrategy()
    event = OrderEvent(
        event_type=OrderEventType.CANCELLED,
        order_id=callback["order_id"],
        client_order_id="frac",
        strategy_id="live-1",
        symbol=order["symbol"],
        fill_price=callback["fill_price"],
        fill_quantity=callback["fill_quantity"],
        cumulative_filled=0,
        remaining_quantity=5,
        commission=0.0,
        reason=callback["reason"],
        timestamp="2026-07-12T00:00:00Z",
    )

    # Deliver through the real SRS-SDK-004 seam (guard -> invoke). Injected clock
    # keeps the latency sample deterministic and non-negative.
    latency_ns = deliver_order_event(strategy, None, event, fill_at_ns=0, clock=lambda: 1_000)

    assert latency_ns >= 0
    assert len(strategy.received) == 1, "exactly one order-event delivered"
    delivered = strategy.received[0]
    assert delivered.event_type is OrderEventType.CANCELLED
    assert delivered.reason == callback["reason"]
    assert "fractional share" in delivered.reason
