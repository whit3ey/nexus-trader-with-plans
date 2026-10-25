import unittest
from bot.strategy.market_structure import Swing, find_swings, classify_structure, detect_bos_choch, analyse_structure
from tests.helpers import c


def candles_from_highs(highs, width=1.0):
    return [c(i, h - width / 2, h, h - width, h - width / 2) for i, h in enumerate(highs)]


class StructureTests(unittest.TestCase):
    def test_find_swing_high_and_low(self):
        highs = [10, 11, 12, 20, 12, 11, 10, 9, 8, 7, 8, 9, 10]
        sw = find_swings(candles_from_highs(highs), lookback=2)
        kinds = [(s.index, s.kind) for s in sw]
        self.assertIn((3, "high"), kinds)
        self.assertIn((9, "low"), kinds)

    def test_hh_hl_is_bullish(self):
        sw = [Swing(1, 0, 100, "high"), Swing(2, 0, 90, "low"), Swing(3, 0, 110, "high"), Swing(4, 0, 95, "low")]
        r = classify_structure(sw)
        self.assertEqual((r["structure"], r["trend"]), ("HH_HL", "bullish"))

    def test_lh_ll_is_bearish(self):
        sw = [Swing(1, 0, 110, "high"), Swing(2, 0, 95, "low"), Swing(3, 0, 105, "high"), Swing(4, 0, 90, "low")]
        r = classify_structure(sw)
        self.assertEqual((r["structure"], r["trend"]), ("LH_LL", "bearish"))

    def test_mixed_is_neutral(self):
        sw = [Swing(1, 0, 100, "high"), Swing(2, 0, 90, "low"), Swing(3, 0, 110, "high"), Swing(4, 0, 85, "low")]
        self.assertEqual(classify_structure(sw)["trend"], "neutral")

    def test_not_enough_swings(self):
        self.assertEqual(classify_structure([Swing(1, 0, 100, "high")])["structure"], "UNCLEAR")

    def _break_candles(self, last_close):
        cs = [c(i, 100, 101, 99, 100) for i in range(6)]
        cs.append(c(6, 108, 109, 107, 108))
        cs.append(c(7, 108, 112 if last_close > 110 else 109, 107, last_close))
        return cs

    def test_bos_in_trend_direction(self):
        cs = self._break_candles(111)
        sw = [Swing(3, 0, 110, "high")]
        r = detect_bos_choch(cs, sw, "bullish")
        self.assertEqual((r["bos"], r["choch"], r["level"]), ("bullish", None, 110))

    def test_choch_against_trend(self):
        cs = self._break_candles(111)
        r = detect_bos_choch(cs, [Swing(3, 0, 110, "high")], "bearish")
        self.assertEqual((r["bos"], r["choch"]), (None, "bullish"))

    def test_no_break_when_close_below_level(self):
        cs = self._break_candles(109)
        r = detect_bos_choch(cs, [Swing(3, 0, 110, "high")], "bullish")
        self.assertIsNone(r["bos"])

    def test_already_broken_earlier_not_repeated(self):
        cs = self._break_candles(111)
        cs[-2] = c(6, 108, 112, 107, 111)  # previous candle already closed above
        r = detect_bos_choch(cs, [Swing(3, 0, 110, "high")], "bullish")
        self.assertIsNone(r["bos"])

    def test_analyse_structure_uptrend_zigzag(self):
        highs = [10, 12, 14, 12, 10, 12, 14, 16, 18, 16, 14, 16, 18, 20, 22, 20, 18, 20, 22, 24, 22, 20]
        r = analyse_structure(candles_from_highs(highs), lookback=2)
        self.assertEqual(r["trend"], "bullish")


if __name__ == "__main__":
    unittest.main()
