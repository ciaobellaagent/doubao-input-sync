"""Legacy adaptation is offline, deterministic and limited to attachments."""
from __future__ import annotations

import ast
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from app import server
from scripts import stage_legacy_attachments as staging


FIXTURE = Path(__file__).parent / "fixtures" / "legacy_attachment_server.py"


def load_candidate(config=""):
    original = FIXTURE.read_text(encoding="utf-8")
    candidate = staging.stage_source(original)
    namespace = {"__name__": "app.legacy_staging_test", "__package__": "app", "__file__": str(FIXTURE)}
    with patch.dict(os.environ, {"DOUBAO_ATTACHMENT_ORIGINS": config}):
        exec(compile(candidate, str(FIXTURE), "exec"), namespace)
    return namespace


class LegacyStagingTests(unittest.TestCase):
    def test_staging_preserves_original_bytes_and_entire_ast_outside_insertions(self):
        source = FIXTURE.read_text(encoding="utf-8")
        candidate = staging.stage_source(source)
        self.assertEqual(candidate.count(staging.MARKER), 1)
        restored = candidate
        for addition in (staging.MODULE_ADDITIONS, staging.GET_ADDITION, staging.POST_ADDITION):
            self.assertEqual(restored.count(addition), 1)
            restored = restored.replace(addition, "", 1)
        self.assertEqual(restored, source)
        self.assertEqual(ast.dump(ast.parse(restored)), ast.dump(ast.parse(source)))
        self.assertEqual(load_candidate()["legacy_untouched_marker"](), "old text behavior stays here")

    def test_duplicate_adaptation_current_server_and_any_pinned_function_drift_are_rejected(self):
        source = FIXTURE.read_text(encoding="utf-8")
        with self.assertRaisesRegex(staging.UnsupportedLegacySource, "already_staged"):
            staging.stage_source(staging.stage_source(source))
        for before, after in [
                ('if path == "/api/ping":', 'if path == "/api/changed":'),
                ('if path == "/api/settings":', 'if path == "/api/changed":'),
                ('body = json.dumps(payload, ensure_ascii=False)', 'body = json.dumps(payload, ensure_ascii=True)'),
                ('data = path.read_bytes()', 'data = b"different"'),
                ('if not self.base_path:', 'if self.base_path is None:'),
                ('from .signal_trial import handle_get', 'from .other_signal import handle_get')]:
            with self.subTest(before=before), self.assertRaises(staging.UnsupportedLegacySource):
                staging.stage_source(source.replace(before, after, 1))
        with self.assertRaises(staging.UnsupportedLegacySource):
            staging.stage_source(Path(server.__file__).read_text(encoding="utf-8"))

    def test_rejects_ambiguous_handler_base_name_collision_and_invalid_source(self):
        source = FIXTURE.read_text(encoding="utf-8")
        for invalid in [source + "\nclass RelayHandler: pass\n",
                        source.replace("class RelayHandler(BaseHTTPRequestHandler):", "class RelayHandler(object):"),
                        source + "\n_doubao_attachment_injected = True\n", "[", source.replace("\n", "\r\n")]:
            with self.subTest(source=invalid[:30]), self.assertRaises(staging.UnsupportedLegacySource):
                staging.stage_source(invalid)

    def test_file_staging_is_exclusive_separate_private_and_never_changes_input(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "server.py"
            output = Path(directory) / "server-candidate.py"
            original = FIXTURE.read_bytes()
            source.write_bytes(original)
            receipt = staging.stage_file(source, output)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(receipt["source_sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                staging.stage_file(source, output)
            with self.assertRaisesRegex(staging.UnsupportedLegacySource, "in_place"):
                staging.stage_file(source, source)
            alias = Path(directory) / "source-alias.py"
            alias.symlink_to(source)
            with self.assertRaisesRegex(staging.UnsupportedLegacySource, "in_place"):
                staging.stage_file(source, alias)
            self.assertEqual(source.read_bytes(), original)

    def test_exact_origin_configuration_rejects_wildcards_paths_credentials_and_noncanonical_values(self):
        namespace = load_candidate("https://relay.example http://[::1]:8080")
        self.assertEqual(namespace["_DOUBAO_ATTACHMENT_ORIGINS"],
                         frozenset({"https://relay.example", "http://[::1]:8080"}))
        parse = namespace["_doubao_attachment_configured_origins"]
        for invalid in ["*", "https://*.example", "https://" + "user:password@relay.example", "https://relay.example/",
                        "https://relay.example/path", "https://relay.example?room=x", "https://relay.example#fragment",
                        "ftp://relay.example", "https://relay.example:443", "https://RELAY.example",
                        "http://relay.example:0", "https://relay.example%2f.evil.example", "http://[invalid]"]:
            with self.subTest(origin=invalid), patch.dict(os.environ, {"DOUBAO_ATTACHMENT_ORIGINS": invalid}):
                with self.assertRaisesRegex(ValueError, "invalid_attachment_origin_config"):
                    parse()

    def test_cli_only_writes_candidate_and_uses_fixed_failure_reason(self):
        script = Path(staging.__file__)
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "server-candidate.py"
            command = [sys.executable, str(script), "--source", str(FIXTURE), "--output", str(candidate)]
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertTrue(json.loads(result.stdout)["ok"])
            self.assertIn(staging.MARKER, candidate.read_text())
            duplicate = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(duplicate.returncode, 1)
            self.assertEqual(json.loads(duplicate.stdout), {"ok": False, "error": "legacy_candidate_file_error"})
            invalid = Path(directory) / "invalid.py"
            invalid.write_text("# source with an unknown version\n")
            rejected = subprocess.run([sys.executable, str(script), "--source", str(invalid),
                                       "--output", str(Path(directory) / "rejected.py")], capture_output=True, text=True)
            self.assertEqual(rejected.returncode, 1)
            self.assertEqual(json.loads(rejected.stdout)["error"], "legacy_source_version_mismatch")
            self.assertFalse((Path(directory) / "rejected.py").exists())


class LegacyStagedHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.module = load_candidate("https://relay.example")
        static = Path(self.temp.name) / "static"
        static.mkdir()
        (static / "attachments.js").write_text("window.attachmentsLoaded = true;")
        (static / "attachments.css").write_text("body {color: black;}")
        self.module["STATIC_ROOT"] = static
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.module["RelayHandler"])
        self.httpd.daemon_threads = True
        self.httpd.store = server.RoomStore(0.5, 45)
        self.httpd.copy_store = server.CopyArchiveStore(Path(self.temp.name) / "archives.sqlite3")
        self.httpd.default_room = "test"
        self.httpd.base_path = "/doubao"
        self.httpd.local_ip = "127.0.0.1"
        self.httpd.archive_idle_seconds = 0.5
        self.httpd.claim_ttl_seconds = 45
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.httpd.server_address, timeout=3)
        try:
            headers = dict(headers or {})
            if isinstance(body, dict):
                body = json.dumps(body).encode()
                headers.setdefault("Content-Type", "application/json")
            connection.request("POST" if body is not None else "GET", "/doubao" + path, body, headers)
            response = connection.getresponse()
            data = response.read()
            if "application/json" in response.getheader("Content-Type", ""):
                data = json.loads(data)
            return response.status, data, dict(response.getheaders())
        finally:
            connection.close()

    def test_binary_upload_roundtrip_json_routes_and_exact_asset_paths(self):
        identity = str(uuid4())
        data = b"x" * (1024 * 1024 + 1)
        status, result, _ = self.request("/api/attachments/upload?room_id=test&attachment_id=" + identity, data,
                                        {"X-Attachment-Name": "sample.bin", "Content-Type": "application/octet-stream"})
        self.assertEqual(status, 200)
        self.assertEqual(result["attachment"]["size"], len(data))
        self.assertEqual(self.request("/api/attachments?room_id=test")[1]["items"], [result["attachment"]])
        status, downloaded, headers = self.request("/api/attachments/blob?room_id=test&attachment_id=" + identity)
        self.assertEqual((status, downloaded), (200, data))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        for name, content_type in [("attachments.js", "application/javascript"), ("attachments.css", "text/css")]:
            status, _, headers = self.request("/assets/" + name)
            self.assertEqual(status, 200)
            self.assertTrue(headers["Content-Type"].startswith(content_type))
        self.assertEqual(self.request("/assets/../archives.sqlite3")[0], 404)
        status, _, _ = self.request("/api/attachments/delete", {"room_id": "test", "attachment_id": identity})
        self.assertEqual(status, 200)

    def test_only_attachment_routes_receive_new_guards_and_legacy_text_behavior_is_preserved(self):
        bad = {"Host": "unconfigured.example", "Origin": "https://unconfigured.example", "Content-Type": "text/plain"}
        self.assertEqual(self.request("/api/ping", headers=bad)[0], 200)
        self.httpd.store.update_settings("test", capture_mode="manual")
        body = json.dumps({"room_id": "test", "text": "legacy text", "source": "legacy-test"}).encode()
        status, result, _ = self.request("/api/update", body, bad)
        self.assertEqual(status, 201)
        self.assertEqual(result["text"], "legacy text")
        self.assertEqual(self.request("/api/attachments?room_id=test", headers=bad)[0], 403)
        self.assertEqual(self.request("/assets/attachments.js", headers=bad)[0], 403)
        self.assertEqual(self.request("/api/attachments/delete", {}, bad)[0], 403)

    def test_exact_hosts_origins_and_fetch_site_no_forwarded_header_trust(self):
        for headers in [{"Host": "relay.example", "Origin": "https://relay.example"},
                        {"Origin": "http://127.0.0.1:" + str(self.httpd.server_port)}]:
            self.assertEqual(self.request("/api/attachments?room_id=test", headers=headers)[0], 200)
        invalid = [{"Host": "relay.example.evil.example"}, {"Origin": "https://unconfigured.example"},
                   {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                   {"Host": "unconfigured.example", "X-Forwarded-Host": "relay.example", "X-Forwarded-Proto": "https"}]
        for headers in invalid:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/attachments?room_id=test", headers=headers)[0], 403)

    def test_duplicate_security_headers_and_json_framing_are_rejected(self):
        for name, value in [("Host", "relay.example"), ("Origin", "https://relay.example"),
                            ("Sec-Fetch-Site", "same-origin")]:
            connection = http.client.HTTPConnection(*self.httpd.server_address, timeout=3)
            try:
                connection.putrequest("GET", "/doubao/api/attachments?room_id=test", skip_host=True)
                connection.putheader("Host", "relay.example")
                connection.putheader("Origin", "https://relay.example")
                connection.putheader("Sec-Fetch-Site", "same-origin")
                connection.putheader(name, value)
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
            finally:
                connection.close()
        status, result, _ = self.request("/api/attachments/delete", b"{}", {"Content-Type": "text/plain"})
        self.assertEqual((status, result["error"]), (415, "content_type_must_be_json"))
        self.assertEqual(self.request("/api/attachments", {})[0], 404)


if __name__ == "__main__":
    unittest.main()
