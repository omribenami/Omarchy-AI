"""Fall back to another voice model when the default one fails.

2026-09-28: gemini-3.8-live closed calls with "1011 Internal error
encountered" at 08:35, 08:50 and 09:29, then failed every probe session
(6 of 6, even a bare "Say OK" with no tools), while
gemini-3.1-flash-live-preview and gemini-2.5-flash-native-audio-latest
answered normally on the same key. To the user she just "turned herself
off".

Now a provider failure moves the conversation to the next model in
Config.gemini_fallback_models, carrying the recent transcript over. A failed
model is skipped for DOWN_SECONDS so the next call does not hit it first.

The chain crosses companies and keys (2026-10-05): the default Gemini model
on the Gemini key, then an OpenAI realtime model on the Vercel AI Gateway
key (voice/gateway_live.py; a model id with a "/" is a Gateway id), then
the older Gemini models. So one company's outage, an exhausted quota or a
missing key no longer ends the conversation: a model whose key file is
absent is left out of the chain, and a quota error skips every model on the
same key.

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
from pathlib import Path

from ..core import alert_clips, provider_health, retry
from ..core.quota import is_quota_error

log = logging.getLogger("omarchy_ai.voice.fallback")

DOWN_SECONDS = 600
ANNOUNCE_EVERY = 600
CARRY_OVER_TURNS = 20
CARRY_OVER_CHARS = 4000

COMPANIES = {"gemini": "Google"}

# Spoken text, in English; the clips are rendered from these strings (see
# texts(): the "switching" one names the default model).
SWITCHING = "The default model {model} is experiencing issues on {company}'s side, switching to a backup model."
FAILED = "Sorry, falling back did not fix the problem, please try a different provider."
# Said when she starts working without a word (gemini_live._silent_work_due).
WORKING = "On it, one moment."

# Closed by the server for its own reasons, not ours: 1011 is "internal
# error", 1006 an abnormal close, 503/UNAVAILABLE an overloaded backend.
# Since 2026-10-05 also what a different company's model can cure: a refused
# or timed-out connection, a rejected key, other server-side codes.
_FAILURE = re.compile(
    r"\b1011\b|\b1006\b|\b1012\b|\b1013\b|\b1014\b|internal error|\b50[0234]\b|\b529\b|unavailable|"
    r"deadline exceeded|overloaded|timed out|timeout|connect failed|connection (closed|refused|reset)|"
    r"\b401\b|\b403\b|api key|unauthenticated|permission denied", re.I)


class Reconnect(ConnectionError):
    """This connection is over through no fault of the model: connect again
    to the same model and carry the conversation over."""

_lock = threading.Lock()
_down: dict[str, float] = {}
_announced_at = -1e9
HEALTH_KIND = "voice_model"


def _restore(entries) -> None:
    """After a restart: a model that failed before it is still skipped."""
    with _lock:
        for e in entries:
            failed_at = provider_health.to_monotonic(e["until"]) - DOWN_SECONDS
            _down[e["provider"]] = max(_down.get(e["provider"], -1e9), failed_at)


provider_health.register(HEALTH_KIND, _restore)


def display_name(model: str) -> str:
    """gemini-3.8-live -> Gemini 3.8 Live; openai/gpt-realtime-2 -> GPT Realtime 2"""
    return " ".join("GPT" if part == "gpt" else part if part[:1].isdigit() else part.capitalize()
                    for part in model.rsplit("/", 1)[-1].split("-"))


def company(model: str) -> str:
    return COMPANIES.get(model.rsplit("/", 1)[-1].split("-", 1)[0], "the provider")


def is_provider_failure(error: BaseException) -> bool:
    """Would another model get the user an answer? Out of quota counts: the
    next model may be on another key (see same_key)."""
    if isinstance(error, Reconnect):
        return False
    return bool(_FAILURE.search(str(error))) or is_quota_error(str(error)) or isinstance(error, (OSError, TimeoutError))


def ended_reason(error: BaseException) -> str:
    """Why a live session ended, in a few words for the user (core/retry.py
    classify_live), or "" when nothing more is known."""
    failure = retry.classify_live(error)
    code = retry.close_code(error)
    if code in retry.TRANSIENT_CLOSE:
        return f"Gemini closed it: {code} {retry.TRANSIENT_CLOSE[code]}"
    return {
        retry.PERMANENT: f"Gemini refused it: {code}" if code else "the provider refused the request",
        retry.TIMEOUT: "the provider did not answer in time",
        retry.TRANSPORT: "the network connection broke",
        retry.NOT_SENT: "the provider could not be reached",
        retry.SERVER: "the provider failed on its side",
    }.get(failure.kind, "")


def via_gateway(model: str) -> bool:
    """"openai/gpt-realtime-2" is a Vercel AI Gateway id; "gemini-3.8-live" is Google's own."""
    return "/" in model


def key_path(config, model: str) -> Path:
    return Path(config.vercel_gateway_api_key_path if via_gateway(model) else config.gemini_api_key_path)


def configured(config) -> list[str]:
    """Every model of the chain, in order, whatever its state."""
    return list(dict.fromkeys([config.gemini_model, *config.gemini_fallback_models]))


def is_down(model: str) -> bool:
    with _lock:
        return _down.get(model, -1e9) + DOWN_SECONDS > time.monotonic()


def chain(config) -> list[str]:
    """Models to try, in order: those whose key is on this machine, skipping
    ones that failed recently (all of them if every one did)."""
    models = [m for m in configured(config) if key_path(config, m).is_file()] or configured(config)
    up = [m for m in models if not is_down(m)]
    return up or models


def mark_down(model: str, error: BaseException) -> None:
    with _lock:
        _down[model] = time.monotonic()
    provider_health.record(HEALTH_KIND, model, "down", time.time() + DOWN_SECONDS, str(error))
    log.warning("Voice model failed on the provider side, skipping it for %ds: model=%s error=%s",
                DOWN_SECONDS, model, " ".join(str(error).split())[:200])


def next_index(models: list[str], index: int, error: BaseException) -> int | None:
    """The model to go on with after models[index] failed. Out of quota
    skips the rest of that key too."""
    for later in range(index + 1, len(models)):
        if is_quota_error(str(error)) and via_gateway(models[later]) == via_gateway(models[index]):
            continue
        return later
    return None


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
            "fallback-failed": FAILED, "working": WORKING}


def ack_texts() -> dict[str, str]:
    """{clip stem: text} of the user's own acknowledgement (a learned rule
    quoting it, core/outbox.py ack_phrase), rendered like the other clips."""
    from ..core import outbox
    phrase = outbox.ack_phrase()
    return {outbox.ack_stem(phrase): phrase} if phrase else {}


def ack_clip():
    """The acknowledgement clip: the user's own words when a rule quotes them
    and they were rendered, else "On it, one moment." in their language."""
    from ..core import outbox
    phrase = outbox.ack_phrase()
    if phrase:
        found = alert_clips.find([outbox.ack_stem(phrase)], outbox.phrase_language(phrase))
        if found is not None:
            return found
    return alert_clips.find(["working"])


_ack_pcm: tuple[str, bytes] = ("", b"")


def ack_pcm(rate: int = 24000) -> bytes:
    """The acknowledgement as mono s16 PCM for the conversation's own audio
    queue (decoded once per clip, so it plays within the ack deadline)."""
    global _ack_pcm
    path = ack_clip()
    if path is None:
        log.error("No acknowledgement clip")
        return b""
    if _ack_pcm[0] == str(path) and _ack_pcm[1]:
        return _ack_pcm[1]
    data = _decode(path, rate)
    if data:
        _ack_pcm = (str(path), data)
    return data


def clip(kind: str, model: str | None = None):
    """The notice in the user's language, else English; a "switching" clip
    for this model before the generic one."""
    stems = [f"fallback-{kind}-{model}", f"fallback-{kind}"] if kind == "switching" and model else [f"fallback-{kind}"]
    if kind == "working":
        stems = ["working"]
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
    out = _decode(path, rate)
    if out:
        log.info("Fallback notice sent to the call: %s", path.name)
    return out


def _decode(path, rate: int) -> bytes:
    try:
        return subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-f", "s16le", "-ac", "1",
                               "-ar", str(rate), "-"], capture_output=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        log.exception("Could not decode %s", path)
        return b""


STALLED = ("[Turn recovery] The voice connection stopped answering the user and was restarted just now. This is "
           "the same conversation; the user heard nothing for a while and probably repeated themselves. If their "
           "last message is still unanswered, answer or act on it now, and begin with a few spoken words so they "
           "know you are on it. If you cannot tell what they last asked, say in one short sentence, in their "
           "language, that you lost them for a moment and ask them to say it again. Do not greet.")


# Said on the new connection after the old one closed mid-conversation
# (core/retry.py classify_live: 1011, 1006, 1001, a network blip). Audio
# already sent cannot be replayed, so she asks for it again.
DROPPED = ("[Turn recovery] The connection dropped for a moment and was restored just now. This is the same "
           "conversation. In one short sentence, in the language the user was speaking, tell them you lost the "
           "connection for a moment and you are back, and ask them to say that again if their last request is "
           "still unanswered. If an action was already running when it dropped, it may or may not have finished: "
           "say so and do not run it again unless they ask. Do not greet.")


def carry_over(transcript: list[dict], stalled: bool = False, dropped: bool = False,
               conversation: str = "") -> str | None:
    """Context for the fallback model: what was said before the switch.
    `stalled`: the same model on a new connection after it stopped
    answering (gemini_live.StuckTurn); the user has heard nothing.
    `dropped`: the same model on a new connection after the old one closed
    (gemini_live._resume_dropped); she says she is back.
    A session longer than CARRY_OVER_TURNS also gets a summary of the turns
    before them (core/summarize.py): its `conversation`'s, or one made from
    the transcript (text chat has no conversation)."""
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
    if len(turns) > CARRY_OVER_TURNS:
        try:
            from ..core import summarize
            earlier = summarize.carry_over_summary([(str(role), said) for role, said in turns[:-CARRY_OVER_TURNS]],
                                                   conversation, keep_last=CARRY_OVER_TURNS)
        except Exception:  # noqa: BLE001 -- the last turns still go over
            log.warning("carry-over summary unavailable", exc_info=True)
            earlier = ""
        if earlier:
            text = earlier + "\nLast turns (verbatim):\n" + text
    if stalled or dropped:
        return (STALLED if stalled else DROPPED) + ("\nConversation so far:\n" + text if text else "")
    if not text:
        return None
    return ("[Context, not a request] The previous voice model connection failed and you are taking over this "
            "conversation. The user has already been told. Continue naturally; do not greet or apologize. "
            "Conversation so far:\n" + text)
