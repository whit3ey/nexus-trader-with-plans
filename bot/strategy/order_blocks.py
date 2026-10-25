"""Deterministic order-block detection.

Bullish OB: the last bearish candle before a bullish displacement candle that
closes above the recent highs. Bearish OB is the mirror image.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Sequence

from .atr import atr_series
from .types import Candle


@dataclass
class OrderBlock:
    direction: str        # "bullish" | "bearish"
    high: float
    low: float
    timeframe: str
    origin_index: int
    origin_ts: int
    age: int
    status: str           # "fresh" | "mitigated" | "broken"
    distance: float

    def to_dict(self) -> Dict[str, object]:
        d = asdict(self)
        d["high"] = round(self.high, 4)
        d["low"] = round(self.low, 4)
        d["distance"] = round(self.distance, 4)
        return d


def detect_order_blocks(candles: Sequence[Candle], timeframe: str, atr_period: int = 14,
                        displacement_atr_mult: float = 1.2, structure_window: int = 5,
                        max_age: int = 80, include_dead: bool = False) -> List[OrderBlock]:
    atrs = atr_series(candles, atr_period)
    n = len(candles)
    if n < structure_window + 3:
        return []
    price = candles[-1].close
    blocks: List[OrderBlock] = []
    seen_origins = set()
    for i in range(structure_window + 1, n):
        atr = atrs[i]
        if atr is None:
            continue
        d = candles[i]
        window = candles[i - structure_window:i]
        if d.bullish and d.body >= displacement_atr_mult * atr and d.close > max(c.high for c in window):
            direction = "bullish"
            origin = next((j for j in range(i - 1, i - structure_window - 1, -1) if candles[j].bearish), None)
        elif d.bearish and d.body >= displacement_atr_mult * atr and d.close < min(c.low for c in window):
            direction = "bearish"
            origin = next((j for j in range(i - 1, i - structure_window - 1, -1) if candles[j].bullish), None)
        else:
            continue
        if origin is None or (origin, direction) in seen_origins:
            continue
        seen_origins.add((origin, direction))
        age = n - 1 - origin
        if age > max_age:
            continue
        o = candles[origin]
        hi, lo = o.high, o.low
        status = "fresh"
        for later in candles[i + 1:]:
            if direction == "bullish":
                if later.close < lo:
                    status = "broken"
                    break
                if later.low <= hi:
                    status = "mitigated"
            else:
                if later.close > hi:
                    status = "broken"
                    break
                if later.high >= lo:
                    status = "mitigated"
        if status != "fresh" and not include_dead:
            continue
        dist = 0.0 if lo <= price <= hi else min(abs(price - lo), abs(price - hi))
        blocks.append(OrderBlock(direction, hi, lo, timeframe, origin, o.ts, age, status, dist))
    return blocks


def nearest_order_block(blocks: Sequence[OrderBlock], direction: Optional[str] = None) -> Optional[OrderBlock]:
    pool = [b for b in blocks if direction is None or b.direction == direction]
    return min(pool, key=lambda b: (b.distance, b.age)) if pool else None
