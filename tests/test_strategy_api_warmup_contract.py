"""Contract tests for SRS-SDK-005 (SyRS SYS-8 / NFR-R3; StRS SN-1.23 / SC-18 /
BG-1 / BG-2 / BG-7).

Shells out to ``tools/strategy_api_warmup_check.py`` for the positive-evidence
path, then mutates a tmpdir copy of ``python/atp_strategy/`` to verify each
invariant in the warm-up contract actually catches a regression: dropped
``WarmupState`` members, silenced ``assert_warmup_complete`` body, missing
``WarmupController.run`` body, dropped ``on_warmup_complete`` callback,
forbidden double-firing of the lifecycle callback, and missing exports.
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

from strategy_api_warmup_check import (  # noqa: E402
    StrategyApiWarmupCheckError,
    assert_strategy_api_warmup_static,
    load_config,
)


class _MutationRig:
    """Copy ``python/atp_strategy/`` into a tmpdir and run the warmup check."""

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
        return assert_strategy_api_warmup_static(config, root=self.root)


class StrategyApiWarmupScriptTest(unittest.TestCase):
    """Positive evidence: the CLI emits the required evidence needles."""

    def test_script_passes_and_emits_evidence_needles(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/strategy_api_warmup_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-SDK-005 PASS", result.stdout)
        for needle in (
            "WarmupState = ['COMPLETE', 'IN_PROGRESS', 'PENDING']",
            "three-state lifecycle locked",
            "WarmupController methods ['run']",
            "Strategy.on_warmup_complete(self, context) signature locked",
            "WarmupNotComplete subclasses StrategyAPIError per SyRS SYS-64",
            "200 historical bars before the executable gate flips",
            "SMA(200).is_ready == True at the boundary",
            "on_warmup_complete fires exactly once",
            "fail-closed on a short historical replay",
            "SRS-SDK-005 AC behaviourally locked",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class WarmupStateMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_dropping_pending_member_is_caught(self) -> None:
        self.rig.mutate(
            "warmup.py",
            find='    PENDING = "PENDING"\n',
            replace="",
        )
        with self.assertRaisesRegex(StrategyApiWarmupCheckError, r"WarmupState members"):
            self.rig.run(self.config)

    def test_dropping_in_progress_member_is_caught(self) -> None:
        self.rig.mutate(
            "warmup.py",
            find='    IN_PROGRESS = "IN_PROGRESS"\n',
            replace="",
        )
        with self.assertRaisesRegex(StrategyApiWarmupCheckError, r"WarmupState members"):
            self.rig.run(self.config)

    def test_dropping_complete_member_is_caught(self) -> None:
        self.rig.mutate(
            "warmup.py",
            find='    COMPLETE = "COMPLETE"\n',
            replace="",
        )
        with self.assertRaises(StrategyApiWarmupCheckError):
            self.rig.run(self.config)


class WarmupControllerShapeMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_dropping_bars_replayed_property_is_caught(self) -> None:
        self.rig.mutate(
            "warmup.py",
            find='    @property\n    def bars_replayed(self) -> int:\n        """Cumulative bars delivered to ``on_bar`` during warm-up so far."""\n        return self._bars_replayed\n',
            replace="",
        )
        with self.assertRaisesRegex(
            StrategyApiWarmupCheckError,
            r"WarmupController\.bars_replayed must be a property",
        ):
            self.rig.run(self.config)

    def test_demoting_state_to_method_is_caught(self) -> None:
        # Swap the @property decorator off `state` so it's a plain method.
        self.rig.mutate(
            "warmup.py",
            find="    @property\n    def state(self) -> WarmupState:\n",
            replace="    def state(self) -> WarmupState:\n",
        )
        with self.assertRaisesRegex(
            StrategyApiWarmupCheckError,
            r"WarmupController\.state must be a property",
        ):
            self.rig.run(self.config)


class WarmupBehaviouralMutationTest(unittest.TestCase):
    """Behavioural mutations: lifecycle ordering, gate, replay count."""

    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_silencing_assert_warmup_complete_is_caught(self) -> None:
        # Replace the guard body so it never raises — the AC ordering
        # gate goes silent and the PENDING-state check trips.
        self.rig.mutate(
            "warmup.py",
            find=(
                "    if state is None or state is not WarmupState.COMPLETE:\n"
                "        observed = state.value if isinstance(state, WarmupState) else state\n"
                "        raise WarmupNotComplete("
            ),
            replace=(
                "    if False:\n"
                "        observed = state.value if isinstance(state, WarmupState) else state\n"
                "        raise WarmupNotComplete("
            ),
        )
        with self.assertRaisesRegex(
            StrategyApiWarmupCheckError,
            r"assert_warmup_complete did not raise on PENDING state",
        ):
            self.rig.run(self.config)

    def test_skipping_state_transition_to_complete_is_caught(self) -> None:
        # If the controller never transitions to COMPLETE, the
        # behavioural check fires the lifecycle in IN_PROGRESS.
        self.rig.mutate(
            "warmup.py",
            find="        self._state = WarmupState.COMPLETE\n        self._strategy.on_warmup_complete(self._context)\n",
            replace="        self._strategy.on_warmup_complete(self._context)\n",
        )
        with self.assertRaises(StrategyApiWarmupCheckError):
            self.rig.run(self.config)

    def test_short_circuiting_history_replay_is_caught(self) -> None:
        # If the replay loop is skipped, on_bar receives 0 bars and SMA
        # is_ready stays False — multiple invariants trip.
        self.rig.mutate(
            "warmup.py",
            find=(
                "        for symbol, asset_class in self._subscriptions:\n"
                "            bars = list(\n"
            ),
            replace=(
                "        for symbol, asset_class in []:\n"
                "            bars = list(\n"
            ),
        )
        with self.assertRaises(StrategyApiWarmupCheckError):
            self.rig.run(self.config)

    def test_dropping_on_warmup_complete_invocation_is_caught(self) -> None:
        # If the callback never fires, warmup_complete_calls == 0 trips
        # the behavioural exercise. The mutation removes the only call
        # site so the lifecycle silently misses its boundary signal.
        self.rig.mutate(
            "warmup.py",
            find="        self._strategy.on_warmup_complete(self._context)\n",
            replace="",
        )
        with self.assertRaises(StrategyApiWarmupCheckError):
            self.rig.run(self.config)

    def test_dropping_callback_definition_on_strategy_is_caught(self) -> None:
        # If Strategy.on_warmup_complete itself is renamed, the
        # callback-shape check + the behavioural exercise both trip.
        self.rig.mutate(
            "api.py",
            find="    def on_warmup_complete(self, context: StrategyContext) -> None:\n",
            replace="    def _renamed_on_warmup_complete(self, context: StrategyContext) -> None:\n",
        )
        with self.assertRaises(StrategyApiWarmupCheckError):
            self.rig.run(self.config)

    def test_short_history_shortfall_guard_is_required(self) -> None:
        # Remove the shortfall length check. The behavioural exercise's
        # short-history sub-case must catch the regression — opening the
        # executable gate on N-1 bars violates the SRS-SDK-005 AC.
        self.rig.mutate(
            "warmup.py",
            find="            if len(bars) < self._warmup_bars:\n",
            replace="            if False:\n",
        )
        with self.assertRaisesRegex(
            StrategyApiWarmupCheckError,
            r"WarmupController\.run\(\) did not raise on a short historical replay",
        ):
            self.rig.run(self.config)

    def test_negative_warmup_bars_must_raise(self) -> None:
        # If construction silently coerces a negative warmup_bars to a
        # non-negative value, the behavioural check trips on its
        # construction-guard sub-case.
        self.rig.mutate(
            "warmup.py",
            find=(
                "        if warmup_bars < 0:\n"
                '            raise ValueError(f"warmup_bars must be a non-negative int (got {warmup_bars})")\n'
            ),
            replace="        warmup_bars = max(warmup_bars, 0)\n",
        )
        with self.assertRaisesRegex(
            StrategyApiWarmupCheckError,
            r"WarmupController construction with warmup_bars=-1 did not raise",
        ):
            self.rig.run(self.config)


class WarmupDocstringMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_stripping_required_token_from_docstring_is_caught(self) -> None:
        # Remove the SC-18 reference from on_warmup_complete's docstring.
        # The contract check enforces the full required-tokens list.
        self.rig.mutate(
            "api.py",
            find="StRS ``SC-18``",
            replace="StRS ``SC-NONE``",
        )
        with self.assertRaisesRegex(
            StrategyApiWarmupCheckError,
            r"Strategy\.on_warmup_complete docstring is missing required tokens",
        ):
            self.rig.run(self.config)


class WarmupExportsMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_dropping_warmup_controller_export_is_caught(self) -> None:
        # Remove the WarmupController re-export from __init__.py.
        self.rig.mutate(
            "__init__.py",
            find="from .warmup import WarmupController, WarmupState, assert_warmup_complete\n",
            replace="from .warmup import WarmupState, assert_warmup_complete\n",
        )
        with self.assertRaises(StrategyApiWarmupCheckError):
            self.rig.run(self.config)


if __name__ == "__main__":
    unittest.main()
