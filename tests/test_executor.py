import unittest

from bot.exchange.base import ExchangeError
from bot.exchange.paper import PaperExchange
from bot.execution.executor import compute_trailing_stop, infer_exit_reason
from tests.fakes import Rig, NoStopExchange, params, make_cfg

TS = 1_790_000_000_000


class TrailingMathTests(unittest.TestCase):
    def test_long_trails_up_only_after_activation(self):
        self.assertIsNone(compute_trailing_stop("Buy", 99_700, 100_000, 300, 100_200, 200, 0.9, 1.0, 0.1))  # only 0.67R
        self.assertEqual(compute_trailing_stop("Buy", 99_700, 100_000, 300, 100_400, 200, 0.9, 1.0, 0.1), 100_220.0)

    def test_long_never_moves_down(self):
        self.assertIsNone(compute_trailing_stop("Buy", 100_220, 100_000, 300, 100_300, 200, 0.9, 1.0, 0.1))  # cand 100120 < sl
        self.assertIsNone(compute_trailing_stop("Buy", 100_220, 100_000, 300, 100_400, 200, 0.9, 1.0, 0.1))  # equal => not tighter

    def test_short_trails_down_only(self):
        self.assertEqual(compute_trailing_stop("Sell", 100_300, 100_000, 300, 99_600, 200, 0.9, 1.0, 0.1), 99_780.0)
        self.assertIsNone(compute_trailing_stop("Sell", 99_780, 100_000, 300, 99_700, 200, 0.9, 1.0, 0.1))

    def test_invalid_inputs_return_none(self):
        self.assertIsNone(compute_trailing_stop("Buy", 0, 100, 5, 110, 1, 0.9, 1.0, 0.1))
        self.assertIsNone(compute_trailing_stop("Buy", 95, 100, 0, 110, 1, 0.9, 1.0, 0.1))

    def test_exit_reason_inference(self):
        self.assertEqual(infer_exit_reason("Buy", 100_700, 99_700, 100_700, 100_000, 0.1), "TP")
        self.assertEqual(infer_exit_reason("Buy", 99_700, 99_700, 100_700, 100_000, 0.1), "SL")
        self.assertEqual(infer_exit_reason("Buy", 100_220, 100_220, 100_700, 100_000, 0.1), "TRAILING_STOP")
        self.assertEqual(infer_exit_reason("Buy", 100_300, 99_700, 100_700, 100_000, 0.1), "MANUAL_OR_OTHER")


class OpenTradeTests(unittest.TestCase):
    def test_market_entry_is_filled_protected_and_recorded(self):
        r = Rig()
        res = r.executor.open_position(params(), TS, 85.0, {"setup": "t"})
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.status, "filled")
        pos = r.ex.get_positions("BTCUSDT")[0]
        self.assertEqual((pos["stopLoss"], pos["takeProfit"]), (99_700.0, 100_700.0))
        t = r.db.get_trade(res.trade_id)
        self.assertEqual((t["direction"], t["status"], t["ai_confidence"]), ("LONG", "open", 85.0))
        self.assertGreater(t["risk_amount"], 40)
        self.assertEqual(t["context"]["initial_sl"], 99_700.0)
        self.assertEqual(r.limits.stats.trades, 1)
        self.assertEqual(r.db.order_by_link_id(res.link)["status"], "filled")

    def test_same_signal_twice_is_not_submitted_twice(self):
        r = Rig()
        self.assertTrue(r.executor.open_position(params(), TS, 85).ok)
        again = r.executor.open_position(params(), TS, 85)
        self.assertFalse(again.ok)
        self.assertEqual(again.status, "duplicate")
        self.assertEqual(len(r.ex.executions), 1)

    def test_same_candle_after_close_still_cannot_repeat(self):
        r = Rig()
        first = r.executor.open_position(params(), TS, 85)
        r.move(99_000, low=99_000)       # stop hit -> flat
        self.assertEqual(r.ex.get_positions("BTCUSDT"), [])
        again = r.executor.open_position(params(), TS, 85)   # duplicate candle event
        self.assertEqual(again.status, "duplicate")
        self.assertEqual(len(r.db.recent_orders("BTCUSDT")), 1)

    def test_blocked_unless_permission_gate_open(self):
        r = Rig(cfg=make_cfg(mode="TESTNET", trading=False, auto=False, testnet=True))
        res = r.executor.open_position(params(), TS, 85)
        self.assertEqual(res.status, "blocked")
        self.assertEqual(r.ex.executions, [])

    def test_blocked_while_halted_or_paused(self):
        r = Rig()
        r.halt.halt("test")
        self.assertEqual(r.executor.open_position(params(), TS, 85).status, "blocked")
        r.halt.clear()
        r.halt.paused_by_user = True
        self.assertEqual(r.executor.open_position(params(), TS, 85).status, "blocked")

    def test_exchange_state_unreadable_means_no_order(self):
        class Broken(PaperExchange):
            def get_positions(self, symbol):
                raise ExchangeError("timeout", None, True)
        r = Rig(ex_cls=Broken)
        res = r.executor.open_position(params(), TS, 85)
        self.assertEqual(res.status, "uncertain")
        self.assertEqual(r.ex.executions, [])
        self.assertEqual(r.db.recent_orders("BTCUSDT"), [])

    def test_rejected_order_is_recorded_not_retried_blindly(self):
        class Rejecting(PaperExchange):
            def place_order(self, **p):
                raise ExchangeError("insufficient margin", 110007)
        r = Rig(ex_cls=Rejecting)
        res = r.executor.open_position(params(), TS, 85)
        self.assertEqual(res.status, "rejected")
        self.assertEqual(r.db.order_by_link_id(res.link)["status"], "rejected")

    def test_network_error_with_order_not_found_is_clean_failure(self):
        class Flaky(PaperExchange):
            def place_order(self, **p):
                raise ExchangeError("timeout", None, True)
        r = Rig(ex_cls=Flaky)
        res = r.executor.open_position(params(), TS, 85)
        self.assertEqual(res.status, "error")
        self.assertEqual(r.db.order_by_link_id(res.link)["status"], "failed")

    def test_network_error_but_order_actually_placed_is_adopted(self):
        class PlacedThenError(PaperExchange):
            def place_order(self, **p):
                super().place_order(**p)
                raise ExchangeError("timeout after send", None, True)
        r = Rig(ex_cls=PlacedThenError)
        res = r.executor.open_position(params(), TS, 85)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(len(r.ex.executions), 1)

    def test_unprotectable_position_is_closed_and_trading_halted(self):
        r = Rig(ex_cls=NoStopExchange)
        res = r.executor.open_position(params(), TS, 85)
        self.assertFalse(res.ok)
        self.assertEqual(res.status, "unprotected_closed")
        self.assertEqual(r.ex.get_positions("BTCUSDT"), [])       # flat again
        self.assertTrue(r.halt.halted)
        self.assertIn("stop loss", r.halt.reason)
        self.assertEqual(r.db.open_trades(), [])
        # the closing order must be reduce-only (2 executions: open + close)
        self.assertEqual(len(r.ex.executions), 2)

    def test_limit_entry_pending_then_filled_then_protected(self):
        r = Rig()
        p = params(order_type="Limit", entry=99_900.0, sl=99_600.0, tp=100_600.0)
        res = r.executor.open_position(p, TS, 82)
        self.assertEqual(res.status, "pending")
        self.assertIsNone(r.executor.service_pending())           # not filled yet
        r.move(99_850.0, high=99_950, low=99_850)                  # price trades through the limit
        done = r.executor.service_pending()
        self.assertTrue(done.ok, done.reason)
        self.assertEqual(r.ex.get_positions("BTCUSDT")[0]["stopLoss"], 99_600.0)
        self.assertIsNone(r.executor.pending)

    def test_limit_entry_expires_and_is_cancelled(self):
        r = Rig()
        res = r.executor.open_position(params(order_type="Limit", entry=99_000.0, sl=98_700.0, tp=99_900.0), TS, 82)
        self.assertEqual(res.status, "pending")
        r.advance(r.executor.limit_ttl + 1)
        out = r.executor.service_pending()
        self.assertEqual(out.status, "rejected")
        self.assertEqual(r.ex.get_open_orders("BTCUSDT"), [])
        self.assertIsNone(r.executor.pending)

    def test_pending_entry_blocks_second_entry(self):
        r = Rig()
        r.executor.open_position(params(order_type="Limit", entry=99_900.0, sl=99_600.0, tp=100_600.0), TS, 82)
        res = r.executor.open_position(params(), TS + 300_000, 85)
        self.assertEqual(res.status, "duplicate")


class ManageAndCloseTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig()
        self.res = self.r.executor.open_position(params(), TS, 85)
        self.assertTrue(self.res.ok)

    def test_trailing_stop_ratchets_and_never_loosens(self):
        r = self.r
        r.move(100_450)                       # ~1.5R
        pos = r.ex.get_positions("BTCUSDT")[0]
        acts = r.executor.manage_position(pos, atr=200.0)
        self.assertTrue(any("trailing" in a for a in acts), acts)
        sl1 = r.ex.get_positions("BTCUSDT")[0]["stopLoss"]
        self.assertGreater(sl1, 99_700.0)
        r.move(100_300)                        # pullback
        r.executor.manage_position(r.ex.get_positions("BTCUSDT")[0], atr=200.0)
        self.assertEqual(r.ex.get_positions("BTCUSDT")[0]["stopLoss"], sl1)   # unchanged
        r.move(100_600)
        r.executor.manage_position(r.ex.get_positions("BTCUSDT")[0], atr=200.0)
        self.assertGreater(r.ex.get_positions("BTCUSDT")[0]["stopLoss"], sl1)

    def test_missing_stop_is_restored(self):
        r = self.r
        r.ex.positions["BTCUSDT"]["stopLoss"] = 0.0     # someone removed the stop
        acts = r.executor.manage_position(r.ex.get_positions("BTCUSDT")[0], atr=200.0)
        self.assertTrue(any("restored" in a for a in acts))
        self.assertEqual(r.ex.get_positions("BTCUSDT")[0]["stopLoss"], 99_700.0)

    def test_stop_loss_hit_is_recorded_with_negative_r(self):
        r = self.r
        r.move(99_600, low=99_600)
        closed = r.executor.detect_closed(r.ex.get_positions("BTCUSDT"))
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["reason"], "SL")
        self.assertLess(closed[0]["r"], 0)
        self.assertGreater(closed[0]["r"], -1.6)   # fees push a stop-out slightly beyond -1R
        t = r.db.closed_trades()[0]
        self.assertEqual((t["status"], t["exit"] < t["entry"]), ("closed", True))
        self.assertEqual((r.limits.stats.losses, r.limits.stats.consecutive_losses), (1, 1))

    def test_take_profit_hit_is_recorded_with_positive_r(self):
        r = self.r
        r.move(100_800, high=100_800)
        closed = r.executor.detect_closed(r.ex.get_positions("BTCUSDT"))
        self.assertEqual(closed[0]["reason"], "TP")
        self.assertGreater(closed[0]["r"], 1.5)
        self.assertEqual(r.limits.stats.wins, 1)

    def test_no_close_detected_while_position_open(self):
        self.assertEqual(self.r.executor.detect_closed(self.r.ex.get_positions("BTCUSDT")), [])


class PartialTPTests(unittest.TestCase):
    def test_partial_close_then_break_even_stop(self):
        r = Rig(**{"tp__mode": "partial", "trailing__enabled": False})
        r.executor.open_position(params(qty=0.1), TS, 85)
        r.move(100_320)                                   # > 1R (risk dist ~300.5)
        acts = r.executor.manage_position(r.ex.get_positions("BTCUSDT")[0], atr=200.0)
        self.assertTrue(any("partial" in a for a in acts), acts)
        pos = r.ex.get_positions("BTCUSDT")[0]
        self.assertAlmostEqual(pos["size"], 0.05)
        self.assertGreaterEqual(pos["stopLoss"], 100_000.0)
        again = r.executor.manage_position(pos, atr=200.0)  # only once
        self.assertFalse(any("partial" in a for a in again))


if __name__ == "__main__":
    unittest.main()
