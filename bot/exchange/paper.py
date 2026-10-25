"""Paper-trading exchange: simulates fills, fees, slippage, SL/TP and trailing stops.

It NEVER sends trading orders anywhere. Market data comes from a separate read-only source.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Protocol

from ..risk.position_sizing import InstrumentInfo, round_down_to_step
from ..strategy.types import Candle
from .base import CODE_DUPLICATE_LINK_ID, Exchange, ExchangeError, flat_position


class MarketSource(Protocol):
    def get_instrument(self, symbol: str) -> InstrumentInfo: ...
    def get_ticker(self, symbol: str) -> Dict[str, Any]: ...
    def get_klines(self, symbol: str, interval: str, limit: int) -> List[Candle]: ...


class PaperExchange(Exchange):
    name = "paper"
    simulated = True

    def __init__(self, market: MarketSource, starting_equity: float = 10_000.0, taker_fee_pct: float = 0.055,
                 maker_fee_pct: float = 0.02, slippage_pct: float = 0.02, clock: Callable[[], float] = time.time):
        self.market = market
        self.balance = float(starting_equity)
        self.starting_equity = float(starting_equity)
        self.taker = taker_fee_pct / 100.0
        self.maker = maker_fee_pct / 100.0
        self.slip = slippage_pct / 100.0
        self.clock = clock
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.orders: Dict[str, Dict[str, Any]] = {}      # orderLinkId -> order
        self.executions: List[Dict[str, Any]] = []
        self.closed: List[Dict[str, Any]] = []            # closed-trade events not yet consumed
        self.leverage = 1
        self._seq = 0

    # --- market data passthrough -----------------------------------------------------------
    def get_instrument(self, symbol): return self.market.get_instrument(symbol)
    def get_ticker(self, symbol): return self.market.get_ticker(symbol)
    def get_klines(self, symbol, interval, limit): return self.market.get_klines(symbol, interval, limit)

    # --- account ------------------------------------------------------------------------------
    def _unrealised(self, symbol: str, price: float) -> float:
        p = self.positions.get(symbol)
        if not p:
            return 0.0
        d = 1 if p["side"] == "Buy" else -1
        return (price - p["avgPrice"]) * p["size"] * d

    def get_wallet(self) -> Dict[str, float]:
        upl = 0.0
        used = 0.0
        for sym, p in self.positions.items():
            price = float(self.market.get_ticker(sym)["price"])
            upl += self._unrealised(sym, price)
            used += p["size"] * p["avgPrice"] / max(self.leverage, 1)
        equity = self.balance + upl
        return {"equity": equity, "available": max(equity - used, 0.0)}

    def get_all_positions(self):
        out = []
        for symbol, p in self.positions.items():
            if p and p.get("size", 0) > 0:
                out.extend(self.get_positions(symbol))
        return out

    def get_positions(self, symbol):
        p = self.positions.get(symbol)
        if not p or p["size"] <= 0:
            return []
        price = float(self.market.get_ticker(symbol)["price"])
        out = dict(p)
        out.update(unrealisedPnl=self._unrealised(symbol, price), markPrice=price, leverage=self.leverage)
        return [out]

    def get_all_open_orders(self):
        return [dict(o) for o in self.orders.values() if o["orderStatus"] in ("New", "PartiallyFilled", "Untriggered")]

    def get_open_orders(self, symbol):
        return [dict(o) for o in self.orders.values() if o["symbol"] == symbol and o["orderStatus"] in ("New", "PartiallyFilled")]

    def get_order(self, symbol, order_link_id):
        o = self.orders.get(order_link_id)
        return dict(o) if o else None

    def set_leverage(self, symbol, leverage):
        self.leverage = int(leverage)

    # --- orders ---------------------------------------------------------------------------------
    def _next_id(self) -> str:
        self._seq += 1
        return f"paper-{self._seq}"

    def place_order(self, **p):
        symbol, side, qty = p["symbol"], p["side"], float(p["qty"])
        link = p.get("orderLinkId") or self._next_id()
        if link in self.orders:
            raise ExchangeError("duplicate orderLinkId", CODE_DUPLICATE_LINK_ID)
        inst = self.market.get_instrument(symbol)
        if qty < inst.min_qty or round_down_to_step(qty, inst.qty_step) != qty:
            raise ExchangeError(f"invalid qty {qty}", 10001)
        reduce_only = bool(p.get("reduceOnly"))
        pos = self.positions.get(symbol)
        if reduce_only:
            if not pos or pos["size"] <= 0 or pos["side"] == side:
                raise ExchangeError("reduce-only order would not reduce a position", 110017)
            qty = min(qty, pos["size"])
        elif pos and pos["size"] > 0 and pos["side"] != side:
            raise ExchangeError("opposite-side order would flip position (one-way mode, use reduceOnly)", 110017)
        order = {"symbol": symbol, "side": side, "orderType": p["orderType"], "qty": qty,
                 "price": p.get("price"), "orderId": self._next_id(), "orderLinkId": link,
                 "orderStatus": "New", "reduceOnly": reduce_only, "stopLoss": p.get("stopLoss"),
                 "takeProfit": p.get("takeProfit"), "createdTime": self.clock(), "avgPrice": 0.0}
        self.orders[link] = order
        if p["orderType"] == "Market":
            t = self.market.get_ticker(symbol)
            px = float(t["ask"] if side == "Buy" else t["bid"])
            px *= (1 + self.slip) if side == "Buy" else (1 - self.slip)
            self._fill(order, px, self.taker)
        return {"orderId": order["orderId"], "orderLinkId": link}

    def _fill(self, order: Dict[str, Any], price: float, fee_rate: float) -> None:
        symbol, side, qty = order["symbol"], order["side"], order["qty"]
        if not order["reduceOnly"]:
            wallet = self.get_wallet()
            if qty * price / max(self.leverage, 1) > wallet["available"] + 1e-9:
                order["orderStatus"] = "Rejected"
                raise ExchangeError("insufficient margin", 110007)
        fee = qty * price * fee_rate
        order.update(orderStatus="Filled", avgPrice=price, cumExecFee=fee)
        self.executions.append({"execId": f"x{len(self.executions) + 1}", "orderLinkId": order["orderLinkId"],
                                "symbol": symbol, "side": side, "execPrice": price, "execQty": qty,
                                "execFee": fee, "execTime": self.clock()})
        self.balance -= fee
        pos = self.positions.get(symbol)
        if order["reduceOnly"] or (pos and pos["side"] != side and pos["size"] > 0):
            self._reduce(symbol, qty, price, fee, "manual_close", order["orderLinkId"])
        else:
            if pos and pos["size"] > 0:
                tot = pos["size"] + qty
                pos["avgPrice"] = (pos["avgPrice"] * pos["size"] + price * qty) / tot
                pos["size"] = tot
                pos["fees"] += fee
            else:
                self.positions[symbol] = {"symbol": symbol, "side": side, "size": qty, "avgPrice": price,
                                          "stopLoss": float(order.get("stopLoss") or 0.0),
                                          "takeProfit": float(order.get("takeProfit") or 0.0),
                                          "fees": fee, "openTime": self.clock(), "openLink": order["orderLinkId"]}

    def _reduce(self, symbol: str, qty: float, price: float, fee: float, reason: str, link: str) -> None:
        pos = self.positions[symbol]
        d = 1 if pos["side"] == "Buy" else -1
        qty = min(qty, pos["size"])
        pnl = (price - pos["avgPrice"]) * qty * d
        self.balance += pnl
        pos["size"] -= qty
        pos["fees"] = pos.get("fees", 0.0) + (fee if reason != "manual_open" else 0.0)
        if pos["size"] <= 1e-12:
            self.closed.append({"symbol": symbol, "side": pos["side"], "entry": pos["avgPrice"], "exit": price,
                                "qty": qty, "pnl": pnl, "fees": pos["fees"], "reason": reason,
                                "closeLink": link, "openLink": pos.get("openLink"), "time": self.clock()})
            del self.positions[symbol]

    def cancel_order(self, symbol, order_link_id):
        o = self.orders.get(order_link_id)
        if o and o["orderStatus"] == "New":
            o["orderStatus"] = "Cancelled"

    def cancel_all_orders(self, symbol):
        for o in self.orders.values():
            if o["symbol"] == symbol and o["orderStatus"] == "New":
                o["orderStatus"] = "Cancelled"

    def set_trading_stop(self, symbol, stop_loss=None, take_profit=None):
        pos = self.positions.get(symbol)
        if not pos:
            raise ExchangeError("no position to protect", 34040)
        if stop_loss is not None:
            pos["stopLoss"] = float(stop_loss)
        if take_profit is not None:
            pos["takeProfit"] = float(take_profit)

    def get_executions(self, symbol, limit=50):
        return [e for e in self.executions if e["symbol"] == symbol][-limit:]

    def get_closed_pnl(self, symbol, limit=20):
        return [c for c in self.closed if c["symbol"] == symbol][-limit:]

    def check_api_permissions(self):
        return {"ok": True, "withdraw": False, "read_only": False, "note": "paper mode - no credentials used"}

    # --- simulation step ---------------------------------------------------------------------------
    def process_tick(self, high: float, low: float, bid: float, ask: float) -> None:
        """Advance the simulation with the price range seen since the last tick.

        When SL and TP are both inside the range, the stop is assumed to hit first (conservative).
        """
        for o in list(self.orders.values()):
            if o["orderStatus"] != "New" or o["orderType"] != "Limit":
                continue
            px = float(o["price"])
            if (o["side"] == "Buy" and low <= px) or (o["side"] == "Sell" and high >= px):
                try:
                    self._fill(o, px, self.maker)
                except ExchangeError:
                    pass
        for sym, pos in list(self.positions.items()):
            long_ = pos["side"] == "Buy"
            sl, tp = pos.get("stopLoss") or 0.0, pos.get("takeProfit") or 0.0
            hit_sl = sl and ((long_ and low <= sl) or (not long_ and high >= sl))
            hit_tp = tp and ((long_ and high >= tp) or (not long_ and low <= tp))
            if hit_sl:
                px = sl * ((1 - self.slip) if long_ else (1 + self.slip))
                fee = pos["size"] * px * self.taker
                self.balance -= fee
                self._reduce(sym, pos["size"], px, fee, "SL", f"sl-{self._next_id()}")
            elif hit_tp:
                px = tp
                fee = pos["size"] * px * self.maker
                self.balance -= fee
                self._reduce(sym, pos["size"], px, fee, "TP", f"tp-{self._next_id()}")

    def pop_closed_events(self) -> List[Dict[str, Any]]:
        out, self.closed = self.closed, []
        return out

    # --- persistence (so a restart does not lose a paper position) ---------------------------------
    def dump_state(self) -> Dict[str, Any]:
        return {"balance": self.balance, "positions": self.positions, "orders": self.orders,
                "executions": self.executions[-200:], "closed": self.closed[-50:], "leverage": self.leverage,
                "seq": self._seq}

    def load_state(self, st: Dict[str, Any]) -> None:
        self.balance = float(st.get("balance", self.balance))
        self.positions = st.get("positions", {}) or {}
        self.orders = st.get("orders", {}) or {}
        self.executions = st.get("executions", []) or []
        self.closed = st.get("closed", []) or []
        self.leverage = int(st.get("leverage", 1))
        self._seq = int(st.get("seq", 0))
