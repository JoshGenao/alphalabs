#!/usr/bin/env python3
"""Phase 1 Docker Compose deployment checks for SRS-ARCH-004."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class DeploymentCheckError(AssertionError):
    pass


def load_config(root: Path = ROOT) -> dict:
    return json.loads(
        (root / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
    )


def fail(message: str) -> None:
    raise DeploymentCheckError(message)


def deployment_config(config: dict) -> dict:
    if "deployment" not in config:
        fail("architecture metadata is missing deployment block")
    return config["deployment"]


def _service_block_contains(compose_text: str, service_name: str, needle: str) -> bool:
    pattern = re.compile(
        rf"^\s+{re.escape(service_name)}:\n(?P<body>(?:^[ \t].*\n|^\s*\n)*)",
        re.MULTILINE,
    )
    match = pattern.search(compose_text)
    if match is None:
        return False
    return needle in match.group("body")


def assert_compose_services(config: dict, root: Path = ROOT) -> list[str]:
    deployment = deployment_config(config)
    compose_path = root / deployment["compose_file"]
    if not compose_path.exists():
        fail(f"compose file is missing: {deployment['compose_file']}")

    compose_text = compose_path.read_text(encoding="utf-8")

    profile = deployment["phase1_profile"]
    if f'"{profile}"' not in compose_text and f"'{profile}'" not in compose_text:
        fail(f"compose file does not declare the {profile!r} profile")

    missing = [
        service
        for service in deployment["required_services"]
        if not re.search(rf"^\s+{re.escape(service)}:\s*$", compose_text, re.MULTILINE)
    ]
    if missing:
        fail(f"compose file is missing Phase 1 services: {', '.join(missing)}")

    bind_host = deployment["dashboard_bind_host"]
    if not _service_block_contains(compose_text, "phase1-dashboard-api", f"{bind_host}:"):
        fail(
            f"phase1-dashboard-api must publish ports bound to {bind_host} (SRS-SEC-002)"
        )

    return [
        f"compose declares {len(deployment['required_services'])} Phase 1 services "
        f"under profile {profile!r}",
        f"phase1-dashboard-api binds {bind_host} (SRS-SEC-002)",
    ]


def assert_compose_env_and_volumes(config: dict, root: Path = ROOT) -> list[str]:
    deployment = deployment_config(config)
    compose_text = (root / deployment["compose_file"]).read_text(encoding="utf-8")

    missing_env = [
        env_var
        for env_var in deployment["required_env_vars"]
        if env_var not in compose_text
    ]
    if missing_env:
        fail(
            "compose file does not reference required env vars: "
            + ", ".join(missing_env)
        )

    for volume in deployment["required_volumes"]:
        env_token = "${" + volume["env"]
        if env_token not in compose_text:
            fail(
                f"compose file does not bind {volume['name']} volume to {volume['env']}"
            )
        if not re.search(
            rf"^\s+{re.escape(volume['name'])}:\s*$", compose_text, re.MULTILINE
        ):
            fail(f"compose file does not declare named volume {volume['name']}")

    return [
        f"compose passes {len(deployment['required_env_vars'])} env vars to Phase 1 services",
        "SSD primary tier and NAS archive tier mounted via ATP_SSD_DATA_DIR / ATP_NAS_DATA_DIR",
    ]


def assert_dockerfiles_present(config: dict, root: Path = ROOT) -> list[str]:
    deployment = deployment_config(config)
    missing = [
        dockerfile
        for dockerfile in deployment["required_dockerfiles"]
        if not (root / dockerfile).exists()
    ]
    if missing:
        fail("required Dockerfiles are missing: " + ", ".join(missing))
    return [
        f"{len(deployment['required_dockerfiles'])} Dockerfiles present "
        "(core-runtime, strategy-python, dashboard-api, jupyter, ib-gateway)"
    ]


def assert_env_example(config: dict, root: Path = ROOT) -> list[str]:
    deployment = deployment_config(config)
    env_path = root / deployment["env_example"]
    if not env_path.exists():
        fail(f"env template is missing: {deployment['env_example']}")
    env_text = env_path.read_text(encoding="utf-8")
    missing = [
        env_var
        for env_var in deployment["required_env_vars"]
        if not re.search(rf"^{re.escape(env_var)}=", env_text, re.MULTILINE)
    ]
    if missing:
        fail(
            f"{deployment['env_example']} does not list required keys: "
            + ", ".join(missing)
        )
    return [
        f"{deployment['env_example']} enumerates all "
        f"{len(deployment['required_env_vars'])} required env vars"
    ]


def assert_deployment_doc(config: dict, root: Path = ROOT) -> list[str]:
    deployment = deployment_config(config)
    doc_path = root / deployment["deployment_doc"]
    if not doc_path.exists():
        fail(f"deployment doc is missing: {deployment['deployment_doc']}")
    doc_text = doc_path.read_text(encoding="utf-8")
    missing = [
        keyword
        for keyword in deployment["portability_doc_keywords"]
        if keyword.lower() not in doc_text.lower()
    ]
    if missing:
        fail(
            f"{deployment['deployment_doc']} does not address portability keywords: "
            + ", ".join(missing)
        )
    return [
        f"{deployment['deployment_doc']} documents Phase 1 target, "
        "cloud VPS as future target, and portability constraints"
    ]


def assert_deployment_static(config: dict, root: Path = ROOT) -> list[str]:
    evidence: list[str] = [
        "SRS-ARCH-004 Phase 1 deployment evidence:",
    ]
    evidence.extend(assert_compose_services(config, root))
    evidence.extend(assert_compose_env_and_volumes(config, root))
    evidence.extend(assert_dockerfiles_present(config, root))
    evidence.extend(assert_env_example(config, root))
    evidence.extend(assert_deployment_doc(config, root))
    return evidence


def make_fixture_root(fixture: str) -> tempfile.TemporaryDirectory[str]:
    temp_dir = tempfile.TemporaryDirectory()
    temp_root = Path(temp_dir.name)
    (temp_root / "architecture").mkdir()
    (temp_root / "docker").mkdir()
    (temp_root / "docs").mkdir()

    shutil.copy2(CONFIG_PATH, temp_root / "architecture" / "runtime_services.json")
    shutil.copy2(ROOT / "docker-compose.yml", temp_root / "docker-compose.yml")
    shutil.copy2(ROOT / ".env.example", temp_root / ".env.example")
    shutil.copy2(ROOT / "docs" / "DEPLOYMENT.md", temp_root / "docs" / "DEPLOYMENT.md")
    for dockerfile in (
        "core-runtime.Dockerfile",
        "strategy-python.Dockerfile",
        "dashboard-api.Dockerfile",
        "jupyter.Dockerfile",
        "ib-gateway.Dockerfile",
    ):
        shutil.copy2(ROOT / "docker" / dockerfile, temp_root / "docker" / dockerfile)

    compose_path = temp_root / "docker-compose.yml"
    if fixture == "missing-jupyter":
        text = compose_path.read_text(encoding="utf-8")
        text = re.sub(
            r"\n  phase1-jupyter:\n(?:    [^\n]*\n|    \n|      [^\n]*\n)+",
            "\n",
            text,
        )
        compose_path.write_text(text, encoding="utf-8")
    elif fixture == "missing-ssd":
        text = compose_path.read_text(encoding="utf-8")
        text = text.replace("ATP_SSD_DATA_DIR", "ATP_REMOVED_FOR_FIXTURE")
        compose_path.write_text(text, encoding="utf-8")
    elif fixture == "missing-portability-doc":
        (temp_root / "docs" / "DEPLOYMENT.md").write_text(
            "# Deployment\n\nTBD.\n", encoding="utf-8"
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
            "missing-jupyter",
            "missing-ssd",
            "missing-portability-doc",
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
        evidence = assert_deployment_static(config, root)
    except DeploymentCheckError as error:
        print(f"SRS-ARCH-004 FAIL: {error}", file=sys.stderr)
        return 1
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    print("SRS-ARCH-004 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
