import logging
import tempfile
import unittest
from pathlib import Path

from bot.database.database import Database
from bot.logging_config import redact, register_secret, setup_logging, get_logger, shutdown_logging


class LoggingTests(unittest.TestCase):
    def tearDown(self):
        shutdown_logging()

    def test_registered_secrets_and_patterns_are_redacted(self):
        register_secret("SUPERSECRETVALUE123")
        self.assertNotIn("SUPERSECRETVALUE123", redact("auth failed for SUPERSECRETVALUE123 today"))
        self.assertNotIn("abcd1234efgh", redact("api_secret=abcd1234efgh"))
        self.assertNotIn("abcd1234efgh", redact('{"api_key": "abcd1234efgh"}'))
        tg = "123456789:AAH" + "x" * 32
        self.assertNotIn(tg, redact(f"token {tg}"))

    def test_log_files_created_rotated_and_never_contain_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            register_secret("TOPSECRET-KEY-98765")
            setup_logging(d, "INFO", max_bytes=2000, backup_count=2, console=False)
            get_logger("bot.risk").info("secret leaked? TOPSECRET-KEY-98765")
            get_logger("bot.ai").info("prompt with api_key=zzzzzzzzzzzz")
            get_logger("bot.trading").info("order placed")
            get_logger("bot").error("boom TOPSECRET-KEY-98765")
            for h in logging.getLogger("bot").handlers + logging.getLogger("bot.risk").handlers:
                h.flush()
            names = {p.name for p in Path(d).iterdir()}
            for n in ("app.log", "errors.log", "risk.log", "ai.log", "trading.log"):
                self.assertIn(n, names)
            blob = "".join(p.read_text() for p in Path(d).iterdir())
            self.assertNotIn("TOPSECRET-KEY-98765", blob)
            self.assertNotIn("zzzzzzzzzzzz", blob)
            self.assertIn("REDACTED", blob)
            setup_logging(d, "INFO", console=False)  # re-init is safe (closes old handlers)

    def test_rotation_happens(self):
        with tempfile.TemporaryDirectory() as d:
            setup_logging(d, "INFO", max_bytes=1000, backup_count=2, console=False)
            for i in range(200):
                get_logger("bot").info("line %d %s", i, "x" * 50)
            self.assertTrue((Path(d) / "app.log.1").exists())


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def test_integrity_and_kv(self):
        self.assertTrue(self.db.integrity_ok())
        self.db.kv_set("a", {"x": 1})
        self.db.kv_set("a", {"x": 2})
        self.assertEqual(self.db.kv_get("a"), {"x": 2})
        self.assertEqual(self.db.kv_get("missing", 7), 7)

    def test_duplicate_order_link_id_rejected_by_database(self):
        args = ("BTCUSDT", "LONG", "open", "lid-1", "Buy", "Market", 0.01, None, "new", "PAPER")
        self.assertTrue(self.db.insert_order(*args))
        self.assertFalse(self.db.insert_order(*args))
        self.assertEqual(len(self.db.recent_orders("BTCUSDT")), 1)
        self.db.update_order("lid-1", "filled", "ex-9")
        row = self.db.order_by_link_id("lid-1")
        self.assertEqual((row["status"], row["exchange_order_id"]), ("filled", "ex-9"))

    def test_duplicate_execution_ignored(self):
        self.assertTrue(self.db.insert_execution("BTCUSDT", "lid-1", "e1", 100.0, 0.01, 0.05))
        self.assertFalse(self.db.insert_execution("BTCUSDT", "lid-1", "e1", 100.0, 0.01, 0.05))

    def test_trade_lifecycle(self):
        tid = self.db.open_trade(symbol="BTCUSDT", direction="LONG", mode="PAPER", entry=100.0, qty=1.0,
                                 stop_loss=95.0, take_profit=110.0, risk_amount=5.0, order_link_id="lid-1",
                                 ai_confidence=80, context={"why": "test"})
        self.assertEqual(len(self.db.open_trades("BTCUSDT")), 1)
        self.db.close_trade(tid, 110.0, 10.0, 0.1, 2.0, "TP")
        self.assertEqual(self.db.open_trades(), [])
        t = self.db.closed_trades()[0]
        self.assertEqual((t["exit_reason"], t["r_multiple"], t["pnl"]), ("TP", 2.0, 10.0))

    def test_daily_stats_roundtrip_and_event_tables(self):
        self.db.save_daily_stats("2026-10-06", {"trades": 3})
        self.db.save_daily_stats("2026-10-06", {"trades": 4})
        self.assertEqual(self.db.load_daily_stats("2026-10-06"), {"trades": 4})
        self.db.record_ai_decision("BTCUSDT", "qwen3", {"decision": "WAIT", "confidence": 10}, False, "WAIT")
        self.db.record_risk_event("reject", "stale data", "WARN")
        self.db.record_error("ollama", "timeout")
        self.db.record_event("start", "bot started")
        for table in ("ai_decisions", "risk_events", "errors", "system_events"):
            self.assertEqual(len(self.db.recent(table)), 1, table)
        with self.assertRaises(ValueError):
            self.db.recent("sqlite_master")

    def test_file_database_persists_across_connections(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "t.db"
            a = Database(p)
            a.kv_set("k", 5)
            a.close()
            self.assertEqual(Database(p).kv_get("k"), 5)


if __name__ == "__main__":
    unittest.main()
