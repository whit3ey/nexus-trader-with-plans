"""Entry point:  python -m bot.main   (normally started by ./start.sh)

Starts the trading engine and the local dashboard in one process, writes data/bot.pid and
data/state.json, and shuts down cleanly on SIGTERM/SIGINT. Open positions are NOT closed on
shutdown - they keep their exchange-side stop loss and take profit.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

from .config import ConfigError, PROJECT_ROOT, execution_permission, live_gate_report, load_config
from .logging_config import get_logger, setup_logging, shutdown_logging

BANNER = {"PAPER": "PAPER MODE   - simulated trades only, nothing is sent to Bybit",
          "TESTNET": "TESTNET MODE - orders go to the Bybit TESTNET (fake money)",
          "LIVE": "LIVE MODE    - REAL MONEY"}


def write_state_loop(engine, path: Path, stop: threading.Event) -> None:
    while not stop.wait(5):
        try:
            st = engine.get_state()
            slim = {k: st[k] for k in ("time", "mode", "display_mode", "symbol", "uptime_s")}
            slim.update(system=st["system"], risk=st["risk"], position=st["position"], ai=st["ai"], market={"price": st["market"]["price"]})
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(slim, default=str))
            tmp.replace(path)
        except Exception:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="autonomous_ai_trader")
    ap.add_argument("--config", default=None)
    ap.add_argument("--no-dashboard", action="store_true")
    ap.add_argument("--from-launchd", action="store_true", help="started automatically by macOS at login/boot")
    ap.add_argument("--skip-checks", action="store_true")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
    except ConfigError as e:
        print(f"CONFIGURATION ERROR\n{e}\n\nFix config/config.yaml or .env and start again.", file=sys.stderr)
        return 2
    lg = cfg.raw["logging"]
    setup_logging(cfg.path(lg["directory"]), lg["level"], int(lg["max_bytes"]), int(lg["backup_count"]), console=not a.from_launchd)
    log = get_logger("bot")
    print("=" * 72)
    print(f"  {BANNER[cfg.mode]}")
    print("=" * 72)
    perm = execution_permission(cfg)
    if cfg.mode != "PAPER":
        print("Live-trading gate:")
        for line in live_gate_report(cfg):
            print("  " + line)
        print(f"Orders will be placed: {'YES -> ' + perm.destination if perm.can_place_orders else 'NO'}")
        for b in perm.blockers:
            print("  - " + b)
    if a.from_launchd and cfg.mode == "LIVE" and not cfg.raw["launchd"]["allow_live_autostart"]:
        msg = ("Automatic start of LIVE trading is not allowed: set launchd.allow_live_autostart: true in "
               "config/config.yaml if you really want unattended live trading after a restart.")
        print(msg)
        log.warning(msg)
        return 0       # exit code 0 => launchd does not restart us in a loop
    if not a.skip_checks:
        from .diagnostics import format_report, run_checks
        checks = run_checks(cfg, quick=True, network=True)
        print(format_report(checks))
        fatal = [c for c in checks if c.status == "FAIL" and c.name in ("Python", "Configuration", "Database", "Dependencies")]
        if fatal:
            print("\nCannot start: " + ", ".join(c.name for c in fatal))
            return 3
    from .factory import build_engine
    from dashboard.app import DashboardServer
    engine = build_engine(cfg)
    stop = threading.Event()
    from .telegram_control import TelegramControl
    telegram = TelegramControl(engine, os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", ""))
    telegram.start()
    if telegram.enabled:
        log.info("Telegram command polling enabled for configured chat")

    def on_signal(signum, frame):
        log.info("signal %s received - shutting down", signum)
        engine.shutdown()
        telegram.stop()
        stop.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    pid_file = cfg.path("data/bot.pid")
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))
    dash = None
    if not a.no_dashboard:
        d = cfg.raw["dashboard"]
        try:
            dash = DashboardServer(engine, d["host"], int(d["port"])).start()
            print(f"\nDashboard: http://{d['host']}:{dash.port}")
        except OSError as e:
            print(f"Dashboard could not start on port {d['port']}: {e}. Is the bot already running? (./status.sh)")
            log.error("dashboard failed: %s", e)
    threading.Thread(target=write_state_loop, args=(engine, cfg.path("data/state.json"), stop), daemon=True).start()
    try:
        engine.startup()
        print(f"Selected AI model: {engine.model_name}   |   Mode: {cfg.display_mode}\nRunning. Press Ctrl+C or run ./stop.sh to stop.\n")
        engine.run()
    finally:
        engine.shutdown()
        telegram.stop()
        if dash:
            dash.stop()
        try:
            pid_file.unlink()
        except OSError:
            pass
        engine.db.close()
        log.info("bot stopped")
        shutdown_logging()
    return 0


if __name__ == "__main__":
    sys.exit(main())
