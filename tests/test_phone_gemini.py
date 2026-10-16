import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from omarchy_ai.phone.gemini import OutputAudio, _attachment_parts
from omarchy_ai.phone.server import _relay_offer


class PhoneGeminiTests(unittest.IsolatedAsyncioTestCase):
    def test_gemini_routes_without_openai_key(self):
        with patch('omarchy_ai.phone.gemini.relay_offer', return_value='answer') as relay, patch('omarchy_ai.phone.server._read_api_key') as key:
            config = SimpleNamespace(provider='gemini')
            self.assertEqual(_relay_offer(config, 'offer'), 'answer')
            relay.assert_called_once_with(config, 'offer', '', None)
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

    async def test_image_attachment_sends_jev_text_to_gemini_not_image_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.webp"
            path.write_bytes(b"private image bytes")
            meta = {"name": "photo.webp", "mime": "image/webp", "size": path.stat().st_size}
            config = SimpleNamespace()
            with patch("omarchy_ai.core.conversations.attachment", return_value=(path, meta)), \
                    patch("omarchy_ai.execution.vision.look", return_value="Jev says: outdoor") as look, \
                    patch("omarchy_ai.phone.gemini.asyncio.to_thread",
                          side_effect=lambda function, *args, **kwargs: function(*args, **kwargs)) as to_thread, \
                    patch("omarchy_ai.phone.gemini.types.Part.from_bytes") as from_bytes:
                attachment_meta, parts = await _attachment_parts(
                    config, "conversation", ["attachment-id"], "How many cars?"
                )
        self.assertEqual(attachment_meta, [meta])
        self.assertEqual(len(parts), 1)
        self.assertIn("Jev says: outdoor", parts[0]["text"])
        self.assertNotIn("private image bytes", parts[0]["text"])
        from_bytes.assert_not_called()
        to_thread.assert_awaited_once()
        look.assert_called_once_with(
            path, "How many cars?", config, subject="an image the user attached"
        )



class PhoneStartupTests(unittest.TestCase):
    def test_a_new_phone_call_ends_the_last_one_and_waits_for_it_to_be_saved(self):
        # 2026-09-30 13:36: a dead text chat stayed open on the server, so it was never saved.
        import threading
        from omarchy_ai.phone import gemini
        loop = asyncio.new_event_loop()
        adapter = SimpleNamespace(_hangup=asyncio.Event())
        saved = threading.Event()
        runner = threading.Thread(target=loop.run_forever, daemon=True)
        runner.start()
        call = (loop, adapter, saved)
        gemini._calls.append(call)
        try:
            threading.Timer(0.2, saved.set).start()
            gemini._end_previous_calls(timeout=2)
            self.assertTrue(adapter._hangup.is_set())
            self.assertTrue(saved.is_set())
        finally:
            gemini._calls.remove(call)
            loop.call_soon_threadsafe(loop.stop)
            runner.join(2)
            loop.close()

    def test_phone_peer_never_waits_on_a_stun_server(self):
        # Regression: default aiortc STUN made every phone session wait 5.0s.
        from pathlib import Path
        source = (Path(__file__).parents[1] / "src/omarchy_ai/phone/gemini.py").read_text()
        self.assertIn("RTCPeerConnection(RTCConfiguration(iceServers=[]))", source)
        self.assertNotIn("RTCPeerConnection()", source)
