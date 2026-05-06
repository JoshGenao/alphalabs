"""Configuration catalogue and readiness-report data shapes (SRS-ARCH-005).

The catalogue is loaded from ``architecture/runtime_services.json`` so the JSON
file remains the single source of truth. Validation logic lives in
``validate.py``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


_PACKAGE_DIR = Path(__file__).resolve().parent
_RUNTIME_SERVICES_PATH = (
    _PACKAGE_DIR.parents[1] / "architecture" / "runtime_services.json"
)


class Category(str, Enum):
    """The six SRS-ARCH-005 configuration categories."""

    CREDENTIALS = "credentials"
    STORAGE_PATHS = "storage_paths"
    IB_ACCOUNT = "ib_account"
    MARKET_DATA_LIMITS = "market_data_limits"
    RESOURCE_LIMITS = "resource_limits"
    NOTIFICATION_CHANNELS = "notification_channels"


class KeyType(str, Enum):
    """Validator types supported by the SRS-ARCH-005 catalogue."""

    INT = "int"
    FLOAT = "float"
    PATH = "path"
    SECRET = "secret"
    ENUM = "enum"
    HOST = "host"


class Severity(str, Enum):
    """Severity of a structured readiness failure."""

    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class KeySpec:
    """Declarative spec for one configuration key.

    Loaded from the ``configuration.keys`` array in
    ``architecture/runtime_services.json``.
    """

    name: str
    category: Category
    type: KeyType
    validator: dict[str, Any]
    default: str | None
    secret: bool
    srs_trace: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "KeySpec":
        return cls(
            name=raw["name"],
            category=Category(raw["category"]),
            type=KeyType(raw["type"]),
            validator=dict(raw.get("validator", {})),
            default=raw.get("default"),
            secret=bool(raw.get("secret", False)),
            srs_trace=tuple(raw.get("srs_trace", ())),
        )


@dataclass(frozen=True)
class ReadinessFailure:
    """One structured readiness failure entry.

    Acceptance-criteria field shape per SRS-ARCH-005: every failure carries an
    addressable ``key``, ``category``, ``severity``, ``reason``, and SRS trace.
    """

    key: str
    category: Category
    severity: Severity
    reason: str
    srs_trace: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["category"] = self.category.value
        data["severity"] = self.severity.value
        data["srs_trace"] = list(self.srs_trace)
        return data

    def as_json_line(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True)


@dataclass
class ReadinessReport:
    """Aggregate result of ``load_and_validate``.

    ``ok`` is true only when there are no error-severity failures. Warnings do
    not flip ``ok`` to false (they are surfaced for operator awareness without
    holding the system out of pre-trade state).
    """

    failures: list[ReadinessFailure] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(f.severity is Severity.ERROR for f in self.failures)

    @property
    def errors(self) -> list[ReadinessFailure]:
        return [f for f in self.failures if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[ReadinessFailure]:
        return [f for f in self.failures if f.severity is Severity.WARNING]

    def as_json_lines(self) -> str:
        return "\n".join(f.as_json_line() for f in self.failures)


def load_catalogue(path: Path | None = None) -> dict[str, Any]:
    """Read the ``configuration`` block from runtime_services.json."""

    config_path = path or _RUNTIME_SERVICES_PATH
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if "configuration" not in raw:
        raise RuntimeError(
            f"runtime_services.json at {config_path} is missing the "
            "'configuration' block (SRS-ARCH-005)"
        )
    return raw["configuration"]


def _build_required_keys() -> tuple[KeySpec, ...]:
    catalogue = load_catalogue()
    return tuple(KeySpec.from_dict(item) for item in catalogue["keys"])


REQUIRED_KEYS: tuple[KeySpec, ...] = _build_required_keys()


CATEGORIES: tuple[Category, ...] = tuple(Category)


PLACEHOLDER_VALUE: str = load_catalogue().get(
    "placeholder_value", "placeholder-set-in-environment"
)


PRODUCTION_ENVS: frozenset[str] = frozenset(
    load_catalogue().get("production_envs", ["staging", "production"])
)
