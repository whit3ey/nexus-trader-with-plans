"""SQLite persistence. Timestamps are stored in UTC. No secrets are ever stored here."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_snapshots (id INTEGER PRIMARY KEY, ts REAL, symbol TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS ai_decisions (id INTEGER PRIMARY KEY, ts REAL, symbol TEXT, model TEXT,
    decision TEXT, confidence REAL, data TEXT, accepted INTEGER, rejection_reason TEXT);
CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY, ts REAL, symbol TEXT, direction TEXT,
    accepted INTEGER, reason TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, created_ts REAL, symbol TEXT, direction TEXT,
    purpose TEXT, order_link_id TEXT UNIQUE, exchange_order_id TEXT, side TEXT, order_type TEXT,
    qty REAL, price REAL, status TEXT, mode TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS executions (id INTEGER PRIMARY KEY, ts REAL, symbol TEXT, order_link_id TEXT,
    exec_id TEXT UNIQUE, price REAL, qty REAL, fee REAL, data TEXT);
CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, open_ts REAL, close_ts REAL, symbol TEXT,
    direction TEXT, mode TEXT, entry REAL, exit REAL, qty REAL, stop_loss REAL, take_profit REAL,
    risk_amount REAL, fees REAL, pnl REAL, r_multiple REAL, exit_reason TEXT, order_link_id TEXT,
    ai_confidence REAL, status TEXT, context TEXT);
CREATE TABLE IF NOT EXISTS positions (id INTEGER PRIMARY KEY, ts REAL, symbol TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS daily_stats (day TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS risk_events (id INTEGER PRIMARY KEY, ts REAL, level TEXT, kind TEXT, message TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS errors (id INTEGER PRIMARY KEY, ts REAL, component TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS system_events (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, message TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_ts);
CREATE INDEX IF NOT EXISTS idx_trades_open ON trades(open_ts);
"""


def _j(obj: Any) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"))


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(args))
            self._conn.commit()
            return cur

    def _all(self, sql: str, args: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(args)).fetchall()]

    def integrity_ok(self) -> bool:
        with self._lock:
            return self._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    # --- key/value ---------------------------------------------------------------
    def kv_set(self, key: str, value: Any) -> None:
        self._exec("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, _j(value)))

    def kv_get(self, key: str, default: Any = None) -> Any:
        rows = self._all("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    # --- logging-type records ----------------------------------------------------------
    def record_snapshot(self, symbol: str, data: Dict[str, Any]) -> None:
        self._exec("INSERT INTO market_snapshots(ts,symbol,data) VALUES(?,?,?)", (time.time(), symbol, _j(data)))

    def record_ai_decision(self, symbol: str, model: str, decision: Optional[Dict[str, Any]],
                           accepted: bool, rejection_reason: str = "") -> None:
        d = decision or {}
        self._exec("INSERT INTO ai_decisions(ts,symbol,model,decision,confidence,data,accepted,rejection_reason)"
                   " VALUES(?,?,?,?,?,?,?,?)",
                   (time.time(), symbol, model, d.get("decision"), d.get("confidence"), _j(d),
                    int(accepted), rejection_reason))

    def record_signal(self, symbol: str, direction: Optional[str], accepted: bool, reason: str,
                      data: Optional[Dict[str, Any]] = None) -> None:
        self._exec("INSERT INTO signals(ts,symbol,direction,accepted,reason,data) VALUES(?,?,?,?,?,?)",
                   (time.time(), symbol, direction, int(accepted), reason, _j(data or {})))

    def record_risk_event(self, kind: str, message: str, level: str = "INFO", data: Optional[Dict[str, Any]] = None) -> None:
        self._exec("INSERT INTO risk_events(ts,level,kind,message,data) VALUES(?,?,?,?,?)",
                   (time.time(), level, kind, message, _j(data or {})))

    def record_error(self, component: str, message: str) -> None:
        self._exec("INSERT INTO errors(ts,component,message) VALUES(?,?,?)", (time.time(), component, message[:2000]))

    def record_event(self, kind: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        self._exec("INSERT INTO system_events(ts,kind,message,data) VALUES(?,?,?,?)",
                   (time.time(), kind, message, _j(data or {})))

    def record_position(self, symbol: str, data: Dict[str, Any]) -> None:
        self._exec("INSERT INTO positions(ts,symbol,data) VALUES(?,?,?)", (time.time(), symbol, _j(data)))

    # --- orders ---------------------------------------------------------------------------
    def insert_order(self, symbol: str, direction: str, purpose: str, order_link_id: str, side: str,
                     order_type: str, qty: float, price: Optional[float], status: str, mode: str,
                     data: Optional[Dict[str, Any]] = None) -> bool:
        """Returns False (and stores nothing) if order_link_id already exists."""
        try:
            self._exec("INSERT INTO orders(created_ts,symbol,direction,purpose,order_link_id,side,order_type,qty,price,status,mode,data)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (time.time(), symbol, direction, purpose, order_link_id, side, order_type, qty, price,
                        status, mode, _j(data or {})))
            return True
        except sqlite3.IntegrityError:
            return False

    def update_order(self, order_link_id: str, status: str, exchange_order_id: Optional[str] = None) -> None:
        self._exec("UPDATE orders SET status=?, exchange_order_id=COALESCE(?,exchange_order_id) WHERE order_link_id=?",
                   (status, exchange_order_id, order_link_id))

    def recent_orders(self, symbol: str, since_seconds: int = 3600) -> List[Dict[str, Any]]:
        return self._all("SELECT * FROM orders WHERE symbol=? AND created_ts>=? ORDER BY id DESC",
                         (symbol, time.time() - since_seconds))

    def order_by_link_id(self, order_link_id: str) -> Optional[Dict[str, Any]]:
        rows = self._all("SELECT * FROM orders WHERE order_link_id=?", (order_link_id,))
        return rows[0] if rows else None

    def insert_execution(self, symbol: str, order_link_id: str, exec_id: str, price: float, qty: float,
                         fee: float, data: Optional[Dict[str, Any]] = None) -> bool:
        try:
            self._exec("INSERT INTO executions(ts,symbol,order_link_id,exec_id,price,qty,fee,data) VALUES(?,?,?,?,?,?,?,?)",
                       (time.time(), symbol, order_link_id, exec_id, price, qty, fee, _j(data or {})))
            return True
        except sqlite3.IntegrityError:
            return False

    # --- trades -------------------------------------------------------------------------------
    def open_trade(self, **f: Any) -> int:
        cols = ("open_ts", "symbol", "direction", "mode", "entry", "qty", "stop_loss", "take_profit",
                "risk_amount", "order_link_id", "ai_confidence", "context")
        vals = [f.get(c) for c in cols]
        vals[0] = vals[0] or time.time()
        vals[-1] = _j(f.get("context") or {})
        cur = self._exec(f"INSERT INTO trades({','.join(cols)},status) VALUES({','.join('?' * len(cols))},'open')", vals)
        return int(cur.lastrowid)

    def close_trade(self, trade_id: int, exit_price: float, pnl: float, fees: float, r_multiple: float,
                    exit_reason: str) -> None:
        self._exec("UPDATE trades SET close_ts=?, exit=?, pnl=?, fees=?, r_multiple=?, exit_reason=?, status='closed' WHERE id=?",
                   (time.time(), exit_price, pnl, fees, r_multiple, exit_reason, trade_id))

    def open_trades(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        if symbol:
            return self._all("SELECT * FROM trades WHERE status='open' AND symbol=?", (symbol,))
        return self._all("SELECT * FROM trades WHERE status='open'")

    def closed_trades(self, limit: int = 500) -> List[Dict[str, Any]]:
        return self._all("SELECT * FROM trades WHERE status='closed' ORDER BY id DESC LIMIT ?", (limit,))

    def save_daily_stats(self, day: str, data: Dict[str, Any]) -> None:
        self._exec("INSERT INTO daily_stats(day,data) VALUES(?,?) ON CONFLICT(day) DO UPDATE SET data=excluded.data",
                   (day, _j(data)))

    def load_daily_stats(self, day: str) -> Optional[Dict[str, Any]]:
        rows = self._all("SELECT data FROM daily_stats WHERE day=?", (day,))
        return json.loads(rows[0]["data"]) if rows else None

    def recent(self, table: str, limit: int = 50) -> List[Dict[str, Any]]:
        if table not in ("ai_decisions", "signals", "risk_events", "errors", "system_events", "orders", "trades"):
            raise ValueError("unknown table")
        return self._all(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,))

    def update_trade(self, trade_id: int, **fields: Any) -> None:
        allowed = {"stop_loss", "take_profit", "qty", "context", "entry", "risk_amount"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update {bad}")
        if "context" in fields:
            fields["context"] = _j(fields["context"])
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE trades SET {sets} WHERE id=?", [*fields.values(), trade_id])

    def get_trade(self, trade_id: int) -> Optional[Dict[str, Any]]:
        rows = self._all("SELECT * FROM trades WHERE id=?", (trade_id,))
        if not rows:
            return None
        t = rows[0]
        try:
            t["context"] = json.loads(t.get("context") or "{}")
        except ValueError:
            t["context"] = {}
        return t
