#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

LABEL="io.github.ciaobellaagent.doubao-input-sync.helper"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"
CONFIG_DIR="${HOME}/.config/doubao-input-sync"
CONFIG_FILE="${CONFIG_DIR}/helper.env"
LOG_DIR="${HOME}/Library/Logs/doubao-input-sync"

SERVER_URL="${SERVER_URL:-}"
ROOM_ID="${ROOM_ID:-}"
MODE="${MODE:-paste}"
TRIGGER="${TRIGGER:-archive}"
REQUEST_TIMEOUT_SECONDS="${REQUEST_TIMEOUT_SECONDS:-8}"

if [[ -z "$SERVER_URL" || -z "$ROOM_ID" ]]; then
  echo "Usage: SERVER_URL=<url> ROOM_ID=<room> [MODE=paste] [TRIGGER=archive] ./scripts/install_launch_agent.sh" >&2
  exit 1
fi

umask 077
export DOUBAO_PROJECT_ROOT="$PROJECT_ROOT" DOUBAO_LABEL="$LABEL"
export SERVER_URL ROOM_ID MODE TRIGGER REQUEST_TIMEOUT_SECONDS
python3 - <<'PYCODE'
import os
from pathlib import Path
import plistlib
import shlex
import tempfile

home = Path.home()
config_dir = home / ".config/doubao-input-sync"
log_dir = home / "Library/Logs/doubao-input-sync"
launch_dir = home / "Library/LaunchAgents"
for directory in (config_dir, log_dir, launch_dir):
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise SystemExit("Refusing symlink configuration directory")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
for directory in (config_dir, log_dir):
    directory.chmod(0o700)

def write_private(path, data):
    if path.is_symlink():
        raise SystemExit("Refusing symlink configuration file")
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

keys = ("SERVER_URL", "ROOM_ID", "MODE", "TRIGGER", "REQUEST_TIMEOUT_SECONDS")
config = "".join(f"{key}={shlex.quote(os.environ[key])}\n" for key in keys)
write_private(config_dir / "helper.env", config.encode())
root = os.environ["DOUBAO_PROJECT_ROOT"]
label = os.environ["DOUBAO_LABEL"]
plist = {
    "Label": label,
    "ProgramArguments": ["/bin/zsh", str(Path(root) / "scripts/run_helper_daemon.sh")],
    "WorkingDirectory": root, "RunAtLoad": True, "KeepAlive": True,
    "StandardOutPath": str(log_dir / "helper.stdout.log"),
    "StandardErrorPath": str(log_dir / "helper.stderr.log"),
    "ProcessType": "Interactive",
}
write_private(launch_dir / f"{label}.plist", plistlib.dumps(plist))
PYCODE

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
launchctl kickstart -k "gui/$(id -u)/${LABEL}"

echo "Installed launch agent: ${LABEL}"
echo "Plist: ${PLIST_PATH}"
echo "Config: ${CONFIG_FILE}"
echo "Logs: ${LOG_DIR}"
