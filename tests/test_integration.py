"""Integration tests against the REAL services. They are skipped unless you opt in:

    RUN_INTEGRATION=1 .venv/bin/python -m unittest tests.test_integration -v

Needs: Ollama running with your model (Ollama tests), internet (Bybit public tests), and
BYBIT_TESTNET_API_KEY / BYBIT_TESTNET_API_SECRET in .env (Testnet tests). LIVE is never touched.
Nothing here can place a live order: the only order placed is one far-from-market TESTNET limit order that is cancelled.
"""
import os
import unittest

RUN = os.environ.get("RUN_INTEGRATION") == "1"
skip = unittest.skipUnless(RUN, "set RUN_INTEGRATION=1 to run integration tests against real services")


def cfg_for(mode):
    import tempfile, yaml
    from bot.config import load_config
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    yaml.safe_dump({"mode": mode}, f)
    f.close()
    return load_config(f.name)


@skip
class OllamaIntegration(unittest.TestCase):
    def setUp(self):
        from bot.ai.ollama_client import OllamaClient
        from bot.hardware import resolve_model
        self.cfg = cfg_for("PAPER")
        self.client = OllamaClient(self.cfg.ai["host"], timeout=120)
        self.model = resolve_model(self.cfg.ai["model"], self.cfg.root)

    def test_connection(self):
        self.assertTrue(self.client.is_up(), "Ollama is not running: start it with `ollama serve`")

    def test_model_exists(self):
        self.assertTrue(self.client.has_model(self.model), f"run: ollama pull {self.model}")

    def test_model_returns_valid_structured_decision(self):
        from bot.ai.decision_engine import DecisionEngine
        res = DecisionEngine(self.client, self.model, dict(self.cfg.ai, timeout_seconds=180)).decide(
            {"symbol": "BTCUSDT", "price": 100000.0, "note": "no setup, answer WAIT"}, "", ["integration test"])
        self.assertTrue(res.ok, res.error)


@skip
class BybitPublicIntegration(unittest.TestCase):
    def test_market_data(self):
        from bot.exchange.bybit_client import BybitExchange
        ex = BybitExchange(testnet=False, credentials=None)
        t = ex.get_ticker("BTCUSDT")
        self.assertGreater(t["price"], 0)
        self.assertGreater(len(ex.get_klines("BTCUSDT", "5", 200)), 100)
        self.assertGreater(ex.get_instrument("BTCUSDT").tick_size, 0)
        self.assertGreater(len(ex.get_orderbook("BTCUSDT")["bids"]), 0)


@unittest.skipUnless(RUN and os.environ.get("BYBIT_TESTNET_API_KEY"), "REQUIRES YOUR TESTNET CREDENTIALS (BYBIT_TESTNET_API_KEY/SECRET) and RUN_INTEGRATION=1")
class BybitTestnetIntegration(unittest.TestCase):
    def setUp(self):
        from bot.exchange.bybit_client import BybitExchange
        self.ex = BybitExchange(testnet=True, credentials=(os.environ["BYBIT_TESTNET_API_KEY"], os.environ["BYBIT_TESTNET_API_SECRET"]))

    def test_authentication_and_permissions(self):
        p = self.ex.check_api_permissions()
        self.assertFalse(p["withdraw"], "testnet key must not have withdrawal permission either")
        self.assertGreaterEqual(self.ex.get_wallet()["equity"], 0)

    def test_order_create_cancel_and_positions(self):
        from bot.diagnostics import check_testnet_orders
        cfg = cfg_for("TESTNET")
        os.environ.setdefault("BYBIT_TESTNET", "true")
        results = check_testnet_orders(cfg)
        self.assertTrue(all(c.status == "PASS" for c in results), [(c.name, c.status, c.detail) for c in results])


if __name__ == "__main__":
    unittest.main()
