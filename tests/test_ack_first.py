"""DoD-7 in a simulated live session (voice/gemini_live.py + core/outbox.py):
a slow request is acknowledged within 1.5 s (p95), once, never on top of her
own words, and a background result she does not speak is delivered once.

Live problems behind it (2026-10-18): "check my last 4 emails" was first
answered 15-25 s later; the fan turned on and she said nothing more."""

from __future__ import annotations

import asyncio
import statistics
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import no_live_desktop  # noqa: F401 -- never a real desktop command
from fakes.effects_journal import isolate_journal
from google.genai import types
from omarchy_ai.config import Config
from omarchy_ai.core import outbox
from omarchy_ai.execution import runner
from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.voice import fallback, switchboard
from omarchy_ai.voice.gemini_live import GeminiLiveSession

setUpModule, tearDownModule = isolate_journal()

CLIP = b"\x01\x00" * 480  # 20 ms of "acknowledgement"


class Simulated(unittest.IsolatedAsyncioTestCase):
    """A session with a fake connection, a fake (slow) tool and the clip stubbed."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        outbox.reset_for_tests()
        self.addCleanup(outbox.reset_for_tests)
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        for target in (
            (patch.object(outbox, "LEDGER_PATH", Path(temp.name) / "outbox.json")),
            (patch.object(fallback, "ack_pcm", lambda rate=24000: CLIP)),
            (
                patch.object(
                    switchboard.Switchboard,
                    "review",
                    lambda self, tool, args, ctx: switchboard.Verdict(
                        "execute", tool, dict(args)
                    ),
                )
            ),
            (patch("omarchy_ai.voice.gemini_live.run_action", side_effect=self.action)),
            (
                patch(
                    "omarchy_ai.voice.gemini_live.LiveSession._current_window",
                    lambda: {},
                )
            ),
            (
                patch(
                    "omarchy_ai.voice.gemini_live.user_tools.exists", lambda name: False
                )
            ),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.sessions: list[GeminiLiveSession] = []

    def action(self, name, args):
        if args.get("slow"):
            self.release.wait(10)
        return ActionResult(True, f"{name} done")

    def session(self, typed=False):
        """typed: the text chat's workers (voice/text_chat.py): no _task_status,
        result delivery alone, and the server's messages through _receive."""
        s = GeminiLiveSession(Config())
        s.ack_silent_work = not typed  # where the clip is heard (desktop, phone)
        s._last_user_speech = 0.0 if typed else time.monotonic()
        server: asyncio.Queue = asyncio.Queue()

        async def receive():
            while True:
                yield await server.get()

        conn = SimpleNamespace(
            send_tool_response=AsyncMock(),
            send_client_content=AsyncMock(),
            receive=receive,
            server=server,
        )
        workers = [asyncio.create_task(s._tools(conn))]
        if typed:

            async def drain():  # the chat plays no audio (TextChat._drain_audio)
                while True:
                    await s._audio.get()

            workers += [
                asyncio.create_task(s._result_watch(conn)),
                asyncio.create_task(s._receive(conn)),
                asyncio.create_task(drain()),
            ]
        else:
            workers.append(asyncio.create_task(s._task_status(conn)))
        self.sessions.append(s)

        async def stop():
            s._hangup.set()
            for w in workers:
                w.cancel()
            self.release.set()
            await asyncio.gather(*workers, *s._bg_tasks, return_exceptions=True)
            runner.default_runner().cancel(owner=s)

        self.addAsyncCleanup(stop)
        return s, conn

    @staticmethod
    def call(s, call_id, name, **args):
        s._calls.put_nowait(types.FunctionCall(id=call_id, name=name, args=args))

    @staticmethod
    def clips(s):
        return [data for _, data in list(s._audio._queue)]  # type: ignore[attr-defined]

    async def until(self, check, timeout=3.0):
        end = time.monotonic() + timeout
        while not check() and time.monotonic() < end:
            await asyncio.sleep(0.01)
        return check()


class AckFirstTests(Simulated):
    async def test_a_slow_request_is_acknowledged_within_1_5_s_p95(self):
        latencies = []

        async def one(i):
            s, _ = self.session()
            started = time.monotonic()
            self.call(
                s, f"g{i}", "myapi_gmail_search", query="newer_than:7d", slow=True
            )
            self.assertTrue(await self.until(lambda: not s._audio.empty(), timeout=5))
            latencies.append(time.monotonic() - started)

        await asyncio.gather(*(one(i) for i in range(20)))
        latencies.sort()
        p95 = latencies[int(0.95 * len(latencies)) - 1]
        print(
            f"\nDoD-7 ack latency over {len(latencies)} sessions: p50={statistics.median(latencies):.2f}s "
            f"p95={p95:.2f}s max={latencies[-1]:.2f}s"
        )
        self.assertLessEqual(p95, outbox.ACK_WITHIN_SECONDS)
        for s in self.sessions:
            self.assertEqual(self.clips(s), [CLIP], "exactly one acknowledgement each")

    async def test_no_ack_when_she_already_spoke_for_the_request(self):
        s, _ = self.session()
        self.call(s, "b1", "browser_task", request="find the train times", slow=True)
        await asyncio.sleep(0.15)
        s._last_audio_at = time.monotonic()  # "על זה" / "on it", in her own voice
        await asyncio.sleep(1.2)
        self.assertEqual(self.clips(s), [])
        notes = [a["channels"] for a in outbox.audit() if a["kind"] == outbox.ACK]
        self.assertEqual(notes, [["model"]])

    async def test_one_ack_per_request_even_for_two_slow_calls(self):
        s, _ = self.session()
        self.call(s, "t1", "start_task", goal="write the report", slow=True)
        self.call(s, "t2", "myapi_call", service="calendar", slow=True)
        await asyncio.sleep(1.3)
        self.assertEqual(self.clips(s), [CLIP])

    async def test_a_quick_call_is_not_acknowledged_by_the_clip(self):
        s, _ = self.session()
        self.call(s, "v1", "volume_up", slow=True)
        await asyncio.sleep(1.0)
        self.assertEqual(self.clips(s), [])

    async def test_typed_requests_get_no_spoken_acknowledgement(self):
        s, _ = self.session()
        s._last_user_speech = 0.0  # the text chat: no words spoken aloud
        self.call(s, "g1", "myapi_gmail_search", query="x", slow=True)
        await asyncio.sleep(1.0)
        self.assertEqual(self.clips(s), [])


class ResultDeliveryTests(Simulated):
    def setUp(self):
        super().setUp()
        patcher = patch.object(GeminiLiveSession, "RESULT_UNSPOKEN_SECONDS", 0.3)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def notices(conn):
        return [
            c
            for c in conn.send_client_content.call_args_list
            if "Automatic result notice" in c.kwargs["turns"].parts[0].text
        ]

    async def test_an_unspoken_background_result_is_delivered_once(self):
        s, conn = self.session()
        s._last_user_speech = time.monotonic() - 10  # her acknowledgement is long over
        self.call(s, "u1", "use_tool", request="turn on the fan")
        with patch.object(
            s,
            "_pick",
            AsyncMock(
                return_value=SimpleNamespace(
                    run=True,
                    tool="smart_home",
                    args={"what": "fan on"},
                    picked=True,
                    evidence={},
                )
            ),
        ):
            self.assertTrue(
                await self.until(lambda: conn.send_tool_response.await_count == 1)
            )
            await asyncio.sleep(1.2)
        notices = self.notices(conn)
        self.assertEqual(len(notices), 1, "delivered, and only once")
        text = notices[0].kwargs["turns"].parts[0].text
        self.assertIn("turn on the fan", text)
        self.assertTrue(notices[0].kwargs["turn_complete"])

    async def test_a_result_she_speaks_is_not_delivered_again(self):
        s, conn = self.session()
        s._last_user_speech = time.monotonic() - 10
        self.call(s, "b1", "browser_task", request="open the train times")
        self.assertTrue(
            await self.until(lambda: conn.send_tool_response.await_count == 1)
        )
        s._words.append(
            (time.monotonic() + 0.05, "The train times are open.")
        )  # she tells the user
        await asyncio.sleep(0.8)
        self.assertEqual(self.notices(conn), [])

    async def test_a_lookup_that_feeds_her_next_call_is_not_reported(self):
        s, conn = self.session()
        s._last_user_speech = time.monotonic() - 10
        self.call(s, "l1", "list_commands", request="anything")
        self.assertTrue(
            await self.until(lambda: conn.send_tool_response.await_count == 1)
        )
        await asyncio.sleep(0.8)
        self.assertEqual(self.notices(conn), [])


def server_message(text=None, audio=b"", turn_complete=False):
    """One Live server message: her audio and output transcription, maybe the turn's end."""
    parts = [SimpleNamespace(inline_data=SimpleNamespace(data=audio))] if audio else []
    content = SimpleNamespace(
        input_transcription=None,
        output_transcription=SimpleNamespace(text=text) if text is not None else None,
        interrupted=False,
        turn_complete=turn_complete,
        model_turn=SimpleNamespace(parts=parts) if parts else None,
    )
    return SimpleNamespace(
        tool_call_cancellation=None, tool_call=None, server_content=content
    )


class EmptyTurnTests(Simulated):
    """Live, typed chat 2026-10-18 14:08:58-14:09:30: "Turn on the backyard
    fan"; the result came back at 14:09:07, her next turn completed at
    14:09:10 with no words, and the result was never told."""

    notices = staticmethod(ResultDeliveryTests.notices)

    def setUp(self):
        super().setUp()
        patcher = patch.object(GeminiLiveSession, "RESULT_UNSPOKEN_SECONDS", 0.3)
        patcher.start()
        self.addCleanup(patcher.stop)
        for target in ("_maybe_check_claim", "_maybe_escalate"):
            patcher = patch.object(GeminiLiveSession, target, lambda self, *a: None)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def fan(self, typed):
        s, conn = self.session(typed=typed)
        s._last_user_speech = 0.0 if typed else time.monotonic() - 10
        self.call(s, "fan", "use_tool", request="Turn on the backyard fan")
        with patch.object(
            s,
            "_pick",
            AsyncMock(
                return_value=SimpleNamespace(
                    run=True,
                    tool="smart_home",
                    args={"what": "fan on"},
                    picked=True,
                    evidence={},
                )
            ),
        ):
            self.assertTrue(
                await self.until(lambda: conn.send_tool_response.await_count == 1)
            )
        return s, conn

    async def test_an_empty_turn_after_the_result_is_not_her_telling_it(self):
        for typed in (True, False):
            with self.subTest(typed=typed):
                s, conn = await self.fan(typed)
                if typed:
                    conn.server.put_nowait(
                        server_message(audio=b"\0\0" * 10, turn_complete=True)
                    )
                else:
                    s._last_audio_at = s._last_turn_done_at = (
                        time.monotonic()
                    )  # audio, no words
                await asyncio.sleep(1.0)
                self.assertEqual(len(self.notices(conn)), 1, "prompted once")
                await asyncio.sleep(0.5)
                self.assertEqual(len(self.notices(conn)), 1, "and only once")

    async def test_her_words_after_the_result_are_hers_to_tell(self):
        s, conn = await self.fan(typed=True)
        conn.server.put_nowait(
            server_message(text="The fan is on.", turn_complete=True)
        )
        await asyncio.sleep(1.0)
        self.assertEqual(self.notices(conn), [])
        self.assertGreater(s._last_words_at, 0)

    async def test_an_acknowledgement_said_after_the_result_is_not_telling_it(self):
        # Live, typed chat 2026-10-18 18:18:07-18:18:17: the call, the result at
        # 18:18:15.9, then her only words, the acknowledgement, and her turn's end.
        for ack, phrase in (("על ", None), ("Right ", "Right away!")):
            with (
                self.subTest(phrase=phrase),
                patch.object(outbox, "ack_phrase", return_value=phrase),
            ):
                s, conn = await self.fan(typed=True)
                rest = "זה!" if phrase is None else "away!"
                conn.server.put_nowait(server_message(text=ack))
                conn.server.put_nowait(
                    server_message(text=rest, audio=b"\0\0", turn_complete=True)
                )
                await asyncio.sleep(1.0)
                self.assertEqual(len(self.notices(conn)), 1, "prompted once")
                await asyncio.sleep(0.5)
                self.assertEqual(len(self.notices(conn)), 1, "and only once")

    async def test_an_acknowledgement_followed_by_the_result_is_telling_it(self):
        s, conn = await self.fan(typed=True)
        conn.server.put_nowait(server_message(text="על זה! "))
        conn.server.put_nowait(
            server_message(text="הדלקתי את המאוורר בחצר.", turn_complete=True)
        )
        await asyncio.sleep(1.0)
        self.assertEqual(self.notices(conn), [])

    async def test_blank_transcription_is_not_words(self):
        s, conn = await self.fan(typed=True)
        conn.server.put_nowait(
            server_message(text="  ", audio=b"\0\0", turn_complete=True)
        )
        await asyncio.sleep(1.0)
        self.assertEqual(len(self.notices(conn)), 1)


if __name__ == "__main__":
    unittest.main()
