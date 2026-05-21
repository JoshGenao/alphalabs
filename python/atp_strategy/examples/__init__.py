"""Runnable canonical strategy examples for the Python Strategy API.

SRS trace: ``SRS-SDK-009`` (SyRS ``NFR-U2``, StRS ``SN-1.01``, ``C-1``).

This subpackage ships self-contained ``Strategy`` subclasses that
demonstrate every documented capability of the SDK end-to-end so a
Python-proficient trader can clone an example, edit it, and author a
new strategy without ever opening ``python/atp_strategy/api.py`` or
any other internal file.

Each example file imports ONLY from ``atp_strategy`` (the package
facade) and the Python standard library. Deep-path imports
(``from atp_strategy.api import …``) are intentionally not used —
the public package surface is the documented contract.

Each example file is also runnable as ``python -m
atp_strategy.examples.<name>``: the ``__main__`` block constructs a
minimal in-process dispatcher (``_HarnessContext`` /
``_HarnessHistory``) and walks the strategy through warm-up + a
handful of executable bars + a sample order event. The harness is a
zero-dependency stub for local verification only; the live runtime
delivers the same callback surface from real IB Gateway / paper
simulation paths per ``SRS-SDK-001`` ``AC-14``.

Modules:

* :mod:`atp_strategy.examples.hello` — minimal subscribe-and-log
  strategy that proves SDK installation.
* :mod:`atp_strategy.examples.sma_crossover` — canonical 200-bar
  warm-up + fast/slow SMA crossover + scheduled flatten.
* :mod:`atp_strategy.examples.dual_asset_analytics` —
  equity-tradable strategy that subscribes to both EQUITY and
  OPTION data for analysis (single-tradable-asset invariant per
  ``SRS-SDK-003``).
"""

__all__: list[str] = []
