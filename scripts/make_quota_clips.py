"""Render the spoken alerts in voice/alerts/: quota (quota-<provider>-<lang>.ogg)
and model fallback (fallback-*.ogg, see omarchy_ai.voice.fallback).

When a provider is out of credits the assistant cannot use it to speak, and
this machine has no local TTS engine, so the warnings are pre-rendered once
in the assistant's own Gemini voice and shipped with the package. Re-run
after changing the wording in omarchy_ai.core.quota.MESSAGES or
omarchy_ai.voice.fallback.MESSAGES, or after changing gemini_model (the
"switching" clip names it):

    .venv/bin/python scripts/make_quota_clips.py [quota|fallback]

It speaks with the first live model that works (the default may be the one
that is down).

Each clip is checked against Gemini's own transcript of what it said.
"""
import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
import wave

from google import genai

from omarchy_ai.config import load_config
from omarchy_ai.core.quota import CLIP_DIR, MESSAGES
from omarchy_ai.voice import fallback

cfg = load_config()
client = genai.Client(api_key=Path(cfg.gemini_api_key_path).read_text().strip())


def clips() -> list[tuple[str, str]]:
    """(file name, text) for every clip to render."""
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    out = []
    if which in ("all", "quota"):
        out += [(f"quota-{provider}-{lang}.ogg", text) for (provider, lang), text in MESSAGES.items()]
    if which in ("all", "fallback"):
        model = cfg.gemini_model
        generic = {"en": "the provider", "he": "הספק"}
        for (kind, lang), text in fallback.MESSAGES.items():
            if kind == "switching":
                out.append((fallback.clip_name(kind, lang, model),
                            text.format(model=fallback.display_name(model), company=fallback.company(model))))
                # For a default model with no clip of its own.
                out.append((fallback.clip_name(kind, lang),
                            text.replace(" {model}", "").format(company=generic[lang])))
            else:
                out.append((fallback.clip_name(kind, lang), text))
    return out


async def speak(text: str) -> tuple[bytes, str]:
    audio, heard = bytearray(), []
    config = {"response_modalities": ["AUDIO"], "output_audio_transcription": {},
              "system_instruction": "You are a text-to-speech engine. Read the user's text aloud exactly as written, "
                                    "calmly and clearly, in its own language. Say nothing else."}
    async with client.aio.live.connect(model=VOICE_MODEL, config=config) as session:
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


def transcribe(pcm: bytes) -> str:
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(24000)
        out.writeframes(pcm)
    try:
        response = client.models.generate_content(model="gemini-2.5-flash", contents=[
            genai.types.Part.from_bytes(data=buf.getvalue(), mime_type="audio/wav"),
            "Transcribe this audio exactly, in its own language and script. Output only the transcript."])
        return response.text or ""
    except Exception as error:  # noqa: BLE001
        print(f"  transcription failed: {str(error)[:60]}")
        return ""


def good(text: str, pcm: bytes, heard: str) -> bool:
    """A take is kept only if its length fits the text (~12-20 chars/s of
    speech) and Gemini's own transcript matches it: 2026-09-28 a flaky
    gemini-3.8-live returned a 0.7s Hebrew clip and a 13.4s English one."""
    seconds = len(pcm) / 48000
    if not len(text) / 22 <= seconds <= len(text) / 8:
        return False
    said = {w.strip(".,'").lower() for w in heard.split()}
    words = {w.strip(".,'").lower() for w in text.split()}
    return len(said & words) >= 0.7 * len(words)


async def main():
    global VOICE_MODEL
    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    for VOICE_MODEL in [cfg.gemini_model, *cfg.gemini_fallback_models]:
        try:
            await speak("OK.")
            break
        except Exception as error:  # noqa: BLE001
            print(f"{VOICE_MODEL} unavailable ({str(error)[:60]}), trying the next model")
    print(f"voice: {VOICE_MODEL}")
    for name, text in clips():
        for attempt in range(4):
            try:
                pcm, heard = await speak(text)
            except Exception as error:  # noqa: BLE001
                print(f"  {name}: attempt {attempt + 1} failed: {str(error)[:60]}")
                continue
            if not heard.strip():
                # Live often returns no transcript for Hebrew: transcribe the take itself.
                heard = transcribe(pcm)
            if good(text, pcm, heard):
                break
            print(f"  {name}: attempt {attempt + 1} rejected ({len(pcm) / 48000:.1f}s, heard {heard!r:.60})")
        else:
            raise SystemExit(f"{name}: no good take")
        with tempfile.NamedTemporaryFile(suffix=".wav") as raw:
            with wave.open(raw.name, "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(24000)
                out.writeframes(pcm)
            target = CLIP_DIR / name
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw.name, "-c:a", "libvorbis", "-q:a", "4",
                            str(target)], check=True)
        print(f"{target.name}: {len(pcm) / 48000:.1f}s\n  text:  {text}\n  heard: {' '.join(heard.split())}")


asyncio.run(main())
