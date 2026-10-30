"""A house action still running when its conversation ends finishes, and is
reported (execution/runner.py ToolRunner.release)."""

import asyncio
import threading
import unittest

import no_live_desktop  # noqa: F401 -- run alone too: no real notification, push or desktop command

from omarchy_ai.execution import provenance, runner
from omarchy_ai.execution.actions.base import ActionResult


class ReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_released_call_runs_on_uncancelled_and_reports_how_it_ended(self):
        tool_runner, owner = runner.ToolRunner(), object()
        go, cancelled, reported = threading.Event(), [], []
        ended = threading.Event()

        def execute(name, args):
            runner.CURRENT.get().on_cancel(lambda: cancelled.append(name))
            go.wait(5)
            return ActionResult(True, "closed")

        def on_late(name, args, result):
            reported.append((name, args, result.ok, provenance.unfence(result.message).strip()))
            ended.set()

        waiting = asyncio.create_task(tool_runner.arun("garage_tool", {"operation": "call"}, execute=execute,
                                                       owner=owner, deadline=30))
        await asyncio.sleep(0.05)
        released = tool_runner.release(owner=owner, keep=lambda name, args: args.get("operation") == "call",
                                       on_late=on_late)
        self.assertEqual(released, 1)
        self.assertEqual(tool_runner.cancel(owner=owner, reason="the conversation ended"), 0)  # no longer its owner's
        waiting.cancel()                                     # the conversation's tasks are cancelled
        await asyncio.gather(waiting, return_exceptions=True)
        go.set()
        self.assertTrue(await asyncio.to_thread(ended.wait, 5))
        self.assertEqual(cancelled, [])
        self.assertEqual(reported, [("garage_tool", {"operation": "call"}, True, "closed")])

    async def test_a_call_not_kept_is_cancelled_as_before(self):
        tool_runner, owner, go = runner.ToolRunner(), object(), threading.Event()

        def execute(name, args):
            go.wait(5)
            return ActionResult(True, "x")

        waiting = asyncio.create_task(tool_runner.arun("status_tool", {"operation": "status"}, execute=execute,
                                                       owner=owner, deadline=30))
        await asyncio.sleep(0.05)
        self.assertEqual(tool_runner.release(owner=owner, keep=lambda name, args: False, on_late=None), 0)
        self.assertEqual(tool_runner.cancel(owner=owner, reason="the conversation ended"), 1)
        result = await waiting
        go.set()
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()


class HeardBetterTests(unittest.TestCase):
    """2026-10-28: "turn it off" misheard; the switchboard sent the light back eleven times."""

    def test_a_resent_reversible_house_call_runs_but_not_the_first_or_an_irreversible_one(self):
        from unittest.mock import patch
        from omarchy_ai.config import Config
        from omarchy_ai.voice import gemini_live, switchboard
        session = gemini_live.GeminiLiveSession(Config())
        args = {"operation": "call", "domain": "light", "service": "turn_off", "entity_id": "light.dining_light"}
        reject = switchboard.Verdict("reject", evidence={"gap": "not_asked"})
        with patch.object(gemini_live.user_tools, "exists", return_value=True):
            self.assertFalse(session._heard_better("home_assistant", args, reject))   # the first send
            session._sent_back("home_assistant", args, "Not run")
            self.assertTrue(session._heard_better("home_assistant", args, reject))    # sent again unchanged
            self.assertFalse(session._heard_better("home_assistant", args,
                                                   switchboard.Verdict("reject", evidence={"gap": "values"})))
            with patch.object(gemini_live, "effect_class", return_value="irreversible"):
                self.assertFalse(session._heard_better("home_assistant", args, reject))
