import asyncio
import json
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

    def test_one_failure_and_she_is_still_trying_does_not_escalate(self):
        e = Escalator(judge())
        e.observe_call("browser_task", {}, False, "stopped", 5.0)
        self.assertIsNone(e.due(6.0, ["open an issue"], "Let me try another way."))

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


if __name__ == "__main__":
    unittest.main()
