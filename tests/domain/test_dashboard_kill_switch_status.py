"""L7 domain — the UI-4 kill-switch pane must never claim an all-clear.

This is the display an operator reads to answer "did the liquidation actually
happen?". The trading-safety invariant is one-directional: the pane may
under-claim (say UNKNOWN when something in fact succeeded) but it may NEVER
over-claim. A fabricated "IB DISCONNECTED", a stale green leg left on screen
after the feed died, or an unreadable state directory rendering as "never
activated" all tell an operator that live positions are closed when they may
not be.

Pinned here, on both sides of the wire:

* **server** — no snapshot may carry a resolved leg it did not substantiate;
  unknown is ``None``, never ``[]`` and never ``False``; mounting the pane adds
  no mutating surface and does not weaken the SYS-44a confirmation guard;
* **client** — every degraded branch of the poll clears the whole rail, and a
  rung renders resolved only when the payload's live value agrees with its
  status (the server cannot talk the client into drawing a green leg).

SRS trace: ``UI-4``, ``SRS-SAFE-001`` (SYS-44a sequence + confirmation),
``SRS-SAFE-002`` (SYS-44b timeout), ``SRS-UI-001`` (read-only dashboard).
"""

from __future__ import annotations

import http.client
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from atp_dashboard import (
    KILL_SWITCH_SNAPSHOT_PATH,
    DurableKillSwitchStatusSource,
    KillSwitchStatusProvider,
    ReadinessBackedProvider,
    mount_dashboard,
)
from atp_dashboard.provider import DEFERRED
from atp_runtime import OperatorInterfaceRuntime

pytestmark = [pytest.mark.domain, pytest.mark.safety]

_APP_JS = Path(__file__).resolve().parents[2] / "python/atp_dashboard/assets/app.js"

#: Statuses a rung may carry. Anything outside this set is a rendering the pane
#: has no vocabulary for — and therefore must not invent.
_STATUSES = {"UNKNOWN", "NOT_ATTEMPTED", "SUCCEEDED", "FAILED", "MIXED"}


@pytest.fixture()
def mounted() -> Iterator[tuple[OperatorInterfaceRuntime, str, int]]:
    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(
        runtime, ReadinessBackedProvider({}), kill_switch=KillSwitchStatusProvider()
    )
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield runtime, host, port
    finally:
        publisher.stop()
        runtime.stop()


def _request(host: str, port: int, method: str, path: str) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        raw = response.read() or b"{}"
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        return response.status, body
    finally:
        conn.close()


def _assert_never_over_claims(snapshot: dict) -> None:
    """The core invariant, applied to any snapshot the pane may be handed."""

    for leg in snapshot["sequence"]:
        assert leg["status"] in _STATUSES, f"{leg['phase']} carries an unknown status vocabulary"
        if str(leg.get("data_source", "")).startswith(DEFERRED):
            # A deferred cell may never carry a value — the SRS-UI-001
            # no-fabrication convention, applied to the safety pane.
            assert leg["value"] is None, f"{leg['phase']} fabricated a deferred value"
            assert leg["status"] == "UNKNOWN", f"{leg['phase']} resolved a deferred leg"
        else:
            # A resolved leg must substantiate itself: the live value has to
            # agree with the status it renders.
            assert leg["value"] == leg["status"], f"{leg['phase']} value/status disagree"
            assert leg["status"] != "UNKNOWN"
    # Unknown collections are None, never the all-clear-shaped empty list.
    assert snapshot["orders"] is None or isinstance(snapshot["orders"], list)
    assert snapshot["activated"] in (True, False, None)
    assert snapshot["tier"] in ("FIXTURE", "LIVE", None)


# --------------------------------------------------------------------------- #
# Server side
# --------------------------------------------------------------------------- #


def test_an_unobservable_kill_switch_never_renders_an_all_clear(mounted) -> None:
    _, host, port = mounted
    status, snapshot = _request(host, port, "GET", KILL_SWITCH_SNAPSHOT_PATH)

    assert status == 200
    _assert_never_over_claims(snapshot)
    # With nothing configured every leg — cancellation, liquidation, timeout,
    # notification, disconnect — is explicitly UNKNOWN, and the pane does not
    # claim the kill switch was never activated.
    assert {leg["status"] for leg in snapshot["sequence"]} == {"UNKNOWN"}
    assert snapshot["activated"] is None
    assert snapshot["orders"] is None


def test_a_corrupt_activation_record_never_reads_as_never_activated(tmp_path: Path) -> None:
    # The single most dangerous misreading: state we cannot parse rendered as
    # "the kill switch has not fired". The sequence may in fact have run.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "kill_switch_last_activation.json").write_text("}{", encoding="utf-8")
    provider = KillSwitchStatusProvider(
        DurableKillSwitchStatusSource(state_dir=state_dir, log_dir=tmp_path)
    )

    snapshot = provider.kill_switch_snapshot()

    assert snapshot["activated"] is not False
    assert snapshot["activated"] is None
    assert snapshot["ok"] is False
    _assert_never_over_claims(snapshot)


def test_an_unsubstantiated_record_never_reads_as_an_activation(tmp_path: Path) -> None:
    # The mirror of the corruption case: a record that PARSES but proves
    # nothing must not put the pane into a "the kill switch fired" state. Both
    # directions of the tri-state have to fail closed — over-claiming an
    # activation is as misleading as denying one.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "kill_switch_last_activation.json").write_text("{}", encoding="utf-8")
    provider = KillSwitchStatusProvider(
        DurableKillSwitchStatusSource(state_dir=state_dir, log_dir=tmp_path)
    )

    snapshot = provider.kill_switch_snapshot()

    assert snapshot["activated"] is not True
    assert snapshot["activated"] is None
    assert snapshot["ok"] is False
    _assert_never_over_claims(snapshot)


def test_evidence_is_never_shown_under_a_different_activations_receipt(tmp_path: Path) -> None:
    # Durable-state drift: a record whose report belongs to a DIFFERENT
    # activation than its receipt. Rendering its cancellation / liquidation /
    # disconnect legs would give the operator false post-liquidation evidence
    # attributed to the wrong activation. Attribute nothing.
    import json as _json

    from atp_safety.state import persist_last_activation

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    persist_last_activation(
        state_dir,
        {
            "activation_id": "act-receipt",
            "response": {
                "activation_id": "act-receipt",
                "activated_at": "2026-07-21T00:00:00.000+00:00",
                "cancelled_orders": [],
                "liquidation_orders": [],
                "paper_engines_halted": 0,
                "ib_gateway_disconnected": True,
            },
            "report": {
                # ...but the evidence came from somewhere else.
                "activation_id": "act-a-different-run",
                "activated_at_epoch_ms": 1,
                "paper_halt": {"status": "SUCCEEDED"},
                "paper_halt_summary": {"engines_total": 0, "transitioned": 0, "already_halted": 0},
                "resting_order_cancels": [],
                "liquidations": [],
                "ib_disconnect": {"status": "SUCCEEDED"},
                "timings": {"liquidations_submitted_ms": 5},
                "within_nfr_p3": True,
                "all_engines_halted": True,
            },
            "ran_clean": True,
            "audit_recorded": True,
            "halted_log_latency_ms": 10.0,
            "persisted_at_ns": 1,
        },
    )
    snapshot = KillSwitchStatusProvider(
        DurableKillSwitchStatusSource(state_dir=state_dir, log_dir=tmp_path)
    ).kill_switch_snapshot()

    assert snapshot["activated"] is None
    assert snapshot["ok"] is False
    assert snapshot["activation_id"] is None
    # Neither id appears as an accepted receipt anywhere in the payload.
    assert "act-a-different-run" not in _json.dumps(snapshot["sequence"])
    _assert_never_over_claims(snapshot)


def test_an_incomplete_record_never_claims_the_gateway_is_disconnected(tmp_path: Path) -> None:
    # The single worst over-claim this pane could make. A truncated or
    # version-skewed activation record carrying a SUCCEEDED disconnect CALL but
    # no trustworthy `ib_gateway_disconnected` proof must render UNKNOWN: the
    # operator must not be told the broker link is closed when it may be open
    # with live positions behind it.
    from atp_safety.state import persist_last_activation

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    persist_last_activation(
        state_dir,
        {
            "activation_id": "act-truncated",
            "response": {
                "activation_id": "act-truncated",
                "activated_at": "2026-07-21T00:00:00.000+00:00",
                "cancelled_orders": [],
                "liquidation_orders": [],
                "paper_engines_halted": 0,
                # ib_gateway_disconnected ABSENT — the proof never landed.
            },
            "report": {
                "activation_id": "act-truncated",
                "activated_at_epoch_ms": 1,
                "paper_halt": {"status": "SUCCEEDED"},
                "paper_halt_summary": {"engines_total": 0, "transitioned": 0, "already_halted": 0},
                "resting_order_cancels": [],
                "liquidations": [],
                "ib_disconnect": {"status": "SUCCEEDED"},
                "timings": {"liquidations_submitted_ms": 5},
                "within_nfr_p3": True,
                "all_engines_halted": True,
            },
            "ran_clean": True,
            "audit_recorded": True,
            "halted_log_latency_ms": 10.0,
            "persisted_at_ns": 1,
        },
    )
    snapshot = KillSwitchStatusProvider(
        DurableKillSwitchStatusSource(state_dir=state_dir, log_dir=tmp_path)
    ).kill_switch_snapshot()

    disconnect = next(leg for leg in snapshot["sequence"] if leg["phase"] == "disconnect")
    assert disconnect["status"] == "UNKNOWN"
    assert disconnect["value"] is None
    assert "disconnected" not in str(disconnect["detail"]).lower().replace(
        "gateway-disconnected", ""
    )
    _assert_never_over_claims(snapshot)


def test_mounting_the_pane_adds_no_mutating_surface(mounted) -> None:
    _, host, port = mounted
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        assert _request(host, port, method, KILL_SWITCH_SNAPSHOT_PATH)[0] in (404, 405)
    # And the read-only guarantee for the dashboard namespace as a whole holds.
    assert _request(host, port, "POST", "/dashboard")[0] in (404, 405)


def test_the_pane_does_not_weaken_the_sys44a_confirmation_guard(mounted) -> None:
    _, host, port = mounted
    # Unconfirmed activation is still refused pre-handler…
    status, body = _request(host, port, "POST", "/api/v1/kill-switch")
    assert status == 428
    assert body["error"]["category"] == "CONFIRMATION_REQUIRED"
    # …and the confirmed POST on this un-wired runtime is still the honest 501,
    # never a silent success the pane would then have to explain.
    status, body = _request(host, port, "POST", "/api/v1/kill-switch?confirm=true")
    assert status == 501
    assert body["error"]["type"] == "HANDLER_DEFERRED"
    assert body["error"]["detail"]["owner"] == "SRS-SAFE-001"


# --------------------------------------------------------------------------- #
# Client side — the rendering rules that keep a dead feed from looking healthy
# --------------------------------------------------------------------------- #


def test_the_activation_control_targets_only_the_contract_route() -> None:
    app_js = _APP_JS.read_text(encoding="utf-8")

    assert 'const KILL_SWITCH_ROUTE = "/api/v1/kill-switch?confirm=true";' in app_js
    fetch_targets = [line for line in app_js.splitlines() if "fetch(" in line and "api/v1" in line]
    assert all(
        "kill-switch" not in target or "KILL_SWITCH_ROUTE" in target for target in fetch_targets
    )
    # The status pane is a READ under the dashboard namespace, and the poll
    # never becomes a second kill path.
    assert 'const KILL_SWITCH_STATUS_ROUTE = "/dashboard/api/kill-switch";' in app_js
    assert app_js.count("/api/v1/kill-switch") == 1


def test_the_client_resolves_a_rung_only_when_the_payload_substantiates_it() -> None:
    # The client-side half of the no-over-claim invariant: a status string alone
    # never draws a resolved rung — the payload must also carry a live value
    # that agrees with it.
    app_js = _APP_JS.read_text(encoding="utf-8")

    assert 'typeof leg.value === "string" && leg.value === leg.status' in app_js
    assert 'leg.status !== "UNKNOWN"' in app_js


def test_every_degraded_poll_branch_clears_the_whole_rail() -> None:
    # The false-all-clear class: a failing/absent/stalled feed must not leave
    # the previous (possibly green) sequence on screen. Each degraded branch of
    # the poll routes to the single fail-closed clear.
    app_js = _APP_JS.read_text(encoding="utf-8")
    start = app_js.index("async function pollKillSwitchOnce()")
    body = app_js[start : app_js.index("async function pollKillSwitch()", start)]

    # 404 (route gone), non-OK, and the unreachable/timeout catch.
    assert body.count("ksUnknown(") == 3, "a degraded poll branch does not clear the rail"
    # The only path that renders observed state is the OK one.
    assert body.count("renderKillSwitch(") == 1
    assert "res.ok" in body
    # A body that will not parse is handed on as null, which renderKillSwitch
    # treats as malformed — never silently skipped.
    assert "catch (_e) { body = null; }" in body


def test_a_malformed_payload_is_refused_wholesale() -> None:
    app_js = _APP_JS.read_text(encoding="utf-8")
    start = app_js.index("function renderKillSwitch(snap)")
    body = app_js[start : app_js.index("async function pollKillSwitchOnce()", start)]

    # Shape drift (wrong length, wrong phase order, non-string status) refuses
    # the WHOLE payload rather than rendering a partial sequence.
    assert "seq.length === KS_PHASES.length" in body
    assert "leg.phase === KS_PHASES[i][0]" in body
    assert body.count("ksUnknown(") == 2  # not-an-object, and shape drift


def test_the_activation_request_is_serialized_and_bounded() -> None:
    # One liquidate sequence in flight at a time, from EITHER trigger, and a
    # stalled runtime must not wedge the control inert forever.
    app_js = _APP_JS.read_text(encoding="utf-8")

    assert "if (killInFlight) return;" in app_js
    assert "AbortSignal.timeout(KILL_FETCH_TIMEOUT_MS)" in app_js
    # Identity binding: a 2xx designates an activation only with a concrete id.
    assert 'typeof body.activation_id === "string"' in app_js
    assert "killConfirmedId = id;" in app_js
