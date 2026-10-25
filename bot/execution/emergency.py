"""Emergency close. It can only REDUCE an existing position (reduceOnly, opposite side) - never open one."""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from ..exchange.base import Exchange, ExchangeError
from ..logging_config import get_logger

log = get_logger("bot.trading")


def close_position_reduce_only(ex: Exchange, symbol: str, sleep: Callable[[float], None] = time.sleep,
                               attempts: int = 3, confirm_seconds: float = 10.0, tag: str = "emg") -> bool:
    """Market-close whatever is open using reduce-only orders. Returns True once the position is confirmed flat."""
    for attempt in range(attempts):
        try:
            positions = ex.get_positions(symbol)
        except ExchangeError as e:
            log.error("close: cannot read positions: %s", e)
            sleep(1.0 * (attempt + 1))
            continue
        if not positions:
            return True
        for p in positions:
            side = "Sell" if p["side"] == "Buy" else "Buy"
            link = f"ait-{tag}-{int(time.time() * 1000)}-{attempt}"
            try:
                ex.place_order(symbol=symbol, side=side, orderType="Market", qty=p["size"],
                               reduceOnly=True, orderLinkId=link)
            except ExchangeError as e:
                log.error("close order failed (attempt %d): %s", attempt + 1, e)
        waited = 0.0
        while waited <= confirm_seconds:
            try:
                if not ex.get_positions(symbol):
                    return True
            except ExchangeError:
                pass
            sleep(0.5)
            waited += 0.5
    try:
        return not ex.get_positions(symbol)
    except ExchangeError:
        return False


def emergency_close(ex: Exchange, symbol: str, db, halt, alerts=None, sleep: Callable[[float], None] = time.sleep,
                    reason: str = "emergency close requested") -> Dict[str, Any]:
    """1 stop new trading 2 query positions 3 cancel opening orders 4 reduce-only close 5 confirm
    6 record 7 disable autonomous trading."""
    steps: List[str] = []
    halt.halt(f"emergency: {reason}")                      # 1 + 7 (persisted; needs explicit resume)
    steps.append("new trading stopped and autonomous mode disabled")
    ok_flat = False
    err = ""
    try:
        before = ex.get_positions(symbol)                  # 2
        steps.append(f"positions found: {len(before)}")
        try:                                               # 3
            for o in ex.get_open_orders(symbol):
                if not o.get("reduceOnly"):
                    ex.cancel_order(symbol, o["orderLinkId"]) if o.get("orderLinkId") else None
            steps.append("opening orders cancelled")
        except ExchangeError as e:
            err = f"cancel orders failed: {e}"
            steps.append(err)
            try:
                ex.cancel_all_orders(symbol)
                steps.append("fallback: cancel-all issued")
            except ExchangeError as e2:
                err += f"; cancel-all failed: {e2}"
        if before:
            ok_flat = close_position_reduce_only(ex, symbol, sleep)      # 4 + 5
            steps.append("position closed and confirmed flat" if ok_flat else "POSITION NOT CONFIRMED CLOSED")
        else:
            ok_flat = True
    except ExchangeError as e:
        err = f"exchange error: {e}"
        steps.append(err)
    report = {"ok": ok_flat, "steps": steps, "error": err, "time": time.time()}
    db.record_event("emergency_close", "; ".join(steps), report)       # 6
    db.record_risk_event("emergency_close", reason, "CRITICAL" if not ok_flat else "WARN", report)
    log.critical("EMERGENCY CLOSE: %s", "; ".join(steps))
    if alerts:
        alerts.send("emergency_stop", "; ".join(steps))
    return report
