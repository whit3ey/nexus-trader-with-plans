"""Rotating log files with automatic secret redaction."""
from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable, Set

_SECRETS: Set[str] = set()
_PATTERNS = [
    re.compile(r"(?i)(api[_-]?secret|secret|bot_token|token|signature|password)(['\"]?\s*[:=]\s*['\"]?)([^\s'\",}&]+)"),
    re.compile(r"(?i)(api[_-]?key|x-bapi-api-key)(['\"]?\s*[:=]\s*['\"]?)([^\s'\",}&]+)"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),  # Telegram bot token shape
]


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        _SECRETS.add(value)


def register_env_secrets() -> None:
    for name in ("BYBIT_API_KEY", "BYBIT_API_SECRET", "BYBIT_TESTNET_API_KEY",
                 "BYBIT_TESTNET_API_SECRET", "TELEGRAM_BOT_TOKEN"):
        register_secret(os.environ.get(name))


def redact(text: str) -> str:
    out = str(text)
    for s in sorted(_SECRETS, key=len, reverse=True):
        out = out.replace(s, "***REDACTED***")
    out = _PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}***REDACTED***", out)
    out = _PATTERNS[1].sub(lambda m: f"{m.group(1)}{m.group(2)}***REDACTED***", out)
    out = _PATTERNS[2].sub("***REDACTED***", out)
    return out


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:
            record.msg = "<unprintable log message>"
            record.args = ()
        return True


LOG_FILES = {
    "app": "app.log", "trading": "trading.log", "errors": "errors.log",
    "ai": "ai.log", "risk": "risk.log",
}
_FMT = logging.Formatter("%(asctime)sZ | %(levelname)-7s | %(name)s | %(message)s")
_FMT.converter = __import__("time").gmtime  # UTC timestamps


def _handler(path: Path, level: int, max_bytes: int, backups: int) -> logging.Handler:
    h = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    h.setLevel(level)
    h.setFormatter(_FMT)
    h.addFilter(RedactingFilter())
    return h


def setup_logging(directory: str | Path, level: str = "INFO", max_bytes: int = 5_242_880,
                  backup_count: int = 5, console: bool = True) -> None:
    """Configure logger tree: bot (app.log+errors.log), bot.trading, bot.ai, bot.risk."""
    register_env_secrets()
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    lvl = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger("bot")
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.setLevel(lvl)
    root.propagate = False
    root.addHandler(_handler(d / LOG_FILES["app"], lvl, max_bytes, backup_count))
    root.addHandler(_handler(d / LOG_FILES["errors"], logging.ERROR, max_bytes, backup_count))
    if console:
        ch = logging.StreamHandler()
        ch.setLevel(lvl)
        ch.setFormatter(_FMT)
        ch.addFilter(RedactingFilter())
        root.addHandler(ch)
    for name, fname in (("bot.trading", "trading.log"), ("bot.ai", "ai.log"), ("bot.risk", "risk.log")):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
            h.close()
        lg.setLevel(lvl)
        lg.addHandler(_handler(d / fname, lvl, max_bytes, backup_count))
        lg.propagate = True  # also appears in app.log


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("bot") else f"bot.{name}")


def shutdown_logging() -> None:
    """Flush and detach all file handlers (used on clean shutdown and in tests)."""
    for name in ("bot", "bot.trading", "bot.ai", "bot.risk"):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            try:
                h.flush()
                h.close()
            except Exception:
                pass
            lg.removeHandler(h)
