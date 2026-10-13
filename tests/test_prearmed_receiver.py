import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import mac_prearmed_receiver as receiver


class ProductionConnectionTests(unittest.TestCase):
    def test_control_timeout_changes_only_production_api(self):
        with patch.object(receiver.subprocess, "run", return_value=Mock(returncode=0, stdout='{"ok": true}\n200')) as run:
            receiver.ControlApi("http://local/doubao").request("status", query={"room": "air"})
            production_args = run.call_args.args[0]
            self.assertEqual(production_args[production_args.index("--max-time") + 1], "8.0")
            self.assertEqual(production_args[production_args.index("--connect-timeout") + 1], "6.0")
            self.assertEqual(run.call_args.kwargs["timeout"], 9.0)
            receiver.Api("http://local/doubao").request("status", query={"room": "air"})
            legacy_args = run.call_args.args[0]
            self.assertEqual(legacy_args[legacy_args.index("--max-time") + 1], "4")

    def test_control_error_preserves_status_and_only_known_safe_codes(self):
        api = receiver.ControlApi("http://local/doubao")
        for error, expected in (("preparation_mismatch", "preparation_mismatch"),
                                ("receiver_token_invalid", "receiver_token_invalid"),
                                ("private response details", "server_rejected"),
                                ({"secret": "not retained"}, "server_rejected")):
            response = Mock(returncode=0, stdout=json.dumps({"ok": False, "error": error}) + "\n409")
            with self.subTest(error=error), patch.object(receiver.subprocess, "run", return_value=response):
                with self.assertRaises(receiver.ControlApiError) as caught:
                    api.request("prepared", {})
                self.assertEqual(caught.exception.http_status, 409)
                self.assertEqual(caught.exception.error_code, expected)
                self.assertEqual(str(caught.exception), "server_rejected")

    def test_explicit_control_timeout_is_bounded(self):
        for value in (0, -1, 31, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                receiver.ControlApi("http://local", value)
        with patch.object(receiver.subprocess, "run", return_value=Mock(returncode=0, stdout='{"ok": true}\n200')) as run:
            receiver.ControlApi("http://local", 10).request("status")
            self.assertEqual(run.call_args.kwargs["timeout"], 11)

    def test_startup_retries_before_exclusive_lifecycle(self):
        api, stop = Mock(), threading.Event()
        calls = 0
        with patch.object(receiver, "exclusive_trial") as exclusive, \
                patch.object(receiver, "receive") as receive, \
                patch.object(receiver, "emit"), \
                patch.object(stop, "wait", return_value=False):
            def status(*_, **__):
                nonlocal calls
                calls += 1
                exclusive.assert_not_called()
                if calls == 1:
                    raise receiver.ApiError("transport_failed")
                return {"ok": True}
            api.request.side_effect = status
            receiver.run_when_online(api, "air", stop, 120)
            self.assertEqual(calls, 2)
            exclusive.assert_called_once()
            receive.assert_called_once_with(api, "air", stop, 120, 6.0)

    def test_stopping_failed_startup_leaves_daily_helper_untouched(self):
        api, stop = Mock(), threading.Event()
        def failed(*_, **__):
            stop.set()
            raise subprocess.TimeoutExpired("curl", 9)
        api.request.side_effect = failed
        with patch.object(receiver, "exclusive_trial") as exclusive, \
                patch.object(receiver, "receive") as receive, patch.object(receiver, "emit"):
            receiver.run_when_online(api, "air", stop)
            exclusive.assert_not_called()
            receive.assert_not_called()
        api.request.assert_called_once()

    def test_stop_during_successful_startup_does_not_pause_daily_helper(self):
        api, stop = Mock(), threading.Event()
        def ready(*_, **__):
            stop.set()
            return {"ok": True}
        api.request.side_effect = ready
        with patch.object(receiver, "exclusive_trial") as exclusive:
            receiver.run_when_online(api, "air", stop)
            exclusive.assert_not_called()

    def test_relay_fetch_and_ack_use_six_seconds(self):
        stop = threading.Event()
        states = iter([
            {"version": 1, "latest_archive": {"archive_id": 1, "text": "old"}},
            {"version": 2, "latest_archive": {"archive_id": 2, "text": "explicit"}},
        ])
        def acknowledged(*_):
            stop.set()
            return {"ok": True}
        with patch.object(receiver.relay, "fetch_state", side_effect=lambda *_: next(states)) as fetch, \
                patch.object(receiver.relay, "acknowledge_archive", side_effect=acknowledged) as ack, \
                patch.object(receiver.relay, "copy_to_clipboard"), \
                patch.object(receiver, "paste_matching", return_value="executed"), \
                patch.object(receiver, "emit"), patch.object(stop, "wait", return_value=False):
            receiver.relay_worker("http://local", "air", stop, threading.Lock())
        self.assertEqual(fetch.call_args.args[2], 6.0)
        self.assertEqual(ack.call_args.args[5], 6.0)


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.session = str(uuid4())
        self.prep = {"preparation_id": str(uuid4()), "sha256": hashlib.sha256(b"expected").hexdigest(), "remaining_ms": 60000}
        self.action = {"action_id": str(uuid4()), "session_id": self.session, "remaining_ms": 5000,
                       "preparation_id": self.prep["preparation_id"], "sha256": self.prep["sha256"]}
        self.paste = Mock(return_value="executed")
        self.watcher = Mock()
        self.watcher.prepare.return_value = True
        self.lock = threading.Lock()
        self.consumer = receiver.MatchConsumer(self.session, self.watcher, self.paste, self.lock, monotonic=lambda: 12)
        self.assertTrue(self.consumer.prepare(self.prep))

    def test_passive_preparation_never_pastes(self):
        self.paste.assert_not_called()
        self.watcher.prepare.assert_called_once_with(self.prep["sha256"])

    def test_unready_watcher_not_acknowledged(self):
        self.watcher.prepare.return_value = False
        self.assertFalse(self.consumer.prepare(self.prep))
        self.paste.assert_not_called()

    def test_matching_authorized_action_no_fixed_delay_and_rtt_deducted(self):
        self.assertEqual(self.consumer.consume(self.action, 11)[1:], ("executed", None))
        self.paste.assert_called_once_with(16, self.prep["sha256"])

    def test_duplicate_returns_original_ack_without_repaste(self):
        result = self.consumer.consume(self.action, 11)
        self.assertEqual(self.consumer.consume(self.action, 11), result)
        self.paste.assert_called_once()

    def test_wrong_preparation_or_hash_cannot_paste(self):
        for patch_fields in ({"sha256": "0" * 64}, {"preparation_id": str(uuid4())}):
            action = {**self.action, "action_id": str(uuid4()), **patch_fields}
            self.assertEqual(self.consumer.consume(action, 11)[1:], ("cancelled", "mismatch"))
        self.paste.assert_not_called()

    def test_invalid_session_and_text_body_rejected(self):
        for fields in ({"session_id": str(uuid4())}, {"text": "unexpected"}, {"sha256": "bad"}):
            self.assertIsNone(self.consumer.consume({**self.action, **fields}, 11))
        self.paste.assert_not_called()

    def test_timeout_and_focus_failure_never_use_relay(self):
        for reason in ("clipboard_timeout", "clipboard_changed", "focus_changed", "modifier_held", "unsafe_target"):
            self.paste.return_value = reason
            action = {**self.action, "action_id": str(uuid4())}
            self.assertEqual(self.consumer.consume(action, 11)[1:], ("cancelled", reason))
        self.assertEqual(self.paste.call_count, 5)

    def test_invalid_or_elapsed_budget_never_pastes(self):
        for remaining in (float("nan"), float("inf"), True, "5000", 5001, 0, -1, 999):
            action = {**self.action, "action_id": str(uuid4()), "remaining_ms": remaining}
            self.assertEqual(self.consumer.consume(action, 11)[1:], ("cancelled", "expired"))
        self.paste.assert_not_called()

    def test_uncertain_native_failure_consumed(self):
        self.paste.side_effect = RuntimeError("uncertain")
        result = self.consumer.consume(self.action, 11)
        self.assertEqual(result[1:], ("cancelled", "paste_failed"))
        self.assertEqual(self.consumer.consume(self.action, 11), result)
        self.paste.assert_called_once()

    def test_shared_lock_held_during_match_paste(self):
        self.paste.side_effect = lambda *_: "executed" if self.lock.locked() else "paste_failed"
        self.assertEqual(self.consumer.consume(self.action, 11)[1], "executed")
        self.assertFalse(self.lock.locked())


class WatcherTests(unittest.TestCase):
    def test_observer_primes_matching_and_mismatching_fingerprint_without_action(self):
        current = "a" * 64
        watcher = receiver.PassiveWatcher(lambda: current)
        try:
            self.assertTrue(watcher.prepare("b" * 64))
            self.assertFalse(watcher.matched)
            self.assertTrue(watcher.prepare(current))
            self.assertTrue(watcher.matched)
        finally:
            watcher.close()
        self.assertFalse(watcher.thread.is_alive())


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.apply = Mock(return_value="executed")
        self.ack = Mock(return_value={"ok": True})
        self.lock = threading.Lock()
        self.consumer = receiver.RelayConsumer(self.apply, self.ack, self.lock)

    def payload(self, version, archive_id, text="explicit send"):
        return {"version": version, "latest_archive": {"archive_id": archive_id, "text": text}}

    def test_startup_skips_old_archive_and_new_explicit_send_works(self):
        self.consumer.consume(self.payload(10, 5))
        self.apply.assert_not_called()
        self.consumer.consume(self.payload(11, 6))
        self.apply.assert_called_once_with("explicit send")
        self.consumer.retry_ack()
        self.ack.assert_called_once_with(6)

    def test_ack_failure_retries_only_ack_never_paste(self):
        self.consumer.consume(self.payload(10, 5))
        self.consumer.consume(self.payload(11, 6))
        self.ack.side_effect = [RuntimeError("offline"), {"ok": True}]
        self.consumer.retry_ack()
        self.consumer.consume(self.payload(11, 6))
        self.consumer.retry_ack()
        self.apply.assert_called_once()
        self.assertEqual(self.ack.call_count, 2)
        self.assertFalse(self.consumer.pending)

    def test_epoch_reset_seeds_snapshot_then_accepts_new_archive(self):
        self.consumer.consume(self.payload(10, 5))
        self.consumer.consume(self.payload(11, 6))
        self.consumer.consume(self.payload(1, 1, "stale from reset"))
        self.assertEqual(self.apply.call_count, 1)
        self.assertFalse(self.consumer.pending)
        self.consumer.consume(self.payload(2, 2, "new after reset"))
        self.assertEqual(self.apply.call_count, 2)
        self.apply.assert_called_with("new after reset")

    def test_native_failure_no_success_ack_and_no_repaste(self):
        self.apply.return_value = "unsafe_target"
        self.consumer.consume(self.payload(1, 1))
        self.consumer.consume(self.payload(2, 2))
        self.consumer.consume(self.payload(2, 2))
        self.consumer.retry_ack()
        self.apply.assert_called_once()
        self.ack.assert_not_called()

    def test_relay_shares_native_lock_with_match_consumer(self):
        self.apply.side_effect = lambda *_: "executed" if self.lock.locked() else "paste_failed"
        self.consumer.consume(self.payload(1, 1))
        self.consumer.consume(self.payload(2, 2))
        self.assertEqual(self.consumer.pending, {2})
        self.assertFalse(self.lock.locked())

    def test_copy_archive_or_live_draft_does_not_trigger(self):
        self.consumer.consume(self.payload(1, 1))
        self.consumer.consume({"version": 2, "text": "draft", "copy_history": [{"text": "copy only"}], "latest_archive": {"archive_id": 1, "text": "old"}})
        self.apply.assert_not_called()


class ReceiveProtocolTests(unittest.TestCase):
    def test_replaced_preparation_conflict_keeps_real_store_session_without_paste(self):
        from app.signal_trial import SignalError, SignalStore
        store = SignalStore(poll_seconds=0)
        first_id, latest_id = str(uuid4()), str(uuid4())
        stop = threading.Event()
        requests, sessions, prepared = [], [], []
        ready_before_stop = None

        def curl(command, **kwargs):
            nonlocal ready_before_stop
            operation = command[-1].rsplit("/", 1)[-1]
            payload = json.loads(kwargs["input"])
            requests.append(operation)
            code = 200
            try:
                if operation == "register":
                    result = store.register("air", receiver.PROTOCOL)
                    sessions.append(result["session_id"])
                    store.prepare("air", result["session_id"], first_id, "a" * 64)
                elif operation == "poll":
                    result = store.poll("air", payload["session_id"], payload["receiver_token"])
                    if result.get("preparation", {}).get("preparation_id") == first_id:
                        # Exact production race: B replaces A after A's poll
                        # response is generated, but before the Mac ACKs A.
                        store.prepare("air", payload["session_id"], latest_id, "b" * 64)
                elif operation == "prepared":
                    prepared.append(payload["preparation_id"])
                    result = store.prepared("air", payload["session_id"], payload["receiver_token"], payload["preparation_id"])
                    ready_before_stop = store.status("air")
                    stop.set()
                elif operation == "unregister":
                    result = store.unregister("air", payload["session_id"], payload["receiver_token"])
                else:
                    self.fail("unexpected native authorization: " + operation)
            except SignalError as error:
                result, code = {"ok": False, "error": error.reason}, int(error.status)
            return Mock(returncode=0, stdout=json.dumps(result) + "\n" + str(code))

        watcher = Mock()
        watcher.prepare.return_value = True
        with patch.object(receiver.subprocess, "run", side_effect=curl), \
                patch.object(receiver, "PassiveWatcher", return_value=watcher), \
                patch.object(receiver, "relay_worker"), \
                patch.object(receiver, "paste_matching") as paste, \
                patch.object(receiver, "emit") as emit:
            receiver.receive(receiver.ControlApi("http://local/doubao"), "air", stop, duration=3)
        self.assertEqual(requests, ["register", "poll", "prepared", "poll", "prepared", "unregister"])
        self.assertEqual(len(sessions), 1)
        self.assertEqual(prepared, [first_id, latest_id])
        self.assertEqual(ready_before_stop["session_id"], sessions[0])
        self.assertTrue(ready_before_stop["preparation"]["ready"])
        self.assertEqual(ready_before_stop["preparation"]["preparation_id"], latest_id)
        self.assertNotIn("disconnected", [call.args[0] for call in emit.call_args_list])
        self.assertIn("preparation_superseded", [call.args[0] for call in emit.call_args_list])
        paste.assert_not_called()

    def test_other_prepared_errors_still_disconnect_and_rotate_session(self):
        errors = [receiver.ControlApiError(403, "receiver_token_invalid"),
                  receiver.ControlApiError(409, "session_mismatch"),
                  receiver.ControlApiError(409, "receiver_unavailable"),
                  receiver.ControlApiError(409, "poll_in_progress"),
                  receiver.ControlApiError(500, "preparation_mismatch"),
                  receiver.ControlApiError(409, "unknown"),
                  receiver.ApiError("transport_failed"),
                  subprocess.TimeoutExpired("curl", 9)]
        for error in errors:
            with self.subTest(error=repr(error), status=getattr(error, "http_status", None)):
                stop = threading.Event()
                requests = []
                session, preparation_id = str(uuid4()), str(uuid4())
                api = Mock(base="http://local/doubao")
                def request(name, payload):
                    requests.append(name)
                    if name == "register":
                        if requests.count("register") == 2:
                            stop.set()
                        return {"session_id": session, "receiver_token": "token", "protocol": receiver.PROTOCOL}
                    if name == "poll":
                        return {"session_id": session, "preparation": {"preparation_id": preparation_id, "sha256": "a" * 64}}
                    if name == "prepared":
                        raise error
                    return {"ok": True}
                api.request.side_effect = request
                watcher = Mock()
                watcher.prepare.return_value = True
                with patch.object(receiver, "PassiveWatcher", return_value=watcher), \
                        patch.object(receiver, "relay_worker"), \
                        patch.object(receiver, "paste_matching") as paste, \
                        patch.object(receiver, "emit") as emit, \
                        patch.object(stop, "wait", return_value=False):
                    receiver.receive(api, "air", stop, duration=3)
                self.assertEqual(requests, ["register", "poll", "prepared", "unregister", "register", "unregister"])
                self.assertIn("disconnected", [call.args[0] for call in emit.call_args_list])
                paste.assert_not_called()

    def test_prepare_ack_precedes_click_and_only_click_dispatches(self):
        session, preparation_id, action_id = (str(uuid4()) for _ in range(3))
        fingerprint = "a" * 64
        stop_event = threading.Event()
        calls = []
        responses = iter([
            {"session_id": session, "preparation": {"preparation_id": preparation_id, "sha256": fingerprint, "remaining_ms": 60000}},
            {"session_id": session, "action": {"action_id": action_id, "session_id": session, "preparation_id": preparation_id, "sha256": fingerprint, "remaining_ms": 5000}},
        ])
        api = Mock(base="http://local/doubao")
        def request(name, payload):
            calls.append(name)
            if name == "register":
                self.assertEqual(payload["protocol"], receiver.PROTOCOL)
                return {"session_id": session, "receiver_token": "token", "protocol": receiver.PROTOCOL}
            if name == "poll":
                try:
                    return next(responses)
                except StopIteration:
                    stop_event.wait(0.01)
                    return {"session_id": session}
            if name == "ack":
                self.assertEqual(payload["status"], "executed")
                stop_event.set()
            return {"ok": True}
        api.request.side_effect = request
        watcher = Mock()
        watcher.prepare.return_value = True
        with patch.object(receiver, "PassiveWatcher", return_value=watcher), \
                patch.object(receiver, "relay_worker"), \
                patch.object(receiver, "paste_matching", return_value="executed") as paste, \
                patch.object(receiver, "emit"):
            receiver.receive(api, "air", stop_event)
        self.assertEqual(calls[:4], ["register", "poll", "prepared", "poll"])
        self.assertIn("ack", calls)
        self.assertEqual(calls[-1], "unregister")
        paste.assert_called_once()
        watcher.close.assert_called_once()


    def test_main_polls_during_native_wait_and_does_not_discard_next_action(self):
        session, preparation_id = str(uuid4()), str(uuid4())
        first_id, second_id = str(uuid4()), str(uuid4())
        fingerprint = "a" * 64
        native_started, release_native = threading.Event(), threading.Event()
        stop_event = threading.Event()
        counts = {"poll": 0, "ack": 0}
        api = Mock(base="http://local/doubao")
        def action(action_id):
            return {"action_id": action_id, "session_id": session, "preparation_id": preparation_id, "sha256": fingerprint, "remaining_ms": 5000}
        def request(name, payload):
            if name == "register":
                return {"session_id": session, "receiver_token": "token", "protocol": receiver.PROTOCOL}
            if name == "poll":
                counts["poll"] += 1
                if counts["poll"] == 1:
                    return {"session_id": session, "preparation": {"preparation_id": preparation_id, "sha256": fingerprint}}
                if counts["poll"] == 2:
                    return {"session_id": session, "action": action(first_id)}
                if counts["poll"] == 3:
                    self.assertTrue(native_started.wait(1))
                    release_native.set()
                    return {"session_id": session, "action": action(second_id)}
                stop_event.wait(0.01)
                return {"session_id": session}
            if name == "ack":
                counts["ack"] += 1
                if counts["ack"] == 2:
                    stop_event.set()
            return {"ok": True}
        def paste(*_):
            native_started.set()
            self.assertTrue(release_native.wait(1))
            return "executed"
        api.request.side_effect = request
        watcher = Mock()
        watcher.prepare.return_value = True
        with patch.object(receiver, "PassiveWatcher", return_value=watcher), \
                patch.object(receiver, "relay_worker"), \
                patch.object(receiver, "paste_matching", side_effect=paste) as native, \
                patch.object(receiver, "emit"):
            receiver.receive(api, "air", stop_event, duration=3)
        self.assertEqual(native.call_count, 2)
        self.assertEqual(counts["ack"], 2)
        self.assertGreaterEqual(counts["poll"], 3)


    def test_session_cap_drains_final_native_action_and_ack_before_rotation(self):
        session, preparation_id, action_id = (str(uuid4()) for _ in range(3))
        stop_event = threading.Event()
        native_started, release_native = threading.Event(), threading.Event()
        events = []
        poll_count = 0
        api = Mock(base="http://local/doubao")
        def request(name, payload):
            nonlocal poll_count
            if name == "register":
                return {"session_id": session, "receiver_token": "token", "protocol": receiver.PROTOCOL}
            if name == "poll":
                poll_count += 1
                if poll_count == 1:
                    return {"session_id": session, "preparation": {"preparation_id": preparation_id, "sha256": "a" * 64}}
                if poll_count == 2:
                    return {"session_id": session, "action": {"action_id": action_id, "session_id": session, "preparation_id": preparation_id, "sha256": "a" * 64, "remaining_ms": 5000}}
                self.assertTrue(native_started.wait(1))
                events.append("poll_while_final_action_running")
                release_native.set()
                return {"session_id": session}
            if name == "ack":
                self.assertEqual(payload["status"], "executed")
                events.append("ack")
            if name == "unregister":
                events.append("unregister")
                stop_event.set()
            return {"ok": True}
        def paste(deadline, fingerprint, stopped):
            native_started.set()
            self.assertTrue(release_native.wait(1))
            self.assertFalse(stopped(), "rotation must not cancel final authorized action")
            events.append("native_finished")
            return "executed"
        api.request.side_effect = request
        watcher = Mock()
        watcher.prepare.return_value = True
        with patch.object(receiver, "MAX_ACTIONS_PER_SESSION", 1), \
                patch.object(receiver, "PassiveWatcher", return_value=watcher), \
                patch.object(receiver, "relay_worker"), \
                patch.object(receiver, "paste_matching", side_effect=paste), \
                patch.object(receiver, "emit"):
            receiver.receive(api, "air", stop_event, duration=3)
        self.assertIn("poll_while_final_action_running", events)
        self.assertLess(events.index("native_finished"), events.index("ack"))
        self.assertLess(events.index("ack"), events.index("unregister"))


class NativeTests(unittest.TestCase):
    def test_native_diagnostics_are_bounded_and_do_not_change_failure_result(self):
        with patch.object(receiver, "emit") as emit:
            self.assertEqual(receiver.native_result("paste_failed|input_role|-1728", 0), "paste_failed")
            emit.assert_called_once_with("native_error", stage="input_role", error_code=-1728)
        for output in ["private clipboard body", "paste_failed|private input|-1728",
                       "paste_failed|input_role|private content", "paste_failed|input_role|-1728|secret"]:
            with self.subTest(output=output), patch.object(receiver, "emit") as emit:
                self.assertEqual(receiver.native_result(output, 0), "paste_failed")
                emit.assert_called_once_with("native_error", stage="invalid_result")
        with patch.object(receiver, "emit") as emit:
            self.assertEqual(receiver.native_result("executed", 0), "executed")
            emit.assert_not_called()
            self.assertEqual(receiver.native_result("secret", 1), "paste_failed")
            emit.assert_called_once_with("native_error", stage="process_exit", exit_code=1)

    @unittest.skipUnless(sys.platform == "darwin", "AppleScript compile requires macOS")
    def test_native_guard_compiles_without_side_effect(self):
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run(["/usr/bin/osacompile", "-o", str(Path(temp) / "guard.scpt"), str(SCRIPTS / "prearmed_paste.applescript")], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_expired_native_budget_does_not_launch(self):
        with patch.object(receiver.subprocess, "Popen") as popen:
            self.assertEqual(receiver.paste_matching(time.monotonic() - 1, "a" * 64), "expired")
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
