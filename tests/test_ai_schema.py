import json
import unittest
from bot.ai.schemas import parse_decision, DecisionError


def good(**over):
    d = {"decision": "LONG", "confidence": 82, "entry_type": "MARKET", "entry_price": None,
         "stop_loss": 99_700.0, "take_profit": 100_700.0, "risk_reward": 2.3,
         "reason": "sweep + BOS", "market_regime": "TREND", "setup_type": "sweep_bos_fvg",
         "invalidations": ["close below 99700"]}
    d.update(over)
    return d


class SchemaTests(unittest.TestCase):
    def test_valid_decision_from_dict_and_json(self):
        a = parse_decision(good())
        b = parse_decision(json.dumps(good()))
        self.assertEqual(a, b)
        self.assertEqual((a.decision, a.confidence, a.stop_loss), ("LONG", 82.0, 99_700.0))

    def test_wait_needs_no_prices(self):
        d = parse_decision(good(decision="WAIT", entry_type="NONE", stop_loss=None, take_profit=None, risk_reward=None))
        self.assertEqual(d.decision, "WAIT")

    def test_each_required_field_missing_is_rejected(self):
        for k in good():
            payload = good()
            del payload[k]
            with self.assertRaises(DecisionError, msg=k):
                parse_decision(payload)

    def test_invalid_json_rejected(self):
        for bad in ("not json", "{'decision': 'LONG'}", '{"decision": "LONG"', "", "LONG now!"):
            with self.assertRaises(DecisionError):
                parse_decision(bad)

    def test_json_wrapped_in_prose_is_not_accepted(self):
        with self.assertRaises(DecisionError):
            parse_decision("Sure! Here you go: " + json.dumps(good()))

    def test_non_object_rejected(self):
        for bad in ("[]", "42", "null", '"LONG"'):
            with self.assertRaises(DecisionError):
                parse_decision(bad)

    def test_enums_validated(self):
        for key, val in (("decision", "BUY"), ("entry_type", "STOP"), ("market_regime", "BULL")):
            with self.assertRaises(DecisionError):
                parse_decision(good(**{key: val}))

    def test_confidence_bounds_and_types(self):
        for val in (-1, 101, "high", True, float("nan"), None):
            with self.assertRaises(DecisionError, msg=repr(val)):
                parse_decision(good(confidence=val))

    def test_trade_requires_stop_and_target(self):
        for k in ("stop_loss", "take_profit"):
            with self.assertRaises(DecisionError):
                parse_decision(good(**{k: None}))

    def test_trade_with_entry_type_none_rejected(self):
        with self.assertRaises(DecisionError):
            parse_decision(good(entry_type="NONE"))

    def test_limit_needs_entry_price(self):
        with self.assertRaises(DecisionError):
            parse_decision(good(entry_type="LIMIT", entry_price=None))
        self.assertEqual(parse_decision(good(entry_type="LIMIT", entry_price=100_000)).entry_price, 100_000.0)

    def test_string_prices_and_negative_prices_rejected(self):
        with self.assertRaises(DecisionError):
            parse_decision(good(stop_loss="99700"))
        with self.assertRaises(DecisionError):
            parse_decision(good(stop_loss=-5))

    def test_invalidations_must_be_string_list(self):
        with self.assertRaises(DecisionError):
            parse_decision(good(invalidations="x"))
        with self.assertRaises(DecisionError):
            parse_decision(good(invalidations=[1, 2]))

    def test_infinite_price_rejected(self):
        with self.assertRaises(DecisionError):
            parse_decision(good(take_profit=float("inf")))


if __name__ == "__main__":
    unittest.main()
