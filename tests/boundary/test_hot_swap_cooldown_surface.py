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


def test_the_execution_and_status_routes_stay_deferred(tmp_path) -> None:
    # A deliberate refusal, not an oversight: GET /api/v1/hot-swap/status declares three
    # fields that are SRS-RESV-004/005/003's to produce, and binding it from the cool-down
    # half alone would fabricate them.
    for where in _mount(_StubCli(NEVER_STATUS), tmp_path):
        status, body = _request(where, "GET", "/api/v1/hot-swap/status")
        assert status == 501, body
        assert body["error"]["type"] == "HANDLER_DEFERRED", body

        status, body = _request(where, "POST", "/api/v1/hot-swap?confirm=true", {"x": 1})
        assert status == 501, body
        assert body["error"]["type"] == "HANDLER_DEFERRED", body
