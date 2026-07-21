"""SRS-ERR-001 / SyRS SYS-64 -- the BROKER-SIDE structured order-error envelope is safe and honest.

L7 domain (safety) test, paired with the err001_broker_envelope_cli operator surface. Where the
sibling ``test_err001_error_cli.py`` covers the reject paths reachable at the execution boundary,
this one covers the half SESSION 65 recorded as the blocking gap: when Interactive Brokers REJECTS a
submission, the operator must receive a complete, correctly-classified SRS-ERR-001 envelope.

Why this is a safety boundary, not a formatting concern. A live order rejected by the broker is a
position the operator believes they have and do not. The envelope is the only record they get, so:

  * a DROPPED rejection is an invisible failed order;
  * a rejection stripped of its vendor detail is unactionable (the operator cannot tell an
    unfundable order from an unknown symbol from a rate-limit backoff); and
  * a MISCLASSIFIED rejection is worse than an unclassified one, because it sends the operator to
    the wrong remedy with false confidence. That last one is a real regression this repo shipped: an
    unmapped broker rejection, and every local ``validate()`` failure, previously reported
    ``INVALID_SYMBOL``. The SRS-ERR-001 acceptance criterion requires a SyRS category only "when
    applicable", so borrowing an inapplicable one is a false claim about the failure.

This test proves the invariant from three angles:

  1. Behavioral -- it shells out to the Rust integration test
     ``crates/atp-orchestrator/tests/srs_err_001_broker_envelope_cli.rs`` (which drives the
     err001_broker_envelope_cli binary in fresh OS processes) and asserts each mapped vendor code
     yields the applicable SyRS SYS-64 category, an unmapped rejection is surfaced without being
     fabricated, the live and paper arms share one error contract, and an ACCEPTED submission makes
     every proof subcommand fail closed with no proof.

  2. Structural (non-vacuity) -- it asserts, via ``tools/error_handling_check.py``, that the CLI
     drives the REAL execution engine and the REAL IB adapter (not a hand-rolled classifier that
     could agree with itself), prints every `:true` proof headline, and carries a fail-closed path --
     each guard shown non-vacuous by a mutation that must be caught.

  3. Taxonomy guard (anti-regression) -- it scans the production sources and asserts no
     ``validate()``-failure site constructs ``InvalidSymbol``, so ``INVALID_SYMBOL`` keeps meaning
     exactly one thing: the broker says the symbol does not exist.

Scope: the classification and the envelope are proven over the REAL SRS-EXE-006 adapter with a
scripted transport supplying the vendor code + message a socket would carry. Observing a REAL
gateway emit these rejections is the operator-gated
``crates/atp-orchestrator/tests/srs_err_001_broker_envelope_live.rs``; this test asserts that gate
exists and fails closed without its env gate, but never runs it (it binds a fixed shared port).
"""

from __future__ import annotations

import re
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
    broker_cli_source,
    check_broker_envelope_cli,
    load_config,
)

L5_TEST = "srs_err_001_broker_envelope_cli"


def _run_cargo_test(test_name: str) -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust integration test")
    return subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-orchestrator",
            "--test",
            L5_TEST,
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_one_passed(result: subprocess.CompletedProcess[str], test_name: str) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"{test_name} failed:\n{combined}"
    assert "1 passed" in combined, f"{test_name} did not run (filtered out?):\n{combined}"


# --------------------------------------------------------------------------- #
# Behavioral -- the real binary, driven in fresh processes
# --------------------------------------------------------------------------- #


def test_broker_rejections_carry_the_syrs64_category() -> None:
    """Each vendor code the SRS-EXE-006 classifier maps yields a complete envelope."""
    name = "broker_rejections_map_to_syrs64_categories"
    _assert_one_passed(_run_cargo_test(name), name)


def test_unmapped_rejection_is_surfaced_never_fabricated() -> None:
    """An unmapped rejection is surfaced under BROKER_REJECTED, never relabelled INVALID_SYMBOL."""
    name = "unmapped_rejections_are_surfaced_never_fabricated"
    _assert_one_passed(_run_cargo_test(name), name)


def test_live_and_paper_share_one_error_contract() -> None:
    """SyRS SYS-64: the error contract is identical for live and paper execution modes."""
    name = "live_and_paper_arms_share_one_error_contract"
    _assert_one_passed(_run_cargo_test(name), name)


def test_accepted_submission_fails_closed_on_every_proof_subcommand() -> None:
    """An ACCEPTING transport rejects nothing, so no proof may be derived from it."""
    name = "accepted_fault_fails_closed_on_every_proof_subcommand"
    _assert_one_passed(_run_cargo_test(name), name)


def test_proofs_are_deterministic_across_processes() -> None:
    name = "identical_inputs_are_byte_identical_across_processes"
    _assert_one_passed(_run_cargo_test(name), name)


# --------------------------------------------------------------------------- #
# Structural (non-vacuity) -- each guard is shown to catch its own removal
# --------------------------------------------------------------------------- #


def test_cli_drives_the_real_adapter_chain() -> None:
    config = load_config()
    check_broker_envelope_cli(config, broker_cli_source(config))
    # A stubbed engine, bridge, adapter, or transport would let the binary assert whatever it liked
    # about its own classification. Removing any one of them must be caught.
    for token, replacement in (
        ("ExecutionEngine", "StubEngine"),
        ("route_order", "fake_route"),
        # NB: the replacement must not CONTAIN the token, or the presence check still passes
        # and the mutation proves nothing (e.g. "designate" -> "fake_designate" is vacuous).
        ("designate", "authorize"),
        ("dispatch_order", "fake_dispatch"),
        ("IbBrokerageBridge", "FakeBridge"),
        ("InteractiveBrokersBrokerage", "StubBrokerage"),
        ("ScriptedIbGateway", "FakeGateway"),
    ):
        mutated = broker_cli_source(config).replace(token, replacement)
        with pytest.raises(ErrorHandlingCheckError):
            check_broker_envelope_cli(config, mutated)


def test_cli_prints_every_proof_headline() -> None:
    config = load_config()
    for proof in (
        "broker-envelope-complete:true",
        "unmapped-surfaced-not-fabricated:true",
        "live-paper-parity:true",
    ):
        mutated = broker_cli_source(config).replace(proof, "renamed:true")
        with pytest.raises(ErrorHandlingCheckError):
            check_broker_envelope_cli(config, mutated)


def test_cli_fail_closed_path_is_real() -> None:
    config = load_config()
    mutated = broker_cli_source(config).replace("inject=accepted", "inject=allowed")
    with pytest.raises(ErrorHandlingCheckError):
        check_broker_envelope_cli(config, mutated)


def test_every_advertised_vendor_code_is_actually_driven() -> None:
    """The contract may not advertise a mapped code the operator surface never exercises."""
    config = load_config()
    spec = config["error_handling_contract"]["broker_cli"]
    src = broker_cli_source(config)
    for codes in spec["mapped_vendor_codes"].values():
        for code in codes:
            mutated = src.replace(f"code: {code},", "code: 999999,")
            with pytest.raises(ErrorHandlingCheckError):
                check_broker_envelope_cli(config, mutated)


def test_operator_gated_live_test_fails_closed_without_its_env_gate() -> None:
    """The flip gate must never report success without actually exercising IB."""
    config = load_config()
    block = config["error_handling_contract"]
    spec = block["broker_cli"]
    live = (
        REPO_ROOT / block["orchestrator_crate"]["path"] / "tests" / f"{spec['live_gate_test']}.rs"
    )
    src = live.read_text(encoding="utf-8")

    # #[ignore] keeps it out of the parallel agent pool (it binds fixed port 4002), and the feature
    # gate keeps the live socket out of the default build.
    assert "#[ignore" in src, "the live gate must be #[ignore]"
    assert f'feature = "{spec["live_gate_feature"]}"' in src

    # The env gate must be ASSERTED, not used as an early return. A test that quietly returns when
    # ATP_RUN_INTEGRATION is unset reports a green that looks exactly like a real IB round trip --
    # the precise false-evidence shape this repo has been bitten by before.
    gate = f'std::env::var("{spec["live_gate_env"]}")'
    assert gate in src, f"the live gate must consult {spec['live_gate_env']}"
    after_gate = src.split(gate, 1)[1]
    assert after_gate.lstrip().startswith(".as_deref()") or "assert_eq!" in src.split(gate, 1)[0], (
        "the env var must be read inside an assertion"
    )
    assert "assert_eq!" in src
    prologue = src.split(gate, 1)[0]
    assert prologue.rstrip().endswith("assert_eq!(") or "assert_eq!(" in prologue, (
        "the env gate must fail closed via assert_eq!, never an early return"
    )


# --------------------------------------------------------------------------- #
# Taxonomy guard -- INVALID_SYMBOL must keep meaning exactly one thing
# --------------------------------------------------------------------------- #

#: Production sources that construct order-submission error envelopes. The adapter crate is
#: deliberately excluded: ``classify_ib_order_error`` is the ONE place allowed to produce
#: ``InvalidSymbol``, because there it means what the wire string says -- the broker reports no
#: security definition for the symbol (IB codes 200 / 203).
_ENVELOPE_SOURCE_DIRS = (
    "crates/atp-execution/src",
    "crates/atp-orchestrator/src",
)

#: A ``validate()``-failure rejection: the order never reached a broker, so nothing can be known
#: about whether its symbol exists.
_VALIDATE_FAILURE = re.compile(r"\.validate\(\)")

#: An envelope CONSTRUCTION site. The leading ``\b`` matters: it excludes a test's
#: ``expected_category: OrderErrorCategory::InvalidSymbol`` (an assertion about what the adapter
#: classifier should return), which is a declaration of intent, not a construction.
_INVALID_SYMBOL_CONSTRUCTION = re.compile(r"\bcategory: OrderErrorCategory::InvalidSymbol")


def _rust_sources() -> list[Path]:
    files: list[Path] = []
    for rel in _ENVELOPE_SOURCE_DIRS:
        files.extend(sorted((REPO_ROOT / rel).rglob("*.rs")))
    assert files, "no Rust sources found; the guard would pass vacuously"
    return files


def test_no_validate_failure_site_reports_invalid_symbol() -> None:
    """The regression guard for the taxonomy fix SRS-ERR-001 landed.

    Before this feature, eight production sites reported a local validation failure (blank symbol,
    non-positive quantity/price, malformed composite) or an unmapped broker rejection as
    ``INVALID_SYMBOL``. That sent an operator hunting a delisting when the real cause was a bad
    order parameter. Each now carries ``OrderParametersInvalid`` or ``BrokerRejected``.

    A ``validate()`` call and an ``InvalidSymbol`` construction inside the same function body is the
    shape of that regression returning.
    """
    offenders: list[str] = []
    for path in _rust_sources():
        src = path.read_text(encoding="utf-8")
        for match in _INVALID_SYMBOL_CONSTRUCTION.finditer(src):
            # Look back over the enclosing region for a validate() call feeding this construction.
            window = src[max(0, match.start() - 600) : match.start()]
            if _VALIDATE_FAILURE.search(window):
                line = src[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{line}")
    assert not offenders, (
        "a validate()-failure rejection must carry ORDER_PARAMETERS_INVALID, not INVALID_SYMBOL "
        "(which asserts the broker says the symbol does not exist -- a claim no local validation "
        f"can make). Offending sites: {', '.join(offenders)}"
    )


def test_invalid_symbol_is_only_produced_by_the_adapter_classifier() -> None:
    """``INVALID_SYMBOL`` must be reachable only from a real vendor 'no security definition'.

    The execution and orchestrator layers may REFER to the variant -- the CLI sweeps the whole
    vocabulary, and the bridge passes a classifier verdict through -- but neither may CONSTRUCT an
    envelope with it. Only ``classify_ib_order_error`` (atp-adapters, IB codes 200/203) is in a
    position to know a symbol does not exist, because only it has heard back from the broker.
    """
    constructions: list[str] = []
    for path in _rust_sources():
        src = path.read_text(encoding="utf-8")
        for match in _INVALID_SYMBOL_CONSTRUCTION.finditer(src):
            line = src[: match.start()].count("\n") + 1
            constructions.append(f"{path.relative_to(REPO_ROOT)}:{line}")
    assert not constructions, (
        "INVALID_SYMBOL asserts the broker reports no security definition for the symbol; only "
        "classify_ib_order_error may reach that conclusion. Envelope(s) constructing it directly: "
        f"{', '.join(constructions)}"
    )
