import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.voice import escalation, switchboard
from omarchy_ai.voice.escalation import Escalator

REJECTED = ("Not run: Jev checked report_issue {\"title\": \"Fatal kernel bug in Wi-Fi driver\"} against the user's "
            "request 'תפתחי שיעור באתר שלומצ'י' and the user did not ask for this.")


def judge(gave_up=False, asks_why=False):
    """Stands in for Jev's reading of the reply and the user's words."""
    calls = []
    def decide(reply, latest):
        calls.append((reply, latest))
        return gave_up, asks_why
    decide.calls = calls
    return decide


class EscalatorTests(unittest.TestCase):
    def test_proactive_goal_carries_prior_requirements_into_background_task(self):
        goal = escalation.proactive_goal([
            "Use the full carrier bill and half the other bill.",
            "Never submit it; notify me when it is ready for review.",
            "Now create this month's report.",
        ])
        self.assertIn("full carrier bill", goal)
        self.assertIn("Never submit", goal)
        self.assertIn("Current request:\nNow create", goal)
        self.assertIn("until it is completed", goal)

    def test_two_failures_escalate_once_with_a_full_brief(self):
        # 2026-09-26 23:36-23:39: report_issue had no token, then browser_task stopped.
        e = Escalator(judge())
        turns = ["תפתחי שיעור באתר שלומצ'י בגיט שלומצ'י לגבי בעיית שראינו."]
        e.observe_call("report_issue", {"title": "Wi-Fi freeze"}, False,
                       "Issue NOT filed: no GitHub issue token configured on this machine", 10.0)
        self.assertIsNone(e.due(10.0, turns, ""))
        e.observe_call("browser_task", {"url": "https://github.com"}, False,
                       "Browser task stopped after 2 actions: Stopped repeated interaction", 21.0)
        goal = e.due(21.0, turns, "")
        self.assertIn("2 attempts failed", goal)
        self.assertIn("שיעור", goal)
        self.assertIn("report_issue", goal)
        self.assertIn("browser_task", goal)
        self.assertIn("approval", goal)
        self.assertIsNone(e.due(22.0, turns, ""))  # once per incident

    def test_one_failure_then_giving_up_escalates(self):
        decide = judge(gave_up=True)
        e = Escalator(decide)
        e.observe_call("report_issue", {}, False, "Issue NOT filed: no GitHub issue token configured", 5.0)
        # The real reply (2026-09-26), in whatever language she spoke.
        goal = e.due(8.0, ["open an issue"], "איני יכולה לפתוח את הדיווח כרגע, כיוון שאין לי גישה")
        self.assertIn("could not do it", goal)
        self.assertEqual(decide.calls, [("איני יכולה לפתוח את הדיווח כרגע, כיוון שאין לי גישה", "open an issue")])

    def test_one_failure_then_user_asks_why_escalates(self):
        e = Escalator(judge(asks_why=True))
        e.observe_call("browser_task", {}, False, "stopped", 5.0)
        self.assertIn("find out why", e.due(9.0, ["open an issue", "Tu dois essayer de comprendre pourquoi."], ""))

    def disputing(self, answers):
        answers = list(answers)
        return Escalator(judge(), disputes=lambda reply, latest: answers.pop(0))

    def test_a_disputed_result_gets_a_self_check_then_a_handover(self):
        # Journal 2026-10-05 16:04-16:09: eleven ok=True searches on the wrong machine.
        e = self.disputing([True, True])
        turns = ["ssh to the server and find the folder there"]
        e.observe_call("terminal_task", {"command": "find ~ -name custom_components"}, True,
                       "started 'find ~ -name custom_components' in assistant terminal 'find'. WHERE IT RUNS: "
                       "this computer (desk), not a remote server.", 10.0)
        self.assertEqual(e.take_review_note(11.0), "")
        self.assertIsNone(e.due(11.0, turns, ""))  # nothing failed
        self.assertIsNone(e.disputed(20.0, turns + ["that cannot be, the folder exists for certain"], "not found"))
        note = e.take_review_note(21.0)
        self.assertIn("one of your own assumptions is wrong", note)
        self.assertIn("WHERE IT RUNS: this computer", note)
        self.assertIn("find ~ -name custom_components", note)
        self.assertEqual(e.take_review_note(22.0), "")  # once
        goal = e.disputed(60.0, turns + ["I told you, it is on the server"], "still not found")
        self.assertIn("the user said twice that her result is wrong", goal)
        self.assertIn("each reported success", goal)
        self.assertIn("find ~ -name custom_components", goal)
        self.assertFalse(e.reviewable(61.0))  # cooldown, and the steps went with the task

    def test_no_dispute_no_recent_work_or_her_own_task_means_no_self_check(self):
        e = self.disputing([False])
        e.observe_call("read_file", {"path": "a"}, True, "text", 10.0)
        self.assertIsNone(e.disputed(12.0, ["thanks, and what else is in it?"], "it lists three items"))
        self.assertEqual(e.take_review_note(13.0), "")
        idle = Escalator(judge(), disputes=lambda *a: self.fail("Jev asked with nothing to dispute"))
        self.assertFalse(idle.reviewable(5.0))
        self.assertIsNone(idle.disputed(5.0, ["are you sure?"], ""))
        idle.observe_call("read_file", {}, True, "text", 10.0)
        self.assertIsNone(idle.disputed(10.0 + escalation.REVIEW_WINDOW + 1, ["are you sure?"], ""))  # too old
        idle.observe_call("read_file", {}, True, "text", 400.0)
        idle.observe_call("start_task", {"goal": "g"}, True, "{}", 401.0)
        self.assertFalse(idle.reviewable(402.0))

    def test_a_stale_self_check_is_not_delivered(self):
        e = self.disputing([True])
        e.observe_call("list_files", {"path": "."}, True, "[]", 10.0)
        e.disputed(12.0, ["are you sure?"], "nothing there")
        self.assertEqual(e.take_review_note(12.0 + escalation.REVIEW_NOTE_TTL + 1), "")

    def test_one_failure_and_she_is_still_trying_does_not_escalate(self):
        e = Escalator(judge())
        e.observe_call("browser_task", {}, False, "stopped", 5.0)
        self.assertIsNone(e.due(6.0, ["open an issue"], "Let me try another way."))

    def test_one_missing_capability_escalates_immediately_to_build_a_tool(self):
        e = Escalator(judge())
        e.observe_call("use_tool", {"request": "control the sprinkler"}, False,
                       "Missing capability: No catalog tool does this.", 5.0)
        goal = e.due(5.0, ["turn on the garden sprinkler"], "")
        self.assertIn("missing capability", goal)
        self.assertIn("Claude Code or Codex", goal)
        self.assertIn("resume this SAME task", goal)
        self.assertIn("finish the original goal", goal)

    def test_nothing_failed_nothing_escalates_and_jev_is_not_asked(self):
        decide = judge(gave_up=True)
        e = Escalator(decide)
        e.observe_call("volume_up", {}, True, "ok", 1.0)
        self.assertFalse(e.pending(2.0))
        self.assertIsNone(e.due(2.0, ["why is the sky blue"], "I can't see the sky"))
        self.assertEqual(decide.calls, [])

    def test_jev_unavailable_means_only_repeated_failures_escalate(self):
        from omarchy_ai.core.jev import JevError
        e = Escalator()
        e.observe_call("browser_task", {}, False, "stopped", 5.0)
        with patch("omarchy_ai.core.jev.Jev.ask", side_effect=JevError("Gateway circuit open")):
            self.assertIsNone(e.due(6.0, ["open an issue"], "I can't do that"))

    def test_interruptions_clarifications_and_control_calls_are_not_failures(self):
        e = Escalator(judge())
        e.observe_call("report_issue", {}, False, REJECTED, 0.5)
        e.observe_call("type_text", {}, False, "Input NOT sent: focus is unverified or changed.", 0.6)
        e.observe_call("read_file", {}, False, "that file or folder does not exist", 0.7)
        e.observe_call("close_window", {}, False, "Not executed: user interrupted before this action started.", 1.0)
        e.observe_call("close_window", {}, False, "Not run: Jev judged the request 'x' ambiguous about what", 2.0)
        e.observe_call("task_status", {}, False, "no such task", 3.0)
        self.assertIsNone(e.due(4.0, ["close it"], "I can't"))

    def test_guards_and_handoffs_do_not_escalate_a_live_mission(self):
        # 2026-09-27 01:14:07-01:14:45, the real sequence.
        e = Escalator(judge())
        e.observe_call("desktop_task", {"goal": "Close the browser"}, False,
                       '{"status": "handoff", "reason": "Live model must handle the remaining goal"}', 7.0)
        e.observe_call("run_omarchy_command", {"args": ["capture", "recording", "start"]}, False,
                       "Not executed: you just read a multi-step script. Perform it with run_mission", 45.0)
        self.assertIsNone(e.due(45.0, ["תתחילי הקלטה של המסך ואחרי זה תריצי את כל הפרסומת"], ""))

    def test_waiting_on_the_user_is_not_a_failure(self):
        # 2026-09-27 02:06 (which TV) and 10:24 (a sign-in only the user can finish).
        e = Escalator(judge(gave_up=True))
        e.observe_call("run_mission", {}, False, "Mission stopped at step 8 (start_casting {}): found 2 TVs on the "
                       "network (A, B) -- call list_cast_targets and ask the user which one, then call start_casting", 1.0)
        e.observe_call("browser_task", {}, False, "Browser task NOT verified (blocked). Likely cause (from the page "
                       "itself): The assistant's browser is not signed in to github.com. Signing in here needs the "
                       "user's password: call show_browser and ask the user to sign in there", 2.0)
        self.assertIsNone(e.due(3.0, ["upload it"], "I can't"))

    def test_old_failures_expire(self):
        e = Escalator(judge())
        e.observe_call("browser_task", {}, False, "stopped", 0.0)
        e.observe_call("browser_task", {}, False, "stopped", escalation.FAILURE_WINDOW + 10)
        self.assertIsNone(e.due(escalation.FAILURE_WINDOW + 10, ["go"], ""))

    def test_her_own_start_task_handles_the_incident(self):
        e = Escalator(judge())
        e.observe_call("browser_task", {}, False, "stopped", 1.0)
        e.observe_call("start_task", {"goal": "x"}, True, "{}", 2.0)
        e.observe_call("browser_task", {}, False, "stopped", 3.0)
        e.observe_call("browser_task", {}, False, "stopped", 4.0)
        self.assertIsNone(e.due(5.0, ["go"], ""))

    def test_cooldown_after_an_escalation(self):
        e = Escalator(judge())
        for t in (1.0, 2.0):
            e.observe_call("browser_task", {}, False, "stopped", t)
        self.assertIsNotNone(e.due(2.0, ["go"], ""))
        for t in (3.0, 4.0):
            e.observe_call("browser_task", {}, False, "stopped", t)
        self.assertIsNone(e.due(4.0, ["go"], ""))
        for t in (200.0, 201.0):
            e.observe_call("browser_task", {}, False, "stopped", t)
        self.assertIsNotNone(e.due(201.0, ["go"], ""))

    def test_brief_fits_start_task(self):
        e = Escalator(judge())
        for t in range(6):
            e.observe_call("browser_task", {"steps": ["x" * 500] * 5}, False, "y" * 2000, float(t))
        self.assertLessEqual(len(e.due(6.0, ["z" * 2000] * 10, "w" * 2000)), 4000)


def claims(verdict=True, claims_action=0.95, asks_action=0.95):
    """Stands in for Jev's reading of a reply that says it is being done."""
    calls = []
    def decide(reply, latest):
        calls.append((reply, latest))
        return verdict, {"claims_action": claims_action, "asks_action": asks_action}
    decide.calls = calls
    return decide


class UnbackedClaimTests(unittest.TestCase):
    """2026-10-12: "on it" after only reading a skill, and nothing ran."""

    TURNS = ["water the plants every minute for five minutes"]

    def ask(self, **p):
        from omarchy_ai.core.jev import Jev
        answers = {name: {"p": value} for name, value in p.items()}
        with patch.object(Jev, "ask", return_value=answers) as ask:
            verdict = escalation.jev_unbacked("On it, starting now.", self.TURNS[0])
        state, questions = ask.call_args.args
        self.assertEqual(set(questions), {"claims_action", "asks_action"})
        self.assertEqual(state, {"assistant_reply": "On it, starting now.", "user_latest_message": self.TURNS[0]})
        self.assertEqual(ask.call_args.kwargs, {"timeout": 3, "retries": 0, "fail_fast": True})
        return verdict

    def test_both_judgments_are_needed(self):
        self.assertEqual(self.ask(claims_action=0.9, asks_action=0.85),
                         (True, {"claims_action": 0.9, "asks_action": 0.85}))
        self.assertFalse(self.ask(claims_action=0.79, asks_action=0.95)[0])  # she answered, asked or declined
        self.assertFalse(self.ask(claims_action=0.95, asks_action=0.79)[0])  # the user was only talking

    def test_jev_unavailable_is_no_claim(self):
        from omarchy_ai.core.jev import Jev, JevError
        with patch.object(Jev, "ask", side_effect=JevError("Gateway circuit open (20s left)")):
            verdict, evidence = escalation.jev_unbacked("On it.", self.TURNS[0])
        self.assertFalse(verdict)
        self.assertIn("circuit open", evidence["unavailable"])

    def test_a_claim_with_no_call_behind_it_becomes_a_task_goal(self):
        from omarchy_ai.runtime.task import request_text
        decide = claims()
        e = Escalator(judge(), unbacked=decide)
        goal, evidence = e.unbacked(50.0, ["earlier words", *self.TURNS], "On it, starting the countdown.", since=40.0)
        self.assertEqual(decide.calls, [("On it, starting the countdown.", self.TURNS[0])])
        self.assertEqual(evidence, {"claims_action": 0.95, "asks_action": 0.95})
        self.assertIn("no tool that does it ran", goal)
        self.assertIn("What the voice assistant tried: nothing.", goal)
        self.assertIn("On it, starting the countdown.", goal)
        self.assertEqual(request_text(goal), self.TURNS[0])
        self.assertLessEqual(len(goal), 4000)
        self.assertEqual(e.unbacked(60.0, self.TURNS, "It is running.", since=55.0), (None, {}))  # once per incident
        self.assertEqual(len(decide.calls), 1)

    def test_the_brief_lists_what_she_only_read(self):
        e = Escalator(judge(), unbacked=claims())
        e.observe_call("list_files", {"path": "~"}, True, "[]", 10.0)  # before these words
        e.observe_call("load_skill", {"name": "water-plants"}, True, '{"name": "water-plants"}', 45.0)
        goal, _ = e.unbacked(50.0, self.TURNS, "On it.", since=40.0)
        self.assertIn("none of which carried it out", goal)
        self.assertIn("load_skill(", goal)
        self.assertNotIn("list_files", goal)

    def test_an_ordinary_answer_is_left_alone(self):
        decide = claims(False, claims_action=0.04, asks_action=0.1)
        e = Escalator(judge(), unbacked=decide)
        self.assertEqual(e.unbacked(50.0, ["what is the temperature?"], "It is 73 degrees."),
                         (None, {"claims_action": 0.04, "asks_action": 0.1}))
        self.assertEqual(len(decide.calls), 1)
        self.assertTrue(e.claimable(51.0))  # nothing was handed over

    def test_nothing_is_judged_right_after_a_task_was_started_or_escalated(self):
        decide = claims()
        e = Escalator(judge(), unbacked=decide)
        e.observe_call("start_task", {"goal": "g"}, True, "{}", 10.0)
        self.assertFalse(e.claimable(20.0))
        self.assertEqual(e.unbacked(20.0, self.TURNS, "It has started."), (None, {}))
        failed = Escalator(judge(), unbacked=decide)
        for at in (1.0, 2.0):
            failed.observe_call("browser_task", {}, False, "stopped", at)
        self.assertIsNotNone(failed.due(3.0, self.TURNS, ""))
        self.assertEqual(failed.unbacked(4.0, self.TURNS, "I handed it over."), (None, {}))
        self.assertEqual(decide.calls, [])
        self.assertIsNotNone(e.unbacked(10.0 + escalation.COOLDOWN, self.TURNS, "On it.")[0])

    def test_the_notice_says_nothing_happened_and_who_does_it_now(self):
        text = escalation.notice("20261012-090000-abc123", unbacked=True)
        self.assertIn("no tool ran, so nothing happened", text)
        self.assertIn("20261012-090000-abc123 is doing it now", text)
        self.assertIn("Do not also do it yourself", text)
        self.assertIn("already doing this job", escalation.notice("20261012-090000-abc123", existing=True, unbacked=True))


class SessionEscalationTests(unittest.TestCase):
    """GeminiLiveSession starts the task itself and queues the notice (desktop, phone and text share it)."""

    def session(self, verdict):
        from omarchy_ai.config import Config
        from omarchy_ai.voice.gemini_live import GeminiLiveSession
        with patch("omarchy_ai.voice.gemini_live.EchoCancellation"):
            s = GeminiLiveSession(Config())
        s._transcript = [{"role": "user", "text": "open an issue on Omarchy about the Wi-Fi freeze"}]
        s._switchboard = SimpleNamespace(review=lambda tool, args, ctx: verdict)
        s._escalator = Escalator(judge())  # no network in tests
        return s

    def test_second_failure_starts_a_task_and_announces_it(self):
        s = self.session(switchboard.Verdict("execute", "browser_task", {}))
        started = ActionResult(True, json.dumps({"task_id": "20260927-000000-abc123", "status": "started"}))

        def act(name, args):
            return started if name == "start_task" else ActionResult(False, "Browser task stopped after 2 actions")

        async def scenario():
            session = SimpleNamespace(send_tool_response=AsyncMock())
            with patch("omarchy_ai.voice.gemini_live.run_action", side_effect=act) as run, \
                    patch("omarchy_ai.voice.gemini_live.LiveSession._current_window", return_value={}):
                for n in range(2):
                    await s._run_call(session, SimpleNamespace(id=f"c{n}", name="browser_task", args={"url": "x"}))
                await asyncio.gather(*list(s._bg_tasks))
            return run

        run = asyncio.run(scenario())
        goals = [c.args[1]["goal"] for c in run.call_args_list if c.args[0] == "start_task"]
        self.assertEqual(len(goals), 1)
        self.assertIn("open an issue on Omarchy", goals[0])
        self.assertIn("20260927-000000-abc123", s._announcements.get_nowait()["notice"])

    def test_the_self_check_rides_on_her_next_tool_result(self):
        s = self.session(switchboard.Verdict("execute", "list_files", {}))
        s._escalator = Escalator(judge(), disputes=lambda reply, latest: True)
        s._escalator.observe_call("list_files", {"path": "."}, True, "[]", time.monotonic())
        s._transcript = [{"role": "user", "text": "find the folder"}, {"role": "assistant", "text": "It is not there."},
                         {"role": "user", "text": "that cannot be right"}]

        async def scenario():
            session = SimpleNamespace(send_tool_response=AsyncMock())
            with patch("omarchy_ai.voice.gemini_live.run_action", return_value=ActionResult(True, "[]")), \
                    patch("omarchy_ai.voice.gemini_live.LiveSession._current_window", return_value={}):
                await s._check_dispute()
                for n in range(2):
                    await s._run_call(session, SimpleNamespace(id=f"c{n}", name="list_files", args={"path": "."}))
            return [c.kwargs["function_responses"].response["message"] for c in session.send_tool_response.call_args_list]

        first, second = asyncio.run(scenario())
        self.assertIn("automatic self-check", first)
        self.assertNotIn("automatic self-check", second)

    def test_a_reroute_to_start_task_counts_as_delegating(self):
        s = self.session(switchboard.Verdict("reroute", "start_task", {"goal": "g"}, message="routed"))
        s._escalator.observe_call("browser_task", {}, False, "stopped", 0.0)

        async def scenario():
            session = SimpleNamespace(send_tool_response=AsyncMock())
            with patch("omarchy_ai.voice.gemini_live.run_action", return_value=ActionResult(True, "{}")), \
                    patch("omarchy_ai.voice.gemini_live.LiveSession._current_window", return_value={}):
                await s._run_call(session, SimpleNamespace(id="c1", name="terminal_task", args={}))

        asyncio.run(scenario())
        self.assertIsNone(s._escalator.due(1.0, ["go"], "I can't"))


STARTED = ActionResult(True, json.dumps({"task_id": "20261012-090000-abc123", "status": "started"}))
SKILL = {"name": "water-plants", "description": "Water the plants in rounds.", "body": "1. Open the valve.",
         "note": "This only read the skill: nothing has been run."}


class SessionClaimTests(unittest.TestCase):
    """The turn check behind the unbacked-claim guard, and reading a skill
    that was meant to be run. Typed chat and phone calls share _receive."""

    REQUEST = "water the plants every minute for five minutes"

    def session(self, decide, reply="On it, starting the countdown.", running=False):
        from omarchy_ai.config import Config
        from omarchy_ai.voice.gemini_live import GeminiLiveSession
        with patch("omarchy_ai.voice.gemini_live.EchoCancellation"):
            s = GeminiLiveSession(Config())
        s.CLAIM_SETTLE_SECONDS = 0
        s._task_running = lambda: running  # no task store in tests
        s._transcript = [{"role": "user", "text": self.REQUEST}] + ([{"role": "assistant", "text": reply}] if reply else [])
        s._switchboard = SimpleNamespace(review=lambda tool, args, ctx: switchboard.Verdict("execute", tool, dict(args)))
        s._escalator = Escalator(judge(), unbacked=decide)
        return s

    def turn(self, s, before=None, act=lambda name, args: STARTED):
        """Her turn completes; returns the goals of the tasks that were started."""
        async def scenario():
            session = SimpleNamespace(send_tool_response=AsyncMock())
            with patch("omarchy_ai.voice.gemini_live.run_action", side_effect=act) as run, \
                    patch("omarchy_ai.voice.gemini_live.LiveSession._current_window", return_value={}):
                if before:
                    await before(session)
                s._maybe_check_claim()
                while s._bg_tasks:
                    await asyncio.gather(*list(s._bg_tasks))
            return [c.args[1]["goal"] for c in run.call_args_list if c.args[0] == "start_task"]
        return asyncio.run(scenario())

    def test_a_reply_that_says_it_started_with_no_call_starts_the_task(self):
        decide = claims()
        s = self.session(decide)
        goals = self.turn(s)
        self.assertEqual(len(goals), 1)
        self.assertIn(self.REQUEST, goals[0])
        self.assertIn("no tool that does it ran", goals[0])
        notice = s._announcements.get_nowait()["notice"]
        self.assertIn("20261012-090000-abc123", notice)
        self.assertIn("no tool ran", notice)
        self.assertEqual(self.turn(s), [])  # each user turn is judged once
        self.assertEqual(len(decide.calls), 1)

    def test_a_turn_that_only_read_is_put_to_jev_and_an_answer_starts_nothing(self):
        decide = claims(False, claims_action=0.05, asks_action=0.2)
        s = self.session(decide, reply="")
        s._transcript = [{"role": "user", "text": "what is the battery level?"}]

        async def read(session):
            await s._run_call(session, SimpleNamespace(id="c1", name="battery_status", args={}))
            s._transcript.append({"role": "assistant", "text": "It is at 73 percent."})

        goals = self.turn(s, read, act=lambda name, args: ActionResult(True, "73%"))
        self.assertEqual(goals, [])
        self.assertEqual(decide.calls, [("It is at 73 percent.", "what is the battery level?")])  # asked, not assumed
        self.assertTrue(s._announcements.empty())

    def test_a_turn_that_only_read_and_then_claims_is_handed_over(self):
        s = self.session(claims(), reply="")

        async def read(session):
            await s._run_call(session, SimpleNamespace(id="c1", name="list_skills", args={}))
            s._transcript.append({"role": "assistant", "text": "On it, starting the countdown."})

        goals = self.turn(s, read, act=lambda name, args: STARTED if name == "start_task" else ActionResult(True, "[]"))
        self.assertEqual(len(goals), 1)
        self.assertIn("list_skills(", goals[0])

    def test_a_turn_in_which_a_tool_acted_is_not_judged(self):
        decide = claims()
        s = self.session(decide, reply="")

        async def act_first(session):
            await s._run_call(session, SimpleNamespace(id="c1", name="set_volume", args={"level": 40}))
            s._transcript.append({"role": "assistant", "text": "Done, the volume is at 40."})

        self.assertEqual(self.turn(s, act_first, act=lambda name, args: ActionResult(True, "volume 40")), [])
        self.assertEqual(decide.calls, [])
        # A call Jev sent back did nothing: saying "done" after it is a claim.
        s = self.session(decide, reply="")
        s._switchboard = SimpleNamespace(review=lambda tool, args, ctx: switchboard.Verdict(
            "reject", tool, dict(args), message="Not run: Jev checked set_volume and the user did not ask for this."))

        async def sent_back(session):
            await s._run_call(session, SimpleNamespace(id="c2", name="set_volume", args={"level": 40}))
            s._transcript.append({"role": "assistant", "text": "Done, I started the watering."})

        self.assertEqual(len(self.turn(s, sent_back)), 1)

    def test_turns_that_are_not_an_answer_to_the_user_are_not_judged(self):
        decide = claims()
        cases = {
            "nothing said yet": lambda s: s._transcript.pop(),
            "a call is still running": lambda s: s._pending_calls.add("c9"),
            "a background call is still working": lambda s: s._working.update(c9={}),
            "an automatic prompt was answered": lambda s: setattr(s, "_claim_checked", s._user_mark()),
            "the fast path acted": lambda s: setattr(s, "_jev_done", ("set_volume", {}, time.monotonic(), "ok", "t")),
            "the fast path handed it over": lambda s: setattr(s, "_proactive_task_turn", self.REQUEST),
            "she started a task herself": lambda s: s._escalator.observe_call("start_task", {}, True, "{}",
                                                                                 time.monotonic()),
            "no user words at all": lambda s: s._transcript.pop(0),
        }
        for name, arrange in cases.items():
            s = self.session(decide)
            arrange(s)
            self.assertEqual(self.turn(s), [], name)
        s = self.session(decide, running=True)  # a task of this conversation is at work
        self.assertEqual(self.turn(s), [])
        self.assertEqual(decide.calls, [])

    def test_an_interrupted_turn_is_not_judged_and_a_call_that_ends_later_gets_its_own_check(self):
        decide = claims()
        s = self.session(decide)

        async def interrupted():
            s._maybe_check_claim(True)
            return list(s._bg_tasks)
        self.assertEqual(asyncio.run(interrupted()), [])
        s._pending_calls.add("c1")
        self.assertEqual(self.turn(s), [])  # still running: not judged, and not used up
        s._pending_calls.clear()
        self.assertEqual(len(self.turn(s)), 1)

    def test_typed_and_spoken_turns_reach_the_check_through_receive(self):
        from google.genai import types
        decide = claims()
        s = self.session(decide, reply="")  # the typed line is in the transcript, as text_chat and the phone put it

        async def scenario():
            async def receive():
                yield types.LiveServerMessage(server_content=types.LiveServerContent(
                    output_transcription=types.Transcription(text="On it, starting the countdown.")))
                yield types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True))
                s._hangup.set()
            with patch("omarchy_ai.voice.gemini_live.run_action", return_value=STARTED) as run:
                await s._receive(SimpleNamespace(receive=receive))
                while s._bg_tasks:
                    await asyncio.gather(*list(s._bg_tasks))
            return [c.args[1]["goal"] for c in run.call_args_list if c.args[0] == "start_task"]

        goals = asyncio.run(scenario())
        self.assertEqual(len(goals), 1)
        self.assertEqual(decide.calls, [("On it, starting the countdown.", self.REQUEST)])

    def read_skill(self, wanted, call=None):
        decide = claims()
        s = self.session(decide, reply="")
        call = call or SimpleNamespace(id="c1", name="use_tool", args={"name": "load_skill", "args": {"name": "water-plants"}})
        ran = []

        def act(name, args):
            ran.append((name, args))
            return STARTED if name == "run_skill" else ActionResult(True, json.dumps(SKILL))

        async def scenario():
            session = SimpleNamespace(send_tool_response=AsyncMock())
            with patch("omarchy_ai.voice.gemini_live.run_action", side_effect=act), \
                    patch("omarchy_ai.voice.gemini_live.LiveSession._current_window", return_value={}), \
                    patch("omarchy_ai.core.skills.wants_run", return_value=wanted) as wants:
                await s._run_call(session, call)
            return session.send_tool_response.call_args.kwargs["function_responses"].response, wants
        response, wants = asyncio.run(scenario())
        return s, ran, response, wants, decide

    def test_reading_a_skill_the_user_asked_to_run_starts_it(self):
        s, ran, response, wants, decide = self.read_skill(True)
        self.assertEqual(ran, [("load_skill", {"name": "water-plants"}),
                               ("run_skill", {"name": "water-plants", "request": self.REQUEST})])
        self.assertEqual(wants.call_args.args[:2], (self.REQUEST, {k: SKILL[k] for k in ("name", "description", "body")}))
        self.assertTrue(response["ok"])
        self.assertIn("20261012-090000-abc123", response["message"])
        self.assertNotIn("Open the valve", response["message"])
        # It counts as a task she started: nothing escalates the same job.
        self.assertFalse(s._escalator.claimable(time.monotonic()))
        s._transcript.append({"role": "assistant", "text": "It has started."})
        self.assertEqual(self.turn(s), [])
        self.assertEqual(decide.calls, [])

    def test_reading_a_skill_to_know_what_it_says_only_reads_it(self):
        s, ran, response, wants, _ = self.read_skill(False)
        self.assertEqual([name for name, _ in ran], ["load_skill"])
        self.assertIn("Open the valve", response["message"])
        self.assertIn("nothing has been run", response["message"])
        self.assertTrue(s._escalator.claimable(time.monotonic()))

    def test_find_skill_passes_its_resolved_request_on_and_no_match_runs_nothing(self):
        found = {"skill": "water-plants", "description": SKILL["description"], "instructions": SKILL["body"]}
        call = SimpleNamespace(id="c1", name="find_skill", args={"request": "water the garden plants now"})
        with patch.dict(SKILL, found, clear=True):
            _, ran, response, wants, _ = self.read_skill(True, call)
        self.assertEqual(ran[-1], ("run_skill", {"name": "water-plants", "request": "water the garden plants now"}))
        self.assertEqual(wants.call_args.kwargs, {"asked": "water the garden plants now"})
        with patch.dict(SKILL, {"skill": None, "reason": "No saved skill does this."}, clear=True):
            _, ran, response, wants, _ = self.read_skill(True, call)
        self.assertEqual([name for name, _ in ran], ["find_skill"])
        wants.assert_not_called()


if __name__ == "__main__":
    unittest.main()
