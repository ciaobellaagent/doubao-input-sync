#!/usr/bin/env python3
"""Windows helper: copy synced archive text to clipboard or paste into the active window."""

from __future__ import annotations

import argparse
import base64
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


HELPER_USER_AGENT = "doubao-input-sync-windows-helper/1.0"
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
        HELPER_USER_AGENT,
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
            "User-Agent": HELPER_USER_AGENT,
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
        HELPER_USER_AGENT,
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
            "User-Agent": HELPER_USER_AGENT,
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


def archive_marker(archive: dict) -> tuple[int, str, int] | None:
    """Identify an archive across SSE reconnects and relay counter resets."""
    archive_id = int(archive.get("archive_id") or 0)
    archived_at = str(archive.get("archived_at") or "")
    version = int(archive.get("version") or 0)
    if archived_at or version:
        return archive_id, archived_at, version
    return None


def seed_archive_state(state: dict, archive: dict) -> None:
    state["last_archive_id"] = int(archive.get("archive_id") or 0)
    state["last_archive_marker"] = archive_marker(archive)


def is_new_archive(state: dict, archive: dict) -> bool:
    archive_id = int(archive.get("archive_id") or 0)
    marker = archive_marker(archive)

    if marker is not None:
        if marker == state["last_archive_marker"]:
            return False
    elif archive_id <= state["last_archive_id"]:
        # Legacy relay payloads without archived_at/version can only use the
        # monotonic counter. Current payloads use the marker above, which also
        # survives an in-memory relay restart that resets archive_id to 1.
        return False

    state["last_archive_id"] = archive_id
    state["last_archive_marker"] = marker
    return True


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
        HELPER_USER_AGENT,
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
            encoding="utf-8",
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


def _powershell_executable() -> str:
    return "powershell.exe" if os.name == "nt" else "pwsh"


def run_powershell(script: str, input_text: str = "") -> None:
    encoded_command = base64.b64encode(("$ProgressPreference = 'SilentlyContinue'; " + script).encode("utf-16le")).decode(
        "ascii"
    )
    command = [_powershell_executable()]
    if os.name == "nt":
        command.append("-STA")
    command.extend(
        [
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded_command,
        ]
    )
    subprocess.run(
        command,
        input=input_text.encode("utf-8"),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=True,
    )


def copy_to_clipboard(text: str) -> None:
    # Clipboard contents travel over stdin, never in the command line or logs.
    run_powershell(
        "[Console]::InputEncoding = [Text.Encoding]::UTF8; "
        "$text = [Console]::In.ReadToEnd(); "
        "Add-Type -AssemblyName System.Windows.Forms; "
        "[System.Windows.Forms.Clipboard]::SetText($text, [System.Windows.Forms.TextDataFormat]::UnicodeText)",
        input_text=text,
    )


def paste_to_active_window(text: str, paste_delay_seconds: float) -> None:
    copy_to_clipboard(text)
    if paste_delay_seconds > 0:
        time.sleep(paste_delay_seconds)
    run_powershell(
        "$shell = New-Object -ComObject WScript.Shell; "
        "Start-Sleep -Milliseconds 80; "
        "$shell.SendKeys('^v')"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Watch synced text and project it to Windows.")
    parser.add_argument("--server-url", default="http://127.0.0.1:18766")
    parser.add_argument("--room-id", default="doubao-win-test")
    parser.add_argument("--interval-seconds", type=float, default=0.25)
    parser.add_argument("--request-timeout-seconds", type=float, default=12.0)
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
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Ignore the latest existing version/archive at startup and only react to newer content.",
    )
    parser.add_argument("--stop-after-updates", type=int, default=0)
    parser.add_argument(
        "--paste-delay-seconds",
        type=float,
        default=0.15,
        help="Delay between clipboard write and Ctrl+V in paste mode.",
    )
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


def delivery_action(args: argparse.Namespace) -> str:
    if args.dry_run:
        return "dry_run"
    return args.mode


def process_payload(args: argparse.Namespace, payload: dict, state: dict) -> bool:
    version = payload.get("version", 0)
    text = payload.get("text", "")
    latest_archive = payload.get("latest_archive")
    trigger_ref = None
    archive_id = 0

    if args.skip_existing and not state["startup_seeded"]:
        state["last_version"] = max(state["last_version"], version)
        if latest_archive:
            seed_archive_state(state, latest_archive)
        state["startup_seeded"] = True
        return False

    if args.trigger == "archive":
        if not latest_archive:
            return False

        archive_id = latest_archive.get("archive_id", 0)
        if not is_new_archive(state, latest_archive):
            return False

        text = latest_archive.get("text", "")
        trigger_ref = f"archive:{archive_id}"
    else:
        if version <= state["last_version"]:
            return False
        state["last_version"] = version
        trigger_ref = f"version:{version}"

    if not text:
        return False

    action = delivery_action(args)
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
        record["action"] = action
    else:
        try:
            if args.mode == "clipboard":
                copy_to_clipboard(text)
            else:
                paste_to_active_window(text, args.paste_delay_seconds)
            record["action"] = action
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
                error_record["hint"] = "Foreground paste uses WScript.Shell SendKeys; keep the target input focused."
            print(json.dumps(error_record, ensure_ascii=False), flush=True)
            return False

    if args.trigger == "archive" and archive_id > 0:
        try:
            ack_payload, ack_attempts = acknowledge_archive_with_retry(
                args.server_url,
                args.room_id,
                archive_id,
                args.client_id,
                action,
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
                if archive_acknowledged(ack_state, archive_id, action):
                    record["desktop_ack"] = "ok"
                    record["desktop_ack_archive_id"] = archive_id
                    record["desktop_ack_verified_after_error"] = True
            except (HTTPError, URLError, socket.timeout) as verify_exc:
                record["desktop_ack_verify_error"] = str(getattr(verify_exc, "reason", verify_exc))
            if record["desktop_ack"] != "ok":
                remember_pending_ack(state, archive_id, action)

    print(json.dumps(record, ensure_ascii=False), flush=True)
    state["applied_updates"] += 1
    return bool(args.stop_after_updates and state["applied_updates"] >= args.stop_after_updates)


def main() -> int:
    args = build_parser().parse_args()
    curl_resolve = parse_curl_resolve(args.curl_resolve)
    if not args.client_id:
        args.client_id = f"windows-helper-{socket.gethostname()}-{os.getpid()}"
    state = {
        "last_version": -1,
        "last_archive_id": -1,
        "last_archive_marker": None,
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

            payload = fetch_state(args.server_url, args.room_id, args.request_timeout_seconds, curl_resolve)
            connection_refused_count = 0
            retry_pending_acks(args, state)
            if process_payload(args, payload, state):
                return 0
            time.sleep(args.interval_seconds)
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
                    "relay server is not reachable; start it from "
                    f"{project_root} or set SERVER_URL to the public relay"
                )
            print(json.dumps(record, ensure_ascii=False), flush=True)
            time.sleep(retry_delay_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
