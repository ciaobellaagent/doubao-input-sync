#!/usr/bin/env python3
"""Stage attachment-only additions to the recognized legacy relay handler.

This is a source adapter, not a deployer: it never imports the input, changes an
existing file, starts a server, or changes legacy text routes. Relevant legacy
functions must match the pinned source fingerprints exactly. New security
checks apply exclusively to attachment APIs and attachment static assets.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path


MAX_SOURCE_BYTES = 256 * 1024
MARKER = "# DOUBAO_LEGACY_ATTACHMENTS_ADAPTER_V1"
SUPPORTED_SOURCE_SHA256 = frozenset({
    # Recognized unmodified legacy source; no deployment identifiers are stored.
    "9170fc914fd04cdcb94dd5d929ef399b5ffa1289be7e6bbd8b642106df40cdc6",
    # Sanitized handler-only regression fixture (tests/fixtures).
    "17b318c24b146f65f11c02e15f0dbe70bc23b218b669e275ac2b0c9ef52202e3",
})
SUPPORTED_FUNCTIONS = {
    "do_GET": "4466b4d3d31b8a80aba334b7fabd334808c434098de899517e0bbe84ab7044ec",
    "do_POST": "04b1d13388166c6d7b510862b9e660cf60adcf133c82f27f81cf322b92ae03e3",
    "_send_json": "cb0680120ad920d535dd1761a09108b7b9dbca652a4dac9f0b6877ce99e18861",
    "_serve_file": "d339235665743c492272cdf7a48e71846c08aace984306723f571321883776bc",
    "_strip_base_path": "2f2b898bca3f1912fcb59a5eaa7a66132a02763adb8e38a81f088cae9938df05",
}
SIGNAL_IMPORT_SHA256 = "28882361157cfc6814456621680aaffcf0cadc011e4f6189e4a240b400e7c3d0"

MODULE_ADDITIONS = '''

# DOUBAO_LEGACY_ATTACHMENTS_ADAPTER_V1
try:
    from .attachments import handle_get as handle_attachment_get, handle_post as handle_attachment_post
except ImportError:  # Direct entrypoint: python app/server.py.
    from attachments import handle_get as handle_attachment_get, handle_post as handle_attachment_post


def _doubao_attachment_configured_origins():
    values = os.environ.get("DOUBAO_ATTACHMENT_ORIGINS", "").split()
    if len(values) > 32:
        raise ValueError("invalid_attachment_origin_config")
    origins = set()
    for value in values:
        try:
            parsed = urlparse(value)
            port = parsed.port
            host = parsed.hostname
        except ValueError:
            raise ValueError("invalid_attachment_origin_config") from None
        if (len(value) > 2048 or parsed.scheme not in {"http", "https"} or not host or
                not host.isascii() or any(c.isspace() for c in value) or "*" in value or "%" in value or
                parsed.username is not None or parsed.password is not None or
                parsed.path or parsed.params or parsed.query or parsed.fragment or
                (port is not None and not 1 <= port <= 65535)):
            raise ValueError("invalid_attachment_origin_config")
        host = host.lower()
        authority = "[" + host + "]" if ":" in host else host
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            authority += ":" + str(port)
        canonical = parsed.scheme + "://" + authority
        # Exact origin syntax, including default-port normalization; no URL
        # paths, wildcard matches, credentials, forwarded-header trust or regex.
        if value != canonical:
            raise ValueError("invalid_attachment_origin_config")
        origins.add(canonical)
    return frozenset(origins)


_DOUBAO_ATTACHMENT_ORIGINS = _doubao_attachment_configured_origins()


def _doubao_attachment_allowed(handler):
    port = handler.server.server_address[1]
    origins = {"http://" + host + ":" + str(port) for host in ("127.0.0.1", "localhost", "[::1]")}
    if port == 80:
        origins.update({"http://127.0.0.1", "http://localhost", "http://[::1]"})
    origins.update(_DOUBAO_ATTACHMENT_ORIGINS)
    authorities = {urlparse(origin).netloc for origin in origins}
    hosts = handler.headers.get_all("Host", [])
    received_origins = handler.headers.get_all("Origin", [])
    sites = handler.headers.get_all("Sec-Fetch-Site", [])
    error = None
    if len(hosts) != 1 or hosts[0].lower() not in authorities:
        error = "host_not_allowed"
    elif len(received_origins) > 1 or (received_origins and received_origins[0] not in origins):
        error = "origin_not_allowed"
    elif len(sites) > 1 or (sites and sites[0].lower() in {"cross-site", "same-site"}):
        error = "cross_origin_request"
    if error:
        handler.close_connection = True
        handler._send_json({"ok": False, "error": error}, status=HTTPStatus.FORBIDDEN)
        return False
    return True
'''

GET_ADDITION = '''        if path == "/api/attachments" or path.startswith("/api/attachments/") or path in {
                "/assets/attachments.js", "/assets/attachments.css"}:
            if not _doubao_attachment_allowed(self):
                return
            if path == "/assets/attachments.js":
                self._serve_file(STATIC_ROOT / "attachments.js", "application/javascript; charset=utf-8")
            elif path == "/assets/attachments.css":
                self._serve_file(STATIC_ROOT / "attachments.css", "text/css; charset=utf-8")
            elif not handle_attachment_get(self, path, parsed.query):
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

'''

POST_ADDITION = '''        if path == "/api/attachments" or path.startswith("/api/attachments/"):
            if not _doubao_attachment_allowed(self):
                return
            if not handle_attachment_post(self, path, parsed.query):
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

'''


class UnsupportedLegacySource(ValueError):
    pass


def _require(allowed, error):
    if not allowed:
        raise UnsupportedLegacySource(error)


def _fingerprint(source, node):
    lines = source.splitlines(keepends=True)
    return hashlib.sha256("".join(lines[node.lineno - 1:node.end_lineno]).encode("utf-8")).hexdigest()


def _legacy_nodes(source):
    _require(isinstance(source, str) and len(source.encode("utf-8")) <= MAX_SOURCE_BYTES,
             "legacy_source_too_large")
    _require("\r" not in source and source.endswith("\n"), "unsupported_source_line_endings")
    _require(MARKER not in source, "legacy_source_already_staged")
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        raise UnsupportedLegacySource("legacy_source_invalid_python") from None
    _require(hashlib.sha256(source.encode("utf-8")).hexdigest() in SUPPORTED_SOURCE_SHA256,
             "legacy_source_version_mismatch")
    _require(not any((isinstance(node, ast.Name) and node.id.startswith("_doubao_attachment")) or
                     (isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name.startswith("_doubao_attachment"))
                     for node in ast.walk(tree)), "legacy_adapter_name_collision")
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "RelayHandler"]
    _require(len(classes) == 1, "legacy_handler_missing_or_ambiguous")
    handler = classes[0]
    _require(len(handler.bases) == 1 and isinstance(handler.bases[0], ast.Name) and
             handler.bases[0].id == "BaseHTTPRequestHandler" and not handler.decorator_list and
             not handler.keywords, "legacy_handler_base_mismatch")
    methods = {}
    for name, expected in SUPPORTED_FUNCTIONS.items():
        candidates = [node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == name]
        _require(len(candidates) == 1 and not candidates[0].decorator_list,
                 "legacy_function_missing_or_ambiguous:" + name)
        _require(_fingerprint(source, candidates[0]) == expected, "legacy_function_version_mismatch:" + name)
        methods[name] = candidates[0]
    imports = [node for node in tree.body if isinstance(node, ast.Try) and
               any(isinstance(child, ast.ImportFrom) and child.module == "signal_trial" for child in node.body)]
    _require(len(imports) == 1 and _fingerprint(source, imports[0]) == SIGNAL_IMPORT_SHA256,
             "legacy_signal_import_version_mismatch")
    _require(any(isinstance(node, ast.Import) and any(alias.name == "os" and alias.asname is None
                                                    for alias in node.names) for node in tree.body),
             "legacy_os_import_missing")
    _require(any(isinstance(node, ast.ImportFrom) and node.module == "urllib.parse" and
                 any(alias.name == "urlparse" and alias.asname is None for alias in node.names)
                 for node in tree.body), "legacy_urlparse_import_missing")
    return tree, handler, methods, imports[0]


def stage_source(source):
    """Return a checked candidate; the supplied source string is never executed."""
    tree, _, methods, signal_import = _legacy_nodes(source)
    lines = source.splitlines(keepends=True)
    additions = [(signal_import.end_lineno, MODULE_ADDITIONS)]
    for name, addition in (("do_GET", GET_ADDITION), ("do_POST", POST_ADDITION)):
        method = methods[name]
        # Pinned versions have parsed/path/None guard followed by signal dispatch.
        _require(len(method.body) > 3 and isinstance(method.body[3], ast.If), "legacy_route_anchor_mismatch")
        additions.append((method.body[3].lineno - 1, addition))
    candidate_lines = list(lines)
    for offset, addition in sorted(additions, reverse=True):
        candidate_lines.insert(offset, addition)
    candidate = "".join(candidate_lines)
    # Inverse text proof: all bytes outside the three insertions are unchanged.
    restored = candidate
    for addition in (MODULE_ADDITIONS, GET_ADDITION, POST_ADDITION):
        _require(restored.count(addition) == 1, "legacy_candidate_insertion_ambiguous")
        restored = restored.replace(addition, "", 1)
    _require(restored == source, "legacy_non_attachment_source_changed")
    # Independently peel the added AST nodes and compare the entire original AST.
    original_nodes = len(tree.body)
    modified = ast.parse(candidate)
    module_additions = ast.parse(MODULE_ADDITIONS).body
    start = tree.body.index(signal_import) + 1
    _require([ast.dump(node) for node in modified.body[start:start + len(module_additions)]] ==
             [ast.dump(node) for node in module_additions], "legacy_module_addition_mismatch")
    del modified.body[start:start + len(module_additions)]
    modified_handler = next(node for node in modified.body if isinstance(node, ast.ClassDef) and node.name == "RelayHandler")
    for name in ("do_GET", "do_POST"):
        function = next(node for node in modified_handler.body if isinstance(node, ast.FunctionDef) and node.name == name)
        _require(len(function.body) > 3 and isinstance(function.body[3], ast.If), "legacy_candidate_route_mismatch")
        del function.body[3]
    _require(len(modified.body) == original_nodes and ast.dump(modified, include_attributes=False) ==
             ast.dump(tree, include_attributes=False), "legacy_non_attachment_ast_changed")
    compile(candidate, "<staged-legacy-server>", "exec")
    return candidate


def stage_file(source, output):
    """Exclusively create a separate candidate; never overwrite or modify input."""
    source, output = Path(source), Path(output)
    _require(source.resolve() != output.resolve(), "legacy_in_place_update_forbidden")
    with source.open("rb") as stream:
        data = stream.read(MAX_SOURCE_BYTES + 1)
    _require(len(data) <= MAX_SOURCE_BYTES, "legacy_source_too_large")
    try:
        candidate = stage_source(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise UnsupportedLegacySource("legacy_source_not_utf8") from None
    descriptor = os.open(str(output), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(candidate.encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    return {"ok": True, "source_sha256": hashlib.sha256(data).hexdigest(),
            "candidate_sha256": hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
            "output": str(output), "boundary": "candidate_only_no_deployment_no_legacy_route_changes"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = stage_file(args.source, args.output)
    except (UnsupportedLegacySource, OSError) as exc:
        # Do not include source, file bodies or environment configuration.
        reason = str(exc) if isinstance(exc, UnsupportedLegacySource) else "legacy_candidate_file_error"
        print(json.dumps({"ok": False, "error": reason}))
        raise SystemExit(1)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
