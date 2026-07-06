"""L4 boundary — the kill-switch handlers wired onto the operator runtime.

``atp_safety.wire_kill_switch`` over a fake backend (transport-free
in-process dispatch, matching ``test_operator_interface_runtime_wiring``'s
registry-level layer):

* REST ``POST /api/v1/kill-switch?confirm=true`` → 200 with EXACTLY the
  SDK-pinned response fields; the unconfirmed 428 guard is unchanged.
* CLI ``kill-switch activate`` → exit 3 without ``--confirm``, exit 0 with;
  a backend ``TimeoutError`` → 504 → exit ``TIMEOUT``; a backend failure →
  500 ``KILL_SWITCH_BACKEND_UNAVAILABLE`` (never success-shaped).
* Replay idempotence: a second activate returns the SAME ``activation_id``
  with NO second backend call.
* ``kill-switch status`` → honest empty before any activation, populated
  after.
* The KILL_SWITCH workflow flips ``fully_served`` in the status report once
  wired; a BARE runtime still serves the deferred 501.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from atp_logging import LogClass, Source
from atp_logging.persistence import JsonlLogStore
from atp_runtime import OperatorInterfaceRuntime
from atp_safety import ActivationOutcome, KillSwitchBackendError, wire_kill_switch

pytestmark = [pytest.mark.boundary]


def _fake_report(activation_id: str) -> dict:
    return {
        "activation_id": activation_id,
        "live_strategy_id": "alpha-live",
        "activated_at_epoch_ms": 1_750_000_000_000,
        "paper_halt": {"status": "SUCCEEDED"},
        "paper_halt_summary": {
            "engines_total": 30,
            "transitioned": 30,
            "already_halted": 0,
        },
        "resting_order_cancels": [
            {
                "order_id": "alpha-live/ks-rest-0000",
                "symbol": "AAPL",
                "broker_order_id": "B-0000",
                "outcome": {"status": "SUCCEEDED"},
            }
        ],
        "liquidations": [
            {
                "symbol": "AAPL",
                "side": "SELL",
                "quantity": 100,
                "outcome": {"status": "SUCCEEDED"},
            },
            {
                "symbol": "MSFT",
                "side": "BUY",
                "quantity": 50,
                "outcome": {"status": "SUCCEEDED"},
            },
        ],
        "ib_disconnect": {"status": "SUCCEEDED"},
        "timings": {
            "halt_completed_ms": 0,
            "cancels_completed_ms": 1,
            "liquidations_submitted_ms": 2,
            "disconnect_completed_ms": 3,
        },
        "fully_clean": True,
        "within_nfr_p3": True,
        "all_engines_halted": True,
        "events_recorded": 1,
    }


class FakeBackend:
    """Deterministic in-process backend standing in for the Rust CLI."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def activate(self, activation_id: str) -> ActivationOutcome:
        self.calls.append(activation_id)
        return ActivationOutcome(report=_fake_report(activation_id), ran_clean=True)


class FailingBackend:
    def activate(self, activation_id: str) -> ActivationOutcome:
        raise KillSwitchBackendError("fixture binary missing")


class HangingBackend:
    def activate(self, activation_id: str) -> ActivationOutcome:
        raise TimeoutError("kill-switch activation exceeded 10.0s")


def _wired_runtime(tmp_path: Path, backend) -> tuple[OperatorInterfaceRuntime, JsonlLogStore]:
    runtime = OperatorInterfaceRuntime()
    store = JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    wire_kill_switch(runtime, backend=backend, system_log_store=store, state_dir=state_dir)
    return runtime, store


FROZEN_RESPONSE_FIELDS = {
    "activation_id",
    "activated_at",
    "cancelled_orders",
    "liquidation_orders",
    "paper_engines_halted",
    "ib_gateway_disconnected",
}


def test_rest_activate_returns_exactly_the_frozen_fields(tmp_path: Path) -> None:
    runtime, _ = _wired_runtime(tmp_path, FakeBackend())
    status, body = runtime.dispatch_rest("POST", "/api/v1/kill-switch?confirm=true", b"{}")
    assert status == 200
    assert set(body) == FROZEN_RESPONSE_FIELDS
    assert body["paper_engines_halted"] == 30
    assert body["ib_gateway_disconnected"] is True
    assert body["activated_at"].endswith("+00:00")
    liquidations = {entry["symbol"]: entry for entry in body["liquidation_orders"]}
    assert liquidations["AAPL"]["side"] == "SELL"
    assert liquidations["MSFT"]["quantity"] == 50


def test_rest_unconfirmed_guard_is_unchanged(tmp_path: Path) -> None:
    runtime, _ = _wired_runtime(tmp_path, FakeBackend())
    status, body = runtime.dispatch_rest("POST", "/api/v1/kill-switch", b"{}")
    assert status == 428
    assert body["error"]["type"] == "CONFIRMATION_REQUIRED"


def test_activation_writes_activation_and_halted_audit_records(tmp_path: Path) -> None:
    runtime, store = _wired_runtime(tmp_path, FakeBackend())
    status, body = runtime.dispatch_rest("POST", "/api/v1/kill-switch?confirm=true", b"{}")
    assert status == 200
    records = store.read(source=Source.KILL_SWITCH)
    assert [record.event_type for record in records] == ["ACTIVATION", "HALTED"]
    assert {record.correlation_id for record in records} == {body["activation_id"]}


def test_second_activate_replays_without_a_second_backend_call(tmp_path: Path) -> None:
    backend = FakeBackend()
    runtime, _ = _wired_runtime(tmp_path, backend)
    _, first = runtime.dispatch_rest("POST", "/api/v1/kill-switch?confirm=true", b"{}")
    status, second = runtime.dispatch_rest("POST", "/api/v1/kill-switch?confirm=true", b"{}")
    assert status == 200
    assert second == first, "the replay returns the persisted response verbatim"
    assert backend.calls == [first["activation_id"]], (
        "the liquidate sequence must not re-fire on a repeat activation"
    )


def test_backend_failure_is_never_success_shaped(tmp_path: Path) -> None:
    runtime, store = _wired_runtime(tmp_path, FailingBackend())
    status, body = runtime.dispatch_rest("POST", "/api/v1/kill-switch?confirm=true", b"{}")
    assert status == 500
    assert body["error"]["type"] == "KILL_SWITCH_BACKEND_UNAVAILABLE"
    assert store.read() == [], "no audit record is fabricated for a failed backend"
    # And no replay guard was armed: status still reports never-activated.
    cli = runtime.cli_dispatcher()
    out = io.StringIO()
    assert cli.dispatch(["kill-switch", "status", "--json"], stdout=out) == 0
    assert json.loads(out.getvalue())["activated"] is False


def test_backend_timeout_maps_to_gateway_timeout_and_cli_timeout_exit(
    tmp_path: Path,
) -> None:
    runtime, _ = _wired_runtime(tmp_path, HangingBackend())
    status, body = runtime.dispatch_rest("POST", "/api/v1/kill-switch?confirm=true", b"{}")
    assert status == 504
    assert body["error"]["type"] == "GATEWAY_TIMEOUT"
    cli = runtime.cli_dispatcher()
    out = io.StringIO()
    exit_code = cli.dispatch(["kill-switch", "activate", "--confirm"], stdout=out)
    assert exit_code == 6, "a hung kill switch must exit TIMEOUT, not hang or succeed"


def test_cli_activate_confirmation_and_success_exit_codes(tmp_path: Path) -> None:
    runtime, _ = _wired_runtime(tmp_path, FakeBackend())
    cli = runtime.cli_dispatcher()

    unconfirmed = io.StringIO()
    assert cli.dispatch(["kill-switch", "activate"], stdout=unconfirmed) == 3

    confirmed = io.StringIO()
    assert cli.dispatch(["kill-switch", "activate", "--confirm", "--json"], stdout=confirmed) == 0
    body = json.loads(confirmed.getvalue())
    assert set(body) == FROZEN_RESPONSE_FIELDS


def test_cli_status_honest_empty_then_populated(tmp_path: Path) -> None:
    runtime, _ = _wired_runtime(tmp_path, FakeBackend())
    cli = runtime.cli_dispatcher()

    before = io.StringIO()
    assert cli.dispatch(["kill-switch", "status", "--json"], stdout=before) == 0
    assert json.loads(before.getvalue()) == {
        "activated": False,
        "last_activation": None,
    }

    assert cli.dispatch(["kill-switch", "activate", "--confirm"], stdout=io.StringIO()) == 0

    after = io.StringIO()
    assert cli.dispatch(["kill-switch", "status", "--json"], stdout=after) == 0
    status_body = json.loads(after.getvalue())
    assert status_body["activated"] is True
    last = status_body["last_activation"]
    assert last["ran_clean"] is True
    assert last["audit_recorded"] is True
    assert last["within_nfr_p3"] is True
    assert last["halted_log_latency_ms"] is not None
    assert last["response"]["paper_engines_halted"] == 30


def test_workflow_status_flips_kill_switch_fully_served(tmp_path: Path) -> None:
    bare = OperatorInterfaceRuntime()
    bare_rows = {row["id"]: row for row in bare.status_snapshot()["workflows"]}
    assert bare_rows["KILL_SWITCH"]["fully_served"] is False
    assert "SRS-SAFE-001" in bare_rows["KILL_SWITCH"]["deferred_owners"]

    wired, _ = _wired_runtime(tmp_path, FakeBackend())
    wired_rows = {row["id"]: row for row in wired.status_snapshot()["workflows"]}
    assert wired_rows["KILL_SWITCH"]["fully_served"] is True
    assert wired_rows["KILL_SWITCH"]["deferred_owners"] == []


def test_bare_runtime_still_serves_the_deferred_501() -> None:
    runtime = OperatorInterfaceRuntime()
    status, body = runtime.dispatch_rest("POST", "/api/v1/kill-switch?confirm=true", b"{}")
    assert status == 501
    assert body["error"]["type"] == "HANDLER_DEFERRED"
    assert body["error"]["detail"]["owner"] == "SRS-SAFE-001"


def test_wire_kill_switch_requires_an_existing_state_dir(tmp_path: Path) -> None:
    runtime = OperatorInterfaceRuntime()
    store = JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM)
    with pytest.raises(FileNotFoundError):
        wire_kill_switch(
            runtime,
            backend=FakeBackend(),
            system_log_store=store,
            state_dir=tmp_path / "missing",
        )


class FlakyStore:
    """Durable-store wrapper that fails writes until healed (reads delegate)."""

    def __init__(self, inner: JsonlLogStore) -> None:
        self.inner = inner
        self.failing = True

    def write(self, record) -> None:
        if self.failing:
            raise OSError("injected: audit volume unavailable")
        self.inner.write(record)

    def read(self, **filters):
        return self.inner.read(**filters)


def test_failed_audit_write_is_retried_on_replay_never_refires(tmp_path: Path) -> None:
    # The sequence RAN but the durable SRS-LOG-001 writes failed: the handler
    # surfaces KILL_SWITCH_AUDIT_WRITE_FAILED (500) with the replay guard
    # already armed. A retry must NOT re-fire the liquidate sequence — it
    # replays the persisted response AND retries the pending audit writes, so
    # the AC-required HALTED record eventually lands once the store heals.
    backend = FakeBackend()
    runtime = OperatorInterfaceRuntime()
    store = FlakyStore(JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    wire_kill_switch(runtime, backend=backend, system_log_store=store, state_dir=state_dir)

    status, body = runtime.dispatch_rest("POST", "/api/v1/kill-switch?confirm=true", b"{}")
    assert status == 500
    assert body["error"]["type"] == "KILL_SWITCH_AUDIT_WRITE_FAILED"
    assert len(backend.calls) == 1, "the sequence itself ran exactly once"
    assert store.read() == [], "no audit record was fabricated"

    # Store still broken: the retry replays (no re-fire) and surfaces the
    # still-failing audit write.
    status, body = runtime.dispatch_rest("POST", "/api/v1/kill-switch?confirm=true", b"{}")
    assert status == 500
    assert body["error"]["type"] == "KILL_SWITCH_AUDIT_WRITE_FAILED"
    assert len(backend.calls) == 1, "a repeat activation must never re-liquidate"

    # Store heals: the replay retries the pending writes and returns the
    # persisted response with the SAME activation id.
    store.failing = False
    status, body = runtime.dispatch_rest("POST", "/api/v1/kill-switch?confirm=true", b"{}")
    assert status == 200
    assert body["activation_id"] == backend.calls[0]
    assert len(backend.calls) == 1
    records = store.read(source=Source.KILL_SWITCH)
    assert [record.event_type for record in records] == ["ACTIVATION", "HALTED"]
    assert {record.correlation_id for record in records} == {backend.calls[0]}

    # Status now reports the audit as recorded (latency honestly None — the
    # original activation moment is unmeasurable after the fact).
    cli = runtime.cli_dispatcher()
    out = io.StringIO()
    assert cli.dispatch(["kill-switch", "status", "--json"], stdout=out) == 0
    last = json.loads(out.getvalue())["last_activation"]
    assert last["audit_recorded"] is True
    assert last["halted_log_latency_ms"] is None


def test_dashboard_affordance_targets_the_contract_route() -> None:
    # SYS-44a "accessible from the dashboard": the minimal SRS-SAFE-001
    # control POSTs to the CONTRACT route on the same runtime (the dashboard
    # itself adds NO mutating endpoint — test_dashboard_safety.py pins that).
    # This drift guard fails if the asset ever points somewhere else.
    repo_root = Path(__file__).resolve().parents[2]
    app_js = (repo_root / "python/atp_dashboard/assets/app.js").read_text(encoding="utf-8")
    assert '"/api/v1/kill-switch?confirm=true"' in app_js
    assert 'method: "POST"' in app_js
    index_html = (repo_root / "python/atp_dashboard/assets/index.html").read_text(encoding="utf-8")
    assert 'id="killswitch-btn"' in index_html


def test_unpersistable_replay_guard_is_surfaced_never_silent(tmp_path: Path) -> None:
    # The one window an operator-layer guard cannot close: the sequence ran
    # but the guard record could not be persisted. The handler must surface
    # KILL_SWITCH_REPLAY_GUARD_UNARMED (explicitly warning a blind retry
    # would re-fire) and still best-effort land the audit records — never a
    # success-shaped 200, never a silent log.
    import shutil as shutil_module

    backend = FakeBackend()
    runtime = OperatorInterfaceRuntime()
    store = JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    wire_kill_switch(runtime, backend=backend, system_log_store=store, state_dir=state_dir)

    # Yank the state directory AFTER wiring: persist_last_activation fails.
    shutil_module.rmtree(state_dir)

    status, body = runtime.dispatch_rest("POST", "/api/v1/kill-switch?confirm=true", b"{}")
    assert status == 500
    assert body["error"]["type"] == "KILL_SWITCH_REPLAY_GUARD_UNARMED"
    assert "re-run" in body["error"]["message"] or "re-fire" in body["error"]["message"]
    assert body["error"]["detail"]["audit_recorded"] is True, (
        "the audit records must still be attempted best-effort"
    )
    records = store.read(source=Source.KILL_SWITCH)
    assert [record.event_type for record in records] == ["ACTIVATION", "HALTED"], (
        "one durable trace of the activation must exist even with the guard unarmed"
    )
    assert len(backend.calls) == 1
