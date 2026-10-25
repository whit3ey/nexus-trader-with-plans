"""Optional Telegram alerts. Best-effort: an alert failure never affects trading."""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, Optional

from .logging_config import get_logger, redact

log = get_logger("bot")

EVENTS = {"bot_started", "bot_stopped", "trade_opened", "trade_closed", "sl_hit", "tp_hit", "daily_loss_limit",
          "emergency_stop", "api_failure", "ai_failure", "unexpected_position", "system_error"}


class Alerts:
    def __init__(self, enabled: bool, token: str = "", chat_id: str = "", mode: str = "",
                 http: Optional[Callable[[str, bytes], None]] = None):
        self.enabled = bool(enabled and token and chat_id)
        self._token, self._chat, self.mode = token, chat_id, mode
        self._http = http or self._post
        self._last: Dict[str, float] = {}
        self.sent = []   # in-memory record (used by tests / dashboard)

    @classmethod
    def from_env(cls, mode: str, enabled_cfg: bool = False) -> "Alerts":
        env_on = os.environ.get("TELEGRAM_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
        return cls(env_on or enabled_cfg, os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                   os.environ.get("TELEGRAM_CHAT_ID", ""), mode)

    @staticmethod
    def _post(url: str, data: bytes) -> None:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=8).read()

    def send(self, event: str, message: str, dedupe_seconds: float = 0) -> bool:
        text = redact(f"[{self.mode}] {event}: {message}")[:3500]
        key = f"{event}:{message[:60]}"
        now = time.time()
        if dedupe_seconds and now - self._last.get(key, 0) < dedupe_seconds:
            return False
        self._last[key] = now
        self.sent.append((event, text))
        self.sent = self.sent[-100:]
        log.info("ALERT %s", text)
        if not self.enabled:
            return False
        try:
            url = f"https://api.telegram.org/bot{self._token}/sendMessage"
            self._http(url, urllib.parse.urlencode({"chat_id": self._chat, "text": text}).encode())
            return True
        except Exception as e:   # never let alerting break trading
            log.warning("telegram alert failed: %s", type(e).__name__)
            return False
