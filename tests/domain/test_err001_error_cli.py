"""SRS-ERR-001 / SyRS SYS-64 -- the structured order-error envelope operator CLI is safe and honest.

L7 domain (safety) test, paired with the err001_error_envelope_cli operator surface. The structured
error envelope is a trading-safety boundary: every order-submission rejection must hand the operator
a complete, classifiable record -- a SyRS-defined category, an error type, a human-readable message,
and the ORIGINAL order parameters unchanged -- and a rejected order must reach NO brokerage. If a
reject path could drop a field, mutate the original order, or leak a broker call, the operator would
either lose the audit trail needed to act on a blocked order or have an unintended order placed. The
safety core of SRS-ERR-001 is therefore: the SyRS SYS-64 category vocabulary is total + stable, every
execution-boundary reject path yields a complete envelope with the original order intact, and no
rejected order produces an IB side effect. The operator binary err001_error_envelope_cli makes that
falsifiable at the workflow an operator drives (categories -> all-categories-mapped:true; envelope ->
envelope-complete:true; no-broker -> no-ib-side-effect:true). This test proves the invariant from
three angles:

  1. Behavioral -- it shells out to the Rust integration test
     ``crates/atp-execution/tests/srs_err_001_error_envelope_cli.rs`` (which drives the
     err001_error_envelope_cli binary in fresh OS processes) and asserts the category vocabulary is
     total, every reject path produces a complete envelope, no reject path reaches a brokerage, and an
     authorized (success) submission makes every reject-proof subcommand fail closed with no proof.

  2. Structural (non-vacuity) -- it asserts, via ``tools/error_handling_check.py``, that the CLI drives
     the REAL execution engine (not a hand-rolled stand-in that could agree with itself), prints every
     `:true` proof headline, and carries a fail-closed path -- each guard shown non-vacuous by a
     mutation that must be caught.

  3. Scope honesty -- it pins that the contract names BOTH operator surfaces, still states the
     feature stays passes:false for the ONE remaining reason (no REAL IB gateway rejection has been
     observed becoming a StructuredOrderError; that is the operator-gated
     srs_err_001_broker_envelope_live test), no longer advertises the now-closed SRS-EXE-006 as
     blocking, and names the genuinely ADJACENT category owners (the data layer, the market-data
     subscription manager, the orchestrator/kill-switch gates) as SEPARATE requirements NOT part of
     SRS-ERR-001's acceptance criterion -- so a later edit cannot silently re-inflate or deflate the
     scope.

The broker-side half of SYS-64 (INVALID_SYMBOL / INSUFFICIENT_BUYING_POWER / RATE_LIMITED) is proven
by the sibling L7 ``tests/domain/test_err001_broker_envelope.py``.
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

from error_handling_check import (  # noqa: E402
    ErrorHandlingCheckError,
    check_error_cli,
    cli_source,
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
            "atp-execution",
            "--test",
            "srs_err_001_error_envelope_cli",
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
# Behavioral -- category vocabulary total; envelope complete; reject paths reach no broker
# --------------------------------------------------------------------------- #


def test_category_vocabulary_is_total_and_stable() -> None:
    # Every SyRS SYS-64 OrderErrorCategory maps to a distinct, non-empty, UPPER_SNAKE wire string --
    # all-categories-mapped:true.
    _assert_one_passed(
        _run_cargo_test("categories_maps_every_syrs_64_category"),
        "SRS-ERR-001 category vocabulary total",
    )


def test_every_reject_path_yields_a_complete_envelope() -> None:
    # Each execution-boundary reject path returns a structured error carrying its category, a non-empty
    # type, a non-empty message, and the unchanged original order -- envelope-complete:true.
    _assert_one_passed(
        _run_cargo_test("envelope_is_complete_on_every_reject_path"),
        "SRS-ERR-001 envelope complete",
    )


def test_no_reject_path_reaches_a_brokerage() -> None:
    # The safety core: a sweep of every reject path proves each is a structured rejection with ZERO
    # broker calls -- a rejected order never reaches IB (no-ib-side-effect:true).
    _assert_one_passed(
        _run_cargo_test("reject_paths_make_no_broker_calls"),
        "SRS-ERR-001 no IB side effect",
    )


def test_authority_gate_enforces_single_live_designation() -> None:
    # The production authority gate (route_order) rejects a non-designated strategy with a structured
    # envelope and no broker call -- both with no live strategy and with a DIFFERENT strategy live --
    # while the single designated live strategy is authorized (reaches the broker). authority-enforced.
    _assert_one_passed(
        _run_cargo_test("authority_enforces_single_live_designation"),
        "SRS-ERR-001 authority gate enforced",
    )


def test_authorized_submission_fails_closed_on_every_proof_subcommand() -> None:
    # An authorized (Live+Connected+Fresh / designated-live) submission is not a rejection and
    # legitimately reaches the broker, so every reject-proof subcommand must fail closed with no proof
    # line -- the proofs are contingent on a genuine rejection.
    _assert_one_passed(
        _run_cargo_test("authorized_fault_fails_closed_on_every_proof_subcommand"),
        "SRS-ERR-001 authorized fails closed",
    )


# --------------------------------------------------------------------------- #
# Structural -- the CLI guards are real (non-vacuous)
# --------------------------------------------------------------------------- #


def test_cli_drives_the_real_execution_engine() -> None:
    config = load_config()
    # The operator binary must drive the REAL engine, so the envelope proof runs over the real
    # submit_live_order, not a hand-rolled echo that could agree with itself. A mutation that FULLY
    # removes the engine token must be caught.
    check_error_cli(config, cli_source(config))
    for token, replacement in (
        ("ExecutionEngine", "StubEngine"),
        ("submit_live_order", "fake_submit"),
        ("route_order", "fake_route"),
        ("OrderLedger", "FakeLedger"),
    ):
        mutated = cli_source(config).replace(token, replacement)
        with pytest.raises(ErrorHandlingCheckError):
            check_error_cli(config, mutated)


def test_cli_prints_every_proof_headline() -> None:
    config = load_config()
    # Dropping any `:true` proof headline would hide an unproven acceptance half; it must be caught.
    for proof in (
        "all-categories-mapped:true",
        "envelope-complete:true",
        "no-ib-side-effect:true",
        "authority-enforced:true",
    ):
        mutated = cli_source(config).replace(proof, "renamed:true")
        with pytest.raises(ErrorHandlingCheckError):
            check_error_cli(config, mutated)


def test_cli_fail_closed_path_is_real() -> None:
    config = load_config()
    # Removing the fail-closed path would let a success-path submission produce a reject proof; it must
    # be caught.
    mutated = cli_source(config).replace("inject=authorized", "inject=allowed")
    with pytest.raises(ErrorHandlingCheckError):
        check_error_cli(config, mutated)


# --------------------------------------------------------------------------- #
# Scope honesty -- the contract names both surfaces and the ONE remaining blocking owner
# --------------------------------------------------------------------------- #


def test_scope_names_both_surfaces_and_the_remaining_blocking_owner() -> None:
    # An operator must read an HONEST scope. SESSION 65 recorded the blocking gap as "the SyRS SYS-64
    # broker-validation categories are vocabulary-only, pending the deferred IB adapter
    # (SRS-EXE-006)". SRS-EXE-006 is now passes:true and err001_broker_envelope_cli gives those
    # categories a production construction site, so that deferral is REALIZED and must no longer be
    # advertised as blocking. What remains is strictly narrower: no test has observed a REAL IB
    # gateway rejection become a StructuredOrderError.
    #
    # The contract must therefore (1) name BOTH operator binaries, (2) still state the feature stays
    # passes:false -- with the honest reason -- and (3) name the operator-gated live test as the ONE
    # blocking deferral, so a later edit can neither re-inflate the scope back onto SRS-EXE-006 nor
    # quietly drop the remaining gap.
    config = load_config()
    block = config["error_handling_contract"]
    description = block["description"]
    assert "err001_error_envelope_cli" in description
    assert "err001_broker_envelope_cli" in description
    assert "stays passes:false" in description
    # The realized categories are named as realized, not as pending vocabulary.
    for category in ("INVALID_SYMBOL", "INSUFFICIENT_BUYING_POWER", "RATE_LIMITED"):
        assert category in description, category
    assert "srs_err_001_broker_envelope_live" in description
    assert "vocabulary-only" not in description, (
        "the SyRS SYS-64 broker-validation categories now have a production construction site; "
        "describing them as vocabulary-only understates what shipped"
    )

    deferred = {entry["feature"]: entry["what"] for entry in block["deferred"]}
    # The blocking deferral is now SRS-ERR-001's own live leg, NOT the (closed) IB adapter.
    assert "SRS-ERR-001" in deferred, "the remaining live-observation gap must be named"
    assert "passes:false" in deferred["SRS-ERR-001"]
    assert "ATP_RUN_INTEGRATION" in deferred["SRS-ERR-001"]
    assert "SRS-EXE-006" not in deferred, (
        "SRS-EXE-006 is passes:true and its broker-validation envelopes are realized; "
        "keeping it in deferred[] would misreport a closed dependency as blocking"
    )
    # The genuinely ADJACENT owners stay deferred -- they are separate requirements, not part of
    # SRS-ERR-001's acceptance criterion.
    for owner in ("SRS-MD-002", "SRS-DATA-013"):
        assert owner in deferred, owner
