import unittest
from unittest.mock import AsyncMock, patch

from google.genai import errors
from omarchy_ai.config import Config
from omarchy_ai.core import alert_clips
from omarchy_ai.voice import fallback
from omarchy_ai.voice.gemini_live import GeminiLiveSession


def api_error(code, message):
    return errors.APIError(code, {"error": {"message": message}})


class FallbackTests(unittest.TestCase):
    def setUp(self):
        fallback._down.clear()
        fallback._announced_at = -1e9

    def test_classifies_googles_failures_not_quota_or_ours(self):
        # The real 2026-09-28 errors, from _receive and from send_realtime_input.
        self.assertTrue(fallback.is_provider_failure(api_error(1011, "Internal error encountered.")))
        self.assertTrue(fallback.is_provider_failure(RuntimeError(
            "received 1011 (internal error) Internal error encountered.; then sent 1011 (internal error)")))
        self.assertFalse(fallback.is_provider_failure(api_error(1011, "Resource has been exhausted (e.g. check quota).")))
        self.assertFalse(fallback.is_provider_failure(ValueError("no path given")))

    def test_chain_skips_a_model_that_just_failed(self):
        config = Config()
        self.assertEqual(fallback.chain(config)[0], "gemini-3.8-live")
        fallback.mark_down("gemini-3.8-live", RuntimeError("1011"))
        self.assertEqual(fallback.chain(config), config.gemini_fallback_models)
        for model in config.gemini_fallback_models:
            fallback.mark_down(model, RuntimeError("1011"))
        self.assertEqual(fallback.chain(config)[0], "gemini-3.8-live")  # all down: try them all again

    def test_announces_once_per_outage(self):
        self.assertTrue(fallback.should_announce())
        self.assertFalse(fallback.should_announce())

    def test_names_and_clips(self):
        self.assertEqual(fallback.display_name("gemini-3.8-live"), "Gemini 3.8 Live")
        self.assertEqual(fallback.company("gemini-3.8-live"), "Google")
        texts = fallback.texts(Config().gemini_model)
        self.assertIn("Gemini 3.8 Live is experiencing issues on Google's side", texts["fallback-switching-gemini-3.8-live"])
        self.assertIn("on the provider's side", texts["fallback-switching"])
        for stem in texts:
            self.assertTrue((alert_clips.PACKAGED / f"{stem}-en.ogg").exists(), stem)
        with patch.object(alert_clips, "language", return_value="en"):
            self.assertEqual(fallback.clip("switching", "gemini-9-live").name, "fallback-switching-en.ogg")

    def test_carry_over_joins_transcript_fragments(self):
        text = fallback.carry_over([{"role": "user", "text": "Check the email "}, {"role": "user", "text": "from Alex"},
                                    {"role": "assistant", "text": "Checking now."}])
        self.assertIn("user: Check the email from Alex\nassistant: Checking now.", text)
        self.assertIsNone(fallback.carry_over([]))


class SessionFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fallback._down.clear()
        fallback._announced_at = -1e9
        self.s = GeminiLiveSession(Config())
        self.s._say_clip = AsyncMock()
        self.models = fallback.chain(Config())

    async def test_default_failing_moves_on_and_tells_the_user_once(self):
        self.assertTrue(await self.s._fall_back(self.models, 0, api_error(1011, "Internal error encountered.")))
        self.s._say_clip.assert_awaited_once_with("switching", "gemini-3.8-live")
        self.assertEqual(fallback.chain(Config())[0], self.models[1])

    async def test_last_model_failing_says_sorry_and_ends(self):
        self.assertFalse(await self.s._fall_back(self.models, len(self.models) - 1,
                                                 api_error(1011, "Internal error encountered.")))
        self.s._say_clip.assert_awaited_once_with("failed")

    async def test_other_errors_and_hangups_are_not_fallbacks(self):
        self.assertFalse(await self.s._fall_back(self.models, 0, ValueError("bug")))
        self.s._hangup.set()
        self.assertFalse(await self.s._fall_back(self.models, 0, api_error(1011, "Internal error encountered.")))
        self.s._say_clip.assert_not_awaited()

    async def test_next_call_starts_on_the_fallback(self):
        fallback.mark_down("gemini-3.8-live", RuntimeError("1011"))
        self.assertEqual((await self.s._start_models())[0], self.models[1])
        self.s._say_clip.assert_awaited_once_with("switching", "gemini-3.8-live")



class RunLoopFallbackTests(unittest.IsolatedAsyncioTestCase):
    """The desktop run loop against a fake Live API: the default model dies
    mid-call with the real 1011, the conversation continues on the next."""
    async def test_mid_call_1011_reconnects_on_the_fallback_with_context(self):
        import asyncio
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        fallback._down.clear()
        fallback._announced_at = -1e9
        config = Config(watchdog_enabled=False, jev_fast_path=False)
        s = GeminiLiveSession(config)
        s._transcript.append({"role": "user", "text": "Check the email from Alex"})
        s._say_clip = AsyncMock()
        s._echo = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), source="echo")
        connected, sessions = [], []

        def fake_session(model):
            async def receive():
                await asyncio.sleep(0.05)
                if model == config.gemini_model:
                    raise api_error(1011, "Internal error encountered.")
                s._hangup.set()
                await asyncio.sleep(3600)
                yield  # pragma: no cover
            session = SimpleNamespace(receive=receive, send_client_content=AsyncMock(),
                                      send_realtime_input=AsyncMock(), send_tool_response=AsyncMock())
            sessions.append(session)
            return session

        @asynccontextmanager
        async def connect(model, config):
            connected.append(model)
            yield fake_session(model)

        client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=connect), aclose=AsyncMock()))
        mic = SimpleNamespace(stdout=asyncio.StreamReader(), returncode=0, wait=AsyncMock())
        with patch("google.genai.Client", return_value=client), \
                patch("omarchy_ai.voice.gemini_live.Path.read_text", return_value="key"), \
                patch("omarchy_ai.display.assistant_huds.open_automatic"), \
                patch("omarchy_ai.voice.gemini_live.TvMicReceiver", side_effect=OSError("no tv")), \
                patch("omarchy_ai.voice.gemini_live.status_icon.set_live"), \
                patch("omarchy_ai.voice.gemini_live.announce_update", AsyncMock()), \
                patch("omarchy_ai.voice.gemini_live.append_session"), \
                patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mic)):
            await asyncio.wait_for(s.run(), 5)
        self.assertEqual(connected, [config.gemini_model, config.gemini_fallback_models[0]])
        s._say_clip.assert_awaited_once_with("switching", config.gemini_model)
        carried = sessions[1].send_client_content.call_args.kwargs["turns"]["parts"][0]["text"]
        self.assertIn("user: Check the email from Alex", carried)


if __name__ == "__main__":
    unittest.main()
