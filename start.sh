#!/bin/bash
# start.sh - verify everything, then start the bot + dashboard in the background.
#   ./start.sh               background (recommended)
#   ./start.sh --foreground  run in this window (Ctrl+C to stop)
cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"
FG=0; [ "${1:-}" = "--foreground" ] && FG=1
PYV=".venv/bin/python"
if [ -t 1 ]; then G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; B=$'\033[1m'; N=$'\033[0m'; else G=""; R=""; Y=""; B=""; N=""; fi
fail() { echo "${R}${B}Cannot start:${N} $*"; exit 1; }

[ -x "$PYV" ] || fail "not installed yet. Run ./setup.sh first."
mkdir -p data logs

# already running?
if [ -f data/bot.pid ] && kill -0 "$(cat data/bot.pid)" 2>/dev/null; then
  echo "Already running (PID $(cat data/bot.pid)). Use ./status.sh or ./stop.sh"; exit 0
fi
rm -f data/bot.pid

# configuration valid?
INFO="$(PYTHONPATH="$ROOT" "$PYV" -c 'from bot.config import load_config; c=load_config(); print(c.display_mode+"|"+str(c.raw["dashboard"]["host"])+"|"+str(c.raw["dashboard"]["port"]))' 2>&1)" \
  || fail "configuration problem:
$INFO"
MODE="${INFO%%|*}"; REST="${INFO#*|}"; HOST="${REST%%|*}"; PORT="${REST##*|}"

# Ollama running + model present?
ollama_up() { curl -s -m 3 http://127.0.0.1:11434/api/version >/dev/null 2>&1; }
OLLAMA_BIN="$(command -v ollama 2>/dev/null || true)"
[ -z "$OLLAMA_BIN" ] && [ -x /Applications/Ollama.app/Contents/Resources/ollama ] && OLLAMA_BIN=/Applications/Ollama.app/Contents/Resources/ollama
if ! ollama_up; then
  if [ -n "$OLLAMA_BIN" ]; then
    echo "Ollama is not running - starting it ..."
    if [ -d /Applications/Ollama.app ]; then open -a Ollama; else nohup "$OLLAMA_BIN" serve > logs/ollama.log 2>&1 & fi
    for i in $(seq 1 30); do ollama_up && break; sleep 1; done
  fi
  ollama_up || echo "${Y}WARNING: Ollama is not reachable. The bot will run but cannot trade (no AI). Run ./setup.sh to install/start it.${N}"
fi
if ollama_up; then
  MODEL="$(PYTHONPATH="$ROOT" "$PYV" -c 'from bot.config import load_config; from bot.hardware import resolve_model; c=load_config(); print(resolve_model(c.ai["model"], c.root))')"
  if ! "$OLLAMA_BIN" list 2>/dev/null | awk -v m="$MODEL" 'NR>1 && ($1==m || $1==m":latest"){f=1} END{exit !f}'; then
    echo "${Y}WARNING: AI model '$MODEL' is not downloaded. Run: ollama pull $MODEL   (or ./setup.sh)${N}"
  fi
fi

echo
echo "${B}Mode: $MODE${N}"
case "$MODE" in
  "LIVE MODE") echo "${R}${B}*** LIVE MODE - REAL MONEY. Orders are only sent if TRADING_ENABLED=true and AUTONOMOUS_MODE=true in .env ***${N}" ;;
  "TESTNET MODE") echo "${Y}Testnet: fake money on Bybit's test exchange.${N}" ;;
  *) echo "Paper: simulated trades only." ;;
esac

RUNNER=("$PYV" -m bot.main)
command -v caffeinate >/dev/null 2>&1 && RUNNER=(caffeinate -i "$PYV" -m bot.main)   # keep the Mac awake while the bot runs

if [ "$FG" = 1 ]; then
  exec env PYTHONPATH="$ROOT" "${RUNNER[@]}"
fi
PYTHONPATH="$ROOT" nohup "${RUNNER[@]}" > logs/console.log 2>&1 &
echo "Starting ..."
for i in $(seq 1 40); do
  sleep 1
  if curl -s -m 2 "http://$HOST:$PORT/api/state" >/dev/null 2>&1; then
    echo "${G}${B}Running.${N}  Dashboard:  http://$HOST:$PORT"
    echo "Status: ./status.sh    Stop: ./stop.sh    Logs: logs/app.log"
    exit 0
  fi
  if [ -f data/bot.pid ] && ! kill -0 "$(cat data/bot.pid)" 2>/dev/null; then break; fi
done
echo "${R}The bot did not come up. Last lines of logs/console.log:${N}"
tail -25 logs/console.log
exit 1
