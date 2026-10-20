"""Private attachment archives and separately authorized, at-most-once delivery.

An upload never creates a paste action. Preparation only downloads an attachment;
the receiver may change the clipboard only after consuming a fresh action.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
from http import HTTPStatus
import math
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import threading
import time
import unicodedata
from urllib.parse import parse_qs, quote, unquote
from uuid import UUID, uuid4

try:
    from . import signal_trial as signal
except ImportError:
    import signal_trial as signal


MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_FILES = 512
CHUNK_BYTES = 64 * 1024
REASONS = frozenset({"download_failed", "hash_mismatch", "unsupported_attachment",
                     "clipboard_failed", "paste_failed", "paste_unconfirmed", "expired",
                     "focus_changed", "no_focused_input", "unsafe_target", "modifier_held",
                     "permission_missing", "receiver_stopped", "clipboard_changed",
                     "receiver_expired", "receiver_unregistered", "superseded", "mismatch",
                     "cache_full", "file_too_large", "preparation_failed"})
_INIT_LOCK = threading.Lock()


def uuid_value(value, name):
    # Canonical UUIDs only: no braces, URNs, truncation or filesystem syntax.
    if not isinstance(value, str):
        raise signal.SignalError("invalid_" + name)
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError):
        raise signal.SignalError("invalid_" + name) from None
    if canonical != value:
        raise signal.SignalError("invalid_" + name)
    return value


def clean_filename(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise signal.SignalError("invalid_attachment_name")
    value = value.replace("\\", "/").split("/")[-1].replace(":", "_")
    value = "".join(c for c in value if not unicodedata.category(c).startswith("C"))
    value = value.strip()
    if value.startswith(".") and "." not in value[1:]:
        value = "attachment" + value
    value = value.strip(" .") or "attachment.bin"
    suffix = Path(value).suffix
    if len(suffix.encode("utf-8")) > 24:
        suffix = ""
    stem = value[:-len(suffix)] if suffix else value
    budget = 220 - len(suffix.encode("utf-8"))
    stem = stem.encode("utf-8")[:budget].decode("utf-8", "ignore").rstrip(" .") or "attachment"
    return stem + suffix


def detect_media_type(head):
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        brands = {head[i:i + 4] for i in range(8, min(len(head), 64), 4)}
        if brands & {b"avif", b"avis"}:
            return "image/avif"
        if brands & {b"heic", b"heix", b"hevc", b"hevx"}:
            return "image/heic"
        if brands & {b"mif1", b"msf1"}:
            return "image/heif"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if head.startswith(b"BM"):
        return "image/bmp"
    return "application/octet-stream"


class AttachmentStore:
    def __init__(self, archive_path, *, max_file_bytes=MAX_FILE_BYTES,
                 max_total_bytes=MAX_TOTAL_BYTES, max_files=MAX_FILES):
        self.root = Path(archive_path).parent / "attachments"
        self.path = self.root / "attachments.sqlite3"
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_files = max_files
        self.lock = threading.RLock()
        # Up to four private temporary blobs (at most 80 MiB by default). A
        # client that trickles a body holds no archive, hub or signal lock.
        self.upload_slots = threading.BoundedSemaphore(4)

    def _connect(self):
        if self.root.is_symlink():
            raise signal.SignalError("attachment_storage_unavailable", HTTPStatus.SERVICE_UNAVAILABLE)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        if self.path.is_symlink():
            raise signal.SignalError("attachment_storage_unavailable", HTTPStatus.SERVICE_UNAVAILABLE)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        connection = sqlite3.connect(str(self.path), timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("""CREATE TABLE IF NOT EXISTS attachments (
            room_id TEXT NOT NULL, attachment_id TEXT NOT NULL, filename TEXT NOT NULL,
            size INTEGER NOT NULL, sha256 TEXT NOT NULL, media_type TEXT NOT NULL,
            created_at TEXT NOT NULL, blob_name TEXT NOT NULL UNIQUE,
            PRIMARY KEY (room_id, attachment_id))""")
        connection.commit()
        return connection

    @staticmethod
    def _metadata(row):
        return {key: row[key] for key in ("attachment_id", "filename", "size", "sha256",
                                         "media_type", "created_at")}

    def _row(self, connection, room, attachment_id):
        row = connection.execute("SELECT * FROM attachments WHERE room_id = ? AND attachment_id = ?",
                                 (room, attachment_id)).fetchone()
        if row is None:
            raise signal.SignalError("attachment_not_found", HTTPStatus.NOT_FOUND)
        return row

    def metadata(self, room, attachment_id):
        with self.lock:
            connection = self._connect()
            try:
                return self._metadata(self._row(connection, room, attachment_id))
            finally:
                connection.close()

    def items(self, room):
        with self.lock:
            connection = self._connect()
            try:
                return [self._metadata(row) for row in connection.execute(
                    "SELECT * FROM attachments WHERE room_id = ? ORDER BY created_at DESC, rowid DESC",
                    (room,)).fetchall()]
            finally:
                connection.close()

    def upload(self, room, attachment_id, filename, source, size, expected_sha256=None):
        if size < 0:
            raise signal.SignalError("invalid_content_length")
        if size > self.max_file_bytes:
            raise signal.SignalError("attachment_too_large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        if expected_sha256 is not None and (not isinstance(expected_sha256, str) or
                                             re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None):
            raise signal.SignalError("invalid_sha256")
        filename = clean_filename(filename)
        if not self.upload_slots.acquire(blocking=False):
            raise signal.SignalError("attachment_upload_busy", HTTPStatus.TOO_MANY_REQUESTS)
        blob_path, committed = None, False
        try:
            # Early rejection saves reading a body when the persisted quota is
            # already full. No SQL transaction is kept while receiving bytes.
            with self.lock:
                connection = self._connect()
                try:
                    self._upload_baseline(connection, room, attachment_id, size)
                finally:
                    connection.close()
            blob_name = str(uuid4()) + ".blob"
            blob_path = self.root / blob_name
            fd = os.open(str(blob_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, "O_NOFOLLOW", 0), 0o600)
            digest, head, remaining = hashlib.sha256(), b"", size
            deadline = time.monotonic() + 90
            read_chunk = source.read1 if isinstance(source, io.BufferedReader) else source.read
            with os.fdopen(fd, "wb") as sink:
                while remaining:
                    if time.monotonic() >= deadline:
                        raise signal.SignalError("incomplete_body", HTTPStatus.REQUEST_TIMEOUT)
                    chunk = read_chunk(min(CHUNK_BYTES, remaining))
                    if time.monotonic() >= deadline:
                        raise signal.SignalError("incomplete_body", HTTPStatus.REQUEST_TIMEOUT)
                    if not chunk or len(chunk) > min(CHUNK_BYTES, remaining):
                        raise signal.SignalError("incomplete_body")
                    digest.update(chunk)
                    if len(head) < 64:
                        head = (head + chunk)[:64]
                    sink.write(chunk)
                    remaining -= len(chunk)
                sink.flush()
                os.fsync(sink.fileno())
            sha256 = digest.hexdigest()
            if expected_sha256 is not None and not secrets.compare_digest(sha256, expected_sha256):
                raise signal.SignalError("hash_mismatch", HTTPStatus.CONFLICT)
            with self.lock:
                connection = self._connect()
                try:
                    # Other uploads/deletions may have committed during the
                    # slow stream. Recheck quota and identity atomically now.
                    connection.execute("BEGIN IMMEDIATE")
                    existing = self._upload_baseline(connection, room, attachment_id, size)
                    if existing is not None:
                        if existing["size"] != size or not secrets.compare_digest(existing["sha256"], sha256):
                            raise signal.SignalError("attachment_id_conflict", HTTPStatus.CONFLICT)
                        return self._metadata(existing)
                    created_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                    connection.execute("INSERT INTO attachments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                       (room, attachment_id, filename, size, sha256,
                                        detect_media_type(head), created_at, blob_name))
                    result = self._metadata(self._row(connection, room, attachment_id))
                    connection.commit()
                    committed = True
                    return result
                finally:
                    connection.close()
        finally:
            try:
                if blob_path is not None and not committed:
                    blob_path.unlink(missing_ok=True)
            finally:
                self.upload_slots.release()

    def _upload_baseline(self, connection, room, attachment_id, size):
        existing = connection.execute(
            "SELECT * FROM attachments WHERE room_id = ? AND attachment_id = ?",
            (room, attachment_id)).fetchone()
        count, total = connection.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM attachments").fetchone()
        if existing is None and (count >= self.max_files or total + size > self.max_total_bytes):
            raise signal.SignalError("attachment_quota_reached", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        return existing

    def open_blob(self, room, attachment_id):
        with self.lock:
            connection = self._connect()
            try:
                row = self._row(connection, room, attachment_id)
                # Disk names originate here, never from client paths or URLs.
                if re.fullmatch(r"[0-9a-f-]{36}\.blob", row["blob_name"]) is None:
                    raise signal.SignalError("attachment_integrity_failed", HTTPStatus.CONFLICT)
                fd = os.open(str(self.root / row["blob_name"]), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                source = os.fdopen(fd, "rb")
                metadata = self._metadata(row)
            finally:
                connection.close()
        # The open descriptor pins the snapshot even if its archive is deleted.
        # Hashing or a slow disk never holds the archive's metadata lock.
        try:
            digest, count = hashlib.sha256(), 0
            while True:
                chunk = source.read(CHUNK_BYTES)
                if not chunk:
                    break
                count += len(chunk)
                if count > self.max_file_bytes:
                    raise signal.SignalError("attachment_integrity_failed", HTTPStatus.CONFLICT)
                digest.update(chunk)
            if count != metadata["size"] or not secrets.compare_digest(digest.hexdigest(), metadata["sha256"]):
                raise signal.SignalError("attachment_integrity_failed", HTTPStatus.CONFLICT)
            source.seek(0)
            return metadata, source
        except Exception:
            source.close()
            raise

    def delete(self, room, attachment_id):
        with self.lock:
            connection = self._connect()
            try:
                with connection:
                    row = self._row(connection, room, attachment_id)
                    connection.execute("DELETE FROM attachments WHERE room_id = ? AND attachment_id = ?",
                                       (room, attachment_id))
                # A crash may leave an unreferenced private blob, never a replayable action.
                (self.root / row["blob_name"]).unlink(missing_ok=True)
            finally:
                connection.close()


@dataclass
class Preparation:
    room: str
    session_id: str
    preparation_id: str
    attachment: dict
    deadline: float
    ready: bool = False
    error: str | None = None

    def public(self, now):
        return {"preparation_id": self.preparation_id, "attachment": dict(self.attachment),
                "ready": self.ready, "remaining_ms": max(0, math.floor((self.deadline - now) * 1000)),
                **({"error": self.error} if self.error else {})}


@dataclass
class Action:
    room: str
    session_id: str
    action_id: str
    preparation_id: str
    attachment: dict
    token_hash: bytes
    deadline: float
    receipt_deadline: float
    status: str = "accepted"
    reason: str | None = None

    def public(self):
        return {"action_id": self.action_id, "status": self.status,
                **({"reason": self.reason} if self.reason else {})}


class AttachmentHub:
    """Finite control state; archives and process restarts can never replay actions.

    Control locks nest SignalStore.changed -> hub.lock. Archive I/O happens
    outside both: slow disk/upload work cannot stop text receiver heartbeats.
    Action IDs remain tombstoned until restart; pending ACKs are never evicted.
    """
    def __init__(self, archive, signals, *, clock=time.monotonic, max_actions=4096, max_rooms=128):
        self.archive, self.signals, self.clock = archive, signals, clock
        self.max_actions, self.max_rooms = max_actions, max_rooms
        self.lock = threading.RLock()
        self.receivers = {}
        self.preparations = {}
        self.actions = {}
        self.latest = {}
        self.deleting = set()
        self.archive_revision = 0

    def _expire(self):
        now = self.clock()
        for room, (session_id, observed) in list(self.receivers.items()):
            current = self.signals.rooms.get(room)
            if (now - observed >= 6 or current is None or not current.active or
                    current.session_id != session_id):
                del self.receivers[room]
        for room, preparation in list(self.preparations.items()):
            current = self.signals.rooms.get(room)
            if (now >= preparation.deadline or current is None or not current.active or
                    current.session_id != preparation.session_id):
                del self.preparations[room]
        for action in self.actions.values():
            if action.status not in {"accepted", "delivered"}:
                continue
            current = self.signals.rooms.get(action.room)
            if action.status == "accepted" and (current is None or not current.active or
                                                   current.session_id != action.session_id):
                action.status, action.reason = "cancelled", "receiver_expired"
            elif now >= (action.receipt_deadline if action.status == "delivered" else action.deadline):
                action.status, action.reason = "expired", "expired"

    def _active(self, room, session_id, token=None):
        session = (self.signals._receiver(room, session_id, token) if token is not None else
                   self.signals._current(room, session_id))
        self._expire()
        return session

    def _ready(self, room, session_id):
        receiver = self.receivers.get(room)
        return bool(receiver and receiver[0] == session_id and self.clock() - receiver[1] < 6)

    def _outstanding(self, room):
        return any(a.room == room and a.status in {"accepted", "delivered"} for a in self.actions.values())

    def status(self, room, action_id=None):
        items = self.archive.items(room)
        with self.signals.changed, self.lock:
            current = self.signals.rooms.get(room)
            if current is not None:
                self.signals._expire(current)
            self._expire()
            active = current is not None and current.active
            preparation = self.preparations.get(room)
            action = self.actions.get(action_id or self.latest.get(room))
            if action and action.room != room:
                action = None
            return {"ok": True, "items": items,
                    "limits": {"max_file_bytes": self.archive.max_file_bytes,
                               "max_total_bytes": self.archive.max_total_bytes},
                    "receiver": {"ready": bool(active and self._ready(room, current.session_id)),
                                 "session_id": current.session_id if active else None},
                    "preparation": preparation.public(self.clock()) if preparation else None,
                    "action": action.public() if action else None}

    def prepare(self, room, session_id, preparation_id, attachment_id):
        with self.lock:
            revision = self.archive_revision
        metadata = self.archive.metadata(room, attachment_id)
        with self.signals.changed, self.lock:
            self._active(room, session_id)
            if revision != self.archive_revision or (room, attachment_id) in self.deleting:
                raise signal.SignalError("attachment_changed", HTTPStatus.CONFLICT)
            if not self._ready(room, session_id):
                raise signal.SignalError("attachment_receiver_unavailable", HTTPStatus.CONFLICT)
            if self._outstanding(room):
                raise signal.SignalError("action_in_progress", HTTPStatus.CONFLICT)
            prior = self.preparations.get(room)
            if prior and prior.preparation_id == preparation_id:
                if prior.attachment["attachment_id"] != attachment_id:
                    raise signal.SignalError("preparation_id_conflict", HTTPStatus.CONFLICT)
                return {"ok": True, "preparation": prior.public(self.clock())}
            preparation = Preparation(room, session_id, preparation_id,
                                      metadata, self.clock() + 60)
            self.preparations[room] = preparation
            return {"ok": True, "preparation": preparation.public(self.clock())}

    def prepared(self, room, session_id, token, preparation_id, status, reason=None):
        with self.signals.changed, self.lock:
            self._active(room, session_id, token)
            preparation = self.preparations.get(room)
            if preparation is None or preparation.preparation_id != preparation_id:
                raise signal.SignalError("preparation_mismatch", HTTPStatus.CONFLICT)
            preparation.ready, preparation.error = status == "ready", reason if status == "failed" else None
            return {"ok": True, "preparation": preparation.public(self.clock())}

    def action(self, room, session_id, preparation_id, action_id):
        with self.signals.changed, self.lock:
            session = self._active(room, session_id)
            prior = self.actions.get(action_id)
            if prior is not None:
                if (prior.room, prior.session_id, prior.preparation_id) != (room, session_id, preparation_id):
                    raise signal.SignalError("action_id_conflict", HTTPStatus.CONFLICT)
                return {"ok": True, "action": prior.public()}
            if len(self.actions) >= self.max_actions:
                raise signal.SignalError("attachment_action_capacity_reached", HTTPStatus.TOO_MANY_REQUESTS)
            if not self._ready(room, session_id):
                raise signal.SignalError("attachment_receiver_unavailable", HTTPStatus.CONFLICT)
            if self._outstanding(room):
                raise signal.SignalError("action_in_progress", HTTPStatus.CONFLICT)
            preparation = self.preparations.get(room)
            if preparation is None or preparation.preparation_id != preparation_id:
                raise signal.SignalError("preparation_mismatch", HTTPStatus.CONFLICT)
            if not preparation.ready or preparation.error:
                raise signal.SignalError("preparation_not_ready", HTTPStatus.CONFLICT)
            if (room, preparation.attachment["attachment_id"]) in self.deleting:
                raise signal.SignalError("attachment_in_use", HTTPStatus.CONFLICT)
            now = self.clock()
            action = Action(room, session_id, action_id, preparation_id, dict(preparation.attachment),
                            hashlib.sha256(session.receiver_token.encode("ascii")).digest(), now + 5, now + 20)
            self.actions[action_id] = action
            self.latest[room] = action_id
            return {"ok": True, "action": action.public()}

    def poll(self, room, session_id, token):
        with self.signals.changed, self.lock:
            self._active(room, session_id, token)
            if room not in self.receivers and len(self.receivers) >= self.max_rooms:
                raise signal.SignalError("attachment_receiver_capacity_reached", HTTPStatus.TOO_MANY_REQUESTS)
            self.receivers[room] = (session_id, self.clock())
            action = next((a for a in self.actions.values() if a.room == room and
                           a.session_id == session_id and a.status == "accepted"), None)
            delivery = None
            if action is not None:
                # Consume before serializing/writing: a lost response is never retried.
                remaining_ms = math.floor((action.deadline - self.clock()) * 1000)
                if remaining_ms > 0:
                    action.status = "delivered"
                    action.receipt_deadline = self.clock() + 15
                    delivery = {"action_id": action.action_id, "session_id": session_id,
                                "remaining_ms": remaining_ms,
                                "preparation_id": action.preparation_id, "attachment": dict(action.attachment)}
                else:
                    action.status, action.reason = "expired", "expired"
            preparation = self.preparations.get(room)
            return {"ok": True, "preparation": preparation.public(self.clock()) if preparation else None,
                    "action": delivery}

    def ack(self, room, session_id, token, action_id, status, reason=None):
        with self.signals.changed, self.lock:
            self._expire()
            action = self.actions.get(action_id)
            if action is None or (action.room, action.session_id) != (room, session_id):
                raise signal.SignalError("action_unknown", HTTPStatus.NOT_FOUND)
            if not secrets.compare_digest(action.token_hash, hashlib.sha256(token.encode("ascii")).digest()):
                raise signal.SignalError("receiver_token_invalid", HTTPStatus.FORBIDDEN)
            if action.status in {"executed", "cancelled", "expired"}:
                if (action.status, action.reason) != (status, reason):
                    raise signal.SignalError("action_already_finished", HTTPStatus.CONFLICT)
            elif action.status != "delivered":
                raise signal.SignalError("action_not_delivered", HTTPStatus.CONFLICT)
            else:
                action.status, action.reason = status, reason
            return {"ok": True, "action": action.public()}

    def delete(self, room, attachment_id):
        # Resolve unknown identities without holding any control lock.
        self.archive.metadata(room, attachment_id)
        key = (room, attachment_id)
        with self.signals.changed, self.lock:
            self._expire()
            if any(a.room == room and a.attachment["attachment_id"] == attachment_id and
                   a.status in {"accepted", "delivered"} for a in self.actions.values()):
                raise signal.SignalError("attachment_in_use", HTTPStatus.CONFLICT)
            if key in self.deleting:
                raise signal.SignalError("attachment_in_use", HTTPStatus.CONFLICT)
            if len(self.deleting) >= self.archive.max_files:
                raise signal.SignalError("attachment_delete_busy", HTTPStatus.TOO_MANY_REQUESTS)
            self.deleting.add(key)
            self.archive_revision += 1
        try:
            self.archive.delete(room, attachment_id)
        finally:
            with self.lock:
                preparation = self.preparations.get(room)
                if preparation and preparation.attachment["attachment_id"] == attachment_id:
                    del self.preparations[room]
                self.deleting.discard(key)
                self.archive_revision += 1
        return {"ok": True}


def _hub(handler):
    with _INIT_LOCK:
        if not hasattr(handler.server, "attachment_hub"):
            handler.server.attachment_hub = AttachmentHub(
                AttachmentStore(handler.copy_store.path), signal._store(handler))
        return handler.server.attachment_hub


def _query(query, required, optional=()):
    values = parse_qs(query, keep_blank_values=True)
    if not set(required) <= values.keys() or set(values) - set(required) - set(optional) or any(
            len(items) != 1 for items in values.values()):
        raise signal.SignalError("invalid_query")
    return {key: items[0] for key, items in values.items()}


def _error(handler, exc):
    handler.close_connection = True
    handler._send_json({"ok": False, "error": exc.reason}, status=exc.status)


def handle_get(handler, path, query):
    if path not in {"/api/attachments", "/api/attachments/blob"}:
        return False
    try:
        values = _query(query, {"room_id", "attachment_id"} if path.endswith("/blob") else {"room_id"},
                        () if path.endswith("/blob") else {"action_id"})
        room = signal.room_id(values["room_id"])
        hub = _hub(handler)
        if path == "/api/attachments":
            action_id = uuid_value(values["action_id"], "action_id") if "action_id" in values else None
            handler._send_json(hub.status(room, action_id))
        else:
            attachment_id = uuid_value(values["attachment_id"], "attachment_id")
            metadata, source = hub.archive.open_blob(room, attachment_id)
            with source:
                handler.send_response(HTTPStatus.OK)
                handler.send_header("Content-Type", metadata["media_type"])
                handler.send_header("Content-Length", str(metadata["size"]))
                handler.send_header("Content-Disposition", "attachment; filename=\"attachment.bin\"; filename*=UTF-8''" +
                                    quote(metadata["filename"], safe=""))
                handler.send_header("X-Content-Type-Options", "nosniff")
                handler.send_header("Cache-Control", "no-store")
                handler.end_headers()
                while True:
                    chunk = source.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
    except signal.SignalError as exc:
        _error(handler, exc)
    except (OSError, sqlite3.Error):
        _error(handler, signal.SignalError("attachment_storage_unavailable", HTTPStatus.SERVICE_UNAVAILABLE))
    return True


def handle_post(handler, path, query=""):
    if not path.startswith("/api/attachments/"):
        return False
    try:
        operation = path[len("/api/attachments/"):]
        if operation == "upload":
            values = _query(query, {"room_id", "attachment_id"})
            room = signal.room_id(values["room_id"])
            attachment_id = uuid_value(values["attachment_id"], "attachment_id")
            if handler.headers.get("Transfer-Encoding"):
                raise signal.SignalError("transfer_encoding_not_supported")
            lengths = handler.headers.get_all("Content-Length", [])
            if not lengths:
                raise signal.SignalError("content_length_required", HTTPStatus.LENGTH_REQUIRED)
            if len(lengths) != 1 or len(lengths[0]) > 12 or re.fullmatch(r"[0-9]+", lengths[0]) is None:
                raise signal.SignalError("invalid_content_length")
            names = handler.headers.get_all("X-Attachment-Name", [])
            if len(names) != 1 or re.search(r"%(?![0-9a-fA-F]{2})", names[0]):
                raise signal.SignalError("invalid_attachment_name")
            try:
                filename = unquote(names[0], encoding="utf-8", errors="strict")
            except UnicodeDecodeError:
                raise signal.SignalError("invalid_attachment_name") from None
            hashes = handler.headers.get_all("X-Attachment-SHA256", [])
            if len(hashes) > 1:
                raise signal.SignalError("invalid_sha256")
            old_timeout = handler.connection.gettimeout()
            try:
                handler.connection.settimeout(10)
                metadata = _hub(handler).archive.upload(room, attachment_id, filename, handler.rfile,
                                                      int(lengths[0]), hashes[0] if hashes else None)
            except (socket.timeout, TimeoutError):
                raise signal.SignalError("incomplete_body", HTTPStatus.REQUEST_TIMEOUT) from None
            finally:
                handler.connection.settimeout(old_timeout)
            handler._send_json({"ok": True, "attachment": metadata})
            return True
        if query:
            raise signal.SignalError("invalid_query")
        if operation not in {"prepare", "prepared", "action", "poll", "ack", "delete"}:
            raise signal.SignalError("attachment_endpoint_not_found", HTTPStatus.NOT_FOUND)
        payload = signal._read_json(handler)
        required = {"room_id"}
        if operation != "delete":
            required.add("session_id")
        if operation in {"poll", "prepared", "ack"}:
            required.add("receiver_token")
        if operation in {"prepare", "prepared", "action"}:
            required.add("preparation_id")
        if operation in {"prepare", "delete"}:
            required.add("attachment_id")
        if operation in {"action", "ack"}:
            required.add("action_id")
        if operation in {"prepared", "ack"}:
            required.add("status")
        signal._keys(payload, required, {"reason"} if operation in {"prepared", "ack"} else None)
        room = signal.room_id(payload.pop("room_id"))
        for key in ("session_id", "preparation_id", "attachment_id", "action_id"):
            if key in payload:
                payload[key] = uuid_value(payload[key], key)
        if "receiver_token" in payload:
            payload["token"] = signal._token(payload.pop("receiver_token"))
        if operation in {"prepared", "ack"}:
            allowed = {"ready", "failed"} if operation == "prepared" else {"executed", "cancelled"}
            if not isinstance(payload["status"], str) or payload["status"] not in allowed:
                raise signal.SignalError("invalid_status")
            if "reason" in payload and (not isinstance(payload["reason"], str) or payload["reason"] not in REASONS):
                raise signal.SignalError("invalid_reason")
            if payload["status"] in {"ready", "executed"} and "reason" in payload:
                raise signal.SignalError("success_cannot_have_reason")
            if payload["status"] == "failed" and "reason" not in payload:
                payload["reason"] = "download_failed"
        handler._send_json(getattr(_hub(handler), operation)(room, **payload))
    except signal.SignalError as exc:
        _error(handler, exc)
    except (OSError, sqlite3.Error):
        _error(handler, signal.SignalError("attachment_storage_unavailable", HTTPStatus.SERVICE_UNAVAILABLE))
    return True
