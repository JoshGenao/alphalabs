#!/usr/bin/env python3
"""Contract evidence script for SRS-SDK-003 (single tradable asset class invariant).

Verifies that the Python Strategy SDK exposes:

* An ``AssetClass`` enum containing exactly ``EQUITY`` and ``OPTION``.
* A ``StrategyConfig`` dataclass carrying a ``tradable_asset_class``
  field with no default (operator-supplied per container).
* An ``OrderRequest`` dataclass carrying an ``asset_class`` field
  whose default is ``AssetClass.EQUITY``.
* A ``StrategyContext.subscribe`` Protocol method that accepts an
  ``asset_class`` keyword (default ``AssetClass.EQUITY``) so a strategy
  may subscribe to both equities and options for analysis regardless
  of its tradable class.
* A ``StrategyContext.order`` Protocol method whose docstring names
  ``AssetClassViolation`` — locks the structural promise that
  ``order`` enforces the invariant.
* A shipped ``assert_asset_class(config, request)`` helper, re-exported
  by the ``atp_strategy`` package, that raises ``AssetClassViolation``
  iff ``request.asset_class != config.tradable_asset_class``. The
  helper is exercised with all four EQUITY/OPTION combinations on
  every contract-check run so silent-skip or inverted-comparison
  mutations are caught at the check layer (not only by the L7 domain
  test).
* ``AssetClassViolation`` derives from ``StrategyAPIError`` and is
  re-exported by the ``atp_strategy`` package.

SRS-SDK-003 traces SyRS ``SYS-5`` (single tradable asset class while
analysis subscriptions span both) and ``SYS-64`` (structured-error
contract on order submission failures); StRS ``SN-1.07`` / ``BG-1`` /
``BG-5``.

Mirrors the PASS/FAIL output style of
``tools/strategy_api_scheduler_check.py``.

Invoke:
    python3 tools/strategy_api_subscriptions_check.py
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


class StrategyApiSubscriptionsCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise StrategyApiSubscriptionsCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "strategy_api_subscriptions_contract" not in config:
        fail("architecture metadata is missing strategy_api_subscriptions_contract")
    return config["strategy_api_subscriptions_contract"]


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
    except Exception as exc:  # pragma: no cover — surfaces as a subscriptions fail
        fail(f"failed to import atp_strategy from {python_root}: {exc!r}")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_asset_class_enum_members(config: dict, root: Path) -> str:
    block = contract_block(config)
    required = set(block["required_asset_class_members"])
    api = _load_sdk_module(root)
    members = {m.name for m in api.AssetClass}
    if members != required:
        fail(
            f"AssetClass enum members are {sorted(members)}; expected exactly "
            f"{sorted(required)} — SYS-5 enumerates EQUITY and OPTION as the "
            "only tradable asset classes; extras would silently broaden the "
            "tradable surface and missing members would block the invariant"
        )
    return (
        f"AssetClass enum members are exactly {sorted(required)} — "
        "SYS-5 single-tradable-class enumeration locked"
    )


def check_strategy_config_field(config: dict, root: Path) -> str:
    block = contract_block(config)
    required_fields = list(block["required_config_fields"])
    api = _load_sdk_module(root)
    fields = {f.name: f for f in dataclasses.fields(api.StrategyConfig)}
    missing = [name for name in required_fields if name not in fields]
    if missing:
        fail(
            f"StrategyConfig is missing required fields {missing} — "
            "SRS-SDK-003 AC requires tradable_asset_class on the "
            "container-time configuration"
        )
    field = fields["tradable_asset_class"]
    if field.type not in ("AssetClass", api.AssetClass):
        # dataclasses.Field.type is the unevaluated annotation string under
        # `from __future__ import annotations`; accept either the string
        # form or the resolved type.
        fail(
            f"StrategyConfig.tradable_asset_class type annotation is "
            f"{field.type!r}; expected AssetClass — typing must lock the "
            "operator-supplied class to the enum surface"
        )
    if field.default is not dataclasses.MISSING or field.default_factory is not dataclasses.MISSING:
        fail(
            "StrategyConfig.tradable_asset_class has a default value — the "
            "tradable class must be operator-supplied per container so "
            "production never silently defaults to one asset class"
        )
    return (
        "StrategyConfig.tradable_asset_class: AssetClass field is required "
        "(no default) — operator must declare each strategy's tradable class"
    )


def check_order_request_field(config: dict, root: Path) -> str:
    block = contract_block(config)
    required_fields = list(block["required_request_fields"])
    required_default = block["required_request_default_asset_class"]
    api = _load_sdk_module(root)
    fields = {f.name: f for f in dataclasses.fields(api.OrderRequest)}
    missing = [name for name in required_fields if name not in fields]
    if missing:
        fail(
            f"OrderRequest is missing required fields {missing} — "
            "SRS-SDK-003 requires every order to carry its asset_class "
            "so the runtime can route and the guard can enforce"
        )
    field = fields["asset_class"]
    if field.type not in ("AssetClass", api.AssetClass):
        fail(f"OrderRequest.asset_class type annotation is {field.type!r}; expected AssetClass")
    expected_default = getattr(api.AssetClass, required_default)
    # StrEnum subclasses str, so `field.default == "EQUITY"` evaluates true
    # even when the literal string slipped through where the enum member was
    # expected. The default must be the AssetClass enum itself so type
    # checkers and downstream switch/case logic see the enum identity.
    if type(field.default) is not api.AssetClass or field.default is not expected_default:
        fail(
            f"OrderRequest.asset_class default is {field.default!r} "
            f"(type={type(field.default).__name__}); expected "
            f"AssetClass.{required_default} — equities are the Phase-1 "
            "baseline and the default must be the enum, not a bare string"
        )
    return (
        f"OrderRequest.asset_class: AssetClass = AssetClass.{required_default} "
        "field locked — every order carries the class the guard checks against"
    )


def check_subscribe_protocol_signature(config: dict, root: Path) -> str:
    block = contract_block(config)
    required_params = list(block["required_subscribe_params"])
    required_default = block["required_subscribe_default_asset_class"]
    api = _load_sdk_module(root)
    sig = inspect.signature(api.StrategyContext.subscribe)
    params = list(sig.parameters)
    if params != required_params:
        fail(
            f"StrategyContext.subscribe signature is {params!r}; expected "
            f"{required_params!r} — SRS-SDK-003 AC half-A requires the "
            "subscribe method to accept asset_class so analysis can span "
            "both equities and options"
        )
    ac_param = sig.parameters["asset_class"]
    if ac_param.annotation not in ("AssetClass", api.AssetClass):
        fail(
            f"StrategyContext.subscribe.asset_class annotation is "
            f"{ac_param.annotation!r}; expected AssetClass"
        )
    expected_default = getattr(api.AssetClass, required_default)
    # StrEnum subclasses str; bare-string slips would compare equal. Lock
    # the default to the AssetClass enum identity so type checkers see the
    # enum at the strategy-author surface (catches mutation case "literal
    # string default").
    if type(ac_param.default) is not api.AssetClass or ac_param.default is not expected_default:
        fail(
            f"StrategyContext.subscribe.asset_class default is "
            f"{ac_param.default!r} (type={type(ac_param.default).__name__}); "
            f"expected AssetClass.{required_default} — the default must be "
            "the enum (not a literal string) so type-checking catches "
            "misuse at the strategy-author surface"
        )
    doc = inspect.getdoc(api.StrategyContext.subscribe) or ""
    if "both equities and options" not in doc and "both asset classes" not in doc:
        fail(
            "StrategyContext.subscribe docstring no longer affirms both "
            "asset classes are subscribable for analysis regardless of "
            "tradable class — SRS-SDK-003 AC half-A would silently regress"
        )
    return (
        f"StrategyContext.subscribe({', '.join(required_params)}) accepts "
        f"asset_class: AssetClass = AssetClass.{required_default}; "
        "docstring affirms both-class analysis subscriptions per SRS-SDK-003 AC half-A"
    )


def check_order_protocol_docstring(config: dict, root: Path) -> str:
    block = contract_block(config)
    required_tokens = list(block["required_order_protocol_docstring_tokens"])
    api = _load_sdk_module(root)
    doc = inspect.getdoc(api.StrategyContext.order) or ""
    missing = [t for t in required_tokens if t not in doc]
    if missing:
        fail(
            f"StrategyContext.order docstring is missing required tokens "
            f"{missing} — the Protocol must publicly promise the guard "
            "to strategy authors (SRS-SDK-003 AC half-B)"
        )
    return (
        f"StrategyContext.order docstring names {required_tokens} — "
        "Protocol publicly commits to the asset-class guard"
    )


def check_assert_asset_class_helper(config: dict, root: Path) -> str:
    block = contract_block(config)
    required_helpers = list(block["required_helper_functions"])
    api = _load_sdk_module(root)
    for name in required_helpers:
        if not hasattr(api, name):
            fail(
                f"atp_strategy package is missing required helper "
                f"function {name!r} — concrete StrategyContext.order "
                "implementations must call the SDK-shipped guard rather "
                "than reimplementing the comparison"
            )
    helper = api.assert_asset_class
    if not callable(helper):
        fail("atp_strategy.assert_asset_class is not callable")
    sig = inspect.signature(helper)
    params = list(sig.parameters)
    if params != ["config", "request"]:
        fail(f"assert_asset_class signature is {params!r}; expected ['config', 'request']")
    # Behavioural runtime exercise — catches "mutate body to pass" and
    # "swap == for !=" regressions that pure-static scans cannot see.
    eq_cfg = api.StrategyConfig(strategy_id="s-eq", tradable_asset_class=api.AssetClass.EQUITY)
    op_cfg = api.StrategyConfig(strategy_id="s-op", tradable_asset_class=api.AssetClass.OPTION)
    eq_req = api.OrderRequest(
        symbol="AAPL",
        quantity=1,
        side=api.OrderSide.BUY,
        order_type=api.OrderType.MARKET,
        asset_class=api.AssetClass.EQUITY,
    )
    op_req = api.OrderRequest(
        symbol="SPY",
        quantity=1,
        side=api.OrderSide.BUY,
        order_type=api.OrderType.MARKET,
        asset_class=api.AssetClass.OPTION,
    )
    # Same-class: no raise
    try:
        helper(eq_cfg, eq_req)
    except api.AssetClassViolation as exc:
        fail(f"assert_asset_class raised on matching EQUITY/EQUITY: {exc!r}")
    try:
        helper(op_cfg, op_req)
    except api.AssetClassViolation as exc:
        fail(f"assert_asset_class raised on matching OPTION/OPTION: {exc!r}")
    # Mismatched: must raise with strategy_id + offending class
    for cfg, req, expected_id, expected_class in (
        (eq_cfg, op_req, "s-eq", "OPTION"),
        (op_cfg, eq_req, "s-op", "EQUITY"),
    ):
        try:
            helper(cfg, req)
        except api.AssetClassViolation as exc:
            message = str(exc)
            if expected_id not in message or expected_class not in message:
                fail(
                    f"assert_asset_class violation message does not name "
                    f"strategy_id={expected_id!r} and offending class "
                    f"{expected_class!r}: {message!r}"
                )
        else:
            fail(
                f"assert_asset_class did not raise on mismatched "
                f"{cfg.tradable_asset_class.value}/{req.asset_class.value} — "
                "SRS-SDK-003 AC half-B requires order rejection on the "
                "non-configured class"
            )
    return (
        "assert_asset_class(config, request) helper shipped and re-exported; "
        "raises AssetClassViolation on mismatch (EQUITY/OPTION, OPTION/EQUITY) "
        "and is silent on match (EQUITY/EQUITY, OPTION/OPTION) — SyRS SYS-5 "
        "single-tradable-class invariant enforced behaviourally"
    )


def check_asset_class_violation_export(config: dict, root: Path) -> str:
    block = contract_block(config)
    required_exports = set(block["required_exports"])
    api = _load_sdk_module(root)
    missing_exports = [name for name in required_exports if not hasattr(api, name)]
    if missing_exports:
        fail(f"atp_strategy package is missing required exports {missing_exports}")
    if not issubclass(api.AssetClassViolation, api.StrategyAPIError):
        fail(
            "AssetClassViolation must derive from StrategyAPIError so the "
            "structured-error contract (SyRS SYS-64) reaches user strategy "
            "code through the documented base class"
        )
    return (
        f"atp_strategy re-exports {sorted(required_exports)}; "
        "AssetClassViolation subclasses StrategyAPIError per SyRS SYS-64"
    )


# --------------------------------------------------------------------------- #
# Public assert helper used by the L3 mutation rig
# --------------------------------------------------------------------------- #


def assert_strategy_api_subscriptions_static(
    config: dict | None = None, root: Path = ROOT
) -> list[str]:
    """Run every subscriptions-contract check and return evidence strings.

    Raises ``StrategyApiSubscriptionsCheckError`` on the first failure.
    """
    config = config if config is not None else load_config(root)
    return [
        check_asset_class_enum_members(config, root),
        check_strategy_config_field(config, root),
        check_order_request_field(config, root),
        check_subscribe_protocol_signature(config, root),
        check_order_protocol_docstring(config, root),
        check_assert_asset_class_helper(config, root),
        check_asset_class_violation_export(config, root),
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
        evidence = assert_strategy_api_subscriptions_static(root=args.root)
    except StrategyApiSubscriptionsCheckError as exc:
        print(f"SRS-SDK-003 FAIL: {exc}", file=sys.stderr)
        return 1
    print("SRS-SDK-003 PASS — single tradable asset class invariant")
    for line in evidence:
        print(f"  * {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
