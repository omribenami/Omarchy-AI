"""A realtime voice model through Vercel AI Gateway, behind the session
interface of google-genai's Live API.

GeminiLiveSession (and the phone bridge on top of it) talks to one object:
send_realtime_input / send_client_content / send_tool_response / receive.
This module gives it the same object for a Gateway model id such as
"openai/gpt-realtime-2", so a conversation whose Gemini model fails can go on
with another company's model on the Gateway key, with the same tools, echo
gate, stuck-turn guard and transcript (voice/fallback.py).

Wire format, probed against the live endpoint on 2026-10-05:
- POST /v1/realtime/client-secrets {"model"} with the Gateway key returns a
  single-use {"token"}; the socket is /v4/ai/realtime-model?ai-model-id=<id>
  with subprotocols ai-gateway-realtime.v1 and ai-gateway-auth.<token>.
- The first frame must be {"type": "session-update", "config": {...}}
  ("session-start" closes the socket with 1008).
- Events are the AI SDK's normalized realtime events
  (@ai-sdk/provider realtime-model/v4), one JSON text frame each.

Gemini itself is NOT routed through here. The Gateway's Google mapping
refuses what this assistant's Gemini session depends on (probed the same
day, each alone: speech-detection sensitivity and silence/prefix timings,
activity handling, transcription language hints, per-tool
BLOCKING/NON_BLOCKING; all of them left the session without a
"session-created"), and it has no clientContent turns. Gemini keeps its
direct connection and key.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from . import fallback

log = logging.getLogger("omarchy_ai.voice.gateway_live")

SECRETS_URL = "https://ai-gateway.vercel.sh/v1/realtime/client-secrets"
SOCKET_URL = "wss://ai-gateway.vercel.sh/v4/ai/realtime-model"
MIC_RATE = 16000    # what GeminiLiveSession sends
MODEL_RATE = 24000  # OpenAI realtime PCM, in and out (Gemini's output rate too)


def session_config(live_config: dict) -> dict:
    """The normalized session config for a google-genai Live config
    (gemini_live.build_live_config)."""
    tools = [{"type": "function", "name": t["name"], "description": t.get("description", ""),
              "parameters": t.get("parameters_json_schema") or {"type": "object", "properties": {}}}
             for group in live_config.get("tools") or [] for t in group.get("function_declarations") or []]
    detection = (live_config.get("realtime_input_config") or {}).get("automatic_activity_detection") or {}
    return {
        "instructions": live_config.get("system_instruction") or "",
        "outputModalities": ["audio"],
        "inputAudioFormat": {"type": "audio/pcm", "rate": MODEL_RATE},
        "outputAudioFormat": {"type": "audio/pcm", "rate": MODEL_RATE},
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
        "turnDetection": {"type": "server-vad",
                          "silenceDurationMs": detection.get("silence_duration_ms", 600),
                          "prefixPaddingMs": detection.get("prefix_padding_ms", 300)},
        "tools": tools,
    }


def _mint(key: str, model: str) -> str:
    request = urllib.request.Request(
        SECRETS_URL, data=json.dumps({"model": model}).encode(), method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())["token"]
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:300]
        raise ConnectionError(f"Vercel AI Gateway refused a realtime session: HTTP {error.code} {detail}") from error
    except (OSError, ValueError, KeyError) as error:
        raise ConnectionError(f"Vercel AI Gateway is unavailable: {error}") from error


def _text(turns) -> str:
    """The text of a google-genai `turns` value (a dict or types.Content,
    or a list of them); other parts (images, files) have no realtime form."""
    out = []
    for turn in turns if isinstance(turns, (list, tuple)) else [turns]:
        parts = turn.get("parts") if isinstance(turn, dict) else getattr(turn, "parts", None)
        for part in parts or []:
            text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
            if text:
                out.append(text)
    return "\n".join(out)


def _message(**server) -> SimpleNamespace:
    content = SimpleNamespace(input_transcription=None, output_transcription=None, interrupted=False,
                              turn_complete=False, model_turn=None)
    content.__dict__.update(server)
    return SimpleNamespace(tool_call=None, tool_call_cancellation=None, server_content=content if server else None)


class GatewayLiveSession:
    def __init__(self, socket, model: str):
        self._socket = socket
        self._model = model
        self._responding = False    # a response is being generated
        self._owed = False          # a response to start once this one is done
        self._called = False        # this response asked for a tool

    async def _send(self, event: dict) -> None:
        await self._socket.send(json.dumps(event))

    async def _respond(self) -> None:
        # A second response-create while one runs is an error on OpenAI's side.
        if self._responding:
            self._owed = True
        else:
            self._responding = True
            await self._send({"type": "response-create"})

    @staticmethod
    def _resample(pcm: bytes) -> bytes:
        """16 kHz mic frames to the model's 24 kHz (linear; 20ms frames keep the spacing exact)."""
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        if not samples.size:
            return b""
        positions = np.arange(samples.size * MODEL_RATE // MIC_RATE) * (MIC_RATE / MODEL_RATE)
        return np.interp(positions, np.arange(samples.size), samples).astype("<i2").tobytes()

    async def send_realtime_input(self, *, audio=None, **_) -> None:
        if audio is not None and audio.data:
            await self._send({"type": "input-audio-append",
                              "audio": base64.b64encode(self._resample(audio.data)).decode()})

    async def send_client_content(self, *, turns, turn_complete: bool = True) -> None:
        text = _text(turns)
        if text:
            await self._send({"type": "conversation-item-create",
                              "item": {"type": "text-message", "role": "user", "text": text}})
        if turn_complete:
            await self._respond()

    async def send_tool_response(self, *, function_responses) -> None:
        responses = function_responses if isinstance(function_responses, (list, tuple)) else [function_responses]
        for response in responses:
            await self._send({"type": "conversation-item-create", "item": {
                "type": "function-call-output", "callId": response.id, "name": response.name,
                "output": json.dumps(response.response, ensure_ascii=False)}})
        await self._respond()

    async def receive(self):
        """The messages of one model turn, like google-genai's receive()."""
        import websockets
        while True:
            try:
                event = json.loads(await self._socket.recv())
            except websockets.ConnectionClosedOK as closed:
                # The Gateway ends every realtime session after 25 minutes.
                raise fallback.Reconnect(f"Gateway realtime session ended: {closed}") from closed
            except websockets.ConnectionClosed as closed:
                raise ConnectionError(f"Gateway realtime connection closed: {closed}") from closed
            kind = event.get("type")
            if kind == "audio-delta":
                data = base64.b64decode(event.get("delta") or "")
                yield _message(model_turn=SimpleNamespace(parts=[SimpleNamespace(inline_data=SimpleNamespace(data=data))]))
            elif kind == "audio-transcript-delta":
                yield _message(output_transcription=SimpleNamespace(text=event.get("delta") or ""))
            elif kind == "input-transcription-completed":
                if event.get("transcript"):
                    yield _message(input_transcription=SimpleNamespace(text=event["transcript"]))
            elif kind == "function-call-arguments-done":
                self._called = True
                try:
                    args = json.loads(event.get("arguments") or "{}")
                except ValueError:
                    args = {}
                call = SimpleNamespace(id=event.get("callId"), name=event.get("name"),
                                       args=args if isinstance(args, dict) else {})
                message = _message()
                message.tool_call = SimpleNamespace(function_calls=[call])
                yield message
            elif kind == "response-created":
                self._responding, self._called = True, False
            elif kind == "speech-started":
                if self._responding:
                    yield _message(interrupted=True)  # the server cancels her reply on its own
            elif kind == "response-done":
                self._responding = False
                called, self._called = self._called, False
                if self._owed:
                    self._owed = False
                    await self._respond()
                elif not called:  # after a tool call her turn goes on with its result
                    yield _message(turn_complete=True)
                    return
            elif kind == "session-closed":
                raise fallback.Reconnect(f"Gateway realtime session ended: {event.get('reason')}")
            elif kind == "error":
                log.warning("Gateway realtime error: model=%s code=%s %s", self._model, event.get("code"),
                            str(event.get("message"))[:300])


@asynccontextmanager
async def connect(config, model: str, live_config: dict):
    """`async with connect(...) as session`, as client.aio.live.connect."""
    import websockets
    key = Path(config.vercel_gateway_api_key_path).read_text().strip()
    token = await asyncio.to_thread(_mint, key, model)
    url = SOCKET_URL + "?ai-model-id=" + urllib.parse.quote(model, safe="")
    try:
        socket = await websockets.connect(url, subprotocols=["ai-gateway-realtime.v1", "ai-gateway-auth." + token],
                                          open_timeout=10, max_size=None)
    except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as error:
        raise ConnectionError(f"Vercel AI Gateway realtime connect failed: {error}") from error
    try:
        await socket.send(json.dumps({"type": "session-update", "config": session_config(live_config)}))
        yield GatewayLiveSession(socket, model)
    finally:
        await socket.close()
