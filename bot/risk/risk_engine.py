"""The risk engine is the final authority: it can reject any AI decision, and the AI cannot override it.

validate() collects EVERY failed check (so the log explains the full rejection) and,
only if nothing failed, returns fully rounded order parameters.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..ai.schemas import AIDecision
from ..logging_config import get_logger
from .limits import LimitTracker
from .position_sizing import InstrumentInfo, compute_position, risk_reward, round_to_tick

log = get_logger("bot.risk")


@dataclass
class RiskContext:
    price: float
    bid: float
    ask: float
    atr: float
    equity: float
    available_balance: float
    instrument: InstrumentInfo
    open_positions: int
    data_age_seconds: float
    api_healthy: bool = True
    ws_healthy: bool = True
    ollama_healthy: bool = True
    account_state_confirmed: bool = True
    position_state_certain: bool = True
    order_state_certain: bool = True
    volatility_ratio: Optional[float] = None
    trend_aligned: bool = False        # 1H trend agrees with the trade direction
    has_trigger: bool = False          # sweep / BOS / CHoCH aligned (from deterministic engine)
    trading_halted_reason: Optional[str] = None   # e.g. emergency stop or alarm
    is_plan: bool = False              # resting limit at a zone price has not reached
    plan_zone_lower: Optional[float] = None
    plan_zone_upper: Optional[float] = None


@dataclass
class OrderParams:
    side: str                # Bybit side: "Buy" | "Sell"
    direction: str           # LONG | SHORT
    order_type: str          # Market | Limit
    qty: float
    entry_price: float
    stop_loss: float
    take_profit: float
    leverage: int
    risk_amount: float
    risk_reward: float
    notional: float
    valid_minutes: Optional[float] = None
    abandon_price: Optional[float] = None
    tp_management: str = ""            # "" = use the configured style; else fixed|partial|trailing
    risk_scale: float = 1.0            # fraction of configured risk actually used (never above 1)


@dataclass
class RiskResult:
    approved: bool
    reasons: List[str] = field(default_factory=list)
    order: Optional[OrderParams] = None
    is_wait: bool = False
    checks: Dict[str, bool] = field(default_factory=dict)

    def summary(self) -> str:
        return "APPROVED" if self.approved else "REJECTED: " + "; ".join(self.reasons)


class RiskEngine:
    def __init__(self, risk_cfg: Dict[str, Any], ai_cfg: Dict[str, Any], tp_cfg: Optional[Dict[str, Any]] = None,
                 fee_round_trip: float = 0.0):
        self.fee_round_trip = fee_round_trip   # taker fee x2 (+ slippage) as a fraction of notional
        self.r = risk_cfg
        self.ai = ai_cfg
        self.tp = tp_cfg or {}

    def validate(self, d: AIDecision, ctx: RiskContext, limits: LimitTracker) -> RiskResult:
        res = RiskResult(False)
        if d.decision == "WAIT":
            res.is_wait = True
            res.reasons.append("AI decided WAIT")
            return res
        fails = res.reasons
        checks = res.checks

        def check(name: str, ok: bool, msg: str) -> bool:
            checks[name] = bool(ok)
            if not ok:
                fails.append(msg)
            return bool(ok)

        r = self.r
        # --- system health / uncertainty => never trade --------------------------
        check("not_halted", not ctx.trading_halted_reason, f"trading halted: {ctx.trading_halted_reason}")
        check("api_health", ctx.api_healthy, "Bybit API unhealthy")
        check("ws_health", ctx.ws_healthy, "WebSocket unhealthy")
        check("ollama_health", ctx.ollama_healthy, "Ollama unhealthy")
        check("account_state", ctx.account_state_confirmed, "account state not confirmed")
        check("position_state", ctx.position_state_certain, "position state uncertain")
        check("order_state", ctx.order_state_certain, "order state uncertain")
        check("data_fresh", ctx.data_age_seconds <= float(r["max_data_age_seconds"]),
              f"market data stale ({ctx.data_age_seconds:.0f}s > {r['max_data_age_seconds']}s)")
        valid_inputs = all(math.isfinite(x) and x > 0 for x in (ctx.price, ctx.bid, ctx.ask, ctx.atr, ctx.equity))
        check("inputs_valid", valid_inputs, "market/account inputs missing or invalid")
        if not valid_inputs:
            return res

        # --- account-level limits ----------------------------------------------------
        halts = limits.halt_reasons()
        check("account_limits", not halts, "; ".join(halts))
        check("open_positions", ctx.open_positions < int(r["max_open_positions"]),
              f"max open positions reached ({ctx.open_positions}/{r['max_open_positions']})")

        # --- AI confidence --------------------------------------------------------------
        min_c, strong_c = float(self.ai["min_confidence"]), float(self.ai["strong_confidence"])
        check("confidence_min", d.confidence >= min_c, f"AI confidence {d.confidence:.0f} below minimum {min_c:.0f}")
        if min_c <= d.confidence < strong_c:
            strong_ok = ctx.trend_aligned and ctx.has_trigger
            check("confidence_mid_tier", strong_ok,
                  f"confidence {d.confidence:.0f} is in the middle band and needs 1H trend alignment AND a trigger")

        # --- market conditions ---------------------------------------------------------------
        spread_pct = (ctx.ask - ctx.bid) / ((ctx.ask + ctx.bid) / 2) * 100
        check("spread", ctx.ask >= ctx.bid and spread_pct <= float(r["max_spread_percent"]),
              f"spread {spread_pct:.4f}% exceeds {r['max_spread_percent']}%")
        vr_ok = ctx.volatility_ratio is None or ctx.volatility_ratio <= float(r["abnormal_atr_ratio"])
        check("volatility", vr_ok and d.market_regime != "HIGH_VOLATILITY",
              "abnormal volatility" if not vr_ok else "AI classified market as HIGH_VOLATILITY")

        # --- price geometry --------------------------------------------------------------------
        long_ = d.decision == "LONG"
        if d.entry_type == "MARKET":
            entry = ctx.ask if long_ else ctx.bid
        else:
            entry = float(d.entry_price or 0)
        tick = ctx.instrument.tick_size
        entry = round_to_tick(entry, tick)
        sl = round_to_tick(float(d.stop_loss), tick)
        tp = round_to_tick(float(d.take_profit), tick)
        geometry_ok = (sl < entry < tp) if long_ else (tp < entry < sl)
        check("stop_target_geometry", geometry_ok,
              "stop loss / take profit are on the wrong side of entry" if not geometry_ok else "")
        if d.entry_type == "LIMIT":
            cap = float(r.get("plan_max_distance_atr", 6.0)) if ctx.is_plan else float(r["max_entry_deviation_atr"])
            dev = abs(entry - ctx.price) / ctx.atr
            check("entry_deviation", dev <= cap + 1e-9,
                  f"limit entry is {dev:.2f} ATR from price (max {cap})")
        if ctx.is_plan:
            check("plan_is_limit", d.entry_type == "LIMIT", "a plan must be a LIMIT order resting at the zone")
            on_pullback = (entry < ctx.price) if long_ else (entry > ctx.price)
            check("plan_pullback_side", on_pullback, "planned entry must sit on the pullback side of price")
            if ctx.plan_zone_lower is not None and ctx.plan_zone_upper is not None:
                lo, hi = float(ctx.plan_zone_lower), float(ctx.plan_zone_upper)
                check("plan_at_zone", lo - tick <= entry <= hi + tick,
                      "planned limit is not inside the fresh zone")
        if not geometry_ok:
            return res
        stop_dist = abs(entry - sl)
        stop_atr = stop_dist / ctx.atr
        check("stop_not_too_tight", stop_atr >= float(r["min_stop_atr_mult"]),
              f"stop is only {stop_atr:.2f} ATR away (min {r['min_stop_atr_mult']})")
        check("stop_not_too_wide", stop_atr <= float(r["max_stop_atr_mult"]),
              f"stop is {stop_atr:.2f} ATR away (max {r['max_stop_atr_mult']})")
        rr = risk_reward(d.decision, entry, sl, tp)
        check("risk_reward", rr >= float(r["min_risk_reward"]) - 1e-9,
              f"risk/reward {rr:.2f} below minimum {r['min_risk_reward']}")
        if d.risk_reward is not None:
            check("rr_claim_honest", d.risk_reward <= rr + 0.25,
                  f"AI claimed R:R {d.risk_reward:.2f} but real R:R is {rr:.2f}")
        if 70 <= d.confidence < strong_c and ctx.trend_aligned and ctx.has_trigger:
            check("mid_tier_rr", rr >= float(r["min_risk_reward"]) + 0.5,
                  "middle confidence band needs R:R at least 0.5 above the minimum")

        # The model may only shrink risk, and may only pick a take-profit style. It cannot
        # raise the account risk, remove a stop, or move account-level limits.
        scale = 1.0 if d.risk_scale is None else float(d.risk_scale)
        scale_ok = 0.25 - 1e-9 <= scale <= 1.0 + 1e-9
        check("risk_scale", d.risk_scale is None or scale_ok,
              f"risk_scale {scale:g} must be between 0.25 and 1.0 of configured risk (never more)")
        applied_scale = scale if scale_ok else 1.0
        style = (d.tp_management or "").lower()
        if d.tp_management is not None and style not in ("fixed", "partial", "trailing"):
            check("tp_management", False, "tp_management must be fixed, partial or trailing")
            style = ""
        minutes: Optional[float] = None
        abandon: Optional[float] = None
        if ctx.is_plan:
            minutes = float(r.get("plan_default_valid_minutes", 60)) if d.valid_minutes is None else float(d.valid_minutes)
            check("plan_lifetime", 5 <= minutes <= 240,
                  f"plan lifetime {minutes:g} minutes is outside 5-240")
            abandon = float(d.abandon_price) if d.abandon_price is not None else float(tp)
            beyond = abandon > ctx.price if long_ else abandon < ctx.price
            check("abandon_beyond_price", beyond,
                  "abandon level must be beyond current price (it defaults to the target); otherwise the idea is already dead")

        # --- sizing & balance --------------------------------------------------------------------
        sz = compute_position(ctx.equity, float(r["risk_per_trade_percent"]) * applied_scale, entry, sl,
                              ctx.instrument, float(r["max_leverage"]),
                              float(r.get("max_position_notional_usdt", 0) or 0), self.fee_round_trip)
        check("position_size", sz.ok, sz.reason)
        leverage = 1
        if sz.ok:
            leverage = max(1, math.ceil(sz.notional / max(ctx.available_balance * 0.9, 1e-9)))
            check("leverage", leverage <= float(r["max_leverage"]),
                  f"required leverage {leverage}x exceeds max {r['max_leverage']}x")
            check("balance", ctx.available_balance > 0 and sz.notional / max(leverage, 1) <= ctx.available_balance * 0.95,
                  "insufficient available balance for required margin")

        if fails:
            res.approved = False
            log.info("RISK REJECT %s %s: %s", d.decision, d.setup_type, "; ".join(fails))
            return res
        res.approved = True
        res.order = OrderParams(
            side="Buy" if long_ else "Sell", direction=d.decision,
            order_type="Market" if d.entry_type == "MARKET" else "Limit",
            qty=sz.qty, entry_price=entry, stop_loss=sl, take_profit=tp,
            leverage=leverage, risk_amount=sz.risk_amount, risk_reward=rr, notional=sz.notional,
            valid_minutes=minutes, abandon_price=abandon, tp_management=style, risk_scale=applied_scale)
        log.info("RISK APPROVE %s qty=%s entry=%s sl=%s tp=%s rr=%.2f risk=%.2f",
                 d.decision, sz.qty, entry, sl, tp, rr, sz.risk_amount)
        return res
