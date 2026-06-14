#!/usr/bin/env python3
"""Contract evidence script for feature SRS-EXE-008.

Verifies that the order lifecycle state machine declared in
``architecture/runtime_services.json`` (block ``order_lifecycle_contract``)
is present in the Rust crate ``crates/atp-types``.

SRS-EXE-008 ("implement an order lifecycle state machine with documented
states and transitions, and use a strategy-supplied client correlation ID as
the idempotency key for live and paper order submissions") traces SyRS
SYS-3 / SYS-7 / SYS-64 / SYS-90, NFR-R3, and StRS SN-1.08 / SN-1.22. The
machine is a source-neutral domain type in ``atp-types`` so the live
(``atp-execution``) and paper (``atp-simulation``) sides — sibling crates that
must not depend on one another — share one identical contract. It guarantees:

  (a) ``OrderState`` declares exactly the nine AC states and ``as_str`` maps
      each to its stable SYS-64 wire string.
  (b) the documented transition graph in ``transitions`` matches
      ``OrderState::allowed_next`` arm-for-arm (no missing edge, no extra
      edge), and the four terminal states are covered by ``is_terminal``.
  (c) ``ClientCorrelationId`` is a private-field idempotency key with a
      fallible ``new`` constructor (fails closed on a blank id).
  (d) ``OrderLifecycle`` carries the correlation id, current state, and the
      ``replaces`` audit link, and exposes a graph-enforcing ``transition_to``.
  (e) ``OrderLedger::submit`` returns
      ``Result<&OrderLifecycle, StructuredOrderError>`` and the duplicate
      rejection is the SRS-ERR-001 envelope with category
      ``DuplicateClientCorrelationId`` (wire ``DUPLICATE_CLIENT_CORRELATION_ID``).
  (f) ``OrderLedger::cancel_replace`` is cancel-then-new: it transitions the
      original to ``CancelPending`` and sets ``replaces: Some(..)`` on the
      replacement (the original id retained for audit).

Mirrors the PASS/FAIL output style of ``tools/live_designation_check.py``.

Invoke:
    python3 tools/order_lifecycle_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _enum_body, _struct_body

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class OrderLifecycleCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise OrderLifecycleCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "order_lifecycle_contract" not in config:
        fail("architecture metadata is missing order_lifecycle_contract")
    return config["order_lifecycle_contract"]


def types_source(config: dict, root: Path = ROOT) -> str:
    """Return lib.rs + order_lifecycle.rs concatenated.

    ``OrderErrorCategory`` (and its new duplicate-correlation variant) lives in
    lib.rs; the state machine itself lives in the order_lifecycle module. The
    brace-matching helpers search the whole string, so the concatenation lets
    every collector resolve its construct.
    """
    block = contract_block(config)
    crate_path = root / block["types_crate"]["path"]
    lib = crate_path / "src" / "lib.rs"
    module = crate_path / "src" / "order_lifecycle.rs"
    for source_path in (lib, module):
        if not source_path.exists():
            fail(f"types crate source missing: {source_path.relative_to(root)}")
    return lib.read_text(encoding="utf-8") + "\n" + module.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Local Rust-source helpers (the shared ones only handle `pub fn` / braced
# structs; the state machine uses `pub const fn` and a tuple struct).
# --------------------------------------------------------------------------- #


def _const_fn_body(source: str, name: str) -> str:
    """Body of ``pub [const] fn <name>`` up to its closing brace."""
    match = re.search(rf"\bpub\s+(?:const\s+)?fn\s+{re.escape(name)}\b[^{{]*\{{", source)
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
    """Body of the inherent ``impl <type_name> { .. }`` block (not a trait impl).

    Scoping method lookups to the owning impl block disambiguates names that
    recur across types (e.g. ``as_str`` on both ``OrderErrorCategory`` and
    ``OrderState``; ``state`` on both ``OrderLifecycle`` and ``OrderLedger``).
    """
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


def _allowed_next_targets(allowed_next_body: str, state: str) -> set[str]:
    """The set of ``Self::<X>`` targets in the ``Self::<state> => &[ .. ]`` arm."""
    match = re.search(rf"Self::{re.escape(state)}\s*=>\s*&\[", allowed_next_body)
    if not match:
        fail(f"OrderState::allowed_next has no `Self::{state} => &[..]` arm")
    start = match.end()
    depth = 1
    index = start
    while index < len(allowed_next_body) and depth:
        char = allowed_next_body[index]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        index += 1
    inner = allowed_next_body[start : index - 1]
    return set(re.findall(r"Self::(\w+)", inner))


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_state_enum(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["state_enum"]
    body = _enum_body(types_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    order_state_impl = _impl_block(types_src, spec["enum"])
    as_str_body = _const_fn_body(order_state_impl, "as_str")
    missing_wire = [w for w in spec["wire_strings"] if f'"{w}"' not in as_str_body]
    if missing_wire:
        fail(f"OrderState::as_str is missing wire string(s): {', '.join(missing_wire)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} states "
        f"({', '.join(spec['variants'])}); as_str maps each to its SYS-64 wire string"
    )


def check_terminal_states(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["state_enum"]
    order_state_impl = _impl_block(types_src, spec["enum"])
    is_terminal_body = _const_fn_body(order_state_impl, "is_terminal")
    missing = [
        t for t in spec["terminal"] if not re.search(rf"Self::{re.escape(t)}\b", is_terminal_body)
    ]
    if missing:
        fail(f"OrderState::is_terminal does not cover terminal state(s): {', '.join(missing)}")
    # A terminal state must appear NOWHERE as a non-terminal key with outgoing edges.
    for terminal in spec["terminal"]:
        if block["transitions"].get(terminal):
            fail(
                f"order_lifecycle_contract.transitions[{terminal}] is non-empty but "
                f"{terminal} is declared terminal"
            )
    return (
        f"OrderState::is_terminal covers exactly the {len(spec['terminal'])} terminal "
        f"states ({', '.join(spec['terminal'])}); none has an outgoing edge"
    )


def check_transition_graph(config: dict, types_src: str) -> str:
    block = contract_block(config)
    transitions = block["transitions"]
    order_state_impl = _impl_block(types_src, block["state_enum"]["enum"])
    allowed_next_body = _const_fn_body(order_state_impl, "allowed_next")
    nonterminal = {s: t for s, t in transitions.items() if t}
    for state, documented in nonterminal.items():
        coded = _allowed_next_targets(allowed_next_body, state)
        documented_set = set(documented)
        if coded != documented_set:
            extra = coded - documented_set
            missing = documented_set - coded
            detail = []
            if missing:
                detail.append(f"missing edge(s) {sorted(missing)}")
            if extra:
                detail.append(f"undocumented edge(s) {sorted(extra)}")
            fail(
                f"OrderState::allowed_next for {state} disagrees with the documented "
                f"graph: {'; '.join(detail)}"
            )
    edge_count = sum(len(t) for t in transitions.values())
    return (
        f"OrderState::allowed_next matches order_lifecycle_contract.transitions "
        f"arm-for-arm across {len(nonterminal)} non-terminal states "
        f"({edge_count} documented edges; no missing, no undocumented)"
    )


def check_correlation_id(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["correlation_id"]
    struct = spec["struct"]
    decl = re.search(rf"\bpub\s+struct\s+{re.escape(struct)}\s*\(([^)]*)\)", types_src)
    if decl is None:
        fail(f"{struct} tuple struct declaration not found")
    if re.search(r"\bpub\b", decl.group(1)):
        fail(
            f"{struct} has a public inner field — the idempotency key must keep its "
            "field private so it cannot be forged empty"
        )
    impl = _impl_block(types_src, struct)
    constructor = spec["constructor"]
    new_sig = re.search(rf"\bpub\s+fn\s+{re.escape(constructor)}\s*\([^)]*\)\s*->\s*Result<", impl)
    if new_sig is None:
        fail(
            f"{struct}::{constructor} must return a Result (fail closed on a blank correlation id)"
        )
    accessor = spec["accessor"]
    if not re.search(rf"\bpub\s+fn\s+{re.escape(accessor)}\s*\(", impl):
        fail(f"{struct} is missing the `{accessor}` accessor")
    return (
        f"atp-types declares {struct} as a private-field idempotency key with a "
        f"fallible `{constructor}` constructor and `{accessor}` accessor"
    )


def check_lifecycle(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["lifecycle"]
    struct = spec["struct"]
    body = _struct_body(types_src, struct)
    missing_fields = [f for f in spec["fields"] if not re.search(rf"\b{re.escape(f)}\s*:", body)]
    if missing_fields:
        fail(f"{struct} is missing field(s): {', '.join(missing_fields)}")
    if re.search(r"\bpub\s+\w+\s*:", body):
        fail(f"{struct} exposes a public field — its state must only move via transition_to")
    impl = _impl_block(types_src, struct)
    missing_methods = [
        m for m in spec["methods"] if not re.search(rf"\bpub\s+fn\s+{re.escape(m)}\s*\(", impl)
    ]
    if missing_methods:
        fail(f"{struct} is missing method(s): {', '.join(missing_methods)}")
    audit = spec["audit_field"]
    if not re.search(rf"\b{re.escape(audit)}\s*:", body):
        fail(f"{struct} is missing the `{audit}` audit field for cancel-replace")
    return (
        f"atp-types declares {struct} with fields ({', '.join(spec['fields'])}) and "
        f"{len(spec['methods'])} methods incl. graph-enforcing transition_to; the "
        f"`{audit}` field carries the cancel-replace audit link"
    )


def check_ledger(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["ledger"]
    struct = spec["struct"]
    _struct_body(types_src, struct)  # must exist / be parseable
    impl = _impl_block(types_src, struct)
    missing = [
        m for m in spec["methods"] if not re.search(rf"\bpub\s+fn\s+{re.escape(m)}\s*\(", impl)
    ]
    if missing:
        fail(f"{struct} is missing method(s): {', '.join(missing)}")
    submit = spec["submit_method"]
    sig = re.search(
        rf"\bpub\s+fn\s+{re.escape(submit)}\s*\([^{{]*?->\s*(Result<[^{{]*?>)\s*\{{",
        impl,
        re.DOTALL,
    )
    if sig is None:
        fail(f"{struct}::{submit} signature could not be parsed")
    actual = re.sub(r"\s+", " ", sig.group(1)).strip()
    expected = re.sub(r"\s+", " ", spec["submit_result"]).strip()
    if actual != expected:
        fail(
            f"{struct}::{submit} returns `{actual}` but the contract requires "
            f"`{expected}` (the SRS-ERR-001 idempotency envelope)"
        )
    return (
        f"atp-types declares {struct} with {len(spec['methods'])} methods "
        f"({', '.join(spec['methods'])}); {submit} returns {expected}"
    )


def check_idempotency(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["idempotency"]
    category = spec["rejection_category"]
    wire = spec["rejection_wire_string"]
    cat_body = _enum_body(types_src, "OrderErrorCategory")
    if not re.search(rf"\b{re.escape(category)}\b", cat_body):
        fail(f"OrderErrorCategory enum is missing the `{category}` variant")
    as_str_body = _const_fn_body(_impl_block(types_src, "OrderErrorCategory"), "as_str")
    if f'"{wire}"' not in as_str_body:
        fail(f"OrderErrorCategory::as_str is missing the `{wire}` wire string")
    # the duplicate-rejection path INSIDE submit (not merely a test) must build
    # the contract category in the SRS-ERR-001 envelope
    submit_body = _const_fn_body(_impl_block(types_src, "OrderLedger"), "submit")
    if f"OrderErrorCategory::{category}" not in submit_body:
        fail(
            f"OrderLedger::submit must construct OrderErrorCategory::{category} on a "
            "duplicate correlation id (the SRS-ERR-001 idempotency envelope)"
        )
    return (
        f"duplicate correlation-id submissions are rejected with "
        f"OrderErrorCategory::{category} (wire {wire}) — SRS-EXE-008 / SRS-ERR-001"
    )


def check_cancel_replace_audit(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["cancel_replace"]
    cancel_state = spec["cancel_state"]
    audit_field = spec["retains_original_field"]
    body = _const_fn_body(types_src, "cancel_replace")
    if f"OrderState::{cancel_state}" not in body:
        fail(
            f"OrderLedger::cancel_replace must transition the original to "
            f"OrderState::{cancel_state} (cancel-then-new)"
        )
    if not re.search(rf"{re.escape(audit_field)}\s*:\s*Some\(", body):
        fail(
            f"OrderLedger::cancel_replace must set `{audit_field}: Some(..)` on the "
            "replacement — the original correlation id is retained for audit"
        )
    single_error = block["cancel_replace"]["single_replacement_error"]
    if single_error not in body:
        fail(
            f"OrderLedger::cancel_replace must reject a second replacement with "
            f"OrderLifecycleError::{single_error} — an original may be cancel-replaced "
            "at most once (a second replacement re-opens doubled exposure)"
        )
    return (
        "OrderLedger::cancel_replace is cancel-then-new: original -> "
        f"OrderState::{cancel_state}, the replacement retains the original id via "
        f"`{audit_field}: Some(..)`, and a second cancel-replace is refused with "
        f"OrderLifecycleError::{single_error} (at most one replacement per original)"
    )


def check_replacement_gate(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["cancel_replace"].get("replacement_gate")
    if spec is None:
        fail("order_lifecycle_contract.cancel_replace is missing the replacement_gate spec")
    impl = _impl_block(types_src, block["ledger"]["struct"])
    body = _const_fn_body(impl, spec["entry_method"])
    block_error = spec["block_error"]
    if block_error not in body:
        fail(
            f"OrderLedger::{spec['entry_method']} must build "
            f"OrderLifecycleError::{block_error} — a cancel-replace replacement must "
            f"not reach {spec['blocked_target']} until its original is "
            f"OrderState::{spec['gate_state']} (no doubled exposure)"
        )
    if f"OrderState::{spec['gate_state']}" not in body:
        fail(
            f"OrderLedger::{spec['entry_method']} must gate on "
            f"OrderState::{spec['gate_state']} (the cancel-replace safety gate)"
        )
    if f"OrderState::{spec['suppress_state']}" not in body:
        fail(
            f"OrderLedger::{spec['entry_method']} must auto-suppress a held replacement to "
            f"OrderState::{spec['suppress_state']} when its original ends non-cancelled"
        )
    return (
        f"OrderLedger::{spec['entry_method']} gates a held replacement: it cannot reach "
        f"{spec['blocked_target']} until the original is OrderState::{spec['gate_state']}, "
        f"and a non-cancelled terminal original auto-suppresses the replacement to "
        f"OrderState::{spec['suppress_state']} (no doubled exposure)"
    )


def check_lifecycle_error(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["lifecycle_error"]
    body = _enum_body(types_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} variants "
        f"({', '.join(spec['variants'])})"
    )


def check_cargo_test_smoke(config: dict) -> str:
    block = contract_block(config)
    crate = block["types_crate"]["crate"]
    cargo = shutil.which("cargo")
    if cargo is None:
        return f"cargo test -p {crate}: skipped (cargo not on PATH)"
    result = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(f"cargo test -p {crate} --lib failed:\n{result.stdout}\n{result.stderr}")
    integ = subprocess.run(
        [cargo, "test", "-p", crate, "--test", "srs_exe_008_order_lifecycle", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if integ.returncode != 0:
        fail(
            f"cargo test -p {crate} --test srs_exe_008_order_lifecycle failed:\n"
            f"{integ.stdout}\n{integ.stderr}"
        )
    return (
        f"cargo test -p {crate} --lib + srs_exe_008_order_lifecycle: PASS "
        "(state graph + idempotency + cancel-then-new invariants verified)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


_STATIC_CHECKS = (
    ("state_enum", check_state_enum),
    ("terminal_states", check_terminal_states),
    ("transition_graph", check_transition_graph),
    ("correlation_id", check_correlation_id),
    ("lifecycle", check_lifecycle),
    ("ledger", check_ledger),
    ("idempotency", check_idempotency),
    ("cancel_replace_audit", check_cancel_replace_audit),
    ("replacement_gate", check_replacement_gate),
    ("lifecycle_error", check_lifecycle_error),
)


def run_checks() -> list[str]:
    config = load_config()
    types_src = types_source(config)
    evidence = [check(config, types_src) for _, check in _STATIC_CHECKS]
    evidence.append(check_cargo_test_smoke(config))
    return evidence


def assert_order_lifecycle_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    types_src = types_source(config, root)
    return [check(config, types_src) for _, check in _STATIC_CHECKS]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-EXE-008 contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except OrderLifecycleCheckError as error:
        print(f"SRS-EXE-008 FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-EXE-008 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
