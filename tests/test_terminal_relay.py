import json
import unittest
from unittest.mock import patch

from omarchy_ai.config import Config
from omarchy_ai.execution import actions
from omarchy_ai.execution.actions import ActionResult


class RelayInstructionTests(unittest.TestCase):
    def instructions(self, preferences=()):
        from omarchy_ai.voice.live import build_session_config
        with patch("omarchy_ai.voice.live.load_recent_context", return_value=""), \
                patch("omarchy_ai.voice.live.load_preferences", return_value=list(preferences)), \
                patch("omarchy_ai.voice.live.updates.wake_notice", return_value=""):
            return build_session_config(Config())["instructions"]

    def test_blanket_ban_on_typing_requests_into_agents_is_gone(self):
        text = self.instructions()
        self.assertNotIn("Never type the user's conversational request", text)
        self.assertIn("TERMINAL AND AI-AGENT RELAY", text)
        self.assertIn("VERBATIM", text)

    def test_relay_rule_comes_after_and_overrides_learned_preferences(self):
        text = self.instructions(["Never type URLs into a terminal; use the browser address bar instead."])
        self.assertLess(text.index("Never type URLs into a terminal"), text.index("TERMINAL AND AI-AGENT RELAY"))
        self.assertIn("overrides any older rule or learned preference", text)


class TypeTextTests(unittest.TestCase):
    def run_type(self, text, window_class="foot"):
        calls = []
        def fake_run(argv, timeout=None, cwd=None):
            calls.append(argv)
            if argv[:2] == ["hyprctl", "activewindow"]:
                return ActionResult(True, json.dumps({"class": window_class}))
            return ActionResult(True, "")
        with patch.object(actions.shutil, "which", return_value="/usr/bin/x"), \
                patch.object(actions, "_run", side_effect=fake_run), \
                patch.object(actions.subprocess, "run") as copy:
            copy.return_value.returncode = 0
            result = actions.type_text({"text": text})
        return result, calls, copy

    def test_single_line_is_typed_as_is(self):
        result, calls, copy = self.run_type("claude --resume")
        self.assertTrue(result.ok)
        self.assertEqual(calls, [["wtype", "--", "claude --resume"]])
        copy.assert_not_called()

    def test_multi_line_prompt_is_pasted_as_one_block_into_a_terminal(self):
        prompt = "Use the claude_design MCP (https://api.anthropic.com/v1/design/mcp)\n- `Phone Bridge.dc.html`\n"
        result, calls, copy = self.run_type(prompt)
        self.assertTrue(result.ok)
        self.assertEqual(copy.call_args.kwargs["input"], prompt.rstrip("\n"))
        self.assertEqual(calls[-1], ["wtype", "-M", "ctrl", "-M", "shift", "-k", "v", "-m", "shift", "-m", "ctrl"])
        self.assertFalse(any(c[:2] == ["wtype", "--"] for c in calls), "newlines must never be typed as Return")

    def test_multi_line_outside_a_terminal_uses_ctrl_v(self):
        _, calls, _ = self.run_type("a\nb", window_class="chromium")
        self.assertEqual(calls[-1], ["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"])


if __name__ == "__main__":
    unittest.main()
