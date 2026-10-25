"""Deterministic Fair Value Gap detection."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Sequence

from .atr import atr_series
from .types import Candle


@dataclass
class FVG:
    direction: str       # "bullish" | "bearish"
    upper: float
    lower: float
    timeframe: str
    index: int           # index of the 3rd (completing) candle
    ts: int
    age: int             # candles since formation
    filled: bool         # fully filled (price traded through the whole gap)
    fill_pct: float      # 0..1 how much of the gap has been traded into
    distance: float      # absolute distance from current price to nearest edge (0 if inside)
    size: float

    def to_dict(self) -> Dict[str, object]:
        d = asdict(self)
        for k in ("upper", "lower", "distance", "size", "fill_pct"):
            d[k] = round(d[k], 4)
        return d


def detect_fvgs(candles: Sequence[Candle], timeframe: str, atr_period: int = 14,
                min_atr_mult: float = 0.3, max_age: int = 60,
                include_filled: bool = False) -> List[FVG]:
    """Three-candle imbalance: c[i-2] and c[i] do not overlap.

    Gaps smaller than min_atr_mult x ATR (at the completing candle) are ignored.
    """
    atrs = atr_series(candles, atr_period)
    n = len(candles)
    if n < 3:
        return []
    price = candles[-1].close
    found: List[FVG] = []
    for i in range(2, n):
        a, c = candles[i - 2], candles[i]
        atr = atrs[i]
        if atr is None:
            continue
        if a.high < c.low:
            direction, lower, upper = "bullish", a.high, c.low
        elif a.low > c.high:
            direction, lower, upper = "bearish", c.high, a.low
        else:
            continue
        size = upper - lower
        if size < min_atr_mult * atr:
            continue
        age = n - 1 - i
        if age > max_age:
            continue
        # fill status uses candles after formation
        deepest = 0.0
        for later in candles[i + 1:]:
            if direction == "bullish":
                depth = (upper - max(later.low, lower)) / size if later.low < upper else 0.0
            else:
                depth = (min(later.high, upper) - lower) / size if later.high > lower else 0.0
            deepest = max(deepest, min(max(depth, 0.0), 1.0))
        filled = deepest >= 1.0 - 1e-12
        if filled and not include_filled:
            continue
        if lower <= price <= upper:
            dist = 0.0
        else:
            dist = min(abs(price - lower), abs(price - upper))
        found.append(FVG(direction, upper, lower, timeframe, i, c.ts, age, filled,
                         deepest, dist, size))
    return found


def nearest_fvg(fvgs: Sequence[FVG], direction: Optional[str] = None) -> Optional[FVG]:
    pool = [f for f in fvgs if direction is None or f.direction == direction]
    return min(pool, key=lambda f: (f.distance, f.age)) if pool else None
