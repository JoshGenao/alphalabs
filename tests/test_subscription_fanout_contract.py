"""Contract tests for SRS-MD-001 (consolidated subscription fan-out).

SRS-MD-001 / SyRS SYS-70 / StRS SN-1.10 / SN-1.29 / SC-25 / A-13 — the
consolidation + fan-out half of SYS-70 (the line-limit half is SRS-MD-002).

Mirrors ``tests/test_subscription_limit_contract.py``: shells out to
``tools/subscription_fanout_check.py``, then exercises each per-check
function in-process, including negative spot-checks that mutate the Rust
source in memory and assert the contract actually catches the regression
(dropped field, leaked vendor field, missing variant, removed line-counter
impl, a SECOND upstream-subscription insert that would break dedup, removed
fan-out symbol validation, leaked vendor token).
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

from subscription_fanout_check import (  # noqa: E402
    SubscriptionFanoutCheckError,
    assert_subscription_fanout_static,
    check_asset_class_enum,
    check_atomic_admission,
    check_change_sink_port,
    check_dedup_invariant,
    check_input_validation,
    check_line_counter_impl,
    check_market_data_tick,
    check_registry_error_enum,
    check_registry_key_type,
    check_registry_struct,
    check_security_key,
    check_subscription_change_enum,
    check_subscription_change_event,
    check_vendor_isolation,
    load_config,
    market_data_source,
    run_checks,
    types_source,
)


class SubscriptionFanoutScriptTest(unittest.TestCase):
    def test_srs_md_001_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/subscription_fanout_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-MD-001 SDK-SURFACE PASS", result.stdout)
        for needle in (
            "MarketDataTick with the 3 fan-out fields (symbol, asset_class, tick_seq)",
            "AssetClass with 2 tradable classes (Equity, Option)",
            "SecurityKey (private symbol+asset_class, normalizing `new`)",
            "carried by SubscriptionRequest, MarketDataTick via .security_key()",
            "SubscriptionChange with 6 transitions",
            "Opened/Closed are the only line-affecting ones",
            "SubscriptionChangeEvent with the 5 required fields",
            "subscriber_count, lines_in_use",
            "SubscriptionChangeSink with 1 method (record)",
            "SubscriptionRegistryError with 3 fail-closed variants",
            "EmptySymbol, EmptyStrategyId, LineLimitReached",
            "ConsolidatedSubscriptionRegistry with the 7 dedup + fan-out methods",
            "keys the consolidated set on SecurityKey",
            "no raw-symbol conflation",
            "IS the concrete SubscriptionLineCounter",
            "closes subscription_limit_contract deferral 'owner: SRS-MD-001'",
            "inserts a new upstream subscription EXACTLY ONCE",
            "enforces the IB line ceiling atomically",
            "the probe-then-mutate race",
            "canonicalize via SecurityKey",
            "fail closed on empty symbol / strategy id",
            "free of all 5 forbidden vendor SDK tokens",
            "feature_list.json keeps SRS-MD-001 passes:false",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)
        self.md_src = market_data_source(self.config)


class MarketDataTickTest(_Fixture):
    def test_required_fields_present(self) -> None:
        evidence = check_market_data_tick(self.config, self.types_src)
        self.assertIn("symbol, asset_class, tick_seq", evidence)

    def test_dropped_field_is_caught(self) -> None:
        mutated = self.types_src.replace("    pub tick_seq: u64,", "", 1)
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_market_data_tick(self.config, mutated)
        self.assertIn("tick_seq", str(ctx.exception))

    def test_dropped_asset_class_field_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub struct MarketDataTick {\n    pub symbol: String,\n    pub asset_class: AssetClass,",
            "pub struct MarketDataTick {\n    pub symbol: String,",
            1,
        )
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_market_data_tick(self.config, mutated)
        self.assertIn("asset_class", str(ctx.exception))

    def test_leaked_broker_field_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub struct MarketDataTick {\n    pub symbol: String,",
            "pub struct MarketDataTick {\n    pub broker: String,\n    pub symbol: String,",
            1,
        )
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_market_data_tick(self.config, mutated)
        self.assertIn("broker", str(ctx.exception))


class AssetClassEnumTest(_Fixture):
    def test_tradable_classes_present(self) -> None:
        evidence = check_asset_class_enum(self.config, self.types_src)
        self.assertIn("Equity, Option", evidence)

    def test_dropped_variant_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub enum AssetClass {\n    #[default]\n    Equity,",
            "pub enum AssetClass {\n    #[default]\n    EquityX,",
            1,
        )
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_asset_class_enum(self.config, mutated)
        self.assertIn("Equity", str(ctx.exception))


class SecurityKeyTest(_Fixture):
    def test_canonical_key_evidence(self) -> None:
        evidence = check_security_key(self.config, self.types_src)
        self.assertIn("SecurityKey", evidence)
        self.assertIn("security_key()", evidence)

    def test_public_field_breaks_normalization_guarantee(self) -> None:
        # Making a field public lets a caller bypass the normalizing
        # constructor — the check must reject it.
        mutated = self.types_src.replace(
            "pub struct SecurityKey {\n    symbol: String,",
            "pub struct SecurityKey {\n    pub symbol: String,",
            1,
        )
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_security_key(self.config, mutated)
        self.assertIn("private", str(ctx.exception))

    def test_missing_normalization_is_caught(self) -> None:
        mutated = self.types_src.replace(".trim().to_uppercase()", ".to_string()")
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_security_key(self.config, mutated)
        self.assertIn("normalize", str(ctx.exception))

    def test_missing_carrier_accessor_is_caught(self) -> None:
        # Rename the first carrier's (SubscriptionRequest) security_key()
        # accessor so the canonical key can't be derived from it.
        mutated = self.types_src.replace("pub fn security_key(", "pub fn dropped_key(", 1)
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_security_key(self.config, mutated)
        self.assertIn("security_key", str(ctx.exception))


class SubscriptionChangeEnumTest(_Fixture):
    def test_all_variants_present(self) -> None:
        evidence = check_subscription_change_enum(self.config, self.types_src)
        for variant in ("Opened", "Closed", "SubscriberAdded", "NotSubscribed"):
            self.assertIn(variant, evidence)

    def test_dropped_variant_is_caught(self) -> None:
        mutated = self.types_src.replace("    SubscriberAdded,", "", 1)
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_subscription_change_enum(self.config, mutated)
        self.assertIn("SubscriberAdded", str(ctx.exception))


class SubscriptionChangeEventTest(_Fixture):
    def test_required_fields_present(self) -> None:
        evidence = check_subscription_change_event(self.config, self.types_src)
        self.assertIn("subscriber_count, lines_in_use", evidence)

    def test_dropped_lines_in_use_field_is_caught(self) -> None:
        mutated = self.types_src.replace("    pub lines_in_use: u32,", "", 1)
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_subscription_change_event(self.config, mutated)
        self.assertIn("lines_in_use", str(ctx.exception))

    def test_leaked_tick_id_field_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub struct SubscriptionChangeEvent {\n    pub change: SubscriptionChange,",
            "pub struct SubscriptionChangeEvent {\n    pub tick_id: u64,\n    pub change: SubscriptionChange,",
            1,
        )
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_subscription_change_event(self.config, mutated)
        self.assertIn("tick_id", str(ctx.exception))


class ChangeSinkPortTest(_Fixture):
    def test_record_method_present(self) -> None:
        evidence = check_change_sink_port(self.config, self.md_src)
        self.assertIn("record", evidence)

    def test_missing_record_method_is_caught(self) -> None:
        mutated = self.md_src.replace(
            "    fn record(&self, event: SubscriptionChangeEvent);",
            "    fn dropped(&self, event: SubscriptionChangeEvent);",
            1,
        )
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_change_sink_port(self.config, mutated)
        self.assertIn("record", str(ctx.exception))


class RegistryErrorEnumTest(_Fixture):
    def test_fail_closed_variants_present(self) -> None:
        evidence = check_registry_error_enum(self.config, self.md_src)
        self.assertIn("EmptySymbol", evidence)
        self.assertIn("EmptyStrategyId", evidence)
        self.assertIn("LineLimitReached", evidence)

    def test_dropped_variant_is_caught(self) -> None:
        mutated = self.md_src.replace("    EmptyStrategyId,", "", 1)
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_registry_error_enum(self.config, mutated)
        self.assertIn("EmptyStrategyId", str(ctx.exception))

    def test_dropped_line_limit_variant_is_caught(self) -> None:
        mutated = self.md_src.replace("    LineLimitReached { configured_limit: u32 },", "", 1)
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_registry_error_enum(self.config, mutated)
        self.assertIn("LineLimitReached", str(ctx.exception))


class RegistryKeyTypeTest(_Fixture):
    def test_keyed_on_security_key(self) -> None:
        evidence = check_registry_key_type(self.config, self.md_src)
        self.assertIn("SecurityKey", evidence)

    def test_raw_string_key_is_caught(self) -> None:
        # Reverting to a raw-symbol key is the exact conflation regression
        # the canonical SecurityKey closes.
        mutated = self.md_src.replace(
            "subscribers: BTreeMap<SecurityKey, Vec<StrategyId>>,",
            "subscribers: BTreeMap<String, Vec<StrategyId>>,",
            1,
        )
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_registry_key_type(self.config, mutated)
        self.assertIn("SecurityKey", str(ctx.exception))


class AtomicAdmissionTest(_Fixture):
    def test_atomic_enforcement_evidence(self) -> None:
        evidence = check_atomic_admission(self.config, self.md_src)
        self.assertIn("atomically", evidence)

    def test_removed_limit_check_is_caught(self) -> None:
        # Drop the ceiling check from subscribe — the over-limit / race
        # regression Codex flagged.
        mutated = self.md_src.replace(
            "if self.subscribers.len() as u32 >= self.line_limit {",
            "if false {",
            1,
        )
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_atomic_admission(self.config, mutated)
        self.assertIn(">= self.line_limit", str(ctx.exception))

    def test_removed_limit_error_is_caught(self) -> None:
        # Rename only subscribe's return variant (braces stay balanced); the
        # enum/Display occurrences are untouched.
        mutated = self.md_src.replace(
            "Err(SubscriptionRegistryError::LineLimitReached {",
            "Err(SubscriptionRegistryError::SomethingElse {",
            1,
        )
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_atomic_admission(self.config, mutated)
        self.assertIn("LineLimitReached", str(ctx.exception))


class RegistryStructTest(_Fixture):
    def test_dedup_fan_out_methods_present(self) -> None:
        evidence = check_registry_struct(self.config, self.md_src)
        for method in ("subscribe", "unsubscribe", "fan_out", "distinct_subscriptions"):
            self.assertIn(method, evidence)

    def test_dropped_method_is_caught(self) -> None:
        mutated = self.md_src.replace(
            "pub fn subscriber_count(", "pub fn dropped_subscriber_count("
        )
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_registry_struct(self.config, mutated)
        self.assertIn("subscriber_count", str(ctx.exception))


class LineCounterImplTest(_Fixture):
    def test_registry_is_the_concrete_line_counter(self) -> None:
        evidence = check_line_counter_impl(self.config, self.md_src)
        self.assertIn("IS the concrete SubscriptionLineCounter", evidence)
        self.assertIn("owner: SRS-MD-001", evidence)

    def test_removed_line_counter_impl_is_caught(self) -> None:
        mutated = self.md_src.replace(
            "impl SubscriptionLineCounter for ConsolidatedSubscriptionRegistry {",
            "impl SubscriptionLineCounter for SomeOtherType {",
            1,
        )
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_line_counter_impl(self.config, mutated)
        self.assertIn("SubscriptionLineCounter", str(ctx.exception))

    def test_non_dedup_aware_try_acquire_is_caught(self) -> None:
        # Strip the contains_key short-circuit: a try_acquire that no longer
        # recognises an already-subscribed security would charge a new line
        # for a duplicate — breaking the dedup property at the gate.
        mutated = self.md_src.replace("self.subscribers.contains_key(", "self.never_called(")
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_line_counter_impl(self.config, mutated)
        self.assertIn("contains_key", str(ctx.exception))


class DedupInvariantTest(_Fixture):
    def test_invariant_evidence(self) -> None:
        evidence = check_dedup_invariant(self.config, self.md_src)
        self.assertIn("EXACTLY ONCE", evidence)

    def test_second_upstream_insert_breaks_dedup_and_is_caught(self) -> None:
        # Inject a SECOND self.subscribers.insert(...) into subscribe — a
        # regression where an additional subscriber opens a second upstream
        # subscription instead of deduping onto the existing line.
        mutated = self.md_src.replace(
            "Self::validate_strategy_id(&request.strategy_id)?;",
            "Self::validate_strategy_id(&request.strategy_id)?;\n        self.subscribers.insert(String::new(), Vec::new());",
            1,
        )
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_dedup_invariant(self.config, mutated)
        self.assertIn("EXACTLY ONE", str(ctx.exception))

    def test_removed_dedup_push_is_caught(self) -> None:
        mutated = self.md_src.replace(
            "existing.push(request.strategy_id.clone());",
            "/* dedup append removed */",
            1,
        )
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_dedup_invariant(self.config, mutated)
        self.assertIn("existing.push", str(ctx.exception))


class InputValidationTest(_Fixture):
    def test_fail_closed_evidence(self) -> None:
        evidence = check_input_validation(self.config, self.md_src)
        self.assertIn("fail closed", evidence)

    def test_removed_fan_out_validation_is_caught(self) -> None:
        # Replace fan_out's fail-closed canonicalization with an unchecked
        # unwrap — a tick with an empty symbol would no longer be rejected.
        mutated = self.md_src.replace(
            "let key = tick\n            .security_key()\n            .ok_or(SubscriptionRegistryError::EmptySymbol)?;",
            "let key = tick.security_key().unwrap();",
            1,
        )
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_input_validation(self.config, mutated)
        self.assertIn("fan_out", str(ctx.exception))


class VendorIsolationTest(_Fixture):
    def test_no_vendor_tokens(self) -> None:
        evidence = check_vendor_isolation(self.config, self.md_src)
        self.assertIn("free of all", evidence)

    def test_leaked_vendor_token_is_caught(self) -> None:
        mutated = self.md_src + "\n// uses interactive_brokers under the hood\n"
        with self.assertRaises(SubscriptionFanoutCheckError) as ctx:
            check_vendor_isolation(self.config, mutated)
        self.assertIn("interactive_brokers", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_fifteen_items(self) -> None:
        # 14 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(run_checks()), 15)

    def test_static_evidence_is_fourteen_items(self) -> None:
        self.assertEqual(len(assert_subscription_fanout_static(load_config(), ROOT)), 14)


if __name__ == "__main__":
    unittest.main()
