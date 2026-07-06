"""``SRS-ORCH-005`` / SyRS SYS-80 / NFR-S2 — the rollback handler behind the CLI +
REST operator surfaces.

The CLI ``strategy rollback`` command and the REST
``POST /api/v1/strategies/{strategy_id}/lifecycle`` route are fully declared on
the frozen SRS-API-001 contract (``python/atp_cli/commands.py`` /
``python/atp_api/routes.py``), and the runtime's transport-level guards already
force a ``--confirm`` / ``confirm`` token on every rollback (the same
requires-confirmation control live promotion uses — ``rest_server.py``'s
action-level guard, ``cli_dispatch.py``'s command guard). This module supplies
the BEHAVIOUR those surfaces dispatch to: it shells the cargo-built
``orch005_rollback_cli`` operator binary (the repo's only cross-language
boundary pattern — subprocess → Rust binary → parse stdout, see
``python/atp_strategy/store_history.py``), whose ``rollback`` subcommand drives
the fail-closed ``StrategyOrchestrator::rollback`` gate: SYS-80 previous-version
retention, exact-target matching, and the DOMAIN-level strategy-bound
confirmation for a live rollback (the structural mirror of live promotion's
``LiveDesignationConfirmation``).

Scope / honesty
---------------
* The handler transcribes the operator's explicit ``confirm`` act (the same act
  ``promote-live`` captures on its surface) into the strategy- and
  surface-naming acknowledgement phrase the typed ``RollbackConfirmation``
  records for audit. ``request.confirmed`` is re-checked here (defense in depth
  under the transport guard) — an unconfirmed request never reaches the binary.
* The LIVE-strategy identity fed to the gate comes from the injectable
  ``live_strategy_provider``; the REAL live-designation source is the deferred
  SRS-EXE-001 / SRS-RESV-* runtime (see ``rollback_contract.deferred[]``), so
  the default provider reports no live strategy and the transport confirmation
  guard remains the enforced control on this path. The domain-level live guard
  is proven end to end by the Rust bin/gate tests and the domain test.
* Composition is OPT-IN (:func:`mount_rollback`), exactly like
  ``atp_dashboard.mount_dashboard``: the bare ``OperatorInterfaceRuntime`` keeps
  every operation deferred (a structured 501 naming its owner). Wiring
  ``mount_rollback`` into a shipped process main is the deferred SRS-API-001
  composition leg.
* The dashboard arm of SYS-80's "dashboard, CLI, or REST API" is the deferred
  SRS-UI-001 control (a confirm-modal POSTing this same confirmed lifecycle
  route); this module puts the confirmed endpoint on the very server the
  dashboard is mounted on.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from atp_runtime import OperatorInterfaceRuntime
from atp_runtime.errors import ErrorCategory, InterfaceError
from atp_runtime.registry import (
    DeferredHandler,
    HandlerResult,
    OperationKey,
    Request,
    Surface,
)

__all__ = [
    "LifecycleActionHandler",
    "RollbackCliRunner",
    "RollbackHandler",
    "mount_rollback",
]

# Default location of the cargo-built operator binary, relative to the repo root
# (python/atp_orchestration/rollback_handler.py -> parents[2] == repo root).
# Build it with ``cargo build -p atp-orchestrator --bin orch005_rollback_cli``.
_DEFAULT_BINARY = Path(__file__).resolve().parents[2] / "target" / "debug" / "orch005_rollback_cli"

# Default per-invocation subprocess budget (seconds); a wedged binary surfaces a
# structured GATEWAY_TIMEOUT, never an indefinite hang of the operator surface.
_DEFAULT_TIMEOUT_S = 30.0

# The operation identifiers this feature owns on the frozen contract.
CLI_ROLLBACK_OPERATION = OperationKey(Surface.CLI, "strategy rollback")
REST_LIFECYCLE_OPERATION = OperationKey(
    Surface.REST, "POST /api/v1/strategies/{strategy_id}/lifecycle"
)

# Substring -> structured mapping for the bin's fail-closed refusals. The bin's
# stderr carries the gate's typed `RollbackError` Display text; each refusal is
# mapped to the CLOSED interface category set with the machine reason in
# `detail` (the category set never grows for domain reasons, by design).
_REFUSAL_RULES: tuple[tuple[str, ErrorCategory, str], ...] = (
    (
        "requires the same explicit confirmation",
        ErrorCategory.CONFIRMATION_REQUIRED,
        "LIVE_ROLLBACK_UNCONFIRMED",
    ),
    ("has no recorded deployment", ErrorCategory.NOT_FOUND, "NEVER_DEPLOYED"),
    ("no retained previous version", ErrorCategory.NOT_FOUND, "NO_PREVIOUS_VERSION"),
    ("does not match the retained previous", ErrorCategory.BAD_REQUEST, "TARGET_MISMATCH"),
    ("target hash invalid", ErrorCategory.BAD_REQUEST, "TARGET_HASH_INVALID"),
    (
        "confirmation token is bound to strategy",
        ErrorCategory.BAD_REQUEST,
        "CONFIRMATION_STRATEGY_MISMATCH",
    ),
    (
        "live status unavailable",
        ErrorCategory.INTERNAL_ERROR,
        "LIVE_STATUS_UNAVAILABLE",
    ),
)


class RollbackCliRunner(Protocol):
    """The subprocess surface the handler depends on (injectable for tests)."""

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]: ...


def _default_runner(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run the rollback CLI with ``argv`` as a list (``shell=False``)."""

    if not Path(argv[0]).exists():
        raise FileNotFoundError(
            f"rollback binary not found at {argv[0]}; build it with "
            "`cargo build -p atp-orchestrator --bin orch005_rollback_cli`"
        )
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)


def _no_live_strategy() -> str | None:
    """Default live-strategy provider: the real source is deferred (SRS-EXE-001 /
    SRS-RESV-*); until it lands, no live identity is fed to the gate and the
    transport confirmation guard remains the enforced control on this path."""

    return None


class RollbackHandler:
    """Turns a confirmed rollback :class:`Request` into an ``orch005_rollback_cli``
    invocation and parses the outcome (both the CLI and the REST lifecycle
    surfaces dispatch here).

    Args:
        state_path: The bin's durable state snapshot (the demonstration
            registry port; the durable registry store is deferred).
        binary: Path to the cargo-built ``orch005_rollback_cli``.
        runner: Injectable subprocess runner (tests substitute a fake).
        timeout: Per-invocation wall-clock budget in seconds.
        live_strategy_provider: Injectable source of the current live strategy
            id; defaults to "none live" (the real source is the deferred
            SRS-EXE-001 / SRS-RESV-* runtime).
    """

    def __init__(
        self,
        *,
        state_path: str | Path,
        binary: str | Path | None = None,
        runner: RollbackCliRunner | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        live_strategy_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self._state_path = str(state_path)
        self._binary = Path(binary) if binary is not None else _DEFAULT_BINARY
        self._runner = runner if runner is not None else _default_runner
        self._timeout = float(timeout)
        self._live_strategy_provider = (
            live_strategy_provider if live_strategy_provider is not None else _no_live_strategy
        )

    def handle(self, request: Request) -> HandlerResult:
        strategy_id = self._strategy_id(request)
        target = self._target_hash(request)
        # Defense in depth: the transport guards (REST action-level 428, CLI
        # --confirm exit) already enforced this, but the handler re-checks so a
        # future dispatch path cannot reach the binary unconfirmed.
        if not request.confirmed:
            raise InterfaceError(
                ErrorCategory.CONFIRMATION_REQUIRED,
                f"rollback of {strategy_id!r} requires the explicit confirmation control "
                "(the same control as live promotion — NFR-S2 / SyRS SYS-80).",
                type="LIVE_ROLLBACK_UNCONFIRMED",
                detail={"srs_refs": ["SRS-ORCH-005", "SYS-80"]},
            )

        # The operator's explicit confirm act, transcribed into the audit
        # acknowledgement the strategy-bound RollbackConfirmation records.
        acknowledgement = (
            f"operator confirmed rollback of {strategy_id} via {request.surface.value}"
        )
        argv = [
            str(self._binary),
            "rollback",
            "--state",
            self._state_path,
            "--strategy",
            strategy_id,
            "--target",
            target,
            "--acknowledge",
            acknowledgement,
        ]
        live = self._live_strategy_provider()
        if live is not None:
            argv.extend(["--live", live])

        completed = self._invoke(argv, strategy_id)
        if completed.returncode != 0:
            raise self._refusal_error(completed.stderr.strip(), strategy_id)

        outcome = _parse_key_values(completed.stdout)
        rolled_back_to = outcome.get("rolled-back-to", "")
        # The bin prints `<hash>@<ts>`; the route's deployment_version_hash
        # response field carries the hash half.
        deployment_version_hash = rolled_back_to.split("@", 1)[0]
        if not deployment_version_hash:
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                f"orch005_rollback_cli returned no rolled-back-to line for {strategy_id!r} "
                "(refusing to report a rollback that cannot be evidenced)",
                type="ROLLBACK_OUTPUT_UNPARSEABLE",
            )
        return HandlerResult(
            200,
            {
                "strategy_id": strategy_id,
                "lifecycle_state": "rolled-back",
                "deployment_version_hash": deployment_version_hash,
                "rolled_back_from": outcome.get("rolled-back-from", ""),
                "rolled_back_to": rolled_back_to,
                "was_live": outcome.get("was-live", "false") == "true",
            },
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    @staticmethod
    def _strategy_id(request: Request) -> str:
        value = request.path_params.get("strategy_id") or request.query.get("strategy_id")
        if not value or not value.strip():
            raise InterfaceError(
                ErrorCategory.BAD_REQUEST,
                "rollback requires a strategy_id",
                type="MISSING_STRATEGY_ID",
            )
        return value.strip()

    @staticmethod
    def _target_hash(request: Request) -> str:
        raw = (
            request.body.get("target_version_hash")
            or request.query.get("target_version_hash")
            or ""
        )
        value = str(raw).strip()
        if not value:
            raise InterfaceError(
                ErrorCategory.BAD_REQUEST,
                "rollback requires target_version_hash (the retained previous version's "
                "hash — see `strategy show` / the lifecycle GET for the retained pair)",
                type="MISSING_TARGET_VERSION_HASH",
            )
        return value

    def _invoke(self, argv: list[str], strategy_id: str) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(argv, timeout=self._timeout)
        except subprocess.TimeoutExpired as expired:
            raise InterfaceError(
                ErrorCategory.GATEWAY_TIMEOUT,
                f"orch005_rollback_cli timed out after {self._timeout}s for "
                f"strategy={strategy_id!r}",
                type="ROLLBACK_CLI_TIMEOUT",
            ) from expired
        except OSError as launch_error:
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                f"orch005_rollback_cli could not be launched for strategy={strategy_id!r} "
                f"(is it built? `cargo build -p atp-orchestrator --bin orch005_rollback_cli`): "
                f"{launch_error}",
                type="ROLLBACK_CLI_LAUNCH_FAILED",
            ) from launch_error

    @staticmethod
    def _refusal_error(stderr: str, strategy_id: str) -> InterfaceError:
        for needle, category, reason in _REFUSAL_RULES:
            if needle in stderr:
                return InterfaceError(
                    category,
                    f"rollback of {strategy_id!r} refused: {stderr}",
                    type=reason,
                    detail={"reason": reason, "stderr": stderr},
                )
        return InterfaceError(
            ErrorCategory.INTERNAL_ERROR,
            f"orch005_rollback_cli failed for strategy={strategy_id!r}: {stderr}",
            type="ROLLBACK_CLI_FAILED",
            detail={"stderr": stderr},
        )


class LifecycleActionHandler:
    """The shared REST lifecycle route's dispatcher: ``action == "rollback"`` is
    handled here (SRS-ORCH-005); every other action delegates to the honest
    structured 501 naming its owner (SRS-ORCH-004's start/stop/restart wiring),
    so registering this handler never over-claims the rest of the route."""

    def __init__(self, rollback: RollbackHandler) -> None:
        self._rollback = rollback
        self._deferred = DeferredHandler(
            owner="SRS-ORCH-004",
            summary="Strategy lifecycle transitions (start, stop, restart).",
        )

    def handle(self, request: Request) -> HandlerResult:
        action = str(request.body.get("action") or request.query.get("action") or "").strip()
        if action == "rollback":
            return self._rollback.handle(request)
        return self._deferred.handle(request)


def mount_rollback(
    runtime: OperatorInterfaceRuntime,
    *,
    state_path: str | Path,
    binary: str | Path | None = None,
    runner: RollbackCliRunner | None = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
    live_strategy_provider: Callable[[], str | None] | None = None,
) -> RollbackHandler:
    """Register the SRS-ORCH-005 rollback behaviour on ``runtime`` (opt-in
    composition, exactly like ``atp_dashboard.mount_dashboard``): the CLI
    ``strategy rollback`` command and the rollback action of the REST lifecycle
    route dispatch to the same :class:`RollbackHandler`. Returns the handler so
    the composing process can share/inspect it."""

    handler = RollbackHandler(
        state_path=state_path,
        binary=binary,
        runner=runner,
        timeout=timeout,
        live_strategy_provider=live_strategy_provider,
    )
    runtime.registry.register(CLI_ROLLBACK_OPERATION, handler)
    runtime.registry.register(REST_LIFECYCLE_OPERATION, LifecycleActionHandler(handler))
    return handler


def _parse_key_values(stdout: str) -> dict[str, str]:
    """Parse the bin's deterministic ``key:value`` proof lines."""

    values: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            values[key] = value
    return values
