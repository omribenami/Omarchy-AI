import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.phone import presence


class PhonePresenceTests(unittest.TestCase):
    """2026-09-27: lid closed, idle lock at 08:50:07, panel off -> blank phone mirror."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.calls = []
        self.user_stay_awake = False

        def run(*argv):
            self.calls.append(argv[3])
            on = self.user_stay_awake or (argv[3] != "allow-idle" and presence._OWNED.exists())
            return SimpleNamespace(stdout='{"enabled":%s}' % ("true" if on else "false"))
        self.patches = [
            patch.object(presence, "_OWNED", Path(self.dir.name) / "phone-stay-awake"),
            patch.object(presence, "_run", side_effect=run),
            patch("omarchy_ai.execution.actions._hyprctl_dispatch", return_value=ActionResult(True, "ok")),
        ]
        self.dpms = [p.start() for p in self.patches][2]
        presence._last_seen, presence._holding = 0.0, False

    def tearDown(self):
        for p in self.patches:
            p.stop()
        presence._last_seen, presence._holding = 0.0, False
        self.dir.cleanup()

    def test_a_connected_phone_holds_off_the_lock_and_wakes_the_panel(self):
        presence._last_seen = presence.time.monotonic()
        presence._update()
        self.assertEqual(self.calls, ["status", "stay-awake"])
        self.assertTrue(presence._OWNED.exists())
        self.dpms.assert_called_once()

    def test_idle_is_allowed_again_when_the_phone_goes_quiet(self):
        presence._last_seen = presence.time.monotonic()
        presence._update()
        presence._last_seen -= presence.CONNECTED_SECONDS + 1
        presence._update()
        self.assertEqual(self.calls[-1], "allow-idle")
        self.assertFalse(presence._OWNED.exists())

    def test_the_users_own_stay_awake_is_left_alone(self):
        self.user_stay_awake = True
        presence._last_seen = presence.time.monotonic()
        presence._update()
        presence._last_seen -= presence.CONNECTED_SECONDS + 1
        presence._update()
        self.assertEqual(self.calls, ["status"])

    def test_a_hold_left_by_a_crashed_run_is_released_at_start(self):
        presence._OWNED.touch()
        presence._update()
        self.assertEqual(self.calls, ["allow-idle"])

    def test_requests_do_nothing_until_the_daemon_starts_presence(self):
        with patch.object(presence, "_thread", None):
            presence.seen()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
