import importlib.util
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import mac_signal_trial as trial


class ActionConsumerTests(unittest.TestCase):
    def setUp(self):
        self.session = str(uuid4())
        self.action = {"action_id": str(uuid4()), "session_id": self.session, "remaining_ms": 5000}
        self.paste = Mock(return_value="executed")
        self.consumer = trial.ActionConsumer(self.session, self.paste, monotonic=lambda: 12)

    def test_dedupes_before_side_effect_even_after_native_failure(self):
        self.paste.side_effect = RuntimeError("native uncertain")
        self.assertEqual(self.consumer.consume(self.action, 11)[1:], ("cancelled", "paste_failed"))
        self.assertIsNone(self.consumer.consume(self.action, 11))
        self.paste.assert_called_once()

    def test_request_time_including_long_poll_is_removed_from_budget(self):
        self.assertEqual(self.consumer.consume(self.action, 7.5)[1:], ("cancelled", "expired"))
        self.paste.assert_not_called()

    def test_old_receiver_or_body_payload_never_executes(self):
        for action in ({**self.action, "session_id": str(uuid4())}, {**self.action, "text": "forbidden"}):
            self.assertIsNone(self.consumer.consume(action, 11))
        self.paste.assert_not_called()

    def test_invalid_ttl_cannot_bypass_expiry(self):
        for ttl in [float("nan"), float("inf"), True, "5000", 5001, 0, -1]:
            action = {**self.action, "action_id": str(uuid4()), "remaining_ms": ttl}
            self.assertEqual(self.consumer.consume(action, 11)[1:], ("cancelled", "expired"))
        self.paste.assert_not_called()

    def test_success_reports_only_command_execution(self):
        self.assertEqual(self.consumer.consume(self.action, 11)[1:], ("executed", None))
        self.paste.assert_called_once_with(16, 0.5)

    def test_focus_change_cancel_not_retried(self):
        self.paste.return_value = "focus_changed"
        self.assertEqual(self.consumer.consume(self.action, 11)[1:], ("cancelled", "focus_changed"))
        self.assertIsNone(self.consumer.consume(self.action, 11))


class LifecycleTests(unittest.TestCase):
    def test_conflict_never_enters_receiver_or_calls_system_processes(self):
        from helper_instance import HelperInstanceError
        with tempfile.TemporaryDirectory() as temp:
            lock = trial.HelperInstanceLock(Path(temp).resolve() / "lock", process_probe=lambda **kw: {"process_state": "none", "pids": []})
            with lock, patch.object(trial, "HelperInstanceLock", side_effect=lambda: type(lock)(Path(temp).resolve() / "lock", process_probe=lambda **kw: {"process_state": "none", "pids": []})), patch.object(trial.subprocess, "run") as process:
                with self.assertRaises(HelperInstanceError):
                    with trial.exclusive_trial():
                        self.fail("second receiver must refuse")
                process.assert_not_called()

    def test_exception_releases_lease(self):
        with tempfile.TemporaryDirectory() as temp:
            lock = trial.HelperInstanceLock(Path(temp).resolve() / "lock", process_probe=lambda **kw: {"process_state": "none", "pids": []})
            with patch.object(trial, "HelperInstanceLock", return_value=lock), patch.object(trial, "emit"):
                with self.assertRaises(ValueError):
                    with trial.exclusive_trial():
                        raise ValueError("receiver stopped")
                with trial.exclusive_trial():
                    self.assertIsNotNone(lock.descriptor)
            self.assertIsNone(lock.descriptor)


if __name__ == "__main__":
    unittest.main()
