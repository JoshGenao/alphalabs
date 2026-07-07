"""L3 contract — the SRS-SDK-007 consolidation surface (Python side of the drift guard).

Pins the shape ``tools/strategy_api_check.py:check_sdk_007`` and the strategy authors depend
on, so a rename / signature drift / dropped export fails here rather than at runtime:

* ``TimeBarConsolidator`` and ``consolidate_bars`` are exported from the ``atp_strategy`` facade;
* ``TimeBarConsolidator`` structurally satisfies the ``BarConsolidator`` Protocol
  (``runtime_checkable``) and carries the streaming ``update`` / ``flush`` surface;
* ``consolidate_bars`` / ``TimeBarConsolidator.consolidate`` keep their declared signatures;
* the produced period set is exactly the ``{5m, 15m, 1h, 1d}`` the ``StrategyContext.consolidate``
  docstring advertises (doc ↔ implementation agreement).
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "python"))

import atp_strategy as sdk  # noqa: E402
from atp_strategy import BarConsolidator, TimeBarConsolidator, consolidate_bars  # noqa: E402
from atp_strategy.api import StrategyContext  # noqa: E402
from atp_strategy.resample import SUPPORTED_PERIODS  # noqa: E402

pytestmark = pytest.mark.contract


def test_facade_exports_the_consolidation_primitives() -> None:
    assert "TimeBarConsolidator" in sdk.__all__
    assert "consolidate_bars" in sdk.__all__
    assert sdk.TimeBarConsolidator is TimeBarConsolidator
    assert sdk.consolidate_bars is consolidate_bars


def test_time_bar_consolidator_satisfies_the_protocol() -> None:
    inst = TimeBarConsolidator("5m")
    assert isinstance(inst, BarConsolidator)  # runtime_checkable structural conformance
    for member in ("consolidate", "update", "flush", "period"):
        assert hasattr(inst, member), member


def test_context_consolidate_surface_is_present() -> None:
    # The same surface check_sdk_007 pins: BarConsolidator exposes consolidate(); the context
    # exposes a callable consolidate().
    assert hasattr(BarConsolidator, "consolidate")
    assert callable(getattr(StrategyContext, "consolidate", None))


def test_consolidate_bars_signature() -> None:
    params = list(inspect.signature(consolidate_bars).parameters)
    assert params == ["bars", "period"]


def test_protocol_consolidate_signature_is_source_symbol_and_keyword_period() -> None:
    sig = inspect.signature(TimeBarConsolidator.consolidate)
    params = sig.parameters
    assert list(params) == ["self", "source_symbol", "period"]
    assert params["period"].kind is inspect.Parameter.KEYWORD_ONLY


def test_supported_periods_match_documented_set() -> None:
    assert set(SUPPORTED_PERIODS) == {"5m", "15m", "1h", "1d"}
    # The StrategyContext.consolidate docstring must advertise exactly these periods — the
    # repo is doc-drift sensitive, so pin doc ↔ implementation agreement here.
    doc = StrategyContext.consolidate.__doc__ or ""
    for period in SUPPORTED_PERIODS:
        assert f'"{period}"' in doc, period


def test_no_execution_mode_leak_in_public_names() -> None:
    # AC-14: the consolidation surface must not branch on live/paper (no mode-named params).
    forbidden = {"live", "paper", "mode", "is_live", "simulated"}
    for func in (consolidate_bars, TimeBarConsolidator.update, TimeBarConsolidator.consolidate):
        assert not (set(inspect.signature(func).parameters) & forbidden), func
