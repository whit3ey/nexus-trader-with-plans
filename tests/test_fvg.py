import unittest
from tests.helpers import c, flat
from bot.strategy.fvg import detect_fvgs, nearest_fvg


def bullish_gap_series(extra=0, tiny=False):
    s = flat(20)
    s.append(c(20, 100, 106, 99, 105))                  # a: high 106
    s.append(c(21, 105, 125, 104, 124))                 # b: displacement
    s.append(c(22, 124, 130, 107 if tiny else 115, 128))  # c: low 115 (gap 106-115) or 107 (tiny)
    for k in range(extra):
        s.append(c(23 + k, 128, 130, 124, 128))
    return s


class FVGTests(unittest.TestCase):
    def test_detects_bullish_fvg_with_boundaries(self):
        f = detect_fvgs(bullish_gap_series(), "5")
        self.assertEqual(len(f), 1)
        g = f[0]
        self.assertEqual((g.direction, g.lower, g.upper), ("bullish", 106.0, 115.0))
        self.assertEqual(g.timeframe, "5")
        self.assertFalse(g.filled)
        self.assertEqual(g.age, 0)
        self.assertEqual(g.size, 9.0)

    def test_detects_bearish_fvg(self):
        s = flat(20)
        s.append(c(20, 100, 101, 94, 95))
        s.append(c(21, 95, 96, 75, 76))
        s.append(c(22, 76, 85, 70, 72))
        f = detect_fvgs(s, "5")
        self.assertEqual(len(f), 1)
        self.assertEqual((f[0].direction, f[0].lower, f[0].upper), ("bearish", 85.0, 94.0))

    def test_tiny_gap_ignored_relative_to_atr(self):
        self.assertEqual(detect_fvgs(bullish_gap_series(tiny=True), "5", min_atr_mult=0.3), [])
        # but a permissive threshold would keep it
        self.assertEqual(len(detect_fvgs(bullish_gap_series(tiny=True), "5", min_atr_mult=0.01)), 1)

    def test_age_and_distance(self):
        f = detect_fvgs(bullish_gap_series(extra=5), "5")[0]
        self.assertEqual(f.age, 5)
        self.assertAlmostEqual(f.distance, 128 - 115)

    def test_partial_fill_tracked(self):
        s = bullish_gap_series()
        s.append(c(23, 128, 128, 110, 112))  # trades down to 110 -> (115-110)/9
        f = detect_fvgs(s, "5")[0]
        self.assertFalse(f.filled)
        self.assertAlmostEqual(f.fill_pct, 5 / 9, places=6)
        self.assertEqual(f.distance, 0.0)  # price 112 is inside the gap

    def test_fully_filled_excluded_by_default(self):
        s = bullish_gap_series()
        s.append(c(23, 128, 128, 100, 101))
        self.assertEqual(detect_fvgs(s, "5"), [])
        inc = detect_fvgs(s, "5", include_filled=True)
        self.assertEqual(len(inc), 1)
        self.assertTrue(inc[0].filled)

    def test_old_gaps_dropped(self):
        self.assertEqual(detect_fvgs(bullish_gap_series(extra=70), "5", max_age=60), [])

    def test_nearest_fvg(self):
        f = detect_fvgs(bullish_gap_series(), "5")
        self.assertIs(nearest_fvg(f, "bullish"), f[0])
        self.assertIsNone(nearest_fvg(f, "bearish"))


if __name__ == "__main__":
    unittest.main()
