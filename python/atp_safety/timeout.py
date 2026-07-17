"""SRS-SAFE-002 liquidation-timeout backend — the fail-closed bridge to the
Rust timeout runtime, plus the SRS-LOG-001 durable-record step.

Mirrors :mod:`atp_safety.backend` (the SRS-SAFE-001 activation bridge): the
repo's one cross-language boundary pattern is *subprocess → cargo-built Rust
binary*. The backend here shells ``safe002_liquidation_timeout_cli resolve``
— the orchestrator composition that drives the REAL
``atp-execution`` timeout gate through the REAL ``PollingLiquidationProbe``
(full wait window on a simulated clock), the REAL SRS-NOTIF-001
``OperatorNotifier`` (fixture email/SMS transports; the concrete SMTP/SMS
adapters are the deferred SRS-NOTIF-001 leg) and the REAL
``IbGatewayLiquidationCleanup`` (fixture IB gateway; the live transport is
the deferred SRS-EXE-006 leg).

Every failure mode is CLOSED — a timeout drill that cannot run must say so,
never look like it ran:

* missing / non-executable binary → :class:`LiquidationTimeoutBackendError`;
* subprocess timeout → ``TimeoutError``;
* usage/scenario failure (exit 2, no outcome) →
  :class:`LiquidationTimeoutBackendError`;
* unparseable / missing / key-incomplete outcome line →
  :class:`LiquidationTimeoutBackendError`.

Exit codes 1 (the SYS-44b sequence RAN — the outcome is the truth, failures
included) and 3 (fail-closed probe refusal — nothing destructive ran) are
**not** backend errors: they are parsed outcomes the caller inspects.

:func:`resolve_liquidation_timeout` is the composition step: run the backend,
then write the SYS-44b "log the unfilled order details" record durably to the
SRS-LOG-001 store. A failed durable write surfaces as
:class:`LiquidationTimeoutAuditError` carrying the outcome — the audit
failure is never swallowed, and the caller still receives what happened.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from atp_logging import LogRecord
from atp_logging.persistence import JsonlLogStore

from .audit import build_liquidation_timeout_record

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_BINARY = _REPO_ROOT / "target" / "debug" / "safe002_liquidation_timeout_cli"
_OUTCOME_PREFIX = "outcome:"

#: Keys the CLI outcome must carry for the audit record + caller decisions.
#: Anything missing means version skew / truncation — fail closed.
_REQUIRED_OUTCOME_KEYS = (
    "disposition",
    "notification",
    "gateway_calls",
    "probe_polls",
    "simulated_elapsed_ms",
    "category",
    "error_type",
    "manual_resolution_required",
    "cleanup",
)

#: The dispositions the CLI can print, keyed by its exit code.
_DISPOSITIONS_BY_EXIT = {
    0: ("FILLED_BEFORE_TIMEOUT",),
    1: ("TIMED_OUT_UNFILLED",),
    3: ("PROBE_UNAVAILABLE", "PROBE_INCONSISTENT"),
}

#: Dispositions whose contract is "NO SYS-44b cleanup ran": filled (the error
#: path never engaged) and the fail-closed probe refusals (the gate takes no
#: automated action on an unconfirmable/inconsistent order state). A payload
#: claiming otherwise is contradictory and must be refused, never trusted.
_NO_CLEANUP_DISPOSITIONS = frozenset(
    {"FILLED_BEFORE_TIMEOUT", "PROBE_UNAVAILABLE", "PROBE_INCONSISTENT"}
)

#: The three SYS-44b cleanup legs recorded on every outcome.
_CLEANUP_LEGS = ("operator_alert", "liquidation_cancel", "ib_disconnect")


class LiquidationTimeoutBackendError(Exception):
    """The timeout backend could not run (or could not be trusted).

    Distinct from a drill whose SYS-44b sequence ran with recorded failures —
    that comes back as a normal :class:`LiquidationTimeoutOutcome` whose
    payload says so.
    """


class LiquidationTimeoutAuditError(Exception):
    """The durable SRS-LOG-001 write for a resolved timeout failed.

    Carries the parsed outcome so the caller still knows what happened — the
    SYS-44b side effects are NOT rolled back by a failed audit write, and the
    failure is never silently swallowed.
    """

    def __init__(self, message: str, outcome: LiquidationTimeoutOutcome) -> None:
        super().__init__(message)
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class LiquidationTimeoutOutcome:
    """A parsed liquidation-timeout outcome from the Rust runtime.

    Attributes:
        payload: The full outcome (the CLI's ``outcome:{json}`` line) —
            disposition, per-side-effect cleanup outcomes, notification and
            gateway evidence.
        exit_code: The CLI exit code (0 filled / 1 timed out / 3 fail-closed
            probe refusal).
    """

    payload: Mapping[str, object]
    exit_code: int

    @property
    def disposition(self) -> str:
        return str(self.payload["disposition"])

    @property
    def timed_out(self) -> bool:
        return self.disposition == "TIMED_OUT_UNFILLED"

    @property
    def manual_resolution_required(self) -> bool:
        return bool(self.payload["manual_resolution_required"])


class LiquidationTimeoutBackend(Protocol):
    """Executes one SYS-44b timeout drill and returns its parsed outcome."""

    def resolve(
        self, scenario_args: Sequence[str] = ()
    ) -> LiquidationTimeoutOutcome:  # pragma: no cover - protocol
        ...


class _Runner(Protocol):
    def __call__(
        self, argv: Sequence[str], *, timeout_s: float
    ) -> subprocess.CompletedProcess[str]:  # pragma: no cover - protocol
        ...


def _default_runner(argv: Sequence[str], *, timeout_s: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed binary path, no shell
        list(argv),
        shell=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


class RustCliLiquidationTimeoutBackend:
    """Backend shelling ``safe002_liquidation_timeout_cli resolve`` fail-closed.

    Args:
        binary: Path to the cargo-built CLI (default
            ``<repo>/target/debug/safe002_liquidation_timeout_cli``).
        timeout_s: Subprocess deadline. The drill's wait loop runs on a
            simulated clock (a 30 s scenario completes in milliseconds), so
            the default only needs process-startup headroom.
        runner: Injectable subprocess runner (tests).
    """

    def __init__(
        self,
        binary: Path | None = None,
        *,
        timeout_s: float = 10.0,
        runner: _Runner | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise LiquidationTimeoutBackendError(
                f"RustCliLiquidationTimeoutBackend.timeout_s must be positive; got {timeout_s}"
            )
        self._binary = Path(binary) if binary is not None else _DEFAULT_BINARY
        self._timeout_s = float(timeout_s)
        self._runner: _Runner = runner if runner is not None else _default_runner

    def resolve(self, scenario_args: Sequence[str] = ()) -> LiquidationTimeoutOutcome:
        if self._runner is _default_runner and not self._binary.is_file():
            raise LiquidationTimeoutBackendError(
                f"liquidation-timeout CLI not found at {self._binary} — build it with "
                "`cargo build -p atp-orchestrator --bin safe002_liquidation_timeout_cli`"
            )
        argv = [str(self._binary), "resolve", *scenario_args]
        try:
            completed = self._runner(argv, timeout_s=self._timeout_s)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f"liquidation-timeout drill exceeded {self._timeout_s}s") from error
        if completed.returncode not in _DISPOSITIONS_BY_EXIT:
            raise LiquidationTimeoutBackendError(
                "liquidation-timeout CLI could not run the drill "
                f"(exit {completed.returncode}): "
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )
        payload = _parse_outcome(completed.stdout)
        disposition = str(payload.get("disposition"))
        allowed = _DISPOSITIONS_BY_EXIT[completed.returncode]
        if disposition not in allowed:
            raise LiquidationTimeoutBackendError(
                f"liquidation-timeout CLI exit {completed.returncode} reported "
                f"disposition {disposition!r} (expected one of {allowed}) — "
                "refusing a mismatched outcome"
            )
        _assert_outcome_consistency(payload, disposition)
        return LiquidationTimeoutOutcome(payload=payload, exit_code=completed.returncode)


def _parse_outcome(stdout: str) -> dict[str, object]:
    line = next(
        (line for line in stdout.splitlines() if line.startswith(_OUTCOME_PREFIX)),
        None,
    )
    if line is None:
        raise LiquidationTimeoutBackendError(
            f"liquidation-timeout CLI produced no outcome line; stdout was: {stdout!r}"
        )
    try:
        payload = json.loads(line[len(_OUTCOME_PREFIX) :])
    except json.JSONDecodeError as error:
        raise LiquidationTimeoutBackendError(
            f"liquidation-timeout CLI outcome is not valid JSON: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise LiquidationTimeoutBackendError(
            f"liquidation-timeout CLI outcome must be a JSON object; got {type(payload).__name__}"
        )
    missing = [key for key in _REQUIRED_OUTCOME_KEYS if key not in payload]
    if missing:
        raise LiquidationTimeoutBackendError(
            f"liquidation-timeout CLI outcome is missing required keys: {missing}"
        )
    return payload


def _assert_outcome_consistency(payload: Mapping[str, object], disposition: str) -> None:
    """Refuse a payload whose evidence contradicts its disposition.

    The exit-code/disposition pairing alone is not enough — both directions
    of skew are refused:

    * A non-timeout disposition whose evidence shows the destructive SYS-44b
      cleanup ran (trusting it would suppress the durable
      ``LIQUIDATION_TIMEOUT`` record for side effects that actually
      happened): every ``_NO_CLEANUP_DISPOSITIONS`` outcome must show NO
      gateway calls, NO accepted pages, and every cleanup leg
      ``NOT_ATTEMPTED``.
    * A ``TIMED_OUT_UNFILLED`` disposition whose evidence shows the SYS-44b
      sequence did NOT run (writing the durable record for it would imply the
      timeout was handled while the page/cancel/disconnect never fired):
      ``manual_resolution_required`` must be true and every cleanup leg must
      have been ATTEMPTED (``SUCCEEDED`` or ``FAILED`` — never
      ``NOT_ATTEMPTED``; a failed attempt is a valid, observable outcome).

    And every non-filled disposition must carry the ``unfilled_order``
    details so refusals and timeouts alike stay auditable.
    """

    if disposition in _NO_CLEANUP_DISPOSITIONS:
        contradictions: list[str] = []
        gateway_calls = payload["gateway_calls"]
        if not isinstance(gateway_calls, list) or gateway_calls:
            contradictions.append(f"gateway_calls={gateway_calls!r}")
        notification = payload["notification"]
        if not isinstance(notification, Mapping):
            contradictions.append(f"notification={notification!r}")
        else:
            for channel_key in ("email_accepted", "sms_accepted"):
                if notification.get(channel_key) != 0:
                    contradictions.append(
                        f"notification.{channel_key}={notification.get(channel_key)!r}"
                    )
        cleanup = payload["cleanup"]
        if not isinstance(cleanup, Mapping):
            contradictions.append(f"cleanup={cleanup!r}")
        else:
            for leg in _CLEANUP_LEGS:
                side_effect = cleanup.get(leg)
                status = side_effect.get("status") if isinstance(side_effect, Mapping) else None
                if status != "NOT_ATTEMPTED":
                    contradictions.append(f"cleanup.{leg}.status={status!r}")
        if contradictions:
            raise LiquidationTimeoutBackendError(
                f"liquidation-timeout CLI reported disposition {disposition!r} "
                "(no SYS-44b cleanup may have run) but its own evidence "
                f"contradicts that: {', '.join(contradictions)} — refusing the "
                "outcome rather than suppressing a safety record for side "
                "effects that may have happened"
            )
    if disposition == "TIMED_OUT_UNFILLED":
        contradictions = []
        if payload["manual_resolution_required"] is not True:
            contradictions.append(
                f"manual_resolution_required={payload['manual_resolution_required']!r}"
            )
        cleanup = payload["cleanup"]
        if not isinstance(cleanup, Mapping):
            contradictions.append(f"cleanup={cleanup!r}")
        else:
            for leg in _CLEANUP_LEGS:
                side_effect = cleanup.get(leg)
                status = side_effect.get("status") if isinstance(side_effect, Mapping) else None
                # A FAILED attempt is a valid, observable outcome; an
                # unattempted leg on a confirmed timeout is a contract breach.
                if status not in ("SUCCEEDED", "FAILED"):
                    contradictions.append(f"cleanup.{leg}.status={status!r}")
        if contradictions:
            raise LiquidationTimeoutBackendError(
                "liquidation-timeout CLI reported TIMED_OUT_UNFILLED (the SYS-44b "
                "sequence must have run: page + cancel + disconnect each attempted, "
                "positions awaiting manual resolution) but its own evidence "
                f"contradicts that: {', '.join(contradictions)} — refusing to write "
                "a durable record implying the timeout was handled"
            )
    if disposition != "FILLED_BEFORE_TIMEOUT":
        order = payload.get("unfilled_order")
        order_id = str(order.get("order_id", "")).strip() if isinstance(order, Mapping) else ""
        if not order_id:
            raise LiquidationTimeoutBackendError(
                f"liquidation-timeout CLI disposition {disposition!r} carries no "
                "unfilled_order details — a refusal without the order identity "
                "is not auditable; refusing the outcome"
            )


def _with_durable_audit_flag(
    outcome: LiquidationTimeoutOutcome, recorded: bool
) -> LiquidationTimeoutOutcome:
    """Stamp the DURABLE-audit truth onto the outcome payload.

    The CLI's ``cleanup.event_sink_recorded`` reflects only the Rust
    in-memory event sink; whether the SYS-44b details actually reached the
    durable SRS-LOG-001 store is decided HERE, after the write — so a failed
    write can never masquerade as a recorded audit.
    """

    payload = dict(outcome.payload)
    payload["durable_audit_recorded"] = recorded
    return LiquidationTimeoutOutcome(payload=payload, exit_code=outcome.exit_code)


def resolve_liquidation_timeout(
    backend: LiquidationTimeoutBackend,
    store: JsonlLogStore,
    *,
    scenario_args: Sequence[str] = (),
    timestamp_ns: int | None = None,
) -> tuple[LiquidationTimeoutOutcome, LogRecord | None]:
    """Run one SYS-44b timeout drill and durably log its outcome.

    Returns ``(outcome, record)``. For a ``TIMED_OUT_UNFILLED`` disposition
    the SYS-44b ``LIQUIDATION_TIMEOUT`` record is written durably to
    ``store`` (the "details are logged" leg) and the returned outcome payload
    carries ``durable_audit_recorded: True`` — the durable truth is owned by
    THIS step, never by the CLI's in-memory ``cleanup.event_sink_recorded``.
    A failed write raises :class:`LiquidationTimeoutAuditError` whose carried
    outcome is stamped ``durable_audit_recorded: False`` — never silently
    swallowed, never claiming a record that does not exist. Filled and
    fail-closed dispositions write no ``LIQUIDATION_TIMEOUT`` record (nothing
    timed out); the returned record is ``None``.
    """

    outcome = backend.resolve(scenario_args)
    if not outcome.timed_out:
        return outcome, None
    record = build_liquidation_timeout_record(outcome.payload, timestamp_ns=timestamp_ns)
    try:
        store.write(record)
    except Exception as error:  # noqa: BLE001 - every write failure must surface
        raise LiquidationTimeoutAuditError(
            f"SYS-44b LIQUIDATION_TIMEOUT audit write failed: {error}",
            _with_durable_audit_flag(outcome, False),
        ) from error
    return _with_durable_audit_flag(outcome, True), record
