import os
import shutil
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from omarchy_ai.execution import actions


class RunTests(unittest.TestCase):
    """2026-10-15 19:58: omarchy-toggle-nightlight started hyprsunset in the
    background, which kept the output pipes open, and the night light came
    on while she reported "omarchy-toggle-nightlight timed out" 10s later."""

    def script(self, body: str) -> str:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = Path(tmp) / "starts-background"
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def run_script(self, body: str, timeout: float = 10):
        with patch.object(actions.base, "_desktop_env", return_value=dict(os.environ)):
            started = time.monotonic()
            result = actions._run([self.script(body)], timeout=timeout)
            return result, time.monotonic() - started

    def test_returns_when_the_script_exits_although_its_child_keeps_running(self):
        result, elapsed = self.run_script("sleep 30 &\necho started\n")
        self.assertTrue(result.ok)
        self.assertEqual(result.message, "started")
        self.assertLess(elapsed, 3)

    def test_failure_reports_the_first_error_line(self):
        result, _ = self.run_script("sleep 30 &\necho 'no hyprsunset' >&2\necho second >&2\nexit 2\n")
        self.assertFalse(result.ok)
        self.assertEqual(result.message, "no hyprsunset")

    def test_a_real_timeout_and_a_missing_program(self):
        path = self.script("sleep 30\n")
        with patch.object(actions.base, "_desktop_env", return_value=dict(os.environ)):
            started = time.monotonic()
            result = actions._run([path], timeout=0.5)
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual((result.ok, result.message), (False, f"{path} timed out"))
            result = actions._run(["no-such-program-for-omarchy-ai-tests"])
        self.assertEqual((result.ok, result.message), (False, "no-such-program-for-omarchy-ai-tests is not installed"))


if __name__ == "__main__":
    unittest.main()
