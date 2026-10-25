"""Average True Range (Wilder smoothing) - deterministic volatility measure."""
from __future__ import annotations

from statistics import median
from typing import List, Optional, Sequence

from .types import Candle


def true_ranges(candles: Sequence[Candle]) -> List[float]:
    out = []
    for i, c in enumerate(candles):
        if i == 0:
            out.append(c.high - c.low)
        else:
            pc = candles[i - 1].close
            out.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return out


def atr_series(candles: Sequence[Candle], period: int = 14) -> List[Optional[float]]:
    """ATR per candle, None until `period` true ranges are available."""
    if period < 1:
        raise ValueError("period must be >= 1")
    tr = true_ranges(candles)
    out: List[Optional[float]] = [None] * len(candles)
    if len(tr) < period:
        return out
    a = sum(tr[:period]) / period
    out[period - 1] = a
    for i in range(period, len(tr)):
        a = (a * (period - 1) + tr[i]) / period
        out[i] = a
    return out


def latest_atr(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    s = atr_series(candles, period)
    return s[-1] if s else None


def volatility_ratio(candles: Sequence[Candle], period: int = 14, lookback: int = 100) -> Optional[float]:
    """Current ATR divided by the median ATR of the recent past (abnormal-volatility check)."""
    s = [x for x in atr_series(candles, period) if x is not None]
    if len(s) < 5:
        return None
    window = s[-lookback:]
    m = median(window)
    return s[-1] / m if m > 0 else None
