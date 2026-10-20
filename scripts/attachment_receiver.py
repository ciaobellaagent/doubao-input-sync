"""Explicit attachment preparation and one-shot paste, sharing the text lock.

Downloads and image conversion do not access the clipboard or desktop focus.
Only a separately authorized, short-lived action may invoke native paste.
"""
from __future__ import annotations

from concurrent.futures import Future
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from paste_diagnostics import action_context, current_action_id, trace

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_CACHE_BYTES = 512 * 1024 * 1024
MAX_ACTIONS = 256
CACHE_ROOT = Path.home() / "Library/Application Support/DoubaoInputSync/attachments"
NATIVE = Path.home() / "Library/Application Support/DoubaoInputSync/native/workbuddy-paste"
_PREFETCH_SLOTS = threading.BoundedSemaphore(4)
REASONS = frozenset({"expired", "receiver_stopped", "focus_changed", "no_focused_input",
                     "unsafe_target", "permission_missing", "modifier_held", "clipboard_changed",
                     "paste_failed", "paste_unconfirmed", "mismatch", "download_failed",
                     "cache_full", "file_too_large", "preparation_failed"})
NATIVE_ERRORS = {"attachment_cache_unsafe": "mismatch", "attachment_too_large": "file_too_large",
                 "attachment_read_failed": "preparation_failed", "attachment_changed": "mismatch",
                 "attachment_write_failed": "preparation_failed", "attachment_prepare_failed": "preparation_failed",
                 "attachment_hash_mismatch": "mismatch"}


class AttachmentError(Exception):
    def __init__(self, code, status=None):
        self.code, self.status = code, status
        super().__init__(code)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def canonical_uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def metadata(value):
    if not isinstance(value, dict):
        raise AttachmentError("mismatch")
    name, size, digest = value.get("filename"), value.get("size"), value.get("sha256")
    if (not canonical_uuid(value.get("attachment_id")) or not isinstance(name, str)
            or not name or name.startswith(".") or name != name.strip()
            or any(c in name for c in "/\\:") or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or len(name.encode("utf-8")) > 240 or type(size) is not int or not 0 <= size <= MAX_FILE_BYTES
            or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
        raise AttachmentError("mismatch")
    return value


class AttachmentApi:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.http = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, operation, payload):
        if operation not in {"poll", "prepared", "ack"}:
            raise AttachmentError("mismatch")
        request = Request(self.base + "/api/attachments/" + operation,
                          json.dumps(payload).encode("utf-8"),
                          {"Content-Type": "application/json", "Accept": "application/json"})
        started, outcome, http_status = time.monotonic(), "ok", None
        try:
            with self.http.open(request, timeout=6) as response:
                raw = response.read(32769)
                http_status = response.status
            if len(raw) > 32768:
                raise AttachmentError("download_failed")
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise AttachmentError("download_failed")
            return result
        except HTTPError as error:
            outcome, http_status = "http_error", error.code
            # Never retain a proxy/login page, token, URL or arbitrary error text.
            raise AttachmentError("server_rejected", error.code) from None
        except (URLError, OSError, ValueError):
            outcome = "transport_failed"
            raise AttachmentError("download_failed") from None
        except AttachmentError:
            outcome = "invalid_response"
            raise
        finally:
            if operation != "poll" or outcome != "ok":
                trace("control", route="attachment", endpoint=operation, outcome=outcome,
                      http_status=http_status, elapsed_ms=(time.monotonic() - started) * 1000)

    def download(self, room, item, output, stopped):
        item = metadata(item)
        query = urlencode({"room_id": room, "attachment_id": item["attachment_id"]})
        deadline = time.monotonic() + 60
        digest, size = hashlib.sha256(), 0
        try:
            with self.http.open(self.base + "/api/attachments/blob?" + query, timeout=4) as response:
                length = response.headers.get("Content-Length")
                if length is None or int(length) != item["size"]:
                    raise AttachmentError("mismatch")
                while True:
                    if stopped() or time.monotonic() >= deadline:
                        raise AttachmentError("receiver_stopped" if stopped() else "download_failed")
                    # read(n) can wait for a whole block while a peer keeps
                    # trickling bytes. One transport read lets stop/deadline be
                    # checked between packets instead of resetting idle timers.
                    block = response.read1(64 * 1024)
                    if stopped():
                        raise AttachmentError("receiver_stopped")
                    if not block:
                        break
                    size += len(block)
                    if size > item["size"] or size > MAX_FILE_BYTES:
                        raise AttachmentError("file_too_large")
                    digest.update(block)
                    output.write(block)
        except (URLError, OSError, ValueError):
            raise AttachmentError("download_failed") from None
        if size != item["size"] or digest.hexdigest() != item["sha256"]:
            raise AttachmentError("mismatch")


def private_directory(path):
    path = Path(path)
    if not path.exists():
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    for parent in [path, *path.parents]:
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise AttachmentError("mismatch")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise AttachmentError("mismatch")
    return path


def file_digest(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_size > MAX_FILE_BYTES or info.st_mode & 0o022):
        raise AttachmentError("mismatch")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(block)
    return info.st_size, digest.hexdigest()


def run_native(command, stopped, deadline):
    if stopped() or time.monotonic() >= deadline:
        return "receiver_stopped" if stopped() else "expired"
    environment = {**os.environ, "DOUBAO_PASTE_TRACE_ID": current_action_id() or ""}
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=environment)
    try:
        while process.poll() is None:
            if stopped() or time.monotonic() >= deadline:
                process.terminate()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
                return "receiver_stopped" if stopped() else "expired"
            time.sleep(0.025)
        output = process.stdout.read(257)
        if process.returncode or len(output) > 256:
            return "paste_failed"
        value = output.decode("ascii", errors="replace").strip()
        if value in NATIVE_ERRORS:
            return NATIVE_ERRORS[value]
        return value if value in REASONS | {"paste_dispatched", "image_ready", "file_ready"} else "paste_failed"
    finally:
        process.stdout.close()


class AttachmentCache:
    def __init__(self, api, room, root=CACHE_ROOT, native=NATIVE):
        self.api, self.room, self.root, self.native = api, room, Path(root), Path(native)

    def prepare(self, preparation, stopped):
        item = metadata(preparation.get("attachment"))
        if not canonical_uuid(preparation.get("preparation_id")):
            raise AttachmentError("mismatch")
        root = private_directory(self.root)
        directory = private_directory(root / item["attachment_id"])
        path = directory / item["filename"]
        png = directory / ".clipboard.png"
        if path.exists() or path.is_symlink():
            if file_digest(path) != (item["size"], item["sha256"]):
                raise AttachmentError("mismatch")
        else:
            # The exact private cache is the only scanned directory. Do not
            # delete older files: an application may still read a pasted URL.
            used = 0
            for folder in root.iterdir():
                if not canonical_uuid(folder.name):
                    continue
                private_directory(folder)
                for file in folder.iterdir():
                    info = file.lstat()
                    if not stat.S_ISREG(info.st_mode):
                        raise AttachmentError("mismatch")
                    used += info.st_size
            if used + item["size"] > MAX_CACHE_BYTES:
                raise AttachmentError("cache_full")
            fd, temporary = tempfile.mkstemp(prefix=".download-", dir=directory)
            try:
                with os.fdopen(fd, "wb") as output:
                    self.api.download(self.room, item, output, stopped)
                    output.flush()
                    os.fsync(output.fileno())
                if stopped():
                    raise AttachmentError("receiver_stopped")
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        result = run_native([str(self.native), "--prepare-attachment", str(path), str(png)],
                            stopped, time.monotonic() + 20)
        if result not in {"image_ready", "file_ready"}:
            raise AttachmentError(result if result in REASONS else "preparation_failed")
        return {"attachment": item, "path": path,
                "png": png if result == "image_ready" else None,
                "preparation_id": preparation["preparation_id"]}


def preparation_task(cache, preparation, stopped):
    """At most four passive daemon jobs; disk stalls cannot hold text restart.

    A job can only produce a checked cache entry. The actor publishes readiness
    only while its original session is live; no job has a paste capability.
    """
    future = Future()

    def work():
        if not _PREFETCH_SLOTS.acquire(blocking=False):
            future.set_exception(AttachmentError("preparation_failed"))
            return
        try:
            if stopped():
                raise AttachmentError("receiver_stopped")
            future.set_result(cache.prepare(preparation, stopped))
        except BaseException as error:
            future.set_exception(error)
        finally:
            _PREFETCH_SLOTS.release()

    threading.Thread(target=work, name="attachment-download", daemon=True).start()
    return future


class AttachmentConsumer:
    def __init__(self, session_id, action_lock, native=NATIVE, paste=run_native, clock=time.monotonic):
        self.session_id, self.action_lock, self.native = session_id, action_lock, Path(native)
        self.paste, self.clock, self.seen = paste, clock, {}

    def consume(self, action, poll_started, prepared, stopped):
        if not isinstance(action, dict) or not canonical_uuid(action.get("action_id")):
            raise AttachmentError("mismatch")
        action_id = action["action_id"]
        if action_id in self.seen:
            return self.seen[action_id]
        if len(self.seen) >= MAX_ACTIONS:
            raise AttachmentError("cache_full")
        self.seen[action_id] = ("cancelled", "expired")
        remaining = action.get("remaining_ms")
        item = metadata(action.get("attachment"))
        if (action.get("session_id") != self.session_id or type(remaining) not in {int, float}
                or not math.isfinite(remaining) or not 0 < remaining <= 5000):
            return self.seen[action_id]
        deadline = poll_started + remaining / 1000
        if (not prepared or action.get("preparation_id") != prepared["preparation_id"]
                or item["attachment_id"] != prepared["attachment"]["attachment_id"]
                or item["sha256"] != prepared["attachment"]["sha256"]):
            self.seen[action_id] = ("cancelled", "mismatch")
            return self.seen[action_id]
        if not self.action_lock.acquire(timeout=max(0, deadline - self.clock())):
            return self.seen[action_id]
        try:
            if stopped() or deadline - self.clock() < 0.1:
                result = "receiver_stopped" if stopped() else "expired"
            else:
                command = [str(self.native), "--paste-attachment",
                           str((time.time() + deadline - self.clock()) * 1000),
                           str(prepared["path"]), item["sha256"],
                           str(prepared["png"]) if prepared["png"] else "-", str(os.getpid()), "0"]
                result = self.paste(command, stopped, deadline)
            self.seen[action_id] = (("executed", None) if result == "paste_dispatched" else
                                    ("cancelled", result if result in REASONS else "paste_failed"))
            return self.seen[action_id]
        finally:
            self.action_lock.release()


class AttachmentActor:
    def __init__(self, base, identity, action_lock, stopped, api=None, cache=None):
        self.api = api or AttachmentApi(base)
        self.identity, self.stopped = dict(identity), stopped
        self.local_stop, self.exhausted = threading.Event(), False
        self.consumer = AttachmentConsumer(identity["session_id"], action_lock)
        self.cache = cache or AttachmentCache(self.api, identity["room_id"])
        self.thread = threading.Thread(target=self.run, name="attachment-receiver", daemon=True)

    def stop(self):
        return self.local_stop.is_set() or self.stopped()

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.local_stop.set()
        self.thread.join(timeout=8)

    def acknowledge(self, action_id, status, reason):
        receipt = {**self.identity, "action_id": action_id, "status": status}
        if reason:
            receipt["reason"] = reason
        for attempt in range(2):
            try:
                self.api.request("ack", receipt)
                return
            except AttachmentError as error:
                if error.status == 409:
                    return
                if attempt or self.stop():
                    raise

    def run(self):
        future, preparing, prepared = None, None, None
        try:
            while not self.stop():
                try:
                    if future is not None and future.done():
                        report = {**self.identity, "preparation_id": preparing["preparation_id"]}
                        try:
                            prepared = future.result()
                            report["status"] = "ready"
                        except Exception as error:
                            prepared = None
                            report.update(status="failed", reason=error.code if isinstance(error, AttachmentError)
                                          and error.code in REASONS else "download_failed")
                        future = None
                        self.api.request("prepared", report)
                    started = time.monotonic()
                    payload = self.api.request("poll", self.identity)
                    if self.stop():
                        break
                    action = payload.get("action")
                    if action:
                        with action_context(action.get("action_id")):
                            trace("action_received", route="attachment", server_remaining_ms=action.get("remaining_ms"),
                                  poll_ms=(time.monotonic() - started) * 1000)
                            try:
                                status, reason = self.consumer.consume(action, started, prepared, self.stop)
                            except Exception:
                                status, reason = "cancelled", "paste_failed"
                            trace("action_result", route="attachment", status=status, reason=reason)
                            self.acknowledge(action["action_id"], status, reason)
                    preparation = payload.get("preparation")
                    if preparation and not preparation.get("ready") and not preparation.get("error") and future is None:
                        preparing = preparation
                        future = preparation_task(self.cache, preparation, self.stop)
                    if len(self.consumer.seen) >= MAX_ACTIONS:
                        self.exhausted = True
                        break
                except Exception:
                    # Isolated optional capability: no exception contents, URLs,
                    # filenames or body may enter diagnostics or kill text sync.
                    pass
                self.local_stop.wait(0.75)
        finally:
            self.local_stop.set()
