#!/usr/bin/env python3
"""Contract evidence script for feature SRS-EXE-003 (order types).

Verifies the **source-neutral order-type authority** for SRS-EXE-003 ("support
market, limit, stop, and stop-limit orders for equities and options in live and
paper modes"; SyRS SYS-3 / SYS-82; StRS SN-1.08 / BG-1): the order-type
vocabulary + price-validation authority declared in
``crates/atp-types/src/order_type.rs`` (plus the crate-root ``AssetClass``)
matches the machine-readable mirror in
``architecture/runtime_services.json`` (block ``order_type_contract``), is in
lock-step with the Python SDK surface (``atp_strategy.api``), and is the SINGLE
shared definition the paper path (atp-simulation, SRS-SIM-001) consumes via
re-export today; the live path (atp-execution, SRS-EXE-001/006) will consume the
same definition when its order intake lands (deferred -- so the AC is not yet
met and SRS-EXE-003 stays passes:false).

This check guarantees:

  (a) ``OrderType`` declares the four order types and ``as_str`` maps each to its
      stable wire string; the wire set equals ``order_types[].wire`` and the
      Python ``OrderType`` member values one-for-one.
  (b) ``OrderSide`` and the crate-root ``AssetClass`` wire strings equal the
      ``sides`` / ``asset_classes`` mirror and the Python ``OrderSide`` /
      ``AssetClass`` member values.
  (c) the price-requirement matrix (``requires_limit_price`` /
      ``requires_stop_price``) matches ``order_types[]`` arm-for-arm, over all
      four types (totality; no missing, no undocumented).
  (d) ``OrderType::validate_prices`` is fail-closed: it rejects a non-positive
      limit/stop price with the documented ``OrderTypeError`` variants.
  (e) ``atp-simulation``'s ``paper_order`` RE-EXPORTS this authority (it does NOT
      redefine ``OrderType`` / ``Side`` / ``AssetClass``), so the order-type model
      is a SINGLE shared definition — the paper path consumes it via re-export;
      the live path will consume the same definition (deferred). This is the
      prerequisite for "identical for live and paper", not a claim it already is.
  (f) ``paper_order::validate_leg`` enforces the SAME price-positivity rule as
      ``OrderType::validate_prices`` (so the paper intake cannot drift from the
      shared authority while the call-through is deferred).

This is the SDK-surface / pure-logic half only. The accept->ack round-trip
through the live IB adapter and the internal simulation engine, order
state-tracking, the orchestrator routing of non-live orders, option contract
identity, and live multi-leg composites are owned by other features (see
``order_type_contract.deferred``); SRS-EXE-003 stays ``passes: false`` in
``feature_list.json`` until those land.

Mirrors the PASS/FAIL output style of ``tools/order_event_dispatch_check.py``.

Invoke:
    python3 tools/order_type_check.py
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
PAPER_ORDER_PATH = "crates/atp-simulation/src/paper_order.rs"
FILL_MODEL_PATH = "crates/atp-simulation/src/fill_model.rs"


class OrderTypeCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise OrderTypeCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "order_type_contract" not in config:
        fail("architecture metadata is missing order_type_contract")
    return config["order_type_contract"]


def types_source(root: Path = ROOT) -> str:
    """Return lib.rs + order_type.rs concatenated.

    ``OrderType`` / ``OrderSide`` / ``OrderTypeError`` live in order_type.rs; the
    crate-root ``AssetClass`` lives in lib.rs. The brace-matching helpers search
    the whole string, so the concatenation lets every collector resolve its
    construct.
    """
    crate_path = root / TYPES_CRATE / "src"
    sources = [crate_path / "lib.rs", crate_path / "order_type.rs"]
    for source_path in sources:
        if not source_path.exists():
            fail(f"types crate source missing: {source_path.relative_to(root)}")
    return "\n".join(source_path.read_text(encoding="utf-8") for source_path in sources)


def paper_order_source(root: Path = ROOT) -> str:
    path = root / PAPER_ORDER_PATH
    if not path.exists():
        fail(f"paper order source missing: {PAPER_ORDER_PATH}")
    return path.read_text(encoding="utf-8")


def fill_model_source(root: Path = ROOT) -> str:
    path = root / FILL_MODEL_PATH
    if not path.exists():
        fail(f"fill model source missing: {FILL_MODEL_PATH}")
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Rust-source helpers
# --------------------------------------------------------------------------- #


def _fn_body(source: str, name: str) -> str:
    """Body of ``[pub] [const] fn <name>`` up to its matching closing brace."""
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
    """Body of the inherent ``impl <type_name> { .. }`` block."""
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


def _wire_map(impl_body: str) -> dict[str, str]:
    """Map ``Self::<Variant> [ {..} | (..) ] => "<WIRE>"`` arms in an ``as_str``
    body. Handles unit variants (``OrderSide`` / ``AssetClass``) AND data-carrying
    variants (``OrderType::Limit { .. }``)."""
    as_str_body = _fn_body(impl_body, "as_str")
    pairs = re.findall(r'Self::(\w+)\s*(?:\{[^}]*\}|\([^)]*\))?\s*=>\s*"([^"]+)"', as_str_body)
    if not pairs:
        fail('could not parse any `Self::Variant => "WIRE"` arms from an as_str body')
    return dict(pairs)


def _matches_variants(impl_body: str, fn_name: str) -> set[str]:
    """The set of ``Self::<Variant>`` tokens named in a predicate body."""
    body = _fn_body(impl_body, fn_name)
    return set(re.findall(r"Self::(\w+)", body))


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
    except Exception as exc:  # pragma: no cover — surfaces as an order-type fail
        fail(f"failed to import atp_strategy from {python_root}: {exc!r}")


def _py_enum_values(api: object, symbol: str) -> list[str]:
    enum = getattr(api, symbol, None)
    if enum is None:
        fail(f"atp_strategy does not export {symbol}")
    return sorted(member.value for member in enum)


# --------------------------------------------------------------------------- #
# Static checks
# --------------------------------------------------------------------------- #


def check_order_type_enum(config: dict, types_src: str, root: Path = ROOT) -> str:
    block = contract_block(config)
    enum = block["order_type_enum"]
    wire_map = _wire_map(_impl_block(types_src, enum))
    coded_wires = sorted(wire_map.values())
    expected = sorted(o["wire"] for o in block["order_types"])
    if coded_wires != expected:
        fail(
            f"{enum}::as_str wire strings {coded_wires} disagree with "
            f"order_type_contract.order_types[].wire {expected}"
        )
    # Variant names must match the contract too (totality of the four types).
    coded_names = sorted(wire_map.keys())
    expected_names = sorted(o["name"] for o in block["order_types"])
    if coded_names != expected_names:
        fail(
            f"{enum} variants {coded_names} disagree with order_type_contract.order_types "
            f"names {expected_names}"
        )
    api = _load_sdk_module(root)
    py_values = _py_enum_values(api, block["python_parity"]["order_type_symbol"])
    if py_values != expected:
        fail(
            f"Python OrderType values {py_values} disagree with the Rust/JSON order-type "
            f"vocabulary {expected} — live/paper/SDK wire strings must match"
        )
    return (
        f"{enum} declares {len(wire_map)} order types; as_str wire strings == "
        f"order_types[].wire == Python OrderType values ({', '.join(expected)})"
    )


def check_order_side_enum(config: dict, types_src: str, root: Path = ROOT) -> str:
    block = contract_block(config)
    enum = block["order_side_enum"]
    coded = sorted(_wire_map(_impl_block(types_src, enum)).values())
    expected = sorted(block["sides"])
    if coded != expected:
        fail(
            f"{enum}::as_str wire strings {coded} disagree with order_type_contract.sides {expected}"
        )
    api = _load_sdk_module(root)
    py_values = _py_enum_values(api, block["python_parity"]["order_side_symbol"])
    if py_values != expected:
        fail(f"Python OrderSide values {py_values} disagree with the side vocabulary {expected}")
    return f"{enum}::as_str == sides == Python OrderSide values ({', '.join(expected)})"


def check_asset_class(config: dict, types_src: str, root: Path = ROOT) -> str:
    block = contract_block(config)
    enum = block["asset_class_enum"]
    coded = sorted(_wire_map(_impl_block(types_src, enum)).values())
    expected = sorted(block["asset_classes"])
    if coded != expected:
        fail(
            f"{enum}::as_str wire strings {coded} disagree with "
            f"order_type_contract.asset_classes {expected}"
        )
    api = _load_sdk_module(root)
    py_values = _py_enum_values(api, block["python_parity"]["asset_class_symbol"])
    if py_values != expected:
        fail(
            f"Python AssetClass values {py_values} disagree with the asset-class vocabulary {expected}"
        )
    return f"{enum}::as_str == asset_classes == Python AssetClass values ({', '.join(expected)})"


def check_price_matrix(config: dict, types_src: str) -> str:
    block = contract_block(config)
    enum = block["order_type_enum"]
    impl = _impl_block(types_src, enum)
    limit_variants = _matches_variants(impl, "requires_limit_price")
    stop_variants = _matches_variants(impl, "requires_stop_price")
    for entry in block["order_types"]:
        name = entry["name"]
        coded_limit = name in limit_variants
        coded_stop = name in stop_variants
        if coded_limit != entry["requires_limit_price"]:
            fail(
                f"{enum}::requires_limit_price for {name} is {coded_limit} but order_type_contract "
                f"says {entry['requires_limit_price']}"
            )
        if coded_stop != entry["requires_stop_price"]:
            fail(
                f"{enum}::requires_stop_price for {name} is {coded_stop} but order_type_contract "
                f"says {entry['requires_stop_price']}"
            )
    # Totality: the matrix predicates may not name any variant outside the contract.
    contract_names = {o["name"] for o in block["order_types"]}
    stray = (limit_variants | stop_variants) - contract_names
    if stray:
        fail(f"{enum} price-matrix predicates name undocumented variant(s): {sorted(stray)}")
    return (
        f"{enum} price-requirement matrix matches order_types[] arm-for-arm over all "
        f"{len(contract_names)} types (limit-bearing: {sorted(limit_variants)}; "
        f"stop-bearing: {sorted(stop_variants)})"
    )


def check_validate_prices_fail_closed(config: dict, types_src: str) -> str:
    block = contract_block(config)
    enum = block["order_type_enum"]
    error_enum = block["price_error_enum"]
    body = _fn_body(_impl_block(types_src, enum), "validate_prices")
    # STRUCTURAL check (not a bare token search): each price must be guarded by a
    # condition that is EXACTLY `price_minor <= 0` immediately returning the
    # matching error. A token search would pass a disabled/weakened guard such as
    # `if false && price_minor <= 0` or `if price_minor < 0`, certifying fail-OPEN
    # validation on the no-cargo static path (architecture_check).
    for variant in ("NonPositiveStopPrice", "NonPositiveLimitPrice"):
        guard = re.compile(
            r"if\s+price_minor\s*<=\s*0\s*\{\s*return\s+Err\(\s*"
            rf"(?:{re.escape(error_enum)}::)?{re.escape(variant)}\b"
        )
        if not guard.search(body):
            fail(
                f"{enum}::validate_prices must guard {error_enum}::{variant} with exactly "
                "`if price_minor <= 0 {{ return Err(..) }}` — a missing, disabled, or weakened "
                "guard (e.g. `if false && price_minor <= 0`, or `< 0`) is fail-OPEN"
            )
    # Reject an obvious unreachable wrap (intact guards nested in an always-false
    # block). This is a best-effort static supplement; the AUTHORITATIVE fail-closed
    # proof is the executable Rust test required by check_cargo_test_smoke.
    if re.search(r"\bif\s+false\b", body):
        fail(
            f"{enum}::validate_prices contains an `if false` block — a price guard made unreachable "
            "by an always-false condition is fail-OPEN even with the guard text intact"
        )
    return (
        f"{enum}::validate_prices is fail-closed: each price is guarded by an exact "
        f"`price_minor <= 0` branch returning {error_enum}::NonPositive{{Limit,Stop}}Price "
        "(structurally verified, no unreachable wrap; the executable Rust test is authoritative)"
    )


def check_paper_reexport(config: dict, paper_src: str) -> str:
    block = contract_block(config)
    consumer = block["paper_consumer"]
    # The paper path must RE-EXPORT the shared authority, not redefine it.
    for symbol in consumer["reexports"]:
        if not re.search(rf"\bpub\s+use\s+atp_types::[^;]*\b{re.escape(symbol)}\b", paper_src):
            fail(
                f"{consumer['path']} must `pub use` atp_types' {symbol} (re-export the shared "
                "order-type authority), not define its own"
            )
    # No local redefinition of the order-type vocabulary may reappear.
    for forbidden in ("OrderType", "Side", "AssetClass"):
        if re.search(rf"\bpub\s+enum\s+{re.escape(forbidden)}\b", paper_src):
            fail(
                f"{consumer['path']} redefines `pub enum {forbidden}` — the order-type vocabulary "
                "must be the single atp-types authority (a local copy can drift from live)"
            )
    return (
        f"{consumer['path']} re-exports atp-types' {', '.join(consumer['reexports'])} and defines "
        "no local copy — the order-type model is a single shared definition (paper consumes it "
        "via re-export; live consumption deferred)"
    )


def check_paper_validate_parity(config: dict, paper_src: str) -> str:
    block = contract_block(config)
    consumer = block["paper_consumer"]
    body = _fn_body(paper_src, consumer["validate_fn"])
    # The paper intake must DELEGATE to the shared authority (not re-implement the
    # rule), so paper and live validation cannot drift even semantically.
    if ".validate_prices()" not in body:
        fail(
            f"{consumer['path']}::{consumer['validate_fn']} must DELEGATE price positivity to "
            "OrderType::validate_prices() (the shared SRS-EXE-003 authority), not re-implement it — "
            "a copy can drift from the live path"
        )
    for needle in ("NonPositiveLimitPrice", "NonPositiveStopPrice"):
        if needle not in body:
            fail(
                f"{consumer['path']}::{consumer['validate_fn']} must map the shared "
                f"OrderTypeError::{needle} into OrderError::{needle}"
            )
    return (
        f"{consumer['path']}::{consumer['validate_fn']} DELEGATES price positivity to "
        "OrderType::validate_prices() and maps the result into OrderError (paper intake cannot "
        "drift from the shared authority — semantic parity, not a copy)"
    )


def check_fill_model_delegation(config: dict, fill_src: str) -> str:
    block = contract_block(config)
    consumer = block["fill_model_consumer"]
    error_enum = consumer["error_enum"]
    body = _fn_body(fill_src, consumer["validate_fn"])
    # The SRS-SIM-002 fill path re-checks a raw OrderType; it too must DELEGATE to
    # the shared authority so it cannot drift from intake validation.
    if ".validate_prices()" not in body:
        fail(
            f"{consumer['path']}::{consumer['validate_fn']} must DELEGATE price positivity to "
            "OrderType::validate_prices() (the shared SRS-EXE-003 authority), not re-implement it — "
            "the fill path could otherwise drift from intake validation"
        )
    for needle in ("NonPositiveLimitPrice", "NonPositiveStopPrice"):
        if needle not in body:
            fail(
                f"{consumer['path']}::{consumer['validate_fn']} must map the shared "
                f"OrderTypeError::{needle} into {error_enum}::{needle}"
            )
    return (
        f"{consumer['path']}::{consumer['validate_fn']} DELEGATES price positivity to "
        f"OrderType::validate_prices() and maps into {error_enum} (the fill path shares the same "
        "single authority — no drift)"
    )


def check_cargo_test_smoke(config: dict) -> str:
    block = contract_block(config)
    crate = block["types_crate"]["crate"]
    integration = block["integration_test"]
    cargo = shutil.which("cargo")
    if cargo is None:
        # FAIL CLOSED, do not skip-and-PASS. A static regex over validate_prices can
        # be fooled (e.g. guards wrapped in an unreachable `if false { .. }`); the
        # AUTHORITATIVE fail-closed proof is the executable Rust test
        # (order_type L1 + the srs_exe_003 integration test, which assert a
        # non-positive price is rejected). For this safety-critical gate, refusing
        # to certify without that proof is the correct behavior.
        fail(
            f"cargo is not on PATH: the executable proof that {block['order_type_enum']}::"
            "validate_prices is fail-closed cannot run. This safety gate FAILS CLOSED rather than "
            "reporting PASS on the static regex alone. Install the Rust toolchain."
        )
    lib = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", "order_type", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if lib.returncode != 0:
        fail(f"cargo test -p {crate} --lib order_type failed:\n{lib.stdout}\n{lib.stderr}")
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
        f"cargo test -p {crate} --lib order_type + --test {integration}: PASS "
        "(order-type vocabulary + price matrix + fail-closed validation verified)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


def _run_static(
    config: dict, types_src: str, paper_src: str, fill_src: str, root: Path = ROOT
) -> list[str]:
    return [
        check_order_type_enum(config, types_src, root),
        check_order_side_enum(config, types_src, root),
        check_asset_class(config, types_src, root),
        check_price_matrix(config, types_src),
        check_validate_prices_fail_closed(config, types_src),
        check_paper_reexport(config, paper_src),
        check_paper_validate_parity(config, paper_src),
        check_fill_model_delegation(config, fill_src),
    ]


def run_checks() -> list[str]:
    config = load_config()
    types_src = types_source()
    paper_src = paper_order_source()
    fill_src = fill_model_source()
    evidence = _run_static(config, types_src, paper_src, fill_src, ROOT)
    evidence.append(check_cargo_test_smoke(config))
    return evidence


def assert_order_type_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    types_src = types_source(root)
    paper_src = paper_order_source(root)
    fill_src = fill_model_source(root)
    return _run_static(config, types_src, paper_src, fill_src, root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-EXE-003 SDK-surface contract evidence")
    parser.parse_args(argv)

    try:
        config = load_config()
        evidence = run_checks()
    except OrderTypeCheckError as error:
        print(f"SRS-EXE-003 FAIL: {error}", file=sys.stderr)
        return 1

    # Scope honestly: this is the SDK-surface / pure-logic half (the source-neutral
    # order-type vocabulary + price-validation authority shared by live and paper).
    # It does NOT accept/ack orders through the live IB adapter or the simulation
    # engine, state-track them, route non-live orders, model option contract
    # identity, or submit live multi-leg composites — all deferred. This is NOT a
    # full SRS-EXE-003 requirement pass.
    print("SRS-EXE-003 SDK-SURFACE PASS (contract evidence only; not a full requirement pass)")
    for item in evidence:
        print(f"- {item}")
    print("Deferred end-to-end evidence (SRS-EXE-003 stays passes:false until these land):")
    for owner in contract_block(config).get("deferred", []):
        print(f"  * {owner}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
