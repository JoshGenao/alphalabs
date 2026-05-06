"""Startup configuration validator for SRS-ARCH-005.

``load_and_validate`` consumes a mapping of environment variables and returns a
:class:`ReadinessReport` whose failures are field-addressable and JSON-serialisable.
The function is pure: no filesystem or network access. The catalogue itself is
loaded once at import time from ``architecture/runtime_services.json``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from .schema import (
    PLACEHOLDER_VALUE,
    PRODUCTION_ENVS,
    REQUIRED_KEYS,
    Category,
    KeySpec,
    KeyType,
    ReadinessFailure,
    ReadinessReport,
    Severity,
)


def _fail(spec: KeySpec, severity: Severity, reason: str) -> ReadinessFailure:
    return ReadinessFailure(
        key=spec.name,
        category=spec.category,
        severity=severity,
        reason=reason,
        srs_trace=spec.srs_trace,
    )


def _validate_int(spec: KeySpec, raw: str) -> ReadinessFailure | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _fail(spec, Severity.ERROR, f"expected integer, got {raw!r}")
    bounds = spec.validator
    if "min" in bounds and value < bounds["min"]:
        return _fail(spec, Severity.ERROR, f"value {value} is below min {bounds['min']}")
    if "max" in bounds and value > bounds["max"]:
        return _fail(spec, Severity.ERROR, f"value {value} is above max {bounds['max']}")
    return None


def _validate_float(spec: KeySpec, raw: str) -> ReadinessFailure | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _fail(spec, Severity.ERROR, f"expected float, got {raw!r}")
    bounds = spec.validator
    if "min" in bounds and value < bounds["min"]:
        return _fail(spec, Severity.ERROR, f"value {value} is below min {bounds['min']}")
    if "max" in bounds and value > bounds["max"]:
        return _fail(spec, Severity.ERROR, f"value {value} is above max {bounds['max']}")
    return None


def _validate_path(spec: KeySpec, raw: str) -> ReadinessFailure | None:
    if spec.validator.get("non_empty", True) and not raw.strip():
        return _fail(spec, Severity.ERROR, "path is empty")
    if spec.validator.get("absolute") and not raw.startswith("/"):
        return _fail(
            spec,
            Severity.ERROR,
            f"path must be absolute (start with '/'); got {raw!r}",
        )
    return None


def _validate_secret(
    spec: KeySpec, raw: str, atp_env: str | None
) -> ReadinessFailure | None:
    if spec.validator.get("non_empty", True) and not raw:
        return _fail(spec, Severity.ERROR, "secret is empty")
    if raw == PLACEHOLDER_VALUE:
        if atp_env in PRODUCTION_ENVS:
            return _fail(
                spec,
                Severity.ERROR,
                f"placeholder secret value {PLACEHOLDER_VALUE!r} is not "
                f"permitted when ATP_ENV={atp_env!r}",
            )
        return _fail(
            spec,
            Severity.WARNING,
            f"placeholder secret value {PLACEHOLDER_VALUE!r} present "
            f"(allowed in development; replace before staging/production)",
        )
    return None


def _validate_enum(spec: KeySpec, raw: str) -> ReadinessFailure | None:
    choices = spec.validator.get("choices", ())
    if choices and raw not in choices:
        return _fail(
            spec,
            Severity.ERROR,
            f"value {raw!r} not in allowed choices {list(choices)}",
        )
    return None


def _validate_host(spec: KeySpec, raw: str) -> ReadinessFailure | None:
    if spec.validator.get("non_empty", True) and not raw.strip():
        return _fail(spec, Severity.ERROR, "host is empty")
    return None


_VALIDATORS = {
    KeyType.INT: _validate_int,
    KeyType.FLOAT: _validate_float,
    KeyType.PATH: _validate_path,
    KeyType.ENUM: _validate_enum,
    KeyType.HOST: _validate_host,
}


def _resolve_atp_env(env: Mapping[str, str]) -> str | None:
    raw = env.get("ATP_ENV")
    if raw is None or raw == "":
        return None
    return raw


def load_and_validate(
    env: Mapping[str, str], *, atp_env: str | None = None
) -> ReadinessReport:
    """Validate ``env`` against the SRS-ARCH-005 key catalogue.

    Returns a :class:`ReadinessReport`. ``ok`` is false when any error-severity
    failure is present. Warnings (e.g. placeholder secrets in development) do
    not flip ``ok`` but are still surfaced. ``atp_env`` overrides the env-vars
    own ``ATP_ENV`` when provided; this is the production-mode escalation hook.
    """

    failures: list[ReadinessFailure] = []
    effective_env = atp_env if atp_env is not None else _resolve_atp_env(env)

    for spec in REQUIRED_KEYS:
        raw = env.get(spec.name)
        if raw is None:
            failures.append(
                _fail(spec, Severity.ERROR, f"required key {spec.name} is not set")
            )
            continue

        if spec.type is KeyType.SECRET:
            failure = _validate_secret(spec, raw, effective_env)
        else:
            failure = _VALIDATORS[spec.type](spec, raw)

        if failure is not None:
            failures.append(failure)

    evidence = _build_evidence(failures, effective_env)
    return ReadinessReport(failures=failures, evidence=evidence)


def _build_evidence(
    failures: list[ReadinessFailure], atp_env: str | None
) -> list[str]:
    by_category: dict[Category, list[KeySpec]] = defaultdict(list)
    for spec in REQUIRED_KEYS:
        by_category[spec.category].append(spec)

    error_keys = {f.key for f in failures if f.severity is Severity.ERROR}
    warning_keys = {f.key for f in failures if f.severity is Severity.WARNING}

    evidence: list[str] = [
        "SRS-ARCH-005 configuration system evidence:",
        f"{len(REQUIRED_KEYS)} keys catalogued across "
        f"{len(by_category)} categories (ATP_ENV={atp_env or 'unset'!r})",
    ]
    for category in Category:
        specs = by_category.get(category, [])
        if not specs:
            continue
        cat_errors = sum(1 for s in specs if s.name in error_keys)
        cat_warnings = sum(1 for s in specs if s.name in warning_keys)
        status = (
            f"{len(specs)} keys"
            + (f", {cat_errors} error" if cat_errors else "")
            + (f", {cat_warnings} warning" if cat_warnings else "")
            + (" — OK" if not cat_errors and not cat_warnings else "")
        )
        names = ", ".join(s.name for s in specs)
        evidence.append(f"{category.value}: {status} ({names})")
    return evidence


def render_failures(report: ReadinessReport) -> str:
    """Render structured failures as newline-separated JSON lines for stderr."""

    return report.as_json_lines()


def parse_env_example(text: str) -> dict[str, str]:
    """Parse a ``.env``-style file into a mapping.

    Used by ``tools/config_check.py`` to source defaults from
    ``.env.example`` when the process env is empty (e.g., outside the dev
    shell). Comments and blank lines are skipped; values are not unquoted.
    """

    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        out[key.strip()] = value.strip()
    return out


def merge_env(*sources: Mapping[str, str]) -> dict[str, str]:
    """Right-precedence merge: later sources override earlier ones."""

    merged: dict[str, str] = {}
    for source in sources:
        for key, value in source.items():
            if value is None or value == "":
                continue
            merged[key] = value
    return merged


__all__ = [
    "load_and_validate",
    "merge_env",
    "parse_env_example",
    "render_failures",
]
