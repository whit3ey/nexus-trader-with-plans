"""Plans: a fresh zone ahead of price, a resting limit, and the limits the model still cannot cross."""
import unittest
from unittest import mock

from bot.ai.schemas import AIDecision, DecisionError, parse_decision
from bot.config import DEFAULTS
from bot.execution.executor import Executor
from bot.risk.limits import LimitTracker
from bot.risk.risk_engine import RiskEngine
from bot.strategy.signals import Setup, evaluate_setup
from bot.strategy.types import Candle
from tests.fakes import Rig, params
from tests.test_engine import CANDLE_TS, FakeDecider, SNAP, make_engine
from tests.test_risk_engine import INST, NOW, ctx, make

S = DEFAULTS["strategy"]


def zone(**over):
    z = {"direction": "bullish", "lower": 98_800.0, "upper": 99_000.0, "distance": 1_000.0, "fill_pct": 0.0, "filled": False}
    z.update(over)
    return z


def snap(**over):
    s = {"price": 100_000.0, "atr_5m": 200.0, "trend_1h": "bullish", "bos_5m": None, "choch_5m": None,
         "liquidity_sweep": {"swept": False}, "fvg_5m": [], "order_block_5m": [], "order_block_1h": [], "fvg_1h": []}
    s.update(over)
    return s


def plan_decision(**over):
    d = dict(decision="LONG", confidence=90.0, entry_type="LIMIT", entry_price=99_000.0,
             stop_loss=98_700.0, take_profit=100_600.0, risk_reward=5.3, reason="plan the pullback",
             market_regime="TREND", setup_type="plan_fvg", invalidations=(),
             valid_minutes=45.0, abandon_price=100_600.0, tp_management="trailing", risk_scale=0.5)
    d.update(over)
    return AIDecision(**d)


def plan_ctx(**over):
    return ctx(is_plan=True, plan_zone_lower=98_800.0, plan_zone_upper=99_000.0, has_trigger=False, **over)


class MapperPlanTests(unittest.TestCase):
    def test_fresh_zone_on_the_pullback_side_is_a_plan(self):
        s = evaluate_setup(snap(fvg_5m=[zone()]), S)
        self.assertEqual((s.has_setup, s.kind, s.direction_hint), (True, "plan", "LONG"))
        self.assertEqual(s.zone["entry"], 99_000.0)
        self.assertLessEqual(s.zone["distance_atr"], 6.0)

    def test_trigger_at_the_zone_still_wins(self):
        near = zone(lower=99_900.0, upper=99_950.0, distance=50.0)
        s = evaluate_setup(snap(bos_5m="bullish", fvg_5m=[near]), S)
        self.assertEqual((s.has_setup, s.kind, s.direction_hint), (True, "trigger", "LONG"))

    def test_zone_without_a_freshness_mark_is_not_a_plan(self):
        bare = {"direction": "bullish", "distance": 50.0}
        self.assertFalse(evaluate_setup(snap(fvg_5m=[bare]), S).has_setup)

    def test_wrong_side_too_far_or_mitigated_is_not_a_plan(self):
        above = zone(lower=100_400.0, upper=100_800.0, distance=400.0)
        self.assertFalse(evaluate_setup(snap(fvg_5m=[above]), S).has_setup)
        far = zone(lower=97_000.0, upper=97_200.0, distance=2_800.0)
        self.assertFalse(evaluate_setup(snap(order_block_5m=[far]), S).has_setup)
        dead = {"direction": "bullish", "low": 98_800.0, "high": 99_000.0, "distance": 1_000.0, "status": "mitigated"}
        self.assertFalse(evaluate_setup(snap(order_block_5m=[dead]), S).has_setup)

    def test_short_plan_is_the_mirror(self):
        supply = zone(direction="bearish", lower=101_000.0, upper=101_400.0, distance=1_000.0)
        s = evaluate_setup(snap(trend_1h="bearish", fvg_5m=[supply]), S)
        self.assertEqual((s.kind, s.direction_hint, s.zone["entry"]), ("plan", "SHORT", 101_000.0))

    def test_neutral_trend_never_plans(self):
        self.assertFalse(evaluate_setup(snap(trend_1h="neutral", fvg_5m=[zone()]), S).has_setup)


class PlanSchemaTests(unittest.TestCase):
    def test_optional_plan_fields_default_quietly(self):
        d = parse_decision({"decision": "WAIT", "confidence": 10, "entry_type": "NONE", "entry_price": None,
                            "stop_loss": None, "take_profit": None, "risk_reward": None, "reason": "no",
                            "market_regime": "UNCLEAR", "setup_type": "none", "invalidations": []})
        self.assertEqual((d.valid_minutes, d.abandon_price, d.tp_management, d.risk_scale), (None, None, None, None))

    def test_plan_fields_are_kept_and_mistyped_ones_rejected(self):
        raw = {"decision": "LONG", "confidence": 90, "entry_type": "LIMIT", "entry_price": 99_000,
               "stop_loss": 98_700, "take_profit": 100_600, "risk_reward": 5.3, "reason": "plan",
               "market_regime": "TREND", "setup_type": "plan", "invalidations": [],
               "valid_minutes": 30, "abandon_price": 100_600, "tp_management": "Fixed_rr", "risk_scale": 0.25}
        d = parse_decision(raw)
        self.assertEqual((d.valid_minutes, d.tp_management, d.risk_scale), (30.0, "fixed", 0.25))
        raw["tp_management"] = "none"
        with self.assertRaises(DecisionError):
            parse_decision(raw)
        raw["tp_management"] = "fixed"
        raw["risk_scale"] = "all"
        with self.assertRaises(DecisionError):
            parse_decision(raw)


class PlanRiskTests(unittest.TestCase):
    def test_plan_inside_six_atr_is_approved_at_reduced_risk(self):
        eng, lim = make()
        r = eng.validate(plan_decision(), plan_ctx(), lim)
        self.assertTrue(r.approved, r.summary())
        o = r.order
        self.assertEqual((o.order_type, o.entry_price, o.stop_loss, o.take_profit), ("Limit", 99_000.0, 98_700.0, 100_600.0))
        self.assertEqual((o.valid_minutes, o.abandon_price, o.tp_management, o.risk_scale), (45.0, 100_600.0, "trailing", 0.5))
        self.assertEqual(o.qty, 0.083)                 # half of 0.5% — the model may only take less
        self.assertLessEqual(o.risk_amount, 25.0)

    def test_omitted_lifetime_and_abandon_use_the_defaults(self):
        eng, lim = make()
        d = plan_decision(valid_minutes=None, abandon_price=None, risk_scale=None, tp_management=None)
        r = eng.validate(d, plan_ctx(), lim)
        self.assertTrue(r.approved, r.summary())
        self.assertEqual(r.order.valid_minutes, 60.0)
        self.assertEqual(r.order.abandon_price, r.order.take_profit)
        self.assertEqual(r.order.risk_scale, 1.0)

    def test_model_cannot_raise_risk_or_skip_the_usual_gates(self):
        self.assert_rejected(plan_decision(risk_scale=1.5), "risk_scale")
        self.assert_rejected(plan_decision(risk_scale=0.1), "0.25")
        self.assert_rejected(plan_decision(entry_type="MARKET", entry_price=None), "LIMIT")
        self.assert_rejected(plan_decision(entry_price=100_200.0, stop_loss=99_800.0, take_profit=101_200.0, risk_reward=None), "pullback")
        self.assert_rejected(plan_decision(entry_price=99_200.0, stop_loss=98_800.0, take_profit=100_600.0, risk_reward=None), "fresh zone")
        self.assert_rejected(plan_decision(stop_loss=98_950.0, take_profit=100_600.0, risk_reward=None), "min")
        self.assert_rejected(plan_decision(valid_minutes=4), "5-240")
        self.assert_rejected(plan_decision(valid_minutes=300), "5-240")
        self.assert_rejected(plan_decision(abandon_price=99_500.0), "abandon")
        self.assert_rejected(plan_decision(confidence=75), "middle band")
        eng, lim = make()
        lim.update_equity(9_700)
        r = eng.validate(plan_decision(), plan_ctx(), lim)
        self.assertFalse(r.approved)
        self.assertTrue(any("daily loss" in x for x in r.reasons))

    def assert_rejected(self, decision, contains):
        eng, lim = make()
        r = eng.validate(decision, plan_ctx(), lim)
        self.assertFalse(r.approved, r.summary())
        self.assertTrue(any(contains in x for x in r.reasons), r.reasons)

    def test_a_near_limit_that_is_not_a_plan_still_uses_the_tight_deviation_cap(self):
        eng, lim = make()
        d = plan_decision(entry_price=99_000.0, confidence=90)
        r = eng.validate(d, ctx(), lim)          # not a plan: 5 ATR is too far
        self.assertFalse(r.approved)
        self.assertTrue(any("ATR from price" in x for x in r.reasons))


class PlanOrderTests(unittest.TestCase):
    def test_resting_plan_keeps_stop_and_target_and_survives_restart(self):
        r = Rig()
        p = params(order_type="Limit", entry=99_000.0, sl=98_700.0, tp=101_200.0)
        p.valid_minutes = 40
        p.abandon_price = 101_200.0
        p.tp_management = "fixed"
        p.risk_scale = 0.5
        res = r.executor.open_position(p, 1, 90)
        self.assertEqual(res.status, "pending")
        order = r.ex.get_open_orders("BTCUSDT")[0]
        self.assertEqual((order["stopLoss"], order["takeProfit"], order["price"]), (98_700.0, 101_200.0, 99_000.0))
        restarted = Executor(r.cfg, r.ex, r.db, r.limits, r.halt, None, sleep=lambda s: None, clock=r.clock)
        self.assertEqual(restarted.pending["abandon_price"], 101_200.0)
        self.assertIsNone(restarted.service_pending(100_000.0))
        r.advance(301)                              # the short global TTL must not kill a 40-minute plan
        self.assertIsNotNone(restarted.pending)
        r.advance(40 * 60)
        out = restarted.service_pending(100_000.0)
        self.assertIn("expired", out.reason)
        self.assertEqual(r.ex.get_open_orders("BTCUSDT"), [])

    def test_abandon_level_cancels_before_the_fill(self):
        r = Rig()
        p = params(order_type="Limit", entry=99_000.0, sl=98_700.0, tp=101_200.0)
        p.valid_minutes = 90
        p.abandon_price = 101_200.0
        r.executor.open_position(p, 1, 90)
        r.move(101_200.0)
        out = r.executor.service_pending(101_200.0)
        self.assertIn("abandon", out.reason)
        self.assertEqual(r.ex.get_open_orders("BTCUSDT"), [])
        self.assertEqual(r.ex.get_positions("BTCUSDT"), [])

    def test_fixed_style_does_not_trail_and_trailing_style_cannot_loosen(self):
        r = Rig()
        p = params()
        p.tp_management = "fixed"
        self.assertTrue(r.executor.open_position(p, 1, 90).ok)
        r.move(100_450)
        r.executor.manage_position(r.ex.get_positions("BTCUSDT")[0], 200)
        self.assertEqual(r.ex.get_positions("BTCUSDT")[0]["stopLoss"], 99_700.0)

        r2 = Rig(**{"trailing__enabled": False})
        p2 = params()
        p2.tp_management = "trailing"
        self.assertTrue(r2.executor.open_position(p2, 1, 90).ok)
        r2.move(100_450)
        r2.executor.manage_position(r2.ex.get_positions("BTCUSDT")[0], 200)
        tightened = r2.ex.get_positions("BTCUSDT")[0]["stopLoss"]
        self.assertGreater(tightened, 99_700.0)
        # Ease back, but stay above the new stop so the position is still open.
        # The trail must not hand that stop back.
        r2.move(tightened + 80)
        r2.executor.manage_position(r2.ex.get_positions("BTCUSDT")[0], 200)
        self.assertGreaterEqual(r2.ex.get_positions("BTCUSDT")[0]["stopLoss"], tightened)


class PlanEngineTests(unittest.TestCase):
    def _step(self, eng, rig, setup, ts):
        for iv in ("5", "60", "15"):
            rig.market.candles[iv] = [Candle(ts, 100, 101, 99, 100, 1)]
        with mock.patch("bot.engine.build_snapshot", return_value=dict(SNAP)), \
             mock.patch("bot.engine.evaluate_setup", return_value=setup):
            eng.step()

    def test_a_plan_is_asked_at_most_once_per_15_minutes(self):
        rig = Rig()
        wait = AIDecision("WAIT", 40, "NONE", None, None, None, None, "not yet", "UNCLEAR", "plan", ())
        dec = FakeDecider([wait])
        eng = make_engine(rig, dec)
        eng.startup()
        plan = Setup(True, "LONG", ["PLAN"], 3, "plan", {"lower": 98_800.0, "upper": 99_000.0, "entry": 99_000.0})
        self._step(eng, rig, plan, CANDLE_TS)
        self.assertEqual(dec.calls, 1)
        rig.advance(60)
        self._step(eng, rig, plan, CANDLE_TS + 300_000)
        self.assertEqual(dec.calls, 1)
        rig.advance(900)
        self._step(eng, rig, plan, CANDLE_TS + 600_000)
        self.assertEqual(dec.calls, 2)

    def test_approved_plan_shows_on_the_open_trade_tile(self):
        rig = Rig()
        dec = FakeDecider([plan_decision()])
        eng = make_engine(rig, dec)
        eng.startup()
        plan = Setup(True, "LONG", ["PLAN fresh zone"], 3, "plan",
                     {"lower": 98_800.0, "upper": 99_000.0, "entry": 99_000.0})
        self._step(eng, rig, plan, CANDLE_TS)
        self.assertEqual(dec.calls, 1)
        resting = rig.ex.get_open_orders("BTCUSDT")
        self.assertEqual(len(resting), 1)
        self.assertEqual((resting[0]["stopLoss"], resting[0]["takeProfit"]), (98_700.0, 100_600.0))
        view = eng.get_state()["plan"]
        self.assertEqual(view["kind"], "plan")
        self.assertEqual(view["direction"], "LONG")
        self.assertEqual(view["entry"], 99_000.0)
        self.assertGreater(view["minutes_left"], 40)
        self.assertEqual(view["abandon"], 100_600.0)
        rig.market.set(100_600.0)
        eng.step()
        self.assertIsNone(eng.get_state()["plan"])
        self.assertEqual(rig.ex.get_open_orders("BTCUSDT"), [])


if __name__ == "__main__":
    unittest.main()
