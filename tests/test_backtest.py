import unittest
from unittest import mock

from bot.backtest.engine import (BTTrade, Backtester, aggregate, compute_metrics, rule_based_decision,
                                 synthetic_candles, format_result, ASSUMPTIONS)
from bot.strategy.signals import Setup
from bot.strategy.types import Candle
from tests.fakes import make_cfg
from tests.helpers import c


def trade(pnl, r):
    return BTTrade("LONG", 0, 100, 99, 99, 102, 1, 1, pnl=pnl, r=r)


class MetricsTests(unittest.TestCase):
    def test_metrics_hand_calculation(self):
        ts = [trade(20, 2), trade(-10, -1), trade(-10, -1), trade(30, 3), trade(-10, -1)]
        m = compute_metrics(ts, 1000.0)
        self.assertEqual((m["total_trades"], m["wins"], m["losses"]), (5, 2, 3))
        self.assertEqual(m["win_rate_pct"], 40.0)
        self.assertAlmostEqual(m["profit_factor"], 50 / 30, places=3)
        self.assertEqual(m["max_consecutive_losses"], 2)
        self.assertEqual(m["total_r"], 2.0)
        self.assertEqual(m["average_r"], 0.4)
        self.assertEqual(m["expectancy_r"], 0.4)
        self.assertEqual(m["net_pnl"], 20.0)
        # equity: 1000 ->1020 ->1010 ->1000 ->1030 ->1020 ; worst dd from 1020 to 1000
        self.assertAlmostEqual(m["max_drawdown_pct"], (1020 - 1000) / 1020 * 100, places=2)

    def test_empty_and_all_wins(self):
        self.assertEqual(compute_metrics([], 1000)["total_trades"], 0)
        m = compute_metrics([trade(10, 1)], 1000)
        self.assertEqual(m["profit_factor"], float("inf"))


class AggregateTests(unittest.TestCase):
    def test_complete_groups_only_and_ohlc(self):
        cs = [c(i, 100 + i, 101 + i, 99 + i, 100.5 + i, 1) for i in range(30)]
        h = aggregate(cs, 12)
        self.assertEqual(len(h), 2)                              # 30 candles -> 2 complete 12-groups (aligned to epoch)
        self.assertEqual(h[0].open, 100.0)
        self.assertEqual(h[0].high, max(x.high for x in cs[:12]))
        self.assertEqual(h[0].close, cs[11].close)
        self.assertEqual(h[0].volume, 12.0)

    def test_misaligned_start_drops_partial_group(self):
        cs = [c(i, 100, 101, 99, 100, 1, start=300_000 * 5) for i in range(30)]   # starts mid-bucket
        for g in aggregate(cs, 12):
            self.assertEqual(g.ts % (12 * 300_000), 0)


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_stop_assumed_first_when_candle_touches_both(self):
        bt = Backtester(self.cfg, [])
        t = BTTrade("LONG", 0, 100, 95, 95, 110, 1, 5.0, entry_fee=0.05)
        self.assertTrue(bt._process(t, c(1, 100, 112, 90, 100)))
        self.assertEqual(t.reason, "SL")
        self.assertLess(t.pnl, 0)

    def test_target_hit_and_fees(self):
        bt = Backtester(self.cfg, [])
        t = BTTrade("LONG", 0, 100, 95, 95, 110, 2, 10.0, entry_fee=0.11)
        self.assertTrue(bt._process(t, c(1, 100, 111, 99, 105)))
        self.assertEqual((t.reason, t.exit), ("TP", 110))
        self.assertAlmostEqual(t.pnl, 20 - t.fees)
        self.assertGreater(t.fees, 0.11)

    def test_untouched_candle_keeps_position(self):
        bt = Backtester(self.cfg, [])
        t = BTTrade("SHORT", 0, 100, 105, 105, 90, 1, 5.0)
        self.assertFalse(bt._process(t, c(1, 100, 104, 96, 100)))

    def test_rule_based_decision_geometry(self):
        snap = {"fvg_5m": [{"direction": "bullish", "lower": 99.0, "upper": 100.0, "distance": 0.0}], "order_block_5m": [], "order_block_1h": []}
        d = rule_based_decision(snap, "LONG", 2.0, 10.0, 100.5)
        self.assertLess(d.stop_loss, 100.5)
        self.assertGreaterEqual(100.5 - d.stop_loss, 10.0)           # at least 1 ATR
        self.assertAlmostEqual((d.take_profit - 100.5) / (100.5 - d.stop_loss), 2.02)
        s = rule_based_decision({"fvg_5m": [], "order_block_5m": [], "order_block_1h": []}, "SHORT", 2.0, 10.0, 100.0)
        self.assertEqual(s.stop_loss, 115.0)
        self.assertAlmostEqual(s.take_profit, 100 - 2.02 * 15)

    def test_full_run_invariants_with_forced_setups(self):
        candles = synthetic_candles(1500, seed=11)
        always = Setup(True, "LONG", ["forced for test"], 5)
        with mock.patch("bot.backtest.engine.evaluate_setup", return_value=always):
            res = Backtester(self.cfg, candles, 10_000, step=2, warmup=800).run()
        self.assertGreater(len(res.trades), 0)
        last_exit = 0
        daily = {}
        for t in res.trades:
            self.assertGreaterEqual(t.stop, t.initial_stop)           # a trailing stop only ever tightens
            self.assertLess(t.initial_stop, t.entry)
            self.assertGreaterEqual(t.entry_ts, last_exit)            # never two positions at once
            last_exit = t.exit_ts
            self.assertLess(t.risk_amount, 10_000 * 0.005 * 1.05)     # risk budget (incl. fees) respected
            if t.reason == "SL":
                self.assertGreaterEqual(t.r, -1.35)                    # slippage on the stop adds a little beyond -1R
            day = t.entry_ts // 86_400_000
            daily[day] = daily.get(day, 0) + 1
        self.assertLessEqual(max(daily.values()), self.cfg.risk["max_daily_trades"])
        self.assertAlmostEqual(res.end_equity, 10_000 + sum(t.pnl for t in res.trades), places=6)

    def test_is_deterministic(self):
        candles = synthetic_candles(1300, seed=3)
        always = Setup(True, "SHORT", ["forced"], 5)
        with mock.patch("bot.backtest.engine.evaluate_setup", return_value=always):
            a = Backtester(self.cfg, candles, step=3, warmup=800).run()
            b = Backtester(self.cfg, candles, step=3, warmup=800).run()
        self.assertEqual(a.metrics, b.metrics)

    def test_real_strategy_runs_without_error_on_synthetic_data(self):
        res = Backtester(self.cfg, synthetic_candles(1400, seed=5), step=4, warmup=800, synthetic=True).run()
        self.assertGreaterEqual(res.metrics["total_trades"], 0)
        text = format_result(res)
        self.assertIn("SYNTHETIC", text)
        self.assertIn("ASSUMPTIONS AND LIMITATIONS", text)

    def test_report_always_states_limits(self):
        res = Backtester(self.cfg, synthetic_candles(900, seed=2), step=5, warmup=800).run()
        self.assertIn("Past performance", format_result(res))
        self.assertIn("AI is not used", ASSUMPTIONS)


if __name__ == "__main__":
    unittest.main()
