#!/bin/bash
LABEL="com.autonomous-ai-trader"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Automatic start removed. (A running bot keeps running until ./stop.sh)"
