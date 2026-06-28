"""SRS-BT-010 / SyRS SYS-62 -- a backtest produces deterministic results for identical inputs:
repeated runs yield bit-identical trade logs, equity curves, and metrics, and a nondeterminism
source is *caught and localized*, never silently averaged away.

L7 domain (safety) test. The acceptance criterion's safety core is that the backtest an
operator promotes a strategy on is *reproducible*: if two identical runs could produce different
trade logs or equity curves, a strategy's apparent quality -- and the go-live decision -- would
depend on run order, the platform's floating-point summation order, or a stray random value, and
an operator could promote a strategy on a backtest nobody can reproduce. The determinism surface
makes that guarantee falsifiable. This test proves the invariant from two angles:

  1. Behavioral -- it shells out to the Rust integration test
     ``crates/atp-simulation/tests/srs_bt_010_determinism.rs`` and asserts that identical inputs
     yield a bit-identical RunDigest (across repeated runs, across source-iteration-order
     shuffles, and spanning the metric family), and -- critically -- that a strategy consulting
     cross-run mutable state is CAUGHT and localized rather than reported reproducible.

  2. Structural -- it asserts, via ``tools/determinism_check.py``, that the result digest is
     integer-EXACT (no f64 in the money path), that the metric ratios fold through their exact
     ``to_bits`` payload, that the harness cross-checks the canonical digests, that
     ``DeterminismError`` carries its localized variants, that the verifier itself uses no
     parallelism/RNG/clock, that ``atp-simulation`` declares no broker/live/orchestrator
     dependency, and that the module leaks no vendor SDK token.

Each structural guard is checked for non-vacuity: a float leaked into the result digest, a
non-bit-exact metric fold, a removed digest cross-check, a dropped error variant, an injected
nondeterminism source, an injected broker dependency, and a leaked vendor token are each shown
to be caught.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPO_ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from determinism_check import (  # noqa: E402
    DeterminismCheckError,
    cargo_source,
    check_cross_process_cli,
    check_determinism,
    check_error_enum,
    check_harness,
    check_manifest,
    check_metrics_digest,
    check_no_broker_dependency,
    check_result_digest,
    check_vendor_isolation,
    load_config,
    module_source,
)


def _run_cargo_test(
    test_name: str, test_file: str = "srs_bt_010_determinism"
) -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust integration test")
    return subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-simulation",
            "--test",
            test_file,
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_one_passed(result: subprocess.CompletedProcess[str], label: str) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"{label} failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined, f"unexpected cargo test output for {label}:\n{combined}"


# --------------------------------------------------------------------------- #
# Behavioral -- the verifier reproduces identical runs and catches nondeterminism
# --------------------------------------------------------------------------- #


def test_identical_inputs_produce_an_identical_digest() -> None:
    # The safety core: two identical runs are bit-for-bit identical and fingerprint the same.
    _assert_one_passed(
        _run_cargo_test("identical_runs_produce_identical_results_and_digest"),
        "SRS-BT-010 identical-run digest",
    )


def test_a_nondeterministic_strategy_is_caught_and_localized() -> None:
    # Critical: a strategy that consults cross-run mutable state must be CAUGHT (not silently
    # producing a different, equally-"valid" result) and the divergence localized to the first
    # fill -- so an operator can never unknowingly promote on an unreproducible backtest.
    _assert_one_passed(
        _run_cargo_test("verify_reproducible_catches_a_nondeterministic_strategy"),
        "SRS-BT-010 nondeterminism caught",
    )


def test_digest_is_invariant_to_source_iteration_order() -> None:
    # The engine stable-sorts the replay window, so the result must not depend on the order the
    # data source hands over bars -- the criterion's "ordering does not introduce nondeterminism".
    _assert_one_passed(
        _run_cargo_test("digest_is_invariant_to_source_iteration_order"),
        "SRS-BT-010 order invariance",
    )


def test_digest_spans_the_metric_family() -> None:
    # The criterion names three artifacts (trade logs, equity curves, AND metrics); digest_run
    # bundles all three, and bundling metrics changes the fingerprint (so they cannot be dropped).
    _assert_one_passed(
        _run_cargo_test("digest_run_spans_the_metric_family"),
        "SRS-BT-010 metric-family digest",
    )


def test_metrics_harness_verifies_all_three_artifacts() -> None:
    # The full acceptance-test harness runs the engine twice AND computes + compares the metric
    # family (the crate's own deterministic metrics::compute, over immutable inputs) for both
    # runs, returning a digest spanning trade log + equity curve + metrics. There is no caller
    # reduction, so no mutable state is shared between the metric computation and the replays --
    # which is what makes the metric clause safe to verify in any ordering.
    _assert_one_passed(
        _run_cargo_test("metrics_harness_verifies_all_three_artifacts"),
        "SRS-BT-010 three-artifact harness",
    )


def test_runs_match_rejects_provenance_divergence() -> None:
    # Two results from different catalogs or date ranges are not the same run, even when their
    # trades + equity coincide -- runs_match must reject the provenance mismatch.
    _assert_one_passed(
        _run_cargo_test("runs_match_rejects_provenance_divergence"),
        "SRS-BT-010 provenance comparison",
    )


def test_property_sweep_reproducibility_and_order_invariance() -> None:
    # Over many fixed-seed-but-varied inputs: every run reproduces and is order-invariant.
    _assert_one_passed(
        _run_cargo_test("property_sweep_reproducibility_and_order_invariance"),
        "SRS-BT-010 property sweep",
    )


# --------------------------------------------------------------------------- #
# Behavioral (cross-process) -- the platform-generated-randomness closure
# --------------------------------------------------------------------------- #


def test_two_fresh_processes_produce_an_identical_digest() -> None:
    # The safety core of the platform-randomness clause: the in-process double-run cannot see
    # nondeterminism that is stable within a process but varies across a restart. This spawns the
    # bt010_repro_cli binary in TWO separate OS processes and asserts byte-identical fingerprints --
    # the only way an operator can trust that a backtest reproduces across restarts, not just within
    # one run of the binary.
    _assert_one_passed(
        _run_cargo_test(
            "cross_process_identical_inputs_produce_identical_digests",
            "srs_bt_010_cross_process",
        ),
        "SRS-BT-010 cross-process identity",
    )


def test_cross_process_run_digest_is_independent_of_the_rng_seed() -> None:
    # "platform-generated random values do not introduce nondeterminism", made observable: two fresh
    # processes with DIFFERENT seeds produce the same run-digest (the engine consumes no platform
    # randomness) -- the seed only changes the provenance manifest.
    _assert_one_passed(
        _run_cargo_test(
            "cross_process_seed_changes_manifest_but_not_run_digest",
            "srs_bt_010_cross_process",
        ),
        "SRS-BT-010 cross-process seed independence",
    )


def test_cross_process_digest_is_sensitive_to_inputs() -> None:
    # The identity check is non-vacuous: a different input (lot) changes the run-digest across
    # processes, so the fingerprint is a function of the run, not a constant.
    _assert_one_passed(
        _run_cargo_test(
            "cross_process_different_input_changes_the_run_digest",
            "srs_bt_010_cross_process",
        ),
        "SRS-BT-010 cross-process input sensitivity",
    )


# --------------------------------------------------------------------------- #
# Structural -- each guard is real (non-vacuous)
# --------------------------------------------------------------------------- #


def test_result_digest_is_integer_exact() -> None:
    config = load_config()
    # The real result digest folds money as exact i64 minor units -- no f64 / to_bits.
    check_result_digest(config, module_source(config))
    # ...and the guard must not be vacuous: a float leaked into the money path is caught.
    mutated = module_source(config).replace(
        "push_count(out, result.trade_log.len());",
        "push_count(out, result.trade_log.len());\n    let _leak: f64 = 0.0;",
        1,
    )
    with pytest.raises(DeterminismCheckError):
        check_result_digest(config, mutated)


def test_metric_ratios_fold_bit_exactly() -> None:
    config = load_config()
    # The real metric fold uses to_bits, so a +0.0/-0.0 or NaN-bit difference is caught and no
    # lexical float-formatting nondeterminism enters the digest.
    check_metrics_digest(config, module_source(config))
    # ...and the guard must not be vacuous: a to_string fold is caught.
    mutated = module_source(config).replace(
        "line.push_str(&v.to_bits().to_string());",
        "line.push_str(&v.to_string());",
        1,
    )
    with pytest.raises(DeterminismCheckError):
        check_metrics_digest(config, mutated)


def test_harness_cross_checks_the_digests() -> None:
    config = load_config()
    # The real harness cross-checks the canonical digests, so a BacktestResult field absent from
    # the structural compare cannot diverge while the run is reported reproducible.
    check_harness(config, module_source(config))
    # ...and the guard must not be vacuous: removing the cross-check is caught.
    mutated = module_source(config).replace(
        "return Err(DeterminismError::Digest);",
        "return Ok(digest);",
        1,
    )
    with pytest.raises(DeterminismCheckError):
        check_harness(config, mutated)


def test_metrics_harness_actually_compares_metrics() -> None:
    config = load_config()
    # The metric clause must be VERIFIED, not assumed: the metrics harness computes the metric
    # family for both runs and compares it. Dropping that comparison is the exact gap the first
    # review flagged, so the guard must catch it.
    check_harness(config, module_source(config))
    mutated = module_source(config).replace("metrics_match(&metrics_a, &metrics_b)?;", "", 1)
    with pytest.raises(DeterminismCheckError):
        check_harness(config, mutated)


def test_metrics_harness_uses_the_deterministic_metric_family() -> None:
    config = load_config()
    # The metric family is the crate's own deterministic metrics::compute over immutable inputs --
    # NOT a caller-supplied reduction that could share mutable state with the run (which is what
    # let the previous reviews construct false pass/fail metric scenarios). The guard must catch a
    # metric computation that abandons the deterministic family.
    check_harness(config, module_source(config))
    mutated = module_source(config).replace("compute_metrics(", "fabricate(", 1)
    with pytest.raises(DeterminismCheckError):
        check_harness(config, mutated)


def test_runs_match_compares_provenance() -> None:
    config = load_config()
    # runs_match must compare data_source + range, so two results from different catalogs or date
    # ranges are never reported identical merely because their trades + equity coincide.
    check_harness(config, module_source(config))
    mutated = module_source(config).replace("left.data_source != right.data_source", "false", 1)
    with pytest.raises(DeterminismCheckError):
        check_harness(config, mutated)


def test_scope_names_the_cross_process_closure_and_remaining_deferred_owners() -> None:
    # Safety: an operator must read an HONEST scope. The cross-process closure (the bt010_repro_cli
    # binary + the input-provenance manifest) is what closes the platform-generated-randomness
    # clause a same-process double-run cannot, so the contract must (1) name that binary + the
    # manifest as realized, (2) still describe the in-process verifier, and (3) name the genuinely
    # remaining deferred owners (the REST/dashboard surface, the real Python strategy host, and
    # stamping the digest onto the persisted record) without over-claiming the Python-host end to
    # end. This pins the scope honesty so a later edit cannot silently re-inflate or deflate it.
    config = load_config()
    block = config["backtest_determinism_contract"]
    description = block["description"]
    # The in-process verifier is still described, and the cross-process closure is now named.
    assert "IN-PROCESS" in description
    assert "bt010_repro_cli" in description
    assert "RunManifest" in description
    # The platform-generated-randomness clause and its closure are stated.
    assert "process-seeded nondeterminism" in description
    # The genuinely remaining deferred owners are named honestly (REST/UI, Python host, store stamp).
    deferred = " ".join(block["deferred"])
    assert "SRS-API-001" in deferred
    assert "Python strategy host" in deferred
    assert "SRS-BT-009 store integration" in deferred
    # The cross-process workflow + the manifest are NO LONGER in the deferred list (they are realized).
    assert "input-provenance manifest" not in deferred


def test_manifest_proves_inputs_were_identical() -> None:
    config = load_config()
    # The input-provenance manifest fingerprints every immutable-input clause SRS-BT-010 names, so a
    # verification PROVES the inputs matched rather than assuming the fixture is deterministic.
    check_manifest(config, module_source(config))
    # ...and the guard must not be vacuous: dropping the cost model from the manifest is caught (two
    # runs with different cost models would otherwise be reported as "identical inputs").
    mutated = module_source(config).replace("pub cost_config: CostConfig,", "", 1)
    with pytest.raises(DeterminismCheckError):
        check_manifest(config, mutated)


def test_cross_process_cli_is_registered() -> None:
    config = load_config()
    # The bt010_repro_cli binary is what makes the cross-process comparison possible; without a
    # registered bin the platform-randomness clause cannot be closed.
    check_cross_process_cli(config, cargo_source(config))
    # ...and the guard must not be vacuous: an unregistered bin is caught.
    mutated = cargo_source(config).replace('name = "bt010_repro_cli"', 'name = "x"', 1)
    with pytest.raises(DeterminismCheckError):
        check_cross_process_cli(config, mutated)


def test_error_enum_localizes_divergence() -> None:
    config = load_config()
    # The real DeterminismError localizes WHERE two runs diverged (a fill/equity index, length,
    # final equity, bars, a named metric, or the digest), so a nondeterminism source is pinpointed.
    check_error_enum(config, module_source(config))
    # ...and the guard must not be vacuous: dropping the digest cross-check variant is caught.
    mutated = module_source(config).replace("    Digest,\n}", "}", 1)
    with pytest.raises(DeterminismCheckError):
        check_error_enum(config, mutated)


def test_verifier_uses_no_nondeterminism_source() -> None:
    config = load_config()
    # The verifier itself uses no parallelism/RNG/clock, so it cannot introduce the very
    # nondeterminism it checks for.
    check_determinism(config, module_source(config))
    # ...and the guard must not be vacuous: an injected parallel iterator is caught.
    mutated = module_source(config) + "\nfn _leak() { let _ = vec![0].par_iter(); }\n"
    with pytest.raises(DeterminismCheckError):
        check_determinism(config, mutated)


def test_module_has_no_broker_or_orchestrator_dependency() -> None:
    config = load_config()
    # The real Cargo.toml declares no broker/live/orchestrator dependency, so the determinism
    # surface is self-contained over the backtest + metrics types.
    check_no_broker_dependency(config, cargo_source(config))
    # ...and the guard must not be vacuous: an injected dependency is caught.
    mutated = cargo_source(config) + '\natp-execution = { path = "../atp-execution" }\n'
    with pytest.raises(DeterminismCheckError):
        check_no_broker_dependency(config, mutated)


def test_module_leaks_no_vendor_token() -> None:
    config = load_config()
    # The real module carries no vendor SDK token (adapter isolation, SRS-ARCH-003).
    check_vendor_isolation(config, module_source(config))
    # ...and the guard must not be vacuous: a leaked token is caught.
    mutated = module_source(config) + "\n// run digests mirrored to ib_insync under the hood\n"
    with pytest.raises(DeterminismCheckError):
        check_vendor_isolation(config, mutated)
