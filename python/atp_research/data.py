"""Read-only historical-data access for the Jupyter research environment (``SRS-RES-002``).

The research environment answers the SyRS ``SYS-34b`` requirement that the Jupyter
notebook have access to the system's historical data **via the unified data access
interface** (``SYS-27`` / ``SRS-DATA-007``). Rather than a second data path, this
module hands notebooks the exact same source-neutral, read-only binding every other
consumer (strategy code, backtests, factor jobs) uses:
:class:`atp_strategy.store_history.StoreBackedHistoricalData`.

No-live-order isolation (``SRS-RES-002`` / SyRS ``SYS-34c`` / ``NFR-S6``)
------------------------------------------------------------------------
The returned handle exposes ONLY the ``HistoricalData`` read surface
(``get_bars`` / ``get_bars_range``) over the durable market-data store. It has no
order-submission, cancellation, position-mutation, or brokerage-credential surface:
the notebook can *read* market history but cannot *trade*. (The container-level
guarantee that the Jupyter process runs with read-only data mounts and no brokerage
credentials or execution network is the SEPARATE security control ``SRS-SEC-004``;
this module provides the API surface, ``SRS-SEC-004`` provides the sandbox.)

Operator provisioning of the query binary
-----------------------------------------
The ``SRS-DATA-007`` binding answers queries by driving the cargo-built
``data007_query_cli`` Rust binary over a subprocess. In a repo checkout it defaults
to ``target/debug/data007_query_cli``; the **operator-supplied JupyterLab image**
(see ``docker/jupyter.Dockerfile`` — a Phase-1 stub; the production image and the
Docker Compose stack that mounts data + built binaries are ``SRS-ARCH-004``) must
therefore make that binary available. Point notebooks at it with the
``ATP_DATA_QUERY_BINARY`` environment variable (honored here) or the explicit
``query_binary=`` argument. If the binary is absent the FIRST query fails **closed**
with an actionable :class:`~atp_strategy.store_history.StoreQueryError` naming
``data007_query_cli`` — never a hang or a silent empty result. Wiring the built
binary into the live JupyterLab image is the deferred (serialized) integration step
that keeps ``SRS-RES-002`` at ``passes:false``.
"""

from __future__ import annotations

import os
from typing import Any

from atp_strategy.store_history import StoreBackedHistoricalData

__all__ = ["open_historical_data"]

#: Environment variable an operator sets in the JupyterLab image to point notebooks
#: at the bundled/mounted ``data007_query_cli`` binary without editing notebook code.
_QUERY_BINARY_ENV = "ATP_DATA_QUERY_BINARY"


def open_historical_data(
    *,
    store_dir: str | os.PathLike[str] | None = None,
    query_binary: str | os.PathLike[str] | None = None,
    **kwargs: Any,
) -> StoreBackedHistoricalData:
    """Open a **read-only** historical-data handle for notebook research.

    Thin factory over :class:`atp_strategy.store_history.StoreBackedHistoricalData`
    (the ``SRS-DATA-007`` unified-interface binding). A notebook queries by
    ``(symbol, resolution, date range)`` through ``get_bars`` / ``get_bars_range``
    **without naming a source provider** — the core ``SRS-DATA-007`` invariant.

    Args:
        store_dir: Directory holding the persisted market-data store. Falls back to
            the ``ATP_DATA_STORE_DIR`` config key; the binding fails closed with
            :class:`ValueError` if neither is set (it never reads an empty catalog).
        query_binary: Path to the cargo-built ``data007_query_cli``. When omitted it
            is resolved from the ``ATP_DATA_QUERY_BINARY`` environment variable (the
            operator's JupyterLab-image knob, see the module docstring), and only if
            that is unset does the binding fall back to its repo default
            ``target/debug/data007_query_cli``. An absent binary fails **closed** on
            the first query with an actionable
            :class:`~atp_strategy.store_history.StoreQueryError`, never a hang.
        **kwargs: Forwarded verbatim to
            :class:`~atp_strategy.store_history.StoreBackedHistoricalData`
            (``clock``, ``runner``, ``timeout``) — e.g. an injected ``runner`` for
            tests.

    Returns:
        A :class:`~atp_strategy.store_history.StoreBackedHistoricalData` — a
        read-only handle exposing ``get_bars`` / ``get_bars_range`` only. There is
        deliberately no order-submission or credential surface on it
        (``SRS-RES-002`` no-live-order isolation).
    """
    if query_binary is None:
        env_binary = os.environ.get(_QUERY_BINARY_ENV)
        if env_binary is not None and env_binary.strip():
            query_binary = env_binary
    return StoreBackedHistoricalData(store_dir=store_dir, query_binary=query_binary, **kwargs)
