"""``SRS-RESV-005`` / SyRS SYS-49d — the Hot-Swap **execution** surface (REST).

This module binds ``POST /api/v1/hot-swap``: the route whose success means a swap
*happened*. It is deliberately separate from :mod:`atp_orchestration.hot_swap_triggers`,
which decides and logs but never demotes or promotes.

It shells the cargo-built ``resv005_hot_swap_promote_cli``, which drives the REAL
``StrategyOrchestrator::execute_hot_swap`` gate — the SRS-RESV-004 demotion gate, then the
SRS-RESV-005 promotion gate — over the REAL SRS-EXE-001 live-designation authority and the
REAL SRS-SIM-004 paper-state snapshot. One implementation, one code path, shared with the
CLI arm (the repo's subprocess → Rust binary → parse ``key:value`` stdout boundary).

Rules this surface must not break
---------------------------------

**A non-2xx means nothing mutated.** The shipped UI-5 consumer says so explicitly
(``assets/app.js``: *"A refusal (non-2xx) mutated nothing — no pending guard; retry is
allowed"*). So a refusal is a non-2xx **only** when it is decided BEFORE the swap runs
(bad request, missing confirmation, nothing live to demote). Once the gate has executed,
the outcome is a 200 carrying ``promotion_state`` — including when the promotion was
BLOCKED. Returning an error status for an executed-but-blocked swap would tell the pane it
may safely retry a swap whose demotion had in fact already released the live strategy.

**Success is bound to a durable artefact, not to an exit code.** ``swap_id`` is derived
from the promotion journal's record ordinal — the position a later reader can go to and
find that very record. If the journal append failed, ``swap_id`` is absent, and the pane
already refuses to call such a response promoted. An id is never invented.

**The subprocess budget must exceed the operation it waits on.** A demote-then-promote can
legitimately run for the SYS-49b demotion timeout (60 s default) before answering, so the
budget here is 90 s — matching the SPA's own ``HOT_FETCH_TIMEOUT_MS``. The 30 s default used
by the trigger client would manufacture an ambiguous result on a real swap.

**Unknown is not "nothing".** An unreadable designation snapshot, an unparseable proof
stream, or a binary that cannot be launched each surface as a structured error. None of
them reads as "no strategy is live", which would let a promotion run over a live strategy.

Scope
-----
Each POST is ONE demote-then-promote attempt, and the cross-attempt block is real:
``SRS-RESV-004`` ships the durable demotion-pending lockout, and ``resolve_demotion``
consults it BEFORE its probe, so a swap attempted while a previous demotion is
unresolved is refused before any side effect fires. This surface threads that lock
through (``demotion_lock_path``); it does not reimplement the rule.

``GET /api/v1/hot-swap/status`` still keeps its structured 501: its
``cooldown_expires_at`` field is owned by the unbuilt ``SRS-RESV-006`` cool-down, and
answering with part of a payload would be worse than not answering.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from atp_runtime import OperatorInterfaceRuntime
from atp_runtime.errors import ErrorCategory, InterfaceError
from atp_runtime.registry import HandlerResult, OperationKey, Request, Surface

__all__ = [
    "BINARY_ENV_KNOB",
    "SwapExecutionHandler",
    "SwapCliRunner",
    "default_binary",
    "mount_hot_swap_execution",
]

#: The operation this feature owns on the frozen SRS-API-001 contract.
REST_HOT_SWAP_EXECUTE = OperationKey(Surface.REST, "POST /api/v1/hot-swap")

#: Environment override for the operator binary's location. The fallback is a
#: DEVELOPMENT path (cargo output inside the checkout); a deployed image has no
#: reason to keep that layout, and without a knob the surface would look mounted
#: and then fail on first use.
BINARY_ENV_KNOB = "ATP_HOT_SWAP_PROMOTE_BINARY"

_DEFAULT_BINARY = (
    Path(__file__).resolve().parents[2] / "target" / "debug" / "resv005_hot_swap_promote_cli"
)

#: Per-invocation subprocess budget (seconds). MUST exceed the SYS-49b demotion
#: timeout it waits on (60 s default) or a real swap returns an ambiguous result;
#: matches the SPA's HOT_FETCH_TIMEOUT_MS.
_DEFAULT_TIMEOUT_S = 90.0

#: The exact set of body keys this route accepts. Anything else is refused rather
#: than silently dropped: accepting and ignoring a field reports a swap the caller
#: did not ask for as though it were the one they did.
_REQUEST_FIELDS = frozenset({"candidate_strategy_id", "confirm"})

#: The closed set of demotion outcomes the binary may report. Anything else —
#: including ABSENT — is unknown, and unknown fails closed rather than defaulting to
#: a value that would let a promotion be reported with no demotion proof.
_DEMOTION_OUTCOMES = frozenset({"FLAT_CONFIRMED", "DEMOTION_PENDING", "NOT_STARTED"})

#: Owner of the durable cross-attempt demotion-pending lockout (see the module docs).
_LOCKOUT_OWNER = "SRS-RESV-004"

#: Owner of the live-designation route to use when NOTHING is live — promoting with
#: no strategy to demote is a designation, not a Hot-Swap.
_DESIGNATION_OWNER = "SRS-EXE-001"


def default_binary(env: Mapping[str, str] | None = None) -> Path:
    """The operator binary's path: the env override when set, else the dev fallback."""

    source = os.environ if env is None else env
    override = source.get(BINARY_ENV_KNOB)
    return Path(override) if override else _DEFAULT_BINARY


class SwapCliRunner(Protocol):
    """The subprocess surface this module depends on (injectable for tests)."""

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]: ...


def _default_runner(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run the promotion CLI with ``argv`` as a list (``shell=False``)."""

    if not Path(argv[0]).exists():
        raise FileNotFoundError(
            f"hot-swap promotion binary not found at {argv[0]}; build it with "
            "`cargo build -p atp-orchestrator --bin resv005_hot_swap_promote_cli`"
        )
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)


def parse_proof_lines(stdout: str) -> dict[str, str]:
    """Parse the binary's deterministic ``key:value`` proof lines.

    Contradictory duplicates are REFUSED, not resolved. A version-skewed or wrong
    binary emitting two different ``promotion`` lines has not said which is true,
    and last-one-wins would let the handler report whichever happened to come
    second as the outcome of a real-money state change.
    """

    values: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        if key in values and values[key] != value:
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                f"the hot-swap binary emitted contradictory {key!r} lines "
                f"({values[key]!r} then {value!r}); the proof stream is ambiguous",
                type="SWAP_OUTPUT_UNREADABLE",
            )
        values[key] = value
    return values


class SwapExecutionHandler:
    """``POST /api/v1/hot-swap`` — execute one demote-then-promote attempt."""

    def __init__(
        self,
        *,
        state_path: str | Path,
        paper_state_dir: str | Path,
        log_path: str | Path,
        demotion_lock_path: str | Path,
        fixture_safety_inputs: Mapping[str, str] | None = None,
        binary: str | Path | None = None,
        runner: SwapCliRunner | None = None,
        timeout: float | None = None,
    ) -> None:
        #: A composer's EXPLICIT declaration that it is running a drill on fixture
        #: safety facts: ``{"positions": ..., "deployed_version": ...}``. Default
        #: ``None`` is the shipped posture — the route then refuses rather than
        #: promoting on facts nobody proved (see :meth:`handle`).
        #:
        #: It is deliberately not a bare pair of "sources": naming it after the TIER
        #: is what stops a future composer wiring a fixture in and forgetting it is
        #: one. When the real producers land (SRS-EXE-006 / SRS-ORCH-004) they get
        #: their own parameter and this one keeps meaning exactly "drill".
        self._fixture_safety_inputs = dict(fixture_safety_inputs or {})
        self._state_path = str(state_path)
        self._paper_state_dir = str(paper_state_dir)
        self._log_path = str(log_path)
        #: SRS-RESV-004's durable demotion-pending lockout. `resolve_demotion`
        #: consults it BEFORE its probe, so a swap attempted while a previous
        #: demotion is unresolved is refused before any side effect fires. Required,
        #: not optional: an optional path would let a composer opt out of the block.
        self._demotion_lock_path = str(demotion_lock_path)
        self._binary = Path(binary) if binary is not None else default_binary()
        self._runner = runner if runner is not None else _default_runner
        self._timeout = float(timeout) if timeout is not None else _DEFAULT_TIMEOUT_S

    def handle(self, request: Request) -> HandlerResult:
        if request.method not in (None, "POST"):
            raise InterfaceError(
                ErrorCategory.BAD_REQUEST,
                f"unsupported method {request.method!r} on the hot-swap execution route",
                type="UNSUPPORTED_METHOD",
            )

        candidate = self._read_candidate(request)

        # Defence in depth under the transport's requires-confirmation guard: a
        # future dispatch path must not reach the binary unconfirmed. Checked
        # BEFORE anything is read, so an unconfirmed request touches nothing.
        if not request.confirmed:
            raise InterfaceError(
                ErrorCategory.CONFIRMATION_REQUIRED,
                "executing a Hot-Swap designates a strategy live and requires explicit "
                "operator confirmation (SyRS SYS-2d / NFR-S2)",
                type="CONFIRMATION_REQUIRED",
            )

        # FIXTURE-TIER REFUSAL, before anything is read or run.
        #
        # SYS-49d turns on two SAFETY facts: the live account is flat, and the
        # candidate runs the same artifact. Their real producers are deferred
        # (SRS-EXE-006 / SRS-ORCH-004). A served route that promoted anyway would
        # report PROMOTED without proving either — a false green on a live trading
        # path, which is worse than an unbound route.
        #
        # So the route is MOUNTED and the gate behind it is real, but it refuses
        # until a composer supplies real sources. This is deliberately a different
        # 501 from an unbound operation: it names exactly which fact is missing and
        # who owns it, and it is the wiring point those owners plug into.
        missing = {
            "open IB positions (flat-start)": (
                "SRS-EXE-006" if "positions" not in self._fixture_safety_inputs else None
            ),
            "liquidation outcome (demotion reached flat)": (
                "SRS-EXE-006" if "liquidation" not in self._fixture_safety_inputs else None
            ),
            "deployed version (code identity)": (
                "SRS-ORCH-004" if "deployed_version" not in self._fixture_safety_inputs else None
            ),
        }
        if any(missing.values()):
            raise InterfaceError(
                ErrorCategory.NOT_IMPLEMENTED,
                "refusing to execute a Hot-Swap without real safety inputs: the promoted "
                "strategy must be proven to start with no open IB positions and to run the "
                "same artifact it ran as paper, and no producer of those facts is composed "
                "on this runtime. The gate itself is built (SRS-RESV-005); these inputs are "
                "not",
                type="SAFETY_INPUTS_UNAVAILABLE",
                detail={
                    # De-duplicated: two of the three facts share SRS-EXE-006, and
                    # naming it twice reads as two separate gaps.
                    "owner": ", ".join(sorted({o for o in missing.values() if o})),
                    "missing": sorted(k for k, v in missing.items() if v),
                },
            )

        demoting = self._current_live_strategy()
        if demoting is None:
            # Non-2xx, and correctly so: nothing has mutated. Promoting with no
            # live strategy to demote is a DESIGNATION, not a Hot-Swap, and the
            # route for that already exists.
            raise InterfaceError(
                ErrorCategory.BAD_REQUEST,
                "no strategy is currently live, so there is nothing to demote; use the "
                "live-designation route to designate a strategy live",
                type="NO_LIVE_STRATEGY_TO_DEMOTE",
                detail={
                    "owner": _DESIGNATION_OWNER,
                    "route": "POST /api/v1/strategies/{strategy_id}/promote-live",
                },
            )
        if demoting == candidate:
            raise InterfaceError(
                ErrorCategory.BAD_REQUEST,
                f"{candidate!r} is already the live strategy; a Hot-Swap must name a "
                "different candidate",
                type="SAME_STRATEGY_SWAP",
            )

        completed = self._invoke(
            [
                "swap",
                "--state",
                self._state_path,
                "--demoting",
                demoting,
                "--candidate",
                candidate,
                "--paper-state",
                self._paper_state_dir,
                "--log",
                self._log_path,
                "--demotion-lock",
                self._demotion_lock_path,
                # Explicit, never defaulted — and the fixture tier travels WITH
                # them, so the binary refuses if a caller ever supplies the values
                # without declaring what they are.
                "--positions",
                self._fixture_safety_inputs["positions"],
                "--deployed-version",
                self._fixture_safety_inputs["deployed_version"],
                "--liquidation",
                self._fixture_safety_inputs["liquidation"],
                "--allow-fixture-safety-inputs",
                "--confirm",
            ]
        )
        values = parse_proof_lines(completed.stdout)
        promotion = values.get("promotion")
        if promotion not in ("PROMOTED", "BLOCKED"):
            # The gate never reported an outcome: the swap may or may not have run.
            # This is NOT a 2xx (the pane would treat it as an accepted swap) and
            # NOT a silent success. Surface it as the internal failure it is.
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                "the hot-swap binary did not report a promotion outcome "
                f"(exit {completed.returncode}): {completed.stderr.strip() or 'no detail'}. "
                "The swap state is UNKNOWN — confirm the live strategy from durable "
                "status before retrying",
                type="SWAP_OUTCOME_UNREADABLE",
                detail={"owner": _LOCKOUT_OWNER},
            )

        # The DEMOTION half must be proven too, and proven POSITIVELY.
        #
        # Deriving it as `FLAT_CONFIRMED or else DEMOTION_PENDING` meant a stale,
        # truncated or wrong binary whose stdout carried `promotion:PROMOTED` but no
        # `demotion-outcome` produced a 200 reading "promoted, demotion pending" —
        # a live promotion reported with no successful-demotion proof behind it,
        # which is the one thing this requirement exists to prevent. Absent is not
        # DEMOTION_PENDING; it is unknown, and unknown fails closed.
        demotion = values.get("demotion-outcome")
        if demotion not in _DEMOTION_OUTCOMES:
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                f"the hot-swap binary reported promotion {promotion!r} with no readable "
                f"demotion outcome (got {demotion!r}); refusing to report a promotion "
                "whose demotion cannot be evidenced. The swap state is UNKNOWN — confirm "
                "the live strategy from durable status before retrying",
                type="SWAP_OUTCOME_UNREADABLE",
                detail={"owner": _LOCKOUT_OWNER},
            )
        # A swap that NEVER STARTED did not mutate anything: no demotion-side port was
        # touched, no lockout was engaged, the live slot is untouched. So it must be a
        # non-2xx — this surface documents non-2xx as "nothing mutated; retry is
        # allowed", and that is exactly true here.
        #
        # Reporting it as a 200 `DEMOTION_PENDING` (which is what collapsing the
        # three-valued outcome into a boolean produced) told the pane a swap had been
        # accepted and left its control inert awaiting durable confirmation of a
        # demotion-pending state that was never created — an operator-visible dead
        # end. DEMOTION_PENDING is reserved for the real SYS-49b timeout path.
        if demotion == "NOT_STARTED":
            reason = values.get("refusal") or "SWAP_NOT_STARTED"
            raise InterfaceError(
                ErrorCategory.BAD_REQUEST,
                f"the swap did not start ({reason}): no demotion ran and nothing was "
                "changed; re-read the current live strategy before retrying",
                type=reason,
                detail={"owner": _DESIGNATION_OWNER},
            )
        if promotion == "PROMOTED" and demotion != "FLAT_CONFIRMED":
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                f"the hot-swap binary reported a PROMOTED swap whose demotion was "
                f"{demotion!r}, not FLAT_CONFIRMED; SYS-49d permits a promotion only "
                "after a successful demotion, so this proof stream is incoherent and is "
                "refused rather than reported",
                type="SWAP_OUTCOME_INCOHERENT",
                detail={"owner": _LOCKOUT_OWNER},
            )

        # A promotion whose AUDIT RECORD did not land is not a clean success. The
        # designation is already written and persisted, so the swap happened — but
        # nothing durable addresses it, and an irreversible live-trading state change
        # with no record is an operator-reconciliation event, not a 200 that reads
        # like every other 200.
        #
        # Reported as a distinct state rather than as PROMOTED. The shipped pane
        # computes `promoted = !!swapId && promotion === "PROMOTED"`, so this value
        # correctly holds its control inert and waits for durable confirmation
        # instead of showing a completed swap.
        recorded = values.get("promotion-recorded")
        if promotion == "PROMOTED" and recorded != "true":
            promotion = "PROMOTED_UNRECORDED"

        body: dict[str, object] = {
            # DEMOTED / DEMOTION_PENDING — the closed vocabulary the shipped UI-5
            # pane already routes on, derived from a POSITIVELY proven outcome.
            "demotion_state": ("DEMOTED" if demotion == "FLAT_CONFIRMED" else "DEMOTION_PENDING"),
            "promotion_state": promotion,
        }
        ordinal = values.get("swap-record-ordinal", "-")
        # An absent journal record means there is no durable position to address, so
        # the id is explicitly NULL rather than omitted or invented. Null (not a
        # missing key) because the declared contract promises the field on every
        # 200 — dropping it would make the commonest degraded response a subset of
        # what the schema advertises. The pane already refuses to call a response
        # without a string id "promoted", so the null is load-bearing.
        body["swap_id"] = f"sw-{ordinal}" if ordinal != "-" else None
        return HandlerResult(200, body)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _read_candidate(self, request: Request) -> str:
        body = request.body or {}
        unknown = set(body) - _REQUEST_FIELDS
        if unknown:
            raise InterfaceError(
                ErrorCategory.BAD_REQUEST,
                f"unsupported request field(s) {sorted(unknown)!r} on the hot-swap "
                f"execution route; accepted fields are {sorted(_REQUEST_FIELDS)!r}",
                type="UNKNOWN_REQUEST_FIELD",
            )
        candidate = body.get("candidate_strategy_id")
        # A coerced id is refused: the most consequential field this request
        # carries must be said in full, not inferred from a truthy value.
        if not isinstance(candidate, str) or not candidate.strip():
            raise InterfaceError(
                ErrorCategory.BAD_REQUEST,
                "candidate_strategy_id is required and must be a non-empty string",
                type="MISSING_CANDIDATE_STRATEGY_ID",
            )
        return candidate.strip()

    def _current_live_strategy(self) -> str | None:
        """The demoting side, read from the DURABLE designation record.

        The caller does not supply it: SRS-RESV-003's contract is explicit that a
        proposal's demoting id is a request, not a verified fact. Reading it here
        (and the gate revalidating it again at execution time) is what keeps a
        swap from being aimed at a strategy that is not actually live.
        """

        completed = self._invoke(["status", "--state", self._state_path])
        if completed.returncode != 0:
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
                "the live-designation record produced no `designated` line; refusing to "
                "treat an unreadable answer as 'no strategy is live'",
                type="LIVE_DESIGNATION_UNREADABLE",
            )
        return None if designated == "none" else designated

    def _invoke(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        argv = [str(self._binary), *args]
        try:
            return self._runner(argv, timeout=self._timeout)
        except subprocess.TimeoutExpired as expired:
            # AMBIGUOUS, and reported as such: a demotion may still be in flight.
            raise InterfaceError(
                ErrorCategory.GATEWAY_TIMEOUT,
                f"resv005_hot_swap_promote_cli did not answer within {self._timeout}s; "
                "the swap may still be in flight — confirm the live strategy from "
                "durable status before retrying",
                type="SWAP_CLI_TIMEOUT",
                detail={"owner": _LOCKOUT_OWNER},
            ) from expired
        except OSError as launch_error:
            raise InterfaceError(
                ErrorCategory.GATEWAY_TIMEOUT,
                "resv005_hot_swap_promote_cli could not be launched (is it built? "
                "`cargo build -p atp-orchestrator --bin resv005_hot_swap_promote_cli`): "
                f"{launch_error}",
                type="SWAP_CLI_UNAVAILABLE",
            ) from launch_error


def mount_hot_swap_execution(
    runtime: OperatorInterfaceRuntime,
    *,
    state_path: str | Path,
    paper_state_dir: str | Path,
    log_path: str | Path,
    demotion_lock_path: str | Path,
    fixture_safety_inputs: Mapping[str, str] | None = None,
    binary: str | Path | None = None,
    runner: SwapCliRunner | None = None,
    timeout: float | None = None,
) -> SwapExecutionHandler:
    """Register the ``SRS-RESV-005`` swap-execution behaviour on ``runtime``.

    Opt-in composition, exactly like :func:`mount_hot_swap_triggers` and
    :func:`mount_rollback`: a bare runtime keeps ``POST /api/v1/hot-swap`` at its
    structured 501, so a deployment that has not composed this handler never answers
    on the endpoint whose success means a swap happened.

    Returns the handler so a composing process can hold the same instance.
    """

    handler = SwapExecutionHandler(
        state_path=state_path,
        paper_state_dir=paper_state_dir,
        log_path=log_path,
        demotion_lock_path=demotion_lock_path,
        fixture_safety_inputs=fixture_safety_inputs,
        binary=binary,
        runner=runner,
        timeout=timeout,
    )
    runtime.registry.register(REST_HOT_SWAP_EXECUTE, handler)
    return handler
