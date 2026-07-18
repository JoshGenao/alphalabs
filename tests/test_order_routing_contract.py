"""Contract tests for SRS-EXE-002 (SyRS SYS-2b / SYS-2e, AC-10; StRS SN-1.06 /
SN-1.29 / C-11).

Mirrors ``tests/test_live_designation_contract.py``: shells out to
``tools/order_routing_check.py``, then exercises each per-check function
in-process, including negative spot-checks that verify the contract actually
catches regressions of the AC-10 safety invariant (a non-live strategy routed
to the live broker, a simulation-arm dispatch that touches an IB port, a
live-arm dispatch that touches the simulation port, a ``SimulatedOrderReceipt``
that carries a ``broker_order_id``, and ``atp-execution`` taking a dependency on
``atp-simulation``).
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from order_routing_check import (  # noqa: E402
    OrderRoutingCheckError,
    assert_order_routing_static,
    check_dependency_direction,
    check_dispatch_guard,
    check_route_destination,
    check_route_enum,
    check_routing_receipt,
    check_simulated_receipt,
    check_simulation_port,
    execution_source,
    load_config,
    manifest_source,
    run_checks,
)


class OrderRoutingScriptTest(unittest.TestCase):
    def test_srs_exe_002_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/order_routing_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-EXE-002 ROUTING-AUTHORITY PASS", result.stdout)
        for needle in (
            "OrderRoute with EXACTLY 2 destinations (LiveBrokerage, InternalSimulation)",
            "SimulatedOrderReceipt with `sim_order_id` and NOT `broker_order_id`",
            "OrderRoutingReceipt with 2 outcomes (Live, Simulated)",
            "InternalSimulationSubmit port (submit_simulated -> SimulatedOrderReceipt)",
            "route_destination maps the engine-owned LiveRoutingDecision authority",
            "NotDesignated -> OrderRoute::InternalSimulation",
            "a non-live strategy can never route to the broker (AC-10)",
            "dispatch_order routes on `self.route_destination`",
            "does NOT depend on `atp-simulation`",
            "cargo test -p atp-execution --lib order_routing",
            "srs_exe_002_order_routing",
            "srs_exe_002_routing_wiring",
            "exe002_order_routing_cli",
            "Deferred with named owners",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class RouteEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_both_destinations_present(self) -> None:
        evidence = check_route_enum(self.config, self.exec_src)
        self.assertIn("LiveBrokerage", evidence)
        self.assertIn("InternalSimulation", evidence)

    def test_missing_internal_simulation_variant_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "    /// Every non-designated (non-live) strategy — its orders route to the\n"
            "    /// internal simulation engine and never create an IB order (AC-10).\n"
            "    InternalSimulation,\n",
            "",
            1,
        )
        with self.assertRaises(OrderRoutingCheckError) as ctx:
            check_route_enum(self.config, mutated)
        self.assertIn("InternalSimulation", str(ctx.exception))

    def test_extra_unreviewed_route_variant_is_caught(self) -> None:
        # An unreviewed third destination (e.g. one that submits straight to the
        # broker) must not be declarable past the safety gate.
        mutated = self.exec_src.replace(
            "    InternalSimulation,\n}",
            "    InternalSimulation,\n    DirectIbBypass,\n}",
            1,
        )
        with self.assertRaises(OrderRoutingCheckError) as ctx:
            check_route_enum(self.config, mutated)
        self.assertIn("DirectIbBypass", str(ctx.exception))


class SimulatedReceiptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_distinct_from_order_receipt(self) -> None:
        evidence = check_simulated_receipt(self.config, self.exec_src)
        self.assertIn("sim_order_id", evidence)
        self.assertIn("broker_order_id", evidence)

    def test_broker_order_id_on_simulated_receipt_is_caught(self) -> None:
        # Adding a broker_order_id to the simulated receipt would let a paper
        # order carry a broker order id — the type must stay distinct.
        mutated = self.exec_src.replace(
            "    pub sim_order_id: String,\n",
            "    pub sim_order_id: String,\n    pub broker_order_id: String,\n",
            1,
        )
        with self.assertRaises(OrderRoutingCheckError) as ctx:
            check_simulated_receipt(self.config, mutated)
        self.assertIn("broker_order_id", str(ctx.exception))


class RoutingReceiptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_both_outcomes_present(self) -> None:
        evidence = check_routing_receipt(self.config, self.exec_src)
        self.assertIn("Live", evidence)
        self.assertIn("Simulated", evidence)


class SimulationPortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_port_present(self) -> None:
        evidence = check_simulation_port(self.config, self.exec_src)
        self.assertIn("InternalSimulationSubmit", evidence)
        self.assertIn("submit_simulated", evidence)

    def test_missing_submit_method_is_caught(self) -> None:
        mutated = self.exec_src.replace("fn submit_simulated(", "fn submit_somethingelse(")
        with self.assertRaises(OrderRoutingCheckError) as ctx:
            check_simulation_port(self.config, mutated)
        self.assertIn("submit_simulated", str(ctx.exception))


class RouteDestinationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_authority_mapping(self) -> None:
        evidence = check_route_destination(self.config, self.exec_src)
        self.assertIn("LiveRoutingDecision", evidence)
        self.assertIn("OrderRoute::InternalSimulation", evidence)

    def test_non_live_routed_to_broker_is_caught(self) -> None:
        # THE AC-10 safety regression: mapping NotDesignated (non-live) to the
        # live broker instead of the simulation engine MUST be caught.
        mutated = self.exec_src.replace(
            "LiveRoutingDecision::NotDesignated => OrderRoute::InternalSimulation,",
            "LiveRoutingDecision::NotDesignated => OrderRoute::LiveBrokerage,",
            1,
        )
        with self.assertRaises(OrderRoutingCheckError) as ctx:
            check_route_destination(self.config, mutated)
        self.assertIn("InternalSimulation", str(ctx.exception))

    def test_missing_authority_call_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "self.designation.authority_for(strategy_id)",
            "self.never_resolves_authority(strategy_id)",
            1,
        )
        with self.assertRaises(OrderRoutingCheckError) as ctx:
            check_route_destination(self.config, mutated)
        self.assertIn("authority_for", str(ctx.exception))


class DispatchGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_guard_routes_on_destination_and_delegates(self) -> None:
        evidence = check_dispatch_guard(self.config, self.exec_src)
        self.assertIn("self.route_destination", evidence)
        self.assertIn(".route_order", evidence)
        self.assertIn(".submit_simulated", evidence)

    def test_simulation_arm_touching_ib_port_is_caught(self) -> None:
        # Smuggle a broker call into the InternalSimulation arm — a non-live
        # dispatch must consult no IB port.
        mutated = self.exec_src.replace(
            ".submit_simulated(submission)",
            ".submit_order(submission).submit_simulated(submission)",
            1,
        )
        with self.assertRaises(OrderRoutingCheckError) as ctx:
            check_dispatch_guard(self.config, mutated)
        self.assertIn("submit_order", str(ctx.exception))

    def test_live_arm_touching_simulation_port_is_caught(self) -> None:
        # Smuggle a simulation call into the LiveBrokerage arm — the live
        # dispatch must not touch the simulation port.
        mutated = self.exec_src.replace(
            ".map(OrderRoutingReceipt::Live)",
            ".submit_simulated(submission).map(OrderRoutingReceipt::Live)",
            1,
        )
        with self.assertRaises(OrderRoutingCheckError) as ctx:
            check_dispatch_guard(self.config, mutated)
        self.assertIn("submit_simulated", str(ctx.exception))

    def test_extra_unreviewed_dispatch_arm_is_caught(self) -> None:
        # An extra dispatch arm that routes straight to the broker must not
        # escape inspection (it would never be checked for forbidden ports).
        mutated = self.exec_src.replace(
            "OrderRoute::InternalSimulation => simulation",
            "OrderRoute::DirectIbBypass => broker.submit_order(submission).map(OrderRoutingReceipt::Live),\n"
            "            OrderRoute::InternalSimulation => simulation",
            1,
        )
        with self.assertRaises(OrderRoutingCheckError) as ctx:
            check_dispatch_guard(self.config, mutated)
        self.assertIn("DirectIbBypass", str(ctx.exception))


class DependencyDirectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.manifest_src = manifest_source(self.config)

    def test_no_simulation_dependency(self) -> None:
        evidence = check_dependency_direction(self.config, self.manifest_src)
        self.assertIn("atp-simulation", evidence)

    def test_simulation_dependency_is_caught(self) -> None:
        mutated = self.manifest_src.replace(
            "[dependencies]\n",
            '[dependencies]\natp-simulation = { path = "../atp-simulation" }\n',
            1,
        )
        with self.assertRaises(OrderRoutingCheckError) as ctx:
            check_dependency_direction(self.config, mutated)
        self.assertIn("atp-simulation", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_eight_evidence_items(self) -> None:
        evidence = run_checks()
        # 7 static + 1 cargo smoke.
        self.assertEqual(len(evidence), 8)

    def test_assert_order_routing_static_emits_seven_evidence_items(self) -> None:
        config = load_config()
        evidence = assert_order_routing_static(config, ROOT)
        self.assertEqual(len(evidence), 7)


if __name__ == "__main__":
    unittest.main()
