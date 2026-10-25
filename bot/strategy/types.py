from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class Candle:
    ts: int          # candle open time, ms since epoch (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def bullish(self) -> bool:
        return self.close > self.open

    @property
    def bearish(self) -> bool:
        return self.close < self.open


def validate_candles(candles: List[Candle], min_len: int = 1) -> List[str]:
    """Data-quality checks: ordering, gaps, impossible OHLC."""
    problems = []
    if len(candles) < min_len:
        problems.append(f"need at least {min_len} candles, have {len(candles)}")
    for i, c in enumerate(candles):
        if c.high < max(c.open, c.close, c.low) or c.low > min(c.open, c.close, c.high):
            problems.append(f"candle {i} has inconsistent OHLC")
            break
    for a, b in zip(candles, candles[1:]):
        if b.ts <= a.ts:
            problems.append("candles are not strictly increasing in time")
            break
    return problems
