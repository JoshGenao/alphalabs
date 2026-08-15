"""SRS-RESV-006 / SyRS SYS-49e — the cool-down's Python surfaces.

L4 boundary. Drives the REAL handlers and the REAL dashboard leg against a stub CLI
runner, so no cargo build is needed and every case is deterministic.

Two properties dominate this file:

1. **A cool-down refusal is a CONFIRMATION_REQUIRED, not an INTERNAL_ERROR.** Before this
   feature the manual handler mapped *any* non-zero exit to
   ``INTERNAL_ERROR / MANUAL_TRIGGER_UNLOGGED`` — "was not logged and therefore did not
   fire". For a cool-down refusal that is a lie about a system working exactly as
   specified, and it routes the operator to an audit-sink incident that is not happening.
2. **The transport's SYS-49a confirmation does NOT satisfy the SYS-49e acknowledgement.**
   They are two tokens on purpose. One token for both would make every ordinary confirmed
   manual trigger a silent cool-down override, and the operator would never see the warning
   the acceptance criterion exists to show them.
"""

from __future__ import annotations

import http.client
import json
import subprocess
from collections.abc import Iterator

import pytest
from atp_hotswap import (
    CliHotSwapCooldownSource,
    CompositeHotSwapStatusSource,
    HotSwapStatusUnavailable,
    HotSwapTriggerOutputUnreadable,
)
from atp_runtime import OperatorInterfaceRuntime

pytestmark = pytest.mark.boundary


ACTIVE_STATUS = (
    "observed-at-seconds:1715003600\n"
    "cooldown-state:ACTIVE\n"
    "cooldown-in-effect:true\n"
    "cooldown-started-at-seconds:1715000000\n"
    "cooldown-expires-at-seconds:1715604800\n"
    "cooldown-days-default:7\n"
    "cooldown-completion-provisional:false\n"
)
#: The same window, opened by phase one and never confirmed (adversarial review r13).
PROVISIONAL_STATUS = ACTIVE_STATUS.replace(
    "cooldown-completion-provisional:false", "cooldown-completion-provisional:true"
)
NEVER_STATUS = (
    "observed-at-seconds:1715003600\n"
    "cooldown-state:NEVER_SWAPPED\n"
    "cooldown-in-effect:false\n"
    "cooldown-days-default:7\n"
)
UNKNOWN_STATUS = (
    "cooldown-state:UNKNOWN\n"
    "cooldown-in-effect:true\n"
    "cooldown-unreadable-reason:hot-swap cool-down window /x is malformed: file is empty\n"
)

MANUAL_REFUSED = (
    "manual-always-available:true\n"
    "observed-at-seconds:1715003600\n"
    "cooldown-state:ACTIVE\n"
    "cooldown-confirmation-required:true\n"
    "cooldown-confirmed:false\n"
    "cooldown-started-at-seconds:1715000000\n"
    "cooldown-expires-at-seconds:1715604800\n"
    "manual-refused:COOLDOWN_CONFIRMATION_REQUIRED\n"
    "manual-logged:false\n"
    "cooldown-warning:a Hot-Swap cool-down is in effect (SyRS SYS-49e): expires 1715604800s.\n"
    "cooldown-override-available:--confirm-cooldown\n"
)
MANUAL_FIRED = (
    "manual-always-available:true\n"
    "observed-at-seconds:1715003600\n"
    "cooldown-state:ACTIVE\n"
    "cooldown-confirmation-required:true\n"
    "cooldown-confirmed:true\n"
    "cooldown-override:true\n"
    "fired:MANUAL_PROMOTION demoting:live-a candidate:cand-b rationale:manual-selection\n"
    "manual-logged:true\n"
    "log-persisted:/tmp/t.jsonl\n"
    "log-file-records:1\n"
    "trigger-record-ordinal:1\n"
)


class _StubCli:
    """Records the argv it was handed and replays a scripted result."""

    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


# --------------------------------------------------------------------------- #
# The dashboard leg
# --------------------------------------------------------------------------- #


def test_an_active_window_resolves_both_boundaries_as_iso_utc() -> None:
    source = CliHotSwapCooldownSource(
        "/x/cooldown.json", binary="/bin/true", runner=_StubCli(ACTIVE_STATUS)
    )
    state = source.live_state()
    assert state == {
        "cooldown": {
            "in_effect": True,
            # r13: whether the swap that opened this window ever confirmed it. An
            # exact-shape assertion, so a new key cannot be added to the published
            # payload without a reviewer of this file seeing it.
            "completion_provisional": False,
            "started_at": "2024-05-06T12:53:20Z",
            "expires_at": "2024-05-13T12:53:20Z",
        }
    }


def test_never_swapped_resolves_in_effect_without_inventing_timestamps() -> None:
    # There is genuinely no window to date, so the two timestamp cells stay deferred rather
    # than carrying a fabricated instant.
    source = CliHotSwapCooldownSource(
        "/x/cooldown.json", binary="/bin/true", runner=_StubCli(NEVER_STATUS)
    )
    assert source.live_state() == {"cooldown": {"in_effect": False}}


def test_an_unknown_window_is_unavailable_not_in_effect_false() -> None:
    # CLAUDE.md rule 3, on the surface that renders it: a corrupt window reported as
    # "no cool-down" is a false all-clear, and the pane's promote control keys off it.
    source = CliHotSwapCooldownSource(
        "/x/cooldown.json", binary="/bin/true", runner=_StubCli(UNKNOWN_STATUS, returncode=1)
    )
    with pytest.raises(HotSwapStatusUnavailable, match="file is empty"):
        source.live_state()


def test_a_contradictory_proof_stream_is_refused_rather_than_half_believed() -> None:
    # ACTIVE alongside in-effect:false has not said which is true, and picking one would
    # publish a swap-safety state nobody asserted.
    contradictory = "cooldown-state:ACTIVE\ncooldown-in-effect:false\n"
    source = CliHotSwapCooldownSource(
        "/x/cooldown.json", binary="/bin/true", runner=_StubCli(contradictory)
    )
    with pytest.raises(HotSwapStatusUnavailable, match="contradicts itself"):
        source.live_state()


def test_a_window_missing_a_boundary_is_refused() -> None:
    partial = "cooldown-state:ACTIVE\ncooldown-in-effect:true\ncooldown-started-at-seconds:1\n"
    source = CliHotSwapCooldownSource(
        "/x/cooldown.json", binary="/bin/true", runner=_StubCli(partial)
    )
    with pytest.raises(HotSwapStatusUnavailable, match="half-known"):
        source.live_state()


def test_the_cooldown_leg_states_no_fact_it_does_not_own() -> None:
    # The demotion and live-strategy cells belong to SRS-RESV-004/005. Omitting their keys
    # is what keeps them rendering their own owners' deferrals instead of this producer's
    # silence being read as an answer.
    source = CliHotSwapCooldownSource(
        "/x/cooldown.json", binary="/bin/true", runner=_StubCli(ACTIVE_STATUS)
    )
    state = source.live_state()
    assert set(state) == {"cooldown"}
    assert source.trigger_config() is None
    assert source.promotion_candidate() is None


# --------------------------------------------------------------------------- #
# The composite merge
# --------------------------------------------------------------------------- #


class _DemotionLeg:
    def live_state(self) -> dict[str, object]:
        return {"demotion_pending": False, "demotion_detail": "no lockout"}


def test_the_two_live_state_producers_are_merged_not_overwritten() -> None:
    composite = CompositeHotSwapStatusSource(
        demotion=_DemotionLeg(),
        cooldown=CliHotSwapCooldownSource(
            "/x/cooldown.json", binary="/bin/true", runner=_StubCli(ACTIVE_STATUS)
        ),
    )
    state = composite.live_state()
    assert state is not None
    assert state["demotion_pending"] is False
    assert state["cooldown"]["in_effect"] is True


def test_a_cooldown_leg_alone_still_composes() -> None:
    composite = CompositeHotSwapStatusSource(
        cooldown=CliHotSwapCooldownSource(
            "/x/cooldown.json", binary="/bin/true", runner=_StubCli(NEVER_STATUS)
        )
    )
    assert composite.live_state() == {"cooldown": {"in_effect": False}}


def test_no_legs_mounted_keeps_every_live_cell_deferred() -> None:
    assert CompositeHotSwapStatusSource().live_state() is None


def test_two_producers_answering_the_same_key_is_refused() -> None:
    # One of them is reporting a fact it does not own, and silently picking a winner would
    # publish whichever happened to run second.
    class _Overreaching:
        def live_state(self) -> dict[str, object]:
            return {"cooldown": {"in_effect": False}}

    composite = CompositeHotSwapStatusSource(
        demotion=_Overreaching(),
        cooldown=CliHotSwapCooldownSource(
            "/x/cooldown.json", binary="/bin/true", runner=_StubCli(ACTIVE_STATUS)
        ),
    )
    with pytest.raises(HotSwapStatusUnavailable, match="does not own"):
        composite.live_state()


# --------------------------------------------------------------------------- #
# The REST manual arm
# --------------------------------------------------------------------------- #


def _mount(stub: _StubCli, tmp_path) -> Iterator[tuple[str, int]]:
    from atp_orchestration import mount_hot_swap_triggers

    runtime = OperatorInterfaceRuntime()
    mount_hot_swap_triggers(
        runtime,
        state_path=tmp_path / "triggers.json",
        log_path=tmp_path / "triggers.jsonl",
        cooldown_state_path=tmp_path / "cooldown.json",
        binary=tmp_path / "fake-bin",
        runner=stub,
    )
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield host, port
    finally:
        runtime.stop()


def _request(
    where: tuple[str, int], method: str, path: str, body: dict | None = None
) -> tuple[int, dict]:
    host, port = where
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read() or b"{}"
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {}
        return response.status, parsed
    finally:
        conn.close()


#: The route always carries the SYS-49a transport confirmation. That is exactly the point of
#: the SYS-49e tests below: this token is ALWAYS present, so if it also meant "override the
#: cool-down" there would be no cool-down left to override.
_MANUAL_PATH = "/api/v1/hot-swap/triggers/manual?confirm=true"


def _fire(where: tuple[str, int], body: dict) -> tuple[int, dict]:
    return _request(where, "POST", _MANUAL_PATH, body)


def test_a_cooldown_refusal_is_confirmation_required_not_internal_error(tmp_path) -> None:
    stub = _StubCli(MANUAL_REFUSED, returncode=1)
    for where in _mount(stub, tmp_path):
        status, body = _fire(
            where, {"demoting_strategy_id": "live-a", "candidate_strategy_id": "cand-b"}
        )
        error = body["error"]
        assert error["type"] == "HOT_SWAP_COOLDOWN_CONFIRMATION_REQUIRED", body
        assert error["category"] == "CONFIRMATION_REQUIRED", body
        # 428 Precondition Required — the runtime's established mapping for that category
        # (atp_runtime/errors.py), the same status the SYS-49a confirmation refusal returns.
        # The point is the CATEGORY: an operator-clearable precondition, not a 5xx.
        assert status == 428, body
        # The operator needs the remedy, not just the refusal.
        assert error["detail"]["override_field"] == "confirm_cooldown", body
        assert error["detail"]["cooldown_expires_at_seconds"] == "1715604800", body
        assert "SYS-49e" in error["message"], body


def test_the_transport_confirmation_does_not_satisfy_the_cooldown_confirmation(tmp_path) -> None:
    # `?confirm=true` is the SYS-49a token and it is ALWAYS on this route. If it also meant
    # "override the cool-down", the flag below would already be on the argv and every
    # ordinary confirmed trigger would silently override a live window.
    stub = _StubCli(MANUAL_REFUSED, returncode=1)
    for where in _mount(stub, tmp_path):
        _fire(where, {"demoting_strategy_id": "live-a", "candidate_strategy_id": "cand-b"})
        assert stub.calls, "the binary must have been invoked"
        assert "--confirm-cooldown" not in stub.calls[0], stub.calls[0]


def test_confirm_cooldown_is_forwarded_to_the_binary_as_a_flag(tmp_path) -> None:
    stub = _StubCli(MANUAL_FIRED)
    for where in _mount(stub, tmp_path):
        status, body = _fire(
            where,
            {
                "demoting_strategy_id": "live-a",
                "candidate_strategy_id": "cand-b",
                "confirm_cooldown": True,
            },
        )
        assert status == 200, body
        argv = stub.calls[0]
        assert "--confirm-cooldown" in argv, argv
        assert "--cooldown-state" in argv, "the window path must always be passed"


def test_a_non_boolean_confirm_cooldown_is_refused_not_coerced(tmp_path) -> None:
    # `bool("false")` is True in Python, so a lenient read would turn an operator's explicit
    # refusal into an override of a live safety window.
    stub = _StubCli(MANUAL_FIRED)
    for where in _mount(stub, tmp_path):
        status, body = _fire(
            where,
            {
                "demoting_strategy_id": "live-a",
                "candidate_strategy_id": "cand-b",
                "confirm_cooldown": "yes please",
            },
        )
        assert status == 400, body
        assert body["error"]["type"] == "NON_BOOLEAN_COOLDOWN_CONFIRMATION", body
        assert not stub.calls, "a malformed override must never reach the binary"


def test_an_unlogged_refusal_is_still_reported_as_an_internal_error(tmp_path) -> None:
    # The control for the first test: a genuine audit-sink failure must KEEP its old
    # classification. The new branch narrows the mapping, it does not replace it.
    stub = _StubCli("manual-logged:false\n", returncode=1, stderr="log store unwritable")
    for where in _mount(stub, tmp_path):
        _status, body = _fire(
            where, {"demoting_strategy_id": "live-a", "candidate_strategy_id": "cand-b"}
        )
        assert body["error"]["type"] == "MANUAL_TRIGGER_UNLOGGED", body


def test_mounting_the_trigger_arm_alone_serves_neither_execution_nor_status(tmp_path) -> None:
    # Composition stays per-arm. Mounting the TRIGGER surface must not make the
    # EXECUTION route answer — a surface that decides and logs must never respond on
    # the endpoint whose success means a swap happened. Execution is bound separately
    # (mount_hot_swap_execution, exercised below), and GET /api/v1/hot-swap/status has
    # a built owner for all four of its fields but no composer: binding it is
    # SRS-API-001's process-main.
    for where in _mount(_StubCli(NEVER_STATUS), tmp_path):
        status, body = _request(where, "GET", "/api/v1/hot-swap/status")
        assert status == 501, body
        assert body["error"]["type"] == "HANDLER_DEFERRED", body

        status, body = _request(where, "POST", "/api/v1/hot-swap?confirm=true", {"x": 1})
        assert status == 501, body
        assert body["error"]["type"] == "HANDLER_DEFERRED", body


# --------------------------------------------------------------------------- #
# The EXECUTION route (adversarial review r2 — `cooldown-execution-bypass`)
# --------------------------------------------------------------------------- #
#
# The trigger arm above was already cool-down aware; the route whose success means
# a swap HAPPENED was not. These pin the transport half of the fix: the flag is
# forwarded, it stays distinct from the SYS-2d confirmation, and the refusal is a
# CONFIRMATION_REQUIRED an operator can clear rather than a malformed request.

SWAP_REFUSED_BY_COOLDOWN = (
    "transports:FIXTURE\n"
    "observed-at-seconds:1715003600\n"
    "cooldown-state:ACTIVE\n"
    "cooldown-in-effect:true\n"
    "cooldown-confirmed:false\n"
    "demotion-outcome:NOT_STARTED\n"
    "promotion:BLOCKED\n"
    "refusal:HOT_SWAP_COOLDOWN_CONFIRMATION_REQUIRED\n"
    "designation-after:live-a\n"
    "designation-persisted:unchanged\n"
    "swap-record-ordinal:-\n"
    "promotion-recorded:not-configured\n"
)
SWAP_PROMOTED = (
    "transports:FIXTURE\n"
    "observed-at-seconds:1715003600\n"
    "cooldown-state:NEVER_SWAPPED\n"
    "cooldown-in-effect:false\n"
    "cooldown-confirmed:false\n"
    "demotion-outcome:FLAT_CONFIRMED\n"
    "promotion:PROMOTED\n"
    "cooldown-window:STARTED\n"
    "cooldown-window-started-at-seconds:1715003600\n"
    "designation-after:cand-b\n"
    "designation-persisted:durable\n"
    "swap-record-ordinal:1\n"
    "promotion-recorded:true\n"
)
#: A swap that COMPLETED and whose window did not start — the fail-open.
SWAP_PROMOTED_NO_WINDOW = SWAP_PROMOTED.replace(
    "cooldown-window:STARTED\ncooldown-window-started-at-seconds:1715003600\n",
    "cooldown-window:NOT_STARTED\n",
)
DESIGNATION_STATUS = "designated:live-a\n"


class _ScriptedCli:
    """Replays one result per call, and records every argv."""

    def __init__(self, *results: tuple[int, str]) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        returncode, stdout = self.results.pop(0)
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    @property
    def swap_argv(self) -> list[str]:
        return next(call for call in self.calls if "swap" in call)


def _mount_execution(stub: _ScriptedCli, tmp_path) -> Iterator[tuple[str, int]]:
    from atp_orchestration import mount_hot_swap_execution

    runtime = OperatorInterfaceRuntime()
    mount_hot_swap_execution(
        runtime,
        state_path=tmp_path / "live.state",
        paper_state_dir=tmp_path / "paper",
        log_path=tmp_path / "promotions.jsonl",
        demotion_lock_path=tmp_path / "demotion-pending.json",
        cooldown_state_path=tmp_path / "cooldown.json",
        fixture_safety_inputs={
            "positions": "flat",
            "deployed_version": "sha256:" + "a" * 64,
            "liquidation": "flat",
        },
        binary=tmp_path / "fake-bin",
        runner=stub,
    )
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield host, port
    finally:
        runtime.stop()


def _swap_request(where: tuple[str, int], body: dict) -> tuple[int, dict]:
    return _request(where, "POST", "/api/v1/hot-swap?confirm=true", body)


def test_the_SHIPPED_entrypoint_composes_the_execution_route(tmp_path) -> None:
    """Adversarial review r9 — implemented is not shipped (`adversarial-precheck` rule 7).

    Every other case here mounts the route itself, so they prove it works WHEN mounted
    and say nothing about whether `python -m atp_dashboard` mounts it. It did not: the
    SPA posts to `/api/v1/hot-swap?confirm=true` and an operator got a generic
    `HANDLER_DEFERRED` 501 naming nobody.

    This drives `serve()`'s OWN composition helpers over an env mapping, which is the
    only thing that can fail when the shipped entrypoint stops composing the route.

    The route is deliberately mounted WITHOUT fixture safety inputs, so it still
    cannot promote — it refuses with `SAFETY_INPUTS_UNAVAILABLE` naming SRS-EXE-006
    and SRS-ORCH-004. Both halves are asserted: that it is SERVED (not deferred), and
    that it REFUSES (not promotes). Declaring fixtures in the shipped path would let
    the dashboard report a promotion decided on a fixture flat-account, which is the
    false green SRS-RESV-005's round-1 review blocked.
    """
    from atp_dashboard.server import _mount_hot_swap_execution_arm

    env = {
        "ATP_HOT_SWAP_DESIGNATION_STATE": str(tmp_path / "live.state"),
        "ATP_HOT_SWAP_PAPER_STATE_DIR": str(tmp_path / "paper"),
        "ATP_HOT_SWAP_PROMOTION_LOG": str(tmp_path / "swaps.jsonl"),
        "ATP_HOT_SWAP_DEMOTION_STATE": str(tmp_path / "demotion-pending.json"),
        "ATP_HOT_SWAP_COOLDOWN_STATE": str(tmp_path / "cooldown.json"),
    }
    runtime = OperatorInterfaceRuntime()
    _mount_hot_swap_execution_arm(runtime, env)
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        status, body = _request(
            (host, port), "POST", "/api/v1/hot-swap?confirm=true", {"candidate_strategy_id": "c"}
        )
    finally:
        runtime.stop()

    # SERVED — not the generic unbound-handler 501.
    assert body["error"]["type"] != "HANDLER_DEFERRED", body
    # ...and REFUSING, with the owners of the two facts it cannot prove.
    assert status == 501, body
    assert body["error"]["type"] == "SAFETY_INPUTS_UNAVAILABLE", body
    owner = body["error"]["detail"]["owner"]
    assert "SRS-EXE-006" in owner and "SRS-ORCH-004" in owner, body


def test_the_shipped_arm_refuses_to_start_without_its_cooldown_window(tmp_path) -> None:
    # The window is load-bearing on this surface — it gates the swap AND records the
    # completion that starts the next one. A surface that could execute a swap it
    # cannot cool down must not come up claiming it can, so a missing knob is a boot
    # failure that names the cause once rather than once per request.
    from atp_dashboard.server import _mount_hot_swap_execution_arm

    env = {
        "ATP_HOT_SWAP_DESIGNATION_STATE": str(tmp_path / "live.state"),
        "ATP_HOT_SWAP_PAPER_STATE_DIR": str(tmp_path / "paper"),
        "ATP_HOT_SWAP_PROMOTION_LOG": str(tmp_path / "swaps.jsonl"),
        "ATP_HOT_SWAP_DEMOTION_STATE": str(tmp_path / "demotion-pending.json"),
        # ATP_HOT_SWAP_COOLDOWN_STATE deliberately absent.
    }
    with pytest.raises(ValueError) as caught:
        _mount_hot_swap_execution_arm(OperatorInterfaceRuntime(), env)
    assert "ATP_HOT_SWAP_COOLDOWN_STATE" in str(caught.value)
    assert "SYS-49e" in str(caught.value)


def test_the_execution_route_forwards_the_cooldown_state_on_every_swap(tmp_path) -> None:
    # Unconditional, never optional: a route that could skip the window would BE the
    # execution bypass this gate closes.
    stub = _ScriptedCli((0, DESIGNATION_STATUS), (0, SWAP_PROMOTED))
    for where in _mount_execution(stub, tmp_path):
        status, body = _swap_request(where, {"candidate_strategy_id": "cand-b"})
        assert status == 200, body
        argv = stub.swap_argv
        assert "--cooldown-state" in argv, argv
        assert argv[argv.index("--cooldown-state") + 1].endswith("cooldown.json"), argv
        assert "--confirm-cooldown" not in argv, "an override must be asked for"
        assert body["cooldown_window"] == "STARTED", body


def test_an_execution_cooldown_refusal_is_confirmation_required(tmp_path) -> None:
    # NOT a BAD_REQUEST. The request was well-formed and the caller can clear it by
    # re-sending — reporting it as malformed hides the one action that completes it.
    stub = _ScriptedCli((0, DESIGNATION_STATUS), (1, SWAP_REFUSED_BY_COOLDOWN))
    for where in _mount_execution(stub, tmp_path):
        status, body = _swap_request(where, {"candidate_strategy_id": "cand-b"})
        error = body["error"]
        assert error["category"] == "CONFIRMATION_REQUIRED", body
        assert error["type"] == "HOT_SWAP_COOLDOWN_CONFIRMATION_REQUIRED", body
        assert status == 428, body
        assert error["detail"]["confirm_field"] == "confirm_cooldown", body
        assert error["detail"]["owner"] == "SRS-RESV-006", body
        assert "SYS-49e" in error["message"], body


def test_the_transport_confirmation_does_not_waive_the_execution_cooldown(tmp_path) -> None:
    # `?confirm=true` is ALWAYS on this route (it is `requires_confirmation`). If it
    # also meant "override the cool-down", every ordinary confirmed swap would be a
    # silent override and there would be no window left to override.
    stub = _ScriptedCli((0, DESIGNATION_STATUS), (1, SWAP_REFUSED_BY_COOLDOWN))
    for where in _mount_execution(stub, tmp_path):
        _swap_request(where, {"candidate_strategy_id": "cand-b"})
        assert "--confirm-cooldown" not in stub.swap_argv


def test_confirm_cooldown_is_forwarded_to_the_swap_binary(tmp_path) -> None:
    stub = _ScriptedCli((0, DESIGNATION_STATUS), (0, SWAP_PROMOTED))
    for where in _mount_execution(stub, tmp_path):
        status, _ = _swap_request(
            where, {"candidate_strategy_id": "cand-b", "confirm_cooldown": True}
        )
        assert status == 200
        assert "--confirm-cooldown" in stub.swap_argv


def test_a_non_boolean_execution_confirm_cooldown_is_refused_not_coerced(tmp_path) -> None:
    # "false" is a truthy string; 0 is falsy. The one field that waives a seven-day
    # safety window is not inferred from a value's truthiness.
    stub = _ScriptedCli((0, DESIGNATION_STATUS))
    for where in _mount_execution(stub, tmp_path):
        status, body = _swap_request(
            where, {"candidate_strategy_id": "cand-b", "confirm_cooldown": "true"}
        )
        assert status == 400, body
        assert body["error"]["type"] == "INVALID_CONFIRM_COOLDOWN", body
        assert not [call for call in stub.calls if "swap" in call], (
            "a refused request must not have reached the binary"
        )


def test_a_swap_whose_window_did_not_start_is_not_a_clean_success(tmp_path) -> None:
    # The fail-open: the candidate IS live, so this is a 200 (a non-2xx would say
    # nothing mutated and invite a retry over a changed live slot) — but not a clean
    # `PROMOTED`, because the triggers this swap should have suppressed are armed.
    stub = _ScriptedCli((0, DESIGNATION_STATUS), (1, SWAP_PROMOTED_NO_WINDOW))
    for where in _mount_execution(stub, tmp_path):
        status, body = _swap_request(where, {"candidate_strategy_id": "cand-b"})
        assert status == 200, body
        assert body["promotion_state"] == "PROMOTED_COOLDOWN_NOT_STARTED", body
        assert body["cooldown_window"] == "NOT_STARTED", body


def test_a_binary_that_reports_no_window_at_all_is_unknown_never_started(tmp_path) -> None:
    # A stale or truncated binary has not evidenced a window. Absent must not read as
    # STARTED (CLAUDE.md rule 3) — that would report a cool-down nobody opened.
    stripped = SWAP_PROMOTED.replace(
        "cooldown-window:STARTED\ncooldown-window-started-at-seconds:1715003600\n", ""
    )
    stub = _ScriptedCli((0, DESIGNATION_STATUS), (0, stripped))
    for where in _mount_execution(stub, tmp_path):
        status, body = _swap_request(where, {"candidate_strategy_id": "cand-b"})
        assert status == 200, body
        assert body["cooldown_window"] == "UNKNOWN", body
        assert body["promotion_state"] == "PROMOTED_COOLDOWN_NOT_STARTED", body


def test_an_unrecordable_window_refuses_the_swap_as_an_operator_repair(tmp_path) -> None:
    # Adversarial review r4 [critical]. Distinct from the CONFIRMATION refusal: no
    # amount of confirming makes an unwritable store writable, so routing this to
    # `confirm_cooldown` would send the operator in a circle. It is also not a
    # BAD_REQUEST — the caller did nothing wrong.
    unrecordable = SWAP_REFUSED_BY_COOLDOWN.replace(
        "refusal:HOT_SWAP_COOLDOWN_CONFIRMATION_REQUIRED",
        "refusal:HOT_SWAP_COOLDOWN_UNRECORDABLE",
    )
    stub = _ScriptedCli((0, DESIGNATION_STATUS), (1, unrecordable))
    for where in _mount_execution(stub, tmp_path):
        status, body = _swap_request(where, {"candidate_strategy_id": "cand-b"})
        error = body["error"]
        assert error["type"] == "HOT_SWAP_COOLDOWN_UNRECORDABLE", body
        assert error["category"] == "INTERNAL_ERROR", body
        assert error["detail"]["owner"] == "SRS-RESV-006", body
        # The remedy must be the RIGHT one — never "re-send with confirm_cooldown".
        assert "confirm_cooldown" not in error["message"], error["message"]
        assert "Repair the cool-down state file" in error["message"], error["message"]
        assert "Nothing was demoted" in error["message"], error["message"]


def test_a_blocked_swap_still_declares_its_cooldown_window_field(tmp_path) -> None:
    # Present on EVERY 200, including a refusal: an optional field would make the
    # commonest degraded response a subset of what the published schema advertises.
    blocked = SWAP_REFUSED_BY_COOLDOWN.replace(
        "demotion-outcome:NOT_STARTED", "demotion-outcome:FLAT_CONFIRMED"
    ).replace("refusal:HOT_SWAP_COOLDOWN_CONFIRMATION_REQUIRED", "refusal:LIVE_POSITIONS_OPEN")
    stub = _ScriptedCli((0, DESIGNATION_STATUS), (1, blocked))
    for where in _mount_execution(stub, tmp_path):
        status, body = _swap_request(where, {"candidate_strategy_id": "cand-b"})
        assert status == 200, body
        assert body["promotion_state"] == "BLOCKED", body
        # Present AND correct. Asserting presence alone is what let the field's own
        # docstring drift into claiming NOT_STARTED here (adversarial review r21): the
        # promote CLI emits its `cooldown-window` line on the success arm only, and an
        # absent line is UNKNOWN — never STARTED, and not NOT_STARTED either, because
        # "no window was due" is a different claim from "we could not tell".
        assert body["cooldown_window"] == "UNKNOWN", body


def test_a_provisional_window_is_published_as_provisional() -> None:
    """Adversarial review r13 — the interrupted swap reaches the operator's pane.

    A window opened by phase one and never confirmed suppresses exactly like a real
    one, which is the safe direction. But only an operator can find out whether the
    candidate actually went live, and a pane that renders the two identically leaves
    them nothing to act on.
    """
    source = CliHotSwapCooldownSource(
        "/x/cooldown.json", binary="/bin/true", runner=_StubCli(PROVISIONAL_STATUS)
    )
    cooldown = source.live_state()["cooldown"]
    assert cooldown["in_effect"] is True, "a provisional window still suppresses"
    assert cooldown["completion_provisional"] is True


def test_a_confirmed_window_is_published_as_confirmed() -> None:
    # The non-vacuity control: a surface that reported `True` unconditionally would
    # satisfy the case above and train an operator to ignore the flag entirely.
    source = CliHotSwapCooldownSource(
        "/x/cooldown.json", binary="/bin/true", runner=_StubCli(ACTIVE_STATUS)
    )
    assert source.live_state()["cooldown"]["completion_provisional"] is False


def test_an_unanswerable_provisional_flag_is_never_published_as_confirmed() -> None:
    # CLAUDE.md rule 3 at the boundary. `unknown` means the store could not answer,
    # and publishing that as `False` would render an unreadable window as a healthy
    # completed swap — the same fail-open one layer out.
    unknown = ACTIVE_STATUS.replace(
        "cooldown-completion-provisional:false", "cooldown-completion-provisional:unknown"
    )
    source = CliHotSwapCooldownSource(
        "/x/cooldown.json", binary="/bin/true", runner=_StubCli(unknown)
    )
    assert source.live_state()["cooldown"]["completion_provisional"] is None


def test_a_provisional_flag_the_surface_does_not_understand_is_refused() -> None:
    # A third value would be a fact this build cannot honour; rendering the rest of
    # the window while dropping it silently is the fail-open direction.
    garbled = ACTIVE_STATUS.replace(
        "cooldown-completion-provisional:false", "cooldown-completion-provisional:maybe"
    )
    source = CliHotSwapCooldownSource(
        "/x/cooldown.json", binary="/bin/true", runner=_StubCli(garbled)
    )
    with pytest.raises(HotSwapTriggerOutputUnreadable, match="unknown provisional flag"):
        source.live_state()


def test_a_display_only_deployment_still_boots(tmp_path) -> None:
    """Adversarial review r21 [block] — the arm's opt-in must be about the arm.

    ``ATP_HOT_SWAP_DESIGNATION_STATE`` is the pane's live-strategy DISPLAY knob, and
    the composition contract in ``server.py`` promises four independent legs: "any one
    composes without the others; unset, a leg is absent and its cells keep their
    deferred placeholder." Opting the execution ROUTE in on that knob broke the
    promise and, worse, was a boot regression — a deployment that had always set only
    the display knob suddenly raised out of ``serve()`` demanding four more variables
    for a route it had never asked for.
    """
    from atp_dashboard.server import _mount_hot_swap_execution_arm

    runtime = OperatorInterfaceRuntime()
    _mount_hot_swap_execution_arm(runtime, {"ATP_HOT_SWAP_DESIGNATION_STATE": str(tmp_path / "s")})

    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        status, body = _request(
            (host, port), "POST", "/api/v1/hot-swap?confirm=true", {"candidate_strategy_id": "c"}
        )
    finally:
        runtime.stop()
    # Absent, not broken: the route keeps the structured 501 the frozen contract gives
    # every unbound operation, which is what "a leg is absent" means here.
    assert status == 501, body
    assert body["error"]["type"] == "HANDLER_DEFERRED", body


def test_naming_the_promotion_journal_opts_the_route_in(tmp_path) -> None:
    # The non-vacuity control. An arm that never mounted would satisfy the case above
    # and ship the r9 defect — a control an operator can see and a route nobody bound.
    from atp_dashboard.server import _mount_hot_swap_execution_arm

    env = {
        "ATP_HOT_SWAP_PROMOTION_LOG": str(tmp_path / "swaps.jsonl"),
        "ATP_HOT_SWAP_DESIGNATION_STATE": str(tmp_path / "live.state"),
        "ATP_HOT_SWAP_PAPER_STATE_DIR": str(tmp_path / "paper"),
        "ATP_HOT_SWAP_DEMOTION_STATE": str(tmp_path / "demotion-pending.json"),
        "ATP_HOT_SWAP_COOLDOWN_STATE": str(tmp_path / "cooldown.json"),
    }
    runtime = OperatorInterfaceRuntime()
    _mount_hot_swap_execution_arm(runtime, env)
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        status, body = _request(
            (host, port), "POST", "/api/v1/hot-swap?confirm=true", {"candidate_strategy_id": "c"}
        )
    finally:
        runtime.stop()
    assert body["error"]["type"] != "HANDLER_DEFERRED", body


def test_opting_in_without_the_designation_record_is_a_boot_failure(tmp_path) -> None:
    # The designation state moved from opt-in to REQUIREMENT, so the loud failure it
    # used to provide has to still exist — a route that could execute a swap with
    # nothing durably designated must not come up claiming it can.
    from atp_dashboard.server import _mount_hot_swap_execution_arm

    env = {
        "ATP_HOT_SWAP_PROMOTION_LOG": str(tmp_path / "swaps.jsonl"),
        "ATP_HOT_SWAP_PAPER_STATE_DIR": str(tmp_path / "paper"),
        "ATP_HOT_SWAP_DEMOTION_STATE": str(tmp_path / "demotion-pending.json"),
        "ATP_HOT_SWAP_COOLDOWN_STATE": str(tmp_path / "cooldown.json"),
        # ATP_HOT_SWAP_DESIGNATION_STATE deliberately absent.
    }
    with pytest.raises(ValueError) as caught:
        _mount_hot_swap_execution_arm(OperatorInterfaceRuntime(), env)
    assert "ATP_HOT_SWAP_DESIGNATION_STATE" in str(caught.value)
