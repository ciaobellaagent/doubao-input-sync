"""Compile guards and exercise their pure policy; never run UI/clipboard code."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
GUARDS = ("prearmed_paste.applescript", "signal_paste.applescript")


@unittest.skipUnless(sys.platform == "darwin", "AppleScript compiler requires macOS")
class AppleScriptInputSafetyTests(unittest.TestCase):
    def test_progress_uses_fixed_stderr_without_ui_or_clipboard_access(self):
        source = (SCRIPTS / "prearmed_paste.applescript").read_text()
        handlers = []
        for name in ("traceEnabled", "progress"):
            start = source.index(f"on {name}(")
            end = source.index(f"end {name}", start) + len(f"end {name}")
            handlers.append(source[start:end])
        program = ('use framework "Foundation"\nproperty diagnosticCount : 0\nproperty diagnosticCapturePending : false\non run\nmy progress("as_start", 0)\n'
                   'return "ok"\nend run\non legacyProgress(a, b)\nreturn\nend legacyProgress\n' + "\n".join(handlers))
        for enabled in ("1", "0", ""):
            result = subprocess.run(["/usr/bin/osascript", "-"], input=program,
                                    capture_output=True, text=True, timeout=20,
                                    env={**os.environ, "DOUBAO_PASTE_TRACE": enabled})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "ok")
            self.assertEqual(result.stderr, "DOUBAO_TRACE|as_start|0\n" if enabled == "1" else "")

    def test_legacy_progress_is_private_bounded_and_correlated_in_temporary_directory(self):
        source = (SCRIPTS / "prearmed_paste.applescript").read_text()
        handlers = []
        for name in ("traceEnabled", "progress", "legacyProgress", "nowMs"):
            start = source.index(f"on {name}(")
            end = source.index(f"end {name}", start) + len(f"end {name}")
            handlers.append(source[start:end])
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            directory = base / "Library/Logs/DoubaoInputSync"
            directory.mkdir(parents=True)
            log = directory / "native-errors.log"
            log.write_bytes(b"x" * 2097000)
            isolated = "\n".join(handlers).replace("current application's NSHomeDirectory() as text", '"' + str(base) + '"')
            program = ('use framework "Foundation"\nproperty diagnosticCount : 0\nproperty diagnosticCapturePending : false\nproperty diagnosticLegacyStarted : false\n'
                       'property diagnosticTraceId : "00000000-0000-4000-8000-000000000001"\n'
                       'on run\nrepeat 70 times\nmy progress("as_start", 0)\nend repeat\n'
                       'return "ok"\nend run\n' + isolated)
            result = subprocess.run(["/usr/bin/osascript", "-"], input=program,
                                    capture_output=True, text=True, timeout=20,
                                    env={**os.environ, "DOUBAO_PASTE_TRACE": "0"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            lines = log.read_text().splitlines()
            self.assertEqual(len(lines), 64)
            self.assertTrue(all(line.endswith("|as_start|0|00000000-0000-4000-8000-000000000001") for line in lines))
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            self.assertEqual((directory / "native-errors.log.1").stat().st_size, 2097000)

    def test_optional_metadata_absence_is_not_a_malformed_present_value(self):
        source = (SCRIPTS / "prearmed_paste.applescript").read_text()
        handlers = []
        for name in ("optionalAXText", "optionalAXBool", "inputRoute", "inputSafety"):
            start = source.index(f"on {name}(")
            end = source.index(f"end {name}", start) + len(f"end {name}")
            handlers.append(source[start:end])
        cases = [
            ('my optionalAXText(missing value, "")', ""),
            ('my optionalAXBool(missing value, true)', "true"),
            ('my optionalAXBool(missing value, false)', "false"),
            ('my optionalAXBool(false, true)', "false"),
            ('my optionalAXText(true, "")', "error:-1700"),
            ('my optionalAXBool("false", false)', "error:-1700"),
            ('my optionalAXBool(0, false)', "error:-1700"),
            ('my inputRoute("AXTextArea", my optionalAXText(missing value, ""), true, false)', "ready"),
            ('my inputRoute("AXWebArea", my optionalAXText(missing value, ""), true, false)', "native_focus"),
            ('my inputRoute("AXTextField", my optionalAXText("AXSecureTextField", ""), true, false)', "unsafe_target"),
            ('my inputRoute("AXTextArea", "", true, my optionalAXBool(true, false))', "unsafe_target"),
            ('my inputRoute("AXTextArea", "", my optionalAXBool(false, true), false)', "no_focused_input"),
        ]
        calls = ["set outcomes to {}"]
        for expression, _ in cases:
            calls += ["try", f"set end of outcomes to ({expression}) as text",
                      "on error messageText number errorNumber",
                      'set end of outcomes to "error:" & errorNumber', "end try"]
        calls += ['set AppleScript\'s text item delimiters to "|"', "return outcomes as text"]
        program = "on run\n" + "\n".join(calls) + "\nend run\n" + "\n".join(handlers)
        result = subprocess.run(["/usr/bin/osascript", "-"], input=program,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.rstrip("\n").split("|"), [expected for _, expected in cases])

    def test_only_initial_representation_errors_can_use_native_resolution(self):
        source = (SCRIPTS / "prearmed_paste.applescript").read_text()
        start = source.index("on nativeFocusError(")
        end = source.index("end nativeFocusError", start) + len("end nativeFocusError")
        cases = [(stage, code, "true") for stage in ("focused_input", "focused_window", "input_role")
                 for code in (-1728, -1700)]
        cases += [(stage, code, "false") for stage in
                  ("target_process", "focus_recheck", "clipboard_read", "modifiers", "key_dispatch")
                  for code in (-1728, -1700)]
        cases += [("input_role", code, "false") for code in (-1743, -1719, -25211, -25204, 0)]
        calls = ["set outcomes to {}"]
        calls += [f'set end of outcomes to (my nativeFocusError("{stage}", {code})) as text'
                  for stage, code, _ in cases]
        calls += ['set AppleScript\'s text item delimiters to "|"', "return outcomes as text"]
        program = "on run\n" + "\n".join(calls) + "\nend run\n" + source[start:end]
        result = subprocess.run(["/usr/bin/osascript", "-"], input=program,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().split("|"), [expected for _, _, expected in cases])

    def test_native_focus_resolution_is_only_for_enabled_nontext_wrappers(self):
        source = (SCRIPTS / "prearmed_paste.applescript").read_text()
        handlers = []
        for name in ("inputSafety", "inputRoute"):
            start = source.index(f"on {name}(")
            end = source.index(f"end {name}", start) + len(f"end {name}")
            handlers.append(source[start:end])
        cases = [
            ('"AXTextArea", "", true, false', "ready"),
            ('"AXWebArea", "", true, false', "native_focus"),
            ('"AXGroup", "", true, false', "native_focus"),
            ('"AXUnknown", "", true, false', "native_focus"),
            ('"AXWebArea", "", false, false', "no_focused_input"),
            ('"AXTextArea", "", false, false', "no_focused_input"),
            ('"AXGroup", "AXSecureTextField", true, false', "unsafe_target"),
            ('"AXGroup", "", true, true', "unsafe_target"),
        ]
        calls = ["set outcomes to {}"]
        calls += [f"set end of outcomes to my inputRoute({arguments})" for arguments, _ in cases]
        calls += ['set AppleScript\'s text item delimiters to "|"', "return outcomes as text"]
        program = "on run\n" + "\n".join(calls) + "\nend run\n" + "\n".join(handlers)
        result = subprocess.run(["/usr/bin/osascript", "-"], input=program,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().split("|"), [expected for _, expected in cases])

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
