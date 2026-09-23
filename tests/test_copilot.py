import unittest
from unittest.mock import patch

from omarchy_ai.execution import actions, operator, workbench
from omarchy_ai.execution.actions import ActionResult


class OperatorTests(unittest.TestCase):
    def test_visibility_rule(self):
        # The user's rule: while she is the only operator, show her work.
        with patch.object(operator, "user_active", return_value=False):
            self.assertTrue(operator.visible("auto"))
        with patch.object(operator, "user_active", return_value=True):
            self.assertFalse(operator.visible("auto"))
            self.assertTrue(operator.visible("yes"))      # the user asked to watch
        with patch.object(operator, "user_active", return_value=None):
            self.assertTrue(operator.visible("auto"))     # unknown: old behaviour
        self.assertFalse(operator.visible("no"))

    def test_quiet_flag_is_never_used_because_it_hides_the_answer(self):
        # Real bug: `omarchy-shell -q` suppresses all output, so the answer
        # was always empty and co-pilot mode could never see the user.
        with patch.object(operator.subprocess, "run") as run:
            run.return_value.returncode, run.return_value.stdout = 0, "idle\n"
            operator._cache = (0.0, None)
            self.assertFalse(operator.user_active())
        self.assertNotIn("-q", run.call_args.args[0])


class TerminalTaskTests(unittest.TestCase):
    def run_task(self, active, show="auto"):
        workbench.pending_handover.clear()
        with patch.object(operator, "user_active", return_value=active), \
                patch.object(workbench, "start", return_value=ActionResult(True, "started")), \
                patch.object(workbench, "show", return_value=ActionResult(True, "on screen")) as shown, \
                patch.object(workbench, "send", return_value=ActionResult(True, "typed")) as sent:
            result = actions.terminal_task({"command": "yay -S claude-code", "name": "install", "show": show})
        return result, shown, sent

    def test_only_operator_sees_the_work(self):
        result, shown, sent = self.run_task(active=False)
        shown.assert_called_once_with("install")
        sent.assert_called_once_with("install", "yay -S claude-code")
        self.assertIn("on the user's screen", result.message)

    def test_busy_user_is_not_interrupted_and_gets_a_handover_later(self):
        result, shown, _ = self.run_task(active=True)
        shown.assert_not_called()
        self.assertIn("install", workbench.pending_handover)
        self.assertIn("background", result.message)

    def test_explicit_background_is_never_handed_over(self):
        self.run_task(active=True, show="no")
        self.assertEqual(workbench.pending_handover, set())


class WorkbenchTests(unittest.TestCase):
    def test_password_goes_through_stdin_never_argv(self):
        calls = []
        def tmux(*args, input_text=None, timeout=5):
            calls.append((args, input_text))
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch.object(workbench, "_tmux", side_effect=tmux):
            workbench.submit_password("install", "s3cret")
        self.assertTrue(all("s3cret" not in " ".join(args) for args, _ in calls))
        self.assertIn(("load-buffer", "-b", "oai-secret", "-"), [a for a, _ in calls])
        self.assertEqual([i for _, i in calls if i], ["s3cret"])
        self.assertIn("-d", next(a for a, _ in calls if a[0] == "paste-buffer"))  # buffer deleted

    def test_multiline_text_is_sent_literally_line_by_line(self):
        calls = []
        def tmux(*args, input_text=None, timeout=5):
            calls.append(args)
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch.object(workbench, "_tmux", side_effect=tmux):
            workbench.send("t", "echo a\necho b")
        keys = [a[3:] for a in calls if a[0] == "send-keys"]
        self.assertEqual(keys, [("-l", "--", "echo a"), ("Enter",), ("-l", "--", "echo b"), ("Enter",)])


if __name__ == "__main__":
    unittest.main()
