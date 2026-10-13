#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SERVER_URL="${SERVER_URL:-http://127.0.0.1:18766}"
ROOM_ID="${ROOM_ID:-doubao}"
TRIGGER="${TRIGGER:-archive}"
REQUEST_TIMEOUT_SECONDS="${REQUEST_TIMEOUT_SECONDS:-12}"
STREAM_MAX_TIME_SECONDS="${STREAM_MAX_TIME_SECONDS:-90}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-0.25}"
CURL_RESOLVE="${CURL_RESOLVE:-}"
TRANSPORT="${TRANSPORT:-stream}"

if [[ -n "${CF_ACCESS_CLIENT_ID:-}" || -n "${CF_ACCESS_CLIENT_SECRET:-}" ]]; then
  if [[ -z "${CF_ACCESS_CLIENT_ID:-}" || -z "${CF_ACCESS_CLIENT_SECRET:-}" ]]; then
    echo "Set both CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET" >&2
    exit 1
  fi
  export CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET
fi

cd "$PROJECT_ROOT"

echo "Starting foreground paste helper"
echo "server_url=${SERVER_URL}"
echo "room_id=${ROOM_ID}"
echo "trigger=${TRIGGER}"
echo "request_timeout_seconds=${REQUEST_TIMEOUT_SECONDS}"
echo "stream_max_time_seconds=${STREAM_MAX_TIME_SECONDS}"
echo "interval_seconds=${INTERVAL_SECONDS}"
echo "curl_resolve=${CURL_RESOLVE}"
echo "transport=${TRANSPORT}"
echo "mode=paste"
if [[ -n "${CF_ACCESS_CLIENT_ID:-}" ]]; then
  echo "cloudflare_access=service_token"
fi

helper_args=(
  --server-url "$SERVER_URL" \
  --room-id "$ROOM_ID" \
  --interval-seconds "$INTERVAL_SECONDS" \
  --mode paste \
  --trigger "$TRIGGER" \
  --transport "$TRANSPORT" \
  --request-timeout-seconds "$REQUEST_TIMEOUT_SECONDS" \
  --stream-max-time-seconds "$STREAM_MAX_TIME_SECONDS"
)

if [[ -n "$CURL_RESOLVE" ]]; then
  helper_args+=(--curl-resolve "$CURL_RESOLVE")
fi

exec /usr/bin/python3 scripts/mac_paste_helper.py "${helper_args[@]}"
