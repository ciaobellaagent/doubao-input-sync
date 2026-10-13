#!/bin/zsh
set -euo pipefail

LABEL="io.github.ciaobellaagent.doubao-input-sync.helper"

echo "LaunchAgent label: ${LABEL}"
launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null || {
  echo "LaunchAgent is not loaded."
  exit 1
}
