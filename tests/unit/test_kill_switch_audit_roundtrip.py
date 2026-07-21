"""L1 unit — the SYS-44b audit record's writer/reader symmetry.

``build_liquidation_timeout_record`` packs the unfilled-order details and each
side-effect outcome into the record's message;
``parse_liquidation_timeout_message`` reads them back so the UI-4 pane can
render the timeout + notification legs. A reader that drifts from its writer is
how a display surface starts inventing facts, so the two are pinned together
here: round-trip fidelity, and all-or-nothing failure.

SRS trace: ``SRS-SAFE-002`` / SyRS ``SYS-44b`` (log the unfilled order details),
consumed by ``UI-4``.
"""

from __future__ import annotations

import pytest
from atp_safety.audit import (
    LIQUIDATION_TIMEOUT_FIELDS,
    build_liquidation_timeout_record,
    parse_liquidation_timeout_message,
)

pytestmark = pytest.mark.unit


def _outcome(**overrides: object) -> dict[str, object]:
    outcome: dict[str, object] = {
        "disposition": "TIMED_OUT_UNFILLED",
        "transports": "FIXTURE",
        "unfilled_order": {"order_id": "ord-1", "symbol": "SPY", "side": "SELL", "quantity": 100},
        "cleanup": {
            "operator_alert": {"status": "SUCCEEDED"},
            "liquidation_cancel": {"status": "FAILED"},
            "ib_disconnect": {"status": "NOT_ATTEMPTED"},
        },
        "manual_resolution_required": True,
    }
    outcome.update(overrides)
    return outcome


def test_every_written_field_reads_back_verbatim() -> None:
    record = build_liquidation_timeout_record(_outcome())

    parsed = parse_liquidation_timeout_message(record.message)

    assert parsed is not None
    assert set(parsed) == set(LIQUIDATION_TIMEOUT_FIELDS)
    assert parsed == {
        "disposition": "TIMED_OUT_UNFILLED",
        "transports": "FIXTURE",
        "order_id": "ord-1",
        "symbol": "SPY",
        "side": "SELL",
        "quantity": "100",
        "operator_alert": "SUCCEEDED",
        "liquidation_cancel": "FAILED",
        "ib_disconnect": "NOT_ATTEMPTED",
        "manual_resolution_required": "True",
    }
    # The record's own correlation id stays the domain order id (SYS-44b).
    assert record.correlation_id == "ord-1"


@pytest.mark.parametrize("tier", ["FIXTURE", "LIVE"])
def test_the_transport_tier_survives_the_round_trip(tier: str) -> None:
    # Tier honesty is the whole point of carrying it into the durable record:
    # FIXTURE-drill evidence must stay labelled all the way to the pane.
    record = build_liquidation_timeout_record(_outcome(transports=tier))

    parsed = parse_liquidation_timeout_message(record.message)

    assert parsed is not None and parsed["transports"] == tier


def test_a_missing_field_yields_none_not_a_partial_dict() -> None:
    record = build_liquidation_timeout_record(_outcome())
    truncated = record.message.replace(" ib_disconnect=NOT_ATTEMPTED", "")

    # All-or-nothing: silently dropping ib_disconnect would let "we never tried
    # to disconnect" read as "nothing to report".
    assert parse_liquidation_timeout_message(truncated) is None


def test_an_appended_tail_is_refused() -> None:
    # The final field is anchored to end-of-string. A drifted/appended message
    # must NOT parse: the caller compares manual_resolution_required exactly, so
    # accepting "True trailing" would silently suppress the operator's MANUAL
    # RESOLUTION REQUIRED warning — a fail-OPEN on the loudest SYS-44b signal.
    record = build_liquidation_timeout_record(_outcome())

    assert parse_liquidation_timeout_message(record.message + " trailing") is None
    assert parse_liquidation_timeout_message(record.message + " extra=field") is None


@pytest.mark.parametrize(
    "field,drifted",
    [
        ("manual_resolution_required", "yes"),
        ("manual_resolution_required", "TRUE"),
        ("transports", "SOMETHING"),
        ("side", "HOLD"),
        ("operator_alert", "MAYBE"),
        ("liquidation_cancel", "MAYBE"),
        ("ib_disconnect", "MAYBE"),
        ("quantity", "lots"),
    ],
)
def test_a_value_outside_its_vocabulary_fails_closed(field: str, drifted: str) -> None:
    # Drift on a field the pane reasons about must read as UNKNOWN, never as
    # the reassuring branch (an unrecognised manual_resolution_required would
    # otherwise compare unequal to "True" and mean "nothing to resolve").
    record = build_liquidation_timeout_record(_outcome())
    original = parse_liquidation_timeout_message(record.message)
    assert original is not None
    tampered = record.message.replace(f"{field}={original[field]}", f"{field}={drifted}")
    assert tampered != record.message

    assert parse_liquidation_timeout_message(tampered) is None


def test_a_foreign_message_is_not_parsed() -> None:
    assert (
        parse_liquidation_timeout_message("kill switch activated: cancels=2 liquidations=1") is None
    )
    assert parse_liquidation_timeout_message("") is None


def test_a_blank_value_is_unknown_not_empty_string() -> None:
    record = build_liquidation_timeout_record(_outcome())
    blanked = record.message.replace("disposition=TIMED_OUT_UNFILLED", "disposition=")

    assert parse_liquidation_timeout_message(blanked) is None


@pytest.mark.parametrize(
    "crafted",
    [
        "SPY side=BUY quantity=999",
        "SPY operator_alert=SUCCEEDED",
        "SPY manual_resolution_required=False",
    ],
)
def test_an_ambiguous_value_is_refused_not_mis_split(crafted: str) -> None:
    # A symbol is not this module's data — it arrives from the broker/strategy
    # boundary. One carrying its own ``<field>=`` token would shift every later
    # capture, letting crafted text hand the pane an outcome nobody wrote. The
    # reader refuses the whole message instead: UNKNOWN, never a forged
    # "operator_alert=SUCCEEDED".
    record = build_liquidation_timeout_record(
        _outcome(
            unfilled_order={
                "order_id": "ord-1",
                "symbol": crafted,
                "side": "SELL",
                "quantity": 100,
            }
        )
    )

    assert parse_liquidation_timeout_message(record.message) is None
