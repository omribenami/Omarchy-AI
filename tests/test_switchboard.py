import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from omarchy_ai.core.jev import JevError
from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.voice import switchboard
from omarchy_ai.voice.switchboard import Context, Switchboard


def setUpModule():
    # The shortlist reads the user's own tools: none here, never the real ones.
    global _no_user_tools
    _no_user_tools = patch("omarchy_ai.execution.user_tools.schemas", return_value=[])
    _no_user_tools.start()


def tearDownModule():
    _no_user_tools.stop()


class FakeJev:
    def __init__(self, route="execute", p=0.97, matches=0.95, gap="none", fail=False, confirmed=0.99):
        self.answers = {"route": {"choice": route, "p": p}, "matches": {"p": matches}, "gap": {"choice": gap, "p": 0.9}}
        self.answers["confirmed"] = {"p": confirmed}
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

    def test_a_user_tools_declared_lookup_is_a_read(self):
        # 2026-10-08: a user tool's `list` after a failed command, matches 0.24-0.27.
        with patch("omarchy_ai.execution.user_tools.read_operations", return_value=frozenset({"list", "get"})):
            board = Switchboard(FakeJev("reject", 0.93, 0.24, "not_asked"))
            v = board.review("smart_home", {"operation": "list", "name": "tv"}, ctx("I think its clear enough"))
            self.assertEqual(v.action, "execute")
            v = board.review("smart_home", {"operation": "call", "name": "x"}, ctx("I think its clear enough"))
            self.assertEqual(v.action, "reject")

    def test_jev_sees_the_call_that_just_failed(self):
        jev = FakeJev()
        context = ctx("I think its clear enough")
        context.failed = {"tool": "use_tool", "args": {"request": "turn off the TV"}, "error": "multiple devices"}
        Switchboard(jev).review("smart_home", {"operation": "call"}, context)
        self.assertEqual(jev.calls[0][0]["failed_just_before"]["error"], "multiple devices")
        Switchboard(jev).review("volume_up", {}, ctx("louder"))
        self.assertNotIn("failed_just_before", jev.calls[1][0])

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

    def test_myapi_write_requires_jev_verified_confirmation(self):
        args = {"service": "gmail", "path": "/send", "method": "POST"}
        denied = Switchboard(FakeJev(confirmed=0.1)).review("myapi_write", args, ctx("no"))
        self.assertEqual(denied.action, "reject")
        approved = Switchboard(FakeJev(confirmed=0.99)).review("myapi_write", args, ctx("yes"))
        self.assertEqual(approved.action, "execute")

    def test_direct_send_command_counts_as_explicit_approval(self):
        args = {"recipient_email": "chris@example.com", "subject": "Quote", "body": "Any update?"}
        verdict = Switchboard(FakeJev(confirmed=0.99)).review(
            "myapi_gmail_send", args, ctx("Send this email to Chris at chris@example.com"))
        self.assertEqual(verdict.action, "execute")

    def test_myapi_write_fails_closed_when_jev_is_down(self):
        args = {"service": "gmail", "path": "/send", "method": "POST"}
        verdict = Switchboard(FakeJev(fail=True)).review("myapi_write", args, ctx("yes"))
        self.assertEqual(verdict.action, "reject")

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


class TaskRelayTests(unittest.TestCase):
    # 2026-09-27 19:33: relaying "I want you to do it. SSH to it yourself" to a
    # waiting task was rejected and she told the user she couldn't.
    def test_relaying_the_users_words_to_a_task_is_not_second_guessed(self):
        from omarchy_ai.voice.switchboard import Context, Switchboard
        jev = SimpleNamespace(ask=lambda *a, **k: self.fail("Jev must not be asked"))
        ctx = Context(request="I want you to do it. SSH to it yourself", earlier=[], hint=None, calls=[])
        for args in ({"task_id": "t1", "answer": "I want you to do it. SSH to it yourself"},
                     {"task_id": "t1", "guidance": "use ssh-copy-id"}):
            self.assertEqual(Switchboard(jev).review("task_respond", args, ctx).action, "execute")

    def test_approving_is_still_reviewed(self):
        from omarchy_ai.voice.switchboard import Context, Switchboard
        asked = []
        jev = SimpleNamespace(ask=lambda *a, **k: asked.append(1) or {
            "route": {"choice": "reject", "p": 0.95}, "matches": {"p": 0.02}, "gap": {"choice": "not_asked"}})
        ctx = Context(request="what's the weather", earlier=[], hint=None, calls=[])
        verdict = Switchboard(jev).review("task_respond", {"task_id": "t1", "approve": True}, ctx)
        self.assertEqual((verdict.action, asked), ("reject", [1]))


SPEAKER = {"channel": "satellite", "talking_to": "the voice speaker", "at_this_computer": False,
           "room": "living room"}
SMART_HOME = {"name": "smart_home", "description": "Control the lights, scenes and devices in the house rooms.",
              "parameters": {"type": "object", "properties": {"operation": {"type": "string"}}}}


class BetterJev(FakeJev):
    """FakeJev that also answers better_tool when it is asked."""

    def __init__(self, *a, better=None, better_p=0.9, probabilities=None, **k):
        super().__init__(*a, **k)
        self.better, self.better_p, self.probabilities = better, better_p, probabilities

    def ask(self, state, questions, **kwargs):
        answers = dict(super().ask(state, questions, **kwargs))
        if "better_tool" in questions:
            answers["better_tool"] = {"choice": self.better or next(iter(questions["better_tool"]["criteria"])),
                                      "p": self.better_p,
                                      **({"probabilities": self.probabilities} if self.probabilities else {})}
        return answers


class OriginAndAlternativesTests(unittest.TestCase):
    """2026-10-15: Jev decides with where the request came from, and chooses among tools."""

    def setUp(self):
        patcher = patch("omarchy_ai.execution.user_tools.schemas", return_value=[SMART_HOME])
        patcher.start()
        self.addCleanup(patcher.stop)

    def calm(self, origin=SPEAKER):
        # 19:58:57, said to the speaker in a room: "make it a bit calmer here".
        return Context("make it a bit calmer here", [], None, [], origin=origin)

    def test_a_speaker_request_carries_its_origin_to_jev(self):
        jev = BetterJev()
        Switchboard(jev).review("nightlight_toggle", {}, self.calm())
        state, questions = jev.calls[0]
        self.assertEqual(state["origin"], SPEAKER)
        self.assertIn("`origin`", questions["route"]["instructions"])
        self.assertIn("`origin`", questions["matches"]["instructions"])
        # Without an origin the state has none (nothing guessed).
        Switchboard(jev).review("nightlight_toggle", {}, self.calm(None))
        self.assertNotIn("origin", jev.calls[1][0])

    def test_jev_chooses_among_a_shortlist_that_holds_the_call_and_the_users_tools(self):
        jev = BetterJev()
        Switchboard(jev).review("nightlight_toggle", {}, self.calm())
        offered = jev.calls[0][1]["better_tool"]["criteria"]
        self.assertIn("nightlight_toggle", offered)
        self.assertIn("smart_home", offered)
        self.assertIn("start_task", offered)
        self.assertLessEqual(len(offered), switchboard.SHORTLIST + 1)

    def test_a_wrong_kind_of_call_with_a_confident_better_tool_is_sent_to_that_tool(self):
        jev = BetterJev("execute", 0.9, 0.3, "tool", better="smart_home", better_p=0.92)
        v = Switchboard(jev).review("nightlight_toggle", {}, self.calm())
        self.assertEqual(v.action, "reject")
        self.assertIn("smart_home", v.message)
        self.assertIn("use_tool", v.message)  # a catalog tool is reached through use_tool
        self.assertIn("the voice speaker", v.message)
        self.assertEqual(v.evidence["better"], "smart_home")

    def test_a_better_tool_needs_both_confidence_and_a_misfit(self):
        unsure = BetterJev("execute", 0.9, 0.3, "tool", better="smart_home", better_p=0.6)
        self.assertEqual(Switchboard(unsure).review("nightlight_toggle", {}, self.calm()).action, "execute")
        fits = BetterJev("execute", 0.95, 0.9, "none", better="smart_home", better_p=0.95)
        self.assertEqual(Switchboard(fits).review("nightlight_toggle", {}, self.calm()).action, "execute")

    def test_a_call_jev_barely_backs_among_the_tools_does_not_run(self):
        # 2026-10-15 replays: the kids countdown as set_reminder (matches 0.6,
        # but run_skill 0.94 / set_reminder 0.03), and "calmer" said to the
        # speaker as nightlight_toggle (route reject 0.95, night light 0.22).
        kids = BetterJev("reject", 0.47, 0.6, "none", better="run_skill", better_p=0.94,
                         probabilities={"run_skill": 0.94, "set_reminder": 0.03, "smart_home": 0.03})
        v = Switchboard(kids).review("set_reminder", {"minutes": 2}, ctx("tell the kids they have 2 minutes, then lock"))
        self.assertEqual((v.action, v.evidence["call_support"]), ("reject", 0.03))
        self.assertIn("run_skill", v.message)
        calm = BetterJev("reject", 0.95, 0.73, "none", better="run_skill", better_p=0.38,
                         probabilities={"run_skill": 0.38, "smart_home": 0.34, "nightlight_toggle": 0.22, "start_task": 0.06})
        v = Switchboard(calm).review("nightlight_toggle", {}, self.calm())
        self.assertEqual(v.action, "reject")
        self.assertIn("More likely: run_skill, smart_home", v.message)
        self.assertIn("the voice speaker", v.message)

    def test_a_call_jev_backs_runs_even_when_another_tool_is_close(self):
        backed = BetterJev("execute", 0.88, 0.69, "none", better="smart_home", better_p=0.51,
                           probabilities={"smart_home": 0.51, "start_task": 0.3, "volume_up": 0.19})
        self.assertEqual(Switchboard(backed).review("smart_home", {"operation": "call"}, self.calm()).action, "execute")
        # Doubt alone, without a confident reject route, never stops a call Jev still half backs.
        half = BetterJev("reject", 0.6, 0.7, "none", better="smart_home", better_p=0.5,
                         probabilities={"smart_home": 0.5, "nightlight_toggle": 0.45, "start_task": 0.05})
        self.assertEqual(Switchboard(half).review("nightlight_toggle", {}, self.calm()).action, "execute")

    def test_a_misfit_jev_would_reject_does_not_run(self):
        # 2026-10-15 20:48:02: hide_window_labels, route=reject p=0.72 matches=0.16 gap=not_asked, ran.
        jev = BetterJev("reject", 0.72, 0.16, "not_asked", better="hide_window_labels")
        v = Switchboard(jev).review("hide_window_labels", {}, ctx("what time is it"))
        self.assertEqual(v.action, "reject")
        # The same doubt about the values alone keeps the old, stricter bar.
        jev = BetterJev("reject", 0.72, 0.16, "values", better="volume_up")
        self.assertEqual(Switchboard(jev).review("volume_up", {}, ctx("louder")).action, "execute")

    def test_a_read_stays_lenient_and_is_not_offered_alternatives(self):
        jev = BetterJev("reject", 0.72, 0.16, "not_asked", better="smart_home", better_p=0.99)
        v = Switchboard(jev).review("list_windows", {}, self.calm())
        self.assertEqual(v.action, "execute")
        self.assertNotIn("better_tool", jev.calls[0][1])

    def test_a_write_keeps_its_confirmation_question_without_a_shortlist(self):
        jev = BetterJev(confirmed=0.99)
        Switchboard(jev).review("myapi_write", {"service": "gmail", "path": "/send", "method": "POST"}, ctx("yes"))
        self.assertIn("confirmed", jev.calls[0][1])
        self.assertNotIn("better_tool", jev.calls[0][1])


class FastPassOriginTests(unittest.TestCase):
    class Jev:
        def __init__(self):
            self.states = []

        def ask(self, state, questions, **kwargs):
            self.states.append(state)
            return {"command": {"choice": "other", "p": 0.9}, "single": {"p": 0.1},
                    "route": {"choice": "desktop_goal", "p": 0.6}}

    def test_the_fast_pass_sees_where_the_request_came_from(self):
        from omarchy_ai.voice import jev_fast
        jev = self.Jev()
        decision, route = jev_fast.judge("make it a bit calmer here", jev, origin={**SPEAKER, "person": None})
        self.assertIsNone(decision)
        self.assertEqual(jev.states[0]["origin"], SPEAKER)  # None fields dropped
        jev_fast.judge("switch to workspace 4", jev, origin={})
        self.assertNotIn("origin", jev.states[1])


class SessionOriginTests(unittest.TestCase):
    """The session reads where the request came from for every Jev decision."""

    def session(self):
        from omarchy_ai.config import Config
        from omarchy_ai.voice.gemini_live import GeminiLiveSession
        with patch("omarchy_ai.voice.gemini_live.EchoCancellation"):
            s = GeminiLiveSession(Config())
        s._transcript = [{"role": "user", "text": "make it a bit calmer here"}]
        return s

    def test_the_conversation_in_context_gives_the_switchboard_its_origin(self):
        import contextvars
        from omarchy_ai.core import conversations
        s = self.session()

        def inside():
            conversations.CURRENT.set("conv-1")
            with patch("omarchy_ai.core.conversations.source", return_value="satellite"), \
                    patch("omarchy_ai.core.origin.load_config", return_value=SimpleNamespace(satellite_room="living room")):
                return s._switchboard_context()
        c = contextvars.copy_context().run(inside)
        self.assertEqual({k: c.origin[k] for k in SPEAKER}, SPEAKER)

    def test_a_session_without_a_conversation_uses_its_channel(self):
        import contextvars
        s = self.session()
        s.channel = "desktop-text"
        c = contextvars.copy_context().run(s._switchboard_context)
        self.assertEqual((c.origin["channel"], c.origin["at_this_computer"]), ("desktop-text", True))

    def test_the_instructions_say_where_the_user_is(self):
        from omarchy_ai.voice.gemini_live import origin_note
        with patch("omarchy_ai.core.conversations.source", return_value="phone-voice"):
            note = origin_note("conv-1")
        self.assertIn("WHERE THE USER IS", note)
        self.assertIn("not at this computer", note)
        self.assertIn("at this computer", origin_note(None, "desktop-text"))


class TaskStatusRepeatTests(unittest.TestCase):
    """2026-10-15 20:48:06-20:48:38: ~40 task_status calls in 32 seconds for one task."""

    def summary(self, steps=3):
        return json.dumps({"id": "t1", "goal": "x" * 2000, "status": "running", "phase": "work", "steps": steps,
                           "last_step": {"executor": "codex"}, "question": None})

    def test_an_unchanged_task_asked_again_soon_gets_a_short_answer(self):
        s = SessionOriginTests.session(self)
        first = s._task_status_repeat({"task_id": "t1"}, ActionResult(True, self.summary()))
        self.assertIn("x" * 100, first.message)
        again = s._task_status_repeat({"task_id": "t1"}, ActionResult(True, self.summary()))
        self.assertTrue(again.ok)
        self.assertIn('"unchanged": true', again.message)
        self.assertIn("Stop calling task_status", again.message)
        self.assertLess(len(again.message), 600)
        moved = s._task_status_repeat({"task_id": "t1"}, ActionResult(True, self.summary(steps=4)))
        self.assertIn("x" * 100, moved.message)  # a change gets the whole status

    def test_after_the_window_the_full_status_comes_back(self):
        s = SessionOriginTests.session(self)
        s._task_status_repeat({}, ActionResult(True, self.summary()))
        s._task_seen["t1"] = (s._task_seen["t1"][0], s._task_seen["t1"][1] - s.TASK_REPEAT_SECONDS - 1)
        self.assertIn("x" * 100, s._task_status_repeat({}, ActionResult(True, self.summary())).message)

    def test_the_repeat_is_short_through_the_live_dispatch(self):
        s = SessionOriginTests.session(self)
        s._switchboard = SimpleNamespace(review=lambda tool, args, ctx: switchboard.Verdict("execute", tool, args))
        messages = []
        for _ in range(2):
            session = SimpleNamespace(send_tool_response=AsyncMock())
            with patch("omarchy_ai.voice.gemini_live.run_action", return_value=ActionResult(True, self.summary())), \
                    patch("omarchy_ai.voice.gemini_live.LiveSession._current_window", return_value={}):
                asyncio.run(s._run_call(session, SimpleNamespace(id="c1", name="task_status", args={"task_id": "t1"})))
            messages.append(session.send_tool_response.call_args.kwargs["function_responses"].response["message"])
        self.assertIn("x" * 100, messages[0])
        self.assertIn('"unchanged": true', messages[1])
