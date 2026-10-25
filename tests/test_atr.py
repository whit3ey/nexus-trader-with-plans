import unittest
from tests.helpers import c, flat
from bot.strategy.atr import true_ranges, atr_series, latest_atr, volatility_ratio


class ATRTests(unittest.TestCase):
    def test_constant_range_gives_atr_equal_range(self):
        s = atr_series(flat(30), 14)
        self.assertIsNone(s[12])
        self.assertAlmostEqual(s[13], 10.0)
        self.assertAlmostEqual(s[-1], 10.0)

    def test_wilder_smoothing_matches_hand_calculation(self):
        candles = flat(14) + [c(14, 100, 112, 88, 100)]  # TR = 24
        self.assertEqual(true_ranges(candles)[-1], 24.0)
        self.assertAlmostEqual(latest_atr(candles, 14), (10 * 13 + 24) / 14)

    def test_gap_up_uses_previous_close(self):
        candles = [c(0, 100, 101, 99, 100), c(1, 110, 112, 109, 111)]
        self.assertEqual(true_ranges(candles)[1], 12.0)  # |high - prev close|

    def test_not_enough_data(self):
        self.assertIsNone(latest_atr(flat(5), 14))

    def test_volatility_ratio_flags_spike(self):
        candles = flat(60)
        for k in range(3):
            i = 60 + k
            candles.append(c(i, 100, 140, 60, 100))
        self.assertGreater(volatility_ratio(candles, 14), 1.5)
        self.assertAlmostEqual(volatility_ratio(flat(60), 14), 1.0)


if __name__ == "__main__":
    unittest.main()
