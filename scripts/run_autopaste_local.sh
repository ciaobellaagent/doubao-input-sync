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
PORT="${PORT:-18766}"
BASE_PATH="${BASE_PATH:-}"
MODE="${MODE:-paste}"
TRIGGER="${TRIGGER:-archive}"
ARCHIVE_IDLE_SECONDS_VALUE="${ARCHIVE_IDLE_SECONDS:-2.0}"

SERVER_URL="${SERVER_URL:-http://127.0.0.1:${PORT}${BASE_PATH}}"
STARTED_SERVER=0
SERVER_PID=""

cleanup() {
  if [[ "$STARTED_SERVER" == "1" && -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM

cd "$PROJECT_ROOT"

IS_LOCAL_SERVER_URL=0
if [[ "$SERVER_URL" == "http://127.0.0.1:${PORT}${BASE_PATH}" || "$SERVER_URL" == "http://localhost:${PORT}${BASE_PATH}" ]]; then
  IS_LOCAL_SERVER_URL=1
fi

if ! curl -fsS "${SERVER_URL}/api/ping" >/dev/null 2>&1; then
  if [[ "$IS_LOCAL_SERVER_URL" == "1" ]]; then
    echo "No relay server detected on ${SERVER_URL}, starting one locally..."
    SERVER_ARGS=(python3 app/server.py --host 127.0.0.1 --port "$PORT" --default-room "$ROOM_ID" --archive-idle-seconds "$ARCHIVE_IDLE_SECONDS_VALUE")
    if [[ -n "$BASE_PATH" ]]; then
      SERVER_ARGS+=(--base-path "$BASE_PATH")
    fi
    "${SERVER_ARGS[@]}" &
    SERVER_PID=$!
    STARTED_SERVER=1
    for _ in {1..40}; do
      if curl -fsS "${SERVER_URL}/api/ping" >/dev/null 2>&1; then
        break
      fi
      sleep 0.25
    done
  else
    echo "Remote relay server is not reachable at ${SERVER_URL}" >&2
    exit 1
  fi
fi

if ! curl -fsS "${SERVER_URL}/api/ping" >/dev/null 2>&1; then
  echo "Relay server failed to start on ${SERVER_URL}" >&2
  exit 1
fi

echo "Auto paste helper is watching ${SERVER_URL} room=${ROOM_ID} mode=${MODE} trigger=${TRIGGER} archive_idle_seconds=${ARCHIVE_IDLE_SECONDS_VALUE}"
echo "Local test page: ${SERVER_URL}/mobile/${ROOM_ID}"
echo "For a phone on another device, configure an authenticated HTTPS gateway first (see SECURITY.md)."

python3 scripts/mac_paste_helper.py \
  --server-url "$SERVER_URL" \
  --room-id "$ROOM_ID" \
  --mode "$MODE" \
  --trigger "$TRIGGER" \
  "${@:1}"
