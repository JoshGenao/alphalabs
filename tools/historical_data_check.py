#!/usr/bin/env python3
"""Contract evidence script for feature API-7.

Verifies that the unified historical data interface declared in
``architecture/runtime_services.json`` (block ``unified_historical_data``)
is reachable from the Rust adapter crate ``crates/atp-adapters`` and the
Python strategy package ``python/atp_strategy``.

API-7 traces SRS-DATA-007 + SRS-DATA-012. Mirrors the PASS/FAIL
output style of ``tools/data_provider_check.py``.

Invoke:
    python3 tools/historical_data_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from adapter_check import _trait_block
from _rust_parser import _enum_body, _struct_body

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"
PYTHON_ROOT = ROOT / "python"


class HistoricalDataCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise HistoricalDataCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def unified_block(config: dict) -> dict:
    if "unified_historical_data" not in config:
        fail("architecture metadata is missing unified_historical_data")
    return config["unified_historical_data"]


def adapter_source(config: dict, root: Path = ROOT) -> str:
    block = unified_block(config)
    crate_path = root / block["adapter_crate"]["path"]
    source_path = crate_path / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"adapter crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def python_protocol_source(config: dict, root: Path = ROOT) -> str:
    block = unified_block(config)
    package = block["python_protocol"]["package"]
    package_path = root / "python" / package
    if not package_path.exists():
        fail(f"Python strategy package missing: {package_path.relative_to(root)}")
    api_path = package_path / "api.py"
    if not api_path.exists():
        fail(f"Python strategy api module missing: {api_path.relative_to(root)}")
    init_path = package_path / "__init__.py"
    if not init_path.exists():
        fail(f"Python strategy package __init__ missing: {init_path.relative_to(root)}")
    return (
        api_path.read_text(encoding="utf-8")
        + "\n\n# __init__\n"
        + init_path.read_text(encoding="utf-8")
    )


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_request_struct(config: dict, source: str) -> str:
    block = unified_block(config)
    struct = block["request_struct"]
    body = _struct_body(source, struct)
    missing = [
        field
        for field in block["request_fields"]
        if not re.search(rf"\bpub\s+{re.escape(field)}\s*:", body)
    ]
    if missing:
        fail(f"{struct} is missing required fields: {', '.join(missing)}")
    return (
        f"{struct} declares {len(block['request_fields'])} query fields: "
        f"{', '.join(block['request_fields'])}"
    )


def check_result_envelope(config: dict, source: str) -> str:
    block = unified_block(config)
    struct = block["result_struct"]
    body = _struct_body(source, struct)
    missing = [
        field
        for field in block["result_fields"]
        if not re.search(rf"\bpub\s+{re.escape(field)}\s*:", body)
    ]
    if missing:
        fail(f"{struct} is missing required fields: {', '.join(missing)}")
    leaks = [
        field
        for field in block["result_forbidden_fields"]
        if re.search(rf"\bpub\s+{re.escape(field)}\s*:", body)
    ]
    if leaks:
        fail(
            f"{struct} leaks vendor-source field(s): {', '.join(leaks)} "
            "(API-7 requires a source-neutral envelope)"
        )
    return (
        f"{struct} envelope is source-neutral with {len(block['result_fields'])} "
        f"fields ({', '.join(block['result_fields'])}) and rejects "
        f"{len(block['result_forbidden_fields'])} forbidden vendor fields"
    )


def check_asset_class_enum(config: dict, source: str) -> str:
    block = unified_block(config)
    name = block["asset_class_enum"]
    body = _enum_body(source, name)
    missing = [
        variant
        for variant in block["asset_class_variants"]
        if not re.search(rf"\b{re.escape(variant)}\b", body)
    ]
    if missing:
        fail(f"{name} enum is missing variants: {', '.join(missing)}")
    return (
        f"{name} declares {len(block['asset_class_variants'])} variants: "
        f"{', '.join(block['asset_class_variants'])}"
    )


def check_normalization_enum(config: dict, source: str) -> str:
    block = unified_block(config)
    name = block["normalization_enum"]
    body = _enum_body(source, name)
    missing = [
        variant
        for variant in block["normalization_variants"]
        if not re.search(rf"\b{re.escape(variant)}\b", body)
    ]
    if missing:
        fail(f"{name} enum is missing variants: {', '.join(missing)}")
    return (
        f"{name} declares {len(block['normalization_variants'])} SRS-DATA-012 "
        f"variants: {', '.join(block['normalization_variants'])}"
    )


def check_trait_signature(config: dict, source: str) -> str:
    block = unified_block(config)
    trait = block["trait"]
    method = block["method"]
    result_type = block["result_type"]
    body = _trait_block(source, trait)
    pattern = (
        rf"\bfn\s+{re.escape(method)}\s*\([^)]*\)\s*->\s*"
        rf"{re.escape(result_type)}"
    )
    if not re.search(pattern, body):
        fail(
            f"{trait}::{method} does not return {result_type} (API-7 source-neutral query envelope)"
        )
    return f"{trait}::{method} returns {result_type} (SRS-DATA-007 + SRS-DATA-012)"


def check_python_protocol(config: dict, python_source: str) -> str:
    block = unified_block(config)
    proto = block["python_protocol"]
    protocol = proto["protocol"]
    method = proto["method"]
    class_match = re.search(
        rf"\bclass\s+{re.escape(protocol)}\b[^:]*:\s*\n",
        python_source,
    )
    if not class_match:
        fail(f"Python package missing Protocol class `{protocol}`")
    enum = proto["normalization_enum"]
    if not re.search(rf"\bclass\s+{re.escape(enum)}\b", python_source):
        fail(f"Python package missing `{enum}` enum")
    # Search the Protocol body (everything after the class header up to the
    # next top-level ``class`` / ``def`` / module-level marker) so doctest
    # examples earlier in the module are skipped.
    body_after = python_source[class_match.end() :]
    next_top = re.search(r"\n(class\s+\w|def\s+\w|@\w)", body_after)
    body = body_after if next_top is None else body_after[: next_top.start()]
    # Pick the multi-line ``def`` (real Protocol method) over the single-line
    # doctest example earlier in the docstring.
    method_match = None
    for candidate in re.finditer(
        rf"def\s+{re.escape(method)}\s*\((?P<params>[^)]*)\)",
        body,
        re.DOTALL,
    ):
        if "\n" in candidate.group("params"):
            method_match = candidate
            break
    if method_match is None:
        fail(f"Python `{protocol}.{method}` not found")
    signature = method_match.group("params")
    missing = [
        name for name in proto["parameters"] if not re.search(rf"\b{re.escape(name)}\b", signature)
    ]
    if missing:
        fail(f"Python `{protocol}.{method}` is missing parameters: {', '.join(missing)}")
    if f'"{enum}"' not in python_source and f"'{enum}'" not in python_source:
        # The __init__ marker section embeds the re-export list as plain
        # identifiers, not quoted strings; fall back to a token search.
        if not re.search(rf"\b{re.escape(enum)}\b", python_source.split("# __init__")[1]):
            fail(f"`{enum}` is not re-exported from atp_strategy")
    return (
        f"atp_strategy.{protocol}.{method} accepts "
        f"{len(proto['parameters'])} parameters "
        f"({', '.join(proto['parameters'])}) and re-exports {enum}"
    )


def check_cargo_test_smoke(config: dict, source: str) -> str:
    block = unified_block(config)
    crate = block["adapter_crate"]["crate"]
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
        fail(f"cargo test -p {crate} failed:\n{result.stdout}\n{result.stderr}")
    combined = result.stdout + result.stderr
    if "test result: ok" not in combined and "0 failed" not in combined:
        fail(f"cargo test output did not include `test result: ok`:\n{combined}")
    return f"cargo test -p {crate} --lib: PASS (unified historical query trait surface verified)"


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


_RUST_CHECKS = (
    check_request_struct,
    check_result_envelope,
    check_asset_class_enum,
    check_normalization_enum,
    check_trait_signature,
)


def run_checks() -> list[str]:
    config = load_config()
    source = adapter_source(config)
    python_source = python_protocol_source(config)
    evidence: list[str] = []
    for check in _RUST_CHECKS:
        evidence.append(check(config, source))
    evidence.append(check_python_protocol(config, python_source))
    evidence.append(check_cargo_test_smoke(config, source))
    return evidence


def assert_unified_historical_data_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    source = (
        root / config["unified_historical_data"]["adapter_crate"]["path"] / "src" / "lib.rs"
    ).read_text(encoding="utf-8")
    python_source = python_protocol_source(config, root)
    evidence: list[str] = []
    for check in _RUST_CHECKS:
        evidence.append(check(config, source))
    evidence.append(check_python_protocol(config, python_source))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="API-7 contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except HistoricalDataCheckError as error:
        print(f"API-7 FAIL: {error}", file=sys.stderr)
        return 1

    print("API-7 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
