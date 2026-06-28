"""Contract tests for ERR-3 (SRS-MD-004 + SyRS SYS-39a/64/87 + NFR-P5).

Mirrors ``tests/test_connectivity_contract.py``: shells out to
``tools/freshness_check.py``, then exercises each per-check function
in-process, including negative spot-checks that verify the contract
actually catches regressions (forbidden vendor / tick fields, missing
variants, broker calls leaking into the Stale leaf, missing
stale_events.record call, accidentally triggering a reconnect on
stale data).
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

from freshness_check import (  # noqa: E402
    FreshnessCheckError,
    assert_freshness_static,
    check_freshness_guard_in_submit_live_order,
    check_freshness_probe_port,
    check_freshness_state_enum,
    check_stale_data_event_sink_port,
    check_stale_data_event_struct,
    execution_source,
    load_config,
    run_checks,
    types_source,
)


class FreshnessCheckScriptTest(unittest.TestCase):
    def test_err_3_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/freshness_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ERR-3 PASS", result.stdout)
        for needle in (
            "MarketDataFreshness with 2 states",
            "Fresh, Stale",
            "SRS-MD-004 / NFR-P5",
            "StaleDataEvent with the 4 required fields",
            "state, strategy_id, symbol, staleness_seconds",
            "rejects 5 forbidden broker/vendor/tick fields",
            "MarketDataFreshnessProbe with 2 methods",
            "freshness, staleness_seconds",
            "StaleDataEventSink with 1 method",
            "MarketDataFreshness::Fresh inside ConnectivityState::Connected",
            "OrderErrorCategory::MarketDataStale",
            "stale_events.record",
            "zero broker side effect (ERR-3)",
            "err_3_stale_data_blocked",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class MarketDataFreshnessEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_both_states_present(self) -> None:
        evidence = check_freshness_state_enum(self.config, self.types_src)
        for variant in ("Fresh", "Stale"):
            self.assertIn(variant, evidence)

    def test_missing_stale_variant_is_caught(self) -> None:
        mutated = self.types_src.replace("    Stale,", "    StaleX,", 1)
        with self.assertRaises(FreshnessCheckError) as ctx:
            check_freshness_state_enum(self.config, mutated)
        self.assertIn("Stale", str(ctx.exception))

    def test_missing_fresh_variant_is_caught(self) -> None:
        mutated = self.types_src.replace("    Fresh,", "", 1)
        with self.assertRaises(FreshnessCheckError) as ctx:
            check_freshness_state_enum(self.config, mutated)
        self.assertIn("Fresh", str(ctx.exception))


class StaleDataEventStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_the_four_required_fields(self) -> None:
        evidence = check_stale_data_event_struct(self.config, self.types_src)
        for field in ("state", "strategy_id", "symbol", "staleness_seconds"):
            self.assertIn(field, evidence)

    def test_struct_rejects_leaked_broker_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct StaleDataEvent {\n    pub state: MarketDataFreshness,",
            "pub struct StaleDataEvent {\n    pub broker: String,\n    pub state: MarketDataFreshness,",
            1,
        )
        with self.assertRaises(FreshnessCheckError) as ctx:
            check_stale_data_event_struct(self.config, mutated)
        self.assertIn("broker", str(ctx.exception))

    def test_struct_rejects_leaked_tick_id_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct StaleDataEvent {\n    pub state: MarketDataFreshness,",
            "pub struct StaleDataEvent {\n    pub tick_id: u64,\n    pub state: MarketDataFreshness,",
            1,
        )
        with self.assertRaises(FreshnessCheckError) as ctx:
            check_stale_data_event_struct(self.config, mutated)
        self.assertIn("tick_id", str(ctx.exception))

    def test_struct_rejects_leaked_vendor_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct StaleDataEvent {\n    pub state: MarketDataFreshness,",
            "pub struct StaleDataEvent {\n    pub vendor: String,\n    pub state: MarketDataFreshness,",
            1,
        )
        with self.assertRaises(FreshnessCheckError) as ctx:
            check_stale_data_event_struct(self.config, mutated)
        self.assertIn("vendor", str(ctx.exception))

    def test_missing_staleness_seconds_field_is_caught(self) -> None:
        mutated = self.types_src.replace("pub staleness_seconds: u64,", "", 1)
        with self.assertRaises(FreshnessCheckError) as ctx:
            check_stale_data_event_struct(self.config, mutated)
        self.assertIn("staleness_seconds", str(ctx.exception))


class MarketDataFreshnessProbePortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_port_exposes_freshness_and_staleness_seconds(self) -> None:
        evidence = check_freshness_probe_port(self.config, self.exec_src)
        self.assertIn("freshness", evidence)
        self.assertIn("staleness_seconds", evidence)

    def test_missing_freshness_method_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "fn freshness(&self, symbol: &str)",
            "fn dropped_freshness_method(&self, symbol: &str)",
        )
        with self.assertRaises(FreshnessCheckError) as ctx:
            check_freshness_probe_port(self.config, mutated)
        self.assertIn("freshness", str(ctx.exception))


class StaleDataEventSinkPortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_port_exposes_record(self) -> None:
        evidence = check_stale_data_event_sink_port(self.config, self.exec_src)
        self.assertIn("record", evidence)

    def test_missing_record_method_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "fn record(&self, event: StaleDataEvent)",
            "fn dropped_record_method(&self, event: StaleDataEvent)",
        )
        with self.assertRaises(FreshnessCheckError) as ctx:
            check_stale_data_event_sink_port(self.config, mutated)
        self.assertIn("record", str(ctx.exception))


class FreshnessGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_broker_call_is_gated_on_fresh_leaf(self) -> None:
        evidence = check_freshness_guard_in_submit_live_order(self.config, self.exec_src)
        self.assertIn("MarketDataFreshness::Fresh", evidence)
        self.assertIn("ConnectivityState::Connected", evidence)
        self.assertIn("OrderErrorCategory::MarketDataStale", evidence)
        self.assertIn("stale_events.record", evidence)
        self.assertIn("zero broker side effect (ERR-3)", evidence)

    def test_broker_call_inside_stale_leaf_is_caught(self) -> None:
        # Mutate the Stale leaf to call broker.submit_order — the regression
        # the regex check exists to catch.
        mutated = self.exec_src.replace(
            "stale_events.record(StaleDataEvent {",
            "let _ = broker.submit_order(submission.clone()); stale_events.record(StaleDataEvent {",
            1,
        )
        with self.assertRaises(FreshnessCheckError) as ctx:
            check_freshness_guard_in_submit_live_order(self.config, mutated)
        self.assertIn("zero broker side effect", str(ctx.exception))

    def test_missing_stale_event_record_call_is_caught(self) -> None:
        # Strip the whole stale_events.record(StaleDataEvent { ... }); block
        # so the remaining source still parses.
        marker_open = "stale_events.record(StaleDataEvent {"
        start = self.exec_src.find(marker_open)
        self.assertGreaterEqual(
            start, 0, "could not locate stale_events.record(...) in execution source"
        )
        depth = 0
        index = start + len(marker_open) - 1  # position at the `{`
        while index < len(self.exec_src):
            char = self.exec_src[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        end = self.exec_src.find(";", index) + 1
        mutated = self.exec_src[:start] + "/* stale event removed */" + self.exec_src[end:]
        with self.assertRaises(FreshnessCheckError) as ctx:
            check_freshness_guard_in_submit_live_order(self.config, mutated)
        self.assertIn("stale_events.record", str(ctx.exception))

    def test_reconnect_call_in_stale_leaf_is_caught(self) -> None:
        # Staleness is a data-side condition: the Stale leaf must NOT call
        # the connectivity reconnect port. Injecting one must trip the
        # check.
        mutated = self.exec_src.replace(
            "stale_events.record(StaleDataEvent {",
            "connectivity.request_reconnect(); stale_events.record(StaleDataEvent {",
            1,
        )
        with self.assertRaises(FreshnessCheckError) as ctx:
            check_freshness_guard_in_submit_live_order(self.config, mutated)
        self.assertIn("connectivity.request_reconnect", str(ctx.exception))

    def test_missing_freshness_match_is_caught(self) -> None:
        # Rewrite the Connected sub-arm to bypass the freshness probe and
        # go straight to the broker — the regression the nested-match check
        # exists to catch.
        mutated = self.exec_src.replace(
            "ConnectivityState::Connected => match freshness.freshness(&submission.symbol) {",
            "ConnectivityState::Connected => match never_called(&submission.symbol) {",
            1,
        )
        with self.assertRaises(FreshnessCheckError) as ctx:
            check_freshness_guard_in_submit_live_order(self.config, mutated)
        self.assertIn("freshness.freshness", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_six_evidence_items(self) -> None:
        evidence = run_checks()
        # 5 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(evidence), 6)

    def test_assert_freshness_static_emits_five_evidence_items(self) -> None:
        config = load_config()
        evidence = assert_freshness_static(config, ROOT)
        self.assertEqual(len(evidence), 5)


if __name__ == "__main__":
    unittest.main()
