#!/bin/bash
# Started by macOS launchd after login/restart. Makes sure Ollama is up, then starts the bot.
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
ollama_up() { curl -s -m 3 http://127.0.0.1:11434/api/version >/dev/null 2>&1; }
if ! ollama_up; then
  OB="$(command -v ollama 2>/dev/null || true)"
  if [ -d /Applications/Ollama.app ]; then open -a Ollama
  elif [ -n "$OB" ]; then nohup "$OB" serve > logs/ollama.log 2>&1 &
  fi
  for i in $(seq 1 60); do ollama_up && break; sleep 1; done
fi
export PYTHONPATH="$ROOT"
if command -v caffeinate >/dev/null 2>&1; then exec caffeinate -i .venv/bin/python -m bot.main --from-launchd; fi
exec .venv/bin/python -m bot.main --from-launchd
