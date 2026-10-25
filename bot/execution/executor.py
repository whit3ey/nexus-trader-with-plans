"""Deterministic execution layer. Only this module talks to the exchange to open/modify/close trades.

The AI never reaches it: the risk engine produces OrderParams, and this class applies the
safety protocol (duplicate checks, confirmation, stop-loss verification, emergency protection).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ..config import Config, execution_permission
from ..exchange.base import Exchange, ExchangeError
from ..logging_config import get_logger
from ..risk.limits import LimitTracker
from ..risk.position_sizing import InstrumentInfo, round_down_to_step, round_to_tick
from ..risk.risk_engine import OrderParams
from .dedupe import check_duplicate, make_order_link_id
from .emergency import close_position_reduce_only
from .state import HaltState

log = get_logger("bot.trading")


@dataclass
class ExecResult:
    ok: bool
    status: str            # filled | pending | rejected | duplicate | blocked | unprotected_closed | uncertain | error
    reason: str = ""
    link: str = ""
    trade_id: Optional[int] = None


def compute_trailing_stop(side: str, current_sl: float, entry: float, initial_risk: float, mark: float,
                          atr: float, mult: float, activate_rr: float, tick: float) -> Optional[float]:
    """ATR trailing stop. Returns a NEW stop only if it is strictly tighter (never widens risk)."""
    if initial_risk <= 0 or atr <= 0 or current_sl <= 0 or mark <= 0:
        return None
    if side == "Buy":
        if mark - entry < activate_rr * initial_risk:
            return None
        cand = round_to_tick(mark - mult * atr, tick)
        return cand if current_sl < cand < mark else None
    if entry - mark < activate_rr * initial_risk:
        return None
    cand = round_to_tick(mark + mult * atr, tick)
    return cand if mark < cand < current_sl else None


def infer_exit_reason(side: str, exit_price: float, sl: float, tp: float, entry: float, tick: float) -> str:
    tol = max(tick * 3, exit_price * 0.0008)
    if tp and abs(exit_price - tp) <= tol:
        return "TP"
    if sl and abs(exit_price - sl) <= tol:
        profitable_stop = (side == "Buy" and sl > entry) or (side == "Sell" and sl < entry)
        return "TRAILING_STOP" if profitable_stop else "SL"
    return "MANUAL_OR_OTHER"


class Executor:
    def __init__(self, cfg: Config, ex: Exchange, db, limits: LimitTracker, halt: HaltState, alerts=None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time, streams=None):
        self.cfg, self.ex, self.db, self.limits, self.halt = cfg, ex, db, limits, halt
        self.alerts, self.sleep, self.clock, self.streams = alerts, sleep, clock, streams
        self.symbol = cfg.symbol
        self.mode = cfg.mode
        self.pending: Optional[Dict[str, Any]] = db.kv_get("pending_entry")
        e = cfg.section("execution")
        self.order_timeout = float(e["order_timeout_seconds"])
        self.sl_timeout = float(e["sl_confirm_timeout_seconds"])
        self.limit_ttl = float(e.get("limit_order_ttl_seconds", 300))

    # ------------------------------------------------------------------ helpers
    def _alert(self, event: str, msg: str) -> None:
        if self.alerts:
            self.alerts.send(event, msg)

    def _inst(self) -> InstrumentInfo:
        return self.ex.get_instrument(self.symbol)

    def _wait_order(self, link: str, timeout: float) -> Optional[Dict[str, Any]]:
        """Confirm order state via the WebSocket cache first, then REST."""
        deadline = self.clock() + timeout
        last: Optional[Dict[str, Any]] = None
        while True:
            upd = self.streams.order_update(link) if self.streams else None
            try:
                o = self.ex.get_order(self.symbol, link) or upd
            except ExchangeError:
                o = upd
            if o:
                last = o
                if o.get("orderStatus") in ("Filled", "Cancelled", "Rejected", "Deactivated", "Expired"):
                    return o
            if self.clock() >= deadline:
                return last
            self.sleep(0.5)

    # ------------------------------------------------------------------ opening
    def open_position(self, p: OrderParams, candle_ts: int, confidence: float,
                      context: Optional[Dict[str, Any]] = None) -> ExecResult:
        perm = execution_permission(self.cfg)
        if not perm.can_place_orders:
            return ExecResult(False, "blocked", "; ".join(perm.blockers))
        why = self.halt.blocking_reason()
        if why:
            return ExecResult(False, "blocked", why)
        if self.pending:
            return ExecResult(False, "duplicate", "an entry order is already pending")
        link = make_order_link_id(self.symbol, p.direction, candle_ts)
        try:   # fresh exchange state BEFORE every opening order
            positions = self.ex.get_positions(self.symbol)
            orders = self.ex.get_open_orders(self.symbol)
        except ExchangeError as e:
            return ExecResult(False, "uncertain", f"cannot confirm exchange state: {e}")
        dup = check_duplicate(link, self.symbol, p.direction, positions, orders,
                              self.db.recent_orders(self.symbol, 3600), now=self.clock())
        if dup.duplicate:
            return ExecResult(False, "duplicate", dup.reason, link)
        if not self.db.insert_order(self.symbol, p.direction, "open", link, p.side, p.order_type, p.qty,
                                    p.entry_price if p.order_type == "Limit" else None, "submitting", self.mode,
                                    {"sl": p.stop_loss, "tp": p.take_profit}):
            return ExecResult(False, "duplicate", "orderLinkId already recorded", link)
        try:
            self.ex.set_leverage(self.symbol, p.leverage)
        except ExchangeError as e:
            self.db.update_order(link, "failed")
            return ExecResult(False, "rejected", f"could not set leverage: {e}", link)
        params = dict(symbol=self.symbol, side=p.side, orderType=p.order_type, qty=p.qty, orderLinkId=link,
                      stopLoss=p.stop_loss, takeProfit=p.take_profit, reduceOnly=False)
        if p.order_type == "Limit":
            params["price"] = p.entry_price
        try:
            resp = self.ex.place_order(**params)
        except ExchangeError as e:
            if e.is_duplicate:
                resp = {"orderLinkId": link}
            elif e.retryable or e.code is None:
                try:
                    exists = self.ex.get_order(self.symbol, link)
                except ExchangeError:
                    exists = None
                    self.db.update_order(link, "unknown")
                    self.db.record_risk_event("order_state_uncertain", str(e), "CRITICAL")
                    return ExecResult(False, "uncertain", f"order state unknown after error: {e}", link)
                if not exists:
                    self.db.update_order(link, "failed")
                    return ExecResult(False, "error", f"order not placed: {e}", link)
                resp = {"orderLinkId": link}
            else:
                self.db.update_order(link, "rejected")
                self.db.record_risk_event("order_rejected", str(e), "WARN")
                return ExecResult(False, "rejected", str(e), link)
        self.db.update_order(link, "submitted", resp.get("orderId"))
        ctx = dict(context or {}, initial_sl=p.stop_loss, initial_tp=p.take_profit, planned_entry=p.entry_price,
                   planned_qty=p.qty, planned_risk=p.risk_amount, rr=p.risk_reward, partial_done=False,
                   direction=p.direction, side=p.side, tp_management=p.tp_management or "",
                   risk_scale=p.risk_scale, abandon_price=p.abandon_price, valid_minutes=p.valid_minutes)
        ttl = float(p.valid_minutes) * 60.0 if p.valid_minutes else self.limit_ttl
        entry = {"link": link, "ctx": ctx, "confidence": confidence, "placed": self.clock(),
                 "expires_at": self.clock() + ttl, "abandon_price": p.abandon_price,
                 "params": p.__dict__}
        if p.order_type == "Limit":
            self.pending = entry
            self.db.kv_set("pending_entry", entry)
            return ExecResult(True, "pending", "limit entry placed", link)
        o = self._wait_order(link, self.order_timeout)
        status = (o or {}).get("orderStatus")
        if status in ("Rejected", "Cancelled", "Deactivated", "Expired"):
            self.db.update_order(link, status.lower())
            return ExecResult(False, "rejected", f"order {status}", link)
        if status != "Filled":                        # timeout: cancel, then see if anything filled
            try:
                self.ex.cancel_order(self.symbol, link)
            except ExchangeError:
                pass
        return self._finalize_entry(entry)

    # ------------------------------------------------------------------ protection / finalising
    def _verify_protection(self, sl: float, tp: float, tick: float) -> Optional[Dict[str, Any]]:
        """Return the live position only if the stop loss is confirmed on the exchange."""
        deadline = self.clock() + self.sl_timeout
        tries = 0
        while True:
            try:
                pos = self.ex.get_positions(self.symbol)
            except ExchangeError:
                pos = []
            if pos:
                cur = pos[0]
                if cur.get("stopLoss") and abs(cur["stopLoss"] - sl) <= tick * 2:
                    if not tp or cur.get("takeProfit"):
                        return cur
                    try:
                        self.ex.set_trading_stop(self.symbol, take_profit=tp)
                    except ExchangeError as e:
                        self.db.record_risk_event("tp_not_set", str(e), "WARN")
                        return cur          # stop confirmed; TP failure is not safety-critical
                    continue
                try:
                    self.ex.set_trading_stop(self.symbol, stop_loss=sl, take_profit=tp or None)
                except ExchangeError as e:
                    log.error("set_trading_stop failed: %s", e)
            if tries >= 4 or self.clock() >= deadline:
                return None
            tries += 1
            self.sleep(min(0.5 * 2 ** tries, 4))

    def _finalize_entry(self, entry: Dict[str, Any]) -> ExecResult:
        link, ctx = entry["link"], entry["ctx"]
        try:
            positions = self.ex.get_positions(self.symbol)
        except ExchangeError as e:
            self.halt.halt("cannot read positions after entry")
            return ExecResult(False, "uncertain", f"cannot read position after entry: {e}", link)
        if not positions:
            self.db.update_order(link, "not_filled")
            return ExecResult(False, "rejected", "entry order did not fill", link)
        tick = self._inst().tick_size
        pos = self._verify_protection(ctx["initial_sl"], ctx.get("initial_tp") or 0.0, tick)
        if pos is None:   # unprotected position: close it NOW, raise the alarm, stop autonomous trading
            log.critical("stop loss could not be confirmed - closing position")
            closed = close_position_reduce_only(self.ex, self.symbol, self.sleep, tag="protect")
            msg = "stop loss could not be placed/confirmed; position " + ("closed" if closed else "NOT CONFIRMED CLOSED")
            self.halt.halt(msg)
            self.db.record_risk_event("unprotected_position", msg, "CRITICAL")
            self._alert("system_error", msg)
            self.db.update_order(link, "protect_failed")
            return ExecResult(False, "unprotected_closed", msg, link)
        self.db.update_order(link, "filled")
        qty, avg = pos["size"], pos["avgPrice"]
        risk_amount = abs(avg - ctx["initial_sl"]) * qty
        ctx["initial_risk_dist"] = abs(avg - ctx["initial_sl"])
        direction = "LONG" if pos["side"] == "Buy" else "SHORT"
        tid = self.db.open_trade(symbol=self.symbol, direction=direction, mode=self.mode, entry=avg, qty=qty,
                                 stop_loss=ctx["initial_sl"], take_profit=ctx.get("initial_tp"),
                                 risk_amount=risk_amount, order_link_id=link, ai_confidence=entry["confidence"],
                                 context=ctx)
        self.limits.record_trade_opened()
        self.db.save_daily_stats(self.limits.stats.day, self.limits.snapshot())
        self.pending = None
        self.db.kv_set("pending_entry", None)
        self._alert("trade_opened", f"{direction} {qty} {self.symbol} @ {avg} SL {ctx['initial_sl']} TP {ctx.get('initial_tp')}")
        log.info("TRADE OPENED #%s %s qty=%s entry=%s sl=%s tp=%s", tid, direction, qty, avg, ctx["initial_sl"], ctx.get("initial_tp"))
        return ExecResult(True, "filled", "position open and stop loss confirmed", link, tid)

    def service_pending(self, mark: Optional[float] = None) -> Optional[ExecResult]:
        """Called every loop: resolves a pending LIMIT entry (fill / cancel / expiry / abandon).

        The order already rests with its stop and target attached. This only cancels it.
        It cannot widen or remove a stop. The pending record is reloaded from the database
        after a restart, including the deadline and the abandon level.
        """
        if not self.pending:
            return None
        link = self.pending["link"]
        try:
            o = self.ex.get_order(self.symbol, link)
        except ExchangeError:
            return None
        status = (o or {}).get("orderStatus")
        expires = self.pending.get("expires_at")
        if expires is None:
            expires = self.pending["placed"] + self.limit_ttl
        age = self.clock() - self.pending["placed"]
        if status == "Filled" or (status in ("PartiallyFilled",) and self.clock() >= expires):
            if status == "PartiallyFilled":
                try:
                    self.ex.cancel_order(self.symbol, link)
                except ExchangeError:
                    pass
            return self._finalize_entry(self.pending)
        if status in ("Cancelled", "Rejected", "Deactivated", "Expired") or (o is None and age > 30):
            self.db.update_order(link, (status or "gone").lower())
            self.pending = None
            self.db.kv_set("pending_entry", None)
            return ExecResult(False, "rejected", f"pending entry ended: {status}", link)
        direction = (self.pending.get("params") or {}).get("direction") or (self.pending.get("ctx") or {}).get("direction")
        abandon = self.pending.get("abandon_price")
        if mark is not None and abandon is not None and direction in ("LONG", "SHORT"):
            hit = (direction == "LONG" and float(mark) >= float(abandon)) or (
                direction == "SHORT" and float(mark) <= float(abandon))
            if hit:
                return self._cancel_pending(link, "abandoned",
                                             "plan abandoned: price reached the abandon level before the fill")
        if self.clock() >= expires:
            return self._cancel_pending(link, "expired", "limit entry expired and was cancelled")
        return None

    def _cancel_pending(self, link: str, status: str, reason: str) -> Optional[ExecResult]:
        try:
            self.ex.cancel_order(self.symbol, link)
        except ExchangeError:
            return None
        self.db.update_order(link, status)
        self.pending = None
        self.db.kv_set("pending_entry", None)
        return ExecResult(False, "rejected", reason, link)

    # ------------------------------------------------------------------ managing an open position
    def _trade_for(self, pos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        direction = "LONG" if pos["side"] == "Buy" else "SHORT"
        for t in self.db.open_trades(self.symbol):
            if t["direction"] == direction:
                return self.db.get_trade(t["id"])
        return None

    def manage_position(self, pos: Dict[str, Any], atr: float) -> List[str]:
        """Take-profit style chosen for this trade, plus stop protection.

        The AI picks fixed, partial or trailing at entry. It cannot remove a stop or
        widen one: a missing stop is restored, and a trail only ever ratchets tighter.
        With no per-trade choice, the configured style is used.
        """
        actions: List[str] = []
        trade = self._trade_for(pos)
        if not trade:
            return actions
        ctx = trade["context"]
        tick = self._inst().tick_size
        side, entry, mark = pos["side"], pos["avgPrice"], pos["markPrice"] or pos["avgPrice"]
        cur_sl = pos.get("stopLoss") or 0.0
        if cur_sl <= 0:   # a stop must always exist; the model has no way to clear it
            sl = ctx.get("initial_sl")
            try:
                self.ex.set_trading_stop(self.symbol, stop_loss=sl)
                actions.append(f"stop loss was missing - restored at {sl}")
                self.db.record_risk_event("sl_restored", f"restored at {sl}", "WARN")
            except ExchangeError as e:
                self.halt.halt(f"position has no stop loss and it cannot be restored: {e}")
                close_position_reduce_only(self.ex, self.symbol, self.sleep, tag="protect")
                actions.append("could not restore stop - position closed")
            return actions
        risk_dist = ctx.get("initial_risk_dist") or abs(entry - ctx.get("initial_sl", entry))
        tpc = self.cfg.section("tp")
        tr = self.cfg.section("trailing")
        mode = str(ctx.get("tp_management") or "")
        if mode == "fixed":
            do_partial, do_trail = False, False
        elif mode == "partial":
            do_partial, do_trail = True, False
        elif mode == "trailing":
            do_partial, do_trail = False, True
        else:
            do_partial = tpc["mode"] == "partial"
            do_trail = bool(tr["enabled"])
        d = 1 if side == "Buy" else -1
        r_now = ((mark - entry) * d) / risk_dist if risk_dist else 0.0
        # partial take-profit
        if do_partial and not ctx.get("partial_done") and r_now >= float(tpc["partial_at_rr"]):
            inst = self._inst()
            part = round_down_to_step(pos["size"] * float(tpc["partial_percent"]) / 100.0, inst.qty_step)
            if part >= inst.min_qty and pos["size"] - part >= inst.min_qty:
                try:
                    self.ex.place_order(symbol=self.symbol, side="Sell" if side == "Buy" else "Buy", orderType="Market",
                                        qty=part, reduceOnly=True, orderLinkId=f"ait-part-{trade['id']}")
                    ctx["partial_done"] = True
                    be = round_to_tick(entry, tick)
                    if (side == "Buy" and be > cur_sl) or (side == "Sell" and be < cur_sl):
                        self.ex.set_trading_stop(self.symbol, stop_loss=be)
                        cur_sl = be
                    self.db.update_trade(trade["id"], context=ctx)
                    actions.append(f"partial take-profit {part} at {r_now:.2f}R, stop to break-even")
                except ExchangeError as e:
                    actions.append(f"partial close failed: {e}")
            else:
                ctx["partial_done"] = True
        if do_trail:
            new_sl = compute_trailing_stop(side, cur_sl, entry, risk_dist, mark, atr, float(tr["atr_multiplier"]),
                                           float(tr["activate_at_rr"]), tick)
            if new_sl is not None:
                widening = (side == "Buy" and new_sl <= cur_sl) or (side == "Sell" and new_sl >= cur_sl)
                if not widening:   # belt and braces: never loosen a stop
                    try:
                        self.ex.set_trading_stop(self.symbol, stop_loss=new_sl)
                        self.db.update_trade(trade["id"], stop_loss=new_sl)
                        actions.append(f"trailing stop {cur_sl} -> {new_sl}")
                    except ExchangeError as e:
                        actions.append(f"trailing update failed: {e}")
        return actions

    # ------------------------------------------------------------------ detecting closes
    def record_close(self, trade: Dict[str, Any], note: str = "") -> Optional[Dict[str, Any]]:
        """Finish bookkeeping for a trade that is no longer open on the exchange."""
        ctx = trade.get("context") if isinstance(trade.get("context"), dict) else json.loads(trade.get("context") or "{}")
        events: List[Dict[str, Any]] = []
        try:
            events = self.ex.get_closed_pnl(self.symbol, 10)
        except ExchangeError:
            pass
        mine = [e for e in events if e.get("openLink") == trade.get("order_link_id")] or \
               [e for e in events if e.get("time", 0) >= (trade.get("open_ts") or 0) - 5]
        side = "Buy" if trade["direction"] == "LONG" else "Sell"
        tick = self._inst().tick_size
        if mine:
            e = mine[-1]
            fees = float(e.get("fees", 0.0))
            net = float(e["pnl"]) - fees if self.ex.simulated else float(e["pnl"])
            exit_price = float(e["exit"])
            reason = e.get("reason") if e.get("reason") in ("SL", "TP") else None
        else:
            exit_price, fees, net, reason = float(trade.get("entry") or 0), 0.0, 0.0, None
            note = (note + " (no exchange P/L found - recorded as 0)").strip()
        sl_now = trade.get("stop_loss") or ctx.get("initial_sl") or 0.0
        reason = reason or infer_exit_reason(side, exit_price, sl_now, trade.get("take_profit") or 0.0,
                                             trade.get("entry") or 0.0, tick)
        risk = float(trade.get("risk_amount") or 0.0)
        r_mult = net / risk if risk > 0 else 0.0
        self.db.close_trade(trade["id"], exit_price, net, fees, r_mult, reason + (f" [{note}]" if note else ""))
        self.limits.record_trade_closed(net, fees, r_mult)
        self.limits.update_equity(self.limits.last_equity + 0.0)
        self.db.save_daily_stats(self.limits.stats.day, self.limits.snapshot())
        ev = {"SL": "sl_hit", "TP": "tp_hit"}.get(reason, "trade_closed")
        self._alert(ev, f"{trade['direction']} closed ({reason}) P/L {net:.2f} USDT, {r_mult:.2f}R")
        log.info("TRADE CLOSED #%s reason=%s pnl=%.2f R=%.2f", trade["id"], reason, net, r_mult)
        return {"trade_id": trade["id"], "pnl": net, "r": r_mult, "reason": reason}

    def detect_closed(self, positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for t in self.db.open_trades(self.symbol):
            full = self.db.get_trade(t["id"])
            if not any((p["side"] == "Buy") == (t["direction"] == "LONG") for p in positions):
                res = self.record_close(full)
                if res:
                    out.append(res)
        return out
