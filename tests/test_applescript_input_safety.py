"""Compile guards and exercise their pure policy; never run UI/clipboard code."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
GUARDS = ("prearmed_paste.applescript", "signal_paste.applescript")


@unittest.skipUnless(sys.platform == "darwin", "AppleScript compiler requires macOS")
class AppleScriptInputSafetyTests(unittest.TestCase):
    def test_both_complete_scripts_compile_without_running(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in GUARDS:
                with self.subTest(script=name):
                    result = subprocess.run(
                        ["/usr/bin/osacompile", "-o", str(Path(directory) / (name + ".scpt")), str(SCRIPTS / name)],
                        capture_output=True, text=True, timeout=20,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_pure_policy_rejects_protected_disabled_and_unreadable_attributes(self):
        cases = [
            ('"AXTextArea", "", true, false', "ready"),
            ('"AXTextField", "", true, false', "ready"),
            ('"AXComboBox", "", true, false', "ready"),
            ('"AXTextField", "AXSecureTextField", true, false', "unsafe_target"),
            ('"AXTextArea", "", true, true', "unsafe_target"),
            ('"AXTextField", "", false, false', "no_focused_input"),
            ('"AXWebArea", "", true, false', "no_focused_input"),
            ('"AXTextField", missing value, true, false', "error:-1700"),
            ('"AXTextField", "", true, missing value', "error:-1700"),
            ('"AXTextField", "", "true", false', "error:-1700"),
            ('"AXTextField", "", true, "false"', "error:-1700"),
            ('missing value, "", true, false', "error:-1700"),
        ]
        for name in GUARDS:
            source = (SCRIPTS / name).read_text()
            start = source.index("on inputSafety(")
            end = source.index("end inputSafety", start) + len("end inputSafety")
            # Extract only the pure handler. The actual UI run handler and
            # fieldSafety AX adapter are never submitted to osascript.
            pure_handler = source[start:end]
            calls = ["set outcomes to {}"]
            for arguments, _ in cases:
                calls += ["try", f"set end of outcomes to my inputSafety({arguments})",
                          "on error messageText number errorNumber",
                          'set end of outcomes to "error:" & errorNumber', "end try"]
            calls += ['set AppleScript\'s text item delimiters to "|"', "return outcomes as text"]
            program = "on run\n" + "\n".join(calls) + "\nend run\n" + pure_handler
            with self.subTest(script=name):
                result = subprocess.run(["/usr/bin/osascript", "-"], input=program,
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip().split("|"), [expected for _, expected in cases])


if __name__ == "__main__":
    unittest.main()
