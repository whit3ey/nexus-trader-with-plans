"""Combines all deterministic features into a compact snapshot and a pre-filter.

The AI is only consulted when `evaluate_setup` finds a candidate. The AI never
computes any of these values; it only receives them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .atr import latest_atr, volatility_ratio
from .fvg import detect_fvgs, nearest_fvg
from .liquidity import detect_liquidity, detect_sweep
from .market_structure import analyse_structure
from .order_blocks import detect_order_blocks, nearest_order_block
from .types import Candle, validate_candles


@dataclass
class Setup:
    has_setup: bool
    direction_hint: Optional[str] = None     # "LONG" | "SHORT"
    reasons: List[str] = field(default_factory=list)
    score: int = 0
    kind: Optional[str] = None               # "trigger" (price is at the zone) | "plan" (zone is still ahead)
    zone: Optional[Dict[str, Any]] = None    # the zone a PLAN limit must sit inside


def _zone_edges(zone: Dict[str, Any]) -> Optional[tuple]:
    if "lower" in zone and "upper" in zone:
        lo, hi = float(zone["lower"]), float(zone["upper"])
    elif "low" in zone and "high" in zone:
        lo, hi = float(zone["low"]), float(zone["high"])
    else:
        return None
    if not (lo > 0 and hi > lo):
        return None
    return lo, hi


def _is_fresh(zone: Dict[str, Any], max_fill: float) -> bool:
    """A zone is fresh only when the detector says so. Bare distance hints are not plans."""
    if "status" in zone:
        return zone.get("status") == "fresh"
    if zone.get("filled") is True:
        return False
    if "fill_pct" in zone and zone.get("fill_pct") is not None:
        return float(zone["fill_pct"]) <= max_fill
    return False


def _volume_state(candles: Sequence[Candle], period: int) -> str:
    if len(candles) < period + 1:
        return "unknown"
    avg = sum(c.volume for c in candles[-period - 1:-1]) / period
    if avg <= 0:
        return "unknown"
    r = candles[-1].volume / avg
    return "above_average" if r >= 1.3 else ("below_average" if r <= 0.7 else "average")


def build_snapshot(strategy_cfg: Dict[str, Any], symbol: str, c1h: Sequence[Candle],
                   c5m: Sequence[Candle], c15m: Optional[Sequence[Candle]] = None,
                   market: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build the structured snapshot. Raises ValueError when data is unusable."""
    market = market or {}
    for name, cs, need in (("1H", c1h, 60), ("5M", c5m, 60)):
        problems = validate_candles(list(cs), need)
        if problems:
            raise ValueError(f"{name} candle data unusable: {'; '.join(problems)}")
    p = strategy_cfg
    lb = int(p["swing_lookback"])
    atr1h = latest_atr(c1h, p["atr_period"])
    atr5 = latest_atr(c5m, p["atr_period"])
    if atr1h is None or atr5 is None or atr5 <= 0:
        raise ValueError("ATR unavailable")
    price = float(market.get("price") or c5m[-1].close)

    s1h = analyse_structure(c1h, lb)
    s5 = analyse_structure(c5m, lb)
    s15 = analyse_structure(c15m, lb) if c15m and len(c15m) >= 30 else None

    fvg5 = detect_fvgs(c5m, "5", p["atr_period"], p["fvg_min_atr_mult"], p["fvg_max_age_candles"])
    fvg1h = detect_fvgs(c1h, "60", p["atr_period"], p["fvg_min_atr_mult"], p["fvg_max_age_candles"])
    ob5 = detect_order_blocks(c5m, "5", p["atr_period"], p["ob_displacement_atr_mult"], 5, p["ob_max_age_candles"])
    ob1h = detect_order_blocks(c1h, "60", p["atr_period"], p["ob_displacement_atr_mult"], 5, p["ob_max_age_candles"])
    liq5 = detect_liquidity(c5m, atr5, lb, p["equal_level_tolerance_atr"])
    liq1h = detect_liquidity(c1h, atr1h, lb, p["equal_level_tolerance_atr"], range_bars=48)
    sweep = detect_sweep(c5m, {**liq5, "prev_session_high": liq1h.get("prev_session_high"),
                               "prev_session_low": liq1h.get("prev_session_low")})

    def top(items, n=3):
        return [x.to_dict() for x in sorted(items, key=lambda z: z.distance)[:n]]

    snap = {
        "symbol": symbol,
        "price": round(price, 4),
        "bid": market.get("bid"), "ask": market.get("ask"),
        "trend_1h": s1h["trend"], "structure_1h": s1h["structure"],
        "trend_5m": s5["trend"], "structure_5m": s5["structure"],
        "trend_15m": s15["trend"] if s15 else None,
        "bos_5m": s5["bos"], "choch_5m": s5["choch"],
        "atr_1h": round(atr1h, 4), "atr_5m": round(atr5, 4),
        "volatility_ratio_5m": round(volatility_ratio(c5m, p["atr_period"]) or 0, 3),
        "volume_state": _volume_state(c5m, int(p["volume_avg_period"])),
        "fvg_5m": top(fvg5), "fvg_1h": top(fvg1h, 2),
        "order_block_5m": top(ob5), "order_block_1h": top(ob1h, 2),
        "liquidity_5m": liq5, "liquidity_1h": {k: liq1h[k] for k in
                                               ("prev_session_high", "prev_session_low", "range_high", "range_low")},
        "liquidity_sweep": sweep,
        "funding_rate": market.get("funding_rate"),
        "open_interest": market.get("open_interest"),
        "position": market.get("position"),
        "last_candle_ts_5m": c5m[-1].ts,
    }
    return snap


def evaluate_setup(snapshot: Dict[str, Any], strategy_cfg: Dict[str, Any]) -> Setup:
    """Deterministic pre-filter: only call the AI when a candidate setup exists."""
    prox = float(strategy_cfg["proximity_atr"]) * snapshot["atr_5m"]
    for hint, trend, sweep_side, bos_dir, zone_dir in (
        ("LONG", "bullish", "LONG", "bullish", "bullish"),
        ("SHORT", "bearish", "SHORT", "bearish", "bearish"),
    ):
        if snapshot["trend_1h"] != trend:
            continue
        reasons: List[str] = []
        score = 1
        reasons.append(f"1H trend {trend}")
        sweep = snapshot["liquidity_sweep"]
        trigger = False
        if sweep.get("swept") and sweep.get("direction_hint") == sweep_side:
            reasons.append("liquidity sweep aligned"); score += 2; trigger = True
        if snapshot["bos_5m"] == bos_dir:
            reasons.append("5M break of structure aligned"); score += 2; trigger = True
        if snapshot["choch_5m"] == bos_dir:
            reasons.append("5M change of character aligned"); score += 2; trigger = True
        zones = [z for z in snapshot["fvg_5m"] if z["direction"] == zone_dir and z["distance"] <= prox]
        zones += [z for z in snapshot["order_block_5m"] if z["direction"] == zone_dir and z["distance"] <= prox]
        zones += [z for z in snapshot["order_block_1h"] if z["direction"] == zone_dir and z["distance"] <= prox]
        if zones:
            reasons.append(f"price within {strategy_cfg['proximity_atr']} ATR of {len(zones)} aligned zone(s)")
            score += 2
        if trigger and zones:
            return Setup(True, hint, reasons, score, "trigger", None)
    plan = _evaluate_plan(snapshot, strategy_cfg)
    if plan is not None:
        return plan
    return Setup(False, None, ["no aligned trigger + zone combination"], 0)


def _evaluate_plan(snapshot: Dict[str, Any], strategy_cfg: Dict[str, Any]) -> Optional[Setup]:
    """Hand the AI a PLAN when the 1H trend is clear and a fresh zone is waiting.

    The zone must sit entirely on the pullback side of price, no farther than
    plan_max_atr (default 6). Price is not there yet, so there is no trigger:
    the model may only answer with a resting limit at the zone.
    """
    price = snapshot.get("price")
    atr = snapshot.get("atr_5m")
    try:
        price_f, atr_f = float(price), float(atr)
    except (TypeError, ValueError):
        return None
    if price_f <= 0 or atr_f <= 0:
        return None
    max_atr = float(strategy_cfg.get("plan_max_atr", 6.0))
    max_fill = float(strategy_cfg.get("plan_fresh_fill_pct", 0.2))
    max_dist = max_atr * atr_f
    for hint, trend, zone_dir, below in (
        ("LONG", "bullish", "bullish", True),
        ("SHORT", "bearish", "bearish", False),
    ):
        if snapshot.get("trend_1h") != trend:
            continue
        best = None
        for key in ("fvg_5m", "order_block_5m", "fvg_1h", "order_block_1h"):
            for zone in snapshot.get(key) or []:
                if not isinstance(zone, dict) or zone.get("direction") != zone_dir:
                    continue
                if not _is_fresh(zone, max_fill):
                    continue
                edges = _zone_edges(zone)
                if edges is None:
                    continue
                lo, hi = edges
                # Entirely on the pullback side: a long plans a dip, a short plans a rally.
                if below:
                    if not hi < price_f:
                        continue
                    dist = price_f - hi
                    entry = hi
                else:
                    if not lo > price_f:
                        continue
                    dist = lo - price_f
                    entry = lo
                if dist <= 0 or dist > max_dist + 1e-9:
                    continue
                if best is None or dist < best[0]:
                    best = (dist, key, lo, hi, entry)
        if best is None:
            continue
        dist, key, lo, hi, entry = best
        away = dist / atr_f
        reasons = [
            f"1H trend {trend}",
            f"PLAN: fresh {zone_dir} zone {away:.2f} ATR away on the pullback side ({key})",
        ]
        zone = {"source": key, "lower": lo, "upper": hi, "entry": entry,
                "distance_atr": round(away, 4), "direction": zone_dir}
        return Setup(True, hint, reasons, 3, "plan", zone)
    return None
