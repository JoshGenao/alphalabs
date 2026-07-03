"""SRS-SDK-004 / SyRS SYS-7, SYS-85, NFR-P4 — paper order-event callback delivery.

L7 domain (safety) test for the **production** delivery seam
``atp_strategy.dispatch`` (``SimulatedFill`` → ``build_order_event`` →
``deliver_order_event`` / ``deliver_simulated_fill``). Distinct from
``test_order_event_dispatch.py`` (which exercises a *test-only* ``_RefDispatcher``):
here the callbacks flow through the importable production helper that the
architecture metadata says concrete dispatchers must reuse.

Locks:

* All four AC-named categories (FILL, PARTIAL_FILL, CANCELLED, REJECTED) are
  delivered through the production seam into user strategy code carrying the
  documented fields — fill price, fill quantity, commission, order identifiers —
  with values correctly converted from the engine's integer minor units.
* Fail-closed delivery: a malformed / schema-drifted / fabricated descriptor or a
  bad sink raises ``OrderEventContractError`` and the user callback is **never**
  invoked (no-fabrication; the guard runs before user code).
* Money conversion (integer minor units → float) round-trips to the nearest minor
  unit, so paper / live P&L reconciles.
* **NFR-P4 paper leg**: paper callback delivery latency through the seam is well
  under the 100 ms p95 budget, evaluated by the authoritative SRS-PERF-001
  percentile engine (the ``nfr_p95_cli`` binary) when it is built, plus a
  binary-free upper-bound guard that always runs. NOTE: this measures the
  SDK-owned *delivery seam* (descriptor → callback); the engine-inclusive,
  PTP-disciplined end-to-end paper proof is deferred / serialized to SRS-SIM-001,
  and the live < 1000 ms leg to SRS-EXE-001 / SRS-EXE-006 (needs IB Gateway).
"""

from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"

if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

import pytest  # noqa: E402
from atp_strategy.api import (  # noqa: E402
    PAPER_CALLBACK_LATENCY_P95_MS,
    OrderEvent,
    OrderEventContractError,
    OrderEventType,
)
from atp_strategy.dispatch import (  # noqa: E402
    MINOR_UNITS_PER_UNIT,
    SimulatedFill,
    build_order_event,
    deliver_order_event,
    deliver_simulated_fill,
)

pytestmark = [pytest.mark.domain, pytest.mark.safety]


# --------------------------------------------------------------------------- #
# Recording sink + descriptor builders
# --------------------------------------------------------------------------- #


class _RecordingStrategy:
    """Minimal strategy sink recording every delivered event."""

    def __init__(self) -> None:
        self.received: list[OrderEvent] = []

    def on_order_event(self, context: object, event: OrderEvent) -> None:
        self.received.append(event)


def _fill(order_id: str = "ord-1", *, at_ns: int | None = None) -> SimulatedFill:
    return SimulatedFill(
        event_type=OrderEventType.FILL,
        sim_order_id=order_id,
        client_order_id=f"cli-{order_id}",
        strategy_id="s1",
        symbol="AAPL",
        fill_price_minor=12_345,  # $123.45
        fill_quantity=10,
        cumulative_filled=10,
        remaining_quantity=0,
        commission_minor=5,  # $0.05
        reason=None,
        simulated_fill_at_ns=at_ns if at_ns is not None else time.perf_counter_ns(),
        timestamp="2026-07-03T13:30:00Z",
    )


def _partial_fill(order_id: str = "ord-2", *, at_ns: int | None = None) -> SimulatedFill:
    return SimulatedFill(
        event_type=OrderEventType.PARTIAL_FILL,
        sim_order_id=order_id,
        client_order_id=f"cli-{order_id}",
        strategy_id="s1",
        symbol="AAPL",
        fill_price_minor=10_010,  # $100.10
        fill_quantity=4,
        cumulative_filled=4,
        remaining_quantity=6,
        commission_minor=2,  # $0.02
        reason=None,
        simulated_fill_at_ns=at_ns if at_ns is not None else time.perf_counter_ns(),
        timestamp="2026-07-03T13:30:01Z",
    )


def _cancelled(order_id: str = "ord-3", *, at_ns: int | None = None) -> SimulatedFill:
    # Never-filled cancel: explicit zeros per the documented convention.
    return SimulatedFill(
        event_type=OrderEventType.CANCELLED,
        sim_order_id=order_id,
        client_order_id=f"cli-{order_id}",
        strategy_id="s1",
        symbol="AAPL",
        fill_price_minor=0,
        fill_quantity=0,
        cumulative_filled=0,
        remaining_quantity=0,
        commission_minor=0,
        reason="operator cancel",
        simulated_fill_at_ns=at_ns if at_ns is not None else time.perf_counter_ns(),
        timestamp="2026-07-03T13:30:02Z",
    )


def _rejected(order_id: str = "ord-4", *, at_ns: int | None = None) -> SimulatedFill:
    return SimulatedFill(
        event_type=OrderEventType.REJECTED,
        sim_order_id=order_id,
        client_order_id=f"cli-{order_id}",
        strategy_id="s1",
        symbol="AAPL",
        fill_price_minor=0,
        fill_quantity=0,
        cumulative_filled=0,
        remaining_quantity=0,
        commission_minor=0,
        reason="insufficient buying power",
        simulated_fill_at_ns=at_ns if at_ns is not None else time.perf_counter_ns(),
        timestamp="2026-07-03T13:30:03Z",
    )


_AC_BUILDERS = (_fill, _partial_fill, _cancelled, _rejected)


# --------------------------------------------------------------------------- #
# Four-category delivery through the production seam
# --------------------------------------------------------------------------- #


class FourCategoryProductionDeliveryTest(unittest.TestCase):
    def test_all_four_categories_delivered_with_ac_fields(self) -> None:
        strategy = _RecordingStrategy()
        for builder in _AC_BUILDERS:
            deliver_simulated_fill(strategy, None, builder())

        self.assertEqual(
            [e.event_type for e in strategy.received],
            [
                OrderEventType.FILL,
                OrderEventType.PARTIAL_FILL,
                OrderEventType.CANCELLED,
                OrderEventType.REJECTED,
            ],
        )
        # Every delivered event carries the four AC field families with non-None
        # values (order identifiers, fill price, fill quantity, commission).
        for event in strategy.received:
            self.assertTrue(event.order_id)
            self.assertTrue(event.client_order_id)
            self.assertIsNotNone(event.fill_price)
            self.assertIsNotNone(event.fill_quantity)
            self.assertIsNotNone(event.commission)

    def test_fill_field_values_converted_from_minor_units(self) -> None:
        strategy = _RecordingStrategy()
        deliver_simulated_fill(strategy, None, _fill("ord-x"))
        (event,) = strategy.received
        self.assertEqual(event.order_id, "ord-x")
        self.assertEqual(event.client_order_id, "cli-ord-x")
        self.assertEqual(event.fill_price, 123.45)
        self.assertEqual(event.fill_quantity, 10)
        self.assertEqual(event.commission, 0.05)
        self.assertEqual(event.remaining_quantity, 0)

    def test_partial_fill_carries_remaining_quantity(self) -> None:
        strategy = _RecordingStrategy()
        deliver_simulated_fill(strategy, None, _partial_fill())
        (event,) = strategy.received
        self.assertEqual(event.fill_quantity, 4)
        self.assertEqual(event.remaining_quantity, 6)
        self.assertEqual(event.commission, 0.02)

    def test_cancelled_and_rejected_carry_reason_and_zero_fills(self) -> None:
        for builder, expected_reason in (
            (_cancelled, "operator cancel"),
            (_rejected, "insufficient buying power"),
        ):
            strategy = _RecordingStrategy()
            deliver_simulated_fill(strategy, None, builder())
            (event,) = strategy.received
            self.assertEqual(event.reason, expected_reason)
            self.assertEqual(event.fill_price, 0.0)
            self.assertEqual(event.fill_quantity, 0)
            self.assertEqual(event.commission, 0.0)


# --------------------------------------------------------------------------- #
# Fail-closed delivery / no-fabrication
# --------------------------------------------------------------------------- #


class FailClosedDeliveryTest(unittest.TestCase):
    def test_missing_fill_price_never_reaches_callback(self) -> None:
        strategy = _RecordingStrategy()
        bad = SimulatedFill(
            event_type=OrderEventType.FILL,
            sim_order_id="ord-bad",
            client_order_id="cli-bad",
            strategy_id="s1",
            symbol="AAPL",
            fill_price_minor=None,  # missing on an AC-named category
            fill_quantity=10,
            cumulative_filled=10,
            remaining_quantity=0,
            commission_minor=5,
            reason=None,
            simulated_fill_at_ns=time.perf_counter_ns(),
            timestamp="2026-07-03T13:30:00Z",
        )
        with self.assertRaises(OrderEventContractError):
            deliver_simulated_fill(strategy, None, bad)
        self.assertEqual(strategy.received, [])  # callback never invoked

    def test_non_int_minor_money_fails_closed_in_builder(self) -> None:
        bad = SimulatedFill(
            event_type=OrderEventType.FILL,
            sim_order_id="ord-bad",
            client_order_id="cli-bad",
            strategy_id="s1",
            symbol="AAPL",
            fill_price_minor="12345",  # type: ignore[arg-type]  # schema drift
            fill_quantity=10,
            cumulative_filled=10,
            remaining_quantity=0,
            commission_minor=5,
            reason=None,
            simulated_fill_at_ns=time.perf_counter_ns(),
            timestamp="2026-07-03T13:30:00Z",
        )
        with self.assertRaises(OrderEventContractError):
            build_order_event(bad)

    def test_bool_minor_money_is_rejected(self) -> None:
        # bool is an int subclass; a True/False minor value is a schema bug.
        bad = SimulatedFill(
            event_type=OrderEventType.FILL,
            sim_order_id="ord-bad",
            client_order_id="cli-bad",
            strategy_id="s1",
            symbol="AAPL",
            fill_price_minor=True,  # type: ignore[arg-type]
            fill_quantity=10,
            cumulative_filled=10,
            remaining_quantity=0,
            commission_minor=5,
            reason=None,
            simulated_fill_at_ns=time.perf_counter_ns(),
            timestamp="2026-07-03T13:30:00Z",
        )
        with self.assertRaises(OrderEventContractError):
            build_order_event(bad)

    def test_non_descriptor_rejected(self) -> None:
        with self.assertRaises(OrderEventContractError):
            build_order_event({"event_type": "FILL"})  # type: ignore[arg-type]

    def test_bad_event_type_rejected(self) -> None:
        bad = SimulatedFill(
            event_type="FILL",  # type: ignore[arg-type]  # bare string, not the enum
            sim_order_id="ord-bad",
            client_order_id="cli-bad",
            strategy_id="s1",
            symbol="AAPL",
            fill_price_minor=12_345,
            fill_quantity=10,
            cumulative_filled=10,
            remaining_quantity=0,
            commission_minor=5,
            reason=None,
            simulated_fill_at_ns=time.perf_counter_ns(),
            timestamp="2026-07-03T13:30:00Z",
        )
        with self.assertRaises(OrderEventContractError):
            build_order_event(bad)

    def test_sink_without_callback_rejected(self) -> None:
        event = build_order_event(_fill())
        with self.assertRaises(OrderEventContractError):
            deliver_order_event(object(), None, event, fill_at_ns=time.perf_counter_ns())
        with self.assertRaises(OrderEventContractError):
            deliver_order_event(None, None, event, fill_at_ns=time.perf_counter_ns())

    def test_non_int_fill_at_ns_rejected(self) -> None:
        strategy = _RecordingStrategy()
        event = build_order_event(_fill())
        with self.assertRaises(OrderEventContractError):
            deliver_order_event(strategy, None, event, fill_at_ns=1.5)  # type: ignore[arg-type]
        self.assertEqual(strategy.received, [])

    def test_guard_runs_before_user_callback(self) -> None:
        """The seam must delegate to the SDK guard before invoking user code."""
        strategy = _RecordingStrategy()
        event = build_order_event(_fill())
        sentinel = OrderEventContractError("sentinel-guard-invoked")
        with mock.patch(
            "atp_strategy.dispatch.assert_order_event_payload", side_effect=sentinel
        ) as guard:
            with self.assertRaises(OrderEventContractError):
                deliver_order_event(strategy, None, event, fill_at_ns=time.perf_counter_ns())
        guard.assert_called_once_with(event)
        self.assertEqual(strategy.received, [])  # user code not reached


# --------------------------------------------------------------------------- #
# Money conversion round-trip
# --------------------------------------------------------------------------- #


class MinorToFloatConversionTest(unittest.TestCase):
    def test_round_trips_to_nearest_minor_unit(self) -> None:
        for price_minor, comm_minor in (
            (12_345, 5),
            (10_000, 0),
            (99_999, 137),
            (1, 1),
            (250_050, 3_500),
        ):
            fill = SimulatedFill(
                event_type=OrderEventType.FILL,
                sim_order_id="ord-rt",
                client_order_id="cli-rt",
                strategy_id="s1",
                symbol="AAPL",
                fill_price_minor=price_minor,
                fill_quantity=10,
                cumulative_filled=10,
                remaining_quantity=0,
                commission_minor=comm_minor,
                reason=None,
                simulated_fill_at_ns=time.perf_counter_ns(),
                timestamp="2026-07-03T13:30:00Z",
            )
            event = build_order_event(fill)
            assert event.fill_price is not None and event.commission is not None
            self.assertEqual(round(event.fill_price * MINOR_UNITS_PER_UNIT), price_minor)
            self.assertEqual(round(event.commission * MINOR_UNITS_PER_UNIT), comm_minor)

    def test_none_money_passes_through_on_ack(self) -> None:
        ack = SimulatedFill(
            event_type=OrderEventType.ACK,
            sim_order_id="ord-ack",
            client_order_id="cli-ack",
            strategy_id="s1",
            symbol="AAPL",
            fill_price_minor=None,
            fill_quantity=None,
            cumulative_filled=0,
            remaining_quantity=10,
            commission_minor=None,
            reason=None,
            simulated_fill_at_ns=time.perf_counter_ns(),
            timestamp="2026-07-03T13:30:00Z",
        )
        event = build_order_event(ack)
        self.assertIsNone(event.fill_price)
        self.assertIsNone(event.commission)


# --------------------------------------------------------------------------- #
# NFR-P4 paper-leg latency budget
# --------------------------------------------------------------------------- #


def _nfr_p95_cli() -> Path | None:
    """Locate the built ``nfr_p95_cli`` binary, or return ``None`` if absent."""
    for profile in ("release", "debug"):
        candidate = ROOT / "target" / profile / "nfr_p95_cli"
        if candidate.exists():
            return candidate
    return None


class PaperCallbackLatencyBudgetTest(unittest.TestCase):
    SAMPLE_COUNT = 2000

    def _collect_delivery_latencies_ns(self) -> list[int]:
        """Drive the production seam and collect per-delivery latency samples (ns).

        Rotates through all four AC categories so the measurement covers the real
        category mix (each category runs the field-presence guard). ``fill_at_ns``
        is stamped fresh immediately before assemble + deliver so each sample is
        the SDK delivery-seam latency (build + guard + callback), the portion of
        the NFR-P4 paper budget this SDK owns.
        """
        strategy = _RecordingStrategy()
        samples: list[int] = []
        for i in range(self.SAMPLE_COUNT):
            builder = _AC_BUILDERS[i % len(_AC_BUILDERS)]
            fill = builder(f"ord-{i}", at_ns=0)  # placeholder stamp; overwritten below
            t0 = time.perf_counter_ns()
            event = build_order_event(fill)
            sample = deliver_order_event(strategy, None, event, fill_at_ns=t0)
            samples.append(sample)
        self.assertEqual(len(strategy.received), self.SAMPLE_COUNT)
        self.assertTrue(all(s >= 0 for s in samples))
        return samples

    def test_paper_delivery_p95_under_budget_via_authoritative_engine(self) -> None:
        samples = self._collect_delivery_latencies_ns()

        binary = _nfr_p95_cli()
        if binary is None:
            self.skipTest(
                "nfr_p95_cli binary not built (run `cargo build -p atp-types "
                "--bin nfr_p95_cli`); authoritative-engine leg skipped — the "
                "binary-free upper-bound guard below still enforces the budget"
            )
        stdin = "\n".join(str(s) for s in samples)
        result = subprocess.run(
            [str(binary), "paper"],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"nfr_p95_cli exited {result.returncode}\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}",
        )
        verdict_lines = [ln for ln in result.stdout.splitlines() if ln.startswith("nfr:")]
        self.assertEqual(len(verdict_lines), 1, msg=result.stdout)
        self.assertIn("leg:paper", verdict_lines[0])
        self.assertIn("verdict:PASS", verdict_lines[0])

    def test_paper_delivery_max_latency_under_budget(self) -> None:
        """Binary-free guard: the MAX delivery latency (a strict upper bound on
        p95) is under the paper budget, so the budget holds regardless of whether
        the authoritative-engine leg ran. Needs no percentile math."""
        samples = self._collect_delivery_latencies_ns()
        max_ms = max(samples) / 1_000_000.0
        self.assertLess(
            max_ms,
            float(PAPER_CALLBACK_LATENCY_P95_MS),
            msg=f"max paper delivery latency {max_ms:.3f} ms exceeds the "
            f"{PAPER_CALLBACK_LATENCY_P95_MS} ms budget (p95 would too)",
        )


class NfrCliFailClosedTest(unittest.TestCase):
    """The authoritative-engine CLI must refuse bad inputs without a verdict.

    Complements the Rust unit tests on the arg parser (``parse_args`` /
    ``read_samples``) with the integration-level exit-code contract: a refused
    input prints NO ``verdict:`` line and exits non-zero, and a budget breach
    exits non-zero with a ``verdict:FAIL`` — so a regression that let a breach
    read as a pass would fail the gate.
    """

    def setUp(self) -> None:
        self.binary = _nfr_p95_cli()
        if self.binary is None:
            self.skipTest("nfr_p95_cli binary not built")

    def _run(self, cli_args: list[str], stdin: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.binary), *cli_args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_unknown_leg_exits_nonzero_without_verdict(self) -> None:
        result = self._run(["bogus"], "1000000 2000000 3000000")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("verdict:", result.stdout)

    def test_empty_samples_exits_nonzero_without_verdict(self) -> None:
        result = self._run(["paper"], "")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("verdict:", result.stdout)

    def test_unknown_flag_exits_nonzero(self) -> None:
        result = self._run(["paper", "--bogus", "5"], "1000000")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("verdict:", result.stdout)

    def test_partial_ptp_flags_exit_nonzero(self) -> None:
        result = self._run(["paper", "--ptp-offset-ns", "500"], "1000000")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("verdict:", result.stdout)

    def test_budget_breach_exits_nonzero_with_fail_verdict(self) -> None:
        # 200 ms samples exceed the 100 ms paper budget — the verdict is FAIL and
        # the process exits non-zero (a regression that inverted this would let a
        # breach pass the gate).
        stdin = "\n".join(["200000000"] * 200)
        result = self._run(["paper"], stdin)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("verdict:FAIL", result.stdout)


if __name__ == "__main__":
    unittest.main()
