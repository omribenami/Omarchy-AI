import io
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import threading

import numpy as np
from omarchy_ai.voice import audio, wake


class WakeMicrophoneTests(unittest.TestCase):
    def test_desktop_frame_is_not_replaced_by_tv_silence(self):
        desktop = np.full(audio.FRAME_SAMPLES, 1234, dtype=np.int16)
        proc = SimpleNamespace(stdout=io.BytesIO(desktop.tobytes()), tv_mic=MagicMock())
        proc.tv_mic.read.return_value = bytes(audio.FRAME_BYTES)
        np.testing.assert_array_equal(audio.read_frame(proc), desktop)
        proc.tv_mic.read.assert_not_called()

    def detect(self, desktop_scores, tv_scores, remote=True):
        desktop, tv = MagicMock(), MagicMock()
        for model, scores in [(desktop, desktop_scores), (tv, tv_scores)]:
            model.models = {'omachy': object()}
            model.predict.side_effect = [{'omachy': value} for value in scores]
        config = SimpleNamespace(custom_wake_model_paths=['omachy.onnx'],
                                 mic_device=None, wake_threshold=.5, wake_trigger_frames=2)
        proc = MagicMock()
        proc.tv_mic.read.return_value = bytes(audio.FRAME_BYTES) if remote else None
        frame = np.zeros(audio.FRAME_SAMPLES, dtype=np.int16)
        with patch.object(wake, 'Model', side_effect=[desktop, tv]), \
             patch.object(audio, 'open_stream', return_value=proc), \
             patch.object(audio, 'close_stream'), \
             patch.object(audio, 'read_frame', side_effect=[frame, frame, frame, None]):
            detector = wake.WakeWordDetector(config)
            detected = detector.listen(threading.Event())
        return detected, detector.last_source, desktop, tv

    def test_desktop_wakes_while_tv_streams_silence(self):
        detected, source, desktop, tv = self.detect([.9, .9], [0, 0])
        self.assertTrue(detected)
        self.assertEqual(source, 'desktop')
        self.assertEqual(desktop.predict.call_count, 2)
        tv.predict.assert_called()

    def test_tv_wakes_without_desktop_speech(self):
        detected, source, desktop, tv = self.detect([0, 0], [.9, .9])
        self.assertTrue(detected)
        self.assertEqual(source, 'tv')
        self.assertEqual(tv.predict.call_count, 2)

    def test_scores_from_different_microphones_cannot_combine(self):
        detected, _, _, _ = self.detect([.9, 0, 0], [0, .9, 0])
        self.assertFalse(detected)

    def test_desktop_works_without_tv(self):
        detected, source, _, tv = self.detect([.9, .9], [], remote=False)
        self.assertTrue(detected)
        self.assertEqual(source, 'desktop')
        tv.predict.assert_not_called()


class ConversationMicrophoneTests(unittest.IsolatedAsyncioTestCase):
    async def check_source(self, source, expected):
        from omarchy_ai.voice import live
        desktop = np.full(live.FRAME_SAMPLES, 111, dtype=np.int16).tobytes()
        remote = np.full(live.FRAME_SAMPLES, 222, dtype=np.int16).tobytes()
        proc = MagicMock()
        proc.stdout = io.BytesIO(desktop)
        proc.poll.return_value = 0
        with patch.object(live.subprocess, 'Popen', return_value=proc), \
             patch.object(live, 'TvMicReceiver') as receiver:
            receiver.return_value.read.return_value = remote
            track = live.MicTrack(source=source)
            try:
                frame = await track.recv()
                self.assertTrue(np.all(frame.to_ndarray() == expected))
                if source == 'desktop':
                    receiver.assert_not_called()
                else:
                    receiver.assert_called_once_with(live.RATE)
            finally:
                track.stop()

    async def test_desktop_conversation_cannot_be_hijacked_by_tv(self):
        await self.check_source('desktop', 111)

    async def test_tv_conversation_uses_tv_audio(self):
        await self.check_source('tv', 222)


if __name__ == '__main__':
    unittest.main()
