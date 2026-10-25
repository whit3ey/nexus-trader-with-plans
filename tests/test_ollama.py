import json
import unittest

from bot.ai.decision_engine import DecisionEngine, TOOL_SPECS, ALLOWED_TOOLS
from bot.ai.ollama_client import OllamaClient, OllamaError, OllamaUnavailable
from bot.ai.prompts import SYSTEM_PROMPT, build_user_prompt
from bot.config import DEFAULTS
from tests.fake_ollama import FakeOllama, msg

GOOD = {"decision": "LONG", "confidence": 82, "entry_type": "MARKET", "entry_price": None,
        "stop_loss": 99700.0, "take_profit": 100700.0, "risk_reward": 2.3, "reason": "sweep + BOS",
        "market_regime": "TREND", "setup_type": "sweep_bos", "invalidations": ["close below 99700"]}
SNAP = {"symbol": "BTCUSDT", "price": 100000.0, "atr_5m": 200.0}
AI = dict(DEFAULTS["ai"])


def client(srv, **kw):
    kw.setdefault("timeout", 3)
    kw.setdefault("max_retries", 1)
    return OllamaClient(srv.url, sleep=lambda s: None, **kw)


class ClientTests(unittest.TestCase):
    def test_health_models_and_pull(self):
        with FakeOllama(models=["qwen3:14b"]) as srv:
            c = client(srv)
            self.assertTrue(c.is_up())
            self.assertEqual(c.version(), "0.99.0")
            self.assertTrue(c.has_model("qwen3:14b"))
            self.assertFalse(c.has_model("qwen3:4b"))
            seen = []
            c.pull("qwen3:4b", seen.append)
            self.assertEqual(seen, ["pulling manifest", "downloading", "success"])
            self.assertTrue(c.has_model("qwen3:4b"))

    def test_unreachable_server(self):
        c = OllamaClient("http://127.0.0.1:1", timeout=1, max_retries=0)
        self.assertFalse(c.is_up())
        with self.assertRaises(OllamaUnavailable):
            c.list_models()

    def test_chat_sends_schema_options_and_think_flag(self):
        with FakeOllama() as srv:
            srv.chat_replies.append(msg("{}"))
            client(srv).chat("m", [{"role": "user", "content": "hi"}], schema={"type": "object"},
                             temperature=0.2, num_ctx=4096, think=False)
            req = srv.chat_requests[0]
            self.assertEqual(req["format"], {"type": "object"})
            self.assertEqual(req["options"]["num_ctx"], 4096)
            self.assertEqual(req["options"]["temperature"], 0.2)
            self.assertIs(req["think"], False)
            self.assertIs(req["stream"], False)

    def test_retries_server_error_then_succeeds(self):
        with FakeOllama() as srv:
            srv.chat_replies += [{"status": 503}, msg("ok")]
            m = client(srv).chat("m", [{"role": "user", "content": "x"}])
            self.assertEqual(m["content"], "ok")
            self.assertEqual(len(srv.chat_requests), 2)

    def test_client_error_is_not_retried(self):
        with FakeOllama() as srv:
            srv.chat_replies += [{"status": 400, "error": "bad request"}, msg("never")]
            with self.assertRaises(OllamaError):
                client(srv).chat("m", [{"role": "user", "content": "x"}])
            self.assertEqual(len(srv.chat_requests), 1)

    def test_timeout_raises_unavailable_after_bounded_retries(self):
        with FakeOllama() as srv:
            srv.chat_replies += [{"sleep": 1.5}, {"sleep": 1.5}]
            c = client(srv, timeout=0.5, max_retries=1)
            with self.assertRaises(OllamaUnavailable):
                c.chat("m", [{"role": "user", "content": "x"}])
            self.assertLessEqual(len(srv.chat_requests), 2)   # bounded, never infinite

    def test_think_unsupported_falls_back_without_think(self):
        with FakeOllama() as srv:
            srv.chat_replies += [{"status": 400, "error": "model does not support think"}, msg("ok")]
            m = client(srv).chat("m", [{"role": "user", "content": "x"}], think=True)
            self.assertEqual(m["content"], "ok")
            self.assertNotIn("think", srv.chat_requests[1])


class DecisionEngineTests(unittest.TestCase):
    def engine(self, srv, tools=None):
        return DecisionEngine(client(srv), "qwen3:14b", AI, tools)

    def test_valid_decision(self):
        with FakeOllama() as srv:
            srv.chat_replies.append(msg(json.dumps(GOOD)))
            out = self.engine(srv).decide(SNAP, "LONG")
            self.assertTrue(out.ok)
            self.assertEqual((out.decision.decision, out.attempts), ("LONG", 1))

    def test_leading_think_block_is_stripped_but_prose_is_not_accepted(self):
        with FakeOllama() as srv:
            srv.chat_replies.append(msg("<think>hmm let me see</think>\n" + json.dumps(GOOD)))
            self.assertTrue(self.engine(srv).decide(SNAP).ok)
        with FakeOllama() as srv:
            srv.chat_replies += [msg("Sure, go LONG! " + json.dumps(GOOD)), msg("still prose " + json.dumps(GOOD))]
            out = self.engine(srv).decide(SNAP)
            self.assertFalse(out.ok)

    def test_malformed_then_valid_retries_exactly_once(self):
        with FakeOllama() as srv:
            srv.chat_replies += [msg("not json"), msg(json.dumps(GOOD))]
            out = self.engine(srv).decide(SNAP)
            self.assertTrue(out.ok)
            self.assertEqual(out.attempts, 2)
            retry_msg = srv.chat_requests[1]["messages"][-1]["content"]
            self.assertIn("not valid", retry_msg)

    def test_malformed_twice_is_rejected_and_not_retried_further(self):
        with FakeOllama() as srv:
            bad = dict(GOOD)
            del bad["stop_loss"]
            srv.chat_replies += [msg(json.dumps(bad)), msg("{}"), msg(json.dumps(GOOD))]
            out = self.engine(srv).decide(SNAP)
            self.assertFalse(out.ok)
            self.assertIsNone(out.decision)
            self.assertEqual(len(srv.chat_requests), 2)
            self.assertIn("invalid", out.error)

    def test_ai_unavailable_returns_failure_not_exception(self):
        eng = DecisionEngine(OllamaClient("http://127.0.0.1:1", timeout=1, max_retries=0), "m", AI)
        out = eng.decide(SNAP)
        self.assertFalse(out.ok)
        self.assertIn("unavailable", out.error)

    def test_tool_call_roundtrip_uses_whitelisted_read_only_tools(self):
        with FakeOllama() as srv:
            call = [{"function": {"name": "get_current_position", "arguments": {}}}]
            srv.chat_replies += [msg("", call), msg(json.dumps(GOOD))]
            out = self.engine(srv, {"get_current_position": lambda: None}).decide(SNAP)
            self.assertTrue(out.ok)
            self.assertEqual(out.tool_calls, ["get_current_position"])
            self.assertEqual(srv.chat_requests[1]["messages"][-1]["role"], "tool")

    def test_unknown_or_dangerous_tool_is_refused(self):
        with FakeOllama() as srv:
            call = [{"function": {"name": "execute_any_api_request", "arguments": {"path": "/v5/asset/withdraw"}}}]
            srv.chat_replies += [msg("", call), msg(json.dumps(GOOD))]
            ran = []
            eng = self.engine(srv, {"execute_any_api_request": lambda: ran.append(1)})
            out = eng.decide(SNAP)
            self.assertEqual(ran, [])
            self.assertIn("not available", srv.chat_requests[1]["messages"][-1]["content"])
            self.assertTrue(out.ok)
            self.assertNotIn("execute_any_api_request", eng.tools)

    def test_tool_specs_contain_only_read_only_names(self):
        names = {t["function"]["name"] for t in TOOL_SPECS}
        self.assertEqual(names, {"get_market_snapshot", "get_current_position", "get_open_orders",
                                 "get_account_summary", "get_strategy_analysis"})
        self.assertEqual(names, ALLOWED_TOOLS)
        for n in names:
            self.assertTrue(n.startswith("get_"))

    def test_tool_failure_does_not_crash(self):
        with FakeOllama() as srv:
            call = [{"function": {"name": "get_account_summary", "arguments": {}}}]
            srv.chat_replies += [msg("", call), msg(json.dumps(GOOD))]

            def boom():
                raise RuntimeError("db down")
            out = self.engine(srv, {"get_account_summary": boom}).decide(SNAP)
            self.assertTrue(out.ok)

    def test_prompt_has_spec_text_and_no_secrets(self):
        self.assertIn("You do not have direct control of the exchange.", SYSTEM_PROMPT)
        self.assertIn("WAIT is always an acceptable decision.", SYSTEM_PROMPT)
        p = build_user_prompt(SNAP, "LONG")
        self.assertIn('"price":100000.0', p)
        for word in ("api_key", "api_secret", "BYBIT_API"):
            self.assertNotIn(word, p + SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
