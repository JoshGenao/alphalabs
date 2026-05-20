#!/usr/bin/env python3
"""SDK-surface contract evidence script for SRS-SDK-004 (order event callbacks).

This check verifies the **SDK-surface half** of the SRS-SDK-004
acceptance criteria — the Python Strategy SDK ships the structural
field-presence contract for order event callbacks. It does **not**
verify the SyRS ``NFR-P4`` p95 latency budgets end-to-end: those
budgets must be measured against the real live IB execution
(``SRS-EXE-001``) and internal paper simulation (``SRS-SIM-001``)
dispatchers, which are out of scope for this contract. Until those
subsystems ship, this check + its companion L7 reference dispatcher
in ``tests/domain/test_order_event_dispatch.py`` are the
prerequisite SDK surface; ``SRS-SDK-004`` stays ``passes: false``
in ``feature_list.json``.

What this check verifies (SDK surface only):

* ``OrderEventType`` enum exposes at least the four AC-named categories
  ``FILL``, ``PARTIAL_FILL``, ``CANCELLED``, and ``REJECTED`` (the
  ``ACK`` and ``EXPIRED`` members are required for completeness).
* ``OrderEvent`` dataclass carries the AC-required fields fill price,
  fill quantity, commission and order identifiers, plus the
  ``client_order_id`` / ``strategy_id`` / ``symbol`` / ``timestamp``
  fields concrete dispatchers must populate.
* A shipped ``assert_order_event_payload(event)`` helper, re-exported
  by the ``atp_strategy`` package, raises ``OrderEventContractError``
  on payloads that violate the AC-required field presence rules:
  FILL/PARTIAL_FILL missing fill_price/fill_quantity/commission, and
  CANCELLED/REJECTED/EXPIRED missing a reason string.
* Public callback-latency budget constants
  ``LIVE_CALLBACK_LATENCY_P95_MS == 1000`` and
  ``PAPER_CALLBACK_LATENCY_P95_MS == 100`` (SyRS ``NFR-P4``) so
  concrete live IB execution (``SRS-EXE-001``) and internal paper
  simulation (``SRS-SIM-001``) read the numbers from exactly one
  place rather than redefining them.
* ``Strategy.on_order_event`` callback signature is exactly
  ``(self, context, event)`` and its docstring publicly commits to
  the ``assert_order_event_payload`` guard so the delivery surface
  is identical for live and paper modes (``SRS-SDK-001`` AC-14).
* ``OrderEventContractError`` derives from ``StrategyAPIError`` and
  is re-exported by the ``atp_strategy`` package per SyRS ``SYS-64``.

Mirrors the PASS/FAIL output style of
``tools/strategy_api_subscriptions_check.py``.

Invoke:
    python3 tools/strategy_api_order_events_check.py
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class StrategyApiOrderEventsCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise StrategyApiOrderEventsCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "strategy_api_order_events_contract" not in config:
        fail("architecture metadata is missing strategy_api_order_events_contract")
    return config["strategy_api_order_events_contract"]


def _load_sdk_module(root: Path) -> object:
    """Reload ``atp_strategy`` from ``root`` (supports mutation-test tmpdirs)."""
    python_root = root / "python"
    if not python_root.is_dir():
        fail(f"python/ directory missing under {root}")
    str_root = str(python_root)
    if str_root in sys.path:
        sys.path.remove(str_root)
    sys.path.insert(0, str_root)
    for name in list(sys.modules):
        if name == "atp_strategy" or name.startswith("atp_strategy."):
            sys.modules.pop(name, None)
    try:
        return importlib.import_module("atp_strategy")
    except Exception as exc:  # pragma: no cover — surfaces as an order-events fail
        fail(f"failed to import atp_strategy from {python_root}: {exc!r}")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_order_event_type_members(config: dict, root: Path) -> str:
    block = contract_block(config)
    required = set(block["required_event_type_members"])
    api = _load_sdk_module(root)
    members = {m.name for m in api.OrderEventType}
    missing = required - members
    if missing:
        fail(
            f"OrderEventType is missing required members {sorted(missing)}; "
            "SRS-SDK-004 AC names FILL, PARTIAL_FILL, CANCELLED and REJECTED "
            "as the four lifecycle event categories user code must receive"
        )
    return (
        f"OrderEventType includes {sorted(required)} — SRS-SDK-004 AC "
        f"category set covered (full enum: {sorted(members)})"
    )


def check_order_event_fields(config: dict, root: Path) -> str:
    block = contract_block(config)
    required_fields = list(block["required_event_fields"])
    api = _load_sdk_module(root)
    fields = {f.name for f in dataclasses.fields(api.OrderEvent)}
    missing = [name for name in required_fields if name not in fields]
    if missing:
        fail(
            f"OrderEvent is missing required fields {missing} — SRS-SDK-004 "
            "AC requires fill price, fill quantity, commission and order "
            "identifiers on every event delivered to user code"
        )
    return (
        f"OrderEvent dataclass carries {sorted(required_fields)} — SRS-SDK-004 "
        "AC field presence locked"
    )


def check_assert_order_event_payload_helper(config: dict, root: Path) -> str:
    block = contract_block(config)
    required_helpers = list(block["required_helper_functions"])
    api = _load_sdk_module(root)
    for name in required_helpers:
        if not hasattr(api, name):
            fail(
                f"atp_strategy package is missing required helper function "
                f"{name!r} — concrete dispatchers must call the SDK-shipped "
                "guard rather than reimplementing field-presence checks"
            )
    helper = api.assert_order_event_payload
    if not callable(helper):
        fail("atp_strategy.assert_order_event_payload is not callable")
    sig = inspect.signature(helper)
    params = list(sig.parameters)
    if params != ["event"]:
        fail(f"assert_order_event_payload signature is {params!r}; expected ['event']")

    # Behavioural runtime exercise — catches "mutate body to pass" and
    # field-skip regressions that pure-static scans cannot see. The
    # ``remaining_quantity`` default is keyed to ``event_type`` so the
    # lifecycle-consistency rule is satisfied for both FILL (final,
    # rem=0) and PARTIAL_FILL (more working, rem>0) without each
    # caller having to pass it explicitly.
    def _event(
        event_type,
        *,
        fill_price=100.0,
        fill_quantity=10,
        commission=0.05,
        reason=None,
        remaining_quantity=None,
    ):
        if remaining_quantity is None:
            remaining_quantity = 6 if event_type == api.OrderEventType.PARTIAL_FILL else 0
        return api.OrderEvent(
            event_type=event_type,
            order_id="ord-1",
            client_order_id="cli-1",
            strategy_id="s1",
            symbol="AAPL",
            fill_price=fill_price,
            fill_quantity=fill_quantity,
            cumulative_filled=fill_quantity if fill_quantity else 0,
            remaining_quantity=remaining_quantity,
            commission=commission,
            reason=reason,
            timestamp="2026-05-03T13:30:00Z",
        )

    # Positive cases — every well-formed event must pass silently. The
    # SRS-SDK-004 AC names FILL / PARTIAL_FILL / CANCELLED / REJECTED as
    # the four callback categories whose payloads must include fill
    # price, fill quantity, and commission; dispatchers populate
    # explicit zeros on a never-filled cancel or reject and the
    # cumulative average / total on a partially-filled cancel or
    # reject. ACK / EXPIRED are completeness members and may carry
    # None for those fields.
    good_events = (
        _event(api.OrderEventType.FILL),
        _event(api.OrderEventType.PARTIAL_FILL, fill_quantity=4),
        # Never-filled cancellation — zeros, not None — per the AC.
        _event(
            api.OrderEventType.CANCELLED,
            fill_price=0.0,
            fill_quantity=0,
            commission=0.0,
            reason="user requested",
        ),
        # Partially-filled cancellation — cumulative average / total.
        _event(
            api.OrderEventType.CANCELLED,
            fill_price=99.5,
            fill_quantity=4,
            commission=0.02,
            reason="user requested",
        ),
        _event(
            api.OrderEventType.REJECTED,
            fill_price=0.0,
            fill_quantity=0,
            commission=0.0,
            reason="insufficient buying power",
        ),
        _event(
            api.OrderEventType.EXPIRED,
            fill_price=None,
            fill_quantity=None,
            commission=None,
            reason="time-in-force lapsed",
        ),
        _event(
            api.OrderEventType.ACK,
            fill_price=None,
            fill_quantity=None,
            commission=None,
        ),
    )
    for ev in good_events:
        try:
            helper(ev)
        except api.OrderEventContractError as exc:
            fail(f"assert_order_event_payload raised on well-formed {ev.event_type.value}: {exc!r}")

    # Negative cases — malformed payloads (None, dict, schema-drifted
    # object) must produce a structured OrderEventContractError, not
    # an AttributeError on the first field access. The payload-shape
    # guard at the top of the helper is what enforces this.
    malformed_payload_cases = (
        ("None payload", None, "payload is not an OrderEvent"),
        (
            "dict payload",
            {"event_type": "FILL", "order_id": "ord-x"},
            "payload is not an OrderEvent",
        ),
    )
    for label, malformed, expected_token in malformed_payload_cases:
        try:
            helper(malformed)
        except api.OrderEventContractError as exc:
            if expected_token not in str(exc):
                fail(
                    f"assert_order_event_payload raised on {label} but the "
                    f"message does not signal the payload-shape violation "
                    f"({expected_token!r}): {str(exc)!r}"
                )
        except Exception as exc:
            fail(
                f"assert_order_event_payload raised a non-structured "
                f"{type(exc).__name__} on {label}: {exc!r} — the guard "
                "must surface OrderEventContractError per SyRS SYS-64 "
                "even on schema/version drift at the Rust/Python boundary"
            )
        else:
            fail(
                f"assert_order_event_payload did not raise on {label} — "
                "the payload-shape guard at dispatch is missing"
            )

    # Negative cases — wrong-type / non-finite field values must
    # produce a structured error (OrderEventContractError), not pass
    # through to user code where downstream P&L / routing would
    # crash or silently corrupt. These cover the most common
    # Rust/Python boundary drift patterns: ints where strings are
    # expected, strings where numerics are expected, NaN / inf, and
    # boolean slips through ``int`` annotations.
    import math as _math_local

    def _e(**overrides):
        base = dict(
            event_type=api.OrderEventType.FILL,
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
        base.update(overrides)
        return api.OrderEvent(**base)

    type_violation_cases = (
        ("order_id as int", _e(order_id=123), "invalid order_id type"),
        ("symbol as list", _e(symbol=["AAPL"]), "invalid symbol type"),
        ("fill_price as string", _e(fill_price="100.0"), "invalid fill_price type"),
        ("commission as NaN", _e(commission=_math_local.nan), "invalid commission value"),
        ("fill_price as inf", _e(fill_price=_math_local.inf), "invalid fill_price value"),
        ("fill_quantity as bool", _e(fill_quantity=True), "invalid fill_quantity type"),
        ("fill_quantity as float", _e(fill_quantity=10.5), "invalid fill_quantity type"),
        (
            "cumulative_filled negative",
            _e(cumulative_filled=-1),
            "invalid cumulative_filled value",
        ),
        ("timestamp as int", _e(timestamp=1234567890), "invalid timestamp type"),
        # Negative fill_price has no physical meaning — must surface
        # at the dispatch boundary, not corrupt downstream P&L.
        (
            "fill_price negative",
            _e(fill_price=-1.0),
            "fill_price value: expected non-negative",
        ),
        # Zero fill_price on a FILL event is also impossible (you
        # cannot fill at zero); the FILL/PARTIAL_FILL branch demands
        # strictly positive price.
        (
            "FILL with zero fill_price",
            _e(fill_price=0.0),
            "fill_price must be positive",
        ),
        # Lifecycle consistency: PARTIAL_FILL with no remaining is
        # actually a final FILL — catch the mislabel at the boundary.
        (
            "PARTIAL_FILL with remaining_quantity=0",
            _e(
                event_type=api.OrderEventType.PARTIAL_FILL,
                fill_quantity=4,
                remaining_quantity=0,
            ),
            "PARTIAL_FILL event",
        ),
        (
            "FILL with remaining_quantity > 0",
            _e(remaining_quantity=5),
            "FILL event",
        ),
        # Cross-field cumulative-state invariant on terminal events.
        # CANCELLED / REJECTED report cumulative state; fill_quantity
        # must match cumulative_filled.
        (
            "CANCELLED with fill_quantity != cumulative_filled",
            _e(
                event_type=api.OrderEventType.CANCELLED,
                fill_price=99.5,
                fill_quantity=4,
                commission=0.02,
                cumulative_filled=0,
                remaining_quantity=10,
                reason="user requested",
            ),
            "inconsistent cumulative state",
        ),
        # Non-Optional order-state quantity fields must reject None.
        (
            "FILL with cumulative_filled=None",
            _e(cumulative_filled=None),
            "invalid cumulative_filled type",
        ),
        (
            "PARTIAL_FILL with remaining_quantity=None",
            _e(
                event_type=api.OrderEventType.PARTIAL_FILL,
                fill_quantity=4,
                cumulative_filled=4,
                remaining_quantity=None,
            ),
            "invalid remaining_quantity type",
        ),
    )
    for label, bad_event, expected_token in type_violation_cases:
        try:
            helper(bad_event)
        except api.OrderEventContractError as exc:
            if expected_token not in str(exc):
                fail(
                    f"assert_order_event_payload raised on {label} but the "
                    f"message does not signal the type/value violation "
                    f"({expected_token!r}): {str(exc)!r}"
                )
        except Exception as exc:
            fail(
                f"assert_order_event_payload raised a non-structured "
                f"{type(exc).__name__} on {label}: {exc!r} — schema "
                "drift at the Rust/Python boundary must surface as "
                "OrderEventContractError per SyRS SYS-64"
            )
        else:
            fail(
                f"assert_order_event_payload did not raise on {label} — "
                "runtime type / finite enforcement at dispatch is missing"
            )

    # Negative cases — invalid event_type discriminants must produce a
    # structured error (OrderEventContractError), not a Python
    # AttributeError on the later ``.value`` access. Construct these
    # via raw OrderEvent calls so the bad value reaches the helper.
    invalid_event_type_cases = (
        ("bare-string 'FILL' event_type", "FILL"),
        ("unknown-string event_type", "UNKNOWN"),
        ("None event_type", None),
    )
    for label, bad_event_type in invalid_event_type_cases:
        bad_event = api.OrderEvent(
            event_type=bad_event_type,
            order_id="ord-x",
            client_order_id="cli-x",
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
        try:
            helper(bad_event)
        except api.OrderEventContractError as exc:
            if "invalid event_type" not in str(exc):
                fail(
                    f"assert_order_event_payload raised on {label} but the "
                    f"message does not signal an invalid event_type: "
                    f"{str(exc)!r}"
                )
        except Exception as exc:
            fail(
                f"assert_order_event_payload raised a non-structured "
                f"{type(exc).__name__} on {label}: {exc!r} — the guard "
                "must surface OrderEventContractError per SyRS SYS-64 so "
                "schema/version drift cannot bypass the structured-error "
                "contract"
            )
        else:
            fail(
                f"assert_order_event_payload did not raise on {label} — "
                "the discriminant validation at dispatch is missing; "
                "a bare-string or unknown event_type would silently "
                "reach user code"
            )

    # Negative cases — each AC-required missing field must raise across
    # all four AC-named callback categories.
    negatives = (
        ("FILL missing fill_price", _event(api.OrderEventType.FILL, fill_price=None), "fill_price"),
        (
            "FILL missing fill_quantity",
            _event(api.OrderEventType.FILL, fill_quantity=None),
            "fill_quantity",
        ),
        (
            "FILL missing commission",
            _event(api.OrderEventType.FILL, commission=None),
            "commission",
        ),
        (
            "PARTIAL_FILL missing fill_price",
            _event(api.OrderEventType.PARTIAL_FILL, fill_price=None, fill_quantity=4),
            "fill_price",
        ),
        (
            "PARTIAL_FILL missing commission",
            _event(api.OrderEventType.PARTIAL_FILL, fill_quantity=4, commission=None),
            "commission",
        ),
        # CANCELLED / REJECTED also covered by the AC — None on the
        # named fields must raise.
        (
            "CANCELLED missing fill_price",
            _event(
                api.OrderEventType.CANCELLED,
                fill_price=None,
                fill_quantity=0,
                commission=0.0,
                reason="user requested",
            ),
            "fill_price",
        ),
        (
            "CANCELLED missing fill_quantity",
            _event(
                api.OrderEventType.CANCELLED,
                fill_price=0.0,
                fill_quantity=None,
                commission=0.0,
                reason="user requested",
            ),
            "fill_quantity",
        ),
        (
            "CANCELLED missing commission",
            _event(
                api.OrderEventType.CANCELLED,
                fill_price=0.0,
                fill_quantity=0,
                commission=None,
                reason="user requested",
            ),
            "commission",
        ),
        (
            "REJECTED missing fill_price",
            _event(
                api.OrderEventType.REJECTED,
                fill_price=None,
                fill_quantity=0,
                commission=0.0,
                reason="insufficient buying power",
            ),
            "fill_price",
        ),
        (
            "REJECTED missing commission",
            _event(
                api.OrderEventType.REJECTED,
                fill_price=0.0,
                fill_quantity=0,
                commission=None,
                reason="insufficient buying power",
            ),
            "commission",
        ),
        # Reason requirement on terminal events.
        (
            "CANCELLED missing reason",
            _event(
                api.OrderEventType.CANCELLED,
                fill_price=0.0,
                fill_quantity=0,
                commission=0.0,
                reason=None,
            ),
            "reason",
        ),
        (
            "REJECTED missing reason",
            _event(
                api.OrderEventType.REJECTED,
                fill_price=0.0,
                fill_quantity=0,
                commission=0.0,
                reason=None,
            ),
            "reason",
        ),
        (
            "EXPIRED missing reason",
            _event(
                api.OrderEventType.EXPIRED,
                fill_price=None,
                fill_quantity=None,
                commission=None,
                reason=None,
            ),
            "reason",
        ),
    )
    for label, ev, expected_field in negatives:
        try:
            helper(ev)
        except api.OrderEventContractError as exc:
            message = str(exc)
            if expected_field not in message:
                fail(
                    f"assert_order_event_payload raised on {label} but the "
                    f"message does not name the missing field "
                    f"{expected_field!r}: {message!r}"
                )
            if ev.event_type.value not in message:
                fail(
                    f"assert_order_event_payload raised on {label} but the "
                    f"message does not name the event_type "
                    f"{ev.event_type.value!r}: {message!r}"
                )
        else:
            fail(
                f"assert_order_event_payload did not raise on {label} — "
                "SRS-SDK-004 AC requires field-presence enforcement at "
                "dispatch so user code can rely on the documented payload"
            )

    return (
        "assert_order_event_payload(event) helper shipped and re-exported; "
        "raises OrderEventContractError on FILL/PARTIAL_FILL/CANCELLED/"
        "REJECTED missing fill_price/fill_quantity/commission and on "
        "CANCELLED/REJECTED/"
        "EXPIRED missing reason; silent on well-formed events — SRS-SDK-004 "
        "AC field-presence invariant enforced behaviourally"
    )


def check_callback_latency_budgets(config: dict, root: Path) -> str:
    block = contract_block(config)
    required_live = int(block["required_live_callback_latency_p95_ms"])
    required_paper = int(block["required_paper_callback_latency_p95_ms"])
    api = _load_sdk_module(root)
    live = getattr(api, "LIVE_CALLBACK_LATENCY_P95_MS", None)
    paper = getattr(api, "PAPER_CALLBACK_LATENCY_P95_MS", None)
    if not isinstance(live, int) or isinstance(live, bool):
        fail(f"LIVE_CALLBACK_LATENCY_P95_MS must be an int (got {type(live).__name__}={live!r})")
    if not isinstance(paper, int) or isinstance(paper, bool):
        fail(f"PAPER_CALLBACK_LATENCY_P95_MS must be an int (got {type(paper).__name__}={paper!r})")
    if live != required_live:
        fail(
            f"LIVE_CALLBACK_LATENCY_P95_MS is {live}; expected {required_live} — "
            "SyRS NFR-P4 pins the live callback p95 budget at 1,000 ms and "
            "the SDK must be the single source of truth for the number"
        )
    if paper != required_paper:
        fail(
            f"PAPER_CALLBACK_LATENCY_P95_MS is {paper}; expected "
            f"{required_paper} — SyRS NFR-P4 pins the paper callback p95 "
            "budget at 100 ms and the SDK must be the single source of "
            "truth for the number"
        )
    return (
        f"LIVE_CALLBACK_LATENCY_P95_MS == {required_live} and "
        f"PAPER_CALLBACK_LATENCY_P95_MS == {required_paper} — Python "
        "SDK constants in parity with the cross-language source of "
        "truth in architecture/runtime_services.json (Rust core "
        "dispatchers read the metadata directly per AGENTS.md "
        "dependency direction; the contract check enforces parity)"
    )


def check_on_order_event_callback(config: dict, root: Path) -> str:
    block = contract_block(config)
    required_params = list(block["required_on_order_event_params"])
    required_tokens = list(block["required_on_order_event_docstring_tokens"])
    api = _load_sdk_module(root)
    callback = getattr(api.Strategy, "on_order_event", None)
    if not callable(callback):
        fail("Strategy.on_order_event is not callable")
    sig = inspect.signature(callback)
    params = list(sig.parameters)
    if params != required_params:
        fail(
            f"Strategy.on_order_event signature is {params!r}; expected "
            f"{required_params!r} — SRS-SDK-004 user-facing surface"
        )
    doc = inspect.getdoc(callback) or ""
    missing_tokens = [t for t in required_tokens if t not in doc]
    if missing_tokens:
        fail(
            f"Strategy.on_order_event docstring is missing required tokens "
            f"{missing_tokens} — concrete dispatchers must publicly read "
            "the guard and latency budgets from the documented surface"
        )
    return (
        f"Strategy.on_order_event({', '.join(required_params)}) signature "
        f"locked; docstring names {required_tokens} — concrete dispatchers "
        "read the guard and latency budgets from a documented surface"
    )


def check_order_event_contract_error_export(config: dict, root: Path) -> str:
    block = contract_block(config)
    required_exports = set(block["required_exports"])
    api = _load_sdk_module(root)
    missing_exports = [name for name in required_exports if not hasattr(api, name)]
    if missing_exports:
        fail(f"atp_strategy package is missing required exports {missing_exports}")
    if not issubclass(api.OrderEventContractError, api.StrategyAPIError):
        fail(
            "OrderEventContractError must derive from StrategyAPIError so the "
            "structured-error contract (SyRS SYS-64) reaches user strategy "
            "code through the documented base class"
        )
    return (
        f"atp_strategy re-exports {sorted(required_exports)}; "
        "OrderEventContractError subclasses StrategyAPIError per SyRS SYS-64"
    )


# --------------------------------------------------------------------------- #
# Public assert helper used by the L3 mutation rig
# --------------------------------------------------------------------------- #


def assert_strategy_api_order_events_static(
    config: dict | None = None, root: Path = ROOT
) -> list[str]:
    """Run every order-events-contract check and return evidence strings.

    Raises ``StrategyApiOrderEventsCheckError`` on the first failure.
    """
    config = config if config is not None else load_config(root)
    # Run the export check first so a missing OrderEventContractError or
    # missing assert_order_event_payload surfaces structurally before the
    # behavioural exercise tries to construct an ``except`` clause against
    # the missing class.
    return [
        check_order_event_contract_error_export(config, root),
        check_order_event_type_members(config, root),
        check_order_event_fields(config, root),
        check_assert_order_event_payload_helper(config, root),
        check_callback_latency_budgets(config, root),
        check_on_order_event_callback(config, root),
    ]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="repository root (default: the parent of this script's dir)",
    )
    args = parser.parse_args(argv)
    try:
        evidence = assert_strategy_api_order_events_static(root=args.root)
    except StrategyApiOrderEventsCheckError as exc:
        print(f"SRS-SDK-004 SDK-SURFACE FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        "SRS-SDK-004 SDK-SURFACE PASS — order event callback contract "
        "(NFR-P4 p95 latency proof deferred to SRS-EXE-001 + SRS-SIM-001)"
    )
    for line in evidence:
        print(f"  * {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
