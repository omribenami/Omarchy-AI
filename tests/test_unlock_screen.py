import unittest
from types import SimpleNamespace
from unittest.mock import patch

from omarchy_ai.execution import actions
from omarchy_ai.execution.actions import ActionResult


class UnlockScreenTests(unittest.TestCase):
    """2026-09-27 09:05: asked from the phone to unlock, she ran lock_screen twice."""

    def unlock(self, lock_states, password="hunter2-secret", enabled=True):
        states = iter(lock_states)
        runs = []

        def run(argv, **kwargs):
            runs.append((argv, kwargs.get("input")))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(actions, "_screen_locked", side_effect=lambda: next(states)), \
                patch.object(actions.shutil, "which", return_value="/usr/bin/wtype"), \
                patch.object(actions, "load_config", return_value=SimpleNamespace(sudo_access_enabled=enabled)), \
                patch.object(actions.sudo_approval, "retrieve", return_value=password), \
                patch.object(actions, "_hyprctl_dispatch", return_value=ActionResult(True, "ok")), \
                patch.object(actions.time, "sleep"), \
                patch.object(actions.subprocess, "run", side_effect=run):
            return actions.unlock_screen_for_paired_phone(), runs

    def test_the_room_microphone_path_refuses(self):
        result = actions.run_action("unlock_screen", {})
        self.assertFalse(result.ok)
        self.assertIn("paired phone", result.message)

    def test_types_the_password_on_stdin_and_verifies(self):
        result, runs = self.unlock([True, True, False])
        self.assertTrue(result.ok)
        self.assertIn("verified", result.message)
        self.assertIn((["wtype", "-"], "hunter2-secret"), runs)
        self.assertTrue(all("hunter2-secret" not in " ".join(argv) for argv, _ in runs))
        self.assertNotIn("hunter2-secret", result.message)

    def test_still_locked_is_reported_not_claimed(self):
        result, _ = self.unlock([True] + [True] * 20)
        self.assertFalse(result.ok)
        self.assertIn("still locked", result.message)

    def test_not_locked_types_nothing(self):
        result, runs = self.unlock([False])
        self.assertTrue(result.ok)
        self.assertFalse([argv for argv, _ in runs if argv[0] == "wtype"])

    def test_needs_sudo_access_and_a_saved_password(self):
        self.assertFalse(self.unlock([True], enabled=False)[0].ok)
        self.assertFalse(self.unlock([True], password=None)[0].ok)


class UnlockRoutingTests(unittest.TestCase):
    def session(self, phone):
        from omarchy_ai.config import Config
        from omarchy_ai.voice.gemini_live import GeminiLiveSession
        with patch("omarchy_ai.voice.gemini_live.EchoCancellation"):
            s = GeminiLiveSession(Config())
        s.from_paired_phone = phone
        return s

    def test_only_the_phone_session_reaches_the_real_unlock(self):
        with patch("omarchy_ai.execution.actions.unlock_screen_for_paired_phone",
                   return_value=ActionResult(True, "unlocked")) as real:
            self.assertFalse(self.session(False)._run_tool("unlock_screen", {}).ok)
            real.assert_not_called()
            self.assertTrue(self.session(True)._run_tool("unlock_screen", {}).ok)
            real.assert_called_once()

    def test_lock_and_unlock_are_told_apart_in_the_catalog(self):
        from omarchy_ai.execution import catalog
        tools = catalog.catalog()
        self.assertIn("unlock_screen", tools)
        self.assertIn("UNLOCK", tools["lock_screen"]["description"])


class LockedScreenKeyboardTests(unittest.TestCase):
    """The user, 2026-09-28: while the screen was locked, everything she typed
    went into the lock screen's password field."""

    def attempt(self, locked, name, args):
        runs = []

        def run(argv, **kwargs):
            runs.append(argv)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(actions, "_screen_locked", return_value=locked), \
                patch.object(actions.shutil, "which", return_value="/usr/bin/wtype"), \
                patch.object(actions, "load_config", return_value=SimpleNamespace(sudo_access_enabled=True)), \
                patch.object(actions.sudo_approval, "retrieve", return_value="pw"), \
                patch.object(actions.subprocess, "run", side_effect=run):
            return getattr(actions, name)(args), [argv for argv in runs if argv[0] in ("wtype", "wl-copy")]

    def test_nothing_is_typed_while_locked(self):
        for name, args in [("type_text", {"text": "ssh me@host"}), ("type_text", {"text": "one\ntwo"}),
                           ("press_key", {"key": "Return"}), ("submit_sudo_password", {})]:
            result, typed = self.attempt(True, name, args)
            self.assertFalse(result.ok, name)
            self.assertIn("screen is locked", result.message)
            self.assertEqual(typed, [], name)

    def test_unlocked_or_unknown_types_as_before(self):
        for locked in (False, None):
            result, typed = self.attempt(locked, "press_key", {"key": "Return"})
            self.assertTrue(result.ok)
            self.assertEqual(typed, [["wtype", "-k", "Return"]])

    def test_lock_state_falls_back_to_the_compositor(self):
        codes = {0: True, 1: False, 2: None}
        for code, expected in codes.items():
            with patch.object(actions, "_run", return_value=ActionResult(False, "no IPC")), \
                    patch.object(actions, "_desktop_env", return_value={}), \
                    patch.object(actions.subprocess, "run", return_value=SimpleNamespace(returncode=code)):
                self.assertIs(actions._screen_locked(), expected, code)

    def test_a_locked_screen_is_not_a_failure_to_escalate(self):
        from omarchy_ai.voice.escalation import Escalator
        e = Escalator(lambda reply, latest: (False, False))
        for at in (1.0, 2.0):
            e.observe_call("type_text", {}, False, actions.SCREEN_LOCKED, at)
        self.assertFalse(e.pending(3.0))


if __name__ == "__main__":
    unittest.main()
