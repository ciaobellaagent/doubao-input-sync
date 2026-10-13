from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import windows_paste_helper as helper


def payload(archive_id: int, archived_at: str, version: int, text: str = "probe") -> dict:
    archive = {
        "archive_id": archive_id,
        "archived_at": archived_at,
        "version": version,
        "text": text,
    }
    return {
        "room_id": "test-room",
        "version": version,
        "text": text,
        "latest_archive": archive,
    }


def state() -> dict:
    return {
        "last_version": -1,
        "last_archive_id": -1,
        "last_archive_marker": None,
        "applied_updates": 0,
        "startup_seeded": False,
        "curl_resolve": [],
        "pending_acks": {},
    }


class ArchiveIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.args = helper.build_parser().parse_args(
            [
                "--room-id",
                "test-room",
                "--dry-run",
                "--stop-after-updates",
                "1",
            ]
        )
        self.args.client_id = "unit-test"

    def process(self, item: dict, helper_state: dict) -> bool:
        with patch.object(helper, "acknowledge_archive_with_retry", return_value=({"ok": True}, 1)):
            return helper.process_payload(self.args, item, helper_state)

    def test_startup_archive_is_seeded_and_not_delivered(self) -> None:
        helper_state = state()
        first = payload(18, "2026-06-27T08:00:00+00:00", 708)

        self.assertFalse(self.process(first, helper_state))
        self.assertEqual(helper_state["last_archive_id"], 18)
        self.assertEqual(helper_state["applied_updates"], 0)

    def test_same_archive_is_ignored_after_startup(self) -> None:
        helper_state = state()
        first = payload(18, "2026-06-27T08:00:00+00:00", 708)

        self.process(first, helper_state)
        self.assertFalse(self.process(first, helper_state))
        self.assertEqual(helper_state["applied_updates"], 0)

    def test_lower_archive_id_after_relay_restart_is_delivered(self) -> None:
        helper_state = state()
        before_restart = payload(18, "2026-06-27T08:00:00+00:00", 708)
        after_restart = payload(2, "2026-07-20T13:21:03+00:00", 297)

        self.process(before_restart, helper_state)
        self.assertTrue(self.process(after_restart, helper_state))
        self.assertEqual(helper_state["last_archive_id"], 2)
        self.assertEqual(helper_state["applied_updates"], 1)

    def test_legacy_payload_keeps_monotonic_id_guard(self) -> None:
        helper_state = state()
        helper_state["startup_seeded"] = True
        helper_state["last_archive_id"] = 5
        legacy = payload(4, "", 0)

        self.assertFalse(self.process(legacy, helper_state))
        self.assertEqual(helper_state["applied_updates"], 0)


class ForegroundPasteTests(unittest.TestCase):
    def test_paste_copies_then_sends_ctrl_v(self) -> None:
        with (
            patch.object(helper, "copy_to_clipboard") as copy_to_clipboard,
            patch.object(helper.time, "sleep") as sleep,
            patch.object(helper, "run_powershell") as run_powershell,
        ):
            helper.paste_to_active_window("远端信息", 0.2)

        copy_to_clipboard.assert_called_once_with("远端信息")
        sleep.assert_called_once_with(0.2)
        self.assertIn("SendKeys('^v')", run_powershell.call_args.args[0])

    def test_paste_mode_acknowledges_paste_action(self) -> None:
        args = helper.build_parser().parse_args(
            [
                "--room-id",
                "test-room",
                "--mode",
                "paste",
                "--no-skip-existing",
                "--stop-after-updates",
                "1",
            ]
        )
        args.client_id = "unit-test"
        helper_state = state()
        item = payload(1, "2026-07-27T03:20:00+00:00", 1, "远端信息")

        with (
            patch.object(helper, "paste_to_active_window") as paste_to_active_window,
            patch.object(
                helper,
                "acknowledge_archive_with_retry",
                return_value=({"ok": True}, 1),
            ) as acknowledge,
        ):
            should_stop = helper.process_payload(args, item, helper_state)

        self.assertTrue(should_stop)
        paste_to_active_window.assert_called_once_with("远端信息", 0.15)
        self.assertEqual(acknowledge.call_args.args[4], "paste")


if __name__ == "__main__":
    unittest.main()
