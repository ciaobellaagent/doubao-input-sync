#!/usr/bin/env python3
"""Smoke test for the local relay server and macOS helper contract."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

# Isolated loopback fixtures must never depend on a workstation/CI proxy.
urlopen = build_opener(ProxyHandler({})).open

import mac_paste_helper


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_ping(server_url: str, timeout_seconds: float = 10, server_process=None) -> None:
    deadline = time.time() + timeout_seconds
    last_error = "no response"
    while time.time() < deadline:
        if server_process is not None and server_process.poll() is not None:
            raise RuntimeError(f"local relay exited before readiness: {server_process.returncode}")
        try:
            with urlopen(f"{server_url}/api/ping", timeout=1) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("ok"):
                return
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.2)
    raise RuntimeError(f"server failed to answer /api/ping before timeout ({last_error})")


def read_sse_event(server_url: str, room_id: str, results: List[Dict[str, object]], stop_after: int = 2) -> None:
    stream_url = f"{server_url}/api/stream?{urlencode({'room_id': room_id})}"
    with urlopen(stream_url, timeout=10) as response:
        event_name = None
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line:
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
                continue
            if line.startswith("data:") and event_name == "room_state":
                payload = json.loads(line.split(":", 1)[1].strip())
                results.append(payload)
                if len(results) >= stop_after:
                    return


def post_json(server_url: str, path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{server_url}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def post_update(server_url: str, room_id: str, text: str, source: str, capture_mode: str | None = None) -> dict:
    payload = {"room_id": room_id, "text": text, "source": source}
    if capture_mode is not None:
        payload["capture_mode"] = capture_mode
    return post_json(server_url, "/api/update", payload)


def post_settings(server_url: str, room_id: str, capture_mode: str) -> dict:
    return post_json(
        server_url,
        "/api/settings",
        {"room_id": room_id, "capture_mode": capture_mode},
    )


def post_theme(server_url: str, room_id: str, theme: str) -> dict:
    return post_json(
        server_url,
        "/api/settings",
        {"room_id": room_id, "theme": theme},
    )


def post_capture(server_url: str, room_id: str, expected_version: int) -> dict:
    return post_json(
        server_url,
        "/api/capture",
        {"room_id": room_id, "expected_version": expected_version},
    )


def post_capture_expect_conflict(server_url: str, room_id: str, expected_version: int) -> dict:
    try:
        post_capture(server_url, room_id, expected_version)
    except HTTPError as exc:
        payload = json.loads(exc.read().decode("utf-8"))
        payload["http_status"] = exc.code
        return payload
    raise AssertionError("capture unexpectedly accepted a stale version")


def get_state(server_url: str, room_id: str) -> dict:
    with urlopen(f"{server_url}/api/state?{urlencode({'room_id': room_id})}", timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_archive_count(server_url: str, room_id: str, count: int, timeout_seconds: float = 8) -> dict:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        payload = get_state(server_url, room_id)
        if len(payload.get("history") or []) >= count:
            return payload
        time.sleep(0.15)
    raise RuntimeError(f"archive history did not reach {count} items before timeout")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Doubao Input Sync smoke test.")
    parser.add_argument("--room-id", default="smoke-room")
    parser.add_argument("--base-path", default="")
    parser.add_argument("--output-json", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    port = find_free_port()
    base_path = args.base_path.rstrip("/")
    server_url = f"http://127.0.0.1:{port}{base_path}"

    with tempfile.TemporaryDirectory(prefix="doubao_sync_smoke_") as temp_dir:
        temp_path = Path(temp_dir)
        server_log = temp_path / "server.log"
        helper_log = temp_path / "helper.log"

        with server_log.open("w", encoding="utf-8") as server_handle:
            server_args = [
                sys.executable,
                "-u",
                "-c",
                "import faulthandler, runpy; faulthandler.dump_traceback_later(8); runpy.run_path('app/server.py', run_name='__main__')",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--default-room",
                args.room_id,
                "--archive-idle-seconds",
                "0.6",
                "--archive-db",
                str(temp_path / "archives.sqlite3"),
            ]
            server_args.extend(["--base-path", args.base_path])
            server_proc = subprocess.Popen(
                server_args,
                cwd=PROJECT_ROOT,
                stdout=server_handle,
                stderr=subprocess.STDOUT,
            )

        try:
            wait_for_ping(server_url, server_process=server_proc)

            sse_events: List[Dict[str, object]] = []
            sse_thread = threading.Thread(
                target=read_sse_event,
                args=(server_url, args.room_id, sse_events, 8),
                daemon=True,
            )
            sse_thread.start()
            time.sleep(0.3)

            with helper_log.open("w", encoding="utf-8") as helper_handle:
                helper_proc = subprocess.Popen(
                    [
                        sys.executable,
                        "scripts/mac_paste_helper.py",
                        "--server-url",
                        server_url,
                        "--room-id",
                        args.room_id,
                        "--mode",
                        "clipboard",
                        "--dry-run",
                        "--stop-after-updates",
                        "1",
                    ],
                    cwd=PROJECT_ROOT,
                    stdout=helper_handle,
                    stderr=subprocess.STDOUT,
                )

            time.sleep(0.4)
            post_settings(server_url, args.room_id, "auto")
            auto_draft_before_manual = post_update(
                server_url,
                args.room_id,
                "这段自动草稿会在计时结束前切成换气模式",
                "smoke-test-mode-switch",
                capture_mode="auto",
            )
            manual_settings = post_settings(server_url, args.room_id, "manual")
            theme_settings = post_theme(server_url, args.room_id, "blue")
            time.sleep(0.9)
            switched_pause_state = get_state(server_url, args.room_id)
            posted = post_update(
                server_url,
                args.room_id,
                "这是一段允许长停顿的手动测试文本",
                "smoke-test-manual",
                capture_mode="manual",
            )
            state = get_state(server_url, args.room_id)
            time.sleep(0.9)
            paused_state = get_state(server_url, args.room_id)
            stale_capture_result = post_capture_expect_conflict(server_url, args.room_id, posted["version"] - 1)
            capture_result = post_capture(server_url, args.room_id, posted["version"])
            archived_state = wait_for_archive_count(server_url, args.room_id, 1)

            helper_exit = helper_proc.wait(timeout=5)
            acknowledged_state = get_state(server_url, args.room_id)

            auto_settings = post_settings(server_url, args.room_id, "auto")
            auto_posted = post_update(
                server_url,
                args.room_id,
                "这是一段停顿后自动发送的测试文本",
                "smoke-test-auto",
                capture_mode="auto",
            )
            auto_archived_state = wait_for_archive_count(server_url, args.room_id, 2)

            sse_thread.join(timeout=5)

            helper_output = helper_log.read_text(encoding="utf-8").strip().splitlines()
            manual_archive_id = capture_result.get("archive", {}).get("archive_id")
            acknowledged_history = acknowledged_state.get("history") or []
            manual_acknowledged = next(
                (item for item in acknowledged_history if item.get("archive_id") == manual_archive_id),
                {},
            )
            helper_reset_state = {
                "startup_seeded": True,
                "last_version": 12581,
                "last_archive_id": 298,
                "pending_acks": {297: {"archive_id": 297}},
            }
            relay_reset_detected = mac_paste_helper.reset_relay_sequence_if_needed(
                {"version": 0},
                helper_reset_state,
                emit_log=False,
            )
            report = {
                "status": "passed",
                "server_url": server_url,
                "room_id": args.room_id,
                "posted": posted,
                "state": state,
                "auto_draft_before_manual": auto_draft_before_manual,
                "switched_pause_state": switched_pause_state,
                "paused_state": paused_state,
                "manual_settings": manual_settings,
                "theme_settings": theme_settings,
                "capture_result": capture_result,
                "stale_capture_result": stale_capture_result,
                "archived_state": archived_state,
                "acknowledged_state": acknowledged_state,
                "auto_settings": auto_settings,
                "auto_posted": auto_posted,
                "auto_archived_state": auto_archived_state,
                "sse_events": sse_events,
                "helper_exit_code": helper_exit,
                "helper_output": helper_output,
                "checks": {
                    "manual_mode_enabled": manual_settings.get("settings", {}).get("capture_mode") == "manual",
                    "server_theme_sync_preserved": theme_settings.get("settings", {}).get("theme") == "blue" and theme_settings.get("settings", {}).get("capture_mode") == "manual",
                    "switch_to_manual_cancels_auto_timer": not switched_pause_state.get("history"),
                    "state_reflects_update": state.get("text") == "这是一段允许长停顿的手动测试文本",
                    "manual_pause_does_not_archive": not paused_state.get("history"),
                    "stale_manual_send_is_rejected": stale_capture_result.get("http_status") == 409 and stale_capture_result.get("error") == "version_conflict",
                    "manual_capture_is_immediate": capture_result.get("archived") is True,
                    "sse_received_update": any(event.get("version") == posted.get("version") for event in sse_events),
                    "helper_saw_update": any('"status": "updated"' in line for line in helper_output),
                    "history_archived_manual": bool(archived_state.get("history")) and archived_state["history"][-1].get("text") == "这是一段允许长停顿的手动测试文本",
                    "helper_acknowledged_archive": bool(manual_acknowledged.get("desktop_received_at")) and manual_acknowledged.get("desktop_delivery_action") == "dry_run",
                    "auto_mode_restored": auto_settings.get("settings", {}).get("capture_mode") == "auto",
                    "mode_change_preserves_theme": auto_settings.get("settings", {}).get("theme") == "blue",
                    "auto_mode_archives_after_idle": bool(auto_archived_state.get("history")) and auto_archived_state["history"][-1].get("text") == "这是一段停顿后自动发送的测试文本",
                    "helper_recovers_after_relay_restart": relay_reset_detected and helper_reset_state["last_version"] == -1 and helper_reset_state["last_archive_id"] == -1 and not helper_reset_state["pending_acks"],
                },
            }

            if not all(report["checks"].values()):
                report["status"] = "failed"
                raise AssertionError(json.dumps(report, ensure_ascii=False, indent=2))

            Path(args.output_json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            return 0
        except Exception:
            # This disposable server only processes synthetic smoke-test data.
            sys.stderr.write("Local smoke-test relay output:\n" + server_log.read_text()[-4000:] + "\n")
            raise
        finally:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                server_proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
