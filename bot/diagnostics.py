"""System diagnostics:  python -m bot.diagnostics [--quick] [--skip-tests] [--no-ai-call] [--testnet-orders]

Prints [PASS]/[FAIL]/[WARN]/[SKIP] per component with Reason and Fix for anything that is not PASS.
"""
from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from .config import PROJECT_ROOT, Config, ConfigError, execution_permission, live_gate_report, load_config


@dataclass
class Check:
    name: str
    status: str      # PASS | FAIL | WARN | SKIP
    detail: str = ""
    fix: str = ""


def _p(name, detail=""): return Check(name, "PASS", detail)
def _f(name, reason, fix): return Check(name, "FAIL", reason, fix)
def _w(name, reason, fix=""): return Check(name, "WARN", reason, fix)
def _s(name, reason): return Check(name, "SKIP", reason)


def check_platform() -> Check:
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return _p("Apple Silicon detected", platform.platform())
    return _w("Apple Silicon detected", f"this machine is {sys.platform}/{platform.machine()}, not Apple Silicon",
              "The bot works elsewhere, but the model choice and speed are tuned for Apple Silicon Macs.")


def check_python() -> Check:
    v = sys.version_info
    if v >= (3, 10):
        return _p("Python", f"{v.major}.{v.minor}.{v.micro}")
    return _f("Python", f"Python {v.major}.{v.minor} is too old (need 3.10+)", "Run ./setup.sh and accept the Python install.")


def check_dependencies() -> Check:
    missing = []
    for mod, pkg in (("yaml", "PyYAML"), ("dotenv", "python-dotenv"), ("pybit", "pybit")):
        try:
            __import__(mod)
        except Exception:
            missing.append(pkg)
    if not missing:
        return _p("Dependencies", "PyYAML, python-dotenv, pybit")
    sev = _f if "PyYAML" in missing else _w
    return sev("Dependencies", "missing: " + ", ".join(missing), "Run ./setup.sh (it offers to install them) or: .venv/bin/pip install -r requirements.txt")


def check_config(cfg: Optional[Config], err: str) -> Check:
    if cfg is None:
        return _f("Configuration", err, "Fix config/config.yaml or .env, or delete the bad line and re-run.")
    return _p("Configuration", f"{cfg.display_mode}; symbol {cfg.symbol}; trading_enabled={cfg.trading_enabled}; autonomous={cfg.autonomous_mode}")


def check_live_gate(cfg: Config) -> Check:
    perm = execution_permission(cfg)
    lines = "; ".join(live_gate_report(cfg))
    if cfg.mode == "LIVE" and perm.can_place_orders:
        return _w("Live trading gate", "LIVE orders are ENABLED. " + lines, "Set TRADING_ENABLED=false or AUTONOMOUS_MODE=false to stop live orders.")
    return _p("Live trading gate", ("live orders blocked: " + lines) if cfg.mode != "PAPER" else "PAPER mode - no orders ever leave this computer")


def check_ollama(cfg: Config):
    from .ai.ollama_client import OllamaClient
    from .hardware import resolve_model
    client = OllamaClient(cfg.ai["host"], timeout=10, max_retries=0)
    up = client.is_up()
    out = [_p("Ollama", f"server {client.version()} at {cfg.ai['host']}") if up else
           _f("Ollama", f"server not reachable at {cfg.ai['host']}", "Start it: `ollama serve` (or open the Ollama app). If not installed: ./setup.sh")]
    model = resolve_model(cfg.ai["model"], cfg.root)
    if not up:
        out.append(_s("AI model", f"{model}: cannot check (Ollama is down)"))
    elif client.has_model(model):
        out.append(_p("AI model", f"{model} installed"))
    else:
        out.append(_f("AI model", f"{model} is not downloaded", f"Run: ollama pull {model}   (or ./setup.sh)"))
    return out, client, model


def check_ai_response(cfg: Config, client, model: str) -> Check:
    from .ai.decision_engine import DecisionEngine
    eng = DecisionEngine(client, model, dict(cfg.ai, timeout_seconds=max(float(cfg.ai["timeout_seconds"]), 120)))
    t0 = time.time()
    res = eng.decide({"symbol": cfg.symbol, "price": 100000.0, "note": "diagnostic - no setup present, answer WAIT"},
                     "", ["diagnostic test: return WAIT"])
    if res.ok:
        return _p("AI structured output", f"valid JSON decision ({res.decision.decision}) in {time.time() - t0:.1f}s")
    return _f("AI structured output", res.error, "Try a different model via OLLAMA_MODEL in .env, or check `ollama list`.")


def check_bybit_public(cfg: Config):
    from .exchange.bybit_client import BybitExchange
    from .exchange.base import ExchangeError
    ex = BybitExchange(testnet=cfg.mode == "TESTNET", credentials=None, category=cfg.raw["category"])
    out: List[Check] = []
    try:
        t = ex.get_ticker(cfg.symbol)
        out.append(_p("Bybit API", f"{ex.environment} {cfg.symbol} price {t['price']}"))
        inst = ex.get_instrument(cfg.symbol)
        c5 = ex.get_klines(cfg.symbol, cfg.section("timeframes")["entry"], 300)
        c1 = ex.get_klines(cfg.symbol, cfg.section("timeframes")["structure"], 300)
        from .strategy.signals import build_snapshot
        snap = build_snapshot(cfg.strategy, cfg.symbol, c1, c5, None, {"price": t["price"]})
        out.append(_p("Market data", f"{len(c5)} x 5M and {len(c1)} x 1H candles; ATR(5M)={snap['atr_5m']}; tick {inst.tick_size}, step {inst.qty_step}; "
                                     f"funding {t['funding_rate']}, OI {t['open_interest']}"))
    except ExchangeError as e:
        out.append(_f("Bybit API", str(e), "Check your internet connection / VPN. Bybit may be blocked in some regions."))
        out.append(_s("Market data", "needs Bybit API"))
    except ValueError as e:
        out.append(_f("Market data", str(e), "Candle data was incomplete; try again in a minute."))
    return out


def check_permissions(cfg: Config) -> Check:
    from .exchange.bybit_client import BybitExchange
    from .exchange.base import ExchangeError
    if cfg.mode == "PAPER":
        return _s("API permissions", "PAPER mode uses no API key")
    creds = cfg.credentials()
    if not creds:
        return _s("API permissions", "REQUIRES YOUR CREDENTIALS (set BYBIT_API_KEY / BYBIT_API_SECRET in .env)")
    ex = BybitExchange(testnet=cfg.mode == "TESTNET", credentials=creds, category=cfg.raw["category"])
    try:
        r = ex.check_api_permissions()
        w = ex.get_wallet()
    except ExchangeError as e:
        return _f("API permissions", str(e), "Check the key/secret, that the key belongs to the right environment (testnet vs live), and its IP restriction.")
    if r["withdraw"]:
        return _f("API permissions", "key has WITHDRAWAL permission", "Delete this key and create a new one with ONLY Contract trade permission.")
    if not r["ok"]:
        return _f("API permissions", "; ".join(r["problems"]), "Create a key with 'Contract - Orders & Positions' permission.")
    return _p("API permissions", f"trade-only key OK; IP restricted: {r['ip_restricted']}; equity {w['equity']:.2f} USDT")


def check_websocket(cfg: Config) -> Check:
    from .exchange.websocket import StreamMonitor
    m = StreamMonitor(cfg.symbol, cfg.mode == "TESTNET", None, cfg.section("timeframes")["entry"])
    if not m.available:
        return _f("WebSocket", "pybit is not installed", "Run ./setup.sh or: .venv/bin/pip install pybit")
    if not m.start():
        return _f("WebSocket", m.error, "Check your internet connection.")
    deadline = time.time() + 10
    first = m.last_msg
    while time.time() < deadline and not m.ticker:
        time.sleep(0.3)
    ok = bool(m.ticker)
    m.stop()
    return _p("WebSocket", "receiving live ticker data") if ok else _f("WebSocket", "connected but no data within 10s", "Check firewall/VPN.")


def check_database(cfg: Config) -> Check:
    from .database.database import Database
    try:
        db = Database(cfg.path(cfg.raw["database"]["path"]))
        ok = db.integrity_ok()
        db.kv_set("diagnostic", time.time())
        db.close()
        return _p("Database", str(cfg.path(cfg.raw["database"]["path"]))) if ok else _f("Database", "integrity check failed", "Move data/trader.db aside (keep a copy) and restart to create a fresh one.")
    except Exception as e:
        return _f("Database", f"{type(e).__name__}: {e}", "Check that the data/ folder is writable.")


def check_risk_engine(cfg: Config) -> Check:
    from .ai.schemas import AIDecision
    from .risk.limits import LimitTracker
    from .risk.position_sizing import InstrumentInfo
    from .risk.risk_engine import RiskContext, RiskEngine
    eng = RiskEngine(cfg.risk, cfg.ai)
    lim = LimitTracker(dict(cfg.risk), 10_000.0)
    inst = InstrumentInfo("T", 0.1, 0.001, 0.001, 100, 5)
    ctx = RiskContext(100000, 99999.5, 100000.5, 200, 10000, 10000, inst, 0, 5, trend_aligned=True, has_trigger=True)
    good = AIDecision("LONG", 90, "MARKET", None, 99700, 100700, 2.3, "t", "TREND", "t", ())
    bad = AIDecision("LONG", 99, "MARKET", None, 100500, 99000, None, "t", "TREND", "t", ())   # wrong-side stop/target
    a, b = eng.validate(good, ctx, lim), eng.validate(bad, ctx, lim)
    if a.approved and not b.approved:
        return _p("Risk engine", "approves a valid setup, rejects an invalid one")
    return _f("Risk engine", "self-test failed (check risk settings in config.yaml)", "Restore config/config.yaml defaults.")


def check_dashboard(cfg: Config) -> Check:
    import http.client
    from dashboard.app import DashboardServer

    class Stub:
        cfg = None
        def get_state(self): return {"ok": True}
    srv = DashboardServer(Stub(), "127.0.0.1", 0).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
        c.request("GET", "/")
        r = c.getresponse()
        r.read()
        return _p("Dashboard", f"serves pages; configured for http://{cfg.raw['dashboard']['host']}:{cfg.raw['dashboard']['port']}") if r.status == 200 else _f("Dashboard", f"HTTP {r.status}", "Reinstall the project files.")
    except Exception as e:
        return _f("Dashboard", str(e), "Reinstall the project files.")
    finally:
        srv.stop()


def check_unit_tests() -> Check:
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=PROJECT_ROOT,
                       capture_output=True, text=True, timeout=600)
    tail = (r.stderr.strip().splitlines() or [""])[-3:]
    summary = " ".join(x for x in tail if x)
    return _p("Tests", summary) if r.returncode == 0 else _f("Tests", summary, "Run `.venv/bin/python -m unittest discover -s tests -t . -v` and read the first failure.")


def check_testnet_orders(cfg: Config) -> List[Check]:
    """Places ONE far-from-market limit order on TESTNET, verifies it, cancels it, reads positions. Never on LIVE."""
    from .exchange.bybit_client import BybitExchange
    from .exchange.base import ExchangeError
    from .risk.position_sizing import round_down_to_step, round_to_tick
    if cfg.mode != "TESTNET":
        return [_s("Testnet orders", "only runs in TESTNET mode (never touches LIVE)")]
    creds = cfg.credentials()
    if not creds:
        return [_s("Testnet orders", "REQUIRES YOUR CREDENTIALS (BYBIT_TESTNET_API_KEY / SECRET in .env)")]
    ex = BybitExchange(testnet=True, credentials=creds, category=cfg.raw["category"])
    out: List[Check] = []
    link = f"ait-diag-{int(time.time())}"
    try:
        inst = ex.get_instrument(cfg.symbol)
        price = ex.get_ticker(cfg.symbol)["price"]
        px = round_to_tick(price * 0.7, inst.tick_size)                      # 30% below market: will not fill
        qty = round_down_to_step(max(inst.min_qty, inst.min_notional * 1.5 / px), inst.qty_step)
        ex.place_order(symbol=cfg.symbol, side="Buy", orderType="Limit", qty=qty, price=px, orderLinkId=link)
        o = ex.get_order(cfg.symbol, link)
        out.append(_p("Testnet order creation", f"order {link} status {o['orderStatus'] if o else '?'}") if o and o["orderStatus"] in ("New", "Untriggered") else _f("Testnet order creation", f"unexpected state {o}", "Check testnet balance (request testnet funds in Bybit)."))
        ex.cancel_order(cfg.symbol, link)
        time.sleep(1)
        o = ex.get_order(cfg.symbol, link)
        out.append(_p("Testnet order cancellation", "cancelled") if o and o["orderStatus"] == "Cancelled" else _f("Testnet order cancellation", f"state {o}", "Cancel it manually in the Bybit testnet UI."))
        pos = ex.get_positions(cfg.symbol)
        out.append(_p("Testnet position retrieval", f"{len(pos)} open position(s)"))
    except ExchangeError as e:
        out.append(_f("Testnet orders", str(e), "Check testnet key permissions (Contract trade) and that you have testnet USDT."))
    return out


def run_checks(cfg: Optional[Config], err: str = "", quick: bool = False, skip_tests: bool = False,
               ai_call: bool = True, testnet_orders: bool = False, network: bool = True) -> List[Check]:
    res = [check_platform(), check_python(), check_dependencies(), check_config(cfg, err)]
    if cfg is None:
        return res
    res.append(check_live_gate(cfg))
    ol, client, model = check_ollama(cfg)
    res.extend(ol)
    if ai_call and not quick and ol[0].status == "PASS" and ol[1].status == "PASS":
        res.append(check_ai_response(cfg, client, model))
    if network:
        res.extend(check_bybit_public(cfg))
        res.append(check_permissions(cfg))
        res.append(check_websocket(cfg) if not quick else _s("WebSocket", "skipped in quick mode"))
    res += [check_database(cfg), check_risk_engine(cfg), check_dashboard(cfg)]
    if testnet_orders:
        res.extend(check_testnet_orders(cfg))
    res.append(_s("Tests", "skipped") if (quick or skip_tests) else check_unit_tests())
    return res


def format_report(checks: List[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"[{c.status}] {c.name}" + (f" - {c.detail}" if c.status in ("PASS", "SKIP") and c.detail else ""))
        if c.status in ("FAIL", "WARN"):
            lines.append(f"       Reason: {c.detail}")
            if c.fix:
                lines.append(f"       Fix: {c.fix}")
    n = {s: sum(1 for c in checks if c.status == s) for s in ("PASS", "WARN", "SKIP", "FAIL")}
    lines.append("")
    lines.append(f"Result: {n['PASS']} passed, {n['WARN']} warnings, {n['SKIP']} skipped, {n['FAIL']} FAILED")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="autonomous_ai_trader diagnostics")
    ap.add_argument("--quick", action="store_true", help="skip tests, AI call and WebSocket")
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--no-ai-call", action="store_true", help="do not ask the model for a test decision")
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--testnet-orders", action="store_true", help="place+cancel one far-away TESTNET limit order")
    ap.add_argument("--config", default=None)
    a = ap.parse_args(argv)
    cfg, err = None, ""
    try:
        cfg = load_config(a.config)
    except ConfigError as e:
        err = str(e)
    checks = run_checks(cfg, err, a.quick, a.skip_tests, not a.no_ai_call, a.testnet_orders, not a.no_network)
    if cfg:
        print(f"Mode: {cfg.display_mode}\n")
    print(format_report(checks))
    return 1 if any(c.status == "FAIL" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
