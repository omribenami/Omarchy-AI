import asyncio
import base64
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from omarchy_ai.voice.omarchy import GatewayClient, OmarchySession, _answer_boolean


class OmarchyGatewayTests(unittest.TestCase):
    def _config(self, key_path):
        return SimpleNamespace(
            vercel_gateway_api_key_path=str(key_path), omarchy_stt_model="openai/gpt-4o-mini-transcribe",
            omarchy_tts_model="openai/tts-1", omarchy_jev_model="typesafe-ai/jev",
            omarchy_text_model="google/gemini-2.5-flash-lite", omarchy_tts_voice="nova",
        )

    def test_jev_uses_gateway_evaluation_protocol_and_typed_criteria(self):
        with TemporaryDirectory() as directory:
            key = Path(directory) / "key"; key.write_text("gateway-secret")
            client = GatewayClient(self._config(key))
            with patch.object(client, "_request", return_value=json.dumps({"answers": {"risk": {"type": "choice", "choice": "normal"}}}).encode()) as request:
                result = client.evaluate("raise volume")
        self.assertEqual(result["risk"]["choice"], "normal")
        path, body, content_type, headers = request.call_args.args
        self.assertEqual(path, "/ai/evaluation-model")
        self.assertEqual(headers["ai-model-id"], "typesafe-ai/jev")
        self.assertEqual(json.loads(body)["questions"]["risk"]["criteria"]["normal"], "A normal allowed desktop request or question.")
        self.assertEqual(content_type, "application/json")

    def test_gateway_audio_models_use_sdk_protocol(self):
        with TemporaryDirectory() as directory:
            key = Path(directory) / "key"; key.write_text("gateway-secret")
            client = GatewayClient(self._config(key))
            with patch.object(client, "_request", side_effect=[json.dumps({"text": "שלום"}).encode(), json.dumps({"audio": base64.b64encode(b"pcm0").decode()}).encode()]) as request:
                self.assertEqual(client.transcribe(b"\0" * 320), "שלום")
                self.assertEqual(client.speech("שלום"), b"pcm0")
        self.assertEqual(request.call_args_list[0].args[0], "/ai/transcription-model")
        self.assertEqual(request.call_args_list[1].args[0], "/ai/speech-model")
        self.assertEqual(json.loads(request.call_args_list[1].args[1])["outputFormat"], "pcm")

    def test_jev_boolean_is_probability_only(self):
        self.assertEqual(_answer_boolean({"type": "boolean", "probability": .97}), (True, .97))
        self.assertEqual(_answer_boolean({"type": "boolean", "probability": .03}), (False, .03))

    def test_wire_url_uses_v4_for_sdk_and_v1_for_chat(self):
        with TemporaryDirectory() as directory:
            key = Path(directory) / "key"; key.write_text("gateway-secret")
            client = GatewayClient(self._config(key))
            with patch('omarchy_ai.voice.omarchy.urllib.request.urlopen') as send:
                for endpoint in ('/ai/transcription-model', '/ai/speech-model', '/ai/evaluation-model', '/chat/completions'):
                    client._request(endpoint, b'{}', 'application/json')
                    req = send.call_args.args[0]
                    version = 'v4' if endpoint.startswith('/ai/') else 'v1'
                    self.assertEqual(req.full_url, 'https://ai-gateway.vercel.sh/' + version + endpoint)

    def test_chat_converts_existing_tools_to_chat_completions_shape(self):
        with TemporaryDirectory() as directory:
            key = Path(directory) / "key"; key.write_text("gateway-secret")
            client = GatewayClient(self._config(key))
            with patch.object(client, "_request", return_value=json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()) as request:
                self.assertEqual(client.chat([{"role": "user", "content": "hi"}])["content"], "ok")
        payload = json.loads(request.call_args.args[1])
        self.assertIn("function", payload["tools"][0])
        self.assertNotIn("name", payload["tools"][0])

    def test_browser_text_helper_advances_multi_item_searches(self):
        with TemporaryDirectory() as directory:
            key = Path(directory) / "key"; key.write_text("gateway-secret")
            client = GatewayClient(self._config(key))
            response = {"choices": [{"message": {"content": '{"text":"whole milk"}'}}]}
            with patch.object(client, "_request", return_value=json.dumps(response).encode()) as request:
                context = {"goal": "add bananas and milk", "recent_actions": [{"action": "Search", "text": "bananas"}]}
                self.assertEqual(client.text_value(context), "whole milk")
        payload = json.loads(request.call_args.args[1])
        prompt = payload["messages"][0]["content"]
        self.assertIn("next unfinished item", prompt)
        self.assertEqual(json.loads(payload["messages"][1]["content"]), context)


class OmarchyVisualizerTests(unittest.IsolatedAsyncioTestCase):
    async def capture(self, scores, max_seconds=20):
        session = OmarchySession(SimpleNamespace(omarchy_end_silence_ms=480, omarchy_max_utterance_seconds=max_seconds))
        session._vad = SimpleNamespace(reset_states=Mock(), predict=Mock(side_effect=scores))
        process = SimpleNamespace(stdout=SimpleNamespace(readexactly=AsyncMock(return_value=b"\x01\x00" * 480)), returncode=None, terminate=Mock(), wait=AsyncMock())
        with patch("omarchy_ai.voice.omarchy.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            pcm = await session._capture_turn()
        process.terminate.assert_called_once()
        process.wait.assert_awaited_once()
        return pcm, process.stdout.readexactly.await_count

    async def test_capture_ends_after_speech_and_480ms_silence(self):
        pcm, count = await self.capture([0.] * 10 + [1.] * 10 + [0.] * 16)
        self.assertEqual(count, 36)
        self.assertEqual(len(pcm), 35 * 960)

    async def test_capture_ignores_silence(self):
        pcm, count = await self.capture([0.] * 34, max_seconds=1)
        self.assertEqual(pcm, b"")
        self.assertEqual(count, 34)

    async def test_capture_does_not_cut_at_short_pause(self):
        _, count = await self.capture([1.] * 10 + [0.] * 5 + [1.] * 10 + [0.] * 16)
        self.assertEqual(count, 41)

    async def test_capture_bounds_continuous_speech(self):
        pcm, count = await self.capture([1.] * 34, max_seconds=1)
        self.assertEqual(len(pcm), 34 * 960)
        self.assertEqual(count, 34)

    def session(self, responses):
        session = OmarchySession(SimpleNamespace())
        session._instructions = lambda: "Test assistant"
        session.client = SimpleNamespace(chat=Mock(side_effect=responses))
        session._guard.run = Mock(return_value=SimpleNamespace(ok=True, message="Switched workspace."))
        return session

    async def test_jev_direct_action_skips_chat(self):
        session = self.session([])
        reply = await session._respond("workspace five", {"risk": {"choice": "normal"}, "needs_desktop_action": {"probability": .99}, "fast_action": {"choice": "workspace_5", "probabilities": {"workspace_5": 1}}})
        self.assertEqual(reply, "Switched workspace.")
        session.client.chat.assert_not_called()
        self.assertEqual(session._guard.run.call_args.args[1:], ("workspace_switch", {"number": 5}))

    async def test_empty_retry_and_namespaced_action(self):
        session = self.session([{}, {"tool_calls": [{"id": "a", "function": {"name": "default_api.workspace_switch", "arguments": '{"number":5}'}}]}, {"content": "Done."}])
        self.assertEqual(await session._respond("workspace five", {}), "Done.")
        self.assertEqual(session._guard.run.call_count, 1)
        self.assertEqual(session._guard.run.call_args.args[1], "workspace_switch")

    async def test_clarification_never_executes(self):
        session = self.session([{"tool_calls": [{"id": "c", "function": {"name": "close_window", "arguments": "{}"}}]}, {"content": "Which window should I close?"}])
        await session._respond("move it", {"risk": {"choice": "clarify"}, "needs_desktop_action": {"probability": .99}, "fast_action": {"choice": "workspace_5"}})
        session._guard.run.assert_not_called()

    async def test_acknowledgement_does_not_request_clarification(self):
        session = self.session([])
        self.assertEqual(await session._respond("OK.", {"risk": {"choice": "clarify"}}), "Okay.")
        session.client.chat.assert_not_called()

    async def test_unknown_namespace_rejected(self):
        session = self.session([])
        self.assertFalse((await session._execute("evil.workspace_switch", {}))["ok"])
        session._guard.run.assert_not_called()

    async def test_empty_retries_are_bounded(self):
        session = self.session([{}, {}, {}])
        reply = await session._respond("hello", {})
        self.assertIn("no answer", reply)
        self.assertEqual(session.client.chat.call_count, 3)

    async def test_gateway_messages_have_one_leading_system_prompt(self):
        session = self.session([{"content": "Understood."}])
        await session._respond("hello", {"risk": {"choice": "normal"}, "needs_desktop_action": {"probability": .1}})
        messages = session.client.chat.call_args.args[0]
        self.assertEqual(sum(message["role"] == "system" for message in messages), 1)
        self.assertEqual(messages[0]["role"], "system")

    async def test_pcm_speech_drives_existing_visualizer_levels(self):
        config = SimpleNamespace(voice="marin", watchdog_enabled=True)
        session = OmarchySession(config)
        session.client = SimpleNamespace(speech=lambda _text: b"\x10\x00" * 960)
        fake_process = SimpleNamespace(stdin=SimpleNamespace(write=lambda _data: None, drain=AsyncMock(), close=lambda: None), returncode=0, wait=AsyncMock())
        with patch("omarchy_ai.voice.omarchy.asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_process)), patch("omarchy_ai.voice.omarchy.watchdog.level") as level, patch("omarchy_ai.voice.omarchy.watchdog.state"):
            await session._speak("hello")
        self.assertTrue(level.called)
        self.assertEqual(level.call_args_list[-1].args[0], 0)


if __name__ == "__main__":
    unittest.main()
