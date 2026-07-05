#!/usr/bin/env python3
"""Contract evidence script for the SRS-SAFE-001 paper-engine HALTED gate sub-component.

SRS-SAFE-001 (kill switch; SyRS SYS-44a; NFR-P3; NFR-SC1; StRS SN-1.11) requires, among the
QuantConnect-Liquidate sequence, that "paper simulation engines transition to the HALTED state
with no further on_fill callbacks emitted". This script verifies the ONE named sub-component this
slice ships -- the per-engine Running -> Halted transition and the un-bypassable refuse-to-fill
gate -- which lives in ``crates/atp-simulation`` (module ``halt``), per the structural contract in
``architecture/runtime_services.json`` (block ``paper_halt_contract``):

  (a) ``PaperEngineState`` has ``Running`` / ``Halted``; ``HaltReason`` is a closed set
      (``KillSwitch``); ``HaltError`` has ``Halted`` / ``Sim`` and composes the fill-native
      ``SimError`` via ``impl From<SimError> for HaltError``; ``HaltOutcome`` has
      ``Transitioned`` / ``AlreadyHalted``; ``HaltTransition`` carries ``reason`` + ``sequence``.
  (b) THE UN-BYPASSABLE GATE: ``HaltablePaperEngine`` owns the inner ``PaperSimulationEngine`` as a
      PRIVATE field (``engine: PaperSimulationEngine``, never ``pub`` / ``pub(crate)``) and exposes
      NO inner-engine accessor, ``Deref``, or ``into_inner`` -- so a halted engine cannot be filled
      around. This is the load-bearing safety assertion.
  (c) ``simulate_fill`` returns ``HaltError::Halted`` BEFORE it delegates to
      ``self.engine.simulate_fill`` -- a halted engine produces no fill, so no on_fill callback can
      be driven (the SRS-SAFE-001 clause, realized at the domain level).
  (d) ``halt`` is idempotent: it has both the ``HaltOutcome::Transitioned`` (fresh) and
      ``HaltOutcome::AlreadyHalted`` (no-op) arms and records a ``HaltTransition``.
  (e) ``lib.rs`` re-exports ``pub mod halt;``; the module carries no vendor-SDK token and the
      ``atp-simulation`` crate has no dependency on the live/broker path (``atp-execution`` /
      ``atp-adapters``), so the gate is std-only and in-crate (cross-crate kill-switch composition
      is the deferred SRS-EXE-002 orchestrator's job).

The PASS line is ``SRS-SAFE-001 HALT-GATE PASS`` -- a SUB-COMPONENT pass, NOT a full/SDK-SURFACE
pass. feature_list.json keeps SRS-SAFE-001 ``passes:false``; the closing line names the genuinely
DEFERRED owners of the rest of the sequence (SRS-EXE-006 IB cancel/disconnect, SRS-EXE-002 /
SAFE-001 runtime activation + 5s NFR-P3, SRS-LOG-001 1s observability, SRS-NOTIF-001 email/SMS,
SRS-API-001 / SRS-UI trigger).

Mirrors the PASS/FAIL output style of ``tools/sim_ledger_check.py``.

Invoke:
    python3 tools/sim_halt_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _enum_body, _fn_block, _struct_body

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class SimHaltCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise SimHaltCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "paper_halt_contract" not in config:
        fail("architecture metadata is missing paper_halt_contract")
    return config["paper_halt_contract"]


def halt_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["simulation_crate"]["path"] / "src" / f"{block['halt_module']}.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def lib_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["simulation_crate"]["path"] / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def cargo_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = (
        root / block["simulation_crate"]["path"] / block["no_broker_dependency"]["cargo_toml"]
    )
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide a token."""
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_state_enum(config: dict, halt_src: str) -> str:
    spec = contract_block(config)["state_enum"]
    body = _enum_body(halt_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing run-state variant(s): {', '.join(missing)}")
    return f"atp-simulation declares {spec['enum']} with {', '.join(spec['variants'])}"


def check_reason_enum(config: dict, halt_src: str) -> str:
    spec = contract_block(config)["reason_enum"]
    body = _enum_body(halt_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing variant(s): {', '.join(missing)}")
    return f"atp-simulation declares {spec['enum']} ({', '.join(spec['variants'])})"


def check_outcome_enum(config: dict, halt_src: str) -> str:
    spec = contract_block(config)["outcome_enum"]
    body = _enum_body(halt_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing variant(s): {', '.join(missing)}")
    return f"atp-simulation declares {spec['enum']} ({', '.join(spec['variants'])})"


def check_halt_error_enum(config: dict, halt_src: str) -> str:
    spec = contract_block(config)["halt_error_enum"]
    body = _enum_body(halt_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing variant(s): {', '.join(missing)}")
    if _compact(spec["from_simerror_token"]) not in _compact(halt_src):
        fail(
            f"{spec['enum']} must compose the fill-native SimError "
            f"(`{spec['from_simerror_token']}`) so a Running gate surfaces a fill error unchanged"
        )
    return (
        f"atp-simulation declares {spec['enum']} ({', '.join(spec['variants'])}) and composes "
        f"SimError via `{spec['from_simerror_token']}`"
    )


def check_transition_struct(config: dict, halt_src: str) -> str:
    spec = contract_block(config)["transition_struct"]
    body = _struct_body(halt_src, spec["struct"])
    missing = [f for f in spec["fields"] if not re.search(rf"\b{re.escape(f)}\b", body)]
    if missing:
        fail(f"{spec['struct']} is missing observability field(s): {', '.join(missing)}")
    return (
        f"atp-simulation declares {spec['struct']} carrying {', '.join(spec['fields'])} "
        "(in-memory SRS-LOG-001 groundwork; no wall-clock time)"
    )


def check_gate_unbypassable(config: dict, halt_src: str) -> str:
    """The load-bearing safety assertion: a HELD gate cannot be coerced into a fill once halted.

    For a given gate value, the only path to a paper fill is the gate's own simulate_fill. That
    holds iff the inner PaperSimulationEngine is a PRIVATE field with no accessor / Deref /
    into_inner escape hatch AND the gate is not Clone (a clone could stay Running after the original
    is halted). (This seals a HELD gate, not the whole system -- the bare PaperSimulationEngine
    stays a public primitive; system-wide routing onto halt-aware engines is the deferred
    SRS-EXE-002 orchestrator's job, recorded in deferred[].)
    """
    spec = contract_block(config)["gate_struct"]
    body = _struct_body(halt_src, spec["struct"])
    compact_body = _compact(body)

    # The inner engine must be DECLARED on the gate...
    if _compact(spec["engine_field_token"]) not in compact_body:
        fail(
            f"{spec['struct']} must own the inner engine as `{spec['engine_field_token']}` "
            "(the gate wraps a real PaperSimulationEngine)"
        )
    # ...and it must be PRIVATE (never pub / pub(crate)), or the gate is bypassable.
    leaked_pub = [t for t in spec["forbidden_pub_tokens"] if _compact(t) in compact_body]
    if leaked_pub:
        fail(
            f"{spec['struct']} exposes the inner engine field publicly ({', '.join(leaked_pub)}) -- "
            "the engine field MUST be private so a halted engine cannot be filled around"
        )
    # ...and there must be NO accessor / Deref / into_inner escape hatch anywhere in the module.
    compact_src = _compact(halt_src)
    leaked_accessor = [t for t in spec["forbidden_accessor_tokens"] if _compact(t) in compact_src]
    if leaked_accessor:
        fail(
            f"the halt module exposes an inner-engine escape hatch ({', '.join(leaked_accessor)}) -- "
            "no accessor returning the inner engine, no Deref, and no into_inner may exist, or the "
            "gate is bypassable"
        )
    return (
        f"{spec['struct']} owns a PRIVATE {spec['engine_field_token']} with no public field, "
        "accessor, Deref, or into_inner escape hatch -- a held gate is sealed (system-wide routing "
        "onto halt-aware engines is the deferred SRS-EXE-002 orchestrator's job)"
    )


def check_gate_not_clonable(config: dict, halt_src: str) -> str:
    """A second load-bearing safety assertion: the gate must NOT be Clone.

    A clone would be an independent value whose own run state could stay Running after the original
    is halted, so a pre-halt copy could keep filling -- the exact bypass the gate prevents. The gate
    must therefore not derive (or impl) Clone.
    """
    spec = contract_block(config)["gate_struct"]
    forbidden = spec["forbidden_derive_token"]
    # The attributes (derive list, etc.) immediately preceding the struct.
    match = re.search(rf"((?:#\[[^\]]*\]\s*)*)pub struct {re.escape(spec['struct'])}\b", halt_src)
    attrs = match.group(1) if match else ""
    if re.search(rf"\b{re.escape(forbidden)}\b", attrs):
        fail(
            f"{spec['struct']} must NOT derive {forbidden} -- a cloned pre-halt handle could keep "
            "filling after the original is halted, bypassing the kill switch"
        )
    # Defensively, also reject a hand-written Clone impl for the gate.
    if _compact(f"impl Clone for {spec['struct']}") in _compact(halt_src):
        fail(
            f"{spec['struct']} must NOT implement {forbidden} -- a cloned pre-halt handle could keep "
            "filling after the original is halted, bypassing the kill switch"
        )
    return (
        f"{spec['struct']} does not derive or implement {forbidden} -- no pre-halt copy can outlive "
        "the halt and keep filling"
    )


def check_simulate_fill_gate(config: dict, halt_src: str) -> str:
    spec = contract_block(config)["simulate_fill_gate"]
    fn_body = _fn_block(halt_src, spec["fn"])
    compact_body = _compact(fn_body)
    halted = _compact(spec["halted_guard_token"])
    delegate = _compact(spec["delegate_token"])
    if halted not in compact_body:
        fail(f"{spec['fn']} must return `{spec['halted_guard_token']}` when the engine is halted")
    if delegate not in compact_body:
        fail(f"{spec['fn']} must delegate to `{spec['delegate_token']}` while running")
    # The halted guard MUST come before the delegation, so a halted engine never reaches the fill.
    if compact_body.index(halted) >= compact_body.index(delegate):
        fail(
            f"{spec['fn']} must check the halted state and return `{spec['halted_guard_token']}` "
            f"BEFORE delegating to `{spec['delegate_token']}` -- otherwise a halted engine could "
            "still produce a fill"
        )
    if _compact(spec["return_type_token"]) not in _compact(halt_src):
        fail(f"{spec['fn']} must return `{spec['return_type_token']}`")
    return (
        f"atp-simulation {spec['fn']} returns {spec['halted_guard_token']} BEFORE delegating to "
        f"{spec['delegate_token']} -- a halted engine produces no fill"
    )


def check_halt_idempotent(config: dict, halt_src: str) -> str:
    spec = contract_block(config)["halt_fn"]
    fn_body = _compact(_fn_block(halt_src, spec["fn"]))
    for key, label in (
        ("transition_token", "the fresh Running -> Halted transition"),
        ("idempotent_token", "the idempotent no-op (already-halted) arm"),
        ("record_token", "the recorded HaltTransition"),
    ):
        if _compact(spec[key]) not in fn_body:
            fail(f"{spec['fn']} is missing {label} (`{spec[key]}`)")
    return (
        f"atp-simulation {spec['fn']} is idempotent: it records a {spec['record_token']} and "
        f"returns {spec['transition_token']} on the first call, {spec['idempotent_token']} after"
    )


def check_module_reexport(config: dict, lib_src: str) -> str:
    spec = contract_block(config)["module_reexport"]
    if _compact(spec["lib_reexport_token"]) not in _compact(lib_src):
        fail(
            f"atp-simulation lib.rs must re-export `{spec['lib_reexport_token']}` so the halt gate "
            "is part of the simulation engine"
        )
    return f"atp-simulation lib.rs re-exports `{spec['lib_reexport_token']}`"


def check_no_broker_dependency(config: dict, cargo_text: str) -> str:
    spec = contract_block(config)["no_broker_dependency"]
    leaked = [t for t in spec["forbidden_dep_tokens"] if t in cargo_text]
    if leaked:
        fail(
            f"atp-simulation Cargo.toml must NOT depend on the live/broker path: found "
            f"{', '.join(leaked)} -- the halt gate is the paper-engine half; cross-crate kill-switch "
            "composition is the deferred SRS-EXE-002 orchestrator's job"
        )
    return (
        f"atp-simulation Cargo.toml declares no dependency on the live/broker path "
        f"({', '.join(spec['forbidden_dep_tokens'])}) -- the halt gate stays in-crate"
    )


def check_vendor_isolation(config: dict, halt_src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [t for t in tokens if t in halt_src]
    if leaked:
        fail(
            f"atp-simulation halt module leaks vendor SDK token(s): {', '.join(leaked)} "
            "(the core engine must isolate vendors behind adapters per SRS-ARCH-003)"
        )
    return (
        f"atp-simulation halt module is free of all {len(tokens)} forbidden vendor SDK tokens "
        "(SRS-ARCH-003 adapter isolation)"
    )


def check_cargo_test_smoke(config: dict, require_cargo: bool = False) -> str:
    block = contract_block(config)
    crate = block["simulation_crate"]["crate"]
    integration = block["rust_integration_test"]
    cargo = shutil.which("cargo")
    if cargo is None:
        if require_cargo:
            fail(
                f"cargo not on PATH but --require-cargo set: cannot verify the runnable {crate} "
                "halt-gate path compiles + passes (install the Rust toolchain)"
            )
        return f"cargo test -p {crate} --test {integration}: skipped (cargo not on PATH)"
    lib = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", "halt::", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if lib.returncode != 0:
        fail(f"cargo test -p {crate} --lib halt:: failed:\n{lib.stdout}\n{lib.stderr}")
    integ = subprocess.run(
        [cargo, "test", "-p", crate, "--test", integration, "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if integ.returncode != 0:
        fail(f"cargo test -p {crate} --test {integration} failed:\n{integ.stdout}\n{integ.stderr}")
    return (
        f"cargo test -p {crate} --lib halt:: + {integration}: PASS "
        "(a Running gate fills identically to the bare engine, a halted gate produces no fill, halt "
        "is idempotent, the transition is observable, the gate does not mask fill-native errors, and "
        "the same order fills then is refused across the transition)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

# (name, collector, source-key) — "halt" reads halt.rs, "lib" reads lib.rs,
# "cargo" reads the crate Cargo.toml.
_STATIC_CHECKS = (
    ("state_enum", check_state_enum, "halt"),
    ("reason_enum", check_reason_enum, "halt"),
    ("outcome_enum", check_outcome_enum, "halt"),
    ("halt_error_enum", check_halt_error_enum, "halt"),
    ("transition_struct", check_transition_struct, "halt"),
    ("gate_unbypassable", check_gate_unbypassable, "halt"),
    ("gate_not_clonable", check_gate_not_clonable, "halt"),
    ("simulate_fill_gate", check_simulate_fill_gate, "halt"),
    ("halt_idempotent", check_halt_idempotent, "halt"),
    ("module_reexport", check_module_reexport, "lib"),
    ("no_broker_dependency", check_no_broker_dependency, "cargo"),
    ("vendor_isolation", check_vendor_isolation, "halt"),
)

# Genuinely DEFERRED owners of the LIVE half of the SRS-SAFE-001 kill-switch sequence — separate
# requirements this paper-engine halt gate does NOT close. The activation runtime above the gate
# (fan-out + cancel/liquidate/disconnect sequence + 5s NFR-P3 measurement + operator surfaces) is
# now BUILT as the SRS-SAFE-001 slice (see kill_switch_activation_contract + tools/
# kill_switch_check.py); SRS-SAFE-001 still stays passes:false until the live path exists.
_DEFERRED_OWNERS = (
    "SRS-EXE-006 (the REAL IB transport behind the activation gate's cancel/liquidate/disconnect port)",
    "SRS-EXE-002 (hosting every real paper strategy on fleet-registered halt gates)",
    "SRS-LOG-001 (the log feature's own dashboard-viewing flip; the activation layer now writes ACTIVATION+HALTED durably)",
    "SRS-NOTIF-001 (operator email/SMS)",
    "SRS-API-001 / SRS-UI (the operator runtime's own flip; the kill-switch handlers themselves are wired by atp_safety)",
)


def assert_sim_halt_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    sources = {
        "halt": halt_source(config, root),
        "lib": lib_source(config, root),
        "cargo": cargo_source(config, root),
    }
    return [check(config, sources[source_key]) for _, check, source_key in _STATIC_CHECKS]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_sim_halt_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SRS-SAFE-001 paper-engine HALTED gate sub-component contract evidence"
    )
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable — the runnable halt-gate path must compile.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except SimHaltCheckError as error:
        print(f"SRS-SAFE-001 HALT-GATE FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-SAFE-001 HALT-GATE PASS")
    for item in evidence:
        print(f"- {item}")
    print(
        "- deferred owners (separate requirements this sub-component does NOT close; "
        "SRS-SAFE-001 stays passes:false): " + ", ".join(_DEFERRED_OWNERS)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
