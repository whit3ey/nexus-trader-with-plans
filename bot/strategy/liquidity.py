"""Liquidity levels (swings, equal highs/lows, session and range extremes) and sweeps."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from .market_structure import find_swings
from .types import Candle


def _day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def equal_levels(prices: Sequence[float], tolerance: float) -> List[float]:
    """Cluster prices within `tolerance`; clusters with >=2 members are 'equal' levels."""
    out: List[float] = []
    pts = sorted(prices)
    cluster: List[float] = []
    for p in pts:
        if cluster and p - cluster[-1] > tolerance:
            if len(cluster) >= 2:
                out.append(sum(cluster) / len(cluster))
            cluster = []
        cluster.append(p)
    if len(cluster) >= 2:
        out.append(sum(cluster) / len(cluster))
    return out


def detect_liquidity(candles: Sequence[Candle], atr: Optional[float], lookback: int = 3,
                     equal_tol_atr: float = 0.15, range_bars: int = 48) -> Dict[str, object]:
    n = len(candles)
    res: Dict[str, object] = {
        "swing_highs": [], "swing_lows": [], "equal_highs": [], "equal_lows": [],
        "prev_session_high": None, "prev_session_low": None,
        "range_high": None, "range_low": None,
    }
    if n < 2 * lookback + 2:
        return res
    swings = find_swings(candles, lookback)
    sh = [s.price for s in swings if s.kind == "high"][-6:]
    sl = [s.price for s in swings if s.kind == "low"][-6:]
    res["swing_highs"] = [round(x, 4) for x in sh]
    res["swing_lows"] = [round(x, 4) for x in sl]
    tol = (atr or 0.0) * equal_tol_atr
    if tol > 0:
        res["equal_highs"] = [round(x, 4) for x in equal_levels(sh, tol)]
        res["equal_lows"] = [round(x, 4) for x in equal_levels(sl, tol)]
    today = _day(candles[-1].ts)
    prev_days = sorted({_day(c.ts) for c in candles if _day(c.ts) < today})
    if prev_days:
        pd_ = prev_days[-1]
        day_c = [c for c in candles if _day(c.ts) == pd_]
        res["prev_session_high"] = max(c.high for c in day_c)
        res["prev_session_low"] = min(c.low for c in day_c)
    window = candles[-range_bars:]
    res["range_high"] = max(c.high for c in window)
    res["range_low"] = min(c.low for c in window)
    return res


def detect_sweep(candles: Sequence[Candle], liquidity: Dict[str, object], recent: int = 3) -> Dict[str, object]:
    """A sweep: within the last `recent` candles a wick exceeds a level but the candle CLOSES back inside.

    Sweep of highs  -> bearish setup hint ("sell_side" reversal), sweep of lows -> bullish hint.
    """
    out = {"swept": False, "side": None, "level": None, "direction_hint": None}
    if len(candles) < recent + 1:
        return out
    highs = [x for k in ("swing_highs", "equal_highs") for x in liquidity.get(k, [])]
    highs += [liquidity[k] for k in ("prev_session_high",) if liquidity.get(k) is not None]
    lows = [x for k in ("swing_lows", "equal_lows") for x in liquidity.get(k, [])]
    lows += [liquidity[k] for k in ("prev_session_low",) if liquidity.get(k) is not None]
    for c in reversed(candles[-recent:]):
        for lvl in sorted(highs, reverse=True):
            if c.high > lvl and c.close < lvl:
                return {"swept": True, "side": "highs", "level": lvl, "direction_hint": "SHORT"}
        for lvl in sorted(lows):
            if c.low < lvl and c.close > lvl:
                return {"swept": True, "side": "lows", "level": lvl, "direction_hint": "LONG"}
    return out
