"""Configuration loading, validation and the trading-mode safety gate.

Settings come from config/config.yaml, with a few overrides from environment
variables (.env). Secrets are only ever read from the environment.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VALID_MODES = ("PAPER", "TESTNET", "LIVE")


class ConfigError(Exception):
    pass


DEFAULTS: Dict[str, Any] = {
    "mode": "PAPER",
    "symbol": "BTCUSDT",
    "category": "linear",
    "timeframes": {"structure": "60", "entry": "5", "confirm": "15", "candles_to_load": 300},
    "scanner": {"max_candidates_to_evaluate": 0},
    "multi_agent": {"enabled": True, "max_workers": 6},
    "risk": {
        "risk_per_trade_percent": 0.5, "max_daily_loss_percent": 2.0,
        "max_account_drawdown_percent": 10.0, "max_daily_trades": 10,
        "max_open_positions": 1, "min_risk_reward": 2.0, "max_leverage": 5.0,
        "max_consecutive_losses": 4, "max_spread_percent": 0.05,
        "max_data_age_seconds": 90, "min_stop_atr_mult": 0.8, "max_stop_atr_mult": 4.0,
        "max_entry_deviation_atr": 1.5, "abnormal_atr_ratio": 3.0,
        "max_position_notional_usdt": 0,
        "plan_max_distance_atr": 6.0,
        "plan_default_valid_minutes": 60,
    },
    "strategy": {
        "atr_period": 14, "swing_lookback": 3, "fvg_min_atr_mult": 0.3,
        "fvg_max_age_candles": 60, "ob_displacement_atr_mult": 1.2,
        "ob_max_age_candles": 80, "equal_level_tolerance_atr": 0.15,
        "proximity_atr": 1.5, "volume_avg_period": 20,
        "plan_max_atr": 6.0, "plan_fresh_fill_pct": 0.2,
        "plan_ask_cooldown_seconds": 900,
    },
    "tp": {"mode": "fixed_rr", "rr": 2.0, "partial_percent": 50, "partial_at_rr": 1.0},
    "trailing": {"enabled": True, "atr_multiplier": 0.9, "activate_at_rr": 1.0},
    "ai": {
        "provider": "ollama", "host": "http://127.0.0.1:11434", "model": "auto",
        "temperature": 0.1, "context_length": 8192, "timeout_seconds": 90,
        "max_retries": 1, "thinking": False, "use_tools": False, "min_confidence": 70, "strong_confidence": 80,
    },
    "execution": {
        "analysis_interval_seconds": 300, "evaluate_on_5m_close": True,
        "order_timeout_seconds": 20, "sl_confirm_timeout_seconds": 10,
        "slippage_percent_paper": 0.02, "taker_fee_percent": 0.055,
        "maker_fee_percent": 0.02,
        "limit_order_ttl_seconds": 300,
        "loop_interval_seconds": 5,
        "require_websocket": True,
    },
    "paper": {"starting_equity": 10000.0},
    "launchd": {"allow_live_autostart": False},
    "alerts": {"telegram_enabled": False},
    "dashboard": {"host": "127.0.0.1", "port": 8080, "display_timezone": "Europe/London"},
    "logging": {"directory": "logs", "level": "INFO", "max_bytes": 5242880, "backup_count": 5},
    "database": {"path": "data/trader.db"},
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    raw: Dict[str, Any]
    trading_enabled: bool = False
    autonomous_mode: bool = False
    bybit_testnet_flag: bool = True
    root: Path = PROJECT_ROOT

    # convenient accessors -------------------------------------------------
    @property
    def mode(self) -> str:
        return self.raw["mode"]

    @property
    def symbol(self) -> str:
        return self.raw["symbol"]

    def section(self, name: str) -> Dict[str, Any]:
        return self.raw[name]

    @property
    def risk(self) -> Dict[str, Any]:
        return self.raw["risk"]

    @property
    def strategy(self) -> Dict[str, Any]:
        return self.raw["strategy"]

    @property
    def ai(self) -> Dict[str, Any]:
        return self.raw["ai"]

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    # credentials (never logged, never put in prompts) -----------------------
    def credentials(self) -> Optional[tuple]:
        """Return (key, secret) for the active environment, or None.

        TESTNET uses BYBIT_TESTNET_* first, then BYBIT_API_*.
        LIVE uses ONLY BYBIT_API_* so testnet keys can never reach live.
        PAPER never needs credentials (public market data only).
        """
        if self.mode == "PAPER":
            return None
        if self.mode == "TESTNET":
            k = os.environ.get("BYBIT_TESTNET_API_KEY") or os.environ.get("BYBIT_API_KEY")
            s = os.environ.get("BYBIT_TESTNET_API_SECRET") or os.environ.get("BYBIT_API_SECRET")
        else:
            k = os.environ.get("BYBIT_API_KEY")
            s = os.environ.get("BYBIT_API_SECRET")
        return (k, s) if k and s else None

    @property
    def use_testnet_endpoint(self) -> bool:
        """True only when mode is TESTNET. PAPER reads public LIVE market data."""
        return self.mode == "TESTNET"

    @property
    def display_mode(self) -> str:
        return {"PAPER": "PAPER MODE", "TESTNET": "TESTNET MODE", "LIVE": "LIVE MODE"}[self.mode]


def validate(cfg: Config) -> List[str]:
    """Return a list of problems (empty list = valid)."""
    errs: List[str] = []
    r = cfg.risk
    if cfg.mode not in VALID_MODES:
        errs.append(f"mode must be one of {VALID_MODES}, got {cfg.mode!r}")
    if cfg.raw.get("category") != "linear":
        errs.append("category must be 'linear' (USDT perpetuals)")
    if not str(cfg.symbol).endswith("USDT"):
        errs.append("symbol must be a USDT perpetual such as BTCUSDT")
    checks = [
        ("risk_per_trade_percent", 0 < r["risk_per_trade_percent"] <= 5),
        ("max_daily_loss_percent", 0 < r["max_daily_loss_percent"] <= 50),
        ("max_account_drawdown_percent", 0 < r["max_account_drawdown_percent"] <= 90),
        ("max_daily_trades", r["max_daily_trades"] >= 1),
        ("max_open_positions", r["max_open_positions"] >= 1),
        ("min_risk_reward", r["min_risk_reward"] >= 1.0),
        ("max_leverage", 1 <= r["max_leverage"] <= 100),
        ("min_stop_atr_mult", r["min_stop_atr_mult"] > 0),
        ("max_stop_atr_mult", r["max_stop_atr_mult"] > r["min_stop_atr_mult"]),
        ("plan_max_distance_atr", 0 < float(r.get("plan_max_distance_atr", 6)) <= 20),
        ("plan_default_valid_minutes", 5 <= float(r.get("plan_default_valid_minutes", 60)) <= 240),
    ]
    for name, ok in checks:
        if not ok:
            errs.append(f"risk.{name} has an invalid value: {r[name]!r}")
    ai = cfg.ai
    if ai["provider"] != "ollama":
        errs.append("ai.provider must be 'ollama' (local). Cloud LLMs are not supported.")
    host = str(ai["host"])
    if not (host.startswith("http://127.0.0.1") or host.startswith("http://localhost")):
        errs.append("ai.host must be a local Ollama address (127.0.0.1 / localhost)")
    if not (0 <= ai["min_confidence"] <= 100 and ai["min_confidence"] <= ai["strong_confidence"] <= 100):
        errs.append("ai.min_confidence must be <= ai.strong_confidence, both within 0-100")
    strat = cfg.strategy
    if not (1.0 <= float(strat.get("plan_max_atr", 6)) <= 20):
        errs.append(f"strategy.plan_max_atr has an invalid value: {strat.get('plan_max_atr')!r}")
    cool = float(strat.get("plan_ask_cooldown_seconds", 900))
    if not (60 <= cool <= 24 * 3600):
        errs.append(f"strategy.plan_ask_cooldown_seconds has an invalid value: {cool!r}")
    # Environment-flag consistency: never mix testnet / live endpoints.
    if cfg.mode == "TESTNET" and not cfg.bybit_testnet_flag:
        errs.append("mode TESTNET requires BYBIT_TESTNET=true in .env")
    if cfg.mode == "LIVE" and cfg.bybit_testnet_flag:
        errs.append("mode LIVE requires BYBIT_TESTNET=false in .env")
    if cfg.mode == "PAPER" and cfg.autonomous_mode and cfg.trading_enabled:
        pass  # allowed: paper trades are simulated only
    return errs


def load_config(path: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> Config:
    """Load config.yaml + environment. Raises ConfigError on any problem."""
    if env is not None:
        os.environ.update(env)
    try:
        from dotenv import load_dotenv  # optional dependency
        load_dotenv(PROJECT_ROOT / ".env", override=False)
    except Exception:
        pass
    cfg_path = Path(path) if path else PROJECT_ROOT / "config" / "config.yaml"
    user: dict = {}
    if cfg_path.exists():
        try:
            import yaml
            user = yaml.safe_load(cfg_path.read_text()) or {}
            if not isinstance(user, dict):
                raise ValueError("top level of config.yaml must be a mapping")
        except Exception as e:  # corrupted configuration
            raise ConfigError(f"Cannot read {cfg_path}: {e}") from e
    raw = _deep_merge(DEFAULTS, user)
    if os.environ.get("OLLAMA_MODEL"):
        raw["ai"]["model"] = os.environ["OLLAMA_MODEL"].strip()
    if "mode" in raw:
        raw["mode"] = str(raw["mode"]).upper()
    cfg = Config(
        raw=raw,
        trading_enabled=_env_bool("TRADING_ENABLED", False),
        autonomous_mode=_env_bool("AUTONOMOUS_MODE", False),
        bybit_testnet_flag=_env_bool("BYBIT_TESTNET", True),
    )
    errs = validate(cfg)
    if errs:
        raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(errs))
    return cfg


@dataclass
class ExecutionPermission:
    """What the system is allowed to do with orders right now."""
    destination: str               # "NONE" | "PAPER" | "TESTNET" | "LIVE"
    can_place_orders: bool
    can_trade_autonomously: bool
    blockers: List[str] = field(default_factory=list)


def execution_permission(cfg: Config) -> ExecutionPermission:
    """Decide where orders may go. LIVE needs mode=LIVE + TRADING_ENABLED + AUTONOMOUS_MODE."""
    blockers: List[str] = []
    if cfg.mode == "PAPER":
        return ExecutionPermission(
            "PAPER", True, cfg.autonomous_mode,
            ["Mode is PAPER: orders are simulated and never sent to Bybit."],
        )
    if cfg.credentials() is None:
        blockers.append("Bybit API credentials for this mode are missing in .env.")
    if not cfg.trading_enabled:
        blockers.append("TRADING_ENABLED=false in .env.")
    if not cfg.autonomous_mode:
        blockers.append("AUTONOMOUS_MODE=false in .env (no automatic trading).")
    if cfg.mode == "LIVE" and cfg.bybit_testnet_flag:
        blockers.append("BYBIT_TESTNET=true conflicts with mode LIVE.")
    ok = not blockers
    return ExecutionPermission(cfg.mode if ok else "NONE", ok, ok, blockers)


def live_gate_report(cfg: Config) -> List[str]:
    """Human-readable explanation of exactly what is preventing LIVE trading."""
    lines = []
    lines.append(("[OK]   " if cfg.mode == "LIVE" else "[BLOCK] ") + f"mode is {cfg.mode} (needs LIVE)")
    lines.append(("[OK]   " if cfg.trading_enabled else "[BLOCK] ") + f"TRADING_ENABLED={str(cfg.trading_enabled).lower()} (needs true)")
    lines.append(("[OK]   " if cfg.autonomous_mode else "[BLOCK] ") + f"AUTONOMOUS_MODE={str(cfg.autonomous_mode).lower()} (needs true)")
    lines.append(("[OK]   " if not cfg.bybit_testnet_flag else "[BLOCK] ") + f"BYBIT_TESTNET={str(cfg.bybit_testnet_flag).lower()} (needs false for LIVE)")
    live_creds = bool(os.environ.get("BYBIT_API_KEY") and os.environ.get("BYBIT_API_SECRET"))
    lines.append(("[OK]   " if live_creds else "[BLOCK] ") + "live API credentials present in .env")
    return lines
