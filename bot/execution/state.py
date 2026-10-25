"""Persistent halt flag. Emergency stops and alarms survive restarts and need an explicit resume."""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

from ..database.database import Database


class HaltState:
    KEY = "halt_state"

    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()
        s = db.kv_get(self.KEY, {}) or {}
        self.halted: bool = bool(s.get("halted", False))
        self.reason: str = s.get("reason", "")
        self.since: float = float(s.get("since", 0.0))
        self.paused_by_user: bool = False   # soft pause from dashboard (not persisted)

    def halt(self, reason: str) -> None:
        with self._lock:
            self.halted, self.reason, self.since = True, reason, time.time()
            self.db.kv_set(self.KEY, {"halted": True, "reason": reason, "since": self.since})
        self.db.record_event("halt", reason)

    def clear(self) -> None:
        with self._lock:
            self.halted, self.reason = False, ""
            self.db.kv_set(self.KEY, {"halted": False, "reason": "", "since": 0})
        self.db.record_event("resume", "halt cleared by user")

    def blocking_reason(self) -> Optional[str]:
        if self.halted:
            return f"HALTED: {self.reason}"
        if self.paused_by_user:
            return "paused by user"
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {"halted": self.halted, "reason": self.reason, "since": self.since, "paused": self.paused_by_user}
