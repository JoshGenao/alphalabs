"""Contract tests for SRS-DATA-009 (transparent cold-read failover to NAS + bounded SSD cache).

SRS-DATA-009 / SyRS SYS-68 / StRS SN-1.28, BG-5 — requests outside SSD retention are served from NAS
and cached on SSD without consumer code changes; the cold-read cache does not exceed the configurable
SSD share (default 20%) and is evicted before hot runtime data. This slice ships the ``cold_read``
module (``TieredReader``) in ``crates/atp-data`` over the SRS-DATA-008 ``TieredStore``.

Mirrors ``tests/test_data008_tiering_contract.py``: shells out to
``tools/data009_cold_read_check.py``, exercises each per-check function in-process with negative
spot-checks that mutate the Rust source in memory (a dropped cap helper, an SSD-primary reference
leaked into the eviction path, a money-into-float field, an injected clock read, a leaked vendor
token, a CLI that persists directly), AND drives the operator CLI end to end (ingest → archive-cold →
cold-read query → cache hit → cap enforcement → evict) so the behaviour — not just the structure — is
proven with a real on-disk cache.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from data009_cold_read_check import (  # noqa: E402
    ColdReadCheckError,
    assert_cold_read_static,
    cargo_source,
    check_cap_enforcement,
    check_cli,
    check_config_type,
    check_consumer_wiring,
    check_determinism,
    check_evict_before_hot,
    check_module_reexport,
    check_no_vendor_tokens,
    check_numeric_boundary,
    check_reader_type,
    check_transparency,
    cli_source,
    cold_read_source,
    data007_cli_source,
    lib_source,
    load_config,
)

TIER_CLI = ROOT / "target" / "debug" / "data008_tier_cli"
COLD_CLI = ROOT / "target" / "debug" / "data009_cold_read_cli"
DATA007_CLI = ROOT / "target" / "debug" / "data007_query_cli"


class ColdReadScriptTest(unittest.TestCase):
    def test_srs_data_009_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/data009_cold_read_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-DATA-009 COLD-READ-FAILOVER PASS", result.stdout)
        for needle in (
            "exposes the cold-read module",
            "defaults the share to 20%",
            "transparent read surface",
            "consults NAS only for cold ranges",
            "never exceeds the configurable SSD share",
            "evicted before (and without ever touching) hot runtime data",
            "integer arithmetic",
            "reads no wall-clock",
            "names no vendor SDK",
            "never persists directly",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = cold_read_source(self.config)
        self.lib_src = lib_source(self.config)
        self.cargo_src = cargo_source(self.config)
        self.cli_src = cli_source(self.config)
        self.data007_cli_src = data007_cli_source(self.config)


class ConsumerWiringTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn(
            "existing SRS-DATA-007 read surface",
            check_consumer_wiring(self.config, self.data007_cli_src),
        )

    def test_unwired_consumer_is_caught(self) -> None:
        # If the existing SRS-DATA-007 read CLI does not route through TieredReader, cold NAS fallback
        # would be opt-in to a new CLI rather than transparent to an existing consumer — the check must
        # catch that (the AC's "without requiring consumer code changes").
        mutated = self.data007_cli_src.replace("TieredReader", "PlainStore")
        with self.assertRaises(ColdReadCheckError):
            check_consumer_wiring(self.config, mutated)

    def test_flag_only_wiring_is_caught(self) -> None:
        # Wiring only the explicit --nas flag (not the ATP_NAS_DATA_DIR config key) would still require
        # a changed invocation; the check must require env-driven auto-engagement.
        mutated = self.data007_cli_src.replace("ATP_NAS_DATA_DIR", "SOME_UNUSED_KEY")
        with self.assertRaises(ColdReadCheckError):
            check_consumer_wiring(self.config, mutated)


class DivergenceGuardTest(_Fixture):
    def test_evidence(self) -> None:
        from data009_cold_read_check import check_divergence_guard

        self.assertIn("fails closed", check_divergence_guard(self.config, self.src))

    def test_key_only_dedup_is_caught(self) -> None:
        # If merge_record stops comparing full record content on a duplicate key, a stale/corrupt cache
        # entry silently shadows the NAS record — the check must catch the dropped value comparison.
        from data009_cold_read_check import check_divergence_guard

        mutated = self.src.replace("Some(&i) if &assembled[i] == record => Ok(false),", "")
        with self.assertRaises(ColdReadCheckError):
            check_divergence_guard(self.config, mutated)


class RetentionGuardTest(_Fixture):
    def test_evidence(self) -> None:
        from data009_cold_read_check import check_retention_guard

        self.assertIn(
            "only records older than the hot window",
            check_retention_guard(self.config, self.src),
        )

    def test_full_range_nas_fallback_is_caught(self) -> None:
        # If the NAS fallback stops restricting to the cold window, a hot record could be served from
        # NAS, masking an SRS-DATA-008 retention breach — the check must catch the dropped cold filter.
        from data009_cold_read_check import check_retention_guard

        mutated = self.src.replace("record.key().event_ts < hot_window_start", "true")
        with self.assertRaises(ColdReadCheckError):
            check_retention_guard(self.config, mutated)


class StaticAggregateTest(_Fixture):
    def test_all_static_checks_pass(self) -> None:
        evidence = assert_cold_read_static(self.config)
        self.assertTrue(all(isinstance(line, str) and line for line in evidence))
        self.assertEqual(len(evidence), 14)


class ModuleReexportTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("cold-read module", check_module_reexport(self.config, self.lib_src))

    def test_dropped_reexport_is_caught(self) -> None:
        mutated = self.lib_src.replace("pub mod cold_read;", "// removed")
        with self.assertRaises(ColdReadCheckError):
            check_module_reexport(self.config, mutated)


class ConfigTypeTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("floor(capacity*share/100)", check_config_type(self.config, self.src))

    def test_dropped_default_share_const_is_caught(self) -> None:
        mutated = self.src.replace(
            "pub const DEFAULT_COLD_READ_CACHE_SHARE_PERCENT: u32 = 20;", "// removed", 1
        )
        with self.assertRaises(ColdReadCheckError):
            check_config_type(self.config, mutated)

    def test_wrong_default_share_is_caught(self) -> None:
        # A default other than 20% breaks the AC's "defaulting to 20 percent".
        mutated = self.src.replace(
            "pub const DEFAULT_COLD_READ_CACHE_SHARE_PERCENT: u32 = 20;",
            "pub const DEFAULT_COLD_READ_CACHE_SHARE_PERCENT: u32 = 50;",
        )
        with self.assertRaises(ColdReadCheckError):
            check_config_type(self.config, mutated)

    def test_dropped_fail_closed_variant_is_caught(self) -> None:
        mutated = self.src.replace("ZeroSsdCapacity", "ZeroSsdCapacityMaybe")
        with self.assertRaises(ColdReadCheckError):
            check_config_type(self.config, mutated)


class ReaderTypeTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("transparent read surface", check_reader_type(self.config, self.src))

    def test_dropped_query_fn_is_caught(self) -> None:
        mutated = self.src.replace("pub fn query", "fn query_internal", 1)
        with self.assertRaises(ColdReadCheckError):
            check_reader_type(self.config, mutated)


class TransparencyTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("transparent", check_transparency(self.config, self.src))

    def test_dropped_cold_gate_is_caught(self) -> None:
        # Removing the hot-window gate would make every read consult NAS (or none) — not the
        # SRS-DATA-008 retention-aware fallback.
        mutated = self.src.replace("hot_window_start", "always_zero")
        with self.assertRaises(ColdReadCheckError) as ctx:
            check_transparency(self.config, mutated)
        self.assertIn("hot_window_start", str(ctx.exception))

    def test_dropped_event_ts_ordering_is_caught(self) -> None:
        mutated = self.src.replace("event_ts.cmp", "no_order_cmp")
        with self.assertRaises(ColdReadCheckError):
            check_transparency(self.config, mutated)


class CapEnforcementTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("never exceeds", check_cap_enforcement(self.config, self.src))

    def test_dropped_cap_helper_in_writeback_is_caught(self) -> None:
        # If write_back_cache stops calling keep_most_recent, the cache is unbounded — the exact
        # regression the AC's "do not exceed the configurable SSD share" forbids.
        mutated = self.src.replace(
            "let survivors = keep_most_recent(cache.records(), cap);",
            "let survivors = cache.records().to_vec();",
        )
        with self.assertRaises(ColdReadCheckError):
            check_cap_enforcement(self.config, mutated)


class EvictBeforeHotTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("never opens the SSD primary", check_evict_before_hot(self.config, self.src))

    def test_ssd_primary_leaked_into_eviction_is_caught(self) -> None:
        # Injecting an ssd_dir reference into the eviction body would mean cache eviction could touch
        # the hot tier — the check must catch it ("evicted before hot runtime data").
        body_marker = "let dir = self.cold_cache_dir();\n        if !dir.is_dir()"
        self.assertIn(body_marker, self.src)
        mutated = self.src.replace(
            body_marker,
            "let dir = self.cold_cache_dir();\n"
            "        let _hot = self.tier.config().ssd_dir();\n"
            "        if !dir.is_dir()",
            1,
        )
        with self.assertRaises(ColdReadCheckError) as ctx:
            check_evict_before_hot(self.config, mutated)
        self.assertIn("ssd_dir", str(ctx.exception))


class NumericBoundaryTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("integer arithmetic", check_numeric_boundary(self.config, self.src))

    def test_injected_float_in_code_is_caught(self) -> None:
        mutated = self.src.replace(
            "self.ssd_capacity_records",
            "(self.ssd_capacity_records as f64) as u64 + self.ssd_capacity_records",
            1,
        )
        with self.assertRaises(ColdReadCheckError):
            check_numeric_boundary(self.config, mutated)

    def test_float_in_a_comment_is_allowed(self) -> None:
        # A doc comment mentioning f64 must NOT trip the check (comments are stripped first).
        mutated = self.src.replace(
            "pub struct ColdReadConfig",
            "// this cap is integer, never an f64 ratio\npub struct ColdReadConfig",
            1,
        )
        self.assertIn("integer arithmetic", check_numeric_boundary(self.config, mutated))


class DeterminismTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("no wall-clock", check_determinism(self.config, self.src))

    def test_injected_clock_read_is_caught(self) -> None:
        mutated = self.src.replace(
            "let mut assembled: Vec<MarketDataRecord> = Vec::new();",
            "let _leak = std::time::SystemTime::now();\n"
            "        let mut assembled: Vec<MarketDataRecord> = Vec::new();",
            1,
        )
        with self.assertRaises(ColdReadCheckError):
            check_determinism(self.config, mutated)


class VendorTokensTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("no vendor SDK", check_no_vendor_tokens(self.config, self.src))

    def test_vendor_token_in_code_is_caught(self) -> None:
        mutated = self.src.replace(
            "use crate::tiering::TieredStore;",
            "use crate::tiering::TieredStore;\nuse databento::Client;",
            1,
        )
        with self.assertRaises(ColdReadCheckError):
            check_no_vendor_tokens(self.config, mutated)


class CliTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("never persists directly", check_cli(self.config, self.cli_src))

    def test_dropped_cap_breach_exit_is_caught(self) -> None:
        mutated = self.cli_src.replace("if !result.cold_cache_within_cap()", "if false")
        with self.assertRaises(ColdReadCheckError):
            check_cli(self.config, mutated)

    def test_direct_persist_in_cli_is_caught(self) -> None:
        # A CLI that persists directly would be an SSD-only path the SRS-DATA-008 routing sweep flags;
        # the guard forbids it (persistence is library-owned).
        mutated = self.cli_src.replace(
            "let result = reader.query(&query, now).map_err(|err| err.to_string())?;",
            "store.save_to_path(&ssd).unwrap();\n"
            "    let result = reader.query(&query, now).map_err(|err| err.to_string())?;",
            1,
        )
        with self.assertRaises(ColdReadCheckError):
            check_cli(self.config, mutated)


@unittest.skipUnless(
    TIER_CLI.exists() and DATA007_CLI.exists(),
    "cargo-built data008_tier_cli + data007_query_cli required (run `cargo build -p atp-data`)",
)
class Data007TransparentFailoverTest(unittest.TestCase):
    """Codex-requested regression: an EXISTING SRS-DATA-007 read surface (data007_query_cli) reads a
    record archived off SSD and receives it from NAS WITHOUT changing the query shape."""

    NOW = "1700000000"
    COLD_TS = "1680000000"

    def _run(self, cli: Path, *args: str, env: dict | None = None) -> dict:
        result = subprocess.run(
            [str(cli), *args], cwd=ROOT, check=False, capture_output=True, text=True, env=env
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                out.setdefault(key, value)
        return out

    @staticmethod
    def _clean_env(**overrides: str) -> dict:
        # Strip ambient tier config so the single-tier assertion is deterministic regardless of the
        # developer's shell, then apply the test's overrides.
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("ATP_NAS_DATA_DIR", "ATP_DATA_STORE_DIR")
        }
        env.update(overrides)
        return env

    def _seed_archived_off(self, ssd: Path, nas: Path) -> None:
        self._run(
            TIER_CLI,
            "ingest",
            "--ssd",
            str(ssd),
            "--nas",
            str(nas),
            "--kind",
            "daily-equity-bar",
            "--event-ts",
            self.COLD_TS,
        )
        self._run(TIER_CLI, "archive-cold", "--ssd", str(ssd), "--nas", str(nas), "--now", self.NOW)

    def test_data007_query_transparently_falls_back_via_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ssd, nas = Path(tmp) / "ssd", Path(tmp) / "nas"
            ssd.mkdir()
            nas.mkdir()
            self._seed_archived_off(ssd, nas)
            base_query = [
                "query",
                "--symbol",
                "AAPL",
                "--resolution",
                "1d",
                "--start",
                "1670000000",
                "--end",
                self.NOW,
            ]
            env = self._clean_env()
            # SINGLE-TIER (no NAS configured): existing behaviour MISSES the archived-off record.
            single = self._run(DATA007_CLI, *base_query, "--dir", str(ssd), env=env)
            self.assertEqual(
                single["match_count"], "0", "single-tier read misses archived-off data"
            )
            # SAME query shape + explicit --nas: served transparently from NAS.
            tiered = self._run(
                DATA007_CLI,
                *base_query,
                "--dir",
                str(ssd),
                "--nas",
                str(nas),
                "--now",
                self.NOW,
                "--ssd-capacity",
                "100",
                env=env,
            )
            self.assertEqual(
                tiered["match_count"], "1", "archived-off record served via NAS fallback"
            )
            self.assertEqual(tiered["served_from_nas"], "1")
            self.assertEqual(tiered["record.0.event_ts"], self.COLD_TS)
            self.assertEqual(tiered["cold_cache_within_cap"], "true")

    def test_data007_query_auto_tiers_from_env_with_unchanged_invocation(self) -> None:
        """The headline 'without consumer code changes' proof: the EXACT SAME query invocation (no
        tier flags, no --now) auto-falls-back to NAS when ATP_NAS_DATA_DIR is configured in the
        environment, exactly like --dir already resolves from ATP_DATA_STORE_DIR."""
        with tempfile.TemporaryDirectory() as tmp:
            ssd, nas = Path(tmp) / "ssd", Path(tmp) / "nas"
            ssd.mkdir()
            nas.mkdir()
            self._seed_archived_off(ssd, nas)
            invocation = [
                "query",
                "--symbol",
                "AAPL",
                "--resolution",
                "1d",
                "--start",
                "1670000000",
                "--end",
                self.NOW,
            ]
            # Env WITHOUT a NAS tier -> single-tier -> misses the archived-off record.
            single = self._run(
                DATA007_CLI, *invocation, env=self._clean_env(ATP_DATA_STORE_DIR=str(ssd))
            )
            self.assertEqual(single["match_count"], "0")
            # SAME invocation, env WITH ATP_NAS_DATA_DIR configured -> transparent NAS fallback.
            tiered = self._run(
                DATA007_CLI,
                *invocation,
                env=self._clean_env(ATP_DATA_STORE_DIR=str(ssd), ATP_NAS_DATA_DIR=str(nas)),
            )
            self.assertEqual(tiered.get("tier"), "cold-read", "env NAS auto-engages tiering")
            self.assertEqual(tiered["match_count"], "1")
            self.assertEqual(tiered["served_from_nas"], "1")
            self.assertEqual(tiered["record.0.event_ts"], self.COLD_TS)

    def test_configured_but_unmounted_nas_degrades_not_silently_single_tier(self) -> None:
        """A configured NAS tier whose mount is ABSENT must surface a DEGRADED cold read
        (tier:cold-read, nas_reachable:false) — never a silent single-tier read that hides the NAS
        outage (an archived-off record would then look like an empty result, not a degraded alert)."""
        with tempfile.TemporaryDirectory() as tmp:
            ssd, nas = Path(tmp) / "ssd", Path(tmp) / "nas"
            ssd.mkdir()
            nas.mkdir()
            self._seed_archived_off(ssd, nas)
            # The NAS mount disappears after ingestion/archival.
            import shutil

            shutil.rmtree(nas)
            invocation = [
                "query",
                "--symbol",
                "AAPL",
                "--resolution",
                "1d",
                "--start",
                "1670000000",
                "--end",
                self.NOW,
                "--now",
                self.NOW,
            ]
            out = self._run(
                DATA007_CLI,
                *invocation,
                env=self._clean_env(ATP_DATA_STORE_DIR=str(ssd), ATP_NAS_DATA_DIR=str(nas)),
            )
            self.assertEqual(out.get("tier"), "cold-read", "configured NAS still engages tiering")
            self.assertEqual(
                out["nas_reachable"], "false", "the NAS outage is surfaced as degraded"
            )

    def test_split_adjusted_stays_single_tier_with_nas_configured(self) -> None:
        # Tiered cold-read serves RAW only; a split-adjusted query with a NAS configured falls through
        # to the single-tier SSD path (no tier: line), not tiering (the DATA-011/012 follow-up).
        with tempfile.TemporaryDirectory() as tmp:
            ssd, nas = Path(tmp) / "ssd", Path(tmp) / "nas"
            ssd.mkdir()
            nas.mkdir()
            result = subprocess.run(
                [
                    str(DATA007_CLI),
                    "query",
                    "--dir",
                    str(ssd),
                    "--nas",
                    str(nas),
                    "--symbol",
                    "AAPL",
                    "--resolution",
                    "1d",
                    "--start",
                    "1670000000",
                    "--end",
                    self.NOW,
                    "--ssd-capacity",
                    "100",
                    "--normalization",
                    "split-adjusted",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                env=self._clean_env(),
            )
            self.assertNotEqual(result.returncode, 0, "uncovered split-adjusted fails closed")
            self.assertNotIn("tier:cold-read", result.stdout, "split-adjusted is not tiered")


@unittest.skipUnless(
    TIER_CLI.exists() and COLD_CLI.exists(),
    "cargo-built data008_tier_cli + data009_cold_read_cli required (run `cargo build -p atp-data`)",
)
class ColdReadEndToEndTest(unittest.TestCase):
    """Drive the operator CLIs over real on-disk tiers: ingest a cold batch, archive it off SSD, then
    prove the cold read transparently serves from NAS, caches on SSD, honours the cap, and evicts
    without touching hot data."""

    NOW = "1700000000"
    COLD_TS = "1680000000"

    def _run(self, cli: Path, *args: str) -> dict:
        result = subprocess.run(
            [str(cli), *args], cwd=ROOT, check=False, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                out.setdefault(key, value)  # first occurrence (scalar fields precede record: lines)
        return out

    def test_cold_read_failover_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ssd = Path(tmp) / "ssd"
            nas = Path(tmp) / "nas"
            ssd.mkdir()
            nas.mkdir()

            # 1) Ingest a COLD daily batch (SSD-first + NAS sync).
            ingest = self._run(
                TIER_CLI,
                "ingest",
                "--ssd",
                str(ssd),
                "--nas",
                str(nas),
                "--kind",
                "daily-equity-bar",
                "--event-ts",
                self.COLD_TS,
            )
            self.assertEqual(ingest["nas_sync"], "synced")
            self.assertEqual(ingest["ssd_inserted"], "2")

            # 2) Archive cold data off SSD (kept on NAS).
            archived = self._run(
                TIER_CLI, "archive-cold", "--ssd", str(ssd), "--nas", str(nas), "--now", self.NOW
            )
            self.assertEqual(archived["archived"], "2")

            # 3) SSD primary now MISSES the cold data; NAS retains it.
            report = self._run(
                TIER_CLI, "report", "--ssd", str(ssd), "--nas", str(nas), "--now", self.NOW
            )
            self.assertEqual(report["ssd_total"], "0")
            self.assertEqual(report["nas_total"], "2")

            # 4) Cold-read query for AAPL 1d → transparent NAS fallback + cache on SSD (cap 20% of 100).
            q1 = self._run(
                COLD_CLI,
                "query",
                "--ssd",
                str(ssd),
                "--nas",
                str(nas),
                "--symbol",
                "AAPL",
                "--resolution",
                "1d",
                "--start",
                "1670000000",
                "--end",
                self.NOW,
                "--now",
                self.NOW,
                "--ssd-capacity",
                "100",
            )
            self.assertEqual(q1["records"], "1")
            self.assertEqual(q1["served_from_ssd"], "0")
            self.assertEqual(q1["served_from_nas"], "1")
            self.assertEqual(q1["newly_cached"], "1")
            self.assertEqual(q1["nas_reachable"], "true")
            self.assertEqual(q1["cold_cache_capacity"], "20")
            self.assertEqual(q1["cold_cache_within_cap"], "true")

            # 5) Second identical query → a CACHE hit; NAS not needed for that record.
            q2 = self._run(
                COLD_CLI,
                "query",
                "--ssd",
                str(ssd),
                "--nas",
                str(nas),
                "--symbol",
                "AAPL",
                "--resolution",
                "1d",
                "--start",
                "1670000000",
                "--end",
                self.NOW,
                "--now",
                self.NOW,
                "--ssd-capacity",
                "100",
            )
            self.assertEqual(q2["served_from_cache"], "1")
            self.assertEqual(q2["served_from_nas"], "0")

            # 6) cache-report confirms the cache is within its cap.
            rep = self._run(
                COLD_CLI,
                "cache-report",
                "--ssd",
                str(ssd),
                "--nas",
                str(nas),
                "--ssd-capacity",
                "100",
            )
            self.assertEqual(rep["cold_cache_entries"], "1")
            self.assertEqual(rep["cold_cache_capacity"], "20")
            self.assertEqual(rep["within_cap"], "true")

            # 7) evict-cache drains the cold-read cache to 0 (hot data untouched — SSD primary is empty
            #    here anyway, but the point is eviction operates only on the cache).
            ev = self._run(
                COLD_CLI,
                "evict-cache",
                "--ssd",
                str(ssd),
                "--nas",
                str(nas),
                "--ssd-capacity",
                "100",
                "--max-entries",
                "0",
            )
            self.assertEqual(ev["evicted"], "1")
            self.assertEqual(ev["cold_cache_entries"], "0")

    def test_cap_enforced_end_to_end(self) -> None:
        """With capacity 10 and 20% share (cap 2), a five-record cold read serves all five from NAS
        but caches at most 2 — the cache never exceeds the configurable SSD share."""
        with tempfile.TemporaryDirectory() as tmp:
            ssd = Path(tmp) / "ssd"
            nas = Path(tmp) / "nas"
            ssd.mkdir()
            nas.mkdir()

            # Ingest five distinct cold daily dates, then archive them all off SSD.
            for i in range(5):
                event_ts = str(1680000000 + i * 86400)
                self._run(
                    TIER_CLI,
                    "ingest",
                    "--ssd",
                    str(ssd),
                    "--nas",
                    str(nas),
                    "--kind",
                    "daily-equity-bar",
                    "--event-ts",
                    event_ts,
                )
            self._run(
                TIER_CLI, "archive-cold", "--ssd", str(ssd), "--nas", str(nas), "--now", self.NOW
            )

            # Query the whole cold range for AAPL: 5 dates on NAS, cap = floor(10 * 20 / 100) = 2.
            q = self._run(
                COLD_CLI,
                "query",
                "--ssd",
                str(ssd),
                "--nas",
                str(nas),
                "--symbol",
                "AAPL",
                "--resolution",
                "1d",
                "--start",
                "1670000000",
                "--end",
                self.NOW,
                "--now",
                self.NOW,
                "--ssd-capacity",
                "10",
                "--cache-share",
                "20",
            )
            self.assertEqual(q["records"], "5", "all five served transparently")
            self.assertEqual(q["served_from_nas"], "5")
            self.assertEqual(q["cold_cache_capacity"], "2")
            self.assertEqual(q["cold_cache_entries"], "2", "cache capped at the 20% share")
            self.assertEqual(q["cold_cache_within_cap"], "true")
            self.assertGreaterEqual(int(q["cache_evicted"]), 3)


if __name__ == "__main__":
    unittest.main()
