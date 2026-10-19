"""Claim before result (voice/gemini_live.py, execution/runner.py): her
words while a call that acts is still running are not its result.

Live 2026-10-17 20:42: "turn on good night"; asked to acknowledge, she said
"on it! I turned on the goodnight routine" 3 s after the call started; the
call failed ("couldn't understand") 8 s later, a retry reported no change
(`{"ok": true, "changed": []}`), and the user was never told. 2026-10-18
18:45: "living room and hallway"; "I turned on the lights" 2 s in; the call
failed 5 s later; ~12 more calls, two of them "changed": [], and the user was
never told what really happened."""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import no_live_desktop  # noqa: F401 -- never a real desktop command
from omarchy_ai.execution import runner
from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.voice import escalation
from omarchy_ai.voice.gemini_live import GeminiLiveSession
from test_ack_first import Simulated

NO_CHANGE = '{"ok": true, "changed": []}'
FAILED = '{"error": "Sorry, I could not understand that", "done": false}'
CLAIM = "על זה! הדלקתי את האורות בסלון ובמסדרון."  # "on it! I turned on the lights"


class ClaimBeforeResult(Simulated):
    def setUp(self):
        super().setUp()
        self.results: list[ActionResult] = []
        self.gates: list[threading.Event] = []
        self.told_not_done = False
        for target in (
            patch.object(GeminiLiveSession, "RESULT_UNSPOKEN_SECONDS", 0.3),
            patch.object(
                GeminiLiveSession, "_maybe_check_claim", lambda self, *a: None
            ),
            patch.object(GeminiLiveSession, "_maybe_escalate", lambda self, *a: None),
            patch.object(
                escalation, "jev_told_not_done", lambda reply: self.told_not_done
            ),
        ):
            target.start()
            self.addCleanup(target.stop)

    def action(self, name, args):
        gate = self.gates.pop(0) if self.gates else None
        if gate is not None:
            gate.wait(10)
        return (
            self.results.pop(0) if self.results else ActionResult(True, f"{name} done")
        )

    async def request(self, *, typed=True):
        s, conn = self.session(typed=typed)
        # The user's words are over: nothing here is waiting on them.
        s._last_user_speech = time.monotonic() - (0 if typed else 40)
        if typed:
            s._last_user_speech = 0.0
        pick = patch.object(
            s,
            "_pick",
            AsyncMock(
                return_value=SimpleNamespace(
                    run=True,
                    tool="smart_home",
                    args={"what": "lights on"},
                    picked=True,
                    evidence={},
                )
            ),
        )
        pick.start()
        self.addCleanup(pick.stop)
        return s, conn

    def start(self, s, call_id, held=True):
        gate = threading.Event()
        self.gates.append(gate if held else None)  # type: ignore[arg-type]
        self.call(
            s, call_id, "use_tool", request="turn on the living room and hallway lights"
        )
        return gate

    @staticmethod
    def say(s, text):
        s._last_words_at = time.monotonic()
        s._words.append((s._last_words_at, text))

    @staticmethod
    def prompts(conn, marker):
        return [
            c.kwargs["turns"].parts[0].text
            for c in conn.send_client_content.call_args_list
            if marker in c.kwargs["turns"].parts[0].text
        ]

    def told(self, conn):
        return self.prompts(conn, "Automatic correction") + self.prompts(
            conn, "Automatic result notice"
        )

    async def answered(self, conn, n):
        self.assertTrue(
            await self.until(lambda: conn.send_tool_response.await_count >= n)
        )

    async def test_a_claim_then_a_failure_is_corrected_once(self):
        s, conn = await self.request()
        self.results.append(ActionResult(False, FAILED))
        gate = self.start(s, "u1")
        await asyncio.sleep(0.15)
        self.say(s, CLAIM)  # t+3s in the journal
        await asyncio.sleep(0.15)
        gate.set()  # the failure, t+7s
        await self.answered(conn, 1)
        self.assertTrue(await self.until(lambda: self.told(conn)))
        await asyncio.sleep(0.8)
        told = self.told(conn)
        self.assertEqual(len(told), 1, "exactly one telling for the request")
        self.assertIn("Automatic correction", told[0])
        self.assertIn("did not work", told[0])
        self.assertTrue(
            conn.send_client_content.call_args_list[-1].kwargs["turn_complete"]
        )

    async def test_a_claim_a_failure_then_a_success_reports_the_real_outcome_once(self):
        s, conn = await self.request()
        self.results += [
            ActionResult(False, FAILED),
            ActionResult(True, "Turned on 2 lights."),
        ]
        gate = self.start(s, "u1")
        await asyncio.sleep(0.1)
        self.say(s, CLAIM)
        gate.set()
        await self.answered(conn, 1)
        self.start(s, "u2", held=False)  # her retry, at once
        await self.answered(conn, 2)
        self.assertTrue(await self.until(lambda: self.told(conn)))
        await asyncio.sleep(0.8)
        told = self.told(conn)
        self.assertEqual(len(told), 1)
        self.assertIn("Automatic result notice", told[0])
        self.assertIn("a later call", told[0])
        self.assertEqual(self.prompts(conn, "Automatic correction"), [])

    async def test_the_lights_replay_failure_then_no_change_is_told_honestly(self):
        s, conn = await self.request()
        self.results += [
            ActionResult(False, FAILED),
            ActionResult(True, NO_CHANGE),
            ActionResult(True, NO_CHANGE),
        ]
        gate = self.start(s, "u1")
        await asyncio.sleep(0.1)
        self.say(s, CLAIM)
        gate.set()
        await self.answered(conn, 1)
        self.start(s, "u2", held=False)
        await self.answered(conn, 2)
        self.start(s, "u3", held=False)
        await self.answered(conn, 3)
        # What she reads of a "changed": [] result says it confirms nothing.
        sent = (
            conn.send_tool_response.call_args_list[-1]
            .kwargs["function_responses"]
            .response
        )
        self.assertTrue(sent["ok"])
        self.assertIn(runner.NO_CHANGE, sent["message"])
        self.assertTrue(await self.until(lambda: self.told(conn)))
        await asyncio.sleep(0.8)
        told = self.told(conn)
        self.assertEqual(len(told), 1)
        self.assertIn("reported no change", told[0])
        self.assertIn("already was so", told[0])

    async def test_no_change_after_a_claim_is_corrected_even_if_she_says_done_again(
        self,
    ):
        s, conn = await self.request()
        self.results.append(ActionResult(True, NO_CHANGE))
        gate = self.start(s, "u1")
        await asyncio.sleep(0.1)
        self.say(s, CLAIM)
        gate.set()
        await self.answered(conn, 1)
        self.say(
            s, "הפעלתי את אוטומציית הלילה טוב."
        )  # "I activated the goodnight routine", again
        self.assertTrue(await self.until(lambda: self.told(conn)))
        await asyncio.sleep(0.6)
        self.assertEqual(len(self.told(conn)), 1)

    async def test_no_correction_when_she_already_told_it_did_not_work(self):
        s, conn = await self.request()
        self.told_not_done = True
        self.results.append(ActionResult(False, FAILED))
        gate = self.start(s, "u1")
        await asyncio.sleep(0.1)
        self.say(s, CLAIM)
        gate.set()
        await self.answered(conn, 1)
        self.say(s, "Sorry, that did not work: the service did not understand.")
        await asyncio.sleep(1.2)
        self.assertEqual(self.told(conn), [])

    async def test_no_correction_when_the_result_bore_her_out(self):
        s, conn = await self.request()
        self.results.append(ActionResult(True, "Turned on 2 lights."))
        gate = self.start(s, "u1")
        await asyncio.sleep(0.1)
        self.say(s, CLAIM)
        gate.set()
        await self.answered(conn, 1)
        await asyncio.sleep(1.2)
        self.assertEqual(
            self.told(conn), [], "her words were true: nothing to correct or repeat"
        )

    async def test_no_claim_before_the_result_leaves_result_delivery_as_it_was(self):
        s, conn = await self.request()
        self.results.append(ActionResult(False, FAILED))
        gate = self.start(s, "u1")
        await asyncio.sleep(0.1)
        gate.set()
        await self.answered(conn, 1)
        self.assertTrue(await self.until(lambda: self.told(conn)))
        await asyncio.sleep(0.6)
        told = self.told(conn)
        self.assertEqual(len(told), 1)
        self.assertIn("Automatic result notice", told[0])
        self.assertEqual(self.prompts(conn, "Automatic correction"), [])

    async def test_a_spoken_request_is_corrected_too(self):
        s, conn = await self.request(typed=False)
        self.results.append(ActionResult(False, FAILED))
        gate = self.start(s, "u1")
        await asyncio.sleep(0.1)
        self.say(s, CLAIM)
        gate.set()
        await self.answered(conn, 1)
        self.assertTrue(
            await self.until(lambda: self.prompts(conn, "Automatic correction"))
        )
        await asyncio.sleep(0.6)
        self.assertEqual(len(self.told(conn)), 1)


class AcknowledgementPrompt(Simulated):
    async def test_the_acknowledgement_prompt_says_not_to_claim_it_is_done(self):
        s, conn = self.session()
        s.ack_silent_work = False  # no clip: she is prompted to acknowledge it herself
        self.call(s, "u1", "use_tool", request="turn on the lights", slow=True)
        with patch.object(
            s,
            "_pick",
            AsyncMock(
                return_value=SimpleNamespace(
                    run=True,
                    tool="smart_home",
                    args={"slow": True},
                    picked=True,
                    evidence={},
                )
            ),
        ):
            await asyncio.sleep(0.1)
            s._last_turn_done_at = time.monotonic()
            prompted = await self.until(
                lambda: conn.send_client_content.await_count >= 1, timeout=3
            )
        self.assertTrue(prompted)
        text = conn.send_client_content.call_args_list[0].kwargs["turns"].parts[0].text
        self.assertIn("Automatic status", text)
        self.assertIn("Do not say it is done", text)


class NoChange(unittest.TestCase):
    def test_what_counts_as_no_change(self):
        self.assertTrue(runner.no_change(NO_CHANGE))
        self.assertTrue(runner.no_change('{"changed": []}'))
        for message in (
            '{"ok": true, "changed": ["light.kitchen"]}',
            '{"ok": false, "changed": []}',
            "[]",
            "done",
            '{"state": "on"}',
            "{not json",
        ):
            with self.subTest(message=message):
                self.assertFalse(runner.no_change(message))

    def test_the_note_comes_first_and_only_for_a_call_that_acts(self):
        tool_runner = runner.ToolRunner()
        acted = tool_runner.run(
            "volume_up", {}, execute=lambda name, args: ActionResult(True, NO_CHANGE)
        )
        self.assertTrue(acted.ok)
        self.assertTrue(acted.message.startswith(runner.NO_CHANGE), acted.message)
        read = tool_runner.run(
            "battery_status",
            {},
            execute=lambda name, args: ActionResult(True, NO_CHANGE),
        )
        self.assertNotIn(runner.NO_CHANGE, read.message)


if __name__ == "__main__":
    unittest.main()
