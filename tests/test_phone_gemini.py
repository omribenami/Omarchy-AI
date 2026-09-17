import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from omarchy_ai.phone.gemini import OutputAudio
from omarchy_ai.phone.server import _relay_offer


class PhoneGeminiTests(unittest.IsolatedAsyncioTestCase):
    def test_gemini_routes_without_openai_key(self):
        with patch('omarchy_ai.phone.gemini.relay_offer', return_value='answer') as relay, patch('omarchy_ai.phone.server._read_api_key') as key:
            config = SimpleNamespace(provider='gemini')
            self.assertEqual(_relay_offer(config, 'offer'), 'answer')
            relay.assert_called_once_with(config, 'offer')
            key.assert_not_called()

    async def test_audio_is_framed_and_interruption_flushes_partial_buffer(self):
        adapter = SimpleNamespace(_generation=0, _audio=asyncio.Queue())
        adapter._audio.put_nowait((0, b'\x01\x02' * 960))
        track = OutputAudio(adapter)
        first = await track.recv()
        self.assertEqual(first.samples, 480)
        self.assertEqual(bytes(first.planes[0]), b'\x01\x02' * 480)
        adapter._generation += 1
        second = await track.recv()
        self.assertEqual(bytes(second.planes[0]), b'\0' * 960)
        self.assertEqual(second.pts, 480)
        track.stop()
