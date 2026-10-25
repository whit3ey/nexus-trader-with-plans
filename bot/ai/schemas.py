"""Strict validation of the AI decision. Anything malformed is rejected - never repaired."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

DECISIONS = ("LONG", "SHORT", "WAIT")
ENTRY_TYPES = ("MARKET", "LIMIT", "NONE")
REGIMES = ("TREND", "RANGE", "HIGH_VOLATILITY", "UNCLEAR")
TP_MANAGEMENTS = ("fixed", "partial", "trailing", "fixed_rr")
REQUIRED = ("decision", "confidence", "entry_type", "entry_price", "stop_loss", "take_profit",
            "risk_reward", "reason", "market_regime", "setup_type", "invalidations")


class DecisionError(ValueError):
    pass


@dataclass(frozen=True)
class AIDecision:
    decision: str
    confidence: float
    entry_type: str
    entry_price: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    risk_reward: Optional[float]
    reason: str
    market_regime: str
    setup_type: str
    invalidations: Tuple[str, ...] = field(default_factory=tuple)
    valid_minutes: Optional[float] = None       # how long a resting plan may wait (5-240)
    abandon_price: Optional[float] = None       # idea is dead if traded here before the fill
    tp_management: Optional[str] = None         # fixed | partial | trailing
    risk_scale: Optional[float] = None          # 0.25-1.0 of the configured risk, never more

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["invalidations"] = list(self.invalidations)
        return d


def _num(v: Any, name: str, allow_none: bool) -> Optional[float]:
    if v is None:
        if allow_none:
            return None
        raise DecisionError(f"{name} must be a number")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise DecisionError(f"{name} must be a number, got {type(v).__name__}")
    if not math.isfinite(v):
        raise DecisionError(f"{name} must be finite")
    return float(v)


def _optional_num(payload: Dict[str, Any], name: str) -> Optional[float]:
    if name not in payload or payload[name] is None:
        return None
    return _num(payload[name], name, False)


def parse_decision(payload: Any) -> AIDecision:
    """Accepts a JSON string or dict. Raises DecisionError on ANY problem."""
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as e:
            raise DecisionError(f"invalid JSON: {e}") from e
    if not isinstance(payload, dict):
        raise DecisionError("decision must be a JSON object")
    missing = [k for k in REQUIRED if k not in payload]
    if missing:
        raise DecisionError(f"missing required fields: {', '.join(missing)}")
    decision = payload["decision"]
    if decision not in DECISIONS:
        raise DecisionError(f"decision must be one of {DECISIONS}")
    entry_type = payload["entry_type"]
    if entry_type not in ENTRY_TYPES:
        raise DecisionError(f"entry_type must be one of {ENTRY_TYPES}")
    regime = payload["market_regime"]
    if regime not in REGIMES:
        raise DecisionError(f"market_regime must be one of {REGIMES}")
    conf = _num(payload["confidence"], "confidence", False)
    if not 0 <= conf <= 100:
        raise DecisionError("confidence must be within 0-100")
    trade = decision in ("LONG", "SHORT")
    entry = _num(payload["entry_price"], "entry_price", not trade or entry_type == "MARKET")
    sl = _num(payload["stop_loss"], "stop_loss", not trade)
    tp = _num(payload["take_profit"], "take_profit", not trade)
    rr = _num(payload["risk_reward"], "risk_reward", True)
    if trade:
        if entry_type == "NONE":
            raise DecisionError("a LONG/SHORT decision needs entry_type MARKET or LIMIT")
        if sl is None or tp is None:
            raise DecisionError("stop_loss and take_profit are required for a trade")
        if entry_type == "LIMIT" and (entry is None or entry <= 0):
            raise DecisionError("LIMIT entry needs a positive entry_price")
        if sl <= 0 or tp <= 0:
            raise DecisionError("prices must be positive")
    if not isinstance(payload["reason"], str) or not isinstance(payload["setup_type"], str):
        raise DecisionError("reason and setup_type must be strings")
    inv = payload["invalidations"]
    if not isinstance(inv, list) or not all(isinstance(x, str) for x in inv):
        raise DecisionError("invalidations must be a list of strings")
    valid_minutes = _optional_num(payload, "valid_minutes")
    abandon = _optional_num(payload, "abandon_price")
    risk_scale = _optional_num(payload, "risk_scale")
    raw_style = payload.get("tp_management", None)
    style: Optional[str]
    if raw_style is None:
        style = None
    elif not isinstance(raw_style, str) or raw_style.strip().lower() not in TP_MANAGEMENTS:
        raise DecisionError("tp_management must be fixed, partial or trailing")
    else:
        style = raw_style.strip().lower()
        if style == "fixed_rr":
            style = "fixed"
    return AIDecision(decision, conf, entry_type, entry, sl, tp, rr,
                      payload["reason"][:500], regime, payload["setup_type"][:100], tuple(inv[:10]),
                      valid_minutes, abandon, style, risk_scale)


NUM_OR_NULL = {"type": ["number", "null"]}
DECISION_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": list(DECISIONS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 100},
        "entry_type": {"type": "string", "enum": list(ENTRY_TYPES)},
        "entry_price": NUM_OR_NULL,
        "stop_loss": NUM_OR_NULL,
        "take_profit": NUM_OR_NULL,
        "risk_reward": NUM_OR_NULL,
        "reason": {"type": "string"},
        "market_regime": {"type": "string", "enum": list(REGIMES)},
        "setup_type": {"type": "string"},
        "invalidations": {"type": "array", "items": {"type": "string"}},
        "valid_minutes": NUM_OR_NULL,
        "abandon_price": NUM_OR_NULL,
        "tp_management": {"type": ["string", "null"], "enum": ["fixed", "partial", "trailing", None]},
        "risk_scale": NUM_OR_NULL,
    },
    "required": list(REQUIRED),
}
