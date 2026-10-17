"""Synthetic subprocess/control tests; no desktop, clipboard or live endpoint."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mac_prearmed_receiver as receiver


class ProgressTests(unittest.TestCase):
    def test_stderr_is_bounded_and_only_fixed_numeric_markers_are_retained(self):
        progress = receiver.NativeProgress()
        with patch.object(receiver, "trace") as trace:
            progress.feed(b"private clipboard value\nDOUBAO_TRACE|as_focused_")
            progress.feed(b"input|0\nDOUBAO_TRACE|not_a_known_stage|42\n")
            progress.feed(b"DOUBAO_TRACE|as_start|text\nDOUBAO_TRACE|as_start|2147483648\n")
            progress.feed(b"x" * 50000 + b"DOUBAO_TRACE|as_start|0\n")
            progress.feed(b"DOUBAO_TRACE|as_dispatch_intent|0\n")
            self.assertEqual([call.kwargs for call in trace.call_args_list], [
                {"stage": "as_focused_input", "code": 0},
                {"stage": "as_dispatch_intent", "code": 0},
            ])
            for _ in range(100):
                progress.feed(b"DOUBAO_TRACE|as_start|0\n")
            self.assertEqual(trace.call_count, 64)
            self.assertLessEqual(len(progress.buffer), 256)

    def run_child(self, code, budget=2):
        real_popen = subprocess.Popen
        traces = []
        calls = []
        def child(command, **kwargs):
            calls.append((command, kwargs))
            return real_popen([sys.executable, "-c", code], **kwargs)
        with patch.object(receiver.subprocess, "Popen", side_effect=child), \
                patch.object(receiver, "trace", side_effect=lambda event, **fields: traces.append((event, fields))):
            result = receiver.paste_matching(time.monotonic() + budget, hashlib.sha256(b"synthetic").hexdigest())
        return result, traces, calls

    def test_deadline_before_and_after_dispatch_is_not_a_retry_authorization(self):
        for stage in ("as_focused_input", "as_dispatch_returned"):
            with self.subTest(stage=stage):
                code = 'import sys,time; print("DOUBAO_TRACE|' + stage + '|0", file=sys.stderr, flush=True); time.sleep(5)'
                result, traces, calls = self.run_child(code, budget=0.35)
                self.assertEqual(result, "expired")
                self.assertEqual(len(calls), 1)
                self.assertIn(("native_stage", {"stage": stage, "code": 0}), traces)
                self.assertEqual(traces[-1][1]["termination"], "deadline")
                self.assertIsNotNone(traces[-1][1]["exit_code"])

    def test_exit_drains_last_marker_and_does_not_log_raw_stderr(self):
        result, traces, _ = self.run_child('import sys; print("sensitive OS error", file=sys.stderr); print("DOUBAO_TRACE|as_dispatch_returned|0", file=sys.stderr); print("executed")')
        self.assertEqual(result, "executed")
        self.assertIn(("native_stage", {"stage": "as_dispatch_returned", "code": 0}), traces)
        self.assertEqual(traces[-1][1]["termination"], "exit")
        self.assertNotIn("sensitive", repr(traces))

    def test_action_context_is_attached_to_child_without_body_or_new_authority(self):
        action_id = str(uuid4())
        with receiver.action_context(action_id):
            result, traces, calls = self.run_child('print("executed")')
        self.assertEqual(result, "executed")
        self.assertEqual(calls[0][1]["env"]["DOUBAO_PASTE_TRACE_ID"], action_id)
        self.assertEqual(calls[0][1]["env"]["DOUBAO_PASTE_TRACE"], "1")

    def test_logging_time_cannot_extend_child_deadline(self):
        with patch.object(receiver, "trace", side_effect=lambda *a, **k: time.sleep(0.13)), \
                patch.object(receiver.subprocess, "Popen") as popen:
            self.assertEqual(receiver.paste_matching(time.monotonic() + 0.12, "a" * 64), "expired")
        popen.assert_not_called()

    def test_lock_and_already_spent_poll_budget_do_not_dispatch(self):
        session, prep_id = str(uuid4()), str(uuid4())
        action = {"action_id": str(uuid4()), "session_id": session, "preparation_id": prep_id,
                  "sha256": "a" * 64, "remaining_ms": 5000}
        lock, paste = threading.Lock(), Mock()
        consumer = receiver.MatchConsumer(session, Mock(), paste, lock, monotonic=lambda: 20)
        consumer.preparation = prep_id, "a" * 64
        with patch.object(receiver, "trace") as trace:
            result = consumer.consume(action, 10)
        self.assertEqual(result[1:], ("cancelled", "expired"))
        paste.assert_not_called()
        self.assertLess(next(c.kwargs["remaining_ms"] for c in trace.call_args_list if c.args[0] == "action_start"), 0)
        # A shared relay lock consumes budget; no second attempt follows expiry.
        consumer.monotonic = time.monotonic
        action = {**action, "action_id": str(uuid4()), "remaining_ms": 120}
        lock.acquire()
        try:
            result = consumer.consume(action, time.monotonic())
        finally:
            lock.release()
        self.assertEqual(result[1:], ("cancelled", "expired"))
        self.assertEqual(consumer.consume(action, 0), result)
        paste.assert_not_called()


class ControlDiagnosticTests(unittest.TestCase):
    def test_ack_conflict_and_transport_failure_are_separate_and_redacted(self):
        api = receiver.ControlApi("http://127.0.0.1")
        responses = [
            (Mock(returncode=0, stdout='{"ok":false,"error":"action_already_finished"}\n409'),
             {"outcome": "http_error", "http_status": 409, "error_code": "action_already_finished"}),
            (Mock(returncode=28, stderr="private OS error", stdout=""),
             {"outcome": "transport_failed", "curl_code": 28}),
            (Mock(returncode=0, stdout="private proxy text\n502"),
             {"outcome": "invalid_response", "http_status": 502}),
        ]
        for response, expected in responses:
            with self.subTest(expected=expected), patch.object(receiver.subprocess, "run", return_value=response), \
                    patch.object(receiver, "trace") as trace:
                with self.assertRaises(receiver.ApiError):
                    api.request("ack", {"receiver_token": "private token", "action_id": str(uuid4())})
                fields = trace.call_args.kwargs
                self.assertEqual(fields["endpoint"], "ack")
                for key, value in expected.items():
                    self.assertEqual(fields[key], value)
                self.assertNotIn("private", repr(fields))

    def test_quiet_poll_not_logged_but_timeout_is(self):
        api = receiver.ControlApi("http://127.0.0.1")
        with patch.object(receiver.subprocess, "run", return_value=Mock(returncode=0, stdout='{"ok":true}\n200')), \
                patch.object(receiver, "trace") as trace:
            api.request("poll", {})
            trace.assert_not_called()
        with patch.object(receiver.subprocess, "run", side_effect=subprocess.TimeoutExpired("private command", 9)), \
                patch.object(receiver, "trace") as trace:
            with self.assertRaises(subprocess.TimeoutExpired):
                api.request("poll", {})
            self.assertEqual(trace.call_args.kwargs["outcome"], "process_timeout")
            self.assertNotIn("private", repr(trace.call_args))

    def test_server_expired_ack_conflict_reproduces_without_paste(self):
        from app.signal_trial import SignalStore, SignalError
        now = [100.0]
        store = SignalStore(clock=lambda: now[0], poll_seconds=0)
        registration = store.register("synthetic")
        session, token = registration["session_id"], registration["receiver_token"]
        action_id = str(uuid4())
        store.action("synthetic", session, action_id)
        store.poll("synthetic", session, token)
        now[0] += 5.01
        with self.assertRaises(SignalError) as error:
            store.ack("synthetic", session, token, action_id, "cancelled", "expired")
        self.assertEqual(error.exception.reason, "action_already_finished")
        self.assertEqual(error.exception.status, 409)


@unittest.skipUnless(sys.platform == "darwin", "Native logger requires macOS Swift")
class NativeLoggerTests(unittest.TestCase):
    def test_native_metadata_correlation_rotation_and_event_cap_without_ui(self):
        import tempfile
        source = (Path(receiver.HERE) / "workbuddy_paste.swift").read_text()
        logger = source[source.index("let traceActionID:"):source.index("struct AXFailure:")]
        logger = logger.replace("NSHomeDirectory()", "CommandLine.arguments[1]")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            swift = base / "log-test.swift"
            swift.write_text("import Foundation\nimport Darwin\n" + logger +
                             '\nfor _ in 0..<100 { note("native_start", 0) }\n')
            binary = base / "log-test"
            subprocess.run(["/usr/bin/xcrun", "swiftc", str(swift), "-o", str(binary)],
                           check=True, capture_output=True, timeout=30)
            logdir = base / "Library/Logs/DoubaoInputSync"
            logdir.mkdir(parents=True)
            log = logdir / "native-errors.log"
            log.write_bytes(b"x" * (2 * 1024 * 1024))
            (logdir / "native-errors.log.1").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
            action_id = str(uuid4())
            subprocess.run([str(binary), str(base)], check=True, capture_output=True, timeout=5,
                           env={**os.environ, "DOUBAO_PASTE_TRACE_ID": action_id})
            lines = log.read_text().splitlines()
            self.assertEqual(len(lines), 64)
            self.assertTrue(all(line.endswith("|native_start|0|" + action_id) for line in lines))
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            for path in logdir.glob("native-errors.log*"):
                self.assertLessEqual(path.stat().st_size, 2 * 1024 * 1024)
            subprocess.run([str(binary), str(base)], check=True, capture_output=True, timeout=5,
                           env={**os.environ, "DOUBAO_PASTE_TRACE_ID": "private invalid value"})
            self.assertTrue(log.read_text().splitlines()[-1].endswith("|native_start|0|-"))
            self.assertNotIn("private", log.read_text())
