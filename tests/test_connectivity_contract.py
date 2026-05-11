"""Contract tests for ERR-2 (SRS-SAFE-003 + SRS-MD-005 + SyRS SYS-45/46/NFR-R2).

Mirrors ``tests/test_error_handling_contract.py``: shells out to
``tools/connectivity_check.py``, then exercises each per-check function
in-process, including negative spot-checks that verify the contract
actually catches regressions (forbidden vendor fields, missing variants,
broker calls leaking into the blocked branch, missing reconnect /
event-record calls).
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

from connectivity_check import (  # noqa: E402
    ConnectivityCheckError,
    assert_connectivity_static,
    check_brokerage_connectivity_port,
    check_connectivity_event_sink_port,
    check_connectivity_event_struct,
    check_connectivity_guard_in_submit_live_order,
    check_connectivity_state_enum,
    execution_source,
    load_config,
    run_checks,
    types_source,
)


class ConnectivityCheckScriptTest(unittest.TestCase):
    def test_err_2_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/connectivity_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ERR-2 PASS", result.stdout)
        for needle in (
            "ConnectivityState with 3 states",
            "Connected, Unreachable, ScheduledRestartWindow",
            "SRS-SAFE-003 / SRS-MD-005",
            "ConnectivityEvent with the 4 required fields",
            "state, strategy_id, symbol, scheduled_restart",
            "rejects 4 forbidden broker/vendor fields",
            "BrokerageConnectivity with 2 methods",
            "state, request_reconnect",
            "ConnectivityEventSink with 1 method",
            "ConnectivityState::Connected",
            "OrderErrorCategory::ConnectivityBlocked",
            "connectivity.request_reconnect",
            "zero broker side effect (ERR-2)",
            "err_2_connectivity_blocked",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class ConnectivityStateEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_all_three_states_present(self) -> None:
        evidence = check_connectivity_state_enum(self.config, self.types_src)
        for variant in ("Connected", "Unreachable", "ScheduledRestartWindow"):
            self.assertIn(variant, evidence)

    def test_missing_unreachable_variant_is_caught(self) -> None:
        mutated = self.types_src.replace("Unreachable,", "UnreachableX,", 1)
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_connectivity_state_enum(self.config, mutated)
        self.assertIn("Unreachable", str(ctx.exception))

    def test_missing_scheduled_restart_variant_is_caught(self) -> None:
        mutated = self.types_src.replace("ScheduledRestartWindow,", "", 1)
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_connectivity_state_enum(self.config, mutated)
        self.assertIn("ScheduledRestartWindow", str(ctx.exception))


class ConnectivityEventStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_the_four_required_fields(self) -> None:
        evidence = check_connectivity_event_struct(self.config, self.types_src)
        for field in ("state", "strategy_id", "symbol", "scheduled_restart"):
            self.assertIn(field, evidence)

    def test_struct_rejects_leaked_broker_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct ConnectivityEvent {\n    pub state: ConnectivityState,",
            "pub struct ConnectivityEvent {\n    pub broker: String,\n    pub state: ConnectivityState,",
            1,
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_connectivity_event_struct(self.config, mutated)
        self.assertIn("broker", str(ctx.exception))

    def test_struct_rejects_leaked_session_id_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct ConnectivityEvent {\n    pub state: ConnectivityState,",
            "pub struct ConnectivityEvent {\n    pub ib_session_id: String,\n    pub state: ConnectivityState,",
            1,
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_connectivity_event_struct(self.config, mutated)
        self.assertIn("ib_session_id", str(ctx.exception))

    def test_missing_scheduled_restart_field_is_caught(self) -> None:
        mutated = self.types_src.replace("pub scheduled_restart: bool,", "", 1)
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_connectivity_event_struct(self.config, mutated)
        self.assertIn("scheduled_restart", str(ctx.exception))


class BrokerageConnectivityPortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_port_exposes_state_and_request_reconnect(self) -> None:
        evidence = check_brokerage_connectivity_port(self.config, self.exec_src)
        self.assertIn("state", evidence)
        self.assertIn("request_reconnect", evidence)

    def test_missing_request_reconnect_is_caught(self) -> None:
        mutated = self.exec_src.replace("fn request_reconnect", "fn dropped_reconnect_method")
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_brokerage_connectivity_port(self.config, mutated)
        self.assertIn("request_reconnect", str(ctx.exception))


class ConnectivityEventSinkPortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_port_exposes_record(self) -> None:
        evidence = check_connectivity_event_sink_port(self.config, self.exec_src)
        self.assertIn("record", evidence)

    def test_missing_record_method_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "fn record(&self, event: ConnectivityEvent)",
            "fn dropped_record_method(&self, event: ConnectivityEvent)",
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_connectivity_event_sink_port(self.config, mutated)
        self.assertIn("record", str(ctx.exception))


class ConnectivityGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_broker_call_is_gated_on_connected_state(self) -> None:
        evidence = check_connectivity_guard_in_submit_live_order(
            self.config, self.exec_src
        )
        self.assertIn("ConnectivityState::Connected", evidence)
        self.assertIn("OrderErrorCategory::ConnectivityBlocked", evidence)
        self.assertIn("connectivity.request_reconnect", evidence)
        self.assertIn("zero broker side effect (ERR-2)", evidence)

    def test_broker_call_inside_blocked_branch_is_caught(self) -> None:
        # Mutate the blocked branch to call broker.submit_order — the
        # regression the regex check exists to catch.
        mutated = self.exec_src.replace(
            "connectivity.request_reconnect();",
            "let _ = broker.submit_order(submission.clone()); connectivity.request_reconnect();",
            1,
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_connectivity_guard_in_submit_live_order(self.config, mutated)
        self.assertIn("zero broker side effect", str(ctx.exception))

    def test_missing_reconnect_call_in_blocked_branch_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "connectivity.request_reconnect();", "/* reconnect removed */", 1
        )
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_connectivity_guard_in_submit_live_order(self.config, mutated)
        self.assertIn("request_reconnect", str(ctx.exception))

    def test_missing_event_record_call_in_blocked_branch_is_caught(self) -> None:
        # Strip the whole events.record(ConnectivityEvent { ... }); block so
        # the remaining source still parses.
        marker_open = "events.record(ConnectivityEvent {"
        start = self.exec_src.find(marker_open)
        self.assertGreaterEqual(start, 0, "could not locate events.record(...) in execution source")
        depth = 0
        index = start + len(marker_open) - 1  # position at the `{`
        # Walk to the matching closing brace of the struct literal.
        while index < len(self.exec_src):
            char = self.exec_src[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        # After the closing brace expect `);` to terminate the call.
        end = self.exec_src.find(";", index) + 1
        mutated = self.exec_src[:start] + "/* event removed */" + self.exec_src[end:]
        with self.assertRaises(ConnectivityCheckError) as ctx:
            check_connectivity_guard_in_submit_live_order(self.config, mutated)
        self.assertIn("events.record", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_six_evidence_items(self) -> None:
        evidence = run_checks()
        # 5 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(evidence), 6)

    def test_assert_connectivity_static_emits_five_evidence_items(self) -> None:
        config = load_config()
        evidence = assert_connectivity_static(config, ROOT)
        self.assertEqual(len(evidence), 5)


if __name__ == "__main__":
    unittest.main()
