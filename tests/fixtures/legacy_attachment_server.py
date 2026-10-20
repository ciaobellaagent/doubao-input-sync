"""Sanitized legacy handler fixture: no deployment addresses or runtime data.

Only the exact handler functions needed by the staging contract are retained;
state implementations come from the current test server.
"""
from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from app.server import (RoomStore, CopyArchiveStore, RoomUpdateConflict,
                        parse_capture_mode, utc_now_iso, CAPTURE_MODES, THEMES)

try:
    from .signal_trial import handle_get as handle_signal_get, handle_post as handle_signal_post
except ImportError:  # Direct entrypoint: python app/server.py.
    from signal_trial import handle_get as handle_signal_get, handle_post as handle_signal_post


STATIC_ROOT = Path(__file__).parent


class RelayHandler(BaseHTTPRequestHandler):
    @property
    def store(self) -> RoomStore:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def copy_store(self) -> CopyArchiveStore:
        return self.server.copy_store  # type: ignore[attr-defined]

    @property
    def default_room(self) -> str:
        return self.server.default_room  # type: ignore[attr-defined]

    @property
    def local_ip(self) -> str:
        return self.server.local_ip  # type: ignore[attr-defined]

    @property
    def base_path(self) -> str:
        return self.server.base_path  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = self._strip_base_path(parsed.path)
        if path is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        if handle_signal_get(self, path, parsed.query, STATIC_ROOT):
            return

        if path == "/api/ping":
            self._send_json({"ok": True, "ts": utc_now_iso()})
            return

        if path == "/api/server-info":
            self._send_json(
                {
                    "host": self.server.server_address[0],
                    "port": self.server.server_address[1],
                    "default_room": self.default_room,
                    "base_path": self.base_path,
                    "local_ip": self.local_ip,
                    "local_base_url": f"http://{self.local_ip}:{self.server.server_address[1]}{self.base_path}",
                    "archive_idle_seconds": self.server.archive_idle_seconds,  # type: ignore[attr-defined]
                    "claim_ttl_seconds": self.server.claim_ttl_seconds,  # type: ignore[attr-defined]
                    "capture_modes": sorted(CAPTURE_MODES),
                    "themes": sorted(THEMES),
                }
            )
            return

        if path == "/api/state":
            room_id = self._room_id_from_query(parsed.query)
            self._send_json(self.store.get(room_id).payload())
            return

        if path == "/api/helper-state":
            room_id = self._room_id_from_query(parsed.query)
            self._send_json(self.store.get(room_id).helper_payload())
            return

        if path == "/api/copy-history":
            self._handle_copy_history(parsed.query)
            return

        if path == "/api/stream":
            room_id = self._room_id_from_query(parsed.query)
            self._stream_room(room_id)
            return

        if path == "/" or path.startswith("/mobile/") or path.startswith("/pc/"):
            self._serve_index()
            return

        if path == "/debug/latency":
            self._serve_index("latency.html")
            return

        if path == "/assets/client.js":
            self._serve_file(STATIC_ROOT / "client.js", "application/javascript; charset=utf-8")
            return

        if path == "/assets/styles.css":
            self._serve_file(STATIC_ROOT / "styles.css", "text/css; charset=utf-8")
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = self._strip_base_path(parsed.path)
        if path is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        if handle_signal_post(self, path):
            return

        if path == "/api/settings":
            self._handle_settings_update()
            return

        if path == "/api/claim":
            self._handle_claim()
            return

        if path == "/api/release":
            self._handle_release()
            return

        if path == "/api/archive-ack":
            self._handle_archive_ack()
            return

        if path == "/api/capture":
            self._handle_capture()
            return

        if path == "/api/copy-archive":
            self._handle_copy_archive()
            return

        if path != "/api/update":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length) or b"{}")
        room_id = (payload.get("room_id") or self.default_room).strip()
        text = payload.get("text", "")
        source = (payload.get("source") or "unknown").strip()
        raw_capture_mode = payload.get("capture_mode")

        if not room_id:
            self._send_json({"error": "room_id is required"}, status=HTTPStatus.BAD_REQUEST)
            return

        if not isinstance(text, str):
            self._send_json({"error": "text must be a string"}, status=HTTPStatus.BAD_REQUEST)
            return

        capture_mode: Optional[str] = None
        if raw_capture_mode is not None:
            try:
                capture_mode = parse_capture_mode(str(raw_capture_mode))
            except (TypeError, ValueError):
                self._send_json({"error": "capture_mode must be auto, manual, or clipboard"}, status=HTTPStatus.BAD_REQUEST)
                return

        expected_settings_revision = payload.get("expected_settings_revision")
        if expected_settings_revision is not None and (
            type(expected_settings_revision) is not int or expected_settings_revision < 0
        ):
            self._send_json({"error": "expected_settings_revision must be a nonnegative integer"}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            room = self.store.update(
                room_id=room_id,
                text=text,
                source=source,
                capture_mode=capture_mode,
                expected_settings_revision=expected_settings_revision,
            )
        except RoomUpdateConflict as exc:
            self._send_json({"ok": False, "error": exc.error, "state": exc.state.payload()}, status=HTTPStatus.CONFLICT)
            return
        self._send_json(room.payload(), status=HTTPStatus.CREATED)

    def _send_json(self, payload: Dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path, content_type: str) -> None:
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _strip_base_path(self, path: str) -> str | None:
        if not self.base_path:
            return path
        if path == self.base_path:
            return "/"
        if path.startswith(f"{self.base_path}/"):
            return path[len(self.base_path) :]
        return None



def legacy_untouched_marker():
    return "old text behavior stays here"
