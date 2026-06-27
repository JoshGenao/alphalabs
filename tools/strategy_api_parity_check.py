#!/usr/bin/env python3
"""Contract evidence script for SRS-SDK-001 (Python Strategy API parity).

Verifies that the Python Strategy API surface declared in
``architecture/runtime_services.json`` (block
``strategy_api_parity_contract``) is structurally identical for live IB
execution and internal paper simulation — i.e. user-authored strategies
have no observable way to discover or branch on the execution mode and
the SDK module itself does not bind to a specific brokerage / data
vendor.

SRS-SDK-001 traces SyRS AC-14 / SYS-82..SYS-87 and StRS SN-1.01 /
SN-1.29 / SN-3.01. The contract guarantees:

* **No mode-discriminator leakage.** No source file under
  ``python/atp_strategy/`` may reference an attribute or symbol named
  ``execution_mode`` / ``is_paper`` / ``is_live`` / ``mode``,
  ``ExecutionMode`` or ``StrategyMode``. Targets attribute / name nodes
  in the AST (not string literals) so legitimate identifiers like
  ``live_dividend_events`` do not trip the check.

* **No vendor-SDK imports inside the SDK module.** The user-facing
  package may not ``import`` (or ``from … import``) ``ibapi`` /
  ``ib_insync`` / ``ib_async`` / ``ibapi_client`` / ``databento`` /
  ``sharadar`` / ``polygon``. Complements the diff-only vendor scan in
  ``tools/critic_check.py::check_vendor_leakage`` with a full-tree
  invariant that catches a vendor binding that already slipped in.

* **Protocol surface completeness.** ``StrategyContext`` declares every
  required method (``subscribe``, ``order``, ``cancel``, ``log``,
  ``get_state``, ``set_state``, ``indicator``, ``consolidate``) and
  every required attribute (``config``, ``schedule``, ``calendar``,
  ``history``). Single source of truth for the L7 stub-driver parity
  test below.

* **Signature-name forbidden list.** For every required context method,
  ``inspect.signature`` may not contain any of the forbidden parameter
  names. Extends the 6-method scan in
  ``tools/strategy_api_check.py::check_sdk_001`` to all 8 methods.

* **StrategyConfig forbidden fields.** No dataclass field on
  ``StrategyConfig`` may be named ``execution_mode`` / ``is_paper`` /
  ``is_live`` / ``mode``. Closes the configured-time leak loophole —
  the orchestrator selects the path; user code never sees the choice.

Mirrors the PASS/FAIL output style of
``tools/orchestrator_deployment_version_check.py``.

Invoke:
    python3 tools/strategy_api_parity_check.py
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import importlib
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class StrategyApiParityCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise StrategyApiParityCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads(
        (root / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
    )


def contract_block(config: dict) -> dict:
    if "strategy_api_parity_contract" not in config:
        fail("architecture metadata is missing strategy_api_parity_contract")
    return config["strategy_api_parity_contract"]


def _sdk_root(config: dict, root: Path) -> Path:
    block = contract_block(config)
    sdk_path = root / block["sdk_path"]
    if not sdk_path.is_dir():
        fail(f"strategy SDK path missing: {sdk_path.relative_to(root)}")
    return sdk_path


def _iter_sdk_py_files(sdk_root: Path) -> list[Path]:
    return sorted(p for p in sdk_root.rglob("*.py") if p.is_file())


def _load_sdk_module(config: dict, root: Path) -> object:
    """Reload the SDK package from ``root`` (supports mutation-test tmpdirs)."""
    block = contract_block(config)
    package = block["sdk_package"]
    python_root = root / "python"
    if not python_root.is_dir():
        fail(f"python/ directory missing under {root}")
    # Reset sys.path / sys.modules so a tmpdir copy loads fresh.
    str_root = str(python_root)
    if str_root in sys.path:
        sys.path.remove(str_root)
    sys.path.insert(0, str_root)
    for name in list(sys.modules):
        if name == package or name.startswith(package + "."):
            sys.modules.pop(name, None)
    try:
        return importlib.import_module(package)
    except Exception as exc:  # pragma: no cover — surfaces as a parity fail
        fail(f"failed to import {package} from {python_root}: {exc!r}")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_no_mode_discriminator_leakage(config: dict, root: Path) -> str:
    block = contract_block(config)
    forbidden_attrs = set(block["forbidden_attribute_names"])
    forbidden_symbols = set(block["forbidden_symbol_names"])
    sdk_root = _sdk_root(config, root)
    for path in _iter_sdk_py_files(sdk_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden_attrs:
                fail(
                    f"{path.relative_to(root)}:{node.lineno} accesses "
                    f"forbidden attribute `.{node.attr}` — AC-14 forbids "
                    "any execution-mode discriminator on the SDK surface"
                )
            if isinstance(node, ast.Name) and node.id in forbidden_symbols:
                fail(
                    f"{path.relative_to(root)}:{node.lineno} references "
                    f"forbidden symbol `{node.id}` — execution mode must "
                    "not be visible to user code (AC-14 / SYS-82..SYS-87)"
                )
    return (
        f"python/{block['sdk_package']}/ source tree contains no "
        f"attribute/name references to {sorted(forbidden_attrs)} or "
        f"{sorted(forbidden_symbols)} (AC-14: user code cannot branch on "
        "execution mode)"
    )


def check_no_vendor_sdk_imports(config: dict, root: Path) -> str:
    block = contract_block(config)
    forbidden = set(block["forbidden_vendor_imports"])
    sdk_root = _sdk_root(config, root)
    for path in _iter_sdk_py_files(sdk_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_pkg = alias.name.split(".", 1)[0]
                    if root_pkg in forbidden:
                        fail(
                            f"{path.relative_to(root)}:{node.lineno} imports "
                            f"forbidden vendor SDK `{alias.name}` — the "
                            "Strategy API must not bind to a specific "
                            "brokerage / data-vendor (dependency boundary)"
                        )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                root_pkg = module.split(".", 1)[0]
                if root_pkg in forbidden:
                    fail(
                        f"{path.relative_to(root)}:{node.lineno} imports "
                        f"forbidden vendor SDK `{module}` — the Strategy "
                        "API must not bind to a specific brokerage / data "
                        "vendor (dependency boundary)"
                    )
    return (
        f"python/{block['sdk_package']}/ does not import any of the "
        f"forbidden vendor SDKs {sorted(forbidden)} (provider-agnostic "
        "user surface)"
    )


def check_protocol_surface_complete(config: dict, root: Path) -> str:
    block = contract_block(config)
    api = _load_sdk_module(config, root)
    ctx = api.StrategyContext
    required_methods = block["required_context_methods"]
    missing_methods = [m for m in required_methods if not hasattr(ctx, m)]
    if missing_methods:
        fail(
            f"StrategyContext is missing required methods "
            f"{missing_methods} — the live and paper drivers cannot "
            "implement an identical surface without them (SRS-SDK-001)"
        )
    required_attrs = block["required_context_attrs"]
    annotations = getattr(ctx, "__annotations__", {})
    missing_attrs = [a for a in required_attrs if a not in annotations]
    if missing_attrs:
        fail(
            f"StrategyContext is missing required attributes "
            f"{missing_attrs} in its Protocol annotation block — "
            "live/paper parity requires the same attribute surface"
        )
    return (
        f"StrategyContext declares all {len(required_methods)} required "
        f"methods ({', '.join(required_methods)}) and all "
        f"{len(required_attrs)} required attributes "
        f"({', '.join(required_attrs)})"
    )


def check_signature_no_mode_params(config: dict, root: Path) -> str:
    block = contract_block(config)
    forbidden = set(block["forbidden_param_names"])
    api = _load_sdk_module(config, root)
    ctx = api.StrategyContext
    leaked: list[str] = []
    for method in block["required_context_methods"]:
        func = getattr(ctx, method, None)
        if func is None:
            fail(
                f"StrategyContext.{method} is missing — signature check "
                "cannot proceed (protocol surface incomplete)"
            )
        params = set(inspect.signature(func).parameters)
        bad = params & forbidden
        if bad:
            leaked.append(f"{method}({sorted(bad)})")
    if leaked:
        fail(
            "StrategyContext methods leak execution-mode parameters: "
            f"{leaked} — same surface must serve live and paper (AC-14)"
        )
    return (
        f"every StrategyContext method ({len(block['required_context_methods'])} "
        f"total) rejects execution-mode parameter names {sorted(forbidden)}"
    )


def check_config_no_mode_fields(config: dict, root: Path) -> str:
    block = contract_block(config)
    forbidden = set(block["forbidden_config_fields"])
    api = _load_sdk_module(config, root)
    cfg_cls = api.StrategyConfig
    if not dataclasses.is_dataclass(cfg_cls):
        fail(
            "StrategyConfig must be a dataclass — parity check relies on "
            "introspecting its fields"
        )
    field_names = {f.name for f in dataclasses.fields(cfg_cls)}
    leaked = field_names & forbidden
    if leaked:
        fail(
            f"StrategyConfig exposes execution-mode field(s) {sorted(leaked)} "
            "— the orchestrator selects the live/paper path; user code "
            "must not see the choice (AC-14 / SYS-82..SYS-87)"
        )
    return (
        f"StrategyConfig has no field named {sorted(forbidden)}; "
        "orchestrator-selected execution path is invisible to user code"
    )


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


CHECKS = (
    ("no_mode_discriminator_leakage", check_no_mode_discriminator_leakage),
    ("no_vendor_sdk_imports", check_no_vendor_sdk_imports),
    ("protocol_surface_complete", check_protocol_surface_complete),
    ("signature_no_mode_params", check_signature_no_mode_params),
    ("config_no_mode_fields", check_config_no_mode_fields),
)


def assert_strategy_api_parity_static(
    config: dict, root: Path = ROOT
) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py``."""
    return [fn(config, root) for _, fn in CHECKS]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Override the repository root (used by mutation tests).",
    )
    args = parser.parse_args()
    try:
        config = load_config(args.root)
        evidence = assert_strategy_api_parity_static(config, args.root)
    except StrategyApiParityCheckError as error:
        print(f"SRS-SDK-001 FAIL: {error}")
        return 1
    print("SRS-SDK-001 PASS")
    for line in evidence:
        print(f"  - {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
