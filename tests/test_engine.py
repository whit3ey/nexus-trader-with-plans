import json
import unittest
from unittest import mock

from bot.ai.decision_engine import DecisionResult
from bot.ai.schemas import AIDecision
from bot.engine import TradingEngine
from bot.exchange.base import ExchangeError
from bot.exchange.paper import PaperExchange
from bot.strategy.signals import Setup
from bot.strategy.types import Candle
from tests.fakes import Rig, make_cfg, FakeMarket

CANDLE_TS = 500_000_000   # ms; well before the test clock (1_000_000 s), so the candle counts as closed


def decision(kind="LONG", conf=85.0, sl=99_700.0, tp=100_700.0):
    return AIDecision(kind, conf, "MARKET" if kind != "WAIT" else "NONE", None,
                      sl if kind != "WAIT" else None, tp if kind != "WAIT" else None, 2.3 if kind != "WAIT" else None,
                      "test", "TREND", "sweep_bos", ())


class FakeClient:
    def is_up(self):
        return True


class FakeDecider:
    def __init__(self, replies):
        self.replies, self.calls, self.tools, self.client = list(replies), 0, {}, FakeClient()

    def decide(self, snap, hint="", reasons=None, kind="trigger"):
        self.calls += 1
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, DecisionResult):
            return r
        return DecisionResult(True, r, attempts=1, latency_s=0.1)


SNAP = {"price": 100_000.0, "trend_1h": "bullish", "structure_1h": "HH_HL", "trend_5m": "bullish", "atr_5m": 200.0,
        "bos_5m": "bullish", "choch_5m": None, "liquidity_sweep": {"swept": True, "direction_hint": "LONG"},
        "volatility_ratio_5m": 1.0, "volume_state": "average", "fvg_5m": [], "order_block_5m": [],
        "liquidity_5m": {}}
SETUP = Setup(True, "LONG", ["1H trend bullish", "5M BOS aligned"], 5)


def make_engine(rig, decider, candle_ts=CANDLE_TS):
    rig.market.candles = {"5": [Candle(candle_ts, 100, 101, 99, 100, 1)], "60": [Candle(candle_ts, 100, 101, 99, 100, 1)],
                          "15": [Candle(candle_ts, 100, 101, 99, 100, 1)]}
    eng = TradingEngine(rig.cfg, rig.db, rig.ex, rig.market, decider, None, None, "fake-model",
                        sleep=lambda s: None, clock=rig.clock)
    eng.executor = rig.executor
    eng.limits = rig.limits
    eng.halt = rig.halt
    eng.executor.halt = rig.halt
    return eng


PATCHES = ("bot.engine.build_snapshot", "bot.engine.evaluate_setup")


class EngineTests(unittest.TestCase):
    def run_with(self, fn, setup=SETUP):
        with mock.patch("bot.engine.build_snapshot", return_value=dict(SNAP)), \
             mock.patch("bot.engine.evaluate_setup", return_value=setup):
            return fn()

    def test_full_trade_lifecycle_in_paper_mode(self):
        rig = Rig()
        dec = FakeDecider([decision()])
        eng = make_engine(rig, dec)
        eng.startup()
        self.assertTrue(eng.recon.certain)
        self.run_with(eng.step)
        self.assertEqual(dec.calls, 1)
        pos = rig.ex.get_positions("BTCUSDT")
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]["stopLoss"], 99_700.0)
        trade = rig.db.open_trades("BTCUSDT")[0]
        self.assertEqual(trade["mode"], "PAPER")
        self.assertEqual(rig.limits.stats.trades, 1)
        # price runs up: trailing stop engages (last_atr5 set by the evaluation)
        rig.market.set(100_450)
        self.run_with(eng.step)
        self.assertGreater(rig.ex.get_positions("BTCUSDT")[0]["stopLoss"], 99_700.0)
        # take profit hit
        rig.market.set(100_800)
        self.run_with(eng.step)
        self.assertEqual(rig.ex.get_positions("BTCUSDT"), [])
        closed = rig.db.closed_trades()[0]
        self.assertGreater(closed["pnl"], 0)
        self.assertGreater(closed["r_multiple"], 0)
        self.assertEqual(rig.limits.stats.wins, 1)
        st = eng.get_state()
        json.dumps(st, default=str)                        # dashboard payload must serialise
        self.assertEqual(st["display_mode"], "PAPER MODE")
        self.assertEqual(st["risk"]["daily_trades"], 1)
        for key in ("entry_type", "stop_loss", "take_profit", "risk_reward", "market_regime", "invalidations"):
            self.assertIn(key, st["ai"])

    def test_same_candle_never_evaluated_twice(self):
        rig = Rig()
        dec = FakeDecider([decision("WAIT")])
        eng = make_engine(rig, dec)
        eng.startup()
        for _ in range(4):
            self.run_with(eng.step)
        self.assertEqual(dec.calls, 1)

    def test_wait_and_invalid_ai_output_never_trade(self):
        rig = Rig()
        eng = make_engine(rig, FakeDecider([decision("WAIT")]))
        eng.startup()
        self.run_with(eng.step)
        self.assertEqual(rig.ex.executions, [])
        rig2 = Rig()
        bad = DecisionResult(False, None, error="invalid AI decision: missing required fields", attempts=2)
        eng2 = make_engine(rig2, FakeDecider([bad]))
        eng2.startup()
        self.run_with(eng2.step)
        self.assertEqual(rig2.ex.executions, [])
        self.assertEqual(rig2.db.recent("ai_decisions")[0]["accepted"], 0)

    def test_risk_engine_can_veto_a_confident_ai(self):
        rig = Rig()
        eng = make_engine(rig, FakeDecider([decision(conf=99, sl=99_990.0, tp=100_020.0)]))   # absurdly tight stop
        eng.startup()
        self.run_with(eng.step)
        self.assertEqual(rig.ex.executions, [])
        row = rig.db.recent("ai_decisions")[0]
        self.assertEqual(row["accepted"], 0)
        self.assertIn("ATR", row["rejection_reason"])

    def test_autonomous_scanner_blocks_stale_configured_pair_when_no_candidate(self):
        rig = Rig()
        # Real exchange adapters expose this market-wide method; the test double normally does not.
        rig.market.get_all_tickers = lambda: []
        eng = make_engine(rig, FakeDecider([decision()]))
        eng.last_universe_scan = rig.clock()
        eng.scanner_selected_symbol = None
        reason = eng._blocked_reason()
        self.assertIn("no eligible pair", reason)
        self.assertIsNone(eng.scanner_selected_symbol)

    def test_autonomous_scanner_blocks_when_active_symbol_is_not_selected(self):
        rig = Rig()
        rig.market.get_all_tickers = lambda: []
        eng = make_engine(rig, FakeDecider([decision()]))
        eng.last_universe_scan = rig.clock()
        eng.scanner_selected_symbol = "BTCUSDT"
        eng.symbol = "XMRUSDT"
        reason = eng._blocked_reason()
        self.assertIn("scanner selected BTCUSDT", reason)

    def test_halted_system_does_not_even_ask_the_ai(self):
        rig = Rig()
        dec = FakeDecider([decision()])
        eng = make_engine(rig, dec)
        eng.startup()
        rig.halt.halt("test halt")
        self.run_with(eng.step)
        self.assertEqual(dec.calls, 0)
        self.assertEqual(rig.ex.executions, [])

    def test_daily_loss_limit_stops_trading(self):
        rig = Rig()
        dec = FakeDecider([decision()])
        eng = make_engine(rig, dec)
        eng.startup()
        rig.ex.balance = 9_700.0    # the account really lost 3% today
        self.run_with(eng.step)
        self.assertEqual(dec.calls, 0)
        self.assertEqual(rig.ex.executions, [])

    def test_resume_refused_while_risk_limit_still_active(self):
        rig = Rig()
        eng = make_engine(rig, FakeDecider([decision()]))
        eng.startup()
        eng.limits.last_equity = eng.limits.stats.start_equity * 0.97
        eng.halt.halt("daily loss limit reached")
        result = eng.resume()
        self.assertFalse(result["ok"])
        self.assertIn("risk limit still active", result["error"])
        self.assertTrue(eng.halt.halted)

    def test_restart_recovery_resumes_management_without_new_trade(self):
        rig = Rig()
        eng = make_engine(rig, FakeDecider([decision()]))
        eng.startup()
        self.run_with(eng.step)
        self.assertEqual(len(rig.ex.get_positions("BTCUSDT")), 1)
        n_exec = len(rig.ex.executions)
        # --- "crash": a brand new engine & executor over the SAME exchange and database
        from bot.execution.executor import Executor
        from bot.execution.state import HaltState
        from bot.risk.limits import LimitTracker
        dec2 = FakeDecider([decision()])
        eng2 = TradingEngine(rig.cfg, rig.db, rig.ex, rig.market, dec2, None, None, "fake", sleep=lambda s: None, clock=rig.clock)
        eng2.startup()
        self.assertTrue(eng2.recon.certain)
        self.assertFalse(eng2.recon.unexpected_position)        # known trade recognised
        self.assertEqual(eng2.limits.stats.trades, 1)           # daily counters restored from the DB
        rig.market.candles["5"] = [Candle(CANDLE_TS + 300_000, 100, 101, 99, 100, 1)]   # a NEW candle
        self.run_with(eng2.step)
        self.assertEqual(dec2.calls, 0)                         # position open => AI not asked
        self.assertEqual(len(rig.ex.executions), n_exec)        # no new order
        rig.market.set(100_450)
        eng2.last_atr5 = 200.0
        self.run_with(eng2.step)
        self.assertGreater(rig.ex.get_positions("BTCUSDT")[0]["stopLoss"], 99_700.0)   # still managed

    def test_restart_while_flat_does_not_reopen_the_same_signal(self):
        rig = Rig()
        eng = make_engine(rig, FakeDecider([decision()]))
        eng.startup()
        self.run_with(eng.step)
        rig.market.set(99_000)
        rig.ex.process_tick(99_000, 99_000, 99_000, 99_000)      # stop out
        eng.step()
        self.assertEqual(rig.ex.get_positions("BTCUSDT"), [])
        dec2 = FakeDecider([decision()])
        eng2 = TradingEngine(rig.cfg, rig.db, rig.ex, rig.market, dec2, None, None, "fake", sleep=lambda s: None, clock=rig.clock)
        eng2.startup()
        self.assertEqual(eng2.last_eval_ts, CANDLE_TS)          # remembered across restarts
        self.run_with(eng2.step)
        self.assertEqual(dec2.calls, 0)

    def test_unexpected_position_blocks_trading_and_is_reported(self):
        rig = Rig()
        rig.ex.positions["BTCUSDT"] = {"symbol": "BTCUSDT", "side": "Buy", "size": 0.05, "avgPrice": 99_000.0,
                                       "stopLoss": 0.0, "takeProfit": 0.0, "fees": 0, "openTime": 0, "openLink": "x"}
        dec = FakeDecider([decision()])
        eng = make_engine(rig, dec)
        from tests.helpers import flat
        rig.market.candles["5"] = flat(60)           # enough history for an ATR-based recovery stop
        eng.startup()
        self.assertTrue(eng.recon.unexpected_position)
        self.assertTrue(any(r["kind"] == "unexpected_position" for r in rig.db.recent("risk_events")))
        self.run_with(eng.step)
        self.assertEqual(dec.calls, 0)
        self.assertEqual(rig.ex.positions["BTCUSDT"]["size"], 0.05)       # untouched size
        self.assertEqual(rig.ex.positions["BTCUSDT"]["stopLoss"], 0.0)    # unmanaged position is read-only

    def test_unexpected_unprotectable_position_is_left_untouched(self):
        rig = Rig()
        rig.ex.positions["BTCUSDT"] = {"symbol": "BTCUSDT", "side": "Buy", "size": 0.05, "avgPrice": 99_000.0,
                                       "stopLoss": 0.0, "takeProfit": 0.0, "fees": 0, "openTime": 0, "openLink": "x"}
        eng = make_engine(rig, FakeDecider([decision()]))      # only 1 candle => no ATR => cannot place a stop
        eng.startup()
        self.assertEqual(len(rig.ex.get_positions("BTCUSDT")), 1)
        self.assertFalse(eng._blocked_reason() is None)  # unexpected position blocks entries without touching it

    def test_emergency_close_flattens_halts_and_needs_explicit_resume(self):
        rig = Rig()
        eng = make_engine(rig, FakeDecider([decision()]))
        eng.startup()
        self.run_with(eng.step)
        rep = eng.emergency("test")
        self.assertTrue(rep["ok"])
        self.assertEqual(rig.ex.get_positions("BTCUSDT"), [])
        self.assertTrue(rig.halt.halted)
        n = len(rig.ex.executions)
        rig.market.candles["5"] = [Candle(CANDLE_TS + 600_000, 100, 101, 99, 100, 1)]
        self.run_with(eng.step)
        self.assertEqual(len(rig.ex.executions), n)             # nothing opened while halted
        self.assertEqual(eng.resume(), {"ok": True})
        self.assertFalse(rig.halt.halted)

    def test_emergency_close_with_no_position_opens_nothing(self):
        rig = Rig()
        eng = make_engine(rig, FakeDecider([decision("WAIT")]))
        eng.startup()
        rep = eng.emergency()
        self.assertTrue(rep["ok"])
        self.assertEqual(rig.ex.executions, [])

    def test_halt_survives_restart(self):
        rig = Rig()
        eng = make_engine(rig, FakeDecider([decision()]))
        eng.startup()
        eng.emergency()
        eng2 = TradingEngine(rig.cfg, rig.db, rig.ex, rig.market, FakeDecider([decision()]), None, None, "f", sleep=lambda s: None, clock=rig.clock)
        self.assertTrue(eng2.halt.halted)

    def test_exchange_outage_is_survived_without_trading(self):
        rig = Rig()
        dec = FakeDecider([decision()])
        eng = make_engine(rig, dec)
        eng.startup()
        with mock.patch.object(rig.market, "get_ticker", side_effect=ExchangeError("down", None, True)):
            for _ in range(4):
                eng.step()
        self.assertFalse(eng.health.api_healthy)
        self.run_with(lambda: None)
        self.assertEqual(dec.calls, 0)
        self.assertEqual(rig.ex.executions, [])

    def test_unhandled_exception_in_loop_does_not_kill_the_bot(self):
        rig = Rig()
        eng = make_engine(rig, FakeDecider([decision()]))
        eng.startup()
        with mock.patch.object(eng, "step", side_effect=RuntimeError("boom")):
            eng.stop_event.wait = lambda t=None: False
            eng.run(max_loops=3)
        self.assertIn("boom", eng.last_error)
        self.assertGreaterEqual(len(rig.db.recent("errors")), 1)
        self.assertFalse(eng.health.position_certain)       # uncertainty => no new trades

    def test_paper_state_is_persisted_for_restart(self):
        rig = Rig()
        eng = make_engine(rig, FakeDecider([decision()]))
        eng.startup()
        self.run_with(eng.step)
        fresh = PaperExchange(rig.market, 10_000.0, 0.055, 0.02, 0.0, clock=rig.clock)
        eng2 = TradingEngine(rig.cfg, rig.db, fresh, rig.market, FakeDecider([decision()]), None, None, "f", sleep=lambda s: None, clock=rig.clock)
        self.assertEqual(len(fresh.get_positions("BTCUSDT")), 1)
        eng2.startup()
        self.assertFalse(eng2.recon.unexpected_position)


if __name__ == "__main__":
    unittest.main()


class PermissionStartupTests(unittest.TestCase):
    def make(self, perm, mode="TESTNET"):
        class RealisticPaper(PaperExchange):
            simulated = False            # behaves like a real exchange from the engine's point of view
            environment = "TESTNET"
            def check_api_permissions(self_inner):
                return perm
        rig = Rig(ex_cls=RealisticPaper, cfg=make_cfg(mode=mode, testnet=(mode == "TESTNET")))
        return rig, make_engine(rig, FakeDecider([decision("WAIT")]))

    def test_key_with_withdrawal_permission_halts_trading(self):
        rig, eng = self.make({"ok": False, "withdraw": True, "problems": ["API key HAS WITHDRAWAL permission"]})
        eng.startup()
        self.assertTrue(rig.halt.halted)
        self.assertIn("WITHDRAWAL", rig.halt.reason)

    def test_live_mode_with_unusable_key_halts(self):
        rig, eng = self.make({"ok": False, "withdraw": False, "problems": ["API key is read-only"]}, mode="LIVE")
        eng.startup()
        self.assertTrue(rig.halt.halted)

    def test_trade_only_key_is_accepted(self):
        rig, eng = self.make({"ok": True, "withdraw": False, "problems": []})
        eng.startup()
        self.assertFalse(rig.halt.halted)


class EmergencyCliTests(unittest.TestCase):
    def test_cli_closes_paper_position_from_saved_state(self):
        import os, tempfile
        from unittest import mock
        from bot.database.database import Database
        rig = Rig()
        from tests.fakes import params
        rig.executor.open_position(params(), 1_790_000_000_000, 85)
        db_path = os.path.join(tempfile.mkdtemp(), "t.db")
        disk = Database(db_path)
        disk.kv_set("paper_state", rig.ex.dump_state())
        disk.close()
        cfg = make_cfg()
        cfg.raw["database"]["path"] = db_path
        cfg.raw["logging"]["directory"] = tempfile.mkdtemp()
        from bot import emergency_cli
        with mock.patch.object(emergency_cli, "load_config", return_value=cfg), \
             mock.patch.object(emergency_cli, "build_exchanges", return_value=(rig.market, PaperExchange(rig.market, 10_000.0, 0.055, 0.02, 0.0))):
            self.assertEqual(emergency_cli.main(["--yes"]), 0)
        db = Database(db_path)
        self.assertEqual(db.kv_get("paper_state")["positions"], {})
        self.assertTrue(db.kv_get("halt_state")["halted"])
