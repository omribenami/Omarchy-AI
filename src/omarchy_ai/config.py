"""Omarchy AI configuration. Defaults live here; ~/.config/omarchy-ai/config.yaml
overrides them (same merge-over-defaults pattern jarvisd and omavoice use).
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "omarchy-ai"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "omarchy-ai"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "omarchy-ai"

USER_CONFIG_PATH = CONFIG_DIR / "config.yaml"
SOCKET_PATH = RUNTIME_DIR / "omarchy-ai.sock"

# omavoice's key file is reused directly — same OpenAI account/project, no
# reason to duplicate it. If omavoice is ever fully removed, move this to
# ~/.config/omarchy-ai/key instead.
DEFAULT_KEY_PATH = Path("~/.config/omavoice/key").expanduser()

API_URL = "https://api.openai.com/v1/live/sessions"


@dataclass
class Config:
    # Wake word (openWakeWord). See jarvisd's README for tuning notes —
    # same detector, same knobs.
    wake_word: str = "hey_jarvis"
    custom_wake_model_path: str | None = None
    wake_threshold: float = 0.5
    wake_trigger_frames: int = 3
    mic_device: str | None = None

    # gpt-live-1 session.
    api_key_path: str = str(DEFAULT_KEY_PATH)
    live_model: str = "gpt-live-1"
    responses_model: str = "gpt-5"
    voice: str = "marin"
    instructions: str = (
        "You are Omarchy AI, a voice assistant for a Linux desktop called "
        "Omarchy. "
        "Keep responses brief and conversational. Respond specifically to "
        "what the user actually said. You have tools to actually control "
        "the desktop — volume, brightness, workspaces, opening apps, "
        "locking the screen, and more. Use them when the user asks for "
        "something they describe, rather than just talking about it — if "
        "they say 'turn up the volume', call volume_up, don't just "
        "acknowledge it. Several windows can be open at once — actions "
        "like fullscreen or close only affect whichever window is "
        "currently focused, which may not be the one the user means. If "
        "they name a specific app or it's ambiguous which window they "
        "mean, call list_windows and focus_window first rather than "
        "guessing. You can recall what you've already done this "
        "conversation with get_recent_actions (optionally filtered to a "
        "window) — use it before repeating an action or when asked what "
        "you've done. If you're still not sure what the user means after "
        "that, describe_screen lets you actually look at the screen — "
        "it's slower, so only reach for it when genuinely needed, not by "
        "default. For anything not covered by your other specific tools, "
        "search list_commands — it covers Omarchy's full set of bound "
        "commands (app launchers, menus, capture, clipboard, themes, and "
        "more) — then run the exact title it returns with execute_command. "
        "You can also type text and press keys into the focused window "
        "with type_text/press_key (e.g. to type a command into a "
        "terminal or search into a browser) — press_key with 'Return' to "
        "submit/run what was typed, since type_text alone doesn't. "
        "Focusing a window does not focus a specific field inside it — "
        "to type a URL in a browser, first press_key 'l' with "
        "modifiers ['ctrl'] to focus the address bar, then type_text the "
        "URL, then press_key 'Return'. "
        "Briefly confirm what you did after calling a "
        "tool. When they indicate they want to end the conversation "
        "(goodbye, stop, that's all, or similar, in whatever language "
        "they're using), say a brief goodbye and call the "
        "end_conversation tool."
    )

    # How a session ends: the user saying one of these (fuzzy-matched
    # against the live input transcript) hangs up immediately and returns
    # to wake-word listening, same as omavoice's "Q" / stop command.
    exit_phrases: list[str] = field(
        default_factory=lambda: [
            "stop", "bye", "goodbye", "good bye", "finish",
            "end conversation", "that's all", "thats all", "never mind",
        ]
    )
    exit_phrase_score_threshold: float = 82.0

    # Safety cap so a stuck session can't run (and bill) forever if the exit
    # phrase is never heard for some reason.
    max_session_seconds: int = 600

    # Time given to actually speak before the first response is requested —
    # a stopgap for real VAD-based silence detection (see jarvisd's
    # audio.py capture_utterance for the pattern to port over next).
    speak_window_seconds: int = 6

    log_level: str = "INFO"


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> Config:
    data: dict = {}
    if USER_CONFIG_PATH.exists():
        with open(USER_CONFIG_PATH) as f:
            data = yaml.safe_load(f) or {}
    known = set(Config.__dataclass_fields__)
    return Config(**{k: v for k, v in data.items() if k in known})


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, STATE_DIR, RUNTIME_DIR):
        d.mkdir(parents=True, exist_ok=True)
