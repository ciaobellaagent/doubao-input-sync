#!/usr/bin/env python3
"""Action-only receiver with a per-user exclusive clipboard lease.

Run from the already-authorized Terminal host. No clipboard reads, writes,
archive fetches, text payloads, keystroke retries, or app activation.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from helper_instance import HelperInstanceLock

HERE = Path(__file__).resolve().parent
REASONS = {"focus_changed", "no_focused_input", "unsafe_target", "expired", "receiver_stopped",
           "paste_failed", "receiver_expired", "receiver_unregistered", "permission_missing",
           "modifier_held", "superseded"}


def emit(event, **fields):
    print(json.dumps({"event": event, "at": time.time(), **fields}), flush=True)


class ApiError(RuntimeError):
    pass


class Api:
    def __init__(self, base):
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("invalid_server_url")
        self.base = base.rstrip("/")

    def request(self, name, payload=None, query=None):
        url = self.base + "/api/signal/" + name
        if query:
            url += "?" + urlencode(query)
        cmd = ["/usr/bin/curl", "-sS", "--noproxy", "*", "--http1.1", "--max-time", "4",
               "--connect-timeout", "3", "--max-redirs", "0", "--max-filesize", "16384",
               "-w", "\n%{http_code}", "-H", "Accept: application/json"]
        body = None
        if payload is not None:
            cmd += ["-X", "POST", "-H", "Content-Type: application/json", "--data-binary", "@-"]
            body = json.dumps(payload)
        result = subprocess.run(cmd + [url], input=body, text=True, capture_output=True, timeout=5)
        if result.returncode:
            raise ApiError("transport_failed")
        try:
            data, code = result.stdout.rsplit("\n", 1)
            data = json.loads(data)
            if int(code) not in {200, 201, 202} or not isinstance(data, dict) or data.get("ok") is False:
                raise ApiError("server_rejected")
            return data
        except (ValueError, TypeError) as exc:
            raise ApiError("invalid_response") from exc


def uuid_string(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


class ActionConsumer:
    """At-most-once dispatch within a session; consume before native side effect."""
    def __init__(self, session_id, paste, *, delay_ms=500, monotonic=time.monotonic):
        self.session_id = session_id
        self.paste = paste
        self.delay_ms = delay_ms
        self.monotonic = monotonic
        self.seen = set()

    def consume(self, action, poll_started):
        if not isinstance(action, dict) or set(action) != {"action_id", "session_id", "remaining_ms"}:
            return None
        action_id = action["action_id"]
        if not uuid_string(action_id) or action["session_id"] != self.session_id:
            return None
        if action_id in self.seen or len(self.seen) >= 256:
            return None
        self.seen.add(action_id)
        remaining = action["remaining_ms"]
        if type(remaining) not in {int, float} or not math.isfinite(remaining) or not 0 < remaining <= 5000:
            return (action_id, "cancelled", "expired")
        # Subtract ALL request time, including server wait: a conservative bound
        # without trusting synchronization between the Mac and VPS clocks.
        budget = remaining / 1000 - (self.monotonic() - poll_started)
        if budget <= self.delay_ms / 1000 + 0.15:
            return (action_id, "cancelled", "expired")
        try:
            outcome = self.paste(self.monotonic() + budget, self.delay_ms / 1000)
        except Exception:
            outcome = "paste_failed"
        if outcome == "executed":
            return (action_id, "executed", None)
        return (action_id, "cancelled", outcome if outcome in REASONS else "paste_failed")


def paste_existing(deadline, delay):
    remaining = deadline - time.monotonic()
    if remaining <= delay + 0.15:
        return "expired"
    epoch_deadline = (time.time() + remaining) * 1000
    proc = subprocess.run(["/usr/bin/osascript", str(HERE / "signal_paste.applescript"),
                           str(epoch_deadline), str(delay)], text=True, capture_output=True,
                          timeout=max(0.1, remaining + 0.25))
    return proc.stdout.strip() if proc.returncode == 0 else "paste_failed"


@contextlib.contextmanager
def exclusive_trial():
    """Refuse overlapping receivers; never stop or restart another service."""
    with HelperInstanceLock():
        emit("exclusive_receiver_ready", pid=os.getpid())
        yield


def receive(api, room, stop, duration):
    end = time.monotonic() + duration
    while not stop() and time.monotonic() < end:
        identity = None
        try:
            registration = api.request("register", {"room_id": room})
            session_id = registration["session_id"]
            token = registration["receiver_token"]
            if not uuid_string(session_id) or not isinstance(token, str) or not token:
                raise ApiError("invalid_registration")
            identity = {"room_id": room, "session_id": session_id, "receiver_token": token}
            consumer = ActionConsumer(session_id, paste_existing)
            emit("session_ready", room=room)
            while not stop() and time.monotonic() < end and len(consumer.seen) < 256:
                started = time.monotonic()
                response = api.request("poll", identity)
                if response.get("session_id") != session_id:
                    raise ApiError("session_changed")
                if stop():
                    break
                action = response.get("action")
                if action is None:
                    continue
                result = consumer.consume(action, started)
                if result is None:
                    raise ApiError("unexpected_action")
                action_id, status, reason = result
                emit("action", action_id=action_id, status=status, reason=reason)
                ack = {**identity, "action_id": action_id, "status": status}
                if reason:
                    ack["reason"] = reason
                # A failed ack never repeats a key. Reconnect rotates session.
                api.request("ack", ack)
        except (ApiError, KeyError, subprocess.TimeoutExpired):
            emit("disconnected")
        finally:
            if identity:
                try:
                    api.request("unregister", identity)
                except (ApiError, subprocess.TimeoutExpired):
                    pass
        if not stop() and time.monotonic() < end:
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--room", required=True)
    parser.add_argument("--duration", type=int, default=7200, help="Seconds until this receiver exits")
    args = parser.parse_args()
    if not 1 <= args.duration <= 14400:
        parser.error("duration must be 1..14400")
    api = Api(args.server_url)
    # Verify deployment before pausing anything on this Mac.
    api.request("status", query={"room": args.room})
    stopped = False

    def stop(signum, frame):
        nonlocal stopped
        stopped = True

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop)
    with exclusive_trial():
        receive(api, args.room, lambda: stopped, args.duration)
    emit("stopped")


if __name__ == "__main__":
    main()
