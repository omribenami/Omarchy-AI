"""A reminder asked on the voice satellite, said on the speaker too when it fires."""
import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from omarchy_ai.config import Config
from omarchy_ai.core import agenda, conversations
from omarchy_ai.execution import actions
from omarchy_ai.voice import satellite as voice_satellite


def local_noon() -> float:
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 12, 0, 0, 0, 0, -1))


class SpeakerReminderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        patch.object(agenda, "JOBS_PATH", base / "agenda.json").start()
        patch.object(agenda, "INBOX_PATH", base / "inbox.jsonl").start()
        self.notify = patch.object(agenda, "notify").start()
        patch.object(agenda, "_refresh_routines_hud").start()
        patch.object(agenda, "tick").start()
        patch("time.time", return_value=local_noon()).start()  # "later today" never crosses midnight
        self.source = patch.object(conversations, "source", return_value="satellite").start()
        self.addCleanup(patch.stopall)

    def schedule(self, **extra):
        return actions.schedule_task({"title": "Take the bread out", "kind": "remind", "in_minutes": 30,
                                      "announce_on_speaker": True, **extra})

    def test_accepted_on_the_speaker_for_today_and_stored(self):
        result = self.schedule()
        self.assertTrue(result.ok, result.message)
        self.assertEqual(json.loads(result.message)["scheduled"]["announce"], "satellite")
        self.assertEqual([j.get("announce") for j in agenda._load()], ["satellite"])

    def test_rejected_away_from_the_speaker(self):
        self.source.return_value = "desktop-voice"
        result = self.schedule()
        self.assertFalse(result.ok)
        self.assertIn("announce_on_speaker", result.message)
        self.assertEqual(agenda._load(), [])

    def test_rejected_for_another_day_or_kind(self):
        for extra in ({"in_minutes": 13 * 60}, {"in_minutes": None, "at": "tomorrow 9:00"},
                      {"kind": "command", "command": "true"}):
            result = self.schedule(**extra)
            self.assertFalse(result.ok, extra)
            self.assertIn("later today", result.message)
        self.assertEqual(agenda._load(), [])

    def test_plain_reminder_and_a_smuggled_announce_are_not_spoken(self):
        result = actions.schedule_task({"title": "Call back", "kind": "remind", "in_minutes": 5,
                                        "announce": "satellite"})
        self.assertTrue(result.ok, result.message)
        self.assertNotIn("announce", agenda._load()[0])

    def test_firing_notifies_as_before_and_marks_the_result_for_the_speaker(self):
        self.schedule()
        heard = []
        patch.object(agenda, "listeners", [heard.append]).start()
        agenda.run_job(agenda._load()[0], now=time.time() + 1800)
        self.notify.assert_called_once_with("Reminder", "Take the bread out")
        self.assertEqual((heard[0]["outcome"], heard[0]["announce"]), ("reminded", "satellite"))


def daemon(state="listening", session=None):
    from omarchy_ai.core.daemon import OmaDaemon
    d = OmaDaemon.__new__(OmaDaemon)
    d.config, d._state, d._session = Config(provider="gemini"), state, session
    d._pending_announcements, d._manual, d._listen_stop = [], False, threading.Event()
    return d


ENTRY = {"id": "e1", "job_id": "t-1", "title": "Take the bread out", "kind": "remind", "outcome": "reminded",
         "detail": "Reminder shown: Take the bread out", "announce": "satellite"}


class SpeakerDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sat = voice_satellite.Satellite(Config())
        self.sat._client, self.sat._loop, self.sat._phase = Mock(), asyncio.get_running_loop(), "wake"
        self.sat._run_session = Mock(side_effect=lambda *a: asyncio.sleep(0))
        patch.object(voice_satellite, "_running", self.sat).start()
        patch.object(agenda, "_calls", {}).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.sat._pool.shutdown)

    def adapter(self):
        adapter = Mock(spec=["announce", "_hangup"])
        adapter._hangup = asyncio.Event()
        return adapter

    async def test_no_conversation_she_calls_out_on_the_speaker(self):
        d = daemon()
        d._on_agenda_result(dict(ENTRY))
        await asyncio.sleep(0)
        self.assertEqual(self.sat._phase, "talk")
        self.assertEqual(self.sat._run_session.call_args.args[2], True)  # proactive
        self.assertEqual([e["id"] for e in self.sat._waiting], ["e1"])
        self.assertFalse(d._listen_stop.is_set())  # the desktop is not woken to say it again

    async def test_open_satellite_call_says_it_once(self):
        adapter = self.adapter()
        self.sat._adapter, self.sat._phase = adapter, "talk"
        agenda.attach_call(adapter, asyncio.get_running_loop())
        daemon()._on_agenda_result(dict(ENTRY))
        await asyncio.sleep(0)
        adapter.announce.assert_called_once()
        self.sat._run_session.assert_not_called()

    async def test_connecting_conversation_says_it_when_up(self):
        adapter = self.adapter()
        self.sat._adapter, self.sat._phase = adapter, "talk"
        daemon()._on_agenda_result(dict(ENTRY))
        adapter.announce.assert_called_once()
        self.sat._run_session.assert_not_called()

    async def test_a_conversation_starting_takes_it(self):
        self.sat._phase = "talk"                        # woken, its session not built yet
        self.assertTrue(voice_satellite.announce(dict(ENTRY)))
        self.sat._run_session.assert_not_called()
        self.assertEqual([e["id"] for e in self.sat._waiting], ["e1"])

    async def test_proactive_session_carries_it_as_announcement(self):
        built = []

        class Stop(Exception):
            pass

        def fake(config, announcements=None):
            built.append(list(announcements or []))
            raise Stop
        self.sat._waiting = [dict(ENTRY)]
        with patch("omarchy_ai.voice.gemini_live.GeminiLiveSession", fake), self.assertRaises(Stop):
            await voice_satellite.Satellite._run_session(self.sat, None, 0, True)
        self.assertEqual(([e["id"] for e in built[0]], self.sat._waiting), (["e1"], []))

    async def test_other_results_and_no_satellite_keep_the_old_path(self):
        d = daemon()
        d._on_agenda_result({"id": "r1", "title": "Build finished"})
        self.sat._run_session.assert_not_called()
        self.assertTrue(d._listen_stop.is_set())
        with patch.object(voice_satellite, "_running", None):
            d = daemon()
            d._on_agenda_result(dict(ENTRY))
            self.assertTrue(d._listen_stop.is_set())


if __name__ == "__main__":
    unittest.main()
