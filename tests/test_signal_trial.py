from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
from http.server import ThreadingHTTPServer

from app import server
from app.signal_trial import ACK_REASONS, SignalError, SignalStore


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class SignalStoreTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = SignalStore(clock=self.clock, poll_seconds=0)
        self.room = "signal-trial-test"

    def register(self):
        self.registration = self.store.register(self.room)
        self.session = self.registration["session_id"]
        self.token = self.registration["receiver_token"]
        return self.registration

    def action(self, action_id=None):
        action_id = action_id or str(uuid4())
        return self.store.action(self.room, self.session, action_id)

    def poll(self):
        return self.store.poll(self.room, self.session, self.token)

    def assert_error(self, reason, function, *args):
        with self.assertRaises(SignalError) as caught:
            function(*args)
        self.assertEqual(caught.exception.reason, reason)

    def test_no_receiver_and_status_does_not_allocate_rooms(self):
        self.assertEqual(self.store.status(self.room), {
            "ok": True, "ready": False, "session_id": None, "action": None,
        })
        self.assertEqual(self.store.rooms, {})
        self.assert_error("receiver_unavailable", self.store.action,
                          self.room, str(uuid4()), str(uuid4()))

    def test_single_receiver_lease_and_old_session_cannot_replay(self):
        self.register()
        first_session, first_token = self.session, self.token
        first_action = self.action()["action"]["action_id"]
        self.assert_error("receiver_already_registered", self.store.register, self.room)
        self.clock.now += 6
        self.assertFalse(self.store.status(self.room)["ready"])
        self.register()
        self.assertNotEqual(self.session, first_session)
        self.assert_error("session_mismatch", self.store.action, self.room, first_session, first_action)
        self.assert_error("session_mismatch", self.store.poll, self.room, first_session, first_token)
        self.assertIsNone(self.poll()["action"])

    def test_consumed_before_response_and_never_replayed_on_reconnect(self):
        self.register()
        action_id = self.action()["action"]["action_id"]
        delivery = self.poll()["action"]
        self.assertEqual(set(delivery), {"action_id", "session_id", "remaining_ms"})
        self.assertEqual(delivery["action_id"], action_id)
        self.assertGreater(delivery["remaining_ms"], 0)
        # Discarding a response or reconnecting must not repeat the command.
        self.assertIsNone(self.poll()["action"])
        self.assertEqual(self.action(action_id)["action"]["status"], "delivered")
        self.assertIsNone(self.poll()["action"])
        self.assert_error("action_in_progress", self.action)

    def test_duplicate_posts_and_ack_are_idempotent(self):
        self.register()
        accepted = self.action()
        action_id = accepted["action"]["action_id"]
        self.assertEqual(self.action(action_id), accepted)
        self.poll()
        result = self.store.ack(self.room, self.session, self.token, action_id, "executed")
        self.assertEqual(result["action"]["status"], "executed")
        self.assertEqual(self.store.ack(self.room, self.session, self.token, action_id, "executed"), result)
        self.assertEqual(self.action(action_id)["action"], result["action"])
        self.assertIsNone(self.poll()["action"])
        self.assertEqual(self.action()["action"]["status"], "accepted")

    def test_expiry_on_status_before_and_after_delivery(self):
        for delivered in (False, True):
            with self.subTest(delivered=delivered):
                self.store = SignalStore(clock=self.clock, poll_seconds=0)
                self.register()
                action_id = self.action()["action"]["action_id"]
                if delivered:
                    self.poll()
                self.clock.now += 5
                status = self.store.status(self.room, action_id)
                self.assertTrue(status["ready"])
                self.assertEqual(status["action"]["status"], "expired")
                self.assertEqual(self.action(action_id)["action"]["status"], "expired")
                self.assertIsNone(self.poll()["action"])
                self.assert_error("action_already_finished", self.store.ack,
                                  self.room, self.session, self.token, action_id, "executed")

    def test_cancelled_action_stays_cancelled_and_pending_cannot_ack(self):
        self.register()
        action_id = self.action()["action"]["action_id"]
        self.assert_error("action_not_delivered", self.store.ack,
                          self.room, self.session, self.token, action_id, "executed")
        self.poll()
        self.store.ack(self.room, self.session, self.token, action_id, "cancelled", "focus_changed")
        self.assertEqual(self.action(action_id)["action"], {
            "action_id": action_id, "status": "cancelled", "reason": "focus_changed",
        })
        self.assertIsNone(self.poll()["action"])

    def test_unregister_invalidates_pending_and_does_not_renew_on_ack(self):
        self.register()
        action_id = self.action()["action"]["action_id"]
        self.store.unregister(self.room, self.session, self.token)
        status = self.store.status(self.room, action_id)
        self.assertFalse(status["ready"])
        self.assertEqual(status["action"]["reason"], "receiver_unregistered")
        self.assert_error("receiver_unavailable", self.poll)
        self.register()
        self.clock.now += 4
        action_id = self.action()["action"]["action_id"]
        self.poll()
        self.clock.now += 2
        self.store.ack(self.room, self.session, self.token, action_id, "executed")
        self.clock.now += 4
        self.assertFalse(self.store.status(self.room)["ready"])

    def test_receiver_token_and_room_isolation(self):
        self.register()
        action_id = self.action()["action"]["action_id"]
        self.assert_error("receiver_token_invalid", self.store.poll, self.room, self.session, "x" * 43)
        self.assert_error("receiver_token_invalid", self.store.unregister, self.room, self.session, "x" * 43)
        self.assert_error("receiver_token_invalid", self.store.ack, self.room, self.session, "x" * 43, action_id, "executed")
        other = self.store.register("another-room")
        self.assertIsNone(self.store.poll("another-room", other["session_id"], other["receiver_token"])["action"])
        self.assertEqual(self.poll()["action"]["action_id"], action_id)
        self.assertNotIn("receiver_token", json.dumps(self.store.status(self.room)))
        self.assertNotIn(self.token, json.dumps(self.store.status(self.room)))

    def test_action_cap_does_not_evict_deduplication_ids(self):
        self.store = SignalStore(clock=self.clock, poll_seconds=0, max_actions=2)
        self.register()
        completed = []
        for _ in range(2):
            action_id = self.action()["action"]["action_id"]
            self.poll()
            self.store.ack(self.room, self.session, self.token, action_id, "executed")
            completed.append(action_id)
        self.assert_error("action_limit_reached", self.action)
        for action_id in completed:
            self.assertEqual(self.action(action_id)["action"]["status"], "executed")
        self.store.unregister(self.room, self.session, self.token)
        self.register()
        self.assertEqual(self.action()["action"]["status"], "accepted")

    def test_room_store_bounded_and_active_receivers_never_evicted(self):
        self.store = SignalStore(clock=self.clock, poll_seconds=0, max_rooms=2)
        self.register()
        self.store.register("other")
        self.assert_error("signal_capacity_reached", self.store.register, "third")
        self.clock.now += 6
        self.store.register("third")
        self.assertEqual(len(self.store.rooms), 2)

    def test_one_long_poll_at_a_time_and_unregister_wakes_it(self):
        self.store = SignalStore(poll_seconds=1)
        self.register()
        with ThreadPoolExecutor(max_workers=2) as pool:
            waiting = pool.submit(self.poll)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                with self.store.changed:
                    if self.store.rooms[self.room].polling:
                        break
                time.sleep(0.005)
            else:
                self.fail("long poll did not start")
            self.assert_error("poll_in_progress", self.poll)
            action_id = self.action()["action"]["action_id"]
            self.assertEqual(waiting.result(timeout=2)["action"]["action_id"], action_id)
            self.store.ack(self.room, self.session, self.token, action_id, "executed")
            waiting = pool.submit(self.poll)
            self.store.unregister(self.room, self.session, self.token)
            with self.assertRaises(SignalError):
                waiting.result(timeout=2)


class ClipboardMatchTests(unittest.TestCase):
    setUp = SignalStoreTests.setUp
    register = SignalStoreTests.register
    action = SignalStoreTests.action
    poll = SignalStoreTests.poll
    assert_error = SignalStoreTests.assert_error
    def match_session(self):
        self.registration = self.store.register(self.room, "clipboard-match-v1")
        self.session = self.registration["session_id"]
        self.token = self.registration["receiver_token"]
        self.preparation_id = str(uuid4())
        self.sha = "a" * 64
        return self.store.prepare(self.room, self.session, self.preparation_id, self.sha)

    def prepare_ready(self):
        self.match_session()
        self.poll()
        self.store.prepared(self.room, self.session, self.token, self.preparation_id)

    def test_preparation_never_authorizes_action_and_only_receiver_can_ready(self):
        self.match_session()
        self.assertIsNone(self.poll()["action"])
        self.assertEqual(self.poll()["preparation"]["sha256"], self.sha)
        self.assertFalse(self.store.status(self.room)["preparation"]["ready"])
        self.assert_error("receiver_token_invalid", self.store.prepared,
                          self.room, self.session, "x" * 43, self.preparation_id)
        self.assert_error("preparation_not_ready", self.store.action,
                          self.room, self.session, str(uuid4()), self.preparation_id)
        self.store.prepared(self.room, self.session, self.token, self.preparation_id)
        self.assertTrue(self.store.status(self.room)["preparation"]["ready"])
        self.assertIsNone(self.poll()["action"])

    def test_action_is_explicit_authorization_and_freezes_hash(self):
        self.prepare_ready()
        self.assert_error("preparation_mismatch", self.action)
        action_id = str(uuid4())
        self.store.action(self.room, self.session, action_id, self.preparation_id)
        delivery = self.poll()["action"]
        self.assertEqual(delivery["sha256"], self.sha)
        self.assertEqual(delivery["preparation_id"], self.preparation_id)
        self.assert_error("action_in_progress", self.store.prepare,
                          self.room, self.session, str(uuid4()), "b" * 64)
        self.assertIsNone(self.poll()["action"])
        self.store.ack(self.room, self.session, self.token, action_id, "executed")
        self.assertEqual(self.store.action(self.room, self.session, action_id, self.preparation_id)["action"]["status"], "executed")
        self.assert_error("action_id_conflict", self.store.action,
                          self.room, self.session, action_id, str(uuid4()))
        self.assertIsNone(self.poll()["action"])

    def test_replaced_preparation_invalidates_old_ready_and_action(self):
        self.prepare_ready()
        old = self.preparation_id
        self.store.prepare(self.room, self.session, str(uuid4()), "b" * 64)
        self.assert_error("preparation_mismatch", self.store.prepared,
                          self.room, self.session, self.token, old)
        self.assert_error("preparation_mismatch", self.store.action,
                          self.room, self.session, str(uuid4()), old)
        self.assertFalse(self.store.status(self.room)["preparation"]["ready"])

    def test_preparation_conflict_refresh_and_expiry_without_memory_growth(self):
        self.prepare_ready()
        self.assert_error("preparation_id_conflict", self.store.prepare,
                          self.room, self.session, self.preparation_id, "b" * 64)
        for _ in range(12):
            self.clock.now += 4
            self.poll()
        refreshed = self.store.prepare(self.room, self.session, self.preparation_id, self.sha)
        self.assertEqual(refreshed["preparation"]["remaining_ms"], 60000)
        self.assertTrue(refreshed["preparation"]["ready"])
        for _ in range(15):
            self.clock.now += 4
            self.poll()
        self.assertIsNone(self.store.status(self.room)["preparation"])
        self.assert_error("preparation_mismatch", self.store.action,
                          self.room, self.session, str(uuid4()), self.preparation_id)
        for _ in range(1000):
            self.store.prepare(self.room, self.session, str(uuid4()), self.sha)
        self.assertEqual(len(self.store.rooms), 1)
        self.assertEqual(len(self.store.rooms[self.room].actions), 0)

    def test_legacy_session_cannot_prepare_and_bad_hash_protocol_fail_closed(self):
        for invalid in (None, [], {}, "unknown"):
            self.assert_error("invalid_protocol", self.store.register, self.room, invalid)
        self.register()
        self.assert_error("protocol_mismatch", self.store.prepare,
                          self.room, self.session, str(uuid4()), "a" * 64)
        self.assert_error("protocol_mismatch", self.store.action,
                          self.room, self.session, str(uuid4()), str(uuid4()))
        for invalid in (None, [], "A" * 64, "a" * 63, "a" * 65):
            self.assert_error("invalid_sha256", self.store.prepare,
                              self.room, self.session, str(uuid4()), invalid)

    def test_match_timeout_ack_is_terminal_without_retry(self):
        self.prepare_ready()
        action_id = str(uuid4())
        self.store.action(self.room, self.session, action_id, self.preparation_id)
        self.poll()
        self.store.ack(self.room, self.session, self.token, action_id, "cancelled", "clipboard_timeout")
        self.assertEqual(self.store.status(self.room, action_id)["action"]["reason"], "clipboard_timeout")
        self.assertIsNone(self.poll()["action"])


class SignalApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.RelayHandler)
        self.httpd.daemon_threads = True
        self.httpd.store = server.RoomStore(archive_idle_seconds=0.5, claim_ttl_seconds=45)
        self.httpd.copy_store = server.CopyArchiveStore(Path(self.temp.name) / "copy.sqlite3")
        self.httpd.default_room = "unused"
        self.httpd.base_path = "/doubao"
        self.httpd.local_ip = "127.0.0.1"
        self.httpd.signal_store = SignalStore(poll_seconds=0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path, payload=None, *, raw=None, content_type="application/json", method=None):
        conn = http.client.HTTPConnection(*self.httpd.server_address, timeout=4)
        try:
            body = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
            conn.request(method or ("POST" if body is not None else "GET"), "/doubao" + path,
                         body, {"Content-Type": content_type})
            response = conn.getresponse()
            result = response.read()
            return response.status, json.loads(result) if response.getheader("Content-Type", "").startswith("application/json") else result
        finally:
            conn.close()

    def register(self):
        status, result = self.request("/api/signal/register", {"room_id": "test-room"})
        self.assertEqual(status, 200)
        return {"room_id": "test-room", "session_id": result["session_id"], "receiver_token": result["receiver_token"]}

    def test_match_api_handshake_strict_fields_and_old_client_fails_closed(self):
        status, result = self.request("/api/signal/register", {"room_id": "match", "protocol": "clipboard-match-v1"})
        self.assertEqual(status, 200)
        receiver = {"room_id": "match", "session_id": result["session_id"], "receiver_token": result["receiver_token"]}
        prep = {"room_id": "match", "session_id": result["session_id"], "preparation_id": str(uuid4()), "sha256": "a" * 64}
        self.assertEqual(self.request("/api/signal/prepare", {**prep, "text": "must never enter"})[0], 400)
        for digest in (None, [], "bad", "A" * 64, "a" * 65):
            with self.subTest(digest=digest):
                invalid = self.request("/api/signal/prepare", {**prep, "sha256": digest})
                self.assertEqual(invalid[0], 400)
                self.assertEqual(invalid[1]["error"], "invalid_sha256")
        self.assertEqual(self.request("/api/signal/prepare", {**prep, "preparation_id": "invalid"})[0], 400)
        self.assertEqual(self.request("/api/signal/prepare", prep)[0], 200)
        self.assertEqual(self.request("/api/signal/poll", receiver)[1]["preparation"]["preparation_id"], prep["preparation_id"])
        self.assertEqual(self.request("/api/signal/prepared", {**receiver, "preparation_id": prep["preparation_id"]})[0], 200)
        action = {"room_id": "match", "session_id": result["session_id"], "action_id": str(uuid4())}
        self.assertEqual(self.request("/api/signal/action", action)[1]["error"], "preparation_mismatch")
        self.assertEqual(self.request("/api/signal/action", {**action, "preparation_id": prep["preparation_id"]})[0], 200)
        self.assertEqual(self.request("/api/signal/poll", receiver)[1]["action"]["sha256"], prep["sha256"])
        self.assertEqual(self.request("/api/signal/ack", {**receiver, "action_id": action["action_id"], "status": "cancelled", "reason": "clipboard_timeout"})[0], 200)

    def test_api_lifecycle_without_text_archive_or_relay_leakage(self):
        before = self.httpd.store.get("test-room").payload()
        watcher = self.httpd.store.subscribe("test-room")
        receiver = self.register()
        action = {"room_id": "test-room", "session_id": receiver["session_id"], "action_id": str(uuid4())}
        status, accepted = self.request("/api/signal/action", action)
        self.assertEqual(status, 200)
        self.assertEqual(accepted["action"]["status"], "accepted")
        status, delivery = self.request("/api/signal/poll", receiver)
        self.assertEqual(status, 200)
        self.assertEqual(delivery["action"]["action_id"], action["action_id"])
        self.assertNotIn("receiver_token", json.dumps(delivery))
        self.assertEqual(self.request("/api/signal/poll", receiver)[1]["action"], None)
        status, ack = self.request("/api/signal/ack", {**receiver, "action_id": action["action_id"], "status": "executed"})
        self.assertEqual(status, 200)
        self.assertEqual(ack["action"]["status"], "executed")
        status, readback = self.request("/api/signal/status?room=test-room&action_id=" + action["action_id"])
        self.assertEqual(status, 200)
        self.assertEqual(readback["action"]["status"], "executed")
        self.assertNotIn(receiver["receiver_token"], json.dumps(readback))
        self.assertEqual(self.httpd.store.get("test-room").payload(), before)
        self.assertFalse(self.httpd.copy_store.path.exists())
        with self.assertRaises(queue.Empty):
            watcher.get_nowait()
        self.assertEqual(self.request("/api/signal/unregister", receiver)[0], 200)
        self.assertFalse(self.request("/api/signal/status?room=test-room")[1]["ready"])

    def test_every_endpoint_rejects_unknown_body_fields_and_does_not_echo_text(self):
        receiver = self.register()
        action_id = str(uuid4())
        payloads = {
            "register": {"room_id": "other"},
            "action": {"room_id": "test-room", "session_id": receiver["session_id"], "action_id": action_id},
            "poll": receiver,
            "ack": {**receiver, "action_id": action_id, "status": "cancelled"},
            "unregister": receiver,
        }
        for endpoint, payload in payloads.items():
            for key in ("text", "digest", "unexpected"):
                with self.subTest(endpoint=endpoint, key=key):
                    status, result = self.request("/api/signal/" + endpoint, {**payload, key: "private-test-body"})
                    self.assertEqual(status, 400)
                    self.assertEqual(result["error"], "invalid_fields")
                    self.assertNotIn("private-test-body", json.dumps(result))

    def test_rejects_oversize_malformed_non_object_and_duplicate_keys(self):
        cases = [
            (b"x" * 4097, "application/json", 413, "body_too_large"),
            (b"{", "application/json", 400, "invalid_json"),
            (b"[]", "application/json", 400, "json_object_required"),
            (b'{"room_id":"a","room_id":"b"}', "application/json", 400, "duplicate_json_key"),
            (b'{"room_id":"a"}', "text/plain", 415, "content_type_must_be_json"),
        ]
        for raw, content_type, expected, reason in cases:
            with self.subTest(reason=reason):
                status, result = self.request("/api/signal/register", raw=raw, content_type=content_type)
                self.assertEqual(status, expected)
                self.assertEqual(result["error"], reason)

    def test_required_fields_invalid_uuid_room_token_and_ack_reason(self):
        receiver = self.register()
        cases = [
            ("register", {}, "invalid_fields"),
            ("register", {"room_id": " "}, "invalid_room_id"),
            ("register", {"room_id": "\ud800"}, "invalid_room_id"),
            ("action", {"room_id": "test-room", "session_id": [], "action_id": str(uuid4())}, "invalid_session_id"),
            ("poll", {**receiver, "receiver_token": None}, "invalid_receiver_token"),
            ("ack", {**receiver, "action_id": str(uuid4()), "status": []}, "invalid_ack_status"),
            ("ack", {**receiver, "action_id": str(uuid4()), "status": "cancelled", "reason": "private text"}, "invalid_ack_reason"),
            ("ack", {**receiver, "action_id": str(uuid4()), "status": "executed", "reason": "focus_changed"}, "executed_cannot_have_reason"),
        ]
        for endpoint, payload, reason in cases:
            with self.subTest(reason=reason):
                status, result = self.request("/api/signal/" + endpoint, payload)
                self.assertEqual(status, 400)
                self.assertEqual(result["error"], reason)
        self.assertIn("focus_changed", ACK_REASONS)

    def test_status_query_strict_and_no_token_response(self):
        for query in ("", "room=a&room=b", "room=a&text=body", "room=a&action_id=bad"):
            with self.subTest(query=query):
                self.assertEqual(self.request("/api/signal/status?" + query)[0], 400)
        receiver = self.register()
        result = self.request("/api/signal/status?room=test-room")[1]
        self.assertTrue(result["ready"])
        self.assertNotIn(receiver["receiver_token"], json.dumps(result))

    def test_lazy_initialization_is_shared_and_does_not_require_main(self):
        del self.httpd.signal_store
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.request("/api/signal/register", {"room_id": "same-room"}), range(4)))
        self.assertEqual(sorted(status for status, _ in responses), [200, 409, 409, 409])

    def test_trial_static_routes_replace_base_path_and_leave_existing_routes(self):
        static = Path(self.temp.name) / "static"
        static.mkdir()
        (static / "signal-trial.html").write_text('<script src="__BASE_PATH__/assets/signal-trial.js"></script>')
        (static / "signal-trial.js").write_text("window.signalTrial = true;")
        (static / "signal-trial.css").write_text("body { color: black; }")
        with patch.object(server, "STATIC_ROOT", static):
            status, body = self.request("/signal/test-room")
            self.assertEqual(status, 200)
            self.assertIn(b"/doubao/assets/signal-trial.js", body)
            self.assertNotIn(b"__BASE_PATH__", body)
            self.assertEqual(self.request("/assets/signal-trial.js")[0], 200)
            self.assertEqual(self.request("/assets/signal-trial.css")[0], 200)
        self.assertEqual(self.request("/api/ping")[0], 200)
        self.assertEqual(self.request("/api/helper-state?room_id=test-room")[0], 200)


if __name__ == "__main__":
    unittest.main()
