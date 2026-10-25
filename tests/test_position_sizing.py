import unittest
from bot.risk.position_sizing import (InstrumentInfo, compute_position, risk_reward,
                                      round_down_to_step, round_to_tick)

INST = InstrumentInfo("BTCUSDT", tick_size=0.1, qty_step=0.001, min_qty=0.001, max_qty=100, min_notional=5)


class SizingTests(unittest.TestCase):
    def test_size_from_risk_not_percent_of_account(self):
        r = compute_position(10_000, 0.5, 100_000, 99_000, INST, 5)
        self.assertTrue(r.ok)
        self.assertAlmostEqual(r.qty, 0.05)
        self.assertAlmostEqual(r.risk_amount, 50.0)
        self.assertAlmostEqual(r.notional, 5000.0)

    def test_rounding_never_exceeds_risk_budget(self):
        r = compute_position(10_000, 0.5, 100_000, 99_300, INST, 5)  # dist 700 -> 0.0714285
        self.assertEqual(r.qty, 0.071)
        self.assertLessEqual(r.risk_amount, 50.0)

    def test_short_side_same_math(self):
        r = compute_position(10_000, 0.5, 100_000, 101_000, INST, 5)
        self.assertAlmostEqual(r.qty, 0.05)

    def test_leverage_cap_reduces_quantity(self):
        r = compute_position(1_000, 0.5, 100_000, 99_990, INST, 5)  # raw qty 0.5 -> notional 50k
        self.assertTrue(r.ok)
        self.assertAlmostEqual(r.notional, 5_000.0)
        self.assertLessEqual(r.leverage_needed, 5.0 + 1e-9)

    def test_explicit_notional_cap(self):
        r = compute_position(10_000, 0.5, 100_000, 99_990, INST, 5, max_notional=2_000)
        self.assertAlmostEqual(r.notional, 2_000.0)

    def test_below_min_qty_rejected(self):
        r = compute_position(100, 0.5, 100_000, 99_000, INST, 5)  # qty 0.0005 -> 0
        self.assertFalse(r.ok)
        self.assertIn("below the exchange minimum", r.reason)

    def test_min_notional_rejected(self):
        inst = InstrumentInfo("X", 0.1, 0.001, 0.001, 100, min_notional=10_000)
        self.assertFalse(compute_position(10_000, 0.5, 100_000, 99_000, inst, 5).ok)

    def test_bad_inputs(self):
        self.assertFalse(compute_position(0, 0.5, 100, 99, INST, 5).ok)
        self.assertFalse(compute_position(1000, 0.5, 100, 100, INST, 5).ok)
        self.assertFalse(compute_position(1000, 0.5, -1, 99, INST, 5).ok)

    def test_fees_are_reserved_inside_the_risk_budget(self):
        no_fee = compute_position(10_000, 0.5, 100_000, 99_000, INST, 5)
        fee = compute_position(10_000, 0.5, 100_000, 99_000, INST, 5, fee_round_trip=0.0012)
        self.assertLess(fee.qty, no_fee.qty)
        total_loss_at_stop = fee.qty * 1_000 + fee.notional * 0.0012
        self.assertLessEqual(total_loss_at_stop, 50.0 + 1e-9)
        self.assertLessEqual(fee.risk_amount, 50.0 + 1e-9)

    def test_rounding_helpers(self):
        self.assertEqual(round_down_to_step(0.0719, 0.001), 0.071)
        self.assertEqual(round_down_to_step(0.3, 0.1), 0.3)  # decimal-safe, no 0.29999
        self.assertEqual(round_to_tick(100_000.37, 0.5), 100_000.5)
        self.assertEqual(round_to_tick(100_000.2, 0.5), 100_000.0)

    def test_risk_reward(self):
        self.assertAlmostEqual(risk_reward("LONG", 100, 95, 110), 2.0)
        self.assertAlmostEqual(risk_reward("SHORT", 100, 105, 90), 2.0)
        self.assertEqual(risk_reward("LONG", 100, 105, 110), 0.0)   # stop above entry
        self.assertEqual(risk_reward("SHORT", 100, 105, 110), 0.0)  # target above entry
        with self.assertRaises(ValueError):
            risk_reward("FLAT", 1, 1, 1)


if __name__ == "__main__":
    unittest.main()
