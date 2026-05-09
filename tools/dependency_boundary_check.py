#!/usr/bin/env python3
"""Dependency-boundary checks for SRS-ARCH-002."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"
DEPENDENCY_SECTIONS = ("dependencies", "dev-dependencies", "build-dependencies")


class DependencyBoundaryError(AssertionError):
    pass


@dataclass(frozen=True)
class CrateBoundary:
    crate: str
    path: Path
    allowed_dependencies: frozenset[str]


def read_toml(path: Path) -> dict:
    with path.open("rb") as file:
        return tomllib.load(file)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def fail(message: str) -> None:
    raise DependencyBoundaryError(message)


def crate_boundaries(config: dict, root: Path) -> list[CrateBoundary]:
    dependency_config = config["dependency_direction"]
    entries = dependency_config["enforced_crates"] + dependency_config.get("sidecar_crates", [])
    return [
        CrateBoundary(
            crate=entry["crate"],
            path=root / entry["path"],
            allowed_dependencies=frozenset(entry["allowed_dependencies"]),
        )
        for entry in entries
    ]


def all_workspace_crates(config: dict) -> set[str]:
    return {service["crate"] for service in config["core_runtime_services"]}


def dependencies_from_section(section: dict | None) -> Iterable[str]:
    if not section:
        return ()
    return section.keys()


def internal_manifest_dependencies(manifest: dict, workspace_crates: set[str]) -> set[str]:
    dependencies: set[str] = set()
    for section_name in DEPENDENCY_SECTIONS:
        dependencies.update(dependencies_from_section(manifest.get(section_name)))

    for target_config in manifest.get("target", {}).values():
        if not isinstance(target_config, dict):
            continue
        for section_name in DEPENDENCY_SECTIONS:
            dependencies.update(dependencies_from_section(target_config.get(section_name)))

    return dependencies & workspace_crates


def assert_cargo_dependency_direction(config: dict, root: Path = ROOT) -> list[str]:
    workspace_crates = all_workspace_crates(config)
    checked = crate_boundaries(config, root)
    for boundary in checked:
        manifest_path = boundary.path / "Cargo.toml"
        if not manifest_path.exists():
            fail(f"{boundary.crate} is missing Cargo.toml at {manifest_path.relative_to(root)}")

        manifest = read_toml(manifest_path)
        dependencies = internal_manifest_dependencies(manifest, workspace_crates)
        unexpected = sorted(dependencies - boundary.allowed_dependencies)
        if unexpected:
            allowed = ", ".join(sorted(boundary.allowed_dependencies)) or "no internal crates"
            fail(
                f"{boundary.crate} depends on forbidden internal crate(s): "
                f"{', '.join(unexpected)}; allowed: {allowed}"
            )

    return [
        f"SRS-ARCH-002 Cargo graph checked {len(checked)} crates against declared one-way dependencies"
    ]


def source_lines(crate_path: Path) -> Iterable[tuple[Path, int, str]]:
    for path in sorted((crate_path / "src").rglob("*.rs")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            yield path, line_number, line


def assert_forbidden_source_imports(config: dict, root: Path = ROOT) -> list[str]:
    dependency_config = config["dependency_direction"]
    boundaries_by_crate = {boundary.crate: boundary for boundary in crate_boundaries(config, root)}
    scanned_crates = dependency_config["lower_layer_scan_crates"]
    forbidden_imports = dependency_config["forbidden_imports"]

    for crate_name in scanned_crates:
        boundary = boundaries_by_crate.get(crate_name)
        if boundary is None:
            fail(f"{crate_name} is listed for source scanning but has no dependency boundary entry")

        for path, line_number, line in source_lines(boundary.path):
            for forbidden in forbidden_imports:
                token = forbidden["token"]
                allowed_crates = set(forbidden.get("allowed_crates", []))
                if crate_name in allowed_crates:
                    continue
                if token not in line:
                    continue
                relative = path.relative_to(root)
                fail(
                    f"{crate_name} imports forbidden module token {token!r} at "
                    f"{relative}:{line_number}: {forbidden['reason']}"
                )

    return [
        f"SRS-ARCH-002 source scan found no dashboard, orchestrator, or vendor imports "
        f"in {len(scanned_crates)} lower-layer crates"
    ]


def assert_dependency_direction(config: dict, root: Path = ROOT) -> list[str]:
    evidence: list[str] = []
    flow = " -> ".join(config["dependency_direction"]["flow"])
    evidence.append(f"SRS-ARCH-002 enforced dependency flow: {flow}")
    evidence.extend(assert_cargo_dependency_direction(config, root))
    evidence.extend(assert_forbidden_source_imports(config, root))
    return evidence


def make_fixture_root(fixture: str) -> tempfile.TemporaryDirectory[str]:
    temp_dir = tempfile.TemporaryDirectory()
    temp_root = Path(temp_dir.name)
    shutil.copytree(ROOT / "crates", temp_root / "crates")
    shutil.copy2(ROOT / "Cargo.toml", temp_root / "Cargo.toml")
    (temp_root / "architecture").mkdir()
    shutil.copy2(CONFIG_PATH, temp_root / "architecture" / "runtime_services.json")

    if fixture == "lower-layer-orchestrator-import":
        source = temp_root / "crates" / "atp-data" / "src" / "lib.rs"
        source.write_text(
            source.read_text(encoding="utf-8")
            + "\nuse atp_orchestrator::StrategyOrchestrator;\n",
            encoding="utf-8",
        )
    elif fixture == "lower-layer-vendor-adapter-import":
        source = temp_root / "crates" / "atp-strategy-engine" / "src" / "lib.rs"
        source.write_text(
            source.read_text(encoding="utf-8") + "\nuse ib_gateway::IbGatewayClient;\n",
            encoding="utf-8",
        )
    elif fixture == "lower-layer-dashboard-import":
        source = temp_root / "crates" / "atp-types" / "src" / "lib.rs"
        source.write_text(
            source.read_text(encoding="utf-8") + "\nuse atp_dashboard::DashboardState;\n",
            encoding="utf-8",
        )
    else:
        temp_dir.cleanup()
        raise ValueError(f"unknown fixture: {fixture}")

    return temp_dir


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        choices=[
            "lower-layer-orchestrator-import",
            "lower-layer-vendor-adapter-import",
            "lower-layer-dashboard-import",
        ],
        help="Run the check against a temporary workspace containing a known violation.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    root = ROOT
    if args.fixture:
        temp_dir = make_fixture_root(args.fixture)
        root = Path(temp_dir.name)

    try:
        config = load_config(root)
        evidence = assert_dependency_direction(config, root)
    except DependencyBoundaryError as error:
        print(f"SRS-ARCH-002 FAIL: {error}", file=sys.stderr)
        return 1
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    print("SRS-ARCH-002 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
