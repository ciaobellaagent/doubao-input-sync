#!/bin/zsh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export SERVER_URL="${SERVER_URL:-http://127.0.0.1:18766}"
export ROOM_ID="${ROOM_ID:-doubao}"
# Reuse the helper's validation, credential transport and no-redirect policy.
PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PYCODE'
import os
from mac_paste_helper import fetch_state
state = fetch_state(os.environ["SERVER_URL"], os.environ["ROOM_ID"], request_timeout_seconds=6, curl_resolve=[])
assert state.get("room_id") == os.environ["ROOM_ID"]
assert isinstance(state.get("version"), int)
print("helper_state_ok")
PYCODE
