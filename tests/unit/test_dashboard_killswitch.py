"""L1 unit — UI-4 kill-switch status provider (SyRS SYS-44a / SYS-44b).

The pane that tells an operator whether a liquidation completed. Every case
here is the same question asked a different way: *when the dashboard does not
know, does it say UNKNOWN?* A fabricated "IB DISCONNECTED" — or an unreadable
state directory rendering as "never activated" — is a lie about whether live
positions are still open.

SRS trace: ``UI-4`` (status feedback), ``SRS-SAFE-001`` (activation sequence,
NFR-P3), ``SRS-SAFE-002`` (SYS-44b unfilled-liquidation timeout).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from atp_dashboard.killswitch import (
    KILL_SWITCH_ACTIVATION_OWNER,
    KILL_SWITCH_NOTIFY_OWNER,
    KILL_SWITCH_SEQUENCE,
    KILL_SWITCH_TIMEOUT_OWNER,
    DurableKillSwitchStatusSource,
    KillSwitchStatusProvider,
    KillSwitchStatusUnavailable,
)
from atp_logging import LogClass
from atp_logging.persistence import JsonlLogStore
from atp_safety.audit import build_liquidation_timeout_record
from atp_safety.state import persist_last_activation

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #


def _report(**overrides: object) -> dict[str, object]:
    report: dict[str, object] = {
        "activation_id": "act-abc",
        "live_strategy_id": "alpha",
        "activated_at_epoch_ms": 1_700_000_000_000,
        "paper_halt": {"status": "SUCCEEDED"},
        "paper_halt_summary": {"engines_total": 3, "transitioned": 2, "already_halted": 1},
        "resting_order_cancels": [
            {
                "order_id": "o1",
                "symbol": "SPY",
                "broker_order_id": "b1",
                "outcome": {"status": "SUCCEEDED"},
            }
        ],
        "liquidations": [
            {"symbol": "SPY", "side": "SELL", "quantity": 10, "outcome": {"status": "SUCCEEDED"}}
        ],
        "ib_disconnect": {"status": "SUCCEEDED"},
        "timings": {
            "halt_completed_ms": 10,
            "cancels_completed_ms": 20,
            "liquidations_submitted_ms": 1842,
            "disconnect_completed_ms": 40,
        },
        "fully_clean": True,
        "within_nfr_p3": True,
        "all_engines_halted": True,
        "events_recorded": 2,
    }
    report.update(overrides)
    return report


def _record(report: dict[str, object], **overrides: object) -> dict[str, object]:
    response = {
        "activation_id": report["activation_id"],
        "activated_at": "2023-11-14T22:13:20.000+00:00",
        "cancelled_orders": report["resting_order_cancels"],
        "liquidation_orders": report["liquidations"],
        "paper_engines_halted": 3,
        "ib_gateway_disconnected": (
            isinstance(report.get("ib_disconnect"), dict)
            and report["ib_disconnect"].get("status") == "SUCCEEDED"  # type: ignore[union-attr]
        ),
    }
    record: dict[str, object] = {
        "activation_id": report["activation_id"],
        "response": response,
        "report": report,
        "ran_clean": True,
        "audit_recorded": True,
        "halted_log_latency_ms": 412.0,
        "persisted_at_ns": 1,
    }
    record.update(overrides)
    return record


def _seeded(
    tmp_path: Path, record: dict[str, object] | None = None
) -> DurableKillSwitchStatusSource:
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    if record is not None:
        persist_last_activation(state_dir, record)
    return DurableKillSwitchStatusSource(state_dir=state_dir, log_dir=tmp_path)


def _legs(snapshot: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(leg["phase"]): leg for leg in snapshot["sequence"]}  # type: ignore[index,union-attr]


# --------------------------------------------------------------------------- #
# Unknown state is UNKNOWN — never an all-clear, never "never activated"
# --------------------------------------------------------------------------- #


def test_no_configured_source_reports_unknown_not_never_activated() -> None:
    snapshot = KillSwitchStatusProvider().kill_switch_snapshot()

    assert snapshot["ok"] is False
    assert snapshot["errors"]
    # The killer distinction: an unconfigured dashboard does not know whether
    # the kill switch fired. It must NOT say False.
    assert snapshot["activated"] is None
    # Unknown order set is None, never [] (an empty table reads as all-clear).
    assert snapshot["orders"] is None
    assert snapshot["tier"] is None
    for leg in snapshot["sequence"]:  # type: ignore[union-attr]
        assert leg["status"] == "UNKNOWN"
        assert leg["value"] is None
        assert str(leg["data_source"]).startswith("deferred:")


def test_sequence_covers_every_ac_leg_in_phase_order() -> None:
    # UI-4 AC: "cancellation, liquidation submission, timeout, notification and
    # disconnect status" — plus the paper-engine halt the sequence starts with.
    snapshot = KillSwitchStatusProvider().kill_switch_snapshot()
    phases = [leg["phase"] for leg in snapshot["sequence"]]  # type: ignore[union-attr]

    assert phases == [spec["phase"] for spec in KILL_SWITCH_SEQUENCE]
    for required in ("cancellation", "liquidation", "timeout", "notification", "disconnect"):
        assert required in phases
    legs = _legs(snapshot)
    # Each unresolved leg names the feature that owes the fact.
    assert legs["cancellation"]["owner"] == KILL_SWITCH_ACTIVATION_OWNER
    assert legs["timeout"]["owner"] == KILL_SWITCH_TIMEOUT_OWNER
    assert legs["notification"]["owner"] == KILL_SWITCH_NOTIFY_OWNER


def test_readable_but_empty_state_dir_reports_not_activated(tmp_path: Path) -> None:
    # The one case where False is honest: the state directory IS readable and
    # genuinely holds no activation record.
    snapshot = KillSwitchStatusProvider(_seeded(tmp_path)).kill_switch_snapshot()

    assert snapshot["ok"] is True
    assert snapshot["activated"] is False
    assert snapshot["activation_id"] is None
    for leg in snapshot["sequence"]:  # type: ignore[union-attr]
        assert leg["status"] == "UNKNOWN"


def test_corrupt_activation_state_fails_closed(tmp_path: Path) -> None:
    # Corrupt is NOT "never activated": the liquidate sequence may well have
    # run. Same fail-closed stance the replay guard takes.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "kill_switch_last_activation.json").write_text("{not json", encoding="utf-8")
    source = DurableKillSwitchStatusSource(state_dir=state_dir, log_dir=tmp_path)

    with pytest.raises(KillSwitchStatusUnavailable):
        source.last_activation()

    snapshot = KillSwitchStatusProvider(source).kill_switch_snapshot()
    assert snapshot["ok"] is False
    assert snapshot["activated"] is None
    assert any("corrupt" in error for error in snapshot["errors"])  # type: ignore[union-attr]
    for leg in snapshot["sequence"]:  # type: ignore[union-attr]
        assert leg["status"] == "UNKNOWN"


# --------------------------------------------------------------------------- #
# A real activation record populates every activation leg
# --------------------------------------------------------------------------- #


def test_activation_record_populates_the_activation_legs(tmp_path: Path) -> None:
    source = _seeded(tmp_path, _record(_report()))
    snapshot = KillSwitchStatusProvider(source).kill_switch_snapshot()
    legs = _legs(snapshot)

    assert snapshot["activated"] is True
    assert snapshot["activation_id"] == "act-abc"
    assert snapshot["within_nfr_p3"] is True
    assert snapshot["liquidations_submitted_ms"] == 1842
    assert legs["halt"]["status"] == "SUCCEEDED"
    assert "3 / 3 engines HALTED" in str(legs["halt"]["detail"])
    assert legs["cancellation"]["status"] == "SUCCEEDED"
    assert legs["liquidation"]["status"] == "SUCCEEDED"
    assert legs["disconnect"]["status"] == "SUCCEEDED"
    # A resolved leg carries a live value equal to its status and a real source.
    for phase in ("halt", "cancellation", "liquidation", "disconnect"):
        assert legs[phase]["value"] == legs[phase]["status"]
        assert legs[phase]["data_source"] == "kill_switch_activation_record"
    # The SYS-44b legs stay deferred — no timeout record exists.
    assert legs["timeout"]["status"] == "UNKNOWN"
    assert legs["notification"]["status"] == "UNKNOWN"
    assert snapshot["orders"] is not None
    assert {str(row["kind"]) for row in snapshot["orders"]} == {  # type: ignore[union-attr,index]
        "CANCEL",
        "LIQUIDATION",
    }


def test_a_failed_phase_is_never_smoothed_into_success(tmp_path: Path) -> None:
    report = _report(
        liquidations=[
            {
                "symbol": "SPY",
                "side": "SELL",
                "quantity": 10,
                "outcome": {"status": "FAILED", "reason": "rejected"},
            },
            {"symbol": "QQQ", "side": "SELL", "quantity": 4, "outcome": {"status": "SUCCEEDED"}},
        ]
    )
    snapshot = KillSwitchStatusProvider(_seeded(tmp_path, _record(report))).kill_switch_snapshot()
    leg = _legs(snapshot)["liquidation"]

    # One FAILED order in a two-order phase makes the whole phase FAILED.
    assert leg["status"] == "FAILED"
    assert "1 FAILED" in str(leg["detail"])


def test_an_nfr_p3_breach_fails_the_liquidation_leg(tmp_path: Path) -> None:
    report = _report(within_nfr_p3=False)
    snapshot = KillSwitchStatusProvider(_seeded(tmp_path, _record(report))).kill_switch_snapshot()
    leg = _legs(snapshot)["liquidation"]

    assert leg["status"] == "FAILED"
    assert "NFR-P3 BREACHED" in str(leg["detail"])


def test_a_fleet_that_did_not_fully_halt_fails_the_halt_leg(tmp_path: Path) -> None:
    report = _report(all_engines_halted=False)
    snapshot = KillSwitchStatusProvider(_seeded(tmp_path, _record(report))).kill_switch_snapshot()
    leg = _legs(snapshot)["halt"]

    # A SUCCEEDED halt CALL over a fleet that is not all halted is still a
    # failure — the AC's observable is the fleet state, not the call.
    assert leg["status"] == "FAILED"
    assert "NOT all engines halted" in str(leg["detail"])


def test_gateway_still_connected_outranks_a_succeeded_disconnect_call(tmp_path: Path) -> None:
    report = _report(ib_disconnect={"status": "SUCCEEDED"})
    record = _record(report)
    # The SDK-pinned response flag disagrees with the per-call outcome.
    record["response"]["ib_gateway_disconnected"] = False  # type: ignore[index]
    snapshot = KillSwitchStatusProvider(_seeded(tmp_path, record)).kill_switch_snapshot()

    assert _legs(snapshot)["disconnect"]["status"] == "FAILED"


@pytest.mark.parametrize("missing_proof", [None, "true", 1, "SUCCEEDED"])
def test_a_succeeded_disconnect_call_without_the_pinned_flag_is_unknown(
    tmp_path: Path, missing_proof: object
) -> None:
    # The worst possible over-claim: telling an operator "IB gateway
    # disconnected" off a truncated / version-skewed record that carries no
    # trustworthy ib_gateway_disconnected proof. A SUCCEEDED disconnect CALL is
    # not evidence the broker link is closed.
    report = _report(ib_disconnect={"status": "SUCCEEDED"})
    record = _record(report)
    if missing_proof is None:
        del record["response"]["ib_gateway_disconnected"]  # type: ignore[attr-defined]
    else:
        record["response"]["ib_gateway_disconnected"] = missing_proof  # type: ignore[index]
    snapshot = KillSwitchStatusProvider(_seeded(tmp_path, record)).kill_switch_snapshot()
    leg = _legs(snapshot)["disconnect"]

    assert leg["status"] == "UNKNOWN"
    assert leg["value"] is None
    assert str(leg["data_source"]).startswith("deferred:")
    assert "proof missing" in str(leg["detail"])


def test_the_pinned_flag_alone_resolves_the_disconnect_leg(tmp_path: Path) -> None:
    # The converse: an unreadable per-call outcome but a strict True on the
    # contract's own field IS proof the disconnect landed.
    report = _report(ib_disconnect={"status": "WEIRD"})
    record = _record(report)
    record["response"]["ib_gateway_disconnected"] = True  # type: ignore[index]
    snapshot = KillSwitchStatusProvider(_seeded(tmp_path, record)).kill_switch_snapshot()
    leg = _legs(snapshot)["disconnect"]

    assert leg["status"] == "SUCCEEDED"
    assert leg["value"] == "SUCCEEDED"


@pytest.mark.parametrize("bogus", ["true", 1, None, "SUCCEEDED"])
def test_a_non_boolean_flag_is_unknown_never_true(tmp_path: Path, bogus: object) -> None:
    # Version skew / a truncated record must not coerce into a reassuring True.
    report = _report(within_nfr_p3=bogus)
    record = _record(report)
    record["ran_clean"] = bogus
    record["audit_recorded"] = bogus
    snapshot = KillSwitchStatusProvider(_seeded(tmp_path, record)).kill_switch_snapshot()

    assert snapshot["within_nfr_p3"] is None
    assert snapshot["ran_clean"] is None
    assert snapshot["audit_recorded"] is None


def test_malformed_order_entries_make_the_whole_phase_unknown(tmp_path: Path) -> None:
    # A partially-parsed order list would under-report how many orders the
    # sequence touched — the one number an operator counts on afterwards.
    report = _report(
        resting_order_cancels=[
            {"order_id": "o1", "symbol": "SPY", "outcome": {"status": "SUCCEEDED"}},
            {"order_id": "o2", "symbol": "QQQ", "outcome": {"status": "WEIRD"}},
        ]
    )
    snapshot = KillSwitchStatusProvider(_seeded(tmp_path, _record(report))).kill_switch_snapshot()
    leg = _legs(snapshot)["cancellation"]

    assert leg["status"] == "UNKNOWN"
    assert leg["value"] is None
    assert str(leg["data_source"]).startswith("deferred:")


def test_untrustworthy_halt_counts_are_unknown_not_a_ratio(tmp_path: Path) -> None:
    report = _report(
        paper_halt_summary={"engines_total": "3", "transitioned": 2, "already_halted": 1}
    )
    snapshot = KillSwitchStatusProvider(_seeded(tmp_path, _record(report))).kill_switch_snapshot()
    leg = _legs(snapshot)["halt"]

    assert leg["status"] == "UNKNOWN"
    assert "engine counts unavailable" in str(leg["detail"])


# --------------------------------------------------------------------------- #
# SYS-44b timeout + notification legs
# --------------------------------------------------------------------------- #


def _seed_timeout(tmp_path: Path, **overrides: object) -> None:
    outcome: dict[str, object] = {
        "disposition": "TIMED_OUT_UNFILLED",
        "transports": "FIXTURE",
        "unfilled_order": {"order_id": "ord-9", "symbol": "SPY", "side": "SELL", "quantity": 25},
        "cleanup": {
            "operator_alert": {"status": "SUCCEEDED"},
            "liquidation_cancel": {"status": "SUCCEEDED"},
            "ib_disconnect": {"status": "SUCCEEDED"},
        },
        "manual_resolution_required": False,
    }
    outcome.update(overrides)
    store = JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM)
    store.write(build_liquidation_timeout_record(outcome))


def test_the_timeout_record_is_shown_but_never_resolves_the_sys44b_legs(tmp_path: Path) -> None:
    # The record is correlated by ORDER id; the activation report carries no id
    # for the liquidations it submitted, so nothing links the two. The newest
    # record may belong to an earlier activation or an operator drill —
    # rendering it as THIS sequence's outcome would assert a link the data does
    # not carry. Its content is surfaced; the rungs stay UNKNOWN.
    _seed_timeout(tmp_path)
    snapshot = KillSwitchStatusProvider(_seeded(tmp_path)).kill_switch_snapshot()
    legs = _legs(snapshot)

    for phase in ("timeout", "notification"):
        assert legs[phase]["status"] == "UNKNOWN"
        assert legs[phase]["value"] is None
        assert str(legs[phase]["data_source"]).startswith("deferred:")
        assert "NOT correlated" in str(legs[phase]["detail"])
    # ...but the operator can still READ what the record says.
    assert "TIMED_OUT_UNFILLED" in str(legs["timeout"]["detail"])
    assert "ord-9" in str(legs["timeout"]["detail"])
    assert "operator page SUCCEEDED" in str(legs["notification"]["detail"])
    assert "FIXTURE transport" in str(legs["notification"]["detail"])
    # Fixture-drill evidence is labelled and can never read as live SYS-44b
    # history, and the payload states its own correlation status.
    assert snapshot["tier"] == "FIXTURE"
    assert snapshot["timeout_correlated"] is False
    assert snapshot["timeout_record"]["order_id"] == "ord-9"  # type: ignore[index]


@pytest.mark.parametrize(
    "disposition",
    [
        "FILLED_BEFORE_TIMEOUT",
        "TIMED_OUT_UNFILLED",
        "PROBE_UNAVAILABLE",
        "PROBE_INCONSISTENT",
        "SOMETHING_NEW",
    ],
)
def test_no_disposition_can_resolve_an_uncorrelated_leg(tmp_path: Path, disposition: str) -> None:
    # Including the reassuring one: FILLED_BEFORE_TIMEOUT from an unrelated
    # order must never render as "this liquidation filled".
    _seed_timeout(tmp_path, disposition=disposition)
    snapshot = KillSwitchStatusProvider(_seeded(tmp_path)).kill_switch_snapshot()
    leg = _legs(snapshot)["timeout"]

    assert leg["status"] == "UNKNOWN"
    assert disposition in str(leg["detail"])


def test_manual_resolution_required_is_surfaced_in_the_detail(tmp_path: Path) -> None:
    _seed_timeout(tmp_path, disposition="FILLED_BEFORE_TIMEOUT", manual_resolution_required=True)
    snapshot = KillSwitchStatusProvider(_seeded(tmp_path)).kill_switch_snapshot()
    leg = _legs(snapshot)["timeout"]

    assert leg["status"] == "UNKNOWN"
    assert "MANUAL RESOLUTION REQUIRED" in str(leg["detail"])


def test_an_unreadable_timeout_message_is_unknown_not_all_clear(tmp_path: Path) -> None:
    # A LIQUIDATION_TIMEOUT record this dashboard cannot parse must not be
    # skipped into a clean pane — refuse to guess its outcome.
    from atp_logging import LogRecord, Severity, Source

    store = JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM)
    store.write(
        LogRecord(
            timestamp_ns=1,
            severity=Severity.CRITICAL,
            source=Source.KILL_SWITCH,
            event_type="LIQUIDATION_TIMEOUT",
            message="kill-switch liquidation timeout: some future format",
            correlation_id="ord-9",
            log_class=LogClass.SYSTEM,
            strategy_id=None,
        )
    )
    snapshot = KillSwitchStatusProvider(_seeded(tmp_path)).kill_switch_snapshot()
    legs = _legs(snapshot)

    assert snapshot["ok"] is False
    assert legs["timeout"]["status"] == "UNKNOWN"
    assert legs["notification"]["status"] == "UNKNOWN"
    assert snapshot["tier"] is None


def test_an_unreadable_log_does_not_blank_the_activation_legs(tmp_path: Path) -> None:
    # The two sources fail INDEPENDENTLY: an unconfigured/unreadable timeout log
    # must not erase an activation record the dashboard genuinely did read.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    persist_last_activation(state_dir, _record(_report()))
    source = DurableKillSwitchStatusSource(state_dir=state_dir, log_dir=None)
    snapshot = KillSwitchStatusProvider(source).kill_switch_snapshot()
    legs = _legs(snapshot)

    assert snapshot["ok"] is False  # the log leg is genuinely unavailable
    assert snapshot["activated"] is True
    assert legs["cancellation"]["status"] == "SUCCEEDED"
    assert legs["timeout"]["status"] == "UNKNOWN"


@pytest.mark.parametrize(
    "drifted,because",
    [
        ({}, "empty object"),
        ({"report": {}, "response": {}}, "no activation_id"),
        ({"activation_id": "   ", "report": {}, "response": {}}, "blank activation_id"),
        ({"activation_id": "act-1"}, "no report/response"),
        ({"activation_id": "act-1", "report": {}}, "no response"),
        ({"activation_id": "act-1", "report": "nope", "response": {}}, "report not a mapping"),
    ],
)
def test_a_readable_but_drifted_record_is_never_a_recorded_activation(
    tmp_path: Path, drifted: dict[str, object], because: str
) -> None:
    # load_last_activation only proves the file parsed as a JSON object. Without
    # a schema gate an empty {} would render as activated:true / ok:true with
    # every leg UNKNOWN — the pane announcing a liquidate sequence it cannot
    # substantiate. An unsubstantiated activation is UNKNOWN, not activated.
    snapshot = KillSwitchStatusProvider(_seeded(tmp_path, drifted)).kill_switch_snapshot()

    assert snapshot["activated"] is None, f"{because} was read as a real activation"
    assert snapshot["ok"] is False
    assert snapshot["activation_id"] is None
    assert snapshot["errors"]
    for leg in snapshot["sequence"]:  # type: ignore[union-attr]
        assert leg["status"] == "UNKNOWN"
        assert leg["value"] is None


@pytest.mark.parametrize("part", ["report", "response"])
def test_a_record_stitched_from_two_activations_is_refused(tmp_path: Path, part: str) -> None:
    # Every leg the pane renders comes out of the report/response. If either
    # names a DIFFERENT activation the record describes neither, and showing
    # its evidence under this receipt would be false post-liquidation proof.
    record = _record(_report())
    record[part]["activation_id"] = "act-someone-else"  # type: ignore[index]
    snapshot = KillSwitchStatusProvider(_seeded(tmp_path, record)).kill_switch_snapshot()

    assert snapshot["activated"] is None
    assert snapshot["ok"] is False
    assert any("identity disagrees" in error for error in snapshot["errors"])  # type: ignore[union-attr]


@pytest.mark.parametrize("part", ["report", "response"])
def test_a_record_whose_report_or_response_names_no_activation_is_refused(
    tmp_path: Path, part: str
) -> None:
    # A partial identity match is not a match: attribute nothing.
    record = _record(_report())
    del record[part]["activation_id"]  # type: ignore[attr-defined]
    snapshot = KillSwitchStatusProvider(_seeded(tmp_path, record)).kill_switch_snapshot()

    assert snapshot["activated"] is None
    assert snapshot["ok"] is False
    assert any("names no activation" in error for error in snapshot["errors"])  # type: ignore[union-attr]
    for leg in snapshot["sequence"]:  # type: ignore[union-attr]
        assert leg["status"] == "UNKNOWN"


def test_the_provider_never_raises_on_a_broken_source() -> None:
    # A monitoring surface must not crash: an exploding source becomes an
    # explicit unavailable snapshot.
    class Broken:
        def last_activation(self) -> None:
            raise KillSwitchStatusUnavailable("state volume gone")

        def last_timeout(self) -> None:
            raise KillSwitchStatusUnavailable("log volume gone")

    snapshot = KillSwitchStatusProvider(Broken()).kill_switch_snapshot()

    assert snapshot["ok"] is False
    assert snapshot["activated"] is None
    assert len(snapshot["errors"]) == 2  # type: ignore[arg-type]
