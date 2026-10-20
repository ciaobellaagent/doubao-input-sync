from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote
from uuid import uuid4

from app import attachments, server
from app.signal_trial import SignalError, SignalStore


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = attachments.AttachmentStore(Path(self.temp.name) / "archives.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def upload(self, content=b"hello", *, attachment_id=None, room="room", filename="file.txt", **kwargs):
        return self.archive.upload(room, attachment_id or str(uuid4()), filename,
                                   io.BytesIO(content), len(content), **kwargs)

    def assert_error(self, reason, function, *args, **kwargs):
        with self.assertRaises(SignalError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.reason, reason)


class AttachmentStoreTests(AttachmentTests):
    def test_bytes_are_durable_private_and_room_isolated(self):
        payload = b"\x89PNG\r\n\x1a\n" + os.urandom(200000)
        item = self.upload(payload, filename="/secret/../picture.png")
        self.assertEqual(item["filename"], "picture.png")
        self.assertEqual(item["media_type"], "image/png")
        self.assertEqual(item["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(item["size"], len(payload))
        self.assert_error("attachment_not_found", self.archive.metadata, "other", item["attachment_id"])
        restored = attachments.AttachmentStore(Path(self.temp.name) / "archives.sqlite3")
        metadata, source = restored.open_blob("room", item["attachment_id"])
        with source:
            self.assertEqual(source.read(), payload)
        self.assertEqual(metadata, item)
        self.assertEqual(stat.S_IMODE(self.archive.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.archive.path.stat().st_mode), 0o600)
        for blob in self.archive.root.glob("*.blob"):
            self.assertEqual(stat.S_IMODE(blob.stat().st_mode), 0o600)
            self.assertNotIn("picture", blob.name)

    def test_zero_byte_files_and_explicit_delete(self):
        item = self.upload(b"", filename="empty.txt")
        self.assertEqual(item["size"], 0)
        self.assertEqual(item["media_type"], "application/octet-stream")
        self.archive.delete("room", item["attachment_id"])
        self.assertEqual(self.archive.items("room"), [])
        self.assertEqual(list(self.archive.root.glob("*.blob")), [])

    def test_duplicate_content_is_idempotent_changed_content_conflicts(self):
        item = self.upload()
        self.assertEqual(self.upload(attachment_id=item["attachment_id"], filename="renamed.txt"), item)
        self.assert_error("attachment_id_conflict", self.upload, b"other", attachment_id=item["attachment_id"])
        self.assertEqual(len(self.archive.items("room")), 1)
        self.assertEqual(len(list(self.archive.root.glob("*.blob"))), 1)
        other = self.upload(room="other", attachment_id=item["attachment_id"], content=b"other")
        self.assertNotEqual(other["sha256"], item["sha256"])

    def test_sha_mismatch_and_truncated_stream_leave_no_archive(self):
        self.assert_error("hash_mismatch", self.upload, expected_sha256="0" * 64)
        self.assert_error("incomplete_body", self.archive.upload, "room", str(uuid4()), "x",
                          io.BytesIO(b"x"), 5)
        self.assertEqual(self.archive.items("room"), [])
        self.assertEqual(list(self.archive.root.glob("*.blob")), [])

    def test_read_verifies_stored_content_hash(self):
        item = self.upload()
        next(self.archive.root.glob("*.blob")).write_bytes(b"other")
        self.assert_error("attachment_integrity_failed", self.archive.open_blob, "room", item["attachment_id"])

    def test_file_total_and_count_limits_never_delete_archives(self):
        self.archive = attachments.AttachmentStore(Path(self.temp.name) / "archives.sqlite3",
                                                   max_file_bytes=5, max_total_bytes=8, max_files=2)
        first = self.upload(b"12345")
        self.assert_error("attachment_too_large", self.upload, b"123456")
        self.assert_error("attachment_quota_reached", self.upload, b"1234")
        self.upload(b"123")
        self.assert_error("attachment_quota_reached", self.upload, b"")
        self.assertEqual(self.upload(b"12345", attachment_id=first["attachment_id"]), first)
        self.assertEqual(len(self.archive.items("room")), 2)

    def test_large_input_is_read_in_bounded_chunks_and_limit_rejects_before_read(self):
        class BoundedReader(io.BytesIO):
            def read(inner, size=-1):
                self.assertGreater(size, 0)
                self.assertLessEqual(size, attachments.CHUNK_BYTES)
                return super().read(size)
        body = b"x" * (attachments.CHUNK_BYTES * 3 + 7)
        self.archive.upload("room", str(uuid4()), "data.bin", BoundedReader(body), len(body))
        class NeverRead:
            def read(self, _):
                raise AssertionError("oversized uploads must be rejected before reading")
        self.assert_error("attachment_too_large", self.archive.upload, "room", str(uuid4()), "large.bin",
                          NeverRead(), attachments.MAX_FILE_BYTES + 1)

    def test_quota_checks_are_atomic_for_parallel_uploads(self):
        self.archive = attachments.AttachmentStore(Path(self.temp.name) / "archives.sqlite3",
                                                   max_total_bytes=5, max_files=1)
        def write(_):
            try:
                self.upload(b"12345")
                return "ok"
            except SignalError as exc:
                return exc.reason
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(write, range(4)))
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("attachment_quota_reached"), 3)

    def test_four_streams_are_bounded_without_a_transaction_or_metadata_lock(self):
        release = threading.Event()
        entered = [threading.Event() for _ in range(4)]
        class SlowSource(io.BytesIO):
            def __init__(self, index):
                super().__init__(b"x")
                self.index = index
            def read(inner, size):
                entered[inner.index].set()
                if not release.wait(5):
                    raise AssertionError("test stream was never released")
                return super().read(size)
        with ThreadPoolExecutor(max_workers=5) as pool:
            uploads = [pool.submit(self.archive.upload, "room", str(uuid4()), "a.bin", SlowSource(i), 1)
                       for i in range(4)]
            try:
                for event in entered:
                    self.assertTrue(event.wait(2))
                self.assert_error("attachment_upload_busy", self.upload, b"fifth")
                self.assertEqual(pool.submit(self.archive.items, "room").result(timeout=1), [])
                # No BEGIN IMMEDIATE may span the slow body read.
                connection = sqlite3.connect(str(self.archive.path), timeout=0.1)
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.rollback()
                finally:
                    connection.close()
            finally:
                release.set()
            for upload in uploads:
                upload.result(timeout=2)
        self.assertEqual(len(self.archive.items("room")), 4)
        self.assertEqual(len(list(self.archive.root.glob("*.blob"))), 4)

    def test_concurrent_streams_recheck_quota_at_commit_and_clean_rejected_blobs(self):
        self.archive = attachments.AttachmentStore(Path(self.temp.name) / "archives.sqlite3",
                                                   max_total_bytes=5, max_files=1)
        release = threading.Barrier(4)
        class RacingSource(io.BytesIO):
            def read(inner, size):
                release.wait(timeout=3)
                return super().read(size)
        def upload(_):
            try:
                self.archive.upload("room", str(uuid4()), "x.bin", RacingSource(b"12345"), 5)
                return "ok"
            except SignalError as exc:
                return exc.reason
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(upload, range(4)))
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("attachment_quota_reached"), 3)
        self.assertEqual(len(self.archive.items("room")), 1)
        self.assertEqual(len(list(self.archive.root.glob("*.blob"))), 1)

    def test_upload_absolute_budget_rejects_a_continuously_active_slow_stream(self):
        now = [100.0]
        class SlowSource:
            def read(inner, size):
                now[0] += 91
                return b"x"
        with patch.object(attachments.time, "monotonic", side_effect=lambda: now[0]):
            self.assert_error("incomplete_body", self.archive.upload, "room", str(uuid4()),
                              "slow.bin", SlowSource(), 1)
        self.assertEqual(self.archive.items("room"), [])
        self.assertEqual(list(self.archive.root.glob("*.blob")), [])

    def test_disk_fsync_and_download_hashing_do_not_hold_metadata_lock(self):
        entered, release = threading.Event(), threading.Event()
        real_fsync = attachments.os.fsync
        def slow_fsync(fd):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test fsync was never released")
            return real_fsync(fd)
        with ThreadPoolExecutor(max_workers=2) as pool:
            with patch.object(attachments.os, "fsync", side_effect=slow_fsync):
                upload = pool.submit(self.upload)
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertEqual(pool.submit(self.archive.items, "room").result(timeout=1), [])
                finally:
                    release.set()
                item = upload.result(timeout=2)
            entered.clear()
            release.clear()
            real_fdopen = attachments.os.fdopen
            class SlowReader:
                def __init__(inner, source):
                    inner.source = source
                def read(inner, size):
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("test hash read was never released")
                    return inner.source.read(size)
                def seek(inner, offset):
                    return inner.source.seek(offset)
                def close(inner):
                    return inner.source.close()
            def slow_fdopen(fd, mode):
                source = real_fdopen(fd, mode)
                return SlowReader(source) if mode == "rb" else source
            with patch.object(attachments.os, "fdopen", side_effect=slow_fdopen):
                download = pool.submit(self.archive.open_blob, "room", item["attachment_id"])
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertEqual(pool.submit(self.archive.items, "room").result(timeout=1), [item])
                finally:
                    release.set()
                _, source = download.result(timeout=2)
                source.close()

    def test_filename_and_magic_detection_do_not_trust_client_type(self):
        for source, expected in [(".env", "attachment.env"), ("notes 12:00.txt", "notes 12_00.txt"), ("../../a.txt", "a.txt"),
                                 ("C:\\dir\\a.txt", "a.txt"), (" a\x00\nb.txt ", "ab.txt"),
                                 ("...", "attachment.bin")]:
            self.assertEqual(attachments.clean_filename(source), expected)
        long_name = attachments.clean_filename("图" * 200 + ".jpeg")
        self.assertLessEqual(len(long_name.encode("utf-8")), 220)
        self.assertTrue(long_name.endswith(".jpeg"))
        samples = [(b"\xff\xd8\xffabc", "image/jpeg"), (b"GIF89a", "image/gif"),
                   (b"RIFF1234WEBP", "image/webp"), (b"0000ftypheic", "image/heic"),
                   (b"0000ftypavif", "image/avif"), (b"<svg onload='x'>", "application/octet-stream"),
                   (b"<script>x</script>", "application/octet-stream")]
        for data, media_type in samples:
            self.assertEqual(attachments.detect_media_type(data), media_type)

    def test_symlink_storage_is_rejected(self):
        destination = Path(self.temp.name) / "elsewhere"
        destination.mkdir()
        self.archive.root.symlink_to(destination, target_is_directory=True)
        self.assert_error("attachment_storage_unavailable", self.upload)
        self.assertEqual(list(destination.iterdir()), [])


class AttachmentHubTests(AttachmentTests):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.signals = SignalStore(clock=self.clock, poll_seconds=0)
        self.hub = attachments.AttachmentHub(self.archive, self.signals, clock=self.clock)
        self.registration = self.signals.register("room")
        self.session = self.registration["session_id"]
        self.token = self.registration["receiver_token"]
        self.item = self.upload()
        self.preparation_id = str(uuid4())

    def poll(self, token=None):
        return self.hub.poll("room", self.session, token or self.token)

    def prepare(self):
        return self.hub.prepare("room", self.session, self.preparation_id, self.item["attachment_id"])

    def ready(self):
        self.poll()
        self.prepare()
        return self.hub.prepared("room", self.session, self.token, self.preparation_id, "ready")

    def action(self, action_id=None):
        return self.hub.action("room", self.session, self.preparation_id, action_id or str(uuid4()))

    def test_upload_is_archive_only_old_receivers_are_not_ready(self):
        status = self.hub.status("room")
        self.assertFalse(status["receiver"]["ready"])
        self.assertEqual(status["items"], [self.item])
        self.assertIsNone(status["preparation"])
        self.assertIsNone(status["action"])
        self.assert_error("attachment_receiver_unavailable", self.prepare)
        self.assertEqual(self.poll(), {"ok": True, "preparation": None, "action": None})
        self.assertTrue(self.hub.status("room")["receiver"]["ready"])
        self.clock.now += 6
        self.assertFalse(self.hub.status("room")["receiver"]["ready"])

    def test_receiver_readiness_capacity_reclaims_expired_rooms_without_erasing_actions(self):
        self.hub.max_rooms = 1
        self.ready()
        action_id = self.action()["action"]["action_id"]
        self.poll()
        self.clock.now += 7
        registration = self.signals.register("new-room")
        self.hub.poll("new-room", registration["session_id"], registration["receiver_token"])
        self.assertEqual(set(self.hub.receivers), {"new-room"})
        self.assertEqual(self.hub.ack("room", self.session, self.token, action_id, "executed")["action"]["status"], "executed")

    def test_preparation_is_passive_and_actions_are_consumed_once(self):
        self.poll()
        prepared = self.prepare()["preparation"]
        self.assertFalse(prepared["ready"])
        response = self.poll()
        self.assertEqual(response["preparation"], prepared)
        self.assertIsNone(response["action"])
        self.assert_error("preparation_not_ready", self.action)
        self.ready()
        action_id = self.action()["action"]["action_id"]
        delivery = self.poll()["action"]
        self.assertEqual(delivery["action_id"], action_id)
        self.assertEqual(delivery["attachment"], self.item)
        self.assertEqual(delivery["remaining_ms"], 5000)
        self.assertIsNone(self.poll()["action"])
        self.assertEqual(self.action(action_id)["action"]["status"], "delivered")
        self.assertIsNone(self.poll()["action"])
        self.assert_error("action_in_progress", self.action)

    def test_receiver_authorization_room_and_current_session_guards(self):
        self.assert_error("receiver_token_invalid", self.poll, "x" * 43)
        self.ready()
        self.assert_error("attachment_not_found", self.archive.metadata, "elsewhere", self.item["attachment_id"])
        self.assert_error("session_mismatch", self.hub.prepare, "room", str(uuid4()),
                          str(uuid4()), self.item["attachment_id"])
        self.assert_error("receiver_token_invalid", self.hub.prepared, "room", self.session, "x" * 43,
                          self.preparation_id, "ready")

    def test_dispatch_deadline_is_never_extended_by_poll_or_duplicate_action(self):
        self.ready()
        action_id = self.action()["action"]["action_id"]
        self.clock.now += 5
        self.assertEqual(self.action(action_id)["action"]["status"], "expired")
        self.assertIsNone(self.poll()["action"])
        self.assert_error("action_already_finished", self.hub.ack, "room", self.session, self.token,
                          action_id, "executed")

    def test_receipt_has_separate_15_second_window_and_old_token_survives_reconnect(self):
        self.ready()
        action_id = self.action()["action"]["action_id"]
        self.poll()
        self.signals.unregister("room", self.session, self.token)
        self.clock.now += 8
        replacement = self.signals.register("room")
        self.assert_error("session_mismatch", self.poll)
        self.assert_error("receiver_token_invalid", self.hub.ack, "room", self.session,
                          replacement["receiver_token"], action_id, "executed")
        response = self.hub.ack("room", self.session, self.token, action_id, "executed")
        self.assertEqual(response["action"]["status"], "executed")
        self.assertEqual(self.hub.status("room", action_id)["action"], response["action"])
        self.assertEqual(self.hub.ack("room", self.session, self.token, action_id, "executed"), response)
        self.assertIsNone(self.hub.status("elsewhere", action_id)["action"])
        self.assert_error("session_mismatch", self.action)
        self.assertEqual(self.hub.poll("room", replacement["session_id"], replacement["receiver_token"])["action"], None)

    def test_receipt_expiry_no_ack_before_delivery_and_no_fake_success(self):
        self.ready()
        action_id = self.action()["action"]["action_id"]
        self.assert_error("action_not_delivered", self.hub.ack, "room", self.session, self.token,
                          action_id, "executed")
        self.poll()
        self.clock.now += 15
        self.assertEqual(self.hub.status("room", action_id)["action"]["status"], "expired")
        self.assert_error("action_already_finished", self.hub.ack, "room", self.session, self.token,
                          action_id, "executed")

    def test_delete_rejects_inflight_and_clears_unused_preparation(self):
        self.ready()
        action_id = self.action()["action"]["action_id"]
        self.assert_error("attachment_in_use", self.hub.delete, "room", self.item["attachment_id"])
        self.poll()
        self.assert_error("attachment_in_use", self.hub.delete, "room", self.item["attachment_id"])
        self.hub.ack("room", self.session, self.token, action_id, "cancelled", "paste_unconfirmed")
        self.hub.delete("room", self.item["attachment_id"])
        self.assertIsNone(self.hub.status("room")["preparation"])

    def test_capacity_rejects_without_evicting_unacknowledged_actions(self):
        self.hub.max_actions = 1
        self.ready()
        action_id = self.action()["action"]["action_id"]
        self.poll()
        self.assert_error("attachment_action_capacity_reached", self.action)
        self.hub.ack("room", self.session, self.token, action_id, "executed")
        self.assert_error("attachment_action_capacity_reached", self.action)
        self.assertEqual(self.action(action_id)["action"]["status"], "executed")

    def test_restart_never_replays_archives_or_action_ids(self):
        self.ready()
        action_id = self.action()["action"]["action_id"]
        restarted = attachments.AttachmentHub(self.archive, self.signals, clock=self.clock)
        self.assertFalse(restarted.status("room")["receiver"]["ready"])
        self.assertIsNone(restarted.status("room", action_id)["action"])
        self.assertIsNone(restarted.poll("room", self.session, self.token)["action"])
        self.assertEqual(len(restarted.status("room")["items"]), 1)

    def test_preparation_failure_is_visible_and_cannot_authorize_action(self):
        self.poll()
        self.prepare()
        result = self.hub.prepared("room", self.session, self.token, self.preparation_id,
                                   "failed", "hash_mismatch")
        self.assertEqual(result["preparation"]["error"], "hash_mismatch")
        self.assertFalse(result["preparation"]["ready"])
        self.assert_error("preparation_not_ready", self.action)
        self.clock.now += 60
        self.assertIsNone(self.hub.status("room")["preparation"])

    def test_attachment_poll_never_renews_main_signal_lease(self):
        self.poll()
        self.clock.now += 5
        self.poll()
        self.assertTrue(self.hub.status("room")["receiver"]["ready"])
        self.clock.now += 1
        self.assert_error("receiver_unavailable", self.poll)
        self.assertFalse(self.hub.status("room")["receiver"]["ready"])

    def test_session_change_cancels_undelivered_action_and_cannot_reassign_it(self):
        self.ready()
        action_id = self.action()["action"]["action_id"]
        self.signals.unregister("room", self.session, self.token)
        replacement = self.signals.register("room")
        self.assertEqual(self.hub.status("room", action_id)["action"]["status"], "cancelled")
        self.assertIsNone(self.hub.poll("room", replacement["session_id"], replacement["receiver_token"])["action"])
        self.assert_error("action_id_conflict", self.hub.action, "room", replacement["session_id"],
                          self.preparation_id, action_id)

    def test_parallel_polls_deliver_at_most_one_action(self):
        self.ready()
        action_id = self.action()["action"]["action_id"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.poll()["action"], range(8)))
        deliveries = [result for result in results if result]
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["action_id"], action_id)

    def test_active_preparation_id_cannot_change_its_attachment(self):
        self.ready()
        other = self.upload(b"different")
        self.assert_error("preparation_id_conflict", self.hub.prepare, "room", self.session,
                          self.preparation_id, other["attachment_id"])

    def test_slow_upload_does_not_block_status_preparation_or_text_heartbeat(self):
        entered, release = threading.Event(), threading.Event()
        class SlowSource(io.BytesIO):
            def read(inner, size):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test stream was never released")
                return super().read(size)
        self.poll()
        with ThreadPoolExecutor(max_workers=4) as pool:
            upload = pool.submit(self.archive.upload, "room", str(uuid4()), "x.bin", SlowSource(b"x"), 1)
            try:
                self.assertTrue(entered.wait(2))
                self.clock.now += 5
                status = pool.submit(self.hub.status, "room").result(timeout=1)
                self.assertEqual(status["items"], [self.item])
                self.assertTrue(status["receiver"]["ready"])
                self.assertEqual(pool.submit(self.signals.poll, "room", self.session,
                                              self.token).result(timeout=1)["action"], None)
                self.assertEqual(pool.submit(self.prepare).result(timeout=1)["preparation"]["attachment"], self.item)
                self.clock.now += 2
                self.assertTrue(self.signals.status("room")["ready"])
            finally:
                release.set()
            upload.result(timeout=2)

    def test_archive_metadata_wait_never_holds_signal_control_lock(self):
        entered, release = threading.Event(), threading.Event()
        real_items = self.archive.items
        def slow_items(room):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test metadata read was never released")
            return real_items(room)
        with ThreadPoolExecutor(max_workers=2) as pool:
            with patch.object(self.archive, "items", side_effect=slow_items):
                status = pool.submit(self.hub.status, "room")
                try:
                    self.assertTrue(entered.wait(2))
                    pool.submit(self.signals.poll, "room", self.session, self.token).result(timeout=1)
                finally:
                    release.set()
                status.result(timeout=2)

    def test_delete_disk_wait_does_not_block_text_or_allow_action(self):
        self.ready()
        entered, release = threading.Event(), threading.Event()
        real_delete = self.archive.delete
        def slow_delete(room, attachment_id):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test delete was never released")
            return real_delete(room, attachment_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            with patch.object(self.archive, "delete", side_effect=slow_delete):
                deleting = pool.submit(self.hub.delete, "room", self.item["attachment_id"])
                try:
                    self.assertTrue(entered.wait(2))
                    pool.submit(self.signals.poll, "room", self.session, self.token).result(timeout=1)
                    self.assert_error("attachment_in_use", self.action)
                finally:
                    release.set()
                deleting.result(timeout=2)
        self.assertIsNone(self.hub.status("room")["preparation"])

    def test_prepare_cannot_publish_metadata_read_before_a_concurrent_delete(self):
        self.poll()
        entered, release = threading.Event(), threading.Event()
        real_metadata = self.archive.metadata
        def slow_metadata(room, attachment_id):
            result = real_metadata(room, attachment_id)
            if threading.current_thread().name.startswith("prepare-race"):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test preparation metadata was never released")
            return result
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="prepare-race") as pool:
            with patch.object(self.archive, "metadata", side_effect=slow_metadata):
                preparing = pool.submit(self.prepare)
                try:
                    self.assertTrue(entered.wait(2))
                    self.hub.delete("room", self.item["attachment_id"])
                finally:
                    release.set()
                self.assert_error("attachment_changed", preparing.result, timeout=2)
        self.assertIsNone(self.hub.status("room")["preparation"])


class AttachmentApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.RelayHandler)
        self.httpd.daemon_threads = True
        self.httpd.store = server.RoomStore(0.5, 45)
        self.httpd.copy_store = server.CopyArchiveStore(Path(self.temp.name) / "archives.sqlite3")
        self.httpd.default_room = "room"
        self.httpd.base_path = "/doubao"
        self.httpd.local_ip = "127.0.0.1"
        self.httpd.signal_store = SignalStore(poll_seconds=0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, path, payload=None, *, raw=None, headers=None, method=None):
        headers = dict(headers or {})
        if payload is not None:
            raw = json.dumps(payload).encode()
            headers.setdefault("Content-Type", "application/json")
        method = method or ("POST" if raw is not None else "GET")
        connection = http.client.HTTPConnection(*self.httpd.server_address, timeout=3)
        try:
            connection.request(method, "/doubao" + path, body=raw, headers=headers)
            response = connection.getresponse()
            body = response.read()
            result = json.loads(body) if "application/json" in response.getheader("Content-Type", "") else body
            return response.status, result, dict(response.getheaders())
        finally:
            connection.close()

    def upload(self, data=b"bytes", attachment_id=None, **kwargs):
        attachment_id = attachment_id or str(uuid4())
        return self.request("/api/attachments/upload?room_id=room&attachment_id=" + attachment_id,
                            raw=data, headers={"X-Attachment-Name": quote("../文件.bin"),
                                               "X-Attachment-Type": "text/html", **kwargs})

    def test_http_upload_list_binary_download_delete_and_existing_ping(self):
        status, result, _ = self.upload()
        self.assertEqual(status, 200)
        item = result["attachment"]
        status, listing, _ = self.request("/api/attachments?room_id=room")
        self.assertEqual(listing["items"], [item])
        self.assertFalse(listing["receiver"]["ready"])
        status, data, headers = self.request("/api/attachments/blob?room_id=room&attachment_id=" + item["attachment_id"])
        self.assertEqual((status, data), (200, b"bytes"))
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("filename*=UTF-8''", headers["Content-Disposition"])
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))
        self.assertEqual(self.request("/api/attachments/blob?room_id=other&attachment_id=" + item["attachment_id"])[0], 404)
        self.assertEqual(self.request("/api/attachments/delete", {"room_id": "room", "attachment_id": item["attachment_id"]})[0], 200)
        self.assertEqual(self.request("/api/ping")[0], 200)

    def test_host_and_origin_guards_apply_to_binary_uploads_before_disk_write(self):
        for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"},
                        {"Sec-Fetch-Site": "cross-site"}):
            status, result, _ = self.upload(**headers)
            self.assertEqual(status, 403)
            self.assertFalse(result["ok"])
        self.assertFalse((Path(self.temp.name) / "attachments").exists())

    def test_uuid_queries_payloads_and_fixed_reason_validation(self):
        cases = [("/api/attachments", None), ("/api/attachments?room_id=room&room_id=other", None),
                 ("/api/attachments?room_id=room&action_id=../file", None),
                 ("/api/attachments/blob?room_id=room&attachment_id=" + uuid4().hex, None),
                 ("/api/attachments/action", {"room_id": "room", "session_id": [],
                                               "preparation_id": str(uuid4()), "action_id": str(uuid4())}),
                 ("/api/attachments/ack", {"room_id": "room", "session_id": str(uuid4()),
                                            "receiver_token": "x" * 43, "action_id": str(uuid4()),
                                            "status": "cancelled", "reason": "private clipboard content"})]
        for path, payload in cases:
            with self.subTest(path=path):
                status, result, _ = self.request(path, payload)
                self.assertEqual(status, 400)
                self.assertFalse(result["ok"])

    def test_upload_rejects_invalid_name_length_framing_and_hash(self):
        status, result, _ = self.upload(**{"X-Attachment-Name": "%ZZ"})
        self.assertEqual((status, result["error"]), (400, "invalid_attachment_name"))
        status, result, _ = self.upload(**{"X-Attachment-SHA256": "0" * 64})
        self.assertEqual((status, result["error"]), (409, "hash_mismatch"))
        status, result, _ = self.upload(**{"Content-Length": str(attachments.MAX_FILE_BYTES + 1)})
        self.assertEqual((status, result["error"]), (413, "attachment_too_large"))
        status, result, _ = self.upload(**{"Transfer-Encoding": "chunked"})
        self.assertEqual((status, result["error"]), (400, "transfer_encoding_not_supported"))

    def test_rejects_duplicate_upload_length_and_noncanonical_uuid_before_writes(self):
        connection = http.client.HTTPConnection(*self.httpd.server_address, timeout=3)
        try:
            connection.putrequest("POST", "/doubao/api/attachments/upload?room_id=room&attachment_id=" + str(uuid4()))
            connection.putheader("Content-Length", "0")
            connection.putheader("Content-Length", "0")
            connection.putheader("X-Attachment-Name", "file.bin")
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            self.assertEqual(json.loads(response.read())["error"], "invalid_content_length")
        finally:
            connection.close()
        self.assertFalse((Path(self.temp.name) / "attachments").exists())
        for invalid in (uuid4().hex, "{" + str(uuid4()) + "}", str(uuid4()).upper(), "../../file"):
            self.assertEqual(self.upload(attachment_id=invalid)[0], 400)
        self.assertFalse((Path(self.temp.name) / "attachments").exists())

    def test_complete_http_protocol_never_exposes_token_in_public_state(self):
        _, registration, _ = self.request("/api/signal/register", {"room_id": "room"})
        identity = {"room_id": "room", "session_id": registration["session_id"],
                    "receiver_token": registration["receiver_token"]}
        _, uploaded, _ = self.upload()
        item = uploaded["attachment"]
        self.assertEqual(self.request("/api/attachments/poll", identity)[0], 200)
        preparation_id = str(uuid4())
        prepare = {"room_id": "room", "session_id": registration["session_id"],
                   "preparation_id": preparation_id, "attachment_id": item["attachment_id"]}
        self.assertEqual(self.request("/api/attachments/prepare", prepare)[0], 200)
        self.assertEqual(self.request("/api/attachments/prepared", {**identity,
                         "preparation_id": preparation_id, "status": "ready"})[0], 200)
        action_id = str(uuid4())
        self.assertEqual(self.request("/api/attachments/action", {"room_id": "room",
                         "session_id": registration["session_id"], "preparation_id": preparation_id,
                         "action_id": action_id})[0], 200)
        _, response, _ = self.request("/api/attachments/poll", identity)
        self.assertEqual(response["action"]["attachment"], item)
        self.assertIsNone(self.request("/api/attachments/poll", identity)[1]["action"])
        self.assertEqual(self.request("/api/attachments/ack", {**identity, "action_id": action_id,
                                                             "status": "executed"})[0], 200)
        _, status, _ = self.request("/api/attachments?room_id=room&action_id=" + action_id)
        self.assertEqual(status["action"]["status"], "executed")
        self.assertNotIn(registration["receiver_token"], json.dumps(status))


if __name__ == "__main__":
    unittest.main()
