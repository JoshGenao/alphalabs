"""Contract tests for SRS-SDK-004 (SyRS SYS-7 / SYS-85 / NFR-P4;
StRS SN-1.22 / SN-1.29 / BG-1).

Shells out to ``tools/strategy_api_order_events_check.py`` for the
positive-evidence path, then mutates a tmpdir copy of
``python/atp_strategy/`` to verify each invariant in the order-events
contract actually catches a regression: dropped OrderEventType members,
dropped OrderEvent fields, missing or silenced assert_order_event_payload
body, latency-budget constants drift, Strategy.on_order_event signature/
docstring drift, and missing OrderEventContractError export.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from strategy_api_order_events_check import (  # noqa: E402
    StrategyApiOrderEventsCheckError,
    assert_strategy_api_order_events_static,
    load_config,
)


class _MutationRig:
    """Copy ``python/atp_strategy/`` into a tmpdir and run the order-events check."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "python").mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            ROOT / "python" / "atp_strategy",
            self.root / "python" / "atp_strategy",
        )

    def close(self) -> None:
        self._tmp.cleanup()

    def mutate(self, relpath: str, *, find: str, replace: str) -> None:
        target = self.root / "python" / "atp_strategy" / relpath
        text = target.read_text(encoding="utf-8")
        if find not in text:
            raise AssertionError(f"mutation rig: substring not found in {relpath}: {find!r}")
        target.write_text(text.replace(find, replace, 1), encoding="utf-8")

    def run(self, config: dict) -> list[str]:
        return assert_strategy_api_order_events_static(config, root=self.root)


class StrategyApiOrderEventsScriptTest(unittest.TestCase):
    """Positive evidence: the CLI emits the required evidence needles."""

    def test_script_passes_and_emits_evidence_needles(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/strategy_api_order_events_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-SDK-004 SDK-SURFACE PASS", result.stdout)
        # Make the scope honesty explicit in the script output: the
        # latency-proof half of NFR-P4 is deferred until the real
        # dispatchers ship (SRS-EXE-001 + SRS-SIM-001).
        self.assertIn(
            "NFR-P4 p95 latency proof deferred to SRS-EXE-001 + SRS-SIM-001",
            result.stdout,
        )
        for needle in (
            "OrderEventType includes ['CANCELLED', 'FILL', 'PARTIAL_FILL', 'REJECTED']",
            "SRS-SDK-004 AC category set covered",
            "OrderEvent dataclass carries",
            "SRS-SDK-004 AC field presence locked",
            "assert_order_event_payload(event) helper shipped and re-exported",
            "raises OrderEventContractError on FILL/PARTIAL_FILL/CANCELLED/REJECTED missing",
            "SRS-SDK-004 AC field-presence invariant enforced behaviourally",
            "LIVE_CALLBACK_LATENCY_P95_MS == 1000",
            "PAPER_CALLBACK_LATENCY_P95_MS == 100",
            "Python SDK constants in parity with the cross-language source of truth",
            "Rust core dispatchers read the metadata directly per AGENTS.md dependency direction",
            "Strategy.on_order_event(self, context, event) signature locked",
            "OrderEventContractError subclasses StrategyAPIError per SyRS SYS-64",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class OrderEventTypeMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_dropping_fill_member_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find='    FILL = "FILL"\n',
            replace="",
        )
        with self.assertRaises(StrategyApiOrderEventsCheckError):
            self.rig.run(self.config)

    def test_dropping_partial_fill_member_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find='    PARTIAL_FILL = "PARTIAL_FILL"\n',
            replace="",
        )
        with self.assertRaisesRegex(
            StrategyApiOrderEventsCheckError, r"OrderEventType is missing required"
        ):
            self.rig.run(self.config)

    def test_dropping_cancelled_member_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find='    CANCELLED = "CANCELLED"\n',
            replace="",
        )
        with self.assertRaises(StrategyApiOrderEventsCheckError):
            self.rig.run(self.config)

    def test_dropping_rejected_member_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find='    REJECTED = "REJECTED"\n',
            replace="",
        )
        with self.assertRaises(StrategyApiOrderEventsCheckError):
            self.rig.run(self.config)


class OrderEventFieldMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_dropping_fill_price_field_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find="    fill_price: float | None\n",
            replace="",
        )
        with self.assertRaisesRegex(
            StrategyApiOrderEventsCheckError,
            r"OrderEvent is missing required fields",
        ):
            self.rig.run(self.config)

    def test_dropping_commission_field_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find="    commission: float | None\n",
            replace="",
        )
        with self.assertRaisesRegex(
            StrategyApiOrderEventsCheckError,
            r"OrderEvent is missing required fields",
        ):
            self.rig.run(self.config)

    def test_dropping_client_order_id_field_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find="    client_order_id: str\n",
            replace="",
        )
        with self.assertRaisesRegex(
            StrategyApiOrderEventsCheckError,
            r"OrderEvent is missing required fields",
        ):
            self.rig.run(self.config)


class AssertOrderEventPayloadMutationTest(unittest.TestCase):
    """Behavioural mutations: silent body, dropped branches, identifier slip."""

    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_silencing_helper_body_is_caught(self) -> None:
        # Replace the entire body's first statement with `return None`
        # so the helper never raises. The behavioural exercise in the
        # check must catch this. The line below is the ruff-formatted
        # one-liner form of the order_id raise — if ruff regrows the
        # statement across multiple lines on a future format pass, this
        # find string is the canary that needs updating.
        self.rig.mutate(
            "api.py",
            find=(
                "    if not event.order_id:\n"
                '        raise OrderEventContractError(f"{event.event_type.value} event missing order_id")\n'
            ),
            replace="    return None\n",
        )
        # The exact case that catches the silenced helper depends on
        # the order of negative cases in the contract check. Pin only
        # the canary phrase 'did not raise on FILL' — the mutation has
        # silenced *every* check downstream, so any FILL negative is a
        # valid signal.
        with self.assertRaisesRegex(
            StrategyApiOrderEventsCheckError,
            r"did not raise on FILL",
        ):
            self.rig.run(self.config)

    def test_dropping_ac_named_four_branch_is_caught(self) -> None:
        # If the AC-named-four field-presence branch is short-circuited,
        # FILL events with missing fill_price would silently pass — and
        # so would CANCELLED / REJECTED with None fill_price /
        # fill_quantity / commission, which is the AC half Codex's
        # judgment review flagged in the first pass on this feature.
        self.rig.mutate(
            "api.py",
            find=(
                "    if event.event_type in ac_named_four:\n"
                "        if event.fill_price is None:\n"
            ),
            replace=(
                "    if False and event.event_type in ac_named_four:\n"
                "        if event.fill_price is None:\n"
            ),
        )
        with self.assertRaisesRegex(
            StrategyApiOrderEventsCheckError,
            r"did not raise on FILL missing fill_price",
        ):
            self.rig.run(self.config)

    def test_dropping_cancelled_from_ac_named_four_is_caught(self) -> None:
        # Codex AC half: CANCELLED must be in the AC-named four. If a
        # future change drops it back to FILL/PARTIAL_FILL-only, the
        # contract weakens and the behavioural exercise must catch the
        # CANCELLED-missing-fill_price negative case.
        self.rig.mutate(
            "api.py",
            find=(
                "    ac_named_four = (\n"
                "        OrderEventType.FILL,\n"
                "        OrderEventType.PARTIAL_FILL,\n"
                "        OrderEventType.CANCELLED,\n"
                "        OrderEventType.REJECTED,\n"
                "    )\n"
            ),
            replace=(
                "    ac_named_four = (\n"
                "        OrderEventType.FILL,\n"
                "        OrderEventType.PARTIAL_FILL,\n"
                "    )\n"
            ),
        )
        with self.assertRaisesRegex(
            StrategyApiOrderEventsCheckError,
            r"did not raise on CANCELLED missing fill_price",
        ):
            self.rig.run(self.config)

    def test_dropping_terminal_branch_is_caught(self) -> None:
        # If the CANCELLED/REJECTED/EXPIRED reason branch is removed,
        # the structured-error contract goes silent.
        self.rig.mutate(
            "api.py",
            find=(
                "    if event.event_type in (\n"
                "        OrderEventType.CANCELLED,\n"
                "        OrderEventType.REJECTED,\n"
                "        OrderEventType.EXPIRED,\n"
                "    ):\n"
                "        if not event.reason:\n"
            ),
            replace=("    if False:\n        if not event.reason:\n"),
        )
        with self.assertRaisesRegex(
            StrategyApiOrderEventsCheckError,
            r"did not raise on (CANCELLED|REJECTED|EXPIRED) missing reason",
        ):
            self.rig.run(self.config)

    def test_inverting_fill_price_check_is_caught(self) -> None:
        # Swap `is None` for `is not None` — the helper now raises on
        # well-formed FILL events and accepts payloads with no
        # fill_price. Both halves trip the behavioural exercise.
        self.rig.mutate(
            "api.py",
            find="        if event.fill_price is None:\n",
            replace="        if event.fill_price is not None:\n",
        )
        with self.assertRaises(StrategyApiOrderEventsCheckError):
            self.rig.run(self.config)

    def test_removing_helper_definition_is_caught(self) -> None:
        # Renaming the def out from under the package __init__'s
        # explicit `from .api import assert_order_event_payload` surfaces
        # as an ImportError during module load. The check raises either
        # way.
        self.rig.mutate(
            "api.py",
            find='def assert_order_event_payload(event: "OrderEvent") -> None:',
            replace='def _removed_assert_order_event_payload(event: "OrderEvent") -> None:',
        )
        with self.assertRaises(StrategyApiOrderEventsCheckError):
            self.rig.run(self.config)

    def test_dropping_type_validation_helpers_is_caught(self) -> None:
        # If the type-validation helpers are short-circuited, an int
        # order_id / NaN commission / list symbol would slip through to
        # downstream code. The behavioural exercise catches this.
        self.rig.mutate(
            "api.py",
            find="    _require_str(\"order_id\", event.order_id)\n",
            replace="    pass  # _require_str disabled\n",
        )
        with self.assertRaises(StrategyApiOrderEventsCheckError):
            self.rig.run(self.config)

    def test_dropping_partial_fill_lifecycle_check_is_caught(self) -> None:
        # If the PARTIAL_FILL remaining_quantity > 0 check is removed,
        # a final fill mislabeled as PARTIAL_FILL would slip past the
        # boundary. The contract check's lifecycle negative catches it.
        self.rig.mutate(
            "api.py",
            find=(
                "    if event.event_type == OrderEventType.PARTIAL_FILL:\n"
                "        if event.remaining_quantity == 0:\n"
            ),
            replace=(
                "    if False and event.event_type == OrderEventType.PARTIAL_FILL:\n"
                "        if event.remaining_quantity == 0:\n"
            ),
        )
        with self.assertRaises(StrategyApiOrderEventsCheckError):
            self.rig.run(self.config)

    def test_dropping_terminal_cumulative_invariant_is_caught(self) -> None:
        # If the fill_quantity == cumulative_filled invariant is
        # removed from terminal events, a dispatcher could report
        # contradictory state and corrupt user P&L reconciliation.
        self.rig.mutate(
            "api.py",
            find=(
                "    if event.event_type in (OrderEventType.CANCELLED, "
                "OrderEventType.REJECTED):\n"
                "        if event.fill_quantity is not None and event.fill_quantity"
                " != event.cumulative_filled:\n"
            ),
            replace=(
                "    if False and event.event_type in (OrderEventType.CANCELLED, "
                "OrderEventType.REJECTED):\n"
                "        if event.fill_quantity is not None and event.fill_quantity"
                " != event.cumulative_filled:\n"
            ),
        )
        with self.assertRaises(StrategyApiOrderEventsCheckError):
            self.rig.run(self.config)

    def test_dropping_final_fill_lifecycle_check_is_caught(self) -> None:
        # The mirror — if the FILL remaining_quantity == 0 check is
        # removed, a partial-fill mislabeled as FILL would slip past.
        self.rig.mutate(
            "api.py",
            find=(
                "    if event.event_type == OrderEventType.FILL:\n"
                "        if event.remaining_quantity != 0:\n"
            ),
            replace=(
                "    if False and event.event_type == OrderEventType.FILL:\n"
                "        if event.remaining_quantity != 0:\n"
            ),
        )
        with self.assertRaises(StrategyApiOrderEventsCheckError):
            self.rig.run(self.config)

    def test_dropping_fill_price_sign_check_is_caught(self) -> None:
        # Negative fill_price has no physical meaning. If the sign
        # guard is removed, a negative price would slip past the
        # boundary — the contract check's negative case catches it.
        self.rig.mutate(
            "api.py",
            find="    if event.fill_price is not None and event.fill_price < 0:\n",
            replace=(
                "    if False and event.fill_price is not None and "
                "event.fill_price < 0:\n"
            ),
        )
        with self.assertRaises(StrategyApiOrderEventsCheckError):
            self.rig.run(self.config)

    def test_dropping_cumulative_filled_non_optional_check_is_caught(self) -> None:
        # If the non-Optional integer check on cumulative_filled is
        # weakened to allow None, a dispatcher could emit None and
        # silently corrupt user position-tracking. The contract
        # check's None case must catch it.
        self.rig.mutate(
            "api.py",
            find=(
                '    _require_non_negative_int("cumulative_filled", '
                "event.cumulative_filled)\n"
            ),
            replace=(
                '    _require_non_negative_int("cumulative_filled", '
                "event.cumulative_filled, allow_none=True)\n"
            ),
        )
        with self.assertRaises(StrategyApiOrderEventsCheckError):
            self.rig.run(self.config)

    def test_dropping_finite_check_is_caught(self) -> None:
        # If finite enforcement is dropped, NaN/inf commission slips
        # past — the contract check's NaN case must catch it.
        self.rig.mutate(
            "api.py",
            find="        if not _math.isfinite(float(value)):\n",
            replace="        if False and not _math.isfinite(float(value)):\n",
        )
        with self.assertRaises(StrategyApiOrderEventsCheckError):
            self.rig.run(self.config)

    def test_dropping_payload_shape_guard_is_caught(self) -> None:
        # If the upfront isinstance(event, OrderEvent) check is
        # removed, None / dict / schema-drifted payloads would
        # AttributeError on the first .event_type access — leaking
        # past the structured-error contract.
        self.rig.mutate(
            "api.py",
            find="    if not isinstance(event, OrderEvent):\n",
            replace="    if False and not isinstance(event, OrderEvent):\n",
        )
        with self.assertRaises(StrategyApiOrderEventsCheckError):
            self.rig.run(self.config)

    def test_dropping_event_type_discriminant_check_is_caught(self) -> None:
        # If the isinstance check is removed, a bare-string event_type
        # would slip past the AC-branch (StrEnum equality) and then
        # crash on .value. The behavioural exercise covers this via
        # the new bare-string negative case in the contract check.
        self.rig.mutate(
            "api.py",
            find="    if not isinstance(event.event_type, OrderEventType):\n",
            replace="    if False and not isinstance(event.event_type, OrderEventType):\n",
        )
        with self.assertRaises(StrategyApiOrderEventsCheckError):
            self.rig.run(self.config)

    def test_dropping_missing_field_from_error_message_is_caught(self) -> None:
        # The structured-error contract requires the missing field to
        # be named so user code can route on it.
        self.rig.mutate(
            "api.py",
            find='f"{event.event_type.value} event {event.order_id} missing fill_price"',
            replace='f"{event.event_type.value} event {event.order_id} bad payload"',
        )
        with self.assertRaisesRegex(
            StrategyApiOrderEventsCheckError,
            r"message does not name the missing field",
        ):
            self.rig.run(self.config)


class LatencyBudgetMutationTest(unittest.TestCase):
    """SyRS NFR-P4: budgets must read 1000 / 100."""

    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_changing_live_budget_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find="LIVE_CALLBACK_LATENCY_P95_MS: int = 1000",
            replace="LIVE_CALLBACK_LATENCY_P95_MS: int = 2500",
        )
        with self.assertRaisesRegex(
            StrategyApiOrderEventsCheckError,
            r"LIVE_CALLBACK_LATENCY_P95_MS is 2500",
        ):
            self.rig.run(self.config)

    def test_changing_paper_budget_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find="PAPER_CALLBACK_LATENCY_P95_MS: int = 100",
            replace="PAPER_CALLBACK_LATENCY_P95_MS: int = 500",
        )
        with self.assertRaisesRegex(
            StrategyApiOrderEventsCheckError,
            r"PAPER_CALLBACK_LATENCY_P95_MS is 500",
        ):
            self.rig.run(self.config)

    def test_removing_live_budget_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find="LIVE_CALLBACK_LATENCY_P95_MS: int = 1000",
            replace="LIVE_CALLBACK_LATENCY_P95_MS_REMOVED: int = 1000",
        )
        with self.assertRaises(StrategyApiOrderEventsCheckError):
            self.rig.run(self.config)


class OnOrderEventCallbackMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_dropping_assert_helper_from_callback_docstring_is_caught(self) -> None:
        # The docstring publicly commits Python dispatchers to call the
        # guard. Dropping the token means concrete drivers might
        # reimplement field-presence and silently drift.
        self.rig.mutate(
            "api.py",
            find="Python dispatchers do so via\n        ``assert_order_event_payload``",
            replace="Python dispatchers handle validation themselves",
        )
        with self.assertRaisesRegex(
            StrategyApiOrderEventsCheckError,
            r"Strategy\.on_order_event docstring is missing required tokens",
        ):
            self.rig.run(self.config)

    def test_dropping_latency_constant_from_docstring_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find=("the Python\n        constants ``LIVE_CALLBACK_LATENCY_P95_MS`` and"),
            replace="the budgets are documented elsewhere — ",
        )
        with self.assertRaises(StrategyApiOrderEventsCheckError):
            self.rig.run(self.config)


class OrderEventContractErrorExportMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_removing_assert_helper_from_package_init_is_caught(self) -> None:
        self.rig.mutate(
            "__init__.py",
            find="    assert_order_event_payload,\n",
            replace="",
        )
        with self.assertRaises(StrategyApiOrderEventsCheckError):
            self.rig.run(self.config)

    def test_removing_contract_error_from_package_init_is_caught(self) -> None:
        self.rig.mutate(
            "__init__.py",
            find="    OrderEventContractError,\n",
            replace="",
        )
        with self.assertRaises(StrategyApiOrderEventsCheckError):
            self.rig.run(self.config)


if __name__ == "__main__":
    unittest.main()
