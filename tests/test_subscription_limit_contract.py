"""Contract tests for ERR-4 (SRS-MD-002 + SyRS SYS-70/64 + StRS A-13).

Mirrors ``tests/test_freshness_contract.py``: shells out to
``tools/subscription_limit_check.py``, then exercises each per-check
function in-process, including negative spot-checks that verify the
contract actually catches regressions (forbidden vendor / tick fields,
missing variants, removed ``events.record`` call, registry mutation
sneaking into the ExceededLimit leaf, dropped ``configured_limit``
field, drifted wire string).
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

from subscription_limit_check import (  # noqa: E402
    SubscriptionLimitCheckError,
    assert_subscription_limit_static,
    check_event_sink_port,
    check_line_counter_port,
    check_subscription_limit_event_struct,
    check_subscription_limit_guard,
    check_subscription_limit_state_enum,
    check_subscription_request_struct,
    load_config,
    market_data_source,
    run_checks,
    types_source,
)


class SubscriptionLimitCheckScriptTest(unittest.TestCase):
    def test_err_4_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/subscription_limit_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ERR-4 PASS", result.stdout)
        for needle in (
            "SubscriptionRequest with the 2 required fields",
            "strategy_id, symbol",
            "SubscriptionLimitState with 2 states",
            "WithinLimit, ExceededLimit",
            "SRS-MD-002 / SyRS SYS-70",
            "SubscriptionLimitEvent with the 5 required fields",
            "state, strategy_id, symbol, current_lines, configured_limit",
            "rejects 5 forbidden broker/vendor/tick fields",
            "SubscriptionLineCounter with 3 methods",
            "lines_in_use, line_limit, try_acquire",
            "SubscriptionLimitEventSink with 1 method",
            "SubscriptionLimitState::WithinLimit",
            "OrderErrorCategory::SubscriptionLimitReached",
            "events.record",
            "mutates nothing in the subscription registry (ERR-4)",
            "err_4_subscription_limit_blocked",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class SubscriptionLimitStateEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_both_states_present(self) -> None:
        evidence = check_subscription_limit_state_enum(self.config, self.types_src)
        for variant in ("WithinLimit", "ExceededLimit"):
            self.assertIn(variant, evidence)

    def test_missing_exceeded_variant_is_caught(self) -> None:
        mutated = self.types_src.replace("    ExceededLimit,", "    ExceededLimitX,", 1)
        with self.assertRaises(SubscriptionLimitCheckError) as ctx:
            check_subscription_limit_state_enum(self.config, mutated)
        self.assertIn("ExceededLimit", str(ctx.exception))

    def test_missing_within_variant_is_caught(self) -> None:
        mutated = self.types_src.replace("    WithinLimit,", "", 1)
        with self.assertRaises(SubscriptionLimitCheckError) as ctx:
            check_subscription_limit_state_enum(self.config, mutated)
        self.assertIn("WithinLimit", str(ctx.exception))


class SubscriptionLimitEventStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_the_five_required_fields(self) -> None:
        evidence = check_subscription_limit_event_struct(self.config, self.types_src)
        for field in (
            "state",
            "strategy_id",
            "symbol",
            "current_lines",
            "configured_limit",
        ):
            self.assertIn(field, evidence)

    def test_struct_rejects_leaked_broker_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct SubscriptionLimitEvent {\n    pub state: SubscriptionLimitState,",
            "pub struct SubscriptionLimitEvent {\n    pub broker: String,\n    pub state: SubscriptionLimitState,",
            1,
        )
        with self.assertRaises(SubscriptionLimitCheckError) as ctx:
            check_subscription_limit_event_struct(self.config, mutated)
        self.assertIn("broker", str(ctx.exception))

    def test_struct_rejects_leaked_tick_id_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct SubscriptionLimitEvent {\n    pub state: SubscriptionLimitState,",
            "pub struct SubscriptionLimitEvent {\n    pub tick_id: u64,\n    pub state: SubscriptionLimitState,",
            1,
        )
        with self.assertRaises(SubscriptionLimitCheckError) as ctx:
            check_subscription_limit_event_struct(self.config, mutated)
        self.assertIn("tick_id", str(ctx.exception))

    def test_struct_rejects_leaked_vendor_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct SubscriptionLimitEvent {\n    pub state: SubscriptionLimitState,",
            "pub struct SubscriptionLimitEvent {\n    pub vendor: String,\n    pub state: SubscriptionLimitState,",
            1,
        )
        with self.assertRaises(SubscriptionLimitCheckError) as ctx:
            check_subscription_limit_event_struct(self.config, mutated)
        self.assertIn("vendor", str(ctx.exception))

    def test_missing_configured_limit_field_is_caught(self) -> None:
        mutated = self.types_src.replace("pub configured_limit: u32,", "", 1)
        with self.assertRaises(SubscriptionLimitCheckError) as ctx:
            check_subscription_limit_event_struct(self.config, mutated)
        self.assertIn("configured_limit", str(ctx.exception))

    def test_missing_current_lines_field_is_caught(self) -> None:
        mutated = self.types_src.replace("pub current_lines: u32,", "", 1)
        with self.assertRaises(SubscriptionLimitCheckError) as ctx:
            check_subscription_limit_event_struct(self.config, mutated)
        self.assertIn("current_lines", str(ctx.exception))


class SubscriptionRequestStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_request_carries_strategy_id_and_symbol(self) -> None:
        evidence = check_subscription_request_struct(self.config, self.types_src)
        self.assertIn("strategy_id", evidence)
        self.assertIn("symbol", evidence)


class SubscriptionLineCounterPortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.market_data_src = market_data_source(self.config)

    def test_port_exposes_three_methods(self) -> None:
        evidence = check_line_counter_port(self.config, self.market_data_src)
        for method in ("lines_in_use", "line_limit", "try_acquire"):
            self.assertIn(method, evidence)

    def test_missing_try_acquire_method_is_caught(self) -> None:
        mutated = self.market_data_src.replace(
            "fn try_acquire(",
            "fn dropped_try_acquire(",
        )
        with self.assertRaises(SubscriptionLimitCheckError) as ctx:
            check_line_counter_port(self.config, mutated)
        self.assertIn("try_acquire", str(ctx.exception))


class SubscriptionLimitEventSinkPortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.market_data_src = market_data_source(self.config)

    def test_port_exposes_record(self) -> None:
        evidence = check_event_sink_port(self.config, self.market_data_src)
        self.assertIn("record", evidence)

    def test_missing_record_method_is_caught(self) -> None:
        mutated = self.market_data_src.replace(
            "fn record(&self, event: SubscriptionLimitEvent)",
            "fn dropped_record_method(&self, event: SubscriptionLimitEvent)",
        )
        with self.assertRaises(SubscriptionLimitCheckError) as ctx:
            check_event_sink_port(self.config, mutated)
        self.assertIn("record", str(ctx.exception))


class SubscriptionLimitGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.market_data_src = market_data_source(self.config)

    def test_acceptance_is_gated_on_within_limit_leaf(self) -> None:
        evidence = check_subscription_limit_guard(self.config, self.market_data_src)
        self.assertIn("SubscriptionLimitState::WithinLimit", evidence)
        self.assertIn("SubscriptionLimitState::ExceededLimit", evidence)
        self.assertIn("OrderErrorCategory::SubscriptionLimitReached", evidence)
        self.assertIn("events.record", evidence)
        self.assertIn("mutates nothing in the subscription registry", evidence)

    def test_missing_events_record_call_is_caught(self) -> None:
        # Strip the `events.record(SubscriptionLimitEvent { ... });`
        # block so the remaining source still parses.
        marker_open = "events.record(SubscriptionLimitEvent {"
        start = self.market_data_src.find(marker_open)
        self.assertGreaterEqual(
            start,
            0,
            "could not locate events.record(...) in market-data source",
        )
        depth = 0
        index = start + len(marker_open) - 1
        while index < len(self.market_data_src):
            char = self.market_data_src[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        end = self.market_data_src.find(";", index) + 1
        mutated = (
            self.market_data_src[:start] + "/* event record removed */" + self.market_data_src[end:]
        )
        with self.assertRaises(SubscriptionLimitCheckError) as ctx:
            check_subscription_limit_guard(self.config, mutated)
        self.assertIn("events.record", str(ctx.exception))

    def test_registry_mutation_in_exceeded_leaf_is_caught(self) -> None:
        # Inject a registry.insert(...) call into the ExceededLimit leaf
        # — the regression the forbidden_mutations list exists to catch.
        mutated = self.market_data_src.replace(
            "events.record(SubscriptionLimitEvent {",
            "registry.insert(); events.record(SubscriptionLimitEvent {",
            1,
        )
        with self.assertRaises(SubscriptionLimitCheckError) as ctx:
            check_subscription_limit_guard(self.config, mutated)
        self.assertIn("registry.insert", str(ctx.exception))

    def test_acceptance_in_exceeded_leaf_is_caught(self) -> None:
        # Inject a SubscriptionAccepted { ... } construction inside the
        # ExceededLimit leaf — the regression the "ExceededLimit must
        # not produce SubscriptionAccepted" rule exists to catch.
        mutated = self.market_data_src.replace(
            "events.record(SubscriptionLimitEvent {",
            "let _smuggled = SubscriptionAccepted { strategy_id: request.strategy_id.clone(), symbol: request.symbol.clone() }; events.record(SubscriptionLimitEvent {",
            1,
        )
        with self.assertRaises(SubscriptionLimitCheckError) as ctx:
            check_subscription_limit_guard(self.config, mutated)
        self.assertIn("zero acceptance side effect", str(ctx.exception))

    def test_missing_try_acquire_call_is_caught(self) -> None:
        # Rewrite the match scrutinee so the gate stops calling the
        # counter — the regression the counter-call check exists to
        # catch.
        mutated = self.market_data_src.replace(
            "match counter.try_acquire(&request)",
            "match never_called(&request)",
            1,
        )
        with self.assertRaises(SubscriptionLimitCheckError) as ctx:
            check_subscription_limit_guard(self.config, mutated)
        self.assertIn("counter.try_acquire", str(ctx.exception))


class SubscriptionLimitWireStringTest(unittest.TestCase):
    """Wire-string drift is caught by the existing atp-types unit test
    `order_error_category_wire_strings_track_syrs_sys_64`. This test
    exercises the cross-crate linkage by spot-checking that the
    `OrderErrorCategory::SubscriptionLimitReached` variant is reachable
    from the atp-market-data crate's source (which the
    `subscription_limit_guard` static check pins through the factory
    call)."""

    def setUp(self) -> None:
        self.config = load_config()
        self.market_data_src = market_data_source(self.config)

    def test_market_data_crate_references_canonical_wire_string_source(self) -> None:
        self.assertIn(
            "OrderErrorCategory::SubscriptionLimitReached",
            self.market_data_src,
            "atp-market-data must reference the canonical wire-string variant",
        )


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_seven_evidence_items(self) -> None:
        evidence = run_checks()
        # 6 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(evidence), 7)

    def test_assert_subscription_limit_static_emits_six_evidence_items(self) -> None:
        config = load_config()
        evidence = assert_subscription_limit_static(config, ROOT)
        self.assertEqual(len(evidence), 6)


if __name__ == "__main__":
    unittest.main()
