import subprocess
import unittest
from unittest.mock import patch

from omarchy_ai.cli import settings


class RestartTests(unittest.TestCase):
    # Real regression (STATUS.md, 2026-09-22): cmd_restart originally
    # refused to restart mid-conversation (c42af67) and a later unrelated
    # cleanup (a3cd8c7) accidentally dropped that check, so "Apply saved
    # changes" could force-restart the daemon out from under a live
    # session. These pin the restored behavior so it can't silently regress
    # again the same way.

    def test_refuses_to_restart_a_busy_conversation_without_touching_systemctl(self):
        with patch.object(settings, "_conversation_busy", return_value=(True, "a conversation appears to be in progress")), \
                patch.object(settings.subprocess, "run") as run:
            result = settings.cmd_restart(None)
        run.assert_not_called()
        self.assertFalse(result["restarted"])
        self.assertEqual(result["reason"], "a conversation appears to be in progress")

    def test_restarts_when_not_busy(self):
        with patch.object(settings, "_conversation_busy", return_value=(False, "no conversation currently in progress")), \
                patch.object(settings.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            result = settings.cmd_restart(None)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["systemctl", "--user", "restart", settings.SERVICE])
        self.assertTrue(result["restarted"])

    def test_reports_the_real_systemctl_failure_reason(self):
        with patch.object(settings, "_conversation_busy", return_value=(False, "no conversation currently in progress")), \
                patch.object(settings.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 1, "", "start request repeated too quickly")):
            result = settings.cmd_restart(None)
        self.assertFalse(result["restarted"])
        self.assertIn("too quickly", result["reason"])


if __name__ == "__main__":
    unittest.main()
