"""Real-time Bybit streams (pybit). Optional: if pybit is missing the bot reports the
WebSocket as 'unavailable' and (in TESTNET/LIVE) refuses to open new trades unless
execution.require_websocket is set to false. REST remains the source of truth."""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple

from ..logging_config import get_logger

log = get_logger("bot.exchange")

try:  # pragma: no cover - depends on installed package
    from pybit.unified_trading import WebSocket as _PybitWS
except Exception:  # pybit not installed
    _PybitWS = None


class StreamMonitor:
    STALE_AFTER = 45.0

    def __init__(self, symbol: str, testnet: bool, credentials: Optional[Tuple[str, str]], interval: str = "5"):
        self.symbol, self.testnet, self.creds, self.interval = symbol, testnet, credentials, interval
        self.available = _PybitWS is not None
        self.started = False
        self.last_msg = 0.0
        self.error = "" if self.available else "pybit not installed"
        self.ticker: Dict[str, Any] = {}
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.position_updates = 0
        self.execution_updates = 0
        self._lock = threading.Lock()
        self._pub = self._priv = None

    # callbacks -------------------------------------------------------------------------------
    def _touch(self) -> None:
        self.last_msg = time.time()

    def _on_ticker(self, msg: Dict[str, Any]) -> None:
        with self._lock:
            self.ticker.update(msg.get("data", {}) or {})
            self._touch()

    def _on_kline(self, msg: Dict[str, Any]) -> None:
        self._touch()

    def _on_order(self, msg: Dict[str, Any]) -> None:
        with self._lock:
            for o in msg.get("data", []) or []:
                if o.get("orderLinkId"):
                    self.orders[o["orderLinkId"]] = o
            self._touch()

    def _on_position(self, msg: Dict[str, Any]) -> None:
        self.position_updates += 1
        self._touch()

    def _on_execution(self, msg: Dict[str, Any]) -> None:
        self.execution_updates += 1
        self._touch()

    # lifecycle ---------------------------------------------------------------------------------
    def start(self) -> bool:
        if not self.available:
            return False
        try:
            self._pub = _PybitWS(testnet=self.testnet, channel_type="linear")
            self._pub.ticker_stream(symbol=self.symbol, callback=self._on_ticker)
            self._pub.kline_stream(interval=int(self.interval), symbol=self.symbol, callback=self._on_kline)
            if self.creds:
                self._priv = _PybitWS(testnet=self.testnet, channel_type="private",
                                      api_key=self.creds[0], api_secret=self.creds[1])
                self._priv.order_stream(callback=self._on_order)
                self._priv.position_stream(callback=self._on_position)
                self._priv.execution_stream(callback=self._on_execution)
            self.started = True
            self.last_msg = time.time()
            return True
        except Exception as e:
            self.error = f"{type(e).__name__}: {str(e)[:120]}"
            log.error("WebSocket start failed: %s", self.error)
            return False

    def stop(self) -> None:
        for ws in (self._pub, self._priv):
            try:
                if ws is not None:
                    ws.exit()
            except Exception:
                pass
        self.started = False

    def state(self) -> str:
        if not self.available:
            return "unavailable"
        if not self.started:
            return "not_started"
        return "healthy" if time.time() - self.last_msg < self.STALE_AFTER else "unhealthy"

    def order_update(self, link: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.orders.get(link)
