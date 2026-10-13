#!/usr/bin/env python3
"""Conservative release hygiene check. Reports locations/rules, never values.

Scans the Git index and optionally all reachable history. This is not a complete
secret detector: manual review and an independent credential scan remain needed.
Use --denylist-file for private deployment identifiers kept OUTSIDE this repo.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys

FORBIDDEN_DIRS = {"runs", "__sys", "attachments", "artifacts", "backups", "node_modules", ".venv"}
FORBIDDEN_FILES = {"AGENTS.md", "CLAUDE.md", "helper.env", ".DS_Store", "workbuddy-paste", "workbuddy-paste-build.json"}
FILE_PATTERN = re.compile(r"(?:\.(?:db|sqlite\d?)(?:-.*)?|\.(?:pem|key|p12|pfx|pub|log|har|pcap|bundle|bak|backup|env))$", re.I)
PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    "ssh-public-identity": re.compile(r"ssh-(?:rsa|ed25519)\s+[A-Za-z0-9+/=]{32,}"),
    "credential-token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{32,})"),
    "personal-home-path": re.compile(r"(?:/Users/|/home/)[A-Za-z0-9_.-]+/|[A-Z]:\\Users\\[A-Za-z0-9_.-]+\\"),
    "private-network-name": re.compile(r"\b[\w.-]+\.(?:ts\.net|local)\b", re.I),
    "embedded-url-credential": re.compile(r"https?://[^\s/'\"]+:[^\s/'\"]+@", re.I),
}
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
EXAMPLE_NETS = tuple(ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))


def inspect_content(name: str, data: bytes, denylist=()):
    findings = []
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return [{"file": name, "rule": "binary-needs-review"}]
    for number, line in enumerate(content.splitlines(), 1):
        rules = {rule for rule, pattern in PATTERNS.items() if pattern.search(line)}
        for match in IPV4.finditer(line):
            try:
                address = ipaddress.ip_address(match.group())
            except ValueError:
                continue
            if not (address.is_loopback or address.is_unspecified or any(address in net for net in EXAMPLE_NETS)):
                rules.add("non-example-ip")
        if any(term.casefold() in line.casefold() for term in denylist):
            rules.add("private-denylist")
        findings.extend({"file": name, "line": number, "rule": rule} for rule in sorted(rules))
    return findings


def forbidden_path(name):
    path = Path(name)
    return (bool(set(path.parts) & FORBIDDEN_DIRS) or path.name in FORBIDDEN_FILES
            or bool(FILE_PATTERN.search(path.name))
            or (path.name.startswith(".env") and path.name != ".env.example")
            or path.name.startswith(("id_rsa", "id_ed25519")))


def git(*args):
    return subprocess.check_output(["git", *args])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--denylist-file", type=Path)
    args = parser.parse_args()
    denylist = [] if args.denylist_file is None else [s.strip() for s in args.denylist_file.read_text().splitlines() if s.strip() and not s.startswith("#")]
    failures, seen = [], set()

    def inspect_tree(records):
        for record in records.split(b"\0"):
            if not record:
                continue
            meta, name_bytes = record.split(b"\t", 1)
            mode, kind, oid = meta.decode().split()
            name = name_bytes.decode()
            if forbidden_path(name):
                failures.append({"file": name, "rule": "private-or-runtime-path"})
            if mode not in {"100644", "100755"} or kind != "blob":
                failures.append({"file": name, "rule": "symlink-or-submodule-needs-review"})
                continue
            if oid not in seen:
                seen.add(oid)
                failures.extend(inspect_content(name, git("cat-file", "blob", oid), denylist))

    # Inspect staged contents, so an ignored but force-added file cannot escape.
    index_tree = git("write-tree").decode().strip()
    inspect_tree(git("ls-tree", "-rz", index_tree))
    commits = git("rev-list", "--all").decode().splitlines() if args.history else []
    for commit in commits:
        inspect_tree(git("ls-tree", "-rz", commit))
        metadata = git("show", "-s", "--format=%an%n%ae%n%cn%n%ce%n%B", commit)
        failures.extend(inspect_content("commit:" + commit, metadata, denylist))
        lines = metadata.decode().splitlines()
        for email in (lines[1], lines[3]):
            if not email.endswith("@users.noreply.github.com"):
                failures.append({"file": "commit:" + commit, "rule": "non-public-git-email"})
    print(json.dumps({"ok": not failures, "blobs_checked": len(seen), "commits_checked": len(commits), "findings": failures}, indent=2))
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())
