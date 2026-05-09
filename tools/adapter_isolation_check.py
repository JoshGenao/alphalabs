#!/usr/bin/env python3
"""Adapter-isolation checks for SRS-ARCH-003."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"
IMPORT_RE = re.compile(r"^\s*(?:use|extern\s+crate)\s+([^;]+);")


class AdapterIsolationError(AssertionError):
    pass


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def fail(message: str) -> None:
    raise AdapterIsolationError(message)


def adapter_config(config: dict) -> dict:
    if "adapter_isolation" not in config:
        fail("architecture metadata is missing adapter_isolation")
    return config["adapter_isolation"]


def assert_adapter_contract_surface(config: dict, root: Path = ROOT) -> list[str]:
    isolation = adapter_config(config)
    adapter_crate = root / isolation["adapter_crate"]["path"]
    source_path = adapter_crate / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"adapter crate source does not exist: {source_path.relative_to(root)}")

    source = source_path.read_text(encoding="utf-8")
    missing_traits = [
        trait
        for trait in isolation["required_traits"]
        if not re.search(rf"\bpub\s+trait\s+{re.escape(trait)}\b", source)
    ]
    if missing_traits:
        fail(f"adapter crate is missing public trait(s): {', '.join(missing_traits)}")

    missing_providers = [
        provider
        for provider in isolation["required_providers"]
        if not re.search(rf"\bpub\s+struct\s+{re.escape(provider)}\b", source)
    ]
    if missing_providers:
        fail(f"adapter crate is missing provider stub(s): {', '.join(missing_providers)}")

    return [
        f"SRS-ARCH-003 adapter crate exposes {len(isolation['required_traits'])} public traits "
        f"and {len(isolation['required_providers'])} provider stubs"
    ]


def service_paths(config: dict) -> dict[str, str]:
    return {service["crate"]: service["path"] for service in config["core_runtime_services"]}


def rust_import_lines(crate_path: Path) -> Iterable[tuple[Path, int, str, str]]:
    for path in sorted((crate_path / "src").rglob("*.rs")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = IMPORT_RE.match(line)
            if match:
                yield path, line_number, line, match.group(1)


def assert_core_vendor_free(config: dict, root: Path = ROOT) -> list[str]:
    isolation = adapter_config(config)
    paths = service_paths(config)
    scanned_crates = isolation["core_vendor_free_crates"]

    for crate_name in scanned_crates:
        crate_rel = paths.get(crate_name)
        if crate_rel is None:
            fail(f"{crate_name} is listed for adapter-isolation scanning but is not a core service")
        crate_path = root / crate_rel
        if not crate_path.exists():
            fail(f"{crate_name} path does not exist: {crate_path.relative_to(root)}")

        for path, line_number, line, import_target in rust_import_lines(crate_path):
            for forbidden in isolation["forbidden_vendor_imports"]:
                token = forbidden["token"]
                if token not in import_target:
                    continue
                relative = path.relative_to(root)
                fail(
                    f"{crate_name} imports vendor token {token!r} at "
                    f"{relative}:{line_number}: {forbidden['reason']} ({line.strip()})"
                )

    return [
        f"SRS-ARCH-003 core source scan found no vendor imports in {len(scanned_crates)} crates"
    ]


def run_command(command: list[str], root: Path) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        fail(f"{command[0]} is required for SRS-ARCH-003 verification: {error}")

    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        fail(f"{' '.join(command)} failed:\n{output}")
    return result.stdout + result.stderr


def assert_adapter_crate_compiles(root: Path = ROOT) -> list[str]:
    run_command(["cargo", "test", "-p", "atp-adapters", "--lib"], root)
    return ["SRS-ARCH-003 atp-adapters compile-only stubs pass cargo test"]


def source_hashes(root: Path, config: dict) -> dict[str, str]:
    paths = service_paths(config)
    hashes: dict[str, str] = {}
    for crate_name in adapter_config(config)["core_vendor_free_crates"]:
        crate_path = root / paths[crate_name]
        for path in sorted((crate_path / "src").rglob("*.rs")):
            relative = str(path.relative_to(root))
            hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def write_fictional_adapter_fixture(root: Path) -> None:
    workspace_manifest = root / "Cargo.toml"
    manifest_text = workspace_manifest.read_text(encoding="utf-8")
    if '"crates/fictional-alt-data-adapter"' not in manifest_text:
        manifest_text = manifest_text.replace(
            '    "crates/atp-notification",\n]',
            '    "crates/atp-notification",\n    "crates/fictional-alt-data-adapter",\n]',
        )
    workspace_manifest.write_text(manifest_text, encoding="utf-8")

    fixture = root / "crates" / "fictional-alt-data-adapter"
    (fixture / "src").mkdir(parents=True)
    (fixture / "Cargo.toml").write_text(
        """[package]
name = "fictional-alt-data-adapter"
version = "0.1.0"
edition = "2021"
rust-version = "1.75"
license = "UNLICENSED"
publish = false

[lib]
path = "src/lib.rs"

[dependencies]
atp-adapters = { path = "../atp-adapters" }
""",
        encoding="utf-8",
    )
    (fixture / "src" / "lib.rs").write_text(
        """use atp_adapters::{
    AdapterBoundary, AdapterCapability, AdapterResult, AlternativeDataProvider,
    AlternativeDataRequest, AlternativeDataSet, DataProviderAdapter,
};

const FICTIONAL_CAPABILITIES: &[AdapterCapability] = &[AdapterCapability::AlternativeData];

#[derive(Debug, Default)]
pub struct LunarSentimentAdapter;

impl AdapterBoundary for LunarSentimentAdapter {
    fn provider_name(&self) -> &'static str {
        "fictional_lunar_sentiment"
    }

    fn capabilities(&self) -> &'static [AdapterCapability] {
        FICTIONAL_CAPABILITIES
    }
}

impl DataProviderAdapter for LunarSentimentAdapter {
    fn provider_family(&self) -> &'static str {
        "alternative-data"
    }
}

impl AlternativeDataProvider for LunarSentimentAdapter {
    fn fetch_alternative_data(
        &self,
        request: AlternativeDataRequest,
    ) -> AdapterResult<AlternativeDataSet> {
        Ok(AlternativeDataSet {
            dataset: request.dataset,
            rows: 1,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn implements_fictional_adapter_using_public_interfaces_only() {
        let adapter = LunarSentimentAdapter;
        let data = adapter
            .fetch_alternative_data(AlternativeDataRequest {
                dataset: "lunar-sentiment".to_string(),
            })
            .unwrap();
        assert_eq!(adapter.provider_name(), "fictional_lunar_sentiment");
        assert_eq!(adapter.provider_family(), "alternative-data");
        assert_eq!(data.rows, 1);
    }
}
""",
        encoding="utf-8",
    )


def assert_structural_fictional_adapter(config: dict, root: Path = ROOT) -> list[str]:
    with tempfile.TemporaryDirectory() as temp:
        temp_root = Path(temp)
        shutil.copytree(root / "crates", temp_root / "crates")
        shutil.copy2(root / "Cargo.toml", temp_root / "Cargo.toml")

        temp_config = json.loads(json.dumps(config))
        before = source_hashes(temp_root, temp_config)
        write_fictional_adapter_fixture(temp_root)
        run_command(["cargo", "test", "-p", "fictional-alt-data-adapter", "--lib"], temp_root)
        after = source_hashes(temp_root, temp_config)
        if before != after:
            changed = sorted(path for path in before if before.get(path) != after.get(path))
            fail(
                "fictional adapter fixture modified core module source files: "
                + ", ".join(changed)
            )

    return [
        "SRS-ARCH-003 fictional alternative-data adapter compiled using public interfaces "
        "with no core source changes"
    ]


def assert_adapter_isolation_static(config: dict, root: Path = ROOT) -> list[str]:
    evidence: list[str] = []
    evidence.extend(assert_adapter_contract_surface(config, root))
    evidence.extend(assert_core_vendor_free(config, root))
    return evidence


def assert_adapter_isolation(config: dict, root: Path = ROOT) -> list[str]:
    evidence = assert_adapter_isolation_static(config, root)
    evidence.extend(assert_adapter_crate_compiles(root))
    evidence.extend(assert_structural_fictional_adapter(config, root))
    return evidence


def make_fixture_root(fixture: str) -> tempfile.TemporaryDirectory[str]:
    temp_dir = tempfile.TemporaryDirectory()
    temp_root = Path(temp_dir.name)
    shutil.copytree(ROOT / "crates", temp_root / "crates")
    shutil.copy2(ROOT / "Cargo.toml", temp_root / "Cargo.toml")
    (temp_root / "architecture").mkdir()
    shutil.copy2(CONFIG_PATH, temp_root / "architecture" / "runtime_services.json")

    if fixture == "core-imports-ib":
        source = temp_root / "crates" / "atp-execution" / "src" / "lib.rs"
        source.write_text(
            source.read_text(encoding="utf-8") + "\nuse interactive_brokers::GatewayClient;\n",
            encoding="utf-8",
        )
    elif fixture == "core-imports-databento":
        source = temp_root / "crates" / "atp-data" / "src" / "lib.rs"
        source.write_text(
            source.read_text(encoding="utf-8") + "\nuse databento::DbnClient;\n",
            encoding="utf-8",
        )
    elif fixture == "core-imports-sharadar":
        source = temp_root / "crates" / "atp-factor-pipeline" / "src" / "lib.rs"
        source.write_text(
            source.read_text(encoding="utf-8") + "\nuse sharadar::FundamentalsClient;\n",
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
            "core-imports-ib",
            "core-imports-databento",
            "core-imports-sharadar",
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
        evidence = assert_adapter_isolation(config, root)
    except AdapterIsolationError as error:
        print(f"SRS-ARCH-003 FAIL: {error}", file=sys.stderr)
        return 1
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    print("SRS-ARCH-003 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
