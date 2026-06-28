#!/usr/bin/env python3
"""Contract evidence script for feature SRS-ORCH-002.

Verifies that the strategy-orchestrator resource-profile gate declared in
``architecture/runtime_services.json`` (block ``resource_profile_contract``)
is reachable from the Rust crates ``crates/atp-types`` and
``crates/atp-orchestrator``.

SRS-ORCH-002 traces SyRS SYS-11 / SYS-57 / NFR-SC1 and StRS SN-1.10 /
BG-6. The contract guarantees:

* Four spec-literal constants in ``atp-types`` carry the SyRS SYS-11
  defaults (live: 512 MB / 25 hundredths CPU; paper: 300 MB / 10
  hundredths CPU).
* Four validation-bound constants in ``atp-types`` carry the
  catalogue min/max (mem 64..65536 MB; cpu 5..1600 hundredths) so the
  orchestrator's ``validate()`` and the SRS-ARCH-005 catalogue agree on
  the same range.
* ``ResourceProfile`` is a ``pub struct`` with exactly two required
  fields (``mem_mb``, ``cpu_hundredths``) and rejects vendor /
  container-runtime / float-CPU bleed.
* ``ResourceProfileError`` enumerates the four validation-failure
  variants.
* ``ResourceProfile`` exposes the four required methods (``live_default``,
  ``paper_default``, ``for_mode``, ``validate``).
* The orchestrator exposes the three convenience helpers
  (``live_profile_default``, ``paper_profile_default``,
  ``profile_for_mode``) so callers do not have to reach into atp-types.
* ``StrategyOrchestrator::launch`` calls ``request.profile.validate()``
  BEFORE ``runtime.create(`` (positional invariant — a refactor that
  validates after create would be caught), constructs the rejection
  via ``StructuredOrchestratorError::resource_profile_invalid``, and
  the validation rejection arm calls NONE of the forbidden runtime /
  sink / registry mutators.
* ``StrategyLaunchOutcome`` is constructed with ``profile: request.profile``
  (no silent re-defaulting at the gate).
* The catalogue defaults in ``configuration.keys`` for
  ATP_LIVE_STRATEGY_MEM_MB / ATP_LIVE_STRATEGY_CPU /
  ATP_PAPER_STRATEGY_MEM_MB / ATP_PAPER_STRATEGY_CPU equal the
  spec-literal constants in ``atp-types`` (with the cores → hundredths
  conversion for the CPU bindings).

Mirrors the PASS/FAIL output style of ``tools/orchestrator_lifecycle_check.py``.

Invoke:
    python3 tools/orchestrator_resource_profile_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Re-uses the shared rust-source parsers in ``tools/_rust_parser.py``
# (the SESSION 20 hoist exists for exactly this reuse case).
from _rust_parser import _enum_body, _fn_block, _struct_body  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class ResourceProfileCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise ResourceProfileCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "resource_profile_contract" not in config:
        fail("architecture metadata is missing resource_profile_contract")
    return config["resource_profile_contract"]


def types_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    crate_path = root / block["types_crate"]["path"]
    source_path = crate_path / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"types crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def orchestrator_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    crate_path = root / block["orchestrator_crate"]["path"]
    source_path = crate_path / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"orchestrator crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def _assert_pub_const_u32(source: str, name: str, expected: int) -> None:
    pattern = re.compile(rf"\bpub\s+const\s+{re.escape(name)}\s*:\s*u32\s*=\s*([0-9_]+)\s*;")
    match = pattern.search(source)
    if match is None:
        fail(
            f"{name} is not declared as `pub const {name}: u32` in atp-types — "
            "SRS-ORCH-002 requires a single source of truth for each spec literal"
        )
    literal = int(match.group(1).replace("_", ""))
    if literal != expected:
        fail(f"{name} has value {literal} but SRS-ORCH-002 / SyRS SYS-11 requires {expected}")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_spec_constants(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["spec_constants"]
    for key in ("live_mem_mb", "live_cpu_hundredths", "paper_mem_mb", "paper_cpu_hundredths"):
        entry = spec[key]
        _assert_pub_const_u32(types_src, entry["name"], entry["value"])
    return (
        "atp-types declares the SyRS SYS-11 spec-literal constants — "
        f"{spec['live_mem_mb']['name']}={spec['live_mem_mb']['value']}, "
        f"{spec['live_cpu_hundredths']['name']}={spec['live_cpu_hundredths']['value']}, "
        f"{spec['paper_mem_mb']['name']}={spec['paper_mem_mb']['value']}, "
        f"{spec['paper_cpu_hundredths']['name']}={spec['paper_cpu_hundredths']['value']}"
    )


def check_validation_constants(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["validation_constants"]
    for key in (
        "mem_floor_mb",
        "mem_ceiling_mb",
        "cpu_floor_hundredths",
        "cpu_ceiling_hundredths",
    ):
        entry = spec[key]
        _assert_pub_const_u32(types_src, entry["name"], entry["value"])
    return (
        "atp-types declares the SRS-ARCH-005 catalogue-aligned validation "
        f"bounds — mem in [{spec['mem_floor_mb']['value']}, "
        f"{spec['mem_ceiling_mb']['value']}] MB; cpu in "
        f"[{spec['cpu_floor_hundredths']['value']}, "
        f"{spec['cpu_ceiling_hundredths']['value']}] hundredths"
    )


def check_resource_profile_struct(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["resource_profile"]
    body = _struct_body(types_src, spec["struct"])
    missing = [
        field
        for field in spec["required_fields"]
        if not re.search(rf"\bpub\s+{re.escape(field)}\s*:", body)
    ]
    if missing:
        fail(f"{spec['struct']} is missing required fields: {', '.join(missing)}")
    leaks = [
        field
        for field in spec["forbidden_fields"]
        if re.search(rf"\bpub\s+{re.escape(field)}\s*:", body)
    ]
    if leaks:
        fail(
            f"{spec['struct']} leaks vendor / float-CPU / container-runtime "
            f"field(s): {', '.join(leaks)} (SRS-ORCH-002 wire types must "
            "stay free of Docker-Engine-specific shape and float-CPU drift)"
        )
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} required fields "
        f"({', '.join(spec['required_fields'])}) and rejects "
        f"{len(spec['forbidden_fields'])} forbidden vendor/float-CPU fields"
    )


def check_resource_profile_error_enum(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["resource_profile_error"]
    body = _enum_body(types_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} "
        f"validation-failure variants ({', '.join(spec['variants'])})"
    )


def check_resource_profile_methods(config: dict, types_src: str) -> str:
    block = contract_block(config)
    methods = block["resource_profile_methods"]
    missing = []
    for method in methods:
        # Both `pub fn` and `pub const fn` are acceptable.
        if not re.search(rf"\bpub\s+(?:const\s+)?fn\s+{re.escape(method)}\b", types_src):
            missing.append(method)
    if missing:
        fail(f"ResourceProfile is missing required methods: {', '.join(missing)}")
    return (
        f"atp-types declares ResourceProfile methods "
        f"({', '.join(methods)}) — defaults, mode dispatch, validation"
    )


def check_orchestrator_helper_methods(config: dict, orch_src: str) -> str:
    block = contract_block(config)
    helpers = block["orchestrator_helper_methods"]
    missing = []
    for method in helpers:
        if not re.search(rf"\bpub\s+(?:const\s+)?fn\s+{re.escape(method)}\b", orch_src):
            missing.append(method)
    if missing:
        fail(f"StrategyOrchestrator is missing required helper methods: {', '.join(missing)}")
    return (
        f"atp-orchestrator exposes Orchestrator helpers "
        f"({', '.join(helpers)}) so callers populate launch requests "
        "without reaching into atp_types"
    )


def check_launch_validate_guard(config: dict, orch_src: str) -> str:
    block = contract_block(config)
    guard = block["launch_validate_guard"]
    body = _fn_block(orch_src, guard["entry_method"])

    validate_token = guard["required_call"] + "("
    create_token = "runtime.create("
    factory_token = guard["rejection_factory"] + "("

    if validate_token not in body:
        fail(
            f"{guard['entry_method']} does not call `{validate_token}` — "
            "SRS-ORCH-002 requires the orchestrator to validate the "
            "resource profile at the gate boundary"
        )
    if create_token not in body:
        fail(
            f"{guard['entry_method']} does not call `{create_token}` — "
            "SRS-ORCH-001 requires the lifecycle gate to delegate to the "
            "runtime port"
        )
    if factory_token not in body:
        fail(
            f"{guard['entry_method']} does not construct the rejection "
            f"via `{factory_token}` — SyRS SYS-64 wire-string single "
            "source of truth"
        )

    validate_pos = body.find(validate_token)
    create_pos = body.find(create_token)
    factory_pos = body.find(factory_token)

    # Positional invariant: validate must come before create. A refactor
    # that creates the container then validates the profile would let a
    # misconfigured override reach the host before the gate refuses it.
    if validate_pos > create_pos:
        fail(
            f"{guard['entry_method']} calls `{validate_token}` AFTER "
            f"`{create_token}` — SRS-ORCH-002 requires validation BEFORE "
            "the runtime port is invoked so a misconfigured override "
            "never reaches the host"
        )

    # Positional invariant: the rejection-factory call must precede the
    # first runtime.create call (the rejection short-circuits the
    # function, so factory_pos is necessarily inside the validation arm).
    if factory_pos > create_pos:
        fail(
            f"{guard['entry_method']} constructs the rejection AFTER "
            f"`{create_token}` — the rejection arm must short-circuit "
            "before the runtime port is invoked"
        )

    # The validation-rejection arm must not invoke any of the forbidden
    # runtime / sink / registry mutators. Approximate the arm body as
    # everything between validate_pos and create_pos (the rejection
    # returns inside the if-let, so its body is contained in this range).
    rejection_region = body[validate_pos:create_pos]
    forbidden = guard.get("forbidden_calls_on_rejection", [])
    for token in forbidden:
        call = token + "("
        if call in rejection_region:
            fail(
                f"{guard['entry_method']} validation-rejection arm calls "
                f"`{call}` — SRS-ORCH-002 requires the rejection to "
                "short-circuit before the runtime / sink / registry is "
                "touched (a misconfigured launch leaves no orphan)"
            )
    return (
        f"atp-orchestrator::{guard['entry_method']} calls "
        f"`{guard['required_call']}` before `runtime.create`, refuses "
        f"misconfigured profiles via `{guard['rejection_factory']}` "
        f"with category {guard['rejection_category']} "
        f"({guard['rejection_wire_string']}), and invokes none of the "
        f"{len(forbidden)} forbidden runtime / sink / registry mutators "
        "on the rejection path"
    )


def check_outcome_profile_equality_guard(config: dict, orch_src: str) -> str:
    block = contract_block(config)
    guard = block["outcome_profile_equality_guard"]
    body = _fn_block(orch_src, "launch")

    required_token = guard["required_construction_token"]
    if required_token not in body:
        fail(
            f"StrategyOrchestrator::launch does not include "
            f"`{required_token}` in the {guard['outcome_struct']} "
            "construction — SRS-ORCH-002 requires the outcome's "
            "profile to equal the request's profile (no silent "
            "re-defaulting at the gate)"
        )
    for forbidden in guard.get("forbidden_construction_tokens", []):
        if forbidden in body:
            fail(
                f"StrategyOrchestrator::launch constructs the outcome "
                f"with `{forbidden}` — re-defaulting at the gate would "
                "lie about what was applied; the outcome must carry the "
                "request's profile verbatim"
            )
    return (
        f"atp-orchestrator::launch constructs {guard['outcome_struct']} "
        f"with `{required_token}` (no silent re-defaulting at the gate)"
    )


def check_config_catalogue_binding(config: dict, _types_src: str) -> str:
    block = contract_block(config)
    binding = block["config_binding"]
    catalogue = config.get("configuration", {}).get("keys", [])
    by_name = {entry["name"]: entry for entry in catalogue}
    spec = block["spec_constants"]

    name_to_value = {
        "LIVE_PROFILE_MEM_MB": spec["live_mem_mb"]["value"],
        "LIVE_PROFILE_CPU_HUNDREDTHS": spec["live_cpu_hundredths"]["value"],
        "PAPER_PROFILE_MEM_MB": spec["paper_mem_mb"]["value"],
        "PAPER_PROFILE_CPU_HUNDREDTHS": spec["paper_cpu_hundredths"]["value"],
    }

    for entry in binding["bindings"]:
        config_key = entry["config_key"]
        constant = entry["constant"]
        if config_key not in by_name:
            fail(
                f"config catalogue is missing key {config_key} bound to "
                f"{constant} (SRS-ORCH-002 requires the orchestrator and "
                "the SRS-ARCH-005 catalogue to share the same defaults)"
            )
        catalogue_default = by_name[config_key].get("default")
        constant_value = name_to_value[constant]
        if entry.get("config_unit") == "cores" and entry.get("constant_unit") == "hundredths":
            # Catalogue stores cores as a float (e.g. 0.25); the constant
            # stores hundredths (e.g. 25). Convert cores → hundredths
            # for the equality check.
            try:
                cores = float(catalogue_default)
            except (TypeError, ValueError):
                fail(
                    f"config catalogue default for {config_key} is not "
                    f"parseable as float: {catalogue_default!r}"
                )
            converted = round(cores * 100)
            if converted != constant_value:
                fail(
                    f"config catalogue default {config_key}={catalogue_default} "
                    f"({converted} hundredths) does not match constant "
                    f"{constant}={constant_value}"
                )
        else:
            try:
                catalogue_int = int(catalogue_default)
            except (TypeError, ValueError):
                fail(
                    f"config catalogue default for {config_key} is not "
                    f"an integer: {catalogue_default!r}"
                )
            if catalogue_int != constant_value:
                fail(
                    f"config catalogue default {config_key}={catalogue_int} "
                    f"does not match constant {constant}={constant_value}"
                )

    return (
        f"SRS-ARCH-005 catalogue defaults agree with atp-types constants "
        f"for all {len(binding['bindings'])} resource-profile config keys "
        "(ATP_LIVE_STRATEGY_MEM_MB / _CPU and ATP_PAPER_STRATEGY_MEM_MB / _CPU)"
    )


def check_cargo_test_smoke(config: dict) -> str:
    block = contract_block(config)
    crate = block["orchestrator_crate"]["crate"]
    cargo = shutil.which("cargo")
    if cargo is None:
        return f"cargo test -p {crate} --test orch_2_resource_profile_contract: skipped (cargo not on PATH)"
    integ = subprocess.run(
        [
            cargo,
            "test",
            "-p",
            crate,
            "--test",
            "orch_2_resource_profile_contract",
            "--quiet",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if integ.returncode != 0:
        fail(
            f"cargo test -p {crate} --test orch_2_resource_profile_contract failed:\n"
            f"{integ.stdout}\n{integ.stderr}"
        )
    return (
        f"cargo test -p {crate} --test orch_2_resource_profile_contract: "
        "PASS (default-profile propagation + invalid-profile refusal verified)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


_STATIC_CHECKS = (
    ("spec_constants", check_spec_constants, "types"),
    ("validation_constants", check_validation_constants, "types"),
    ("resource_profile_struct", check_resource_profile_struct, "types"),
    ("resource_profile_error_enum", check_resource_profile_error_enum, "types"),
    ("resource_profile_methods", check_resource_profile_methods, "types"),
    ("orchestrator_helper_methods", check_orchestrator_helper_methods, "orch"),
    ("launch_validate_guard", check_launch_validate_guard, "orch"),
    ("outcome_profile_equality_guard", check_outcome_profile_equality_guard, "orch"),
    ("config_catalogue_binding", check_config_catalogue_binding, "types"),
)


def run_checks() -> list[str]:
    config = load_config()
    types_src = types_source(config)
    orch_src = orchestrator_source(config)
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        source = types_src if scope == "types" else orch_src
        evidence.append(check(config, source))
    evidence.append(check_cargo_test_smoke(config))
    return evidence


def assert_orchestrator_resource_profile_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    types_src = types_source(config, root)
    orch_src = orchestrator_source(config, root)
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        source = types_src if scope == "types" else orch_src
        evidence.append(check(config, source))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-ORCH-002 contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except ResourceProfileCheckError as error:
        print(f"SRS-ORCH-002 FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-ORCH-002 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
