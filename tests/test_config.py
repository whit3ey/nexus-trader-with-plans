import os
import tempfile
import unittest
from unittest import mock

from bot.config import ConfigError, load_config, execution_permission, live_gate_report

ENV_KEYS = ["BYBIT_API_KEY", "BYBIT_API_SECRET", "BYBIT_TESTNET_API_KEY", "BYBIT_TESTNET_API_SECRET",
            "BYBIT_TESTNET", "TRADING_ENABLED", "AUTONOMOUS_MODE", "OLLAMA_MODEL"]


def load(yaml_text="", **env):
    base = {k: "" for k in ENV_KEYS}
    base.update(env)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(yaml_text)
    try:
        with mock.patch.dict(os.environ, base, clear=False):
            cfg = load_config(f.name)
            perm = execution_permission(cfg)
            report = live_gate_report(cfg)
            creds = cfg.credentials()
            return cfg, perm, report, creds
    finally:
        os.unlink(f.name)


class ConfigTests(unittest.TestCase):
    def test_default_is_paper_and_safe(self):
        cfg, perm, _, creds = load()
        self.assertEqual((cfg.mode, cfg.display_mode), ("PAPER", "PAPER MODE"))
        self.assertFalse(cfg.trading_enabled)
        self.assertFalse(cfg.autonomous_mode)
        self.assertEqual(perm.destination, "PAPER")
        self.assertIsNone(creds)
        self.assertFalse(cfg.use_testnet_endpoint)

    def test_live_requires_all_three_switches(self):
        keys = dict(BYBIT_API_KEY="k" * 10, BYBIT_API_SECRET="s" * 10, BYBIT_TESTNET="false")
        y = "mode: LIVE\n"
        for trading, auto in (("false", "false"), ("true", "false"), ("false", "true")):
            _, perm, report, _ = load(y, TRADING_ENABLED=trading, AUTONOMOUS_MODE=auto, **keys)
            self.assertFalse(perm.can_place_orders, (trading, auto))
            self.assertEqual(perm.destination, "NONE")
            self.assertTrue(any("[BLOCK]" in line for line in report))
        _, perm, report, _ = load(y, TRADING_ENABLED="true", AUTONOMOUS_MODE="true", **keys)
        self.assertTrue(perm.can_place_orders)
        self.assertEqual(perm.destination, "LIVE")
        self.assertFalse(any("[BLOCK]" in line for line in report))

    def test_live_without_credentials_blocked(self):
        _, perm, _, _ = load("mode: LIVE\n", TRADING_ENABLED="true", AUTONOMOUS_MODE="true", BYBIT_TESTNET="false")
        self.assertFalse(perm.can_place_orders)
        self.assertTrue(any("credentials" in b for b in perm.blockers))

    def test_environment_flag_must_match_mode(self):
        with self.assertRaises(ConfigError):
            load("mode: LIVE\n", BYBIT_TESTNET="true")
        with self.assertRaises(ConfigError):
            load("mode: TESTNET\n", BYBIT_TESTNET="false")

    def test_testnet_credentials_never_used_for_live(self):
        env = dict(BYBIT_TESTNET_API_KEY="testkey123", BYBIT_TESTNET_API_SECRET="testsec123", BYBIT_TESTNET="false")
        _, _, _, creds = load("mode: LIVE\n", **env)
        self.assertIsNone(creds)

    def test_live_credentials_never_preferred_for_testnet(self):
        env = dict(BYBIT_API_KEY="livekey123", BYBIT_API_SECRET="livesec123",
                   BYBIT_TESTNET_API_KEY="testkey123", BYBIT_TESTNET_API_SECRET="testsec123", BYBIT_TESTNET="true")
        cfg, _, _, creds = load("mode: TESTNET\n", **env)
        self.assertEqual(creds, ("testkey123", "testsec123"))
        self.assertTrue(cfg.use_testnet_endpoint)

    def test_cloud_or_remote_ai_host_rejected(self):
        with self.assertRaises(ConfigError):
            load("ai:\n  host: https://api.example.com\n")
        with self.assertRaises(ConfigError):
            load("ai:\n  provider: openai\n")

    def test_invalid_risk_values_rejected(self):
        for bad in ("risk:\n  risk_per_trade_percent: 0\n", "risk:\n  max_leverage: 500\n",
                    "risk:\n  min_risk_reward: 0.5\n", "risk:\n  min_stop_atr_mult: 5\n  max_stop_atr_mult: 2\n"):
            with self.assertRaises(ConfigError, msg=bad):
                load(bad)

    def test_wrong_market_rejected(self):
        with self.assertRaises(ConfigError):
            load("category: spot\n")
        with self.assertRaises(ConfigError):
            load("symbol: BTCUSD\n")

    def test_corrupted_yaml_gives_clear_error(self):
        with self.assertRaises(ConfigError):
            load("risk: [unclosed\n  - : :")

    def test_partial_yaml_merges_with_defaults(self):
        cfg, _, _, _ = load("risk:\n  risk_per_trade_percent: 0.25\n")
        self.assertEqual(cfg.risk["risk_per_trade_percent"], 0.25)
        self.assertEqual(cfg.risk["max_leverage"], 5.0)

    def test_model_override_from_env(self):
        cfg, _, _, _ = load("", OLLAMA_MODEL="qwen3:8b")
        self.assertEqual(cfg.ai["model"], "qwen3:8b")

    def test_testnet_with_flags_off_cannot_place_orders(self):
        env = dict(BYBIT_TESTNET_API_KEY="testkey123", BYBIT_TESTNET_API_SECRET="testsec123", BYBIT_TESTNET="true")
        _, perm, _, _ = load("mode: TESTNET\n", **env)
        self.assertFalse(perm.can_place_orders)
        _, perm, _, _ = load("mode: TESTNET\n", TRADING_ENABLED="true", AUTONOMOUS_MODE="true", **env)
        self.assertEqual(perm.destination, "TESTNET")


if __name__ == "__main__":
    unittest.main()
