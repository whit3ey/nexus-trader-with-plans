"""Asks the local model for a structured decision. The model can only READ data through
whitelisted tools and can only RETURN a JSON decision; it never touches the exchange."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..logging_config import get_logger
from .ollama_client import OllamaClient, OllamaError, OllamaUnavailable
from .prompts import CORRECTION_TEMPLATE, SYSTEM_PROMPT, build_user_prompt
from .schemas import AIDecision, DECISION_JSON_SCHEMA, DecisionError, parse_decision

log = get_logger("bot.ai")

MAX_TOOL_ROUNDS = 3

# Read-only tools. No tool can place, modify or cancel an order or change settings.
TOOL_DESCRIPTIONS: Dict[str, str] = {
    "get_market_snapshot": "Latest deterministic market snapshot (prices, structure, FVG, order blocks, liquidity, ATR).",
    "get_current_position": "The current open position, or null.",
    "get_open_orders": "Currently open orders.",
    "get_account_summary": "Equity, available balance and today's risk usage (read-only).",
    "get_strategy_analysis": "Which deterministic strategy conditions are currently met.",
}


def tool_definitions() -> List[dict]:
    return [{"type": "function", "function": {"name": n, "description": d,
                                              "parameters": {"type": "object", "properties": {}}}}
            for n, d in TOOL_DESCRIPTIONS.items()]


TOOL_SPECS: List[dict] = tool_definitions()
ALLOWED_TOOLS = frozenset(TOOL_DESCRIPTIONS)


@dataclass
class DecisionResult:
    ok: bool
    decision: Optional[AIDecision] = None
    error: str = ""
    raw: str = ""
    attempts: int = 0
    latency_s: float = 0.0
    tool_calls: List[str] = field(default_factory=list)
    model: str = ""


_THINK = re.compile(r"<think>.*?</think>", re.S)


def clean_content(text: str) -> str:
    return _THINK.sub("", text or "").strip()


class DecisionEngine:
    def __init__(self, client: OllamaClient, model: str, ai_cfg: Dict[str, Any],
                 tools: Optional[Dict[str, Callable[[], Any]]] = None):
        self.client, self.model, self.cfg = client, model, ai_cfg
        self.tools = {k: v for k, v in (tools or {}).items() if k in ALLOWED_TOOLS}  # whitelist

    def _call_tool(self, name: str) -> str:
        fn = self.tools.get(name)
        if fn is None:
            return json.dumps({"error": f"tool {name!r} is not available"})
        try:
            return json.dumps(fn(), default=str)[:6000]
        except Exception as e:  # tool failure must never crash the loop
            return json.dumps({"error": str(e)})

    def _chat(self, messages: List[dict], result: DecisionResult) -> str:
        use_tools = bool(self.tools)
        for _ in range(MAX_TOOL_ROUNDS + 1):
            msg = self.client.chat(
                self.model, messages, schema=None if use_tools else DECISION_JSON_SCHEMA,
                tools=tool_definitions() if use_tools else None,
                temperature=float(self.cfg["temperature"]), num_ctx=int(self.cfg["context_length"]),
                think=bool(self.cfg.get("thinking", False)), timeout=float(self.cfg["timeout_seconds"]))
            calls = msg.get("tool_calls") or []
            if not calls or len(result.tool_calls) >= MAX_TOOL_ROUNDS * 2:
                content = clean_content(msg.get("content", ""))
                if use_tools and not content:
                    use_tools = False       # force the final structured answer
                    continue
                return content
            messages.append({"role": "assistant", "content": msg.get("content", ""), "tool_calls": calls})
            for c in calls:
                name = c.get("function", {}).get("name", "")
                result.tool_calls.append(name)
                messages.append({"role": "tool", "tool_name": name, "content": self._call_tool(name)})
            use_tools = len(result.tool_calls) < MAX_TOOL_ROUNDS * 2
            if not use_tools:
                messages.append({"role": "user", "content": "Now return only the JSON decision."})
        return ""

    def decide(self, snapshot: Dict[str, Any], hint: str = "", reasons: Optional[List[str]] = None) -> DecisionResult:
        t0 = time.time()
        res = DecisionResult(False, model=self.model)
        messages: List[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(snapshot, hint, reasons)},
        ]
        max_attempts = 1 + int(self.cfg.get("max_retries", 1))   # "retry once" by default
        for attempt in range(1, max_attempts + 1):
            res.attempts = attempt
            try:
                raw = self._chat(messages, res)
            except OllamaError as e:
                res.error = (f"Ollama unavailable: {e}" if isinstance(e, OllamaUnavailable) else f"Ollama failure: {e}")
                log.error(res.error)
                break
            res.raw = raw
            try:
                res.decision = parse_decision(raw)
                res.ok, res.error = True, ""
                break
            except DecisionError as e:
                res.error = f"invalid AI decision: {e}"
                log.warning("attempt %d: %s | raw=%r", attempt, res.error, raw[:300])
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": CORRECTION_TEMPLATE.format(error=e)})
        res.latency_s = time.time() - t0
        log.info("AI decision ok=%s attempts=%d latency=%.1fs %s", res.ok, res.attempts, res.latency_s,
                 res.decision.decision if res.decision else res.error)
        return res
