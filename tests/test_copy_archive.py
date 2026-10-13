from __future__ import annotations

import http.client
import json
import queue
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from app import server
from scripts import mac_paste_helper, windows_paste_helper


class QuietRelayHandler(server.RelayHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class RelayTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "state" / "archives.sqlite3"
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), QuietRelayHandler)
        self.httpd.daemon_threads = True
        self.httpd.store = server.RoomStore(archive_idle_seconds=0.5, claim_ttl_seconds=45)
        self.httpd.copy_store = server.CopyArchiveStore(self.db_path)
        self.httpd.default_room = "test-room"
        self.httpd.base_path = "/doubao"
        self.httpd.local_ip = "127.0.0.1"
        self.httpd.archive_idle_seconds = 0.5
        self.httpd.claim_ttl_seconds = 45
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path: str, payload: object | None = None) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(*self.httpd.server_address, timeout=3)
        try:
            if payload is None:
                connection.request("GET", f"/doubao{path}")
            else:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                connection.request("POST", f"/doubao{path}", body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def snapshot(self, **overrides: object) -> dict:
        return {
            "room_id": "test-room", "copy_id": str(uuid4()),
            "text": "复制快照：\n正在使用豆包输入法 🐱", "copied_at": "2026-10-08T02:01:00.123Z",
            **overrides,
        }


class CopyArchiveApiTests(RelayTestCase):
    def test_copy_is_durable_idempotent_and_kept_out_of_relay(self) -> None:
        before = self.httpd.store.get("test-room").payload()
        watcher = self.httpd.store.subscribe("test-room")
        payload = self.snapshot()
        status, result = self.request("/api/copy-archive", payload)
        self.assertEqual(status, 201)
        self.assertTrue(result["archived"])
        self.assertEqual(result["copy_archive"]["text"], payload["text"])
        self.assertEqual(result["copy_archive"]["source"], "mobile-clipboard")
        self.assertEqual(result["copy_archive"]["chars"], len(payload["text"]))
        self.assertEqual(self.httpd.store.get("test-room").payload(), before)
        self.assertEqual(self.httpd.store.get("test-room").helper_payload()["latest_archive"], None)
        with self.assertRaises(queue.Empty):
            watcher.get_nowait()
        status, repeated = self.request("/api/copy-archive", payload)
        self.assertEqual(status, 200)
        self.assertFalse(repeated["archived"])
        self.assertEqual(repeated["copy_archive"], result["copy_archive"])
        self.httpd.copy_store = server.CopyArchiveStore(self.db_path)
        self.httpd.store = server.RoomStore(0.5, 45)
        status, history = self.request("/api/copy-history?room_id=test-room")
        self.assertEqual(status, 200)
        self.assertEqual(history["items"], [result["copy_archive"]])
        self.assertEqual(self.httpd.store.get("test-room").capture_mode, "clipboard")

    def test_same_identity_with_changed_content_or_time_conflicts(self) -> None:
        payload = self.snapshot()
        self.request("/api/copy-archive", payload)
        for mutation in ({"text": "changed"}, {"copied_at": "2026-10-08T02:02:00Z"}):
            with self.subTest(mutation=mutation):
                status, result = self.request("/api/copy-archive", {**payload, **mutation})
                self.assertEqual(status, 409)
                self.assertEqual(result["error"], "copy_id_conflict")
        self.assertEqual(len(self.httpd.copy_store.history("test-room")), 1)

    def test_parallel_retries_commit_once(self) -> None:
        payload = self.snapshot()
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.request("/api/copy-archive", payload), range(6)))
        self.assertEqual(sorted(status for status, _ in results), [200] * 5 + [201])
        self.assertEqual(sum(bool(result["archived"]) for _, result in results), 1)
        self.assertEqual(len(self.httpd.copy_store.history("test-room")), 1)

    def test_history_is_newest_first_room_scoped_and_display_limit_does_not_delete(self) -> None:
        store = self.httpd.copy_store
        for index in range(55):
            store.archive("test-room", str(uuid4()), f"text {index}", "2026-10-08T02:00:00+00:00")
        store.archive("other-room", str(uuid4()), "other", "2026-10-08T02:00:00+00:00")
        status, history = self.request("/api/copy-history?room_id=test-room")
        self.assertEqual(status, 200)
        self.assertEqual(len(history["items"]), 50)
        self.assertEqual(history["items"][0]["text"], "text 54")
        self.assertEqual(history["items"][-1]["text"], "text 5")
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM copy_archives").fetchone()[0], 56)

    def test_database_failure_never_reports_saved(self) -> None:
        blocked_parent = Path(self.temp.name) / "not-a-directory"
        blocked_parent.write_text("file", encoding="utf-8")
        self.httpd.copy_store = server.CopyArchiveStore(blocked_parent / "archives.sqlite3")
        for path, payload in (("/api/copy-archive", self.snapshot()), ("/api/copy-history", None)):
            status, result = self.request(path, payload)
            self.assertEqual(status, 503)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "archive_unavailable")

    def test_new_archive_file_is_private_and_existing_permissions_are_preserved(self) -> None:
        if server.os.name == "nt":
            self.skipTest("POSIX mode bits do not describe Windows ACLs")
        self.assertEqual(self.request("/api/copy-archive", self.snapshot())[0], 201)
        self.assertEqual(stat.S_IMODE(self.db_path.stat().st_mode), 0o600)
        self.db_path.chmod(0o640)
        self.assertEqual(self.request("/api/copy-history")[0], 200)
        self.assertEqual(stat.S_IMODE(self.db_path.stat().st_mode), 0o640)

    def test_invalid_types_dates_and_text_are_rejected_without_writes(self) -> None:
        mutations = (
            {"room_id": []}, {"room_id": ""}, {"room_id": "x" * 257},
            {"copy_id": 12}, {"copy_id": "not-a-uuid"},
            {"text": []}, {"text": "  "}, {"text": "x" * (server.MAX_COPY_TEXT_CHARS + 1)},
            {"copied_at": True}, {"copied_at": "yesterday"}, {"copied_at": "2026-10-08T02:00:00"},
        )
        for mutation in mutations:
            with self.subTest(mutation=next(iter(mutation))):
                status, result = self.request("/api/copy-archive", self.snapshot(**mutation))
                self.assertIn(status, (400, 413, 422))
                self.assertFalse(result["ok"])
        self.assertFalse(self.db_path.exists())
        for query in ("room_id=", "room_id=a&room_id=b"):
            self.assertEqual(self.request(f"/api/copy-history?{query}")[0], 400)

    def test_bad_json_and_bounded_body(self) -> None:
        for raw, headers, expected in (
            (b"{", {"Content-Type": "application/json"}, 400),
            (b"[]", {"Content-Type": "application/json"}, 400),
            (b"\xff", {"Content-Type": "application/json"}, 400),
            (b"{}", {"Content-Type": "text/plain"}, 415),
            (b"", {"Content-Type": "application/json", "Content-Length": "-1"}, 400),
            (b"", {"Content-Type": "application/json", "Content-Length": "bad"}, 400),
            (b"", {"Content-Type": "application/json", "Content-Length": str(server.MAX_COPY_BODY_BYTES + 1)}, 413),
        ):
            connection = http.client.HTTPConnection(*self.httpd.server_address, timeout=3)
            try:
                connection.request("POST", "/doubao/api/copy-archive", raw, headers)
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                self.assertFalse(json.loads(response.read())["ok"])
            finally:
                connection.close()
        self.assertFalse(self.db_path.exists())

    def test_stale_updates_cannot_override_clipboard_setting(self) -> None:
        status, settings = self.request("/api/settings", {"room_id": "test-room", "capture_mode": "manual"})
        self.assertEqual(status, 200)
        revision = settings["settings"]["revision"]
        draft = {"room_id": "test-room", "text": "old draft", "source": "mobile",
                 "capture_mode": "manual", "expected_settings_revision": revision}
        self.assertEqual(self.request("/api/update", draft)[0], 201)
        status, current = self.request("/api/settings", {"room_id": "test-room", "capture_mode": "clipboard"})
        self.assertEqual(status, 200)
        self.assertEqual(current["settings"]["revision"], revision + 1)
        status, rejected = self.request("/api/update", draft)
        self.assertEqual(status, 409)
        self.assertEqual(rejected["error"], "settings_revision_conflict")
        self.assertEqual(rejected["state"], current)
        draft.pop("expected_settings_revision")
        status, rejected = self.request("/api/update", draft)
        self.assertEqual(status, 409)
        self.assertEqual(rejected["error"], "clipboard_mode")
        self.assertEqual(self.request("/api/capture", {"room_id": "test-room"})[0], 409)
        self.assertEqual(self.httpd.store.get("test-room").payload(), current)

    def test_clipboard_to_auto_waits_for_fresh_draft_before_archiving(self) -> None:
        self.request("/api/settings", {"room_id": "test-room", "capture_mode": "manual"})
        self.request("/api/update", {"room_id": "test-room", "text": "old manual draft", "source": "mobile"})
        self.request("/api/settings", {"room_id": "test-room", "capture_mode": "clipboard"})
        self.assertEqual(self.request("/api/copy-archive", self.snapshot(text="new clipboard draft"))[0], 201)
        self.request("/api/settings", {"room_id": "test-room", "capture_mode": "auto"})
        time.sleep(0.65)
        self.assertEqual(self.httpd.store.get("test-room").history, [])
        self.request("/api/update", {"room_id": "test-room", "text": "new clipboard draft", "source": "mobile"})
        time.sleep(0.65)
        self.assertEqual([entry.text for entry in self.httpd.store.get("test-room").history], ["new clipboard draft"])


class ClipboardModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = server.RoomStore(0.5, 45)

    def test_default_and_clipboard_updates_never_modify_delivery_state(self) -> None:
        self.assertEqual(self.store.get("room").capture_mode, "clipboard")
        before = self.store.get("room").payload()
        watcher = self.store.subscribe("room")
        self.assertEqual(self.store.update("room", "draft", "mobile").payload(), before)
        self.assertEqual(self.store.capture_now("room")["error"], "clipboard_mode")
        with self.assertRaises(queue.Empty):
            watcher.get_nowait()

    def test_switch_to_clipboard_cancels_pending_auto_timer_and_preserves_version(self) -> None:
        with patch.object(server.threading, "Timer") as timer:
            self.store.update_settings("room", capture_mode="auto")
            before = self.store.update("room", "draft", "mobile", capture_mode="auto")
            generation = self.store._archive_generations["room"]
            after = self.store.update_settings("room", capture_mode="clipboard")
        timer.return_value.cancel.assert_called_once()
        self.assertEqual(before.version, after.version)
        self.assertEqual(before.text, after.text)
        self.store._archive_if_idle("room", before.version, generation)
        self.assertEqual(self.store.get("room").history, [])
        ignored = self.store.update("room", "different copy draft", "mobile", capture_mode="clipboard")
        self.assertEqual(ignored.text, before.text)
        self.assertEqual(ignored.version, before.version)
        self.assertFalse(self.store.capture_now("room")["ok"])

    def test_existing_manual_and_auto_paths_still_archive(self) -> None:
        self.store.update_settings("manual", capture_mode="manual")
        manual = self.store.update("manual", "manual text", "mobile", capture_mode="manual")
        self.assertEqual(self.store.capture_now("manual", manual.version)["archive"]["text"], "manual text")
        with patch.object(server.threading, "Timer"):
            self.store.update_settings("auto", capture_mode="auto")
            auto = self.store.update("auto", "auto text", "mobile", capture_mode="auto")
            generation = self.store._archive_generations["auto"]
        self.store._archive_if_idle("auto", auto.version, generation)
        self.assertEqual(self.store.get("auto").history[-1].text, "auto text")

    def test_manual_to_auto_still_rearms_but_clipboard_to_auto_does_not(self) -> None:
        self.store.update_settings("room", capture_mode="manual")
        self.store.update("room", "pending manual draft", "mobile")
        with patch.object(server.threading, "Timer") as timer:
            self.store.update_settings("room", capture_mode="auto")
            timer.assert_called_once()
            self.store.update_settings("room", capture_mode="clipboard")
        with patch.object(server.threading, "Timer") as timer:
            self.store.update_settings("room", capture_mode="auto")
            timer.assert_not_called()
            self.store.update_settings("room", archive_idle_seconds=0.6)
            timer.assert_not_called()
            self.store.update("room", "fresh draft", "mobile")
            timer.assert_called_once()

    def test_settings_revision_tracks_changes_without_changing_delivery_version(self) -> None:
        self.assertEqual(self.store.get("room").settings_revision, 0)
        first = self.store.update_settings("room", capture_mode="manual", theme="green")
        self.assertEqual(first.settings_revision, 1)
        self.assertEqual(first.version, 0)
        no_change = self.store.update_settings("room", capture_mode="manual", theme="green")
        self.assertEqual(no_change.settings_revision, 1)
        with patch.object(server.threading, "Timer"):
            auto = self.store.update("room", "draft", "mobile", capture_mode="auto", expected_settings_revision=1)
        self.assertEqual(auto.settings_revision, 2)
        self.assertEqual(auto.payload()["settings"]["revision"], 2)
        with self.assertRaises(server.RoomUpdateConflict) as conflict:
            self.store.update("room", "stale draft", "mobile", expected_settings_revision=1)
        self.assertEqual(conflict.exception.error, "settings_revision_conflict")
        self.assertEqual(self.store.get("room").text, "draft")

    def test_server_epoch_identifies_restarts_without_changing_delivery_version(self) -> None:
        initial = self.store.get("room")
        UUID(initial.server_epoch)
        self.assertEqual(initial.server_epoch, self.store.get("other-room").server_epoch)
        changed = self.store.update_settings("room", capture_mode="manual")
        self.assertEqual(changed.server_epoch, initial.server_epoch)
        self.assertEqual(changed.clone().server_epoch, initial.server_epoch)
        self.assertEqual(changed.payload()["server_epoch"], initial.server_epoch)
        self.assertEqual(changed.helper_payload()["server_epoch"], initial.server_epoch)
        self.assertEqual(changed.version, 0)
        restarted = server.RoomStore(0.5, 45).get("room")
        self.assertNotEqual(restarted.server_epoch, initial.server_epoch)
        self.assertEqual(restarted.version, 0)
        self.assertEqual(restarted.settings_revision, 0)
        self.assertEqual(restarted.capture_mode, "clipboard")

    def test_database_argument_and_environment_paths_are_absolute(self) -> None:
        with self.assertRaises(ValueError):
            server.parse_archive_db("relative.sqlite3")
        self.assertTrue(server.default_archive_db().is_absolute())
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"DOUBAO_ARCHIVE_DB": f"{temp}/test.sqlite3"}):
            self.assertEqual(server.build_parser().parse_args([]).archive_db, Path(temp) / "test.sqlite3")


class ExistingHelperIsolationTests(RelayTestCase):
    def helper_payload(self, helper: object, transport: str) -> dict:
        if transport == "poll":
            status, payload = self.request("/api/helper-state?room_id=test-room")
            self.assertEqual(status, 200)
            return helper.normalize_payload(payload)
        connection = http.client.HTTPConnection(*self.httpd.server_address, timeout=3)
        try:
            connection.request("GET", "/doubao/api/stream?room_id=test-room")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            lines = []
            while True:
                line = response.fp.readline().decode("utf-8").rstrip("\r\n")
                if not line:
                    break
                lines.append(line)
            return helper.parse_sse_event(lines)
        finally:
            connection.close()

    def test_copy_archives_do_not_trigger_unchanged_mac_or_windows_helpers(self) -> None:
        # Seed a real legacy archive so the test also guards against replaying old text.
        self.httpd.store.update_settings("test-room", capture_mode="manual")
        self.httpd.store.update("test-room", "previous send", "mobile", capture_mode="manual")
        self.httpd.store.capture_now("test-room")
        self.httpd.store.update_settings("test-room", capture_mode="clipboard")
        watcher = self.httpd.store.subscribe("test-room")
        for helper in (mac_paste_helper, windows_paste_helper):
            for transport in ("stream", "poll"):
                for trigger in ("archive", "live"):
                    for mode in ("clipboard", "paste"):
                        with self.subTest(helper=helper.__name__, transport=transport, trigger=trigger, mode=mode):
                            args = helper.build_parser().parse_args(
                                ["--transport", transport, "--trigger", trigger, "--mode", mode]
                            )
                            args.client_id = "test-helper"
                            state = {
                                "last_version": -1, "last_archive_id": -1, "last_archive_marker": None,
                                "startup_seeded": False, "applied_updates": 0, "curl_resolve": [], "pending_acks": {},
                            }
                            paste_function = "paste_to_active_app" if helper is mac_paste_helper else "paste_to_active_window"
                            with (
                                patch.object(helper, "copy_to_clipboard") as copy,
                                patch.object(helper, paste_function) as paste,
                                patch.object(helper, "acknowledge_archive_with_retry") as acknowledge,
                            ):
                                initial = self.helper_payload(helper, transport)
                                self.assertFalse(helper.process_payload(args, initial, state))
                                status, _ = self.request("/api/copy-archive", self.snapshot())
                                self.assertEqual(status, 201)
                                after = self.helper_payload(helper, transport)
                                self.assertEqual(after, initial)
                                self.assertFalse(helper.process_payload(args, after, state))
                                copy.assert_not_called()
                                paste.assert_not_called()
                                acknowledge.assert_not_called()
                                self.assertEqual(state["applied_updates"], 0)
                                with self.assertRaises(queue.Empty):
                                    watcher.get_nowait()


if __name__ == "__main__":
    unittest.main()
