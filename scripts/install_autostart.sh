#!/bin/bash
# START BOT AUTOMATICALLY AFTER MAC RESTART (opt-in).  Remove again with scripts/uninstall_autostart.sh
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
LABEL="com.autonomous-ai-trader"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
[ "$(uname -s)" = "Darwin" ] || { echo "launchd exists only on macOS."; exit 1; }
[ -x .venv/bin/python ] || { echo "Run ./setup.sh first."; exit 1; }
MODE="$(PYTHONPATH="$ROOT" .venv/bin/python -c 'from bot.config import load_config; c=load_config(); print(c.mode+"|"+str(c.raw["launchd"]["allow_live_autostart"]).lower())')" || exit 1
echo "Current mode: ${MODE%%|*}"
if [ "${MODE%%|*}" = "LIVE" ] && [ "${MODE##*|}" != "true" ]; then
  echo
  echo "NOTE: mode is LIVE but launchd.allow_live_autostart is false in config/config.yaml."
  echo "The bot will be launched at login but will REFUSE to start live trading unattended."
  echo "Set 'allow_live_autostart: true' only if you really want unattended live trading after a restart."
fi
read -r -p "Install automatic start at login/restart? [y/N] " a
case "$a" in y|Y|yes) ;; *) echo "Cancelled."; exit 0 ;; esac
mkdir -p "$HOME/Library/LaunchAgents" logs
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$ROOT/scripts/launchd_run.sh</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$ROOT/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/launchd.err.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
launchctl bootstrap "gui/$(id -u)" "$PLIST" && echo "Installed. The bot now starts automatically when you log in (and restarts after a crash)."
echo "Stop it with ./stop.sh; remove auto-start with scripts/uninstall_autostart.sh"
