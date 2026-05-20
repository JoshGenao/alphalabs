#!/usr/bin/env python3
"""Contract evidence script for SRS-SDK-005 (warm-up mechanism).

Verifies that the Python Strategy SDK exposes the warm-up surface every
concrete dispatcher (live IB execution per ``SRS-EXE-001``, internal
paper simulation per ``SRS-SIM-001``, backtest engine per
``SRS-BT-001``) must drive before delivering the first executable bar:

* :class:`atp_strategy.api.WarmupState` enum exposes exactly the
  three lifecycle members ``PENDING``, ``IN_PROGRESS``, ``COMPLETE``.
* :class:`atp_strategy.api.WarmupController` exposes a ``run()``
  method and ``state`` / ``warmup_bars`` / ``bars_replayed`` /
  ``symbols_replayed`` introspection properties.
* A shipped :func:`atp_strategy.api.assert_warmup_complete` helper,
  re-exported by the ``atp_strategy`` package, raises
  :class:`atp_strategy.api.WarmupNotComplete` while warm-up has not
  reached :attr:`WarmupState.COMPLETE`, and is silent once complete.
* ``Strategy.on_warmup_complete`` docstring publicly commits to the
  ``WarmupController`` / ``WarmupState`` / ``assert_warmup_complete``
  surface and to the ``SRS-SDK-005`` AC ordering (so the same surface
  is used in live and paper modes per ``SRS-SDK-001`` ``AC-14``).

The behavioural exercise in :func:`check_warmup_controller_lifecycle`
builds a synthetic dispatcher in-process and walks the AC ordering
end-to-end: a 200-bar historical replay is delivered to ``on_bar``
through the controller before the executable gate flips, an SMA(200)
in the strategy is :pyattr:`SMA.is_ready` after warm-up, and
:func:`assert_warmup_complete` raises until ``state`` is
:attr:`WarmupState.COMPLETE`. The L7 domain test in
``tests/domain/test_warmup_replay.py`` is the matching ``safety:paired``
diff covering the same AC.

Mirrors the PASS/FAIL output style of
``tools/strategy_api_order_events_check.py``.

Invoke:
    python3 tools/strategy_api_warmup_check.py
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class StrategyApiWarmupCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise StrategyApiWarmupCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "strategy_api_warmup_contract" not in config:
        fail("architecture metadata is missing strategy_api_warmup_contract")
    return config["strategy_api_warmup_contract"]


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
    except Exception as exc:  # pragma: no cover — surfaces as a warmup fail
        fail(f"failed to import atp_strategy from {python_root}: {exc!r}")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_warmup_state_members(config: dict, root: Path) -> str:
    block = contract_block(config)
    required = list(block["required_state_machine_members"])
    api = _load_sdk_module(root)
    members = {m.name for m in api.WarmupState}
    if set(members) != set(required):
        fail(
            f"WarmupState members are {sorted(members)}; expected exactly "
            f"{sorted(required)} — the SRS-SDK-005 lifecycle is the cross-"
            "language source of truth for live/paper/backtest dispatchers"
        )
    return f"WarmupState = {sorted(required)} — three-state lifecycle locked"


def check_warmup_controller_shape(config: dict, root: Path) -> str:
    block = contract_block(config)
    required_methods = list(block["required_controller_methods"])
    required_props = list(block["required_controller_properties"])
    api = _load_sdk_module(root)
    controller_cls = api.WarmupController
    for name in required_methods:
        attr = getattr(controller_cls, name, None)
        if attr is None or not callable(attr):
            fail(
                f"WarmupController is missing required method {name!r} — "
                "dispatchers cannot drive warm-up without it"
            )
    for name in required_props:
        attr = getattr(controller_cls, name, None)
        if not isinstance(attr, property):
            fail(
                f"WarmupController.{name} must be a property — dispatchers "
                "rely on read-only introspection of warm-up progress"
            )
    return f"WarmupController methods {required_methods} + properties {required_props} present"


def check_warmup_exports(config: dict, root: Path) -> str:
    block = contract_block(config)
    required_exports = list(block["required_exports"])
    api = _load_sdk_module(root)
    missing = [name for name in required_exports if not hasattr(api, name)]
    if missing:
        fail(f"atp_strategy package is missing required warm-up exports {missing}")
    if not issubclass(api.WarmupNotComplete, api.StrategyAPIError):
        fail(
            "WarmupNotComplete must derive from StrategyAPIError so the "
            "structured-error contract (SyRS SYS-64) reaches user "
            "strategy code through the documented base class"
        )
    if not issubclass(api.WarmupController, object) or not inspect.isclass(api.WarmupController):
        fail("WarmupController must be a class")
    return (
        f"atp_strategy re-exports {sorted(required_exports)}; "
        "WarmupNotComplete subclasses StrategyAPIError per SyRS SYS-64"
    )


def check_on_warmup_complete_callback(config: dict, root: Path) -> str:
    block = contract_block(config)
    required_tokens = list(block["required_on_warmup_complete_docstring_tokens"])
    api = _load_sdk_module(root)
    callback = getattr(api.Strategy, "on_warmup_complete", None)
    if not callable(callback):
        fail("Strategy.on_warmup_complete is not callable")
    sig = inspect.signature(callback)
    params = list(sig.parameters)
    if params != ["self", "context"]:
        fail(
            f"Strategy.on_warmup_complete signature is {params!r}; "
            "expected ['self', 'context'] — SRS-SDK-005 user-facing surface"
        )
    doc = inspect.getdoc(callback) or ""
    missing = [t for t in required_tokens if t not in doc]
    if missing:
        fail(
            f"Strategy.on_warmup_complete docstring is missing required "
            f"tokens {missing} — concrete dispatchers must read the "
            "controller / state / guard surface from the documented "
            "callback so live and paper modes share the same SDK contract"
        )
    return (
        "Strategy.on_warmup_complete(self, context) signature locked; "
        f"docstring names {required_tokens}"
    )


def _build_reference_setup(api: object, *, warmup_bars: int = 200):
    """Construct a minimal in-process strategy + history + ctx for the behavioural check."""

    Bar = api.Bar
    Strategy = api.Strategy
    AssetClass = api.AssetClass
    SMA = api.SMA

    class _Strat(Strategy):
        warmup_bars = 0  # populated below via config

        def __init__(self) -> None:
            self.bars: list[Bar] = []
            self.executable_bars: list[Bar] = []
            self.warmup_complete_calls: int = 0
            self.warmup_complete_at_index: int | None = None
            self.sma = SMA(period=warmup_bars)

        def on_bar(self, context, bar):  # type: ignore[override]
            self.bars.append(bar)
            self.sma.update(bar)

        def on_warmup_complete(self, context):  # type: ignore[override]
            self.warmup_complete_calls += 1
            self.warmup_complete_at_index = len(self.bars)

    class _History:
        def __init__(self, bars: list) -> None:
            self.bars = bars
            self.calls: list[tuple[str, int]] = []

        def get_bars(
            self,
            symbol,
            *,
            lookback,
            frequency="1m",
            end=None,
            asset_class=AssetClass.EQUITY,
            normalization=None,
        ):
            self.calls.append((symbol, lookback))
            return list(self.bars[:lookback])

    class _Ctx:
        def __init__(self, config) -> None:
            self.config = config

    config = api.StrategyConfig(
        strategy_id="s1",
        tradable_asset_class=AssetClass.EQUITY,
        warmup_bars=warmup_bars,
    )
    strategy = _Strat()
    bars = [
        Bar(
            "AAPL",
            f"2026-04-{(i // 24) + 1:02d}T{i % 24:02d}:00:00Z",
            100.0 + i,
            101.0 + i,
            99.0 + i,
            100.5 + i,
            1000 + i,
        )
        for i in range(warmup_bars + 5)
    ]
    history = _History(bars[:warmup_bars])
    ctx = _Ctx(config)
    return strategy, ctx, history, bars, config


def check_warmup_controller_lifecycle(config: dict, root: Path) -> str:
    block = contract_block(config)
    canonical = int(block["required_warmup_bars_canonical"])
    if canonical != 200:
        fail(
            f"required_warmup_bars_canonical is {canonical}; SRS-SDK-005 AC + "
            "StRS SC-18 both pin the canonical example at 200 bars"
        )
    api = _load_sdk_module(root)
    strategy, ctx, history, all_bars, _ = _build_reference_setup(api, warmup_bars=canonical)

    controller = api.WarmupController(
        strategy=strategy,
        context=ctx,
        history=history,
        subscriptions=[("AAPL", api.AssetClass.EQUITY)],
    )

    # Initial state — PENDING, gate closed.
    if controller.state is not api.WarmupState.PENDING:
        fail(
            f"WarmupController.state at construction is {controller.state!r}; "
            "expected WarmupState.PENDING — dispatchers rely on the gate "
            "being closed before run()"
        )
    if controller.warmup_bars != canonical:
        fail(
            f"WarmupController.warmup_bars is {controller.warmup_bars}; "
            f"expected {canonical} (read from StrategyConfig.warmup_bars)"
        )
    try:
        api.assert_warmup_complete(controller.state)
    except api.WarmupNotComplete:
        pass
    else:
        fail(
            "assert_warmup_complete did not raise on PENDING state — the "
            "SDK guard must close the executable gate until warm-up "
            "transitions to COMPLETE"
        )

    # Run the warm-up replay.
    controller.run()

    # Post-state — COMPLETE, exactly canonical historical bars, gate open.
    if controller.state is not api.WarmupState.COMPLETE:
        fail(
            f"WarmupController.state after run() is {controller.state!r}; "
            "expected WarmupState.COMPLETE"
        )
    if controller.bars_replayed != canonical:
        fail(
            f"WarmupController.bars_replayed is {controller.bars_replayed}; "
            f"expected {canonical} historical bars per subscribed symbol"
        )
    if controller.symbols_replayed != ("AAPL",):
        fail(
            f"WarmupController.symbols_replayed is {controller.symbols_replayed!r}; "
            "expected ('AAPL',) in subscription order"
        )
    if len(strategy.bars) != canonical:
        fail(
            f"Strategy.on_bar received {len(strategy.bars)} bars during "
            f"warm-up; expected exactly {canonical} (SRS-SDK-005 AC: a "
            "strategy configured with a 200-bar warm-up receives "
            "historical bars before the first executable bar)"
        )
    if strategy.warmup_complete_calls != 1:
        fail(
            f"Strategy.on_warmup_complete fired "
            f"{strategy.warmup_complete_calls} time(s); expected exactly 1 "
            "(the lifecycle callback must fire once at the warmup boundary)"
        )
    if strategy.warmup_complete_at_index != canonical:
        fail(
            f"Strategy.on_warmup_complete fired after {strategy.warmup_complete_at_index} "
            f"bars; expected after {canonical} (the AC requires the callback "
            "AFTER historical replay and BEFORE the first executable bar)"
        )
    if not strategy.sma.is_ready:
        fail(
            "Strategy.sma.is_ready is False after warm-up; SRS-SDK-005 AC "
            "requires indicator buffers to be initialized before the "
            "first executable bar can be processed"
        )
    if strategy.sma.value is None:
        fail(
            "Strategy.sma.value is None after warm-up; the indicator must "
            "produce a real value once the rolling window is full"
        )
    try:
        api.assert_warmup_complete(controller.state)
    except api.WarmupNotComplete as exc:
        fail(f"assert_warmup_complete raised on COMPLETE state: {exc!r}")

    # Re-running raises rather than silently double-replaying.
    try:
        controller.run()
    except RuntimeError:
        pass
    else:
        fail(
            "WarmupController.run() on a COMPLETE controller must raise "
            "RuntimeError — restart re-execution (NFR-R3) constructs a "
            "fresh controller rather than rewinding state"
        )

    # IN_PROGRESS guard (synthetic state transition).
    fresh = api.WarmupController(
        strategy=strategy,
        context=ctx,
        history=history,
        subscriptions=[],
        warmup_bars=canonical,
    )
    object.__setattr__(fresh, "_state", api.WarmupState.IN_PROGRESS)
    try:
        api.assert_warmup_complete(fresh.state)
    except api.WarmupNotComplete:
        pass
    else:
        fail(
            "assert_warmup_complete did not raise on IN_PROGRESS state — "
            "the executable gate must stay closed during replay"
        )

    # Degenerate path — warmup_bars == 0 fires the callback without history.
    zero_strategy, zero_ctx, zero_history, _, _ = _build_reference_setup(api, warmup_bars=canonical)
    zero_history.bars = []
    zero_controller = api.WarmupController(
        strategy=zero_strategy,
        context=zero_ctx,
        history=zero_history,
        subscriptions=[("AAPL", api.AssetClass.EQUITY)],
        warmup_bars=0,
    )
    zero_controller.run()
    if zero_controller.state is not api.WarmupState.COMPLETE:
        fail(
            "warmup_bars=0 must transition to COMPLETE on run() — the "
            "callback contract still fires so dispatchers do not need "
            "a special case"
        )
    if zero_strategy.warmup_complete_calls != 1:
        fail("warmup_bars=0 must still fire on_warmup_complete exactly once")
    if zero_history.calls:
        fail("warmup_bars=0 must not call HistoricalData.get_bars — there is no history to replay")
    if zero_controller.bars_replayed != 0:
        fail("warmup_bars=0 must record bars_replayed == 0 — sanity check on the counter")

    # Short-history fail-closed guard. SRS-SDK-005 AC requires indicator
    # buffers to be initialised before the first executable bar; if
    # HistoricalData.get_bars returns fewer than warmup_bars bars the
    # controller must refuse to open the executable gate.
    short_strategy, short_ctx, short_history, _, _ = _build_reference_setup(
        api, warmup_bars=canonical
    )
    short_history.bars = short_history.bars[: canonical - 1]
    short_controller = api.WarmupController(
        strategy=short_strategy,
        context=short_ctx,
        history=short_history,
        subscriptions=[("AAPL", api.AssetClass.EQUITY)],
    )
    try:
        short_controller.run()
    except api.WarmupNotComplete as exc:
        if "short" not in str(exc):
            fail(
                "WarmupController.run() on a short historical replay raised "
                f"WarmupNotComplete but the message does not signal the "
                f"shortfall ({str(exc)!r})"
            )
    except Exception as exc:
        fail(
            f"WarmupController.run() on a short historical replay raised "
            f"{type(exc).__name__} (expected WarmupNotComplete) — schema "
            "drift would let dispatchers silently open the executable gate"
        )
    else:
        fail(
            "WarmupController.run() did not raise on a short historical "
            f"replay ({canonical - 1} bars vs. {canonical} requested) — "
            "SRS-SDK-005 AC requires indicator buffers to be initialised "
            "before the first executable bar, so the controller must "
            "refuse to open the executable gate on insufficient history"
        )
    if short_controller.state is not api.WarmupState.IN_PROGRESS:
        fail(
            f"WarmupController.state after short replay is "
            f"{short_controller.state!r}; expected WarmupState.IN_PROGRESS "
            "(the gate must stay closed)"
        )
    if short_strategy.warmup_complete_calls != 0:
        fail(
            "Strategy.on_warmup_complete fired on a short historical "
            "replay — the callback must NOT fire when warm-up cannot "
            "complete; production dispatchers gate trading on the "
            "callback firing"
        )
    try:
        api.assert_warmup_complete(short_controller.state)
    except api.WarmupNotComplete:
        pass
    else:
        fail(
            "assert_warmup_complete did not raise on IN_PROGRESS state "
            "after a short historical replay — the executable gate must "
            "remain closed"
        )

    # Construction guards — non-int / negative values raise structurally.
    failures = (
        ("warmup_bars=-1", {"warmup_bars": -1}),
        ("warmup_bars=1.5", {"warmup_bars": 1.5}),
        ("warmup_bars=True", {"warmup_bars": True}),
        ("frequency=''", {"frequency": ""}),
    )
    for label, override in failures:
        try:
            api.WarmupController(
                strategy=strategy,
                context=ctx,
                history=history,
                subscriptions=[],
                **override,
            )
        except ValueError:
            pass
        except Exception as exc:
            fail(
                f"WarmupController construction with {label} raised "
                f"{type(exc).__name__} (expected ValueError) — bad-config "
                "errors must surface structurally at the boundary"
            )
        else:
            fail(
                f"WarmupController construction with {label} did not raise "
                "— bad config must not silently coerce"
            )

    return (
        f"WarmupController walks {sorted(m.name for m in api.WarmupState)} "
        f"end-to-end on a {canonical}-bar replay; on_bar receives all "
        f"{canonical} historical bars before the executable gate flips, "
        "SMA(200).is_ready == True at the boundary, on_warmup_complete "
        "fires exactly once, assert_warmup_complete raises on "
        "PENDING/IN_PROGRESS and is silent on COMPLETE, controller is "
        "fail-closed on a short historical replay (WarmupNotComplete "
        "raised, state stays IN_PROGRESS, on_warmup_complete suppressed) "
        "— SRS-SDK-005 AC behaviourally locked"
    )


# --------------------------------------------------------------------------- #
# Public assert helper used by the L3 mutation rig
# --------------------------------------------------------------------------- #


def assert_strategy_api_warmup_static(config: dict | None = None, root: Path = ROOT) -> list[str]:
    """Run every warm-up contract check and return evidence strings.

    Raises ``StrategyApiWarmupCheckError`` on the first failure.
    """
    config = config if config is not None else load_config(root)
    return [
        check_warmup_exports(config, root),
        check_warmup_state_members(config, root),
        check_warmup_controller_shape(config, root),
        check_on_warmup_complete_callback(config, root),
        check_warmup_controller_lifecycle(config, root),
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
        evidence = assert_strategy_api_warmup_static(root=args.root)
    except StrategyApiWarmupCheckError as exc:
        print(f"SRS-SDK-005 FAIL: {exc}", file=sys.stderr)
        return 1
    print("SRS-SDK-005 PASS — Python Strategy API warm-up mechanism")
    for line in evidence:
        print(f"  * {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
