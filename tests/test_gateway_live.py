import base64
import json
import unittest

import numpy as np
from google.genai import types

from omarchy_ai.voice import fallback, gateway_live
from omarchy_ai.voice.gateway_live import GatewayLiveSession


class FakeSocket:
    """Server events to hand out, client events as they were sent."""

    def __init__(self, *events):
        self.events = list(events)
        self.sent = []

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return json.dumps(event)


async def turn(session):
    return [message async for message in session.receive()]


class SessionConfigTests(unittest.TestCase):
    def test_live_config_becomes_the_normalized_session_config(self):
        config = gateway_live.session_config({
            "system_instruction": "Be brief.",
            "realtime_input_config": {"automatic_activity_detection": {"silence_duration_ms": 700}},
            "tools": [{"function_declarations": [
                {"name": "list_windows", "description": "Open windows.", "behavior": "BLOCKING",
                 "parameters_json_schema": {"type": "object", "properties": {}}}]}]})
        self.assertEqual(config["instructions"], "Be brief.")
        self.assertEqual(config["tools"], [{"type": "function", "name": "list_windows", "description": "Open windows.",
                                            "parameters": {"type": "object", "properties": {}}}])
        self.assertEqual(config["turnDetection"], {"type": "server-vad", "silenceDurationMs": 700, "prefixPaddingMs": 300})
        self.assertEqual(config["inputAudioFormat"]["rate"], 24000)


class GatewayLiveSessionTests(unittest.IsolatedAsyncioTestCase):
    """The wire format is the one probed against the Gateway on 2026-10-05."""

    async def test_mic_audio_is_resampled_to_the_models_rate(self):
        socket = FakeSocket()
        frame = (np.arange(320) * 10).astype("<i2").tobytes()         # 20ms at 16 kHz
        await GatewayLiveSession(socket, "openai/gpt-realtime-2").send_realtime_input(
            audio=types.Blob(data=frame, mime_type="audio/pcm;rate=16000"))
        sent = np.frombuffer(base64.b64decode(socket.sent[0]["audio"]), dtype="<i2")
        self.assertEqual(socket.sent[0]["type"], "input-audio-append")
        self.assertEqual(sent.size, 480)                               # 20ms at 24 kHz
        self.assertEqual((int(sent[0]), int(sent[3])), (0, 20))        # same ramp, 1.5x the samples

    async def test_context_is_added_silently_and_a_turn_asks_for_a_reply(self):
        socket = FakeSocket()
        session = GatewayLiveSession(socket, "openai/gpt-realtime-2")
        await session.send_client_content(turns={"role": "user", "parts": [{"text": "[Context] so far"}]},
                                          turn_complete=False)
        await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text="Hello")]),
                                          turn_complete=True)
        self.assertEqual([e["type"] for e in socket.sent],
                         ["conversation-item-create", "conversation-item-create", "response-create"])
        self.assertEqual(socket.sent[1]["item"], {"type": "text-message", "role": "user", "text": "Hello"})

    async def test_a_spoken_turn_arrives_as_google_style_messages(self):
        audio = base64.b64encode(b"\x01\x02").decode()
        session = GatewayLiveSession(FakeSocket(
            {"type": "input-transcription-completed", "itemId": "i", "transcript": "what time is it"},
            {"type": "response-created", "responseId": "r"},
            {"type": "audio-delta", "delta": audio}, {"type": "audio-transcript-delta", "delta": "Six."},
            {"type": "custom", "rawType": "rate_limits.updated"},
            {"type": "response-done", "responseId": "r", "status": "completed"}), "openai/gpt-realtime-2")
        heard, sound, said, done = [m.server_content for m in await turn(session)]
        self.assertEqual(heard.input_transcription.text, "what time is it")
        self.assertEqual(sound.model_turn.parts[0].inline_data.data, b"\x01\x02")
        self.assertEqual(said.output_transcription.text, "Six.")
        self.assertTrue(done.turn_complete)

    async def test_a_tool_call_keeps_the_turn_open_until_her_answer(self):
        socket = FakeSocket(
            {"type": "response-created", "responseId": "r1"},
            {"type": "function-call-arguments-done", "callId": "c1", "name": "list_windows", "arguments": "{}"},
            {"type": "response-done", "responseId": "r1", "status": "completed"},
            {"type": "response-created", "responseId": "r2"},
            {"type": "audio-transcript-delta", "delta": "None are open."},
            {"type": "response-done", "responseId": "r2", "status": "completed"})
        session = GatewayLiveSession(socket, "openai/gpt-realtime-2")
        messages = await turn(session)
        call = messages[0].tool_call.function_calls[0]
        self.assertEqual((call.id, call.name, call.args), ("c1", "list_windows", {}))
        self.assertEqual(messages[1].server_content.output_transcription.text, "None are open.")
        self.assertTrue(messages[-1].server_content.turn_complete)
        self.assertEqual(len(messages), 3)                             # no turn_complete after the call itself

    async def test_a_tool_result_waits_for_the_reply_in_progress(self):
        # OpenAI refuses a second response while one is being generated.
        socket = FakeSocket({"type": "response-created", "responseId": "r1"},
                            {"type": "response-done", "responseId": "r1", "status": "completed"},
                            {"type": "response-created", "responseId": "r2"},
                            {"type": "response-done", "responseId": "r2", "status": "completed"})
        session = GatewayLiveSession(socket, "openai/gpt-realtime-2")
        session._responding = True
        await session.send_tool_response(function_responses=types.FunctionResponse(
            id="c1", name="search", response={"ok": True, "message": "3 results"}))
        self.assertEqual([e["type"] for e in socket.sent], ["conversation-item-create"])
        self.assertEqual(json.loads(socket.sent[0]["item"]["output"]), {"ok": True, "message": "3 results"})
        await turn(session)
        self.assertEqual(socket.sent[-1], {"type": "response-create"})

    async def test_the_user_speaking_over_her_is_an_interruption_only_while_she_answers(self):
        session = GatewayLiveSession(FakeSocket(
            {"type": "speech-started"},
            {"type": "response-created", "responseId": "r"}, {"type": "speech-started"},
            {"type": "response-done", "responseId": "r", "status": "cancelled"}), "openai/gpt-realtime-2")
        messages = await turn(session)
        self.assertEqual([m.server_content.interrupted for m in messages], [True, False])

    async def test_a_scheduled_session_end_reconnects_and_a_dropped_one_falls_back(self):
        import websockets
        from websockets.frames import Close
        ended = websockets.ConnectionClosedOK(Close(1000, "session limit"), None)
        with self.assertRaises(fallback.Reconnect):
            await turn(GatewayLiveSession(FakeSocket(ended), "openai/gpt-realtime-2"))
        dropped = websockets.ConnectionClosedError(Close(1011, "internal error"), None)
        with self.assertRaises(ConnectionError) as raised:
            await turn(GatewayLiveSession(FakeSocket(dropped), "openai/gpt-realtime-2"))
        self.assertTrue(fallback.is_provider_failure(raised.exception))


if __name__ == "__main__":
    unittest.main()
