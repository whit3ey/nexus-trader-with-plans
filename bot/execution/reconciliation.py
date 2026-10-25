"""Startup / periodic reconciliation. The exchange is the source of truth for positions and orders."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from ..exchange.base import Exchange, ExchangeError
from ..logging_config import get_logger

log = get_logger("bot.trading")

TERMINAL = ("Filled", "Cancelled", "Rejected", "Deactivated", "Expired")


@dataclass
class ReconReport:
    certain: bool = False
    positions: List[Dict[str, Any]] = field(default_factory=list)
    open_orders: List[Dict[str, Any]] = field(default_factory=list)
    unexpected_position: bool = False
    unprotected_position: bool = False
    adopted_trade_ids: List[int] = field(default_factory=list)
    closed_offline: List[int] = field(default_factory=list)
    cancelled_orders: List[str] = field(default_factory=list)
    pending_link: str = ""
    unexpected_order: bool = False
    actions: List[str] = field(default_factory=list)
    error: str = ""


def reconcile(ex: Exchange, db, symbol: str, executor_close_recorder=None, alerts=None) -> ReconReport:
    """Compare exchange vs local DB and repair the DB (never the other way round)."""
    r = ReconReport()
    try:
        r.positions = ex.get_positions(symbol)
        r.open_orders = ex.get_open_orders(symbol)
    except ExchangeError as e:
        r.error = f"cannot read exchange state: {e}"
        r.actions.append(r.error)
        db.record_error("reconcile", r.error)
        return r                      # certain stays False => no new trades

    db_trades = db.open_trades(symbol)
    # 1) positions on the exchange
    for p in r.positions:
        match = [t for t in db_trades if t["direction"] == ("LONG" if p["side"] == "Buy" else "SHORT")]
        if not match:
            r.unexpected_position = True
            msg = f"unexpected {p['side']} position {p['size']} @ {p['avgPrice']} on {symbol} (not in local database)"
            r.actions.append(msg)
            db.record_risk_event("unexpected_position", msg, "CRITICAL", p)
            db.record_position(symbol, p)
            if alerts:
                alerts.send("unexpected_position", msg, dedupe_seconds=900)
        if not p.get("stopLoss"):
            r.unprotected_position = True
            r.actions.append("position has NO stop loss on the exchange")
    # 2) trades the DB thinks are open but the exchange no longer has
    for t in db_trades:
        has_pos = any((p["side"] == "Buy") == (t["direction"] == "LONG") for p in r.positions)
        if not has_pos:
            if executor_close_recorder:
                executor_close_recorder(t, "closed while the bot was offline")
            else:
                db.close_trade(t["id"], t.get("entry") or 0.0, 0.0, 0.0, 0.0, "closed_while_offline")
            r.closed_offline.append(t["id"])
            r.actions.append(f"trade #{t['id']} was closed on the exchange while offline - database updated")
    # 3) open orders
    known = {o["order_link_id"]: o for o in db.recent_orders(symbol, 7 * 86400)}
    for o in r.open_orders:
        link = o.get("orderLinkId", "")
        if o.get("reduceOnly"):
            continue
        if link in known:
            r.pending_link = link
            r.actions.append(f"pending entry order {link} restored")
        else:
            # Never cancel orders we cannot prove this bot created. They may belong to
            # the user or another bot. Block new entries instead and surface the order.
            r.unexpected_order = True
            msg = f"unmanaged opening order {link or '?'} detected on {symbol}; left untouched"
            r.actions.append(msg)
            db.record_risk_event("unexpected_order", msg, "WARN", o)
    # 4) local order rows stuck in a non-final state
    live_links = {o.get("orderLinkId") for o in r.open_orders}
    for row in known.values():
        if row["status"] in ("submitting", "submitted", "new") and row["order_link_id"] not in live_links:
            try:
                o = ex.get_order(symbol, row["order_link_id"])
            except ExchangeError as e:
                r.error = f"cannot verify order {row['order_link_id']}: {e}"
                return r
            db.update_order(row["order_link_id"], (o or {}).get("orderStatus", "unknown").lower() if o else "not_found")
    r.certain = True
    db.record_event("reconcile", "; ".join(r.actions) or "state consistent",
                    {"positions": len(r.positions), "orders": len(r.open_orders)})
    return r
