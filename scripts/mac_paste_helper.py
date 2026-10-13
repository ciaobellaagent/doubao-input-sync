#!/usr/bin/env python3
"""Optional macOS helper: copy synced text to clipboard or paste into active input."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from http.client import RemoteDisconnected
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


_CLOUDFLARE_ACCESS_HEADERS: dict[str, str] | None = None


def normalize_payload(payload: dict) -> dict:
    if "latest_archive" not in payload:
        history = payload.get("history") or []
        payload["latest_archive"] = history[-1] if history else None
    return payload


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Service credentials must never follow a redirect, even to another path.
        raise HTTPError(req.full_url, code, "relay redirects are not allowed", headers, fp)


def urlopen(request, timeout):
    return build_opener(RejectRedirects()).open(request, timeout=timeout)


def validate_transport(url: str, headers: dict[str, str]) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise URLError("relay URL must be http(s) without embedded credentials")
    if headers and parsed.scheme != "https":
        raise URLError("service credentials require HTTPS")


def cloudflare_access_headers() -> dict[str, str]:
    global _CLOUDFLARE_ACCESS_HEADERS
    if _CLOUDFLARE_ACCESS_HEADERS is not None:
        return dict(_CLOUDFLARE_ACCESS_HEADERS)

    client_id = os.environ.pop("CF_ACCESS_CLIENT_ID", "").strip()
    client_secret = os.environ.pop("CF_ACCESS_CLIENT_SECRET", "").strip()
    if bool(client_id) != bool(client_secret):
        raise RuntimeError("CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET must be configured together")
    if not client_id:
        _CLOUDFLARE_ACCESS_HEADERS = {}
        return {}
    _CLOUDFLARE_ACCESS_HEADERS = {
        "CF-Access-Client-Id": client_id,
        "CF-Access-Client-Secret": client_secret,
    }
    return dict(_CLOUDFLARE_ACCESS_HEADERS)


def curl_access_config(headers: dict[str, str]) -> str:
    lines: list[str] = []
    for name, value in headers.items():
        if "\n" in value or "\r" in value:
            raise RuntimeError(f"{name} contains an invalid newline")
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'header = "{name}: {escaped}"')
    return "\n".join(lines) + ("\n" if lines else "")


def redirect_error(return_code: int) -> str | None:
    if return_code == 47:
        return "authentication required or unexpected redirect from relay"
    return None


def fetch_json(url: str, request_timeout_seconds: float, curl_resolve: list[str]) -> dict:
    access_headers = cloudflare_access_headers()
    validate_transport(url, access_headers)
    access_config = curl_access_config(access_headers)
    curl_args = [
        "curl",
        "-fsS",
        "--location",
        "--max-redirs",
        "0",
        "--http1.1",
        "--max-time",
        str(request_timeout_seconds),
        "--connect-timeout",
        str(min(8.0, request_timeout_seconds)),
        "-H",
        "ngrok-skip-browser-warning: 1",
        "-A",
        "doubao-input-sync-helper/1.0",
    ]
    if access_config:
        curl_args[1:1] = ["--config", "-"]
    for resolve_entry in curl_resolve:
        curl_args.extend(["--resolve", resolve_entry])
    curl_args.append(url)

    try:
        curl_proc = subprocess.run(
            curl_args,
            input=access_config.encode("utf-8") if access_config else None,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        curl_proc = None

    if curl_proc is not None:
        if curl_proc.returncode == 0:
            try:
                return json.loads(curl_proc.stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise URLError(f"curl returned invalid JSON: {exc}") from exc

        reason = redirect_error(curl_proc.returncode)
        reason = reason or curl_proc.stderr.decode("utf-8", errors="replace").strip()
        reason = reason or f"curl exited {curl_proc.returncode}"
        if curl_proc.returncode == 22 and "404" in reason:
            raise HTTPError(url, 404, reason, hdrs=None, fp=None)
        raise URLError(reason)

    request = Request(
        url,
        headers={
            "ngrok-skip-browser-warning": "1",
            "User-Agent": "doubao-input-sync-helper/1.0",
            **access_headers,
        },
    )
    try:
        with urlopen(request, timeout=request_timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except (RemoteDisconnected, ConnectionError, TimeoutError, OSError) as exc:
        raise URLError(str(exc)) from exc


def post_json(url: str, payload: dict, request_timeout_seconds: float, curl_resolve: list[str]) -> dict:
    body_text = json.dumps(payload, ensure_ascii=False)
    access_headers = cloudflare_access_headers()
    validate_transport(url, access_headers)
    access_config = curl_access_config(access_headers)
    escaped_body = body_text.replace("\\", "\\\\").replace('"', '\\"')
    access_config += f'data-binary = "{escaped_body}"\n'
    curl_args = [
        "curl",
        "-fsS",
        "--location",
        "--max-redirs",
        "0",
        "--http1.1",
        "--max-time",
        str(request_timeout_seconds),
        "--connect-timeout",
        str(min(8.0, request_timeout_seconds)),
        "-H",
        "ngrok-skip-browser-warning: 1",
        "-H",
        "Content-Type: application/json",
        "-A",
        "doubao-input-sync-helper/1.0",
        "-X",
        "POST",
    ]
    if access_config:
        curl_args[1:1] = ["--config", "-"]
    for resolve_entry in curl_resolve:
        curl_args.extend(["--resolve", resolve_entry])
    curl_args.append(url)

    try:
        curl_proc = subprocess.run(
            curl_args,
            input=access_config.encode("utf-8") if access_config else None,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        curl_proc = None

    if curl_proc is not None:
        if curl_proc.returncode == 0:
            try:
                return json.loads(curl_proc.stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise URLError(f"curl returned invalid JSON: {exc}") from exc

        reason = redirect_error(curl_proc.returncode)
        reason = reason or curl_proc.stderr.decode("utf-8", errors="replace").strip()
        reason = reason or f"curl exited {curl_proc.returncode}"
        if curl_proc.returncode == 22 and "404" in reason:
            raise HTTPError(url, 404, reason, hdrs=None, fp=None)
        raise URLError(reason)

    request = Request(
        url,
        data=body_text.encode("utf-8"),
        headers={
            "ngrok-skip-browser-warning": "1",
            "User-Agent": "doubao-input-sync-helper/1.0",
            "Content-Type": "application/json",
            **access_headers,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=request_timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except (RemoteDisconnected, ConnectionError, TimeoutError, OSError) as exc:
        raise URLError(str(exc)) from exc


def fetch_state(server_url: str, room_id: str, request_timeout_seconds: float, curl_resolve: list[str]) -> dict:
    query = urlencode({"room_id": room_id})
    helper_url = f"{server_url.rstrip('/')}/api/helper-state?{query}"
    state_url = f"{server_url.rstrip('/')}/api/state?{query}"

    try:
        return fetch_json(helper_url, request_timeout_seconds, curl_resolve)
    except HTTPError as exc:
        if exc.code != 404:
            raise
    except (URLError, socket.timeout):
        pass

    payload = fetch_json(state_url, request_timeout_seconds, curl_resolve)
    return normalize_payload(payload)


def acknowledge_archive(
    server_url: str,
    room_id: str,
    archive_id: int,
    client_id: str,
    action: str,
    request_timeout_seconds: float,
    curl_resolve: list[str],
) -> dict:
    return post_json(
        f"{server_url.rstrip('/')}/api/archive-ack",
        {
            "room_id": room_id,
            "archive_id": archive_id,
            "client_id": client_id,
            "action": action,
        },
        request_timeout_seconds,
        curl_resolve,
    )


def acknowledge_archive_with_retry(
    server_url: str,
    room_id: str,
    archive_id: int,
    client_id: str,
    action: str,
    request_timeout_seconds: float,
    curl_resolve: list[str],
    attempts: int = 3,
) -> tuple[dict, int]:
    last_error: HTTPError | URLError | socket.timeout | None = None
    ack_timeout = min(request_timeout_seconds, 6.0)
    for attempt in range(1, attempts + 1):
        try:
            return (
                acknowledge_archive(
                    server_url,
                    room_id,
                    archive_id,
                    client_id,
                    action,
                    ack_timeout,
                    curl_resolve,
                ),
                attempt,
            )
        except (HTTPError, URLError, socket.timeout) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(float(attempt))
    assert last_error is not None
    raise last_error


def archive_acknowledged(payload: dict, archive_id: int, action: str) -> bool:
    latest_archive = payload.get("latest_archive")
    if latest_archive and latest_archive.get("archive_id") == archive_id:
        return bool(latest_archive.get("desktop_received_at")) and latest_archive.get("desktop_delivery_action") == action

    for entry in payload.get("history") or []:
        if entry.get("archive_id") == archive_id:
            return bool(entry.get("desktop_received_at")) and entry.get("desktop_delivery_action") == action
    return False


def parse_sse_event(lines: list[str]) -> dict | None:
    event_name = ""
    data_lines: list[str] = []
    for line in lines:
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").lstrip())

    if event_name and event_name != "room_state":
        return None
    if not data_lines:
        return None
    try:
        return normalize_payload(json.loads("\n".join(data_lines)))
    except json.JSONDecodeError as exc:
        raise URLError(f"SSE returned invalid JSON: {exc}") from exc


def stream_payloads(
    server_url: str,
    room_id: str,
    request_timeout_seconds: float,
    stream_max_time_seconds: float,
    curl_resolve: list[str],
) -> Iterator[dict]:
    query = urlencode({"room_id": room_id})
    stream_url = f"{server_url.rstrip('/')}/api/stream?{query}"
    access_headers = cloudflare_access_headers()
    validate_transport(stream_url, access_headers)
    access_config = curl_access_config(access_headers)
    curl_args = [
        "curl",
        "-fsS",
        "-N",
        "--location",
        "--max-redirs",
        "0",
        "--http1.1",
        "--max-time",
        str(stream_max_time_seconds),
        "--connect-timeout",
        str(min(8.0, request_timeout_seconds)),
        "-H",
        "ngrok-skip-browser-warning: 1",
        "-A",
        "doubao-input-sync-helper/1.0",
    ]
    if access_config:
        curl_args[1:1] = ["--config", "-"]
    for resolve_entry in curl_resolve:
        curl_args.extend(["--resolve", resolve_entry])
    curl_args.append(stream_url)

    try:
        proc = subprocess.Popen(
            curl_args,
            stdin=subprocess.PIPE if access_config else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise URLError("curl executable was not found") from exc

    if access_config and proc.stdin is not None:
        try:
            proc.stdin.write(access_config)
            proc.stdin.close()
        except BrokenPipeError:
            pass

    assert proc.stdout is not None
    lines: list[str] = []
    try:
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            if not line:
                payload = parse_sse_event(lines)
                lines = []
                if payload is not None:
                    yield payload
                continue
            if line.startswith(":"):
                continue
            lines.append(line)
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.terminate()

    stderr = proc.stderr.read() if proc.stderr is not None else ""
    return_code = proc.wait()
    reason = redirect_error(return_code)
    reason = reason or stderr.strip() or f"stream exited with code {return_code}"
    raise URLError(reason)


def copy_to_clipboard(text: str) -> None:
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)


def paste_to_active_app(text: str) -> None:
    copy_to_clipboard(text)
    subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to keystroke "v" using command down',
        ],
        check=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Watch synced text and project it to macOS.")
    parser.add_argument("--server-url", default="http://127.0.0.1:18766")
    parser.add_argument("--room-id", default="doubao")
    parser.add_argument("--interval-seconds", type=float, default=0.5)
    parser.add_argument("--request-timeout-seconds", type=float, default=8.0)
    parser.add_argument(
        "--stream-max-time-seconds",
        type=float,
        default=90.0,
        help="Recycle the long-lived SSE curl connection periodically so a silent stale stream can self-heal.",
    )
    parser.add_argument(
        "--transport",
        choices=["stream", "poll"],
        default="stream",
        help="stream uses the relay SSE feed; poll repeatedly fetches helper-state.",
    )
    parser.add_argument(
        "--curl-resolve",
        action="append",
        default=[],
        help="Optional curl --resolve entry. Repeat or pass comma-separated HOST:PORT:ADDR values.",
    )
    parser.add_argument("--mode", choices=["clipboard", "paste"], default="clipboard")
    parser.add_argument(
        "--trigger",
        choices=["archive", "live"],
        default="archive",
        help="archive waits for a settled history item; live reacts to every new version.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--client-id",
        default="",
        help="Identifier used when acknowledging received archive items back to the relay.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        default=True,
        help="Ignore the latest existing version/archive at startup and only react to newer content.",
    )
    parser.add_argument("--stop-after-updates", type=int, default=0)
    return parser


def parse_curl_resolve(values: list[str]) -> list[str]:
    entries: list[str] = []
    for value in values:
        for entry in value.split(","):
            entry = entry.strip()
            if entry:
                entries.append(entry)
    return entries


def remember_pending_ack(state: dict, archive_id: int, action: str) -> None:
    state["pending_acks"][archive_id] = {
        "archive_id": archive_id,
        "action": action,
        "attempts": state["pending_acks"].get(archive_id, {}).get("attempts", 0),
    }


def retry_pending_acks(args: argparse.Namespace, state: dict) -> None:
    if not state["pending_acks"]:
        return

    # Retry one pending ack per tick so a bad network stretch cannot block fresh paste handling.
    archive_id = sorted(state["pending_acks"])[0]
    pending = state["pending_acks"][archive_id]
    action = pending["action"]
    pending["attempts"] += 1

    try:
        ack_payload = acknowledge_archive(
            args.server_url,
            args.room_id,
            archive_id,
            args.client_id,
            action,
            min(args.request_timeout_seconds, 6.0),
            state["curl_resolve"],
        )
        if ack_payload.get("ok"):
            state["pending_acks"].pop(archive_id, None)
            record = {
                "status": "desktop_ack_recovered",
                "room_id": args.room_id,
                "archive_id": archive_id,
                "action": action,
                "attempts": pending["attempts"],
            }
            print(json.dumps(record, ensure_ascii=False), flush=True)
            return
    except (HTTPError, URLError, socket.timeout) as exc:
        reason = str(getattr(exc, "reason", exc))
    else:
        reason = "ack endpoint returned ok=false"

    if pending["attempts"] in {1, 3, 8}:
        record = {
            "status": "desktop_ack_still_pending",
            "room_id": args.room_id,
            "archive_id": archive_id,
            "action": action,
            "attempts": pending["attempts"],
            "reason": reason,
        }
        print(json.dumps(record, ensure_ascii=False), flush=True)


def reset_relay_sequence_if_needed(payload: dict, state: dict, emit_log: bool = True) -> bool:
    version = payload.get("version", 0)
    if not state["startup_seeded"] or version >= state["last_version"]:
        return False

    previous_version = state["last_version"]
    previous_archive_id = state["last_archive_id"]
    state["last_version"] = -1
    state["last_archive_id"] = -1
    state["pending_acks"].clear()
    if emit_log:
        print(
            json.dumps(
                {
                    "status": "relay_sequence_reset",
                    "previous_version": previous_version,
                    "new_version": version,
                    "previous_archive_id": previous_archive_id,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return True


def process_payload(args: argparse.Namespace, payload: dict, state: dict) -> bool:
    version = payload.get("version", 0)
    text = payload.get("text", "")
    latest_archive = payload.get("latest_archive")
    trigger_ref = None
    archive_id = 0

    if args.skip_existing and not state["startup_seeded"]:
        state["last_version"] = max(state["last_version"], version)
        if latest_archive:
            state["last_archive_id"] = max(state["last_archive_id"], latest_archive.get("archive_id", 0))
        state["startup_seeded"] = True
        return False

    reset_relay_sequence_if_needed(payload, state)

    if args.trigger == "archive":
        state["last_version"] = max(state["last_version"], version)
        if not latest_archive:
            return False

        archive_id = latest_archive.get("archive_id", 0)
        if archive_id <= state["last_archive_id"]:
            return False

        state["last_archive_id"] = archive_id
        text = latest_archive.get("text", "")
        trigger_ref = f"archive:{archive_id}"
    else:
        if version <= state["last_version"]:
            return False
        state["last_version"] = version
        trigger_ref = f"version:{version}"

    if not text:
        return False

    record = {
        "status": "updated",
        "room_id": args.room_id,
        "version": version,
        "mode": args.mode,
        "trigger": args.trigger,
        "trigger_ref": trigger_ref,
        "chars": len(text),
    }

    if args.dry_run:
        record["action"] = "dry_run"
    else:
        if sys.platform != "darwin":
            raise RuntimeError("mac_paste_helper.py only supports macOS live mode.")
        try:
            if args.mode == "clipboard":
                copy_to_clipboard(text)
            else:
                paste_to_active_app(text)
            record["action"] = "applied"
        except subprocess.CalledProcessError as exc:
            error_record = {
                "status": "apply_failed",
                "room_id": args.room_id,
                "mode": args.mode,
                "trigger": args.trigger,
                "trigger_ref": trigger_ref,
                "reason": "native_command_failed",
                "exit_code": exc.returncode,
            }
            if args.mode == "paste":
                error_record["hint"] = "Grant Accessibility permission to the terminal app that runs this helper."
            print(json.dumps(error_record, ensure_ascii=False), flush=True)
            return False

    if args.trigger == "archive" and archive_id > 0:
        try:
            ack_payload, ack_attempts = acknowledge_archive_with_retry(
                args.server_url,
                args.room_id,
                archive_id,
                args.client_id,
                record["action"],
                args.request_timeout_seconds,
                state["curl_resolve"],
            )
            record["desktop_ack"] = "ok" if ack_payload.get("ok") else "failed"
            record["desktop_ack_archive_id"] = archive_id
            record["desktop_ack_attempts"] = ack_attempts
        except (HTTPError, URLError, socket.timeout) as exc:
            record["desktop_ack"] = "failed"
            record["desktop_ack_error"] = str(getattr(exc, "reason", exc))
            record["desktop_ack_attempts"] = 3
            try:
                ack_state = fetch_state(
                    args.server_url,
                    args.room_id,
                    min(args.request_timeout_seconds, 6.0),
                    state["curl_resolve"],
                )
                if archive_acknowledged(ack_state, archive_id, record["action"]):
                    record["desktop_ack"] = "ok"
                    record["desktop_ack_archive_id"] = archive_id
                    record["desktop_ack_verified_after_error"] = True
            except (HTTPError, URLError, socket.timeout) as verify_exc:
                record["desktop_ack_verify_error"] = str(getattr(verify_exc, "reason", verify_exc))
            if record["desktop_ack"] != "ok":
                remember_pending_ack(state, archive_id, record["action"])

    print(json.dumps(record, ensure_ascii=False), flush=True)
    state["applied_updates"] += 1
    return bool(args.stop_after_updates and state["applied_updates"] >= args.stop_after_updates)


def main() -> int:
    args = build_parser().parse_args()
    # Dry-run fixtures cannot touch the clipboard. Every real entrypoint (poll,
    # stream, foreground wrapper or direct invocation) shares the same user lock.
    # Acquire before fetching any input so rejection cannot replay/archive/ack it.
    if args.dry_run:
        return run_helper(args)
    # Keep cross-platform dry-run/smoke imports free of Unix-only fcntl/pwd.
    from helper_instance import HelperInstanceError, HelperInstanceLock

    try:
        with HelperInstanceLock():
            print(json.dumps({"status": "helper_instance_lock_acquired", "pid": os.getpid()}), flush=True)
            return run_helper(args)
    except HelperInstanceError as exc:
        print(json.dumps({"status": "helper_start_refused", "reason": str(exc)}), flush=True)
        return 73


def run_helper(args: argparse.Namespace) -> int:
    curl_resolve = parse_curl_resolve(args.curl_resolve)
    if not args.client_id:
        args.client_id = f"mac-helper-{socket.gethostname()}-{os.getpid()}"
    state = {
        "last_version": -1,
        "last_archive_id": -1,
        "applied_updates": 0,
        "startup_seeded": False,
        "curl_resolve": curl_resolve,
        "pending_acks": {},
    }
    connection_refused_count = 0
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    while True:
        try:
            if args.transport == "stream":
                for payload in stream_payloads(
                    args.server_url,
                    args.room_id,
                    args.request_timeout_seconds,
                    args.stream_max_time_seconds,
                    curl_resolve,
                ):
                    connection_refused_count = 0
                    retry_pending_acks(args, state)
                    if process_payload(args, payload, state):
                        return 0
                continue
            else:
                payload = fetch_state(args.server_url, args.room_id, args.request_timeout_seconds, curl_resolve)
                connection_refused_count = 0
                retry_pending_acks(args, state)
                if process_payload(args, payload, state):
                    return 0
        except (URLError, socket.timeout) as exc:
            connection_refused_count += 1
            reason = str(getattr(exc, "reason", exc))
            retry_delay_seconds = 30.0 if "authentication required" in reason else args.interval_seconds
            record = {
                "status": "retrying",
                "reason": reason,
                "server_url": args.server_url,
                "retry_delay_seconds": retry_delay_seconds,
            }
            if connection_refused_count == 1:
                record["hint"] = (
                    "relay server is not reachable; start it with "
                    f"`cd {project_root} && ./scripts/run_local_server.sh` "
                    "or use `./scripts/run_autopaste_local.sh` for one-command startup"
                )
            print(json.dumps(record, ensure_ascii=False), flush=True)
            time.sleep(retry_delay_seconds)
            continue


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
