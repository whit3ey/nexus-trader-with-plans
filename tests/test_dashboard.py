import http.client
import json
import unittest

from dashboard.app import DashboardServer
from tests.fakes import Rig, make_cfg
from tests.test_engine import make_engine, FakeDecider, decision


def request(srv, method, path, body=None, headers=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    h = dict(headers or {})
    if host:
        h["Host"] = host
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


CTRL = {"X-Requested-By": "ai-trader", "Content-Type": "application/json"}


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.eng = make_engine(self.rig, FakeDecider([decision("WAIT")]))
        self.eng.startup()
        self.srv = DashboardServer(self.eng, "127.0.0.1", 0).start()

    def tearDown(self):
        self.srv.stop()

    def test_page_and_static_files_are_served(self):
        st, body = request(self.srv, "GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"Autonomous AI Trader", body)
        for path in ("/static/app.js", "/static/style.css"):
            self.assertEqual(request(self.srv, "GET", path)[0], 200)

    def test_state_contains_every_required_section_and_no_secrets(self):
        st, body = request(self.srv, "GET", "/api/state")
        self.assertEqual(st, 200)
        s = json.loads(body)
        for k in ("system", "market", "ai", "position", "risk", "display_mode", "events"):
            self.assertIn(k, s)
        self.assertEqual(s["display_mode"], "PAPER MODE")
        for k in ("ollama", "model", "bybit", "websocket", "halted", "live_gate"):
            self.assertIn(k, s["system"])
        for k in ("daily_trades", "daily_pnl", "max_daily_loss_pct", "drawdown_pct", "risk_per_trade_pct", "available"):
            self.assertIn(k, s["risk"])
        self.assertNotIn("secret", body.decode().lower().replace("api_secret=", ""))

    def test_path_traversal_blocked(self):
        self.assertEqual(request(self.srv, "GET", "/static/../app.py")[0], 404)
        self.assertEqual(request(self.srv, "GET", "/static/%2e%2e/app.py")[0], 404)

    def test_controls_require_custom_header(self):
        st, _ = request(self.srv, "POST", "/api/control", {"action": "pause"}, {"Content-Type": "application/json"})
        self.assertEqual(st, 403)
        self.assertFalse(self.rig.halt.paused_by_user)

    def test_foreign_origin_and_host_rejected(self):
        st, _ = request(self.srv, "POST", "/api/control", {"action": "pause"}, dict(CTRL, Origin="https://evil.example"))
        self.assertEqual(st, 403)
        self.assertEqual(request(self.srv, "GET", "/api/state", host="evil.example")[0], 403)

    def test_pause_and_resume(self):
        self.assertEqual(request(self.srv, "POST", "/api/control", {"action": "pause"}, CTRL)[0], 200)
        self.assertTrue(self.rig.halt.paused_by_user)
        st, body = request(self.srv, "POST", "/api/control", {"action": "resume"}, CTRL)
        self.assertEqual((st, json.loads(body)["ok"]), (200, True))
        self.assertFalse(self.rig.halt.paused_by_user)

    def test_emergency_close_works_and_halts(self):
        st, body = request(self.srv, "POST", "/api/control", {"action": "emergency_close"}, CTRL)
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertTrue(self.rig.halt.halted)

    def test_test_ai_and_refresh(self):
        st, body = request(self.srv, "POST", "/api/control", {"action": "test_ai"}, CTRL)
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["ok"])
        st, body = request(self.srv, "POST", "/api/control", {"action": "refresh"}, CTRL)
        self.assertTrue(json.loads(body)["api"])

    def test_unknown_action_and_bad_json(self):
        self.assertEqual(request(self.srv, "POST", "/api/control", {"action": "withdraw_everything"}, CTRL)[0], 400)

    def test_non_paper_modes_demand_explicit_confirmation(self):
        rig = Rig(cfg=make_cfg(mode="TESTNET", testnet=True))
        eng = make_engine(rig, FakeDecider([decision("WAIT")]))
        srv = DashboardServer(eng, "127.0.0.1", 0).start()
        try:
            st, body = request(srv, "POST", "/api/control", {"action": "emergency_close"}, CTRL)
            self.assertEqual(st, 400)
            self.assertIn("confirmation", json.loads(body)["error"])
            self.assertFalse(rig.halt.halted)
            st, body = request(srv, "POST", "/api/control", {"action": "emergency_close", "confirm": "TESTNET"}, CTRL)
            self.assertEqual(st, 200)
            self.assertTrue(rig.halt.halted)
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()
