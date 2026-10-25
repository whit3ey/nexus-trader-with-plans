import unittest

from bot.exchange.base import ExchangeError
from bot.exchange.paper import PaperExchange
from bot.execution.emergency import close_position_reduce_only, emergency_close
from bot.execution.reconciliation import reconcile
from tests.fakes import Rig, params

TS = 1_790_000_000_000


class EmergencyTests(unittest.TestCase):
    def test_closes_position_cancels_orders_halts_and_records(self):
        r = Rig()
        r.executor.open_position(params(), TS, 85)
        r.ex.place_order(symbol="BTCUSDT", side="Buy", orderType="Limit", qty=0.01, price=90_000.0, orderLinkId="lim1")
        rep = emergency_close(r.ex, "BTCUSDT", r.db, r.halt, None, sleep=lambda s: None)
        self.assertTrue(rep["ok"])
        self.assertEqual(r.ex.get_positions("BTCUSDT"), [])
        self.assertEqual(r.ex.get_open_orders("BTCUSDT"), [])
        self.assertTrue(r.halt.halted)
        self.assertEqual(len(r.db.recent("system_events")), 2)           # halt + emergency record
        self.assertEqual(r.db.recent("risk_events")[0]["kind"], "emergency_close")

    def test_close_order_is_reduce_only_and_opposite_side(self):
        r = Rig()
        r.executor.open_position(params("SHORT"), TS, 85)
        sent = []
        orig = r.ex.place_order
        r.ex.place_order = lambda **p: sent.append(p) or orig(**p)
        self.assertTrue(close_position_reduce_only(r.ex, "BTCUSDT", lambda s: None))
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]["reduceOnly"])
        self.assertEqual(sent[0]["side"], "Buy")

    def test_never_opens_a_position_when_flat(self):
        r = Rig()
        rep = emergency_close(r.ex, "BTCUSDT", r.db, r.halt, None, sleep=lambda s: None)
        self.assertTrue(rep["ok"])
        self.assertEqual(r.ex.executions, [])
        self.assertEqual(r.ex.get_positions("BTCUSDT"), [])
        # even a direct reduce-only call cannot open anything
        with self.assertRaises(ExchangeError):
            r.ex.place_order(symbol="BTCUSDT", side="Sell", orderType="Market", qty=0.01, reduceOnly=True, orderLinkId="x")

    def test_failure_to_close_is_reported_not_hidden(self):
        class Stuck(PaperExchange):
            def place_order(self, **p):
                if p.get("reduceOnly"):
                    raise ExchangeError("exchange down", None, True)
                return super().place_order(**p)
        r = Rig(ex_cls=Stuck)
        r.executor.open_position(params(), TS, 85)
        rep = emergency_close(r.ex, "BTCUSDT", r.db, r.halt, None, sleep=lambda s: None)
        self.assertFalse(rep["ok"])
        self.assertIn("NOT CONFIRMED", " ".join(rep["steps"]))
        self.assertTrue(r.halt.halted)                                    # still halted even if close failed

    def test_halt_persists_in_database(self):
        r = Rig()
        emergency_close(r.ex, "BTCUSDT", r.db, r.halt, None, sleep=lambda s: None)
        from bot.execution.state import HaltState
        self.assertTrue(HaltState(r.db).halted)


class ReconcileTests(unittest.TestCase):
    def test_consistent_state(self):
        r = Rig()
        r.executor.open_position(params(), TS, 85)
        rep = reconcile(r.ex, r.db, "BTCUSDT")
        self.assertTrue(rep.certain)
        self.assertFalse(rep.unexpected_position)
        self.assertFalse(rep.unprotected_position)

    def test_exchange_unreadable_is_uncertain(self):
        class Down(PaperExchange):
            def get_positions(self, s):
                raise ExchangeError("timeout", None, True)
        r = Rig(ex_cls=Down)
        rep = reconcile(r.ex, r.db, "BTCUSDT")
        self.assertFalse(rep.certain)
        self.assertIn("cannot read", rep.error)

    def test_unexpected_position_flagged_and_unprotected_detected(self):
        r = Rig()
        r.ex.positions["BTCUSDT"] = {"symbol": "BTCUSDT", "side": "Sell", "size": 0.1, "avgPrice": 101_000.0,
                                     "stopLoss": 0.0, "takeProfit": 0.0, "fees": 0, "openTime": 0, "openLink": "m"}
        rep = reconcile(r.ex, r.db, "BTCUSDT")
        self.assertTrue(rep.unexpected_position and rep.unprotected_position and rep.certain)

    def test_trade_closed_while_offline_is_settled(self):
        r = Rig()
        res = r.executor.open_position(params(), TS, 85)
        r.move(99_000, low=99_000)                       # stop hit while the bot "was off"
        self.assertEqual(r.ex.get_positions("BTCUSDT"), [])
        rep = reconcile(r.ex, r.db, "BTCUSDT", executor_close_recorder=lambda t, note: r.executor.record_close(r.db.get_trade(t["id"]), note))
        self.assertEqual(rep.closed_offline, [res.trade_id])
        t = r.db.closed_trades()[0]
        self.assertEqual(t["status"], "closed")
        self.assertLess(t["pnl"], 0)
        self.assertEqual(r.db.open_trades(), [])

    def test_unknown_opening_order_is_left_untouched_and_known_one_restored(self):
        r = Rig()
        r.ex.place_order(symbol="BTCUSDT", side="Sell", orderType="Limit", qty=0.01, price=110_000.0, orderLinkId="stranger")
        r.executor.open_position(params(order_type="Limit", entry=99_000.0, sl=98_700.0, tp=99_900.0), TS, 82)
        rep = reconcile(r.ex, r.db, "BTCUSDT")
        self.assertEqual(rep.cancelled_orders, [])
        self.assertTrue(rep.unexpected_order)
        self.assertTrue(rep.pending_link.startswith("ait-"))
        self.assertEqual(len(r.ex.get_open_orders("BTCUSDT")), 2)

    def test_stuck_local_order_status_is_refreshed(self):
        r = Rig()
        r.db.insert_order("BTCUSDT", "LONG", "open", "ait-zzz", "Buy", "Market", 0.01, None, "submitted", "PAPER")
        reconcile(r.ex, r.db, "BTCUSDT")
        self.assertEqual(r.db.order_by_link_id("ait-zzz")["status"], "not_found")


if __name__ == "__main__":
    unittest.main()
