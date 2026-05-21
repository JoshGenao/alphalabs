"""Contract tests for SRS-SDK-003 (SyRS SYS-5 / SYS-64;
StRS SN-1.07 / BG-1 / BG-5).

Shells out to ``tools/strategy_api_subscriptions_check.py`` for the
positive-evidence path, then mutates a tmpdir copy of
``python/atp_strategy/`` to verify each invariant in the subscriptions
contract actually catches a regression: dropped AssetClass enum members,
dropped StrategyConfig / OrderRequest fields, dropped subscribe
parameter, swapped/silenced assert_asset_class body, missing
AssetClassViolation export, and Protocol-docstring drift.
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

from strategy_api_subscriptions_check import (  # noqa: E402
    StrategyApiSubscriptionsCheckError,
    assert_strategy_api_subscriptions_static,
    load_config,
)


class _MutationRig:
    """Copy ``python/atp_strategy/`` into a tmpdir and run the subscriptions check."""

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
        return assert_strategy_api_subscriptions_static(config, root=self.root)


class StrategyApiSubscriptionsScriptTest(unittest.TestCase):
    """Positive evidence: the CLI emits the seven required evidence needles."""

    def test_script_passes_and_emits_evidence_needles(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/strategy_api_subscriptions_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-SDK-003 PASS", result.stdout)
        for needle in (
            "AssetClass enum members are exactly ['EQUITY', 'OPTION']",
            "SYS-5 single-tradable-class enumeration locked",
            "StrategyConfig.tradable_asset_class: AssetClass field is required (no default)",
            "OrderRequest.asset_class: AssetClass = AssetClass.EQUITY field locked",
            "StrategyContext.subscribe(self, symbol, asset_class) accepts asset_class",
            "docstring affirms both-class analysis subscriptions per SRS-SDK-003 AC half-A",
            "StrategyContext.order docstring names ['AssetClassViolation', 'assert_asset_class']",
            "assert_asset_class(config, request) helper shipped and re-exported",
            "raises AssetClassViolation on mismatch",
            "SyRS SYS-5 single-tradable-class invariant enforced behaviourally",
            "AssetClassViolation subclasses StrategyAPIError per SyRS SYS-64",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class AssetClassEnumMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_dropping_option_member_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find='    EQUITY = "EQUITY"\n    OPTION = "OPTION"',
            replace='    EQUITY = "EQUITY"',
        )
        with self.assertRaisesRegex(StrategyApiSubscriptionsCheckError, r"AssetClass enum members"):
            self.rig.run(self.config)

    def test_dropping_equity_member_is_caught(self) -> None:
        # Dropping EQUITY breaks the module-load default on
        # OrderRequest.asset_class = AssetClass.EQUITY, so the SDK fails
        # to import. Either surface is a clear regression signal — assert
        # that the check raises, without pinning to a specific message.
        self.rig.mutate(
            "api.py",
            find='    EQUITY = "EQUITY"\n    OPTION = "OPTION"',
            replace='    OPTION = "OPTION"',
        )
        with self.assertRaises(StrategyApiSubscriptionsCheckError):
            self.rig.run(self.config)


class StrategyConfigFieldMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_dropping_tradable_asset_class_field_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find="    strategy_id: str\n    tradable_asset_class: AssetClass\n",
            replace="    strategy_id: str\n",
        )
        with self.assertRaisesRegex(StrategyApiSubscriptionsCheckError, r"tradable_asset_class"):
            self.rig.run(self.config)

    def test_giving_tradable_asset_class_a_default_is_caught(self) -> None:
        # Operators must supply the tradable class explicitly per container;
        # silently defaulting to one class would mask a misconfiguration.
        self.rig.mutate(
            "api.py",
            find="    tradable_asset_class: AssetClass\n",
            replace="    tradable_asset_class: AssetClass = AssetClass.EQUITY\n",
        )
        with self.assertRaisesRegex(StrategyApiSubscriptionsCheckError, r"has a default value"):
            self.rig.run(self.config)


class OrderRequestFieldMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_dropping_order_request_asset_class_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find="    asset_class: AssetClass = AssetClass.EQUITY\n",
            replace="",
        )
        with self.assertRaises(StrategyApiSubscriptionsCheckError):
            self.rig.run(self.config)

    def test_changing_order_request_default_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find="    asset_class: AssetClass = AssetClass.EQUITY\n",
            replace="    asset_class: AssetClass = AssetClass.OPTION\n",
        )
        with self.assertRaisesRegex(
            StrategyApiSubscriptionsCheckError, r"OrderRequest\.asset_class default"
        ):
            self.rig.run(self.config)


class SubscribeProtocolMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_removing_asset_class_kwarg_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find=(
                "    def subscribe(self, symbol: str, asset_class: AssetClass "
                "= AssetClass.EQUITY) -> None:"
            ),
            replace="    def subscribe(self, symbol: str) -> None:",
        )
        with self.assertRaisesRegex(
            StrategyApiSubscriptionsCheckError, r"StrategyContext\.subscribe signature"
        ):
            self.rig.run(self.config)

    def test_changing_default_to_literal_string_is_caught(self) -> None:
        # The default must be the AssetClass enum so type-checking catches
        # caller misuse at the strategy-author surface — a bare string would
        # silently coerce inside the StrEnum and bypass static analysis.
        self.rig.mutate(
            "api.py",
            find=(
                "    def subscribe(self, symbol: str, asset_class: AssetClass "
                "= AssetClass.EQUITY) -> None:"
            ),
            replace=(
                '    def subscribe(self, symbol: str, asset_class: AssetClass = "EQUITY") -> None:'
            ),
        )
        with self.assertRaisesRegex(
            StrategyApiSubscriptionsCheckError,
            r"StrategyContext\.subscribe\.asset_class default",
        ):
            self.rig.run(self.config)

    def test_dropping_both_class_analysis_docstring_is_caught(self) -> None:
        # The SDK-003 check accepts either "both equities and options"
        # or "both asset classes" in the StrategyContext.subscribe
        # docstring as the AC-half-A affirmation. Drop both wordings
        # by replacing the relevant sentence with one that names
        # neither.
        self.rig.mutate(
            "api.py",
            find=(
                "A strategy may subscribe to both asset classes\n"
                "        (``EQUITY`` and ``OPTION``) for analysis regardless of its\n"
                "        configured ``tradable_asset_class`` (``SRS-SDK-003`` AC half\n"
                "        A); only order submission is gated on the configured tradable\n"
                "        class."
            ),
            replace="See ``SRS-SDK-003``.",
        )
        with self.assertRaisesRegex(
            StrategyApiSubscriptionsCheckError, r"docstring no longer affirms"
        ):
            self.rig.run(self.config)


class OrderProtocolDocstringMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_dropping_assetclass_violation_from_order_docstring_is_caught(self) -> None:
        # The SDK-003 check requires the StrategyContext.order
        # docstring to publicly promise ``AssetClassViolation`` AND
        # ``assert_asset_class`` so authors and Rust dispatcher
        # implementers see the guard. Drop the AssetClassViolation
        # token by rewriting the sentence around it.
        self.rig.mutate(
            "api.py",
            find=(
                "Raises\n"
                "        ``AssetClassViolation`` if ``request.asset_class`` does not\n"
                "        match the strategy's configured tradable class\n"
                "        (``SRS-SDK-003``) — concrete implementations enforce this by\n"
                "        calling :func:`assert_asset_class` before routing."
            ),
            replace="Routes the order to the runtime execution path.",
        )
        with self.assertRaisesRegex(
            StrategyApiSubscriptionsCheckError,
            r"StrategyContext\.order docstring is missing required tokens",
        ):
            self.rig.run(self.config)


class AssertAssetClassHelperMutationTest(unittest.TestCase):
    """Behavioural mutations: catch silent-skip and inverted-comparison bugs."""

    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_removing_helper_definition_is_caught(self) -> None:
        # Renaming the def out from under the package __init__'s explicit
        # `from .api import assert_asset_class` will surface as an
        # ImportError during module load; renaming inside api.py alone
        # would let the check assert directly on `hasattr(api,
        # 'assert_asset_class')`. Either surface counts as catching the
        # regression — pin only on the check's exception type.
        self.rig.mutate(
            "api.py",
            find='def assert_asset_class(config: "StrategyConfig", request: "OrderRequest") -> None:',
            replace='def _removed_assert_asset_class(config: "StrategyConfig", request: "OrderRequest") -> None:',
        )
        with self.assertRaises(StrategyApiSubscriptionsCheckError):
            self.rig.run(self.config)

    def test_silencing_helper_body_is_caught(self) -> None:
        # Replace the body's `if`-block with `return None` so the helper
        # never raises. The behavioural exercise in the check must catch this.
        self.rig.mutate(
            "api.py",
            find=(
                "    if request.asset_class != config.tradable_asset_class:\n"
                "        raise AssetClassViolation(\n"
                '            f"strategy {config.strategy_id} configured for "\n'
                '            f"{config.tradable_asset_class.value} cannot trade "\n'
                '            f"{request.asset_class.value}"\n'
                "        )\n"
            ),
            replace="    return None\n",
        )
        with self.assertRaisesRegex(
            StrategyApiSubscriptionsCheckError,
            r"assert_asset_class did not raise on mismatched",
        ):
            self.rig.run(self.config)

    def test_inverting_comparison_operator_is_caught(self) -> None:
        # Swap `!=` for `==` — the helper now raises on matched classes and
        # is silent on mismatch. Behavioural exercise catches both halves.
        self.rig.mutate(
            "api.py",
            find="if request.asset_class != config.tradable_asset_class:",
            replace="if request.asset_class == config.tradable_asset_class:",
        )
        with self.assertRaises(StrategyApiSubscriptionsCheckError):
            self.rig.run(self.config)

    def test_dropping_strategy_id_from_violation_message_is_caught(self) -> None:
        # The structured-error contract (SyRS SYS-64) requires the offending
        # strategy + class be named so user code can route on it.
        self.rig.mutate(
            "api.py",
            find='            f"strategy {config.strategy_id} configured for "',
            replace='            f"configured for "',
        )
        with self.assertRaisesRegex(
            StrategyApiSubscriptionsCheckError, r"violation message does not name"
        ):
            self.rig.run(self.config)


class AssetClassViolationExportMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_removing_assert_asset_class_from_package_init_is_caught(self) -> None:
        self.rig.mutate(
            "__init__.py",
            find="    assert_asset_class,\n",
            replace="",
        )
        with self.assertRaises(StrategyApiSubscriptionsCheckError):
            self.rig.run(self.config)


if __name__ == "__main__":
    unittest.main()
