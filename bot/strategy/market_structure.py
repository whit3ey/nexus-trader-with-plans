"""Swing detection, HH/HL/LH/LL structure, trend, BOS and CHoCH."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .types import Candle


@dataclass(frozen=True)
class Swing:
    index: int
    ts: int
    price: float
    kind: str  # "high" | "low"


def find_swings(candles: Sequence[Candle], lookback: int = 3) -> List[Swing]:
    """Fractal pivots: a high higher than `lookback` candles on both sides (strict)."""
    swings: List[Swing] = []
    n = len(candles)
    for i in range(lookback, n - lookback):
        hi, lo = candles[i].high, candles[i].low
        left = candles[i - lookback:i]
        right = candles[i + 1:i + 1 + lookback]
        if all(hi > c.high for c in left) and all(hi >= c.high for c in right):
            swings.append(Swing(i, candles[i].ts, hi, "high"))
        if all(lo < c.low for c in left) and all(lo <= c.low for c in right):
            swings.append(Swing(i, candles[i].ts, lo, "low"))
    swings.sort(key=lambda s: s.index)
    return swings


def classify_structure(swings: Sequence[Swing]) -> Dict[str, object]:
    """Compare the last two swing highs and lows."""
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    res: Dict[str, object] = {"structure": "UNCLEAR", "trend": "neutral",
                              "last_swing_high": None, "last_swing_low": None}
    if highs:
        res["last_swing_high"] = highs[-1].price
    if lows:
        res["last_swing_low"] = lows[-1].price
    if len(highs) < 2 or len(lows) < 2:
        return res
    hh = highs[-1].price > highs[-2].price
    hl = lows[-1].price > lows[-2].price
    lh = highs[-1].price < highs[-2].price
    ll = lows[-1].price < lows[-2].price
    if hh and hl:
        res.update(structure="HH_HL", trend="bullish")
    elif lh and ll:
        res.update(structure="LH_LL", trend="bearish")
    elif hh and ll:
        res.update(structure="EXPANDING", trend="neutral")
    elif lh and hl:
        res.update(structure="CONTRACTING", trend="neutral")
    return res


def detect_bos_choch(candles: Sequence[Candle], swings: Sequence[Swing], trend: str) -> Dict[str, object]:
    """Break of structure (continuation) / change of character (reversal) on the latest CLOSE.

    Only swings that are already confirmed (before the last candle) count.
    """
    out = {"bos": None, "choch": None, "level": None}
    if len(candles) < 2:
        return out
    last = candles[-1]
    prev_highs = [s for s in swings if s.kind == "high" and s.index < len(candles) - 1]
    prev_lows = [s for s in swings if s.kind == "low" and s.index < len(candles) - 1]
    if prev_highs and last.close > prev_highs[-1].price and candles[-2].close <= prev_highs[-1].price:
        kind = "bos" if trend == "bullish" else "choch"
        out[kind] = "bullish"
        out["level"] = prev_highs[-1].price
    elif prev_lows and last.close < prev_lows[-1].price and candles[-2].close >= prev_lows[-1].price:
        kind = "bos" if trend == "bearish" else "choch"
        out[kind] = "bearish"
        out["level"] = prev_lows[-1].price
    return out


def analyse_structure(candles: Sequence[Candle], lookback: int = 3) -> Dict[str, object]:
    swings = find_swings(candles, lookback)
    info = classify_structure(swings)
    info.update(detect_bos_choch(candles, swings, str(info["trend"])))
    info["swing_count"] = len(swings)
    return info
