#!/usr/bin/env python3
"""Contract evidence script for feature SRS-SDK-004 (order event callbacks).

Verifies the **Rust-core side** of the SRS-SDK-004 order-event callback
contract: the source-neutral category authority declared in
``crates/atp-types/src/order_event.rs`` matches the machine-readable mirror in
``architecture/runtime_services.json`` (block ``order_event_dispatch_contract``),
and is kept in lock-step with the Python SDK surface
(``strategy_api_order_events_contract`` + the ``atp_strategy`` package).

SRS-SDK-004 ("deliver order event callbacks to Python strategy code") traces
SyRS SYS-7 / SYS-85 / NFR-P4 and StRS SN-1.22 / SN-1.29. AGENTS.md forbids the
Rust core from depending on the Python SDK, so the live (``atp-execution``,
SRS-EXE-001) and paper (``atp-simulation``, SRS-SIM-001) dispatchers derive the
callback category for a lifecycle transition from one shared authority in
``atp-types`` — ``OrderEventCategory::for_transition`` — so identical
transitions yield identical categories for live and paper by construction
(SRS-SDK-001 / AC-14). This check guarantees:

  (a) ``OrderEventCategory`` declares the six categories and ``as_str`` maps each
      to its stable wire string; the wire set equals ``all_categories`` and the
      Python ``OrderEventType`` member values one-for-one.
  (b) ``OrderEventCategory::for_state`` maps every one of the nine
      ``OrderState`` wire strings to its category (or ``null``) exactly as the
      documented ``state_to_event_category`` mirror (totality; no missing, no
      undocumented).
  (c) ``OrderEventCategory::for_transition`` is fail-closed — it consults
      ``OrderState::can_transition_to`` and returns
      ``OrderLifecycleError::IllegalTransition`` for an edge not in the graph
      (no event for an impossible transition).
  (d) the per-category field-presence predicates match the contract:
      ``requires_fill_economics`` covers exactly ``ac_named_categories`` (and
      delegates to ``is_ac_named``); ``requires_reason`` covers
      ``categories_requiring_reason``.
  (e) ``ac_named_categories`` equals the canonical
      ``strategy_api_order_events_contract.ac_named_callback_categories`` (one
      source of truth across the Rust + Python + JSON surfaces).
  (f) the Rust latency consts ``LIVE_CALLBACK_LATENCY_P95_MS`` /
      ``PAPER_CALLBACK_LATENCY_P95_MS`` equal the canonical
      ``strategy_api_order_events_contract`` budgets AND the Python SDK
      constants (NFR-P4 single source of truth).

This is the SDK-surface / pure-logic half only. The AC field *values*, actual
delivery to Python, and the NFR-P4 p95 *latency proof* are owned by the runtime
dispatchers (see ``order_event_dispatch_contract.deferred``); SRS-SDK-004 stays
``passes: false`` in ``feature_list.json`` until those land.

Mirrors the PASS/FAIL output style of ``tools/order_lifecycle_check.py``.

Invoke:
    python3 tools/order_event_dispatch_check.py
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"
TYPES_CRATE = "crates/atp-types"


class OrderEventDispatchCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise OrderEventDispatchCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "order_event_dispatch_contract" not in config:
        fail("architecture metadata is missing order_event_dispatch_contract")
    return config["order_event_dispatch_contract"]


def latency_source_block(config: dict) -> dict:
    block = contract_block(config)
    name = block["latency_source_block"]
    if name not in config:
        fail(f"order_event_dispatch_contract.latency_source_block names a missing block: {name}")
    return config[name]


def types_source(root: Path = ROOT) -> str:
    """Return lib.rs + order_lifecycle.rs + order_event.rs concatenated.

    ``OrderState`` (and its ``as_str`` wire strings) lives in order_lifecycle.rs;
    the ``OrderEventCategory`` authority lives in order_event.rs. The
    brace-matching helpers search the whole string, so the concatenation lets
    every collector resolve its construct.
    """
    crate_path = root / TYPES_CRATE / "src"
    sources = [
        crate_path / "lib.rs",
        crate_path / "order_lifecycle.rs",
        crate_path / "order_event.rs",
    ]
    for source_path in sources:
        if not source_path.exists():
            fail(f"types crate source missing: {source_path.relative_to(root)}")
    return "\n".join(source_path.read_text(encoding="utf-8") for source_path in sources)


# --------------------------------------------------------------------------- #
# Local Rust-source helpers (the shared ones only handle `pub fn` / braced
# structs; this module uses `pub const fn`).
# --------------------------------------------------------------------------- #


def _const_fn_body(source: str, name: str) -> str:
    """Body of ``[pub] [const] fn <name>`` up to its closing brace.

    ``pub`` is optional so this resolves the private ``for_state`` mapper as well
    as the public methods.
    """
    match = re.search(rf"\b(?:pub\s+)?(?:const\s+)?fn\s+{re.escape(name)}\b[^{{]*\{{", source)
    if not match:
        fail(f"Rust source is missing function `{name}`")
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
        fail(f"could not parse function body for `{name}`")
    return source[start : index - 1]


def _impl_block(source: str, type_name: str) -> str:
    """Body of the inherent ``impl <type_name> { .. }`` block (not a trait impl)."""
    match = re.search(rf"\bimpl\s+{re.escape(type_name)}\s*\{{", source)
    if not match:
        fail(f"Rust source is missing `impl {type_name}` block")
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
        fail(f"could not parse impl block for `{type_name}`")
    return source[start : index - 1]


def _enum_body_any(source: str, name: str) -> str:
    """Body of ``[pub] enum <name> { .. }`` (the shared helper only handles `pub`;
    the variant carrier is a *private* enum)."""
    match = re.search(rf"\b(?:pub\s+)?enum\s+{re.escape(name)}\s*\{{", source)
    if not match:
        fail(f"Rust source is missing enum `{name}`")
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
        fail(f"could not parse enum body for `{name}`")
    return source[start : index - 1]


def _as_str_wire_map(impl_body: str) -> dict[str, str]:
    """Map ``<Qualifier>::<Variant> => "<WIRE>"`` arms in an ``as_str`` body.

    The qualifier is ``Self`` for ``OrderState`` and the private ``Category``
    enum for the opaque ``OrderEventCategory`` (which matches on ``self.0``).
    """
    as_str_body = _const_fn_body(impl_body, "as_str")
    return dict(re.findall(r'\w+::(\w+)\s*=>\s*"([^"]+)"', as_str_body))


def _matches_variants(impl_body: str, fn_name: str) -> set[str]:
    """The set of ``<Qualifier>::<Variant>`` tokens named in a predicate body."""
    body = _const_fn_body(impl_body, fn_name)
    return set(re.findall(r"\w+::(\w+)", body))


def _load_sdk_module(root: Path) -> object:
    """Import ``atp_strategy`` from ``root`` (supports mutation-test tmpdirs)."""
    python_root = root / "python"
    if not python_root.is_dir():
        fail(f"python/ directory missing under {root}")
    str_root = str(python_root)
    if str_root in sys.path:
        sys.path.remove(str_root)
    sys.path.insert(0, str_root)
    for name in list(sys.modules):
        if name == "atp_strategy" or name.startswith("atp_strategy."):
            sys.modules.pop(name, None)
    try:
        return importlib.import_module("atp_strategy")
    except Exception as exc:  # pragma: no cover — surfaces as an order-event fail
        fail(f"failed to import atp_strategy from {python_root}: {exc!r}")


# --------------------------------------------------------------------------- #
# Static checks
# --------------------------------------------------------------------------- #


def check_category_enum(config: dict, types_src: str, root: Path = ROOT) -> str:
    block = contract_block(config)
    enum = block["event_category_enum"]
    variant_enum = block["private_variant_enum"]
    # Variants live on the PRIVATE carrier enum (the opaque newtype wraps it).
    enum_body = _enum_body_any(types_src, variant_enum)
    variants = re.findall(r"^\s*([A-Z]\w*)\s*,", enum_body, re.MULTILINE)
    if not variants:
        fail(f"{variant_enum} declares no variants")
    wire_map = _as_str_wire_map(_impl_block(types_src, enum))
    missing = [v for v in variants if v not in wire_map]
    if missing:
        fail(f"{enum}::as_str is missing wire string(s) for variant(s): {', '.join(missing)}")
    coded_wires = sorted(wire_map[v] for v in variants)
    expected = sorted(block["all_categories"])
    if coded_wires != expected:
        fail(
            f"{enum}::as_str wire strings {coded_wires} disagree with "
            f"order_event_dispatch_contract.all_categories {expected}"
        )
    # Cross-surface parity: Python OrderEventType member values == all_categories.
    api = _load_sdk_module(root)
    py_type = getattr(api, "OrderEventType", None)
    if py_type is None:
        fail("atp_strategy does not export OrderEventType")
    py_values = sorted(member.value for member in py_type)
    if py_values != expected:
        fail(
            f"Python OrderEventType values {py_values} disagree with the Rust/JSON "
            f"category vocabulary {expected} — live/paper/SDK wire strings must match"
        )
    return (
        f"{enum} declares {len(variants)} categories; as_str wire strings == "
        f"all_categories == Python OrderEventType values ({', '.join(expected)})"
    )


def check_state_to_category(config: dict, types_src: str) -> str:
    block = contract_block(config)
    enum = block["event_category_enum"]
    documented = block["state_to_event_category"]
    # variant -> wire for OrderState and for the event category enum
    state_wire = _as_str_wire_map(_impl_block(types_src, "OrderState"))
    cat_wire = _as_str_wire_map(_impl_block(types_src, enum))
    for_state_body = _const_fn_body(_impl_block(types_src, enum), "for_state")

    coded: dict[str, str | None] = {}
    for variant, wire in state_wire.items():
        arm = re.search(rf"OrderState::{re.escape(variant)}\s*=>\s*([^,]+),", for_state_body)
        if not arm:
            fail(f"{enum}::for_state has no `OrderState::{variant} => ..` arm (totality gap)")
        value = arm.group(1).strip()
        if value == "None":
            coded[wire] = None
            continue
        cat = re.search(r"Some\(\s*Self\(\s*\w+::(\w+)\s*\)\s*\)", value)
        if not cat:
            fail(
                f"{enum}::for_state arm for OrderState::{variant} is neither None nor "
                "Some(Self(Category::..))"
            )
        cat_variant = cat.group(1)
        if cat_variant not in cat_wire:
            fail(
                f"{enum}::for_state maps OrderState::{variant} to unknown category Self::{cat_variant}"
            )
        coded[wire] = cat_wire[cat_variant]

    if coded != documented:
        fail(
            f"{enum}::for_state {coded} disagrees with "
            f"order_event_dispatch_contract.state_to_event_category {documented}"
        )
    callback_states = sorted(s for s, c in documented.items() if c is not None)
    return (
        f"{enum}::for_state maps all {len(documented)} OrderState wire strings to "
        f"state_to_event_category arm-for-arm ({len(callback_states)} callback-bearing "
        f"states: {', '.join(callback_states)}; 3 internal states -> no callback)"
    )


def check_for_transition_fail_closed(config: dict, types_src: str) -> str:
    block = contract_block(config)
    enum = block["event_category_enum"]
    body = _const_fn_body(_impl_block(types_src, enum), "for_transition")
    if "can_transition_to" not in body:
        fail(
            f"{enum}::for_transition must consult OrderState::can_transition_to (the "
            "SRS-EXE-008 documented graph) before deriving an event"
        )
    if "IllegalTransition" not in body:
        fail(
            f"{enum}::for_transition must return OrderLifecycleError::IllegalTransition for an "
            "edge not in the graph — fail-closed (no event for an impossible transition)"
        )
    return (
        f"{enum}::for_transition is fail-closed: it gates on OrderState::can_transition_to and "
        "returns IllegalTransition for an illegal edge (no callback for an impossible transition)"
    )


def check_no_public_bypass(config: dict, types_src: str) -> str:
    block = contract_block(config)
    enum = block["event_category_enum"]
    mapper = block["private_state_mapper"]
    deriver = block["crate_internal_deriver"]
    entry = block["public_derivation_entry_point"]
    entry_type = block["entry_point_type"]

    variant_enum = block["private_variant_enum"]
    event_struct = block["event_struct"]

    # 1. The category must be an OPAQUE newtype over a PRIVATE variant enum, so a
    #    dispatcher in another crate cannot construct one. (A bare
    #    #[non_exhaustive] enum does NOT suffice — its unit variants stay
    #    nameable for construction.)
    struct_decl = re.search(rf"\bpub\s+struct\s+{re.escape(enum)}\s*\(([^)]*)\)", types_src)
    if not struct_decl:
        fail(
            f"{enum} must be an opaque newtype `pub struct {enum}(<private variant enum>)` so it "
            "cannot be constructed outside this crate"
        )
    if re.search(r"\bpub\b", struct_decl.group(1)):
        fail(f"{enum}'s wrapped field must be PRIVATE (no `pub`) so the category cannot be forged")
    if variant_enum not in struct_decl.group(1):
        fail(f"{enum} must wrap the private variant enum {variant_enum}")
    if re.search(rf"\bpub\s+enum\s+{re.escape(variant_enum)}\b", types_src):
        fail(
            f"the variant carrier {variant_enum} must be PRIVATE (not `pub enum`) — a public "
            f"carrier lets a dispatcher construct {enum} directly"
        )
    _enum_body_any(types_src, variant_enum)  # exists (private) or fails

    # 2. The destination-state mapper must NOT be public — a public mapper lets a
    #    caller emit a callback for an impossible / terminal-state transition.
    if re.search(rf"\bpub\s*(?:\([^)]*\))?\s+(?:const\s+)?fn\s+{re.escape(mapper)}\b", types_src):
        fail(
            f"{enum}::{mapper} must be PRIVATE — a public destination-state mapper is a "
            f"fail-closed bypass. The only public entry point is {entry_type}::{entry}."
        )
    _const_fn_body(_impl_block(types_src, enum), mapper)  # exists (private) or fails

    # 3. The state-pair deriver must be crate-internal (pub(crate)), not public —
    #    a public free function over caller-supplied state pairs lets a dispatcher
    #    fabricate a callback for an order not actually in `from`.
    if re.search(rf"\bpub\s+(?:const\s+)?fn\s+{re.escape(deriver)}\b", types_src):
        fail(
            f"{enum}::{deriver} must be crate-internal (pub(crate)), not public — a public "
            "state-pair deriver lets a dispatcher fabricate a callback from arbitrary states"
        )
    if not re.search(rf"\bpub\(crate\)\s+(?:const\s+)?fn\s+{re.escape(deriver)}\b", types_src):
        fail(f"{enum}::{deriver} must exist as a pub(crate) deriver")

    # 4. The only public way to obtain a callback is the order-bound entry point
    #    on the ledger, which binds the `from` state to the TRACKED order and
    #    returns events only on a successful mutation. It must return ALL events
    #    the transition produces (a Vec), including the cascaded auto-rejection of
    #    a held cancel-replace replacement — that order is terminal afterward, so
    #    its callback could never be re-derived; dropping it would lose a callback.
    entry_sig = re.search(
        rf"\bpub\s+fn\s+{re.escape(entry)}\s*\([^{{]*?->\s*Result<\s*Vec<\s*{re.escape(event_struct)}",
        types_src,
        re.DOTALL,
    )
    if not entry_sig:
        fail(
            f"{entry_type}::{entry} must be `pub fn` returning Result<Vec<{event_struct}>, ..> "
            "(all events a transition produces, so a cascaded rejection is never lost)"
        )
    entry_body = _const_fn_body(_impl_block(types_src, entry_type), entry)
    if enum not in entry_body or deriver not in entry_body:
        fail(
            f"{entry_type}::{entry} must derive the callback via {enum}::{deriver} "
            "(bound to the tracked order), not return a caller-supplied category"
        )
    if "self.orders" not in entry_body:
        fail(
            f"{entry_type}::{entry} must read the tracked order's real state from the ledger "
            "(self.orders), so the `from` state is never a caller argument"
        )
    # Cascade aggregation: the body must look at held replacements (`replaces`)
    # and emit the auto-suppressed REJECTED event.
    if "replaces" not in entry_body or "Rejected" not in entry_body:
        fail(
            f"{entry_type}::{entry} must surface the cascaded auto-rejection of a held "
            "cancel-replace replacement (it inspects `replaces` and emits the REJECTED event), "
            "so a strategy never silently loses that callback"
        )
    return (
        f"{enum} is an opaque newtype over the private {variant_enum} (no foreign construction), "
        f"{mapper} is private, {deriver} is pub(crate), and the sole public entry point "
        f"{entry_type}::{entry} returns Vec<{event_struct}> bound to real mutations — including "
        "the cascaded replacement rejection — so no callback can be fabricated or silently lost"
    )


def check_field_requirements(config: dict, types_src: str) -> str:
    block = contract_block(config)
    enum = block["event_category_enum"]
    impl = _impl_block(types_src, enum)
    cat_wire = _as_str_wire_map(impl)

    ac_named_wires = sorted(cat_wire[v] for v in _matches_variants(impl, "is_ac_named"))
    expected_ac = sorted(block["ac_named_categories"])
    if ac_named_wires != expected_ac:
        fail(
            f"{enum}::is_ac_named covers {ac_named_wires} but ac_named_categories is {expected_ac}"
        )
    expected_fill = sorted(block["categories_requiring_fill_economics"])
    if expected_fill != expected_ac:
        fail(
            "order_event_dispatch_contract.categories_requiring_fill_economics must equal "
            f"ac_named_categories ({expected_ac}); got {expected_fill}"
        )
    fill_body = _const_fn_body(impl, "requires_fill_economics")
    if "is_ac_named" not in fill_body:
        fail(
            f"{enum}::requires_fill_economics must delegate to is_ac_named so the fill-economics "
            "requirement cannot drift from the AC-named set"
        )

    reason_wires = sorted(cat_wire[v] for v in _matches_variants(impl, "requires_reason"))
    expected_reason = sorted(block["categories_requiring_reason"])
    if reason_wires != expected_reason:
        fail(
            f"{enum}::requires_reason covers {reason_wires} but categories_requiring_reason is "
            f"{expected_reason}"
        )
    return (
        f"{enum} field-presence predicates match the SDK contract: requires_fill_economics == "
        f"is_ac_named == {expected_ac}; requires_reason == {expected_reason}"
    )


def check_ac_named_single_source(config: dict) -> str:
    block = contract_block(config)
    source = latency_source_block(config)
    ours = sorted(block["ac_named_categories"])
    canonical = sorted(source["ac_named_callback_categories"])
    if ours != canonical:
        fail(
            f"order_event_dispatch_contract.ac_named_categories {ours} disagrees with the "
            f"canonical strategy_api_order_events_contract.ac_named_callback_categories {canonical}"
        )
    return (
        "ac_named_categories == strategy_api_order_events_contract.ac_named_callback_categories "
        f"({', '.join(ours)}) — one source of truth for the AC-named callback set"
    )


def check_latency_parity(config: dict, types_src: str, root: Path = ROOT) -> str:
    block = contract_block(config)
    source = latency_source_block(config)
    api = _load_sdk_module(root)
    summary = []
    for const_name, source_key in block["latency_consts"].items():
        # Rust const value
        match = re.search(
            rf"\bpub\s+const\s+{re.escape(const_name)}\s*:\s*u32\s*=\s*(\d+)\s*;", types_src
        )
        if not match:
            fail(f"order_event.rs is missing `pub const {const_name}: u32 = ..;`")
        rust_value = int(match.group(1))
        # canonical JSON value
        if source_key not in source:
            fail(f"latency source block is missing key `{source_key}`")
        json_value = source[source_key]
        # Python SDK value
        py_value = getattr(api, const_name, None)
        if py_value is None:
            fail(f"atp_strategy does not export {const_name}")
        if not (rust_value == json_value == py_value):
            fail(
                f"{const_name} disagrees across surfaces: Rust={rust_value}, "
                f"JSON({source_key})={json_value}, Python={py_value} — NFR-P4 budget must "
                "live in exactly one place"
            )
        summary.append(f"{const_name}={rust_value}")
    return (
        f"NFR-P4 budgets are one source of truth (Rust == strategy_api_order_events_contract == "
        f"Python SDK): {', '.join(summary)} ms"
    )


def check_cargo_test_smoke(config: dict) -> str:
    block = contract_block(config)
    crate = block["types_crate"]["crate"]
    integration = block["integration_test"]
    cargo = shutil.which("cargo")
    if cargo is None:
        return f"cargo test -p {crate}: skipped (cargo not on PATH)"
    lib = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", "order_event", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if lib.returncode != 0:
        fail(f"cargo test -p {crate} --lib order_event failed:\n{lib.stdout}\n{lib.stderr}")
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
        f"cargo test -p {crate} --lib order_event + --test {integration}: PASS "
        "(category derivation + fail-closed + live/paper parity verified)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


def _run_static(config: dict, types_src: str, root: Path = ROOT) -> list[str]:
    return [
        check_category_enum(config, types_src, root),
        check_state_to_category(config, types_src),
        check_for_transition_fail_closed(config, types_src),
        check_no_public_bypass(config, types_src),
        check_field_requirements(config, types_src),
        check_ac_named_single_source(config),
        check_latency_parity(config, types_src, root),
    ]


def run_checks() -> list[str]:
    config = load_config()
    types_src = types_source()
    evidence = _run_static(config, types_src, ROOT)
    evidence.append(check_cargo_test_smoke(config))
    return evidence


def assert_order_event_dispatch_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    types_src = types_source(root)
    return _run_static(config, types_src, root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-SDK-004 SDK-surface contract evidence")
    parser.parse_args(argv)

    try:
        config = load_config()
        evidence = run_checks()
    except OrderEventDispatchCheckError as error:
        print(f"SRS-SDK-004 FAIL: {error}", file=sys.stderr)
        return 1

    # Scope honestly: this is the SDK-surface / pure-logic half (the source-neutral
    # category authority + per-category field-presence requirement). It does NOT
    # deliver callbacks to Python, populate the AC field values, or prove the NFR-P4
    # p95 latency budgets — all deferred to the runtime dispatchers. This is NOT a
    # full SRS-SDK-004 requirement pass.
    print("SRS-SDK-004 SDK-SURFACE PASS (contract evidence only; not a full requirement pass)")
    for item in evidence:
        print(f"- {item}")
    print("Deferred end-to-end evidence (SRS-SDK-004 stays passes:false until these land):")
    for owner in contract_block(config).get("deferred", []):
        print(f"  * {owner}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
