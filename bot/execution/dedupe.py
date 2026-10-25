"""Duplicate-order protection. Same signal => same orderLinkId => exchange rejects the repeat."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


def make_order_link_id(symbol: str, direction: str, candle_ts: int, purpose: str = "open", salt: str = "") -> str:
    """Deterministic id (<=36 chars, Bybit limit). Identical for retries / restarts / duplicate candle events."""
    raw = f"{symbol}|{direction}|{candle_ts}|{purpose}|{salt}"
    return "ait-" + hashlib.sha256(raw.encode()).hexdigest()[:30]


@dataclass
class DuplicateCheck:
    duplicate: bool
    reason: str = ""


def check_duplicate(order_link_id: str, symbol: str, direction: str,
                    exchange_positions: Iterable[Dict[str, Any]],
                    exchange_open_orders: Iterable[Dict[str, Any]],
                    recent_local_orders: Iterable[Dict[str, Any]],
                    now: Optional[float] = None, window_seconds: int = 900) -> DuplicateCheck:
    """Run BEFORE every opening order, using fresh exchange data + local DB records."""
    now = now or time.time()
    want_side = "Buy" if direction == "LONG" else "Sell"
    for p in exchange_positions:
        if p.get("symbol") == symbol and float(p.get("size") or 0) > 0:
            return DuplicateCheck(True, f"a {p.get('side')} position already exists on {symbol}")
    for o in exchange_open_orders:
        if o.get("orderLinkId") == order_link_id:
            return DuplicateCheck(True, "an open order with this orderLinkId already exists")
        if o.get("symbol") == symbol and o.get("side") == want_side and not o.get("reduceOnly"):
            return DuplicateCheck(True, "an equivalent opening order is already open")
    for o in recent_local_orders:
        if o.get("order_link_id") == order_link_id:
            return DuplicateCheck(True, "this orderLinkId was already submitted")
        age = now - float(o.get("created_ts", 0))
        if (o.get("symbol") == symbol and o.get("direction") == direction
                and o.get("purpose", "open") == "open" and age < window_seconds
                and o.get("status") not in ("rejected", "cancelled", "failed")):
            return DuplicateCheck(True, f"equivalent order submitted {age:.0f}s ago")
    return DuplicateCheck(False)
