#!/usr/bin/env python3
"""Configuration system check for SRS-ARCH-005.

Validates that every required configuration key is documented in
``architecture/runtime_services.json``, that the ``atp_config`` validator
accepts the development defaults captured in ``.env.example``, and that
each negative fixture produces a structured readiness failure.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from atp_config import (  # noqa: E402  (path manipulation must come first)
    REQUIRED_KEYS,
    Category,
    ReadinessReport,
    Severity,
    load_and_validate,
    merge_env,
    parse_env_example,
)

ENV_EXAMPLE_PATH = ROOT / ".env.example"


class ConfigCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise ConfigCheckError(message)


def _catalogue_defaults() -> dict[str, str]:
    return {spec.name: spec.default for spec in REQUIRED_KEYS if spec.default is not None}


def _env_example_defaults(env_example_path: Path = ENV_EXAMPLE_PATH) -> dict[str, str]:
    if not env_example_path.exists():
        fail(f"missing env template: {env_example_path}")
    return parse_env_example(env_example_path.read_text(encoding="utf-8"))


def _process_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key in {spec.name for spec in REQUIRED_KEYS}
    }


def build_default_env(env_example_path: Path = ENV_EXAMPLE_PATH) -> dict[str, str]:
    """Layer process env over .env.example over catalogue defaults.

    The check passes from a clean shell because every key has a catalogue
    default. Operator-supplied values in the process env take precedence.
    """

    return merge_env(_catalogue_defaults(), _env_example_defaults(env_example_path), _process_env())


def assert_env_example_lists_all_keys(env_example_path: Path = ENV_EXAMPLE_PATH) -> str:
    parsed = _env_example_defaults(env_example_path)
    missing = sorted({spec.name for spec in REQUIRED_KEYS} - set(parsed))
    if missing:
        fail(f".env.example does not document required keys: {', '.join(missing)}")
    return f".env.example documents all {len(REQUIRED_KEYS)} catalogued keys"


def assert_validator_accepts_defaults(env: Mapping[str, str]) -> ReadinessReport:
    report = load_and_validate(env)
    if not report.ok:
        errors = "; ".join(f"{f.key}: {f.reason}" for f in report.errors)
        fail(f"validator rejected default env: {errors}")
    return report


def assert_configuration_static(_config: dict, root: Path = ROOT) -> list[str]:
    """Aggregator entry point used by ``tools/architecture_check.py``."""

    env_example_path = root / ".env.example"
    env_example_evidence = assert_env_example_lists_all_keys(env_example_path)
    env = build_default_env(env_example_path)
    report = assert_validator_accepts_defaults(env)
    return [env_example_evidence, *report.evidence]


def _apply_fixture(fixture: str, env: dict[str, str]) -> dict[str, str]:
    mutated = dict(env)
    if fixture == "missing-credential":
        mutated.pop("DATABENTO_API_KEY", None)
    elif fixture == "placeholder-secret-in-production":
        mutated["ATP_ENV"] = "production"
        # secrets stay at the placeholder default
    elif fixture == "invalid-line-limit":
        mutated["ATP_MARKET_DATA_LINE_LIMIT"] = "not-a-number"
    elif fixture == "missing-resource-limit":
        mutated.pop("ATP_LIVE_STRATEGY_MEM_MB", None)
    elif fixture == "invalid-storage-path":
        mutated["ATP_SSD_DATA_DIR"] = ""
    else:
        raise ValueError(f"unknown fixture: {fixture}")
    return mutated


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        choices=[
            "missing-credential",
            "placeholder-secret-in-production",
            "invalid-line-limit",
            "missing-resource-limit",
            "invalid-storage-path",
        ],
        help="Run the validator against a synthetic env with one violation.",
    )
    return parser.parse_args(argv)


def _render_failure_summary(report: ReadinessReport) -> str:
    parts: list[str] = []
    for failure in report.failures:
        if failure.severity is not Severity.ERROR:
            continue
        parts.append(failure.as_json_line())
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    try:
        env_example_evidence = assert_env_example_lists_all_keys()
        env = build_default_env()
        if args.fixture:
            env = _apply_fixture(args.fixture, env)
            report = load_and_validate(env)
            if report.ok:
                fail(f"fixture {args.fixture!r} did not produce a readiness failure")
            summary = _render_failure_summary(report)
            print(
                f"SRS-ARCH-005 FAIL: fixture {args.fixture!r} produced "
                f"{len(report.errors)} readiness error(s)\n{summary}",
                file=sys.stderr,
            )
            return 1
        report = assert_validator_accepts_defaults(env)
    except ConfigCheckError as error:
        print(f"SRS-ARCH-005 FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-ARCH-005 PASS")
    print(f"- {env_example_evidence}")
    for item in report.evidence:
        print(f"- {item}")
    if report.warnings:
        print(
            f"- {len(report.warnings)} non-blocking warning(s): "
            + ", ".join(f"{w.key}({w.reason.split(' present')[0]})" for w in report.warnings)
        )
    # Cross-check: every category in the catalogue is exercised.
    declared = {spec.category for spec in REQUIRED_KEYS}
    missing_cats = sorted(set(Category) - declared)
    if missing_cats:
        print(
            f"SRS-ARCH-005 FAIL: catalogue is missing categories: "
            f"{', '.join(c.value for c in missing_cats)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
