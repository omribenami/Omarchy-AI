from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from google.genai import errors
from omarchy_ai.config import Config
from omarchy_ai.core import alert_clips
from omarchy_ai.voice import fallback
from omarchy_ai.voice.gemini_live import GeminiLiveSession


def api_error(code, message):
    return errors.APIError(code, {"error": {"message": message}})


def keyed(gemini=True, vercel=True, **settings):
    """A config whose key files are in a temp folder, never this machine's."""
    folder = Path(tempfile.mkdtemp())
    for name, present in (("gemini-key", gemini), ("vercel-key", vercel)):
        if present:
            (folder / name).write_text("k")
    return Config(gemini_api_key_path=str(folder / "gemini-key"),
                  vercel_gateway_api_key_path=str(folder / "vercel-key"), **settings)


class FallbackTests(unittest.TestCase):
    def setUp(self):
        fallback._down.clear()
        fallback._announced_at = -1e9

    def test_classifies_provider_failures_not_ours(self):
        # The real 2026-09-28 errors, from _receive and from send_realtime_input.
        self.assertTrue(fallback.is_provider_failure(api_error(1011, "Internal error encountered.")))
        self.assertTrue(fallback.is_provider_failure(RuntimeError(
            "received 1011 (internal error) Internal error encountered.; then sent 1011 (internal error)")))
        # Another company's model cures these too (2026-10-05).
        self.assertTrue(fallback.is_provider_failure(api_error(1011, "Resource has been exhausted (e.g. check quota).")))
        self.assertTrue(fallback.is_provider_failure(api_error(403, "API key not valid.")))
        self.assertTrue(fallback.is_provider_failure(ConnectionError("Vercel AI Gateway realtime connect failed: x")))
        self.assertTrue(fallback.is_provider_failure(TimeoutError()))
        self.assertFalse(fallback.is_provider_failure(ValueError("no path given")))
        self.assertFalse(fallback.is_provider_failure(fallback.Reconnect("session ended")))  # same model again

    def test_default_order_is_gemini_then_openai_on_the_gateway_then_older_gemini(self):
        models = fallback.chain(keyed())
        self.assertEqual(models, ["gemini-3.8-live", "openai/gpt-realtime-2", "gemini-3.1-flash-live-preview",
                                  "gemini-2.5-flash-native-audio-latest"])
        self.assertEqual([fallback.via_gateway(m) for m in models], [False, True, False, False])

    def test_a_model_without_its_key_is_left_out(self):
        self.assertEqual(fallback.chain(keyed(gemini=False)), ["openai/gpt-realtime-2"])
        self.assertNotIn("openai/gpt-realtime-2", fallback.chain(keyed(vercel=False)))
        self.assertEqual(len(fallback.chain(keyed(gemini=False, vercel=False))), 4)  # nothing to choose by

    def test_settings_panel_gets_the_chain_and_the_key_each_step_needs(self):
        from omarchy_ai.cli import settings
        steps = settings._voice_fallback(keyed(gemini=False))
        self.assertEqual([(s["label"], s["key"], s["ready"]) for s in steps][:2],
                         [("Gemini 3.8 Live", "Gemini key", False), ("GPT Realtime 2", "Gateway key", True)])

    def test_out_of_quota_skips_the_rest_of_that_key(self):
        models = fallback.chain(keyed())
        quota = api_error(1011, "Resource has been exhausted (e.g. check quota).")
        self.assertEqual(fallback.next_index(models, 0, quota), 1)
        self.assertEqual(fallback.next_index(models, 0, RuntimeError("1011")), 1)
        self.assertEqual(fallback.next_index(models, 1, RuntimeError("1011")), 2)
        gemini_only = [m for m in models if not fallback.via_gateway(m)]
        self.assertIsNone(fallback.next_index(gemini_only, 0, quota))
        self.assertIsNone(fallback.next_index(models, 3, RuntimeError("1011")))

    def test_chain_skips_a_model_that_just_failed(self):
        config = keyed()
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
        self.assertIn("backup model", texts["fallback-switching"])  # the next one is not an older Gemini any more
        for stem in texts:
            self.assertTrue((alert_clips.PACKAGED / f"{stem}-en.ogg").exists(), stem)
        with patch.object(alert_clips, "language", return_value="en"):
            self.assertEqual(fallback.clip("switching", "gemini-9-live").name, "fallback-switching-en.ogg")

    def test_carry_over_joins_transcript_fragments(self):
        text = fallback.carry_over([{"role": "user", "text": "Check the email "}, {"role": "user", "text": "from Alex"},
                                    {"role": "assistant", "text": "Checking now."}])
        self.assertIn("user: Check the email from Alex\nassistant: Checking now.", text)
        self.assertIsNone(fallback.carry_over([]))

    def test_carry_over_after_a_stall_asks_for_the_answer_the_user_is_waiting_for(self):
        text = fallback.carry_over([{"role": "user", "text": "Did you check?"}], stalled=True)
        self.assertTrue(text.startswith("[Turn recovery]"))
        self.assertIn("user: Did you check?", text)
        self.assertIn("ask them to say it again", fallback.carry_over([], stalled=True))

    def test_carry_over_after_a_drop_says_she_is_back(self):
        text = fallback.carry_over([{"role": "user", "text": "Turn on the fan"}], dropped=True)
        self.assertTrue(text.startswith(fallback.DROPPED))
        self.assertIn("user: Turn on the fan", text)
        self.assertIn("say that again", fallback.carry_over([], dropped=True))


class SessionFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fallback._down.clear()
        fallback._announced_at = -1e9
        self.config = keyed()
        self.s = GeminiLiveSession(self.config)
        self.s._say_clip = AsyncMock()
        self.models = fallback.chain(self.config)

    async def test_default_failing_moves_on_and_tells_the_user_once(self):
        self.assertTrue(await self.s._fall_back(self.models, 0, api_error(1011, "Internal error encountered.")))
        self.s._say_clip.assert_awaited_once_with("switching", "gemini-3.8-live")
        self.assertEqual(fallback.chain(self.config)[0], self.models[1])

    async def test_last_model_failing_says_sorry_and_ends(self):
        self.assertFalse(await self.s._fall_back(self.models, len(self.models) - 1,
                                                 api_error(1011, "Internal error encountered.")))
        self.s._say_clip.assert_awaited_once_with("failed")

    async def test_other_errors_and_hangups_are_not_fallbacks(self):
        self.assertFalse(await self.s._fall_back(self.models, 0, ValueError("bug")))
        self.s._hangup.set()
        self.assertFalse(await self.s._fall_back(self.models, 0, api_error(1011, "Internal error encountered.")))
        self.s._say_clip.assert_not_awaited()

    async def test_out_of_quota_alerts_and_goes_on_with_the_other_key(self):
        error = api_error(1011, "Resource has been exhausted (e.g. check quota).")
        with patch("omarchy_ai.voice.gemini_live.quota.report") as report:
            self.assertEqual(await self.s._fall_back(self.models, 0, error), 1)
            self.assertEqual(report.call_args.args[0], "gemini")
            gateway_error = ConnectionError("Vercel AI Gateway refused a realtime session: HTTP 402 no credits")
            self.assertEqual(await self.s._fall_back(self.models, 1, gateway_error), 2)
            self.assertEqual(report.call_args.args[0], "vercel")

    async def test_a_model_that_keeps_stalling_is_left_for_the_next_one(self):
        from omarchy_ai.voice.gemini_live import StuckTurn
        self.s._stall_restarts = self.s.MAX_STALL_RESTARTS
        self.assertFalse(self.s._resume_stalled(StuckTurn("no reply")))
        self.assertEqual(await self.s._fall_back(self.models, 0, StuckTurn("no reply")), 1)
        self.assertEqual(self.s._stall_restarts, 0)
        self.assertTrue(self.s._resume_stalled(StuckTurn("no reply")))

    async def test_a_session_the_provider_ended_on_schedule_reconnects_without_blame(self):
        self.assertTrue(self.s._resume_stalled(fallback.Reconnect("Gateway realtime session ended")))
        self.assertEqual(self.s._stall_restarts, 0)
        self.assertFalse(self.s._stalled_restart)   # plain context, not "answer the user now"
        self.assertIsNone(await self.s._fall_back(self.models, 0, fallback.Reconnect("ended")))

    async def test_without_a_gemini_key_it_starts_on_the_gateway_and_blames_nobody(self):
        s = GeminiLiveSession(keyed(gemini=False))
        s._say_clip = AsyncMock()
        self.assertEqual(await s._start_models(), ["openai/gpt-realtime-2"])
        s._say_clip.assert_not_awaited()

    async def test_a_transient_close_reconnects_once_until_a_turn_completes(self):
        drop = api_error(1011, "Internal error encountered.")
        self.assertTrue(self.s._resume_dropped(drop))
        self.assertEqual(self.s._dropped_restart, "1011")
        self.assertFalse(self.s._resume_dropped(drop))  # the reconnect failed too: _fall_back decides
        self.s._drop_restarts = 0  # what a completed turn on the new connection does (_receive)
        self.assertTrue(self.s._resume_dropped(ConnectionResetError("network blip")))

    async def test_quota_policy_auth_and_hangups_do_not_reconnect(self):
        for error in (api_error(1011, "Resource has been exhausted (e.g. check quota)."),
                      api_error(1008, "Policy violation"), api_error(1007, "Invalid argument"),
                      ConnectionError("HTTP 401 unauthorized"), ValueError("bug"),
                      fallback.Reconnect("handled by _resume_stalled")):
            self.assertFalse(self.s._resume_dropped(error), error)
        self.s._hangup.set()
        self.assertFalse(self.s._resume_dropped(api_error(1011, "Internal error encountered.")))

    async def test_next_call_starts_on_the_fallback(self):
        fallback.mark_down("gemini-3.8-live", RuntimeError("1011"))
        self.assertEqual((await self.s._start_models())[0], self.models[1])
        self.s._say_clip.assert_awaited_once_with("switching", "gemini-3.8-live")



class RunLoopFallbackTests(unittest.IsolatedAsyncioTestCase):
    """The desktop run loop against a fake Live API: the default model dies
    mid-call with the real 1011, the conversation continues on the next."""
    async def run_desktop(self, fails):
        """s.run() where connection number n (0-based) to `model` raises the
        real 1011 when fails(n, model); the others end the conversation."""
        import asyncio
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        fallback._down.clear()
        fallback._announced_at = -1e9
        config = keyed(watchdog_enabled=False, jev_fast_path=False)
        s = GeminiLiveSession(config)
        s._transcript.append({"role": "user", "text": "Check the email from Alex"})
        s._say_clip = AsyncMock()
        s._echo = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), source="echo")
        connected, sessions = [], []

        def fake_session(model):
            n = len(sessions)

            async def receive():
                await asyncio.sleep(0.05)
                if fails(n, model):
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

        def gateway_connect(config, model, live_config):
            return connect(model, live_config)

        client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=connect), aclose=AsyncMock()))
        mic = SimpleNamespace(stdout=asyncio.StreamReader(), returncode=0, wait=AsyncMock())
        with patch("google.genai.Client", return_value=client), \
                patch("omarchy_ai.voice.gateway_live.connect", gateway_connect), \
                patch("omarchy_ai.display.assistant_huds.open_automatic"), \
                patch("omarchy_ai.voice.gemini_live.TvMicReceiver", side_effect=OSError("no tv")), \
                patch("omarchy_ai.voice.gemini_live.status_icon.set_live"), \
                patch("omarchy_ai.voice.gemini_live.announce_update", AsyncMock()), \
                patch("omarchy_ai.voice.gemini_live.append_session"), \
                patch("omarchy_ai.core.conversations.DIR", Path(tempfile.mkdtemp())), \
                patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mic)):
            await asyncio.wait_for(s.run(), 5)
        return config, s, connected, sessions

    async def test_mid_call_1011_reconnects_once_then_moves_to_the_fallback_with_context(self):
        config, s, connected, sessions = await self.run_desktop(lambda n, model: model == Config().gemini_model)
        # One reconnect to the same model; it fails again before a turn, so
        # Google's outage is answered by another company's model, on the Gateway key.
        self.assertEqual(connected, [config.gemini_model, config.gemini_model, "openai/gpt-realtime-2"])
        s._say_clip.assert_awaited_once_with("switching", config.gemini_model)
        carried = sessions[2].send_client_content.call_args.kwargs["turns"]["parts"][0]["text"]
        self.assertIn("user: Check the email from Alex", carried)

    async def test_a_single_1011_reconnects_to_the_same_model_and_says_she_is_back(self):
        # Journal 2026-10-17 08:09 and 09:50: one 1011 in the middle of a conversation.
        with self.assertLogs("omarchy_ai.voice.gemini", "WARNING") as logged:
            config, s, connected, sessions = await self.run_desktop(lambda n, model: n == 0)
        self.assertEqual(connected, [config.gemini_model] * 2)
        self.assertEqual(fallback.chain(config)[0], config.gemini_model)  # not marked as failing
        s._say_clip.assert_not_awaited()
        kwargs = sessions[1].send_client_content.call_args.kwargs
        self.assertTrue(kwargs["turn_complete"])  # she speaks first: "lost the connection, say that again?"
        self.assertTrue(kwargs["turns"]["parts"][0]["text"].startswith(fallback.DROPPED))
        self.assertIn("user: Check the email from Alex", kwargs["turns"]["parts"][0]["text"])
        self.assertTrue(any("Gemini session reconnected after 1011" in line for line in logged.output))

    async def test_a_stalled_turn_reconnects_on_the_same_model_and_asks_for_the_answer(self):
        # Journal 2026-10-05 18:37:24 (phone): a forced closure, then 52s of nothing.
        import asyncio
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        fallback._down.clear()
        config = keyed(watchdog_enabled=False, jev_fast_path=False)
        s = GeminiLiveSession(config)
        s.RESTART_AFTER_SECONDS = 0.0
        s._transcript.append({"role": "user", "text": "Did you check?"})
        s._say_clip = AsyncMock()
        s._echo = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), source="echo")
        connected, sessions = [], []

        def fake_session():
            first = not sessions

            async def receive():
                if first:
                    s._forced_at = time.monotonic()   # the closure went out; she stays silent
                else:
                    await asyncio.sleep(0.05)
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
            yield fake_session()

        client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=connect), aclose=AsyncMock()))
        mic = SimpleNamespace(stdout=asyncio.StreamReader(), returncode=0, wait=AsyncMock())
        with patch("google.genai.Client", return_value=client), \
                patch("omarchy_ai.display.assistant_huds.open_automatic"), \
                patch("omarchy_ai.voice.gemini_live.TvMicReceiver", side_effect=OSError("no tv")), \
                patch("omarchy_ai.voice.gemini_live.status_icon.set_live"), \
                patch("omarchy_ai.voice.gemini_live.announce_update", AsyncMock()), \
                patch("omarchy_ai.voice.gemini_live.append_session"), \
                patch("omarchy_ai.core.conversations.DIR", Path(tempfile.mkdtemp())), \
                patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mic)):
            await asyncio.wait_for(s.run(), 5)
        self.assertEqual(connected, [config.gemini_model] * 2)
        self.assertEqual(fallback.chain(config)[0], config.gemini_model)   # not marked as failing
        s._say_clip.assert_not_awaited()
        kwargs = sessions[1].send_client_content.call_args.kwargs
        self.assertTrue(kwargs["turn_complete"])
        self.assertIn("user: Did you check?", kwargs["turns"]["parts"][0]["text"])


if __name__ == "__main__":
    unittest.main()
