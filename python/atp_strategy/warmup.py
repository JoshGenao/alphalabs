"""Warm-up SDK surface for the Python Strategy API (``SRS-SDK-005``).

The warm-up mechanism feeds historical bars into a strategy before the
first **executable** bar arrives, so indicator buffers, rolling windows,
and any user-side state are initialised before the first trading signal
can be generated. SyRS ``SYS-8`` names this mechanism; StRS ``SN-1.23``
and ``SC-18`` (200-bar warm-up) are the stakeholder-level claims.

What this module ships
----------------------
This module is the **SDK surface** that every concrete dispatcher
(live IB execution per ``SRS-EXE-001``, internal paper simulation per
``SRS-SIM-001``, and the backtest engine per ``SRS-BT-001``) must drive
before delivering executable bars to user code:

* :class:`WarmupState` — the three-state lifecycle every dispatcher walks
  before, during, and after warm-up.
* :class:`WarmupController` — a reusable controller that pulls the
  configured number of historical bars per subscribed symbol from a
  :class:`atp_strategy.api.HistoricalData` source, replays them through
  ``Strategy.on_bar`` in chronological order, then invokes
  ``Strategy.on_warmup_complete`` exactly once.
* :func:`assert_warmup_complete` — a guard concrete dispatchers call at
  the boundary where order submission or executable-bar delivery would
  start. Raises :class:`atp_strategy.api.WarmupNotComplete` while
  warm-up is still pending or in progress so the AC-required ordering
  (historical bars before the first executable bar) cannot be silently
  violated.

The same surface is used in live IB execution and internal paper
simulation per ``SRS-SDK-001`` ``AC-14``. Concrete production
dispatchers that bypass the SDK helper here and re-implement warm-up
locally would silently drift the contract; the L7 reference dispatcher
in ``tests/domain/test_warmup_replay.py`` is the executable proof that
the SDK-surface is sufficient end-to-end against the SRS-SDK-005 AC.

Cross-language note
-------------------
Per ``AGENTS.md`` dependency direction, Rust core runtime services may
not depend on the Python SDK. The cross-language source of truth for
the warm-up state machine, the executable-bar gate, and the AC numbers
(``required_warmup_bars_canonical``, etc.) lives in
``architecture/runtime_services.json`` under
``strategy_api_warmup_contract``. Rust dispatchers re-implement the
same ordering and gate locally; the contract evidence script
``tools/strategy_api_warmup_check.py`` is the parity check.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import TYPE_CHECKING

from .api import WarmupNotComplete

if TYPE_CHECKING:
    from .api import HistoricalData, Strategy, StrategyContext


class WarmupState(StrEnum):
    """The three states every warm-up walk passes through.

    A dispatcher constructs the controller in :attr:`PENDING`, transitions
    to :attr:`IN_PROGRESS` for the duration of historical-bar replay, and
    settles in :attr:`COMPLETE` once :meth:`atp_strategy.api.Strategy.on_warmup_complete`
    has been invoked. The AC ordering rule (historical bars before any
    executable bar) is enforced by gating executable delivery on
    :attr:`COMPLETE`.

    Example:
        >>> WarmupState.COMPLETE
        <WarmupState.COMPLETE: 'COMPLETE'>
    """

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"


class WarmupController:
    """Drives the warm-up replay for a single strategy instance.

    Concrete dispatchers (live IB execution per ``SRS-EXE-001``, internal
    paper simulation per ``SRS-SIM-001``, the backtest engine per
    ``SRS-BT-001``) construct one controller per strategy container and
    invoke :meth:`run` once before any executable bar may be delivered.

    Replay order:

    1. State transitions ``PENDING → IN_PROGRESS``.
    2. For each ``(symbol, asset_class)`` in ``subscriptions`` the
       controller calls
       ``history.get_bars(symbol, lookback=warmup_bars, frequency=…)``
       and replays the returned bars through ``strategy.on_bar`` in
       chronological order (the historical-data interface contract
       guarantees ascending timestamps).
    3. State transitions ``IN_PROGRESS → COMPLETE`` and
       ``strategy.on_warmup_complete(ctx)`` is invoked exactly once.
    4. ``Strategy.on_bar`` for the first executable bar may now be
       called by the dispatcher.

    Symbol ordering is preserved from the iterable the dispatcher passes
    so the replay is deterministic. ``warmup_bars == 0`` is a degenerate
    case: the controller immediately transitions to :attr:`COMPLETE` and
    fires ``on_warmup_complete``; no history requests are issued.

    Example:
        >>> from atp_strategy.warmup import WarmupController, WarmupState
        >>> WarmupController.__init__.__name__  # construction is dispatcher-driven
        '__init__'
    """

    def __init__(
        self,
        *,
        strategy: Strategy,
        context: StrategyContext,
        history: HistoricalData,
        subscriptions: Iterable[tuple[str, object]],
        warmup_bars: int | None = None,
        frequency: str = "1m",
    ) -> None:
        if warmup_bars is None:
            warmup_bars = self._resolve_warmup_bars(strategy, context)
        if not isinstance(warmup_bars, int) or isinstance(warmup_bars, bool):
            raise ValueError(
                f"warmup_bars must be a non-negative int (got "
                f"{type(warmup_bars).__name__}={warmup_bars!r})"
            )
        if warmup_bars < 0:
            raise ValueError(f"warmup_bars must be a non-negative int (got {warmup_bars})")
        if not isinstance(frequency, str) or not frequency:
            raise ValueError(f"frequency must be a non-empty str (got {frequency!r})")
        self._strategy = strategy
        self._context = context
        self._history = history
        self._subscriptions: tuple[tuple[str, object], ...] = tuple(subscriptions)
        self._warmup_bars = warmup_bars
        self._frequency = frequency
        self._state: WarmupState = WarmupState.PENDING
        self._bars_replayed: int = 0
        self._symbols_replayed: list[str] = []

    @staticmethod
    def _resolve_warmup_bars(strategy: Strategy, context: StrategyContext) -> int:
        """Resolve warmup_bars from the StrategyContext.config or the Strategy class attr.

        ``StrategyConfig.warmup_bars`` is the authoritative source; the
        class-level ``Strategy.warmup_bars`` is the developer-friendly
        default when no orchestrator config is involved (tests, examples,
        and the L7 reference dispatcher exercise this path).
        """
        config = getattr(context, "config", None)
        if config is not None:
            value = getattr(config, "warmup_bars", None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return int(getattr(strategy, "warmup_bars", 0) or 0)

    @property
    def state(self) -> WarmupState:
        """Current lifecycle state."""
        return self._state

    @property
    def warmup_bars(self) -> int:
        """Configured number of historical bars per subscribed symbol."""
        return self._warmup_bars

    @property
    def bars_replayed(self) -> int:
        """Cumulative bars delivered to ``on_bar`` during warm-up so far."""
        return self._bars_replayed

    @property
    def symbols_replayed(self) -> tuple[str, ...]:
        """Symbols (in replay order) that have completed historical replay."""
        return tuple(self._symbols_replayed)

    def run(self) -> None:
        """Execute the warm-up replay end-to-end.

        Idempotent only in the trivial sense that a controller that has
        already reached :attr:`WarmupState.COMPLETE` raises rather than
        replaying again — the dispatcher must construct a fresh
        controller for any subsequent warm-up (e.g. on restart per
        ``NFR-R3``).
        """
        if self._state is WarmupState.COMPLETE:
            raise RuntimeError(
                "warm-up already complete; construct a fresh "
                "WarmupController for restart re-execution (NFR-R3)"
            )
        if self._state is WarmupState.IN_PROGRESS:
            raise RuntimeError(
                "warm-up already in progress; concurrent run() calls are "
                "not supported on a single controller"
            )
        self._state = WarmupState.IN_PROGRESS

        # Degenerate path: warmup_bars == 0 still walks the state machine
        # so the executable gate flips and ``on_warmup_complete`` fires
        # exactly once. Dispatchers may rely on the callback firing.
        try:
            if self._warmup_bars > 0:
                self._replay_subscriptions()
        except Exception:
            # Replay failure leaves the state in IN_PROGRESS so the
            # executable gate stays closed — production dispatchers can
            # then halt the container and surface the failure to the
            # operator rather than silently entering live trading with
            # uninitialised indicator buffers (the SDK-005 AC is a
            # safety property).
            raise

        self._state = WarmupState.COMPLETE
        self._strategy.on_warmup_complete(self._context)

    def _replay_subscriptions(self) -> None:
        for symbol, asset_class in self._subscriptions:
            bars = list(
                self._history.get_bars(
                    symbol,
                    lookback=self._warmup_bars,
                    frequency=self._frequency,
                    asset_class=asset_class,
                )
            )
            # Fail-closed shortfall guard. The SRS-SDK-005 AC requires
            # indicator buffers to be initialised before the first
            # executable bar; an N-period indicator needs at least N
            # bars to become ready. If history.get_bars returned fewer
            # than warmup_bars bars for any subscribed symbol, opening
            # the executable gate would leave the strategy trading on
            # an unready indicator. The controller refuses to advance:
            # the exception propagates out of run() while ``_state``
            # remains ``IN_PROGRESS`` so the executable gate
            # (``assert_warmup_complete``) stays closed. Production
            # dispatchers must halt the container and surface the
            # shortfall to the operator rather than retry; restart
            # re-execution (NFR-R3) constructs a fresh controller.
            if len(bars) < self._warmup_bars:
                raise WarmupNotComplete(
                    f"warm-up replay short by "
                    f"{self._warmup_bars - len(bars)} bars for {symbol!r}: "
                    f"requested {self._warmup_bars}, "
                    f"history.get_bars returned {len(bars)} — refusing to "
                    "open the executable gate because indicator buffers "
                    f"configured for {self._warmup_bars} bars would be "
                    "unready (SRS-SDK-005 AC: indicator buffers must be "
                    "initialised before the first executable bar)"
                )
            for bar in bars:
                self._strategy.on_bar(self._context, bar)
                self._bars_replayed += 1
            self._symbols_replayed.append(symbol)


def assert_warmup_complete(state: WarmupState | None) -> None:
    """Guard executable order submission / first executable bar on warm-up.

    Concrete dispatchers must call this at the boundary where executable
    bars or order submissions would start. Raises
    :class:`atp_strategy.api.WarmupNotComplete` if the controller has
    not yet transitioned to :attr:`WarmupState.COMPLETE`; that error is
    a :class:`atp_strategy.api.StrategyAPIError` subclass per SyRS
    ``SYS-64`` so user strategy code sees a structured error.

    Example:
        >>> from atp_strategy.warmup import assert_warmup_complete, WarmupState
        >>> assert_warmup_complete(WarmupState.COMPLETE)
    """
    if state is None or state is not WarmupState.COMPLETE:
        observed = state.value if isinstance(state, WarmupState) else state
        raise WarmupNotComplete(
            "warm-up has not completed (state="
            f"{observed!r}); executable order submission and the first "
            "executable bar are gated on WarmupState.COMPLETE per "
            "SRS-SDK-005 AC (historical bars must be replayed before "
            "the first executable bar so indicator buffers are "
            "initialised before any trading signal can be generated)"
        )


__all__ = [
    "WarmupController",
    "WarmupState",
    "assert_warmup_complete",
]
