#!/usr/bin/env python3
"""Contract evidence script for feature SRS-ORCH-003.

Verifies that the strategy-orchestrator workload-priority admission gate
declared in ``architecture/runtime_services.json`` (block
``workload_priority_contract``) is reachable from the Rust crates
``crates/atp-types`` and ``crates/atp-orchestrator``.

SRS-ORCH-003 traces SyRS SYS-57 / SYS-58 and StRS SN-1.10 / C-6 / BG-1 /
BG-6. The contract guarantees:

* One spec-literal constant in ``atp-types`` carries the SyRS SYS-57
  default safety margin (2048 MB) and two validation-bound constants
  mirror the SRS-ARCH-005 catalogue floor/ceiling (256 / 1048576 MB).
* ``HostMemorySafetyMargin`` is a ``pub struct`` with exactly one
  required field (``mb``) and rejects vendor / container-runtime /
  unit-confusion bleed.
* ``HostMemorySafetyMarginError`` enumerates the two validation-failure
  variants.
* ``WorkloadPriority`` enumerates the SYS-57 hierarchy in exact order
  with the ``rank()`` method returning 1..=7 (lower = higher priority).
* ``WorkloadKind`` enumerates ``Continuous`` / ``Batch`` and the per-
  priority default mapping matches SYS-58 (b) (live / market-data /
  paper are Continuous; ingestion / factor / backtest / research are
  Batch).
* ``WorkloadId`` is a newtype wrapping ``String``.
* ``RegisteredWorkload`` carries exactly the four required fields
  (``id``, ``priority``, ``kind``, ``profile``) and rejects vendor /
  container-runtime bleed.
* ``WorkloadAdmissionEvent`` enumerates ``Refused`` / ``Terminated``
  with the required fields per variant.
* The orchestrator declares the three ports (``HostMemoryProbe``,
  ``WorkloadRegistry``, ``WorkloadEventSink``) with the required
  methods.
* The orchestrator crate itself does NOT import sysinfo / procfs /
  bollard / docker_api — these belong on a future adapter crate.
* The orchestrator exposes the five helpers
  (``host_memory_safety_margin_default``, ``safety_margin_from_lookup``,
  ``safety_margin_via_env_lookup``, ``safety_margin_from_env``,
  ``admit_workload``).
* ``StrategyOrchestrator::admit_workload`` filters candidates to
  ``WorkloadKind::Batch``, contains the live-immunity debug_assert,
  constructs the rejection via
  ``StructuredOrchestratorError::host_memory_safety_margin_breach``,
  and the refusal arm calls NONE of the forbidden runtime mutators.
* The catalogue default in ``configuration.keys`` for
  ATP_HOST_MEMORY_SAFETY_MARGIN_MB equals the spec-literal constant
  in ``atp-types``, and the catalogue min/max equal the validation
  constants.

Mirrors the PASS/FAIL output style of
``tools/orchestrator_resource_profile_check.py``.

Invoke:
    python3 tools/orchestrator_workload_priority_check.py
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


def _const_or_pub_fn_block(source: str, fn_name: str) -> str:
    """Variant of ``_fn_block`` that accepts ``pub const fn`` declarations
    in addition to ``pub fn``. SRS-ORCH-003's ``rank``, ``default_kind``,
    ``new``, ``default_margin``, ``as_str``, and similar helpers are
    ``const fn`` so they can be evaluated in const contexts and so a
    future caller can pin them in ``const`` items without runtime cost.
    """
    match = re.search(rf"\bpub\s+(?:const\s+)?fn\s+{re.escape(fn_name)}\b[^\{{]*\{{", source)
    if not match:
        fail(f"Rust source is missing function `{fn_name}`")
    start = match.end()
    depth = 1
    index = start
    while index < len(source) and depth:
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    if depth:
        fail(f"could not parse function body for `{fn_name}`")
    return source[start : index - 1]


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class WorkloadPriorityCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise WorkloadPriorityCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "workload_priority_contract" not in config:
        fail("architecture metadata is missing workload_priority_contract")
    return config["workload_priority_contract"]


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
            "SRS-ORCH-003 requires a single source of truth for each spec literal"
        )
    literal = int(match.group(1).replace("_", ""))
    if literal != expected:
        fail(f"{name} has value {literal} but SRS-ORCH-003 / SyRS SYS-57 requires {expected}")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_spec_constants(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["spec_constants"]
    entry = spec["safety_margin_default_mb"]
    _assert_pub_const_u32(types_src, entry["name"], entry["value"])
    return (
        "atp-types declares the SyRS SYS-57 spec-literal constant — "
        f"{entry['name']}={entry['value']}"
    )


def check_validation_constants(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["validation_constants"]
    for key in ("safety_margin_floor_mb", "safety_margin_ceiling_mb"):
        entry = spec[key]
        _assert_pub_const_u32(types_src, entry["name"], entry["value"])
    return (
        "atp-types declares the SRS-ARCH-005 catalogue-aligned validation "
        f"bounds — safety margin in [{spec['safety_margin_floor_mb']['value']}, "
        f"{spec['safety_margin_ceiling_mb']['value']}] MB"
    )


def check_host_memory_safety_margin_struct(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["host_memory_safety_margin"]
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
            f"{spec['struct']} leaks vendor / container-runtime / unit-"
            f"confusion field(s): {', '.join(leaks)} (SRS-ORCH-003 wire "
            "types must stay free of Docker-Engine-specific shape and "
            "ambiguous unit drift)"
        )
    for method in spec["methods"]:
        if not re.search(rf"\bpub\s+(?:const\s+)?fn\s+{re.escape(method)}\b", types_src):
            fail(f"{spec['struct']} is missing required method `{method}`")
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} required field(s) "
        f"({', '.join(spec['required_fields'])}), rejects "
        f"{len(spec['forbidden_fields'])} forbidden vendor/unit fields, "
        f"and exposes methods ({', '.join(spec['methods'])})"
    )


def check_host_memory_safety_margin_error_enum(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["host_memory_safety_margin_error"]
    body = _enum_body(types_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} "
        f"validation-failure variants ({', '.join(spec['variants'])})"
    )


def check_workload_priority_enum(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["workload_priority_enum"]
    body = _enum_body(types_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing SYS-57 hierarchy variants: {', '.join(missing)}")
    # The hierarchy must appear in the exact spec order — a future
    # refactor that re-orders the variants would silently re-rank them.
    positions = [body.find(v) for v in spec["variants"]]
    if positions != sorted(positions):
        fail(
            f"{spec['enum']} variants are out of SYS-57 hierarchy order — "
            "must appear as LiveStrategy, MarketDataSubscriptionManager, "
            "PaperStrategy, NightlyDataIngestion, FactorPipeline, "
            "Backtest, Research"
        )
    # The rank method must map each variant to the spec'd rank.
    rank_fn_body = _const_or_pub_fn_block(types_src, spec["rank_method"])
    ranks = spec["ranks"]
    for variant, expected in ranks.items():
        arm = re.search(rf"Self::{re.escape(variant)}\s*=>\s*([0-9]+)", rank_fn_body)
        if arm is None:
            fail(
                f"WorkloadPriority::{variant} is missing a rank-method arm — "
                "SYS-57 hierarchy requires each variant to map to a stable rank"
            )
        if int(arm.group(1)) != expected:
            fail(
                f"WorkloadPriority::{variant} rank is {arm.group(1)} but "
                f"SYS-57 hierarchy requires {expected}"
            )
    # The forbidden numeric-priority tokens must not appear anywhere
    # in the enum body or the rank method (a weighted score, a quota,
    # or a class id would invite drift away from the SYS-57 ordinal
    # hierarchy).
    for token in spec.get("forbidden_tokens", []):
        if re.search(rf"\b{re.escape(token)}\b", body) or re.search(
            rf"\b{re.escape(token)}\b", rank_fn_body
        ):
            fail(
                f"{spec['enum']} mentions forbidden token `{token}` — "
                "SyRS SYS-57 is a categorical hierarchy, not a weighted "
                "score / quota / class id"
            )
    return (
        f"atp-types declares {spec['enum']} with the SYS-57 hierarchy "
        f"in exact order ({', '.join(spec['variants'])}) and "
        f"{spec['rank_method']}() mapping live=1 → research=7"
    )


def check_workload_kind_enum(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["workload_kind_enum"]
    body = _enum_body(types_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    # default_kind must map every WorkloadPriority variant to the
    # spec'd WorkloadKind.
    default_fn_body = _const_or_pub_fn_block(types_src, "default_kind")
    mapping = spec["kind_for_priority"]
    for priority, kind in mapping.items():
        if not re.search(rf"Self::{re.escape(priority)}\b", default_fn_body):
            fail(f"default_kind is missing arm for WorkloadPriority::{priority}")
        # Each priority must route to its spec'd kind. The match arm
        # may group several priorities under one arm via `|` so we
        # check the kind appears within a reasonable window after the
        # priority name. Locate the arm that mentions this priority
        # and look ahead for the kind token (until the next `,` or
        # end of body).
        arm_start = default_fn_body.find(f"Self::{priority}")
        # Find the next `=>` after this priority and then the kind
        # token within the arm body (until the next comma at depth 0).
        arrow = default_fn_body.find("=>", arm_start)
        if arrow == -1:
            fail(f"default_kind arm for WorkloadPriority::{priority} is malformed")
        # Read until the next top-level `,`.
        depth = 0
        index = arrow + 2
        while index < len(default_fn_body):
            ch = default_fn_body[index]
            if ch in "({":
                depth += 1
            elif ch in ")}":
                if depth == 0:
                    break
                depth -= 1
            elif ch == "," and depth == 0:
                break
            index += 1
        arm_body = default_fn_body[arrow + 2 : index]
        if f"WorkloadKind::{kind}" not in arm_body:
            fail(
                f"default_kind routes WorkloadPriority::{priority} to a "
                f"kind other than WorkloadKind::{kind} — SyRS SYS-58 (b) "
                f"requires {priority} → {kind}"
            )
    return (
        f"atp-types declares {spec['enum']} with variants "
        f"({', '.join(spec['variants'])}) and default_kind routes each "
        "SYS-57 priority to the SYS-58 (b) batch/continuous split"
    )


def check_workload_id_newtype(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["workload_id"]
    if not re.search(
        rf"\bpub\s+struct\s+{re.escape(spec['struct'])}\s*\(\s*{re.escape(spec['newtype_inner'])}\s*\)\s*;",
        types_src,
    ):
        fail(
            f"{spec['struct']} is not declared as "
            f"`pub struct {spec['struct']}({spec['newtype_inner']})` — "
            "SRS-ORCH-003 requires a stable newtype to identify workloads"
        )
    for method in spec["methods"]:
        if not re.search(rf"\bpub\s+fn\s+{re.escape(method)}\b", types_src):
            fail(f"{spec['struct']} is missing required method `{method}`")
    return (
        f"atp-types declares {spec['struct']} as a newtype over "
        f"{spec['newtype_inner']} with methods "
        f"({', '.join(spec['methods'])})"
    )


def check_registered_workload_struct(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["registered_workload"]
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
            f"{spec['struct']} leaks vendor / container-runtime / temporal "
            f"field(s): {', '.join(leaks)} (the registry projection must "
            "stay free of Docker-Engine-specific shape; uptime / started_at "
            "would couple the gate's decisions to wall-clock state)"
        )
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} required fields "
        f"({', '.join(spec['required_fields'])}) and rejects "
        f"{len(spec['forbidden_fields'])} forbidden vendor/temporal fields"
    )


def check_workload_admission_event_enum(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["workload_admission_event"]
    body = _enum_body(types_src, spec["enum"])
    for variant in spec["variants"]:
        if not re.search(rf"\b{re.escape(variant)}\b", body):
            fail(f"{spec['enum']} is missing variant `{variant}`")
    # Required fields per variant.
    for variant, required in (
        ("Refused", spec["refused_required_fields"]),
        ("Terminated", spec["terminated_required_fields"]),
        ("TerminationFailed", spec["termination_failed_required_fields"]),
        ("HostProbeFailed", spec["host_probe_failed_required_fields"]),
        ("RegistryListingFailed", spec["registry_listing_failed_required_fields"]),
    ):
        # Locate the variant block (everything between `Variant {` and
        # the matching `}` at depth 0).
        anchor = re.search(rf"\b{re.escape(variant)}\s*\{{", body)
        if anchor is None:
            fail(f"{spec['enum']} variant `{variant}` is not a struct variant")
        start = anchor.end()
        depth = 1
        index = start
        while index < len(body) and depth:
            ch = body[index]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            index += 1
        variant_block = body[start : index - 1]
        for field in required:
            if not re.search(rf"\b{re.escape(field)}\s*:", variant_block):
                fail(f"{spec['enum']}::{variant} is missing required field `{field}`")
        for forbidden in spec["forbidden_fields"]:
            if re.search(rf"\b{re.escape(forbidden)}\s*:", variant_block):
                fail(
                    f"{spec['enum']}::{variant} leaks dispatcher-specific "
                    f"field `{forbidden}` — the audit payload must stay "
                    "decoupled from any specific notification dispatcher"
                )
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} "
        f"variants ({', '.join(spec['variants'])}); Refused carries "
        f"{len(spec['refused_required_fields'])} fields; Terminated "
        f"carries {len(spec['terminated_required_fields'])} fields; "
        f"TerminationFailed carries "
        f"{len(spec['termination_failed_required_fields'])} fields; "
        f"HostProbeFailed carries "
        f"{len(spec['host_probe_failed_required_fields'])} fields; "
        f"RegistryListingFailed carries "
        f"{len(spec['registry_listing_failed_required_fields'])} fields"
    )


def check_ports(config: dict, orch_src: str) -> str:
    block = contract_block(config)
    declared = []
    for port_key in (
        "host_memory_probe_port",
        "workload_registry_port",
        "workload_event_sink_port",
    ):
        spec = block[port_key]
        body = _trait_body(orch_src, spec["trait"])
        for method in spec["methods"]:
            if not re.search(rf"\bfn\s+{re.escape(method)}\b", body):
                fail(
                    f"trait {spec['trait']} is missing method `{method}` "
                    "(SRS-ORCH-003 port contract)"
                )
        # Forbidden imports inside the orchestrator crate — these
        # vendor SDKs / OS APIs belong on a future adapter crate, not
        # in atp-orchestrator. The contract pin keeps the crate clean.
        for forbidden in spec.get("forbidden_imports_in_orchestrator_crate", []):
            if forbidden in orch_src:
                fail(
                    f"atp-orchestrator imports `{forbidden}` — concrete "
                    f"adapter for {spec['trait']} must live in a separate "
                    "crate (deferred per workload_priority_contract.deferred)"
                )
        declared.append(spec["trait"])

    # codex critic adapter:error-surface — every host-IO / registry-IO
    # port method must return Result so the gate can distinguish a
    # successful call from a probe / Docker / registry failure. The
    # contract pins each method's signature and the matching error
    # struct.
    probe_spec = block["host_memory_probe_port"]
    for sig_key, struct_key in (("available_mb_signature", "probe_error_struct"),):
        sig = probe_spec.get(sig_key)
        if sig and sig not in orch_src:
            fail(
                f"trait HostMemoryProbe is missing the required "
                f"signature `{sig}` — codex critic adapter:error-surface "
                "requires the gate to fail closed on probe error"
            )
        struct = probe_spec.get(struct_key)
        if struct and not re.search(rf"\bpub\s+struct\s+{re.escape(struct)}\b", orch_src):
            fail(
                f"struct {struct} is missing — codex critic "
                "adapter:error-surface requires a typed probe-failure "
                "surface"
            )

    registry_spec = block["workload_registry_port"]
    for sig_key in ("active_signature", "terminate_signature"):
        sig = registry_spec.get(sig_key)
        if sig and sig not in orch_src:
            fail(
                f"trait WorkloadRegistry is missing the required "
                f"signature `{sig}` — codex critic adapter:error-surface "
                "requires every registry-IO method to surface failure"
            )
    for struct_key in ("registry_error_struct", "termination_error_struct"):
        struct = registry_spec.get(struct_key)
        if struct and not re.search(rf"\bpub\s+struct\s+{re.escape(struct)}\b", orch_src):
            fail(
                f"struct {struct} is missing — codex critic "
                "adapter:error-surface requires a typed failure surface"
            )

    # codex critic adapter:error-surface — WorkloadEventSink::record
    # must return Result so concrete sink implementations and any
    # wrapping caller (a retry logger, a dropped-alert counter, the
    # deferred SRS-NOTIF-001 dispatcher) can observe publication
    # failure. The orchestrator's admit_workload gate itself emits
    # alerts on a best-effort basis — the typed surface lives outside
    # the gate, not inside it.
    sink_spec = block["workload_event_sink_port"]
    record_sig = sink_spec.get("record_signature")
    if record_sig and record_sig not in orch_src:
        fail(
            f"trait WorkloadEventSink is missing the required signature "
            f"`{record_sig}` — codex critic adapter:error-surface "
            "requires record to expose a typed publication-failure "
            "surface for concrete sinks and wrapping callers, even "
            "though the orchestrator gate itself is best-effort"
        )
    sink_error_struct = sink_spec.get("event_sink_error_struct")
    if sink_error_struct and not re.search(
        rf"\bpub\s+struct\s+{re.escape(sink_error_struct)}\b", orch_src
    ):
        fail(
            f"struct {sink_error_struct} is missing — codex critic "
            "adapter:error-surface requires a typed sink-failure surface"
        )
    # WorkloadTerminationError must carry a reason string (not just
    # the workload id) so registry termination failures can be
    # discriminated by the dashboard (codex critic
    # adapter:termination-error-surface).
    termination_struct_body = _struct_body(orch_src, "WorkloadTerminationError")
    if not re.search(r"\bpub\s+reason\s*:\s*String\b", termination_struct_body):
        fail(
            "struct WorkloadTerminationError is missing `pub reason: String` "
            "— codex critic adapter:termination-error-surface requires "
            "the typed reason to surface registry termination causes "
            "(Docker shutdown timeout, cgroup permission denied, etc.) "
            "instead of collapsing to a generic message"
        )

    return (
        "atp-orchestrator declares the SRS-ORCH-003 ports "
        f"({', '.join(declared)}) with the required methods, "
        "Result-returning IO signatures (`available_mb`, `active`, "
        "`terminate`) carrying typed failure surfaces (HostMemoryProbeError, "
        "WorkloadRegistryError, WorkloadTerminationError), and no "
        "vendor-SDK / OS-API imports inside the crate"
    )


def check_orchestrator_helper_methods(config: dict, orch_src: str) -> str:
    block = contract_block(config)
    helpers = block["orchestrator_helper_methods"]
    missing = []
    for method in helpers:
        if not re.search(rf"\bpub\s+(?:const\s+)?fn\s+{re.escape(method)}\b", orch_src):
            missing.append(method)
    if missing:
        fail(
            f"StrategyOrchestrator is missing required SRS-ORCH-003 "
            f"helper methods: {', '.join(missing)}"
        )
    return (
        f"atp-orchestrator exposes Orchestrator helpers "
        f"({', '.join(helpers)}) so callers populate admission inputs "
        "without reaching into atp_types"
    )


def check_admit_workload_guard(config: dict, orch_src: str) -> str:
    block = contract_block(config)
    guard = block["admit_workload_guard"]
    body = _fn_block(orch_src, guard["entry_method"])

    # Required call-order tokens. Each must appear, and they must
    # appear in the declared order. The main canonical arm is
    # probe → active → terminate; sink.record is allowed at any
    # position because the margin-validation early-exit legitimately
    # emits a Refused event before the main arbitration loop reaches
    # `registry.active`.
    last_pos = -1
    for token in guard["required_call_order"]:
        if "." not in token:
            fail(
                f"required_call_order token `{token}` is not a method "
                "call (must be of the form `receiver.method`)"
            )
        receiver, method = token.split(".", 1)
        pattern = re.compile(rf"\b{re.escape(receiver)}\s*\.\s*{re.escape(method)}\s*\(")
        match = pattern.search(body)
        if match is None:
            fail(
                f"{guard['entry_method']} does not call `{token}(` — "
                "SyRS SYS-57 / SYS-58 admission gate requires the "
                "probe → arbitrate → terminate sequence"
            )
        pos = match.start()
        if pos < last_pos:
            fail(
                f"{guard['entry_method']} calls `{token}(` out of the "
                "required order — must appear in the sequence "
                f"{' → '.join(guard['required_call_order'])}"
            )
        last_pos = pos

    # Tokens whose existence is required but whose position is not
    # canonically ordered (sink.record fires from both the
    # margin-validation early-exit and the main refusal arm).
    for token in guard.get("required_calls_without_order", []):
        if "." not in token:
            fail(f"required_calls_without_order token `{token}` is not a method call")
        receiver, method = token.split(".", 1)
        pattern = re.compile(rf"\b{re.escape(receiver)}\s*\.\s*{re.escape(method)}\s*\(")
        if pattern.search(body) is None:
            fail(
                f"{guard['entry_method']} does not call `{token}(` — "
                "the admission gate must emit at least one audit event "
                "on every refusal / termination path"
            )

    # Kind filter must be present so the loop only considers Batch
    # candidates (SyRS SYS-58 (b)).
    filter_token = guard["required_kind_filter_token"]
    if filter_token not in body:
        fail(
            f"{guard['entry_method']} does not filter candidates to "
            f"`{filter_token}` — SyRS SYS-58 (b) restricts eviction to "
            "batch workloads"
        )

    # Live-immunity debug_assert must be present.
    live_token = guard["required_live_immunity_token"]
    if live_token not in body or "debug_assert" not in body:
        fail(
            f"{guard['entry_method']} is missing the `debug_assert!` "
            f"on `{live_token}` — SyRS SYS-58 last clause requires the "
            "live-trading strategy to be unconditionally protected from "
            "eviction"
        )

    # Defence-in-depth: the gate must validate the configured safety
    # margin so a programmatically-constructed margin (test fixture,
    # future REST override) cannot disable the gate.
    margin_token = guard["required_margin_validation_token"]
    if margin_token not in body:
        fail(
            f"{guard['entry_method']} does not call `{margin_token}` "
            "at the gate entry — SRS-ORCH-003 + SRS-ARCH-005 require "
            "defence-in-depth validation of the configured safety "
            "margin so an invalid override cannot silently disable "
            "the gate (codex critic: safety:margin-validation-bypass)"
        )

    # Pre-eviction feasibility: the gate must compute whether the sum
    # of eligible recoverable memory is enough BEFORE issuing any
    # terminate call, so it never kills batch workloads and still
    # refuses (codex critic: orch:partial-eviction-refusal).
    feasibility_token = guard["required_pre_eviction_feasibility_token"]
    if feasibility_token not in body:
        fail(
            f"{guard['entry_method']} does not include the pre-eviction "
            f"feasibility marker `{feasibility_token}` — SyRS SYS-58 (b) "
            "requires the gate to prove enough memory can be freed "
            "BEFORE killing any work (codex critic: "
            "orch:partial-eviction-refusal)"
        )

    # Rejection factory.
    factory_token = guard["rejection_factory"] + "("
    if factory_token not in body:
        fail(
            f"{guard['entry_method']} does not construct the rejection "
            f"via `{factory_token}` — SyRS SYS-64 wire-string single "
            "source of truth"
        )

    # Forbidden runtime calls anywhere in the body — the gate sits in
    # front of the runtime port.
    for forbidden in guard.get("forbidden_calls_on_refusal", []):
        if forbidden + "(" in body:
            fail(
                f"{guard['entry_method']} invokes forbidden runtime "
                f"mutator `{forbidden}` — the admission gate sits in "
                "front of the runtime port and must not touch it"
            )

    return (
        f"atp-orchestrator::{guard['entry_method']} performs the "
        f"SYS-57/SYS-58 sequence ({' → '.join(guard['required_call_order'])}) "
        f"plus audit emission ({', '.join(guard.get('required_calls_without_order', []))}), "
        f"filters to `{filter_token}`, debug_asserts the live-immunity "
        f"invariant on `{live_token}`, validates the safety margin via "
        f"`{margin_token}`, pre-checks total recoverable memory before "
        f"any eviction (`{feasibility_token}`), refuses via "
        f"`{guard['rejection_factory']}` "
        f"({guard['rejection_wire_string']}), and invokes none of the "
        f"{len(guard.get('forbidden_calls_on_refusal', []))} forbidden "
        "runtime mutators"
    )


def check_config_catalogue_binding(config: dict, _types_src: str) -> str:
    block = contract_block(config)
    binding = block["config_binding"]
    catalogue = config.get("configuration", {}).get("keys", [])
    by_name = {entry["name"]: entry for entry in catalogue}

    constant_to_value = {
        "HOST_MEMORY_SAFETY_MARGIN_MB_DEFAULT": block["spec_constants"]["safety_margin_default_mb"][
            "value"
        ],
        "HOST_MEMORY_SAFETY_MARGIN_MB_FLOOR": block["validation_constants"][
            "safety_margin_floor_mb"
        ]["value"],
        "HOST_MEMORY_SAFETY_MARGIN_MB_CEILING": block["validation_constants"][
            "safety_margin_ceiling_mb"
        ]["value"],
    }

    for entry in binding["bindings"]:
        config_key = entry["config_key"]
        if config_key not in by_name:
            fail(
                f"config catalogue is missing key {config_key} bound to "
                f"{entry['constant']} (SRS-ORCH-003 requires the orchestrator "
                "and the SRS-ARCH-005 catalogue to share the safety-margin "
                "defaults and bounds)"
            )
        catalogue_entry = by_name[config_key]
        catalogue_default = catalogue_entry.get("default")
        validator = catalogue_entry.get("validator", {}) or {}
        catalogue_min = validator.get("min")
        catalogue_max = validator.get("max")
        expected_default = constant_to_value[entry["constant"]]
        expected_floor = constant_to_value[entry["floor_constant"]]
        expected_ceiling = constant_to_value[entry["ceiling_constant"]]
        try:
            catalogue_default_int = int(catalogue_default)
            catalogue_min_int = int(catalogue_min)
            catalogue_max_int = int(catalogue_max)
        except (TypeError, ValueError):
            fail(
                f"config catalogue default/validator.min/validator.max for "
                f"{config_key} is not parseable as int: "
                f"{catalogue_default!r} / {catalogue_min!r} / "
                f"{catalogue_max!r}"
            )
        if catalogue_default_int != expected_default:
            fail(
                f"config catalogue default {config_key}={catalogue_default_int} "
                f"does not match constant {entry['constant']}={expected_default}"
            )
        if catalogue_min_int != expected_floor:
            fail(
                f"config catalogue {config_key}.validator.min={catalogue_min_int} "
                f"does not match constant "
                f"{entry['floor_constant']}={expected_floor}"
            )
        if catalogue_max_int != expected_ceiling:
            fail(
                f"config catalogue {config_key}.validator.max={catalogue_max_int} "
                f"does not match constant "
                f"{entry['ceiling_constant']}={expected_ceiling}"
            )

    return (
        f"SRS-ARCH-005 catalogue default + min + max agree with atp-types "
        f"constants for the safety-margin config key "
        f"({binding['bindings'][0]['config_key']})"
    )


def check_cargo_test_smoke(config: dict) -> str:
    block = contract_block(config)
    crate = block["orchestrator_crate"]["crate"]
    cargo = shutil.which("cargo")
    if cargo is None:
        return (
            f"cargo test -p {crate} --test orch_3_workload_priority_contract: "
            "skipped (cargo not on PATH)"
        )
    integ = subprocess.run(
        [
            cargo,
            "test",
            "-p",
            crate,
            "--test",
            "orch_3_workload_priority_contract",
            "--quiet",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if integ.returncode != 0:
        fail(
            f"cargo test -p {crate} --test orch_3_workload_priority_contract failed:\n"
            f"{integ.stdout}\n{integ.stderr}"
        )
    return (
        f"cargo test -p {crate} --test orch_3_workload_priority_contract: "
        "PASS (admission happy path + refusal + eviction + live-immunity verified)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


_STATIC_CHECKS = (
    ("spec_constants", check_spec_constants, "types"),
    ("validation_constants", check_validation_constants, "types"),
    ("host_memory_safety_margin_struct", check_host_memory_safety_margin_struct, "types"),
    ("host_memory_safety_margin_error_enum", check_host_memory_safety_margin_error_enum, "types"),
    ("workload_priority_enum", check_workload_priority_enum, "types"),
    ("workload_kind_enum", check_workload_kind_enum, "types"),
    ("workload_id_newtype", check_workload_id_newtype, "types"),
    ("registered_workload_struct", check_registered_workload_struct, "types"),
    ("workload_admission_event_enum", check_workload_admission_event_enum, "types"),
    ("ports", check_ports, "orch"),
    ("orchestrator_helper_methods", check_orchestrator_helper_methods, "orch"),
    ("admit_workload_guard", check_admit_workload_guard, "orch"),
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


def assert_orchestrator_workload_priority_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    types_src = types_source(config, root)
    orch_src = orchestrator_source(config, root)
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        source = types_src if scope == "types" else orch_src
        evidence.append(check(config, source))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-ORCH-003 contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except WorkloadPriorityCheckError as error:
        print(f"SRS-ORCH-003 FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-ORCH-003 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
