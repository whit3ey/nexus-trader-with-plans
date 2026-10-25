import json
import unittest

from bot.ai.decision_engine import DecisionEngine, tool_definitions, TOOL_DESCRIPTIONS, clean_content
from bot.ai.ollama_client import OllamaClient, OllamaError, OllamaUnavailable, OllamaTimeout
from bot.config import DEFAULTS

GOOD = {"decision": "LONG", "confidence": 82, "entry_type": "MARKET", "entry_price": None,
        "stop_loss": 99_700.0, "take_profit": 100_700.0, "risk_reward": 2.3, "reason": "ok",
        "market_regime": "TREND", "setup_type": "t", "invalidations": []}
AI = dict(DEFAULTS["ai"])


class FakeHTTP:
    """Scripted replies for /api/chat; records every request body."""
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, method, url, body, timeout, stream=False):
        self.calls.append((method, url, body))
        if url.endswith("/api/version"):
            return {"version": "0.0"}
        if url.endswith("/api/tags"):
            return {"models": [{"name": "qwen3:14b"}, {"name": "foo:latest"}]}
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def reply(content, tool_calls=None):
    m = {"role": "assistant", "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return {"message": m}


def engine(replies, tools=None, retries=1):
    http = FakeHTTP(replies)
    client = OllamaClient(http=http, max_retries=0, sleep=lambda s: None)
    return DecisionEngine(client, "qwen3:14b", dict(AI, max_retries=retries), tools), http


class ClientTests(unittest.TestCase):
    def test_only_local_hosts_allowed(self):
        for host in ("https://api.openai.com", "http://192.168.1.5:11434", "http://example.com"):
            with self.assertRaises(OllamaError):
                OllamaClient(host=host)
        OllamaClient(host="http://localhost:11434")

    def test_health_and_models(self):
        c = OllamaClient(http=FakeHTTP([]))
        self.assertTrue(c.is_up())
        self.assertTrue(c.has_model("qwen3:14b"))
        self.assertTrue(c.has_model("foo"))
        self.assertFalse(c.has_model("qwen3:30b"))

    def test_is_up_false_when_unreachable(self):
        def down(*a, **k):
            raise OllamaUnavailable("refused")
        self.assertFalse(OllamaClient(http=down).is_up())

    def test_chat_sends_native_options_and_schema(self):
        http = FakeHTTP([reply("{}")])
        OllamaClient(http=http).chat("m", [{"role": "user", "content": "x"}], schema={"type": "object"},
                                     temperature=0.2, num_ctx=4096, think=True)
        body = http.calls[0][2]
        self.assertEqual((body["model"], body["stream"], body["think"]), ("m", False, True))
        self.assertEqual(body["options"], {"temperature": 0.2, "num_ctx": 4096})
        self.assertEqual(body["format"], {"type": "object"})

    def test_retries_with_backoff_then_raises(self):
        sleeps = []
        http = FakeHTTP([OllamaTimeout("t"), OllamaTimeout("t"), OllamaTimeout("t")])
        c = OllamaClient(http=http, max_retries=2, sleep=sleeps.append)
        with self.assertRaises(OllamaTimeout):
            c.chat("m", [])
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertEqual(len(http.calls), 3)

    def test_recovers_after_transient_failure(self):
        http = FakeHTTP([OllamaUnavailable("x"), reply("hi")])
        out = OllamaClient(http=http, max_retries=1, sleep=lambda s: None).chat("m", [])
        self.assertEqual(out["content"], "hi")

    def test_malformed_server_reply_raises(self):
        with self.assertRaises(OllamaError):
            OllamaClient(http=FakeHTTP([{"nope": 1}])).chat("m", [])


class DecisionEngineTests(unittest.TestCase):
    def test_valid_decision_accepted(self):
        e, http = engine([reply(json.dumps(GOOD))])
        r = e.decide({"price": 1}, "LONG", ["x"])
        self.assertTrue(r.ok)
        self.assertEqual(r.decision.decision, "LONG")
        self.assertEqual(r.attempts, 1)
        sent = http.calls[0][2]
        self.assertIn("format", sent)
        self.assertEqual(sent["messages"][0]["role"], "system")
        self.assertIn("analysis engine", sent["messages"][0]["content"])

    def test_invalid_then_valid_retries_once(self):
        e, http = engine([reply("I think go long!"), reply(json.dumps(GOOD))])
        r = e.decide({}, "LONG")
        self.assertTrue(r.ok)
        self.assertEqual(r.attempts, 2)
        self.assertIn("not valid", http.calls[1][2]["messages"][-1]["content"])

    def test_still_invalid_after_retry_is_rejected(self):
        e, http = engine([reply("nope"), reply('{"decision":"LONG"}')])
        r = e.decide({}, "LONG")
        self.assertFalse(r.ok)
        self.assertIsNone(r.decision)
        self.assertEqual(r.attempts, 2)
        self.assertEqual(len(http.calls), 2)   # never more than one retry

    def test_ollama_down_gives_clean_failure(self):
        e, _ = engine([OllamaUnavailable("refused")])
        r = e.decide({}, "LONG")
        self.assertFalse(r.ok)
        self.assertIn("unavailable", r.error)

    def test_timeout_gives_clean_failure(self):
        e, _ = engine([OllamaTimeout("slow")])
        self.assertFalse(e.decide({}, "LONG").ok)

    def test_thinking_tags_stripped(self):
        self.assertEqual(clean_content("<think>hmm\nmore</think>\n{\"a\":1}"), '{"a":1}')
        e, _ = engine([reply("<think>reasoning</think>" + json.dumps(GOOD))])
        self.assertTrue(e.decide({}).ok)

    def test_tool_calls_are_executed_from_whitelist_only(self):
        called = []
        tools = {"get_current_position": lambda: called.append("pos") or None,
                 "execute_any_api_request": lambda: called.append("EVIL")}
        calls = [{"function": {"name": "get_current_position", "arguments": {}}},
                 {"function": {"name": "execute_any_api_request", "arguments": {}}}]
        e, http = engine([reply("", calls), reply(json.dumps(GOOD))], tools)
        r = e.decide({}, "LONG")
        self.assertTrue(r.ok)
        self.assertEqual(called, ["pos"])
        tool_msgs = [m for m in http.calls[1][2]["messages"] if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 2)
        self.assertIn("not available", tool_msgs[1]["content"])

    def test_tool_failure_does_not_crash(self):
        def boom():
            raise RuntimeError("db down")
        calls = [{"function": {"name": "get_open_orders", "arguments": {}}}]
        e, _ = engine([reply("", calls), reply(json.dumps(GOOD))], {"get_open_orders": boom})
        self.assertTrue(e.decide({}).ok)

    def test_endless_tool_calling_is_cut_off(self):
        calls = [{"function": {"name": "get_open_orders", "arguments": {}}}]
        replies = [reply("", calls)] * 10 + [reply(json.dumps(GOOD))] * 3
        e, http = engine(replies, {"get_open_orders": lambda: []})
        e.decide({})
        self.assertLessEqual(len(http.calls), 10)

    def test_tool_list_is_read_only(self):
        names = {t["function"]["name"] for t in tool_definitions()}
        self.assertEqual(names, set(TOOL_DESCRIPTIONS))
        for n in names:
            self.assertTrue(n.startswith("get_"), n)
            for bad in ("order", "execute", "close", "cancel", "withdraw", "leverage", "config"):
                self.assertNotIn(bad, n.replace("get_open_orders", ""))


if __name__ == "__main__":
    unittest.main()
