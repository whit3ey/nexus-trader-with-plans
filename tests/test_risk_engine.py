import unittest
from datetime import datetime, timezone
from bot.ai.schemas import AIDecision
from bot.config import DEFAULTS
from bot.risk.limits import LimitTracker
from bot.risk.position_sizing import InstrumentInfo
from bot.risk.risk_engine import RiskEngine, RiskContext

INST = InstrumentInfo("BTCUSDT", 0.1, 0.001, 0.001, 100, 5)
NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)


def decision(**o):
    d = dict(decision="LONG", confidence=85.0, entry_type="MARKET", entry_price=None, stop_loss=99_700.0,
             take_profit=100_700.0, risk_reward=2.3, reason="r", market_regime="TREND",
             setup_type="sweep_bos", invalidations=())
    d.update(o)
    return AIDecision(**d)


def ctx(**o):
    c = dict(price=100_000.0, bid=99_999.5, ask=100_000.5, atr=200.0, equity=10_000.0,
             available_balance=10_000.0, instrument=INST, open_positions=0, data_age_seconds=5,
             volatility_ratio=1.0, trend_aligned=True, has_trigger=True)
    c.update(o)
    return RiskContext(**c)


def make():
    return RiskEngine(DEFAULTS["risk"], DEFAULTS["ai"]), LimitTracker(dict(DEFAULTS["risk"]), 10_000.0, now=NOW)


class RiskEngineTests(unittest.TestCase):
    def reject_reason(self, d, c, limits=None, contains=""):
        eng, lim = make()
        r = eng.validate(d, c, limits or lim)
        self.assertFalse(r.approved, r.summary())
        self.assertIsNone(r.order)
        self.assertTrue(any(contains in x for x in r.reasons), f"{contains!r} not in {r.reasons}")
        return r

    def test_good_long_is_approved_with_correct_order(self):
        eng, lim = make()
        r = eng.validate(decision(), ctx(), lim)
        self.assertTrue(r.approved, r.summary())
        o = r.order
        self.assertEqual((o.side, o.order_type, o.direction), ("Buy", "Market", "LONG"))
        self.assertEqual(o.entry_price, 100_000.5)            # market long fills at the ask
        self.assertEqual(o.qty, 0.166)                         # 50 USDT / 300.5 distance, rounded DOWN
        self.assertLessEqual(o.risk_amount, 50.0)
        self.assertGreaterEqual(o.risk_reward, 2.0)
        self.assertLessEqual(o.leverage, 5)

    def test_good_short_is_approved(self):
        eng, lim = make()
        r = eng.validate(decision(decision="SHORT", stop_loss=100_300.0, take_profit=99_300.0), ctx(), lim)
        self.assertTrue(r.approved, r.summary())
        self.assertEqual((r.order.side, r.order.entry_price), ("Sell", 99_999.5))

    def test_wait_is_never_an_order(self):
        eng, lim = make()
        r = eng.validate(decision(decision="WAIT", entry_type="NONE", stop_loss=None, take_profit=None), ctx(), lim)
        self.assertFalse(r.approved)
        self.assertTrue(r.is_wait)

    def test_low_confidence_rejected(self):
        self.reject_reason(decision(confidence=60), ctx(), contains="confidence")

    def test_mid_confidence_needs_alignment_and_trigger(self):
        self.reject_reason(decision(confidence=75), ctx(trend_aligned=False), contains="middle band")
        self.reject_reason(decision(confidence=75), ctx(has_trigger=False), contains="middle band")
        eng, lim = make()
        self.assertTrue(eng.validate(decision(confidence=75, take_profit=100_800.0), ctx(), lim).approved)

    def test_mid_confidence_needs_extra_rr(self):
        # RR ~2.33 passes the 2.0 minimum but not the 2.5 needed in the mid band
        self.reject_reason(decision(confidence=75), ctx(), contains="0.5 above")

    def test_high_confidence_cannot_override_other_failures(self):
        self.reject_reason(decision(confidence=100), ctx(data_age_seconds=500), contains="stale")

    def test_missing_or_wrong_side_stop_rejected(self):
        self.reject_reason(decision(stop_loss=100_300.0), ctx(), contains="wrong side")
        self.reject_reason(decision(take_profit=99_000.0), ctx(), contains="wrong side")

    def test_stop_too_tight_rejected(self):
        self.reject_reason(decision(stop_loss=99_950.0, take_profit=100_200.0), ctx(), contains="min")

    def test_stop_too_wide_rejected(self):
        self.reject_reason(decision(stop_loss=98_000.0, take_profit=104_000.0), ctx(), contains="max")

    def test_rr_below_minimum_rejected(self):
        self.reject_reason(decision(take_profit=100_400.0, risk_reward=None), ctx(), contains="risk/reward")

    def test_inflated_rr_claim_rejected(self):
        self.reject_reason(decision(risk_reward=5.0), ctx(), contains="claimed")

    def test_limit_entry_far_from_price_rejected(self):
        d = decision(entry_type="LIMIT", entry_price=99_000.0, stop_loss=98_700.0, take_profit=99_900.0, risk_reward=None)
        self.reject_reason(d, ctx(), contains="ATR from price")

    def test_limit_entry_near_price_uses_limit_price(self):
        eng, lim = make()
        d = decision(entry_type="LIMIT", entry_price=99_900.0, stop_loss=99_600.0, take_profit=100_600.0, risk_reward=None)
        r = eng.validate(d, ctx(), lim)
        self.assertTrue(r.approved, r.summary())
        self.assertEqual((r.order.order_type, r.order.entry_price), ("Limit", 99_900.0))

    def test_spread_too_wide_rejected(self):
        self.reject_reason(decision(), ctx(bid=99_900.0, ask=100_100.0), contains="spread")

    def test_volatility_filters(self):
        self.reject_reason(decision(), ctx(volatility_ratio=4.0), contains="volatility")
        self.reject_reason(decision(market_regime="HIGH_VOLATILITY"), ctx(), contains="HIGH_VOLATILITY")

    def test_uncertain_state_blocks_trading(self):
        for field, text in (("api_healthy", "API"), ("ws_healthy", "WebSocket"), ("ollama_healthy", "Ollama"),
                            ("account_state_confirmed", "account"), ("position_state_certain", "position"),
                            ("order_state_certain", "order state")):
            self.reject_reason(decision(), ctx(**{field: False}), contains=text)

    def test_halted_flag_blocks(self):
        self.reject_reason(decision(), ctx(trading_halted_reason="emergency stop"), contains="emergency stop")

    def test_max_open_positions(self):
        self.reject_reason(decision(), ctx(open_positions=1), contains="max open positions")

    def test_daily_loss_limit_blocks_trading(self):
        eng, lim = make()
        lim.update_equity(9_700)
        r = eng.validate(decision(), ctx(), lim)
        self.assertFalse(r.approved)
        self.assertTrue(any("daily loss" in x for x in r.reasons))

    def test_daily_trade_limit_blocks_trading(self):
        eng, lim = make()
        for _ in range(10):
            lim.record_trade_opened()
        self.assertFalse(eng.validate(decision(), ctx(), lim).approved)

    def test_insufficient_balance_rejected(self):
        self.reject_reason(decision(), ctx(available_balance=100.0), contains="leverage")

    def test_quantity_below_exchange_minimum_rejected(self):
        self.reject_reason(decision(), ctx(equity=60.0, available_balance=60.0), contains="minimum")

    def test_invalid_inputs_rejected(self):
        self.reject_reason(decision(), ctx(atr=0.0), contains="invalid")
        self.reject_reason(decision(), ctx(price=float("nan")), contains="invalid")

    def test_all_failures_are_reported_not_just_first(self):
        eng, lim = make()
        r = eng.validate(decision(confidence=10), ctx(data_age_seconds=999, api_healthy=False), lim)
        self.assertGreaterEqual(len(r.reasons), 3)

    def test_order_prices_are_rounded_to_tick(self):
        eng, lim = make()
        r = eng.validate(decision(stop_loss=99_700.04, take_profit=100_700.06), ctx(), lim)
        self.assertTrue(r.approved)
        self.assertEqual((r.order.stop_loss, r.order.take_profit), (99_700.0, 100_700.1))


if __name__ == "__main__":
    unittest.main()
