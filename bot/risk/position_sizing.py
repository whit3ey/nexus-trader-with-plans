"""Mathematical position sizing from equity, risk % and stop distance (never 'x% of account')."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Optional


@dataclass(frozen=True)
class InstrumentInfo:
    symbol: str
    tick_size: float
    qty_step: float
    min_qty: float
    max_qty: float = 1e12
    min_notional: float = 5.0


@dataclass
class SizingResult:
    ok: bool
    qty: float = 0.0
    risk_amount: float = 0.0         # actual max loss at stop for rounded qty (before fees/slippage)
    notional: float = 0.0
    leverage_needed: float = 0.0
    reason: str = ""


def _d(x: float) -> Decimal:
    return Decimal(str(x))


def round_down_to_step(value: float, step: float) -> float:
    if step <= 0:
        raise ValueError("step must be positive")
    q = (_d(value) / _d(step)).to_integral_value(rounding=ROUND_DOWN) * _d(step)
    return float(q)


def round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        raise ValueError("tick must be positive")
    q = (_d(price) / _d(tick)).to_integral_value(rounding=ROUND_HALF_UP) * _d(tick)
    return float(q)


def risk_reward(side: str, entry: float, stop: float, target: float) -> float:
    """R multiple of the target. side: 'LONG'|'SHORT'. Returns 0 if the geometry is invalid."""
    if side == "LONG":
        risk, reward = entry - stop, target - entry
    elif side == "SHORT":
        risk, reward = stop - entry, entry - target
    else:
        raise ValueError("side must be LONG or SHORT")
    if risk <= 0 or reward <= 0:
        return 0.0
    return reward / risk


def compute_position(equity: float, risk_percent: float, entry: float, stop: float,
                     inst: InstrumentInfo, max_leverage: float,
                     max_notional: float = 0.0, fee_round_trip: float = 0.0) -> SizingResult:
    """fee_round_trip: fraction of notional paid in fees+slippage for entry AND exit (e.g. 0.0012).
    It is added to the per-unit loss so that stop-out loss INCLUDING fees stays within the risk budget."""
    if equity <= 0:
        return SizingResult(False, reason="equity must be positive")
    if entry <= 0 or stop <= 0:
        return SizingResult(False, reason="entry and stop must be positive")
    dist = abs(entry - stop)
    if dist <= 0:
        return SizingResult(False, reason="stop distance is zero")
    risk_budget = equity * risk_percent / 100.0
    raw_qty = risk_budget / (dist + entry * max(fee_round_trip, 0.0))
    # leverage / notional caps
    notional_cap = equity * max_leverage
    if max_notional and max_notional > 0:
        notional_cap = min(notional_cap, max_notional)
    capped_by = ""
    if raw_qty * entry > notional_cap:
        raw_qty = notional_cap / entry
        capped_by = "leverage/notional cap"
    qty = round_down_to_step(raw_qty, inst.qty_step)
    qty = min(qty, round_down_to_step(inst.max_qty, inst.qty_step))
    if qty < inst.min_qty or qty <= 0:
        return SizingResult(False, reason=f"calculated quantity {qty} is below the exchange minimum {inst.min_qty}"
                            + (f" ({capped_by})" if capped_by else "") +
                            " - the risk budget is too small for this stop distance")
    notional = qty * entry
    if notional < inst.min_notional:
        return SizingResult(False, reason=f"order value {notional:.2f} USDT is below minimum {inst.min_notional}")
    return SizingResult(True, qty=qty, risk_amount=qty * (dist + entry * max(fee_round_trip, 0.0)), notional=notional,
                        leverage_needed=notional / equity, reason=capped_by)
