import unittest
from tests.helpers import c, flat
from bot.strategy.order_blocks import detect_order_blocks, nearest_order_block


def bullish_ob_series(tail=None):
    s = flat(20)
    s.append(c(20, 101, 102, 97, 98))      # last bearish candle (origin)
    s.append(c(21, 98, 118, 97, 117))      # displacement, closes above recent highs
    s.append(c(22, 117, 120, 114, 119))
    for k, cd in enumerate(tail or []):
        s.append(c(23 + k, *cd))
    return s


class OrderBlockTests(unittest.TestCase):
    def test_detects_fresh_bullish_ob(self):
        obs = detect_order_blocks(bullish_ob_series(), "5")
        self.assertEqual(len(obs), 1)
        ob = obs[0]
        self.assertEqual((ob.direction, ob.high, ob.low, ob.origin_index), ("bullish", 102.0, 97.0, 20))
        self.assertEqual(ob.status, "fresh")
        self.assertEqual(ob.age, 2)
        self.assertAlmostEqual(ob.distance, 119 - 102)

    def test_detects_bearish_ob(self):
        s = flat(20)
        s.append(c(20, 99, 103, 98, 102))      # last bullish candle (origin)
        s.append(c(21, 102, 103, 82, 83))      # bearish displacement below recent lows
        s.append(c(22, 83, 86, 80, 81))
        obs = detect_order_blocks(s, "5")
        self.assertEqual(len(obs), 1)
        self.assertEqual((obs[0].direction, obs[0].high, obs[0].low), ("bearish", 103.0, 98.0))

    def test_mitigated_when_price_returns(self):
        s = bullish_ob_series(tail=[(119, 119, 100, 104)])  # low 100 <= zone high 102, close above zone low
        self.assertEqual(detect_order_blocks(s, "5"), [])
        dead = detect_order_blocks(s, "5", include_dead=True)
        self.assertEqual(dead[0].status, "mitigated")

    def test_broken_when_close_beyond_zone(self):
        s = bullish_ob_series(tail=[(119, 119, 90, 95)])    # closes below zone low 97
        dead = detect_order_blocks(s, "5", include_dead=True)
        self.assertEqual(dead[0].status, "broken")

    def test_no_displacement_no_ob(self):
        self.assertEqual(detect_order_blocks(flat(50), "5"), [])

    def test_small_move_below_atr_threshold_is_not_displacement(self):
        s = flat(20)
        s.append(c(20, 101, 102, 97, 98))
        s.append(c(21, 98, 108, 97, 106))   # body 8 < 1.2 * ATR(~10)
        self.assertEqual(detect_order_blocks(s, "5"), [])

    def test_nearest(self):
        obs = detect_order_blocks(bullish_ob_series(), "5")
        self.assertIs(nearest_order_block(obs, "bullish"), obs[0])
        self.assertIsNone(nearest_order_block(obs, "bearish"))


if __name__ == "__main__":
    unittest.main()
