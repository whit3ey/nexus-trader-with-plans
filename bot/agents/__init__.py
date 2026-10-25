"""Lightweight, local multi-agent review layer for the trading engine.

Agents are deterministic specialist reviewers. They do not place orders and cannot
approve a trade; the existing RiskEngine remains the final authority.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List
import time


def _market_agent(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    trend1 = str(snapshot.get("trend_1h") or "unknown").lower()
    trend5 = str(snapshot.get("trend_5m") or "unknown").lower()
    trend15 = str(snapshot.get("trend_15m") or "unknown").lower()
    aligned = trend1 in ("bullish", "bearish") and trend5 == trend1
    if trend15 not in ("unknown", "none", ""):
        aligned = aligned and trend15 == trend1
    state = "aligned" if aligned else "mixed / unclear"
    return {"name": "Market regime", "status": "complete", "state": state,
            "detail": f"1H {trend1}; 15M {trend15}; 5M {trend5}", "directional_alignment": aligned}


def _technical_agent(snapshot: Dict[str, Any], setup: Any) -> Dict[str, Any]:
    hint = getattr(setup, "direction_hint", None)
    has_setup = bool(getattr(setup, "has_setup", False))
    reasons = list(getattr(setup, "reasons", []) or [])
    sweep = snapshot.get("liquidity_sweep") or {}
    sweep_hint = sweep.get("direction_hint") if isinstance(sweep, dict) else None
    confirms = bool(hint and sweep_hint == hint)
    return {"name": "Technical review", "status": "complete", "state": hint if has_setup else "WAIT",
            "detail": "; ".join(reasons[:3]) or "No confirmed setup", "setup_confirmed": has_setup,
            "liquidity_sweep_confirms": confirms}


def _intelligence_agent(intelligence: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(intelligence, dict) or not intelligence:
        return {"name": "News & data", "status": "limited", "state": "No context",
                "detail": "External context unavailable; technical rules remain primary."}
    status = str(intelligence.get("status") or "available")
    analysis = intelligence.get("news_analysis") or {}
    count = analysis.get("headline_count", 0)
    score = analysis.get("heuristic_sentiment_score")
    detail = f"News headlines: {count}; heuristic sentiment: {score if score is not None else 'unavailable'}. Treat as context only."
    return {"name": "News & data", "status": "complete" if status.lower() not in ("error", "unavailable") else "limited",
            "state": status, "detail": detail}


def _risk_advisor(snapshot: Dict[str, Any], setup: Any, config: Dict[str, Any]) -> Dict[str, Any]:
    atr = float(snapshot.get("atr_5m") or 0)
    price = float(snapshot.get("price") or 0)
    atr_pct = (atr / price * 100) if price > 0 else None
    vol_ratio = snapshot.get("volatility_ratio_5m")
    flags: List[str] = []
    if atr <= 0 or price <= 0:
        flags.append("price/ATR data missing")
    if vol_ratio is not None and float(vol_ratio or 0) > float(config.get("abnormal_atr_ratio", 3.0)):
        flags.append("abnormal volatility ratio")
    if not getattr(setup, "has_setup", False):
        flags.append("no deterministic setup")
    if not ((getattr(setup, "direction_hint", None) == "LONG" and snapshot.get("trend_1h") == "bullish") or
            (getattr(setup, "direction_hint", None) == "SHORT" and snapshot.get("trend_1h") == "bearish")):
        flags.append("setup does not align with 1H trend")
    return {"name": "Risk pre-check", "status": "review" if flags else "clear",
            "state": "Caution" if flags else "No advisory flags", "detail": "; ".join(flags) if flags else "Basic data and trend checks passed; final risk engine still validates every order.",
            "atr_percent": round(atr_pct, 4) if atr_pct is not None else None, "flags": flags}


def _scanner_agent(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    symbol = str(snapshot.get("symbol") or "unselected")
    price = snapshot.get("price")
    missing = []
    if not snapshot.get("symbol"): missing.append("symbol not selected")
    if price is None or float(price or 0) <= 0: missing.append("valid price missing")
    state = "Candidate context available" if not missing else "Waiting / incomplete"
    return {"name": "Scanner", "codename": "BRAM", "role": "Pair selection and candidate context", "status": "complete" if not missing else "limited", "state": state,
            "detail": f"Active candidate {symbol}; price {price if price is not None else 'unavailable'}. " + ("; ".join(missing) if missing else "Candidate handed to downstream reviewers."), "flags": missing}


def _execution_agent(snapshot: Dict[str, Any], setup: Any) -> Dict[str, Any]:
    required = {"symbol": snapshot.get("symbol"), "price": snapshot.get("price"), "atr_5m": snapshot.get("atr_5m")}
    missing = [key for key, value in required.items() if value is None or value == "" or (key in ("price", "atr_5m") and float(value or 0) <= 0)]
    has_setup = bool(getattr(setup, "has_setup", False))
    if not has_setup: missing.append("no deterministic setup")
    return {"name": "Execution check", "codename": "TESS", "role": "Pre-flight data completeness; no order placement", "status": "review" if missing else "clear",
            "state": "HOLD" if missing else "Pre-flight data present", "detail": "; ".join(missing) if missing else "Required symbol, price and ATR fields are present. Exchange/order checks and final risk gate still apply.", "flags": missing}


class MultiAgentCoordinator:
    """Run independent specialist reviews concurrently; fail closed to an advisory report."""
    def __init__(self, enabled: bool = True, max_workers: int = 6):
        self.enabled = enabled
        self.max_workers = max(1, min(6, int(max_workers)))
        self.last_report: Dict[str, Any] = {"enabled": enabled, "status": "waiting", "agents": [], "summary": "Waiting for a setup."}

    def review(self, snapshot: Dict[str, Any], setup: Any, intelligence: Dict[str, Any], risk_config: Dict[str, Any]) -> Dict[str, Any]:
        started = time.time()
        if not self.enabled:
            self.last_report = {"enabled": False, "status": "disabled", "agents": [], "summary": "Multi-agent review disabled."}
            return self.last_report
        jobs = {
            "scanner": lambda: _scanner_agent(snapshot),
            "intelligence": lambda: _intelligence_agent(intelligence),
            "technical": lambda: _technical_agent(snapshot, setup),
            "risk": lambda: _risk_advisor(snapshot, setup, risk_config),
            "market": lambda: _market_agent(snapshot),
            "execution": lambda: _execution_agent(snapshot, setup),
        }
        results: Dict[str, Dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="nexus-agent") as pool:
            futures = {pool.submit(fn): key for key, fn in jobs.items()}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    results[key] = future.result()
                except Exception as exc:
                    results[key] = {"name": key.title(), "status": "error", "state": "Unavailable",
                                    "detail": f"Agent error: {type(exc).__name__}; advisory review incomplete."}
        order = ("scanner", "intelligence", "technical", "risk", "market", "execution")
        codenames = {"scanner": "BRAM", "intelligence": "ILSA", "technical": "HOLT", "risk": "KETT", "market": "RIGO", "execution": "TESS"}
        roles = {"scanner": "Pair selection and candidate context", "intelligence": "News and external context", "technical": "Structure, trend and setup checks", "risk": "Risk flags and volatility review", "market": "Multi-timeframe market regime", "execution": "Pre-flight data completeness; no order placement"}
        for key in order:
            results[key].setdefault("codename", codenames[key])
            results[key].setdefault("role", roles[key])
        agents = [results[k] for k in order]
        failed = any(a.get("status") == "error" for a in agents)
        warnings = sum(1 for a in agents if a.get("status") in ("limited", "review", "error"))
        summary = ("One or more specialist reviews are incomplete; interpret AI output cautiously." if failed else
                   f"6 specialist reviews completed in {time.time() - started:.2f}s; {warnings} review/context warning(s). Advisory only; deterministic risk engine remains authoritative.")
        report = {"enabled": True, "status": "partial" if failed else "complete", "agents": agents,
                  "summary": summary, "duration_s": round(time.time() - started, 3),
                  "decision_authority": "deterministic risk engine"}
        self.last_report = report
        return report
