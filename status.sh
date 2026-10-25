#!/bin/bash
# status.sh - is the bot running, in which mode, and what is preventing live trading?
cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"; PYV=".venv/bin/python"
[ -x "$PYV" ] || { echo "Not installed. Run ./setup.sh"; exit 1; }
if [ -f data/bot.pid ] && kill -0 "$(cat data/bot.pid)" 2>/dev/null; then echo "Bot:        RUNNING (PID $(cat data/bot.pid))"; else echo "Bot:        NOT RUNNING"; fi
PYTHONPATH="$ROOT" "$PYV" - <<'PY'
import json, os, time
from bot.config import load_config, execution_permission, live_gate_report, ConfigError
try:
    c = load_config()
except ConfigError as e:
    print("Config:    INVALID\n", e); raise SystemExit(1)
print(f"Mode:       {c.display_mode}")
print(f"Dashboard:  http://{c.raw['dashboard']['host']}:{c.raw['dashboard']['port']}")
p = execution_permission(c)
if c.mode == "PAPER":
    print("Orders:     simulated only - nothing is ever sent to Bybit")
else:
    print(f"Orders:     {'WILL be placed -> ' + p.destination if p.can_place_orders else 'NOT placed'}")
    for b in p.blockers: print("            - " + b)
print("Live gate:")
for l in live_gate_report(c): print("            " + l)
f = c.path("data/state.json")
if f.exists():
    s = json.loads(f.read_text()); age = time.time() - s["time"]
    r, sy = s["risk"], s["system"]
    print(f"State:      updated {age:.0f}s ago | uptime {s['uptime_s']}s | price {s['market']['price']}")
    print(f"Trading:    {'HALTED: ' + sy['halted']['reason'] if sy['halted']['halted'] else ('PAUSED' if sy['halted']['paused'] else 'active')}")
    print(f"Account:    equity {r['equity']:.2f} | daily P/L {r['daily_pnl']:+.2f} | trades today {r['daily_trades']}/{r['max_daily_trades']} | drawdown {r['drawdown_pct']}%")
    print(f"Position:   {s['position'] and (s['position']['side'] + ' ' + str(s['position']['size']) + ' @ ' + str(s['position']['avgPrice'])) or 'none'}")
    print(f"Ollama:     {sy['ollama']} | model {sy['model']} | websocket {sy['websocket']}")
PY
