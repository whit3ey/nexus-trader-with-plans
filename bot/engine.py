"""The trading engine: monitoring loop, strategy evaluation, AI decision, risk gate, execution.

Main loop (every loop_interval_seconds):
  market data -> account -> positions -> close detection -> pending entries -> position management
  -> (on each closed 5M candle) indicators/FVG/OB/liquidity/structure -> setup pre-filter
  -> local AI -> JSON validation -> risk engine -> execution -> confirmation -> monitoring.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .ai.decision_engine import DecisionEngine
from .agents import MultiAgentCoordinator
from .alerts import Alerts
from .config import Config, execution_permission, live_gate_report
from .database.database import Database
from .exchange.base import Exchange, ExchangeError
from .execution.emergency import emergency_close
from .execution.executor import Executor
from .execution.reconciliation import ReconReport, reconcile
from .execution.state import HaltState
from .logging_config import get_logger
from .monitoring.health import HealthMonitor
from .news_intel import NewsIntel
from .risk.limits import LimitTracker
from .risk.risk_engine import RiskContext, RiskEngine
from .strategy.atr import latest_atr
from .strategy.signals import build_snapshot, evaluate_setup

log = get_logger("bot")
tlog = get_logger("bot.trading")

INTERVAL_MS = {"1": 60_000, "5": 300_000, "15": 900_000, "60": 3_600_000}


class TradingEngine:
    def __init__(self, cfg: Config, db: Database, exchange: Exchange, market: Exchange,
                 decider: Optional[DecisionEngine], alerts: Optional[Alerts] = None, streams=None,
                 model_name: str = "", sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time):
        self.cfg, self.db, self.ex, self.market = cfg, db, exchange, market
        self.decider, self.alerts, self.streams = decider, alerts, streams
        self.model_name = model_name
        self.sleep, self.clock = sleep, clock
        self.symbol = cfg.symbol
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.halt = HaltState(db)
        self.health = HealthMonitor(float(cfg.risk["max_data_age_seconds"]))
        e = cfg.section("execution")
        self.fee_rt = float(e["taker_fee_percent"]) * 2 / 100.0 + float(e["slippage_percent_paper"]) * 2 / 100.0
        self.risk = RiskEngine(cfg.risk, cfg.ai, cfg.section("tp"), fee_round_trip=self.fee_rt)
        self.paper = bool(getattr(exchange, "simulated", False))
        if self.paper:
            saved = db.kv_get("paper_state")
            if saved:
                exchange.load_state(saved)       # type: ignore[attr-defined]
        wallet = self._wallet_or_zero()
        self.limits = self._restore_limits(wallet["equity"])
        self.executor = Executor(cfg, exchange, db, self.limits, self.halt, alerts, sleep=sleep, clock=clock, streams=streams)
        self.last_eval_ts: Optional[int] = db.kv_get("last_eval_ts")
        self.last_recon = 0.0
        self.last_ollama_check = 0.0
        self.recon: Optional[ReconReport] = None
        self.last_atr5: float = 0.0
        self.last_price: float = 0.0
        self.last_snapshot: Dict[str, Any] = {}
        self.last_decision: Dict[str, Any] = {}
        self.last_setup: Dict[str, Any] = {}
        self.last_ticker: Dict[str, Any] = {}
        self.wallet = wallet
        self.permissions: Dict[str, Any] = {}
        self.started_at = clock()
        self.loop_count = 0
        self.last_error = ""
        self._alerted: set = set(db.kv_get("risk_alerted_keys", []) or [])
        self.last_universe_scan = 0.0
        self.universe_candidates: List[Dict[str, Any]] = []
        self.universe_scan_note = "Waiting for first market scan"
        # When the exchange supports market-wide scanning, this is the only symbol
        # allowed to open new positions. A configured fallback symbol is display-only.
        self.scanner_selected_symbol: Optional[str] = None
        self.recent_events: List[str] = []
        self.intelligence = NewsIntel()
        self.last_intelligence_scan = 0.0
        self._intelligence_thread = None
        ma = cfg.section("multi_agent")
        self.multi_agent = MultiAgentCoordinator(bool(ma.get("enabled", True)), int(ma.get("max_workers", 4)))
        self.last_agent_report: Dict[str, Any] = self.multi_agent.last_report
        self.last_candles: List[Dict[str, float]] = []
        # Plans are asked at most once per coin per cooldown, even across restarts.
        saved_asks = db.kv_get("plan_asked") or {}
        self._plan_asked: Dict[str, float] = {str(k): float(v) for k, v in saved_asks.items()} if isinstance(saved_asks, dict) else {}

    # ------------------------------------------------------------------ setup helpers
    def _wallet_or_zero(self) -> Dict[str, float]:
        try:
            w = self.ex.get_wallet()
            self.health.account_confirmed = True
            return w
        except ExchangeError as e:
            self.health.account_confirmed = False
            self.health.api_failure(str(e))
            return {"equity": 0.0, "available": 0.0}

    def _restore_limits(self, equity: float) -> LimitTracker:
        peak = self.db.kv_get("peak_equity") or equity
        t = LimitTracker(dict(self.cfg.risk), equity or 1.0, peak_equity=max(peak, equity))
        saved = self.db.load_daily_stats(t.stats.day)
        if saved:
            s = t.stats
            for k in ("start_equity", "trades", "wins", "losses", "realized_pnl", "fees", "consecutive_losses",
                      "max_drawdown_pct"):
                if k in saved:
                    setattr(s, k, saved[k])
            s.r_multiples = list(saved.get("r_multiples", []))
        return t

    def event(self, msg: str) -> None:
        self.recent_events.append(f"{datetime.now(timezone.utc):%H:%M:%S}Z {msg}")
        self.recent_events = self.recent_events[-30:]
        tlog.info(msg)

    def alert(self, event: str, msg: str, dedupe: float = 0) -> None:
        if self.alerts:
            self.alerts.send(event, msg, dedupe)

    # ------------------------------------------------------------------ startup
    def startup(self) -> None:
        """Permission check + reconciliation. Never assumes there are no positions."""
        mode = self.cfg.mode
        self.event(f"starting in {self.cfg.display_mode}")
        for line in live_gate_report(self.cfg):
            log.info("live-gate: %s", line)
        if not self.paper:
            try:
                self.permissions = self.ex.check_api_permissions()
            except ExchangeError as e:
                self.permissions = {"ok": False, "problems": [f"cannot verify API permissions: {e}"]}
            if self.permissions.get("withdraw"):
                self.halt.halt("API key has WITHDRAWAL permission - replace it with a trade-only key")
            elif not self.permissions.get("ok") and mode == "LIVE":
                self.halt.halt("; ".join(self.permissions.get("problems", [])) or "API permissions not verified")
        self.do_reconcile()
        # Never alter an unmanaged position. It may be a manual trade or another bot's trade.
        if self.recon and self.recon.unprotected_position and self.db.open_trades(self.symbol):
            self.protect_unprotected()
        self.alert("bot_started", f"{self.cfg.display_mode} {self.symbol} model={self.model_name}")

    def do_reconcile(self) -> ReconReport:
        with self.lock:
            r = reconcile(self.ex, self.db, self.symbol,
                          executor_close_recorder=lambda t, note: self.executor.record_close(self.db.get_trade(t["id"]), note),
                          alerts=self.alerts)
            self.recon = r
            self.last_recon = self.clock()
            self.health.position_certain = r.certain
            self.health.order_certain = r.certain
            if r.pending_link and not self.executor.pending:
                self.event(f"pending entry {r.pending_link} found at startup")
            for a in r.actions:
                self.event("reconcile: " + a)
            return r

    def protect_unprotected(self) -> None:
        """Safe recovery: a position without a stop loss gets an ATR-based protective stop, else it is closed."""
        try:
            pos = self.ex.get_positions(self.symbol)
            if not pos:
                return
            p = pos[0]
            atr = self.last_atr5 or self._fetch_atr()
            inst = self.ex.get_instrument(self.symbol)
            if atr and atr > 0:
                from .risk.position_sizing import round_to_tick
                dist = 1.5 * atr
                sl = round_to_tick(p["avgPrice"] - dist if p["side"] == "Buy" else p["avgPrice"] + dist, inst.tick_size)
                self.ex.set_trading_stop(self.symbol, stop_loss=sl)
                self.event(f"recovery: protective stop set at {sl}")
                self.db.record_risk_event("recovery_stop", f"stop set at {sl}", "WARN")
                return
            raise ExchangeError("no ATR available for protective stop")
        except ExchangeError as e:
            from .execution.emergency import close_position_reduce_only
            self.event(f"recovery: could not protect position ({e}) - closing it")
            close_position_reduce_only(self.ex, self.symbol, self.sleep, tag="recover")
            self.halt.halt("unprotected position could not be secured")

    def _fetch_atr(self) -> float:
        try:
            c = self.market.get_klines(self.symbol, "5", 60)
            return latest_atr(c, int(self.cfg.strategy["atr_period"])) or 0.0
        except ExchangeError:
            return 0.0

    # ------------------------------------------------------------------ helpers for the risk context
    def ws_ok(self) -> bool:
        if self.paper or not self.cfg.section("execution").get("require_websocket", True):
            return True
        return self.streams is not None and self.streams.state() == "healthy"

    def _positions(self) -> List[Dict[str, Any]]:
        try:
            pos = self.ex.get_positions(self.symbol)
            self.health.position_certain = True
            self.health.api_success()
            return pos
        except ExchangeError as e:
            self.health.position_certain = False
            self.health.api_failure(str(e))
            raise

    # ------------------------------------------------------------------ one loop iteration
    def step(self) -> None:
        self.loop_count += 1
        now = self.clock()
        with self.lock:
            try:
                tk = self.market.get_ticker(self.symbol)
                self.health.api_success()
                self.health.market_data()
            except ExchangeError as e:
                self.health.api_failure(str(e))
                self.last_error = str(e)
                if not self.health.api_healthy:
                    self.alert("api_failure", str(e), dedupe=600)
                self._publish()
                return
            self.last_ticker, price = tk, float(tk["price"])
            prev = self.last_price or price
            self.last_price = price
            if self.paper:
                self.ex.process_tick(max(prev, price), min(prev, price), tk["bid"], tk["ask"])   # type: ignore[attr-defined]
            wallet = self._wallet_or_zero()
            if wallet["equity"] > 0:
                self.wallet = wallet
            try:
                positions = self._positions()
            except ExchangeError:
                self._publish()
                return
            unreal = sum(p.get("unrealisedPnl", 0.0) for p in positions)
            self.limits.roll_day_if_needed(self.wallet["equity"])
            self.limits.update_equity(self.wallet["equity"], unreal)
            self.db.kv_set("peak_equity", self.limits.peak_equity)
            for res in self.executor.detect_closed(positions):
                self.event(f"trade closed: {res['reason']} {res['pnl']:+.2f} USDT ({res['r']:+.2f}R)")
            done = self.executor.service_pending(self.last_price if self.last_price > 0 else None)
            if done:
                self.event(f"pending entry: {done.status} {done.reason}")
                try:
                    positions = self._positions()
                except ExchangeError:
                    positions = []
            if positions and self.last_atr5:
                # Position management is limited to a position recorded by this bot.
                # Unknown/manual/other-bot positions are read-only and remain untouched.
                owned = self.db.open_trades(self.symbol)
                owned_sides = {t.get("direction") for t in owned}
                for p in positions:
                    direction = "LONG" if p.get("side") == "Buy" else "SHORT"
                    if direction in owned_sides:
                        for a in self.executor.manage_position(p, self.last_atr5):
                            self.event(a)
            halt_reasons = self.limits.halt_reasons()
            for reason in halt_reasons:
                # Deduplicate by the risk-limit class, not the changing percentage in the message.
                alert_key = "account_drawdown" if "drawdown" in reason.lower() else ("daily_loss" if "loss" in reason.lower() else reason.split(":", 1)[0])
                if alert_key not in self._alerted:
                    self._alerted.add(alert_key)
                    self.db.kv_set("risk_alerted_keys", sorted(self._alerted))
                    self.halt.halt(reason)
                    self.event("LIMIT: " + reason + " (trading halted until Telegram /reset)")
                    self.alert("daily_loss_limit" if alert_key == "daily_loss" else "system_error", reason)
            # Market-wide scan chooses the only symbol allowed to open a new position.
            # If no candidate qualifies, new entries are blocked instead of falling back
            # to the symbol from config.yaml (which may be stale, e.g. XMRUSDT).
            if now - self.last_universe_scan >= 300:
                self._scan_market_universe()
            if now - self.last_intelligence_scan >= 900 and (self._intelligence_thread is None or not self._intelligence_thread.is_alive()):
                self.last_intelligence_scan = now
                def refresh_intelligence():
                    try:
                        self.intelligence.refresh()
                        # DEX lookup runs off the trading loop so public-data latency cannot
                        # delay risk management or order-state monitoring.
                        self.intelligence.token_market(self.symbol)
                        self.event("news/network intelligence refreshed")
                    except Exception as exc:
                        self.intelligence.status = "Refresh failed: " + type(exc).__name__
                        log.warning("intelligence refresh failed: %s", type(exc).__name__)
                self._intelligence_thread = threading.Thread(target=refresh_intelligence, name="market-intelligence", daemon=True)
                self._intelligence_thread.start()
            if now - self.last_recon > 60:
                self.do_reconcile()
            if self.decider is not None and now - self.last_ollama_check > 30:
                self.last_ollama_check = now
                up = self.decider.client.is_up()
                self.health.set_ollama(up, "" if up else "Ollama server not reachable")
            self._persist_paper()
            self._publish()
        self.maybe_evaluate()
        with self.lock:
            self._persist_paper()

    def _scan_market_universe(self) -> None:
        """Rank liquid pairs, evaluate technical setups, and select the best safe candidate.

        Selection is only allowed when the current symbol has no position/order. Candidate
        symbols with any exchange position or open order are skipped without modification.
        """
        self.last_universe_scan = self.clock()
        self.scanner_selected_symbol = None
        try:
            fetch = getattr(self.market, "get_all_tickers", None)
            if not callable(fetch):
                self.universe_scan_note = "Exchange adapter does not support market-wide ticker scans"
                return
            tickers = fetch()
            max_spread = float(self.cfg.risk.get("max_spread_percent", 0.05))
            ranked = []
            for t in tickers:
                symbol = str(t.get("symbol", ""))
                turnover = float(t.get("turnover_24h") or 0)
                spread = float(t.get("spread_pct") or 999)
                price = float(t.get("price") or 0)
                if not symbol.endswith("USDT") or price <= 0 or turnover < 1_000_000 or spread > max_spread:
                    continue
                score = min(50.0, turnover / 20_000_000) + max(0.0, 30.0 - spread * 300.0)
                ranked.append({"symbol": symbol, "price": price, "turnover_24h": turnover,
                               "spread_pct": round(spread, 5), "funding_rate": t.get("funding_rate"),
                               "open_interest": t.get("open_interest"), "score": round(score, 2),
                               "technical_score": 0.0, "setup": "not evaluated",
                               "reason": "Liquidity/spread screen passed; technical confirmation pending"})
            ranked.sort(key=lambda x: x["score"], reverse=True)

            # Evaluate the entire eligible universe by default. A positive cap is an
            # explicit operator override; 0 means all pairs that passed the filters.
            tf = self.cfg.section("timeframes")
            configured_cap = int(self.cfg.section("scanner").get("max_candidates_to_evaluate", 0))
            max_candidates = max(0, configured_cap)
            evaluable = ranked if max_candidates == 0 else ranked[:max_candidates]
            # Prefer account-wide snapshots so scanning a large universe does not make
            # hundreds of private position/order calls. If these reads fail, fail closed.
            occupied_symbols = set()
            all_positions = getattr(self.ex, "get_all_positions", None)
            all_orders = getattr(self.ex, "get_all_open_orders", None)
            if callable(all_positions) and callable(all_orders):
                try:
                    occupied_symbols.update(str(p.get("symbol", "")) for p in all_positions())
                    occupied_symbols.update(str(o.get("symbol", "")) for o in all_orders())
                except Exception as exc:
                    self.universe_scan_note = f"Market scan blocked: account-wide positions/orders unreadable ({type(exc).__name__}); no entries allowed"
                    self.event(self.universe_scan_note)
                    return
            for item in evaluable:
                symbol = item["symbol"]
                try:
                    # Existing positions and orders are never cancelled, closed, or modified.
                    occupied = (symbol in occupied_symbols) if callable(all_positions) and callable(all_orders) else bool(self.ex.get_positions(symbol) or self.ex.get_open_orders(symbol))
                    if occupied:
                        item["setup"] = "occupied"
                        item["reason"] = "Existing position/order detected; excluded and left untouched"
                        continue
                    c5 = self._closed(self.market.get_klines(symbol, tf["entry"], int(tf["candles_to_load"])), tf["entry"])
                    c1h = self._closed(self.market.get_klines(symbol, tf["structure"], int(tf["candles_to_load"])), tf["structure"])
                    c15 = self._closed(self.market.get_klines(symbol, tf["confirm"], 120), tf["confirm"])
                    if not c5 or not c1h or not c15:
                        item["setup"] = "insufficient data"
                        continue
                    ticker = self.market.get_ticker(symbol)
                    snap = build_snapshot(self.cfg.strategy, symbol, c1h, c5, c15,
                        {"price": ticker.get("price"), "bid": ticker.get("bid"), "ask": ticker.get("ask"),
                         "funding_rate": ticker.get("funding_rate"), "open_interest": ticker.get("open_interest"), "position": None})
                    setup = evaluate_setup(snap, self.cfg.strategy)
                    item["technical_score"] = float(setup.score or 0)
                    item["setup"] = setup.direction_hint if setup.has_setup else "WAIT"
                    item["reason"] = "; ".join(setup.reasons[:3]) if setup.reasons else "No valid setup"
                    item["candidate_price"] = ticker.get("price")
                except Exception as exc:
                    item["setup"] = "scan error"
                    item["reason"] = f"{type(exc).__name__}: {str(exc)[:120]}"

            # Stay on a pair only when it has a position/order this bot can prove it owns.
            # Unknown/manual/other-bot activity is read-only: leave it untouched and allow
            # the scanner to select a different, unoccupied pair.
            active_positions = self.ex.get_positions(self.symbol)
            active_orders = self.ex.get_open_orders(self.symbol)
            owned_trades = self.db.open_trades(self.symbol)
            known_links = {o.get("order_link_id") for o in self.db.recent_orders(self.symbol, 7 * 86400)}
            owned_position_sides = {t.get("direction") for t in owned_trades}
            has_owned_position = any(("LONG" if p.get("side") == "Buy" else "SHORT") in owned_position_sides
                                     for p in active_positions)
            has_owned_order = any(o.get("orderLinkId") in known_links for o in active_orders)
            active_busy = has_owned_position or has_owned_order or bool(self.executor.pending)
            eligible = [x for x in evaluable if x.get("setup") in ("LONG", "SHORT") and x.get("technical_score", 0) > 0]
            eligible.sort(key=lambda x: (x.get("technical_score", 0), x.get("score", 0)), reverse=True)
            selected = eligible[0] if eligible else None
            if selected and not active_busy and not self.executor.pending and not self.halt.blocking_reason():
                chosen = selected["symbol"]
                self.scanner_selected_symbol = chosen
                if chosen != self.symbol:
                    old = self.symbol
                    self.symbol = chosen
                    self.executor.symbol = chosen
                    self.last_eval_ts = None
                    self.last_ticker = self.market.get_ticker(chosen)
                    self.last_price = float(self.last_ticker.get("price") or 0)
                    self.last_snapshot = {}
                    self.last_setup = {"has_setup": True, "direction": selected["setup"],
                                       "score": selected["technical_score"], "reasons": [selected["reason"]]}
                    self.recon = None
                    self.event(f"AI scanner selected {chosen} over {old}: technical={selected['technical_score']:.2f}, liquidity={selected['score']:.2f}")
            self.universe_candidates = sorted(evaluable, key=lambda x: (x.get("technical_score", 0), x.get("score", 0)), reverse=True)
            self.universe_scan_note = (f"Scanned {len(tickers)} tickers; {len(ranked)} passed liquidity/spread filters; "
                f"technically evaluated {sum(1 for x in evaluable if x.get('setup') not in ('occupied', 'scan error', 'not evaluated'))} candidates "
                f"(configured cap: {'all eligible' if max_candidates == 0 else max_candidates}). " +
                (f"Selected {selected['symbol']} {selected['setup']} for new entries." if self.scanner_selected_symbol else "No eligible technical setup or active pair is occupied; new entries blocked."))
            self.event("market scan completed: " + self.universe_scan_note)
        except Exception as exc:
            self.universe_scan_note = f"Market scan failed: {type(exc).__name__}: {exc}"
            log.warning(self.universe_scan_note)

    def _persist_paper(self) -> None:
        if self.paper:
            self.db.kv_set("paper_state", self.ex.dump_state())          # type: ignore[attr-defined]

    # ------------------------------------------------------------------ strategy / AI / risk / execution
    def _closed(self, candles, interval: str):
        ms = INTERVAL_MS[interval]
        now_ms = int(self.clock() * 1000)
        return [c for c in candles if c.ts + ms <= now_ms]

    def maybe_evaluate(self) -> None:
        tf = self.cfg.section("timeframes")
        with self.lock:
            try:
                c5 = self._closed(self.market.get_klines(self.symbol, tf["entry"], int(tf["candles_to_load"])), tf["entry"])
            except ExchangeError as e:
                self.health.api_failure(str(e))
                return
            if not c5:
                return
            candle_ts = c5[-1].ts
            if candle_ts == self.last_eval_ts:
                return                                  # one evaluation per closed 5M candle (no duplicate events)
            try:
                c1h = self._closed(self.market.get_klines(self.symbol, tf["structure"], int(tf["candles_to_load"])), tf["structure"])
                c15 = self._closed(self.market.get_klines(self.symbol, tf["confirm"], 120), tf["confirm"])
            except ExchangeError as e:
                self.health.api_failure(str(e))
                return
            tk = self.last_ticker
            try:
                snap = build_snapshot(self.cfg.strategy, self.symbol, c1h, c5, c15,
                                      {"price": tk.get("price"), "bid": tk.get("bid"), "ask": tk.get("ask"),
                                       "funding_rate": tk.get("funding_rate"), "open_interest": tk.get("open_interest"),
                                       "position": None})
            except ValueError as e:
                self.event(f"strategy skipped: {e}")
                self.last_eval_ts = candle_ts
                return
            self.last_atr5 = snap["atr_5m"]
            self.last_snapshot = snap
            self.last_candles = [{"open": c.open, "high": c.high, "low": c.low, "close": c.close} for c in c5[-80:]]
            self.last_eval_ts = candle_ts
            self.db.kv_set("last_eval_ts", candle_ts)
            self.db.record_snapshot(self.symbol, snap)
            setup = evaluate_setup(snap, self.cfg.strategy)
            self.last_setup = {"has_setup": setup.has_setup, "direction": setup.direction_hint,
                               "reasons": setup.reasons, "score": setup.score,
                               "kind": setup.kind, "zone": setup.zone}
            blocked = self._blocked_reason()
            if not setup.has_setup:
                self.db.record_signal(self.symbol, None, False, "WAIT: " + "; ".join(setup.reasons), {"candle": candle_ts})
                return
            if blocked:
                self.db.record_signal(self.symbol, setup.direction_hint, False, "blocked: " + blocked)
                self.event(f"setup {setup.direction_hint} ignored: {blocked}")
                return
            if setup.kind == "plan" and self._plan_on_cooldown(self.symbol):
                self.db.record_signal(self.symbol, setup.direction_hint, False, "plan cooldown: AI already asked",
                                      {"candle": candle_ts})
                self.event(f"plan {setup.direction_hint} not re-asked (15 minute cooldown)")
                return
        # AI call happens OUTSIDE the lock so monitoring and the dashboard stay responsive.
        self._decide_and_execute(snap, setup, candle_ts)

    def _plan_on_cooldown(self, symbol: str) -> bool:
        cooldown = float(self.cfg.strategy.get("plan_ask_cooldown_seconds", 900))
        last = float(self._plan_asked.get(symbol) or 0)
        return self.clock() - last < cooldown

    def _blocked_reason(self) -> Optional[str]:
        # On adapters that support a market-wide scan, never trade the stale configured
        # symbol when the scanner has no eligible candidate or selected a different pair.
        if callable(getattr(self.market, "get_all_tickers", None)) and self.last_universe_scan > 0:
            if not self.scanner_selected_symbol:
                return "autonomous scanner found no eligible pair; waiting without opening trades"
            if self.scanner_selected_symbol != self.symbol:
                return f"scanner selected {self.scanner_selected_symbol}; active symbol {self.symbol} is stale"
        if self.decider is None:
            return "no AI model available"
        h = self.halt.blocking_reason()
        if h:
            return h
        if self.executor.pending:
            return "entry order pending"
        if self.ex.get_positions(self.symbol):
            return "position already open; existing position is read-only unless recorded as bot-owned"
        limit = self.limits.halt_reasons()
        if limit:
            return "; ".join(limit)
        if not self.health.api_healthy:
            return "Bybit API unhealthy"
        if not (self.recon and self.recon.certain):
            return "exchange state not reconciled"
        if self.recon.unexpected_position:
            return "unexpected position present"
        if getattr(self.recon, "unexpected_order", False):
            return "unmanaged opening order present; left untouched"
        return None

    def _tools(self) -> Dict[str, Callable[[], Any]]:
        return {"get_market_snapshot": lambda: self.last_snapshot,
                "get_current_position": lambda: self.ex.get_positions(self.symbol) or None,
                "get_open_orders": lambda: self.ex.get_open_orders(self.symbol),
                "get_account_summary": lambda: {"equity": self.wallet["equity"], "available": self.wallet["available"],
                                                "today": self.limits.snapshot()},
                "get_strategy_analysis": lambda: self.last_setup}

    def _decide_and_execute(self, snap: Dict[str, Any], setup, candle_ts: int) -> None:
        assert self.decider is not None
        self.decider.tools = self._tools() if self.cfg.ai.get("use_tools") else {}
        # Give the model timestamped public context, clearly separated from exchange-derived indicators.
        ai_snapshot = dict(snap)
        external_intelligence = self.intelligence.snapshot(self.symbol)
        self.last_agent_report = self.multi_agent.review(snap, setup, external_intelligence, self.cfg.risk)
        ai_snapshot["external_intelligence"] = external_intelligence
        ai_snapshot["specialist_agent_review"] = self.last_agent_report
        if setup.kind == "plan" and setup.zone:
            ai_snapshot["plan"] = {
                "kind": "PLAN",
                "direction": setup.direction_hint,
                "zone_lower": setup.zone.get("lower"),
                "zone_upper": setup.zone.get("upper"),
                "suggested_limit": setup.zone.get("entry"),
                "distance_atr": setup.zone.get("distance_atr"),
                "note": "Price is not at the zone. A limit must rest inside the zone, on the pullback side.",
            }
            self._plan_asked[self.symbol] = self.clock()
            self.db.kv_set("plan_asked", self._plan_asked)
        res = self.decider.decide(ai_snapshot, setup.direction_hint or "", setup.reasons, setup.kind or "trigger")
        self.health.set_ollama(res.ok or "unavailable" not in res.error, res.error if not res.ok else "")
        if not res.ok:
            self.db.record_ai_decision(self.symbol, self.model_name, None, False, res.error)
            self.db.record_signal(self.symbol, setup.direction_hint, False, "AI: " + res.error)
            self.last_decision = {"decision": None, "error": res.error, "time": self.clock(), "accepted": False}
            self.event("AI rejected/failed: " + res.error[:120])
            if "unavailable" in res.error:
                self.alert("ai_failure", res.error, dedupe=900)
            return
        d = res.decision
        with self.lock:
            tk = self.last_ticker
            try:
                inst = self.ex.get_instrument(self.symbol)
                open_n = len(self.ex.get_positions(self.symbol))
            except ExchangeError as e:
                self.db.record_signal(self.symbol, d.decision, False, f"state unreadable: {e}")
                return
            zone = setup.zone or {}
            is_plan = setup.kind == "plan"
            ctx = RiskContext(
                price=float(tk.get("price", 0)), bid=float(tk.get("bid", 0)), ask=float(tk.get("ask", 0)),
                atr=snap["atr_5m"], equity=self.wallet["equity"], available_balance=self.wallet["available"],
                instrument=inst, open_positions=open_n, data_age_seconds=self.health.data_age(),
                api_healthy=self.health.api_healthy, ws_healthy=self.ws_ok(), ollama_healthy=(self.health.ollama_ok is not False),
                account_state_confirmed=self.health.account_confirmed,
                position_state_certain=self.health.position_certain and bool(self.recon and self.recon.certain),
                order_state_certain=self.health.order_certain,
                volatility_ratio=snap.get("volatility_ratio_5m") or None,
                trend_aligned=(d.decision == "LONG" and snap["trend_1h"] == "bullish") or
                              (d.decision == "SHORT" and snap["trend_1h"] == "bearish"),
                has_trigger=(not is_plan) and setup.has_setup and setup.direction_hint == d.decision,
                trading_halted_reason=self.halt.blocking_reason(),
                is_plan=is_plan,
                plan_zone_lower=zone.get("lower"),
                plan_zone_upper=zone.get("upper"))
            rr = self.risk.validate(d, ctx, self.limits)
            self.db.record_ai_decision(self.symbol, self.model_name, d.to_dict(), rr.approved,
                                       "" if rr.approved else "; ".join(rr.reasons))
            self.last_decision = {"decision": d.decision, "confidence": d.confidence, "setup": d.setup_type,
                                  "reason": d.reason, "time": self.clock(), "accepted": rr.approved,
                                  "entry_type": d.entry_type, "entry_price": d.entry_price,
                                  "stop_loss": d.stop_loss, "take_profit": d.take_profit,
                                  "risk_reward": d.risk_reward, "market_regime": d.market_regime,
                                  "invalidations": list(d.invalidations),
                                  "valid_minutes": d.valid_minutes, "abandon_price": d.abandon_price,
                                  "tp_management": d.tp_management, "risk_scale": d.risk_scale,
                                  "rejection": "" if rr.approved else "; ".join(rr.reasons), "latency_s": round(res.latency_s, 1)}
            if not rr.approved:
                self.db.record_signal(self.symbol, d.decision, False, "risk: " + "; ".join(rr.reasons), d.to_dict())
                self.db.record_risk_event("rejected", "; ".join(rr.reasons), "INFO", {"decision": d.to_dict()})
                self.event(f"AI {d.decision} ({d.confidence:.0f}) rejected by risk engine: {'; '.join(rr.reasons)[:140]}")
                return
            self.event(f"AI {d.decision} ({d.confidence:.0f}) approved by risk engine - executing")
            ex_res = self.executor.open_position(rr.order, candle_ts, d.confidence,
                                                 {"snapshot": {k: snap[k] for k in ("price", "trend_1h", "structure_1h", "atr_5m", "bos_5m", "choch_5m", "liquidity_sweep")},
                                                  "ai": d.to_dict(), "setup_reasons": setup.reasons,
                                                  "risk": rr.order.__dict__})
            self.db.record_signal(self.symbol, d.decision, ex_res.ok, f"execution: {ex_res.status} {ex_res.reason}", d.to_dict())
            self.event(f"execution: {ex_res.status} - {ex_res.reason}")
            if ex_res.status == "uncertain":
                self.health.order_certain = False
            if ex_res.status == "unprotected_closed":
                self.alert("system_error", ex_res.reason)

    # ------------------------------------------------------------------ controls (dashboard / CLI)
    def pause(self) -> None:
        self.halt.paused_by_user = True
        self.event("trading PAUSED by user")

    def resume(self) -> Dict[str, Any]:
        with self.lock:
            r = self.do_reconcile()
            if not r.certain:
                return {"ok": False, "error": "cannot resume: exchange state could not be verified"}
            active_limits = self.limits.halt_reasons()
            if active_limits:
                return {"ok": False, "error": "risk limit still active; use Telegram /reset only after reviewing current equity and risk: " + "; ".join(active_limits)}
            if r.unexpected_position:
                return {"ok": False, "error": "cannot resume: an unexpected position exists - close it first"}
            self.halt.paused_by_user = False
            if self.halt.halted:
                self.halt.clear()
            self.event("trading RESUMED by user")
            return {"ok": True}

    def reset_risk_baseline(self) -> Dict[str, Any]:
        """Explicit user reset: rebase limits at verified current equity; never edit exchange positions/orders."""
        with self.lock:
            try:
                wallet = self.ex.get_wallet()
                if not wallet or float(wallet.get("equity", 0)) <= 0:
                    return {"ok": False, "error": "current account equity could not be verified"}
                report = self.do_reconcile()
                if not report.certain:
                    return {"ok": False, "error": "exchange positions/orders could not be verified; halt retained"}
                equity = float(wallet["equity"])
                from .risk.limits import LimitTracker, DailyStats
                self.limits = LimitTracker(dict(self.cfg.risk), equity, peak_equity=equity)
                self.executor.limits = self.limits
                self.wallet = wallet
                self.db.kv_set("peak_equity", equity)
                self.db.save_daily_stats(self.limits.stats.day, self.limits.stats.to_dict())
                self._alerted.clear()
                self.db.kv_set("risk_alerted_keys", [])
                self.halt.clear()
                self.event(f"risk baseline reset by user at {equity:.2f} USDT; exchange positions/orders left untouched")
                return {"ok": True, "equity": equity, "positions_modified": False, "orders_modified": False}
            except Exception as exc:
                return {"ok": False, "error": f"risk reset failed: {type(exc).__name__}"}

    def emergency(self, reason: str = "emergency close requested") -> Dict[str, Any]:
        with self.lock:
            rep = emergency_close(self.ex, self.symbol, self.db, self.halt, self.alerts, self.sleep, reason)
            self.event("EMERGENCY CLOSE: " + ("OK" if rep["ok"] else "FAILED - check Bybit manually"))
            return rep

    def test_ai(self) -> Dict[str, Any]:
        if self.decider is None:
            return {"ok": False, "error": "no AI model available"}
        snap = self.last_snapshot or {"note": "no snapshot yet"}
        res = self.decider.decide(snap, "", ["manual test from dashboard - do not trade"])
        return {"ok": res.ok, "error": res.error, "latency_s": round(res.latency_s, 1),
                "decision": res.decision.to_dict() if res.decision else None}

    def refresh_connection(self) -> Dict[str, Any]:
        with self.lock:
            r = self.do_reconcile()
            ok = False
            try:
                self.market.get_ticker(self.symbol)
                ok = True
                self.health.api_success()
            except ExchangeError as e:
                self.health.api_failure(str(e))
            ollama = None
            if self.decider is not None:
                ollama = self.decider.client.is_up()
                self.health.set_ollama(bool(ollama))
            return {"api": ok, "reconciled": r.certain, "ollama": ollama}

    def _plan_view(self) -> Optional[Dict[str, Any]]:
        """Resting limit, if any, for the wall chart and the open-trade tile."""
        pending = self.executor.pending
        if not pending:
            return None
        params = pending.get("params") or {}
        ctx = pending.get("ctx") or {}
        placed = float(pending.get("placed") or 0)
        expires = pending.get("expires_at")
        if expires is None:
            expires = placed + self.executor.limit_ttl
        minutes_left = max(0.0, (float(expires) - self.clock()) / 60.0)
        abandon = pending.get("abandon_price")
        return {
            "kind": "plan" if abandon is not None else "limit",
            "direction": params.get("direction") or ctx.get("direction"),
            "entry": params.get("entry_price"),
            "stop": params.get("stop_loss"),
            "target": params.get("take_profit"),
            "abandon": abandon,
            "minutes_left": round(minutes_left, 1),
            "tp_management": ctx.get("tp_management") or "",
            "risk_scale": ctx.get("risk_scale", 1.0),
        }

    # ------------------------------------------------------------------ state for dashboard / status.sh
    def _publish(self) -> None:
        try:
            self.db.kv_set("state_summary", {"ts": self.clock(), "mode": self.cfg.mode})
        except Exception:
            pass

    def get_state(self) -> Dict[str, Any]:
        with self.lock:
            snap = self.last_snapshot
            try:
                positions = self.ex.get_positions(self.symbol)
            except ExchangeError:
                positions = []
            pos = None
            if positions:
                p = positions[0]
                tr = next(iter(self.db.open_trades(self.symbol)), None)
                t = self.db.get_trade(tr["id"]) if tr else None
                risk_dist = (t or {}).get("context", {}).get("initial_risk_dist") or 0
                d = 1 if p["side"] == "Buy" else -1
                r_mult = ((p["markPrice"] - p["avgPrice"]) * d / risk_dist) if risk_dist else None
                pos = dict(p, r_multiple=r_mult)
            perm = execution_permission(self.cfg)
            lim = self.limits.snapshot()
            r = self.cfg.risk
            return {
                "time": self.clock(), "uptime_s": round(self.clock() - self.started_at),
                "mode": self.cfg.mode, "display_mode": self.cfg.display_mode, "symbol": self.symbol,
                "display_symbol": (self.scanner_selected_symbol if self.scanner_selected_symbol else
                                   ("WAITING — no eligible pair" if callable(getattr(self.market, "get_all_tickers", None)) and self.last_universe_scan > 0 else self.symbol)),
                "system": {"online": True, "ollama": self.health.ollama_ok, "ollama_error": self.health.ollama_error,
                           "model": self.model_name, "bybit": ("ok" if self.health.api_last_ok else "connecting") if self.health.api_healthy else "unhealthy",
                           "bybit_env": getattr(self.ex, "environment", "PAPER (simulated)"),
                           "websocket": (self.streams.state() if self.streams else ("n/a (paper)" if self.paper else "off")),
                           "halted": self.halt.to_dict(), "permission": {"destination": perm.destination,
                                                                        "can_place_orders": perm.can_place_orders,
                                                                        "blockers": perm.blockers},
                           "live_gate": live_gate_report(self.cfg), "api_permissions": self.permissions,
                           "health": self.health.snapshot(), "last_error": self.last_error},
                "market": {"price": self.last_price, "trend_1h": snap.get("trend_1h"), "structure_1h": snap.get("structure_1h"),
                           "trend_5m": snap.get("trend_5m"), "atr_5m": snap.get("atr_5m"), "volume": snap.get("volume_state"),
                           "funding_rate": self.last_ticker.get("funding_rate"), "open_interest": self.last_ticker.get("open_interest"),
                           "fvg": (snap.get("fvg_5m") or [])[:2], "order_block": (snap.get("order_block_5m") or [])[:2],
                           "liquidity": snap.get("liquidity_5m"), "sweep": snap.get("liquidity_sweep"),
                           "setup": self.last_setup, "bid": self.last_ticker.get("bid"), "ask": self.last_ticker.get("ask"),
                           "candles": self.last_candles},
                "ai": self.last_decision,
                "multi_agent": self.last_agent_report,
                "scanner": {"last_scan": self.last_universe_scan, "selected_symbol": self.scanner_selected_symbol,
                            "selection_required": callable(getattr(self.market, "get_all_tickers", None)),
                            "note": self.universe_scan_note, "candidates": self.universe_candidates},
                "intelligence": self.intelligence.snapshot(self.symbol),
                "position": pos, "pending_entry": bool(self.executor.pending),
                "plan": self._plan_view(),
                "risk": {"daily_trades": lim["trades"], "max_daily_trades": r["max_daily_trades"], "daily_pnl": lim["realized_pnl"],
                         "unrealized_pnl": lim["unrealized_pnl"], "daily_loss_pct": round(lim["daily_loss_pct"], 3),
                         "max_daily_loss_pct": r["max_daily_loss_percent"], "drawdown_pct": round(lim["drawdown_pct"], 3),
                         "max_drawdown_pct": r["max_account_drawdown_percent"], "risk_per_trade_pct": r["risk_per_trade_percent"],
                         "equity": self.wallet["equity"], "available": self.wallet["available"], "wins": lim["wins"],
                         "losses": lim["losses"], "win_rate": lim["win_rate"], "average_r": lim["average_r"],
                         "consecutive_losses": lim["consecutive_losses"], "fees": lim["fees"], "halted_by": lim["halted"]},
                "events": list(reversed(self.recent_events[-15:])),
                "recent_trades": self.db.closed_trades(10),
                "recent_decisions": self.db.recent("ai_decisions", 10),
            }

    # ------------------------------------------------------------------ run loop
    def run(self, max_loops: Optional[int] = None) -> None:
        interval = float(self.cfg.section("execution")["loop_interval_seconds"])
        backoff = 1.0
        n = 0
        while not self.stop_event.is_set():
            try:
                self.step()
                backoff = 1.0
            except Exception as e:  # the loop must survive anything; uncertainty => no new trades
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("loop error")
                self.db.record_error("engine", self.last_error)
                self.health.position_certain = False
                self.alert("system_error", self.last_error, dedupe=600)
                self.stop_event.wait(min(backoff, 60))
                backoff = min(backoff * 2, 60)
                self.sleep(0)
            n += 1
            if max_loops and n >= max_loops:
                break
            self.stop_event.wait(interval)

    def shutdown(self) -> None:
        if getattr(self, "_shut", False):
            return
        self._shut = True
        self.stop_event.set()
        self.alert("bot_stopped", "bot stopped (open positions keep their exchange-side stop loss/take profit)")
        if self.streams:
            self.streams.stop()
