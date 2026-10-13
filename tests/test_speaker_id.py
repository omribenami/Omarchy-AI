import asyncio
import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np

from omarchy_ai.config import Config
from omarchy_ai.voice import speaker_id
from omarchy_ai.voice.gemini_live import GeminiLiveSession, build_live_config


def tone(hz, seconds=2.0, rate=16000):
    return (8000 * np.sin(2 * np.pi * hz * np.arange(int(seconds * rate)) / rate)).astype("<i2")


def write_wav(path, samples, rate=16000, channels=1):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(np.repeat(samples, channels).astype("<i2").tobytes())


class FakeIdentifier(speaker_id.Identifier):
    """No model: a recording's "voice" is which half of the spectrum it is in."""

    def embed(self, samples):
        features = speaker_id.fbank(np.asarray(samples))
        spectrum = np.abs(np.fft.rfft(np.asarray(samples, dtype=np.float32)))
        low, high = spectrum[:len(spectrum) // 8].sum(), spectrum[len(spectrum) // 8:].sum()
        self.frames = len(features)
        vector = np.array([low, high], dtype=np.float32)
        return vector / np.linalg.norm(vector)


class FeatureTests(unittest.TestCase):
    def test_filterbank_is_80_bins_every_10ms_with_the_mean_removed(self):
        features = speaker_id.fbank(tone(440, 1.0))
        self.assertEqual(features.shape, (98, 80))           # (16000 - 400) // 160 + 1 frames
        self.assertLess(float(np.abs(features.mean(axis=0)).max()), 1e-3)
        # A higher tone lights a higher mel bin.
        peak = lambda hz: int(speaker_id.fbank(np.concatenate([tone(hz, 0.5), tone(1000, 0.5)]))[:40].mean(axis=0).argmax())
        self.assertLess(peak(300), peak(3000))

    def test_a_recording_is_read_as_mono_16k_whatever_it_was(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "a.wav"
            write_wav(path, tone(440, 1.0, 48000), rate=48000, channels=2)
            samples = speaker_id.read_wav(path)
        self.assertEqual((samples.dtype.kind, abs(len(samples) - 16000) <= 1), ("i", True))


class DecisionTests(unittest.TestCase):
    """Thresholds from the 2026-10-05 measurement: right 0.48+, wrong at most 0.26."""

    def test_a_clear_match_is_named(self):
        self.assertEqual(speaker_id.decide({"dana": 0.61, "alex": 0.12}), "dana")
        self.assertEqual(speaker_id.decide({"dana": 0.5}), "dana")

    def test_a_stranger_or_two_similar_voices_are_nobody(self):
        self.assertIsNone(speaker_id.decide({"dana": 0.26, "alex": 0.14}))    # below the bar
        self.assertIsNone(speaker_id.decide({"dana": 0.45, "alex": 0.40}))    # too close to call
        self.assertIsNone(speaker_id.decide({}))


class EnrollTests(unittest.TestCase):
    def test_each_persons_recordings_become_one_saved_voice(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, hz in (("dana", 200), ("alex", 3000)):
                (root / "speakers" / name).mkdir(parents=True)
                for n in range(2):
                    write_wav(root / "speakers" / name / f"{n}.wav", tone(hz + 20 * n))
            write_wav(root / "speakers" / "dana" / "short.wav", tone(200, 0.3))
            (root / "speakers" / "empty").mkdir()
            (root / "model.onnx").write_bytes(b"x")              # present: nothing is downloaded
            identifier = FakeIdentifier(root / "model.onnx", root / "speakers.json")
            result = speaker_id.enroll(root / "speakers", identifier)
            self.assertEqual(result["speakers"], {"alex": 2, "dana": 2})
            self.assertEqual(len(result["skipped"]), 1)          # the 0.3 s clip
            self.assertEqual(sorted(json.loads((root / "speakers.json").read_text())), ["alex", "dana"])
            self.assertEqual((root / "speakers.json").stat().st_mode & 0o777, 0o600)
            fresh = FakeIdentifier(root / "model.onnx", root / "speakers.json")
            self.assertTrue(fresh.ready())
            self.assertEqual(fresh.identify(tone(210))[0], "dana")
            self.assertEqual(fresh.identify(tone(2900))[0], "alex")

    def test_nothing_enrolled_is_not_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(speaker_id.Identifier(Path(folder) / "m.onnx", Path(folder) / "none.json").ready())


class RememberTests(unittest.TestCase):
    """A person adds a recording of themself from a conversation: existing people only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, hz in (("dana", 200), ("alex", 3000)):
            (self.root / "speakers" / name).mkdir(parents=True)
            write_wav(self.root / "speakers" / name / "0.wav", tone(hz))
        (self.root / "model.onnx").write_bytes(b"x")
        self.identifier = FakeIdentifier(self.root / "model.onnx", self.root / "speakers.json")
        speaker_id.enroll(self.root / "speakers", self.identifier)
        self.identifier._known = None

    def remember(self, name, samples):
        return speaker_id.remember(name, samples, "phone", self.identifier, self.root / "speakers")

    def test_their_own_voice_is_saved_and_enrolled_again(self):
        ok, message = self.remember("Dana", tone(220, 20.0))
        self.assertTrue(ok, message)
        saved = list((self.root / "speakers" / "dana").glob("live-*-phone.wav"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(len(speaker_id.read_wav(saved[0])), int(speaker_id.ADD_MAX_SECONDS * 16000))  # the latest 15 s
        self.assertIn("2 recordings now", message)

    def test_another_persons_voice_cannot_be_added_to_a_name(self):
        ok, message = self.remember("dana", tone(3000, 8.0))     # Alex says "I am Dana"
        self.assertFalse(ok)
        self.assertIn("not recognized as dana", message)
        self.assertEqual(len(list((self.root / "speakers" / "dana").glob("*.wav"))), 1)

    def test_a_new_person_and_too_little_speech_are_refused(self):
        ok, message = self.remember("Robin", tone(220, 8.0))
        self.assertFalse(ok)
        self.assertIn("no enrolled voice yet", message)
        self.assertFalse((self.root / "speakers" / "robin").exists())
        ok, message = self.remember("dana", tone(220, 3.0))
        self.assertFalse(ok)
        self.assertIn("keep talking", message)
        self.assertFalse(self.remember("../alex", tone(3000, 8.0))[0] and (self.root / "alex").exists())


class PeopleTests(unittest.TestCase):
    def test_the_users_file_describes_each_person_for_the_model(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "people.yaml"
            path.write_text("people:\n  Dana:\n    name: Dana\n    relation: the user's daughter\n    age: 9\n"
                            "    about: Likes drawing.\n    treat: Short, simple answers.\n  alex: not a mapping\n")
            household = speaker_id.people(path)
            self.assertEqual(list(household), ["dana"])
            self.assertEqual(speaker_id.describe("dana", household["dana"]),
                             "Dana (the user's daughter, 9 years old). About them: Likes drawing. "
                             "How to treat them: Short, simple answers")
            self.assertEqual(speaker_id.describe("robin", None), "Robin")
            self.assertEqual(speaker_id.house_style(path), "")
            path.write_text("style: Use first names.\npeople: {}\n")
            self.assertEqual((speaker_id.house_style(path), speaker_id.people(path)), ("Use first names.", {}))
            path.write_text(": : not yaml [")
            self.assertEqual(speaker_id.people(path), {})
            self.assertEqual(speaker_id.people(Path(folder) / "missing.yaml"), {})


class TrackerTests(unittest.TestCase):
    def frame(self):
        return bytes(640)                                        # 20 ms

    def add(self, tracker, frames, speech=False, echo=False):
        for _ in range(frames):
            tracker.add(self.frame(), speech=speech, echo=echo)

    def test_kept_audio_drives_the_first_judgement_after_a_second_and_a_half(self):
        tracker = speaker_id.Tracker()
        self.add(tracker, 200, echo=True)                         # her playback is never kept
        self.assertIsNone(tracker.due())
        self.add(tracker, 74, speech=True)
        self.assertIsNone(tracker.due())                         # 1.48 s
        tracker.add(self.frame(), speech=True)
        self.assertEqual(len(tracker.due()), 24000)
        self.assertIsNone(tracker.due())                         # judged once

    def test_later_judgements_wait_for_a_window_and_look_at_the_latest_speech_only(self):
        tracker = speaker_id.Tracker()
        tracker.speaker = "dana"
        for _ in range(199):
            tracker.add(self.frame(), speech=True)
        self.assertIsNone(tracker.due())
        for _ in range(300):
            tracker.add(self.frame(), speech=True)
        self.assertLessEqual(len(tracker.due()), int(speaker_id.WINDOW_SECONDS * 16000) + 320)


    def test_the_latest_utterance_is_the_speech_since_the_last_pause(self):
        tracker = speaker_id.Tracker()
        for speech, frames in ((True, 100), (False, 29), (True, 40)):     # a 0.58 s pause: the same utterance
            self.add(tracker, frames, speech=speech)
        self.assertEqual((len(tracker.latest()[0]), tracker.latest()[1]), ((100 + 29 + 40) * 320, 0.0))
        for speech, frames in ((False, 30), (True, 25), (False, 10)):     # 0.6 s: someone starts a new one
            self.add(tracker, frames, speech=speech)
        # The old utterance is gone; the new one has its 0.2 s pre-roll.
        self.assertEqual((len(tracker.latest()[0]), tracker.latest()[1]), ((10 + 25) * 320, 0.2))

    def test_quiet_frames_inside_an_utterance_are_kept(self):
        tracker = speaker_id.Tracker()
        self.add(tracker, 5, speech=True)
        self.add(tracker, 20)
        self.add(tracker, 5, speech=True)
        self.assertEqual(len(tracker.latest()[0]), 30 * 320)

    def test_soft_onset_is_kept_as_pre_roll(self):
        tracker = speaker_id.Tracker()
        self.add(tracker, 30)
        self.add(tracker, 1, speech=True)
        self.assertEqual(len(tracker.latest()[0]), 11 * 320)

    def test_long_silence_keeps_only_the_short_hangover(self):
        tracker = speaker_id.Tracker()
        self.add(tracker, 5, speech=True)
        self.add(tracker, 100)
        self.assertEqual(len(tracker.latest()[0]), (5 + 10) * 320)
        self.assertEqual(len(tracker.recent()), (5 + 10) * 320)

    def test_echo_is_never_kept_or_used_as_pre_roll(self):
        tracker = speaker_id.Tracker()
        self.add(tracker, 5, echo=True)
        self.add(tracker, 5)
        self.add(tracker, 5, speech=True)
        self.add(tracker, 5, echo=True)
        self.add(tracker, 5)
        self.add(tracker, 5, speech=True)
        self.assertEqual(len(tracker.latest()[0]), (5 + 5 + 5 + 5) * 320)

        tracker = speaker_id.Tracker()
        self.add(tracker, 5, speech=True)
        self.add(tracker, 30, echo=True)                         # echo counts toward the pause
        self.add(tracker, 5, speech=True)
        self.assertEqual(len(tracker.latest()[0]), 5 * 320)      # but not toward the next pre-roll


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Never this machine's household file.
        patcher = patch.object(speaker_id, "PEOPLE_PATH", Path("/nonexistent/people.yaml"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_off_by_default_and_the_model_is_told_the_rule_only_when_on(self):
        self.assertIsNone(GeminiLiveSession(Config())._voices)
        self.assertNotIn("WHO IS SPEAKING", build_live_config(Config())["system_instruction"])
        self.assertIn("WHO IS SPEAKING", build_live_config(Config(speaker_id_enabled=True))["system_instruction"])

    def test_the_users_loud_frames_are_collected_and_her_own_playback_is_not(self):
        s = GeminiLiveSession(Config(speaker_id_enabled=True))
        s._gate(bytes(640), 9000.0, 100.0)
        s._gate(bytes(640), 100.0, 100.02)                       # the room
        s._playback_until = 200.0
        s._gate(bytes(640), 9000.0, 100.04)                      # her echo
        self.assertEqual(s._voices._fresh, 320)

    def test_the_satellite_counts_normal_speech_under_the_guards_level(self):
        s = GeminiLiveSession(Config(speaker_id_enabled=True))
        s._splice_abort_rms, s._voice_rms = 300.0, 120.0
        s._gate(bytes(640), 250.0, 100.0)                        # normal speech on a Voice PE
        s._gate(bytes(640), 60.0, 100.02)                        # the quiet room
        self.assertEqual(s._voices.loud, 320)
        self.assertFalse(s._splice_armed)                        # the stuck-turn guard keeps its own level

    async def watch(self, s, answers, seconds=0.9):
        session = SimpleNamespace(send_client_content=AsyncMock())
        identifier = SimpleNamespace(ready=lambda: True, identify=lambda samples: answers.pop(0))
        with patch.object(speaker_id, "Identifier", return_value=identifier):
            worker = asyncio.create_task(s._speaker_watch(session))
            await asyncio.sleep(seconds)
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        return session.send_client_content

    async def test_a_recognized_voice_is_told_to_the_model_once_as_context(self):
        s = GeminiLiveSession(Config(speaker_id_enabled=True))
        for _ in range(80):
            s._voices.add(bytes(640), True)
        sent = await self.watch(s, [("dana", {"dana": 0.6, "alex": 0.1})])
        self.assertEqual(sent.await_count, 2)                                # "nobody yet", then her
        self.assertIn("Nobody's voice has been recognized", sent.call_args_list[0].kwargs["turns"]["parts"][0]["text"])
        self.assertFalse(sent.call_args.kwargs["turn_complete"])             # not a turn of its own
        self.assertIn("[Speaker] The person speaking now, recognized by their voice: Dana.",
                      sent.call_args.kwargs["turns"]["parts"][0]["text"])
        for _ in range(210):
            s._voices.add(bytes(640), True)
        again = await self.watch(s, [("dana", {"dana": 0.7, "alex": 0.1})])
        again.assert_not_awaited()                                           # still her: nothing to say

    async def test_an_unrecognized_voice_is_said_so_once_and_never_guessed(self):
        s = GeminiLiveSession(Config(speaker_id_enabled=True))
        sent = await self.watch(s, [])
        sent.assert_awaited_once()                                           # before anyone speaks
        text = sent.call_args.kwargs["turns"]["parts"][0]["text"]
        self.assertIn("[Speaker] Nobody's voice has been recognized in this conversation yet.", text)
        self.assertIn("do not call them by a household member's name", text)
        self.assertFalse(sent.call_args.kwargs["turn_complete"])
        for _ in range(80):
            s._voices.add(bytes(640), True)
        sent = await self.watch(s, [(None, {"dana": 0.2, "alex": 0.1})])
        self.assertEqual(sent.await_count, 2)
        self.assertIn("not recognized as anyone enrolled", sent.call_args.kwargs["turns"]["parts"][0]["text"])
        self.assertIsNone(s._voices.speaker)

    async def test_the_model_gets_the_persons_profile_with_the_news(self):
        s = GeminiLiveSession(Config(speaker_id_enabled=True))
        for _ in range(80):
            s._voices.add(bytes(640), True)
        entry = {"name": "Dana", "relation": "the user's daughter", "treat": "Short, simple answers."}
        with patch.object(speaker_id, "people", return_value={"dana": entry}):
            sent = await self.watch(s, [("dana", {"dana": 0.6})])
        text = sent.call_args.kwargs["turns"]["parts"][0]["text"]
        self.assertIn("Dana (the user's daughter). How to treat them: Short, simple answers.", text)

    def test_remember_voice_is_a_tool_only_when_recognition_is_on_and_uses_this_conversations_speech(self):
        self.assertNotIn("remember_voice", GeminiLiveSession(Config()).extra_tools)
        names = lambda config: [t["name"] for t in build_live_config(config)["tools"][0]["function_declarations"]]
        self.assertNotIn("remember_voice", names(Config()))
        with patch.object(speaker_id, "people", return_value={"dana": {"name": "Dana", "age": 9}}):
            on = build_live_config(Config(speaker_id_enabled=True))
        self.assertIn("remember_voice", [t["name"] for t in on["tools"][0]["function_declarations"]])
        self.assertIn("The household: Dana (9 years old).", on["system_instruction"])
        self.assertIn("call them by their first name", on["system_instruction"])
        s = GeminiLiveSession(Config(speaker_id_enabled=True))
        s.from_paired_phone = True
        for _ in range(100):
            s._voices.add(bytes(640), True)
        with patch.object(speaker_id, "remember", return_value=(True, "Saved")) as remember:
            result = s.extra_tools["remember_voice"]({"name": "Dana"})
        self.assertTrue(result.ok)
        self.assertEqual((remember.call_args.args[0], len(remember.call_args.args[1]), remember.call_args.args[2]),
                         ("Dana", 32000, "phone"))
        self.assertFalse(s.extra_tools["remember_voice"]({}).ok)

    async def test_an_unsure_stretch_changes_nothing_and_another_person_is_announced(self):
        s = GeminiLiveSession(Config(speaker_id_enabled=True))
        s._voices.speaker = "dana"
        for _ in range(210):
            s._voices.add(bytes(640), True)
        self.assertFalse((await self.watch(s, [(None, {"dana": 0.3, "alex": 0.28})])).await_count)
        self.assertEqual(s._voices.speaker, "dana")
        for _ in range(210):
            s._voices.add(bytes(640), True)
        sent = await self.watch(s, [("alex", {"dana": 0.1, "alex": 0.6})])
        self.assertIn("Alex", sent.call_args.kwargs["turns"]["parts"][0]["text"])

    async def test_on_but_nobody_enrolled_never_ends_the_conversation(self):
        s = GeminiLiveSession(Config(speaker_id_enabled=True))
        identifier = SimpleNamespace(ready=lambda: False)
        with patch.object(speaker_id, "Identifier", return_value=identifier):
            worker = asyncio.create_task(s._speaker_watch(SimpleNamespace()))
            await asyncio.sleep(0.2)
            self.assertFalse(worker.done())                      # a finished worker would hang the call up
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        off = asyncio.create_task(GeminiLiveSession(Config())._speaker_watch(SimpleNamespace()))
        await asyncio.sleep(0.1)
        self.assertFalse(off.done())
        off.cancel()
        await asyncio.gather(off, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
