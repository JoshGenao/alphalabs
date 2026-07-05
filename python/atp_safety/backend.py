"""Kill-switch activation backend — the fail-closed bridge to the Rust gate.

The repo's one cross-language boundary pattern is *subprocess → cargo-built
Rust binary* (``python/atp_strategy/store_history.py`` precedent). The
backend here shells ``safe001_kill_switch_cli activate`` — the orchestrator
composition that drives the REAL ``atp-execution`` activation gate over a
REAL ``LiveExecutionState`` and a REAL ``atp-simulation`` paper-engine fleet,
with the deterministic mocked-IB fixture transport (the live IB transport is
the deferred SRS-EXE-006 adapter; ``kill_switch_activation_contract``).

Every failure mode is CLOSED — a kill switch that cannot run must say so,
never look like it ran:

* missing / non-executable binary → :class:`KillSwitchBackendError` telling
  the operator to build it;
* subprocess timeout → ``TimeoutError`` (the runtime's ``invoke_handler``
  maps it to ``504 GATEWAY_TIMEOUT`` → CLI exit ``TIMEOUT``);
* usage/fixture failure (exit 2, no report) → :class:`KillSwitchBackendError`;
* unparseable / missing report line → :class:`KillSwitchBackendError`.

Exit code 1 (the sequence RAN but the report records failures) is **not** a
backend error: the report is the truth, and the handler surfaces its
per-phase outcomes rather than masking them behind a transport failure.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_BINARY = _REPO_ROOT / "target" / "debug" / "safe001_kill_switch_cli"
_REPORT_PREFIX = "report:"

#: Keys the CLI report must carry for the handler to build the SDK-pinned
#: response. Anything missing means version skew / truncation — fail closed.
_REQUIRED_REPORT_KEYS = (
    "activation_id",
    "live_strategy_id",
    "activated_at_epoch_ms",
    "paper_halt",
    "paper_halt_summary",
    "resting_order_cancels",
    "liquidations",
    "ib_disconnect",
    "timings",
    "fully_clean",
    "within_nfr_p3",
    "all_engines_halted",
)


class KillSwitchBackendError(Exception):
    """The activation backend could not run (or could not be trusted).

    Distinct from an activation that ran with recorded failures — that comes
    back as a normal :class:`ActivationOutcome` whose report says so.
    """


@dataclass(frozen=True, slots=True)
class ActivationOutcome:
    """A parsed activation report from the Rust gate.

    Attributes:
        report: The full activation report (the CLI's ``report:{json}``
            payload) — per-phase ``SideEffectOutcome``s, timings, and the
            composition-level ``all_engines_halted`` fact.
        ran_clean: ``True`` iff the CLI exited 0 (report fully clean AND
            every engine halted).
    """

    report: Mapping[str, object]
    ran_clean: bool

    @property
    def activation_id(self) -> str:
        return str(self.report["activation_id"])


class KillSwitchBackend(Protocol):
    """Executes one kill-switch activation and returns its parsed report."""

    def activate(self, activation_id: str) -> ActivationOutcome:  # pragma: no cover
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


class RustCliKillSwitchBackend:
    """Backend shelling ``safe001_kill_switch_cli activate`` fail-closed.

    Args:
        binary: Path to the cargo-built CLI (default
            ``<repo>/target/debug/safe001_kill_switch_cli``).
        scenario_args: Extra CLI flags selecting the activation scenario
            (fixture shape / fault injection) — chosen by the composer, never
            defaulted here beyond the CLI's own reference shape.
        timeout_s: Subprocess deadline. The kill switch's own NFR-P3 budget
            is 5 s; the default leaves headroom for process startup while
            still failing fast enough for an operator to react.
        runner: Injectable subprocess runner (tests).
    """

    def __init__(
        self,
        binary: Path | None = None,
        *,
        scenario_args: Sequence[str] = (),
        timeout_s: float = 10.0,
        runner: _Runner | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise KillSwitchBackendError(
                f"RustCliKillSwitchBackend.timeout_s must be positive; got {timeout_s}"
            )
        self._binary = Path(binary) if binary is not None else _DEFAULT_BINARY
        self._scenario_args = tuple(scenario_args)
        self._timeout_s = float(timeout_s)
        self._runner: _Runner = runner if runner is not None else _default_runner

    def activate(self, activation_id: str) -> ActivationOutcome:
        if not activation_id or not activation_id.strip():
            raise KillSwitchBackendError("activation_id must be non-blank")
        if self._runner is _default_runner and not self._binary.is_file():
            raise KillSwitchBackendError(
                f"kill-switch CLI not found at {self._binary} — build it with "
                "`cargo build -p atp-orchestrator --bin safe001_kill_switch_cli`"
            )
        argv = [
            str(self._binary),
            "activate",
            "--activation-id",
            activation_id,
            *self._scenario_args,
        ]
        # A TimeoutExpired propagates as TimeoutError so the operator runtime
        # serialises it as 504 GATEWAY_TIMEOUT (CLI exit TIMEOUT) — a hung
        # kill switch must surface as a timeout, not hang the operator.
        try:
            completed = self._runner(argv, timeout_s=self._timeout_s)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f"kill-switch activation exceeded {self._timeout_s}s") from error
        if completed.returncode not in (0, 1):
            raise KillSwitchBackendError(
                "kill-switch CLI could not run the activation "
                f"(exit {completed.returncode}): {completed.stderr.strip() or completed.stdout.strip()}"
            )
        report = _parse_report(completed.stdout)
        reported_id = report.get("activation_id")
        if reported_id != activation_id:
            raise KillSwitchBackendError(
                f"kill-switch CLI reported activation_id {reported_id!r} for "
                f"requested {activation_id!r} — refusing a mismatched report"
            )
        return ActivationOutcome(report=report, ran_clean=completed.returncode == 0)


def _parse_report(stdout: str) -> dict[str, object]:
    line = next(
        (line for line in stdout.splitlines() if line.startswith(_REPORT_PREFIX)),
        None,
    )
    if line is None:
        raise KillSwitchBackendError(
            f"kill-switch CLI produced no report line; stdout was: {stdout!r}"
        )
    try:
        report = json.loads(line[len(_REPORT_PREFIX) :])
    except json.JSONDecodeError as error:
        raise KillSwitchBackendError(
            f"kill-switch CLI report is not valid JSON: {error}"
        ) from error
    if not isinstance(report, dict):
        raise KillSwitchBackendError(
            f"kill-switch CLI report must be a JSON object; got {type(report).__name__}"
        )
    missing = [key for key in _REQUIRED_REPORT_KEYS if key not in report]
    if missing:
        raise KillSwitchBackendError(f"kill-switch CLI report is missing required keys: {missing}")
    return report
