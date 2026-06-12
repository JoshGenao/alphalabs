"""Contract tests for SRS-BT-010 (produce deterministic backtest results for identical inputs).

SRS-BT-010 / SyRS SYS-62 / StRS SN-1.02 -- repeated runs with identical inputs produce
identical trade logs, equity curves, and metrics; parallelism, floating-point ordering, and
platform random values do not introduce nondeterminism. This slice ships the deterministic-
backtest VERIFICATION surface in ``crates/atp-simulation`` (module ``determinism``); the
deferred halves (the end-to-end guarantee under the real Python strategy host, the operator
repeated-run workflow via SRS-API-001 / SRS-UI, and stamping the RunDigest onto each persisted
SRS-BT-009 record) keep ``feature_list.json`` at ``passes:false``.

Mirrors ``tests/test_factor_analysis_contract.py``: shells out to ``tools/determinism_check.py``,
then exercises each per-check function in-process, including negative spot-checks that mutate
the Rust source / lib.rs / Cargo.toml in memory and assert the contract actually catches the
regression (a dropped Display token, a renamed digest signature, a float leaked into the
integer-exact result digest, a non-bit-exact metric fold, a dropped digest cross-check, a
removed error variant, an injected nondeterminism source, a dropped lib re-export, an injected
broker dependency, and a leaked vendor token).
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

from determinism_check import (  # noqa: E402
    DeterminismCheckError,
    assert_determinism_static,
    cargo_source,
    check_determinism,
    check_digest_fns,
    check_error_enum,
    check_harness,
    check_metrics_digest,
    check_module_reexport,
    check_no_broker_dependency,
    check_result_digest,
    check_run_digest,
    check_vendor_isolation,
    lib_source,
    load_config,
    module_source,
)


class DeterminismScriptTest(unittest.TestCase):
    def test_srs_bt_010_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/determinism_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-BT-010 SDK-SURFACE PASS", result.stdout)
        for needle in (
            "declares RunDigest",
            "exposes `pub fn digest_result",
            "encode_result_body folds the trade log + equity curve as exact i64 minor units",
            "encode_metrics_body folds the eight dimensionless metric ratios via push_opt_f64",
            "verify_reproducible_with_metrics (all three artifacts, INTERLEAVED)",
            "runs_match (incl. data_source + range provenance)",
            "a nondeterministic metric reduction is caught even on identical results",
            "declares DeterminismError with 12 localized",
            "determinism module has no parallelism / RNG / clock token",
            "lib.rs re-exports `pub mod determinism;`",
            "Cargo.toml declares no dependency on the broker/live/orchestrator path",
            "determinism module is free of all 5 forbidden vendor SDK tokens",
            "feature_list.json keeps SRS-BT-010 passes:false",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = module_source(self.config)
        self.lib_src = lib_source(self.config)
        self.cargo_src = cargo_source(self.config)


class StaticCoverageTest(_Fixture):
    def test_all_static_checks_pass(self) -> None:
        evidence = assert_determinism_static(self.config)
        self.assertEqual(len(evidence), 10)
        self.assertTrue(all(isinstance(line, str) and line for line in evidence))


class RunDigestTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("RunDigest", check_run_digest(self.config, self.src))

    def test_dropped_display_token_is_caught(self) -> None:
        # Losing the stable `run-digest:` Display prefix would let the human-facing fingerprint
        # drift silently.
        mutated = self.src.replace('"run-digest:{:016x}"', '"{:016x}"', 1)
        with self.assertRaises(DeterminismCheckError) as ctx:
            check_run_digest(self.config, mutated)
        self.assertIn("run-digest:", str(ctx.exception))


class DigestFnsTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("digest_result", check_digest_fns(self.config, self.src))

    def test_dropped_metrics_param_is_caught(self) -> None:
        # Demoting digest_run's metrics parameter would silently drop the third named artifact
        # (metrics) from the cross-artifact fingerprint.
        mutated = self.src.replace(
            "metrics: Option<&PerformanceMetrics>", "metrics: Option<&()>", 1
        )
        with self.assertRaises(DeterminismCheckError) as ctx:
            check_digest_fns(self.config, mutated)
        self.assertIn("digest_run", str(ctx.exception))


class ResultDigestTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("exact i64 minor units", check_result_digest(self.config, self.src))

    def test_float_leaked_into_result_digest_is_caught(self) -> None:
        # The result digest must stay integer-exact: any f64/to_bits in the money path is a
        # float-formatting nondeterminism source (and a money-correctness leak).
        mutated = self.src.replace(
            "push_count(out, result.trade_log.len());",
            "push_count(out, result.trade_log.len());\n    let _leak: f64 = 0.0;",
            1,
        )
        with self.assertRaises(DeterminismCheckError) as ctx:
            check_result_digest(self.config, mutated)
        self.assertIn("integer-EXACT", str(ctx.exception))

    def test_dropped_integer_field_is_caught(self) -> None:
        mutated = self.src.replace(
            "push_i128(out, i128::from(result.final_equity_minor));",
            "// (final equity dropped)",
            1,
        )
        with self.assertRaises(DeterminismCheckError) as ctx:
            check_result_digest(self.config, mutated)
        self.assertIn("final_equity_minor", str(ctx.exception))


class MetricsDigestTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("push_opt_f64", check_metrics_digest(self.config, self.src))

    def test_non_bit_exact_metric_fold_is_caught(self) -> None:
        # Folding a ratio through to_string instead of to_bits reintroduces float-formatting
        # nondeterminism.
        mutated = self.src.replace(
            "line.push_str(&v.to_bits().to_string());",
            "line.push_str(&v.to_string());",
            1,
        )
        with self.assertRaises(DeterminismCheckError) as ctx:
            check_metrics_digest(self.config, mutated)
        self.assertIn("push_opt_f64", str(ctx.exception))

    def test_dropped_metric_is_caught(self) -> None:
        mutated = self.src.replace("push_opt_f64(out, metrics.win_rate);", "", 1)
        with self.assertRaises(DeterminismCheckError) as ctx:
            check_metrics_digest(self.config, mutated)
        self.assertIn("win_rate", str(ctx.exception))


class HarnessTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("verify_reproducible", check_harness(self.config, self.src))

    def test_dropped_digest_crosscheck_is_caught(self) -> None:
        # Removing the digest cross-check would let a BacktestResult field absent from
        # runs_match diverge while the harness still reports the run reproducible.
        mutated = self.src.replace(
            "return Err(DeterminismError::Digest);",
            "return Ok(digest);",
            1,
        )
        with self.assertRaises(DeterminismCheckError) as ctx:
            check_harness(self.config, mutated)
        self.assertIn("DeterminismError::Digest", str(ctx.exception))

    def test_single_run_is_caught(self) -> None:
        mutated = self.src.replace("let mut second = build_strategy();", "", 1)
        with self.assertRaises(DeterminismCheckError) as ctx:
            check_harness(self.config, mutated)
        self.assertIn("twice", str(ctx.exception))

    def test_dropped_metric_comparison_is_caught(self) -> None:
        # Dropping metrics_match from the metrics harness would let metric nondeterminism pass
        # while the harness reports success -- the exact gap the metric clause closes.
        mutated = self.src.replace("metrics_match(&metrics_a, &metrics_b)?;", "", 1)
        with self.assertRaises(DeterminismCheckError) as ctx:
            check_harness(self.config, mutated)
        self.assertIn("metrics_match", str(ctx.exception))

    def test_non_interleaved_metrics_is_caught(self) -> None:
        # Computing metrics A only after the second run begins (here: removing the early metrics-A
        # binding) masks a run-induced metric state change -- the high finding from the review.
        mutated = self.src.replace("let metrics_a = compute_metrics(&result_a)?;", "", 1)
        with self.assertRaises(DeterminismCheckError) as ctx:
            check_harness(self.config, mutated)
        self.assertIn("INTERLEAVE", str(ctx.exception))

    def test_runs_match_ignoring_provenance_is_caught(self) -> None:
        # runs_match must compare data_source; dropping the check would let two results from
        # different catalogs be reported identical.
        mutated = self.src.replace("left.data_source != right.data_source", "false", 1)
        with self.assertRaises(DeterminismCheckError) as ctx:
            check_harness(self.config, mutated)
        self.assertIn("provenance", str(ctx.exception))


class ErrorEnumTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("DeterminismError", check_error_enum(self.config, self.src))

    def test_dropped_variant_is_caught(self) -> None:
        mutated = self.src.replace("    Digest,\n}", "}", 1)
        with self.assertRaises(DeterminismCheckError) as ctx:
            check_error_enum(self.config, mutated)
        self.assertIn("Digest", str(ctx.exception))


class DeterminismTokenTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn(
            "no parallelism / RNG / clock token", check_determinism(self.config, self.src)
        )

    def test_injected_nondeterminism_source_is_caught(self) -> None:
        mutated = self.src.replace(
            "RunDigest(checksum(body.as_bytes()))",
            "RunDigest(checksum(body.as_bytes()) ^ rand::random::<u64>())",
            1,
        )
        with self.assertRaises(DeterminismCheckError) as ctx:
            check_determinism(self.config, mutated)
        self.assertIn("rand::", str(ctx.exception))


class ModuleReexportTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("re-exports", check_module_reexport(self.config, self.lib_src))

    def test_dropped_reexport_is_caught(self) -> None:
        mutated = self.lib_src.replace("pub mod determinism;", "mod determinism;", 1)
        with self.assertRaises(DeterminismCheckError) as ctx:
            check_module_reexport(self.config, mutated)
        self.assertIn("determinism", str(ctx.exception))


class NoBrokerDependencyTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("no dependency", check_no_broker_dependency(self.config, self.cargo_src))

    def test_injected_broker_dependency_is_caught(self) -> None:
        mutated = self.cargo_src.replace(
            "[dependencies]",
            '[dependencies]\natp-execution = { path = "../atp-execution" }',
            1,
        )
        with self.assertRaises(DeterminismCheckError) as ctx:
            check_no_broker_dependency(self.config, mutated)
        self.assertIn("atp-execution", str(ctx.exception))


class VendorIsolationTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("vendor", check_vendor_isolation(self.config, self.src))

    def test_leaked_vendor_token_is_caught(self) -> None:
        mutated = self.src.replace(
            "const DIGEST_MAGIC: &str",
            'const IB: &str = "ibapi";\nconst DIGEST_MAGIC: &str',
            1,
        )
        with self.assertRaises(DeterminismCheckError) as ctx:
            check_vendor_isolation(self.config, mutated)
        self.assertIn("ibapi", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
