"""A speaker does not talk on its own when nobody asked it to (live
2026-10-29 21:41: a false wake word on the living-room speaker, then a result
of a request typed on the phone said on the ceiling speakers at night)."""

import asyncio
import datetime
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import no_live_desktop
from omarchy_ai.config import Config
from omarchy_ai.core import outbox
from omarchy_ai.voice import satellite as satellite_module
from omarchy_ai.voice.gemini_live import GeminiLiveSession

setUpModule, tearDownModule = no_live_desktop.without_session_probe()


def at(hhmm: str) -> datetime.datetime:
    return datetime.datetime.combine(datetime.date(2026, 10, 29), datetime.time.fromisoformat(hhmm))


class FakeCall:
    def __init__(self, conversation):
        self._transcript = SimpleNamespace(conversation=conversation)
        self.heard = []

    def announce(self, entry):
        self.heard.append(entry["title"])


class FakeLoop:
    def is_closed(self):
        return False

    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


class ResultGoesToItsOwnConversationTests(unittest.TestCase):
    def setUp(self):
        self.phone, self.speaker = FakeCall("phone-chat"), FakeCall("speaker-chat")
        patcher = patch.dict(outbox._calls, {self.phone: FakeLoop(), self.speaker: FakeLoop()}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_the_call_holding_the_tasks_conversation_hears_it(self):
        self.assertTrue(outbox.announce_to_calls({"title": "done", "for_conversation": "phone-chat"}))
        self.assertEqual(self.phone.heard, ["done"])
        self.assertEqual(self.speaker.heard, [])

    def test_with_its_conversation_closed_or_none_every_call_hears_it(self):
        outbox.announce_to_calls({"title": "gone", "for_conversation": "closed-chat"})
        outbox.announce_to_calls({"title": "reminder"})
        self.assertEqual(self.phone.heard, ["gone", "reminder"])
        self.assertEqual(self.speaker.heard, ["gone", "reminder"])

    def test_a_message_carries_its_chat_to_the_calls(self):
        entry = outbox._entry(outbox.Message(outbox.RESULT, "done", conversation="phone-chat"))
        self.assertEqual(entry["for_conversation"], "phone-chat")
        self.assertNotIn("for_conversation", outbox._entry(outbox.Message(outbox.RESULT, "done")))


class HoldUntilSpokenTests(unittest.TestCase):
    def run_announcer(self, adapter, entries, speak_after=None):
        sent = []

        class FakeLive:
            async def send_client_content(self, turns, turn_complete):
                sent.append(turns.parts[0].text)

        async def call():
            announcer = asyncio.create_task(adapter._announcer(FakeLive()))
            for entry in entries:
                adapter.announce(entry)
            await asyncio.sleep(1.2)
            if speak_after is not None:
                adapter.user_spoke_at = 1.0  # monotonic time of the user's words, before her turn
                await asyncio.sleep(1.2)
            adapter._hangup.set()
            await announcer

        asyncio.run(call())
        return sent

    def entry(self):
        return {"id": "r1", "title": "Open Claude Code", "detail": "Task t1 finished.", "outcome": "certified"}

    def test_a_woken_speaker_says_nothing_until_the_user_speaks(self):
        adapter = GeminiLiveSession(Config(provider="gemini"))
        adapter.hold_until_spoken = True
        self.assertEqual(self.run_announcer(adapter, [self.entry()]), [])
        self.assertEqual(len(adapter._held), 1)

    def test_held_results_are_said_once_the_user_speaks(self):
        adapter = GeminiLiveSession(Config(provider="gemini"))
        adapter.hold_until_spoken = True
        [text] = self.run_announcer(adapter, [self.entry()], speak_after=True)
        self.assertIn("Open Claude Code", text)
        self.assertEqual(adapter._held, [])

    def test_other_conversations_say_it_at_once(self):
        adapter = GeminiLiveSession(Config(provider="gemini"))
        [text] = self.run_announcer(adapter, [self.entry()])
        self.assertIn("Open Claude Code", text)


class QuietHoursTests(unittest.TestCase):
    def test_hours_including_across_midnight(self):
        night = Config(provider="gemini", satellite_quiet_hours="22:00-07:00")
        self.assertTrue(satellite_module.quiet_now(night, at("22:00")))
        self.assertTrue(satellite_module.quiet_now(night, at("03:10")))
        self.assertFalse(satellite_module.quiet_now(night, at("07:00")))
        self.assertFalse(satellite_module.quiet_now(night, at("21:59")))
        nap = Config(provider="gemini", satellite_quiet_hours="13:00-15:00")
        self.assertTrue(satellite_module.quiet_now(nap, at("14:00")))
        self.assertFalse(satellite_module.quiet_now(nap, at("16:00")))

    def test_unset_or_malformed_is_never_quiet(self):
        self.assertFalse(satellite_module.quiet_now(Config(provider="gemini"), at("03:00")))
        with self.assertLogs("omarchy_ai.voice.satellite", "WARNING"):
            self.assertFalse(satellite_module.quiet_now(Config(provider="gemini", satellite_quiet_hours="late"),
                                                        at("03:00")))

    def make(self):
        satellite = satellite_module.Satellite(Config(provider="gemini", jev_fast_path=False,
                                                      satellite_host="speaker.test",
                                                      satellite_quiet_hours="00:00-23:59"))
        satellite._event = lambda *a, **k: None
        self.addCleanup(satellite._pool.shutdown, wait=False)
        satellite._loop = FakeLoop()
        return satellite

    def test_in_quiet_hours_a_speaker_starts_no_conversation_by_itself(self):
        satellite = self.make()
        satellite._phase = "wake"
        started = []
        satellite._begin = lambda **kwargs: started.append(kwargs)
        with patch.dict(satellite_module._running, {"speaker.test": satellite}, clear=True), \
                patch.object(satellite_module, "quiet_now", return_value=True):
            self.assertFalse(satellite_module.announce({"title": "reminder"}), "the caller tells the user elsewhere")
            self.assertFalse(satellite_module.ask({"speaker": "speaker.test", "task_id": "t1"}))
            satellite._announce({"title": "reminder"})
        self.assertEqual(started, [])
        self.assertEqual(satellite._waiting, [])

    def test_a_conversation_the_user_opened_still_gets_it(self):
        satellite = self.make()
        heard = []
        satellite._adapter = SimpleNamespace(_hangup=threading.Event(), announce=heard.append)
        satellite._phase = "talk"
        with patch.dict(satellite_module._running, {"speaker.test": satellite}, clear=True), \
                patch.object(satellite_module, "quiet_now", return_value=True):
            self.assertTrue(satellite_module.ask({"speaker": "speaker.test", "task_id": "t1"}))
        self.assertEqual(heard, [{"speaker": "speaker.test", "task_id": "t1"}])


if __name__ == "__main__":
    unittest.main()
