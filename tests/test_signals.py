import json
import random
import unittest

from bot.config import DEFAULTS
from bot.strategy.signals import build_snapshot, evaluate_setup
from bot.strategy.types import Candle, validate_candles

S = DEFAULTS["strategy"]


def random_walk(n, step_ms, seed, start_price=100_000.0, vol=0.002):
    rnd = random.Random(seed)
    out, p = [], start_price
    t0 = 1_790_000_000_000 - (1_790_000_000_000 % 86_400_000)
    for i in range(n):
        o = p
        cl = o * (1 + rnd.gauss(0, vol))
        h = max(o, cl) * (1 + abs(rnd.gauss(0, vol / 2)))
        l = min(o, cl) * (1 - abs(rnd.gauss(0, vol / 2)))
        out.append(Candle(t0 + i * step_ms, o, h, l, cl, rnd.uniform(50, 150)))
        p = cl
    return out


def base_snapshot(**o):
    s = {"atr_5m": 100.0, "trend_1h": "bullish", "bos_5m": None, "choch_5m": None,
         "liquidity_sweep": {"swept": False}, "fvg_5m": [], "order_block_5m": [], "order_block_1h": []}
    s.update(o)
    return s


class SnapshotTests(unittest.TestCase):
    def test_snapshot_has_expected_fields_and_is_json_serialisable(self):
        c1h = random_walk(150, 3_600_000, 1)
        c5 = random_walk(300, 300_000, 2)
        snap = build_snapshot(S, "BTCUSDT", c1h, c5, random_walk(100, 900_000, 3),
                              {"price": c5[-1].close, "funding_rate": 0.0001, "open_interest": 1234.5})
        for k in ("symbol", "price", "trend_1h", "structure_1h", "atr_5m", "volume_state", "fvg_5m",
                  "order_block_5m", "liquidity_5m", "liquidity_sweep", "bos_5m", "funding_rate", "position"):
            self.assertIn(k, snap)
        self.assertGreater(snap["atr_5m"], 0)
        json.dumps(snap)
        self.assertLess(len(json.dumps(snap)), 12_000)  # compact enough for a small context window

    def test_snapshot_is_deterministic(self):
        a = build_snapshot(S, "BTCUSDT", random_walk(150, 3_600_000, 1), random_walk(300, 300_000, 2))
        b = build_snapshot(S, "BTCUSDT", random_walk(150, 3_600_000, 1), random_walk(300, 300_000, 2))
        self.assertEqual(a, b)

    def test_insufficient_or_broken_data_raises(self):
        with self.assertRaises(ValueError):
            build_snapshot(S, "BTCUSDT", random_walk(10, 3_600_000, 1), random_walk(300, 300_000, 2))
        bad = random_walk(300, 300_000, 2)
        bad[100] = Candle(bad[100].ts, 100, 90, 110, 100)  # high < low
        with self.assertRaises(ValueError):
            build_snapshot(S, "BTCUSDT", random_walk(150, 3_600_000, 1), bad)

    def test_candle_validation_detects_unsorted(self):
        cs = random_walk(5, 300_000, 1)
        self.assertTrue(validate_candles([cs[1], cs[0]]))


class SetupFilterTests(unittest.TestCase):
    def zone(self, d="bullish", dist=50.0):
        return {"direction": d, "distance": dist}

    def test_long_setup_needs_trigger_and_zone(self):
        sweep = {"swept": True, "direction_hint": "LONG"}
        s = evaluate_setup(base_snapshot(liquidity_sweep=sweep, fvg_5m=[self.zone()]), S)
        self.assertTrue(s.has_setup)
        self.assertEqual(s.direction_hint, "LONG")

    def test_trigger_without_zone_is_no_setup(self):
        self.assertFalse(evaluate_setup(base_snapshot(bos_5m="bullish"), S).has_setup)

    def test_zone_without_trigger_is_no_setup(self):
        self.assertFalse(evaluate_setup(base_snapshot(fvg_5m=[self.zone()]), S).has_setup)

    def test_zone_too_far_is_ignored(self):
        far = self.zone(dist=5 * 100.0)
        self.assertFalse(evaluate_setup(base_snapshot(bos_5m="bullish", fvg_5m=[far]), S).has_setup)

    def test_counter_trend_not_considered(self):
        snap = base_snapshot(trend_1h="bearish", bos_5m="bullish", fvg_5m=[self.zone()])
        self.assertFalse(evaluate_setup(snap, S).has_setup)

    def test_short_setup_mirror(self):
        snap = base_snapshot(trend_1h="bearish", choch_5m="bearish", order_block_5m=[self.zone("bearish")])
        s = evaluate_setup(snap, S)
        self.assertEqual((s.has_setup, s.direction_hint), (True, "SHORT"))

    def test_neutral_trend_never_trades(self):
        snap = base_snapshot(trend_1h="neutral", bos_5m="bullish", fvg_5m=[self.zone()])
        self.assertFalse(evaluate_setup(snap, S).has_setup)


if __name__ == "__main__":
    unittest.main()
