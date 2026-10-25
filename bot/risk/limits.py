"""Daily/account limit tracking. Once a limit is hit, trading stops - no loss recovery."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utc_day(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


@dataclass
class DailyStats:
    day: str
    start_equity: float
    trades: int = 0
    wins: int = 0
    losses: int = 0
    realized_pnl: float = 0.0
    fees: float = 0.0
    consecutive_losses: int = 0
    r_multiples: List[float] = field(default_factory=list)
    max_drawdown_pct: float = 0.0

    @property
    def win_rate(self) -> float:
        closed = self.wins + self.losses
        return self.wins / closed * 100 if closed else 0.0

    @property
    def average_r(self) -> float:
        return sum(self.r_multiples) / len(self.r_multiples) if self.r_multiples else 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["win_rate"] = self.win_rate
        d["average_r"] = self.average_r
        return d


class LimitTracker:
    def __init__(self, risk_cfg: Dict[str, Any], start_equity: float, peak_equity: Optional[float] = None,
                 now: Optional[datetime] = None):
        self.cfg = risk_cfg
        self.stats = DailyStats(utc_day(now), start_equity)
        self.peak_equity = max(peak_equity or start_equity, start_equity)
        self.last_equity = start_equity
        self.unrealized_pnl = 0.0

    # --- state updates -----------------------------------------------------
    def roll_day_if_needed(self, equity: float, now: Optional[datetime] = None) -> bool:
        today = utc_day(now)
        if today != self.stats.day:
            carry = self.stats.consecutive_losses  # a losing streak survives midnight
            self.stats = DailyStats(today, equity, consecutive_losses=carry)
            return True
        return False

    def update_equity(self, equity: float, unrealized: float = 0.0) -> None:
        self.last_equity = equity
        self.unrealized_pnl = unrealized
        self.peak_equity = max(self.peak_equity, equity)
        if self.peak_equity > 0:
            dd = (self.peak_equity - equity) / self.peak_equity * 100
            self.stats.max_drawdown_pct = max(self.stats.max_drawdown_pct, dd)

    def record_trade_opened(self) -> None:
        self.stats.trades += 1

    def record_trade_closed(self, pnl: float, fees: float, r_multiple: float) -> None:
        self.stats.realized_pnl += pnl
        self.stats.fees += fees
        self.stats.r_multiples.append(r_multiple)
        if pnl > 0:
            self.stats.wins += 1
            self.stats.consecutive_losses = 0
        else:
            self.stats.losses += 1
            self.stats.consecutive_losses += 1

    # --- evaluation -----------------------------------------------------------
    def daily_loss_pct(self) -> float:
        s = self.stats
        if s.start_equity <= 0:
            return 0.0
        by_equity = (s.start_equity - self.last_equity) / s.start_equity * 100
        by_realized = -s.realized_pnl / s.start_equity * 100
        return max(by_equity, by_realized, 0.0)

    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max((self.peak_equity - self.last_equity) / self.peak_equity * 100, 0.0)

    def halt_reasons(self) -> List[str]:
        r = self.cfg
        out: List[str] = []
        if self.stats.trades >= int(r["max_daily_trades"]):
            out.append(f"max daily trades reached ({self.stats.trades}/{r['max_daily_trades']})")
        if self.daily_loss_pct() >= float(r["max_daily_loss_percent"]):
            out.append(f"daily loss limit reached ({self.daily_loss_pct():.2f}% >= {r['max_daily_loss_percent']}%)")
        if self.drawdown_pct() >= float(r["max_account_drawdown_percent"]):
            out.append(f"max account drawdown reached ({self.drawdown_pct():.2f}% >= {r['max_account_drawdown_percent']}%)")
        mcl = int(r.get("max_consecutive_losses", 0) or 0)
        if mcl and self.stats.consecutive_losses >= mcl:
            out.append(f"max consecutive losses reached ({self.stats.consecutive_losses}/{mcl})")
        return out

    def snapshot(self) -> Dict[str, Any]:
        d = self.stats.to_dict()
        d.update(daily_loss_pct=self.daily_loss_pct(), drawdown_pct=self.drawdown_pct(),
                 unrealized_pnl=self.unrealized_pnl, peak_equity=self.peak_equity,
                 halted=self.halt_reasons())
        return d
