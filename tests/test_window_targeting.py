import json
import unittest
from unittest.mock import patch

from omarchy_ai.execution import actions, browser_jev
from omarchy_ai.execution.actions import ActionResult

# The real window list from the 2026-09-22 bug report, trimmed.
CLIENTS = [
    {"address": "0xa", "class": "foot", "title": "◐ Terminal text output and heartbeat autonomy",
     "workspace": {"id": 1}, "focusHistoryID": 3, "mapped": True, "pid": 10},
    {"address": "0xb", "class": "chromium-browser", "title": "Issues · omacom/omarchy - Chromium",
     "workspace": {"id": 1}, "focusHistoryID": 0, "mapped": True, "pid": 42},
    {"address": "0xc", "class": "foot", "title": "✳ Tello JEV navigation with GPT vision",
     "workspace": {"id": 5}, "focusHistoryID": 2, "mapped": True, "pid": 11},
    {"address": "0xd", "class": "foot", "title": "ben-ami@Jarvis-HQ:~/tello-jev-mission",
     "workspace": {"id": 1}, "focusHistoryID": 1, "mapped": True, "pid": 12},
]


class WindowRankingTests(unittest.TestCase):
    def test_terminal_on_workspace_5_is_chosen_over_a_titled_match_on_workspace_1(self):
        # Regression: "terminal" matched a workspace-1 window whose TITLE had
        # the word, and focusing it moved the user off workspace 5 (twice).
        best = actions._rank_windows(CLIENTS, "terminal", None, 5)[0]
        self.assertEqual(best["address"], "0xc")

    def test_the_focused_window_wins_when_it_matches(self):
        self.assertEqual(actions._rank_windows(CLIENTS, "terminal", "0xa", 1)[0]["address"], "0xa")

    def test_same_workspace_then_most_recent(self):
        self.assertEqual(actions._rank_windows(CLIENTS, "terminal", None, 1)[0]["address"], "0xd")

    def test_focus_window_reports_a_workspace_move(self):
        def run(argv, timeout=None, cwd=None):
            if argv[:2] == ["hyprctl", "clients"]:
                return ActionResult(True, json.dumps(CLIENTS))
            if argv[:2] == ["hyprctl", "activeworkspace"]:
                return ActionResult(True, json.dumps({"id": 1}))
            if argv[:2] == ["hyprctl", "activewindow"]:
                run.calls += 1
                return ActionResult(True, json.dumps({"address": "0xa" if run.calls == 1 else "0xc"}))
            return ActionResult(True)
        run.calls = 0
        with patch.object(actions, "_run", side_effect=run), \
                patch.object(actions, "_hyprctl_dispatch", return_value=ActionResult(True)):
            result = actions.focus_window({"target": "Tello JEV"})
        self.assertTrue(result.ok)
        self.assertIn("workspace 5", result.message)

    def test_focused_means_the_active_window(self):
        def run(argv, timeout=None, cwd=None):
            if argv[:2] == ["hyprctl", "clients"]:
                return ActionResult(True, json.dumps(CLIENTS))
            if argv[:2] == ["hyprctl", "activeworkspace"]:
                return ActionResult(True, json.dumps({"id": 5}))
            if argv[:2] == ["hyprctl", "activewindow"]:
                return ActionResult(True, json.dumps({"address": "0xc"}))
            return ActionResult(True)
        with patch.object(actions, "_run", side_effect=run), \
                patch.object(actions, "_hyprctl_dispatch", return_value=ActionResult(True)) as dispatch:
            self.assertTrue(actions.focus_window({"target": "the focused terminal"}).ok)
        self.assertIn("address:0xc", dispatch.call_args.args[0])


class DedicatedBrowserWindowTests(unittest.TestCase):
    def test_only_the_automation_unit_window_is_moved_never_the_users_browser(self):
        shown = type("R", (), {"stdout": "42\n"})()
        with patch.object(browser_jev.subprocess, "run", return_value=shown):
            self.assertEqual(browser_jev._dedicated_window(CLIENTS)["address"], "0xb")
        other = type("R", (), {"stdout": "999\n"})()
        with patch.object(browser_jev.subprocess, "run", return_value=other):
            self.assertIsNone(browser_jev._dedicated_window(CLIENTS))


class RunningProgramAndJevTests(unittest.TestCase):
    def test_claude_terminal_is_found_by_its_running_program_on_the_current_workspace(self):
        clients = [dict(c) for c in CLIENTS]
        clients[0]["running"] = "claude"   # workspace 1
        clients[2]["running"] = "claude"   # workspace 5
        self.assertEqual(actions._rank_windows(clients, "the claude terminal", None, 5)[0]["address"], "0xc")

    def _jev(self, probabilities):
        choice = max(probabilities, key=probabilities.get)
        answer = {"which": {"choice": choice, "p": probabilities[choice], "probabilities": probabilities}}
        return patch("omarchy_ai.core.jev.Jev.ask", return_value=answer)

    def test_jev_fallback_takes_a_clear_pick(self):
        with self._jev({"none": 0.28, "0": 0.0, "1": 0.01, "2": 0.03, "3": 0.68}):
            self.assertEqual(actions._jev_window(CLIENTS, "my shell in the tello folder", 1)[0]["address"], "0xd")

    def test_jev_fallback_refuses_an_ambiguous_split(self):
        with self._jev({"none": 0.23, "0": 0.0, "1": 0.12, "2": 0.26, "3": 0.39}):
            self.assertEqual(actions._jev_window(CLIENTS, "the drone project terminal", 1), [])

    def test_jev_outage_means_no_match_not_a_guess(self):
        from omarchy_ai.core.jev import JevError
        with patch("omarchy_ai.core.jev.Jev.ask", side_effect=JevError("503")):
            self.assertEqual(actions._jev_window(CLIENTS, "anything", 1), [])


class CommandRankingTests(unittest.TestCase):
    def test_jev_ranks_commands_and_lexical_is_the_fallback(self):
        from omarchy_ai.execution import keybindings
        from omarchy_ai.core.jev import JevError
        bindings = [keybindings.Binding("SUPER SHIFT ALT, M", "Expand window left a little", "d", "a"),
                    keybindings.Binding("SUPER, PRINT", "Color picker", "d", "b")]
        answer = {"which": {"choice": "1", "p": 1.0, "probabilities": {"0": 0.0, "1": 1.0}}}
        with patch.object(keybindings, "_load", return_value=bindings), \
                patch("omarchy_ai.core.jev.Jev.ask", return_value=answer):
            self.assertEqual(keybindings.list_commands("pick a color")[0]["title"], "Color picker")
        with patch.object(keybindings, "_load", return_value=bindings), \
                patch("omarchy_ai.core.jev.Jev.ask", side_effect=JevError("503")):
            self.assertTrue(keybindings.list_commands("color picker"))


class MoveWindowTests(unittest.TestCase):
    def run_move(self, args, moved_to):
        dispatched = []
        state = {"calls": 0}
        def run(argv, timeout=None, cwd=None):
            if argv[:2] == ["hyprctl", "clients"]:
                state["calls"] += 1
                clients = [dict(c) for c in CLIENTS]
                if state["calls"] > 1:
                    for c in clients:
                        if c["address"] == "0xc":
                            c["workspace"] = {"id": moved_to}
                return ActionResult(True, json.dumps(clients))
            if argv[:2] == ["hyprctl", "activewindow"]:
                return ActionResult(True, json.dumps({"address": "0xc"}))
            if argv[:2] == ["hyprctl", "activeworkspace"]:
                return ActionResult(True, json.dumps({"id": 5}))
            return ActionResult(True)
        def dispatch(lua, classic):
            dispatched.append(lua)
            return ActionResult(True)
        with patch.object(actions, "_run", side_effect=run), \
                patch.object(actions, "_hyprctl_dispatch", side_effect=dispatch), \
                patch.object(actions, "_with_programs", side_effect=lambda c: c):
            return actions.move_window_to_workspace(args), dispatched

    def test_focused_window_moves_silently_and_is_verified(self):
        result, dispatched = self.run_move({"number": 4}, moved_to=4)
        self.assertTrue(result.ok, result.message)
        self.assertIn("workspace 4", result.message)
        self.assertEqual(dispatched, ['hl.dsp.window.move({ workspace = 4, window = "address:0xc", follow = false })'])

    def test_unverified_move_is_reported_as_such(self):
        result, _ = self.run_move({"number": 4}, moved_to=5)
        self.assertFalse(result.ok)
        self.assertIn("not verified", result.message)

    def test_bad_numbers_are_refused(self):
        self.assertFalse(actions.move_window_to_workspace({"number": 0}).ok)
        self.assertFalse(actions.move_window_to_workspace({"number": "four"}).ok)

    def test_mouse_gestures_are_not_offered_as_commands(self):
        from omarchy_ai.execution import keybindings
        self.assertFalse(keybindings._runnable(keybindings.Binding("SUPER + LEFT MOUSE BUTTON", "Move window", "lua", "")))
        self.assertTrue(keybindings._runnable(keybindings.Binding("SUPER SHIFT + 4", "Move window to workspace 4", "lua", "")))


if __name__ == "__main__":
    unittest.main()
