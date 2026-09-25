"""Render the spoken quota alerts (voice/alerts/quota-<provider>-<lang>.ogg).

When a provider is out of credits the assistant cannot use it to speak, and
this machine has no local TTS engine, so the warnings are pre-rendered once
in the assistant's own Gemini voice and shipped with the package. Re-run
after changing the wording in omarchy_ai.core.quota.MESSAGES:

    .venv/bin/python scripts/make_quota_clips.py

Each clip is checked against Gemini's own transcript of what it said.
"""
import asyncio
from pathlib import Path
import subprocess
import tempfile
import wave

from google import genai

from omarchy_ai.config import load_config
from omarchy_ai.core.quota import CLIP_DIR, MESSAGES

cfg = load_config()
client = genai.Client(api_key=Path(cfg.gemini_api_key_path).read_text().strip())


async def speak(text: str) -> tuple[bytes, str]:
    audio, heard = bytearray(), []
    config = {"response_modalities": ["AUDIO"], "output_audio_transcription": {},
              "system_instruction": "You are a text-to-speech engine. Read the user's text aloud exactly as written, "
                                    "calmly and clearly, in its own language. Say nothing else."}
    async with client.aio.live.connect(model=cfg.gemini_model, config=config) as session:
        await session.send_client_content(turns={"role": "user", "parts": [{"text": text}]}, turn_complete=True)
        async for message in session.receive():
            content = message.server_content
            if content and content.model_turn:
                for part in content.model_turn.parts or []:
                    if part.inline_data:
                        audio += part.inline_data.data
            if content and content.output_transcription and content.output_transcription.text:
                heard.append(content.output_transcription.text)
            if content and content.turn_complete:
                break
    return bytes(audio), "".join(heard)


async def main():
    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    for (provider, lang), text in MESSAGES.items():
        pcm, heard = await speak(text)
        with tempfile.NamedTemporaryFile(suffix=".wav") as raw:
            with wave.open(raw.name, "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(24000)
                out.writeframes(pcm)
            target = CLIP_DIR / f"quota-{provider}-{lang}.ogg"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw.name, "-c:a", "libvorbis", "-q:a", "4",
                            str(target)], check=True)
        print(f"{target.name}: {len(pcm) / 48000:.1f}s\n  text:  {text}\n  heard: {' '.join(heard.split())}")


asyncio.run(main())
