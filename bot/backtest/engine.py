"""Event-driven backtester that reuses the LIVE strategy code, risk engine, sizing, fees and trailing logic.

IMPORTANT LIMITATION: the local AI is NOT used here (it is slow and not reproducible). A transparent
rule-based stand-in turns the deterministic setup into an order. Results therefore describe the
deterministic strategy + risk layer only, not what the AI would have decided.
"""
from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..ai.schemas import AIDecision
from ..config import Config
from ..execution.executor import compute_trailing_stop
from ..risk.limits import LimitTracker
from ..risk.position_sizing import InstrumentInfo, round_to_tick
from ..risk.risk_engine import RiskContext, RiskEngine
from ..strategy.atr import latest_atr
from ..strategy.signals import build_snapshot, evaluate_setup
from ..strategy.types import Candle

ASSUMPTIONS = """ASSUMPTIONS AND LIMITATIONS (read before trusting any number)
 - The local AI is not used. A fixed rule-based stand-in converts each deterministic setup into an order
   (entry at next candle open, stop beyond the nearest aligned zone / at least 1 ATR, take profit at the configured R multiple).
 - Candle data only: intrabar order is unknown. If a candle touches both stop and target, the STOP is assumed to hit first.
 - Fees: taker fee on entries and stop exits, maker fee on take-profits. Slippage applied to entries and stop exits.
 - No funding payments, no liquidations, no partial fills, no order-book depth, no latency, no outages.
 - One position at a time. Daily/drawdown/consecutive-loss limits and risk sizing are the same code used live.
 - Past performance on historical data is NOT evidence of future profit. Parameters tuned on the same data overfit.
 - A small number of trades gives statistically meaningless averages."""


@dataclass
class BTTrade:
    direction: str
    entry_ts: int
    entry: float
    stop: float
    initial_stop: float
    target: float
    qty: float
    risk_amount: float
    exit_ts: int = 0
    exit: float = 0.0
    reason: str = ""
    pnl: float = 0.0
    fees: float = 0.0
    r: float = 0.0
    entry_fee: float = 0.0


@dataclass
class BacktestResult:
    trades: List[BTTrade]
    metrics: Dict[str, Any]
    start_equity: float
    end_equity: float
    candles: int
    synthetic: bool = False


def aggregate(candles: Sequence[Candle], factor: int, base_ms: int = 300_000) -> List[Candle]:
    """Build higher-timeframe candles from 5M candles. Only COMPLETE groups, aligned to the epoch."""
    span = factor * base_ms
    out: List[Candle] = []
    group: List[Candle] = []
    bucket: Optional[int] = None
    for c in candles:
        b = c.ts // span
        if bucket is not None and b != bucket:
            if len(group) == factor:
                out.append(Candle(bucket * span, group[0].open, max(x.high for x in group), min(x.low for x in group),
                                  group[-1].close, sum(x.volume for x in group)))
            group = []
        bucket = b
        group.append(c)
    if len(group) == factor:
        out.append(Candle(bucket * span, group[0].open, max(x.high for x in group), min(x.low for x in group),
                          group[-1].close, sum(x.volume for x in group)))
    return out


def compute_metrics(trades: Sequence[BTTrade], start_equity: float) -> Dict[str, Any]:
    n = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    eq, peak, max_dd = start_equity, start_equity, 0.0
    streak = max_streak = 0
    for t in trades:
        eq += t.pnl
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100 if peak > 0 else 0)
        streak = streak + 1 if t.pnl <= 0 else 0
        max_streak = max(max_streak, streak)
    rs = [t.r for t in trades]
    return {
        "total_trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / n * 100, 2) if n else 0.0,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else (None if not gross_win else float("inf")),
        "max_drawdown_pct": round(max_dd, 2), "average_r": round(sum(rs) / n, 3) if n else 0.0,
        "total_r": round(sum(rs), 3), "expectancy_r": round(sum(rs) / n, 3) if n else 0.0,
        "max_consecutive_losses": max_streak, "net_pnl": round(sum(t.pnl for t in trades), 2),
        "total_fees": round(sum(t.fees for t in trades), 2),
        "return_pct": round(sum(t.pnl for t in trades) / start_equity * 100, 2) if start_equity else 0.0,
    }


def rule_based_decision(snap: Dict[str, Any], direction: str, rr: float, atr: float, price: float) -> AIDecision:
    """Transparent stand-in for the AI: stop beyond the nearest aligned zone, at least 1 ATR, at most 3 ATR."""
    long_ = direction == "LONG"
    want = "bullish" if long_ else "bearish"
    zones = [z for z in (snap.get("fvg_5m") or []) + (snap.get("order_block_5m") or []) + (snap.get("order_block_1h") or [])
             if z["direction"] == want]
    dist = 1.5 * atr
    if zones:
        z = min(zones, key=lambda q: q["distance"])
        lo = z.get("low", z.get("lower"))
        hi = z.get("high", z.get("upper"))
        edge = (price - lo) if long_ else (hi - price)
        dist = min(max(edge + 0.25 * atr, 1.0 * atr), 3.0 * atr)
    sl = price - dist if long_ else price + dist
    margin = rr + 0.02                      # survives tick rounding of entry/stop/target
    tp = price + margin * dist if long_ else price - margin * dist
    return AIDecision(direction, 80.0, "MARKET", None, sl, tp, rr, "rule-based stand-in", "TREND", "rule_stand_in", ())


class Backtester:
    def __init__(self, cfg: Config, candles5: Sequence[Candle], start_equity: float = 10_000.0, step: int = 3,
                 rr: Optional[float] = None, warmup: int = 800, inst: Optional[InstrumentInfo] = None, synthetic: bool = False):
        self.cfg, self.c5, self.step, self.warmup = cfg, list(candles5), max(1, step), warmup
        self.start_equity = start_equity
        self.rr = rr or float(cfg.section("tp")["rr"])
        self.inst = inst or InstrumentInfo(cfg.symbol, 0.1, 0.001, 0.001, 1000, 5.0)
        e = cfg.section("execution")
        self.taker, self.maker = float(e["taker_fee_percent"]) / 100, float(e["maker_fee_percent"]) / 100
        self.slip = float(e["slippage_percent_paper"]) / 100
        self.risk = RiskEngine(cfg.risk, cfg.ai, cfg.section("tp"), fee_round_trip=2 * self.taker + 2 * self.slip)
        self.synthetic = synthetic

    def _close(self, t: BTTrade, ts: int, px: float, reason: str, fee_rate: float) -> None:
        d = 1 if t.direction == "LONG" else -1
        t.exit_ts, t.exit, t.reason = ts, px, reason
        t.fees = t.entry_fee + t.qty * px * fee_rate
        t.pnl = (px - t.entry) * t.qty * d - t.fees
        t.r = t.pnl / t.risk_amount if t.risk_amount > 0 else 0.0

    def _process(self, t: BTTrade, c: Candle) -> bool:
        long_ = t.direction == "LONG"
        hit_sl = c.low <= t.stop if long_ else c.high >= t.stop
        hit_tp = c.high >= t.target if long_ else c.low <= t.target
        if hit_sl:       # stop assumed first when both are touched (conservative)
            px = t.stop * ((1 - self.slip) if long_ else (1 + self.slip))
            gain = (t.stop > t.entry) if long_ else (t.stop < t.entry)
            self._close(t, c.ts, px, "TRAILING_STOP" if gain else "SL", self.taker)
            return True
        if hit_tp:
            self._close(t, c.ts, t.target, "TP", self.maker)
            return True
        return False

    def run(self) -> BacktestResult:
        c5 = self.c5
        tr_cfg = self.cfg.section("trailing")
        tick = self.inst.tick_size
        limits: Optional[LimitTracker] = None
        equity = self.start_equity
        trades: List[BTTrade] = []
        pos: Optional[BTTrade] = None
        for i in range(self.warmup, len(c5) - 1):
            c = c5[i]
            now = datetime.fromtimestamp(c.ts / 1000, tz=timezone.utc)
            if limits is None:
                limits = LimitTracker(dict(self.cfg.risk), equity, now=now)
            limits.roll_day_if_needed(equity, now)
            if pos is not None:
                if self._process(pos, c):
                    equity += pos.pnl
                    limits.record_trade_closed(pos.pnl, pos.fees, pos.r)
                    limits.update_equity(equity)
                    trades.append(pos)
                    pos = None
                elif tr_cfg["enabled"]:
                    window = c5[max(0, i - 200):i + 1]
                    atr = latest_atr(window, int(self.cfg.strategy["atr_period"])) or 0.0
                    side = "Buy" if pos.direction == "LONG" else "Sell"
                    new_sl = compute_trailing_stop(side, pos.stop, pos.entry, abs(pos.entry - pos.initial_stop), c.close, atr,
                                                   float(tr_cfg["atr_multiplier"]), float(tr_cfg["activate_at_rr"]), tick)
                    if new_sl is not None:
                        assert (new_sl > pos.stop) if pos.direction == "LONG" else (new_sl < pos.stop)   # never loosen
                        pos.stop = new_sl
            if pos is not None or (i - self.warmup) % self.step != 0:
                continue
            if limits.halt_reasons():
                continue
            window5 = c5[max(0, i - 299):i + 1]
            h1 = aggregate(c5[max(0, i - 299 * 12):i + 1], 12)
            h15 = aggregate(c5[max(0, i - 120 * 3):i + 1], 3)
            nxt = c5[i + 1]
            try:
                snap = build_snapshot(self.cfg.strategy, self.cfg.symbol, h1, window5, h15,
                                      {"price": c.close, "bid": c.close - 0.5, "ask": c.close + 0.5})
            except ValueError:
                continue
            setup = evaluate_setup(snap, self.cfg.strategy)
            # Plans rest as limits ahead of price. This backtest fills at the next open,
            # so it only replays trigger setups and leaves plans to the live engine.
            if not setup.has_setup or setup.kind == "plan":
                continue
            d = rule_based_decision(snap, setup.direction_hint, self.rr, snap["atr_5m"],
                                    nxt.open + 0.5 if setup.direction_hint == "LONG" else nxt.open - 0.5)
            ctx = RiskContext(price=nxt.open, bid=nxt.open - 0.5, ask=nxt.open + 0.5, atr=snap["atr_5m"], equity=equity,
                              available_balance=equity, instrument=self.inst, open_positions=0, data_age_seconds=0,
                              volatility_ratio=snap.get("volatility_ratio_5m") or None, trend_aligned=True, has_trigger=True)
            rr = self.risk.validate(d, ctx, limits)
            if not rr.approved:
                continue
            o = rr.order
            slip = (1 + self.slip) if o.direction == "LONG" else (1 - self.slip)
            entry = round_to_tick(nxt.open * slip, tick)
            pos = BTTrade(o.direction, nxt.ts, entry, o.stop_loss, o.stop_loss, o.take_profit, o.qty,
                          abs(entry - o.stop_loss) * o.qty + entry * o.qty * (2 * self.taker + self.slip),   # entry slippage is already inside |entry-stop|
                          entry_fee=o.qty * entry * self.taker)
            limits.record_trade_opened()
        if pos is not None:      # still open at the end of data: close at last close, flagged
            last = c5[-1]
            self._close(pos, last.ts, last.close, "END_OF_DATA", self.taker)
            equity += pos.pnl
            trades.append(pos)
        return BacktestResult(trades, compute_metrics(trades, self.start_equity), self.start_equity, equity, len(c5), self.synthetic)


# ------------------------------------------------------------------ data helpers
def load_csv(path: str | Path) -> List[Candle]:
    out = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            out.append(Candle(int(row["ts"]), float(row["open"]), float(row["high"]), float(row["low"]),
                              float(row["close"]), float(row.get("volume", 0) or 0)))
    out.sort(key=lambda c: c.ts)
    return out


def save_csv(path: str | Path, candles: Sequence[Candle]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.ts, c.open, c.high, c.low, c.close, c.volume])


def download_history(symbol: str, days: int, testnet: bool = False, category: str = "linear", progress=print) -> List[Candle]:
    """Public Bybit kline history, 5M, paged backwards (no credentials needed)."""
    import time
    from ..exchange.bybit_client import BybitExchange
    ex = BybitExchange(testnet=testnet, credentials=None, category=category)
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    got: Dict[int, Candle] = {}
    while end > start:
        batch = ex.get_klines(symbol, "5", 1000, end_ms=end)
        if not batch:
            break
        for c in batch:
            got[c.ts] = c
        end = batch[0].ts - 1
        progress(f"  downloaded {len(got)} candles ...")
        time.sleep(0.15)
    return [got[k] for k in sorted(got) if got[k].ts >= start]


def synthetic_candles(n: int, seed: int = 7, start: float = 60_000.0) -> List[Candle]:
    """Regime-switching random walk for demos/tests ONLY. Not market data."""
    rnd = random.Random(seed)
    t0 = 1_700_006_400_000
    out, p, drift, left = [], start, 0.0, 0
    for i in range(n):
        if left <= 0:
            drift, left = rnd.choice([-0.0004, 0.0, 0.0006]), rnd.randint(150, 600)
        left -= 1
        o = p
        cl = o * (1 + drift + rnd.gauss(0, 0.0018))
        h = max(o, cl) * (1 + abs(rnd.gauss(0, 0.0008)))
        l = min(o, cl) * (1 - abs(rnd.gauss(0, 0.0008)))
        out.append(Candle(t0 + i * 300_000, round(o, 1), round(h, 1), round(l, 1), round(cl, 1), rnd.uniform(50, 150)))
        p = cl
    return out


def format_result(res: BacktestResult) -> str:
    m = res.metrics
    pf = "n/a (no losing trades)" if m["profit_factor"] in (None, float("inf")) else m["profit_factor"]
    days = res.candles * 5 / 60 / 24
    lines = ["=" * 60, "BACKTEST RESULT" + ("  (SYNTHETIC DATA - NOT REAL MARKET DATA)" if res.synthetic else ""), "=" * 60,
             f"Candles: {res.candles} x 5M (~{days:.0f} days)",
             f"Start equity: {res.start_equity:,.2f}   End equity: {res.end_equity:,.2f}   Return: {m['return_pct']}%",
             f"Total trades: {m['total_trades']}   Wins: {m['wins']}   Losses: {m['losses']}",
             f"Win rate: {m['win_rate_pct']}%   Profit factor: {pf}",
             f"Max drawdown: {m['max_drawdown_pct']}%   Max consecutive losses: {m['max_consecutive_losses']}",
             f"Average R: {m['average_r']}   Total R: {m['total_r']}   Expectancy: {m['expectancy_r']} R/trade",
             f"Net P/L: {m['net_pnl']:,.2f}   Fees paid: {m['total_fees']:,.2f}"]
    if m["total_trades"] < 30:
        lines.append(f"WARNING: only {m['total_trades']} trades - far too few to conclude anything.")
    lines += ["", ASSUMPTIONS]
    return "\n".join(lines)
