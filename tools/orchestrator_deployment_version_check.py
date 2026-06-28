#!/usr/bin/env python3
"""Contract evidence script for feature SRS-ORCH-004.

Verifies that the strategy-orchestrator deployed-version recording
declared in ``architecture/runtime_services.json`` (block
``deployment_version_contract``) is reachable from the Rust crates
``crates/atp-types`` and ``crates/atp-orchestrator``.

SRS-ORCH-004 traces SyRS SYS-79 / SYS-41 / SYS-21 / IF-9 and StRS
SN-1.01 / SN-1.10 / SN-1.02. The contract guarantees:

* Three spec-literal constants in ``atp-types`` pin the SHA-256 wire
  form: ``SOURCE_HASH_ALGORITHM_PREFIX = "sha256:"``,
  ``SOURCE_HASH_DIGEST_HEX_LENGTH = 64``,
  ``SOURCE_HASH_TOTAL_LENGTH = 71``.
* ``SourceHash`` is a ``pub struct`` newtype wrapping ``String`` with
  the required methods (``new``, ``validate``, ``validate_str``,
  ``as_str``, ``algorithm``, ``digest``).
* ``SourceHashError`` enumerates the four validation-failure variants.
* ``DeployedVersion`` is a ``pub struct`` carrying exactly the two
  required fields (``source_hash``, ``deployed_at_seconds``) and
  rejects vendor / container-runtime / build-system bleed.
* ``StrategyLaunchRequest.deployment_hash`` has type ``SourceHash``
  (not ``String``) — the typed wrapper marks intent and gates
  validation at the orchestrator boundary.
* ``StrategyLaunchOutcome.deployed_version`` carries the recorded
  version through to callers (the audit trail).
* ``OrderErrorCategory::DeployedVersionInvalid`` exists with the
  ``DEPLOYED_VERSION_INVALID`` wire string, and
  ``StructuredOrchestratorError::deployed_version_invalid`` is the
  rejection factory.
* The orchestrator declares the ``DeployedVersionRegistry`` trait
  with ``record`` and ``lookup`` returning ``Result`` types and the
  matching ``DeployedVersionRegistryError`` struct.
* The orchestrator crate itself does NOT import sha2 / sha1 / blake3 /
  sled / rusqlite / redis / git2 — concrete adapters belong on the
  deferred registry implementation.
* ``StrategyOrchestrator::launch`` validates ``request.deployment_hash``
  BEFORE invoking the runtime port, constructs the rejection via
  ``StructuredOrchestratorError::deployed_version_invalid``, and on
  the ``ReadyWithinDeadline`` arm calls ``version_registry.record``
  with the deployed version. The ``DeadlineExceeded`` arm does NOT
  call ``version_registry.record``.

Mirrors the PASS/FAIL output style of
``tools/orchestrator_workload_priority_check.py``.

Invoke:
    python3 tools/orchestrator_deployment_version_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _enum_body, _fn_block, _struct_body, _trait_body  # noqa: F401


class DeploymentVersionCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise DeploymentVersionCheckError(message)


ROOT = Path(__file__).resolve().parents[1]


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "deployment_version_contract" not in config:
        fail("architecture metadata is missing deployment_version_contract")
    return config["deployment_version_contract"]


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


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_spec_constants(config: dict, types_src: str) -> str:
    spec = contract_block(config)["spec_constants"]
    # Algorithm prefix is a string literal — match `pub const NAME: &str = "sha256:";`
    prefix = spec["algorithm_prefix"]
    pattern = re.compile(
        rf'\bpub\s+const\s+{re.escape(prefix["name"])}\s*:\s*&\s*str\s*=\s*"([^"]+)"\s*;'
    )
    match = pattern.search(types_src)
    if match is None:
        fail(
            f"{prefix['name']} is not declared as `pub const {prefix['name']}: &str` "
            "in atp-types — SRS-ORCH-004 requires a single source of truth for "
            "the SHA-256 wire-form prefix"
        )
    if match.group(1) != prefix["value"]:
        fail(
            f"{prefix['name']} has value {match.group(1)!r} but SRS-ORCH-004 / "
            f"SyRS SYS-79 requires {prefix['value']!r}"
        )
    # Numeric constants
    for key in ("digest_hex_length", "total_length"):
        entry = spec[key]
        numeric_pattern = re.compile(
            rf"\bpub\s+const\s+{re.escape(entry['name'])}\s*:\s*usize\s*=\s*([^;]+);"
        )
        num_match = numeric_pattern.search(types_src)
        if num_match is None:
            fail(
                f"{entry['name']} is not declared as `pub const {entry['name']}: usize` "
                "in atp-types"
            )
        # Evaluate simple addition expressions (e.g. `7 + SOURCE_HASH_DIGEST_HEX_LENGTH`)
        # by looking for the numeric literal anywhere in the right-hand side.
        rhs = num_match.group(1).strip()
        if not _eval_const_rhs(rhs, types_src, entry["value"]):
            fail(f"{entry['name']} right-hand side {rhs!r} does not evaluate to {entry['value']}")
    return (
        "atp-types declares the SyRS SYS-79 spec-literal constants — "
        f"{prefix['name']}={prefix['value']!r}, "
        f"{spec['digest_hex_length']['name']}={spec['digest_hex_length']['value']}, "
        f"{spec['total_length']['name']}={spec['total_length']['value']}"
    )


def _eval_const_rhs(rhs: str, types_src: str, expected: int) -> bool:
    """Resolve a simple `<int> + CONST` expression."""
    parts = [part.strip() for part in rhs.split("+")]
    total = 0
    for part in parts:
        if part.isdigit():
            total += int(part)
            continue
        ref = re.search(
            rf"\bpub\s+const\s+{re.escape(part)}\s*:\s*usize\s*=\s*([0-9_]+)\s*;",
            types_src,
        )
        if ref is None:
            return False
        total += int(ref.group(1).replace("_", ""))
    return total == expected


def check_source_hash_struct(config: dict, types_src: str) -> str:
    spec = contract_block(config)["source_hash"]
    pattern = re.compile(
        rf"\bpub\s+struct\s+{re.escape(spec['struct'])}\s*\(\s*{re.escape(spec['newtype_inner'])}\s*\)\s*;"
    )
    if pattern.search(types_src) is None:
        fail(
            f"{spec['struct']} is not declared as "
            f"`pub struct {spec['struct']}({spec['newtype_inner']})` — "
            "SRS-ORCH-004 requires a typed wrapper around the source-hash string"
        )
    missing = [
        method
        for method in spec["methods"]
        if not re.search(rf"\bpub\s+fn\s+{re.escape(method)}\b", types_src)
    ]
    if missing:
        fail(f"{spec['struct']} is missing required methods: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['struct']} as a newtype over "
        f"{spec['newtype_inner']} with methods ({', '.join(spec['methods'])})"
    )


def check_source_hash_error_enum(config: dict, types_src: str) -> str:
    spec = contract_block(config)["source_hash_error"]
    body = _enum_body(types_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with the "
        f"{len(spec['variants'])} validation-failure variants "
        f"({', '.join(spec['variants'])})"
    )


def check_deployed_version_struct(config: dict, types_src: str) -> str:
    spec = contract_block(config)["deployed_version"]
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
            f"{spec['struct']} leaks vendor / container-runtime / build-system "
            f"field(s): {', '.join(leaks)} (SyRS SYS-79 names exactly source "
            "hash + deployment timestamp; coupling to docker_image / git_commit "
            "/ build_number would force the audit trail to track multiple "
            "identifiers for the same code, breaking the SYS-79 single-"
            "identifier guarantee)"
        )
    for method in spec["methods"]:
        if not re.search(rf"\bpub\s+fn\s+{re.escape(method)}\b", types_src):
            fail(f"{spec['struct']} is missing required method `{method}`")
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} required fields "
        f"({', '.join(spec['required_fields'])}), rejects "
        f"{len(spec['forbidden_fields'])} forbidden vendor/build-system "
        f"fields, and exposes methods ({', '.join(spec['methods'])})"
    )


def check_strategy_launch_request_deployment_hash_type(config: dict, types_src: str) -> str:
    expected_type = contract_block(config)["strategy_launch_request_deployment_hash_type"]
    body = _struct_body(types_src, "StrategyLaunchRequest")
    pattern = re.compile(rf"\bpub\s+deployment_hash\s*:\s*{re.escape(expected_type)}\b")
    if pattern.search(body) is None:
        fail(
            "StrategyLaunchRequest.deployment_hash must be typed as "
            f"`{expected_type}` (not `String`) — SRS-ORCH-004 requires the "
            "typed wrapper so the launch gate can validate the wire form at "
            "the orchestrator boundary"
        )
    return (
        f"atp-types pins StrategyLaunchRequest.deployment_hash as `{expected_type}` "
        "so the launch gate validates the SyRS SYS-79 wire form"
    )


def check_strategy_launch_outcome_deployed_version_field(config: dict, types_src: str) -> str:
    field_name = contract_block(config)["strategy_launch_outcome_deployed_version_field"]
    body = _struct_body(types_src, "StrategyLaunchOutcome")
    if not re.search(rf"\bpub\s+{re.escape(field_name)}\s*:\s*DeployedVersion\b", body):
        fail(
            f"StrategyLaunchOutcome is missing `pub {field_name}: DeployedVersion` — "
            "SRS-ORCH-004 requires the deployed version to surface on the "
            "outcome so callers can render the version identifier without "
            "a separate registry round-trip"
        )
    return (
        "atp-types pins StrategyLaunchOutcome.{field}: DeployedVersion so "
        "the launch outcome carries the audit-trail record"
    ).format(field=field_name)


def check_order_error_category_variant_and_wire_string(config: dict, types_src: str) -> str:
    block = contract_block(config)
    variant = block["rejection_category"]
    wire = block["rejection_wire_string"]
    enum_body = _enum_body(types_src, "OrderErrorCategory")
    if not re.search(rf"\b{re.escape(variant)}\b", enum_body):
        fail(
            f"OrderErrorCategory is missing variant `{variant}` — "
            "SRS-ORCH-004 / SyRS SYS-64 require a stable wire category for "
            "deployed-version rejections"
        )
    # The as_str() arm must map the variant to the wire string. There are
    # several as_str methods in atp-types; isolate the OrderErrorCategory
    # impl block before checking.
    impl_match = re.search(r"impl\s+OrderErrorCategory\s*\{", types_src)
    if impl_match is None:
        fail("OrderErrorCategory impl block is missing — cannot check wire string")
    start = impl_match.end()
    depth = 1
    index = start
    while index < len(types_src) and depth:
        ch = types_src[index]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        index += 1
    impl_body = types_src[start : index - 1]
    if not re.search(rf"Self::{re.escape(variant)}\s*=>\s*\"{re.escape(wire)}\"", impl_body):
        fail(
            f"OrderErrorCategory::{variant} does not map to wire string "
            f"`{wire}` in `as_str` — SyRS SYS-64 wire form must be stable"
        )
    return f"atp-types declares OrderErrorCategory::{variant} and maps it to wire string `{wire}`"


def check_deployed_version_invalid_factory(config: dict, types_src: str) -> str:
    factory = contract_block(config)["deployed_version_invalid_factory"]
    if not re.search(rf"\bpub\s+fn\s+{re.escape(factory)}\s*\(", types_src):
        fail(
            f"StructuredOrchestratorError is missing factory method "
            f"`{factory}` — SRS-ORCH-004 rejections must flow through the "
            "single-source-of-truth factory"
        )
    body = _fn_block(types_src, factory)
    if "OrderErrorCategory::DeployedVersionInvalid" not in body:
        fail(
            f"`{factory}` does not reference "
            "`OrderErrorCategory::DeployedVersionInvalid` — the factory must "
            "pin its category invariant"
        )
    if "debug_assert!" not in body:
        fail(
            f"`{factory}` is missing a `debug_assert!` on the category — "
            "mirrors the SRS-ORCH-001/002/003 factory pattern so a future "
            "caller cannot smuggle a different category"
        )
    return (
        f"atp-types declares the rejection factory `{factory}` "
        "with the category invariant pinned via debug_assert"
    )


def check_registry_port(config: dict, orch_src: str) -> str:
    spec = contract_block(config)["deployed_version_registry_port"]
    body = _trait_body(orch_src, spec["trait"])
    for method in spec["methods"]:
        if not re.search(rf"\bfn\s+{re.escape(method)}\b", body):
            fail(f"trait {spec['trait']} is missing method `{method}` (SRS-ORCH-004 port contract)")
    # Pin the typed Result signatures so a future drift to bare unit
    # returns (which would silently swallow registry-IO failures) is
    # caught.
    if spec["record_signature"] not in orch_src:
        fail(
            "trait DeployedVersionRegistry is missing the required "
            f"`record` signature `{spec['record_signature']}` — every "
            "registry-IO method must surface failure through a typed "
            "Result so concrete adapters can be observed by wrapping "
            "callers (codex critic adapter:error-surface)"
        )
    if spec["lookup_signature"] not in orch_src:
        fail(
            "trait DeployedVersionRegistry is missing the required "
            f"`lookup` signature `{spec['lookup_signature']}` — read "
            "path must distinguish 'no record' (Ok(None)) from "
            "'registry failure' (Err)"
        )
    if not re.search(
        rf"\bpub\s+struct\s+{re.escape(spec['registry_error_struct'])}\b",
        orch_src,
    ):
        fail(
            f"struct {spec['registry_error_struct']} is missing — codex "
            "critic adapter:error-surface requires a typed registry-"
            "failure surface"
        )
    for forbidden in spec["forbidden_imports_in_orchestrator_crate"]:
        if forbidden in orch_src:
            fail(
                f"atp-orchestrator imports `{forbidden}` — concrete "
                "adapter for DeployedVersionRegistry (SHA-256 compute, "
                "durable-store backend) must live in a separate crate "
                "(deferred per deployment_version_contract.deferred)"
            )
    return (
        f"atp-orchestrator declares trait {spec['trait']} with methods "
        f"({', '.join(spec['methods'])}), Result-returning IO signatures "
        f"carrying typed failure surface {spec['registry_error_struct']}, "
        "and no vendor-SDK / durable-store / hash-library imports inside the crate"
    )


def check_orchestrator_helper_methods(config: dict, orch_src: str) -> str:
    helpers = contract_block(config)["orchestrator_helper_methods"]
    missing = []
    for method in helpers:
        if not re.search(rf"\bpub\s+fn\s+{re.escape(method)}\b", orch_src):
            missing.append(method)
    if missing:
        fail(
            "StrategyOrchestrator is missing required SRS-ORCH-004 "
            f"helper methods: {', '.join(missing)}"
        )
    return (
        "atp-orchestrator exposes Orchestrator helpers "
        f"({', '.join(helpers)}) so callers can preview the deployed "
        "version identifier without invoking the runtime port"
    )


def check_launch_deployment_version_guard(config: dict, orch_src: str) -> str:
    guard = contract_block(config)["launch_deployment_version_guard"]
    body = _fn_block(orch_src, guard["entry_method"])

    # Validation token must appear (the launch gate must call
    # `request.deployment_hash.validate()` before invoking the runtime).
    if guard["required_validation_token"] not in body:
        fail(
            f"{guard['entry_method']} does not call "
            f"`{guard['required_validation_token']}` — SRS-ORCH-004 "
            "requires defence-in-depth validation of the source hash at "
            "the orchestrator boundary so a misformed override never "
            "reaches `runtime.create`"
        )
    # The validation must happen BEFORE runtime.create. Strip line
    # comments first so a `// reach runtime.create` doc note inside the
    # function body doesn't trip the order check.
    body_no_comments = re.sub(r"//[^\n]*", "", body)
    validation_pos = body_no_comments.find(guard["required_validation_token"])
    create_match = re.search(r"runtime\s*\.\s*create\s*\(", body_no_comments)
    if create_match is None or validation_pos == -1:
        fail(
            f"{guard['entry_method']} is missing either the validation "
            "token or the `runtime.create(` call — invariant cannot be checked"
        )
    if not validation_pos < create_match.start():
        fail(
            f"{guard['entry_method']} calls `runtime.create(` BEFORE "
            "validating `request.deployment_hash` — SRS-ORCH-004 "
            "validate-before-create order is violated (a misformed hash "
            "would reach the runtime)"
        )
    # The rejection factory must appear.
    if guard["required_rejection_factory"] not in body:
        fail(
            f"{guard['entry_method']} does not reference "
            f"`{guard['required_rejection_factory']}` — SRS-ORCH-004 "
            "rejections must flow through the single-source-of-truth factory"
        )
    # version_registry.record must appear (the happy path records the version).
    # Allow whitespace and a newline between the receiver and the method
    # so a rust-fmt'd multi-line call still matches.
    record_token = guard["required_record_call"]
    receiver, method = record_token.split(".", 1)
    record_pattern = re.compile(rf"\b{re.escape(receiver)}\s*\.\s*\n?\s*{re.escape(method)}\s*\(")
    if record_pattern.search(body_no_comments) is None:
        fail(
            f"{guard['entry_method']} does not call `{record_token}(` — "
            "SRS-ORCH-004 / SyRS SYS-79 require the orchestrator to record "
            "the deployed version on a successful launch"
        )
    # The forbidden-on-rejection list: none of these may appear in the
    # rejection arm. Crude check — locate the `deployed_version_invalid`
    # return statement and assert the forbidden tokens are NOT in the
    # short window before it (within the same `if let Err(violation) = ...`
    # block).
    return_idx = body_no_comments.find(guard["required_rejection_factory"])
    if return_idx == -1:
        fail(
            "rejection factory not found inside launch body — "
            "structural invariant cannot be checked"
        )
    # Look at the small window up to the factory call (comments
    # already stripped from body_no_comments).
    window = body_no_comments[max(0, return_idx - 400) : return_idx]
    for forbidden in guard["forbidden_calls_on_deployment_version_rejection"]:
        if forbidden in window:
            fail(
                f"{guard['entry_method']} calls forbidden `{forbidden}` "
                "before returning the DeployedVersionInvalid rejection — "
                "the pre-create rejection must be a pure structured error "
                "with NO sink event, NO version record, and NO runtime "
                "mutation (no container exists to destroy)"
            )

    # DeadlineExceeded arm must NOT call version_registry.record.
    forbidden_record = guard["forbidden_record_call_on_deadline_exceeded_arm"]
    deadline_match = re.search(r"LaunchReadiness::DeadlineExceeded\s*\{[^}]*\}\s*=>\s*\{", body)
    if deadline_match is None:
        fail("launch body is missing the DeadlineExceeded arm")
    arm_start = deadline_match.end()
    depth = 1
    index = arm_start
    while index < len(body) and depth:
        ch = body[index]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        index += 1
    deadline_arm = body[arm_start : index - 1]
    if forbidden_record in deadline_arm:
        fail(
            f"`launch` DeadlineExceeded arm calls forbidden "
            f"`{forbidden_record}` — a version that was never deployed "
            "must not appear in the active-strategy inventory (SyRS SYS-41) "
            "or REST API listing (IF-9)"
        )
    return (
        "atp-orchestrator pins the launch gate: "
        "validate-deployment_hash-before-runtime.create, rejection through "
        "the deployed_version_invalid factory with no sink event / "
        "registry record / runtime call, version_registry.record on the "
        "ReadyWithinDeadline arm only (DeadlineExceeded arm skips it)"
    )


def run_cargo_smoke(config: dict, root: Path = ROOT) -> str:
    """Smoke-test the new orch_4 integration tests by compiling and running."""
    cargo = shutil.which("cargo")
    if cargo is None:
        return "cargo not on PATH — skipping integration-test smoke"
    result = subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-orchestrator",
            "--test",
            "orch_4_deployment_version_contract",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        fail(
            "cargo test -p atp-orchestrator --test orch_4_deployment_version_contract "
            f"failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return "cargo test orch_4_deployment_version_contract passed"


CHECKS = (
    ("spec_constants", check_spec_constants, "types"),
    ("source_hash_struct", check_source_hash_struct, "types"),
    ("source_hash_error_enum", check_source_hash_error_enum, "types"),
    ("deployed_version_struct", check_deployed_version_struct, "types"),
    (
        "strategy_launch_request_deployment_hash_type",
        check_strategy_launch_request_deployment_hash_type,
        "types",
    ),
    (
        "strategy_launch_outcome_deployed_version_field",
        check_strategy_launch_outcome_deployed_version_field,
        "types",
    ),
    (
        "order_error_category_variant_and_wire_string",
        check_order_error_category_variant_and_wire_string,
        "types",
    ),
    (
        "deployed_version_invalid_factory",
        check_deployed_version_invalid_factory,
        "types",
    ),
    ("registry_port", check_registry_port, "orch"),
    ("orchestrator_helper_methods", check_orchestrator_helper_methods, "orch"),
    (
        "launch_deployment_version_guard",
        check_launch_deployment_version_guard,
        "orch",
    ),
)


def collect_evidence(root: Path = ROOT) -> list[str]:
    config = load_config(root)
    types_src = types_source(config, root)
    orch_src = orchestrator_source(config, root)
    evidence: list[str] = []
    for _, fn, source in CHECKS:
        if source == "types":
            evidence.append(fn(config, types_src))
        else:
            evidence.append(fn(config, orch_src))
    return evidence


def assert_orchestrator_deployment_version_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    types_src = types_source(config, root)
    orch_src = orchestrator_source(config, root)
    evidence: list[str] = []
    for _, fn, source in CHECKS:
        if source == "types":
            evidence.append(fn(config, types_src))
        else:
            evidence.append(fn(config, orch_src))
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-cargo",
        action="store_true",
        help="Skip the cargo integration-test smoke (useful when running "
        "inside another test harness that already invokes cargo).",
    )
    args = parser.parse_args()
    try:
        evidence = collect_evidence()
        if not args.skip_cargo:
            evidence.append(run_cargo_smoke(load_config()))
    except DeploymentVersionCheckError as error:
        print(f"SRS-ORCH-004 FAIL: {error}")
        return 1
    print("SRS-ORCH-004 PASS")
    for line in evidence:
        print(f"  - {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
