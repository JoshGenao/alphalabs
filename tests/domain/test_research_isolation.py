"""L7 domain test: research environment no-live-order isolation (``SRS-RES-002``).

SRS-RES-002 / SyRS SYS-34c / NFR-S6: the Jupyter research environment can query
historical data, compute indicators, and render plots **without access to live order
submission**. This is the trading-domain safety invariant — a research notebook must
never be able to place a live order — so it lives at L7.

The invariant proved here:
  * the ``atp_research`` public surface exposes only read/compute/plot capabilities —
    no order-submission, cancellation, position-mutation, or credential name;
  * the read-only data handle exposes only ``get_bars`` / ``get_bars_range`` (reads);
  * ATP's only order-submission surface (``StrategyContext.order``) is an abstract
    ``typing.Protocol`` — it cannot be instantiated into a working submitter, and
    ``atp_research`` neither re-exports it nor any order request/handle type;
  * end to end, a notebook flows data -> indicator -> plot and nowhere touches an
    order path.

(The complementary CONTAINER-level sandbox — read-only mounts, no brokerage
credentials, no execution network reachable from the Jupyter process — is the
separate security control SRS-SEC-004; this test covers the API-surface guarantee.)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

import atp_research as ar  # noqa: E402
from atp_strategy import Bar, NormalizationMode  # noqa: E402
from atp_strategy.api import OrderHandle, OrderRequest, StrategyContext  # noqa: E402
from atp_strategy.store_history import StoreBackedHistoricalData  # noqa: E402

pytestmark = pytest.mark.domain

# Tokens that would betray a trading / credential capability leaking into the
# research surface. None of the legitimate research names (data query, indicators,
# plotting) contain any of these.
_FORBIDDEN_TOKENS = (
    "order",
    "submit",
    "cancel",
    "broker",
    "credential",
    "secret",
    "trade",
    "position",
    "execute",
    "liquidat",
    "sell",
    "buy",
)

_OHLCV = {"open": 9950, "high": 10075, "low": 9910, "close": 10000, "volume": 100000}


def _public_names(obj: object) -> list[str]:
    return [name for name in dir(obj) if not name.startswith("_")]


def _has_forbidden_token(name: str) -> bool:
    lowered = name.lower()
    return any(token in lowered for token in _FORBIDDEN_TOKENS)


class _OneRecordRunner:
    """Minimal fake data007_query_cli returning a single equity bar."""

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        symbol = argv[argv.index("--symbol") + 1]
        resolution = argv[argv.index("--resolution") + 1]
        start = argv[argv.index("--start") + 1]
        end = argv[argv.index("--end") + 1]
        normalization = argv[argv.index("--normalization") + 1]
        fields = "\n".join(f"record.0.field.{k}:{v}" for k, v in _OHLCV.items())
        out = (
            f"symbol:{symbol}\nresolution:{resolution}\nstart:{start}\nend:{end}\n"
            f"kind:any\nnormalization:{normalization}\nmatch_count:1\n"
            f"record.0.event_ts:{start}\nrecord.0.option_contract:-\n{fields}\n"
        )
        return subprocess.CompletedProcess(argv, 0, out, "")


def test_public_surface_has_no_trading_or_credential_name() -> None:
    leaked = [name for name in ar.__all__ if _has_forbidden_token(name)]
    assert leaked == [], f"research surface leaks trading/credential names: {leaked}"
    # dir() (imported symbols / submodules) too — nothing order-ish is reachable.
    leaked_dir = [name for name in _public_names(ar) if _has_forbidden_token(name)]
    assert leaked_dir == [], f"research package exposes trading/credential attrs: {leaked_dir}"


def test_data_handle_is_read_only() -> None:
    handle = ar.open_historical_data(
        store_dir="/tmp/does-not-matter",
        query_binary="/tmp/fake",
        runner=_OneRecordRunner(),
    )
    assert isinstance(handle, StoreBackedHistoricalData)
    public = set(_public_names(handle))
    # The read surface is exactly get_bars / get_bars_range; nothing writes or trades.
    assert public == {"get_bars", "get_bars_range"}, public
    for capability in ("order", "submit", "cancel", "place_order", "positions", "account"):
        assert not hasattr(handle, capability), capability


def test_atp_research_does_not_reexport_order_surface() -> None:
    exported = set(ar.__all__)
    for order_symbol in ("OrderRequest", "OrderHandle", "StrategyContext", "order", "cancel"):
        assert order_symbol not in exported
    # And the concrete objects are not smuggled in under another name.
    order_types = {OrderRequest, OrderHandle, StrategyContext}
    for name in _public_names(ar):
        assert getattr(ar, name) not in order_types, name


def test_order_submission_surface_is_an_uninstantiable_protocol() -> None:
    # ATP's ONLY order-submission entry point is StrategyContext.order, and
    # StrategyContext is an abstract typing.Protocol: there is no concrete submitter
    # in the SDK a notebook could construct, and the orchestrator never wires one
    # into a notebook process.
    assert getattr(StrategyContext, "_is_protocol", False) is True
    assert hasattr(StrategyContext, "order")
    with pytest.raises(TypeError):
        StrategyContext()  # type: ignore[misc]


def test_notebook_flow_data_to_indicator_to_plot_touches_no_order_path() -> None:
    # A representative notebook cell: read history, compute an indicator, render it.
    handle = ar.open_historical_data(
        store_dir="/tmp/does-not-matter",
        query_binary="/tmp/fake",
        runner=_OneRecordRunner(),
    )
    bars = handle.get_bars("X", lookback=1, frequency="1d", normalization=NormalizationMode.RAW)
    assert len(bars) == 1 and isinstance(bars[0], Bar)

    more = [
        Bar("X", f"2026-02-{i + 1:02d}T00:00:00+00:00", c, c + 1, c - 1, c, 10)
        for i, c in enumerate([1.0, 2.0, 3.0])
    ]
    series = ar.compute_series(ar.SMA(period=2), more)
    fig = ar.plot_ohlc(more, indicators={"SMA2": series})
    # It renders — the full capability chain works with no trading surface in reach.
    import io

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png")
    assert buffer.getvalue().startswith(b"\x89PNG")
