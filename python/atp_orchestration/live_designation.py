"""SRS-EXE-001 — the operator's live-designation surface (SyRS SYS-2c / SYS-2d, NFR-S2).

Serves the three operations of the ``LIVE_DESIGNATION`` workflow:

* ``POST /api/v1/strategies/{strategy_id}/promote-live`` (REST, confirmation required)
* ``live promote <strategy_id> --confirm`` (CLI, confirmation required)
* ``live show`` (CLI)

Each one shells ``exe001_live_designation_cli`` against the SAME durable snapshot the
live execution host re-reads on every order and the Hot-Swap moves
(``ATP_HOT_SWAP_DESIGNATION_STATE``). Nothing is decided here: the binary takes the
shared designation lock, refuses an empty confirmation, and refuses to designate a
second strategy while one is live (moving the live slot is a Hot-Swap, which
liquidates and demotes first).

**Confirmation.** The transport's requires-confirmation guard already refused an
unconfirmed request (428 / CLI exit 3), and the handler checks ``request.confirmed``
again before running anything. Neither surface carries free text, so the
acknowledgement handed to the binary names the surface and operation that confirmed
it; that string is what makes the token non-empty, and it is audit text, not a
second secret.

**``promoted_at``.** The snapshot records WHO is live, not when they became live.
So ``promoted_at`` is the time this request moved the designation, and ``null`` when
the strategy was already live and nothing moved. Inventing a time for a promotion
this request did not perform would be a fabricated audit fact.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from atp_runtime import (
    ErrorCategory,
    HandlerResult,
    InterfaceError,
    OperationKey,
    OperatorInterfaceRuntime,
    Request,
    Surface,
)

from .hot_swap_execution import SwapCliRunner

__all__ = [
    "BINARY_ENV_KNOB",
    "CLI_LIVE_PROMOTE",
    "CLI_LIVE_SHOW",
    "REST_PROMOTE_LIVE",
    "LiveDesignationHandlers",
    "default_binary",
    "mount_live_designation",
]

REST_PROMOTE_LIVE = OperationKey(Surface.REST, "POST /api/v1/strategies/{strategy_id}/promote-live")
CLI_LIVE_PROMOTE = OperationKey(Surface.CLI, "live promote")
CLI_LIVE_SHOW = OperationKey(Surface.CLI, "live show")

#: Environment override for the binary's location (a deployed image need not keep
#: the development cargo layout).
BINARY_ENV_KNOB = "ATP_LIVE_DESIGNATION_BINARY"

_DEFAULT_BINARY = (
    Path(__file__).resolve().parents[2] / "target" / "debug" / "exe001_live_designation_cli"
)

#: The binary may wait up to 10 s for a Hot-Swap or an in-flight live order to
#: release the designation lock; allow for that plus process start.
_DEFAULT_TIMEOUT_S = 20.0

#: The alphabet the live host serves (``live_host/server.rs::validate_strategy_id``).
#: Checked here too so a bad id is a 400 that names the rule, not a binary refusal.
_STRATEGY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

#: The binary's exit codes (``exe001_live_designation_cli.rs``).
_EXIT_OK = 0
_EXIT_REFUSED = 2
_EXIT_PUBLISHED_NOT_SYNCED = 3


def default_binary(env: Mapping[str, str] | None = None) -> Path:
    """The binary's path: the env override when set, else the dev fallback."""

    source = os.environ if env is None else env
    override = source.get(BINARY_ENV_KNOB)
    return Path(override) if override else _DEFAULT_BINARY


def _default_runner(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    if not Path(argv[0]).exists():
        raise FileNotFoundError(
            f"live-designation binary not found at {argv[0]}; build it with "
            "`cargo build -p atp-orchestrator --bin exe001_live_designation_cli`"
        )
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)


def parse_proof_lines(stdout: str) -> dict[str, str]:
    """Parse the binary's ``key:value`` lines; contradictory duplicates are refused."""

    values: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        if key in values and values[key] != value:
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                f"exe001_live_designation_cli emitted contradictory {key!r} lines "
                f"({values[key]!r} then {value!r})",
                type="LIVE_DESIGNATION_OUTPUT_UNREADABLE",
            )
        values[key] = value
    return values


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class LiveDesignationHandlers:
    """The three handlers of the ``LIVE_DESIGNATION`` workflow, over one snapshot."""

    def __init__(
        self,
        *,
        state_path: str | Path,
        binary: str | Path | None = None,
        runner: SwapCliRunner | None = None,
        timeout: float | None = None,
        clock: Callable[[], str] = _utc_now_iso,
    ) -> None:
        self._state_path = str(state_path)
        self._binary = Path(binary) if binary is not None else default_binary()
        self._runner: SwapCliRunner = runner or _default_runner
        self._timeout = _DEFAULT_TIMEOUT_S if timeout is None else timeout
        self._clock = clock

    # -- promote-live (REST) / live promote (CLI) ------------------------------

    def promote(self, request: Request) -> HandlerResult:
        if request.surface is Surface.REST:
            strategy_id = request.path_params.get("strategy_id")
            unknown = sorted(set(request.body) - {"confirm"})
            if unknown:
                raise InterfaceError(
                    ErrorCategory.BAD_REQUEST,
                    f"promote-live accepts no body fields besides `confirm`; got {unknown}",
                    type="UNKNOWN_REQUEST_FIELD",
                )
        else:
            strategy_id = request.query.get("strategy_id")
        if not isinstance(strategy_id, str) or not _STRATEGY_ID.fullmatch(strategy_id):
            raise InterfaceError(
                ErrorCategory.BAD_REQUEST,
                f"strategy id {strategy_id!r} must match [A-Za-z0-9][A-Za-z0-9._-]* and be "
                "at most 64 characters (the live host serves one socket per strategy id)",
                type="INVALID_STRATEGY_ID",
            )
        # Defence in depth under the transport guard: nothing runs unconfirmed.
        if not request.confirmed:
            raise InterfaceError(
                ErrorCategory.CONFIRMATION_REQUIRED,
                "designating the live strategy requires explicit operator confirmation "
                "(SyRS SYS-2d / NFR-S2)",
                type="CONFIRMATION_REQUIRED",
            )
        acknowledgement = f"operator confirmed via {request.surface.value} {request.operation}"
        completed = self._invoke(
            [
                "promote",
                "--state",
                self._state_path,
                "--strategy",
                strategy_id,
                "--confirm",
                acknowledgement,
            ]
        )
        if completed.returncode == _EXIT_REFUSED:
            detail = completed.stderr.strip() or "no detail"
            already = "already the designated live strategy" in detail
            raise InterfaceError(
                ErrorCategory.BAD_REQUEST,
                detail,
                type="LIVE_STRATEGY_ALREADY_DESIGNATED" if already else "LIVE_DESIGNATION_REFUSED",
                detail={"strategy_id": strategy_id},
            )
        if completed.returncode not in (_EXIT_OK, _EXIT_PUBLISHED_NOT_SYNCED):
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                f"exe001_live_designation_cli exited {completed.returncode}: "
                f"{completed.stderr.strip() or 'no detail'}",
                type="LIVE_DESIGNATION_CLI_FAILED",
            )
        proof = parse_proof_lines(completed.stdout)
        if proof.get("designated") != strategy_id:
            # The binary said it succeeded but did not name this strategy as live.
            # That is not a promotion we can report.
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                f"the designation binary reported success without designating {strategy_id!r} "
                f"(designated={proof.get('designated')!r})",
                type="LIVE_DESIGNATION_OUTPUT_UNREADABLE",
            )
        changed = proof.get("designation-changed")
        if changed not in ("true", "false"):
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                f"the designation binary reported designation-changed={changed!r}",
                type="LIVE_DESIGNATION_OUTPUT_UNREADABLE",
            )
        # The live slot HAS moved on exit 3; only crash-durability is uncertain. Report
        # the promotion (a non-2xx would invite a retry of something that happened) and
        # carry the caveat. `warning` is always present so the shape never varies.
        warning = None
        if completed.returncode == _EXIT_PUBLISHED_NOT_SYNCED:
            warning = completed.stderr.strip() or "designation published but not fsynced"
        return HandlerResult(
            status_code=200,
            body={
                "strategy_id": strategy_id,
                "is_live": True,
                "promoted_at": self._clock() if changed == "true" else None,
                "warning": warning,
            },
        )

    # -- live show (CLI) -------------------------------------------------------

    def show(self, request: Request) -> HandlerResult:
        completed = self._invoke(["status", "--state", self._state_path])
        if completed.returncode != _EXIT_OK:
            # An unreadable or foreign snapshot is NOT "nothing is live".
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                "the live-designation record could not be read: "
                f"{completed.stderr.strip() or 'no detail'}",
                type="LIVE_DESIGNATION_UNREADABLE",
            )
        designated = parse_proof_lines(completed.stdout).get("designated")
        if designated is None:
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                "the live-designation record produced no `designated` line",
                type="LIVE_DESIGNATION_UNREADABLE",
            )
        return HandlerResult(
            status_code=200,
            body={"designated": None if designated == "none" else designated},
        )

    def _invoke(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        argv = [str(self._binary), *args]
        try:
            return self._runner(argv, timeout=self._timeout)
        except subprocess.TimeoutExpired as expired:
            raise InterfaceError(
                ErrorCategory.GATEWAY_TIMEOUT,
                f"exe001_live_designation_cli did not answer within {self._timeout}s; the "
                "designation may or may not have moved - check `live show` before retrying",
                type="LIVE_DESIGNATION_CLI_TIMEOUT",
            ) from expired
        except OSError as launch_error:
            raise InterfaceError(
                ErrorCategory.GATEWAY_TIMEOUT,
                f"exe001_live_designation_cli could not be launched: {launch_error}",
                type="LIVE_DESIGNATION_CLI_UNAVAILABLE",
            ) from launch_error


class _Bound:
    """Adapt one handler method to the runtime's ``handle(request)`` protocol."""

    def __init__(self, method: Callable[[Request], HandlerResult]) -> None:
        self._method = method

    def handle(self, request: Request) -> HandlerResult:
        return self._method(request)


def mount_live_designation(
    runtime: OperatorInterfaceRuntime,
    *,
    state_path: str | Path,
    binary: str | Path | None = None,
    runner: SwapCliRunner | None = None,
    timeout: float | None = None,
) -> LiveDesignationHandlers:
    """Register all three ``LIVE_DESIGNATION`` operations on ``runtime``.

    Opt-in composition, like the Hot-Swap arms: a bare runtime keeps answering the
    structured 501 naming SRS-EXE-001, so a deployment that has not composed this
    never accepts a designation it cannot record.
    """

    handlers = LiveDesignationHandlers(
        state_path=state_path, binary=binary, runner=runner, timeout=timeout
    )
    runtime.registry.register(REST_PROMOTE_LIVE, _Bound(handlers.promote))
    runtime.registry.register(CLI_LIVE_PROMOTE, _Bound(handlers.promote))
    runtime.registry.register(CLI_LIVE_SHOW, _Bound(handlers.show))
    return handlers
