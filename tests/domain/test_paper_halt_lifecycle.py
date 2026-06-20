"""SRS-SAFE-001 / SyRS SYS-44a — a halted paper engine emits no fill, and the gate is un-bypassable.

L7 domain (safety) test for the kill switch's paper-engine HALTED gate sub-component. The
acceptance criterion's safety core (the slice this covers) is that on kill-switch activation a paper
simulation engine "transition[s] to the HALTED state with no further on_fill callbacks emitted". A
halted engine that could still produce a fill is a trading-safety bug: the kill switch would have
been pulled yet simulated fills (and the callbacks they drive) keep flowing.

DOMAIN-LEVEL REALIZATION (honest scope): there is no callback-emitting runtime loop and no Python
strategy host yet, so "no further on_fill callbacks emitted" is realized by refusing to PRODUCE a
fill -- a halted ``HaltablePaperEngine`` returns ``HaltError::Halted`` with no ``PaperFill``, so no
fill exists to drive a callback. SRS-SAFE-001 STAYS ``passes:false``: this is ONE named
sub-component; the rest of the QuantConnect-Liquidate sequence is deferred to its named owners
(SRS-EXE-006 IB cancel/disconnect; SRS-EXE-002 / SAFE-001 runtime activation + 5s NFR-P3; SRS-LOG-001
1s observability; SRS-NOTIF-001 email/SMS; SRS-API-001 / SRS-UI trigger).

This test proves the invariant from three angles:

  1. Behavioral -- it shells out to the Rust integration test
     ``crates/atp-simulation/tests/srs_safe_001_paper_halt.rs`` and asserts that a halted engine
     produces no fill, the same order fills then is refused across the transition, halt is
     idempotent, the transition is observable, and a Running gate does not mask fill-native errors.

  2. Structural (sealed gate) -- it asserts, via ``tools/sim_halt_check.py``, that the inner
     ``PaperSimulationEngine`` is a PRIVATE field with no accessor / ``Deref`` / ``into_inner``
     escape hatch AND that the gate is not ``Clone`` (a clone could stay Running after the original
     is halted), that ``simulate_fill`` returns ``HaltError::Halted`` before delegating, that
     ``halt`` is idempotent, that the crate has no broker dependency, and that the module leaks no
     vendor token. Each guard is shown non-vacuous by a FULL-TOKEN mutation. This seals a HELD gate,
     not the whole system: the bare ``PaperSimulationEngine`` stays a public fill primitive, so
     routing every non-live strategy onto a halt-aware engine is the deferred SRS-EXE-002
     orchestrator's job (named in the contract's deferred[]).

  3. Scope honesty -- it pins that ``feature_list.json`` keeps SRS-SAFE-001 ``passes:false`` and that
     the contract names the deferred owners of the rest of the sequence, so this slice cannot
     silently over-claim the kill switch.
"""

from __future__ import annotations

import json
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

from sim_halt_check import (  # noqa: E402
    SimHaltCheckError,
    cargo_source,
    check_gate_not_clonable,
    check_gate_unbypassable,
    check_halt_error_enum,
    check_halt_idempotent,
    check_no_broker_dependency,
    check_simulate_fill_gate,
    check_vendor_isolation,
    halt_source,
    load_config,
)


# --------------------------------------------------------------------------- #
# 1. Behavioral — shell to the Rust integration test
# --------------------------------------------------------------------------- #


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
            "srs_safe_001_paper_halt",
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


def test_halted_engine_emits_no_fill() -> None:
    # The safety core: once halted, NO fill is produced for orders that would otherwise fill.
    _assert_one_passed(
        _run_cargo_test("srs_safe_001_halted_engine_emits_no_fill"),
        "SRS-SAFE-001 halted engine emits no fill",
    )


def test_running_fills_then_halt_refuses_same_order() -> None:
    # Negative control: the SAME order fills while Running and is refused after halt.
    _assert_one_passed(
        _run_cargo_test("srs_safe_001_fill_then_halt_then_refuse_sequence"),
        "SRS-SAFE-001 fill then halt then refuse",
    )


def test_halt_is_idempotent_behaviorally() -> None:
    _assert_one_passed(
        _run_cargo_test("srs_safe_001_halt_is_idempotent"),
        "SRS-SAFE-001 idempotent halt",
    )


def test_halt_transition_is_observable() -> None:
    _assert_one_passed(
        _run_cargo_test("srs_safe_001_halt_transition_is_observable"),
        "SRS-SAFE-001 observable transition",
    )


def test_running_gate_does_not_mask_fill_errors() -> None:
    _assert_one_passed(
        _run_cargo_test("srs_safe_001_gate_does_not_mask_fill_native_errors_while_running"),
        "SRS-SAFE-001 running gate surfaces SimError",
    )


# --------------------------------------------------------------------------- #
# 2. Structural (un-bypassable gate) — each guard non-vacuous via FULL-TOKEN mutation
# --------------------------------------------------------------------------- #


def test_inner_engine_is_private_and_unreachable() -> None:
    config = load_config()
    # The real module passes the un-bypassable-gate check.
    check_gate_unbypassable(config, halt_source(config))
    # ...and exposing the inner engine publicly is caught (the gate would be bypassable).
    public_field = halt_source(config).replace(
        "engine: PaperSimulationEngine,", "pub engine: PaperSimulationEngine,", 1
    )
    with pytest.raises(SimHaltCheckError):
        check_gate_unbypassable(config, public_field)
    # ...and an into_inner escape hatch that hands out the inner engine is caught.
    escape_hatch = halt_source(config).replace(
        "    fn halted_reason(&self) -> HaltReason {",
        "    pub fn into_inner(self) -> PaperSimulationEngine {\n        self.engine\n    }\n\n"
        "    fn halted_reason(&self) -> HaltReason {",
        1,
    )
    with pytest.raises(SimHaltCheckError):
        check_gate_unbypassable(config, escape_hatch)


def test_gate_is_not_clonable() -> None:
    config = load_config()
    # The real gate is not Clone, so no pre-halt copy can outlive the halt.
    check_gate_not_clonable(config, halt_source(config))
    # ...and adding a Clone derive is caught (a cloned running handle could fill after halt).
    mutated = halt_source(config).replace(
        "#[derive(Debug)]\npub struct HaltablePaperEngine",
        "#[derive(Debug, Clone)]\npub struct HaltablePaperEngine",
        1,
    )
    assert mutated != halt_source(config), "mutation must add the Clone derive"
    with pytest.raises(SimHaltCheckError):
        check_gate_not_clonable(config, mutated)


def test_simulate_fill_gates_on_halted_before_delegating() -> None:
    config = load_config()
    check_simulate_fill_gate(config, halt_source(config))
    # Breaking the halted-guard token in the gate body lets a halted engine reach the fill.
    mutated = halt_source(config).replace("Err(HaltError::Halted {", "Err(HaltErrorHalted {", 1)
    assert mutated != halt_source(config), "mutation must alter the gate body"
    with pytest.raises(SimHaltCheckError):
        check_simulate_fill_gate(config, mutated)


def test_halt_is_idempotent_structurally() -> None:
    config = load_config()
    check_halt_idempotent(config, halt_source(config))
    # Dropping the already-halted no-op arm removes idempotency.
    mutated = halt_source(config).replace("HaltOutcome::AlreadyHalted", "HaltOutcome::Transitioned")
    with pytest.raises(SimHaltCheckError):
        check_halt_idempotent(config, mutated)


def test_halt_error_composes_simerror() -> None:
    config = load_config()
    check_halt_error_enum(config, halt_source(config))
    mutated = halt_source(config).replace("impl From<SimError> for HaltError", "impl FromNothing")
    with pytest.raises(SimHaltCheckError):
        check_halt_error_enum(config, mutated)


def test_crate_has_no_broker_dependency() -> None:
    config = load_config()
    check_no_broker_dependency(config, cargo_source(config))
    mutated = cargo_source(config) + '\natp-execution = { path = "../atp-execution" }\n'
    with pytest.raises(SimHaltCheckError):
        check_no_broker_dependency(config, mutated)


def test_halt_module_leaks_no_vendor_token() -> None:
    config = load_config()
    check_vendor_isolation(config, halt_source(config))
    mutated = halt_source(config) + "\n// fills routed through ib_insync under the hood\n"
    with pytest.raises(SimHaltCheckError):
        check_vendor_isolation(config, mutated)


# --------------------------------------------------------------------------- #
# 3. Scope honesty — the slice must NOT over-claim the kill switch
# --------------------------------------------------------------------------- #


def test_safe_001_stays_unflipped() -> None:
    features = json.loads((REPO_ROOT / "feature_list.json").read_text(encoding="utf-8"))
    entry = next((f for f in features if f["id"] == "SRS-SAFE-001"), None)
    assert entry is not None, "SRS-SAFE-001 must exist in feature_list.json"
    assert entry["passes"] is False, (
        "SRS-SAFE-001 must stay passes:false -- the paper-engine halt gate is one sub-component, "
        "not the full kill switch (IB cancel/disconnect, activation + 5s budget, notifications, "
        "and the dashboard/CLI/REST trigger are deferred)"
    )


def test_deferred_owners_are_named() -> None:
    config = load_config()
    block = config["paper_halt_contract"]
    deferred = " ".join(f"{e['feature']} {e['what']}" for e in block["deferred"])
    for owner in ("SRS-EXE-006", "SRS-EXE-002", "SRS-LOG-001", "SRS-NOTIF-001", "SRS-API-001"):
        assert owner in deferred, f"deferred[] must name the {owner} owner of the rest of SRS-SAFE-001"
    # The contract description must state the slice is a sub-component, not the closed requirement.
    assert "passes:false" in block["description"]
    assert "SUB-COMPONENT" in block["description"] or "sub-component" in block["description"]
