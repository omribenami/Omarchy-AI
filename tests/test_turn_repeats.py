"""One action per request: a repeat of the turn's previous action is not run
again (execution/verified_input.py REPEAT_SECONDS). Journal 2026-10-17 22:18:
one typed "Turn the volume down a little" ran volume_down twice."""

import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from omarchy_ai.core import logs
from omarchy_ai.execution import catalog, verified_input
from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.execution.verified_input import InputGuard
from omarchy_ai.voice import switchboard


class GuardRepeatTests(unittest.TestCase):
    def setUp(self):
        self.guard = InputGuard()
        self.execute = Mock(return_value=ActionResult(True, "volume 40%"))

    def run_in(self, turn, name, args=None, words=None):
        if words is not None:
            self.guard.heard_user(words)
        with logs.bind(turn_id=turn):
            return self.guard.run(self.execute, name, dict(args or {}))

    def names(self):
        return [c.args[0] for c in self.execute.call_args_list]

    def test_a_same_turn_repeat_returns_the_first_answer_without_running(self):
        first = self.run_in("t1", "volume_down", words="Turn the volume down a little")
        second = self.run_in("t1", "volume_down")
        self.assertEqual(self.names(), ["volume_down"])
        self.assertTrue(second.ok)
        self.assertEqual(first.message, "volume 40%")
        self.assertIn("Already done for this request", second.message)
        self.assertIn("volume 40%", second.message)

    def test_a_silent_first_answer_is_still_confirmed(self):
        self.execute.return_value = ActionResult(True, "")
        self.run_in("t1", "volume_up", words="turn it up")
        self.assertIn("done", self.run_in("t1", "volume_up").message)
        self.assertEqual(self.names(), ["volume_up"])

    def test_different_arguments_run(self):
        self.run_in("t1", "brightness_set", {"percent": 40}, words="dim the screen")
        self.run_in("t1", "brightness_set", {"percent": 30})
        self.assertEqual(self.names(), ["brightness_set", "brightness_set"])

    def test_the_next_turn_runs(self):
        self.run_in("t1", "volume_down", words="turn it down")
        self.run_in("t2", "volume_down", words="a bit more")
        self.assertEqual(self.names(), ["volume_down", "volume_down"])

    def test_reads_always_run(self):
        self.run_in("t1", "list_windows", words="what is open")
        self.run_in("t1", "list_windows")
        self.assertEqual(self.names(), ["list_windows", "list_windows"])

    def test_a_read_in_between_does_not_make_a_repeat_new(self):
        self.run_in("t1", "volume_down", words="turn it down")
        self.run_in("t1", "list_windows")
        self.run_in("t1", "volume_down")
        self.assertEqual(self.names(), ["volume_down", "list_windows"])

    def test_a_repeat_after_another_action_runs(self):
        # focus A, then B, then A again: the user's own sequence, not a double call.
        self.run_in(
            "t1", "workspace_switch", {"number": 1}, words="go to 2 and come back"
        )
        self.run_in("t1", "workspace_switch", {"number": 2})
        self.run_in("t1", "workspace_switch", {"number": 1})
        self.assertEqual(len(self.names()), 3)

    def test_the_user_asking_twice_runs_twice(self):
        for words in (
            "turn the volume up twice",
            "skip two songs",
            "press it 3 times",
            "תגביר פעמיים",
        ):
            with self.subTest(words=words):
                self.execute.reset_mock()
                self.run_in(words, "volume_up", words=words)
                self.run_in(words, "volume_up")
                self.assertEqual(self.names(), ["volume_up", "volume_up"])

    def test_a_failed_first_call_does_not_hold_back_the_second(self):
        self.execute.return_value = ActionResult(False, "no audio device")
        self.run_in("t1", "volume_down", words="turn it down")
        self.run_in("t1", "volume_down")
        self.assertEqual(self.names(), ["volume_down", "volume_down"])

    def test_after_the_window_it_runs_again(self):
        self.run_in("t1", "volume_down", words="turn it down")
        self.guard._previous.at -= verified_input.REPEAT_SECONDS + 1
        self.run_in("t1", "volume_down")
        self.assertEqual(self.names(), ["volume_down", "volume_down"])

    def test_without_a_turn_nothing_is_held_back(self):
        # The Task Runtime, the phone bridge and nested agent calls have no user turn.
        self.guard.run(self.execute, "volume_down", {})
        self.guard.run(self.execute, "volume_down", {})
        self.assertEqual(self.names(), ["volume_down", "volume_down"])

    def test_a_repeat_sent_while_the_first_runs_waits_for_its_answer(self):
        started, release = threading.Event(), threading.Event()

        def slow(name, args):
            started.set()
            release.wait(5)
            return ActionResult(True, "closed")

        self.execute.side_effect = slow
        self.guard.heard_user("close the garage door")
        answers = []

        def call():
            with logs.bind(turn_id="t1"):
                answers.append(self.guard.run(self.execute, "close_window", {}))

        first = threading.Thread(target=call)
        first.start()
        started.wait(5)
        second = threading.Thread(target=call)
        second.start()
        time.sleep(0.05)
        release.set()
        first.join(5)
        second.join(5)
        self.assertEqual(self.names(), ["close_window"])
        self.assertEqual(
            sorted(a.message for a in answers),
            ["Already done for this request, not run again: closed", "closed"],
        )


class SessionRepeatTests(unittest.TestCase):
    """The live session: use_tool resolved to the same tool, through the ToolRunner."""

    def session(self):
        from omarchy_ai.config import Config
        from omarchy_ai.voice import escalation
        from omarchy_ai.voice.gemini_live import GeminiLiveSession

        with patch("omarchy_ai.voice.gemini_live.EchoCancellation"):
            s = GeminiLiveSession(Config())
        s._transcript = [{"role": "user", "text": "Turn the volume down a little"}]
        s._switchboard = SimpleNamespace(
            review=lambda tool, args, ctx: switchboard.Verdict("execute", tool, args)
        )
        s._escalator = escalation.Escalator(
            lambda reply, latest: (False, False)
        )  # no network in tests
        s._turn_id = "turn-1"
        s._input_guard.heard_user("Turn the volume down a little")
        return s

    def dispatch(self, s, run, call_id, args, pick):
        session = SimpleNamespace(send_tool_response=AsyncMock())
        with (
            patch("omarchy_ai.voice.gemini_live.catalog.resolve", return_value=pick),
            patch("omarchy_ai.voice.gemini_live.run_action", run),
            patch(
                "omarchy_ai.voice.gemini_live.LiveSession._current_window",
                return_value={},
            ),
        ):

            async def call():
                await s._run_call(
                    session, SimpleNamespace(id=call_id, name="use_tool", args=args)
                )
                await asyncio.gather(*list(s._bg_tasks))

            asyncio.run(call())
        return session.send_tool_response.call_args.kwargs[
            "function_responses"
        ].response

    def test_two_use_tool_calls_resolved_to_volume_down_run_once(self):
        s, run = self.session(), Mock(return_value=ActionResult(True, ""))
        pick = catalog.Resolution(
            True, "volume_down", {}, picked=True, evidence={"p": 1.0}
        )
        first = self.dispatch(s, run, "c1", {"request": "Turn the volume down"}, pick)
        second = self.dispatch(s, run, "c2", {"request": "Turn down the volume"}, pick)
        run.assert_called_once_with("volume_down", {})
        self.assertEqual(first, {"ok": True, "message": "done"})
        self.assertTrue(second["ok"])
        self.assertIn("Already done for this request", second["message"])
        # By name the second time (journal 2026-10-17: request, then name=volume_up): the same.
        third = self.dispatch(
            s,
            run,
            "c3",
            {"name": "volume_down", "args": {}},
            catalog.Resolution(True, "volume_down", {}, evidence={"by": "name"}),
        )
        run.assert_called_once()
        self.assertIn("Already done", third["message"])
        # The user's next words are a new turn: it runs.
        s._turn_id = "turn-2"
        s._input_guard.heard_user("a bit more")
        self.dispatch(s, run, "c4", {"request": "Turn the volume down"}, pick)
        self.assertEqual(run.call_count, 2)

    def test_the_parameter_offer_then_the_named_call_runs(self):
        s, run = self.session(), Mock(return_value=ActionResult(True, "Reminder set"))
        offer = catalog.Resolution(
            False, message='{"tools": [{"name": "set_reminder"}]}'
        )
        self.assertTrue(
            self.dispatch(s, run, "c1", {"request": "remind me"}, offer)["ok"]
        )
        run.assert_not_called()
        args = {"minutes": 5, "message": "tea"}
        named = self.dispatch(
            s,
            run,
            "c2",
            {"name": "set_reminder", "args": args},
            catalog.Resolution(True, "set_reminder", args, evidence={"by": "name"}),
        )
        run.assert_called_once_with("set_reminder", args)
        self.assertEqual(named["message"], "Reminder set")


class DesktopSessionRepeatTests(unittest.TestCase):
    def test_the_desktop_live_session_runs_a_same_turn_repeat_once(self):
        from omarchy_ai.voice.tool_dispatch import ToolDispatchMixin

        session = SimpleNamespace(
            _resources=None,
            _hangup=asyncio.Event(),
            _action_log=[],
            _input_guard=InputGuard(),
            _demo_waiting_for_action=threading.Event(),
            _watchdog_on=False,
            _dc=None,
            _audit_session="s",
            _turn_id="turn-1",
            _set_watchdog_state=lambda state: None,
            _current_window=lambda: {},
            _send_function_result=Mock(),
        )
        session._input_guard.heard_user("volume up")
        run = Mock(return_value=ActionResult(True, "volume 60%"))

        async def calls():
            for call_id in ("a", "b"):
                await ToolDispatchMixin._run_tool_call(
                    session, call_id, "volume_up", {}
                )

        with patch("omarchy_ai.voice.tool_dispatch.run_action", run):
            session._run_tool_call_ordered = lambda *a: (
                ToolDispatchMixin._run_tool_call_ordered(session, *a)
            )
            asyncio.run(calls())
        run.assert_called_once_with("volume_up", {})
        messages = [c.args[2] for c in session._send_function_result.call_args_list]
        self.assertEqual(messages[0], "volume 60%")
        self.assertIn("Already done", messages[1])


if __name__ == "__main__":
    unittest.main()
