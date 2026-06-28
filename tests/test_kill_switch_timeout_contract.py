"""Contract tests for ERR-8 (SRS-SAFE-002 + SyRS SYS-44b + StRS SN-1.11).

Mirrors ``tests/test_hot_swap_demotion_contract.py``: shells out to
``tools/kill_switch_timeout_check.py``, then exercises each per-check
function in-process, including negative spot-checks that verify the contract
actually catches regressions — forbidden broker/IB-order/vendor fields,
missing enum variants, missing port methods, a dropped page/cancel/disconnect
in the timeout arm, a page/cancel/disconnect leaking into the filled arm, an
acceptance struct sneaking into the timeout arm, a missing operator-alert
channel, and a broken probe call.
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

from kill_switch_timeout_check import (  # noqa: E402
    KillSwitchTimeoutCheckError,
    assert_kill_switch_timeout_static,
    check_alert_event_struct,
    check_ib_cleanup_port,
    check_liquidation_outcome_enum,
    check_liquidation_probe_port,
    check_operator_alert_sink_port,
    check_resolve_kill_switch_timeout_guard,
    check_timeout_event_sink_port,
    check_timeout_event_struct,
    check_timeout_request_struct,
    check_unfilled_order_struct,
    execution_source,
    load_config,
    run_checks,
    types_source,
)


class KillSwitchTimeoutScriptTest(unittest.TestCase):
    def test_err_8_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/kill_switch_timeout_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ERR-8 PASS", result.stdout)
        for needle in (
            "KillSwitchTimeoutRequest with the 3 required fields",
            "live_strategy_id, unfilled_order, timeout_seconds",
            "UnfilledLiquidationOrder with the 4 required fields",
            "order_id, symbol, side, quantity",
            "KillSwitchLiquidationOutcome with 2 required variant(s) (FilledBeforeTimeout, TimedOutUnfilled)",
            "OperatorAlertChannel with 2 required variant(s) (Email, Sms)",
            "KillSwitchAlertEvent with the 6 required fields",
            "SideEffectOutcome with 3 required variant(s) (NotAttempted, Succeeded, Failed)",
            "KillSwitchTimeoutEvent with the 8 required fields",
            "manual_resolution_required",
            "KillSwitchLiquidationProbe with 1 method(s) (await_filled_or_timeout)",
            "KillSwitchOperatorAlertSink with 1 method(s) (dispatch)",
            "IbLiquidationCleanup with 2 method(s) (cancel_unfilled_liquidation_order, disconnect)",
            "KillSwitchTimeoutEventSink with 1 method(s) (record)",
            "KillSwitchLiquidationOutcome::FilledBeforeTimeout arm is the sole",
            "KillSwitchLiquidationOutcome::TimedOutUnfilled arm pages via",
            "OrderErrorCategory::KillSwitchLiquidationTimeout",
            "err_8_kill_switch_liquidation_timeout",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class KillSwitchTimeoutRequestStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_required_fields(self) -> None:
        evidence = check_timeout_request_struct(self.config, self.types_src)
        for field in ("live_strategy_id", "unfilled_order", "timeout_seconds"):
            self.assertIn(field, evidence)

    def test_missing_live_strategy_id_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub struct KillSwitchTimeoutRequest {\n    pub live_strategy_id: StrategyId,",
            "pub struct KillSwitchTimeoutRequest {\n",
            1,
        )
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_timeout_request_struct(self.config, mutated)
        self.assertIn("live_strategy_id", str(ctx.exception))

    def test_struct_rejects_leaked_ib_order_id_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct KillSwitchTimeoutRequest {\n    pub live_strategy_id: StrategyId,",
            "pub struct KillSwitchTimeoutRequest {\n    pub ib_order_id: String,\n"
            "    pub live_strategy_id: StrategyId,",
            1,
        )
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_timeout_request_struct(self.config, mutated)
        self.assertIn("ib_order_id", str(ctx.exception))


class UnfilledLiquidationOrderStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_required_fields(self) -> None:
        evidence = check_unfilled_order_struct(self.config, self.types_src)
        for field in ("order_id", "symbol", "side", "quantity"):
            self.assertIn(field, evidence)

    def test_struct_rejects_leaked_ib_order_id_field(self) -> None:
        # The "log unfilled order details" payload must use a domain order_id,
        # NOT the vendor ib_order_id.
        mutated = self.types_src.replace(
            "pub struct UnfilledLiquidationOrder {\n    pub order_id: String,",
            "pub struct UnfilledLiquidationOrder {\n    pub ib_order_id: String,\n"
            "    pub order_id: String,",
            1,
        )
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_unfilled_order_struct(self.config, mutated)
        self.assertIn("ib_order_id", str(ctx.exception))


class KillSwitchLiquidationOutcomeEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_both_variants_present(self) -> None:
        evidence = check_liquidation_outcome_enum(self.config, self.types_src)
        for variant in ("FilledBeforeTimeout", "TimedOutUnfilled"):
            self.assertIn(variant, evidence)

    def test_missing_filled_variant_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "    FilledBeforeTimeout {",
            "    FilledBeforeTimeoutX {",
            1,
        )
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_liquidation_outcome_enum(self.config, mutated)
        self.assertIn("FilledBeforeTimeout", str(ctx.exception))

    def test_missing_timeout_variant_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "    TimedOutUnfilled {",
            "    TimedOutUnfilledX {",
            1,
        )
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_liquidation_outcome_enum(self.config, mutated)
        self.assertIn("TimedOutUnfilled", str(ctx.exception))


class KillSwitchAlertEventStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_required_fields(self) -> None:
        evidence = check_alert_event_struct(self.config, self.types_src)
        for field in ("channels", "elapsed_seconds", "timeout_seconds", "observed_at_seconds"):
            self.assertIn(field, evidence)

    def test_struct_rejects_leaked_vendor_credentials_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct KillSwitchAlertEvent {\n    pub live_strategy_id: StrategyId,",
            "pub struct KillSwitchAlertEvent {\n    pub vendor_credentials: String,\n"
            "    pub live_strategy_id: StrategyId,",
            1,
        )
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_alert_event_struct(self.config, mutated)
        self.assertIn("vendor_credentials", str(ctx.exception))


class KillSwitchTimeoutEventStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_required_fields(self) -> None:
        evidence = check_timeout_event_struct(self.config, self.types_src)
        for field in (
            "outcome",
            "manual_resolution_required",
            "operator_alert",
            "liquidation_cancel",
            "ib_disconnect",
        ):
            self.assertIn(field, evidence)

    def test_missing_ib_disconnect_field_is_caught(self) -> None:
        mutated = self.types_src.replace("    pub ib_disconnect: SideEffectOutcome,", "", 1)
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_timeout_event_struct(self.config, mutated)
        self.assertIn("ib_disconnect", str(ctx.exception))

    def test_missing_manual_resolution_required_field_is_caught(self) -> None:
        mutated = self.types_src.replace("    pub manual_resolution_required: bool,", "", 1)
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_timeout_event_struct(self.config, mutated)
        self.assertIn("manual_resolution_required", str(ctx.exception))

    def test_struct_rejects_leaked_container_id_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct KillSwitchTimeoutEvent {\n    pub outcome: KillSwitchLiquidationOutcome,",
            "pub struct KillSwitchTimeoutEvent {\n    pub container_id: String,\n"
            "    pub outcome: KillSwitchLiquidationOutcome,",
            1,
        )
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_timeout_event_struct(self.config, mutated)
        self.assertIn("container_id", str(ctx.exception))


class KillSwitchTimeoutPortsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_probe_port_exposes_await_filled_or_timeout(self) -> None:
        evidence = check_liquidation_probe_port(self.config, self.exec_src)
        self.assertIn("await_filled_or_timeout", evidence)

    def test_missing_probe_method_is_caught(self) -> None:
        mutated = self.exec_src.replace("fn await_filled_or_timeout(", "fn dropped_await(", 1)
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_liquidation_probe_port(self.config, mutated)
        self.assertIn("await_filled_or_timeout", str(ctx.exception))

    def test_missing_alert_sink_dispatch_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "fn dispatch(&self, event: KillSwitchAlertEvent)",
            "fn dropped_dispatch(&self, event: KillSwitchAlertEvent)",
            1,
        )
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_operator_alert_sink_port(self.config, mutated)
        self.assertIn("dispatch", str(ctx.exception))

    def test_missing_cleanup_cancel_method_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "fn cancel_unfilled_liquidation_order(", "fn dropped_cancel(", 1
        )
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_ib_cleanup_port(self.config, mutated)
        self.assertIn("cancel_unfilled_liquidation_order", str(ctx.exception))

    def test_missing_cleanup_disconnect_method_is_caught(self) -> None:
        mutated = self.exec_src.replace("fn disconnect(&self)", "fn dropped_disconnect(&self)", 1)
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_ib_cleanup_port(self.config, mutated)
        self.assertIn("disconnect", str(ctx.exception))

    def test_missing_timeout_event_sink_record_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "fn record(&self, event: KillSwitchTimeoutEvent)",
            "fn dropped_record(&self, event: KillSwitchTimeoutEvent)",
            1,
        )
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_timeout_event_sink_port(self.config, mutated)
        self.assertIn("record", str(ctx.exception))


class ResolveKillSwitchTimeoutGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_guard_summary_pins_both_arms(self) -> None:
        evidence = check_resolve_kill_switch_timeout_guard(self.config, self.exec_src)
        self.assertIn("KillSwitchLiquidationOutcome::FilledBeforeTimeout", evidence)
        self.assertIn("KillSwitchLiquidationOutcome::TimedOutUnfilled", evidence)
        self.assertIn("OrderErrorCategory::KillSwitchLiquidationTimeout", evidence)
        self.assertIn("disconnects via", evidence)

    def test_broken_probe_call_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "liquidation.await_filled_or_timeout(&request)", "never_called(&request)", 1
        )
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_resolve_kill_switch_timeout_guard(self.config, mutated)
        self.assertIn("liquidation.await_filled_or_timeout", str(ctx.exception))

    def test_dropped_probe_error_handling_is_caught(self) -> None:
        # A probe failure must fail closed via the distinct probe-unavailable
        # refusal; dropping that factory call must be caught.
        mutated = self.exec_src.replace(
            "StructuredKillSwitchTimeoutError::probe_unavailable", "silently_ignore", 1
        )
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_resolve_kill_switch_timeout_guard(self.config, mutated)
        self.assertIn("probe_unavailable", str(ctx.exception))

    def test_page_leaking_into_filled_arm_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "                Ok(KillSwitchLiquidationResolved {",
            "                alerts.dispatch(noop);\n"
            "                Ok(KillSwitchLiquidationResolved {",
            1,
        )
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_resolve_kill_switch_timeout_guard(self.config, mutated)
        self.assertIn("alerts.dispatch", str(ctx.exception))

    def test_cancel_leaking_into_filled_arm_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "                Ok(KillSwitchLiquidationResolved {",
            "                cleanup.cancel_unfilled_liquidation_order(&request);\n"
            "                Ok(KillSwitchLiquidationResolved {",
            1,
        )
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_resolve_kill_switch_timeout_guard(self.config, mutated)
        self.assertIn("cancel_unfilled_liquidation_order", str(ctx.exception))

    def test_disconnect_leaking_into_filled_arm_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "                Ok(KillSwitchLiquidationResolved {",
            "                cleanup.disconnect();\n"
            "                Ok(KillSwitchLiquidationResolved {",
            1,
        )
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_resolve_kill_switch_timeout_guard(self.config, mutated)
        self.assertIn("cleanup.disconnect", str(ctx.exception))

    def test_missing_filled_arm_acceptance_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "                Ok(KillSwitchLiquidationResolved {",
            "                Ok(SomethingElse {",
            1,
        )
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_resolve_kill_switch_timeout_guard(self.config, mutated)
        self.assertIn("KillSwitchLiquidationResolved", str(ctx.exception))

    def test_dropped_cancel_in_timeout_arm_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "cleanup.cancel_unfilled_liquidation_order(&request)", "dropped_cancel(&request)", 1
        )
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_resolve_kill_switch_timeout_guard(self.config, mutated)
        self.assertIn("cancel_unfilled_liquidation_order", str(ctx.exception))

    def test_dropped_disconnect_in_timeout_arm_is_caught(self) -> None:
        mutated = self.exec_src.replace("cleanup.disconnect()", "dropped_disconnect()", 1)
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_resolve_kill_switch_timeout_guard(self.config, mutated)
        self.assertIn("cleanup.disconnect", str(ctx.exception))

    def test_dropped_page_in_timeout_arm_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "alerts.dispatch(KillSwitchAlertEvent {", "noop_dispatch(KillSwitchAlertEvent {", 1
        )
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_resolve_kill_switch_timeout_guard(self.config, mutated)
        self.assertIn("alerts.dispatch", str(ctx.exception))

    def test_missing_sms_channel_in_timeout_arm_is_caught(self) -> None:
        mutated = self.exec_src.replace(", OperatorAlertChannel::Sms]", "]", 1)
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_resolve_kill_switch_timeout_guard(self.config, mutated)
        self.assertIn("OperatorAlertChannel::Sms", str(ctx.exception))

    def test_acceptance_in_timeout_arm_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "                Err(Box::new(",
            "                let _smuggled = KillSwitchLiquidationResolved {\n"
            "                    elapsed_seconds,\n"
            "                };\n"
            "                Err(Box::new(",
            1,
        )
        with self.assertRaises(KillSwitchTimeoutCheckError) as ctx:
            check_resolve_kill_switch_timeout_guard(self.config, mutated)
        self.assertIn("not an acceptance", str(ctx.exception))


class KillSwitchTimeoutWireStringTest(unittest.TestCase):
    """Wire-string drift is caught by the atp-types unit test
    `order_error_category_wire_strings_track_syrs_sys_64`. This test exercises
    the cross-crate linkage by spot-checking that the gate rejects through the
    category-pinned factory in the atp-execution crate source."""

    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_execution_references_canonical_wire_string_source(self) -> None:
        self.assertIn(
            "StructuredKillSwitchTimeoutError::liquidation_timeout",
            self.exec_src,
            "atp-execution must reject through the category-pinned factory",
        )


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_thirteen_evidence_items(self) -> None:
        evidence = run_checks()
        # 12 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(evidence), 13)

    def test_assert_kill_switch_timeout_static_emits_twelve_evidence_items(self) -> None:
        config = load_config()
        evidence = assert_kill_switch_timeout_static(config, ROOT)
        self.assertEqual(len(evidence), 12)


if __name__ == "__main__":
    unittest.main()
