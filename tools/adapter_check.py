#!/usr/bin/env python3
"""Contract evidence script for feature API-5.

Parses ``crates/atp-adapters/src/lib.rs`` and verifies that the
brokerage / market-data / historical-data adapter traits expose the
methods named in API-5's description, plus a versioned adapter
capability discovery surface (``AdapterVersion`` + ``version()``)
populated for ``InteractiveBrokersAdapter`` per SRS-EXE-007 / SyRS
SYS-65.

Mirrors the PASS/FAIL output style of ``tools/cli_check.py`` and
``tools/websocket_api_check.py``.

Invoke:
    python3 tools/adapter_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class AdapterContractError(AssertionError):
    pass


def fail(message: str) -> None:
    raise AdapterContractError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def adapter_contract(config: dict) -> dict:
    if "adapter_contract" not in config:
        fail("architecture metadata is missing adapter_contract")
    return config["adapter_contract"]


def adapter_source(config: dict, root: Path = ROOT) -> str:
    contract = adapter_contract(config)
    crate_path = root / contract["adapter_crate"]["path"]
    source_path = crate_path / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"adapter crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def _trait_block(source: str, trait: str) -> str:
    """Return the body of ``pub trait <trait>`` up to the matching closing brace."""
    match = re.search(rf"\bpub\s+trait\s+{re.escape(trait)}\b[^\{{]*\{{", source)
    if not match:
        fail(f"adapter crate is missing public trait `{trait}`")
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
        fail(f"could not parse trait body for `{trait}`")
    return source[start : index - 1]


def _required_methods(contract: dict, trait: str) -> list[str]:
    methods = contract["required_methods"].get(trait)
    if not methods:
        fail(f"adapter_contract.required_methods is missing trait `{trait}`")
    return list(methods)


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_brokerage_methods(config: dict, source: str) -> str:
    contract = adapter_contract(config)
    trait = "BrokerageAdapter"
    body = _trait_block(source, trait)
    required = _required_methods(contract, trait)
    missing = [name for name in required if not re.search(rf"\bfn\s+{re.escape(name)}\s*\(", body)]
    if missing:
        fail(f"{trait} is missing methods: {', '.join(missing)}")
    return f"{trait} declares {len(required)} required methods: {', '.join(required)}"


def check_market_data_methods(config: dict, source: str) -> str:
    contract = adapter_contract(config)
    trait = "MarketDataAdapter"
    body = _trait_block(source, trait)
    required = _required_methods(contract, trait)
    missing = [name for name in required if not re.search(rf"\bfn\s+{re.escape(name)}\s*\(", body)]
    if missing:
        fail(f"{trait} is missing methods: {', '.join(missing)}")
    return f"{trait} declares {len(required)} required methods: {', '.join(required)}"


def check_historical_methods(config: dict, source: str) -> str:
    contract = adapter_contract(config)
    trait = "HistoricalDataAdapter"
    body = _trait_block(source, trait)
    required = _required_methods(contract, trait)
    missing = [name for name in required if not re.search(rf"\bfn\s+{re.escape(name)}\s*\(", body)]
    if missing:
        fail(f"{trait} is missing methods: {', '.join(missing)}")
    return f"{trait} declares {len(required)} required methods: {', '.join(required)}"


def check_version_struct(config: dict, source: str) -> str:
    contract = adapter_contract(config)
    name = contract["version_struct"]
    match = re.search(
        rf"pub\s+struct\s+{re.escape(name)}\b[^\{{]*\{{(?P<body>[^}}]*)\}}",
        source,
    )
    if not match:
        fail(f"adapter crate is missing public struct `{name}`")
    body = match.group("body")
    missing = [
        field
        for field in contract["version_struct_fields"]
        if not re.search(rf"\bpub\s+{re.escape(field)}\s*:", body)
    ]
    if missing:
        fail(f"{name} is missing required fields: {', '.join(missing)}")
    return (
        f"{name} declares {len(contract['version_struct_fields'])} fields: "
        f"{', '.join(contract['version_struct_fields'])}"
    )


def check_version_default_method(config: dict, source: str) -> str:
    contract = adapter_contract(config)
    method = contract["version_method"]
    struct = contract["version_struct"]
    body = _trait_block(source, "AdapterBoundary")
    if not re.search(
        rf"\bfn\s+{re.escape(method)}\s*\(\s*&self\s*\)\s*->\s*{re.escape(struct)}\b",
        body,
    ):
        fail(
            f"AdapterBoundary is missing default `{method}() -> {struct}` "
            "method (versioned capability discovery)"
        )
    return (
        f"AdapterBoundary exposes default `fn {method}(&self) -> {struct}` "
        "for versioned capability discovery"
    )


def check_interactive_brokers_version(config: dict, source: str) -> str:
    contract = adapter_contract(config)
    ib = contract["interactive_brokers"]
    struct = ib["provider_struct"]
    constant = ib["protocol_version_constant"]
    method = contract["version_method"]

    constant_match = re.search(
        rf"\bpub\s+const\s+{re.escape(constant)}\s*:\s*&\s*(?:'static\s+)?str\s*=\s*\"(?P<value>[^\"]+)\"",
        source,
    )
    if not constant_match:
        fail(f"adapter crate is missing constant `{constant}` with a string literal")
    value = constant_match.group("value")
    if not value.strip():
        fail(f"{constant} must be a non-empty string literal")
    if value != ib["protocol_version"]:
        fail(
            f"{constant} = {value!r} does not match runtime_services "
            f"adapter_contract.interactive_brokers.protocol_version "
            f"= {ib['protocol_version']!r}"
        )

    impl_match = re.search(
        rf"impl\s+AdapterBoundary\s+for\s+{re.escape(struct)}\s*\{{",
        source,
    )
    if not impl_match:
        fail(f"missing `impl AdapterBoundary for {struct}` block")
    start = impl_match.end()
    depth = 1
    index = start
    while index < len(source) and depth:
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    impl_body = source[start : index - 1]
    if not re.search(rf"\bfn\s+{re.escape(method)}\s*\(", impl_body):
        fail(f"{struct} does not override `{method}()` to expose its IB TWS API version")
    if constant not in impl_body:
        fail(
            f"{struct}::{method} does not reference `{constant}` "
            "(versioned capability discovery must use the documented constant)"
        )

    return (
        f"{struct} overrides `{method}()` and documents "
        f"{ib['protocol_label']} version {value} via {constant}"
    )


def check_cargo_test_smoke(config: dict, source: str) -> str:
    contract = adapter_contract(config)
    crate_path = contract["adapter_crate"]["path"]
    cargo = shutil.which("cargo")
    if cargo is None:
        return f"cargo test -p {contract['adapter_crate']['crate']}: skipped (cargo not on PATH)"
    result = subprocess.run(
        [cargo, "test", "-p", contract["adapter_crate"]["crate"], "--lib", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(
            "cargo test -p "
            f"{contract['adapter_crate']['crate']} failed:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    combined = result.stdout + result.stderr
    if "test result: ok" not in combined and "0 failed" not in combined:
        fail(f"cargo test output did not include `test result: ok`:\n{combined}")
    return (
        f"cargo test -p {contract['adapter_crate']['crate']} --lib: PASS (crate path {crate_path})"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


def run_checks() -> list[str]:
    config = load_config()
    source = adapter_source(config)
    evidence: list[str] = []
    for check in (
        check_brokerage_methods,
        check_market_data_methods,
        check_historical_methods,
        check_version_struct,
        check_version_default_method,
        check_interactive_brokers_version,
        check_cargo_test_smoke,
    ):
        evidence.append(check(config, source))
    return evidence


def assert_adapter_contract_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    source = (
        root / config["adapter_contract"]["adapter_crate"]["path"] / "src" / "lib.rs"
    ).read_text(encoding="utf-8")
    evidence: list[str] = []
    for check in (
        check_brokerage_methods,
        check_market_data_methods,
        check_historical_methods,
        check_version_struct,
        check_version_default_method,
        check_interactive_brokers_version,
    ):
        evidence.append(check(config, source))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="API-5 contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except AdapterContractError as error:
        print(f"API-5 FAIL: {error}", file=sys.stderr)
        return 1

    print("API-5 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
