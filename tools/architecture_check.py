#!/usr/bin/env python3
"""Architecture checks for SRS-ARCH-001 through SRS-ARCH-003."""

from __future__ import annotations

import importlib
import json
import sys
import tomllib
from pathlib import Path

from adapter_isolation_check import AdapterIsolationError, assert_adapter_isolation_static
from config_check import ConfigCheckError, assert_configuration_static
from dependency_boundary_check import DependencyBoundaryError, assert_dependency_direction
from deployment_check import DeploymentCheckError, assert_deployment_static


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
                f"{service['name']} manifest names {package_name!r}, "
                f"expected {service['crate']!r}"
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
        fail(
            f"REST API BIND_HOST is {module.BIND_HOST!r}, "
            f"expected {rest_api['bind_host']!r}"
        )
    if module.AUTH_MODEL != rest_api["auth_model"]:
        fail(
            f"REST API AUTH_MODEL is {module.AUTH_MODEL!r}, "
            f"expected {rest_api['auth_model']!r}"
        )

    return [
        f"{rest_api['package']} covers {len(rest_api['required_capabilities'])} "
        f"capabilities and binds {module.BIND_HOST} ({module.AUTH_MODEL})"
    ]


def assert_container_language_boundary(config: dict) -> list[str]:
    if not COMPOSE_PATH.exists():
        fail("docker-compose.yml is missing")
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")

    missing_crates = [
        service["crate"]
        for service in config["core_runtime_services"]
        if service["crate"] not in compose_text and service["crate"] not in {"atp-types", "atp-adapters"}
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
