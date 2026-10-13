#!/bin/zsh
set -euo pipefail

LABEL="io.github.ciaobellaagent.doubao-input-sync.helper"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
rm -f "$PLIST_PATH"

echo "Removed launch agent plist: ${PLIST_PATH}"
echo "Config file was kept at ~/.config/doubao-input-sync/helper.env"
