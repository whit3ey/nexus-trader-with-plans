#!/bin/bash
# setup.sh - checks everything this project needs and OFFERS to install whatever is missing.
#   ./setup.sh               interactive (asks before each install)
#   ./setup.sh --yes         accept every install offer automatically
#   ./setup.sh --check-only  only report what is installed / missing, change nothing
#   ./setup.sh --skip-tests  skip the unit tests
set -u
cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"
AUTO_YES=0; CHECK_ONLY=0; SKIP_TESTS=0
for a in "$@"; do
  case "$a" in
    --yes|-y) AUTO_YES=1 ;;
    --check-only) CHECK_ONLY=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "Unknown option: $a"; exit 1 ;;
  esac
done

if [ -t 1 ]; then G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; B=$'\033[1m'; N=$'\033[0m'; else G=""; R=""; Y=""; B=""; N=""; fi
ok()   { echo "  ${G}[OK]${N}      $*"; }
miss() { echo "  ${R}[MISSING]${N} $*"; }
warn() { echo "  ${Y}[WARN]${N}    $*"; }
info() { echo "  $*"; }
step() { echo; echo "${B}== $* ==${N}"; }
die()  { echo; echo "${R}${B}SETUP STOPPED:${N} $*"; exit 1; }

ask() {   # ask "question"  -> returns 0 for yes
  [ "$CHECK_ONLY" = 1 ] && return 1
  if [ "$AUTO_YES" = 1 ]; then echo "  $1 -> yes (--yes)"; return 0; fi
  if [ ! -t 0 ]; then echo "  $1 -> no (not an interactive terminal; re-run ./setup.sh in Terminal, or use --yes)"; return 1; fi
  local reply; read -r -p "  $1 [Y/n] " reply
  case "$reply" in n|N|no|NO) return 1 ;; *) return 0 ;; esac
}

echo "${B}Autonomous AI Trader - setup${N}"
[ "$CHECK_ONLY" = 1 ] && echo "(check-only mode: nothing will be installed or changed)"

# ---------------------------------------------------------------- 1. platform
step "1/10  Mac hardware"
OS="$(uname -s)"; ARCH="$(uname -m)"
if [ "$OS" != "Darwin" ]; then
  warn "This is $OS, not macOS. The project is built for Apple Silicon Macs; continuing anyway."
else
  ok "macOS $(sw_vers -productVersion 2>/dev/null)"
  if [ "$ARCH" = "arm64" ]; then ok "Apple Silicon (arm64)"
  else
    if [ "$(sysctl -n sysctl.proc_translated 2>/dev/null)" = "1" ]; then warn "Terminal is running under Rosetta (x86_64 emulation). Use a native Terminal for best speed."
    else warn "Intel Mac detected ($ARCH). It will work but local AI models will be much slower."; fi
  fi
  system_profiler SPHardwareDataType 2>/dev/null | grep -E "Chip:|Memory:|Total Number of Cores:" | sed 's/^ */  /'
fi

# ---------------------------------------------------------------- 2. xcode tools
step "2/10  Apple command line tools"
if [ "$OS" = "Darwin" ]; then
  if xcode-select -p >/dev/null 2>&1; then ok "Command line tools installed"
  else
    miss "Apple command line tools (needed for Git/Python builds)"
    if ask "Install them now? (a macOS window will open; click Install, wait, then run ./setup.sh again)"; then
      xcode-select --install 2>/dev/null; echo "  After the installation finishes, run ./setup.sh again."; exit 0
    else warn "Skipped. Some later steps may fail."; fi
  fi
else info "skipped (not macOS)"; fi

# ---------------------------------------------------------------- 3. homebrew
step "3/10  Homebrew (package installer)"
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
if command -v brew >/dev/null 2>&1; then ok "Homebrew $(brew --version | head -1)"
else
  miss "Homebrew"
  if [ "$OS" = "Darwin" ] && ask "Install Homebrew? (official installer from brew.sh; may ask for your Mac password)"; then
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || die "Homebrew installation failed. See https://brew.sh"
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
    command -v brew >/dev/null 2>&1 && ok "Homebrew installed" || die "Homebrew installed but not found in PATH. Open a new Terminal and re-run ./setup.sh"
  else warn "Without Homebrew you must install Python 3.10+ and Ollama yourself (links below)."; fi
fi

# ---------------------------------------------------------------- 4. python
step "4/10  Python 3.10 or newer"
find_python() {
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then command -v "$c"; return 0; fi
  done; return 1
}
PY="$(find_python)" || PY=""
if [ -n "$PY" ]; then ok "$($PY --version) at $PY"
else
  miss "Python 3.10+"
  if command -v brew >/dev/null 2>&1 && ask "Install Python 3.12 with Homebrew?"; then
    brew install python@3.12 || die "brew install python@3.12 failed"
    PY="$(find_python)" || die "Python installed but not found. Open a new Terminal and re-run ./setup.sh"
    ok "$($PY --version)"
  elif [ "$CHECK_ONLY" = 1 ]; then :
  else die "Python 3.10+ is required. Install it from https://www.python.org/downloads/ then re-run ./setup.sh"; fi
fi

# ---------------------------------------------------------------- 5. venv + packages
step "5/10  Python environment and packages"
if [ -n "$PY" ]; then
  if [ -x .venv/bin/python ]; then ok "virtual environment .venv exists"
  else
    miss "virtual environment (.venv) - an isolated folder for this project's packages"
    if ask "Create it?"; then "$PY" -m venv .venv || die "could not create .venv"; ok "created .venv"; fi
  fi
  if [ -x .venv/bin/python ]; then
    MISSING_PKGS="$(.venv/bin/python scripts/check_env.py 2>/dev/null)"
    if [ -z "$MISSING_PKGS" ]; then ok "all packages installed (PyYAML, python-dotenv, pybit)"
    else
      miss "Python packages: $MISSING_PKGS"
      if ask "Install them with pip (from requirements.txt)?"; then
        .venv/bin/python -m pip install --upgrade pip >/dev/null 2>&1
        .venv/bin/python -m pip install -r requirements.txt || die "pip install failed. Check your internet connection, then re-run ./setup.sh"
        STILL="$(.venv/bin/python scripts/check_env.py 2>/dev/null)"
        [ -z "$STILL" ] && ok "packages installed" || die "still missing after install: $STILL"
      else warn "Skipped. The bot cannot start without PyYAML."; fi
    fi
  fi
fi
PYV=".venv/bin/python"
[ -x "$PYV" ] || { [ "$CHECK_ONLY" = 1 ] && PYV="${PY:-python3}" || die "no virtual environment available"; }

# ---------------------------------------------------------------- 6. ollama
step "6/10  Ollama (runs the AI on your Mac)"
OLLAMA_BIN="$(command -v ollama 2>/dev/null || true)"
[ -z "$OLLAMA_BIN" ] && [ -x /Applications/Ollama.app/Contents/Resources/ollama ] && OLLAMA_BIN=/Applications/Ollama.app/Contents/Resources/ollama
if [ -n "$OLLAMA_BIN" ]; then ok "Ollama installed ($OLLAMA_BIN)"
else
  miss "Ollama"
  if command -v brew >/dev/null 2>&1 && ask "Install Ollama with Homebrew (brew install ollama)?"; then
    brew install ollama || die "brew install ollama failed. You can install it manually from https://ollama.com/download"
    OLLAMA_BIN="$(command -v ollama)" || die "Ollama installed but not found in PATH"
    ok "Ollama installed"
  else
    info "Manual install: download from https://ollama.com/download, open it once, then re-run ./setup.sh"
    [ "$CHECK_ONLY" = 1 ] || warn "Ollama is required for the AI. Continuing so the rest can be checked."
  fi
fi
ollama_up() { curl -s -m 3 http://127.0.0.1:11434/api/version >/dev/null 2>&1; }
if [ -n "$OLLAMA_BIN" ]; then
  if ollama_up; then ok "Ollama server is running"
  else
    miss "Ollama server is not running"
    if ask "Start it now?"; then
      mkdir -p logs
      if [ -d /Applications/Ollama.app ]; then open -a Ollama; else nohup "$OLLAMA_BIN" serve > logs/ollama.log 2>&1 & fi
      for i in $(seq 1 30); do ollama_up && break; sleep 1; done
      ollama_up && ok "Ollama server started" || warn "Ollama did not start in 30s. Try: ollama serve   (in another Terminal window)"
    fi
  fi
fi

# ---------------------------------------------------------------- 7. model
step "7/10  AI model for this Mac"
MODEL=""
if [ -x .venv/bin/python ] || [ "$CHECK_ONLY" = 1 ]; then
  echo
  PYTHONPATH="$ROOT" "$PYV" -m bot.hardware | sed 's/^/  /'
  FROM_ENV="$(grep -E '^OLLAMA_MODEL=.+' .env 2>/dev/null | head -1 | cut -d= -f2-)"
  MODEL="$(OLLAMA_MODEL="${FROM_ENV:-${OLLAMA_MODEL:-}}" PYTHONPATH="$ROOT" "$PYV" -c 'import os; from bot.hardware import *; print(select_model(detect_hardware().memory_gb, override=os.environ.get("OLLAMA_MODEL")).model)')"
  SIZE="$(OLLAMA_MODEL="${FROM_ENV:-${OLLAMA_MODEL:-}}" PYTHONPATH="$ROOT" "$PYV" -c 'import os; from bot.hardware import *; c=select_model(detect_hardware().memory_gb, override=os.environ.get("OLLAMA_MODEL")); print("%.1f" % c.size_gb if c.size_gb else "unknown")')"
  echo
  if [ -n "$MODEL" ] && [ -n "$OLLAMA_BIN" ] && ollama_up; then
    if "$OLLAMA_BIN" list 2>/dev/null | awk -v m="$MODEL" 'NR>1 && ($1==m || $1==m":latest"){f=1} END{exit !f}'; then ok "model $MODEL is already downloaded"
    else
      miss "model $MODEL is not downloaded (about ${SIZE} GB)"
      if ask "Download $MODEL now? (~${SIZE} GB, may take 5-30 minutes depending on your connection)"; then
        "$OLLAMA_BIN" pull "$MODEL" || die "model download failed. Check your connection and run: ollama pull $MODEL"
        ok "model $MODEL downloaded"
      else warn "Skipped. Later run: ollama pull $MODEL"; fi
    fi
  elif [ -n "$MODEL" ]; then warn "Cannot check/download the model until the Ollama server is running."; fi
  if [ -n "$MODEL" ] && [ "$CHECK_ONLY" = 0 ]; then mkdir -p data; echo "$MODEL" > data/selected_model.txt; fi
fi

[ "$CHECK_ONLY" = 1 ] && { echo; echo "${B}Check complete.${N} Run ./setup.sh (without --check-only) to install anything marked MISSING."; exit 0; }

# ---------------------------------------------------------------- 8. files
step "8/10  Configuration, folders, database"
mkdir -p data logs
if [ -f .env ]; then ok ".env already exists (left untouched)"
else cp .env.example .env && chmod 600 .env && ok "created .env (safe defaults: testnet, trading OFF, autonomous OFF)"; fi
chmod 600 .env 2>/dev/null
ok "config/config.yaml present (mode: $(grep -E '^mode:' config/config.yaml | awk '{print $2}'))"
chmod +x setup.sh start.sh stop.sh status.sh scripts/*.sh 2>/dev/null
PYTHONPATH="$ROOT" "$PYV" -c 'from bot.config import load_config; from bot.database.database import Database; c=load_config(); d=Database(c.path(c.raw["database"]["path"])); assert d.integrity_ok(); print("  [OK]      database ready:", c.path(c.raw["database"]["path"]))' || die "configuration or database problem (see message above)"

# ---------------------------------------------------------------- 9. tests
step "9/10  Unit tests"
if [ "$SKIP_TESTS" = 1 ]; then warn "skipped (--skip-tests)"
else
  if "$PYV" -m unittest discover -s tests -t . 2>&1 | tail -4 | sed 's/^/  /'; [ "${PIPESTATUS[0]}" = 0 ]; then ok "all unit tests passed"
  else die "unit tests failed. Run: $PYV -m unittest discover -s tests -t . -v   and send me the first error"; fi
fi

# ---------------------------------------------------------------- 10. connectivity
step "10/10 Connectivity and system diagnostics"
"$PYV" -m bot.diagnostics --skip-tests | sed 's/^/  /'
DIAG=${PIPESTATUS[0]}

echo
echo "${B}================ SETUP RESULT ================${N}"
echo "  Mode now:        PAPER (simulated trades only - no real or testnet orders)"
echo "  AI model:        ${MODEL:-not selected}"
echo "  Diagnostics:     $([ "$DIAG" = 0 ] && echo "${G}all required checks passed${N}" || echo "${Y}some items FAILED - read the [FAIL] lines above (Reason / Fix)${N}")"
echo
echo "  Next:   ./start.sh            then open  http://127.0.0.1:8080"
echo "  Check:  ./status.sh           Stop: ./stop.sh"
echo "  Read README.md before enabling Testnet or Live trading."
exit 0
