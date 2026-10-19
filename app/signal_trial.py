"""Bounded ephemeral paste authorization and passive clipboard preparation.

Copy archives and normal relay deliveries deliberately never enter this module.
Delivery is at most once: a poll consumes an action before writing its response.
Preparation carries only a digest; it never authorizes a paste by itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
import json
import math
import re
from pathlib import Path
import secrets
import socket
import threading
import time
from typing import Callable
from urllib.parse import parse_qs, unquote
from uuid import UUID, uuid4


MAX_BODY_BYTES = 4096
ACK_REASONS = frozenset({
    "focus_changed", "no_focused_input", "unsafe_target", "expired",
    "receiver_stopped", "paste_failed", "receiver_expired",
    "receiver_unregistered", "permission_missing", "modifier_held", "superseded",
    "clipboard_timeout", "clipboard_changed", "mismatch", "paste_unconfirmed",
})
TERMINAL_STATUSES = frozenset({"executed", "cancelled", "expired"})
_INIT_LOCK = threading.Lock()


class SignalError(ValueError):
    def __init__(self, reason: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def room_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or "\x00" in value:
        raise SignalError("invalid_room_id")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise SignalError("invalid_room_id") from None
    return value.strip()


def uuid_value(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise SignalError("invalid_" + name)
    try:
        return str(UUID(value))
    except ValueError:
        raise SignalError("invalid_" + name) from None


MATCH_PROTOCOL = "clipboard-match-v1"
LEGACY_PROTOCOL = "signal-v1"


@dataclass
class Preparation:
    preparation_id: str
    sha256: str
    deadline: float
    ready: bool = False

    def public(self, now: float) -> dict:
        return {"preparation_id": self.preparation_id, "sha256": self.sha256,
                "ready": self.ready, "remaining_ms": max(0, math.floor((self.deadline - now) * 1000))}


@dataclass
class Action:
    action_id: str
    deadline: float
    preparation_id: str | None = None
    sha256: str | None = None
    status: str = "accepted"
    reason: str | None = None
    receipt_deadline: float | None = None

    def public(self) -> dict:
        result = {"action_id": self.action_id, "status": self.status}
        if self.reason is not None:
            result["reason"] = self.reason
        return result


@dataclass
class Session:
    session_id: str
    receiver_token: str
    last_poll_completed: float
    protocol: str = LEGACY_PROTOCOL
    preparation: Preparation | None = None
    active: bool = True
    polling: bool = False
    actions: dict[str, Action] = field(default_factory=dict)
    outstanding_id: str | None = None
    latest_id: str | None = None


class SignalStore:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 poll_seconds: float = 1.0, lease_seconds: float = 6.0,
                 action_seconds: float = 5.0, max_actions: int = 256,
                 max_rooms: int = 128, receipt_seconds: float = 10.0,
                 history_seconds: float = 120.0, max_previous_sessions: int = 4):
        self.clock = clock
        self.poll_seconds = min(max(poll_seconds, 0.0), 1.0)
        self.lease_seconds = lease_seconds
        self.action_seconds = action_seconds
        self.max_actions = max_actions
        self.max_rooms = max_rooms
        self.receipt_seconds = receipt_seconds
        self.history_seconds = history_seconds
        self.max_previous_sessions = max_previous_sessions
        self.rooms: dict[str, Session] = {}
        # Readback/ACK only. A retained session can never authorize another paste.
        self.previous: dict[str, list[tuple[float, Session]]] = {}
        self.changed = threading.Condition()

    def _expire(self, session: Session) -> None:
        now = self.clock()
        if session.preparation and now >= session.preparation.deadline:
            session.preparation = None
        for action in session.actions.values():
            cutoff = action.receipt_deadline if action.status == "delivered" else action.deadline
            if action.status in {"accepted", "delivered"} and now >= cutoff:
                action.status, action.reason = "expired", "expired"
                if session.outstanding_id == action.action_id:
                    session.outstanding_id = None
        if session.active and now - session.last_poll_completed >= self.lease_seconds:
            self._close(session, "receiver_expired")

    def _close(self, session: Session, reason: str) -> None:
        session.active = False
        if session.outstanding_id is not None:
            action = session.actions[session.outstanding_id]
            if action.status == "accepted":
                action.status, action.reason = "cancelled", reason
            session.outstanding_id = None
        self.changed.notify_all()

    def _current(self, room: str, session_id: str) -> Session:
        session = self.rooms.get(room)
        if session is None:
            raise SignalError("receiver_unavailable", HTTPStatus.CONFLICT)
        self._expire(session)
        if session.session_id != session_id:
            raise SignalError("session_mismatch", HTTPStatus.CONFLICT)
        if not session.active:
            raise SignalError("receiver_unavailable", HTTPStatus.CONFLICT)
        return session

    def _receiver(self, room: str, session_id: str, token: str) -> Session:
        session = self._current(room, session_id)
        if not secrets.compare_digest(session.receiver_token, token):
            raise SignalError("receiver_token_invalid", HTTPStatus.FORBIDDEN)
        return session

    def _previous(self, room: str) -> list[Session]:
        now = self.clock()
        retained = [(until, session) for until, session in self.previous.get(room, []) if now < until]
        if retained:
            self.previous[room] = retained
        else:
            self.previous.pop(room, None)
        for _, session in retained:
            self._expire(session)
        return [session for _, session in retained]

    def _receipt_receiver(self, room: str, session_id: str, token: str) -> Session:
        session = self.rooms.get(room)
        candidates = ([session] if session else []) + self._previous(room)
        for candidate in candidates:
            if candidate.session_id == session_id:
                self._expire(candidate)
                if not secrets.compare_digest(candidate.receiver_token, token):
                    raise SignalError("receiver_token_invalid", HTTPStatus.FORBIDDEN)
                return candidate
        raise SignalError("session_mismatch", HTTPStatus.CONFLICT)

    def register(self, room: str, protocol: str = LEGACY_PROTOCOL) -> dict:
        if not isinstance(protocol, str) or protocol not in {LEGACY_PROTOCOL, MATCH_PROTOCOL}:
            raise SignalError("invalid_protocol")
        with self.changed:
            prior = self.rooms.get(room)
            if prior is not None:
                self._expire(prior)
                if prior.active:
                    raise SignalError("receiver_already_registered", HTTPStatus.CONFLICT)
            if prior is None and len(self.rooms) >= self.max_rooms:
                for key, candidate in list(self.rooms.items()):
                    self._expire(candidate)
                    retained = self._previous(key)
                    awaiting_receipt = any(a.status == "delivered"
                                           for old in [candidate, *retained] for a in old.actions.values())
                    if not candidate.active and not candidate.polling and not awaiting_receipt:
                        del self.rooms[key]
                        self.previous.pop(key, None)
                        break
                if len(self.rooms) >= self.max_rooms:
                    raise SignalError("signal_capacity_reached", HTTPStatus.TOO_MANY_REQUESTS)
            session = Session(str(uuid4()), secrets.token_urlsafe(32), self.clock(), protocol=protocol)
            self._previous(room)
            if prior is not None:
                retained = self.previous.setdefault(room, [])
                if len(retained) >= self.max_previous_sessions:
                    removable = next((index for index, (_, old) in enumerate(retained)
                                      if not any(a.status == "delivered" for a in old.actions.values())), None)
                    if removable is None:
                        raise SignalError("signal_capacity_reached", HTTPStatus.TOO_MANY_REQUESTS)
                    del retained[removable]
                retained.append((self.clock() + self.history_seconds, prior))
            self.rooms[room] = session
            self.changed.notify_all()
            return {"ok": True, "session_id": session.session_id, "receiver_token": session.receiver_token, "protocol": session.protocol}

    def status(self, room: str, action_id: str | None = None) -> dict:
        with self.changed:
            session = self.rooms.get(room)
            if session is None:
                return {"ok": True, "ready": False, "session_id": None, "action": None}
            self._expire(session)
            action = session.actions.get(action_id if action_id is not None else session.latest_id)
            if action is None and action_id is not None:
                action = next((old.actions[action_id] for old in reversed(self._previous(room))
                               if action_id in old.actions), None)
            return {"ok": True, "ready": session.active,
                    "session_id": session.session_id if session.active else None,
                    "action": action.public() if action is not None else None,
                    "protocol": session.protocol,
                    "preparation": session.preparation.public(self.clock()) if session.active and session.preparation else None}

    def prepare(self, room: str, session_id: str, preparation_id: str, sha256: str) -> dict:
        if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
            raise SignalError("invalid_sha256")
        with self.changed:
            session = self._current(room, session_id)
            if session.protocol != MATCH_PROTOCOL:
                raise SignalError("protocol_mismatch", HTTPStatus.CONFLICT)
            prior = session.preparation
            if prior and prior.preparation_id == preparation_id and prior.sha256 != sha256:
                raise SignalError("preparation_id_conflict", HTTPStatus.CONFLICT)
            if session.outstanding_id is not None:
                raise SignalError("action_in_progress", HTTPStatus.CONFLICT)
            if prior and prior.preparation_id == preparation_id:
                prior.deadline = self.clock() + 60
            else:
                session.preparation = Preparation(preparation_id, sha256, self.clock() + 60)
            self.changed.notify_all()
            return {"ok": True, "session_id": session_id,
                    "preparation": session.preparation.public(self.clock())}

    def prepared(self, room: str, session_id: str, token: str, preparation_id: str) -> dict:
        with self.changed:
            session = self._receiver(room, session_id, token)
            preparation = session.preparation
            if session.protocol != MATCH_PROTOCOL or preparation is None or preparation.preparation_id != preparation_id:
                raise SignalError("preparation_mismatch", HTTPStatus.CONFLICT)
            preparation.ready = True
            self.changed.notify_all()
            return {"ok": True, "session_id": session_id,
                    "preparation": preparation.public(self.clock())}

    def action(self, room: str, session_id: str, action_id: str,
               preparation_id: str | None = None) -> dict:
        with self.changed:
            session = self._current(room, session_id)
            existing = session.actions.get(action_id)
            if existing is not None:
                if existing.preparation_id != preparation_id:
                    raise SignalError("action_id_conflict", HTTPStatus.CONFLICT)
                return {"ok": True, "session_id": session_id, "action": existing.public()}
            if any(action_id in old.actions for old in self._previous(room)):
                raise SignalError("action_id_conflict", HTTPStatus.CONFLICT)
            if len(session.actions) >= self.max_actions:
                raise SignalError("action_limit_reached", HTTPStatus.CONFLICT)
            if session.outstanding_id is not None:
                raise SignalError("action_in_progress", HTTPStatus.CONFLICT)
            preparation = session.preparation
            if session.protocol == MATCH_PROTOCOL:
                if preparation is None or preparation.preparation_id != preparation_id:
                    raise SignalError("preparation_mismatch", HTTPStatus.CONFLICT)
                if not preparation.ready:
                    raise SignalError("preparation_not_ready", HTTPStatus.CONFLICT)
            elif preparation_id is not None:
                raise SignalError("protocol_mismatch", HTTPStatus.CONFLICT)
            action = Action(action_id, self.clock() + self.action_seconds,
                            preparation_id=preparation_id,
                            sha256=preparation.sha256 if session.protocol == MATCH_PROTOCOL else None)
            session.actions[action_id] = action
            session.outstanding_id = session.latest_id = action_id
            self.changed.notify_all()
            return {"ok": True, "session_id": session_id, "action": action.public()}

    def poll(self, room: str, session_id: str, token: str) -> dict:
        with self.changed:
            session = self._receiver(room, session_id, token)
            if session.polling:
                raise SignalError("poll_in_progress", HTTPStatus.CONFLICT)
            session.polling = True
            # A real monotonic deadline bounds waits even in fake-clock unit tests.
            deadline = time.monotonic() + self.poll_seconds
            try:
                while True:
                    self._receiver(room, session_id, token)
                    action = session.actions.get(session.outstanding_id)
                    if action is not None and action.status == "accepted":
                        remaining_ms = math.floor((action.deadline - self.clock()) * 1000)
                        if remaining_ms > 0:
                            action.status = "delivered"
                            # The original deadline still bounds dispatch on the
                            # receiver. This allowance accepts results only.
                            action.receipt_deadline = action.deadline + self.receipt_seconds
                            return {"ok": True, "session_id": session_id, "action": {
                                "action_id": action.action_id, "session_id": session_id,
                                "remaining_ms": remaining_ms,
                                **({"preparation_id": action.preparation_id, "sha256": action.sha256}
                                   if action.preparation_id else {}),
                            }}
                        action.status, action.reason = "expired", "expired"
                        session.outstanding_id = None
                    preparation = session.preparation
                    if preparation is not None and not preparation.ready and session.outstanding_id is None:
                        return {"ok": True, "session_id": session_id, "action": None,
                                "preparation": preparation.public(self.clock())}
                    remaining_wait = deadline - time.monotonic()
                    if remaining_wait <= 0:
                        return {"ok": True, "session_id": session_id, "action": None}
                    self.changed.wait(remaining_wait)
            finally:
                session.polling = False
                if session.active and self.rooms.get(room) is session:
                    session.last_poll_completed = self.clock()
                self.changed.notify_all()

    def ack(self, room: str, session_id: str, token: str, action_id: str,
            status: str, reason: str | None = None) -> dict:
        with self.changed:
            session = self._receipt_receiver(room, session_id, token)
            action = session.actions.get(action_id)
            if action is None:
                raise SignalError("action_unknown", HTTPStatus.NOT_FOUND)
            if action.status in TERMINAL_STATUSES:
                if action.status != status or action.reason != reason:
                    raise SignalError("action_already_finished", HTTPStatus.CONFLICT)
            elif action.status != "delivered":
                raise SignalError("action_not_delivered", HTTPStatus.CONFLICT)
            else:
                action.status, action.reason = status, reason
                if session.outstanding_id == action_id:
                    session.outstanding_id = None
                self.changed.notify_all()
            return {"ok": True, "session_id": session_id, "action": action.public()}

    def unregister(self, room: str, session_id: str, token: str) -> dict:
        with self.changed:
            session = self._receiver(room, session_id, token)
            self._close(session, "receiver_unregistered")
            return {"ok": True, "ready": False, "session_id": None, "action": None}


def _store(handler) -> SignalStore:
    with _INIT_LOCK:
        if not hasattr(handler.server, "signal_store"):
            handler.server.signal_store = SignalStore()
        return handler.server.signal_store


def _read_json(handler) -> dict:
    if handler.headers.get("Transfer-Encoding"):
        raise SignalError("transfer_encoding_not_supported")
    lengths = handler.headers.get_all("Content-Length", [])
    if not lengths:
        raise SignalError("content_length_required", HTTPStatus.LENGTH_REQUIRED)
    if len(lengths) != 1:
        raise SignalError("invalid_content_length")
    try:
        size = int(lengths[0])
    except ValueError:
        raise SignalError("invalid_content_length") from None
    if size < 0:
        raise SignalError("invalid_content_length")
    if size > MAX_BODY_BYTES:
        raise SignalError("body_too_large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
    if handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise SignalError("content_type_must_be_json", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
    old_timeout = handler.connection.gettimeout()
    try:
        handler.connection.settimeout(3.0)
        raw = handler.rfile.read(size)
    except (OSError, socket.timeout):
        raise SignalError("incomplete_body", HTTPStatus.REQUEST_TIMEOUT) from None
    finally:
        handler.connection.settimeout(old_timeout)
    if len(raw) != size:
        raise SignalError("incomplete_body")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SignalError("duplicate_json_key")
            result[key] = value
        return result

    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise SignalError("invalid_json") from None
    if not isinstance(payload, dict):
        raise SignalError("json_object_required")
    return payload


def _keys(payload: dict, required: set[str], optional: set[str] | None = None) -> None:
    if not required <= payload.keys() or payload.keys() - required - (optional or set()):
        raise SignalError("invalid_fields")


def _token(value: object) -> str:
    if not isinstance(value, str) or not 20 <= len(value) <= 128 or not value.isascii():
        raise SignalError("invalid_receiver_token")
    return value


def handle_post(handler, path: str) -> bool:
    if not path.startswith("/api/signal/"):
        return False
    try:
        operation = path.removeprefix("/api/signal/")
        if operation not in {"register", "prepare", "prepared", "action", "poll", "ack", "unregister"}:
            raise SignalError("signal_endpoint_not_found", HTTPStatus.NOT_FOUND)
        payload = _read_json(handler)
        required = {"room_id"}
        if operation != "register":
            required.add("session_id")
        if operation in {"poll", "ack", "prepared", "unregister"}:
            required.add("receiver_token")
        if operation in {"action", "ack"}:
            required.add("action_id")
        if operation == "ack":
            required.add("status")
        if operation in {"prepare", "prepared"}:
            required.add("preparation_id")
        if operation == "prepare":
            required.add("sha256")
        optional = {"ack": {"reason"}, "register": {"protocol"}, "action": {"preparation_id"}}.get(operation)
        _keys(payload, required, optional)
        room = room_id(payload["room_id"])
        session_id = uuid_value(payload["session_id"], "session_id") if "session_id" in payload else None
        action_id = uuid_value(payload["action_id"], "action_id") if "action_id" in payload else None
        token = _token(payload["receiver_token"]) if "receiver_token" in payload else None
        preparation_id = uuid_value(payload["preparation_id"], "preparation_id") if "preparation_id" in payload else None
        store = _store(handler)
        if operation == "register":
            result = store.register(room, payload.get("protocol", LEGACY_PROTOCOL))
        elif operation == "prepare":
            result = store.prepare(room, session_id, preparation_id, payload["sha256"])
        elif operation == "prepared":
            result = store.prepared(room, session_id, token, preparation_id)
        elif operation == "action":
            result = store.action(room, session_id, action_id, preparation_id)
        elif operation == "poll":
            result = store.poll(room, session_id, token)
        elif operation == "ack":
            status, reason = payload["status"], payload.get("reason")
            if not isinstance(status, str) or status not in {"executed", "cancelled"}:
                raise SignalError("invalid_ack_status")
            if "reason" in payload and (not isinstance(reason, str) or reason not in ACK_REASONS):
                raise SignalError("invalid_ack_reason")
            if status == "executed" and reason is not None:
                raise SignalError("executed_cannot_have_reason")
            result = store.ack(room, session_id, token, action_id, status, reason)
        else:
            result = store.unregister(room, session_id, token)
        handler._send_json(result)
    except SignalError as exc:
        handler.close_connection = True
        handler._send_json({"ok": False, "error": exc.reason}, status=exc.status)
    return True


def handle_get(handler, path: str, query: str, static_root: Path) -> bool:
    if path == "/api/signal/status":
        try:
            values = parse_qs(query, keep_blank_values=True)
            if set(values) - {"room", "action_id"} or "room" not in values or any(len(items) != 1 for items in values.values()):
                raise SignalError("invalid_query")
            room = room_id(values["room"][0])
            action_id = uuid_value(values["action_id"][0], "action_id") if "action_id" in values else None
            handler._send_json(_store(handler).status(room, action_id))
        except SignalError as exc:
            handler._send_json({"ok": False, "error": exc.reason}, status=exc.status)
        return True
    if path.startswith("/signal/"):
        try:
            room_id(unquote(path[len("/signal/"):]))
        except SignalError as exc:
            handler._send_json({"ok": False, "error": exc.reason}, status=exc.status)
            return True
        source = static_root / "signal-trial.html"
        if not source.is_file():
            handler.send_error(HTTPStatus.NOT_FOUND)
            return True
        body = source.read_text(encoding="utf-8").replace("__BASE_PATH__", handler.base_path).encode("utf-8")
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(body)
        return True
    assets = {
        "/assets/signal-trial.js": ("signal-trial.js", "application/javascript; charset=utf-8"),
        "/assets/signal-trial.css": ("signal-trial.css", "text/css; charset=utf-8"),
    }
    if path in assets:
        name, content_type = assets[path]
        if not (static_root / name).is_file():
            handler.send_error(HTTPStatus.NOT_FOUND)
        else:
            handler._serve_file(static_root / name, content_type)
        return True
    return False
