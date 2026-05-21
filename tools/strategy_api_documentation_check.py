#!/usr/bin/env python3
"""Contract evidence script for SRS-SDK-009 (Python Strategy API documentation).

Verifies that the Python Strategy API ships sufficient docstrings, README
sections, and runnable example files for a Python-proficient trader to
author a new strategy without ever opening ``python/atp_strategy/api.py``
or any other internal file.

The cross-language source of truth for the tier system, required README
sections, required example modules, and anti-drift rules lives in
``architecture/runtime_services.json`` under
``strategy_api_documentation_contract``. Mirrors the PASS/FAIL output
style of ``tools/strategy_api_warmup_check.py``.

Invoke:
    python3 tools/strategy_api_documentation_check.py
"""

from __future__ import annotations

import argparse
import ast
import importlib
import inspect
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class StrategyApiDocumentationCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise StrategyApiDocumentationCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "strategy_api_documentation_contract" not in config:
        fail("architecture metadata is missing strategy_api_documentation_contract")
    return config["strategy_api_documentation_contract"]


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
    except Exception as exc:  # pragma: no cover — surfaces as a doc-check fail
        fail(f"failed to import atp_strategy from {python_root}: {exc!r}")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_tier_partition_covers_all(config: dict, root: Path) -> str:
    block = contract_block(config)
    full = set(block["fully_documented_symbols"])
    summary = set(block["summary_documented_symbols"])
    one_line = set(block["one_line_documented_symbols"])
    overlap = (full & summary) | (full & one_line) | (summary & one_line)
    if overlap:
        fail(
            f"tier lists must be disjoint; overlap found: {sorted(overlap)} — "
            "every public symbol belongs to exactly one tier so the contract "
            "is unambiguous"
        )
    api = _load_sdk_module(root)
    declared = set(api.__all__)
    union = full | summary | one_line
    missing = declared - union
    extras = union - declared
    if missing:
        fail(
            f"atp_strategy.__all__ contains symbols not assigned to any "
            f"tier: {sorted(missing)} — every public name must be tiered "
            "so it is documentation-enforced"
        )
    if extras:
        fail(
            f"tier lists reference symbols not in atp_strategy.__all__: "
            f"{sorted(extras)} — the contract is drifting from the package "
            "facade"
        )
    return (
        f"tier partition of atp_strategy.__all__ (size {len(declared)}) is "
        f"disjoint and complete: full={len(full)} summary={len(summary)} "
        f"one_line={len(one_line)}"
    )


def _symbol_docstring(api: object, name: str) -> str:
    obj = getattr(api, name, None)
    if obj is None:
        fail(f"atp_strategy.__all__ exposes {name!r} but the attribute is None")
    # ``inspect.getdoc`` returns the class docstring for primitives such
    # as ``int`` constants and ``None`` for type aliases like
    # ``Callable[..., None]``. Both of those are useless signals here
    # — they don't reflect documentation the strategy author wrote.
    # For classes / functions / methods / modules, fall through to
    # ``inspect.getdoc``; otherwise fall back to the AST attribute
    # docstring extractor below.
    if (
        inspect.isclass(obj)
        or inspect.isfunction(obj)
        or inspect.ismethod(obj)
        or inspect.ismodule(obj)
    ):
        return inspect.getdoc(obj) or ""
    return _ast_attribute_docstring(api, name)


def _ast_attribute_docstring(api: object, name: str) -> str:
    """Extract the conventional triple-quoted string written after a
    module-level assignment to ``name``.

    Python does not attach such strings to the named attribute itself;
    Sphinx / pdoc / our author-grade tier-3 check all read them via the
    module AST. We locate ``name`` in ``api.__init__.py`` (which re-
    exports it from a submodule), follow the re-export to the defining
    submodule, parse that submodule, and return the string Expr that
    immediately follows ``name = ...`` if present.
    """
    for module_path in _candidate_module_paths(api):
        if not module_path.is_file():
            continue
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        body = tree.body
        for i, node in enumerate(body):
            target_names = []
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        target_names.append(t.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target_names.append(node.target.id)
            if name not in target_names:
                continue
            # Look at the next statement.
            if i + 1 < len(body):
                nxt = body[i + 1]
                if (
                    isinstance(nxt, ast.Expr)
                    and isinstance(nxt.value, ast.Constant)
                    and isinstance(nxt.value.value, str)
                ):
                    return inspect.cleandoc(nxt.value.value)
    return ""


def _candidate_module_paths(api: object) -> list[Path]:
    """Locate every source file under the loaded ``atp_strategy`` package.

    The tier-3 attribute-docstring check needs to scan whichever
    submodule defines a given name; we walk every ``.py`` file under
    the package directory (excluding ``__pycache__`` and tests).
    """
    pkg_file = getattr(api, "__file__", None)
    if not pkg_file:
        return []
    pkg_dir = Path(pkg_file).resolve().parent
    return [p for p in pkg_dir.glob("*.py")]


def check_full_tier_min_chars_and_example(config: dict, root: Path) -> str:
    block = contract_block(config)
    api = _load_sdk_module(root)
    min_chars = block["min_docstring_chars_per_tier"]["full"]
    example_patterns = [re.compile(p) for p in block["tier_one_example_marker_regex_alternatives"]]
    for name in block["fully_documented_symbols"]:
        doc = _symbol_docstring(api, name)
        if len(doc) < min_chars:
            fail(
                f"tier-1 (full) symbol {name!r} has docstring of "
                f"{len(doc)} chars; require >= {min_chars} for the "
                "author-grade reference contract (SRS-SDK-009 AC)"
            )
        if not any(p.search(doc) for p in example_patterns):
            fail(
                f"tier-1 (full) symbol {name!r} docstring contains no "
                "usage example — require either a `>>> ` doctest line "
                "or a ```python``` code fence so authors can crib from it"
            )
    return (
        f"every tier-1 symbol ({len(block['fully_documented_symbols'])}) has "
        f">= {min_chars} char docstring and at least one example fence"
    )


def check_summary_and_one_line_tier_min_chars(config: dict, root: Path) -> str:
    block = contract_block(config)
    api = _load_sdk_module(root)
    min_summary = block["min_docstring_chars_per_tier"]["summary"]
    min_one_line = block["min_docstring_chars_per_tier"]["one_line"]
    for name in block["summary_documented_symbols"]:
        doc = _symbol_docstring(api, name)
        if len(doc) < min_summary:
            fail(
                f"tier-2 (summary) symbol {name!r} has docstring of "
                f"{len(doc)} chars; require >= {min_summary}"
            )
    for name in block["one_line_documented_symbols"]:
        doc = _symbol_docstring(api, name)
        if len(doc) < min_one_line:
            fail(
                f"tier-3 (one_line) symbol {name!r} has docstring of "
                f"{len(doc)} chars; require >= {min_one_line}"
            )
    return (
        f"tier-2 ({len(block['summary_documented_symbols'])} symbols, "
        f">= {min_summary} chars) and tier-3 "
        f"({len(block['one_line_documented_symbols'])} symbols, "
        f">= {min_one_line} chars) docstrings meet minimums"
    )


def check_strategy_hooks_documented(config: dict, root: Path) -> str:
    block = contract_block(config)
    api = _load_sdk_module(root)
    min_chars = block["min_docstring_chars_for_hook_or_context_method"]
    for hook in block["required_strategy_overridable_hooks"]:
        method = getattr(api.Strategy, hook, None)
        if not callable(method):
            fail(f"Strategy.{hook} is missing or not callable")
        doc = inspect.getdoc(method) or ""
        if len(doc) < min_chars:
            fail(
                f"Strategy.{hook} docstring has {len(doc)} chars; require "
                f">= {min_chars} so strategy authors know when the hook "
                "fires and what to do in it (SRS-SDK-009 AC)"
            )
    return (
        f"every Strategy hook ({block['required_strategy_overridable_hooks']}) "
        f"has >= {min_chars} char docstring"
    )


def check_strategy_context_methods_documented(config: dict, root: Path) -> str:
    block = contract_block(config)
    api = _load_sdk_module(root)
    min_chars = block["min_docstring_chars_for_hook_or_context_method"]
    for method_name in block["required_strategy_context_methods"]:
        method = getattr(api.StrategyContext, method_name, None)
        if not callable(method):
            fail(f"StrategyContext.{method_name} is missing or not callable")
        doc = inspect.getdoc(method) or ""
        if len(doc) < min_chars:
            fail(
                f"StrategyContext.{method_name} docstring has {len(doc)} "
                f"chars; require >= {min_chars} so strategy authors do not "
                "need to read api.py to understand the runtime surface "
                "(SRS-SDK-009 AC)"
            )
    return (
        f"every StrategyContext method "
        f"({block['required_strategy_context_methods']}) has >= "
        f"{min_chars} char docstring"
    )


def check_readme_sections(config: dict, root: Path) -> str:
    block = contract_block(config)
    readme = root / block["readme_path"]
    if not readme.is_file():
        fail(f"README missing: {readme}")
    text = readme.read_text(encoding="utf-8")
    for section in block["required_readme_sections"]:
        # Section headers in the README are H2 (`## Title`) or H3
        # (`### Title`). Match line-anchored to avoid false positives
        # in body prose that mentions a section title.
        pattern = re.compile(rf"(?m)^#{{2,3}}\s+{re.escape(section)}\b")
        if not pattern.search(text):
            fail(
                f"README {readme} is missing required section "
                f"{section!r} — strategy authors expect every documented "
                "topic to live under its own header so navigation is "
                "predictable (SRS-SDK-009 AC)"
            )
    return (
        f"README {block['readme_path']} contains all "
        f"{len(block['required_readme_sections'])} required sections"
    )


def check_example_modules_importable(config: dict, root: Path) -> str:
    block = contract_block(config)
    api = _load_sdk_module(root)
    pkg_path = root / block["examples_package_path"]
    if not pkg_path.is_dir():
        fail(f"examples package directory missing: {pkg_path}")
    if not (pkg_path / "__init__.py").is_file():
        fail(f"examples package missing __init__.py at {pkg_path}/__init__.py")
    for name in block["required_example_modules"]:
        module_file = pkg_path / f"{name}.py"
        if not module_file.is_file():
            fail(
                f"required example module missing: {module_file} — "
                "every name in required_example_modules must have a "
                "self-contained .py file under the examples package"
            )
        # AST-parse for structural validity.
        try:
            tree = ast.parse(module_file.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            fail(f"example module {module_file} fails ast.parse: {exc}")
        if not (
            tree.body
            and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)
        ):
            fail(
                f"example module {module_file} is missing a module-level "
                "docstring — authors need to know at a glance what each "
                "example demonstrates"
            )
        # Import and locate a Strategy subclass.
        module = importlib.import_module(f"atp_strategy.examples.{name}")
        strategy_subclasses = [
            obj
            for obj in vars(module).values()
            if inspect.isclass(obj) and issubclass(obj, api.Strategy) and obj is not api.Strategy
        ]
        if len(strategy_subclasses) != 1:
            fail(
                f"example module atp_strategy.examples.{name} defines "
                f"{len(strategy_subclasses)} Strategy subclasses; require "
                "exactly 1 so the example is unambiguous"
            )
    return (
        f"all {len(block['required_example_modules'])} example modules "
        f"({block['required_example_modules']}) import, parse, and expose "
        "exactly one Strategy subclass"
    )


def _resolve_relative_import(node: ast.ImportFrom, *, example_module: str) -> str | None:
    """Resolve a relative ``ImportFrom`` to its absolute module path.

    ``example_module`` is the dotted path of the example file itself
    (e.g. ``"atp_strategy.examples.hello"``). For ``node.level == 1``
    the parent package is ``atp_strategy.examples``; for ``node.level
    == 2`` it is ``atp_strategy``. Combined with ``node.module``, the
    resulting absolute path is what the import statement would actually
    resolve to at runtime.

    Returns ``None`` if the relative level escapes the package root —
    that's a runtime ``ValueError`` and irrelevant to anti-internals.
    """
    if node.level == 0:
        return node.module
    parts = example_module.split(".")
    if node.level > len(parts):
        return None
    package_parts = parts[: len(parts) - node.level]
    if node.module:
        return ".".join(package_parts + [node.module])
    return ".".join(package_parts)


def check_example_imports_only_facade(config: dict, root: Path) -> str:
    block = contract_block(config)
    pkg_path = root / block["examples_package_path"]
    # Share the global tuple so all scanners (example modules,
    # docstring fences, README fences) reject the same anti-internals
    # set. A divergence here previously let example files slip
    # `from python.atp_strategy.<sub> import ...` past the example
    # check while the docstring scanner caught it (Codex round 4).
    forbidden_prefixes = FORBIDDEN_INTERNAL_PREFIXES
    for name in block["required_example_modules"]:
        module_file = pkg_path / f"{name}.py"
        if not module_file.is_file():
            # Existence is enforced by check_example_modules_importable,
            # the dedicated check that comes after the AST-only passes;
            # surfacing a FileNotFoundError out of this check would mask
            # the structured contract error from that one.
            continue
        example_module = f"atp_strategy.examples.{name}"
        tree = ast.parse(module_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module_being_imported: str | None
            if isinstance(node, ast.ImportFrom):
                # Normalize relative imports (`from ..api import X`,
                # `from . import _harness`, etc.) to their absolute
                # package path so the forbidden-prefix check catches
                # them. Without this, level > 0 imports into SDK
                # internals (atp_strategy.api / .warmup / etc.) slip
                # through because node.module alone is just `api` /
                # `warmup`. Codex round 5 surfaced this gap.
                module_being_imported = _resolve_relative_import(
                    node, example_module=example_module
                )
                # Additional case caught in Codex round 6: a
                # statement like `from .. import api` has node.module
                # is None and brings in `api` as a binding under the
                # resolved base. Validate each imported alias as
                # `<resolved_base>.<alias.name>` so this form cannot
                # smuggle an SDK-internal submodule through the base-
                # path-only check below.
                if node.level > 0 and node.module is None and module_being_imported:
                    resolved_base = module_being_imported
                    for alias in node.names:
                        resolved_alias = f"{resolved_base}.{alias.name}"
                        if any(
                            resolved_alias == p or resolved_alias.startswith(p + ".")
                            for p in forbidden_prefixes
                        ):
                            fail(
                                f"example {module_file} has `from "
                                f"{'.' * node.level} import {alias.name}` "
                                f"which resolves to {resolved_alias!r} — "
                                "examples must use only the atp_strategy "
                                "package facade (SRS-SDK-009 AC)"
                            )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module_being_imported = alias.name
                    if module_being_imported and any(
                        module_being_imported == p or module_being_imported.startswith(p + ".")
                        for p in forbidden_prefixes
                    ):
                        fail(
                            f"example {module_file} imports "
                            f"{module_being_imported!r} which is a platform "
                            "internal; examples must use only the "
                            "atp_strategy package facade so authors do not "
                            "learn to read internals (SRS-SDK-009 AC)"
                        )
                continue
            else:
                continue
            if module_being_imported is None:
                continue
            if any(
                module_being_imported == p or module_being_imported.startswith(p + ".")
                for p in forbidden_prefixes
            ):
                fail(
                    f"example {module_file} has `from "
                    f"{module_being_imported} import ...` — examples must "
                    "use only the atp_strategy package facade (SRS-SDK-009 AC)"
                )
    return (
        "every example module imports only from atp_strategy, "
        "atp_strategy.examples, or the standard library (relative "
        "imports normalized to absolute paths) — no deep paths into "
        "platform internals"
    )


def check_example_anti_drift(config: dict, root: Path) -> str:
    block = contract_block(config)
    api = _load_sdk_module(root)
    declared = set(api.__all__)
    pkg_path = root / block["examples_package_path"]
    for name in block["required_example_modules"]:
        module_file = pkg_path / f"{name}.py"
        if not module_file.is_file():
            continue
        tree = ast.parse(module_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "atp_strategy":
                for alias in node.names:
                    if alias.name not in declared:
                        fail(
                            f"example {module_file} imports "
                            f"{alias.name!r} from atp_strategy, but "
                            "that name is not in atp_strategy.__all__ — "
                            "examples must reference only documented "
                            "public symbols (SRS-SDK-009 AC anti-drift)"
                        )
    return "every `from atp_strategy import ...` reference in the examples resolves to a name in __all__"


def check_no_execution_mode_branch_in_examples(config: dict, root: Path) -> str:
    block = contract_block(config)
    pkg_path = root / block["examples_package_path"]
    forbidden_tokens = ("live", "paper", "backtest")
    # The token check looks for usage as a string literal compared
    # against a config / context discriminator. We do a minimal lexical
    # match: any string literal that is exactly one of the forbidden
    # tokens (case-insensitive) inside an example file is a violation,
    # because there is no legitimate reason an SRS-SDK-001 AC-14
    # compliant strategy would branch on those.
    for name in block["required_example_modules"]:
        module_file = pkg_path / f"{name}.py"
        if not module_file.is_file():
            continue
        tree = ast.parse(module_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.lower() in forbidden_tokens:
                    fail(
                        f"example {module_file} contains the string "
                        f"literal {node.value!r}; SRS-SDK-001 AC-14 "
                        "requires the same source to run in live and "
                        "paper modes — examples must not branch on "
                        "execution mode (SRS-SDK-009 AC)"
                    )
    return (
        "no example module contains string literals "
        f"{forbidden_tokens} (SRS-SDK-001 AC-14 single-source guarantee)"
    )


def check_readme_pointers_resolve(config: dict, root: Path) -> str:
    block = contract_block(config)
    readme = root / block["readme_path"]
    text = readme.read_text(encoding="utf-8")
    pattern = re.compile(r"python\s+-m\s+atp_strategy\.examples\.([A-Za-z0-9_]+)")
    declared = set(block["required_example_modules"])
    found = set(pattern.findall(text))
    if not found:
        fail(
            f"README {readme} does not reference any "
            "`python -m atp_strategy.examples.<name>` invocation — "
            "the Getting started section must point at the runnable "
            "examples"
        )
    extras = found - declared
    if extras:
        fail(
            f"README {readme} references example modules "
            f"{sorted(extras)} that are not in required_example_modules "
            "— the contract block and README are drifting"
        )
    return (
        f"README references {len(found)} runnable example invocation(s) "
        f"({sorted(found)}); all resolve to required_example_modules"
    )


FORBIDDEN_INTERNAL_PREFIXES = (
    "atp_strategy.api",
    "atp_strategy.warmup",
    "atp_strategy.indicators",
    "atp_strategy.scheduler",
    "atp_strategy.calendar",
    # Repo-layout imports are an even worse leak: they teach authors
    # that the source tree under `python/` is the supported package
    # path, which it is not. ``python.atp_strategy.*`` resolves only
    # when CWD happens to be the repo root with ``python/`` on
    # PYTHONPATH; production strategy containers install the package
    # as ``atp_strategy``. Forbid the whole subtree.
    "python.atp_strategy",
)


def _extract_code_blocks_from_docstring(doc: str) -> list[str]:
    """Return Python snippets a docstring teaches authors to run.

    Handles both ``>>>`` doctest blocks and ```` ```python ```` fenced
    code. ``...`` continuation lines are treated as continuations of
    the preceding ``>>>`` block. Fenced blocks tagged with
    ``python`` (or no tag at all) are extracted; other tags are
    ignored.

    Indentation INSIDE a doctest block is preserved: the first
    ``>>>`` line establishes the baseline column, and any
    subsequent ``...`` continuation re-prepends whatever indentation
    appeared after the ``... `` prefix. This is required for
    multi-line class / for-loop / def doctests to AST-parse — the
    naive ``stripped[3:].lstrip()`` strategy would flatten every
    continuation to column 0 and produce ``IndentationError``,
    silently skipping the block during downstream import scans.
    """
    blocks: list[list[str]] = []
    current_block: list[str] | None = None
    in_fence = False
    fence_lang_python = False
    fence_lines: list[str] = []
    for raw_line in doc.splitlines():
        line = raw_line
        stripped = line.lstrip()
        if in_fence:
            if stripped.startswith("```"):
                if fence_lang_python:
                    blocks.append(list(fence_lines))
                in_fence = False
                fence_lines = []
                fence_lang_python = False
                continue
            fence_lines.append(line)
            continue
        if stripped.startswith("```"):
            in_fence = True
            tag = stripped[3:].strip().lower()
            fence_lang_python = tag in ("", "python", "py")
            fence_lines = []
            # Close any doctest block in progress.
            if current_block is not None:
                blocks.append(current_block)
                current_block = None
            continue
        if stripped.startswith(">>> ") or stripped == ">>>":
            # Preserve everything AFTER the `>>> ` marker, including
            # any leading whitespace the author wrote.
            content = stripped[4:] if len(stripped) > 3 else ""
            if current_block is None:
                current_block = []
            current_block.append(content)
            continue
        if (stripped.startswith("... ") or stripped == "...") and current_block is not None:
            content = stripped[4:] if len(stripped) > 3 else ""
            current_block.append(content)
            continue
        if current_block is not None:
            blocks.append(current_block)
            current_block = None
    if current_block is not None:
        blocks.append(current_block)
    return ["\n".join(b) for b in blocks if b]


def _extract_python_code_blocks_from_markdown(text: str) -> list[str]:
    """Return ``python``-fenced code blocks from a markdown document.

    The language tag is REQUIRED to be ``python`` (or ``py``); a bare
    ``` `` `` open-fence is rejected. Without this, the non-greedy
    capture greedily matched from one close-fence to the next
    open-fence and dragged intervening prose into the parser, producing
    spurious AST failures (and silently masking real ones).
    """
    pattern = re.compile(r"```(?:python|py)\n(.*?)\n```", re.DOTALL)
    return [match.group(1) for match in pattern.finditer(text)]


def _scan_code_for_forbidden_imports(
    code: str,
    *,
    declared: set[str],
) -> list[str]:
    """Return a list of violation messages for ``code``.

    The two anti-drift violations checked here:

    * Any import from an ``atp_strategy.<internal>`` deep path. These
      teach authors to read platform internals.
    * Any ``from atp_strategy import <name>`` whose ``<name>`` is not
      in the package facade's ``__all__``. These teach authors to use
      symbols that may move or disappear.

    Fails CLOSED on SyntaxError: a docstring example that does not
    AST-parse is itself a documentation bug — authors will copy
    broken code. The check surfaces an explicit violation so the
    parser cannot silently let a bad fence through.
    """
    violations: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [
            f"code fence does not AST-parse ({exc.msg} at line {exc.lineno}); "
            "broken examples teach authors broken code"
        ]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if any(module == p or module.startswith(p + ".") for p in FORBIDDEN_INTERNAL_PREFIXES):
                violations.append(
                    f"deep-path import `from {module} import ...` teaches "
                    "authors to read platform internals"
                )
                continue
            if module == "atp_strategy":
                for alias in node.names:
                    if alias.name not in declared:
                        violations.append(
                            f"`from atp_strategy import {alias.name}` references "
                            "a name not in __all__"
                        )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(
                    alias.name == p or alias.name.startswith(p + ".")
                    for p in FORBIDDEN_INTERNAL_PREFIXES
                ):
                    violations.append(
                        f"deep-path import `import {alias.name}` teaches "
                        "authors to read platform internals"
                    )
    return violations


def check_docstring_code_fences_use_facade(config: dict, root: Path) -> str:
    """Every code fence in a public docstring uses only the package facade."""
    block = contract_block(config)
    api = _load_sdk_module(root)
    declared = set(api.__all__)
    public_names = (
        list(block["fully_documented_symbols"])
        + list(block["summary_documented_symbols"])
        + list(block["one_line_documented_symbols"])
    )
    for name in public_names:
        doc = _symbol_docstring(api, name)
        for code in _extract_code_blocks_from_docstring(doc):
            for violation in _scan_code_for_forbidden_imports(code, declared=declared):
                fail(
                    f"docstring of public symbol {name!r} contains "
                    f"code-fence violation: {violation} — strategy "
                    "authors must learn to import only from the "
                    "atp_strategy package facade (SRS-SDK-009 AC anti-drift)"
                )
    return (
        f"every code fence in the {len(public_names)} public docstrings "
        "uses only the atp_strategy facade — no deep-path imports, no "
        "non-__all__ references"
    )


def check_readme_code_fences_use_facade(config: dict, root: Path) -> str:
    """Every ``python`` code fence in the README uses only the package facade."""
    block = contract_block(config)
    api = _load_sdk_module(root)
    declared = set(api.__all__)
    readme = root / block["readme_path"]
    text = readme.read_text(encoding="utf-8")
    fences = _extract_python_code_blocks_from_markdown(text)
    for index, code in enumerate(fences):
        for violation in _scan_code_for_forbidden_imports(code, declared=declared):
            fail(
                f"README {readme} code fence #{index + 1} has anti-drift "
                f"violation: {violation} — author-facing prose must "
                "teach only the atp_strategy package facade"
            )
    return (
        f"every ``python`` code fence in the README ({len(fences)} blocks) "
        "uses only the atp_strategy facade"
    )


def check_documentation_module_paths_exist(config: dict, root: Path) -> str:
    block = contract_block(config)
    for rel in block["documentation_module_paths"]:
        path = root / rel
        if not path.is_file():
            fail(f"documentation_module_paths entry missing on disk: {path}")
        if path.stat().st_size == 0:
            fail(f"documentation_module_paths entry is empty: {path}")
    return (
        f"every documentation_module_paths entry "
        f"({len(block['documentation_module_paths'])}) exists and is non-empty"
    )


# --------------------------------------------------------------------------- #
# Public assert helper used by the L3 mutation rig
# --------------------------------------------------------------------------- #


def assert_strategy_api_documentation_static(
    config: dict | None = None, root: Path = ROOT
) -> list[str]:
    """Run every documentation contract check and return evidence strings.

    Raises ``StrategyApiDocumentationCheckError`` on the first failure.
    """
    config = config if config is not None else load_config(root)
    # Order matters: every AST-only check runs before the import-based
    # ones so a mutation that introduces a forbidden import surfaces as
    # a structured StrategyApiDocumentationCheckError rather than as an
    # ImportError out of check_example_modules_importable. The AST
    # passes are also strictly cheaper.
    return [
        check_tier_partition_covers_all(config, root),
        check_full_tier_min_chars_and_example(config, root),
        check_summary_and_one_line_tier_min_chars(config, root),
        check_strategy_hooks_documented(config, root),
        check_strategy_context_methods_documented(config, root),
        check_readme_sections(config, root),
        check_documentation_module_paths_exist(config, root),
        check_readme_pointers_resolve(config, root),
        check_example_imports_only_facade(config, root),
        check_example_anti_drift(config, root),
        check_no_execution_mode_branch_in_examples(config, root),
        check_docstring_code_fences_use_facade(config, root),
        check_readme_code_fences_use_facade(config, root),
        check_example_modules_importable(config, root),
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
        evidence = assert_strategy_api_documentation_static(root=args.root)
    except StrategyApiDocumentationCheckError as exc:
        print(f"SRS-SDK-009 FAIL: {exc}", file=sys.stderr)
        return 1
    print("SRS-SDK-009 PASS — Python Strategy API documentation surface")
    for line in evidence:
        print(f"  * {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
