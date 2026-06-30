"""Contract tests for SRS-DATA-008 (SSD-primary / NAS-archival tiered storage).

SRS-DATA-008 / SyRS SYS-24, SYS-67, AC-5, NFR-SC2 / StRS C-5, SN-1.26, SN-1.27 — all ingestion
writes to SSD first; new data is synced to NAS; SSD retains at least 90 days of configured hot
data; NAS is used for indefinite retention. This slice ships the tier coordinator (``TieredStore``)
in ``crates/atp-data`` (module ``tiering``), wrapping the SRS-DATA-016 ``MarketDataStore`` directory.

Mirrors ``tests/test_ingestion_idempotency_contract.py``: shells out to
``tools/data008_tiering_check.py``, then exercises each per-check function in-process — including
negative spot-checks that mutate the Rust source in memory and assert the contract actually catches
the regression (a dropped ≥90-day floor, a reversed SSD-first ordering, a dropped no-data-loss
archival guard, a money-into-float field, an injected nondeterminism source, a leaked vendor token).
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

from data008_tiering_check import (  # noqa: E402
    TieringCheckError,
    assert_data_tiering_static,
    cargo_source,
    check_archive_safety,
    check_config,
    check_determinism,
    check_module_reexport,
    check_nas_sync_status,
    check_no_vendor_tokens,
    check_numeric_boundary,
    check_ssd_first_ordering,
    check_tiered_store,
    lib_source,
    load_config,
    tiering_source,
)


class TieringScriptTest(unittest.TestCase):
    def test_srs_data_008_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/data008_tiering_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-DATA-008 TIERED-STORAGE PASS", result.stdout)
        for needle in (
            "floor-enforces the >=90-day hot window",
            "the durable SSD save precedes the NAS push",
            "keeps NAS outcomes DISTINCT",
            "independently cross-checks the tiers",
            "archive_cold is data-loss-safe",
            "no floating-point in the tier code",
            "reads no wall-clock",
            "carries no broker/execution dependency",
            "names no vendor SDK",
            "documents the SSD hot-tier + NAS archival-tier growth estimates",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = tiering_source(self.config)
        self.lib_src = lib_source(self.config)
        self.cargo_src = cargo_source(self.config)


class ConfigTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("floor-enforces", check_config(self.config, self.src))

    def test_dropped_floor_const_is_caught(self) -> None:
        mutated = self.src.replace("pub const MIN_HOT_RETENTION_DAYS: u32 = 90;", "// removed", 1)
        with self.assertRaises(TieringCheckError):
            check_config(self.config, mutated)

    def test_dropped_distinct_guard_is_caught(self) -> None:
        mutated = self.src.replace("TiersNotDistinct", "TiersMaybeDistinct")
        with self.assertRaises(TieringCheckError):
            check_config(self.config, mutated)


class TieredStoreTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("tier API", check_tiered_store(self.config, self.src))

    def test_dropped_archive_fn_is_caught(self) -> None:
        mutated = self.src.replace("pub fn archive_cold", "fn archive_cold_internal")
        with self.assertRaises(TieringCheckError) as ctx:
            check_tiered_store(self.config, mutated)
        self.assertIn("archive_cold", str(ctx.exception))


class SsdFirstOrderingTest(_Fixture):
    def test_real_source_is_ssd_first(self) -> None:
        self.assertIn("SSD-first", check_ssd_first_ordering(self.config, self.src))

    def test_reversed_order_is_caught(self) -> None:
        # NAS push before the SSD save violates SSD-first; the check must reject it.
        reversed_src = (
            "fn ingest() {\n"
            "    self.push_to_ready_nas(records);\n"
            "    ssd.save_to_path(&self.config.ssd_dir);\n"
            "}\n"
        )
        with self.assertRaises(TieringCheckError) as ctx:
            check_ssd_first_ordering(self.config, reversed_src)
        self.assertIn("SSD-first ordering violated", str(ctx.exception))

    def test_missing_ssd_save_is_caught(self) -> None:
        mutated = self.src.replace("ssd.save_to_path(&self.config.ssd_dir)", "noop()")
        with self.assertRaises(TieringCheckError):
            check_ssd_first_ordering(self.config, mutated)


class NasSyncStatusTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("keeps NAS outcomes DISTINCT", check_nas_sync_status(self.config, self.src))

    def test_dropped_degraded_variant_is_caught(self) -> None:
        mutated = self.src.replace("    Degraded {", "    Gone {", 1)
        with self.assertRaises(TieringCheckError):
            check_nas_sync_status(self.config, mutated)

    def test_dropped_failed_variant_is_caught(self) -> None:
        # Folding the reachable-but-broken case back into Degraded (dropping Failed) must be caught.
        mutated = self.src.replace("NasSyncStatus::Failed", "NasSyncStatus::Degraded")
        with self.assertRaises(TieringCheckError):
            check_nas_sync_status(self.config, mutated)


class ArchiveSafetyTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("data-loss-safe", check_archive_safety(self.config, self.src))

    def test_dropped_nas_confirmation_is_caught(self) -> None:
        # Removing the "confirmed byte-identical on NAS" guard would let archival drop an
        # un-archived record — the no-data-loss invariant; the check must catch it.
        mutated = self.src.replace("nas.get(record.key()) == Some(record)", "true")
        with self.assertRaises(TieringCheckError) as ctx:
            check_archive_safety(self.config, mutated)
        self.assertIn("confirmed byte-identical on NAS", str(ctx.exception))

    def test_dropped_alias_classifier_guard_is_caught(self) -> None:
        # Removing the centralized nas_access alias check would let an SSD/NAS symlink alias make
        # archival delete the only copy (and other NAS paths report success); the check must catch it.
        mutated = self.src.replace(
            "same_directory(&self.config.ssd_dir, &self.config.nas_dir)", "false"
        )
        with self.assertRaises(TieringCheckError) as ctx:
            check_archive_safety(self.config, mutated)
        self.assertIn("alias", str(ctx.exception))


class NumericBoundaryTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("no floating-point", check_numeric_boundary(self.config, self.src))

    def test_injected_float_in_code_is_caught(self) -> None:
        mutated = self.src.replace("now_ts: i64", "now_ts: f64", 1)
        with self.assertRaises(TieringCheckError):
            check_numeric_boundary(self.config, mutated)

    def test_float_mentioned_only_in_a_comment_is_allowed(self) -> None:
        # A doc comment may mention the word; only a CODE use is forbidden.
        commented = self.src + "\n// this module deliberately avoids f64 and f32 entirely\n"
        self.assertIn("no floating-point", check_numeric_boundary(self.config, commented))


class DeterminismTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("no wall-clock", check_determinism(self.config, self.src))

    def test_injected_clock_read_is_caught(self) -> None:
        mutated = self.src.replace(
            "now_ts.saturating_sub", "SystemTime::now(); now_ts.saturating_sub", 1
        )
        with self.assertRaises(TieringCheckError):
            check_determinism(self.config, mutated)


class VendorIsolationTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("no vendor SDK", check_no_vendor_tokens(self.config, self.src))

    def test_vendor_token_in_code_is_caught(self) -> None:
        mutated = self.src.replace("use std::fs;", "use std::fs;\nuse ibapi::Client;", 1)
        with self.assertRaises(TieringCheckError):
            check_no_vendor_tokens(self.config, mutated)


class ModuleReexportTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("tier module", check_module_reexport(self.config, self.lib_src))

    def test_missing_reexport_is_caught(self) -> None:
        mutated = self.lib_src.replace("pub mod tiering;", "mod tiering;")
        with self.assertRaises(TieringCheckError):
            check_module_reexport(self.config, mutated)


class StaticBundleTest(_Fixture):
    def test_full_static_bundle_passes(self) -> None:
        evidence = assert_data_tiering_static(self.config)
        self.assertTrue(any("floor-enforces" in line for line in evidence))
        self.assertTrue(any("data-loss-safe" in line for line in evidence))


if __name__ == "__main__":
    unittest.main()
