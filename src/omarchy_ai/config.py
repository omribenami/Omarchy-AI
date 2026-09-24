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
WAKE_MODELS_DIR = CONFIG_DIR / "wake_models"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "omarchy-ai"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "omarchy-ai"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser() / "omarchy-ai"
# Per-window ("tile") terminal output logs — see execution/tile_logs.py.
# Cache, not state: every entry is meant to be ephemeral (one per
# currently-open tracked terminal, deleted when it closes) and safe to
# wipe entirely on every daemon startup (sweep_stale()).
TILE_LOG_DIR = CACHE_DIR / "tile_logs"
# Command-and-directory context from interactive terminals opened by the
# user. Unlike TILE_LOG_DIR these are state: they are intentionally kept
# across a daemon restart so a later voice session can still understand
# what was being worked on.
TERMINAL_CONTEXT_DIR = STATE_DIR / "terminal_context"
# Full transcripts from assistant-opened terminals. Kept briefly after the
# terminal closes or the daemon restarts so a later wake can report whether
# an installation or build finished.
TERMINAL_HISTORY_DIR = STATE_DIR / "terminal_history"

USER_CONFIG_PATH = CONFIG_DIR / "config.yaml"
SOCKET_PATH = RUNTIME_DIR / "omarchy-ai.sock"

# This project's own key file, written 0600 by the settings panel — the
# real setup path for a fresh install (nothing else to know about, no other
# project's config to borrow from).
OMARCHY_KEY_PATH = CONFIG_DIR / "key"
GEMINI_KEY_PATH = CONFIG_DIR / "gemini-key"
VERCEL_GATEWAY_KEY_PATH = CONFIG_DIR / "vercel-ai-gateway-key"
# Where this project started: omavoice's key, reused directly since it's
# the same OpenAI account. Kept as a fallback purely so an install that
# predates the settings panel keeps working without the user having to
# re-enter a key they already had on disk.
LEGACY_KEY_PATH = Path("~/.config/omavoice/key").expanduser()

# MyApi (myapiai.com) integration — see src/omarchy_ai/myapi/. Ed25519
# identity minted by the ASC Quick Connect flow (cli/settings.py's
# connect-myapi), never a bearer token, so it's safe to keep 0600 next to
# the OpenAI key rather than in some separate secrets store. Nested
# subdirectory under CONFIG_DIR, same shape as phone_bridge's own state
# dir.
MYAPI_DIR = CONFIG_DIR / "myapi"
MYAPI_IDENTITY_PATH = MYAPI_DIR / "identity.json"
# Local usage log for the terminal dashboard (src/omarchy_ai/cli/dashboard.py)
# — state, not cache: it's meant to accumulate across restarts, same
# reasoning as conversation_history.jsonl in core/history.py.
MYAPI_USAGE_PATH = STATE_DIR / "myapi_usage.jsonl"


def _default_key_path() -> Path:
    """Prefer this project's own key; fall back to the borrowed omavoice
    one only when we don't have our own yet. Resolved at import time — the
    settings panel restarts the daemon after writing a key, so a fresh
    write is always picked up by the next process."""
    if OMARCHY_KEY_PATH.exists():
        return OMARCHY_KEY_PATH
    if LEGACY_KEY_PATH.exists():
        return LEGACY_KEY_PATH
    return OMARCHY_KEY_PATH


DEFAULT_KEY_PATH = _default_key_path()

API_URL = "https://api.openai.com/v1/live/sessions"


@dataclass
class Config:
    provider: str = "openai"
    gemini_model: str = "gemini-3.8-live"
    # Optional physical mic cap during desktop conversations only. None keeps
    # device/user defaults; restore the previous gain when the session ends.
    gemini_mic_volume_percent: int | None = None
    gemini_api_key_path: str = str(GEMINI_KEY_PATH)
    # Omarchy-ai is a Gateway composition: Jev makes typed agent decisions,
    # GPT-4o mini Transcribe recognizes speech in many languages, TTS-1 is
    # the latency-oriented speech renderer, and the language model only
    # turns approved decisions/tool results into natural replies.
    vercel_gateway_api_key_path: str = str(VERCEL_GATEWAY_KEY_PATH)
    omarchy_jev_model: str = "typesafe-ai/jev"
    omarchy_stt_model: str = "openai/gpt-4o-mini-transcribe"
    omarchy_end_silence_ms: int = 650
    omarchy_max_utterance_seconds: int = 20
    omarchy_tts_model: str = "openai/tts-1"
    # tts-1's voice catalogue is distinct from the Live API's (for example
    # the default Live voice "marin" is not a valid tts-1 voice).
    omarchy_tts_voice: str = "nova"
    omarchy_text_model: str = "google/gemini-3.5-flash-lite"
    # UI choice. Jev remains the structured decision/browser policy; it is
    # never incorrectly used as a free-form text generator.
    omarchy_model_choice: str = "gemini"
    omarchy_vision_model: str = "google/gemini-2.5-flash-lite"
    # Wake word (openWakeWord). See jarvisd's README for tuning notes —
    # same detector, same knobs. Custom-trained models (any number) load
    # simultaneously — anyone whose name is in this list wakes it, not
    # just one fixed phrase. wake_word is now just a label (used in the
    # startup log/greeting), not a lookup key, once custom paths are set.
    wake_word: str = "omachy"
    custom_wake_model_path: str | None = None  # back-compat single-model override
    custom_wake_model_paths: list[str] = field(
        default_factory=lambda: (
            [str(WAKE_MODELS_DIR / "omachy.onnx")]
            if (WAKE_MODELS_DIR / "omachy.onnx").is_file()
            else [str(p) for p in sorted(WAKE_MODELS_DIR.glob("*.onnx"))[:1]]
            if WAKE_MODELS_DIR.is_dir()
            else []
        )
    )
    wake_threshold: float = 0.5
    wake_trigger_frames: int = 3
    mic_device: str | None = None

    # gpt-live-1 session.
    api_key_path: str = str(DEFAULT_KEY_PATH)
    live_model: str = "gpt-live-1"
    responses_model: str = "gpt-5"
    # Real, observed latency: several seconds of silence between tool
    # calls in a multi-step chain (list_commands -> execute_command etc.)
    # — confirmed this is gpt-5's own reasoning time, not local execution
    # (subprocess calls here are all well under 100ms). Same lever
    # omavoice/jarvisd used for codex/claude latency.
    responses_reasoning_effort: str = "low"
    voice: str = "marin"
    instructions: str = (
        "Your name is Omarchy — pronounced 'omachy' (oh-MAH-chee), say it "
        "that way, not letter-by-letter or 'om-AR-chee'. You're the voice "
        "assistant for this Linux desktop, which is also called Omarchy. "
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
        "guessing. If it's still genuinely ambiguous and you need to ask "
        "the user which window they mean, call show_window_labels right "
        "before you ask — scoped to just the ambiguous candidates by app "
        "or title — so a name badge appears on each one and they can "
        "answer by looking at the screen instead of you listing windows "
        "out loud. Call hide_window_labels as soon as you have their "
        "answer, whether or not you then act on it. You can recall what "
        "you've already done this "
        "conversation with get_recent_actions (optionally filtered to a "
        "window) — use it before repeating an action or when asked what "
        "you've done. If you're still not sure what the user means after "
        "that, describe_screen lets you actually look at the screen — "
        "it's slower (it can take several seconds), so only reach for it "
        "when genuinely needed, not by default, and say something brief "
        "like 'let me take a look' right before calling it so the user "
        "knows you're working rather than stalled. If the question is "
        "specifically about a terminal — did a command finish, what was "
        "being worked on — call read_tile_log instead of describe_screen: "
        "it is instant real text. Assistant-opened terminals include full "
        "output; newly opened user terminals also record live output. Older "
        "terminals may have only command/cwd/status context, which is NOT "
        "their screen contents or running output. If live output is missing, "
        "say so and use describe_screen for visible contents. It doesn't replace "
        "describe_screen for other visual UI. If the user says a terminal "
        "job has finished and asks about its result, call "
        "read_tile_log before responding so you can report the actual "
        "result rather than guessing. "
        "For anything not covered by your other specific tools, "
        "search list_commands — it covers Omarchy's full set of bound "
        "commands (app launchers, menus, capture, clipboard, themes, and "
        "more) — then run the exact title it returns with execute_command. "
        "The results are already ranked by relevance — if the top result "
        "clearly matches what the user asked for, use it immediately "
        "rather than searching again with a different word to double-check; "
        "extra searches add real delay the user is waiting through. "
        "You can also type text and press keys into the focused window "
        "with type_text/press_key (e.g. to type a command into a "
        "terminal or search into a browser) — press_key with 'Return' to "
        "submit/run what was typed, since type_text alone doesn't. "
        "Focusing a window does not focus a specific field inside it — "
        "to type a URL in a browser, first press_key 'l' with "
        "modifiers ['ctrl'] to focus the address bar, then type_text the "
        "URL, then press_key 'Return'. "
        "If asked to cast, mirror, or project the screen (e.g. to a TV or "
        "projector), call start_casting — that's the real mechanism, not a "
        "display/monitor command. It takes a few seconds to connect; say "
        "so rather than going silent. If the user named which TV (e.g. "
        "'cast to the living room TV'), pass it as start_casting's target. "
        "If they didn't, and start_casting comes back saying more than one "
        "TV is on the network, don't guess — call list_cast_targets, read "
        "the names out and ask which one they mean, then call "
        "start_casting again with their answer as target. If asked what "
        "TVs are available, call list_cast_targets directly. stop_casting "
        "ends casting. If asked to set up or add a new TV that hasn't been "
        "cast to before, call install_receiver_on_tv — walk through its "
        "guidance step by step out loud rather than assuming what's on "
        "screen, since parts of that setup (enabling Developer options and "
        "Wireless debugging, reading a pairing code) genuinely have to "
        "happen on the TV itself with the user looking at it. "
        "Setting/checking/clearing a reminder — Omarchy's own lightweight "
        "notification-popup reminders — use set_reminder/list_reminders/"
        "clear_reminders, not run_omarchy_command. set_reminder only "
        "takes minutes from now, so convert whatever time the user gave "
        "('in 20 minutes', 'in an hour', 'at 3pm') into a minute count "
        "yourself first. "
        "For other desktop customization — changing the theme, toggling "
        "night light, moving a bar widget, taking a screenshot/recording "
        "— use run_omarchy_command with the matching omarchy CLI args "
        "(e.g. ['theme', 'set', 'catppuccin']); nightlight_toggle is its "
        "own dedicated tool, prefer it over run_omarchy_command for that "
        "one case. Only theme/toggle/reminder/bar/capture actually run "
        "through it. That restriction applies only to "
        "run_omarchy_command, not to an explicit request to run a local "
        "installer in a terminal. When the user explicitly asks you to "
        "run an installer, package command, update, or local script, "
        "inspect the local script with read_file first, briefly say what "
        "it will change, then open a terminal and run it there. Use the "
        "exact unique terminal title returned by open_terminal when you "
        "focus it and read its log, so you never send the command to a "
        "different terminal. If it prompts for sudo and persistent Sudo "
        "Access is enabled in Assistant Settings, that is the user's explicit "
        "authorization to satisfy terminal sudo prompts with their saved "
        "credential. After confirming the exact terminal log contains the "
        "sudo prompt, you MUST call submit_sudo_password and continue the "
        "requested task; do not ask the user to type it. If Sudo Access is "
        "disabled, leave the focused terminal for the user to enter it. "
        "submit_sudo_password uses the GNOME Keyring credential without "
        "revealing the password to you. Never ask for, repeat, or type a "
        "password yourself. The same tool works in the user's own terminal "
        "windows (focus it first), and when the user asks you to use their "
        "saved or sudo password for another prompt (ssh, scp, su -- e.g. 'same "
        "password as here'), do it: confirm the prompt in that window's log, "
        "call submit_sudo_password, then read the log again to see if it was "
        "accepted. Do not tell the user you cannot. "
        "If you type shell commands into a terminal: this machine runs "
        "Omarchy, an Arch-based Linux distro — package commands are "
        "'pacman -S <package>' (official repos) or 'yay -S <package>' "
        "(AUR), never apt/apt-get/dnf/yum/zypper/brew, those don't exist "
        "here. "
        "If they give you a standing correction or preference about how "
        "you should behave going forward — not just for this "
        "conversation — call remember_preference so you keep doing it in "
        "future conversations too. "
        "Talk while you work, not only before or after: for any tool call "
        "that isn't instant, say a brief present-tense line of what "
        "you're about to do — e.g. 'Sure, doing that now' — right as you "
        "call it, in the same turn, so the user hears you working during "
        "the wait instead of silence. That announces intent, not a "
        "result, so it's fine to say before the tool call returns — keep "
        "it separate from claiming the action is DONE. Never say that you "
        "have done, opened, changed, or demonstrated a visible desktop "
        "action, or describe what is now on screen, until its tool result "
        "has actually returned successfully. Do not narrate a future "
        "batch of actions. For a capability demo, do exactly one visible "
        "action at a time: call its tool first, wait for the result, "
        "briefly say what the user can now see, then begin the next "
        "action. "
        "Briefly confirm what you did after calling a tool. "
        "Ending the conversation: when they indicate they want to stop — "
        "goodbye, stop, that's all, or similar — in ANY language, "
        "including languages other than English (e.g. Hebrew), this is "
        "not optional: say a brief goodbye in the language they were "
        "using and then you MUST call the end_conversation tool in that "
        "same turn. Don't just reply with a farewell and wait — actually "
        "call the tool, every time, regardless of what language the "
        "conversation has been in."
    )

    # How a session ends: the user saying one of these (fuzzy-matched
    # against the live input transcript) hangs up immediately and returns
    # to wake-word listening, same as omavoice's "Q" / stop command.
    # English-only for a long time — real gap, confirmed live: a Hebrew
    # "stop"/goodbye never matched any of these, so the conversation never
    # hung up on its own. fuzz.ratio (used to score these) is a plain
    # edit-distance comparison, language-agnostic at the algorithm level;
    # the list itself just needs real words in the languages actually
    # spoken here. Hebrew added as the concrete case in hand — extend with
    # more languages the same way if they come up, rather than trying to
    # build a fully general solution up front.
    exit_phrases: list[str] = field(
        default_factory=lambda: [
            "stop", "bye", "goodbye", "good bye", "finish",
            "end conversation", "that's all", "thats all", "never mind",
            # Hebrew: תפסיק/תפסיקי (stop, m/f), מספיק (enough), סיימנו
            # (we're done), ביי (bye, common loanword), להתראות (goodbye),
            # זהו (that's it).
            "תפסיק", "תפסיקי", "מספיק", "סיימנו", "ביי", "להתראות", "זהו",
        ]
    )
    exit_phrase_score_threshold: float = 82.0

    # Safety cap so a stuck session can't run (and bill) forever if the exit
    # phrase is never heard for some reason.
    max_session_seconds: int = 600

    # Time given to actually speak before the first response is requested —
    # a stopgap for real VAD-based silence detection (see jarvisd's
    # audio.py capture_utterance for the pattern to port over next).
    speak_window_seconds: int = 4

    # Cross-session conversation history — how far back to recall in a new
    # session's instructions, and a hard cap on how much of it to include
    # (this is a live, latency-sensitive voice prompt, not a place for an
    # ever-growing verbatim transcript).
    context_retention_hours: float = 24.0
    context_max_chars: int = 4000

    # Heartbeat for scheduled tasks/watches (core/agenda.py). The tick only
    # starts jobs that are due; with no tasks it costs a file read. Jev is
    # only called for due watches whose observed text changed.
    # Jev decides simple desktop commands (workspace switch, move window,
    # volume, play/pause, fullscreen) from the transcript the moment the
    # user pauses, instead of waiting for Gemini's turn (voice/jev_fast.py).
    jev_fast_path: bool = True

    # Task Runtime (src/omarchy_ai/runtime/, docs/ADR-0002-task-runtime.md):
    # multi-step tasks routed by Jev to the System agent, direct tools,
    # Claude Code or Codex. Commands up to task_auto_approve run without
    # asking (LOW, NORMAL or ELEVATED; HIGH always asks, BLOCKED never runs).
    task_runtime_enabled: bool = True
    task_auto_approve: str = "NORMAL"
    # Worker text model (through the Gateway) for the System agent, planner,
    # direct-tool picker and internal reviewer. A multi-step tool loop needs
    # a stronger model than the latency-tuned omarchy_text_model; None falls
    # back to that one.
    task_agent_model: str | None = "anthropic/claude-sonnet-5"
    task_max_steps: int = 12
    task_max_minutes: int = 60
    task_coding_agent_timeout: int = 1200
    heartbeat_enabled: bool = True
    heartbeat_seconds: int = 60

    log_level: str = "INFO"

    # Watch Dogs overlay (the hacking-HUD Quickshell panel — see
    # src/omarchy_ai/voice/watchdog.py and
    # ~/.config/omarchy/plugins/omarchy-ai.watchdog/). Settable from the
    # omarchy-ai.settings bar panel; the daemon only reads this at startup
    # (LiveSession.run()'s watchdog.start() call site), so a change needs a
    # daemon restart to take effect — see settings.py's restart discipline.
    watchdog_enabled: bool = True
    sudo_access_enabled: bool = False
    # "feed" (tool-call/state text feed, the original design), "visualizer"
    # (ASCII/unicode amplitude bars while speaking), or "both". Selected via
    # the settings panel, sent to the plugin as part of watchdog.start()'s
    # payload each session.
    watchdog_display_mode: str = "visualizer"

    # Phone bridge (src/omarchy_ai/phone/server.py) — a local HTTP server
    # letting a phone on the same LAN open a live conversation from a
    # browser page (WebRTC direct to OpenAI; this server only relays the
    # SDP offer/answer and executes tool calls, no audio passes through
    # Python). User's own request, explicitly framed as a beta: no
    # pairing/auth yet (that's planned as a later QR-code-through-the-PC
    # step) — while enabled, ANYONE who can reach this machine on the LAN
    # can open a conversation and drive the desktop through it. Default
    # off for that reason; this repo's own instance has it on for the
    # beta test itself, via config.yaml, not this source default.
    phone_bridge_enabled: bool = False
    phone_bridge_port: int = 8766

    # Local file tools are intentionally limited to user-owned locations.
    # Add an explicit path here when a project lives elsewhere; paths are
    # resolved before every operation so symlinks cannot escape a root.
    file_access_roots: list[str] = field(
        default_factory=lambda: [str(Path.home()), "/tmp"]
    )

    # MyApi (myapiai.com) — connect state itself lives in
    # MYAPI_IDENTITY_PATH (connect-myapi/disconnect-myapi in cli/settings.py
    # write/delete it), not here. This is the master on/off switch: it's
    # what makes the separate omarchy-ai.myapi bar icon/panel appear at
    # all (hidden by default — opt-in, same "off until asked for"
    # reasoning as phone_bridge_enabled above) and what gates the MyApi
    # tools/instructions being exposed to the model even once connected.
    myapi_enabled: bool = False


# Appended to Config.instructions at session-build time, only when MyApi is
# actually connected (see voice/live.py's build_session_config) — same
# "prefer the fast structured path over a vision call, but the vision path
# still covers what it doesn't" shape as the read_tile_log/describe_screen
# guidance above, just one level up: a connected service instead of the
# browser.
MYAPI_INSTRUCTIONS = (
    "If a request can be answered through a connected MyApi service — "
    "email, calendar, files, Notion, Slack, and whatever else is "
    "connected — prefer that over opening a browser and using "
    "describe_screen: call myapi_list_services if you're not sure what's "
    "connected, myapi_service_methods before a service's first use, then "
    "myapi_call to actually make the request. It's structured data, no "
    "vision call needed, and much faster and cheaper than a screenshot. "
    "For Gmail attachments, use myapi_gmail_search_attachments to find "
    "the message and attachment IDs, then myapi_gmail_download_attachment "
    "to save it in Downloads/Omarchy_AI. myapi_call can only read (GET) "
    "for now, not send/create/delete "
    "anything — if asked to do something that would need that, say "
    "plainly you can't do that yet rather than trying. Only fall back to "
    "the browser/describe_screen path when nothing connected covers the "
    "request."
)


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
    for d in (CONFIG_DIR, STATE_DIR, RUNTIME_DIR, TILE_LOG_DIR, TERMINAL_CONTEXT_DIR, TERMINAL_HISTORY_DIR):
        d.mkdir(parents=True, exist_ok=True)
