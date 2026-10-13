"""Offline safety tests; subprocess fixtures never import/run a live helper."""

import io
import json
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import helper_instance
import mac_paste_helper


def no_processes(**unused):
    return {"process_state": "none", "pids": []}


class SingletonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="doubao-singleton-test-")
        # /tmp is a macOS symlink. Fixture explicitly opts into its real location;
        # the implementation itself never resolves or follows caller symlinks.
        self.root = Path(self.temp.name).resolve()
        self.state_dir = self.root / "private-state"

    def tearDown(self):
        self.temp.cleanup()

    def lease(self, **kwargs):
        return helper_instance.HelperInstanceLock(self.state_dir, process_probe=no_processes, **kwargs)

    def test_read_only_probe_does_not_create_missing_directory(self):
        with patch.object(helper_instance, "probe_processes", return_value=no_processes()):
            result = helper_instance.probe_instances(state_dir=self.state_dir)
        self.assertEqual(result["lock_state"], "absent")
        self.assertFalse(self.state_dir.exists())

    def test_lock_permissions_noninheritance_and_permanent_inode(self):
        with self.lease() as first:
            lock_path = self.state_dir / helper_instance.LOCK_NAME
            inode = lock_path.stat().st_ino
            self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
            self.assertFalse(os.get_inheritable(first.descriptor))
            with self.assertRaisesRegex(helper_instance.HelperInstanceError, "lock_held"):
                self.lease().acquire()
            with patch.object(helper_instance, "probe_processes", return_value=no_processes()):
                self.assertEqual(helper_instance.probe_instances(state_dir=self.state_dir)["lock_state"], "held")
        self.assertTrue(lock_path.exists())
        with self.lease():
            self.assertEqual(lock_path.stat().st_ino, inode)

    def test_global_identity_ignores_home_and_checkout(self):
        before = helper_instance.default_state_dir()
        with patch.dict(os.environ, {"HOME": str(self.root), "ROOM_ID": "other", "SERVER_URL": "https://elsewhere.invalid"}):
            self.assertEqual(helper_instance.default_state_dir(), before)
        self.assertNotIn(str(SCRIPTS), str(before))

    def test_legacy_or_unknown_process_refused_and_lock_released(self):
        for state, pids in [("single", [123]), ("multiple", [123, 456]), ("unknown", []), ("none", [123])]:
            with self.subTest(state=state, pids=pids):
                probe = lambda **unused: {"process_state": state, "pids": pids}
                with self.assertRaisesRegex(helper_instance.HelperInstanceError, "process_exists_or_unknown"):
                    helper_instance.HelperInstanceLock(self.state_dir, process_probe=probe).acquire()
                with self.lease():
                    pass

    def test_transient_contenders_can_disappear_while_lock_remains_held(self):
        for states in (["single", "none"], ["multiple", "single", "none"]):
            with self.subTest(states=states):
                observations = iter(states)
                def probe(**unused):
                    # No contender can obtain the lock during the grace period.
                    with self.assertRaisesRegex(helper_instance.HelperInstanceError, "lock_held"):
                        self.lease().acquire()
                    state = next(observations)
                    return {"process_state": state, "pids": [] if state == "none" else [123]}
                with patch.object(helper_instance.time, "sleep") as sleep:
                    with helper_instance.HelperInstanceLock(self.state_dir, process_probe=probe):
                        pass
                self.assertEqual(sleep.call_count, len(states) - 1)

    def test_persistent_legacy_has_finite_grace_and_is_never_accepted(self):
        probe = Mock(return_value={"process_state": "single", "pids": [123]})
        with patch.object(helper_instance.time, "sleep") as sleep:
            with self.assertRaisesRegex(helper_instance.HelperInstanceError, "process_exists_or_unknown"):
                helper_instance.HelperInstanceLock(self.state_dir, process_probe=probe).acquire()
        self.assertEqual(probe.call_count, 5)
        self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), 0.5)
        for call in probe.call_args_list[1:]:
            self.assertGreater(call.kwargs["timeout_seconds"], 0)
            self.assertLessEqual(call.kwargs["timeout_seconds"], 0.5)
        with self.lease():
            pass

    def test_unknown_never_waits_or_retries(self):
        probe = Mock(return_value={"process_state": "unknown", "pids": []})
        with patch.object(helper_instance.time, "sleep") as sleep:
            with self.assertRaises(helper_instance.HelperInstanceError):
                helper_instance.HelperInstanceLock(self.state_dir, process_probe=probe).acquire()
        probe.assert_called_once()
        sleep.assert_not_called()

    def test_symlink_directory_file_and_hardlinks_rejected(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        self.state_dir.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(helper_instance.HelperInstanceError):
            self.lease().acquire()
        self.state_dir.unlink()
        self.state_dir.mkdir(mode=0o700)
        target = self.root / "untouched"
        target.write_text("unchanged")
        target.chmod(0o600)
        lock_path = self.state_dir / helper_instance.LOCK_NAME
        lock_path.symlink_to(target)
        with self.assertRaises(helper_instance.HelperInstanceError):
            self.lease().acquire()
        self.assertEqual(target.read_text(), "unchanged")
        lock_path.unlink()
        os.link(target, lock_path)
        with self.assertRaises(helper_instance.HelperInstanceError):
            self.lease().acquire()
        self.assertEqual(target.read_text(), "unchanged")

    def test_unsafe_mode_is_not_silently_changed(self):
        self.state_dir.mkdir(mode=0o755)
        self.state_dir.chmod(0o755)
        with self.assertRaises(helper_instance.HelperInstanceError):
            self.lease().acquire()
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o755)

    def test_unsafe_lock_file_mode_and_symlink_probe_are_unknown(self):
        self.state_dir.mkdir(mode=0o700)
        lock_path = self.state_dir / helper_instance.LOCK_NAME
        lock_path.write_text("")
        lock_path.chmod(0o644)
        with self.assertRaises(helper_instance.HelperInstanceError):
            self.lease().acquire()
        with patch.object(helper_instance, "probe_processes", return_value=no_processes()):
            self.assertEqual(helper_instance.probe_instances(state_dir=self.state_dir)["lock_state"], "unknown")
        lock_path.unlink()
        lock_path.symlink_to(self.root / "missing-target")
        with patch.object(helper_instance, "probe_processes", return_value=no_processes()):
            self.assertEqual(helper_instance.probe_instances(state_dir=self.state_dir)["lock_state"], "unknown")

    @staticmethod
    def read_line(proc):
        ready, _, _ = select.select([proc.stdout], [], [], 10)
        if not ready:
            raise AssertionError("offline lock fixture timed out")
        line = proc.stdout.readline().strip()
        if not line:
            raise AssertionError("offline lock fixture exited without status")
        return line

    def start_lock_fixture(self):
        code = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from helper_instance import HelperInstanceLock, HelperInstanceError
print('ready', flush=True)
sys.stdin.read(1)
try:
    with HelperInstanceLock(Path(sys.argv[2]), process_probe=lambda **kw: {'process_state':'none','pids':[]}):
        print('acquired', flush=True)
        sys.stdin.read(1)
except HelperInstanceError:
    print('refused', flush=True)
"""
        return subprocess.Popen(
            [sys.executable, "-u", "-c", code, str(SCRIPTS), str(self.state_dir)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_concurrent_subprocesses_one_winner_and_crash_releases(self):
        processes = [self.start_lock_fixture() for _ in range(4)]
        try:
            for process in processes:
                self.assertEqual(self.read_line(process), "ready")
            for process in processes:
                process.stdin.write("x")
                process.stdin.flush()
            states = [self.read_line(process) for process in processes]
            self.assertEqual(states.count("acquired"), 1)
            self.assertEqual(states.count("refused"), 3)
            winner = processes[states.index("acquired")]
            inode = (self.state_dir / helper_instance.LOCK_NAME).stat().st_ino
            winner.kill()  # Kills only our isolated lock-holder fixture.
            winner.wait(timeout=3)
            with self.lease():
                self.assertEqual((self.state_dir / helper_instance.LOCK_NAME).stat().st_ino, inode)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=3)
                process.stdin.close()
                process.stdout.close()
                process.stderr.close()

    def test_actual_legacy_subprocess_is_detected_without_touching_it(self):
        # Filename-only fixture, not the production code and not a network client.
        legacy_script = self.root / "mac_paste_helper.py"
        legacy_script.write_text("import time\nprint('ready', flush=True)\ntime.sleep(30)\n")
        process = subprocess.Popen(
            [sys.executable, "-u", str(legacy_script)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        real_run = subprocess.run
        def fixture_ps(command, **kwargs):
            if command == ["/bin/ps", "-axo", "pid=,uid=,comm="]:
                return subprocess.CompletedProcess(command, 0, f"{process.pid} {os.getuid()} {sys.executable}\n", "")
            return real_run(command, **kwargs)
        try:
            self.assertEqual(self.read_line(process), "ready")
            with patch.object(helper_instance.subprocess, "run", side_effect=fixture_ps):
                observed = helper_instance.probe_processes()
                self.assertEqual(observed["process_state"], "single")
                self.assertEqual(observed["pids"], [process.pid])
                with self.assertRaises(helper_instance.HelperInstanceError):
                    helper_instance.HelperInstanceLock(self.state_dir).acquire()
            self.assertIsNone(process.poll())
        finally:
            process.terminate()
            process.wait(timeout=3)
            process.stdout.close()
            process.stderr.close()


class ProcessProbeTests(unittest.TestCase):
    def test_exact_script_and_python_flags(self):
        matching = [
            "/usr/bin/python3 scripts/mac_paste_helper.py --mode paste",
            "/usr/bin/python3 -u /other-checkout/scripts/mac_paste_helper.py --room-id other",
            "/usr/bin/python3 -W ignore -X utf8 -- scripts/mac_paste_helper.py",
        ]
        for command in matching:
            with self.subTest(command=command):
                self.assertTrue(helper_instance._helper_command(command, "/usr/bin/python3"))

    def test_non_helper_mentions_do_not_match(self):
        for command in [
            "/usr/bin/python3 -c 'print(\"mac_paste_helper.py\")'",
            "/usr/bin/python3 -m unittest tests.test_mac_helper_singleton",
            "/usr/bin/python3 scripts/mac_paste_helper.py --dry-run",
            "/usr/bin/python3 scripts/another_script.py",
        ]:
            with self.subTest(command=command):
                self.assertFalse(helper_instance._helper_command(command, "/usr/bin/python3"))

    def test_unquoted_script_path_is_unknown(self):
        self.assertIsNone(helper_instance._helper_command(
            "/usr/bin/python3 /path with spaces/mac_paste_helper.py --mode paste", "/usr/bin/python3",
        ))

    def test_ps_failure_is_unknown_and_never_leaks_stderr(self):
        with patch.object(helper_instance.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "secret text")):
            result = helper_instance.probe_processes()
        self.assertEqual(result["process_state"], "unknown")
        self.assertNotIn("secret text", json.dumps(result))

    def test_multiple_processes_and_exclude_self(self):
        rows = f"101 {os.getuid()} /usr/bin/python3\n102 {os.getuid()} /usr/bin/python3\n"
        def fake_ps(command, **kwargs):
            content = rows if "-axo" in command else "/usr/bin/python3 scripts/mac_paste_helper.py --mode paste\n"
            return subprocess.CompletedProcess(command, 0, content, "")
        with patch.object(helper_instance.subprocess, "run", side_effect=fake_ps):
            result = helper_instance.probe_processes()
            excluded = helper_instance.probe_processes(exclude_pid=101)
        self.assertEqual(result["process_state"], "multiple")
        self.assertEqual(result["pids"], [101, 102])
        self.assertEqual(excluded["process_state"], "single")
        self.assertEqual(excluded["pids"], [102])

    def test_shell_and_rg_mentions_excluded_before_argv_read(self):
        rows = f"10 {os.getuid()} /bin/zsh\n11 {os.getuid()} /opt/homebrew/bin/rg\n"
        with patch.object(helper_instance.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, rows, "")) as run:
            result = helper_instance.probe_processes()
        self.assertEqual(result["process_state"], "none")
        self.assertEqual(run.call_count, 1)


class HelperEntryTests(unittest.TestCase):
    def test_all_real_modes_refuse_before_network_or_clipboard(self):
        for mode in ("clipboard", "paste"):
            for transport in ("stream", "poll"):
                with self.subTest(mode=mode, transport=transport):
                    with patch.object(sys, "argv", ["helper", "--mode", mode, "--transport", transport]), \
                         patch.object(helper_instance, "HelperInstanceLock", side_effect=helper_instance.HelperInstanceError("test_refused")), \
                         patch.object(mac_paste_helper, "run_helper") as run, \
                         patch.object(mac_paste_helper, "fetch_json") as fetch, \
                         patch.object(mac_paste_helper, "paste_to_active_app") as paste, \
                         patch.object(mac_paste_helper, "copy_to_clipboard") as copy, \
                         patch("sys.stdout", new_callable=io.StringIO):
                        self.assertEqual(mac_paste_helper.main(), 73)
                        run.assert_not_called()
                        fetch.assert_not_called()
                        paste.assert_not_called()
                        copy.assert_not_called()

    def test_dry_run_remains_cross_platform_and_does_not_acquire_lock(self):
        with patch.object(sys, "argv", ["helper", "--dry-run"]), \
             patch.object(helper_instance, "HelperInstanceLock") as lock, \
             patch.object(mac_paste_helper, "run_helper", return_value=0) as run:
            self.assertEqual(mac_paste_helper.main(), 0)
        lock.assert_not_called()
        self.assertTrue(run.call_args.args[0].dry_run)

    def test_skip_existing_still_skips_archived_input(self):
        args = mac_paste_helper.build_parser().parse_args([])
        self.assertTrue(args.skip_existing)
        state = {"last_version": -1, "last_archive_id": -1, "startup_seeded": False}
        with patch.object(mac_paste_helper, "copy_to_clipboard") as copy, \
             patch.object(mac_paste_helper, "acknowledge_archive_with_retry") as ack:
            self.assertFalse(mac_paste_helper.process_payload(
                args, {"version": 10, "latest_archive": {"archive_id": 9, "text": "old"}}, state,
            ))
        copy.assert_not_called()
        ack.assert_not_called()
        self.assertEqual(state["last_archive_id"], 9)


if __name__ == "__main__":
    unittest.main()
