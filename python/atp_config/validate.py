"""Startup configuration validator for SRS-ARCH-005.

``load_and_validate`` consumes a mapping of environment variables and returns a
:class:`ReadinessReport` whose failures are field-addressable and JSON-serialisable.
The function is pure: no filesystem or network access. The catalogue itself is
loaded once at import time from ``architecture/runtime_services.json``.
"""

from __future__ import annotations

import ipaddress
from collections import defaultdict
from collections.abc import Mapping

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


#: The three RFC 1918 blocks, exactly as the Rust adapter's `Ipv4Addr::is_private`
#: defines them — no more (see :func:`_is_private_egress_address`).
_RFC1918_V4 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
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


#: Named character sets a key's validator may require. Kept as names rather than
#: raw regexes so the catalogue stays declarative and reviewable.
_CHARSETS = {
    # ntfy's own topic alphabet. The transport enforces this too, because the
    # topic is interpolated into the HTTP request line: a `/`, `?`, `#` or a
    # space would retarget or split the request.
    "alnum_dash_underscore": lambda value: all(
        c.isascii() and (c.isalnum() or c in "-_") for c in value
    ),
}


def _validate_charset(spec: KeySpec, raw: str) -> ReadinessFailure | None:
    """Enforce a declared character set WITHOUT echoing the value.

    The reason string reaches logs and the dashboard, and this check also runs on
    secret keys (``ATP_PUSH_TOPIC`` is a publish credential), so it names the key
    and states the rule but never prints what was configured — the same
    discipline the transport applies to its own rejections.
    """

    name = spec.validator.get("charset")
    if name is None:
        return None
    allowed = _CHARSETS.get(name)
    if allowed is None:
        return _fail(spec, Severity.ERROR, f"unknown charset {name!r} in the catalogue")
    if not allowed(raw):
        return _fail(
            spec,
            Severity.ERROR,
            "value contains characters outside the permitted set "
            "(ASCII letters, digits, '-' and '_'); the transport interpolates it "
            "into an HTTP request line, so a '/', '?', '#', space or control "
            "character would retarget or split the request",
        )
    return None


def _validate_secret(spec: KeySpec, raw: str, atp_env: str | None) -> ReadinessFailure | None:
    # `strip()`, not just a length check: a whitespace-only secret is unusable,
    # and every consumer already treats it that way — `PushConfig::new` and
    # `SmtpRelayConfig::new` both reject `trim().is_empty()`. Accepting it here
    # made startup validation assert a channel was configured when the transport
    # would refuse it, so the operator would learn the alert path was broken from
    # the alert that never arrived.
    if spec.validator.get("non_empty", True) and not raw.strip():
        return _fail(spec, Severity.ERROR, "secret is empty")
    # Shape before placeholder-severity: a malformed value is an ERROR in every
    # environment, and reporting it as a mere development-mode warning would let
    # a broken alert endpoint reach staging.
    if raw != PLACEHOLDER_VALUE:
        shape = _validate_charset(spec, raw)
        if shape is not None:
            return shape
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


def _is_private_egress_address(address: ipaddress._BaseAddress) -> bool:
    """Mirror of ``atp_adapters``' ``is_private_egress_address`` (Rust).

    Written out rather than delegated to :attr:`ipaddress.IPv4Address.is_private`
    because Python's definition is BROADER than the adapter's: it also counts
    169.254.0.0/16, 100.64.0.0/10 and 192.0.0.0/24 as private. Accepting a host
    here that the adapter refuses at send time would reproduce the very bug this
    validator exists to catch, only inverted — so the two policies are kept
    literally identical: loopback, plus RFC 1918 for v4 and unique-local
    (fc00::/7) for v6. Link-local is deliberately EXCLUDED from both:
    169.254.169.254 is the cloud metadata endpoint, and the transports send a
    credential immediately after connect.
    """

    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return _is_private_egress_address(mapped)
        return address.is_loopback or (int(address) >> 121) == 0b1111110
    return address.is_loopback or any(address in net for net in _RFC1918_V4)


def _validate_host(spec: KeySpec, raw: str) -> ReadinessFailure | None:
    if spec.validator.get("non_empty", True) and not raw.strip():
        return _fail(spec, Severity.ERROR, "host is empty")

    # Keys whose transport refuses non-private egress must be rejected HERE, at
    # startup, not at send time. A notification endpoint that only fails when it
    # is finally used fails during the incident it was meant to report — the
    # operator learns the alert path is broken from the alert that never came.
    if spec.validator.get("private_egress"):
        try:
            address = ipaddress.ip_address(raw.strip())
        except ValueError:
            # A HOSTNAME IS REFUSED OUTRIGHT, rather than deferred.
            #
            # Deferring it was the first attempt and it left the hole this check
            # exists to close: `ntfy.example.com` would pass readiness and then
            # be refused at send time, so the operator learns the alert path is
            # broken from the alert that never came. Resolving it here is not the
            # alternative — `load_and_validate` is pure by contract (no
            # filesystem, no network), and a name checked once at startup could
            # resolve somewhere else by the time an alert is dispatched anyway.
            #
            # Requiring a literal makes the property STATIC and decidable. The
            # cost is that this endpoint cannot be named by DNS or by a compose
            # service name; that is the right trade for an alert endpoint on a
            # LAN with a fixed address, and it matches how the live integration
            # environment is already configured. The adapter still re-resolves
            # and re-validates on every connect — this is an additional gate,
            # not a replacement for it.
            return _fail(
                spec,
                Severity.ERROR,
                f"host {raw.strip()!r} must be an IP address literal, not a "
                "hostname: this endpoint is restricted to private egress, and a "
                "name cannot be shown to stay private without resolving it",
            )
        if not _is_private_egress_address(address):
            return _fail(
                spec,
                Severity.ERROR,
                f"host {raw.strip()!r} is not a loopback or private (RFC 1918 / "
                "unique-local) address; this endpoint may not be reached over a "
                "public network",
            )
    return None


def _validate_string(spec: KeySpec, raw: str) -> ReadinessFailure | None:
    """Free-form non-secret string (e.g. a notification destination address).

    Deliberately only a non-empty check: this validator must not encode a
    format (an RFC 5322 address grammar, a handset number) that would reject a
    destination the transport itself accepts. The transport owns the format.
    """

    if spec.validator.get("non_empty", True) and not raw.strip():
        return _fail(spec, Severity.ERROR, "value is empty")
    return None


_VALIDATORS = {
    KeyType.INT: _validate_int,
    KeyType.FLOAT: _validate_float,
    KeyType.PATH: _validate_path,
    KeyType.ENUM: _validate_enum,
    KeyType.HOST: _validate_host,
    KeyType.STRING: _validate_string,
}


def _resolve_atp_env(env: Mapping[str, str]) -> str | None:
    raw = env.get("ATP_ENV")
    if raw is None or raw == "":
        return None
    return raw


def load_and_validate(env: Mapping[str, str], *, atp_env: str | None = None) -> ReadinessReport:
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
            failures.append(_fail(spec, Severity.ERROR, f"required key {spec.name} is not set"))
            continue

        if spec.type is KeyType.SECRET:
            failure = _validate_secret(spec, raw, effective_env)
        else:
            failure = _VALIDATORS[spec.type](spec, raw)

        if failure is not None:
            failures.append(failure)

    evidence = _build_evidence(failures, effective_env)
    return ReadinessReport(failures=failures, evidence=evidence)


def _build_evidence(failures: list[ReadinessFailure], atp_env: str | None) -> list[str]:
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


def secret_values(env: Mapping[str, str]) -> set[str]:
    """Return the live plaintext values of the catalogued ``secret`` keys.

    For every SRS-ARCH-005 catalogue key flagged ``secret`` (IB account, SMTP,
    push, and the vendor data-provider keys), include its value from ``env`` when
    present and not the placeholder. The SRS-SEC-001 log-redaction layer uses
    this to learn exactly which plaintext credentials must never reach a log.
    """

    out: set[str] = set()
    for spec in REQUIRED_KEYS:
        if not spec.secret:
            continue
        value = env.get(spec.name)
        if value and value != PLACEHOLDER_VALUE:
            out.add(value)
    return out


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
    "secret_values",
]
