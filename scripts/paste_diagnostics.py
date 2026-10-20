"""Bounded metadata tracing. This module has no input or application capability.

Importing is side-effect free. ``trace`` only validates, serializes and offers a
record to a bounded queue; a single daemon owns all file I/O. A stalled writer
can lose diagnostics, but cannot hold an action/poll thread on its file locks.
Unknown fields/values are dropped and diagnostic errors never escape callers.
``action_context`` only correlates work in the current thread; it does not
authorize an action. Dispatch intent/return records do not prove insertion.
Accepted records retain their original sink after enable/disable. Process exit
may lose the daemon's remaining records; explicit ``flush(timeout)`` is bounded
and is intended for tests or closeout, never the action/poll path.
"""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import queue
import re
import stat
import threading
from threading import local as thread_context
import time
import uuid


DEFAULT_LOG_PATH = Path.home() / "Library/Logs/DoubaoInputSync/paste-trace.jsonl"
LOG_PATH = DEFAULT_LOG_PATH
MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_FILE_COUNT = 3  # Active file plus .1 and .2, newest first.
MAX_LINE_BYTES = 1024  # The terminating newline is included; strictly less.
MAX_QUEUE_RECORDS = 256  # Plus one record currently held by the writer.
_WRITER_METADATA_RESERVE = 80
_MAX_DROPPED = 2147483647

EVENTS = frozenset({
    "control", "action_received", "action_start", "action_duplicate", "lock_wait",
    "process_start", "process_end", "native_stage", "action_result", "ack_start",
    "ack_end", "disconnected", "receiver_start",
})
ENDPOINTS = frozenset({"status", "register", "poll", "prepared", "ack", "unregister"})
OUTCOMES = frozenset({
    "ok", "http_error", "transport_failed", "process_timeout", "invalid_response",
    "invalid_registration", "session_changed", "preparation_failed", "invalid_action",
    "unexpected_error",
})
REASONS = frozenset({
    "focus_changed", "no_focused_input", "unsafe_target", "expired", "receiver_stopped",
    "paste_failed", "receiver_expired", "receiver_unregistered", "permission_missing",
    "modifier_held", "superseded", "clipboard_timeout", "clipboard_changed", "mismatch", "paste_unconfirmed",
    "download_failed", "cache_full", "file_too_large", "preparation_failed",
})
ERROR_CODES = frozenset({
    "preparation_mismatch", "session_mismatch", "receiver_token_invalid",
    "receiver_unavailable", "protocol_mismatch", "receiver_already_registered",
    "action_in_progress", "action_limit_reached", "preparation_id_conflict",
    "preparation_not_ready", "action_id_conflict", "poll_in_progress",
    "action_unknown", "action_already_finished", "action_not_delivered",
    "signal_capacity_reached", "invalid_protocol", "invalid_sha256", "invalid_fields",
    "server_rejected",
})
STAGES = frozenset({
    "start", "target_process", "focused_input", "focused_window", "input_role",
    "native_workbuddy", "accessibility_init", "modifiers", "focus_recheck",
    "clipboard_read", "clipboard_hash", "key_dispatch", "workspace_target_pid",
    "system_events_disagrees_pid", "system_events_representation_error",
    "system_events_target_pid", "system_events_nontext_role", "as_start",
    "as_legacy_lookup", "as_process_lookup", "as_bundle_lookup", "as_focused_input",
    "as_focused_window", "as_input_role", "as_focus_ready", "as_clipboard_wait",
    "as_clipboard_matched", "as_dispatch_intent", "as_dispatch_returned",
    "as_native_handoff",
})

_ENUM_FIELDS = {
    "endpoint": ENDPOINTS,
    "outcome": OUTCOMES,
    "reason": REASONS,
    "status": frozenset({"executed", "cancelled"}),
    "stage": STAGES,
    "route": frozenset({"system_events", "native", "attachment"}),
    "termination": frozenset({"exit", "deadline", "stop", "spawn_error"}),
    "error_code": ERROR_CODES,
}
_TIMING_FIELDS = frozenset({"elapsed_ms", "poll_ms", "queue_ms", "lock_ms"})
_REMAINING_FIELDS = frozenset({"remaining_ms", "server_remaining_ms"})
_INTEGER_FIELDS = {
    "http_status": (0, 599),
    "curl_code": (0, 255),
    "exit_code": (-255, 255),
    "code": (-99999999, 99999999),
    "pid": (1, 2147483647),
}
_UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_SHA_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")
_CONTEXT = thread_context()
_CONFIG_LOCK = threading.Lock()  # Never acquired by trace or the writer.
_ENABLED = False
_ACTIVE = None
_WRITER = None


class _Sink:
    def __init__(self, path, writer):
        self.path = path  # Captured per generation; never read LOG_PATH in I/O.
        self.writer = writer
        self.dropped = 0  # Accessed only under the short queue mutex.


class _TraceQueue(queue.Queue):
    def put_nowait(self, item):
        # Queue coordination never includes serialization or file operations.
        # Count a full queue while holding the same short lock as its size test.
        with self.not_full:
            if self._qsize() >= self.maxsize:
                item[0].dropped = min(_MAX_DROPPED, item[0].dropped + 1)
                raise queue.Full
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()

    def drop(self, sink):
        with self.mutex:
            sink.dropped = min(_MAX_DROPPED, sink.dropped + 1)

    def dropped(self, sink):
        with self.mutex:
            return sink.dropped


def _encode(record):
    return (json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("ascii")


class _Writer:
    def __init__(self):
        self.queue = _TraceQueue(MAX_QUEUE_RECORDS)
        self.origin = time.monotonic()
        self.thread = threading.Thread(target=self.run, name="paste-diagnostics", daemon=True)

    def run(self):
        while True:
            sink, line, produced = self.queue.get()
            try:
                record = json.loads(line)
                # Producer timestamps describe the event. This separate value
                # shows queue/scheduling delay before I/O, capped at ten minutes.
                record["writer_delay_ms"] = round(min(600000, max(0, (time.monotonic() - produced) * 1000)), 3)
                dropped = self.queue.dropped(sink)
                if dropped:
                    record["dropped_records"] = dropped
                enriched = _encode(record)
                if len(enriched) >= MAX_LINE_BYTES or not _write_line(enriched, sink.path):
                    self.queue.drop(sink)
            except Exception:
                # Failed records are discarded once. Never recurse into trace,
                # retain an exception, retry an action, or enqueue a retry.
                self.queue.drop(sink)
            finally:
                self.queue.task_done()


def enable(path=None):
    """Enable a new sink generation, lazily; no file is opened on this thread."""
    global LOG_PATH, _ENABLED, _ACTIVE, _WRITER
    with _CONFIG_LOCK:
        _ENABLED = False
        _ACTIVE = None
        try:
            destination = DEFAULT_LOG_PATH if path is None else Path(path)
            if not destination.is_absolute():
                return
            if _WRITER is None:
                writer = _Writer()
                writer.thread.start()
                _WRITER = writer
            LOG_PATH = destination
            _ACTIVE = _Sink(destination, _WRITER)
            _ENABLED = True
        except Exception:
            pass


def disable():
    """Stop accepting records immediately; accepted records keep their old sink."""
    global _ENABLED, _ACTIVE
    with _CONFIG_LOCK:
        _ENABLED = False
        _ACTIVE = None


def flush(timeout=1.0):
    """Wait up to timeout seconds for accepted records to be attempted once.

    True means the queue drained, not that writes succeeded. This explicit wait
    is never used by trace, enable or disable. No indefinite joins or fsyncs.
    """
    try:
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 <= timeout <= 30:
            return False
        writer = _WRITER
        if writer is None:
            return True
        deadline = time.monotonic() + timeout
        pending = writer.queue
        with pending.all_tasks_done:
            while pending.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                pending.all_tasks_done.wait(remaining)
        return True
    except Exception:
        return False


def _action_id(value):
    if type(value) is uuid.UUID:
        return str(value)
    if type(value) is str and _UUID_PATTERN.fullmatch(value):
        return value.lower()
    return None


def current_action_id():
    """Return a validated correlation UUID for this thread, or None."""
    return getattr(_CONTEXT, "action_id", None)


@contextmanager
def action_context(action_id):
    """Temporarily correlate this thread; invalid IDs clear nested correlation."""
    previous = current_action_id()
    _CONTEXT.action_id = _action_id(action_id)
    try:
        yield
    finally:
        _CONTEXT.action_id = previous


def _metadata(fields):
    result = {}
    action_id = _action_id(fields.get("action_id", current_action_id()))
    if action_id is not None:
        result["action_id"] = action_id
    for name, value in fields.items():
        if name in _ENUM_FIELDS:
            if type(value) is str and value in _ENUM_FIELDS[name]:
                result[name] = value
        elif name == "source_sha":
            # This is the caller's source revision hash, never a content hash.
            if type(value) is str and _SHA_PATTERN.fullmatch(value):
                result[name] = value.lower()
        elif name in _INTEGER_FIELDS:
            low, high = _INTEGER_FIELDS[name]
            if type(value) is int and low <= value <= high:
                result[name] = value
        elif name in _TIMING_FIELDS or name in _REMAINING_FIELDS:
            low = -600000 if name in _REMAINING_FIELDS else 0
            if type(value) in {int, float} and low <= value <= 600000:
                if type(value) is int or math.isfinite(value):
                    result[name] = round(value, 3)
    return result


def _secure_directory(directory):
    """Open every path component without following links, creating lazily."""
    parts = Path(directory).parts
    if not parts or parts[0] != os.sep or any(p in {".", ".."} for p in parts):
        raise OSError("invalid_log_directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(os.sep, flags)
    try:
        for part in parts[1:]:
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        if os.fstat(fd).st_uid != os.geteuid():
            raise OSError("foreign_log_directory")
        os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _check_regular(info):
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise OSError("unsafe_log_file")


def _regular_file(directory_fd, name, writable=False, create=False):
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            return None, None
        before = None
    if before is not None:
        _check_regular(before)
    flags = os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    flags |= (os.O_WRONLY | os.O_APPEND) if writable else os.O_RDONLY
    if before is None:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        _check_regular(info)
        if before is not None and (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
            raise OSError("log_file_changed")
        os.fchmod(fd, 0o600)
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def _write_line(line, path):
    if len(line) >= MAX_LINE_BYTES or len(line) > MAX_LOG_BYTES:
        return False
    path = Path(path)
    directory_fd = _secure_directory(path.parent)
    file_fd = None
    try:
        # Refuse symlinks/special files anywhere in the rotation set before
        # touching the active file. Only these three exact names are managed.
        for suffix in range(1, LOG_FILE_COUNT):
            name = path.name + "." + str(suffix)
            archive_fd, info = _regular_file(directory_fd, name)
            if archive_fd is not None:
                os.close(archive_fd)
                if info.st_size > MAX_LOG_BYTES:
                    os.unlink(name, dir_fd=directory_fd)
        file_fd, info = _regular_file(directory_fd, path.name, writable=True, create=True)
        if info.st_size > MAX_LOG_BYTES:
            os.ftruncate(file_fd, 0)
            size = 0
        else:
            size = info.st_size
        if size + len(line) > MAX_LOG_BYTES:
            os.close(file_fd)
            file_fd = None
            for suffix in range(LOG_FILE_COUNT - 1, 0, -1):
                source = path.name if suffix == 1 else path.name + "." + str(suffix - 1)
                target = path.name + "." + str(suffix)
                try:
                    os.replace(source, target, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            file_fd, _ = _regular_file(directory_fd, path.name, writable=True, create=True)
            size = 0
        # One small append per record. A partial write must not leave a broken
        # JSON line for the next append by this process-local writer.
        if os.write(file_fd, line) != len(line):
            os.ftruncate(file_fd, size)
            return False
        return True
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def trace(event, **fields):
    """Offer bounded metadata without waiting for any writer file I/O/lock."""
    try:
        sink = _ACTIVE
        if sink is None or type(event) is not str or event not in EVENTS:
            return
        produced = time.monotonic()
        record = {"event": event, "at": round(time.time(), 3),
                  "mono_ms": round(max(0, (produced - sink.writer.origin) * 1000), 3), **_metadata(fields)}
        line = _encode(record)
        if len(line) + _WRITER_METADATA_RESERVE >= MAX_LINE_BYTES:
            return
        sink.writer.queue.put_nowait((sink, line, produced))
    except queue.Full:
        pass  # Counted by the queue, reported after the writer recovers.
    except Exception:
        # No arbitrary error text is retained or printed, and no fallback acts.
        pass
