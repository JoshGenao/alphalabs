"""Contract tests for ERR-7 (SRS-RESV-004 + SyRS SYS-49b / SYS-49c +
StRS SN-1.25).

Mirrors ``tests/test_pacing_budget_contract.py``: shells out to
``tools/hot_swap_demotion_check.py``, then exercises each per-check
function in-process, including negative spot-checks that verify the
contract actually catches regressions — forbidden broker/IB-order/vendor
fields, missing enum variants, missing port methods, a dropped
cancel/alert/event call in the timeout arm, an alert/cancel leaking into
the flat arm, a promotion path or acceptance struct sneaking into the
timeout arm, a missing operator-alert channel, and a broken probe call.
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

from hot_swap_demotion_check import (  # noqa: E402
    HotSwapDemotionCheckError,
    assert_hot_swap_demotion_static,
    check_demotion_event_sink_port,
    check_demotion_event_struct,
    check_demotion_outcome_enum,
    check_demotion_request_struct,
    check_liquidation_probe_port,
    check_operator_alert_channel_enum,
    check_operator_alert_event_struct,
    check_operator_alert_sink_port,
    check_resolve_demotion_guard,
    check_side_effect_outcome_enum,
    check_unfilled_order_canceller_port,
    load_config,
    orchestrator_source,
    run_checks,
    types_source,
)


class HotSwapDemotionScriptTest(unittest.TestCase):
    def test_err_7_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/hot_swap_demotion_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ERR-7 PASS", result.stdout)
        for needle in (
            "HotSwapDemotionRequest with the 3 required fields",
            "demoting_strategy_id, candidate_strategy_id, timeout_seconds",
            "HotSwapDemotionOutcome with 2 variants",
            "FlatBeforeTimeout, TimedOutDemotionPending",
            "OperatorAlertChannel with 3 variants",
            "Dashboard, Email, Sms",
            "OperatorAlertEvent with the 6 required fields",
            "SideEffectOutcome with 3 variants",
            "NotAttempted, Succeeded, Failed",
            "HotSwapDemotionEvent with the 7 required fields",
            "liquidation_cancel, operator_alert",
            "HotSwapLiquidationProbe with 1 method(s) (await_flat_or_timeout)",
            "UnfilledOrderCanceller with 1 method(s) (cancel_unfilled_liquidation_orders)",
            "OperatorAlertSink with 1 method(s) (dispatch)",
            "HotSwapDemotionEventSink with 1 method(s) (record)",
            "HotSwapDemotionOutcome::FlatBeforeTimeout arm is the sole",
            "HotSwapDemotionOutcome::TimedOutDemotionPending arm cancels",
            "OrderErrorCategory::HotSwapDemotionTimeout",
            "calls no promotion path (ERR-7)",
            "err_7_hot_swap_demotion_timeout",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class HotSwapDemotionRequestStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_required_fields(self) -> None:
        evidence = check_demotion_request_struct(self.config, self.types_src)
        for field in ("demoting_strategy_id", "candidate_strategy_id", "timeout_seconds"):
            self.assertIn(field, evidence)

    def test_missing_demoting_strategy_id_is_caught(self) -> None:
        mutated = self.types_src.replace("pub demoting_strategy_id: StrategyId,", "", 1)
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_demotion_request_struct(self.config, mutated)
        self.assertIn("demoting_strategy_id", str(ctx.exception))

    def test_struct_rejects_leaked_ib_order_id_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct HotSwapDemotionRequest {\n    pub demoting_strategy_id: StrategyId,",
            "pub struct HotSwapDemotionRequest {\n    pub ib_order_id: String,\n"
            "    pub demoting_strategy_id: StrategyId,",
            1,
        )
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_demotion_request_struct(self.config, mutated)
        self.assertIn("ib_order_id", str(ctx.exception))

    def test_struct_rejects_leaked_broker_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct HotSwapDemotionRequest {\n    pub demoting_strategy_id: StrategyId,",
            "pub struct HotSwapDemotionRequest {\n    pub broker: String,\n"
            "    pub demoting_strategy_id: StrategyId,",
            1,
        )
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_demotion_request_struct(self.config, mutated)
        self.assertIn("broker", str(ctx.exception))


class HotSwapDemotionOutcomeEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_both_variants_present(self) -> None:
        evidence = check_demotion_outcome_enum(self.config, self.types_src)
        for variant in ("FlatBeforeTimeout", "TimedOutDemotionPending"):
            self.assertIn(variant, evidence)

    def test_missing_flat_variant_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "    FlatBeforeTimeout {",
            "    FlatBeforeTimeoutX {",
            1,
        )
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_demotion_outcome_enum(self.config, mutated)
        self.assertIn("FlatBeforeTimeout", str(ctx.exception))

    def test_missing_timeout_variant_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "    TimedOutDemotionPending {",
            "    TimedOutDemotionPendingX {",
            1,
        )
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_demotion_outcome_enum(self.config, mutated)
        self.assertIn("TimedOutDemotionPending", str(ctx.exception))


class OperatorAlertChannelEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_all_three_channels_present(self) -> None:
        evidence = check_operator_alert_channel_enum(self.config, self.types_src)
        for variant in ("Dashboard", "Email", "Sms"):
            self.assertIn(variant, evidence)

    def test_missing_sms_channel_is_caught(self) -> None:
        # SRS-RESV-004 requires the dashboard/email/SMS triad — dropping
        # SMS must be caught (a missed SMS is a missed operator page).
        mutated = self.types_src.replace(
            "pub enum OperatorAlertChannel {\n    Dashboard,\n    Email,\n    Sms,\n}",
            "pub enum OperatorAlertChannel {\n    Dashboard,\n    Email,\n}",
            1,
        )
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_operator_alert_channel_enum(self.config, mutated)
        self.assertIn("Sms", str(ctx.exception))


class SideEffectOutcomeEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_all_three_outcomes_present(self) -> None:
        evidence = check_side_effect_outcome_enum(self.config, self.types_src)
        for variant in ("NotAttempted", "Succeeded", "Failed"):
            self.assertIn(variant, evidence)

    def test_missing_failed_outcome_is_caught(self) -> None:
        # Dropping the Failed variant would make a failed cancel / missed
        # alert indistinguishable from success — must be caught.
        mutated = self.types_src.replace(
            "    Failed { reason: String },", "    FailedX { reason: String },", 1
        )
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_side_effect_outcome_enum(self.config, mutated)
        self.assertIn("Failed", str(ctx.exception))


class OperatorAlertEventStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_required_fields(self) -> None:
        evidence = check_operator_alert_event_struct(self.config, self.types_src)
        for field in ("channels", "elapsed_seconds", "timeout_seconds", "observed_at_seconds"):
            self.assertIn(field, evidence)

    def test_missing_channels_field_is_caught(self) -> None:
        mutated = self.types_src.replace("    pub channels: Vec<OperatorAlertChannel>,", "", 1)
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_operator_alert_event_struct(self.config, mutated)
        self.assertIn("channels", str(ctx.exception))

    def test_struct_rejects_leaked_vendor_credentials_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct OperatorAlertEvent {\n    pub demoting_strategy_id: StrategyId,",
            "pub struct OperatorAlertEvent {\n    pub vendor_credentials: String,\n"
            "    pub demoting_strategy_id: StrategyId,",
            1,
        )
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_operator_alert_event_struct(self.config, mutated)
        self.assertIn("vendor_credentials", str(ctx.exception))


class HotSwapDemotionEventStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_required_fields(self) -> None:
        evidence = check_demotion_event_struct(self.config, self.types_src)
        for field in (
            "outcome",
            "promotion_blocked",
            "liquidation_cancel",
            "operator_alert",
            "observed_at_seconds",
        ):
            self.assertIn(field, evidence)

    def test_missing_liquidation_cancel_field_is_caught(self) -> None:
        mutated = self.types_src.replace("    pub liquidation_cancel: SideEffectOutcome,", "", 1)
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_demotion_event_struct(self.config, mutated)
        self.assertIn("liquidation_cancel", str(ctx.exception))

    def test_missing_promotion_blocked_field_is_caught(self) -> None:
        mutated = self.types_src.replace("    pub promotion_blocked: bool,", "", 1)
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_demotion_event_struct(self.config, mutated)
        self.assertIn("promotion_blocked", str(ctx.exception))

    def test_struct_rejects_leaked_container_id_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct HotSwapDemotionEvent {\n    pub outcome: HotSwapDemotionOutcome,",
            "pub struct HotSwapDemotionEvent {\n    pub container_id: String,\n"
            "    pub outcome: HotSwapDemotionOutcome,",
            1,
        )
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_demotion_event_struct(self.config, mutated)
        self.assertIn("container_id", str(ctx.exception))


class HotSwapDemotionPortsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.orch_src = orchestrator_source(self.config)

    def test_probe_port_exposes_await_flat_or_timeout(self) -> None:
        evidence = check_liquidation_probe_port(self.config, self.orch_src)
        self.assertIn("await_flat_or_timeout", evidence)

    def test_missing_probe_method_is_caught(self) -> None:
        mutated = self.orch_src.replace("fn await_flat_or_timeout(", "fn dropped_await(", 1)
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_liquidation_probe_port(self.config, mutated)
        self.assertIn("await_flat_or_timeout", str(ctx.exception))

    def test_missing_canceller_method_is_caught(self) -> None:
        mutated = self.orch_src.replace(
            "fn cancel_unfilled_liquidation_orders(", "fn dropped_cancel(", 1
        )
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_unfilled_order_canceller_port(self.config, mutated)
        self.assertIn("cancel_unfilled_liquidation_orders", str(ctx.exception))

    def test_missing_alert_sink_dispatch_is_caught(self) -> None:
        mutated = self.orch_src.replace(
            "fn dispatch(&self, event: OperatorAlertEvent)",
            "fn dropped_dispatch(&self, event: OperatorAlertEvent)",
            1,
        )
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_operator_alert_sink_port(self.config, mutated)
        self.assertIn("dispatch", str(ctx.exception))

    def test_missing_demotion_event_sink_record_is_caught(self) -> None:
        mutated = self.orch_src.replace(
            "fn record(&self, event: HotSwapDemotionEvent)",
            "fn dropped_record(&self, event: HotSwapDemotionEvent)",
            1,
        )
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_demotion_event_sink_port(self.config, mutated)
        self.assertIn("record", str(ctx.exception))


class ResolveDemotionGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.orch_src = orchestrator_source(self.config)

    def test_guard_summary_pins_both_arms(self) -> None:
        evidence = check_resolve_demotion_guard(self.config, self.orch_src)
        self.assertIn("HotSwapDemotionOutcome::FlatBeforeTimeout", evidence)
        self.assertIn("HotSwapDemotionOutcome::TimedOutDemotionPending", evidence)
        self.assertIn("OrderErrorCategory::HotSwapDemotionTimeout", evidence)
        self.assertIn("no promotion path", evidence)

    def test_broken_probe_call_is_caught(self) -> None:
        mutated = self.orch_src.replace(
            "liquidation.await_flat_or_timeout(&request)",
            "never_called(&request)",
            1,
        )
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_resolve_demotion_guard(self.config, mutated)
        self.assertIn("liquidation.await_flat_or_timeout", str(ctx.exception))

    def test_alert_leaking_into_flat_arm_is_caught(self) -> None:
        # The flat (in-time) arm must NOT dispatch an operator alert.
        mutated = self.orch_src.replace(
            "                Ok(HotSwapDemotionResolved {",
            "                alerts.dispatch(noop);\n                Ok(HotSwapDemotionResolved {",
            1,
        )
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_resolve_demotion_guard(self.config, mutated)
        self.assertIn("alerts.dispatch", str(ctx.exception))

    def test_cancel_leaking_into_flat_arm_is_caught(self) -> None:
        # The flat (in-time) arm must NOT cancel any liquidation order.
        mutated = self.orch_src.replace(
            "                Ok(HotSwapDemotionResolved {",
            "                canceller.cancel_unfilled_liquidation_orders(&request);\n"
            "                Ok(HotSwapDemotionResolved {",
            1,
        )
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_resolve_demotion_guard(self.config, mutated)
        self.assertIn("cancel_unfilled_liquidation_orders", str(ctx.exception))

    def test_missing_flat_arm_acceptance_is_caught(self) -> None:
        # Anchor on the indented code occurrence (the Rustdoc also mentions
        # `Ok(HotSwapDemotionResolved {`, but `_fn_block` excludes the doc).
        mutated = self.orch_src.replace(
            "                Ok(HotSwapDemotionResolved {",
            "                Ok(SomethingElse {",
            1,
        )
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_resolve_demotion_guard(self.config, mutated)
        self.assertIn("HotSwapDemotionResolved", str(ctx.exception))

    def test_dropped_cancel_in_timeout_arm_is_caught(self) -> None:
        # The cancel call now sits inside `into_outcome(canceller.cancel...)`;
        # dropping the canceller method call must still be caught.
        mutated = self.orch_src.replace(
            "canceller.cancel_unfilled_liquidation_orders(&request)",
            "dropped_cancel(&request)",
            1,
        )
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_resolve_demotion_guard(self.config, mutated)
        self.assertIn("cancel_unfilled_liquidation_orders", str(ctx.exception))

    def test_dropped_alert_in_timeout_arm_is_caught(self) -> None:
        mutated = self.orch_src.replace(
            "alerts.dispatch(OperatorAlertEvent {",
            "noop_dispatch(OperatorAlertEvent {",
            1,
        )
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_resolve_demotion_guard(self.config, mutated)
        self.assertIn("alerts.dispatch", str(ctx.exception))

    def test_missing_sms_channel_in_timeout_arm_is_caught(self) -> None:
        mutated = self.orch_src.replace(
            "                        OperatorAlertChannel::Sms,\n", "", 1
        )
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_resolve_demotion_guard(self.config, mutated)
        self.assertIn("OperatorAlertChannel::Sms", str(ctx.exception))

    def test_promotion_path_in_timeout_arm_is_caught(self) -> None:
        # A `promote(` call in the timeout arm is the regression the
        # forbidden_promotions allowlist exists to catch — promotion must
        # be BLOCKED on every timeout.
        mutated = self.orch_src.replace(
            "                Err(StructuredHotSwapDemotionError::demotion_timeout(",
            "                let _ = self.promote();\n"
            "                Err(StructuredHotSwapDemotionError::demotion_timeout(",
            1,
        )
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_resolve_demotion_guard(self.config, mutated)
        self.assertIn("promotion", str(ctx.exception).lower())

    def test_acceptance_in_timeout_arm_is_caught(self) -> None:
        # A timed-out demotion is not an acceptance — constructing
        # HotSwapDemotionResolved in the timeout arm must be caught.
        mutated = self.orch_src.replace(
            "                Err(StructuredHotSwapDemotionError::demotion_timeout(",
            "                let _smuggled = HotSwapDemotionResolved {\n"
            "                    demoting_strategy_id: request.demoting_strategy_id.clone(),\n"
            "                    candidate_strategy_id: request.candidate_strategy_id.clone(),\n"
            "                    promotion_allowed: true,\n"
            "                    elapsed_seconds,\n"
            "                };\n"
            "                Err(StructuredHotSwapDemotionError::demotion_timeout(",
            1,
        )
        with self.assertRaises(HotSwapDemotionCheckError) as ctx:
            check_resolve_demotion_guard(self.config, mutated)
        self.assertIn("not an acceptance", str(ctx.exception))


class HotSwapDemotionWireStringTest(unittest.TestCase):
    """Wire-string drift is caught by the atp-types unit test
    `order_error_category_wire_strings_track_syrs_sys_64`. This test
    exercises the cross-crate linkage by spot-checking that the
    `OrderErrorCategory::HotSwapDemotionTimeout` variant is reachable from
    the atp-orchestrator crate source (pinned by the guard factory call)."""

    def setUp(self) -> None:
        self.config = load_config()
        self.orch_src = orchestrator_source(self.config)

    def test_orchestrator_references_canonical_wire_string_source(self) -> None:
        self.assertIn(
            "StructuredHotSwapDemotionError::demotion_timeout",
            self.orch_src,
            "atp-orchestrator must reject through the category-pinned factory",
        )


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_twelve_evidence_items(self) -> None:
        evidence = run_checks()
        # 11 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(evidence), 12)

    def test_assert_hot_swap_demotion_static_emits_eleven_evidence_items(self) -> None:
        config = load_config()
        evidence = assert_hot_swap_demotion_static(config, ROOT)
        self.assertEqual(len(evidence), 11)


if __name__ == "__main__":
    unittest.main()
