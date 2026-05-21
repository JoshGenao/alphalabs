"""L7 domain tests for SRS-SDK-009 (Python Strategy API documentation surface).

Where the L3 contract test
(``tests/test_strategy_api_documentation_contract.py``) exercises the
documentation contract via static AST mutations of the SDK source, this
L7 domain test exercises the end-to-end author experience: every
bundled example strategy is constructed, driven through warm-up and a
handful of executable bars against an in-process harness, and verified
to log the expected lines. This proves the SRS-SDK-009 acceptance
criterion that the docs + examples produce a runnable strategy
without the author needing to read platform internals.

Marked ``pytest.mark.domain`` + ``pytest.mark.safety`` so it satisfies
the SAFETY_PATH_RE pair-requirement introduced for the
``strategy[_-]?api[_-]?documentation`` tokens in the prep commit.
"""

from __future__ import annotations

import ast
import importlib
import re
import sys
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "python"))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

pytestmark = [pytest.mark.domain, pytest.mark.safety]

from strategy_api_documentation_check import load_config  # noqa: E402

EXAMPLES_DIR = ROOT / "python" / "atp_strategy" / "examples"


def _fresh_atp_strategy():
    """Return the live ``atp_strategy`` module, re-imported if needed.

    The L3 mutation rig in
    ``tests/test_strategy_api_documentation_contract.py`` clears
    ``sys.modules['atp_strategy*']`` for its tmpdir reloads and
    restores ``sys.path`` afterwards. Re-resolving here on every
    test guarantees this L7 file's ``atp_strategy`` reference and
    the example modules' ``from atp_strategy import …`` references
    share the same class identities, so ``issubclass`` and
    ``except AssetClassViolation`` line up.
    """
    return importlib.import_module("atp_strategy")


def _fresh_harness():
    return importlib.import_module("atp_strategy.examples._harness")


def _example_module(name: str):
    _fresh_atp_strategy()
    return importlib.import_module(f"atp_strategy.examples.{name}")


def _strategy_subclass(module) -> type:
    strategy_base = _fresh_atp_strategy().Strategy
    for obj in vars(module).values():
        if isinstance(obj, type) and issubclass(obj, strategy_base) and obj is not strategy_base:
            return obj
    raise AssertionError(f"no Strategy subclass found in {module.__name__}")


class ExampleStrategyEndToEndTest(unittest.TestCase):
    """Each example must run end-to-end through the harness without error."""

    def test_hello_runs_and_logs_startup_and_three_bars(self) -> None:
        module = _example_module("hello")
        strat_cls = _strategy_subclass(module)
        ctx = _fresh_harness().run(strat_cls(), symbol="AAPL", executable_bars=3)
        # 1 startup line + 3 on_bar lines.
        self.assertGreaterEqual(len(ctx.log_lines), 4)
        self.assertIn("HelloStrategy started", ctx.log_lines[0])
        bar_lines = [line for line in ctx.log_lines if "close=" in line]
        self.assertEqual(len(bar_lines), 3)

    def test_sma_crossover_completes_warmup_and_fires_buy_signal(self) -> None:
        module = _example_module("sma_crossover")
        strat_cls = _strategy_subclass(module)
        ctx = _fresh_harness().run(
            strat_cls(),
            symbol="AAPL",
            history_bars=200,
            executable_bars=12,
        )
        joined = "\n".join(ctx.log_lines)
        self.assertIn("warm-up complete", joined)
        self.assertIn("BUY signal", joined)
        self.assertIn("FILL", joined)
        # Order submitted only after warmup-complete log line.
        warmup_idx = next(i for i, line in enumerate(ctx.log_lines) if "warm-up complete" in line)
        buy_idx = next(i for i, line in enumerate(ctx.log_lines) if "BUY signal" in line)
        self.assertLess(warmup_idx, buy_idx)
        self.assertEqual(len(ctx.orders), 1)

    def test_dual_asset_analytics_demonstrates_asset_class_invariant(self) -> None:
        atp = _fresh_atp_strategy()
        module = _example_module("dual_asset_analytics")
        strat_cls = _strategy_subclass(module)
        ctx = _fresh_harness().run(
            strat_cls(),
            symbol="AAPL",
            asset_class=atp.AssetClass.EQUITY,
            executable_bars=2,
            deliver_fill_for_first_order=False,
        )
        joined = "\n".join(ctx.log_lines)
        self.assertIn("subscribed to AAPL equity + option chain", joined)
        self.assertIn("option order correctly rejected", joined)
        # AC-half-A: dual subscription allowed for analysis.
        sub_classes = {cls for _sym, cls in ctx.subscriptions}
        self.assertEqual(sub_classes, {atp.AssetClass.EQUITY, atp.AssetClass.OPTION})
        # AC-half-B: no actual order should have made it through.
        self.assertEqual(ctx.orders, [])


class ExampleDoesNotReadInternalsTest(unittest.TestCase):
    """AST-level: every example imports only the documented facade."""

    FORBIDDEN_PREFIXES = (
        "atp_strategy.api",
        "atp_strategy.warmup",
        "atp_strategy.indicators",
        "atp_strategy.scheduler",
        "atp_strategy.calendar",
    )

    def test_no_example_imports_from_platform_internals(self) -> None:
        config = load_config()
        block = config["strategy_api_documentation_contract"]
        for name in block["required_example_modules"]:
            module_file = EXAMPLES_DIR / f"{name}.py"
            tree = ast.parse(module_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    src = node.module or ""
                    for prefix in self.FORBIDDEN_PREFIXES:
                        self.assertFalse(
                            src == prefix or src.startswith(prefix + "."),
                            f"example {module_file} reads internal module {src!r}",
                        )


class DocstringCodeFenceRoundtripTest(unittest.TestCase):
    """Tier-1 docstrings with `>>>` blocks must compile under exec()."""

    TIER_ONE_DOCTEST_SYMBOLS = ("Bar", "SMA", "OrderRequest", "WarmupState")

    def test_doctest_blocks_in_tier_one_docstrings_compile(self) -> None:
        atp = _fresh_atp_strategy()
        for name in self.TIER_ONE_DOCTEST_SYMBOLS:
            obj = getattr(atp, name)
            doc = obj.__doc__ or ""
            block_lines: list[str] = []
            for line in doc.splitlines():
                stripped = line.strip()
                if stripped.startswith(">>>") or stripped.startswith("..."):
                    block_lines.append(stripped[4:] if len(stripped) > 3 else "")
            self.assertTrue(block_lines, f"{name}: no `>>>` doctest lines found in docstring")
            source = "\n".join(block_lines)
            # The source must parse as valid Python. A broken doctest
            # would still parse — that's fine; the parse check catches
            # the typical mutation (typo / dropped comma / unbalanced
            # paren) that would otherwise mislead readers.
            try:
                compile(source, f"<{name}-doctest>", "exec")
            except SyntaxError as exc:
                self.fail(f"{name} doctest block does not compile: {exc}")


class NoExecutionModeBranchInExamplesTest(unittest.TestCase):
    """SRS-SDK-001 AC-14: same source runs in live and paper modes."""

    def test_no_example_branches_on_execution_mode(self) -> None:
        forbidden = re.compile(r"\b(live|paper|backtest)\b", re.IGNORECASE)
        config = load_config()
        block = config["strategy_api_documentation_contract"]
        for name in block["required_example_modules"]:
            module_file = EXAMPLES_DIR / f"{name}.py"
            tree = ast.parse(module_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    # Bare execution-mode tokens (the literal string
                    # "live" / "paper" / "backtest" used as a config
                    # value) are forbidden. Phrases containing the
                    # word (e.g. "paper trading") inside docstring
                    # prose are not — `\b...\b` would match those
                    # too, so we require the entire string literal
                    # to be exactly the forbidden token.
                    if node.value.lower() in {"live", "paper", "backtest"}:
                        self.fail(
                            f"example {module_file} contains "
                            f"execution-mode literal {node.value!r}; "
                            "SRS-SDK-001 AC-14 forbids mode branches"
                        )
                if isinstance(node, ast.If):
                    # Match `if mode == "live"` / `if mode in ("live",
                    # "paper")` patterns specifically — comparisons
                    # against bare execution-mode tokens.
                    self.assertFalse(
                        forbidden.search(ast.unparse(node.test)),
                        f"example {module_file} branches on execution mode: "
                        f"{ast.unparse(node.test)!r}",
                    )


class ReadmePointersResolveTest(unittest.TestCase):
    """Every `python -m atp_strategy.examples.<x>` reference is real."""

    def test_readme_module_pointers_resolve(self) -> None:
        config = load_config()
        block = config["strategy_api_documentation_contract"]
        readme_path = ROOT / block["readme_path"]
        text = readme_path.read_text(encoding="utf-8")
        pattern = re.compile(r"python\s+-m\s+atp_strategy\.examples\.([A-Za-z0-9_]+)")
        referenced = set(pattern.findall(text))
        self.assertGreater(len(referenced), 0)
        declared = set(block["required_example_modules"])
        self.assertTrue(
            referenced.issubset(declared),
            f"README references modules not in required_example_modules: "
            f"{sorted(referenced - declared)}",
        )
        for name in referenced:
            module = importlib.import_module(f"atp_strategy.examples.{name}")
            self.assertTrue(
                hasattr(module, "main"),
                f"example {name} has no main() — `python -m ...` would no-op",
            )


class StrategyContextProtocolSurfaceCoveredByExamplesTest(unittest.TestCase):
    """Author-visible runtime calls cover the contract's required methods.

    Coverage is driven from
    ``strategy_api_documentation_contract.required_strategy_context_methods``
    so the README cannot promise a method that no example demonstrates.
    ``indicator`` and ``consolidate`` are convenience constructors —
    the README explicitly notes they are equivalent to importing the
    class directly — so they are exempted from this runtime-call
    requirement and verified by their tier-1 docstrings instead.
    """

    EXEMPT_FROM_RUNTIME_CALL_COVERAGE = frozenset({"indicator", "consolidate"})

    def test_required_context_methods_appear_across_examples(self) -> None:
        config = load_config()
        block = config["strategy_api_documentation_contract"]
        required_appearance = set(block["required_strategy_context_methods"]) - (
            self.EXEMPT_FROM_RUNTIME_CALL_COVERAGE
        )
        seen: set[str] = set()
        for name in block["required_example_modules"]:
            module_file = EXAMPLES_DIR / f"{name}.py"
            source = module_file.read_text(encoding="utf-8")
            for method in required_appearance:
                if f"ctx.{method}(" in source or f"context.{method}(" in source:
                    seen.add(method)
        missing = required_appearance - seen
        self.assertFalse(
            missing,
            f"required StrategyContext methods never demonstrated in any "
            f"example: {sorted(missing)} — add a call to one of the "
            "bundled example strategies",
        )


if __name__ == "__main__":
    unittest.main()
