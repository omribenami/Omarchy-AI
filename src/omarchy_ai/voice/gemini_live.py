"""Minimal Gemini Live audio session.

Gemini Live is a WebSocket protocol, separate from OpenAI's WebRTC session.
This adapter keeps the same wake-to-session lifecycle and uses the local
PipeWire microphone/speaker, while leaving desktop actions on the OpenAI path
until Gemini tool declarations are mapped explicitly.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import math
import struct

from ..core.history import append_session
from ..config import Config
from . import status_icon, watchdog

log = logging.getLogger("omarchy_ai.voice.gemini")


class GeminiLiveSession:
    def __init__(self, config: Config):
        self.config = config
        self._hangup = asyncio.Event()
        self._transcript: list[dict] = []

    def _key(self) -> str:
        with open(self.config.gemini_api_key_path) as f:
            return f.read().strip()

    async def run(self) -> None:
        try:
            from google import genai
            from google.genai import types
        except ImportError as error:
            raise RuntimeError("Gemini provider requires the google-genai package; run uv sync") from error
        client = genai.Client(api_key=self._key())
        mic = subprocess.Popen(
            ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", "--latency", "20ms", "-a", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        speaker = subprocess.Popen(
            ["pw-play", "--rate", "24000", "--channels", "1", "--format", "s16", "--latency", "120ms", "-a", "-"],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        try:
            live_config = {
                "response_modalities": ["AUDIO"],
                "system_instruction": self.config.instructions,
                "input_audio_transcription": {},
                "output_audio_transcription": {},
            }
            log.info("connecting to Gemini Live model %s", self.config.gemini_model)
            async with client.aio.live.connect(model=self.config.gemini_model, config=live_config) as session:
                log.info("Gemini Live session connected")
                status_icon.set_live(True)
                if self.config.watchdog_enabled:
                    watchdog.start(self.config.watchdog_display_mode)
                    watchdog.state("listening")
                async def send_audio() -> None:
                    while not self._hangup.is_set():
                        chunk = await asyncio.get_running_loop().run_in_executor(None, mic.stdout.read, 640)
                        if not chunk:
                            self._hangup.set(); return
                        await session.send_realtime_input(audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))

                sender = asyncio.create_task(send_audio())
                try:
                    # The SDK's receive() iterator ends normally at each
                    # turn_complete boundary. Re-enter it for the next VAD
                    # turn; ending one iterator is not a disconnected socket.
                    while not self._hangup.is_set():
                        async for message in session.receive():
                            server = message.server_content
                            if server and server.input_transcription:
                                text = server.input_transcription.text or ""
                                if text:
                                    self._transcript.append({"role": "user", "text": text})
                            if server and server.output_transcription:
                                text = server.output_transcription.text or ""
                                if text:
                                    self._transcript.append({"role": "assistant", "text": text})
                            if server and server.model_turn and speaker.stdin:
                                for part in server.model_turn.parts or []:
                                    blob = getattr(part, "inline_data", None)
                                    if blob and blob.data:
                                        watchdog.state("speaking")
                                        raw = blob.data
                                        if isinstance(raw, str):
                                            raw = raw.encode()
                                        samples = struct.unpack("<%dh" % (len(raw) // 2), raw[:len(raw) - len(raw) % 2])
                                        rms = math.sqrt(sum(float(v) * v for v in samples) / max(1, len(samples)))
                                        if self.config.watchdog_enabled:
                                            watchdog.level(min(1.0, rms / 12000.0))
                                        speaker.stdin.write(raw)
                                        speaker.stdin.flush()
                            if self._hangup.is_set():
                                break
                        if self.config.watchdog_enabled and not self._hangup.is_set():
                            watchdog.state("listening")
                except Exception:
                    log.exception("Gemini Live session failed while receiving audio")
                    raise
                finally:
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
        finally:
            if self.config.watchdog_enabled:
                watchdog.stop()
            status_icon.set_live(False)
            self._hangup.set()
            mic.terminate(); speaker.terminate()
            try: mic.wait(timeout=2); speaker.wait(timeout=2)
            except subprocess.TimeoutExpired: pass
            append_session(self._transcript, self.config.context_retention_hours)
