import unittest
from bot.strategy.types import Candle
from bot.strategy.liquidity import equal_levels, detect_liquidity, detect_sweep
from tests.helpers import c

HOUR = 3_600_000
DAY0 = 1_790_000_000_000 - (1_790_000_000_000 % 86_400_000)  # a UTC midnight


def zigzag(highs, lows, step=HOUR, start=DAY0):
    return [Candle(start + i * step, (h + l) / 2, h, l, (h + l) / 2, 1.0) for i, (h, l) in enumerate(zip(highs, lows))]


class LiquidityTests(unittest.TestCase):
    def test_equal_levels_cluster(self):
        self.assertEqual(equal_levels([100.0, 100.05, 105.0], 0.1), [100.025])
        self.assertEqual(equal_levels([100, 105, 110], 0.1), [])

    def test_equal_highs_and_swings_detected(self):
        highs = [100] * 20
        lows = [90] * 20
        highs[5], highs[12] = 110, 110.04
        res = detect_liquidity(zigzag(highs, lows), atr=10.0, lookback=2, equal_tol_atr=0.15)
        self.assertIn(110.0, res["swing_highs"])
        self.assertEqual(len(res["equal_highs"]), 1)
        self.assertAlmostEqual(res["equal_highs"][0], 110.02, places=2)

    def test_previous_session_high_low(self):
        n = 60  # 1h candles from UTC midnight -> spans 2.5 days
        highs, lows = [100] * n, [90] * n
        highs[30], lows[35] = 150, 60      # both on day 2 (index 24-47)
        res = detect_liquidity(zigzag(highs, lows), atr=10.0, lookback=2)
        self.assertEqual(res["prev_session_high"], 150)
        self.assertEqual(res["prev_session_low"], 60)

    def test_range_extremes(self):
        highs, lows = [100] * 60, [90] * 60
        highs[-3] = 130
        res = detect_liquidity(zigzag(highs, lows), atr=10.0, range_bars=48)
        self.assertEqual(res["range_high"], 130)
        self.assertEqual(res["range_low"], 90)

    def test_sweep_of_highs_is_bearish_hint(self):
        liq = {"swing_highs": [110.0], "equal_highs": [], "swing_lows": [], "equal_lows": []}
        candles = [c(-1, 100, 101, 99, 100), c(0, 100, 105, 99, 104), c(1, 104, 108, 103, 107), c(2, 107, 111, 106, 108)]  # wick 111, close 108
        s = detect_sweep(candles, liq)
        self.assertTrue(s["swept"])
        self.assertEqual((s["side"], s["direction_hint"], s["level"]), ("highs", "SHORT", 110.0))

    def test_sweep_of_lows_is_bullish_hint(self):
        liq = {"swing_highs": [], "equal_highs": [], "swing_lows": [90.0], "equal_lows": []}
        candles = [c(-1, 100, 101, 99, 100), c(0, 100, 101, 95, 96), c(1, 96, 97, 92, 93), c(2, 93, 96, 88, 94)]  # wick 88, close 94
        s = detect_sweep(candles, liq)
        self.assertEqual((s["swept"], s["direction_hint"]), (True, "LONG"))

    def test_breakout_with_close_beyond_is_not_a_sweep(self):
        liq = {"swing_highs": [110.0], "equal_highs": [], "swing_lows": [], "equal_lows": []}
        candles = [c(-1, 100, 101, 99, 100), c(0, 100, 105, 99, 104), c(1, 104, 108, 103, 107), c(2, 107, 114, 106, 113)]
        self.assertFalse(detect_sweep(candles, liq)["swept"])

    def test_insufficient_data_returns_empty(self):
        res = detect_liquidity(zigzag([100] * 4, [90] * 4), atr=10.0, lookback=3)
        self.assertEqual(res["swing_highs"], [])


if __name__ == "__main__":
    unittest.main()
