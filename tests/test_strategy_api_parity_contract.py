"""Contract tests for SRS-SDK-001 (SyRS AC-14 / SYS-82..SYS-87; StRS
SN-1.01 / SN-1.29 / SN-3.01).

Shells out to ``tools/strategy_api_parity_check.py`` for the
positive-evidence path, then mutates a tmpdir copy of
``python/atp_strategy/`` to verify each invariant in the parity
contract actually catches a regression: mode-discriminator leakage,
vendor-SDK imports, dropped Protocol methods, leaked execution-mode
parameter names, and ``StrategyConfig`` mode fields.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from strategy_api_parity_check import (  # noqa: E402
    StrategyApiParityCheckError,
    assert_strategy_api_parity_static,
    load_config,
)


class _MutationRig:
    """Copy ``python/atp_strategy/`` into a tmpdir and run the parity check."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # Mirror the repo subtree the check inspects: python/atp_strategy
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
            raise AssertionError(
                f"mutation rig: substring not found in {relpath}: {find!r}"
            )
        target.write_text(text.replace(find, replace, 1), encoding="utf-8")

    def write_extra(self, relpath: str, content: str) -> None:
        target = self.root / "python" / "atp_strategy" / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content), encoding="utf-8")

    def run(self, config: dict) -> list[str]:
        return assert_strategy_api_parity_static(config, root=self.root)


class StrategyApiParityScriptTest(unittest.TestCase):
    def test_script_passes_and_emits_evidence_needles(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/strategy_api_parity_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-SDK-001 PASS", result.stdout)
        for needle in (
            "no attribute/name references to",
            "execution_mode",
            "ExecutionMode",
            "does not import any of the forbidden vendor SDKs",
            "ibapi",
            "StrategyContext declares all 8 required methods",
            "subscribe, order, cancel, log, get_state, set_state, indicator, consolidate",
            "all 4 required attributes",
            "config, schedule, calendar, history",
            "every StrategyContext method (8 total) rejects execution-mode parameter names",
            "StrategyConfig has no field named",
            "orchestrator-selected execution path is invisible to user code",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class ModeBranchLeakageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_attribute_access_to_mode_is_caught(self) -> None:
        self.rig.write_extra(
            "leak.py",
            """
            from .api import StrategyContext

            def _leak(ctx: StrategyContext) -> bool:
                return ctx.execution_mode == "live"
            """,
        )
        with self.assertRaises(StrategyApiParityCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("execution_mode", str(cm.exception))

    def test_attribute_access_to_is_paper_is_caught(self) -> None:
        self.rig.write_extra(
            "leak.py",
            """
            def _branch(obj) -> None:
                if obj.is_paper:
                    return
            """,
        )
        with self.assertRaises(StrategyApiParityCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("is_paper", str(cm.exception))

    def test_execution_mode_symbol_reference_is_caught(self) -> None:
        self.rig.write_extra(
            "leak.py",
            """
            ExecutionMode = "stub"

            def _branch():
                return ExecutionMode
            """,
        )
        with self.assertRaises(StrategyApiParityCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("ExecutionMode", str(cm.exception))

    def test_strategy_mode_symbol_reference_is_caught(self) -> None:
        self.rig.write_extra(
            "leak.py",
            """
            StrategyMode = object()

            def _use():
                return StrategyMode
            """,
        )
        with self.assertRaises(StrategyApiParityCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("StrategyMode", str(cm.exception))


class VendorImportLeakageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_import_ibapi_is_caught(self) -> None:
        self.rig.write_extra("leak.py", "import ibapi\n")
        with self.assertRaises(StrategyApiParityCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("ibapi", str(cm.exception))

    def test_from_ib_insync_import_is_caught(self) -> None:
        self.rig.write_extra("leak.py", "from ib_insync import IB\n")
        with self.assertRaises(StrategyApiParityCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("ib_insync", str(cm.exception))

    def test_databento_import_is_caught(self) -> None:
        self.rig.write_extra("leak.py", "import databento\n")
        with self.assertRaises(StrategyApiParityCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("databento", str(cm.exception))


class ProtocolSurfaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_dropping_cancel_method_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find="    def cancel(self, handle: OrderHandle) -> None:",
            replace="    def _removed_cancel(self, handle: OrderHandle) -> None:",
        )
        with self.assertRaises(StrategyApiParityCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("cancel", str(cm.exception))

    def test_dropping_indicator_method_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find='    def indicator(self, name: str, **params: object) -> Indicator:',
            replace='    def _removed_indicator(self, name: str, **params: object) -> Indicator:',
        )
        with self.assertRaises(StrategyApiParityCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("indicator", str(cm.exception))

    def test_dropping_history_attribute_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find="    history: HistoricalData",
            replace="    history_removed: HistoricalData",
        )
        with self.assertRaises(StrategyApiParityCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("history", str(cm.exception))


class SignatureLeakageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_order_with_execution_mode_param_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find="    def order(self, request: OrderRequest) -> OrderHandle:",
            replace="    def order(self, request: OrderRequest, execution_mode: str = \"live\") -> OrderHandle:",
        )
        with self.assertRaises(StrategyApiParityCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("execution_mode", str(cm.exception))

    def test_subscribe_with_is_paper_param_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find="    def subscribe(self, symbol: str, asset_class: AssetClass = AssetClass.EQUITY) -> None:",
            replace="    def subscribe(self, symbol: str, asset_class: AssetClass = AssetClass.EQUITY, is_paper: bool = False) -> None:",
        )
        with self.assertRaises(StrategyApiParityCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("is_paper", str(cm.exception))


class StrategyConfigFieldLeakageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_execution_mode_field_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find="    timezone: str = \"America/New_York\"",
            replace="    timezone: str = \"America/New_York\"\n    execution_mode: str = \"live\"",
        )
        with self.assertRaises(StrategyApiParityCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("execution_mode", str(cm.exception))

    def test_is_paper_field_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find="    timezone: str = \"America/New_York\"",
            replace="    timezone: str = \"America/New_York\"\n    is_paper: bool = False",
        )
        with self.assertRaises(StrategyApiParityCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("is_paper", str(cm.exception))


class AssertStaticReturnsEvidenceTest(unittest.TestCase):
    def test_static_assert_returns_one_evidence_line_per_check(self) -> None:
        evidence = assert_strategy_api_parity_static(load_config())
        self.assertEqual(len(evidence), 5)
        for line in evidence:
            self.assertIsInstance(line, str)
            self.assertGreater(len(line.strip()), 0)


if __name__ == "__main__":
    unittest.main()
