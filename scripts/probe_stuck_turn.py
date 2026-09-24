"""Reproduce the "stuck thinking" freeze against the real Gemini Live API.

Sends one spoken question with the daemon's VAD settings, then keeps
streaming a background (room noise recorded from the mic now, or speech-like
babble) and times the reply. With --guard every frame goes through the
daemon's own GeminiLiveSession._gate (the stuck-turn silence splice).

    .venv/bin/python scripts/probe_stuck_turn.py

Findings 2026-09-24 are in STATUS.md ("Stuck on thinking").
"""
import asyncio
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
from google import genai
from google.genai import types

from omarchy_ai.config import load_config
from omarchy_ai.voice.gemini_live import GeminiLiveSession, build_live_config

CACHE = Path.home() / ".cache/omarchy-ai/probe_stuck_turn"
cfg = load_config()
client = genai.Client(api_key=Path(cfg.gemini_api_key_path).read_text().strip())
VAD = build_live_config(cfg)["realtime_input_config"]


async def question() -> np.ndarray:
    """"Hey, what is two plus two?" spoken by Gemini itself, at 16 kHz."""
    path = CACHE / "speech16k.raw"
    if path.exists():
        return np.fromfile(path, dtype="<i2").astype(float)
    out = bytearray()
    async with client.aio.live.connect(model=cfg.gemini_model, config={"response_modalities": ["AUDIO"],
            "system_instruction": "You are a text-to-speech engine. Speak exactly the user's text, nothing else."}) as s:
        await s.send_client_content(turns={"role": "user", "parts": [{"text": "Hey, what is two plus two?"}]}, turn_complete=True)
        async for m in s.receive():
            sc = m.server_content
            for part in (sc.model_turn.parts or []) if sc and sc.model_turn else []:
                if part.inline_data:
                    out += part.inline_data.data
            if sc and sc.turn_complete:
                break
    a = np.frombuffer(bytes(out), dtype="<i2").astype(float)
    x = np.interp(np.arange(0, len(a), 1.5), np.arange(len(a)), a)          # 24k -> 16k
    x = (x / max(1, np.abs(x).max()) * 12000).astype("<i2")
    CACHE.mkdir(parents=True, exist_ok=True)
    x.tofile(path)
    return x.astype(float)


def room_noise() -> np.ndarray:
    raw = subprocess.run(["timeout", "6", "pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", "-"],
                         capture_output=True).stdout
    a = np.frombuffer(raw[:len(raw) // 2 * 2], dtype="<i2").astype(float)[16000:]
    return a - a.mean()


async def trial(name, speech, background, guard):
    with patch("omarchy_ai.voice.gemini_live.EchoCancellation"):
        live = GeminiLiveSession(cfg)
    pos = [0]

    def bg():
        i = pos[0]
        pos[0] = (i + 320) % (len(background) - 320)
        return background[i:i + 320]

    async with client.aio.live.connect(model=cfg.gemini_model, config={"response_modalities": ["AUDIO"],
            "system_instruction": "You are a voice assistant. Answer briefly.",
            "input_audio_transcription": {}, "realtime_input_config": VAD}) as s:
        reply = {}

        async def receive():
            async for m in s.receive():
                sc = m.server_content
                if sc and sc.input_transcription and sc.input_transcription.text:
                    live._last_user_speech, live._splice_armed = time.monotonic(), True
                if sc and sc.model_turn:
                    reply["at"] = time.monotonic()
                    live._replied()
                    return

        async def send(frame):
            pcm = np.clip(frame, -32768, 32767).astype("<i2").tobytes()
            if guard:
                rms = float(np.sqrt(np.mean(np.frombuffer(pcm, dtype="<i2").astype(float) ** 2)))
                live._mic_levels.append((time.monotonic(), rms, 0))
                pcm = live._gate(pcm, rms, time.monotonic())[0]
            await s.send_realtime_input(audio=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000"))
            await asyncio.sleep(0.02)

        task = asyncio.create_task(receive())
        for _ in range(50):
            await send(bg())
        for frame in speech[:len(speech) // 320 * 320].reshape(-1, 320):
            await send(frame + bg())
        end = time.monotonic()
        while not task.done() and time.monotonic() - end < 40:
            await send(bg())
        task.cancel()
        took = f"{reply['at'] - end:.1f}s" if reply else "NONE in 40s"
        print(f"{name:34s} reply {took:12s} splices={live._splices}", flush=True)


async def main():
    speech = await question() * 2      # ~ the logged user speech level (RMS 5100-12000)
    noise = room_noise()
    babble = np.tile(speech / 2, 20)   # someone talking in the background
    for name, background, guard in [
        ("silence", np.zeros(32000), False),
        ("room noise", noise, False),
        ("background speech x0.1", babble * 0.1, False),
        ("background speech x0.1 + guard", babble * 0.1, True),
        ("background speech x0.3 + guard", babble * 0.3, True),
        ("room noise + guard", noise, True),
    ]:
        await trial(name, speech, background, guard)


asyncio.run(main())
