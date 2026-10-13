"""Build/install the WorkBuddy native helper; does not start apps or change TCC."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

source = Path(__file__).with_name("workbuddy_paste.swift")
directory = Path.home() / "Library/Application Support/DoubaoInputSync/native"
directory.mkdir(parents=True, exist_ok=True, mode=0o700)
if any(path.is_symlink() for path in [directory, *directory.parents]):
    raise RuntimeError("symlink runtime path refused")

with tempfile.TemporaryDirectory(prefix=".workbuddy-build-", dir=directory) as tmp:
    binary = Path(tmp) / "workbuddy-paste"
    subprocess.run(["/usr/bin/xcrun", "swiftc", "-O", str(source), "-o", str(binary)],
                   check=True, timeout=120)
    subprocess.run([str(binary), "--self-test"], check=True, timeout=10)
    binary.chmod(0o700)
    destination = directory / "workbuddy-paste"
    if destination.is_symlink():
        raise RuntimeError("symlink binary refused")
    if destination.exists():
        shutil.copy2(destination, directory / ("workbuddy-paste.before-" + str(time.time_ns())))
    receipt = {
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "installed_at_epoch": time.time(),
        "path": str(destination),
        "pure_self_test": True,
        "native_permissions_and_workbuddy_acceptance": "pending",
    }
    os.replace(binary, destination)
    (directory / "workbuddy-paste-build.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))
