"""Fall back to an older Gemini Live model when the default fails on Google's side.

2026-09-28: gemini-3.8-live closed calls with "1011 Internal error
encountered" at 08:35, 08:50 and 09:29, then failed every probe session
(6 of 6, even a bare "Say OK" with no tools), while
gemini-3.1-flash-live-preview and gemini-2.5-flash-native-audio-latest
answered normally on the same key. To the user she just "turned herself
off".

Now a provider failure moves the conversation to the next model in
Config.gemini_fallback_models, carrying the recent transcript over. A failed
model is skipped for DOWN_SECONDS so the next call does not hit it first.

The notices are pre-rendered clips in the user's language
(core/alert_clips.py): a model that is failing cannot say that it is
failing, and this machine has no local TTS engine.
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
import time

from ..core import alert_clips
from ..core.quota import is_quota_error

log = logging.getLogger("omarchy_ai.voice.fallback")

DOWN_SECONDS = 600
ANNOUNCE_EVERY = 600
CARRY_OVER_TURNS = 20
CARRY_OVER_CHARS = 4000

COMPANIES = {"gemini": "Google"}

# Spoken text, in English; the clips are rendered from these strings (see
# texts(): the "switching" one names the default model).
SWITCHING = "The default model {model} is experiencing issues on {company}'s side, falling back to an older model."
FAILED = "Sorry, falling back did not fix the problem, please try a different provider."

# Closed by the server for its own reasons, not ours: 1011 is "internal
# error", 1006 an abnormal close, 503/UNAVAILABLE an overloaded backend.
_FAILURE = re.compile(r"\b1011\b|\b1006\b|internal error|\b503\b|unavailable|deadline exceeded", re.I)

_lock = threading.Lock()
_down: dict[str, float] = {}
_announced_at = -1e9


def display_name(model: str) -> str:
    """gemini-3.8-live -> Gemini 3.8 Live"""
    return " ".join(part if part[:1].isdigit() else part.capitalize() for part in model.split("-"))


def company(model: str) -> str:
    return COMPANIES.get(model.split("-", 1)[0], "the provider")


def is_provider_failure(error: BaseException) -> bool:
    text = str(error)
    return bool(_FAILURE.search(text)) and not is_quota_error(text)


def chain(config) -> list[str]:
    """Models to try, in order, skipping ones that failed recently (all of
    them if every one did)."""
    models = list(dict.fromkeys([config.gemini_model, *config.gemini_fallback_models]))
    now = time.monotonic()
    with _lock:
        up = [m for m in models if _down.get(m, -1e9) + DOWN_SECONDS <= now]
    return up or models


def mark_down(model: str, error: BaseException) -> None:
    with _lock:
        _down[model] = time.monotonic()
    log.warning("Gemini model failed on the provider side, skipping it for %ds: model=%s error=%s",
                DOWN_SECONDS, model, " ".join(str(error).split())[:200])


def should_announce() -> bool:
    """One "falling back" notice per outage, not one per call."""
    global _announced_at
    with _lock:
        if time.monotonic() - _announced_at < ANNOUNCE_EVERY:
            return False
        _announced_at = time.monotonic()
        return True


def texts(model: str) -> dict[str, str]:
    """{clip stem: English text}: "switching" for this default model, a
    generic one for a default model with no clip of its own, and "failed"."""
    return {f"fallback-switching-{model}": SWITCHING.format(model=display_name(model), company=company(model)),
            "fallback-switching": SWITCHING.replace(" {model}", "").format(company="the provider"),
            "fallback-failed": FAILED}


def clip(kind: str, model: str | None = None):
    """The notice in the user's language, else English; a "switching" clip
    for this model before the generic one."""
    stems = [f"fallback-{kind}-{model}", f"fallback-{kind}"] if kind == "switching" and model else [f"fallback-{kind}"]
    return alert_clips.find(stems)


def play_local(kind: str, model: str | None = None) -> None:
    """Play a notice through this machine's speakers (blocks until done)."""
    path = clip(kind, model)
    if path is None:
        log.error("No fallback clip for %s (%s)", kind, model)
        return
    log.info("Fallback notice played: %s", path.name)
    try:
        subprocess.run(["pw-play", str(path)], timeout=20, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        log.exception("Could not play %s", path)


def pcm(kind: str, model: str | None = None, rate: int = 24000) -> bytes:
    """A notice as mono s16 PCM, for audio that goes out over the phone call."""
    path = clip(kind, model)
    if path is None:
        log.error("No fallback clip for %s (%s)", kind, model)
        return b""
    try:
        out = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-f", "s16le", "-ac", "1",
                              "-ar", str(rate), "-"], capture_output=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        log.exception("Could not decode %s", path)
        return b""
    log.info("Fallback notice sent to the call: %s", path.name)
    return out


def carry_over(transcript: list[dict]) -> str | None:
    """Context for the fallback model: what was said before the switch."""
    # Live transcription arrives in fragments: one line per speaker turn.
    turns: list[list] = []
    for t in transcript:
        if not t.get("text"):
            continue
        if turns and turns[-1][0] == t.get("role"):
            turns[-1][1] += t["text"]
        else:
            turns.append([t.get("role"), t["text"]])
    text = "\n".join(f"{role}: {' '.join(said.split())}" for role, said in turns[-CARRY_OVER_TURNS:])
    text = text[-CARRY_OVER_CHARS:]
    if not text:
        return None
    return ("[Context, not a request] The previous voice model connection failed and you are taking over this "
            "conversation. The user has already been told. Continue naturally; do not greet or apologize. "
            "Conversation so far:\n" + text)
