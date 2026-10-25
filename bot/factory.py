"""Builds the real components for the configured mode (PAPER / TESTNET / LIVE)."""
from __future__ import annotations

from typing import Any, Dict, Optional

from .ai.decision_engine import DecisionEngine
from .ai.ollama_client import OllamaClient
from .alerts import Alerts
from .config import Config
from .database.database import Database
from .engine import TradingEngine
from .exchange.bybit_client import BybitExchange
from .exchange.paper import PaperExchange
from .exchange.websocket import StreamMonitor
from .hardware import resolve_model
from .logging_config import get_logger

log = get_logger("bot")


def build_exchanges(cfg: Config):
    """Returns (market_source, trading_exchange). PAPER reads LIVE public prices but never sends orders."""
    e = cfg.section("execution")
    if cfg.mode == "PAPER":
        market = BybitExchange(testnet=False, credentials=None, category=cfg.raw["category"])
        ex = PaperExchange(market, float(cfg.raw["paper"]["starting_equity"]), float(e["taker_fee_percent"]),
                           float(e["maker_fee_percent"]), float(e["slippage_percent_paper"]))
        return market, ex
    creds = cfg.credentials()       # TESTNET: testnet keys first; LIVE: ONLY the live keys
    ex = BybitExchange(testnet=cfg.mode == "TESTNET", credentials=creds, category=cfg.raw["category"])
    return ex, ex


def build_engine(cfg: Config, start_streams: bool = True) -> TradingEngine:
    db = Database(cfg.path(cfg.raw["database"]["path"]))
    market, ex = build_exchanges(cfg)
    ai = cfg.ai
    client = OllamaClient(ai["host"], timeout=float(ai["timeout_seconds"]), max_retries=int(ai["max_retries"]))
    model = resolve_model(ai["model"], cfg.root)
    decider = DecisionEngine(client, model, ai)
    alerts = Alerts.from_env(cfg.display_mode, bool(cfg.raw["alerts"].get("telegram_enabled")))
    streams: Optional[StreamMonitor] = None
    if cfg.mode != "PAPER":
        streams = StreamMonitor(cfg.symbol, cfg.mode == "TESTNET", cfg.credentials(), cfg.section("timeframes")["entry"])
        if start_streams:
            streams.start()
    return TradingEngine(cfg, db, ex, market, decider, alerts, streams, model)
