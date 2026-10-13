"""Per-user, cross-checkout exclusion for the macOS clipboard helper.

The empty lock file is permanent: never unlink/replace it, including on exit.
The kernel owns the lifetime of the flock, not a PID file or a stale heartbeat.
Process inspection is a migration guard for helpers predating this lock. It is
deliberately conservative and never returns command lines (which may be private).
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
import pwd
import re
import shlex
import stat
import subprocess
import time
from typing import Callable


LOCK_NAME = "clipboard.lock"
_PYTHON_NAME = re.compile(r"python(?:\d+(?:\.\d+)*)?", re.IGNORECASE)


class HelperInstanceError(RuntimeError):
    """Fail-closed reason code; contains no command line or credentials."""


def default_state_dir() -> Path:
    # Do not let a different checkout, HOME, room or server create a second lock:
    # all of these helpers act on the same user's clipboard/frontmost input.
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / "Library/Application Support/DoubaoInputSync/helper-instance"


def _open_state_dir(state_dir: Path | None, *, create: bool) -> int:
    target = default_state_dir() if state_dir is None else Path(state_dir)
    if not target.is_absolute() or ".." in target.parts:
        raise HelperInstanceError("unsafe_lock_directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open("/", flags)
    # Default setup may create the application-specific two levels only. Test
    # fixtures may create their final directory, never arbitrary ancestors.
    first_creatable = len(target.parts) - (2 if state_dir is None else 1)
    try:
        for index, component in enumerate(target.parts[1:], 1):
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                if not create or index < first_creatable:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = child
            if index >= first_creatable:
                info = os.fstat(current)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise HelperInstanceError("unsafe_lock_directory")
        return current
    except BaseException:
        os.close(current)
        raise


def _open_lock(state_dir: Path | None, *, create: bool) -> int:
    directory = _open_state_dir(state_dir, create=create)
    try:
        flags = (os.O_RDWR | os.O_CREAT) if create else os.O_RDONLY
        descriptor = os.open(LOCK_NAME, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    finally:
        os.close(directory)
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise HelperInstanceError("unsafe_lock_file")
    os.set_inheritable(descriptor, False)
    return descriptor


def _helper_command(command: str, executable: str) -> bool | None:
    """True=real helper, False=other process/dry-run, None=ambiguous helper.

ps is not an argv API: paths containing spaces may lose their quoting. Such a
helper candidate is unknown, never evidence that no helper exists.
"""
    try:
        argv = shlex.split(command)
    except ValueError:
        return None if "mac_paste_helper.py" in command else False
    if not argv:
        return None
    # Full executable paths with spaces are common for virtual environments.
    if command.startswith(executable + " "):
        try:
            argv = [executable] + shlex.split(command[len(executable) + 1:])
        except ValueError:
            return None if "mac_paste_helper.py" in command else False
    if not _PYTHON_NAME.fullmatch(Path(argv[0]).name):
        return None if "mac_paste_helper.py" in command else False
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg in {"-c", "-m"}:
            return False  # A string/module mention is not the helper script.
        if arg == "--":
            index += 1
            break
        if arg in {"-W", "-X"}:
            index += 2
            continue
        if arg.startswith("-"):
            if arg in {"-u", "-B", "-E", "-I", "-O", "-OO", "-q", "-s", "-S", "-v", "-V", "-x"} or arg.startswith(("-W", "-X")):
                index += 1
                continue
            return None if "mac_paste_helper.py" in command else False
        break
    if index >= len(argv):
        return False
    if Path(argv[index]).name != "mac_paste_helper.py":
        # The script argument may itself be a path with unquoted spaces.
        return None if "mac_paste_helper.py" in command else False
    return "--dry-run" not in argv[index + 1:]


def probe_processes(*, exclude_pid: int | None = None, timeout_seconds: float = 3.0) -> dict:
    """Read-only same-user Python/script probe, excluding shell/rg mentions."""
    pids: list[int] = []
    unknown = False
    deadline = time.monotonic() + timeout_seconds
    try:
        rows = subprocess.run(
            ["/bin/ps", "-axo", "pid=,uid=,comm="], capture_output=True,
            text=True, timeout=timeout_seconds, check=False,
        )
        if rows.returncode or not rows.stdout.strip():
            raise HelperInstanceError("process_probe_failed")
        for row in rows.stdout.splitlines():
            fields = row.strip().split(None, 2)
            if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
                unknown = True
                continue
            pid, uid, executable = int(fields[0]), int(fields[1]), fields[2]
            if uid != os.getuid() or pid == exclude_pid or not _PYTHON_NAME.fullmatch(Path(executable).name):
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                unknown = True
                break
            args = subprocess.run(
                ["/bin/ps", "-ww", "-p", str(pid), "-o", "args="],
                capture_output=True, text=True, timeout=remaining, check=False,
            )
            if args.returncode or not args.stdout.strip():
                # Process disappearance is expected; any other inability to
                # inspect a live candidate is not proof of absence.
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                unknown = True
                continue
            match = _helper_command(args.stdout.strip(), executable)
            if match is None:
                unknown = True
            elif match:
                pids.append(pid)
    except (OSError, subprocess.SubprocessError, UnicodeError, HelperInstanceError):
        unknown = True
    state = "unknown" if unknown else "none" if not pids else "single" if len(pids) == 1 else "multiple"
    return {"process_state": state, "pids": sorted(set(pids)), "reason": "process_probe_unknown" if unknown else "process_probe_complete"}


def probe_instances(*, state_dir: Path | None = None, exclude_pid: int | None = None) -> dict:
    """Observe process and kernel-lock state without creating/writing anything.

This is a point-in-time readiness gate, not a reservation. Only acquire() can
authorize the helper to work. Even lock-free + one legacy PID means do not start.
"""
    result = probe_processes(exclude_pid=exclude_pid)
    descriptor = None
    try:
        descriptor = _open_lock(state_dir, create=False)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result["lock_state"] = "held"
        else:
            result["lock_state"] = "free"
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    except FileNotFoundError:
        result["lock_state"] = "absent"
    except (OSError, HelperInstanceError):
        result["lock_state"] = "unknown"
        result["reason"] = "lock_probe_unknown"
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return result


class HelperInstanceLock:
    """Nonblocking global clipboard lease, held through the full helper loop."""

    def __init__(self, state_dir: Path | None = None, *, process_probe: Callable[..., dict] | None = None):
        self.state_dir = state_dir
        self.process_probe = process_probe or probe_processes
        self.descriptor: int | None = None

    def acquire(self) -> "HelperInstanceLock":
        if self.descriptor is not None:
            raise HelperInstanceError("lock_already_acquired")
        try:
            descriptor = _open_lock(self.state_dir, create=True)
        except (OSError, HelperInstanceError) as exc:
            raise HelperInstanceError("unsafe_or_unavailable_lock") from exc
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EAGAIN, errno.EACCES}:
                    raise HelperInstanceError("helper_instance_lock_held") from exc
                raise HelperInstanceError("helper_instance_lock_failed") from exc
            observed = self.process_probe(exclude_pid=os.getpid())
            # A modern contender may be visible to ps just before it loses the
            # flock and exits. Keep our lock held while giving only known live
            # candidates a short opportunity to disappear. No signal is sent;
            # persistent legacy helpers and any unknown result still fail closed.
            settle_deadline = time.monotonic() + 0.5
            for delay in (0.05, 0.1, 0.2, 0.1):
                if observed.get("process_state") not in {"single", "multiple"}:
                    break
                remaining = settle_deadline - time.monotonic()
                if remaining <= delay:
                    break
                time.sleep(delay)
                remaining = settle_deadline - time.monotonic()
                if remaining <= 0:
                    break
                observed = self.process_probe(exclude_pid=os.getpid(), timeout_seconds=remaining)
            if observed.get("process_state") != "none" or observed.get("pids"):
                raise HelperInstanceError("helper_process_exists_or_unknown")
            self.descriptor = descriptor
            return self
        except BaseException:
            os.close(descriptor)
            raise

    def close(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None

    def __enter__(self) -> "HelperInstanceLock":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.close()
