"""``ReadinessGate`` — pre-trade hold state machine for ERR-9 / SRS-MD-006.

The gate consumes the structured :class:`atp_config.ReadinessReport` produced
by :func:`atp_config.load_and_validate` and exposes:

* A discrete :class:`GateState` — ``INITIALIZING`` -> (``PRE_TRADE_BLOCKED``
  | ``READY``); ``PRE_TRADE_BLOCKED`` may transition to ``READY`` (on a
  re-evaluation that clears the errors) or to ``OVERRIDDEN`` (on a
  fully-audited operator override).
* Structured payload renderers (``as_dashboard_payload``, ``as_log_records``)
  that the downstream SRS-LOG-001 / SRS-UI-001 / SRS-API-001 surfaces consume
  when they land.
* An ``assert_ready_or_hold`` gate that raises
  :class:`atp_readiness.errors.PreTradeHoldError` while the state is held —
  the executable signal for service boot scripts.

The runtime readiness checks ERR-9 also references (IB connectivity, IB
account auth, SSD data layer access, ingestion freshness within one trading
day, system service health, NAS reachability) live in SRS-MD-006 and are
DEFERRED here; the gate's contract surface is the seam those checks will
plug into.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from atp_config import (
    PLACEHOLDER_VALUE,
    PRODUCTION_ENVS,
    REQUIRED_KEYS,
    Category,
    ReadinessFailure,
    ReadinessReport,
    Severity,
    load_and_validate,
)
from atp_config.vault import VAULT_FILE_ENV, VaultError, load_vault_into_env

from .errors import (
    GateTransitionError,
    OverrideAuditError,
    PreTradeHoldError,
    ReadinessGateError,
)
from .override import OperatorOverride


class GateState(StrEnum):
    """Discrete states of the startup readiness gate.

    * ``INITIALIZING`` — before any readiness evaluation has been recorded.
    * ``PRE_TRADE_BLOCKED`` — at least one error-severity failure is present;
      live and paper order submission must be held per ERR-9.
    * ``READY`` — the SDK-surface half of readiness passes; the runtime half
      (SRS-MD-006) still gates live trading.
    * ``OVERRIDDEN`` — an operator has released the pre-trade hold with a
      fully-audited :class:`OperatorOverride`; SRS-LOG-001 + SRS-NOTIF-001
      consume the audit-trail record when they land.
    """

    INITIALIZING = "initializing"
    PRE_TRADE_BLOCKED = "pre_trade_blocked"
    READY = "ready"
    OVERRIDDEN = "overridden"


_ALLOWED_TRANSITIONS: frozenset[tuple[GateState, GateState]] = frozenset(
    {
        (GateState.INITIALIZING, GateState.PRE_TRADE_BLOCKED),
        (GateState.INITIALIZING, GateState.READY),
        (GateState.PRE_TRADE_BLOCKED, GateState.PRE_TRADE_BLOCKED),
        (GateState.PRE_TRADE_BLOCKED, GateState.READY),
        (GateState.PRE_TRADE_BLOCKED, GateState.OVERRIDDEN),
        (GateState.READY, GateState.PRE_TRADE_BLOCKED),
        (GateState.READY, GateState.READY),
        (GateState.OVERRIDDEN, GateState.PRE_TRADE_BLOCKED),
        (GateState.OVERRIDDEN, GateState.READY),
    }
)
"""Allowed (from_state, to_state) transitions.

The set is intentionally enumerated rather than computed so the contract
metadata block (``architecture/runtime_services.json#startup_readiness_gate_contract.allowed_transitions``)
and the gate stay in pinned parity — the L3 contract test cross-checks this
set against the JSON catalogue.
"""


_OVERRIDE_REQUIRED_FIELDS: tuple[str, ...] = (
    "actor",
    "reason",
    "audit_trail_id",
    "timestamp_ns",
)
"""SRS-MD-006 audit-trail fields the gate enforces on every operator override."""


class ReadinessGate:
    """State machine enforcing the ERR-9 pre-trade hold contract.

    Construct via :meth:`ReadinessGate.from_env` (the typical boot path) or
    via the default constructor for tests. The gate is single-threaded by
    design: every transition method assumes the caller owns the gate.
    """

    def __init__(self) -> None:
        self._state: GateState = GateState.INITIALIZING
        self._report: ReadinessReport | None = None
        # The STATIC (SRS-ARCH-005) half of the most recent evaluation, kept
        # separately from the merged report so a runtime fold never feeds a
        # previous fold's failures back into the next merge (runtime failures
        # must clear when their probes recover).
        self._static_report: ReadinessReport | None = None
        self._overrides: list[OperatorOverride] = []

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, atp_env: str | None = None) -> ReadinessGate:
        """Build a gate seeded with the result of the SRS-ARCH-005 validator.

        When ``ATP_VAULT_FILE`` is set, the SRS-SEC-001 encrypted credential
        vault is decrypted and overlaid onto ``env`` first (so an operator can
        supply secrets encrypted-at-rest instead of as plaintext env vars);
        a vault that cannot be opened fails **closed** to ``PRE_TRADE_BLOCKED``
        with a structured failure rather than trading on missing credentials.
        Then :func:`atp_config.load_and_validate` runs and transitions
        ``INITIALIZING`` -> ``PRE_TRADE_BLOCKED`` (any error-severity failure)
        or ``INITIALIZING`` -> ``READY``.
        """

        _assert_env_is_mapping(env)
        gate = cls()
        gate._apply_report(_load_report(env, atp_env))
        gate._static_report = gate._report
        return gate

    # ------------------------------------------------------------------ #
    # Public accessors
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> GateState:
        """Current gate state."""

        return self._state

    @property
    def report(self) -> ReadinessReport:
        """Latest readiness report.

        Raises :class:`RuntimeError` when accessed before any evaluation has
        seeded the gate (i.e. while ``state`` is still ``INITIALIZING``).
        """

        if self._report is None:
            raise RuntimeError("ReadinessGate has no report yet; call from_env or reevaluate first")
        return self._report

    @property
    def overrides(self) -> tuple[OperatorOverride, ...]:
        """Immutable snapshot of the recorded operator overrides, oldest first."""

        return tuple(self._overrides)

    # ------------------------------------------------------------------ #
    # Transitions
    # ------------------------------------------------------------------ #

    def reevaluate(self, env: Mapping[str, str], *, atp_env: str | None = None) -> None:
        """Re-run the SRS-ARCH-005 validator and apply the new report.

        Allowed source states: any state except ``INITIALIZING`` (the gate
        must have been seeded by :meth:`from_env` first). Re-evaluation does
        not clear recorded operator overrides — the audit trail is durable.
        """

        if self._state is GateState.INITIALIZING:
            raise GateTransitionError(
                "reevaluate requires a seeded gate; call ReadinessGate.from_env first"
            )
        _assert_env_is_mapping(env)
        self._apply_report(_load_report(env, atp_env))
        self._static_report = self._report

    def operator_override(self, override: OperatorOverride) -> None:
        """Release the pre-trade hold with a fully-audited operator override.

        Allowed source state: ``PRE_TRADE_BLOCKED`` only. The override must
        be a real :class:`OperatorOverride` instance with non-empty
        ``actor`` / ``reason`` / ``audit_trail_id`` and a finite,
        non-negative integer ``timestamp_ns``. ``bool`` is rejected on
        ``timestamp_ns`` because Python treats it as an ``int`` subclass.
        Successful overrides are appended to the gate's audit log in the
        order they were issued.
        """

        if not isinstance(override, OperatorOverride):
            raise OverrideAuditError(
                f"operator_override expected an OperatorOverride; got {type(override).__name__}"
            )

        self._assert_override_audit_complete(override)
        self._assert_transition(self._state, GateState.OVERRIDDEN)
        self._overrides.append(override)
        self._state = GateState.OVERRIDDEN

    def assert_ready_or_hold(self) -> None:
        """Raise :class:`PreTradeHoldError` when the gate is held.

        ``READY`` and ``OVERRIDDEN`` pass; ``PRE_TRADE_BLOCKED`` raises;
        ``INITIALIZING`` raises because a gate that has not yet been
        evaluated cannot be considered ready.
        """

        if self._state is GateState.INITIALIZING:
            raise GateTransitionError(
                "ReadinessGate is INITIALIZING; call from_env or reevaluate first"
            )
        if self._state is GateState.PRE_TRADE_BLOCKED:
            raise PreTradeHoldError(self.report)

    def assert_runtime_ready_or_hold(self, runtime_report: ReadinessReport) -> None:
        """Fold a RUNTIME readiness report into the gate, then assert ready.

        SRS-MD-006 / SyRS SYS-76: the runtime readiness probes
        (:mod:`atp_readiness.runtime` builds their ``ReadinessReport``; the
        probe adapters live outside this SDK module by contract) share the
        SAME pre-trade state machine as the static configuration half. The
        current static report and ``runtime_report`` are merged — failures
        and evidence concatenated — and applied through the pinned
        transition table, so an error-severity runtime failure holds the
        gate exactly as a static one does, and an override remains durable
        across re-evaluations. Requires a seeded gate.

        Raises:
            GateTransitionError: the gate is still ``INITIALIZING``.
            PreTradeHoldError: the merged report carries error-severity
                failures — the system holds in the pre-trade state.
        """

        if self._state is GateState.INITIALIZING:
            raise GateTransitionError(
                "assert_runtime_ready_or_hold requires a seeded gate; call "
                "ReadinessGate.from_env first"
            )
        if not isinstance(runtime_report, ReadinessReport):
            raise ReadinessGateError(
                "assert_runtime_ready_or_hold requires a ReadinessReport; got "
                f"{type(runtime_report).__name__}"
            )
        static_report = self._static_report if self._static_report is not None else self.report
        combined = ReadinessReport(
            failures=list(static_report.failures) + list(runtime_report.failures),
            evidence=list(static_report.evidence) + list(runtime_report.evidence),
        )
        self._apply_report(combined)
        self.assert_ready_or_hold()

    # ------------------------------------------------------------------ #
    # Structured payload renderers
    # ------------------------------------------------------------------ #

    def as_log_records(self) -> list[dict[str, Any]]:
        """Render the gate state as the JSON-line payload SRS-LOG-001 consumes.

        Each record carries ``timestamp_ns``, ``severity``, ``key``,
        ``category``, ``reason``, and ``srs_trace`` (the fields enumerated in
        ``architecture/runtime_services.json#startup_readiness_gate_contract.required_log_record_fields``).
        Overrides are emitted as ``severity='override'`` records so the same
        log surface can thread them into the audit log when SRS-LOG-001
        lands.
        """

        timestamp_ns = time.time_ns()
        records: list[dict[str, Any]] = []
        for failure in self.report.failures:
            records.append(
                {
                    "timestamp_ns": timestamp_ns,
                    "severity": failure.severity.value,
                    "key": failure.key,
                    "category": failure.category.value,
                    "reason": failure.reason,
                    "srs_trace": list(failure.srs_trace),
                }
            )
        for override in self._overrides:
            records.append(
                {
                    "timestamp_ns": override.timestamp_ns,
                    "severity": "override",
                    "key": "operator_override",
                    "category": "pre_trade_release",
                    "reason": override.reason,
                    "srs_trace": ["ERR-9", "SRS-MD-006", "SRS-LOG-001"],
                    "actor": override.actor,
                    "audit_trail_id": override.audit_trail_id,
                }
            )
        return records

    def as_dashboard_payload(self) -> dict[str, Any]:
        """Render the gate state as the JSON document SRS-UI-001 / SRS-API-001 consume.

        Fields enumerated in
        ``architecture/runtime_services.json#startup_readiness_gate_contract.required_dashboard_payload_fields``.
        ``ok`` is true when the gate has reached ``READY`` or ``OVERRIDDEN``;
        ``state`` is always the current :class:`GateState` value.
        """

        report = self.report
        return {
            "state": self._state.value,
            "ok": self._state in (GateState.READY, GateState.OVERRIDDEN),
            "errors": [f.as_dict() for f in report.errors],
            "warnings": [f.as_dict() for f in report.warnings],
            "evidence": list(report.evidence),
            "overrides": [o.as_dict() for o in self._overrides],
        }

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _apply_report(self, report: ReadinessReport) -> None:
        target = GateState.READY if not _has_error(report) else GateState.PRE_TRADE_BLOCKED
        self._assert_transition(self._state, target)
        self._report = report
        self._state = target

    def _assert_transition(self, from_state: GateState, to_state: GateState) -> None:
        if (from_state, to_state) not in _ALLOWED_TRANSITIONS:
            raise GateTransitionError(
                f"forbidden gate transition: {from_state.value} -> {to_state.value}"
            )

    def _assert_override_audit_complete(self, override: OperatorOverride) -> None:
        for field_name in _OVERRIDE_REQUIRED_FIELDS:
            if not hasattr(override, field_name):
                raise OverrideAuditError(
                    f"OperatorOverride is missing required audit field {field_name!r}"
                )
        for field_name in ("actor", "reason", "audit_trail_id"):
            value = getattr(override, field_name)
            if not isinstance(value, str):
                raise OverrideAuditError(
                    f"OperatorOverride.{field_name} must be a non-empty string; "
                    f"got {type(value).__name__}"
                )
            if not value.strip():
                raise OverrideAuditError(f"OperatorOverride.{field_name} must be non-empty")
        timestamp_ns = override.timestamp_ns
        if isinstance(timestamp_ns, bool) or not isinstance(timestamp_ns, int):
            raise OverrideAuditError(
                "OperatorOverride.timestamp_ns must be a non-negative int "
                "(bool is rejected); got "
                f"{type(timestamp_ns).__name__}"
            )
        if timestamp_ns < 0 or not math.isfinite(timestamp_ns):
            raise OverrideAuditError(
                "OperatorOverride.timestamp_ns must be a non-negative finite "
                f"integer; got {timestamp_ns!r}"
            )


def _has_error(report: ReadinessReport) -> bool:
    return any(f.severity is Severity.ERROR for f in report.failures)


def _assert_env_is_mapping(env: Any) -> None:
    if not isinstance(env, Mapping):
        raise TypeError(
            "ReadinessGate.from_env / reevaluate require a Mapping[str, str]; "
            f"got {type(env).__name__}"
        )


def _load_report(env: Mapping[str, str], atp_env: str | None) -> ReadinessReport:
    """Overlay the SRS-SEC-001 credential vault, then run the SRS-ARCH-005 validator.

    ``load_vault_into_env`` is a no-op unless ``ATP_VAULT_FILE`` is set, so
    existing plaintext-env dev deployments are unaffected. A configured-but-broken
    vault (missing file, wrong key, tampered token) fails **closed**: it yields a
    single error-severity :class:`ReadinessFailure` so the gate holds pre-trade
    rather than validating against missing credentials.

    Encryption at rest is *enforced* in staging/production: a catalogued secret
    supplied as a real value in the plaintext environment (rather than sealed in
    the vault) is a hard readiness error there — see
    :func:`_plaintext_secret_failures`.
    """

    try:
        resolved = load_vault_into_env(env)
    except VaultError as error:
        return _vault_error_report(error)
    report = load_and_validate(resolved, atp_env=atp_env)
    effective_env = atp_env if atp_env is not None else env.get("ATP_ENV")
    report.failures.extend(_plaintext_secret_failures(env, effective_env))
    return report


def _plaintext_secret_failures(
    env: Mapping[str, str], effective_atp_env: str | None
) -> list[ReadinessFailure]:
    """Reject plaintext catalogued secrets in staging/production (SRS-SEC-001).

    In a production environment every ``secret`` credential must be sealed in the
    encrypted vault, not left in a plaintext ``.env`` / compose value. A real
    (non-placeholder) secret value present in the *original* environment is an
    encryption-at-rest violation — regardless of whether a vault is also
    configured, since the plaintext copy still sits unencrypted at rest.
    Placeholders are handled separately by the SRS-ARCH-005 validator.
    """

    if effective_atp_env not in PRODUCTION_ENVS:
        return []
    failures: list[ReadinessFailure] = []
    for spec in REQUIRED_KEYS:
        if not spec.secret:
            continue
        raw = env.get(spec.name)
        if raw and raw != PLACEHOLDER_VALUE:
            failures.append(
                ReadinessFailure(
                    key=spec.name,
                    category=spec.category,
                    severity=Severity.ERROR,
                    reason=(
                        f"{spec.name} is supplied as a plaintext value while "
                        f"ATP_ENV={effective_atp_env!r}; staging/production credentials "
                        f"must be sealed in the encrypted vault ({VAULT_FILE_ENV}) — SRS-SEC-001"
                    ),
                    srs_trace=("SRS-SEC-001", "NFR-S1", "NFR-S4"),
                )
            )
    return failures


def _vault_error_report(error: VaultError) -> ReadinessReport:
    failure = ReadinessFailure(
        key="ATP_VAULT_FILE",
        category=Category.CREDENTIALS,
        severity=Severity.ERROR,
        reason=f"credential vault could not be opened: {error}",
        srs_trace=("SRS-SEC-001", "NFR-S1", "NFR-S4"),
    )
    return ReadinessReport(
        failures=[failure],
        evidence=["SRS-SEC-001 credential vault load failed — holding pre-trade (fail-closed)"],
    )


__all__ = ["GateState", "ReadinessGate"]
