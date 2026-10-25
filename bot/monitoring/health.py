"""Tracks the health of every dependency the trading loop relies on."""
from __future__ import annotations

import threading
import time
from typing import Any, Dict


class HealthMonitor:
    def __init__(self, max_data_age: float = 90.0, api_fail_threshold: int = 3):
        self._lock = threading.Lock()
        self.max_data_age = max_data_age
        self.api_fail_threshold = api_fail_threshold
        self.api_failures = 0
        self.api_last_ok = 0.0
        self.api_last_error = ""
        self.ollama_ok = None        # None = not checked yet
        self.ollama_error = ""
        self.ws_state = "not_started"   # not_started | healthy | unhealthy | unavailable
        self.data_ts = 0.0
        self.account_confirmed = False
        self.position_certain = False
        self.order_certain = True

    def api_success(self) -> None:
        with self._lock:
            self.api_failures, self.api_last_ok, self.api_last_error = 0, time.time(), ""

    def api_failure(self, err: str) -> None:
        with self._lock:
            self.api_failures += 1
            self.api_last_error = err[:200]

    def market_data(self, ts: float | None = None) -> None:
        with self._lock:
            self.data_ts = ts or time.time()

    def set_ollama(self, ok: bool, err: str = "") -> None:
        with self._lock:
            self.ollama_ok, self.ollama_error = ok, err

    @property
    def api_healthy(self) -> bool:
        return self.api_failures < self.api_fail_threshold

    def data_age(self) -> float:
        return time.time() - self.data_ts if self.data_ts else float("inf")

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"api_healthy": self.api_healthy, "api_failures": self.api_failures,
                    "api_last_error": self.api_last_error, "ollama_ok": self.ollama_ok,
                    "ollama_error": self.ollama_error, "ws_state": self.ws_state,
                    "data_age_s": None if not self.data_ts else round(time.time() - self.data_ts, 1),
                    "account_confirmed": self.account_confirmed, "position_certain": self.position_certain,
                    "order_certain": self.order_certain}
