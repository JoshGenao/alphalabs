"""Bar replay must be deterministic.

Same input bars + same strategy seed must produce a byte-identical trade log.
This catches hidden time/RNG dependencies that break backtest reproducibility.
"""
from __future__ import annotations

try:
    import pytest
except ImportError:
    pass
else:
    pytestmark = [pytest.mark.domain]

    @pytest.mark.skip(reason="pending implementation of internal simulation engine")
    def test_two_replays_produce_identical_trade_logs() -> None:
        raise NotImplementedError(
            "Stub: run the same strategy twice over a fixed bar sequence with "
            "seed=42; assert sha256(trade_log_a) == sha256(trade_log_b)."
        )
