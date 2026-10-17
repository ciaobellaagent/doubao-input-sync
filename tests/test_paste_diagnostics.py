import importlib.util
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import paste_diagnostics as diagnostics


class PasteDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # On macOS /var is a symlink. Resolve our test fixture only; production
        # deliberately refuses linked path components instead of resolving them.
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "private-logs" / "paste-trace.jsonl"
        diagnostics.enable(self.path)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.close_sink)

    def close_sink(self):
        diagnostics.disable()
        self.assertTrue(diagnostics.flush(5))

    def flush(self):
        self.assertTrue(diagnostics.flush(5))

    def records(self, path=None):
        self.flush()
        return [json.loads(line) for line in (path or self.path).read_text().splitlines()]

    def base_fields(self):
        return {"event", "at", "mono_ms", "writer_delay_ms"}

    def test_import_is_lazy_and_defaults_are_bounded(self):
        spec = importlib.util.spec_from_file_location("fresh_paste_diagnostics", SCRIPTS / "paste_diagnostics.py")
        fresh = importlib.util.module_from_spec(spec)
        with patch.object(os, "open") as opened, patch.object(os, "mkdir") as made:
            spec.loader.exec_module(fresh)
            fresh.trace("receiver_start")
        opened.assert_not_called()
        made.assert_not_called()
        self.assertEqual(fresh.MAX_LOG_BYTES, 2 * 1024 * 1024)
        self.assertEqual(fresh.LOG_FILE_COUNT, 3)
        self.assertEqual(fresh.MAX_QUEUE_RECORDS, 256)
        self.assertEqual(fresh.LOG_PATH.name, "paste-trace.jsonl")
        self.assertFalse(self.path.parent.exists())

    def test_disabled_sink_does_not_write_and_enabling_is_lazy(self):
        diagnostics.disable()
        diagnostics.trace("receiver_start")
        self.assertFalse(self.path.parent.exists())
        diagnostics.enable(self.path)
        self.assertFalse(self.path.parent.exists())
        diagnostics.trace("receiver_start")
        self.assertEqual(self.records()[0]["event"], "receiver_start")

    def test_bad_sink_configuration_disables_previous_sink(self):
        for bad_path in ("relative/path", object()):
            diagnostics.enable(self.path)
            diagnostics.enable(bad_path)
            diagnostics.trace("receiver_start")
            self.assertFalse(self.path.parent.exists())

    def test_only_explicit_metadata_is_recorded(self):
        action_id = str(uuid4())
        diagnostics.trace("control", action_id=action_id, endpoint="poll", outcome="http_error",
                          error_code="action_already_finished", http_status=409, curl_code=0,
                          elapsed_ms=12.34567, source_sha="A" * 64, text="private body",
                          url="https://private.example/secret", sha256="b" * 64,
                          token="private token", title="private title", exception=RuntimeError("secret"))
        record = self.records()[0]
        self.assertEqual(record["action_id"], action_id)
        self.assertEqual(record["elapsed_ms"], 12.346)
        self.assertEqual(record["source_sha"], "a" * 64)
        self.assertEqual(record["error_code"], "action_already_finished")
        self.assertEqual(set(record), self.base_fields() | {"action_id", "endpoint", "outcome", "error_code",
                                                         "http_status", "curl_code", "elapsed_ms", "source_sha"})
        self.assertNotIn("private", self.path.read_text())

    def test_unknown_events_and_values_are_dropped(self):
        for event in ("private body", 1, None):
            diagnostics.trace(event, endpoint="poll")
        self.assertFalse(self.path.exists())
        diagnostics.trace("action_result", action_id="invalid", reason="private body", status="private body",
                          stage="private title", route="private URL", termination="private exception",
                          outcome="private body", error_code="private token", source_sha="not a source hash")
        self.assertEqual(set(self.records()[0]), self.base_fields())

    def test_numbers_are_finite_bounded_and_never_booleans(self):
        fields = {"elapsed_ms": math.nan, "poll_ms": math.inf, "queue_ms": -1,
                  "lock_ms": True, "remaining_ms": -600001, "server_remaining_ms": 600001,
                  "http_status": 600, "curl_code": -1, "exit_code": 256,
                  "code": 10 ** 1000, "pid": False}
        diagnostics.trace("control", **fields)
        self.assertEqual(set(self.records()[0]), self.base_fields())
        diagnostics.trace("process_end", elapsed_ms=600000, remaining_ms=-123.25,
                          http_status=0, curl_code=28, exit_code=-15, code=-25212, pid=123)
        record = self.records()[1]
        self.assertEqual(record["remaining_ms"], -123.25)
        self.assertEqual(record["exit_code"], -15)

    def test_arbitrary_objects_are_not_stringified(self):
        class Private:
            def __str__(self):
                raise AssertionError("must not stringify private values")
        private = Private()
        diagnostics.trace("native_stage", stage=private, code=private, action_id=private,
                          source_sha=private, error_code=private)
        self.assertEqual(set(self.records()[0]), self.base_fields())

    def test_context_restores_on_nested_context_and_exception(self):
        outer, inner = str(uuid4()), uuid4()
        self.assertIsNone(diagnostics.current_action_id())
        with diagnostics.action_context(outer.upper()):
            self.assertEqual(diagnostics.current_action_id(), outer)
            diagnostics.trace("action_start")
            with diagnostics.action_context(inner):
                diagnostics.trace("lock_wait")
            with diagnostics.action_context("invalid"):
                self.assertIsNone(diagnostics.current_action_id())
                diagnostics.trace("receiver_start")
            with self.assertRaisesRegex(RuntimeError, "body failure"):
                with diagnostics.action_context(inner):
                    raise RuntimeError("body failure")
            diagnostics.trace("action_result", status="cancelled", reason="expired")
        diagnostics.trace("receiver_start")
        records = self.records()
        self.assertEqual([r.get("action_id") for r in records], [outer, str(inner), None, outer, None])
        self.assertIsNone(diagnostics.current_action_id())

    def test_context_and_records_are_isolated_across_threads(self):
        barrier = threading.Barrier(3)
        identities = [str(uuid4()), str(uuid4())]
        def worker(action_id):
            with diagnostics.action_context(action_id):
                barrier.wait()
                for number in range(20):
                    diagnostics.trace("action_start", elapsed_ms=number)
            barrier.wait()
        threads = [threading.Thread(target=worker, args=(action_id,)) for action_id in identities]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        diagnostics.trace("receiver_start")
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        records = self.records()
        self.assertEqual(len(records), 41)
        for action_id in identities:
            self.assertEqual(sum(r.get("action_id") == action_id for r in records), 20)
        self.assertEqual(sum("action_id" not in r for r in records), 1)

    def test_private_permissions_for_new_and_existing_files(self):
        self.path.parent.mkdir(mode=0o755)
        self.path.write_text("")
        archive = self.path.with_name(self.path.name + ".1")
        archive.write_text("")
        for path in (self.path, archive):
            path.chmod(0o644)
        self.path.parent.chmod(0o755)
        diagnostics.trace("receiver_start")
        self.flush()
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        for path in (self.path, archive):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_rotation_keeps_three_bounded_files(self):
        with patch.object(diagnostics, "MAX_LOG_BYTES", 300):
            for number in range(30):
                diagnostics.trace("process_end", termination="exit", elapsed_ms=number, exit_code=0)
            self.flush()
        files = list(self.path.parent.iterdir())
        self.assertEqual({p.name for p in files}, {self.path.name, self.path.name + ".1", self.path.name + ".2"})
        records = []
        for path in files:
            self.assertLessEqual(path.stat().st_size, 300)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            records.extend(self.records(path))
        self.assertEqual(max(r["elapsed_ms"] for r in records), 29)
        self.assertLess(len(records), 30)

    def test_preexisting_oversized_files_become_bounded(self):
        self.path.parent.mkdir()
        for suffix in ("", ".1", ".2"):
            self.path.with_name(self.path.name + suffix).write_bytes(b"x" * 500)
        with patch.object(diagnostics, "MAX_LOG_BYTES", 220):
            diagnostics.trace("receiver_start")
            self.flush()
        for path in self.path.parent.iterdir():
            self.assertLessEqual(path.stat().st_size, 220)
        self.assertEqual(self.records()[0]["event"], "receiver_start")

    def test_records_stay_below_one_kibibyte(self):
        fields = {"endpoint": "unregister", "outcome": "invalid_registration", "reason": "receiver_unregistered",
                  "status": "cancelled", "stage": "system_events_representation_error", "route": "system_events",
                  "termination": "spawn_error", "error_code": "receiver_already_registered", "source_sha": "a" * 64,
                  "action_id": str(uuid4()), "elapsed_ms": 600000, "poll_ms": 600000, "queue_ms": 600000,
                  "lock_ms": 600000, "remaining_ms": -600000, "server_remaining_ms": -600000,
                  "http_status": 599, "curl_code": 255, "exit_code": -255, "code": -99999999, "pid": 2147483647}
        diagnostics.trace("action_received", **fields)
        self.flush()
        line = self.path.read_bytes()
        self.assertLess(len(line), 1024)
        self.assertEqual(set(self.records()[0]), set(fields) | self.base_fields())

    def test_symlinked_directory_or_ancestor_is_never_followed(self):
        target = self.root / "target"
        target.mkdir(mode=0o755)
        target.chmod(0o755)
        self.path.parent.symlink_to(target, target_is_directory=True)
        diagnostics.trace("receiver_start")
        self.flush()
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        nested = self.path.parent / "nested" / self.path.name
        diagnostics.enable(nested)
        diagnostics.trace("receiver_start")
        self.flush()
        self.assertEqual(list(target.iterdir()), [])

    def test_symlinked_active_or_archive_is_never_followed(self):
        self.path.parent.mkdir()
        target = self.root / "private-target"
        target.write_text("untouched")
        target.chmod(0o644)
        for suffix in ("", ".1", ".2"):
            linked = self.path.with_name(self.path.name + suffix)
            linked.symlink_to(target)
            diagnostics.trace("receiver_start")
            self.flush()
            self.assertEqual(target.read_text(), "untouched")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
            self.assertTrue(linked.is_symlink())
            linked.unlink()
        self.assertFalse(self.path.exists())

    def test_nonregular_and_hardlinked_files_are_not_written(self):
        self.path.parent.mkdir()
        os.mkfifo(self.path)
        diagnostics.trace("receiver_start")
        self.flush()
        self.assertTrue(stat.S_ISFIFO(self.path.lstat().st_mode))
        self.path.unlink()
        target = self.root / "hardlink-target"
        target.write_text("untouched")
        os.link(target, self.path)
        diagnostics.trace("receiver_start")
        self.flush()
        self.assertEqual(target.read_text(), "untouched")

    def test_write_and_rotation_errors_never_escape(self):
        for function in ("_write_line", "_metadata"):
            with patch.object(diagnostics, function, side_effect=RuntimeError("private error")):
                self.assertIsNone(diagnostics.trace("receiver_start"))
                self.flush()
        with patch.object(os, "open", side_effect=OSError("private error")):
            self.assertIsNone(diagnostics.trace("receiver_start"))
            self.flush()
        with patch.object(diagnostics, "MAX_LOG_BYTES", 300):
            diagnostics.trace("receiver_start")
            self.flush()
            self.path.write_bytes(b"x" * 290)
            with patch.object(os, "replace", side_effect=OSError("private error")) as replaced:
                self.assertIsNone(diagnostics.trace("receiver_start"))
                self.flush()
                self.assertTrue(replaced.called)

    def test_partial_append_does_not_leave_broken_record(self):
        diagnostics.trace("receiver_start")
        self.flush()
        original = self.path.read_bytes()
        real_write = os.write
        def partial(fd, data):
            return real_write(fd, data[:10])
        with patch.object(os, "write", side_effect=partial):
            diagnostics.trace("receiver_start")
            self.flush()
        self.assertEqual(self.path.read_bytes(), original)

    def test_writer_block_does_not_block_producer_or_bounded_flush(self):
        entered, release, offered = threading.Event(), threading.Event(), threading.Event()
        real_write = diagnostics._write_line
        def blocked(line, path):
            entered.set()
            release.wait()
            return real_write(line, path)
        with patch.object(diagnostics, "_write_line", side_effect=blocked):
            try:
                diagnostics.trace("receiver_start")
                self.assertTrue(entered.wait(5))
                def producer():
                    diagnostics.trace("action_start", elapsed_ms=123)
                    offered.set()
                producer_thread = threading.Thread(target=producer)
                producer_thread.start()
                self.assertTrue(offered.wait(2), "producer waited for blocked writer")
                self.assertFalse(diagnostics.flush(0.01))
                producer_thread.join(2)
                self.assertFalse(producer_thread.is_alive())
            finally:
                release.set()
                self.flush()
        self.assertEqual(len(self.records()), 2)

    def test_full_queue_drops_without_blocking_or_retry_and_reports_loss(self):
        entered, release, offered = threading.Event(), threading.Event(), threading.Event()
        real_write = diagnostics._write_line
        def blocked(line, path):
            entered.set()
            release.wait()
            return real_write(line, path)
        pending = diagnostics._WRITER.queue
        with patch.object(pending, "maxsize", 2), patch.object(diagnostics, "_write_line", side_effect=blocked):
            try:
                diagnostics.trace("receiver_start")
                self.assertTrue(entered.wait(5))
                def producer():
                    for number in range(10):
                        diagnostics.trace("action_start", elapsed_ms=number)
                    offered.set()
                producer_thread = threading.Thread(target=producer)
                producer_thread.start()
                self.assertTrue(offered.wait(2), "full queue blocked producer")
                self.assertEqual(pending.qsize(), 2)
                producer_thread.join(2)
            finally:
                release.set()
                self.flush()
        records = self.records()
        self.assertEqual(len(records), 3)
        self.assertEqual([r.get("elapsed_ms") for r in records], [None, 0, 1])
        self.assertEqual(records[-1]["dropped_records"], 8)
        diagnostics.trace("action_start", elapsed_ms=999)
        self.assertEqual(self.records()[-1]["dropped_records"], 8)

    def test_failed_write_is_not_retried_and_failure_count_survives(self):
        failed_id, good_id = str(uuid4()), str(uuid4())
        with patch.object(diagnostics, "_write_line", side_effect=OSError("private failure")) as write:
            diagnostics.trace("action_start", action_id=failed_id)
            self.flush()
            self.assertEqual(write.call_count, 1)
        diagnostics.trace("action_start", action_id=good_id)
        record = self.records()[0]
        self.assertEqual(record["action_id"], good_id)
        self.assertEqual(record["dropped_records"], 1)
        self.assertNotIn(failed_id, self.path.read_text())

    def test_late_writer_cannot_write_old_records_to_reenabled_path(self):
        entered, release = threading.Event(), threading.Event()
        old_id, new_id = str(uuid4()), str(uuid4())
        new_path = self.root / "new-sink" / "paste-trace.jsonl"
        real_write = diagnostics._write_line
        def blocked(line, path):
            entered.set()
            release.wait()
            return real_write(line, path)
        with patch.object(diagnostics, "_write_line", side_effect=blocked):
            try:
                diagnostics.trace("action_start", action_id=old_id)
                self.assertTrue(entered.wait(5))
                diagnostics.trace("action_result", action_id=old_id, status="cancelled", reason="expired")
                diagnostics.disable()
                diagnostics.trace("receiver_start")
                diagnostics.enable(new_path)
                diagnostics.trace("action_start", action_id=new_id)
            finally:
                release.set()
                self.flush()
        self.assertEqual([r["action_id"] for r in self.records()], [old_id, old_id])
        self.assertEqual([r["action_id"] for r in self.records(new_path)], [new_id])

    def test_writer_delay_uses_monotonic_clock_and_context_is_captured_before_write(self):
        action_id = str(uuid4())
        with patch.object(diagnostics.time, "monotonic", return_value=diagnostics._WRITER.origin + 2), \
                patch.object(diagnostics.time, "time", return_value=100):
            with diagnostics.action_context(action_id):
                diagnostics.trace("action_start")
        record = self.records()[0]
        self.assertEqual(record["at"], 100)
        self.assertEqual(record["mono_ms"], 2000)
        self.assertGreaterEqual(record["writer_delay_ms"], 0)
        self.assertEqual(record["action_id"], action_id)


if __name__ == "__main__":
    unittest.main()
