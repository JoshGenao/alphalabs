#!/usr/bin/env python3
"""Contract evidence script for feature API-6.

Parses ``crates/atp-adapters/src/lib.rs`` and verifies that the data
provider traits (``BulkEquityDataProvider``, ``FundamentalDataProvider``,
``OptionsDataProvider``, ``UserParquetDataProvider``,
``AlternativeDataProvider``) expose the methods named in API-6's
description and that the documented Phase 1 providers
(``DatabentoAdapter``, ``SharadarAdapter``, ``UserParquetAdapter``,
``FutureStubProvider``) bind through the shared ``DataProviderAdapter``
base trait.

API-6 traces to SRS-DATA-001 through SRS-DATA-007. Mirrors the
PASS/FAIL output style of ``tools/adapter_check.py``.

Invoke:
    python3 tools/data_provider_check.py
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

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class DataProviderContractError(AssertionError):
    pass


def fail(message: str) -> None:
    raise DataProviderContractError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads(
        (root / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
    )


def data_provider_contract(config: dict) -> dict:
    if "data_provider_contract" not in config:
        fail("architecture metadata is missing data_provider_contract")
    return config["data_provider_contract"]


def adapter_source(config: dict, root: Path = ROOT) -> str:
    contract = data_provider_contract(config)
    crate_path = root / contract["adapter_crate"]["path"]
    source_path = crate_path / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"adapter crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def _required_methods(contract: dict, trait: str) -> list[str]:
    methods = contract["required_methods"].get(trait)
    if not methods:
        fail(f"data_provider_contract.required_methods is missing trait `{trait}`")
    return list(methods)


def _check_trait_methods(contract: dict, source: str, trait: str) -> str:
    try:
        body = _trait_block(source, trait)
    except AssertionError as error:
        fail(str(error))
    required = _required_methods(contract, trait)
    missing = [
        name for name in required if not re.search(rf"\bfn\s+{re.escape(name)}\s*\(", body)
    ]
    if missing:
        fail(f"{trait} is missing methods: {', '.join(missing)}")
    return f"{trait} declares {len(required)} required methods: {', '.join(required)}"


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_bulk_equity_methods(config: dict, source: str) -> str:
    return _check_trait_methods(
        data_provider_contract(config), source, "BulkEquityDataProvider"
    )


def check_fundamental_methods(config: dict, source: str) -> str:
    return _check_trait_methods(
        data_provider_contract(config), source, "FundamentalDataProvider"
    )


def check_options_methods(config: dict, source: str) -> str:
    return _check_trait_methods(
        data_provider_contract(config), source, "OptionsDataProvider"
    )


def check_user_parquet_methods(config: dict, source: str) -> str:
    return _check_trait_methods(
        data_provider_contract(config), source, "UserParquetDataProvider"
    )


def check_alternative_methods(config: dict, source: str) -> str:
    return _check_trait_methods(
        data_provider_contract(config), source, "AlternativeDataProvider"
    )


def check_data_provider_base_trait(config: dict, source: str) -> str:
    contract = data_provider_contract(config)
    base = contract["data_provider_base_trait"]
    if not re.search(rf"\bpub\s+trait\s+{re.escape(base)}\b", source):
        fail(f"adapter crate is missing public base trait `{base}`")
    bindings = contract["provider_bindings"]
    missing = [
        binding["struct"]
        for binding in bindings
        if not re.search(
            rf"impl\s+{re.escape(base)}\s+for\s+{re.escape(binding['struct'])}\b",
            source,
        )
    ]
    if missing:
        fail(
            f"providers missing `impl {base} for ...`: {', '.join(missing)}"
        )
    return (
        f"{base} base trait shared by {len(bindings)} providers: "
        f"{', '.join(b['struct'] for b in bindings)}"
    )


def check_provider_bindings(config: dict, source: str) -> str:
    contract = data_provider_contract(config)
    bindings = contract["provider_bindings"]
    total = 0
    for binding in bindings:
        struct = binding["struct"]
        for trait in binding["traits"]:
            if not re.search(
                rf"impl\s+{re.escape(trait)}\s+for\s+{re.escape(struct)}\b",
                source,
            ):
                fail(f"missing `impl {trait} for {struct}` block")
            total += 1
    summary = ", ".join(
        f"{b['struct']}({len(b['traits'])})" for b in bindings
    )
    return f"provider bindings verified: {summary} = {total} impls"


def check_capability_traces(config: dict, source: str) -> str:
    contract = data_provider_contract(config)
    declared_srs = set(contract["srs_refs"])
    traces = contract["capability_traces"]
    required_capabilities = {
        "bulk_equity_download",
        "historical_backfill",
        "incremental_update",
        "fundamentals_ingestion",
        "options_import",
        "user_parquet_import",
    }
    seen_capabilities = {trace["capability"] for trace in traces}
    missing = sorted(required_capabilities - seen_capabilities)
    if missing:
        fail(f"capability_traces is missing API-6 capabilities: {', '.join(missing)}")

    for trace in traces:
        trait = trace["trait"]
        method = trace["method"]
        ref = trace["srs_ref"]
        if ref not in declared_srs:
            fail(
                f"capability `{trace['capability']}` srs_ref {ref!r} is not in "
                f"data_provider_contract.srs_refs"
            )
        try:
            body = _trait_block(source, trait)
        except AssertionError as error:
            fail(str(error))
        if not re.search(rf"\bfn\s+{re.escape(method)}\s*\(", body):
            fail(
                f"capability `{trace['capability']}` does not resolve: "
                f"{trait}::{method} not found in source"
            )

    return (
        f"6 API-6 capabilities trace to (trait, method) pairs across "
        f"{len({t['trait'] for t in traces})} data-provider traits"
    )


def check_unified_historical_query(config: dict, source: str) -> str:
    contract = data_provider_contract(config)
    unified = contract["unified_historical_query"]
    trait = unified["trait"]
    method = unified["method"]
    declared_srs = set(contract["srs_refs"])
    if unified["srs_ref"] not in declared_srs:
        fail(
            f"unified_historical_query.srs_ref {unified['srs_ref']!r} is not in "
            f"data_provider_contract.srs_refs"
        )
    try:
        body = _trait_block(source, trait)
    except AssertionError as error:
        fail(str(error))
    if not re.search(rf"\bfn\s+{re.escape(method)}\s*\(", body):
        fail(f"{trait}::{method} not found (SRS-DATA-007 unified query)")
    return (
        f"{trait}::{method} provides source-neutral historical query "
        f"({unified['srs_ref']})"
    )


def check_cargo_test_smoke(config: dict, source: str) -> str:
    contract = data_provider_contract(config)
    crate = contract["adapter_crate"]["crate"]
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
        fail(
            f"cargo test -p {crate} failed:\n{result.stdout}\n{result.stderr}"
        )
    combined = result.stdout + result.stderr
    if "test result: ok" not in combined and "0 failed" not in combined:
        fail(
            "cargo test output did not include `test result: ok`:\n"
            f"{combined}"
        )
    return (
        f"cargo test -p {crate} --lib: PASS "
        f"(data-provider trait surface verified)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


_STATIC_CHECKS = (
    check_bulk_equity_methods,
    check_fundamental_methods,
    check_options_methods,
    check_user_parquet_methods,
    check_alternative_methods,
    check_data_provider_base_trait,
    check_provider_bindings,
    check_capability_traces,
    check_unified_historical_query,
)


def run_checks() -> list[str]:
    config = load_config()
    source = adapter_source(config)
    evidence: list[str] = []
    for check in _STATIC_CHECKS:
        evidence.append(check(config, source))
    evidence.append(check_cargo_test_smoke(config, source))
    return evidence


def assert_data_provider_contract_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    source = (
        root / config["data_provider_contract"]["adapter_crate"]["path"] / "src" / "lib.rs"
    ).read_text(encoding="utf-8")
    evidence: list[str] = []
    for check in _STATIC_CHECKS:
        evidence.append(check(config, source))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="API-6 contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except DataProviderContractError as error:
        print(f"API-6 FAIL: {error}", file=sys.stderr)
        return 1

    print("API-6 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
