"""SRS-BT-005 / SyRS SYS-17, SYS-36, SYS-37 -- the benchmark comparison defaults to SPY,
computes alpha/beta against the selected benchmark, identifies the benchmark in its
report, stays broker-independent, is deterministic, and fails closed on a malformed
resolution.

L7 domain (safety) test. The acceptance criterion's safety core is that the benchmark an
operator compares a strategy against -- and the alpha/beta they rank and size capital on
-- is *trustworthy*: a run that selects nothing must be compared against and clearly
identify SPY (never silently compared against an empty or wrong benchmark); the
comparison must be computed against the SAME benchmark the report identifies (a source
that substitutes a different benchmark must fail closed, never quietly mislabel the
comparison); a malformed or misaligned resolved series must fail closed rather than
produce a partial-overlap alpha/beta; the comparison must be deterministic; and the
surface must be independent of the IB account. A leak in any of these is a
trading-decision safety bug: a mislabeled or diverging benchmark comparison would
mis-rank a strategy in the Reservoir or misstate relative performance on the dashboard.
This test proves the invariant from three angles:

  1. Behavioral -- it shells out to the Rust integration test
     ``crates/atp-simulation/tests/srs_bt_005_benchmark.rs`` and asserts that an
     unselected benchmark defaults to and identifies SPY, that alpha/beta are computed
     against a user-selected benchmark, that the report identifies the benchmark and its
     excess return, that repeated comparisons are identical (determinism), and that a
     wrong-symbol or misaligned source fails closed.

  2. Structural (broker independence) -- it asserts, via ``tools/benchmark_check.py``,
     that the ``atp-simulation`` crate declares no dependency on the live/broker path
     (``atp-execution`` / ``atp-adapters``) and that the ``benchmark`` module leaks no
     vendor SDK token, so the comparison cannot be reconciled against the IB account.

  3. Structural (determinism + fail-closed trust boundary) -- it asserts the module uses
     no nondeterminism source, re-validates the resolved series at the trust boundary
     before any metric, and verifies every comparison ratio is finite.

Each structural guard is checked for non-vacuity: an injected broker dependency, a
leaked vendor token, an injected nondeterminism source, a removed trust-boundary guard,
and a removed finite check are each shown to be caught.
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

from benchmark_check import (  # noqa: E402
    BenchmarkCheckError,
    benchmark_source,
    cargo_source,
    check_determinism,
    check_nan_guard,
    check_no_broker_dependency,
    check_resolved_identity,
    check_run_window_binding,
    check_source_failure,
    check_trust_boundary,
    check_vendor_isolation,
    load_config,
)


def _run_cargo_test(test_name: str) -> subprocess.CompletedProcess[str]:
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
            "srs_bt_005_benchmark",
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


def test_compare_defaults_to_spy() -> None:
    # The safety core: a run that selects no benchmark is compared against and identifies
    # SPY (SYS-17), never an empty or absent benchmark.
    _assert_one_passed(
        _run_cargo_test("srs_bt_005_compare_defaults_to_spy"),
        "SRS-BT-005 SPY default",
    )


def test_alpha_beta_against_selected_benchmark() -> None:
    # alpha/beta are computed against the user-selected benchmark, and the report
    # identifies it (not the default).
    _assert_one_passed(
        _run_cargo_test("srs_bt_005_alpha_beta_against_selected_benchmark"),
        "SRS-BT-005 selected-benchmark alpha/beta",
    )


def test_report_identifies_benchmark_and_excess_return() -> None:
    # The report carries the benchmark identity and a strategy-vs-benchmark contrast, so
    # an operator can see which benchmark the comparison is against.
    _assert_one_passed(
        _run_cargo_test("srs_bt_005_report_identifies_benchmark_and_excess_return"),
        "SRS-BT-005 report identification",
    )


def test_compare_is_deterministic() -> None:
    # Identical inputs must produce identical comparisons, or a ranking would be unstable.
    _assert_one_passed(
        _run_cargo_test("srs_bt_005_compare_is_deterministic"),
        "SRS-BT-005 determinism",
    )


def test_fails_closed_on_substituted_benchmark_series() -> None:
    # Safety (Codex R3): a source that returns a well-formed series labeled with a
    # different benchmark than the selection is rejected -- identity is bound to the
    # RETURNED data and checked after the fetch, so the comparison can never identify one
    # benchmark while computing against another's levels.
    _assert_one_passed(
        _run_cargo_test("srs_bt_005_fails_closed_on_substituted_benchmark_series"),
        "SRS-BT-005 substituted-series fail-closed",
    )


def test_fails_closed_on_misaligned_source() -> None:
    # Negative control: a resolved series that cannot align period-for-period is rejected,
    # never silently compared against a partial overlap.
    _assert_one_passed(
        _run_cargo_test("srs_bt_005_fails_closed_on_misaligned_source"),
        "SRS-BT-005 misaligned fail-closed",
    )


def test_fails_closed_on_foreign_window() -> None:
    # Safety: the comparison is bound to the strategy run window, so a stale/foreign window
    # that does not contain the equity curve is rejected -- the benchmark can never be
    # measured over a different period than the strategy (Codex R1).
    _assert_one_passed(
        _run_cargo_test("srs_bt_005_fails_closed_on_foreign_window"),
        "SRS-BT-005 run-window binding",
    )


def test_propagates_source_unavailable() -> None:
    # Safety: an operational data-layer read failure (timeout / unavailable / not-found /
    # stale) surfaces a typed SourceFailure so a caller can retry / alert / fail closed,
    # rather than being misclassified as a malformed series (Codex R2).
    _assert_one_passed(
        _run_cargo_test("srs_bt_005_propagates_source_unavailable"),
        "SRS-BT-005 source-unavailable degraded path",
    )


def test_benchmark_crate_has_no_broker_dependency() -> None:
    config = load_config()
    # The real Cargo.toml must declare no live/broker-path dependency, so the comparison
    # is independent of the IB account at the crate boundary.
    check_no_broker_dependency(config, cargo_source(config))
    # ...and the guard must not be vacuous: an injected broker dep is caught.
    mutated = cargo_source(config) + '\natp-execution = { path = "../atp-execution" }\n'
    with pytest.raises(BenchmarkCheckError):
        check_no_broker_dependency(config, mutated)


def test_benchmark_module_leaks_no_vendor_token() -> None:
    config = load_config()
    # The real benchmark module must carry no vendor SDK token.
    check_vendor_isolation(config, benchmark_source(config))
    # ...and the guard must not be vacuous: a leaked token is caught.
    mutated = benchmark_source(config) + "\n// benchmark mirrored to ib_insync under the hood\n"
    with pytest.raises(BenchmarkCheckError):
        check_vendor_isolation(config, mutated)


def test_comparison_is_deterministic() -> None:
    config = load_config()
    # The real module uses no parallelism/RNG/clock, so the comparison is order-independent.
    check_determinism(config, benchmark_source(config))
    # ...and the guard must not be vacuous: an injected parallel iterator is caught.
    mutated = benchmark_source(config) + "\nfn _leak() { let _ = vec![0].par_iter(); }\n"
    with pytest.raises(BenchmarkCheckError):
        check_determinism(config, mutated)


def test_compare_fails_closed_at_the_trust_boundary() -> None:
    config = load_config()
    # The real module re-validates the resolved series (symbol, length, alignment,
    # positivity) before any metric and re-validates through metrics::compute.
    check_trust_boundary(config, benchmark_source(config))
    # ...and the guard must not be vacuous: dropping the length check (which would let a
    # short series compare against a partial overlap) is caught.
    mutated = benchmark_source(config).replace(
        "BenchmarkError::SourceLengthMismatch", "BenchmarkError::EmptyEquityCurve"
    )
    with pytest.raises(BenchmarkCheckError):
        check_trust_boundary(config, mutated)


def test_module_verifies_comparison_ratios_are_finite() -> None:
    config = load_config()
    # The real module verifies each comparison ratio is finite before returning it, so a
    # NaN/inf never leaks into a ranking or dashboard.
    check_nan_guard(config, benchmark_source(config))
    # ...and the guard must not be vacuous: dropping the finite check is caught.
    mutated = benchmark_source(config).replace("is_finite()", "is_nan()")
    with pytest.raises(BenchmarkCheckError):
        check_nan_guard(config, mutated)


def test_comparison_is_bound_to_the_run_window() -> None:
    config = load_config()
    # The real module binds the comparison to the run window (deriving the baseline as-of
    # window.start and rejecting an equity mark outside it), so a stale/unrelated baseline
    # cannot measure the benchmark over a different period than the strategy.
    check_run_window_binding(config, benchmark_source(config))
    # ...and the guard must not be vacuous: dropping the window-coherence check is caught.
    mutated = benchmark_source(config).replace(
        "BenchmarkError::EquityMarkOutsideWindow", "BenchmarkError::EmptyEquityCurve"
    )
    with pytest.raises(BenchmarkCheckError):
        check_run_window_binding(config, mutated)


def test_source_surfaces_typed_operational_failures() -> None:
    config = load_config()
    # The real module gives the resolution port a typed operational-failure contract
    # (timeout / unavailable / not-found / stale) so a read failure is not hidden behind a
    # malformed series.
    check_source_failure(config, benchmark_source(config))
    # ...and the guard must not be vacuous: dropping the stale-data outcome is caught.
    mutated = benchmark_source(config).replace("    StaleData,", "", 1)
    with pytest.raises(BenchmarkCheckError):
        check_source_failure(config, mutated)


def test_benchmark_identity_is_bound_to_returned_data() -> None:
    config = load_config()
    # The real module binds identity to the returned ResolvedBenchmark and validates the
    # symbol after the fetch, so a source cannot return one benchmark's levels while the
    # report identifies another.
    check_resolved_identity(config, benchmark_source(config))
    # ...and the guard must not be vacuous: dropping the post-fetch returned-symbol check
    # is caught.
    mutated = benchmark_source(config).replace("resolved.symbol != benchmark.symbol()", "false")
    with pytest.raises(BenchmarkCheckError):
        check_resolved_identity(config, mutated)
