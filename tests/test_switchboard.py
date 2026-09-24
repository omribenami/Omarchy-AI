import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from omarchy_ai.core.jev import JevError
from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.voice import switchboard
from omarchy_ai.voice.switchboard import Context, Switchboard


class FakeJev:
    def __init__(self, route="execute", p=0.97, matches=0.95, gap="none", fail=False):
        self.answers = {"route": {"choice": route, "p": p}, "matches": {"p": matches}, "gap": {"choice": gap, "p": 0.9}}
        self.fail, self.calls = fail, []

    def ask(self, state, questions, **kwargs):
        self.calls.append((state, questions))
        if self.fail:
            raise JevError("Gateway down")
        return self.answers


def ctx(request="switch to workspace 4", hint=None, calls=()):
    return Context(request, [], hint, list(calls))


class PolicyTests(unittest.TestCase):
    def test_a_matching_call_runs(self):
        v = Switchboard(FakeJev()).review("workspace_switch", {"number": 4}, ctx())
        self.assertEqual((v.action, v.tool, v.args), ("execute", "workspace_switch", {"number": 4}))

    def test_wrong_values_are_sent_back(self):
        # 2026-09-23: the user said workspace 4 and Gemini switched to 5.
        v = Switchboard(FakeJev("reject", 0.93, 0.04, "values")).review("workspace_switch", {"number": 5}, ctx())
        self.assertEqual(v.action, "reject")
        self.assertIn("switch to workspace 4", v.message)
        self.assertIn("values are wrong", v.message)

    def test_an_unsure_reject_still_runs(self):
        v = Switchboard(FakeJev("reject", 0.6, 0.5)).review("volume_up", {}, ctx("louder"))
        self.assertEqual(v.action, "execute")

    def test_reads_are_only_refused_for_obvious_nonsense(self):
        v = Switchboard(FakeJev("reject", 0.95, 0.2)).review("list_windows", {}, ctx())
        self.assertEqual(v.action, "execute")
        v = Switchboard(FakeJev("reject", 0.95, 0.01)).review("read_file", {"path": "/etc/x"}, ctx("volume up"))
        self.assertEqual(v.action, "reject")

    def test_a_multi_step_job_typed_into_a_terminal_goes_to_the_task_runtime(self):
        request = "find what is using port 8080 and stop it"
        v = Switchboard(FakeJev("start_task", 0.95)).review(
            "terminal_task", {"command": "ss -ltnp"}, ctx(request, {"route": "whole_task", "p": 0.9}))
        self.assertEqual((v.action, v.tool, v.args), ("reroute", "start_task", {"goal": request}))

    def test_reroute_needs_the_fast_pass_to_agree_and_no_chain_started(self):
        relay = ctx("tell Claude: fix the login bug", {"route": "terminal", "p": 0.95})
        v = Switchboard(FakeJev("start_task", 0.95)).review("type_text", {"text": "fix the login bug"}, relay)
        self.assertEqual(v.action, "execute")
        chain = ctx("find what uses port 8080", None, [("focus_window", {"target": "0x1"}), ("type_text", {"text": "ss"})])
        jev = FakeJev("execute")
        Switchboard(jev).review("press_key", {"key": "Return"}, chain)
        options = jev.calls[0][1]["route"]["criteria"]
        self.assertNotIn("start_task", options)

    def test_a_desktop_goal_via_command_search_goes_to_the_desktop_loop_when_the_fast_pass_agrees(self):
        request = "move this window to workspace 4"
        v = Switchboard(FakeJev("desktop_task", 0.81)).review(
            "list_commands", {"query": "move window"}, ctx(request, {"route": "instant", "p": 1.0}))
        self.assertEqual((v.action, v.tool, v.args), ("reroute", "desktop_task", {"goal": request}))
        v = Switchboard(FakeJev("desktop_task", 0.81)).review("list_commands", {"query": "move window"}, ctx(request))
        self.assertEqual(v.action, "execute")

    def test_ambiguous_request_is_asked(self):
        v = Switchboard(FakeJev("ask_user", 0.9, 0.5, "ambiguous")).review("close_window", {}, ctx("close it"))
        self.assertEqual(v.action, "ask")

    def test_jev_down_never_stops_the_assistant(self):
        v = Switchboard(FakeJev(fail=True)).review("volume_up", {}, ctx("louder"))
        self.assertEqual(v.action, "execute")
        self.assertIn("unavailable", v.evidence)

    def test_repeated_identical_calls_reuse_the_decision(self):
        jev = FakeJev()
        board = Switchboard(jev)
        for _ in range(3):
            board.review("read_tile_log", {"window": "0x1"}, ctx("what did the build print"))
        self.assertEqual(len(jev.calls), 1)

    def test_no_user_words_means_nothing_to_judge(self):
        jev = FakeJev()
        v = Switchboard(jev).review("list_windows", {}, ctx(""))
        self.assertEqual(v.action, "execute")
        self.assertEqual(jev.calls, [])


class DispatchTests(unittest.TestCase):
    """GeminiLiveSession._run_call obeys the verdict (desktop and phone share it)."""

    def session(self, verdict):
        from omarchy_ai.config import Config
        from omarchy_ai.voice.gemini_live import GeminiLiveSession
        with patch("omarchy_ai.voice.gemini_live.EchoCancellation"):
            s = GeminiLiveSession(Config())
        s._transcript = [{"role": "user", "text": "switch to workspace 4"}]
        s._switchboard = SimpleNamespace(review=lambda tool, args, ctx: verdict)
        return s

    def run_call(self, s, name, args):
        session = SimpleNamespace(send_tool_response=AsyncMock())
        call = SimpleNamespace(id="c1", name=name, args=args)
        with patch("omarchy_ai.voice.gemini_live.run_action", return_value=ActionResult(True, "done")) as run, \
             patch("omarchy_ai.voice.gemini_live.LiveSession._current_window", return_value={}):
            asyncio.run(s._run_call(session, call))
        return run, session.send_tool_response.call_args.kwargs["function_responses"].response

    def test_rejected_call_never_runs_and_the_model_hears_why(self):
        s = self.session(switchboard.Verdict("reject", message="Not run: Jev checked workspace_switch ..."))
        run, response = self.run_call(s, "workspace_switch", {"number": 5})
        run.assert_not_called()
        self.assertFalse(response["ok"])
        self.assertIn("Jev checked", response["message"])

    def test_reroute_runs_the_other_executor_with_the_users_words(self):
        s = self.session(switchboard.Verdict("reroute", "start_task", {"goal": "find what uses port 8080"},
                                             message="Jev routed this request to start_task instead of terminal_task."))
        run, response = self.run_call(s, "terminal_task", {"command": "ss -ltnp"})
        run.assert_called_once_with("start_task", {"goal": "find what uses port 8080"})
        self.assertIn("routed this request to start_task", response["message"])

    def test_approved_read_runs_once(self):
        s = self.session(switchboard.Verdict("execute", "list_windows", {}))
        run, response = self.run_call(s, "list_windows", {})
        run.assert_called_once_with("list_windows", {})
        self.assertTrue(response["ok"])

    def test_context_is_the_latest_request_and_this_turns_calls(self):
        import time
        s = self.session(None)
        s._transcript = [{"role": "user", "text": "open a terminal"}, {"role": "assistant", "text": "Done."},
                         {"role": "user", "text": "switch to "}, {"role": "user", "text": "workspace 4"}]
        s._last_user_speech = time.monotonic() - 1
        s._gemini_inflight = [("list_windows", {}, time.monotonic() - 5), ("focus_window", {"target": "x"}, time.monotonic())]
        s._route_hint = {"route": "instant", "p": 0.97, "request": "switch to workspace 4", "at": time.monotonic()}
        c = s._switchboard_context()
        self.assertEqual(c.request, "switch to workspace 4")
        self.assertEqual(c.earlier, ["open a terminal"])
        self.assertEqual(c.hint, {"route": "instant", "p": 0.97})
        self.assertEqual(c.calls, [("focus_window", {"target": "x"})])


if __name__ == "__main__":
    unittest.main()
