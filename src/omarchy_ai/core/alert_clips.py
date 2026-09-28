"""Spoken alerts in the user's language, playable when no model can speak.

An alert has to be heard exactly when a model cannot talk (out of quota,
failing on the provider's side), and this machine has no local TTS engine,
so every alert is a pre-rendered clip in the assistant's own voice.

The repository ships each alert in English only (voice/alerts/<stem>-en.ogg,
made by scripts/make_quota_clips.py). The user's language is detected from
their recent words (py3langid, offline, 97 languages; Gemini Live's
transcription language_code came back empty in a probe on 2026-09-28). At
the end of a conversation in another language, while a model works, the
alerts are translated and rendered once on this machine into
STATE_DIR/alert_clips/<stem>-<lang>.ogg. Until then, or if rendering fails,
the English clip plays.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import wave

from ..config import STATE_DIR

log = logging.getLogger("omarchy_ai.core.alert_clips")

PACKAGED = Path(__file__).resolve().parent.parent / "voice" / "alerts"
LOCAL = STATE_DIR / "alert_clips"
MIN_LETTERS = 12        # less than this says nothing about the language
RETRY_AFTER = 3600      # a language that failed to render waits this long
ATTEMPTS = 4

_lock = threading.Lock()
_tried: dict[str, float] = {}


def language(default: str = "en") -> str:
    """ISO 639-1 code of the user's recent words (any language py3langid knows)."""
    try:
        lines = (STATE_DIR / "conversation_history.jsonl").read_text().splitlines()[-3:]
        text = " ".join(t["text"] for line in lines for t in json.loads(line).get("turns", [])
                        if t.get("role") == "user")
    except (OSError, ValueError, KeyError, TypeError):
        return default
    if sum(c.isalpha() for c in text) < MIN_LETTERS:
        return default
    import py3langid
    return py3langid.classify(text)[0]


def texts(config) -> dict[str, str]:
    """{clip stem: English text} for every alert."""
    from . import quota
    from ..voice import fallback
    return {**{f"quota-{provider}": text for provider, text in quota.MESSAGES.items()},
            **fallback.texts(config.gemini_model)}


def find(stems, lang: str | None = None) -> Path | None:
    """The first existing clip: the user's language before English, then the
    stems in the given order, a clip rendered here before a shipped one."""
    if isinstance(stems, str):
        stems = [stems]
    lang = lang or language()
    for code in dict.fromkeys([lang, "en"]):
        for stem in stems:
            for folder in (LOCAL, PACKAGED):
                path = folder / f"{stem}-{code}.ogg"
                if path.exists():
                    return path
    return None


def missing(config, lang: str) -> dict[str, str]:
    return {stem: text for stem, text in texts(config).items()
            if not (LOCAL / f"{stem}-{lang}.ogg").exists() and not (PACKAGED / f"{stem}-{lang}.ogg").exists()}


def prepare_in_background(config) -> None:
    """After a conversation: render the alerts in the user's language if they
    are missing. Never raises; at most one render at a time."""
    try:
        lang = language()
        todo = missing(config, lang)
        if not todo:
            return
        with _lock:
            if time.monotonic() - _tried.get(lang, -1e9) < RETRY_AFTER:
                return
            _tried[lang] = time.monotonic()
        threading.Thread(target=lambda: asyncio.run(render(config, lang, todo)), daemon=True,
                         name=f"alert-clips-{lang}").start()
    except Exception:  # noqa: BLE001
        log.exception("Could not start rendering alerts")


async def render(config, lang: str, todo: dict[str, str], dest: Path = LOCAL) -> list[Path]:
    """Speak each text in `lang` (moving to the next live model after a
    failure), keep only verified takes, and write <dest>/<stem>-<lang>.ogg."""
    from google import genai
    from ..voice import fallback
    client = genai.Client(api_key=Path(config.gemini_api_key_path).read_text().strip())
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    models, current = fallback.chain(config), 0
    try:
        for stem, text in todo.items():
            for attempt in range(ATTEMPTS):
                model = models[current % len(models)]
                try:
                    pcm, heard = await _speak(client, model, text, lang)
                except Exception as error:  # noqa: BLE001
                    # 2026-09-28: a flaky gemini-3.8-live failed three takes in a row; the next model did not.
                    log.info("Alert %s-%s attempt %d on %s failed: %s", stem, lang, attempt + 1, model,
                             str(error)[:80] or type(error).__name__)
                    current += 1
                    continue
                if len(heard.strip()) < 0.5 * len(text):
                    # Live's transcript is often partial or empty for non-English speech.
                    heard = await asyncio.to_thread(_transcribe, client, pcm) or heard
                if good(text, lang, pcm, heard):
                    path = dest / f"{stem}-{lang}.ogg"
                    await asyncio.to_thread(_write_ogg, pcm, path)
                    written.append(path)
                    log.info("Alert clip rendered: %s (%.1fs, %s): %s", path.name, len(pcm) / 48000, model,
                             " ".join(heard.split())[:120])
                    break
                log.info("Alert %s-%s take %d rejected (%.1fs): %r", stem, lang, attempt + 1, len(pcm) / 48000,
                         heard[:80])
            else:
                log.warning("Alert clip %s-%s: no good take", stem, lang)
    finally:
        await client.aio.aclose()
    return written


async def _speak(client, model: str, text: str, lang: str) -> tuple[bytes, str]:
    if lang == "en":
        task = "Read the user's text aloud exactly as written, calmly and clearly, in its own language."
    else:
        task = (f"Translate the user's text into the language with ISO 639-1 code '{lang}' and read only the "
                "translation aloud, calmly and clearly. Keep names and model names as written. Address the "
                "listener gender-neutrally where the language marks gender.")
    config = {"response_modalities": ["AUDIO"], "output_audio_transcription": {},
              "system_instruction": f"You are a text-to-speech engine. {task} Say nothing else."}
    audio, heard = bytearray(), []
    async with client.aio.live.connect(model=model, config=config) as session:
        await session.send_client_content(turns={"role": "user", "parts": [{"text": text}]}, turn_complete=True)
        async with asyncio.timeout(60):
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


def good(text: str, lang: str, pcm: bytes, heard: str) -> bool:
    """Keep a take only if its length fits the text and what was said is
    the right thing in the right language (2026-09-28: a flaky
    gemini-3.8-live returned a 0.7s Hebrew take and a 13.4s English one)."""
    seconds = len(pcm) / 48000
    if lang == "en":
        if not len(text) / 22 <= seconds <= len(text) / 8:
            return False
        said = {w.strip(".,'").lower() for w in heard.split()}
        words = {w.strip(".,'").lower() for w in text.split()}
        return len(said & words) >= 0.7 * len(words)
    # A translation runs shorter or longer than the English it came from,
    # and what was heard must cover most of it, not a fragment.
    if not len(text) / 30 <= seconds <= len(text) / 5 or len(heard.strip()) < 0.5 * len(text):
        return False
    import py3langid
    return py3langid.classify(heard)[0] == lang


def _transcribe(client, pcm: bytes) -> str:
    """Live often returns no transcript for non-English speech."""
    buf = io.BytesIO()
    _write_wav(pcm, buf)
    try:
        from google.genai import types
        response = client.models.generate_content(model="gemini-2.5-flash", contents=[
            types.Part.from_bytes(data=buf.getvalue(), mime_type="audio/wav"),
            "Transcribe this audio exactly, in its own language and script. Output only the transcript."])
        return response.text or ""
    except Exception as error:  # noqa: BLE001
        log.info("Alert take transcription failed: %s", str(error)[:80])
        return ""


def _write_wav(pcm: bytes, target) -> None:
    with wave.open(target, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(24000)
        out.writeframes(pcm)


def _write_ogg(pcm: bytes, path: Path) -> None:
    with tempfile.NamedTemporaryFile(suffix=".wav") as raw:
        _write_wav(pcm, raw.name)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw.name, "-c:a", "libvorbis", "-q:a", "4",
                        str(path)], check=True)
