"""L1 unit tests for ``assert_order_event_payload`` (SRS-SDK-004).

Locks the pure-function contract of the shipped guard helper:
re-export from the package root, one-positional signature
``(event)``, silent on well-formed events across all six
``OrderEventType`` members, raises ``OrderEventContractError`` (a
subclass of ``StrategyAPIError``) on each AC-required missing
field, and message content that names the event type, order id,
and missing field for the structured-error contract (SyRS SYS-64).
Also locks the public latency-budget constants.
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"

if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

import atp_strategy as _pkg  # noqa: E402
import pytest  # noqa: E402
from atp_strategy import (  # noqa: E402
    LIVE_CALLBACK_LATENCY_P95_MS,
    PAPER_CALLBACK_LATENCY_P95_MS,
    OrderEvent,
    OrderEventContractError,
    OrderEventType,
    StrategyAPIError,
    assert_order_event_payload,
)
from atp_strategy import api as _api_module  # noqa: E402

pytestmark = pytest.mark.unit


def _event(
    event_type: OrderEventType,
    *,
    order_id: str = "ord-1",
    client_order_id: str = "cli-1",
    strategy_id: str = "s1",
    symbol: str = "AAPL",
    fill_price: float | None = 100.0,
    fill_quantity: int | None = 10,
    cumulative_filled: int = 10,
    remaining_quantity: int = 0,
    commission: float | None = 0.05,
    reason: str | None = None,
    timestamp: str = "2026-05-03T13:30:00Z",
) -> OrderEvent:
    return OrderEvent(
        event_type=event_type,
        order_id=order_id,
        client_order_id=client_order_id,
        strategy_id=strategy_id,
        symbol=symbol,
        fill_price=fill_price,
        fill_quantity=fill_quantity,
        cumulative_filled=cumulative_filled,
        remaining_quantity=remaining_quantity,
        commission=commission,
        reason=reason,
        timestamp=timestamp,
    )


class AssertOrderEventPayloadExportTest(unittest.TestCase):
    def test_imports_assert_helper_from_package_root(self) -> None:
        # Module-level imports (above) are bound at collection time
        # against the real package — verify they resolve and that the
        # symbol participates in the package's documented public surface.
        # We deliberately don't `import atp_strategy` inside the test body
        # because the L3 contract-test mutation rig leaves
        # ``sys.modules['atp_strategy']`` pointing at a (now-deleted)
        # tmpdir copy, which would shadow the real package on subsequent
        # fresh imports within the same pytest session.
        self.assertIs(_pkg.assert_order_event_payload, assert_order_event_payload)
        self.assertIn("assert_order_event_payload", _pkg.__all__)
        # The api module re-exports the same function object — locks the
        # `atp_strategy.api.assert_order_event_payload` import path that
        # the order-events contract check uses for its behavioural
        # exercise.
        self.assertIs(_api_module.assert_order_event_payload, assert_order_event_payload)

    def test_signature_is_one_positional(self) -> None:
        sig = inspect.signature(assert_order_event_payload)
        params = list(sig.parameters)
        self.assertEqual(params, ["event"])


class OrderEventTypeMembersTest(unittest.TestCase):
    """SRS-SDK-004 AC names the four required lifecycle event categories."""

    def test_members_cover_ac_required_categories(self) -> None:
        members = {m.name for m in OrderEventType}
        self.assertGreaterEqual(
            members,
            {"FILL", "PARTIAL_FILL", "CANCELLED", "REJECTED"},
        )

    def test_ack_and_expired_present_for_completeness(self) -> None:
        members = {m.name for m in OrderEventType}
        self.assertIn("ACK", members)
        self.assertIn("EXPIRED", members)


class LatencyBudgetConstantTest(unittest.TestCase):
    """SyRS NFR-P4: SDK is the single source of truth for the budgets."""

    def test_live_budget_is_1000_ms(self) -> None:
        self.assertIsInstance(LIVE_CALLBACK_LATENCY_P95_MS, int)
        self.assertNotIsInstance(LIVE_CALLBACK_LATENCY_P95_MS, bool)
        self.assertEqual(LIVE_CALLBACK_LATENCY_P95_MS, 1000)

    def test_paper_budget_is_100_ms(self) -> None:
        self.assertIsInstance(PAPER_CALLBACK_LATENCY_P95_MS, int)
        self.assertNotIsInstance(PAPER_CALLBACK_LATENCY_P95_MS, bool)
        self.assertEqual(PAPER_CALLBACK_LATENCY_P95_MS, 100)


class AssertPayloadFillBranchTest(unittest.TestCase):
    """SRS-SDK-004 AC half-A: FILL / PARTIAL_FILL field presence."""

    def test_well_formed_fill_is_silent(self) -> None:
        assert_order_event_payload(_event(OrderEventType.FILL))

    def test_well_formed_partial_fill_is_silent(self) -> None:
        assert_order_event_payload(
            _event(
                OrderEventType.PARTIAL_FILL,
                fill_quantity=4,
                cumulative_filled=4,
                remaining_quantity=6,
            )
        )

    def test_fill_missing_price_raises(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, fill_price=None))
        self.assertIn("fill_price", str(ctx.exception))
        self.assertIn("FILL", str(ctx.exception))

    def test_fill_missing_quantity_raises(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, fill_quantity=None))
        self.assertIn("fill_quantity", str(ctx.exception))

    def test_fill_missing_commission_raises(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, commission=None))
        self.assertIn("commission", str(ctx.exception))

    def test_partial_fill_missing_commission_raises(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(
                _event(OrderEventType.PARTIAL_FILL, fill_quantity=4, commission=None)
            )
        self.assertIn("commission", str(ctx.exception))
        self.assertIn("PARTIAL_FILL", str(ctx.exception))

    def test_fill_with_zero_quantity_raises(self) -> None:
        # A fill of zero shares is a runtime bug: the broker should not
        # ack a fill for nothing. Catch this at the dispatch boundary
        # rather than letting it reach user position-tracking code.
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, fill_quantity=0))
        self.assertIn("fill_quantity", str(ctx.exception))


class AssertPayloadTerminalBranchTest(unittest.TestCase):
    """SRS-SDK-004 AC half-B: CANCELLED / REJECTED / EXPIRED reason + cumulative fields."""

    def test_well_formed_never_filled_cancelled_is_silent(self) -> None:
        # Per the AC, CANCELLED events include fill_price / fill_quantity /
        # commission. A never-filled cancellation reports explicit zeros.
        assert_order_event_payload(
            _event(
                OrderEventType.CANCELLED,
                fill_price=0.0,
                fill_quantity=0,
                commission=0.0,
                cumulative_filled=0,
                remaining_quantity=10,
                reason="user requested",
            )
        )

    def test_well_formed_partially_filled_cancelled_is_silent(self) -> None:
        # A partially-filled cancellation reports cumulative average /
        # total — same field-presence contract as a fill event.
        assert_order_event_payload(
            _event(
                OrderEventType.CANCELLED,
                fill_price=99.5,
                fill_quantity=4,
                commission=0.02,
                cumulative_filled=4,
                remaining_quantity=6,
                reason="user requested",
            )
        )

    def test_well_formed_rejected_is_silent(self) -> None:
        assert_order_event_payload(
            _event(
                OrderEventType.REJECTED,
                fill_price=0.0,
                fill_quantity=0,
                commission=0.0,
                cumulative_filled=0,
                remaining_quantity=10,
                reason="insufficient buying power",
            )
        )

    def test_well_formed_expired_with_none_is_silent(self) -> None:
        # EXPIRED is a completeness member outside the AC's named four.
        # Concrete dispatchers may report None for fill_price /
        # fill_quantity / commission when the order never filled.
        assert_order_event_payload(
            _event(
                OrderEventType.EXPIRED,
                fill_price=None,
                fill_quantity=None,
                commission=None,
                cumulative_filled=0,
                remaining_quantity=10,
                reason="time-in-force lapsed",
            )
        )

    def test_cancelled_missing_fill_price_raises(self) -> None:
        # Per the AC, CANCELLED events include fill_price. None is a
        # contract violation regardless of whether the order filled —
        # dispatchers must populate 0.0 on a never-filled cancel.
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(
                _event(
                    OrderEventType.CANCELLED,
                    fill_price=None,
                    fill_quantity=0,
                    commission=0.0,
                    cumulative_filled=0,
                    remaining_quantity=10,
                    reason="user requested",
                )
            )
        self.assertIn("fill_price", str(ctx.exception))
        self.assertIn("CANCELLED", str(ctx.exception))

    def test_cancelled_missing_fill_quantity_raises(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(
                _event(
                    OrderEventType.CANCELLED,
                    fill_price=0.0,
                    fill_quantity=None,
                    commission=0.0,
                    cumulative_filled=0,
                    remaining_quantity=10,
                    reason="user requested",
                )
            )
        self.assertIn("fill_quantity", str(ctx.exception))

    def test_cancelled_missing_commission_raises(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(
                _event(
                    OrderEventType.CANCELLED,
                    fill_price=0.0,
                    fill_quantity=0,
                    commission=None,
                    cumulative_filled=0,
                    remaining_quantity=10,
                    reason="user requested",
                )
            )
        self.assertIn("commission", str(ctx.exception))

    def test_rejected_missing_fill_price_raises(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(
                _event(
                    OrderEventType.REJECTED,
                    fill_price=None,
                    fill_quantity=0,
                    commission=0.0,
                    cumulative_filled=0,
                    remaining_quantity=10,
                    reason="insufficient buying power",
                )
            )
        self.assertIn("fill_price", str(ctx.exception))
        self.assertIn("REJECTED", str(ctx.exception))

    def test_rejected_missing_commission_raises(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(
                _event(
                    OrderEventType.REJECTED,
                    fill_price=0.0,
                    fill_quantity=0,
                    commission=None,
                    cumulative_filled=0,
                    remaining_quantity=10,
                    reason="insufficient buying power",
                )
            )
        self.assertIn("commission", str(ctx.exception))

    def test_cancelled_missing_reason_raises(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(
                _event(
                    OrderEventType.CANCELLED,
                    fill_price=0.0,
                    fill_quantity=0,
                    commission=0.0,
                    cumulative_filled=0,
                    remaining_quantity=10,
                    reason=None,
                )
            )
        self.assertIn("reason", str(ctx.exception))
        self.assertIn("CANCELLED", str(ctx.exception))

    def test_rejected_with_empty_reason_raises(self) -> None:
        # An empty string is structurally present but semantically missing;
        # the structured-error contract (SyRS SYS-64) needs a routable
        # message.
        with self.assertRaises(OrderEventContractError):
            assert_order_event_payload(
                _event(
                    OrderEventType.REJECTED,
                    fill_price=0.0,
                    fill_quantity=0,
                    commission=0.0,
                    cumulative_filled=0,
                    remaining_quantity=10,
                    reason="",
                )
            )

    def test_expired_missing_reason_raises(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(
                _event(
                    OrderEventType.EXPIRED,
                    fill_price=None,
                    fill_quantity=None,
                    commission=None,
                    cumulative_filled=0,
                    remaining_quantity=10,
                    reason=None,
                )
            )
        self.assertIn("reason", str(ctx.exception))
        self.assertIn("EXPIRED", str(ctx.exception))


class AssertPayloadIdentifierBranchTest(unittest.TestCase):
    """Order identifiers must be present on every event."""

    def test_missing_order_id_raises(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, order_id=""))
        self.assertIn("order_id", str(ctx.exception))

    def test_missing_client_order_id_raises(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, client_order_id=""))
        self.assertIn("client_order_id", str(ctx.exception))

    def test_missing_strategy_id_raises(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, strategy_id=""))
        self.assertIn("strategy_id", str(ctx.exception))

    def test_missing_symbol_raises(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, symbol=""))
        self.assertIn("symbol", str(ctx.exception))


class PayloadShapeGuardTest(unittest.TestCase):
    """Malformed payloads must surface OrderEventContractError, not AttributeError.

    Concrete dispatchers crossing the Rust/Python boundary, an un-
    deserialized dict, a schema-drifted OrderEvent from a different
    SDK version, or a ``None`` placeholder all need to produce a
    structured ``OrderEventContractError`` so the SyRS SYS-64
    contract reaches user strategy code rather than leaking
    ``AttributeError`` on the first field access.
    """

    def test_none_payload_raises_structured_error(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(None)  # type: ignore[arg-type]
        message = str(ctx.exception)
        self.assertIn("payload is not an OrderEvent", message)
        self.assertIn("NoneType", message)

    def test_dict_payload_raises_structured_error(self) -> None:
        # Common shape from an unconverted JSON deserialization at the
        # Rust/Python boundary.
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(  # type: ignore[arg-type]
                {
                    "event_type": "FILL",
                    "order_id": "ord-1",
                    "client_order_id": "cli-1",
                }
            )
        self.assertIn("payload is not an OrderEvent", str(ctx.exception))
        self.assertIn("dict", str(ctx.exception))

    def test_arbitrary_object_payload_raises_structured_error(self) -> None:
        # Schema drift / version skew: a different class that quacks
        # like OrderEvent but is not one.
        class _Shadow:
            event_type = OrderEventType.FILL
            order_id = "ord-1"

        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_Shadow())  # type: ignore[arg-type]
        self.assertIn("payload is not an OrderEvent", str(ctx.exception))
        self.assertIn("_Shadow", str(ctx.exception))

    def test_does_not_raise_attribute_error_on_none(self) -> None:
        # Belt-and-braces: pin that an AttributeError CANNOT leak — the
        # structured-error contract is the public surface.
        try:
            assert_order_event_payload(None)  # type: ignore[arg-type]
        except OrderEventContractError:
            pass  # expected
        except AttributeError as exc:
            self.fail(
                f"AttributeError leaked past the structured-error "
                f"contract: {exc!r}"
            )


class EventTypeDiscriminantTest(unittest.TestCase):
    """OrderEventType discriminant must be validated at dispatch.

    ``OrderEventType`` is a ``StrEnum``, so a bare-string ``"FILL"``
    would equality-match enum members in ``in`` checks and then
    crash on ``.value`` access — leaking past the structured-error
    contract (SyRS SYS-64). The guard rejects any non-enum
    ``event_type``.
    """

    def _event_with_type(self, event_type: object) -> OrderEvent:
        return OrderEvent(
            event_type=event_type,  # type: ignore[arg-type]
            order_id="ord-1",
            client_order_id="cli-1",
            strategy_id="s1",
            symbol="AAPL",
            fill_price=100.0,
            fill_quantity=10,
            cumulative_filled=10,
            remaining_quantity=0,
            commission=0.05,
            reason=None,
            timestamp="2026-05-03T13:30:00Z",
        )

    def test_bare_string_fill_is_rejected_with_structured_error(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(self._event_with_type("FILL"))
        message = str(ctx.exception)
        self.assertIn("invalid event_type", message)
        self.assertIn("'FILL'", message)
        self.assertIn("str", message)

    def test_unknown_string_is_rejected_with_structured_error(self) -> None:
        # Without the discriminant check this case falls through every
        # `in` branch silently and reaches user code.
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(self._event_with_type("UNKNOWN"))
        self.assertIn("invalid event_type", str(ctx.exception))
        self.assertIn("'UNKNOWN'", str(ctx.exception))

    def test_none_event_type_is_rejected_with_structured_error(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(self._event_with_type(None))
        self.assertIn("invalid event_type", str(ctx.exception))
        self.assertIn("NoneType", str(ctx.exception))

    def test_arbitrary_object_is_rejected_with_structured_error(self) -> None:
        class _Other:
            value = "FILL"

        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(self._event_with_type(_Other()))
        self.assertIn("invalid event_type", str(ctx.exception))


class FieldTypeValidationTest(unittest.TestCase):
    """Schema-drift type errors must surface as structured errors.

    The dataclass annotations declare the schema; the runtime
    enforces it so Rust/Python boundary payloads with wrong-type
    fields (e.g. int for str, str for float) cannot reach user code.
    """

    def test_non_string_order_id_is_rejected(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, order_id=123))  # type: ignore[arg-type]
        self.assertIn("invalid order_id type", str(ctx.exception))
        self.assertIn("int", str(ctx.exception))

    def test_list_symbol_is_rejected(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, symbol=["AAPL"]))  # type: ignore[arg-type]
        self.assertIn("invalid symbol type", str(ctx.exception))

    def test_string_fill_price_is_rejected(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, fill_price="100.0"))  # type: ignore[arg-type]
        self.assertIn("invalid fill_price type", str(ctx.exception))
        self.assertIn("str", str(ctx.exception))

    def test_nan_commission_is_rejected(self) -> None:
        import math

        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, commission=math.nan))
        self.assertIn("invalid commission value", str(ctx.exception))
        self.assertIn("nan", str(ctx.exception))

    def test_inf_fill_price_is_rejected(self) -> None:
        import math

        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, fill_price=math.inf))
        self.assertIn("invalid fill_price value", str(ctx.exception))
        self.assertIn("inf", str(ctx.exception))

    def test_bool_fill_quantity_is_rejected(self) -> None:
        # ``True`` is an ``int`` subclass; reject it so a boolean slip
        # doesn't pass as a fill_quantity of 1.
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, fill_quantity=True))  # type: ignore[arg-type]
        self.assertIn("invalid fill_quantity type", str(ctx.exception))
        self.assertIn("bool", str(ctx.exception))

    def test_float_fill_quantity_is_rejected(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, fill_quantity=10.5))  # type: ignore[arg-type]
        self.assertIn("invalid fill_quantity type", str(ctx.exception))

    def test_negative_cumulative_filled_is_rejected(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, cumulative_filled=-1))
        self.assertIn("invalid cumulative_filled value", str(ctx.exception))

    def test_none_cumulative_filled_is_rejected(self) -> None:
        # cumulative_filled is non-Optional in the dataclass schema.
        # A dispatcher that emits None silently corrupts user
        # position-tracking; the boundary guard must reject it.
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(
                _event(OrderEventType.FILL, cumulative_filled=None)  # type: ignore[arg-type]
            )
        self.assertIn("invalid cumulative_filled type", str(ctx.exception))
        self.assertIn("non-Optional", str(ctx.exception))

    def test_none_remaining_quantity_is_rejected_on_partial_fill(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(
                _event(
                    OrderEventType.PARTIAL_FILL,
                    fill_quantity=4,
                    cumulative_filled=4,
                    remaining_quantity=None,  # type: ignore[arg-type]
                )
            )
        self.assertIn("invalid remaining_quantity type", str(ctx.exception))
        self.assertIn("non-Optional", str(ctx.exception))

    def test_none_remaining_quantity_is_rejected_on_cancelled(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(
                _event(
                    OrderEventType.CANCELLED,
                    fill_price=0.0,
                    fill_quantity=0,
                    commission=0.0,
                    cumulative_filled=0,
                    remaining_quantity=None,  # type: ignore[arg-type]
                    reason="user requested",
                )
            )
        self.assertIn("invalid remaining_quantity type", str(ctx.exception))

    def test_negative_remaining_quantity_is_rejected(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, remaining_quantity=-1))
        self.assertIn("invalid remaining_quantity value", str(ctx.exception))

    def test_empty_timestamp_is_rejected(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, timestamp=""))
        self.assertIn("missing timestamp", str(ctx.exception))

    def test_non_string_timestamp_is_rejected(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, timestamp=1234567890))  # type: ignore[arg-type]
        self.assertIn("invalid timestamp type", str(ctx.exception))

    def test_negative_fill_price_on_fill_is_rejected(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, fill_price=-1.0))
        self.assertIn("fill_price", str(ctx.exception))
        self.assertIn("non-negative", str(ctx.exception))

    def test_zero_fill_price_on_fill_is_rejected(self) -> None:
        # A fill at zero price is physically impossible.
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(_event(OrderEventType.FILL, fill_price=0.0))
        self.assertIn("fill_price must be positive", str(ctx.exception))

    def test_zero_fill_price_on_partial_fill_is_rejected(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(
                _event(OrderEventType.PARTIAL_FILL, fill_price=0.0, fill_quantity=4)
            )
        self.assertIn("PARTIAL_FILL", str(ctx.exception))
        self.assertIn("fill_price must be positive", str(ctx.exception))

    def test_negative_fill_price_on_cancelled_is_rejected(self) -> None:
        # Negative price is invalid on any event type — the never-
        # filled-cancel convention is explicit zero, not negative.
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(
                _event(
                    OrderEventType.CANCELLED,
                    fill_price=-1.0,
                    fill_quantity=0,
                    commission=0.0,
                    cumulative_filled=0,
                    remaining_quantity=10,
                    reason="user requested",
                )
            )
        self.assertIn("non-negative", str(ctx.exception))

    def test_partial_fill_with_zero_remaining_quantity_is_rejected(self) -> None:
        # A partial fill that leaves nothing working is actually a
        # final FILL — the mislabel would corrupt user order-state
        # machines if it slipped past the boundary.
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(
                _event(
                    OrderEventType.PARTIAL_FILL,
                    fill_quantity=10,
                    cumulative_filled=10,
                    remaining_quantity=0,
                )
            )
        self.assertIn("PARTIAL_FILL", str(ctx.exception))
        self.assertIn("remaining_quantity=0", str(ctx.exception))

    def test_fill_with_nonzero_remaining_quantity_is_rejected(self) -> None:
        # The mirror: a final FILL that leaves shares working is
        # actually a PARTIAL_FILL.
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(
                _event(
                    OrderEventType.FILL,
                    fill_quantity=4,
                    cumulative_filled=4,
                    remaining_quantity=6,
                )
            )
        self.assertIn("FILL event", str(ctx.exception))
        self.assertIn("remaining_quantity=6", str(ctx.exception))

    def test_partial_fill_with_remaining_quantity_above_zero_is_accepted(self) -> None:
        assert_order_event_payload(
            _event(
                OrderEventType.PARTIAL_FILL,
                fill_quantity=4,
                cumulative_filled=4,
                remaining_quantity=6,
            )
        )

    def test_cancelled_with_inconsistent_fill_vs_cumulative_is_rejected(self) -> None:
        # Documented terminal convention: CANCELLED.fill_quantity ==
        # CANCELLED.cumulative_filled (both report cumulative state).
        # A mismatch silently corrupts position/P&L reconciliation.
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(
                _event(
                    OrderEventType.CANCELLED,
                    fill_price=99.5,
                    fill_quantity=4,
                    commission=0.02,
                    cumulative_filled=0,  # inconsistent with fill_quantity=4
                    remaining_quantity=10,
                    reason="user requested",
                )
            )
        message = str(ctx.exception)
        self.assertIn("inconsistent cumulative state", message)
        self.assertIn("fill_quantity=4", message)
        self.assertIn("cumulative_filled=0", message)

    def test_rejected_with_nonzero_fill_zero_cumulative_is_rejected(self) -> None:
        # Mirror: a REJECTED claiming a 4-share fill must report
        # cumulative_filled=4.
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(
                _event(
                    OrderEventType.REJECTED,
                    fill_price=99.5,
                    fill_quantity=4,
                    commission=0.02,
                    cumulative_filled=0,
                    remaining_quantity=10,
                    reason="insufficient buying power",
                )
            )
        self.assertIn("inconsistent cumulative state", str(ctx.exception))

    def test_partially_filled_cancellation_with_consistent_state_is_accepted(self) -> None:
        # 4/4 + reason — the documented happy path for a partially-
        # filled terminal event.
        assert_order_event_payload(
            _event(
                OrderEventType.CANCELLED,
                fill_price=99.5,
                fill_quantity=4,
                commission=0.02,
                cumulative_filled=4,
                remaining_quantity=6,
                reason="user requested",
            )
        )

    def test_zero_fill_price_on_cancelled_is_accepted(self) -> None:
        # Zero IS valid on a never-filled cancellation — that's the
        # documented convention.
        assert_order_event_payload(
            _event(
                OrderEventType.CANCELLED,
                fill_price=0.0,
                fill_quantity=0,
                commission=0.0,
                cumulative_filled=0,
                remaining_quantity=10,
                reason="user requested",
            )
        )

    def test_non_string_reason_on_cancelled_is_rejected(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(
                _event(
                    OrderEventType.CANCELLED,
                    fill_price=0.0,
                    fill_quantity=0,
                    commission=0.0,
                    cumulative_filled=0,
                    remaining_quantity=10,
                    reason=42,  # type: ignore[arg-type]
                )
            )
        self.assertIn("invalid reason type", str(ctx.exception))


class OrderEventContractErrorTest(unittest.TestCase):
    """SyRS SYS-64: structured-error contract reaches user code."""

    def test_violation_is_subclass_of_strategy_api_error(self) -> None:
        self.assertTrue(issubclass(OrderEventContractError, StrategyAPIError))

    def test_violation_message_names_order_id(self) -> None:
        with self.assertRaises(OrderEventContractError) as ctx:
            assert_order_event_payload(
                _event(OrderEventType.FILL, order_id="ord-42", fill_price=None)
            )
        self.assertIn("ord-42", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
