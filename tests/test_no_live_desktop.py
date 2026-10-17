"""The test-run guard that keeps tests off the user's live desktop (tests/no_live_desktop.py).

Importing the guard here also installs it for ``python -m unittest discover``,
which never reads tests/conftest.py.
"""

import asyncio
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import no_live_desktop
from no_live_desktop import LiveDesktopCall


class NoLiveDesktopTests(unittest.TestCase):
    def test_desktop_binaries_never_run(self):
        for argv in (
            ["hyprctl", "clients", "-j"],
            ["omarchy-shell", "-q", "watchdog", "state"],
            ["/usr/bin/wtype", "x"],
        ):
            with self.subTest(argv=argv), self.assertRaises(LiveDesktopCall):
                subprocess.run(argv, capture_output=True)
        with self.assertRaises(LiveDesktopCall):
            subprocess.check_output("notify-send hello", shell=True)

    def test_async_subprocesses_are_guarded(self):
        async def spawn():
            await asyncio.create_subprocess_exec("adb", "devices")

        with self.assertRaises(LiveDesktopCall):
            asyncio.run(spawn())

    def test_fake_binaries_and_ordinary_commands_still_run(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "hyprctl"
            fake.write_text("#!/bin/sh\necho fake\n")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = {
                **os.environ,
                "PATH": f"{directory}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            self.assertEqual(
                subprocess.check_output(["hyprctl"], env=env, text=True), "fake\n"
            )
            self.assertEqual(subprocess.check_output([str(fake)], text=True), "fake\n")
        self.assertEqual(subprocess.run(["true"]).returncode, 0)

    def test_every_omarchy_command_is_a_desktop_command(self):
        with self.assertRaises(LiveDesktopCall):
            no_live_desktop.check(["omarchy-some-future-command"])

    def test_the_keyring_is_never_read(self):
        from omarchy_ai.execution import sudo_approval

        self.assertIsNone(sudo_approval.retrieve())


if __name__ == "__main__":
    unittest.main()
