import unittest
from types import SimpleNamespace
from bot.agents import MultiAgentCoordinator


class MultiAgentTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "symbol": "BTCUSDT", "price": 100000, "atr_5m": 200,
            "trend_1h": "bullish", "trend_15m": "bullish", "trend_5m": "bullish",
            "volatility_ratio_5m": 1.0,
            "liquidity_sweep": {"direction_hint": "LONG"},
        }
        self.setup = SimpleNamespace(has_setup=True, direction_hint="LONG", reasons=["1H trend bullish", "5M BOS aligned"])

    def test_parallel_review_returns_six_specialists_and_is_advisory(self):
        coordinator = MultiAgentCoordinator(True, 4)
        report = coordinator.review(self.snapshot, self.setup, {"status": "ok", "news_analysis": {"headline_count": 2, "heuristic_sentiment_score": 0.1}}, {"abnormal_atr_ratio": 3})
        self.assertEqual(report["status"], "complete")
        self.assertEqual(len(report["agents"]), 6)
        self.assertEqual(report["decision_authority"], "deterministic risk engine")
        self.assertEqual(next(a for a in report["agents"] if a["name"] == "Market regime")["state"], "aligned")
        self.assertEqual(next(a for a in report["agents"] if a["name"] == "Technical review")["state"], "LONG")

    def test_risk_agent_warns_but_does_not_approve_or_veto(self):
        coordinator = MultiAgentCoordinator(True)
        bad = dict(self.snapshot, volatility_ratio_5m=4.0)
        report = coordinator.review(bad, self.setup, {}, {"abnormal_atr_ratio": 3})
        risk = next(a for a in report["agents"] if a["name"] == "Risk pre-check")
        self.assertEqual(risk["status"], "review")
        self.assertIn("abnormal volatility ratio", risk["flags"])
        self.assertNotIn("approved", report)

    def test_disabled_agent_layer(self):
        coordinator = MultiAgentCoordinator(False)
        report = coordinator.review(self.snapshot, self.setup, {}, {})
        self.assertEqual(report["status"], "disabled")
        self.assertEqual(report["agents"], [])


if __name__ == "__main__":
    unittest.main()
