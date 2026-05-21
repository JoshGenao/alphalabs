"""Contract tests for SRS-SDK-009 (SyRS NFR-U2; StRS SN-1.01, C-1).

Shells out to ``tools/strategy_api_documentation_check.py`` for the
positive-evidence path, then mutates a tmpdir copy of
``python/atp_strategy/`` (which includes the README and the
``examples/`` subpackage) to verify each invariant in the documentation
contract actually catches a regression: stripped docstrings, missing
example fences, removed README sections, neutered example files, deep-
path imports into platform internals, anti-drift violations on
``__all__``, and execution-mode string literals.
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

from strategy_api_documentation_check import (  # noqa: E402
    StrategyApiDocumentationCheckError,
    assert_strategy_api_documentation_static,
    load_config,
)


class _MutationRig:
    """Copy ``python/atp_strategy/`` into a tmpdir and run the documentation check.

    The check's ``_load_sdk_module`` mutates ``sys.path`` and clears
    ``sys.modules`` entries for ``atp_strategy*`` to re-import from the
    mutated tmpdir. ``close()`` restores the canonical ``python/``
    entry on ``sys.path`` (and re-imports ``atp_strategy`` from the
    main repo) so subsequent tests in the same pytest session do not
    pick up a deleted tmpdir.
    """

    def __init__(self) -> None:
        self._original_sys_path = list(sys.path)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "python").mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            ROOT / "python" / "atp_strategy",
            self.root / "python" / "atp_strategy",
        )

    def close(self) -> None:
        self._tmp.cleanup()
        # Drop any sys.path entries pointing at the now-deleted tmpdir.
        sys.path[:] = [p for p in sys.path if not p.startswith(str(self.root))]
        # Re-prepend the canonical python/ root so subsequent in-session
        # tests resolve atp_strategy from the real repo.
        canonical = str(ROOT / "python")
        if canonical in sys.path:
            sys.path.remove(canonical)
        sys.path.insert(0, canonical)
        # Evict any atp_strategy modules whose source path points at
        # the dead tmpdir so a future import reloads from the real repo.
        for name in list(sys.modules):
            if name == "atp_strategy" or name.startswith("atp_strategy."):
                module = sys.modules.get(name)
                source = getattr(module, "__file__", None)
                if source and source.startswith(str(self.root)):
                    sys.modules.pop(name, None)

    def write(self, relpath: str, content: str) -> None:
        target = self.root / "python" / "atp_strategy" / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def mutate(self, relpath: str, *, find: str, replace: str) -> None:
        target = self.root / "python" / "atp_strategy" / relpath
        text = target.read_text(encoding="utf-8")
        if find not in text:
            raise AssertionError(f"mutation rig: substring not found in {relpath}: {find!r}")
        target.write_text(text.replace(find, replace, 1), encoding="utf-8")

    def delete(self, relpath: str) -> None:
        (self.root / "python" / "atp_strategy" / relpath).unlink()

    def run(self, config: dict) -> list[str]:
        return assert_strategy_api_documentation_static(config, root=self.root)


class StrategyApiDocumentationScriptTest(unittest.TestCase):
    """Positive evidence: the CLI emits the required evidence needles."""

    def test_script_passes_and_emits_evidence_needles(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/strategy_api_documentation_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            env={**__import__("os").environ, "NUMBA_DISABLE_JIT": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-SDK-009 PASS", result.stdout)
        for needle in (
            "tier partition of atp_strategy.__all__",
            "every tier-1 symbol",
            "tier-2 (6 symbols",
            "every Strategy hook",
            "every StrategyContext method",
            "all 3 example modules",
            "no deep paths into platform internals",
            "every `from atp_strategy import ...` reference",
            "no example module contains string literals",
            "every documentation_module_paths entry",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class TierAndExampleMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.addCleanup(self.rig.close)
        self.config = load_config()

    def test_dropping_strategy_on_bar_docstring_is_caught(self) -> None:
        # Rewrite api.py to drop the on_bar docstring entirely. The
        # contract's 100-char hook minimum must catch this.
        api_path = self.rig.root / "python" / "atp_strategy" / "api.py"
        text = api_path.read_text(encoding="utf-8")
        new_text = _replace_method_docstring(
            text, "    def on_bar(self, context: StrategyContext, bar: Bar) -> None:"
        )
        api_path.write_text(new_text, encoding="utf-8")
        with self.assertRaises(StrategyApiDocumentationCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("Strategy.on_bar", str(cm.exception))

    def test_demoting_strategycontext_order_is_caught(self) -> None:
        api_path = self.rig.root / "python" / "atp_strategy" / "api.py"
        text = api_path.read_text(encoding="utf-8")
        new_text = _replace_method_docstring(
            text, "    def order(self, request: OrderRequest) -> OrderHandle:"
        )
        api_path.write_text(new_text, encoding="utf-8")
        with self.assertRaises(StrategyApiDocumentationCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("StrategyContext.order", str(cm.exception))

    def test_stripping_sma_docstring_below_full_tier_is_caught(self) -> None:
        # Rewrite indicators.py to truncate the SMA class docstring so
        # the tier-1 length / example-fence check trips.
        ind_path = self.rig.root / "python" / "atp_strategy" / "indicators.py"
        text = ind_path.read_text(encoding="utf-8")
        new_text = _replace_class_docstring(text, "class SMA(_IndicatorBase):")
        ind_path.write_text(new_text, encoding="utf-8")
        with self.assertRaises(StrategyApiDocumentationCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("SMA", str(cm.exception))


def _replace_method_docstring(source: str, signature_line: str) -> str:
    """Replace the docstring immediately following ``signature_line`` with a 4-char stub."""
    return _truncate_block_docstring(source, signature_line, indent="        ")


def _replace_class_docstring(source: str, class_header_line: str) -> str:
    """Replace the docstring immediately following ``class_header_line`` with a 4-char stub."""
    return _truncate_block_docstring(source, class_header_line, indent="    ")


def _truncate_block_docstring(source: str, header_line: str, *, indent: str) -> str:
    del indent  # source[:triple_open] already includes the indent
    idx = source.find(header_line + "\n")
    if idx < 0:
        raise AssertionError(f"header line not found: {header_line!r}")
    body_start = idx + len(header_line) + 1
    triple_open = source.find('"""', body_start)
    if triple_open < 0:
        raise AssertionError(f"no opening triple-quote after header: {header_line!r}")
    triple_close = source.find('"""', triple_open + 3)
    if triple_close < 0:
        raise AssertionError(f"no closing triple-quote after header: {header_line!r}")
    return source[:triple_open] + '"""x."""' + source[triple_close + 3 :]


class ReadmeMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.addCleanup(self.rig.close)
        self.config = load_config()

    def test_removing_required_readme_section_is_caught(self) -> None:
        self.rig.mutate(
            "README.md",
            find="## Errors and assertions",
            replace="## Errors and other surfaces",
        )
        with self.assertRaises(StrategyApiDocumentationCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("Errors and assertions", str(cm.exception))

    def test_removing_example_pointer_from_readme_is_caught(self) -> None:
        # Replace every reference to a real example module with a bogus
        # one that is not in required_example_modules.
        for declared in self.config["strategy_api_documentation_contract"][
            "required_example_modules"
        ]:
            self.rig.mutate(
                "README.md",
                find=f"python -m atp_strategy.examples.{declared}",
                replace="python -m atp_strategy.examples.bogus_module",
            )
        with self.assertRaises(StrategyApiDocumentationCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("bogus_module", str(cm.exception))


class ExampleMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.addCleanup(self.rig.close)
        self.config = load_config()

    def test_neutered_example_with_no_strategy_subclass_is_caught(self) -> None:
        self.rig.write(
            "examples/hello.py",
            '"""Stub module."""\n\nVALUE = 1\n',
        )
        with self.assertRaises(StrategyApiDocumentationCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("hello", str(cm.exception))
        self.assertIn("Strategy", str(cm.exception))

    def test_deleted_example_file_is_caught(self) -> None:
        self.rig.delete("examples/hello.py")
        with self.assertRaises(StrategyApiDocumentationCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("hello.py", str(cm.exception))

    def test_bare_relative_alias_import_in_example_is_caught(self) -> None:
        # Codex round 6: `from .. import api` resolves the base to
        # `atp_strategy` and brings in `api` as an alias. The scanner
        # must validate `<resolved_base>.<alias.name>` for every
        # alias on bare-relative `from <dots> import X` forms — not
        # just the base path.
        rig_path = self.rig.root / "python" / "atp_strategy" / "examples" / "hello.py"
        content = rig_path.read_text(encoding="utf-8")
        rig_path.write_text(
            content + "\nif False:  # noqa\n    from .. import api  # noqa\n",
            encoding="utf-8",
        )
        with self.assertRaises(StrategyApiDocumentationCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("atp_strategy.api", str(cm.exception))

    def test_sibling_relative_import_is_allowed(self) -> None:
        # Counter-test: `from . import _harness` resolves to
        # `atp_strategy.examples._harness`, which is sibling
        # scaffolding and NOT forbidden. The scanner must not over-
        # reject this legitimate pattern.
        rig_path = self.rig.root / "python" / "atp_strategy" / "examples" / "hello.py"
        content = rig_path.read_text(encoding="utf-8")
        rig_path.write_text(
            content + "\nif False:  # noqa\n    from . import _harness as _h  # noqa\n",
            encoding="utf-8",
        )
        # Should not raise; the full evidence run returns successfully.
        evidence = self.rig.run(self.config)
        self.assertTrue(evidence)

    def test_relative_internal_import_in_example_is_caught(self) -> None:
        # Append `from ..api import StrategyAPIError` (resolves to
        # atp_strategy.api at import time) under a non-executed
        # branch. The AST scanner must normalize the relative import
        # before applying FORBIDDEN_INTERNAL_PREFIXES — Codex round 5
        # caught a gap where node.module alone was just `api` and the
        # absolute-prefix check missed it.
        rig_path = self.rig.root / "python" / "atp_strategy" / "examples" / "hello.py"
        content = rig_path.read_text(encoding="utf-8")
        rig_path.write_text(
            content + "\nif False:  # noqa\n" + "    from ..api import StrategyAPIError  # noqa\n",
            encoding="utf-8",
        )
        with self.assertRaises(StrategyApiDocumentationCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("atp_strategy.api", str(cm.exception))

    def test_repo_layout_import_in_example_is_caught(self) -> None:
        # Append a `from python.atp_strategy.calendar import X` import
        # inside an example. The example scanner must share the same
        # FORBIDDEN_INTERNAL_PREFIXES set as the docstring + README
        # scanners — Codex round 4 caught a divergence where the
        # example scanner had its own narrower local list.
        rig_path = self.rig.root / "python" / "atp_strategy" / "examples" / "hello.py"
        content = rig_path.read_text(encoding="utf-8")
        rig_path.write_text(
            content
            + "\nif False:  # noqa\n"
            + "    from python.atp_strategy.calendar import UsEquityTradingCalendar  # noqa\n",
            encoding="utf-8",
        )
        with self.assertRaises(StrategyApiDocumentationCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("python.atp_strategy", str(cm.exception))

    def test_deep_path_internal_import_in_example_is_caught(self) -> None:
        # Append a forbidden deep-path import. The contract bans all
        # such imports because they tell authors to read internals.
        rig_path = self.rig.root / "python" / "atp_strategy" / "examples" / "hello.py"
        content = rig_path.read_text(encoding="utf-8")
        rig_path.write_text(
            content + "\nfrom atp_strategy.api import StrategyAPIError as _err  # noqa\n",
            encoding="utf-8",
        )
        with self.assertRaises(StrategyApiDocumentationCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("atp_strategy.api", str(cm.exception))

    def test_anti_drift_import_of_non_all_name_is_caught(self) -> None:
        # Add a `from atp_strategy import SOME_PRIVATE_NAME` that is
        # not in __all__. The anti-drift rule must catch this.
        rig_path = self.rig.root / "python" / "atp_strategy" / "examples" / "hello.py"
        content = rig_path.read_text(encoding="utf-8")
        rig_path.write_text(
            content + "\nfrom atp_strategy import _NOT_PUBLIC  # noqa\n",
            encoding="utf-8",
        )
        with self.assertRaises(StrategyApiDocumentationCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("_NOT_PUBLIC", str(cm.exception))

    def test_execution_mode_string_literal_in_example_is_caught(self) -> None:
        # Introducing a `"live"` string literal into an example file
        # must fail the SRS-SDK-001 AC-14 guard regardless of how the
        # literal is otherwise used.
        rig_path = self.rig.root / "python" / "atp_strategy" / "examples" / "hello.py"
        content = rig_path.read_text(encoding="utf-8")
        rig_path.write_text(content + '\n_FORBIDDEN = "live"\n', encoding="utf-8")
        with self.assertRaises(StrategyApiDocumentationCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("live", str(cm.exception))


class CodeFenceFacadeMutationTest(unittest.TestCase):
    """The docstring and README fence anti-drift checks must catch deep-path
    imports and broken examples introduced via mutation, including ones
    embedded inside loop / class doctest blocks."""

    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.addCleanup(self.rig.close)
        self.config = load_config()

    def test_deep_path_import_in_class_docstring_doctest_is_caught(self) -> None:
        # Append a forbidden deep-path import into the Strategy class
        # docstring as a multi-line `>>>`/`...` block. The fence
        # extractor must preserve indentation so the `class` body
        # parses; the import scanner must flag the deep path.
        api_path = self.rig.root / "python" / "atp_strategy" / "api.py"
        text = api_path.read_text(encoding="utf-8")
        injection = (
            "    Example:\n"
            "        >>> class _Smoke:\n"
            "        ...     from atp_strategy.api import StrategyAPIError\n"
            "        ...     x = StrategyAPIError\n"
            "    "
        )
        # Replace the existing Strategy class docstring's `Example:` block.
        marker = "    Example:\n        >>> class MyStrategy(Strategy):"
        if marker not in text:
            self.fail(f"Strategy docstring shape changed; update the mutation: {marker!r}")
        text = text.replace(marker, injection + "    >>> class MyStrategy(Strategy):", 1)
        api_path.write_text(text, encoding="utf-8")
        with self.assertRaises(StrategyApiDocumentationCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("atp_strategy.api", str(cm.exception))

    def test_repo_layout_import_in_docstring_is_caught(self) -> None:
        # Inject a `from python.atp_strategy.X import Y` repo-layout
        # import into the Strategy class docstring's example block.
        # The scanner must reject this — production containers install
        # the package as `atp_strategy`, not `python.atp_strategy`.
        api_path = self.rig.root / "python" / "atp_strategy" / "api.py"
        text = api_path.read_text(encoding="utf-8")
        injection = (
            "    Example:\n"
            "        >>> from python.atp_strategy.calendar import UsEquityTradingCalendar\n"
            "        >>> UsEquityTradingCalendar.for_exchange  # doctest: +ELLIPSIS\n"
            "        <bound method ...>\n"
            "    "
        )
        marker = "    Example:\n        >>> class MyStrategy(Strategy):"
        if marker not in text:
            self.fail(f"Strategy docstring shape changed; update the mutation: {marker!r}")
        text = text.replace(marker, injection + "    >>> class MyStrategy(Strategy):", 1)
        api_path.write_text(text, encoding="utf-8")
        with self.assertRaises(StrategyApiDocumentationCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("python.atp_strategy", str(cm.exception))

    def test_deep_path_import_in_readme_fence_is_caught(self) -> None:
        readme = self.rig.root / "python" / "atp_strategy" / "README.md"
        text = readme.read_text(encoding="utf-8")
        injection = "\n```python\nfrom atp_strategy.api import StrategyAPIError\n```\n"
        readme.write_text(text + injection, encoding="utf-8")
        with self.assertRaises(StrategyApiDocumentationCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("atp_strategy.api", str(cm.exception))

    def test_unparseable_docstring_example_fence_is_caught(self) -> None:
        # Inject a broken multi-line doctest into Bar's class docstring.
        # The fence extractor must capture the full block (preserving
        # continuation indent) so the AST parse step trips on the
        # unbalanced parenthesis — and the failure must be reported as
        # a contract violation rather than a silent skip.
        api_path = self.rig.root / "python" / "atp_strategy" / "api.py"
        text = api_path.read_text(encoding="utf-8")
        marker = '    Example:\n        >>> Bar("AAPL"'
        if marker not in text:
            self.fail(f"Bar docstring shape changed; update the mutation: {marker!r}")
        injection = (
            "    Example:\n"
            "        >>> for i in range(\n"
            "        ...     # unbalanced — missing closing paren\n"
            '        >>> Bar("AAPL"'
        )
        text = text.replace(marker, injection, 1)
        api_path.write_text(text, encoding="utf-8")
        with self.assertRaises(StrategyApiDocumentationCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("does not AST-parse", str(cm.exception))


class ReadmeScheduleTagWordingTest(unittest.TestCase):
    """The Codex round-2 finding required asserting the README on_schedule
    row stays in sync with the runtime-emitted-tag contract on
    Strategy.on_schedule."""

    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.addCleanup(self.rig.close)
        self.config = load_config()

    def test_runtime_emitted_phrase_is_in_readme(self) -> None:
        readme = ROOT / "python" / "atp_strategy" / "README.md"
        text = readme.read_text(encoding="utf-8")
        self.assertIn(
            "runtime-emitted",
            text,
            "README on_schedule row must describe `tag` as runtime-emitted "
            "(not author-supplied) to match the Strategy.on_schedule "
            "docstring contract; Scheduler.* methods do not accept a tag",
        )

    def test_author_supplied_phrase_must_not_return_to_readme(self) -> None:
        readme_path = self.rig.root / "python" / "atp_strategy" / "README.md"
        text = readme_path.read_text(encoding="utf-8")
        # Inject the prior wording to prove a future regression would
        # be caught by a guard test like this. The L3 check alone
        # does not lock README prose to a positive-evidence string —
        # this test does, and runs in the same suite.
        text = text.replace(
            "`tag` is a runtime-emitted label",
            "`tag` is the label you registered",
        )
        readme_path.write_text(text, encoding="utf-8")
        local_text = readme_path.read_text(encoding="utf-8")
        self.assertNotIn(
            "runtime-emitted",
            local_text,
            "mutation did not actually flip the wording — fixture drift",
        )


class TierThreeMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.addCleanup(self.rig.close)
        self.config = load_config()

    def test_dropping_schedule_callback_attribute_docstring_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find='ScheduleCallback = Callable[["StrategyContext"], None]\n"""Callable invoked by the runtime when a scheduled trigger fires."""',
            replace='ScheduleCallback = Callable[["StrategyContext"], None]',
        )
        with self.assertRaises(StrategyApiDocumentationCheckError) as cm:
            self.rig.run(self.config)
        self.assertIn("ScheduleCallback", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
