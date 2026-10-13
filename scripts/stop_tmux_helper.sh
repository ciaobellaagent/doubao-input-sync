#!/bin/zsh
set -euo pipefail

SESSION_NAME="${TMUX_SESSION_NAME:-doubao-paste}"

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
  tmux kill-session -t "$SESSION_NAME"
  echo "Stopped tmux session: $SESSION_NAME"
else
  echo "tmux session not found: $SESSION_NAME"
fi
