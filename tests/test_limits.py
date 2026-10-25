import unittest
from datetime import datetime, timezone, timedelta
from bot.config import DEFAULTS
from bot.risk.limits import LimitTracker

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def tracker(equity=10_000.0):
    return LimitTracker(dict(DEFAULTS["risk"]), equity, now=NOW)


class LimitTests(unittest.TestCase):
    def test_clean_state_not_halted(self):
        self.assertEqual(tracker().halt_reasons(), [])

    def test_daily_loss_limit_halts(self):
        t = tracker()
        t.update_equity(9_810)           # -1.9%
        self.assertEqual(t.halt_reasons(), [])
        t.update_equity(9_790)           # -2.1%
        self.assertTrue(any("daily loss" in r for r in t.halt_reasons()))

    def test_realized_loss_counts_even_if_equity_recovers_unrealised(self):
        t = tracker()
        t.record_trade_closed(-210, 1, -1.0)
        t.update_equity(10_000)          # equity looks fine, realised does not
        self.assertTrue(any("daily loss" in r for r in t.halt_reasons()))

    def test_max_daily_trades(self):
        t = tracker()
        for _ in range(9):
            t.record_trade_opened()
        self.assertEqual(t.halt_reasons(), [])
        t.record_trade_opened()
        self.assertTrue(any("daily trades" in r for r in t.halt_reasons()))

    def test_consecutive_losses_then_win_resets(self):
        t = tracker()
        for _ in range(3):
            t.record_trade_closed(-10, 0.5, -1)
        self.assertEqual(t.halt_reasons(), [])
        t.record_trade_closed(20, 0.5, 2)
        self.assertEqual(t.stats.consecutive_losses, 0)
        for _ in range(4):
            t.record_trade_closed(-10, 0.5, -1)
        self.assertTrue(any("consecutive" in r for r in t.halt_reasons()))

    def test_consecutive_loss_limit_can_be_disabled(self):
        cfg = dict(DEFAULTS["risk"], max_consecutive_losses=0)
        t = LimitTracker(cfg, 10_000, now=NOW)
        for _ in range(10):
            t.record_trade_closed(-1, 0, -0.1)
        self.assertFalse(any("consecutive" in r for r in t.halt_reasons()))

    def test_account_drawdown_from_peak(self):
        t = tracker()
        t.update_equity(12_000)          # new peak
        t.update_equity(10_700)          # 10.83% below peak
        self.assertAlmostEqual(t.drawdown_pct(), (12_000 - 10_700) / 12_000 * 100)
        self.assertTrue(any("drawdown" in r for r in t.halt_reasons()))

    def test_stats_win_rate_and_average_r(self):
        t = tracker()
        t.record_trade_closed(30, 1, 2.0)
        t.record_trade_closed(-15, 1, -1.0)
        t.record_trade_closed(30, 1, 2.0)
        self.assertAlmostEqual(t.stats.win_rate, 66.6667, places=3)
        self.assertAlmostEqual(t.stats.average_r, 1.0)
        self.assertAlmostEqual(t.stats.fees, 3.0)

    def test_day_rollover_resets_counters_but_keeps_losing_streak(self):
        t = tracker()
        t.record_trade_opened()
        t.record_trade_closed(-10, 0, -1)
        rolled = t.roll_day_if_needed(9_990, now=NOW + timedelta(days=1))
        self.assertTrue(rolled)
        self.assertEqual(t.stats.trades, 0)
        self.assertEqual(t.stats.start_equity, 9_990)
        self.assertEqual(t.stats.consecutive_losses, 1)
        self.assertFalse(t.roll_day_if_needed(9_990, now=NOW + timedelta(days=1, hours=1)))


if __name__ == "__main__":
    unittest.main()
