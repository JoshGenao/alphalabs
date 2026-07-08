"""ATP configuration system contract surface (SRS-ARCH-005).

This package is the declarative source of truth for required configuration
keys, their validation rules, and the structured readiness-report shape
that ``tools/config_check.py`` and (later) the Rust orchestrator consume.

See ``python/atp_config/README.md`` for the operator-facing summary.
"""

from .schema import (
    CATEGORIES,
    PLACEHOLDER_VALUE,
    PRODUCTION_ENVS,
    REQUIRED_KEYS,
    Category,
    KeySpec,
    KeyType,
    ReadinessFailure,
    ReadinessReport,
    Severity,
    load_catalogue,
)
from .validate import (
    load_and_validate,
    merge_env,
    parse_env_example,
    render_failures,
    secret_values,
)

# NOTE: the SRS-SEC-001 credential vault lives in ``atp_config.vault`` and is
# imported from there directly (``from atp_config.vault import CredentialVault``).
# It is deliberately kept OUT of this package ``__init__`` so that importing
# ``atp_config`` for plain env validation does not require ``cryptography`` —
# only the vault feature does.

__all__ = [
    "CATEGORIES",
    "Category",
    "KeySpec",
    "KeyType",
    "PLACEHOLDER_VALUE",
    "PRODUCTION_ENVS",
    "ReadinessFailure",
    "ReadinessReport",
    "REQUIRED_KEYS",
    "Severity",
    "load_and_validate",
    "load_catalogue",
    "merge_env",
    "parse_env_example",
    "render_failures",
    "secret_values",
]
