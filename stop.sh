#!/bin/bash
# stop.sh - stop the bot cleanly.
#   ./stop.sh                    stop the bot (open positions KEEP their exchange-side stop loss / take profit)
#   ./stop.sh --emergency-close  stop the bot, then cancel opening orders, close the position, and halt trading
#   ./stop.sh --force            kill immediately if it will not stop
cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"; PYV=".venv/bin/python"
EMERG=0; FORCE=0
for a in "$@"; do case "$a" in --emergency-close) EMERG=1 ;; --force) FORCE=1 ;; esac; done
if [ -f data/bot.pid ] && kill -0 "$(cat data/bot.pid)" 2>/dev/null; then
  PID="$(cat data/bot.pid)"
  if [ "$FORCE" = 1 ]; then kill -KILL "$PID"; echo "Killed $PID"; rm -f data/bot.pid
  else
    kill -TERM "$PID"; echo "Stopping bot (PID $PID) ..."
    for i in $(seq 1 30); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
    if kill -0 "$PID" 2>/dev/null; then echo "Still running after 30s. Use ./stop.sh --force"; exit 1; fi
    echo "Bot stopped."
  fi
else
  echo "Bot is not running."; rm -f data/bot.pid
fi
if [ "$EMERG" = 1 ]; then
  echo; echo "EMERGENCY CLOSE ..."
  PYTHONPATH="$ROOT" "$PYV" -m bot.emergency_cli
  exit $?
fi
