"""``SRS-RESV-003`` / SyRS SYS-49a — the Hot-Swap TRIGGER surface (REST + dashboard).

SYS-49a names three operator arms — "via the dashboard, CLI, or REST API". The CLI arm is
``resv003_hot_swap_trigger_cli``; this module is the **REST** arm. The dashboard arm is
``atp_dashboard.hotswap.CliHotSwapTriggerSource``, which lives beside the
``HotSwapStatusSource`` protocol it implements (and which this module imports, so both arms
read one configuration through one code path and cannot disagree about it).

Both shell the same cargo-built binary — the repo's only cross-language boundary pattern:
subprocess → Rust binary → parse ``key:value`` stdout, see
``python/atp_orchestration/rollback_handler.py`` and ``python/atp_strategy/store_history.py``
— so the trigger decision, the fail-closed logging rule, and the durable configuration
format have exactly one implementation.

What this surface does, and what it emphatically does not
---------------------------------------------------------
``SRS-RESV-003`` owns the trigger *decision, configuration, and logging* layer. It
proposes and it logs; it does **not** execute a swap. Firing the manual trigger here
produces a durably logged *proposal* — nothing is demoted, nothing is promoted, and no
strategy changes state. Execution is ``SRS-RESV-004`` (the demotion gate) and
``SRS-RESV-005`` (promotion), neither of which is built.

That distinction is the whole reason these routes are separate from
``POST /api/v1/hot-swap``. That route is the swap-execution contract, it resolves to a
structured 501 naming its deferred owner, and this module deliberately does not bind it: a
surface that cannot execute a swap must not answer on the endpoint whose success means one
happened. Every response here carries an explicit ``execution`` block naming the deferred
owner, so no caller — human or machine — can read a fired trigger as a completed
changeover.

Fail-closed rules this layer must preserve
------------------------------------------
* **A trigger that was not logged did not fire.** The binary already makes this
  load-bearing (a rejected audit record exits nonzero, and ``request_manual_promotion``
  returns ``Err(UnloggedHotSwapTrigger)``). This layer must propagate that rather than
  translate a nonzero exit into a generic error and a 2xx into success.
* **Success is bound to a durable artefact, not to an exit code.** A fired manual trigger
  is reported only with the ``trigger-record-ordinal`` the binary read back out of the
  durable log — the position a later reader can go to and find that very record.
* **Unknown is not "disabled".** An unreadable configuration surfaces as
  :class:`HotSwapStatusUnavailable` (for the pane) or a structured error (for REST), never
  as the all-disabled default. See ``atp_orchestrator::trigger_config_store`` for why those
  are three different facts.
"""

from __future__ import annotations

from pathlib import Path

from atp_dashboard.hotswap import (
    CliHotSwapTriggerSource,
    HotSwapStatusUnavailable,
    HotSwapTriggerCliRunner,
    HotSwapTriggerOutputUnreadable,
    parse_trigger_cli_output,
    strict_trigger_bool,
)
from atp_runtime import OperatorInterfaceRuntime
from atp_runtime.errors import ErrorCategory, InterfaceError
from atp_runtime.registry import HandlerResult, OperationKey, Request, Surface

__all__ = [
    "CliHotSwapTriggerSource",
    "HotSwapStatusUnavailable",
    "HotSwapTriggerCliRunner",
    "ManualTriggerHandler",
    "TriggerConfigHandler",
    "mount_hot_swap_triggers",
]

#: The deferred owner of swap EXECUTION. Named in every manual-trigger response so the
#: caller is told, in the payload itself, that nothing was demoted or promoted.
EXECUTION_OWNER = "SRS-RESV-004"

# The operations this feature owns on the frozen SRS-API-001 contract.
REST_TRIGGER_CONFIG_GET = OperationKey(Surface.REST, "GET /api/v1/hot-swap/triggers")
REST_TRIGGER_CONFIG_PUT = OperationKey(Surface.REST, "PUT /api/v1/hot-swap/triggers")
REST_TRIGGER_MANUAL = OperationKey(Surface.REST, "POST /api/v1/hot-swap/triggers/manual")


class TriggerConfigHandler:
    """``GET`` / ``PUT /api/v1/hot-swap/triggers`` — read and durably set the configuration."""

    def __init__(self, source: CliHotSwapTriggerSource, *, state_path: str) -> None:
        self._source = source
        self._state_path = state_path

    def handle(self, request: Request) -> HandlerResult:
        # One handler serves both verbs so the PUT can report its result by re-reading
        # through the exact code path the GET uses — the two can never drift into
        # disagreeing about the same file.
        if request.method == "PUT":
            return self._put(request)
        if request.method == "GET":
            return self._get()
        raise InterfaceError(
            ErrorCategory.BAD_REQUEST,
            f"unsupported method {request.method!r} on the trigger configuration route",
            type="UNSUPPORTED_METHOD",
        )

    def _get(self) -> HandlerResult:
        try:
            config = self._source.trigger_config()
        except HotSwapTriggerOutputUnreadable as unreadable:
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                str(unreadable),
                type="TRIGGER_CONFIG_OUTPUT_UNPARSEABLE",
            ) from unreadable
        except HotSwapStatusUnavailable as unavailable:
            # An unreadable configuration is a real failure with a real cause, not an
            # empty/disabled configuration. Reporting 200 + "disabled" here would tell an
            # operator no automatic swap can fire when nobody knows whether one can.
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                str(unavailable),
                type="TRIGGER_CONFIG_UNREADABLE",
            ) from unavailable
        return HandlerResult(200, _rest_config_body(config))

    def _put(self, request: Request) -> HandlerResult:
        # Defense in depth under the transport's requires-confirmation guard. Arming an
        # automatic trigger is consequential exactly because nothing further is asked of the
        # operator: once enabled, a drawdown breach demotes the live strategy on its own.
        if not request.confirmed:
            raise InterfaceError(
                ErrorCategory.CONFIRMATION_REQUIRED,
                "changing the automatic Hot-Swap trigger configuration requires the "
                "explicit confirmation control (SyRS SYS-49a)",
                type="TRIGGER_CONFIG_UNCONFIRMED",
                detail={"srs_refs": ["SRS-RESV-003", "SYS-49a"]},
            )
        args = ["config", "--state", self._state_path]
        body = request.body

        drawdown = _optional_bool(body, "drawdown_demotion_enabled")
        threshold = body.get("drawdown_demotion_threshold_bps")
        if drawdown is True:
            if threshold is None:
                raise InterfaceError(
                    ErrorCategory.BAD_REQUEST,
                    "enabling drawdown demotion requires drawdown_demotion_threshold_bps "
                    "(1..=10000) — an enabled trigger has no threshold to fire on",
                    type="MISSING_DRAWDOWN_THRESHOLD",
                )
            args.extend(["--set-drawdown-threshold", _positive_int(threshold)])
        elif drawdown is False:
            if threshold is not None:
                raise InterfaceError(
                    ErrorCategory.BAD_REQUEST,
                    "drawdown_demotion_threshold_bps was supplied alongside "
                    "drawdown_demotion_enabled=false — the request states the trigger's "
                    "arming twice, and disagrees",
                    type="CONTRADICTORY_DRAWDOWN_CONFIG",
                )
            args.append("--set-no-drawdown")
        elif threshold is not None:
            raise InterfaceError(
                ErrorCategory.BAD_REQUEST,
                "drawdown_demotion_threshold_bps was supplied without "
                "drawdown_demotion_enabled — refusing to infer whether to arm the trigger",
                type="AMBIGUOUS_DRAWDOWN_CONFIG",
            )

        for field_name, flag in (
            ("top_ranked_promotion_enabled", "--set-top-ranked"),
            ("highest_momentum_promotion_enabled", "--set-highest-momentum"),
        ):
            value = _optional_bool(body, field_name)
            if value is not None:
                args.extend([flag, "on" if value else "off"])

        if len(args) == 3:
            raise InterfaceError(
                ErrorCategory.BAD_REQUEST,
                "no trigger fields supplied; send at least one of "
                "drawdown_demotion_enabled, top_ranked_promotion_enabled, "
                "highest_momentum_promotion_enabled",
                type="EMPTY_TRIGGER_CONFIG_REQUEST",
            )

        completed = self._source._invoke(args)  # noqa: SLF001 — same-module collaborator
        if completed.returncode != 0:
            raise InterfaceError(
                ErrorCategory.BAD_REQUEST,
                f"hot-swap trigger configuration refused: {completed.stderr.strip()}",
                type="TRIGGER_CONFIG_REFUSED",
                detail={"stderr": completed.stderr.strip()},
            )
        # Report what a LATER reader sees, by re-reading through the same path the GET uses
        # — never the request's own intent echoed back.
        return self._get()


class ManualTriggerHandler:
    """``POST /api/v1/hot-swap/triggers/manual`` — fire the always-available manual trigger.

    Produces a durably LOGGED PROPOSAL. It does not demote, promote, or change any
    strategy's state; ``execution`` in the response names the deferred owner that would.
    """

    def __init__(
        self,
        source: CliHotSwapTriggerSource,
        *,
        log_path: str,
    ) -> None:
        self._source = source
        self._log_path = log_path

    def handle(self, request: Request) -> HandlerResult:
        demoting = _required_id(request, "demoting_strategy_id")
        candidate = _required_id(request, "candidate_strategy_id")
        if demoting == candidate:
            raise InterfaceError(
                ErrorCategory.BAD_REQUEST,
                f"demoting and candidate are the same strategy ({demoting!r}); a swap "
                "trigger must name two different strategies",
                type="SAME_STRATEGY_SWAP",
            )
        # Defense in depth under the transport's requires-confirmation guard: a future
        # dispatch path must not reach the binary unconfirmed.
        if not request.confirmed:
            raise InterfaceError(
                ErrorCategory.CONFIRMATION_REQUIRED,
                f"firing a manual Hot-Swap trigger for {candidate!r} requires the explicit "
                "confirmation control (SyRS SYS-49a)",
                type="MANUAL_TRIGGER_UNCONFIRMED",
                detail={"srs_refs": ["SRS-RESV-003", "SYS-49a"]},
            )

        completed = self._source._invoke(  # noqa: SLF001 — same-module collaborator
            [
                "manual",
                "--demoting",
                demoting,
                "--candidate",
                candidate,
                "--log",
                self._log_path,
            ]
        )
        if completed.returncode != 0:
            # The binary exits nonzero when the required audit record was REJECTED. That is
            # not an incidental logging failure: an unlogged trigger is not actionable and
            # must never be reported as fired.
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                f"manual Hot-Swap trigger for {candidate!r} was not logged and therefore "
                f"did not fire: {completed.stderr.strip()}",
                type="MANUAL_TRIGGER_UNLOGGED",
                detail={"stderr": completed.stderr.strip()},
            )

        values = parse_trigger_cli_output(completed.stdout)
        if not strict_trigger_bool(values, "manual-logged"):
            # A zero exit that nonetheless reports the record unlogged is a contradiction;
            # refuse rather than pick the friendlier half.
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                f"manual Hot-Swap trigger for {candidate!r} exited cleanly but reports its "
                "audit record unlogged; refusing to report it as fired",
                type="MANUAL_TRIGGER_UNLOGGED",
            )
        ordinal = values.get("trigger-record-ordinal", "")
        if not ordinal.isdigit() or int(ordinal) < 1:
            # Success is bound to the durable record, not to the exit code: without the
            # ordinal there is no artefact a later reader could go and find.
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                f"manual Hot-Swap trigger for {candidate!r} produced no durable log ordinal "
                "(refusing to report a trigger that cannot be evidenced)",
                type="MANUAL_TRIGGER_UNEVIDENCED",
            )

        fired = values.get("fired", "")
        return HandlerResult(
            200,
            {
                "trigger_kind": fired.split(" ", 1)[0] if fired else "",
                "trigger_id": ordinal,
                "logged": True,
                "demoting_strategy_id": demoting,
                "candidate_strategy_id": candidate,
                "rationale": _rationale_of(fired),
                # The load-bearing honesty field: a fired trigger is a logged PROPOSAL.
                "execution": {
                    "state": "DEFERRED",
                    "owner": EXECUTION_OWNER,
                    "detail": (
                        "the trigger was recorded; no strategy was demoted or promoted "
                        "(swap execution is unbuilt)"
                    ),
                },
            },
        )


def mount_hot_swap_triggers(
    runtime: OperatorInterfaceRuntime,
    *,
    state_path: str | Path,
    log_path: str | Path,
    binary: str | Path | None = None,
    runner: HotSwapTriggerCliRunner | None = None,
    timeout: float | None = None,
) -> CliHotSwapTriggerSource:
    """Register the ``SRS-RESV-003`` trigger behaviour on ``runtime`` (opt-in composition,
    exactly like :func:`atp_orchestration.mount_rollback` and ``atp_dashboard.mount_dashboard``).

    Returns the source so the composing process can hand the SAME instance to
    ``HotSwapStatusProvider`` — the dashboard pane and the REST routes then read one
    configuration through one code path, and cannot disagree about it.

    Deliberately does NOT register ``POST /api/v1/hot-swap``: that is swap EXECUTION,
    owned by the unbuilt ``SRS-RESV-004``/``005``, and it keeps its structured 501.
    """

    source = CliHotSwapTriggerSource(
        state_path,
        binary=binary,
        runner=runner,
        timeout=timeout,
        log_path=log_path,
    )
    config_handler = TriggerConfigHandler(source, state_path=str(state_path))
    runtime.registry.register(REST_TRIGGER_CONFIG_GET, config_handler)
    runtime.registry.register(REST_TRIGGER_CONFIG_PUT, config_handler)
    runtime.registry.register(
        REST_TRIGGER_MANUAL, ManualTriggerHandler(source, log_path=str(log_path))
    )
    return source


# --------------------------------------------------------------------------- #
# Request/response helpers
# --------------------------------------------------------------------------- #


def _rest_config_body(config: dict[str, object] | None) -> dict[str, object]:
    """The GET body for a configuration that may never have been set.

    ``config_source`` is what keeps the two states apart on the wire: ``default`` means
    nothing was ever configured (so the disabled values are the honest default), while
    ``persisted`` means an operator chose them. A caller that cannot tell those apart cannot
    tell "off by default" from "deliberately turned off".
    """

    if config is None:
        return {
            "config_source": "default",
            "manual_promotion_available": True,
            "drawdown_demotion_enabled": False,
            "drawdown_demotion_threshold_bps": None,
            "top_ranked_promotion_enabled": False,
            "highest_momentum_promotion_enabled": False,
            "any_automatic_enabled": False,
            "default_disabled": True,
        }
    # The wire keeps the flat field names the frozen contract declares; the pane's nested
    # per-kind shape is an internal detail of the source, not of the REST route.
    drawdown = config["drawdown_demotion"]
    top_ranked = config["top_ranked_promotion"]
    momentum = config["highest_momentum_promotion"]
    assert isinstance(drawdown, dict) and isinstance(top_ranked, dict)
    assert isinstance(momentum, dict)
    return {
        "config_source": "persisted",
        "manual_promotion_available": config["manual_promotion_available"],
        "drawdown_demotion_enabled": drawdown["enabled"],
        "drawdown_demotion_threshold_bps": drawdown.get("threshold_bps"),
        "top_ranked_promotion_enabled": top_ranked["enabled"],
        "highest_momentum_promotion_enabled": momentum["enabled"],
        "any_automatic_enabled": config["any_enabled"],
        "default_disabled": config["default_disabled"],
    }


def _optional_bool(body: dict[str, object], key: str) -> bool | None:
    """Read an optional boolean request field, refusing a coerced one.

    ``"true"``/``1`` are NOT booleans here. A caller that meant to arm an automatic Hot-Swap
    must say so with a real boolean, or the request is ambiguous about the most consequential
    field it carries.
    """

    if key not in body:
        return None
    value = body[key]
    if isinstance(value, bool):
        return value
    raise InterfaceError(
        ErrorCategory.BAD_REQUEST,
        f"{key} must be a JSON boolean (got {value!r})",
        type="NON_BOOLEAN_TRIGGER_FLAG",
    )


def _positive_int(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InterfaceError(
            ErrorCategory.BAD_REQUEST,
            f"drawdown_demotion_threshold_bps must be an integer (got {value!r})",
            type="NON_INTEGER_THRESHOLD",
        )
    if not 1 <= value <= 10_000:
        raise InterfaceError(
            ErrorCategory.BAD_REQUEST,
            f"drawdown_demotion_threshold_bps must be in [1, 10000] bps (got {value})",
            type="THRESHOLD_OUT_OF_RANGE",
        )
    return str(value)


def _required_id(request: Request, key: str) -> str:
    raw = request.body.get(key) or request.query.get(key) or ""
    value = str(raw).strip()
    if not value:
        raise InterfaceError(
            ErrorCategory.BAD_REQUEST,
            f"a manual Hot-Swap trigger requires {key}",
            type="MISSING_STRATEGY_ID",
        )
    return value


def _rationale_of(fired: str) -> str:
    """Pull the ``rationale:`` field out of the bin's single ``fired:`` proof line."""

    marker = "rationale:"
    index = fired.find(marker)
    return fired[index + len(marker) :].strip() if index >= 0 else ""
