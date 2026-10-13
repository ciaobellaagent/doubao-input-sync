"""Public-release request boundaries; no UI or production service access."""
import argparse
import base64
import os
import contextlib
import http.client
import io
import json
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from app import server
from scripts import mac_paste_helper, windows_paste_helper


class RequestBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.RelayHandler)
        self.httpd.store = server.RoomStore(2, 45)
        self.httpd.copy_store = server.CopyArchiveStore(Path(self.temp.name) / "archive.sqlite3")
        self.httpd.default_room = "test"
        self.httpd.base_path = ""
        self.httpd.local_ip = "127.0.0.1"
        self.httpd.public_origins = frozenset()
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, path="/api/ping", body=None, headers=None):
        conn = http.client.HTTPConnection(*self.httpd.server_address, timeout=2)
        headers = headers or {}
        if body is not None:
            headers.setdefault("Content-Type", "application/json")
        conn.request("POST" if body is not None else "GET", path, body, headers)
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def test_local_and_explicit_public_origins_work_without_trusting_forwarded_headers(self):
        self.assertEqual(self.request(headers={"Origin": self.origin})[0], 200)
        self.httpd.public_origins = {"https://relay.example"}
        for host in [f"127.0.0.1:{self.httpd.server_port}", "relay.example"]:
            status, _ = self.request(headers={"Host": host, "Origin": "https://relay.example", "Sec-Fetch-Site": "same-origin"})
            self.assertEqual(status, 200)
        status, payload = self.request(headers={"Host": "attacker.example", "X-Forwarded-Host": "relay.example", "X-Forwarded-Proto": "https"})
        self.assertEqual((status, payload["error"]), (403, "host_not_allowed"))

    def test_rebinding_host_and_cross_origin_requests_are_denied(self):
        cases = [
            {"Host": "attacker.example"},
            {"Host": f"127.0.0.1:{self.httpd.server_port + 1}"},
            {"Origin": "https://attacker.example"},
            {"Origin": "null"},
            {"Origin": "http://localhost:1"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
        ]
        for headers in cases:
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], 403)
        self.assertEqual(self.request(headers={"Sec-Fetch-Site": "none"})[0], 200)

    def test_plain_text_and_form_posts_cannot_reach_any_mutation(self):
        paths = ["update", "capture", "settings", "claim", "release", "archive-ack", "copy-archive", "signal/register"]
        for path in paths:
            for content_type in ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data"]:
                with self.subTest(path=path, content_type=content_type):
                    status, payload = self.request("/api/" + path, b'{"text":"injected"}', {"Content-Type": content_type})
                    self.assertEqual((status, payload["error"]), (415, "content_type_must_be_json"))
        self.assertEqual(self.httpd.store.get("test").text, "")

    def test_approved_json_post_and_cli_post_work_but_bad_origin_does_not(self):
        _, settings = self.request("/api/settings", b'{"capture_mode":"manual"}')
        body = json.dumps({"text": "explicit local request", "capture_mode": "manual", "expected_settings_revision": settings["settings"]["revision"]}).encode()
        self.assertEqual(self.request("/api/update", body)[0], 201)
        self.assertEqual(self.request("/api/update", body, {"Origin": self.origin, "Content-Type": "application/json; charset=utf-8"})[0], 201)
        self.assertEqual(self.request("/api/update", body, {"Origin": "https://bad.example"})[0], 403)

    def test_legacy_post_body_is_bounded_and_malformed_json_is_rejected(self):
        status, payload = self.request("/api/update", b"", {"Content-Length": str(server.MAX_COPY_BODY_BYTES + 1)})
        self.assertEqual((status, payload["error"]), (413, "body_too_large"))
        for body in [b"[", b"[]", b'{"room_id": 123}']:
            self.assertEqual(self.request("/api/update", body)[0], 400)

    def test_helper_post_curl_config_preserves_exact_json_and_keeps_payload_out_of_argv(self):
        for module in [mac_paste_helper, windows_paste_helper]:
            with self.subTest(module=module.__name__), patch.object(module, "_CLOUDFLARE_ACCESS_HEADERS", {}), patch.object(subprocess, "run", wraps=subprocess.run) as run:
                settings = module.post_json(self.origin + "/api/settings", {"capture_mode": "manual"}, 2, [])
                text = '正文 "quoted" \\ path\nsecond line\tend'
                result = module.post_json(self.origin + "/api/update", {"text": text, "capture_mode": "manual", "expected_settings_revision": settings["settings"]["revision"]}, 2, [])
                self.assertEqual(result["text"], text)
                self.assertNotIn(text, " ".join(run.call_args.args[0]))
                self.assertNotIn("--data-binary", run.call_args.args[0])

    def test_duplicate_security_headers_are_denied(self):
        for name, value in [("Host", f"127.0.0.1:{self.httpd.server_port}"), ("Origin", self.origin), ("Content-Length", "2"), ("Content-Type", "application/json")]:
            conn = http.client.HTTPConnection(*self.httpd.server_address, timeout=2)
            conn.putrequest("POST", "/api/update", skip_host=True)
            conn.putheader("Host", f"127.0.0.1:{self.httpd.server_port}")
            conn.putheader("Origin", self.origin)
            conn.putheader("Content-Length", "2")
            conn.putheader("Content-Type", "application/json")
            conn.putheader(name, value)
            conn.endheaders(b"{}")
            response = conn.getresponse()
            self.assertIn(response.status, [400, 403, 415])
            response.read()
            conn.close()


class StartupBoundaryTests(unittest.TestCase):
    def test_listener_starts_without_reverse_dns_or_machine_hostname(self):
        with patch("socket.getfqdn", side_effect=AssertionError("unexpected reverse DNS")):
            listener = server.RelayHTTPServer(("127.0.0.1", 0), server.RelayHandler)
            try:
                self.assertEqual(listener.server_name, "127.0.0.1")
                self.assertGreater(listener.server_port, 0)
            finally:
                listener.server_close()

    def test_loopback_is_default_and_remote_requires_explicit_opt_in(self):
        self.assertEqual(server.build_parser().parse_args([]).host, "127.0.0.1")
        self.assertTrue(server.loopback_host("localhost"))
        self.assertTrue(server.loopback_host("127.0.0.1"))
        self.assertFalse(server.loopback_host("0.0.0.0"))
        with patch("sys.argv", ["server.py", "--host", "0.0.0.0"]), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                server.main()
        self.assertEqual(caught.exception.code, 2)
        args = server.build_parser().parse_args(["--host", "0.0.0.0", "--allow-remote", "--public-origin", "https://relay.example"])
        self.assertTrue(args.allow_remote)
        self.assertEqual(args.public_origin, ["https://relay.example"])

    def test_public_origin_cannot_include_credentials_path_or_wildcard_scheme(self):
        self.assertEqual(server.parse_public_origin("https://RELAY.example:443/"), "https://relay.example")
        for origin in ["file://relay.example", "https://" + "user:pass@relay.example", "https://relay.example/path", "https://relay.example?q=1", "https://relay.example/#bad", "https://relay.example:bad", "null"]:
            with self.subTest(origin=origin), self.assertRaises(argparse.ArgumentTypeError):
                server.parse_public_origin(origin)


class CredentialRedirectTests(unittest.TestCase):
    def test_no_curl_fallback_rejects_redirect_without_forwarding_service_credentials(self):
        seen = []
        class Sink(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("CF-Access-Client-Secret"))
                self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
            def log_message(self, *args): pass
        sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{sink.server_port}/different-origin")
                self.end_headers()
            do_POST = do_GET
            def log_message(self, *args): pass
        redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        for httpd in (sink, redirect):
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            for module in [mac_paste_helper, windows_paste_helper]:
                with self.subTest(module=module.__name__), patch.object(module, "_CLOUDFLARE_ACCESS_HEADERS", {"CF-Access-Client-Id": "fake-id", "CF-Access-Client-Secret": "fake-secret"}), patch.object(subprocess, "run", side_effect=FileNotFoundError), patch.object(module, "validate_transport"):
                    # Isolate redirect handling using loopback HTTP fixtures; HTTPS is tested below.
                    url = f"http://127.0.0.1:{redirect.server_port}/start"
                    with self.assertRaises(URLError): module.fetch_json(url, 2, [])
                    with self.assertRaises(URLError): module.post_json(url, {}, 2, [])
            self.assertEqual(seen, [])
        finally:
            for httpd in (redirect, sink):
                httpd.shutdown(); httpd.server_close()


class HelperTransportAndPrivacyTests(unittest.TestCase):
    def test_service_headers_require_https_before_any_process_is_spawned(self):
        for module in [mac_paste_helper, windows_paste_helper]:
            with self.subTest(module=module.__name__), patch.object(module, "_CLOUDFLARE_ACCESS_HEADERS", {"CF-Access-Client-Id": "fake-id", "CF-Access-Client-Secret": "fake-secret"}), patch.object(subprocess, "run") as run:
                with self.assertRaises(URLError): module.fetch_json("http://127.0.0.1:1", 2, [])
                with self.assertRaises(URLError): module.post_json("http://127.0.0.1:1", {}, 2, [])
                with self.assertRaises(URLError): next(module.stream_payloads("http://127.0.0.1:1", "room", 2, 3, []))
                run.assert_not_called()
        for url in ["file:///tmp/data", "https://" + "user:pass@relay.example"]:
            with self.assertRaises(URLError): mac_paste_helper.validate_transport(url, {})

    def test_partial_credentials_are_rejected_without_printing_values(self):
        for module in [mac_paste_helper, windows_paste_helper]:
            with patch.object(module, "_CLOUDFLARE_ACCESS_HEADERS", None), patch.dict(os.environ, {"CF_ACCESS_CLIENT_ID": "private-id"}, clear=True):
                with self.assertRaises(RuntimeError) as caught: module.cloudflare_access_headers()
                self.assertNotIn("private-id", str(caught.exception))

    def test_windows_clipboard_text_uses_stdin_and_never_enters_encoded_command(self):
        secret = "private clipboard 内容\nsecond line"
        with patch.object(subprocess, "run") as run:
            windows_paste_helper.copy_to_clipboard(secret)
        call = run.call_args
        command = call.args[0]
        script = base64.b64decode(command[-1]).decode("utf-16le")
        self.assertNotIn(secret, script)
        self.assertNotIn(base64.b64encode(secret.encode()).decode(), script)
        self.assertEqual(call.kwargs["input"], secret.encode("utf-8"))
        self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(call.kwargs["stdout"], subprocess.DEVNULL)

    def test_windows_apply_error_cannot_log_encoded_clipboard_contents(self):
        args = windows_paste_helper.build_parser().parse_args(["--no-skip-existing"])
        state = {"last_version": -1, "last_archive_id": -1, "last_archive_marker": None,
                 "applied_updates": 0, "startup_seeded": False, "curl_resolve": [], "pending_acks": {}}
        payload = {"version": 1, "latest_archive": {"archive_id": 1, "archived_at": "now", "text": "private"}}
        encoded_secret = "REVDSUFTRUZBTFRfU0VDUkVU"
        exception = subprocess.CalledProcessError(7, ["powershell", "-EncodedCommand", encoded_secret])
        out = io.StringIO()
        with patch.object(windows_paste_helper, "copy_to_clipboard", side_effect=exception), contextlib.redirect_stdout(out):
            windows_paste_helper.process_payload(args, payload, state)
        self.assertNotIn(encoded_secret, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["exit_code"], 7)


if __name__ == "__main__":
    unittest.main()
