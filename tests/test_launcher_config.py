"""Exercise installer serialization only: never invoke launchctl or a helper."""
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest


class LauncherConfigTests(unittest.TestCase):
    def test_config_quotes_literals_and_plist_encodes_paths(self):
        script = (Path(__file__).resolve().parents[1] / "scripts/install_launch_agent.sh").read_text()
        python_source = script.split("python3 - <<'PYCODE'\n", 1)[1].split("\nPYCODE", 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            marker = root / "must-not-exist"
            room = f'quoted " room $(touch "{marker}") `false`\nsecond line'
            environment = {**os.environ, "HOME": str(root), "SERVER_URL": "https://relay.example", "ROOM_ID": room,
                           "MODE": "paste", "TRIGGER": "archive", "REQUEST_TIMEOUT_SECONDS": "8",
                           "DOUBAO_PROJECT_ROOT": str(root / 'project & "quoted"'), "DOUBAO_LABEL": "test.helper"}
            subprocess.run(["python3", "-c", python_source], env=environment, check=True)
            config = root / ".config/doubao-input-sync/helper.env"
            result = subprocess.run(["/bin/zsh", "-c", 'source "$1"; printf %s "$ROOM_ID"', "test", str(config)], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, room)
            self.assertFalse(marker.exists())
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            plist = root / "Library/LaunchAgents/test.helper.plist"
            self.assertEqual(plist.stat().st_mode & 0o777, 0o600)
            self.assertEqual(plistlib.loads(plist.read_bytes())["WorkingDirectory"], environment["DOUBAO_PROJECT_ROOT"])


if __name__ == "__main__":
    unittest.main()
