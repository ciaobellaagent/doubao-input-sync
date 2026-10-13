#!/usr/bin/env python3
"""Steady clipboard-match receiver plus explicitly sent relay archives.

Preparation only starts local monitoring. A valid one-shot action authorizes a
paste. No copy archive is fetched as a fallback and mobile drafts stay intact.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from urllib.parse import urlencode

from mac_signal_trial import Api, ApiError, REASONS, emit, exclusive_trial, uuid_string
import mac_paste_helper as relay

HERE = Path(__file__).resolve().parent
PROTOCOL = "clipboard-match-v1"
MAX_ACTIONS_PER_SESSION = 256
MATCH_REASONS = REASONS | {"clipboard_timeout", "clipboard_changed", "mismatch"}


# Keep response diagnostics machine-readable without retaining arbitrary server
# content (which may include a proxy/login page or sensitive error details).
CONTROL_ERROR_CODES = frozenset({
    "preparation_mismatch", "session_mismatch", "receiver_token_invalid",
    "receiver_unavailable", "protocol_mismatch", "receiver_already_registered",
    "action_in_progress", "action_limit_reached", "preparation_id_conflict",
    "preparation_not_ready", "action_id_conflict", "poll_in_progress",
    "action_unknown", "action_already_finished", "action_not_delivered",
    "signal_capacity_reached", "invalid_protocol", "invalid_sha256", "invalid_fields",
})


class ControlApiError(ApiError):
    def __init__(self, http_status, error_code=None):
        super().__init__("server_rejected")
        self.http_status = http_status
        self.error_code = error_code if isinstance(error_code, str) and error_code in CONTROL_ERROR_CODES else "server_rejected"


class ControlApi(Api):
    """Production-only bounded timeouts; legacy probe defaults stay unchanged."""
    def __init__(self, base, request_timeout=8.0):
        super().__init__(base)
        if not math.isfinite(request_timeout) or not 0 < request_timeout <= 30:
            raise ValueError("control_timeout_must_be_0_to_30_seconds")
        self.request_timeout = request_timeout

    def request(self, name, payload=None, query=None):
        url = self.base + "/api/signal/" + name
        if query:
            url += "?" + urlencode(query)
        command = ["/usr/bin/curl", "-sS", "--noproxy", "*", "--http1.1",
                   "--max-time", str(self.request_timeout),
                   "--connect-timeout", str(min(6.0, self.request_timeout)),
                   "--max-redirs", "0", "--max-filesize", "16384",
                   "-w", "\n%{http_code}", "-H", "Accept: application/json"]
        body = None
        if payload is not None:
            command += ["-X", "POST", "-H", "Content-Type: application/json", "--data-binary", "@-"]
            body = json.dumps(payload)
        result = subprocess.run(command + [url], input=body, text=True, capture_output=True,
                                timeout=self.request_timeout + 1)
        if result.returncode:
            raise ApiError("transport_failed")
        try:
            data, code = result.stdout.rsplit("\n", 1)
            data = json.loads(data)
            code = int(code)
            if code not in {200, 201, 202} or not isinstance(data, dict) or data.get("ok") is False:
                raise ControlApiError(code, data.get("error") if isinstance(data, dict) else None)
            return data
        except (ValueError, TypeError) as exc:
            raise ApiError("invalid_response") from exc


def valid_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def read_clipboard_hash():
    # Never retain or log plaintext; environment forces pbpaste's UTF-8 encoding.
    result = subprocess.run(["/usr/bin/pbpaste"], capture_output=True, timeout=1,
                            env={**os.environ, "LC_ALL": "en_US.UTF-8"}, check=True)
    return hashlib.sha256(result.stdout).hexdigest()


class PassiveWatcher:
    """A passive watcher has no paste callback or action capability."""
    def __init__(self, read_hash=read_clipboard_hash):
        self.read_hash = read_hash
        self.expected = None
        self.matched = False
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.guard = threading.Lock()
        self.thread = threading.Thread(target=self.run, name="clipboard-observer", daemon=True)
        self.thread.start()

    def prepare(self, fingerprint):
        with self.guard:
            self.expected = fingerprint
            self.matched = False
            self.ready.clear()
        return self.ready.wait(1.5)

    def run(self):
        while not self.stopped.is_set():
            with self.guard:
                expected = self.expected
            if expected:
                try:
                    current = self.read_hash()
                    with self.guard:
                        if expected == self.expected:
                            self.matched = current == expected
                            self.ready.set()
                except Exception:
                    pass
            self.stopped.wait(0.1)

    def close(self):
        self.stopped.set()
        self.thread.join(2)


NATIVE_STAGES = frozenset({"start", "target_process", "focused_input", "focused_window",
                           "input_role", "native_workbuddy", "accessibility_init", "modifiers", "focus_recheck", "clipboard_read",
                           "clipboard_hash", "key_dispatch"})


def native_result(output, returncode):
    # Error text can contain app/field/clipboard content. Accept only our fixed
    # stage enum and a bounded numeric OS code; never log arbitrary stdout.
    if returncode:
        emit("native_error", stage="process_exit", exit_code=returncode)
        return "paste_failed"
    if output in MATCH_REASONS | {"executed"}:
        return output
    parts = output.split("|")
    if (len(parts) == 3 and parts[0] == "paste_failed" and parts[1] in NATIVE_STAGES
            and re.fullmatch(r"-?[0-9]{1,8}", parts[2])):
        emit("native_error", stage=parts[1], error_code=int(parts[2]))
    else:
        emit("native_error", stage="invalid_result")
    return "paste_failed"


def paste_matching(deadline, fingerprint, stop=lambda: False):
    remaining = deadline - time.monotonic()
    if remaining <= 0.1 or stop():
        return "expired"
    # Native process captures focus BEFORE waiting, rechecks the exact current
    # pasteboard after its hash, and checks both again immediately before ⌘V.
    proc = subprocess.Popen(["/usr/bin/osascript", str(HERE / "prearmed_paste.applescript"),
                             str((time.time() + remaining) * 1000), fingerprint],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        while proc.poll() is None:
            if stop() or time.monotonic() >= deadline:
                proc.terminate()
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1)
                return "receiver_stopped" if stop() else "expired"
            time.sleep(0.025)
        output = proc.stdout.read().strip()
        return native_result(output, proc.returncode)
    finally:
        if proc.stdout:
            proc.stdout.close()


class MatchConsumer:
    def __init__(self, session_id, watcher, paste, action_lock, monotonic=time.monotonic):
        self.session_id = session_id
        self.watcher = watcher
        self.paste = paste
        self.action_lock = action_lock
        self.monotonic = monotonic
        self.preparation = None
        self.seen = {}

    def prepare(self, preparation):
        if (not isinstance(preparation, dict)
                or not uuid_string(preparation.get("preparation_id"))
                or not valid_hash(preparation.get("sha256"))):
            return False
        fingerprint = preparation["sha256"]
        if not self.watcher.prepare(fingerprint):
            return False
        self.preparation = (preparation["preparation_id"], fingerprint)
        return True

    def consume(self, action, poll_started):
        if (not isinstance(action, dict)
                or set(action) != {"action_id", "session_id", "remaining_ms", "preparation_id", "sha256"}
                or not uuid_string(action.get("action_id"))
                or action.get("session_id") != self.session_id
                or not uuid_string(action.get("preparation_id")) or not valid_hash(action.get("sha256"))):
            return None
        action_id = action["action_id"]
        if action_id in self.seen:
            return self.seen[action_id]  # retry ACK only, never a second key
        if len(self.seen) >= MAX_ACTIONS_PER_SESSION:
            return None
        outcome = "expired"
        # Consume before waiting, including on invalid/expired budgets.
        self.seen[action_id] = (action_id, "cancelled", "expired")
        remaining = action.get("remaining_ms")
        if type(remaining) not in {int, float} or not math.isfinite(remaining) or not 0 < remaining <= 5000:
            return self.seen[action_id]
        deadline = poll_started + remaining / 1000
        if self.preparation != (action["preparation_id"], action["sha256"]):
            outcome = "mismatch"
        else:
            # Waiting for an explicit relay paste consumes this action's budget.
            budget = max(0, deadline - self.monotonic())
            if self.action_lock.acquire(timeout=budget):
                try:
                    if deadline - self.monotonic() > 0.1:
                        outcome = self.paste(deadline, action["sha256"])
                except Exception:
                    outcome = "paste_failed"
                finally:
                    self.action_lock.release()
        result = (action_id, "executed", None) if outcome == "executed" else (
            action_id, "cancelled", outcome if outcome in MATCH_REASONS else "paste_failed")
        self.seen[action_id] = result
        return result


class RelayConsumer:
    """Only new ordinary relay archives; copy-history is never consulted."""
    def __init__(self, apply, ack, action_lock):
        self.apply, self.ack, self.action_lock = apply, ack, action_lock
        self.version = None
        self.archive_id = 0
        self.pending = set()

    def consume(self, payload):
        version = payload.get("version", 0)
        archive = payload.get("latest_archive") or {}
        archive_id = archive.get("archive_id", 0)
        if type(version) is not int or type(archive_id) is not int:
            return
        # On server epoch reset seed the new snapshot, never replay its latest.
        if self.version is None or version < self.version or archive_id < self.archive_id:
            self.version, self.archive_id = version, archive_id
            self.pending.clear()
            return
        self.version = version
        if archive_id <= self.archive_id:
            return
        self.archive_id = archive_id  # consumed even if native result uncertain
        text = archive.get("text")
        if not isinstance(text, str) or not text:
            return
        with self.action_lock:
            result = self.apply(text)
        emit("relay_action", archive_id=archive_id, status=result)
        if result == "executed":
            self.pending.add(archive_id)

    def retry_ack(self):
        if not self.pending:
            return
        archive_id = min(self.pending)
        try:
            if self.ack(archive_id).get("ok"):
                self.pending.remove(archive_id)
        except Exception:
            pass


def relay_worker(base, room, stop_event, action_lock, request_timeout=6.0):
    def apply(text):
        if stop_event.is_set():
            return "receiver_stopped"
        # A distinct, explicit normal send authorizes this clipboard write.
        relay.copy_to_clipboard(text)
        return paste_matching(time.monotonic() + 5, hashlib.sha256(text.encode("utf-8")).hexdigest(), stop_event.is_set)

    consumer = RelayConsumer(apply, lambda archive_id: relay.acknowledge_archive(
        base, room, archive_id, "mac-prearmed-receiver", "applied", request_timeout, []), action_lock)
    disconnected = False
    while not stop_event.is_set():
        try:
            payload = relay.fetch_state(base, room, request_timeout, [])
            if stop_event.is_set():
                break
            consumer.consume(payload)
            consumer.retry_ack()
            if disconnected:
                emit("relay_reconnected")
            disconnected = False
        except Exception:
            if not disconnected:
                emit("relay_disconnected")
            disconnected = True
        stop_event.wait(0.25)


def receive(api, room, stop_event, duration=0, relay_timeout=6.0):
    end = time.monotonic() + duration if duration else math.inf
    def stopped():
        return stop_event.is_set() or time.monotonic() >= end
    action_lock = threading.Lock()
    relay_thread = threading.Thread(target=relay_worker, args=(api.base, room, stop_event, action_lock, relay_timeout), name="explicit-relay")
    relay_thread.start()
    try:
        while not stopped():
            identity = None
            watcher = None
            executor = None
            session_lost = threading.Event()
            try:
                registration = api.request("register", {"room_id": room, "protocol": PROTOCOL})
                session_id, token = registration["session_id"], registration["receiver_token"]
                if not uuid_string(session_id) or not isinstance(token, str) or not token or registration.get("protocol") != PROTOCOL:
                    raise ApiError("invalid_registration")
                identity = {"room_id": room, "session_id": session_id, "receiver_token": token}
                watcher = PassiveWatcher()
                consumer = MatchConsumer(session_id, watcher, lambda deadline, sha: paste_matching(deadline, sha, lambda: stopped() or session_lost.is_set()), action_lock)
                executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="authorized-action")
                job = None
                pending = {}
                submitted = set()
                def execute_and_ack(action, poll_started):
                    result = consumer.consume(action, poll_started)
                    if not result:
                        raise ApiError("invalid_action")
                    action_id, status, reason = result
                    emit("action", action_id=action_id, status=status, reason=reason)
                    ack = {**identity, "action_id": action_id, "status": status}
                    if reason:
                        ack["reason"] = reason
                    api.request("ack", ack)
                emit("session_ready", room=room, protocol=PROTOCOL)
                while not stopped():
                    if job and job.done():
                        job.result()
                        job = None
                    # Reaching the cap starts rotation only AFTER the final
                    # authorized action and its ACK finish. Keep polling while
                    # that job runs so its lease remains valid.
                    if len(submitted) >= MAX_ACTIONS_PER_SESSION and job is None:
                        break
                    started = time.monotonic()
                    response = api.request("poll", identity)
                    if response.get("session_id") != session_id:
                        raise ApiError("session_changed")
                    if stopped():
                        break
                    preparation = response.get("preparation")
                    if preparation:
                        if not consumer.prepare(preparation):
                            raise ApiError("preparation_failed")
                        try:
                            api.request("prepared", {**identity, "preparation_id": preparation["preparation_id"]})
                        except ControlApiError as error:
                            if error.http_status != 409 or error.error_code != "preparation_mismatch":
                                raise
                            # The phone can replace A with B while this passive
                            # preparation is in flight. A stale acknowledgement
                            # authorizes nothing; keep the valid session and poll
                            # B instead of unregistering all preparation state.
                            consumer.preparation = None
                            emit("preparation_superseded", preparation_id=preparation["preparation_id"])
                            continue
                        emit("prepared", preparation_id=preparation["preparation_id"])
                    action = response.get("action")
                    if action:
                        action_id = action.get("action_id") if isinstance(action, dict) else None
                        if not uuid_string(action_id):
                            raise ApiError("invalid_action")
                        if action_id not in submitted and action_id not in pending:
                            # Preserve the first conservative deadline even when
                            # another action's ACK is slow. No poll is discarded.
                            pending[action_id] = (action, started)
                    if job and job.done():
                        job.result()
                        job = None
                    if job is None and pending and len(submitted) < MAX_ACTIONS_PER_SESSION:
                        action_id = next(iter(pending))
                        action, poll_started = pending.pop(action_id)
                        submitted.add(action_id)
                        job = executor.submit(execute_and_ack, action, poll_started)
                    if job is not None:
                        # A pending action makes server poll return immediately;
                        # keep lease renewal bounded without a busy curl loop.
                        stop_event.wait(0.15)

            except (ApiError, KeyError, subprocess.TimeoutExpired):
                emit("disconnected")
            finally:
                session_lost.set()
                if executor:
                    executor.shutdown(wait=True)
                if watcher:
                    watcher.close()
                if identity:
                    try:
                        api.request("unregister", identity)
                    except (ApiError, subprocess.TimeoutExpired):
                        pass
            if not stopped():
                stop_event.wait(1)
    finally:
        stop_event.set()
        # Join before releasing global lock/restoring daily helper: no stray
        # relay actor may outlive this receiver's exclusive ownership.
        relay_thread.join()


def run_when_online(api, room, stop_event, duration=0, relay_timeout=6.0):
    """Do not pause the daily helper until a bounded control read succeeds."""
    waiting_logged = False
    while not stop_event.is_set():
        try:
            api.request("status", query={"room": room})
        except (ApiError, subprocess.TimeoutExpired):
            if not waiting_logged:
                emit("startup_waiting_for_connection", daily_helper_unchanged=True)
                waiting_logged = True
            stop_event.wait(1)
            continue
        # Stop can arrive during the successful HTTP call. Never boot out the
        # daily helper after such a stop just to restore it again.
        if stop_event.is_set():
            return
        with exclusive_trial():
            receive(api, room, stop_event, duration, relay_timeout)
        return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--room", required=True)
    parser.add_argument("--duration", type=int, default=0, help="0 keeps receiver running until stopped")
    parser.add_argument("--control-timeout-seconds", type=float, default=8.0)
    parser.add_argument("--relay-timeout-seconds", type=float, default=6.0)
    args = parser.parse_args()
    if args.duration < 0:
        parser.error("duration must be >=0")
    for timeout in (args.control_timeout_seconds, args.relay_timeout_seconds):
        if not math.isfinite(timeout) or not 0 < timeout <= 30:
            parser.error("timeouts must be >0 and <=30 seconds")
    api = ControlApi(args.server_url, args.control_timeout_seconds)
    stop_event = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, lambda *_: stop_event.set())
    run_when_online(api, args.room, stop_event, args.duration, args.relay_timeout_seconds)
    emit("stopped")


if __name__ == "__main__":
    main()
