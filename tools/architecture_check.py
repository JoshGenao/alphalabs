#!/usr/bin/env python3
"""Architecture checks for SRS-ARCH-001 through SRS-ARCH-003."""

from __future__ import annotations

import importlib
import json
import sys
import tomllib
from pathlib import Path

from adapter_check import AdapterContractError, assert_adapter_contract_static
from adapter_isolation_check import AdapterIsolationError, assert_adapter_isolation_static
from config_check import ConfigCheckError, assert_configuration_static
from data_provider_check import (
    DataProviderContractError,
    assert_data_provider_contract_static,
)
from dependency_boundary_check import DependencyBoundaryError, assert_dependency_direction
from deployment_check import DeploymentCheckError, assert_deployment_static
from error_handling_check import (
    ErrorHandlingCheckError,
    assert_error_handling_static,
)
from historical_data_check import (
    HistoricalDataCheckError,
    assert_unified_historical_data_static,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"
COMPOSE_PATH = ROOT / "docker-compose.yml"


class ArchitectureCheckError(AssertionError):
    pass


def read_toml(path: Path) -> dict:
    with path.open("rb") as file:
        return tomllib.load(file)


def fail(message: str) -> None:
    raise ArchitectureCheckError(message)


def assert_workspace_members(config: dict) -> list[str]:
    manifest = read_toml(ROOT / "Cargo.toml")
    members = set(manifest.get("workspace", {}).get("members", []))
    expected = [service["path"] for service in config["core_runtime_services"]]
    missing = sorted(set(expected) - members)
    if missing:
        fail(f"Cargo workspace is missing core service members: {', '.join(missing)}")
    return expected


def assert_rust_service_crates(config: dict) -> list[str]:
    evidence: list[str] = []
    for service in config["core_runtime_services"]:
        crate_path = ROOT / service["path"]
        manifest_path = crate_path / "Cargo.toml"
        source_path = crate_path / "src" / "lib.rs"
        if not manifest_path.exists():
            fail(f"{service['name']} is missing Rust manifest at {manifest_path}")
        if not source_path.exists():
            fail(f"{service['name']} is missing Rust source at {source_path}")

        manifest = read_toml(manifest_path)
        package_name = manifest.get("package", {}).get("name")
        if package_name != service["crate"]:
            fail(
                f"{service['name']} manifest names {package_name!r}, expected {service['crate']!r}"
            )

        python_files = sorted(crate_path.rglob("*.py"))
        if python_files:
            relative = ", ".join(str(path.relative_to(ROOT)) for path in python_files)
            fail(f"{service['name']} has Python files in a core Rust crate: {relative}")

        evidence.append(f"{service['crate']} -> {service['path']}/Cargo.toml")
    return evidence


def assert_strategy_api(config: dict) -> list[str]:
    strategy_api = config["strategy_api"]
    package_path = ROOT / strategy_api["path"]
    if not package_path.exists():
        fail(f"Python Strategy API package path does not exist: {package_path}")

    sys.path.insert(0, str(ROOT / "python"))
    try:
        module = importlib.import_module(strategy_api["package"])
    finally:
        sys.path.pop(0)

    missing = [name for name in strategy_api["required_exports"] if not hasattr(module, name)]
    if missing:
        fail(f"Python Strategy API is missing exports: {', '.join(missing)}")

    return [f"{strategy_api['package']} exports {', '.join(strategy_api['required_exports'])}"]


def assert_rest_api(config: dict) -> list[str]:
    rest_api = config.get("rest_api")
    if rest_api is None:
        return []

    package_path = ROOT / rest_api["path"]
    if not package_path.exists():
        fail(f"REST API package path does not exist: {package_path}")

    sys.path.insert(0, str(ROOT / "python"))
    try:
        module = importlib.import_module(rest_api["package"])
    finally:
        sys.path.pop(0)

    declared_capabilities = {capability.name for capability in module.Capability}
    missing = sorted(set(rest_api["required_capabilities"]) - declared_capabilities)
    if missing:
        fail(f"REST API package is missing capabilities: {', '.join(missing)}")

    snapshot_path = ROOT / rest_api["openapi_snapshot"]
    if not snapshot_path.exists():
        fail(f"REST API OpenAPI snapshot is missing: {snapshot_path}")
    try:
        json.loads(snapshot_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        fail(f"REST API OpenAPI snapshot is not valid JSON: {error}")

    if module.BIND_HOST != rest_api["bind_host"]:
        fail(f"REST API BIND_HOST is {module.BIND_HOST!r}, expected {rest_api['bind_host']!r}")
    if module.AUTH_MODEL != rest_api["auth_model"]:
        fail(f"REST API AUTH_MODEL is {module.AUTH_MODEL!r}, expected {rest_api['auth_model']!r}")

    return [
        f"{rest_api['package']} covers {len(rest_api['required_capabilities'])} "
        f"capabilities and binds {module.BIND_HOST} ({module.AUTH_MODEL})"
    ]


def assert_websocket_api(config: dict) -> list[str]:
    websocket_api = config.get("websocket_api")
    if websocket_api is None:
        return []

    package_path = ROOT / websocket_api["path"]
    if not package_path.exists():
        fail(f"WebSocket API package path does not exist: {package_path}")

    sys.path.insert(0, str(ROOT / "python"))
    try:
        module = importlib.import_module(websocket_api["package"])
    finally:
        sys.path.pop(0)

    declared_channels = {channel.name for channel in module.Channel}
    missing = sorted(set(websocket_api["required_channels"]) - declared_channels)
    if missing:
        fail(f"WebSocket API package is missing channels: {', '.join(missing)}")

    snapshot_path = ROOT / websocket_api["asyncapi_snapshot"]
    if not snapshot_path.exists():
        fail(f"WebSocket API AsyncAPI snapshot is missing: {snapshot_path}")
    try:
        json.loads(snapshot_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        fail(f"WebSocket API AsyncAPI snapshot is not valid JSON: {error}")

    if module.BIND_HOST != websocket_api["bind_host"]:
        fail(
            f"WebSocket API BIND_HOST is {module.BIND_HOST!r}, "
            f"expected {websocket_api['bind_host']!r}"
        )
    if module.AUTH_MODEL != websocket_api["auth_model"]:
        fail(
            f"WebSocket API AUTH_MODEL is {module.AUTH_MODEL!r}, "
            f"expected {websocket_api['auth_model']!r}"
        )
    if module.WS_PATH != websocket_api["ws_path"]:
        fail(f"WebSocket API WS_PATH is {module.WS_PATH!r}, expected {websocket_api['ws_path']!r}")
    if module.MAX_REFRESH_SECONDS != websocket_api["max_refresh_seconds"]:
        fail(
            "WebSocket API MAX_REFRESH_SECONDS is "
            f"{module.MAX_REFRESH_SECONDS}, "
            f"expected {websocket_api['max_refresh_seconds']}"
        )
    for event in module.EVENT_CHANNELS:
        if event.refresh_seconds < 0 or event.refresh_seconds > module.MAX_REFRESH_SECONDS:
            fail(
                f"WebSocket channel {event.name.value} refresh_seconds="
                f"{event.refresh_seconds} violates "
                f"[0, {module.MAX_REFRESH_SECONDS}]s NFR-P2 ceiling"
            )

    return [
        f"{websocket_api['package']} covers "
        f"{len(websocket_api['required_channels'])} channels and binds "
        f"{module.BIND_HOST} {module.WS_PATH} ({module.AUTH_MODEL})"
    ]


def assert_cli(config: dict) -> list[str]:
    cli = config.get("cli")
    if cli is None:
        return []

    package_path = ROOT / cli["path"]
    if not package_path.exists():
        fail(f"CLI package path does not exist: {package_path}")

    sys.path.insert(0, str(ROOT / "python"))
    try:
        module = importlib.import_module(cli["package"])
    finally:
        sys.path.pop(0)

    declared_groups = {group.value for group in module.Group}
    missing = sorted(set(cli["required_groups"]) - declared_groups)
    if missing:
        fail(f"CLI package is missing groups: {', '.join(missing)}")

    snapshot_path = ROOT / cli["manual_snapshot"]
    if not snapshot_path.exists():
        fail(f"CLI manual snapshot is missing: {snapshot_path}")
    try:
        json.loads(snapshot_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        fail(f"CLI manual snapshot is not valid JSON: {error}")

    if module.ACCESS_MODEL != cli["access_model"]:
        fail(f"CLI ACCESS_MODEL is {module.ACCESS_MODEL!r}, expected {cli['access_model']!r}")
    if module.AUTH_MODEL != cli["auth_model"]:
        fail(f"CLI AUTH_MODEL is {module.AUTH_MODEL!r}, expected {cli['auth_model']!r}")
    if module.CLI_ENTRY_POINT != cli["entry_point"]:
        fail(f"CLI entry point is {module.CLI_ENTRY_POINT!r}, expected {cli['entry_point']!r}")

    declared_irreversible = {c.invocation for c in module.COMMANDS if c.requires_confirmation}
    missing_confirm = sorted(set(cli["confirmation_required_commands"]) - declared_irreversible)
    if missing_confirm:
        fail("CLI commands missing requires_confirmation flag: " + ", ".join(missing_confirm))

    return [
        f"{cli['package']} covers {len(cli['required_groups'])} groups and "
        f"runs {module.CLI_ENTRY_POINT} via {module.ACCESS_MODEL} "
        f"({module.AUTH_MODEL})"
    ]


def assert_adapter_contract(config: dict) -> list[str]:
    contract = config.get("adapter_contract")
    if contract is None:
        return []

    static_evidence = assert_adapter_contract_static(config, ROOT)
    method_total = sum(len(v) for v in contract["required_methods"].values())
    ib = contract["interactive_brokers"]
    summary = (
        f"{contract['adapter_crate']['crate']} declares {method_total} required "
        f"trait methods across {len(contract['required_methods'])} adapter traits and "
        f"{ib['provider_struct']} documents {ib['protocol_label']} version "
        f"{ib['protocol_version']} (API-5)"
    )
    return static_evidence + [summary]


def assert_data_provider_contract(config: dict) -> list[str]:
    contract = config.get("data_provider_contract")
    if contract is None:
        return []

    static_evidence = assert_data_provider_contract_static(config, ROOT)
    method_total = sum(len(v) for v in contract["required_methods"].values())
    bindings = contract["provider_bindings"]
    provider_short = "/".join(
        binding["struct"].replace("Adapter", "").replace("Provider", "") for binding in bindings
    )
    summary = (
        f"{contract['adapter_crate']['crate']} declares {method_total} "
        f"data-provider methods across {len(contract['required_methods'])} traits "
        f"and binds {len(bindings)} providers ({provider_short}) to the "
        f"{contract['data_provider_base_trait']} base "
        f"(API-6, SRS-DATA-001..007)"
    )
    return static_evidence + [summary]


def assert_unified_historical_data(config: dict) -> list[str]:
    block = config.get("unified_historical_data")
    if block is None:
        return []

    static_evidence = assert_unified_historical_data_static(config, ROOT)
    summary = (
        f"{block['adapter_crate']['crate']} unified historical query carries "
        f"{len(block['request_fields'])} request fields and a source-neutral "
        f"{block['result_struct']} envelope across "
        f"{len(block['asset_class_variants'])} asset classes / "
        f"{len(block['normalization_variants'])} normalization modes for "
        f"{len(block['consumers'])} consumers (API-7, "
        f"SRS-DATA-007 + SRS-DATA-012)"
    )
    return static_evidence + [summary]


def assert_error_handling(config: dict) -> list[str]:
    block = config.get("error_handling_contract")
    if block is None:
        return []

    static_evidence = assert_error_handling_static(config, ROOT)
    summary = (
        f"{block['execution_crate']['crate']} rejects non-live submissions "
        f"synchronously via {block['entry_point']['type']}::"
        f"{block['entry_point']['method']} with "
        f"{len(block['error_category']['variants'])} SyRS SYS-64 categories "
        f"and {len(block['structured_error']['required_fields'])} structured "
        f"error fields, gating `{block['entry_point']['live_only_call']}` on "
        "StrategyMode::Live (ERR-1, SRS-EXE-001 + SRS-ERR-001)"
    )
    return static_evidence + [summary]


def assert_container_language_boundary(config: dict) -> list[str]:
    if not COMPOSE_PATH.exists():
        fail("docker-compose.yml is missing")
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")

    missing_crates = [
        service["crate"]
        for service in config["core_runtime_services"]
        if service["crate"] not in compose_text
        and service["crate"] not in {"atp-types", "atp-adapters"}
    ]
    if missing_crates:
        fail(f"Container config does not reference core Rust crates: {', '.join(missing_crates)}")

    if "docker/core-runtime.Dockerfile" not in compose_text:
        fail("Container config does not reference the Rust runtime Dockerfile")
    if "docker/strategy-python.Dockerfile" not in compose_text:
        fail("Container config does not reference the Python strategy Dockerfile")

    return ["docker-compose.yml maps core services to docker/core-runtime.Dockerfile"]


def run_checks() -> list[str]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    evidence: list[str] = []
    evidence.extend(assert_workspace_members(config))
    evidence.extend(assert_rust_service_crates(config))
    try:
        evidence.extend(assert_dependency_direction(config, ROOT))
    except DependencyBoundaryError as error:
        fail(str(error))
    try:
        evidence.extend(assert_adapter_isolation_static(config, ROOT))
    except AdapterIsolationError as error:
        fail(str(error))
    evidence.extend(assert_strategy_api(config))
    evidence.extend(assert_rest_api(config))
    evidence.extend(assert_websocket_api(config))
    evidence.extend(assert_cli(config))
    try:
        evidence.extend(assert_adapter_contract(config))
    except AdapterContractError as error:
        fail(str(error))
    try:
        evidence.extend(assert_data_provider_contract(config))
    except DataProviderContractError as error:
        fail(str(error))
    try:
        evidence.extend(assert_unified_historical_data(config))
    except HistoricalDataCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_error_handling(config))
    except ErrorHandlingCheckError as error:
        fail(str(error))
    evidence.extend(assert_container_language_boundary(config))
    try:
        evidence.extend(assert_deployment_static(config, ROOT))
    except DeploymentCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_configuration_static(config, ROOT))
    except ConfigCheckError as error:
        fail(str(error))
    return evidence


def main() -> int:
    try:
        evidence = run_checks()
    except ArchitectureCheckError as error:
        print(f"SRS-ARCH-001 FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-ARCH-001 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
