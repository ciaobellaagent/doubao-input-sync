#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEFAULT_ROOM_ID="$(python3 - <<'PY'
import secrets
print(f"pair-{secrets.token_hex(3)}", end="")
PY
)"
ROOM_ID="${ROOM_ID:-$DEFAULT_ROOM_ID}"
ARCHIVE_IDLE_SECONDS_VALUE="${ARCHIVE_IDLE_SECONDS:-2.0}"

cd "$PROJECT_ROOT"
python3 app/server.py --default-room "$ROOM_ID" --archive-idle-seconds "$ARCHIVE_IDLE_SECONDS_VALUE" "$@"
