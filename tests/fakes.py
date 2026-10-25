"""Shared test doubles: a controllable market and exchange helpers (real PaperExchange logic underneath)."""
import copy
from datetime import datetime, timezone

from bot.config import Config, DEFAULTS
from bot.database.database import Database
from bot.exchange.base import ExchangeError
from bot.exchange.paper import PaperExchange
from bot.execution.executor import Executor
from bot.execution.state import HaltState
from bot.risk.limits import LimitTracker
from bot.risk.position_sizing import InstrumentInfo
from bot.risk.risk_engine import OrderParams

INST = InstrumentInfo("BTCUSDT", 0.1, 0.001, 0.001, 100, 5)


class FakeMarket:
    def __init__(self, price=100_000.0):
        self.price = price
        self.candles = {}

    def set(self, price):
        self.price = price

    def get_instrument(self, symbol):
        return INST

    def get_ticker(self, symbol):
        return {"symbol": symbol, "price": self.price, "bid": self.price - 0.5, "ask": self.price + 0.5,
                "mark": self.price, "index": self.price, "funding_rate": 0.0001, "open_interest": 1000.0}

    def get_klines(self, symbol, interval, limit):
        return self.candles.get(interval, [])[-limit:]


def make_cfg(mode="PAPER", trading=False, auto=False, testnet=True, **overrides):
    raw = copy.deepcopy(DEFAULTS)
    raw["mode"] = mode
    for path, val in overrides.items():
        sec, key = path.split("__")
        raw[sec][key] = val
    return Config(raw=raw, trading_enabled=trading, autonomous_mode=auto, bybit_testnet_flag=testnet)


def params(direction="LONG", entry=100_000.5, qty=0.166, order_type="Market", sl=99_700.0, tp=100_700.0, lev=2):
    if direction == "SHORT":
        sl, tp, entry = 100_300.0, 99_300.0, 99_999.5
    side = "Buy" if direction == "LONG" else "Sell"
    return OrderParams(side=side, direction=direction, order_type=order_type, qty=qty, entry_price=entry,
                       stop_loss=sl, take_profit=tp, leverage=lev, risk_amount=50.0, risk_reward=2.3,
                       notional=qty * entry)


class Rig:
    """Everything wired together around a PaperExchange."""
    def __init__(self, ex_cls=PaperExchange, cfg=None, price=100_000.0, clock=None, **cfg_over):
        self.cfg = cfg or make_cfg(**cfg_over)
        self.market = FakeMarket(price)
        self.t = [1_000_000.0]
        self.clock = clock or (lambda: self.t[0])
        self.ex = ex_cls(self.market, 10_000.0, 0.055, 0.02, 0.0, clock=self.clock)
        self.db = Database(":memory:")
        self.limits = LimitTracker(dict(self.cfg.risk), 10_000.0)
        self.halt = HaltState(self.db)
        self.sleeps = []
        self.executor = Executor(self.cfg, self.ex, self.db, self.limits, self.halt, None,
                                 sleep=lambda s: self.sleeps.append(s) or self.advance(s), clock=self.clock)

    def advance(self, s):
        self.t[0] += s

    def move(self, price, high=None, low=None):
        self.market.set(price)
        t = self.market.get_ticker("BTCUSDT")
        self.ex.process_tick(high or price, low or price, t["bid"], t["ask"])


class NoStopExchange(PaperExchange):
    """Exchange that silently drops stop losses and refuses to set them (worst case for protection logic)."""
    def place_order(self, **p):
        p = dict(p, stopLoss=None)
        return super().place_order(**p)

    def set_trading_stop(self, symbol, stop_loss=None, take_profit=None):
        raise ExchangeError("trading-stop rejected", 10001)
